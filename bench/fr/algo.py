#!/usr/bin/env python3
"""Grade merge_schedules against a hidden pytest suite.

The author never sees these tests. Each case is one pass or fail, so the score
is tests passed out of 20 with nothing subjective in it.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SUITE = os.path.join(HERE, "..", "fixtures", "algo", "test_hidden.py")


def case_names():
    return re.findall(r"^def (test_\w+)", open(SUITE).read(), re.M)


def main(work):
    names = case_names()
    src = os.path.join(work, "schedules.py")
    if not os.path.exists(src):
        print(json.dumps([{"q": n.replace("test_", "").replace("_", " "),
                           "met": False, "note": "schedules.py was not created"}
                          for n in names], indent=2))
        return

    tmp = tempfile.mkdtemp()
    shutil.copy2(src, os.path.join(tmp, "schedules.py"))
    shutil.copy2(SUITE, os.path.join(tmp, "test_hidden.py"))
    r = subprocess.run([sys.executable, "-m", "pytest", "test_hidden.py", "-v",
                        "--tb=no", "-p", "no:cacheprovider"],
                       cwd=tmp, capture_output=True, text=True, timeout=300)
    text = r.stdout + r.stderr
    shutil.rmtree(tmp, ignore_errors=True)

    out = []
    for n in names:
        m = re.search(rf"{re.escape(n)}\b.*?(PASSED|FAILED|ERROR)", text)
        verdict = m.group(1) if m else "NOT RUN"
        out.append({"q": n.replace("test_", "").replace("_", " "),
                    "met": verdict == "PASSED",
                    "note": verdict.lower() if verdict != "PASSED" else "passed"})
    if not any(o["met"] for o in out) and "ImportError" in text or "SyntaxError" in text:
        for o in out:
            o["note"] = "module failed to import"
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main(sys.argv[1])
