#!/usr/bin/env python3
"""Run the naive functional checks over every run in a stamp.

These are deliberately independent of the LLM judge. They ask the questions a
normal user would ask and answer them by driving the artifact: a real browser
for the HTML and SVG, and actually executing the scripts for the analysis. No
model is consulted, so a model cannot flatter its own output.
"""
import json, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(HERE, "runs")
FR = os.path.join(HERE, "fr")

FIX = os.path.join(HERE, "fixtures")

RUNNERS = {
    "todo":    ["node", os.path.join(FR, "todo.js"), "{work}/todo.html"],
    "pelican": ["node", os.path.join(FR, "pelican.js"), "{work}/pelican.svg"],
    "datasci": [sys.executable, os.path.join(FR, "datasci.py"), "{work}"],
    # Ground truth, not opinion: defects planted on purpose, or a hidden suite.
    "bugfind": [sys.executable, os.path.join(FR, "findings.py"),
                os.path.join(FIX, "bugfind"), "{work}/REVIEW.md"],
    "secfind": [sys.executable, os.path.join(FR, "findings.py"),
                os.path.join(FIX, "secfind"), "{work}/SECURITY.md"],
    "algo":    [sys.executable, os.path.join(FR, "algo.py"), "{work}"],
}

def run_one(task, work):
    cmd = RUNNERS.get(task)
    if not cmd:
        return None
    argv = [c.replace("{work}", work) for c in cmd]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=300)
        return json.loads(r.stdout)
    except Exception as e:
        return [{"q": "Did the check run?", "met": False, "note": f"{type(e).__name__}: {e}"}]

def main(stamp):
    sd = os.path.join(RUNS, stamp)
    rows = []
    for name in sorted(os.listdir(sd)):
        rd = os.path.join(sd, name)
        meta_p = os.path.join(rd, "meta.json")
        if not os.path.isdir(rd) or not os.path.exists(meta_p):
            continue
        meta = json.load(open(meta_p))
        v = run_one(meta["task"], os.path.join(rd, "work"))
        if v is None:
            continue
        met, tot = sum(1 for x in v if x["met"]), len(v)
        json.dump({"verdicts": v, "met": met, "total": tot},
                  open(os.path.join(rd, "fr.json"), "w"), indent=2)
        rows.append((meta["task"], meta["arm"], meta["rep"], met, tot, v))
        flag = "" if met == tot else "   <-- FAILS"
        print(f"{name:36} {met}/{tot}{flag}")
        for x in v:
            if not x["met"]:
                print(f"      no: {x['q']}  ({x['note']})")
    print()
    agg = {}
    for task, arm, _, met, tot, _ in rows:
        a = agg.setdefault((task, arm), [0, 0, 0])
        a[0] += met; a[1] += tot; a[2] += 1 if met == tot else 0
    print(f"{'task':9} {'arm':19} {'requirements met':>17} {'builds fully working':>21}")
    print("-" * 70)
    for (task, arm), (met, tot, full) in sorted(agg.items()):
        n = sum(1 for r in rows if r[0] == task and r[1] == arm)
        print(f"{task:9} {arm:19} {met:>8}/{tot:<8} {full:>13}/{n}")

if __name__ == "__main__":
    main(sys.argv[1])
