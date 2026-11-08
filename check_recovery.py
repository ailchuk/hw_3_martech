#!/usr/bin/env python3
"""Self-check: does the analyzer find what was planted, and avoid the traps?

Run after generate_data.py:
    python3 check_recovery.py

Checks:
  1. all planted patterns are in the top 5 leaks;
  2. no "trap" segment (no real effect) and not the tiny segment is in the top 5;
  3. the total EGP estimate lies in the range of the scenarios in doc.md ($3.3k-$53k);
  4. the messy file (duplicates, missing steps, extra column) does not crash the tool;
  5. a file without a session column gives a clear error, not a crash.
"""
import json
import sys
import tempfile
from pathlib import Path

import analyze

DATA = Path("sample_data")
OPTS = {
    "steps": list(analyze.DEFAULT_STEPS), "map": {}, "from_step": "step_4", "to_step": "step_5",
    "clicks": 350000, "ltv": 68.0, "quality": 0.75, "dims": [], "min_n": 200, "alpha": 0.05, "top": 5,
    "interactive": False,  # never ask questions inside the self-check
}
RANGE = (3_300, 53_000)

failures = []


def check(name, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        failures.append(name)


def main():
    planted = json.loads((DATA / "planted_patterns.json").read_text())
    res = analyze.analyze(str(DATA / "events.csv"), OPTS)
    top = [(t["dim"], str(t["value"])) for t in res["leaks"][:5]]
    print("Top 5 leaks:", top, "\n")

    for p in planted["planted"]:
        key = (p["dimension"], p["value"])
        check(f"planted pattern found in top 5: {key[0]} = {key[1]}", key in top)

    bad = [(t["dimension"], t["value"]) for t in planted["traps_no_effect"]]
    bad.append((planted["small_segment_below_threshold"]["dimension"],
                planted["small_segment_below_threshold"]["value"]))
    for key in bad:
        check(f"trap NOT in top 5: {key[0]} = {key[1]}", key not in top)

    total = res["total_egp"]
    check("total EGP within the scenario range", RANGE[0] <= total <= RANGE[1],
          f"(${total:,.0f}, range ${RANGE[0]:,}-${RANGE[1]:,})")

    try:
        analyze.analyze(str(DATA / "events_dirty.csv"), dict(OPTS, min_n=20))
        check("messy file does not crash", True)
    except Exception as e:  # any crash is a failure
        check("messy file does not crash", False, repr(e))

    with tempfile.TemporaryDirectory() as tmp:
        broken = Path(tmp) / "no_session.csv"
        broken.write_text("event,platform\nstep_4,iOS Safari\nstep_5,iOS Safari\n")
        try:
            analyze.analyze(str(broken), OPTS)
            check("missing session column gives a clear error", False, "no error raised")
        except analyze.UserError:
            check("missing session column gives a clear error", True)
        except Exception as e:
            check("missing session column gives a clear error", False, repr(e))

    print()
    if failures:
        print(f"{len(failures)} check(s) failed.")
        sys.exit(1)
    print("All checks passed.")


if __name__ == "__main__":
    main()
