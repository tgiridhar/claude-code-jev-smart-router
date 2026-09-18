#!/usr/bin/env python3
"""Naive functional test of the data analysis.

Asks what somebody handed the folder would ask: does the generator run, do I get
a data file, does it have what was asked for, does the analysis run, and does the
write-up actually talk about the four things requested. It runs the scripts in a
throwaway copy rather than trusting what is already on disk.
"""
import json, os, re, shutil, subprocess, sys, tempfile

def main(work):
    out = []
    rec = lambda q, met, note: out.append({"q": q, "met": bool(met), "note": str(note)[:180]})
    tmp = tempfile.mkdtemp()
    for f in os.listdir(work):
        if f.endswith((".py", ".md")):
            shutil.copy2(os.path.join(work, f), os.path.join(tmp, f))

    gen = os.path.join(tmp, "generate.py")
    if not os.path.exists(gen):
        rec("Is there a script to make the data?", False, "generate.py was not created")
    else:
        r = subprocess.run([sys.executable, "generate.py"], cwd=tmp, capture_output=True,
                           text=True, timeout=300)
        rec("If I run the generator, does it work?", r.returncode == 0,
            "ran cleanly" if r.returncode == 0 else (r.stderr or "").strip().splitlines()[-1][:160])

    csv = os.path.join(tmp, "sales.csv")
    rows, header = 0, ""
    if os.path.exists(csv):
        with open(csv, errors="replace") as f:
            header = f.readline().lower()
            rows = sum(1 for _ in f)
    # Asked for 5000. A small overshoot is a deviation from the brief, not a
    # broken artifact, so it is noted rather than failed.
    ok = 4750 <= rows <= 5250
    rec("Do I get a data file with roughly the 5000 rows I asked for?", ok,
        f"{rows} data rows" + ("" if rows == 5000 else f" (asked for 5000)") if rows
        else "sales.csv not produced")
    want = {"date": ["date"], "region": ["region"], "category": ["categ"],
            "units": ["unit"], "price": ["price"], "discount": ["discount"]}
    missing = [k for k, pats in want.items() if not any(p in header for p in pats)]
    rec("Does it have the columns that were asked for?", not missing,
        "all present" if not missing else f"missing: {', '.join(missing)}")

    ana = os.path.join(tmp, "analyze.py")
    if not os.path.exists(ana):
        rec("Is there a script to analyse it?", False, "analyze.py was not created")
        stdout = ""
    else:
        r = subprocess.run([sys.executable, "analyze.py"], cwd=tmp, capture_output=True,
                           text=True, timeout=300)
        stdout = r.stdout or ""
        rec("If I run the analysis, does it work?", r.returncode == 0 and len(stdout.strip()) > 50,
            "ran and printed results" if r.returncode == 0 else (r.stderr or "").strip().splitlines()[-1][:160])

    fp = os.path.join(work, "FINDINGS.md")
    fx = open(fp, errors="replace").read().lower() if os.path.exists(fp) else ""
    rec("Is there a written summary?", len(fx) > 400, f"{len(fx)} characters" if fx else "FINDINGS.md not created")
    for label, pats in [("the monthly trend", ["month"]),
                        ("the top categories per region", ["region"]),
                        ("the effect of discounts", ["discount"]),
                        ("which dates were odd", ["anomal", "outlier", "unusual"])]:
        rec(f"Does the summary cover {label}?", any(p in fx for p in pats),
            "mentioned" if any(p in fx for p in pats) else "not mentioned")
    rec("Does the summary contain real numbers?", len(re.findall(r"\d[\d,]*\.?\d*", fx)) >= 20,
        f"{len(re.findall(r'[0-9][0-9,]*[.]?[0-9]*', fx))} numeric tokens")
    shutil.rmtree(tmp, ignore_errors=True)
    print(json.dumps(out, indent=2))

if __name__ == "__main__":
    main(sys.argv[1])
