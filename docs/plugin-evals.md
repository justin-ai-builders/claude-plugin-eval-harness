# Plugin evals

Behavioral tests for Claude Code plugins: a JSON suite of prompts and assertions, run against a real plugin by driving Claude Code headless with `claude -p --plugin-dir`.

Deterministic assertions gate. A committed baseline plus `compare.py` catches regressions. The generated Markdown report is suitable for reading locally or posting on a pull request.

Evals are opt-in. Add a suite when a plugin has behavioral contracts worth protecting: a skill must read particular files, ask before writing, degrade gracefully when a dependency is missing, refuse unsafe work, or produce a specific artifact.

This framework is deliberately manual. It does not install or run evals in CI.

## Layout

```text
tools/evals/                         shared contributor tooling
  run.py                             eval runner
  compare.py                         baseline regression gate
  run.sh                             marketplace-aware wrapper
  templates/core_smoke.json          starter suite

plugins/<plugin>/evals/              evals owned with the plugin
  suites/core_smoke.json              must-pass mechanical contracts
  suites/full_behavior.json           optional broader or qualitative suite
  fixtures/                           committed synthetic inputs
  baselines/<suite>_<label>.json      committed reference reports
  out/                                generated, gitignored reports
```

Suites live inside the plugin folder so they move with the plugin and share its ownership. They may be included when the plugin is installed, but they are inert JSON and fixture files rather than runtime components.

## Running

Prerequisites:

- Python 3;
- the `claude` CLI; and
- a logged-in Claude Code session.

Start with the wrapper:

```bash
tools/evals/run.sh hello-world
tools/evals/run.sh hello-world -- --max-tests 2 --print-responses
tools/evals/run.sh hello-world --suite full_behavior
```

Arguments after `--` go to `run.py`. Useful options include:

```text
--model <model>             override the evaluated model
--include-tag <tag>         include tests with a tag; repeatable
--exclude-tag <tag>         exclude tests with a tag; repeatable
--max-tests <n>             run the first n selected tests
--print-responses           print response excerpts during the run
--keep-workspaces           retain every temporary workspace
```

`EVAL_TIMEOUT_SEC` sets the default per-test timeout. Individual tests can override it with `"timeout"`.

Reports land in:

```text
plugins/<plugin>/evals/out/<suite>_latest.json
plugins/<plugin>/evals/out/<suite>_latest.md
plugins/<plugin>/evals/out/<suite>_latest.junit.xml
```

Exit code 0 means every selected test passed. The runner refuses zero-test runs and refuses selections that split a multi-turn conversation group.

## Usage and billing

Eval sessions use whatever authentication the local `claude` CLI uses. On a Claude Code seat, that is normally plan usage. If an `ANTHROPIC_API_KEY` is present in the parent environment, the runner strips it so nested Claude sessions use the machine's stored login.

Model calls can still consume rate limits and plan allowance. Use tag filters and `--max-tests` while iterating instead of repeatedly running every suite.

## Isolation model

Each test runs in a temporary copy of its fixture workspace. File assertions are evaluated against that copy, and committed fixtures are never modified.

This is fixture isolation, not an operating-system sandbox. A real Claude Code session can still use permitted tools and absolute paths. The actual guardrails are:

- No user configuration by default. Tests use project settings plus a strict, empty MCP configuration.
- Live MCP is per-test opt-in with `"mcp": "user"`.
- Tool access is explicitly declared in `defaults.allowed_tools` or per test and passed with permission mode `dontAsk`.
- Extra `claude_args` are restricted to MCP and tool flags. Test data cannot replace the plugin path, model, output format, permissions, or resume behavior owned by the runner.
- File assertion paths must be workspace-relative, cannot contain `..`, and are checked for symlink escapes.

Without `allowed_tools`, the original harness falls back to `bypassPermissions` for quick experiments. Avoid that in maintained suites: derive a narrow allowlist from the skills under test.

Only run suites you have reviewed. Prompts are executed with real tool access. Untrusted plugins require stronger isolation than this framework provides.

## Suite schema

```jsonc
{
  "name": "my_plugin_core_smoke",
  "description": "Must-pass contracts for my-plugin.",
  "defaults": {
    "workspace_fixture": "../fixtures/workspace",
    "allowed_tools": ["Read", "Write", "Glob"],
    "mcp": "none"
  },
  "tests": [
    {
      "id": "unique_snake_case",
      "description": "What this proves.",
      "tags": ["core"],
      "conversation": "new",
      "input": "/my-plugin:my-skill realistic input",
      "workspace_fixture": "../fixtures/alternate-workspace",
      "allowed_tools": ["Read"],
      "mcp": "none",
      "claude_args": [],
      "timeout": 600,
      "skip": false,
      "assertions": [
        {"type": "contains", "value": "expected marker"}
      ]
    }
  ]
}
```

Defaults may be overridden per test. The entire suite is validated before the first model call: schema problems, duplicate IDs, invalid regular expressions, missing fixtures, unsafe file paths, and disallowed CLI flags fail fast.

## Multi-turn conversations

Use `"conversation": "shared:<group>"` for confirmation or refinement flows. Tests in the same group resume one Claude Code session and share one temporary workspace, in file order.

If an earlier turn fails, later turns in the group are skipped and the run is not green. Filters must select the complete group. Shared turns are not retried because a failed attempt may already have changed the workspace or conversation state.

Example:

```json
{
  "id": "write_flow_step_1",
  "conversation": "shared:write-flow",
  "input": "/my-plugin:create-file report.md",
  "assertions": [
    {"type": "regex", "value": "(?i)(confirm|proceed|shall I)"},
    {"type": "file_not_exists", "path": "report.md"}
  ]
}
```

The next test can answer `Yes` and assert that `report.md` exists.

## Assertion reference

Response assertions apply to the final response text.

| Type | Parameters | Passes when |
| --- | --- | --- |
| `contains` | `value` | The case-sensitive substring is present. |
| `not_contains` | `value` | The case-sensitive substring is absent. |
| `contains_any` | `values` | At least one substring is present. |
| `contains_all` | `values` | Every substring is present. |
| `regex` | `value` | `re.search` matches with multiline and dot-all enabled. |
| `not_regex` | `value` | The regular expression does not match. |
| `min_words` | `value` | The response has at least this many whitespace-delimited words. |
| `max_words` | `value` | The response has at most this many words. |
| `between_words` | `min`, `max` | The word count is inside the inclusive range. |
| `max_quoted_words` | `value` | No double-quoted span exceeds the word limit. |
| `quotes_require_source` | `source_regex` | Quotes are accompanied by a matching source marker. |
| `llm_judge` | `rubric`, `model`, `advisory` | A separate Claude call judges the response. |

Natural-language matching is case-sensitive. Use an inline flag such as `(?i)` when casing is not part of the contract.

File assertions apply to the temporary workspace after the turn.

| Type | Passes when |
| --- | --- |
| `file_exists` | At least one regular file matches `path`. |
| `file_not_exists` | No regular file matches `path`. |
| `file_contains` | A matching file exists and at least one contains `value`. |
| `file_not_contains` | Matching files exist and none contains `value`. |
| `file_regex` | A matching file exists and at least one matches `value`. |
| `file_unchanged` | Every baseline file matching `path` remains byte-identical, with no matched files created or deleted. |

`path` can be a file or glob. Content assertions fail when nothing matches; they do not pass vacuously.

## Model judges

`llm_judge` is the escape hatch for behavior that has no reliable mechanical marker.

```json
{
  "type": "llm_judge",
  "model": "haiku",
  "advisory": true,
  "rubric": "The response is a usable draft with no meta-commentary."
}
```

The judge runs once and is never retried. An infrastructure error fails that assertion. In `core_smoke`, keep judges advisory so repeatable mechanical contracts remain the gate. Put deliberately qualitative gates in a broader suite.

## Retries

Retries cover transport failures only. Assertion failures never trigger a second model call. Standalone retries receive a fresh fixture copy. Multi-turn conversation groups are never retried.

This prevents test failures or partial writes from silently changing the next attempt.

## Authoring tests

Derive tests from the plugin's use cases and written skill contracts. Phrase inputs the way a real user invokes the plugin. Assert the minimum that proves the behavior.

Scaffold a suite from:

```text
tools/evals/templates/core_smoke.json
```

Keep fixtures synthetic. Reports and baselines embed full model responses, so any fixture content can appear verbatim in a committed file or pull-request comment.

Before weakening an assertion, inspect the captured response and retained failure workspace. Frequent false negatives include:

- case-sensitive prose markers;
- checking for a written file before a confirmation-gated skill has permission to write;
- applying voice assertions to surrounding commentary instead of asking for only the draft;
- setting word-count minimums on terse confirmation turns;
- timeouts on tool-heavy tests; and
- requiring exact phrasing when the contract is semantic.

Fix an inaccurate test instead of changing correct plugin behavior. Do not loosen a genuine guardrail just to produce a green report.

## Baselines and regression comparison

After a reviewed green run, snapshot the JSON report:

```bash
cp plugins/<plugin>/evals/out/core_smoke_latest.json \
   plugins/<plugin>/evals/baselines/core_smoke_<label>.json
```

Compare a later run with it:

```bash
python3 tools/evals/compare.py \
  plugins/<plugin>/evals/baselines/core_smoke_<label>.json \
  plugins/<plugin>/evals/out/core_smoke_latest.json
```

The comparison exits nonzero for:

- a test that passed in the baseline and now fails;
- a baseline test missing from the latest report;
- a suite-name mismatch; or
- a newly added failing test.

Use `--allow-missing <test-id> ...` only for deliberate removals. A filtered or truncated run is not evidence of no regressions.

After a deliberate behavior change, review it and commit a new baseline in the same change.

## Suggested pull-request protocol

1. Run the appropriate suite manually.
2. Read the full JSON and Markdown reports, including response excerpts.
3. Compare the JSON result against the committed baseline.
4. Fix real regressions or inaccurate assertions.
5. Post the reviewed Markdown report on the pull request if useful.
6. State which suites, tags, and model were run.

For example:

```bash
gh pr comment <pr-number> \
  --body-file plugins/<plugin>/evals/out/core_smoke_latest.md
```

No GitHub Actions workflow is included. Teams can add automation later if they accept the credential, cost, nondeterminism, and untrusted-plugin execution implications.
