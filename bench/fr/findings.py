#!/usr/bin/env python3
"""Grade a review against planted defects.

The defects were put in the fixture deliberately and recorded in planted.json,
so this is a count against known ground truth rather than an opinion. No model
is consulted.

A defect counts as found only when the review mentions the relevant symbol AND
a phrase from that defect's class within the same passage. The window matters:
without it, a review that lists every function in one paragraph and every
generic risk in another would score full marks for saying nothing specific.
"""
import json
import os
import re
import sys

WINDOW = 700


def norm(s):
    return re.sub(r"\s+", " ", s.lower())


def found(review, bug):
    """Look for the symbol, then for a class keyword near it."""
    hay = norm(review)
    keys = [norm(k) for k in bug["keywords"]]

    anchors = [m.start() for m in re.finditer(re.escape(norm(bug["symbol"])), hay)]
    # A precise line reference is an equally good anchor.
    for off in range(-4, 5):
        for pat in (rf"\bline\s*{bug['line'] + off}\b", rf":{bug['line'] + off}\b"):
            anchors += [m.start() for m in re.finditer(pat, hay)]

    for a in anchors:
        seg = hay[max(0, a - WINDOW):a + WINDOW]
        for k in keys:
            if k in seg:
                return True, f"identified near {bug['symbol']}"
    if anchors:
        return False, f"mentions {bug['symbol']} but not the actual defect"
    return False, "not reported"


def main(fixture_dir, review_path):
    planted = json.load(open(os.path.join(fixture_dir, "planted.json")))
    review = open(review_path, errors="replace").read() if os.path.exists(review_path) else ""
    out = []
    if not review.strip():
        for b in planted["bugs"]:
            out.append({"q": f"Did it find the {b['id'].replace('-', ' ')}?",
                        "met": False, "note": "no review file was written"})
        print(json.dumps(out, indent=2))
        return
    for b in planted["bugs"]:
        ok, note = found(review, b)
        out.append({"q": f"Did it find the {b['id'].replace('-', ' ')} in {b['symbol']}?",
                    "met": ok, "note": note})
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
