#!/usr/bin/env bash
# Run a plugin's eval suite. See docs/plugin-evals.md.
#
# Usage:
#   tools/evals/run.sh <plugin-name> [--suite <name>] [-- <run.py args...>]
#
# Examples:
#   tools/evals/run.sh hello-world                         # core_smoke, all tests
#   tools/evals/run.sh hello-world -- --max-tests 2
#   tools/evals/run.sh hello-world --suite full_behavior -- --include-tag quality
#
# Environment:
#   EVAL_TIMEOUT_SEC   default per-test timeout (default 300; tests can override)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

usage() { sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 2; }

[[ $# -ge 1 ]] || usage
PLUGIN="$1"; shift

SUITE_NAME="core_smoke"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --suite) SUITE_NAME="${2:?--suite needs a value}"; shift 2 ;;
    --) shift; break ;;
    *) echo "Unknown argument: $1 (pass run.py args after --)" >&2; usage ;;
  esac
done

PLUGIN_REL="$(python3 - "$PLUGIN" "$ROOT/.claude-plugin/marketplace.json" <<'EOF'
import json, sys
name, manifest = sys.argv[1], sys.argv[2]
plugins = json.load(open(manifest))["plugins"]
match = [p["source"] for p in plugins if p["name"] == name]
if not match:
    known = ", ".join(sorted(p["name"] for p in plugins))
    sys.exit(f"Plugin '{name}' not found in marketplace.json. Known: {known}")
print(match[0].lstrip("./"))
EOF
)"
PLUGIN_DIR="$ROOT/$PLUGIN_REL"

SUITE="$PLUGIN_DIR/evals/suites/$SUITE_NAME.json"
if [[ ! -f "$SUITE" ]]; then
  echo "No suite at $SUITE" >&2
  echo "Scaffold one from tools/evals/templates/core_smoke.json (see docs/plugin-evals.md)." >&2
  exit 1
fi

OUT_DIR="$PLUGIN_DIR/evals/out"
mkdir -p "$OUT_DIR"

python3 "$ROOT/tools/evals/run.py" \
  --adapter claude \
  --plugin-dir "$PLUGIN_DIR" \
  --suite "$SUITE" \
  --retries 2 --retry-delay-sec 2 \
  --timeout-sec "${EVAL_TIMEOUT_SEC:-300}" \
  --out-json "$OUT_DIR/${SUITE_NAME}_latest.json" \
  --out-md "$OUT_DIR/${SUITE_NAME}_latest.md" \
  --out-junit "$OUT_DIR/${SUITE_NAME}_latest.junit.xml" \
  "$@"

echo
echo "Report: $OUT_DIR/${SUITE_NAME}_latest.md"
