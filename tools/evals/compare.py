#!/usr/bin/env python3
"""Compare two eval reports (the regression gate).

Diffs a committed baseline against a fresh run so a skill rewrite or model swap
gets a real before/after instead of a subjective impression. Exits nonzero on:
  - any regression (passed in baseline -> failed in latest)
  - any baseline test missing from the latest report (unless --allow-missing),
    so a filtered or truncated run can't pass itself off as "no regressions"
  - a suite-name mismatch between the two reports
  - a new-in-latest test that is failing

Usage:
  python3 tools/evals/compare.py \
      plugins/<plugin>/evals/baselines/<baseline>.json \
      plugins/<plugin>/evals/out/core_smoke_latest.json \
      [--allow-missing test_id ...]
"""
import argparse
import json
import sys


def load(path):
    with open(path) as fh:
        d = json.load(fh)
    return {r["test_id"]: r for r in d["results"]}, d["summary"], d["meta"]


def main(argv):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("baseline")
    p.add_argument("latest")
    p.add_argument("--allow-missing", nargs="*", default=[],
                   help="Baseline test ids allowed to be absent from the latest report")
    args = p.parse_args(argv)

    (bres, bsum, bmeta) = load(args.baseline)
    (nres, nsum, nmeta) = load(args.latest)

    print(f"BASELINE: {args.baseline}  suite={bmeta.get('suite_name')}  "
          f"passed {bsum['passed']}/{bsum['total']}")
    print(f"LATEST:   {args.latest}  suite={nmeta.get('suite_name')}  "
          f"passed {nsum['passed']}/{nsum['total']}")
    print()

    problems = 0

    if bmeta.get("suite_name") != nmeta.get("suite_name"):
        print(f"❌ suite mismatch: baseline={bmeta.get('suite_name')!r} "
              f"latest={nmeta.get('suite_name')!r} — comparing different suites is meaningless")
        problems += 1

    regressions, fixes, still_failing = [], [], []
    for tid in sorted(set(bres) | set(nres)):
        b, n = bres.get(tid), nres.get(tid)
        bp = b["passed"] if b else None
        np_ = n["passed"] if n else None
        if bp and np_ is False:
            regressions.append(tid)
        elif bp is False and np_:
            fixes.append(tid)
        elif bp is False and np_ is False:
            still_failing.append(tid)

    def show(label, items, mark):
        if items:
            print(f"{label} ({len(items)}):")
            for tid in items:
                print(f"  {mark} {tid}")
                n = nres.get(tid)
                if n and not n["passed"]:
                    for f in n.get("failures", [])[:3]:
                        print(f"        - {f}")
            print()

    show("REGRESSIONS (passed -> failed)", regressions, "❌")
    show("FIXED (failed -> passed)", fixes, "✅")
    show("STILL FAILING", still_failing, "•")
    problems += len(regressions)

    missing = sorted(set(bres) - set(nres) - set(args.allow_missing))
    if missing:
        print(f"❌ baseline tests missing from latest run ({len(missing)}): {missing}")
        print("   (a partial run is not evidence of no regressions; "
              "use --allow-missing for deliberate removals)")
        problems += 1

    new_failing = sorted(tid for tid in set(nres) - set(bres) if not nres[tid]["passed"])
    new_passing = sorted(tid for tid in set(nres) - set(bres) if nres[tid]["passed"])
    if new_passing:
        print(f"new tests, passing: {new_passing}")
    if new_failing:
        print(f"❌ new tests, failing: {new_failing}")
        problems += 1

    if problems == 0:
        print("No regressions.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
