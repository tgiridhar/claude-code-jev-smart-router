#!/usr/bin/env python3
"""Report for the classifier comparison: same ladder, two engines deciding it.

report.py answers a different question -- what routing is worth against a
pinned model -- and computes savings against the control-opus arm. This
comparison runs no controls, because the question is not what routing saves
but whether Jev and Laya route the same work the same way. Feeding these runs
to report.py would print a savings column with no yardstick behind it.

    python3 bench/collect.py <stamp>
    python3 bench/report_classifier.py <stamp>

Writes bench/runs/<stamp>/CLASSIFIER.md and prints the same to stdout.
"""

import argparse
import json
import os
import statistics
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNS = os.path.join(REPO, "bench", "runs")

JEV_ARM = "router-3tier"
LAYA_ARM = "router-3tier-laya"
LAYA_NATIVE_ARM = "router-3tier-laya-native"
SHORT = {JEV_ARM: "Jev", LAYA_ARM: "Laya (jev questions)",
         LAYA_NATIVE_ARM: "Laya (native)"}
ORDER = (JEV_ARM, LAYA_ARM, LAYA_NATIVE_ARM)

TIER_ORDER = ["claude-haiku-4-5", "claude-sonnet-5", "claude-opus-5"]
TIER_SHORT = {"claude-haiku-4-5": "haiku", "claude-sonnet-5": "sonnet",
              "claude-opus-5": "opus"}


def scrub(s):
    """Never write a path from the author's disk into a committed file."""
    return s.replace(os.path.expanduser("~"), "~").replace(REPO, ".")


def tier_share(mix):
    """Share of spend per rung. Tokens alone would understate the expensive
    rungs; spend is what the ladder exists to move."""
    tot = sum(v.get("cost", 0.0) for v in mix.values())
    if not tot:
        return {}
    out = {}
    for m, v in mix.items():
        key = TIER_SHORT.get(m, m)
        out[key] = out.get(key, 0.0) + 100.0 * v.get("cost", 0.0) / tot
    return {k: round(v) for k, v in out.items()}


def fmt_share(share):
    if not share:
        return "-"
    parts = [f"{TIER_SHORT[t][:1]}{share[TIER_SHORT[t]]}%"
             for t in TIER_ORDER if TIER_SHORT[t] in share]
    extra = [f"{k}{v}%" for k, v in share.items() if k not in TIER_SHORT.values()]
    return " ".join(parts + extra)


def med(vals):
    vals = [v for v in vals if v is not None]
    return statistics.median(vals) if vals else None


def cell(runs):
    """Aggregate the repeats of one task x arm."""
    if not runs:
        return None
    met = [r.get("fr_met") for r in runs if r.get("fr_met") is not None]
    tot = [r.get("fr_total") for r in runs if r.get("fr_total") is not None]
    costs = [r["actual"] + r.get("jev_cost", 0.0) for r in runs]
    share = {}
    for r in runs:
        for k, v in tier_share(r.get("model_mix") or {}).items():
            share[k] = share.get(k, 0.0) + v / len(runs)
    failed = sorted({q for r in runs for q in (r.get("fr_failed") or [])})
    return {
        "n": len(runs),
        "ok": sum(1 for r in runs if r.get("check_ok")),
        "met": med(met), "total": tot[0] if tot else None,
        "failed": failed,
        "cost": med(costs),
        "wall": med([r.get("wall_s") for r in runs]),
        "share": {k: round(v) for k, v in share.items()},
        "cls_ms": med([r.get("classify_ms_median") for r in runs]),
        "calls": med([r.get("jev_calls") for r in runs]),
        "routed": med([r.get("routed") for r in runs]),
        "passthrough": med([r.get("passthrough") for r in runs]),
        "reasons": sorted({x for r in runs for x in (r.get("reasons") or [])}),
        "errors": sum(1 for r in runs if r.get("client_error") or r.get("timed_out")),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stamp")
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    stamp_dir = os.path.join(RUNS, a.stamp)
    res = os.path.join(stamp_dir, "results.json")
    if not os.path.exists(res):
        sys.exit(f"no results.json in {a.stamp}; run collect.py first")
    data = json.load(open(res))
    runs = data["runs"]

    arms = [x for x in ORDER if any(r["arm"] == x for r in runs)]
    tasks = sorted({r["task"] for r in runs})

    by = {}
    for t in tasks:
        for arm in arms:
            by[(t, arm)] = cell([r for r in runs if r["task"] == t and r["arm"] == arm])

    shim = None
    sp = os.path.join(stamp_dir, "laya_shim_stats.json")
    if os.path.exists(sp):
        shim = json.load(open(sp))

    L = []
    w = L.append
    w("# Jev vs Laya on the same ladder\n")
    w("Both arms are the router's shipped three-rung ladder "
      "(`haiku-4-5` / `sonnet-5` / `opus-5`) with identical prices, identical")
    w("client model, and identical effort. The only difference is which model "
      "answers the 15 typed questions. There are no pinned controls: the")
    w("question is not what routing saves, which `RESULTS.md` answers, but "
      "whether the two classifiers route the same work the same way.\n")
    w(f"Stamp `{a.stamp}`. {len(runs)} runs over {len(tasks)} task(s).\n")

    w("| task | classifier | requirements met | artifact ok | cost | wall | spend by rung | classify |")
    w("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for t in tasks:
        for arm in arms:
            c = by[(t, arm)]
            if not c:
                continue
            reqs = (f"{c['met']:.0f}/{c['total']}"
                    if c["met"] is not None and c["total"] else "-")
            ms = f"{c['cls_ms']:.0f} ms" if c["cls_ms"] else "-"
            w(f"| {t} | {SHORT[arm]} | {reqs} | {c['ok']}/{c['n']} | "
              f"${c['cost']:.3f} | {c['wall']:.0f}s | {fmt_share(c['share'])} | "
              f"{ms} x{c['calls']:.0f} |")
    w("")

    # Headline deltas, per task, only where both arms ran.
    both = [t for t in tasks if by.get((t, JEV_ARM)) and by.get((t, LAYA_ARM))]
    if both:
        w("## Delta\n")
        w("| task | requirements | cost | classify latency |")
        w("| --- | --- | --- | --- |")
        for t in both:
            j, l = by[(t, JEV_ARM)], by[(t, LAYA_ARM)]
            if j["met"] is not None and l["met"] is not None:
                d = l["met"] - j["met"]
                rq = f"{d:+.0f} ({SHORT[LAYA_ARM]} {l['met']:.0f} vs {j['met']:.0f})"
            else:
                rq = "-"
            cd = (f"{100.0 * (l['cost'] - j['cost']) / j['cost']:+.0f}%"
                  if j["cost"] else "-")
            ld = (f"{l['cls_ms'] / j['cls_ms']:.1f}x slower"
                  if j["cls_ms"] and l["cls_ms"] else "-")
            w(f"| {t} | {rq} | {cd} | {ld} |")
        w("")

    if shim:
        w("## Laya shim\n")
        w(f"- {shim.get('calls')} classifications, "
          f"{shim.get('avg_ms')} ms average, {shim.get('errors')} errors")
        w(f"- confidence reported as `{shim.get('conf_mode')}`")
        w("")

    w("## Routing reasons seen\n")
    for arm in arms:
        rs = sorted({x for t in tasks if by.get((t, arm))
                     for x in by[(t, arm)]["reasons"]})
        w(f"**{SHORT[arm]}**: {', '.join(f'`{x}`' for x in rs) if rs else 'none recorded'}\n")

    w("## What this does and does not measure\n")
    w("Three things have to be stated for the numbers above to be read "
      "correctly.\n")
    w("**Laya is zero-shot here; Jev is not.** Jev is the classifier this "
      "ontology was written against and its thresholds were tuned on. The "
      "Laya checkpoint is an off-the-shelf general decision model whose "
      "published examples are ticket triage, spam and moderation. It has "
      "never seen a router state. A gap in these tables is therefore not a "
      "statement about the two models' capacity; it is mostly a statement "
      "about fit. Laya ships a fine-tuning path, and a fitted Laya is the "
      "comparison this one does not make.\n")
    w("**Laya sees about half the state, and that is not what is wrong.** Its "
      "English checkpoint sets `max_len` 512 and `head_max_len` 192, leaving "
      "roughly 320 tokens against a router state of 676 tokens on the "
      "seven-step demo in `jev_ontology.__main__`. `laya_shim.py` truncates "
      "from the left so the budget is spent on the tail that decides the "
      "tier. This looked like the binding constraint and is not: ablating "
      "truncation direction and raising `max_len` to 1024 and 2048 moves "
      "choice accuracy from 2/8 to 3/8 and leaves noul accuracy at 3/6 with "
      "the probabilities unchanged to two decimals. Four times the context "
      "buys nothing. See `bench/calibrate_classifier.py`.\n")
    w("**Confidence was put on a common scale.** Jev reports a calibrated "
      "peak probability; Laya reports normalized Shannon entropy, which is a "
      "different quantity. Left alone, every Laya choice and score answer "
      "fell below `ROUTER_MIN_CONFIDENCE` and the router discarded all of "
      "them, so the arm would not have been testing a classifier at all. The "
      "shim reports `max(probabilities)` instead. The distributions are "
      "untouched.\n")
    w("**Latency here is not Laya's latency.** It runs on CPU/MPS on this "
      "machine; its published figure is 32.8 ms on a single GPU. Both arms "
      "were given a 20 s classify timeout so that neither could be silently "
      "nulled out by a timeout, which `classify()` would have reported as an "
      "ordinary passthrough. Read the classify column as a property of this "
      "host, not of the model.\n")
    w("Cost: Laya's classifier column is $0 because a self-hosted model has "
      "no per-call price. Hardware cost is real and is not in this table.\n")

    text = scrub("\n".join(L) + "\n")
    out = a.out or os.path.join(stamp_dir, "CLASSIFIER.md")
    with open(out, "w") as f:
        f.write(text)
    print(text)
    print(f"wrote {scrub(out)}")


if __name__ == "__main__":
    main()
