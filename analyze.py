#!/usr/bin/env python3
"""Quiz Leak Finder: finds where, and which segments, drop off at the quiz email step
and how much that costs (EGP per month).

Usage:
    python3 analyze.py sample_data/events.csv

Python standard library only. Output: an HTML report.
"""
import argparse
import csv
import html
import math
import re
import sys
from collections import Counter

DEFAULT_STEPS = ["click", "quiz_open", "step_3", "step_4", "step_5", "offer_view", "purchase"]
SESSION_NAMES = ["session_id", "session", "sessionid", "user_id", "userid", "visitor_id",
                 "visitorid", "client_id", "anonymous_id", "anon_id", "uid", "id"]
EVENT_NAMES = ["event", "event_name", "eventname", "event_type", "step", "action", "name"]
MAX_DISTINCT = 50     # more distinct values than this: not treated as a category
STORE_CAP = 200       # beyond this a non-numeric column is dropped (saves memory)
TIME_HINTS = ("time", "sec", "dur")
BUCKETS = [(10, "<10"), (20, "10-20"), (40, "20-40")]
LAST_BUCKET = "40+"


class UserError(Exception):
    """An error shown to the user in plain language."""


def norm(text):
    return re.sub(r"[\s\-]+", "_", text.strip().lower().lstrip("﻿"))


# ---------------------------------------------------------------- columns

def parse_map(text):
    mapping = {}
    for part in (text or "").split(","):
        if "=" in part:
            key, value = part.split("=", 1)
            mapping[key.strip()] = value.strip()
    return mapping


def ask_column(role_text, header):
    print(f"\nI could not find {role_text} on my own.")
    print("These are the columns in your file:")
    for i, name in enumerate(header, 1):
        print(f"  {i}. {name}")
    try:
        answer = input("Type the number of the right column: ").strip()
        return header[int(answer) - 1]
    except (EOFError, ValueError, IndexError):
        raise UserError(f"No column was chosen for {role_text}.")


def find_column(header, explicit, synonyms, role_key, role_text, interactive=True):
    if explicit:
        for name in header:
            if name == explicit or norm(name) == norm(explicit):
                return name
        raise UserError(f"The file has no column \"{explicit}\". Columns in the file: {', '.join(header)}.")
    normalized = {norm(name): name for name in header}
    for syn in synonyms:
        if syn in normalized:
            return normalized[syn]
    if interactive and sys.stdin.isatty():
        return ask_column(role_text, header)
    raise UserError(
        f"Could not find {role_text}. Columns in the file: {', '.join(header)}.\n"
        f"Run again and add: --map \"{role_key}=<your column name>\""
    )


# ---------------------------------------------------------------- reading

def read_sessions(path, mapping, steps, interactive=True):
    step_index = {name: i for i, name in enumerate(steps)}
    try:
        f = open(path, newline="", encoding="utf-8-sig")
    except OSError:
        raise UserError(f"Cannot open the file \"{path}\". Please check the path.")
    with f:
        reader = csv.reader(f)
        try:
            header = [h.strip() for h in next(reader)]
        except StopIteration:
            raise UserError("The file is empty.")

        sess_col = find_column(header, mapping.get("session_id"), SESSION_NAMES, "session_id",
                               "the column that tells one person (session) from another", interactive)
        ev_col = find_column(header, mapping.get("event"), EVENT_NAMES, "event",
                             "the column that says what the person did (the quiz step)", interactive)
        si, ei = header.index(sess_col), header.index(ev_col)
        dim_idx = [i for i in range(len(header)) if i not in (si, ei)]
        dim_names = [header[i] for i in dim_idx]

        sessions = {}                       # sid -> [furthest step, column values...]
        distinct = [set() for _ in dim_idx]
        overflow = [False] * len(dim_idx)
        numeric = [True] * len(dim_idx)
        seen_value = [False] * len(dim_idx)
        unknown_events = Counter()
        rows = skipped = 0
        width = len(header)

        for row in reader:
            rows += 1
            if len(row) < width:
                row = row + [""] * (width - len(row))
            sid = row[si].strip()
            if not sid:
                skipped += 1
                continue
            entry = sessions.get(sid)
            if entry is None:
                entry = [-1] + [""] * len(dim_idx)
                sessions[sid] = entry
            k = step_index.get(norm(row[ei]))
            if k is None:
                unknown_events[norm(row[ei])] += 1
            elif k > entry[0]:
                entry[0] = k
            for j, i in enumerate(dim_idx):
                v = row[i].strip()
                if not v:
                    continue
                seen_value[j] = True
                if numeric[j]:
                    try:
                        float(v)
                    except ValueError:
                        numeric[j] = False
                if overflow[j]:
                    continue
                if not entry[1 + j]:
                    entry[1 + j] = sys.intern(v)
                if len(distinct[j]) <= STORE_CAP:
                    distinct[j].add(v)
                elif not numeric[j]:
                    overflow[j] = True   # many distinct text values: not a useful category
                    distinct[j].clear()

    if not sessions:
        raise UserError("The file has no data rows.")
    if all(entry[0] < 0 for entry in sessions.values()):
        found = ", ".join(f"\"{e}\"" for e, _ in unknown_events.most_common(8)) or "nothing"
        raise UserError(
            f"In the column \"{ev_col}\" I found none of the quiz steps ({', '.join(steps)}).\n"
            f"Values found: {found}.\n"
            "If your steps are named differently, list them in order: --steps \"step1,step2,...\""
        )

    columns = []
    for j, name in enumerate(dim_names):
        if not seen_value[j] or overflow[j]:
            continue
        if numeric[j]:
            if any(h in norm(name) for h in TIME_HINTS):
                columns.append((j, name, "time"))
        elif 2 <= len(distinct[j]) <= MAX_DISTINCT:
            columns.append((j, name, "category"))

    return {
        "sessions": sessions, "columns": columns, "rows": rows, "skipped_rows": skipped,
        "unknown_events": unknown_events, "session_col": sess_col, "event_col": ev_col,
    }


# ---------------------------------------------------------------- statistics

def bucket(value):
    try:
        x = float(value)
    except ValueError:
        return None
    for limit, label in BUCKETS:
        if x < limit:
            return label
    return LAST_BUCKET


def label_of(entry, col):
    j, _, kind = col
    v = entry[1 + j]
    if not v:
        return None
    return bucket(v) if kind == "time" else v


def two_prop_p(s1, n1, s0, n0):
    if n1 == 0 or n0 == 0:
        return 1.0
    pooled = (s1 + s0) / (n1 + n0)
    var = pooled * (1 - pooled) * (1 / n1 + 1 / n0)
    if var <= 0:
        return 1.0
    z = (s1 / n1 - s0 / n0) / math.sqrt(var)
    return math.erfc(abs(z) / math.sqrt(2))


def bh_adjust(pvals):
    m = len(pvals)
    order = sorted(range(m), key=lambda i: pvals[i])
    adjusted = [1.0] * m
    prev = 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        prev = min(prev, pvals[i] * m / rank)
        adjusted[i] = prev
    return adjusted


# ---------------------------------------------------------------- analysis

def analyze(path, opts):
    steps = opts["steps"]
    data = read_sessions(path, opts["map"], steps, opts.get("interactive", True))
    sessions = data["sessions"]
    entries = [e for e in sessions.values() if e[0] >= 0]
    n_total = len(entries)

    from_name, to_name = opts["from_step"], opts["to_step"]
    if from_name not in steps or to_name not in steps:
        raise UserError(f"Steps {from_name} and {to_name} must be in the list of steps: {', '.join(steps)}.")
    f_idx, t_idx = steps.index(from_name), steps.index(to_name)
    if not f_idx < t_idx < len(steps) - 1:
        raise UserError("The \"to\" step must come after the \"from\" step, with at least one more step (purchase) after it.")

    reached = [sum(1 for e in entries if e[0] >= j) for j in range(len(steps))]
    funnel = [{"step": steps[j], "count": reached[j],
               "pct": reached[j] / n_total if n_total else 0,
               "step_conv": (reached[j] / reached[j - 1]) if j and reached[j - 1] else None}
              for j in range(len(steps))]

    population = [e for e in entries if e[0] >= f_idx]
    n_pop = len(population)
    if n_pop < 2 * opts["min_n"]:
        raise UserError(f"Only {n_pop} sessions reached step \"{from_name}\"; that is too few for a reliable "
                        f"analysis (need at least {2 * opts['min_n']}).")
    n_pass = sum(1 for e in population if e[0] >= t_idx)
    base_rate = n_pass / n_pop
    downstream = reached[-1] / reached[t_idx] if reached[t_idx] else 0.0
    scale = opts["clicks"] / n_total
    value_per_session = downstream * opts["ltv"] * opts["quality"]

    columns = data["columns"]
    if opts["dims"]:
        wanted = {norm(d) for d in opts["dims"]}
        missing = wanted - {norm(c[1]) for c in columns}
        if missing:
            raise UserError("These dimensions are unavailable or unsuitable: " + ", ".join(sorted(missing)) +
                            ". Available: " + ", ".join(c[1] for c in columns) + ".")
        columns = [c for c in columns if norm(c[1]) in wanted]
    if not columns:
        raise UserError("The file has no columns that can split people into groups (platform, creative, etc.).")

    tests = []
    for col in columns:
        counts = {}
        for e in population:
            lab = label_of(e, col)
            if lab is None:
                continue
            c = counts.setdefault(lab, [0, 0])
            c[0] += 1
            if e[0] >= t_idx:
                c[1] += 1
        total_n = sum(c[0] for c in counts.values())
        total_s = sum(c[1] for c in counts.values())
        for lab, (n1, s1) in counts.items():
            n0, s0 = total_n - n1, total_s - s1
            if n1 < opts["min_n"] or n0 < opts["min_n"]:
                continue
            rate1, rate0 = s1 / n1, s0 / n0
            tests.append({"dim": col[1], "value": lab, "n": n1, "rate": rate1, "rest_rate": rate0,
                          "gap": rate0 - rate1, "p": two_prop_p(s1, n1, s0, n0), "col": col})

    for t, adj in zip(tests, bh_adjust([t["p"] for t in tests])):
        t["adj_p"] = adj
        monthly_sessions = t["n"] * max(t["gap"], 0) * scale
        t["monthly_sessions"] = monthly_sessions
        t["egp"] = monthly_sessions * value_per_session
        t["significant_leak"] = t["gap"] > 0 and adj <= opts["alpha"]

    leaks = sorted((t for t in tests if t["significant_leak"]), key=lambda t: t["egp"], reverse=True)

    # Total without double counting: each session takes the largest gap among the
    # significant segments it belongs to.
    gap_lookup = {(t["col"][0], t["value"]): t["gap"] for t in leaks}
    lookup_cols = {t["col"][0]: t["col"] for t in leaks}
    total_gap = 0.0
    if gap_lookup:
        for e in population:
            best = 0.0
            for j, col in lookup_cols.items():
                lab = label_of(e, col)
                g = gap_lookup.get((j, lab))
                if g and g > best:
                    best = g
            total_gap += best
    total_egp = total_gap * scale * value_per_session

    return {
        "path": path, "steps": steps, "n_total": n_total, "funnel": funnel,
        "from_step": from_name, "to_step": to_name, "n_pop": n_pop, "base_rate": base_rate,
        "downstream": downstream, "scale": scale, "value_per_session": value_per_session,
        "leaks": leaks, "tests_run": len(tests), "total_egp": total_egp,
        "opts": opts, "dims_used": [c[1] for c in columns],
        "unknown_events": data["unknown_events"], "skipped_rows": data["skipped_rows"],
        "rows": data["rows"],
    }


# ---------------------------------------------------------------- report

def money(x):
    return f"${x:,.0f}"


def fmt_p(p):
    return "<0.001" if p < 0.001 else f"{p:.3f}"


def pct(x, digits=1):
    return f"{x * 100:.{digits}f}%"


def render_report(res):
    o = res["opts"]
    top = res["leaks"][: o["top"]]
    first = res["funnel"][0]["count"] or 1

    funnel_rows = []
    for f in res["funnel"]:
        conv = f"{pct(f['step_conv'])} of previous" if f["step_conv"] is not None else ""
        focus = " focus" if f["step"] in (res["from_step"], res["to_step"]) else ""
        funnel_rows.append(
            f'<div class="frow{focus}"><div class="fname">{html.escape(f["step"])}</div>'
            f'<div class="fbar"><span style="width:{f["count"] / first * 100:.1f}%"></span></div>'
            f'<div class="fnum">{f["count"]:,} · {pct(f["pct"])}</div>'
            f'<div class="fconv">{conv}</div></div>')

    leak_rows = []
    for i, t in enumerate(top, 1):
        leak_rows.append(
            f"<tr><td>{i}</td><td><b>{html.escape(t['dim'])}</b> = {html.escape(str(t['value']))}</td>"
            f"<td>{t['n']:,}</td><td>{pct(t['rate'])} vs {pct(t['rest_rate'])}</td>"
            f"<td>{t['gap'] * 100:.1f} pp</td><td>{html.escape(fmt_p(t['adj_p']))}</td>"
            f"<td class=\"egp\">{money(t['egp'])}</td></tr>")
    if not leak_rows:
        leak_rows.append('<tr><td colspan="7">No statistically significant leaks found.</td></tr>')

    note_unknown = ""
    if res["unknown_events"]:
        shown = ", ".join(f"{html.escape(k)} ({v})" for k, v in res["unknown_events"].most_common(6))
        note_unknown = f"<li>Events not part of the funnel were ignored: {shown}.</li>"

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Quiz Leak Finder report</title>
<style>
body{{font-family:system-ui,Segoe UI,Arial,sans-serif;max-width:1000px;margin:24px auto;padding:0 16px;color:#1b1f23;line-height:1.45}}
h1{{margin-bottom:4px}} h2{{margin-top:32px;border-bottom:1px solid #d0d7de;padding-bottom:4px}}
.sub{{color:#57606a}} .cards{{display:flex;gap:12px;flex-wrap:wrap;margin:16px 0}}
.card{{border:1px solid #d0d7de;border-radius:8px;padding:12px 16px;min-width:180px}}
.card b{{display:block;font-size:24px}}
.frow{{display:grid;grid-template-columns:110px 1fr 150px 170px;gap:8px;align-items:center;margin:4px 0;font-size:14px}}
.frow.focus .fname{{font-weight:700}} .fbar{{background:#eaeef2;border-radius:4px;height:16px}}
.fbar span{{display:block;height:100%;background:#0969da;border-radius:4px}} .frow.focus .fbar span{{background:#cf222e}}
.fnum{{text-align:right}} .fconv{{color:#57606a}}
table{{border-collapse:collapse;width:100%;font-size:14px}} th,td{{border:1px solid #d0d7de;padding:6px 8px;text-align:left;vertical-align:top}}
th{{background:#f6f8fa}} .egp{{font-weight:700;white-space:nowrap}}
.note{{background:#fff8c5;border:1px solid #d4a72c;border-radius:6px;padding:10px 14px;margin:12px 0}}
</style></head><body>
<h1>Quiz Leak Finder</h1>
<div class="sub">File: {html.escape(str(res['path']))} · {res['n_total']:,} sessions analysed</div>

<div class="cards">
<div class="card">Step {html.escape(res['from_step'])} → {html.escape(res['to_step'])}<b>{pct(res['base_rate'])}</b>of people pass</div>
<div class="card">Value of one recovered session<b>${res['value_per_session']:.2f}</b>{pct(res['downstream'], 2)} buy × ${o['ltv']:g} LTV × quality {o['quality']:g}</div>
<div class="card">Upper bound if all gaps closed<b>{money(res['total_egp'])} / month</b>EGP, overlaps counted once</div>
</div>

<h2>Funnel</h2>
{''.join(funnel_rows)}

<h2>Top leaks on {html.escape(res['from_step'])} → {html.escape(res['to_step'])}</h2>
<table><tr><th>#</th><th>Segment</th><th>People at step</th><th>Pass rate vs rest</th><th>Gap</th><th>Adj. p</th><th>EGP / month</th></tr>
{''.join(leak_rows)}</table>
<div class="note"><b>Segments overlap</b> (for example an iOS user may also have an email typo), so the per-segment EGP
figures must not be added up. The total above counts every person once, using the largest gap among the
segments they belong to, so nobody is counted twice. It is still an upper bound: it assumes every gap
can be closed completely, which is unlikely in practice.</div>

<h2>Assumptions</h2>
<ul>
<li>EGP = extra revenue at the same ad spend. Monthly volume is scaled to {o['clicks']:,} clicks (file has {res['n_total']:,} sessions).</li>
<li>Recovered people buy at the file's average post-step rate ({pct(res['downstream'], 2)}), reduced to {pct(o['quality'], 0)} because people who drop here are likely lower intent.</li>
<li>The gap of a segment is assumed to be fixable up to the level of everyone else. Some gaps (for example browser behaviour) may not be fully fixable.</li>
<li>Analysis looks at one dimension at a time, so findings can be confounded (mobile and iOS overlap). A finding shows where to look, it does not prove the cause.</li>
<li>Significance: two-proportion z-test with Benjamini-Hochberg correction over {res['tests_run']} segment tests (alpha {o['alpha']}); segments with fewer than {o['min_n']} people at the step are skipped.</li>
<li>Dimensions analysed: {html.escape(', '.join(res['dims_used']))}.</li>
{note_unknown}
</ul>
</body></html>"""


# ---------------------------------------------------------------- CLI

def print_summary(res, out_path):
    print(f"\nAnalysed {res['n_total']:,} sessions. Step {res['from_step']} -> {res['to_step']}: "
          f"{pct(res['base_rate'])} of people pass.")
    if not res["leaks"]:
        print("No statistically significant leaks found.")
    else:
        print("\nBiggest leaks (EGP per month; segments overlap, do not add them up):")
        for i, t in enumerate(res["leaks"][: res["opts"]["top"]], 1):
            print(f"  {i}. {t['dim']} = {t['value']}: {pct(t['rate'])} pass vs {pct(t['rest_rate'])} for the rest, "
                  f"~{money(t['egp'])}")
        print(f"\nTotal if all gaps were closed (each session counted once): ~{money(res['total_egp'])} per month.")
    print(f"\nReport saved: {out_path}")


def build_opts(args):
    steps = [norm(s) for s in args.steps.split(",") if s.strip()] if args.steps else list(DEFAULT_STEPS)
    if len(steps) < 3:
        raise UserError("--steps needs at least 3 steps.")
    default_from = steps[3] if len(steps) > 4 else steps[-3]
    default_to = steps[4] if len(steps) > 4 else steps[-2]
    return {
        "steps": steps, "map": parse_map(args.map),
        "from_step": norm(args.from_step) if args.from_step else default_from,
        "to_step": norm(args.to_step) if args.to_step else default_to,
        "clicks": args.clicks, "ltv": args.ltv, "quality": args.quality,
        "dims": [d.strip() for d in args.dims.split(",")] if args.dims else [],
        "min_n": args.min_n, "alpha": args.alpha, "top": args.top,
    }


def main():
    p = argparse.ArgumentParser(description="Quiz Leak Finder: where the quiz loses people and what it costs")
    p.add_argument("csv", help="CSV file with quiz events")
    p.add_argument("--out", default="report.html", help="where to save the report (default: report.html)")
    p.add_argument("--clicks", type=int, default=350000, help="clicks per month (US), used for scaling")
    p.add_argument("--ltv", type=float, default=68.0, help="revenue per purchase, $")
    p.add_argument("--quality", type=float, default=0.75, help="quality of recovered people, 0..1")
    p.add_argument("--map", help='column mapping, e.g. "session_id=user_id,event=event_name"')
    p.add_argument("--steps", help="step names in order, comma separated")
    p.add_argument("--from-step", dest="from_step", help="start of the analysed transition (default: step_4)")
    p.add_argument("--to-step", dest="to_step", help="end of the analysed transition (default: step_5)")
    p.add_argument("--dims", help="columns to split by, comma separated (default: all suitable)")
    p.add_argument("--min-n", dest="min_n", type=int, default=200, help="minimum people in a segment")
    p.add_argument("--alpha", type=float, default=0.05, help="significance threshold")
    p.add_argument("--top", type=int, default=5, help="how many leaks to show")
    args = p.parse_args()

    try:
        opts = build_opts(args)
        res = analyze(args.csv, opts)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(render_report(res))
        print_summary(res, args.out)
    except UserError as e:
        print(f"\nCould not finish: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
