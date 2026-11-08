#!/usr/bin/env python3
"""Synthetic quiz-event generator for the SockLock case.

Creates:
  sample_data/events.csv             main data (~60,000 sessions)
  sample_data/events_dirty.csv       small "messy" file to test robustness
  sample_data/planted_patterns.json  what was planted in the data (used by check_recovery.py)

The funnel is calibrated to the SockLock data room (share of everyone who clicked):
  click 100% -> quiz_open 88% -> step_3 61% -> step_4 58% -> step_5 36%
  -> offer_view 31% -> purchase 2.0%

Four patterns are planted that affect the step_4 -> step_5 (email step) transition,
plus "traps": segments with no effect and one very small segment with a strong effect.

Python standard library only.
"""
import argparse
import csv
import json
import math
import random
import sys
from pathlib import Path

EVENTS = ["click", "quiz_open", "step_3", "step_4", "step_5", "offer_view", "purchase"]
CUMULATIVE_TARGET = [1.00, 0.88, 0.61, 0.58, 0.36, 0.31, 0.02]
FIELDS = ["session_id", "event", "platform", "country", "ad_id", "time_on_step_sec", "email_error"]

PLATFORMS = [
    ("iOS Safari", 0.30), ("iOS Chrome", 0.06), ("Android Chrome", 0.32),
    ("Android Samsung", 0.07), ("Desktop Chrome", 0.16), ("Desktop Safari", 0.04),
    ("Desktop Edge", 0.03), ("Desktop Firefox", 0.02),
]
COUNTRIES = [("US", 0.90), ("CA", 0.04), ("GB", 0.03), ("AU", 0.03)]
ADS = [
    ("ad_01", 0.09), ("ad_02", 0.08), ("ad_03", 0.10), ("ad_04", 0.09),
    ("ad_05", 0.08), ("ad_06", 0.08), ("ad_07", 0.08), ("ad_08", 0.09),
    ("ad_09", 0.10), ("ad_10", 0.09), ("ad_11", 0.116), ("ad_12", 0.004),
]

# Multipliers on the probability of passing step_4 -> step_5 (below 1 = worse).
MULT_IOS_SAFARI = 0.80   # pattern 1
MULT_DOMAIN_TYPO = 0.45  # pattern 2: typo in the email domain (gmial.com)
MULT_AD_07 = 0.75        # pattern 3: low-intent creative
MULT_SLOW = 0.75         # pattern 4: more than 40 seconds on the step
MULT_AD_12 = 0.25        # tiny segment with a strong effect (below the sample-size threshold)

P_DOMAIN_TYPO = 0.06
SLOW_SECONDS = 40.0


def pick(rng, options):
    values = [o[0] for o in options]
    weights = [o[1] for o in options]
    return rng.choices(values, weights=weights, k=1)[0]


def calibrate(multipliers, target_mean):
    """Find k so that the mean of clip(k * m) equals target_mean (bisection)."""
    lo, hi = 0.0, 10.0
    for _ in range(80):
        mid = (lo + hi) / 2
        mean = sum(min(0.99, mid * m) for m in multipliers) / len(multipliers)
        if mean < target_mean:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def simulate(n_sessions, rng):
    sessions = []
    for i in range(n_sessions):
        sessions.append({
            "session_id": f"s{i + 1:06d}",
            "platform": pick(rng, PLATFORMS),
            "country": pick(rng, COUNTRIES),
            "ad_id": pick(rng, ADS),
            "events": ["click"],
            "time": "",
            "email_error": "",
        })

    reached = list(range(n_sessions))  # indexes of sessions that reached the previous event
    for j in range(1, len(EVENTS)):
        event = EVENTS[j]
        target = CUMULATIVE_TARGET[j] / CUMULATIVE_TARGET[j - 1]

        if event == "step_4":
            for idx in reached:
                s = sessions[idx]
                s["time"] = round(max(2.0, rng.lognormvariate(math.log(18), 0.6)), 1)
                s["email_error"] = "domain_typo" if rng.random() < P_DOMAIN_TYPO else "none"

        if event == "step_5":
            mults = []
            for idx in reached:
                s = sessions[idx]
                m = 1.0
                if s["platform"] == "iOS Safari":
                    m *= MULT_IOS_SAFARI
                if s["email_error"] == "domain_typo":
                    m *= MULT_DOMAIN_TYPO
                if s["ad_id"] == "ad_07":
                    m *= MULT_AD_07
                if s["ad_id"] == "ad_12":
                    m *= MULT_AD_12
                if s["time"] > SLOW_SECONDS:
                    m *= MULT_SLOW
                mults.append(m)
            k = calibrate(mults, target)
            probs = [min(0.99, k * m) for m in mults]
        else:
            probs = [target] * len(reached)

        next_reached = []
        for idx, p in zip(reached, probs):
            if rng.random() < p:
                sessions[idx]["events"].append(event)
                next_reached.append(idx)
        reached = next_reached
    return sessions


def to_rows(sessions):
    rows = []
    for s in sessions:
        for event in s["events"]:
            is_step4 = event == "step_4"
            rows.append({
                "session_id": s["session_id"],
                "event": event,
                "platform": s["platform"],
                "country": s["country"],
                "ad_id": s["ad_id"],
                "time_on_step_sec": s["time"] if is_step4 else "",
                "email_error": s["email_error"] if is_step4 else "",
            })
    return rows


def write_csv(path, rows, fields):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def make_dirty(sessions, rng, n=400):
    """A small file with typical real-export problems."""
    rows = to_rows(sessions[:n])
    messy = []
    for r in rows:
        r = dict(r)
        r["debug_note"] = rng.choice(["", "", "", "retry", "bot?"])  # extra column
        if r["event"] == "step_3" and rng.random() < 0.05:
            continue  # missing step event
        if rng.random() < 0.02:
            r["event"] = r["event"].upper()  # mixed case
        messy.append(r)
        if rng.random() < 0.05:
            messy.append(dict(r))  # duplicated event
    return messy


def check_funnel(sessions, tolerance=0.005):
    n = len(sessions)
    ok = True
    print("Funnel (share of all sessions):")
    for j, event in enumerate(EVENTS):
        share = sum(1 for s in sessions if event in s["events"]) / n
        diff = share - CUMULATIVE_TARGET[j]
        flag = "ok" if abs(diff) <= tolerance else "OUT OF TOLERANCE"
        if flag != "ok":
            ok = False
        print(f"  {event:<11} {share * 100:6.2f}%  (target {CUMULATIVE_TARGET[j] * 100:5.1f}%)  {flag}")
    return ok


def planted_description():
    return {
        "note": "Planted patterns affect the step_4 -> step_5 transition.",
        "planted": [
            {"dimension": "platform", "value": "iOS Safari"},
            {"dimension": "email_error", "value": "domain_typo"},
            {"dimension": "ad_id", "value": "ad_07"},
            {"dimension": "time_on_step_sec", "value": "40+"},
        ],
        "traps_no_effect": [
            {"dimension": "country", "value": "CA"},
            {"dimension": "country", "value": "GB"},
            {"dimension": "country", "value": "AU"},
            {"dimension": "ad_id", "value": "ad_03"},
            {"dimension": "ad_id", "value": "ad_09"},
        ],
        "small_segment_below_threshold": {"dimension": "ad_id", "value": "ad_12"},
    }


def main():
    parser = argparse.ArgumentParser(description="Synthetic quiz-event generator")
    parser.add_argument("--sessions", type=int, default=60000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", default="sample_data")
    args = parser.parse_args()

    rng = random.Random(args.seed)
    sessions = simulate(args.sessions, rng)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    write_csv(out / "events.csv", to_rows(sessions), FIELDS)
    write_csv(out / "events_dirty.csv", make_dirty(sessions, rng), FIELDS + ["debug_note"])
    with open(out / "planted_patterns.json", "w", encoding="utf-8") as f:
        json.dump(planted_description(), f, indent=2)

    print(f"Wrote {out}/events.csv, events_dirty.csv, planted_patterns.json")
    if not check_funnel(sessions):
        print("Funnel is outside the +-0.5 pp tolerance.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
