# Claude Plugin Eval Harness

A generic, manual evaluation framework for Claude Code plugins.

This repository is both a working sample marketplace and a template you can copy into your own plugin marketplace. It includes one intentionally tiny `hello-world` plugin, shared eval tooling, example suites, synthetic fixtures, report generation, and baseline regression comparison.

The design is deliberately local-first:

- evals run manually against a real logged-in Claude Code CLI;
- ordinary plugin behavior is graded with deterministic assertions;
- optional model judges can add qualitative feedback;
- fixture workspaces make file effects testable;
- committed baselines make before/after regressions explicit; and
- no CI workflow is included.

## What is where

```text
.
├── .claude-plugin/
│   └── marketplace.json
├── plugins/
│   └── hello-world/
│       ├── .claude-plugin/plugin.json
│       ├── skills/hello/SKILL.md
│       └── evals/
│           ├── suites/
│           │   ├── core_smoke.json
│           │   └── full_behavior.json
│           ├── fixtures/
│           ├── baselines/
│           └── out/
├── tools/evals/
│   ├── run.py
│   ├── run.sh
│   ├── compare.py
│   └── templates/core_smoke.json
└── docs/plugin-evals.md
```

Global contributor tooling lives under `tools/evals/`. Plugin-owned tests, fixtures, and baselines live under each plugin's `evals/` directory.

The harness is contributor tooling. It is not imported by the plugin and does not change the plugin's runtime behavior.

## Requirements

- Python 3
- [Claude Code](https://code.claude.com/docs/en/overview) installed
- a logged-in `claude` CLI

No Python packages need to be installed.

Run the harness regression tests with:

```bash
python3 -m unittest discover -s tests -v
```

## Run the sample

From the repository root:

```bash
tools/evals/run.sh hello-world
```

This resolves `hello-world` through `.claude-plugin/marketplace.json`, loads the plugin with `--plugin-dir`, and runs `plugins/hello-world/evals/suites/core_smoke.json`.

Generated reports appear in:

```text
plugins/hello-world/evals/out/core_smoke_latest.json
plugins/hello-world/evals/out/core_smoke_latest.md
plugins/hello-world/evals/out/core_smoke_latest.junit.xml
```

Run the broader example suite with:

```bash
tools/evals/run.sh hello-world --suite full_behavior
```

Useful iteration commands:

```bash
tools/evals/run.sh hello-world -- --max-tests 1
tools/evals/run.sh hello-world -- --include-tag greeting
tools/evals/run.sh hello-world -- --print-responses
tools/evals/run.sh hello-world -- --model sonnet
```

Everything after `--` is passed through to the Python runner.

## How a test runs

For each selected test, the harness:

1. validates the complete suite before spending model usage;
2. copies the suite's synthetic fixture workspace to a temporary directory;
3. launches `claude -p` in that directory with the plugin loaded;
4. isolates user settings and MCP servers unless the test explicitly opts in;
5. restricts tool access to the suite's allowlist;
6. captures the final response and session ID;
7. evaluates response and file assertions exactly once;
8. retains failed workspaces for inspection; and
9. writes JSON, Markdown, and JUnit reports.

Standalone tests retry transport failures only. Assertion failures never cause another model call. Multi-turn tests can share one Claude session and workspace with `"conversation": "shared:<group>"`.

See [docs/plugin-evals.md](docs/plugin-evals.md) for the complete suite schema, assertion reference, isolation model, and authoring guidance.

## Compare with a baseline

The sample includes a committed green baseline. After a fresh run:

```bash
python3 tools/evals/compare.py \
  plugins/hello-world/evals/baselines/core_smoke_initial.json \
  plugins/hello-world/evals/out/core_smoke_latest.json
```

The comparison fails on regressions, missing baseline tests, suite mismatches, and failing new tests. This prevents a partial run from claiming there were no regressions.

When behavior changes intentionally, review the new output and commit a replacement baseline.

## Use it for your own plugin

1. Copy `plugins/hello-world` to `plugins/your-plugin`.
2. Replace its manifest and skills with your plugin.
3. Add the plugin to `.claude-plugin/marketplace.json`.
4. Derive one test from each behavioral contract worth protecting.
5. Put synthetic project inputs under `plugins/your-plugin/evals/fixtures/`.
6. Derive `allowed_tools` from the skills under test.
7. Run `tools/evals/run.sh your-plugin` and inspect every failure.
8. Commit a reviewed green baseline.

The starter at `tools/evals/templates/core_smoke.json` demonstrates response checks, file effects, guardrails, and multi-turn confirmation flows.

## Try the sample marketplace

Validate the marketplace and plugin structure:

```bash
claude plugin validate . --strict
```

Add the local marketplace from an interactive Claude Code session:

```text
/plugin marketplace add /absolute/path/to/claude-plugin-eval-harness
/plugin install hello-world@claude-plugin-eval-harness
```

Then invoke:

```text
/hello-world:hello Ada Lovelace
```

## Why this is not CI

The included workflow is intentionally manual. Live plugin evals require credentials, consume model usage, can be nondeterministic, and execute plugin-provided prompts and tools. Those tradeoffs deserve an explicit decision rather than a default GitHub Actions job.

If you later add CI, preserve the same validation-before-spend behavior, narrow tool permissions, keep secrets away from untrusted pull requests, and make model/cost changes visible.

## Safety and privacy

Temporary fixture copies are not OS sandboxes. Review every plugin and suite before running it.

Reports and baselines contain full model responses. Use only synthetic fixtures and inspect reports before committing or posting them. Never put secrets, personal information, or confidential source material in fixtures or prompts.

## License and warranty

Licensed under the [MIT License](LICENSE).

This software is provided **as is**, with no warranty expressed or implied. You are responsible for reviewing plugins, protecting credentials, controlling model usage, and deciding whether the results are suitable for your purpose.
