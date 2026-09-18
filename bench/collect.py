#!/usr/bin/env python3
"""Turn a runs/<stamp> directory into results.json.

Every cost figure here is recomputed from the raw token counts in the router's
ev:"usage" trace rows, using the measured price table in prices.json. The
router's own `spend` and `baseline_top` are carried alongside as a cross-check
and to quantify how far the shipped price table is off, but they are not the
numbers reported.

Three figures per run, which is the point of the exercise:

  actual         what this run consumed, at verified prices
  perceived_opus this run's own tokens extrapolated to Opus rates. What the
                 router's dashboard reports. An upper bound: a weaker model
                 that needs more turns has those extra turns billed in at
                 Opus rates, and so does a cache rebuild forced by a switch.
  measured_opus  what a real pinned-Opus run of the same task consumed. No
                 extrapolation. Median across repeats.
"""

import argparse
import json
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(HERE, "runs")
TOP = "claude-opus-5"


def load_prices():
    p = os.path.join(HERE, "prices.json")
    if not os.path.exists(p):
        sys.exit("prices.json missing. Run: python3 bench/calibrate_prices.py")
    return json.load(open(p))["table"]


def rate(table, model, field):
    """Prefix match, the way the router does, so dated ids resolve."""
    if model in table:
        return table[model][field]
    for k, v in table.items():
        base = k.rsplit("-20", 1)[0]
        if model.startswith(base) or base.startswith(model):
            return v[field]
    raise KeyError(f"no price for {model}")


def cost_of(table, model, inp, cr, cc, out, force=None):
    m = force or model
    i, o = rate(table, m, "in"), rate(table, m, "out")
    return (inp * i + cr * i * rate(table, m, "cache_read_mult")
            + cc * i * rate(table, m, "cache_write_mult") + out * o) / 1e6


def jread(path, default=None):
    try:
        return json.load(open(path))
    except Exception:
        return default


def collect_run(rd, table):
    meta = jread(os.path.join(rd, "meta.json"))
    if not meta:
        return None
    metrics = jread(os.path.join(rd, "metrics.json"), {}) or {}
    result = jread(os.path.join(rd, "result.json"), {}) or {}
    check = jread(os.path.join(rd, "check.json"), {}) or {}
    fr = jread(os.path.join(rd, "fr.json"), {}) or {}

    tdir = os.path.join(rd, "traces")
    usage, decisions = [], []
    for fn in sorted(os.listdir(tdir)) if os.path.isdir(tdir) else []:
        if not fn.endswith(".jsonl"):
            continue
        with open(os.path.join(tdir, fn)) as f:
            for line in f:
                try:
                    x = json.loads(line)
                except Exception:
                    continue
                if x.get("ev") == "usage":
                    usage.append(x)
                elif x.get("ev") == "decision":
                    decisions.append(x)

    actual = sum(cost_of(table, u["model"], u["inp"], u["cache_read"],
                         u["cache_create"], u["out"]) for u in usage)
    est_baseline = sum(cost_of(table, u["model"], u["inp"], u["cache_read"],
                               u["cache_create"], u["out"], force=TOP) for u in usage)

    mix = {}
    for u in usage:
        m = mix.setdefault(u["model"], {"calls": 0, "inp": 0, "cache_read": 0,
                                        "cache_create": 0, "out": 0, "cost": 0.0})
        m["calls"] += 1
        for k in ("inp", "cache_read", "cache_create", "out"):
            m[k] += u[k]
        m["cost"] += cost_of(table, u["model"], u["inp"], u["cache_read"],
                             u["cache_create"], u["out"])

    prefix = sum(u["inp"] + u["cache_read"] + u["cache_create"] for u in usage)
    cached_pct = (100.0 * sum(u["cache_read"] for u in usage) / prefix) if prefix else 0.0

    cms = [d["classify_ms"] for d in decisions
           if isinstance(d.get("classify_ms"), (int, float)) and d["classify_ms"] > 0]
    jev = metrics.get("jev") or {}

    # The client's own accounting is deliberately not used for any figure here.
    # It keys usage by the model it REQUESTED, not the one the router served, so
    # on a routed arm it prices Haiku and Sonnet tokens at Opus rates. Only its
    # error flag is read, and only to tell a broken run from a real one.
    return {
        "run_id": meta["run_id"], "task": meta["task"], "arm": meta["arm"], "rep": meta["rep"],
        "wall_s": meta.get("wall_s"), "timed_out": meta.get("timed_out"),
        "check_ok": bool(check.get("ok")), "check_why": check.get("why"),
        # Naive functional verdicts, from driving the artifact. Independent of
        # the LLM judge on purpose.
        "fr_met": fr.get("met"), "fr_total": fr.get("total"),
        "fr_failed": [v["q"] for v in fr.get("verdicts", []) if not v["met"]],

        "actual": round(actual, 6),
        "perceived_opus": round(est_baseline, 6),
        "jev_cost": round(jev.get("cost", 0.0), 6),
        "jev_calls": jev.get("calls", 0),
        "classify_ms_median": round(statistics.median(cms), 1) if cms else None,
        "classify_ms_p90": round(sorted(cms)[int(len(cms) * 0.9)], 1) if len(cms) >= 3 else None,

        "requests": metrics.get("requests_total"),
        "routed": metrics.get("routed"), "passthrough": metrics.get("passthrough"),
        "usage_rows": len(usage), "decision_rows": len(decisions),
        "model_mix": {k: {**v, "cost": round(v["cost"], 6)} for k, v in mix.items()},
        "cached_pct": round(cached_pct, 1),
        "tokens": {"inp": sum(u["inp"] for u in usage),
                   "cache_read": sum(u["cache_read"] for u in usage),
                   "cache_create": sum(u["cache_create"] for u in usage),
                   "out": sum(u["out"] for u in usage)},
        "reasons": sorted({d.get("why", "") for d in decisions if d.get("why")}),
        "phases": sorted({(d.get("ledger") or {}).get("phase_hint")
                          for d in decisions if (d.get("ledger") or {}).get("phase_hint")}),

        "client_error": bool(result.get("is_error")),
        "client_stop_reason": result.get("stop_reason"),

        "router_spend": metrics.get("spend"),
        "router_baseline_top": metrics.get("baseline_top"),
        "router_env": meta.get("router_env", {}),
        "artifact_present": check.get("bytes") is not None or check.get("ok"),
    }


def invariants(r):
    """Anything here firing means the measurement is not trustworthy."""
    bad = []
    if r["requests"] is not None and r["routed"] is not None:
        if r["routed"] + r["passthrough"] != r["requests"]:
            bad.append(f"routed({r['routed']}) + passthrough({r['passthrough']}) "
                       f"!= requests_total({r['requests']})")
    if r["arm"].startswith("control"):
        if r["decision_rows"]:
            bad.append(f"control arm wrote {r['decision_rows']} decision records, isolation broken")
        if r["jev_calls"]:
            bad.append(f"control arm made {r['jev_calls']} classifier calls, isolation broken")
        if r["routed"]:
            bad.append(f"control arm routed {r['routed']} requests")
    else:
        tiers = json.loads(r["router_env"].get("ROUTER_TIERS", "[]"))
        for m in r["model_mix"]:
            base = m.rsplit("-20", 1)[0]
            if not any(t == m or t == base or m.startswith(t) for t in tiers):
                bad.append(f"served {m}, which is not in ROUTER_TIERS {tiers}")
    if r["client_error"]:
        bad.append(f"client reported an error, stop_reason={r['client_stop_reason']}")
    if r["timed_out"]:
        bad.append("run hit its wall clock timeout")
    if r["usage_rows"] == 0:
        bad.append("no usage rows, nothing was measured")
    return bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stamp")
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    stamp_dir = os.path.join(RUNS, a.stamp)
    if not os.path.isdir(stamp_dir):
        sys.exit(f"no such run directory: bench/runs/{a.stamp}")
    table = load_prices()

    runs = []
    for name in sorted(os.listdir(stamp_dir)):
        rd = os.path.join(stamp_dir, name)
        if os.path.isdir(rd):
            r = collect_run(rd, table)
            if r:
                r["invariant_failures"] = invariants(r)
                runs.append(r)

    # The true Opus cost for a task is the control-opus run's actual spend.
    # "Measured Opus" for a task is what the pinned-Opus run of that same task
    # actually consumed. With repeats, take the median so one unlucky run does
    # not become the yardstick.
    per_task = {}
    for r in runs:
        if r["arm"] == "control-opus":
            per_task.setdefault(r["task"], []).append(r["actual"])
    truth = {k: statistics.median(v) for k, v in per_task.items()}
    for r in runs:
        tb = truth.get(r["task"])
        r["measured_opus"] = tb
        total = r["actual"] + r["jev_cost"]
        r["perceived_saving_pct"] = (round(100.0 * (r["perceived_opus"] - total) / r["perceived_opus"], 1)
                               if r["perceived_opus"] else None)
        r["measured_saving_pct"] = (round(100.0 * (tb - total) / tb, 1) if tb else None)
        if r["perceived_saving_pct"] is not None and r["measured_saving_pct"] is not None:
            r["overstatement_pts"] = round(r["perceived_saving_pct"] - r["measured_saving_pct"], 1)
        # how far the router's own price table is from the measured one
        if r["router_spend"]:
            r["router_table_ratio"] = round(r["actual"] / r["router_spend"], 3)

    out = {
        "stamp": a.stamp,
        "prices": table,
        "note": ("All costs recomputed from router ev:usage token counts at measured prices. "
                 "perceived_opus is this run's own tokens repriced at Opus rates, which is "
                 "what the router's dashboard reports. measured_opus is what a real pinned-Opus "
                 "run of the same task actually consumed. The client's own cost accounting is "
                 "not used anywhere."),
        "runs": runs,
    }
    path = a.out or os.path.join(stamp_dir, "results.json")
    with open(path, "w") as f:
        json.dump(out, f, indent=2, sort_keys=True)
        f.write("\n")

    print(f"{len(runs)} runs collected -> {os.path.relpath(path, os.path.dirname(HERE))}\n")
    hdr = (f"{'run':44} {'wall':>6} {'actual':>9} {'perceiv':>9} {'measured':>9} "
           f"{'perc%':>6} {'meas%':>6} {'chk':>4}")
    print(hdr); print("-" * len(hdr))
    for r in runs:
        print(f"{r['run_id']:44} {r['wall_s'] or 0:6.1f} {r['actual']:9.4f} "
              f"{r['perceived_opus']:9.4f} {(r['measured_opus'] or 0):9.4f} "
              f"{(r['perceived_saving_pct'] if r['perceived_saving_pct'] is not None else 0):6.1f} "
              f"{(r['measured_saving_pct'] if r['measured_saving_pct'] is not None else 0):6.1f} "
              f"{'ok' if r['check_ok'] else 'FAIL':>4}")
    bad = [(r["run_id"], r["invariant_failures"]) for r in runs if r["invariant_failures"]]
    if bad:
        print("\ninvariant failures:")
        for rid, fs in bad:
            for f_ in fs:
                print(f"  {rid}: {f_}")
    else:
        print("\nall invariants hold")


if __name__ == "__main__":
    main()
