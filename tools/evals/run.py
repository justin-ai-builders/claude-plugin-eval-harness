#!/usr/bin/env python3
"""Behavior-eval runner for plugins in this marketplace.

Runs a JSON test suite against a plugin by driving the Claude Code CLI headless
(`claude -p`) with the plugin loaded via --plugin-dir. Each test runs in a
temporary copy of a fixture workspace, so file-writing skills can be asserted on
without touching committed fixtures. See docs/plugin-evals.md for the full
convention (suite schema, assertion reference, isolation model).

Usage:
  python3 tools/evals/run.py \
      --plugin-dir plugins/hello-world \
      --suite plugins/hello-world/evals/suites/core_smoke.json \
      --out-json out.json --out-md out.md

Prefer the wrapper: tools/evals/run.sh <plugin-name>
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Match complete double-quoted spans, including newlines and escaped characters.
# The old bounded, single-line pattern let long or multiline quotations bypass
# max_quoted_words entirely.
QUOTE_RE = re.compile(r'"((?:\\.|[^"\\])*)"', re.DOTALL)

EMPTY_MCP_CONFIG = '{"mcpServers": {}}'

# Per-test claude_args may only tune MCP and tool access. Everything else
# (--plugin-dir, --output-format, --permission-mode, --resume, --model, ...)
# is owned by the runner; letting a suite override those would let test data
# silently change what is being evaluated.
CLAUDE_ARGS_ALLOWED = {
    "--mcp-config": True,        # True = takes a value
    "--strict-mcp-config": False,
    "--allowedTools": True,
    "--disallowedTools": True,
}

RESPONSE_ASSERTIONS = {
    "contains", "not_contains", "contains_any", "contains_all",
    "regex", "not_regex", "min_words", "max_words", "between_words",
    "max_quoted_words", "quotes_require_source", "llm_judge",
}
FILE_ASSERTIONS = {
    "file_exists", "file_not_exists", "file_contains", "file_not_contains",
    "file_regex", "file_unchanged",
}
KNOWN_ASSERTIONS = RESPONSE_ASSERTIONS | FILE_ASSERTIONS


@dataclass
class AssertionResult:
    passed: bool
    message: str
    advisory: bool = False  # advisory failures are recorded as warnings, never fail the test


@dataclass
class TestResult:
    test_id: str
    description: str
    tags: List[str]
    passed: bool
    duration_ms: int
    failures: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    response: str = ""
    raw_output: str = ""
    conversation_id: Optional[str] = None
    error: Optional[str] = None
    skipped_dependency: bool = False
    cost_usd: Optional[float] = None
    workspace: Optional[str] = None


class AdapterError(RuntimeError):
    pass


class SuiteError(RuntimeError):
    """Suite definition problem — reported before any model call is made."""


def clean_claude_env() -> Dict[str, str]:
    """Strip session-scoped ANTHROPIC_*/CLAUDE_* vars so nested `claude` calls
    (the adapter and llm_judge) use the machine's stored login, not the
    credentials of a Claude Code session this runner may be spawned from —
    otherwise nested calls 401."""
    return {k: v for k, v in os.environ.items()
            if not k.startswith(("ANTHROPIC", "CLAUDE"))}


def parse_last_json(text: str) -> Optional[Dict[str, Any]]:
    for line in reversed([ln.strip() for ln in text.splitlines() if ln.strip()]):
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None


class Adapter:
    def send(
        self,
        message: str,
        conversation_id: Optional[str],
        cwd: Path,
        extra_args: List[str],
        timeout_sec: int,
    ) -> Tuple[str, Optional[str], str, Optional[float]]:
        """Returns (response, conversation_id, raw_output, cost_usd)."""
        raise NotImplementedError


class ClaudeAdapter(Adapter):
    """Drives the Claude Code CLI headless with the plugin under test loaded.

    Every call re-passes --plugin-dir and runs in the test's temp workspace, so
    resumed turns keep both the plugin and the working directory. Isolation
    flags (MCP config, setting sources, tool allowlist) are composed per test
    by the runner and arrive via extra_args.
    """

    def __init__(self, plugin_dir: Path, model: Optional[str] = None) -> None:
        plugin_dir = plugin_dir.resolve()
        if not (plugin_dir / ".claude-plugin" / "plugin.json").is_file():
            raise SuiteError(f"--plugin-dir has no .claude-plugin/plugin.json: {plugin_dir}")
        self.plugin_dir = plugin_dir
        self.model = model

    def send(self, message, conversation_id, cwd, extra_args, timeout_sec):
        cmd = [
            "claude", "-p", message,
            "--plugin-dir", str(self.plugin_dir),
            "--output-format", "json",
        ]
        if self.model:
            cmd += ["--model", self.model]
        if conversation_id:
            cmd += ["--resume", conversation_id]
        cmd += list(extra_args)
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            env=clean_claude_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
        output = proc.stdout or ""
        payload = parse_last_json(output)
        if payload is None:
            raise AdapterError(
                f"claude -p produced no parseable JSON (exit {proc.returncode}). "
                "Output:\n" + output[-3000:]
            )
        if payload.get("is_error"):
            raise AdapterError(
                f"claude -p reported an error (subtype={payload.get('subtype')}): "
                + str(payload.get("result", ""))[:1000]
            )
        response = payload.get("result", "")
        if not isinstance(response, str):
            response = str(response)
        session_id = payload.get("session_id") or conversation_id
        cost = payload.get("total_cost_usd")
        return response, session_id, output, cost


class CommandAdapter(Adapter):
    """Generic adapter for other CLIs.

    command_template placeholders:
      {message}           - shell-quoted prompt
      {conversation_id}   - shell-quoted conversation id (or empty string)
    """

    def __init__(self, command_template: str, response_format: str = "text") -> None:
        self.command_template = command_template
        if response_format not in {"text", "json"}:
            raise SuiteError("--command-response-format must be 'text' or 'json'")
        self.response_format = response_format

    def send(self, message, conversation_id, cwd, extra_args, timeout_sec):
        cmd = self.command_template.format(
            message=shlex.quote(message),
            conversation_id=shlex.quote(conversation_id or ""),
        )
        proc = subprocess.run(
            cmd,
            shell=True,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
        output = proc.stdout or ""
        if proc.returncode != 0:
            raise AdapterError(
                f"Command adapter exited with status {proc.returncode}. "
                f"Output:\n{output[-3000:]}"
            )
        if self.response_format == "text":
            return output.strip(), conversation_id, output, None
        payload = parse_last_json(output)
        if payload is None:
            raise AdapterError(
                "Could not parse JSON output for command adapter. Expected a last "
                'JSON line like {"response": "...", "conversation_id": "..."}\n'
                f"Raw output:\n{output[-3000:]}"
            )
        response = payload.get("response", "")
        if not isinstance(response, str):
            response = str(response)
        return response, payload.get("conversation_id") or conversation_id, output, None


# ---------------------------------------------------------------------------
# Workspace fixtures


def relativize(path: Any) -> Optional[str]:
    """Report paths relative to cwd where possible — committed baselines and
    PR-posted reports shouldn't embed a developer's home directory."""
    if path is None:
        return None
    p = Path(path).resolve()
    try:
        return str(p.relative_to(Path.cwd()))
    except ValueError:
        return str(p)


def hash_tree(root: Path) -> Dict[str, str]:
    manifest: Dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and not p.is_symlink():
            rel = p.relative_to(root).as_posix()
            manifest[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return manifest


def create_workspace(fixture: Optional[Path], label: str) -> Tuple[Path, Dict[str, str]]:
    tmp = Path(tempfile.mkdtemp(prefix=f"plugineval_{label}_"))
    if fixture is not None:
        shutil.copytree(fixture, tmp, dirs_exist_ok=True)
    return tmp, hash_tree(tmp)


# ---------------------------------------------------------------------------
# Assertions


def word_count(text: str) -> int:
    return len(re.findall(r"\S+", text))


def get_quotes(text: str) -> List[str]:
    return [m.group(1).strip() for m in QUOTE_RE.finditer(text)]


def run_llm_judge(rubric: str, response: str, model: str = "haiku", timeout_sec: int = 120) -> Tuple[bool, str]:
    """Model-graded check via `claude -p`. Bills the runner's own Claude login.
    Called ONCE per assertion — verdicts are never retried, and a judge
    infrastructure error never re-runs the model under test."""
    prompt = (
        "You are a strict, literal test judge for an AI assistant's response.\n"
        "Judge ONLY against the rubric below. Do not reward verbosity, politeness,\n"
        "or effort — only whether the rubric's condition is actually met.\n\n"
        f"<rubric>\n{rubric}\n</rubric>\n\n"
        f"<response_under_test>\n{response}\n</response_under_test>\n\n"
        "Reply with ONLY this JSON on a single line, nothing else:\n"
        '{"pass": true, "reason": "<25 words max>"} or {"pass": false, "reason": "<25 words max>"}'
    )
    proc = subprocess.run(
        ["claude", "-p", "--model", model],
        input=prompt,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout_sec,
        check=False,
        env=clean_claude_env(),
    )
    verdict = parse_last_json(proc.stdout or "")
    if verdict is None:
        raise AdapterError(
            f"llm_judge: could not parse a verdict. "
            f"stderr={(proc.stderr or '').strip()[:200]!r} stdout={(proc.stdout or '')[:300]!r}"
        )
    return bool(verdict.get("pass")), str(verdict.get("reason", ""))


def matches_workspace_glob(relative_path: str, pattern: str) -> bool:
    """Match a POSIX-style relative path using segment-aware glob semantics.

    `*`, `?`, and character classes match within one path segment. `**`
    matches zero or more complete segments, so `**/*.md` includes root-level
    Markdown while `*.md` does not include nested files. The same matcher is
    used for baseline and current snapshots.
    """
    path_parts = tuple(part for part in relative_path.replace("\\", "/").split("/") if part)
    pattern_parts = tuple(part for part in pattern.replace("\\", "/").split("/") if part)
    memo: Dict[Tuple[int, int], bool] = {}

    def match(pattern_index: int, path_index: int) -> bool:
        key = (pattern_index, path_index)
        if key in memo:
            return memo[key]
        if pattern_index == len(pattern_parts):
            result = path_index == len(path_parts)
        elif pattern_parts[pattern_index] == "**":
            result = match(pattern_index + 1, path_index) or (
                path_index < len(path_parts) and match(pattern_index, path_index + 1)
            )
        else:
            result = (
                path_index < len(path_parts)
                and fnmatch.fnmatchcase(path_parts[path_index], pattern_parts[pattern_index])
                and match(pattern_index + 1, path_index + 1)
            )
        memo[key] = result
        return result

    return match(0, 0)


def match_workspace_files(workspace: Path, pattern: str) -> Tuple[List[Path], Optional[str]]:
    """Resolve a workspace-relative path or glob to regular files.

    Returns (files, error). Errors: absolute or '..' paths, paths that resolve
    outside the workspace (symlink escape), or an explicit path that is a
    directory where a file is required.
    """
    parts = Path(pattern).parts
    if Path(pattern).is_absolute() or ".." in parts:
        return [], f"path must be workspace-relative without '..': {pattern!r}"
    root = workspace.resolve()
    is_glob = any(c in pattern for c in "*?[")
    if is_glob:
        candidates = sorted(
            candidate for candidate in workspace.rglob("*")
            if matches_workspace_glob(candidate.relative_to(workspace).as_posix(), pattern)
        )
    else:
        target = workspace / pattern
        if target.is_dir():
            return [], f"{pattern!r} is a directory, expected a file"
        candidates = [target] if target.exists() else []
    files: List[Path] = []
    for c in candidates:
        resolved = c.resolve()
        if resolved != root and root not in resolved.parents:
            return [], f"{pattern!r} resolves outside the workspace: {resolved}"
        if c.is_file():
            files.append(c)
    return files, None


def evaluate_file_assertion(
    assertion: Dict[str, Any],
    workspace: Optional[Path],
    baseline_hashes: Optional[Dict[str, str]],
) -> AssertionResult:
    atype = assertion["type"]
    pattern = assertion.get("path", "")
    if workspace is None:
        return AssertionResult(False, f"{atype}: no workspace for this test")

    if atype == "file_unchanged":
        if baseline_hashes is None:
            return AssertionResult(False, "file_unchanged: no baseline hash manifest")
        matched = [rel for rel in baseline_hashes if matches_workspace_glob(rel, pattern)]
        current, err = match_workspace_files(workspace, pattern)
        if err:
            return AssertionResult(False, f"file_unchanged {pattern!r}: {err}")
        problems: List[str] = []
        for rel in matched:
            f = workspace / rel
            if not f.is_file():
                problems.append(f"deleted: {rel}")
            elif hashlib.sha256(f.read_bytes()).hexdigest() != baseline_hashes[rel]:
                problems.append(f"modified: {rel}")
        for f in current:
            rel = f.resolve().relative_to(workspace.resolve()).as_posix()
            if rel not in baseline_hashes:
                problems.append(f"created: {rel}")
        ok = not problems
        msg = f"file_unchanged {pattern!r} ({len(matched)} baseline file(s))"
        if problems:
            msg += f" violations={problems}"
        return AssertionResult(ok, msg)

    files, err = match_workspace_files(workspace, pattern)
    if err:
        return AssertionResult(False, f"{atype} {pattern!r}: {err}")

    if atype == "file_exists":
        return AssertionResult(bool(files), f"file_exists {pattern!r}"
                               + ("" if files else " (no matching file)"))

    if atype == "file_not_exists":
        extra = f" (found: {[str(f.relative_to(workspace)) for f in files]})" if files else ""
        return AssertionResult(not files, f"file_not_exists {pattern!r}{extra}")

    # Content assertions: require at least one matching file to exist —
    # a vacuous pass on zero matches would hide a renamed or missing fixture.
    if not files:
        return AssertionResult(False, f"{atype} {pattern!r} (no matching file)")
    texts = {f: f.read_text(errors="replace") for f in files}

    if atype == "file_contains":
        needle = assertion.get("value", "")
        ok = any(needle in t for t in texts.values())
        return AssertionResult(ok, f"file_contains {pattern!r} {needle!r}")

    if atype == "file_not_contains":
        needle = assertion.get("value", "")
        offenders = [str(f.relative_to(workspace)) for f, t in texts.items() if needle in t]
        ok = not offenders
        msg = f"file_not_contains {pattern!r} {needle!r}"
        if offenders:
            msg += f" found_in={offenders}"
        return AssertionResult(ok, msg)

    if atype == "file_regex":
        rx = assertion.get("value", "")
        ok = any(re.search(rx, t, re.MULTILINE | re.DOTALL) for t in texts.values())
        return AssertionResult(ok, f"file_regex {pattern!r} /{rx}/")

    return AssertionResult(False, f"Unknown file assertion type '{atype}'")


def evaluate_assertion(
    assertion: Dict[str, Any],
    response: str,
    workspace: Optional[Path] = None,
    baseline_hashes: Optional[Dict[str, str]] = None,
) -> AssertionResult:
    atype = assertion.get("type")
    if atype is None:
        return AssertionResult(False, "Assertion missing 'type'")

    if atype in FILE_ASSERTIONS:
        return evaluate_file_assertion(assertion, workspace, baseline_hashes)

    if atype == "contains":
        needle = assertion.get("value", "")
        return AssertionResult(needle in response, f"contains '{needle}'")

    if atype == "not_contains":
        needle = assertion.get("value", "")
        return AssertionResult(needle not in response, f"not_contains '{needle}'")

    if atype == "contains_any":
        vals = assertion.get("values", [])
        return AssertionResult(any(v in response for v in vals), f"contains_any {vals}")

    if atype == "contains_all":
        vals = assertion.get("values", [])
        missing = [v for v in vals if v not in response]
        msg = "contains_all" + (f" missing={missing}" if missing else "")
        return AssertionResult(not missing, msg)

    if atype == "regex":
        pattern = assertion.get("value", "")
        ok = re.search(pattern, response, re.MULTILINE | re.DOTALL) is not None
        return AssertionResult(ok, f"regex /{pattern}/")

    if atype == "not_regex":
        pattern = assertion.get("value", "")
        ok = re.search(pattern, response, re.MULTILINE | re.DOTALL) is None
        return AssertionResult(ok, f"not_regex /{pattern}/")

    if atype == "min_words":
        minimum = int(assertion.get("value", 0))
        actual = word_count(response)
        return AssertionResult(actual >= minimum, f"min_words {minimum}, got {actual}")

    if atype == "max_words":
        maximum = int(assertion.get("value", 10**9))
        actual = word_count(response)
        return AssertionResult(actual <= maximum, f"max_words {maximum}, got {actual}")

    if atype == "between_words":
        minimum = int(assertion.get("min", 0))
        maximum = int(assertion.get("max", 10**9))
        actual = word_count(response)
        return AssertionResult(minimum <= actual <= maximum,
                               f"between_words {minimum}-{maximum}, got {actual}")

    if atype == "max_quoted_words":
        maximum = int(assertion.get("value", 25))
        over = [(word_count(q), q[:80]) for q in get_quotes(response) if word_count(q) > maximum]
        msg = f"max_quoted_words {maximum}"
        if over:
            msg += f", violating_quotes={over}"
        return AssertionResult(not over, msg)

    if atype == "quotes_require_source":
        source_regex = assertion.get("source_regex", r"\[source:[^\]]+\]")
        if '"' not in response:
            return AssertionResult(True, "quotes_require_source (no quotes present)")
        has_source = re.search(source_regex, response, re.IGNORECASE) is not None
        return AssertionResult(has_source, f"quotes_require_source regex /{source_regex}/")

    if atype == "llm_judge":
        rubric = assertion.get("rubric", "")
        model = assertion.get("model", "haiku")
        advisory = bool(assertion.get("advisory", False))
        tag = f"llm_judge[{model}{', advisory' if advisory else ''}]"
        try:
            ok, reason = run_llm_judge(rubric, response, model=model)
        except Exception as exc:  # noqa: BLE001 — judge infra failure, not a verdict
            return AssertionResult(False, f"{tag} judge error: {exc}", advisory=advisory)
        return AssertionResult(ok, f"{tag} {reason or rubric[:60]}", advisory=advisory)

    return AssertionResult(False, f"Unknown assertion type '{atype}'")


# ---------------------------------------------------------------------------
# Suite loading & validation (all of it before the first model call)


@dataclass
class PreparedTest:
    test_id: str
    description: str
    tags: List[str]
    message: str
    conversation_group: Optional[str]  # None = fresh conversation
    fixture: Optional[Path]
    extra_args: List[str]
    timeout_sec: Optional[int]
    assertions: List[Dict[str, Any]]


def validate_claude_args(raw: List[Any], suite_dir: Path, test_id: str, errors: List[str]) -> List[str]:
    out: List[str] = []
    expecting_value_for: Optional[str] = None
    for token in raw:
        token = str(token)
        if expecting_value_for:
            if expecting_value_for == "--mcp-config" and not token.lstrip().startswith("{"):
                token = str((suite_dir / token).resolve())
                if not Path(token).is_file():
                    errors.append(f"{test_id}: claude_args --mcp-config file not found: {token}")
            out.append(token)
            expecting_value_for = None
            continue
        if token.startswith("-"):
            if token not in CLAUDE_ARGS_ALLOWED:
                errors.append(
                    f"{test_id}: claude_args flag {token!r} not allowed "
                    f"(allowed: {sorted(CLAUDE_ARGS_ALLOWED)})"
                )
                continue
            out.append(token)
            if CLAUDE_ARGS_ALLOWED[token]:
                expecting_value_for = token
        else:
            errors.append(f"{test_id}: claude_args value {token!r} without a preceding flag")
    if expecting_value_for:
        errors.append(f"{test_id}: claude_args flag {expecting_value_for!r} missing its value")
    return out


def validate_assertions(assertions: List[Any], test_id: str, errors: List[str]) -> None:
    if not isinstance(assertions, list) or not assertions:
        errors.append(f"{test_id}: no assertions")
        return

    def require_string(a: Dict[str, Any], field: str, label: str) -> Optional[str]:
        value = a.get(field)
        if not isinstance(value, str) or not value:
            errors.append(f"{label}: '{field}' must be a non-empty string")
            return None
        return value

    def require_nonnegative_int(a: Dict[str, Any], field: str, label: str) -> Optional[int]:
        value = a.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            errors.append(f"{label}: '{field}' must be a non-negative integer")
            return None
        return value

    for i, a in enumerate(assertions):
        label = f"{test_id} assertion[{i}]"
        if not isinstance(a, dict):
            errors.append(f"{label}: not an object")
            continue
        atype = a.get("type")
        if atype not in KNOWN_ASSERTIONS:
            errors.append(f"{label}: unknown type {atype!r}")
            continue

        if atype in {"contains", "not_contains", "regex", "not_regex",
                     "file_contains", "file_not_contains", "file_regex"}:
            require_string(a, "value", label)

        if atype in {"contains_any", "contains_all"}:
            values = a.get("values")
            if (not isinstance(values, list) or not values
                    or any(not isinstance(value, str) or not value for value in values)):
                errors.append(f"{label}: 'values' must be a non-empty array of non-empty strings")

        if atype in {"regex", "not_regex", "file_regex"}:
            pattern = a.get("value")
            if isinstance(pattern, str) and pattern:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    errors.append(f"{label}: invalid regex: {exc}")

        if atype in {"min_words", "max_words"}:
            require_nonnegative_int(a, "value", label)

        if atype == "between_words":
            minimum = require_nonnegative_int(a, "min", label)
            maximum = require_nonnegative_int(a, "max", label)
            if minimum is not None and maximum is not None and minimum > maximum:
                errors.append(f"{label}: 'min' must be less than or equal to 'max'")

        if atype == "max_quoted_words" and "value" in a:
            require_nonnegative_int(a, "value", label)

        if atype == "quotes_require_source" and "source_regex" in a:
            source_regex = require_string(a, "source_regex", label)
            if source_regex is not None:
                try:
                    re.compile(source_regex)
                except re.error as exc:
                    errors.append(f"{label}: invalid source_regex: {exc}")

        if atype == "llm_judge":
            require_string(a, "rubric", label)
            if "model" in a and (not isinstance(a["model"], str) or not a["model"]):
                errors.append(f"{label}: 'model' must be a non-empty string")
            if "advisory" in a and not isinstance(a["advisory"], bool):
                errors.append(f"{label}: 'advisory' must be a boolean")

        if atype in FILE_ASSERTIONS:
            p = require_string(a, "path", label)
            if p is not None and (Path(p).is_absolute() or ".." in Path(p).parts):
                errors.append(f"{label}: path must be workspace-relative without '..': {p!r}")


def prepare_suite(suite: Dict[str, Any], suite_path: Path, args: argparse.Namespace) -> List[PreparedTest]:
    suite_dir = suite_path.parent
    defaults = suite.get("defaults", {})
    errors: List[str] = []
    prepared: List[PreparedTest] = []
    seen_ids: set = set()

    for idx, test in enumerate(suite["tests"], start=1):
        test_id = str(test.get("id", f"test_{idx}"))
        if test_id in seen_ids:
            errors.append(f"duplicate test id: {test_id}")
        seen_ids.add(test_id)

        message = str(test.get("input", ""))
        if not message.strip():
            errors.append(f"{test_id}: empty 'input'")

        conversation_mode = str(test.get("conversation", "new"))
        group = None
        if conversation_mode.startswith("shared:"):
            group = conversation_mode.split(":", 1)[1]
        elif conversation_mode != "new":
            errors.append(f"{test_id}: conversation must be 'new' or 'shared:<group>'")

        fixture = None
        fixture_rel = test.get("workspace_fixture", defaults.get("workspace_fixture"))
        if fixture_rel:
            fixture = (suite_dir / fixture_rel).resolve()
            if not fixture.is_dir():
                errors.append(f"{test_id}: workspace_fixture not found: {fixture}")

        mcp_mode = str(test.get("mcp", defaults.get("mcp", "none")))
        if mcp_mode not in {"none", "user"}:
            errors.append(f"{test_id}: mcp must be 'none' or 'user', got {mcp_mode!r}")

        extra: List[str] = []
        if mcp_mode == "user":
            # Opt-in: the developer's own settings and MCP servers.
            extra += ["--setting-sources", "user,project"]
        else:
            # Isolated by default: no user settings/hooks, no MCP servers.
            extra += ["--setting-sources", "project",
                      "--strict-mcp-config", "--mcp-config", EMPTY_MCP_CONFIG]

        allowed_tools = test.get("allowed_tools", defaults.get("allowed_tools"))
        if allowed_tools:
            extra += ["--allowedTools", ",".join(allowed_tools),
                      "--permission-mode", "dontAsk"]
        else:
            extra += ["--permission-mode", "bypassPermissions"]

        extra += validate_claude_args(list(test.get("claude_args", [])), suite_dir, test_id, errors)

        timeout = test.get("timeout")
        if timeout is not None and (not isinstance(timeout, int) or timeout <= 0):
            errors.append(f"{test_id}: timeout must be a positive integer (seconds)")

        assertions = test.get("assertions", [])
        validate_assertions(assertions, test_id, errors)

        if test.get("skip", False):
            continue
        prepared.append(PreparedTest(
            test_id=test_id,
            description=str(test.get("description", "")).strip(),
            tags=list(test.get("tags", [])),
            message=message,
            conversation_group=group,
            fixture=fixture,
            extra_args=extra,
            timeout_sec=timeout,
            assertions=assertions,
        ))

    if errors:
        raise SuiteError("Suite validation failed:\n  - " + "\n  - ".join(errors))
    return prepared


def select_tests(prepared: List[PreparedTest], args: argparse.Namespace) -> List[PreparedTest]:
    include = set(args.include_tag)
    exclude = set(args.exclude_tag)

    def selected(t: PreparedTest) -> bool:
        tags = set(t.tags)
        if include and tags.isdisjoint(include):
            return False
        if exclude and tags.intersection(exclude):
            return False
        return True

    chosen = [t for t in prepared if selected(t)]
    if args.max_tests is not None:
        chosen = chosen[: args.max_tests]

    # A conversation group must be selected whole or not at all: running turn 2
    # without turn 1 evaluates a conversation that never happened.
    chosen_ids = {t.test_id for t in chosen}
    for group in {t.conversation_group for t in chosen if t.conversation_group}:
        members = [t.test_id for t in prepared if t.conversation_group == group]
        missing = [tid for tid in members if tid not in chosen_ids]
        if missing:
            raise SuiteError(
                f"Selection splits conversation group '{group}': missing {missing}. "
                "Adjust tags/--max-tests to select the whole group or none of it."
            )

    if not chosen and not args.allow_empty:
        raise SuiteError(
            "Zero tests selected after filtering — refusing to report an empty green run. "
            "Pass --allow-empty if this is intentional."
        )
    return chosen


# ---------------------------------------------------------------------------
# Run loop


TRANSPORT_ERRORS = (AdapterError, subprocess.TimeoutExpired, OSError)


@dataclass
class GroupState:
    workspace: Path
    hashes: Dict[str, str]
    conversation_id: Optional[str] = None
    failed: bool = False


def run_suite(args: argparse.Namespace) -> Dict[str, Any]:
    suite_path = Path(args.suite).resolve()
    try:
        suite = json.loads(suite_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SuiteError(f"Failed to read suite JSON: {exc}") from exc
    if not isinstance(suite.get("tests"), list):
        raise SuiteError("Suite JSON must contain a top-level 'tests' array")

    prepared = prepare_suite(suite, suite_path, args)
    tests = select_tests(prepared, args)

    if args.adapter == "claude":
        if not args.plugin_dir:
            raise SuiteError("--plugin-dir is required for --adapter claude")
        adapter: Adapter = ClaudeAdapter(Path(args.plugin_dir), model=args.model)
    else:
        if not args.command_template:
            raise SuiteError("--command-template is required for --adapter command")
        adapter = CommandAdapter(args.command_template, args.command_response_format)

    suite_start = time.time()
    groups: Dict[str, GroupState] = {}
    results: List[TestResult] = []
    kept_workspaces: List[Path] = []

    for idx, test in enumerate(tests, start=1):
        group = groups.get(test.conversation_group) if test.conversation_group else None

        # Dependency skip: a later turn of a failed conversation is meaningless.
        if group and group.failed:
            results.append(TestResult(
                test_id=test.test_id, description=test.description, tags=test.tags,
                passed=False, duration_ms=0, skipped_dependency=True,
                error=f"skipped: earlier turn in conversation group "
                      f"'{test.conversation_group}' failed",
            ))
            print(f"[{idx}/{len(tests)}] SKIP {test.test_id} (dependency failed)", flush=True)
            continue

        if test.conversation_group and group is None:
            ws, hashes = create_workspace(test.fixture, test.test_id)
            group = groups[test.conversation_group] = GroupState(workspace=ws, hashes=hashes)
        if group:
            workspace, hashes, conv_id = group.workspace, group.hashes, group.conversation_id
        else:
            workspace, hashes = create_workspace(test.fixture, test.test_id)
            conv_id = None

        timeout_sec = test.timeout_sec or args.timeout_sec
        started = time.time()
        response, raw, error, cost = "", "", None, None
        out_conv_id = conv_id

        # Retries cover transport failures only, and only for standalone tests:
        # a shared-group turn may already have written files or advanced the
        # conversation, so it is never retried. Standalone retries get a fresh
        # workspace for the same reason.
        attempts = 1 if test.conversation_group else max(1, int(args.retries))
        for attempt in range(1, attempts + 1):
            try:
                response, out_conv_id, raw, cost = adapter.send(
                    test.message, conv_id, cwd=workspace,
                    extra_args=test.extra_args, timeout_sec=timeout_sec,
                )
                error = None
                break
            except TRANSPORT_ERRORS as exc:
                error = f"{type(exc).__name__}: {exc}"
                if attempt < attempts:
                    print(f"  retrying {test.test_id} after transport error "
                          f"(attempt {attempt}/{attempts}): {error}", flush=True)
                    shutil.rmtree(workspace, ignore_errors=True)
                    workspace, hashes = create_workspace(test.fixture, test.test_id)
                    time.sleep(float(args.retry_delay_sec))

        failures: List[str] = []
        warnings: List[str] = []
        if error is not None:
            failures.append(f"transport_error: {error}")
        else:
            # Assertions are evaluated exactly once, outside the retry path —
            # an assertion bug must fail the test, never re-run the model.
            for assertion in test.assertions:
                ar = evaluate_assertion(assertion, response, workspace, hashes)
                if not ar.passed:
                    (warnings if ar.advisory else failures).append(ar.message)

        if group:
            group.conversation_id = out_conv_id
            if failures:
                group.failed = True

        duration_ms = int((time.time() - started) * 1000)
        passed = not failures
        keep = not passed or args.keep_workspaces
        if keep:
            kept_workspaces.append(workspace)
        elif not test.conversation_group:
            shutil.rmtree(workspace, ignore_errors=True)

        results.append(TestResult(
            test_id=test.test_id, description=test.description, tags=test.tags,
            passed=passed, duration_ms=duration_ms, failures=failures,
            warnings=warnings, response=response, raw_output=raw,
            conversation_id=out_conv_id, error=error, cost_usd=cost,
            workspace=str(workspace) if keep else None,
        ))

        status = "PASS" if passed else "FAIL"
        print(f"[{idx}/{len(tests)}] {status} {test.test_id} ({duration_ms}ms)", flush=True)
        for f in failures:
            print(f"  - {f}", flush=True)
        for w in warnings:
            print(f"  ~ advisory: {w}", flush=True)
        if not passed:
            print(f"  workspace kept: {workspace}", flush=True)
        if args.print_responses:
            print(textwrap.indent(response[:1200], prefix="    "), flush=True)

    # Group workspaces survive until the whole group has run.
    for group_name, state in groups.items():
        if not state.failed and not args.keep_workspaces:
            shutil.rmtree(state.workspace, ignore_errors=True)
        elif state.workspace not in kept_workspaces:
            kept_workspaces.append(state.workspace)

    duration_ms = int((time.time() - suite_start) * 1000)
    passed_count = sum(1 for r in results if r.passed)
    skipped_count = sum(1 for r in results if r.skipped_dependency)
    failed_count = len(results) - passed_count - skipped_count
    known_costs = [r.cost_usd for r in results if r.cost_usd is not None]

    payload = {
        "meta": {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "adapter": args.adapter,
            "plugin_dir": relativize(args.plugin_dir),
            "suite_path": relativize(suite_path),
            "suite_name": suite.get("name", ""),
            "filters": {
                "include_tags": args.include_tag,
                "exclude_tags": args.exclude_tag,
                "max_tests": args.max_tests,
            },
            "tests_defined": len(suite["tests"]),
            "tests_selected": len(tests),
            "cost_note": "cost_usd figures exclude llm_judge calls (invoked separately)",
        },
        "summary": {
            "total": len(results),
            "passed": passed_count,
            "failed": failed_count,
            "skipped_dependency": skipped_count,
            "advisory_warnings": sum(len(r.warnings) for r in results),
            "duration_ms": duration_ms,
            "total_cost_usd": round(sum(known_costs), 4) if known_costs else None,
        },
        "results": [
            {
                "test_id": r.test_id,
                "description": r.description,
                "tags": r.tags,
                "passed": r.passed,
                "skipped_dependency": r.skipped_dependency,
                "duration_ms": r.duration_ms,
                "failures": r.failures,
                "warnings": r.warnings,
                "response": r.response,
                "conversation_id": r.conversation_id,
                "error": r.error,
                "cost_usd": r.cost_usd,
                "workspace": r.workspace,
            }
            for r in results
        ],
    }
    return payload


# ---------------------------------------------------------------------------
# Reports


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, data: Dict[str, Any]) -> None:
    ensure_parent(path)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def write_markdown(path: Path, payload: Dict[str, Any]) -> None:
    ensure_parent(path)
    meta, summary = payload["meta"], payload["summary"]
    lines: List[str] = ["# Plugin Eval Report", ""]
    lines.append(f"- Timestamp (UTC): `{meta['timestamp_utc']}`")
    if meta.get("plugin_dir"):
        lines.append(f"- Plugin: `{meta['plugin_dir']}`")
    lines.append(f"- Suite: `{meta['suite_name'] or meta['suite_path']}`")
    lines.append(f"- Selected: `{meta['tests_selected']}` of `{meta['tests_defined']}` defined"
                 + (f" (filters: {meta['filters']})" if any(v for v in meta["filters"].values()) else ""))
    lines.append(f"- Passed: `{summary['passed']}` / `{summary['total']}`"
                 + (f" (`{summary['skipped_dependency']}` skipped on dependency)"
                    if summary["skipped_dependency"] else ""))
    if summary.get("total_cost_usd") is not None:
        lines.append(f"- Cost: `${summary['total_cost_usd']}` (excl. llm_judge calls)")
    lines += ["", "## Results", ""]
    for r in payload["results"]:
        icon = "⏭️" if r["skipped_dependency"] else ("✅" if r["passed"] else "❌")
        lines.append(f"### {icon} {r['test_id']}")
        if r["description"]:
            lines.append(f"- Description: {r['description']}")
        lines.append(f"- Tags: {', '.join(r.get('tags', []))}")
        lines.append(f"- Duration: {r['duration_ms']}ms")
        if not r["passed"] and r.get("failures"):
            lines.append("- Failures:")
            for f in r["failures"]:
                lines.append(f"  - {f}")
        if r.get("warnings"):
            lines.append("- Advisory warnings (do not fail the test):")
            for w in r["warnings"]:
                lines.append(f"  - {w}")
        if r.get("error"):
            lines.append(f"- Error: `{r['error']}`")
        excerpt = (r.get("response") or "").strip()[:800]
        if excerpt:
            lines += ["- Response excerpt:", "", "```text", excerpt, "```"]
        lines.append("")
    path.write_text("\n".join(lines).rstrip() + "\n")


def write_junit(path: Path, payload: Dict[str, Any]) -> None:
    ensure_parent(path)
    summary = payload["summary"]
    suite = ET.Element(
        "testsuite",
        name=payload["meta"].get("suite_name") or "plugin_eval_suite",
        tests=str(summary["total"]),
        failures=str(summary["failed"]),
        skipped=str(summary["skipped_dependency"]),
        errors="0",
        time=f"{summary['duration_ms'] / 1000.0:.3f}",
    )
    for r in payload["results"]:
        case = ET.SubElement(
            suite, "testcase",
            classname="plugin_eval",
            name=r["test_id"],
            time=f"{r['duration_ms'] / 1000.0:.3f}",
        )
        if r["skipped_dependency"]:
            ET.SubElement(case, "skipped", message="dependency_failed")
        elif not r["passed"]:
            failure = ET.SubElement(case, "failure", message="assertion_failed")
            failure.text = "\n".join(r.get("failures", [])) or (r.get("error") or "failed")
        out = ET.SubElement(case, "system-out")
        out.text = (r.get("response") or "")[:4000]
    ET.ElementTree(suite).write(path, encoding="utf-8", xml_declaration=True)


# ---------------------------------------------------------------------------


def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run behavior evals against a plugin.")
    p.add_argument("--suite", required=True, help="Path to suite JSON")
    p.add_argument("--adapter", choices=["claude", "command"], default="claude")
    p.add_argument("--plugin-dir", dest="plugin_dir",
                   help="Plugin directory to load via `claude --plugin-dir` (claude adapter)")
    p.add_argument("--model", help="Model override for the evaluated sessions")
    p.add_argument("--timeout-sec", type=int, default=300,
                   help="Per-test timeout (overridable per test via 'timeout')")
    p.add_argument("--retries", type=int, default=2,
                   help="Attempts per standalone test on transport errors (shared-group turns never retry)")
    p.add_argument("--retry-delay-sec", type=float, default=2.0)
    p.add_argument("--max-tests", type=int, help="Run only first N selected tests")
    p.add_argument("--include-tag", action="append", default=[],
                   help="Only run tests with this tag (repeatable)")
    p.add_argument("--exclude-tag", action="append", default=[],
                   help="Exclude tests with this tag (repeatable)")
    p.add_argument("--allow-empty", action="store_true",
                   help="Permit a run where filters select zero tests")
    p.add_argument("--keep-workspaces", action="store_true",
                   help="Keep all temp workspaces, not just failing ones")
    p.add_argument("--print-responses", action="store_true")

    p.add_argument("--command-template", help="Shell command template for --adapter command")
    p.add_argument("--command-response-format", choices=["text", "json"], default="text")

    p.add_argument("--out-json", help="Write full JSON report")
    p.add_argument("--out-md", help="Write markdown summary")
    p.add_argument("--out-junit", help="Write JUnit XML report")
    return p.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    try:
        payload = run_suite(args)
    except SuiteError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    summary = payload["summary"]
    parts = [f"{summary['passed']}/{summary['total']} passed", f"{summary['failed']} failed"]
    if summary["skipped_dependency"]:
        parts.append(f"{summary['skipped_dependency']} skipped on dependency")
    if summary["advisory_warnings"]:
        parts.append(f"{summary['advisory_warnings']} advisory warning(s)")
    if summary.get("total_cost_usd") is not None:
        parts.append(f"${summary['total_cost_usd']}")
    parts.append(f"{summary['duration_ms']}ms")
    print("Summary: " + ", ".join(parts), flush=True)

    if args.out_json:
        write_json(Path(args.out_json), payload)
        print(f"Wrote JSON report: {args.out_json}", flush=True)
    if args.out_md:
        write_markdown(Path(args.out_md), payload)
        print(f"Wrote Markdown report: {args.out_md}", flush=True)
    if args.out_junit:
        write_junit(Path(args.out_junit), payload)
        print(f"Wrote JUnit report: {args.out_junit}", flush=True)

    return 0 if summary["failed"] == 0 and summary["skipped_dependency"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
