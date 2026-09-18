#!/usr/bin/env python3
"""Solve for the real per-model rates instead of trusting a table.

One tiny request per model through a disabled router. The router's sniffer
gives exact token counts; Claude Code's modelUsage gives the cost it computed
for those same tokens. Every pricing tier in Claude Code's catalog has the
same internal structure:

    output       = 5.0  x input
    cache_read   = 0.1  x input
    cache_write  = 1.25 x input   (5-minute TTL)
                 = 2.0  x input   (1-hour TTL)

So one observation leaves a single unknown, the input rate. Solve it under
both cache-write hypotheses and see which produces a clean catalog number.
That identifies the rate AND which cache TTL the client is buying.

Costs a few cents total.
"""

import json
import os
import subprocess
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import arms as A
import run_bench as rb

MODELS = ["haiku", "sonnet", "opus"]
CATALOG = [1.0, 2.0, 3.0, 5.0, 10.0, 15.0]  # input rates present in the catalog tiers


def one(model):
    arm = A.Arm("cal", model, {"ROUTER_ENABLED": "0"}, False, "")
    td = tempfile.mkdtemp()
    traces, port, sid = os.path.join(td, "tr"), rb.free_port(), str(uuid.uuid4())
    p, lg = rb.start_router(arm, port, traces, os.path.join(td, "r.log"))
    try:
        r = subprocess.run(
            [rb.CLAUDE, "-p", "Reply with exactly the word OK.", "--output-format", "json",
             "--model", model, "--effort", "medium", "--setting-sources", "project",
             "--permission-mode", "bypassPermissions", "--tools", "", "--max-turns", "2",
             "--session-id", sid],
            cwd=td, env=rb.child_env(f"http://127.0.0.1:{port}"),
            capture_output=True, text=True, timeout=240)
        env = json.loads(r.stdout)
    finally:
        rb.stop_router(p, lg)

    tot = {"inp": 0, "cache_read": 0, "cache_create": 0, "out": 0, "cost": 0.0}
    with open(os.path.join(traces, sid + ".jsonl")) as f:
        for line in f:
            x = json.loads(line)
            if x.get("ev") == "usage":
                for k in tot:
                    tot[k] += x[k]
    mu = env.get("modelUsage") or {}
    served = sorted(mu)[0] if mu else "?"
    return served, tot, env.get("total_cost_usd", 0.0), mu.get(served, {}).get("costBasis")


def solve(tot, cost, cw_mult):
    denom = tot["inp"] + cw_mult * tot["cache_create"] + 0.1 * tot["cache_read"] + 5.0 * tot["out"]
    return cost * 1e6 / denom if denom else float("nan")


def nearest(x):
    best = min(CATALOG, key=lambda c: abs(c - x))
    return best, abs(best - x) / best * 100


def main():
    print(f"{'model':34} {'served tokens (in/cr/cw/out)':34} {'CC cost':>10}  "
          f"{'in@1.25x':>9} {'in@2.0x':>9}  verdict")
    print("-" * 122)
    rows = []
    for m in MODELS:
        served, tot, cost, basis = one(m)
        a, b = solve(tot, cost, 1.25), solve(tot, cost, 2.0)
        na, da = nearest(a)
        nb, db = nearest(b)
        if db < da and db < 1.0:
            verdict, rate, ttl = f"${nb:g}/MTok input, 1-hour cache", nb, "1h"
        elif da < 1.0:
            verdict, rate, ttl = f"${na:g}/MTok input, 5-minute cache", na, "5m"
        else:
            verdict, rate, ttl = "no clean fit", None, "?"
        toks = f"{tot['inp']}/{tot['cache_read']}/{tot['cache_create']}/{tot['out']}"
        print(f"{served:34} {toks:34} {cost:10.6f}  {a:9.4f} {b:9.4f}  {verdict}")
        rows.append({"alias": m, "served": served, "tokens": tot, "cc_cost": cost,
                     "cost_basis": basis, "input_rate": rate, "cache_ttl": ttl,
                     "router_cost": round(tot["cost"], 6)})

    print()
    print("router's own figure vs Claude Code's, same tokens:")
    for r in rows:
        if r["router_cost"]:
            print(f"  {r['served']:34} router ${r['router_cost']:.6f}  "
                  f"claude code ${r['cc_cost']:.6f}  ratio {r['cc_cost']/r['router_cost']:.3f}")

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prices.json")
    table = {}
    for r in rows:
        if r["input_rate"]:
            table[r["served"]] = {"in": r["input_rate"], "out": r["input_rate"] * 5.0,
                                  "cache_read_mult": 0.1,
                                  "cache_write_mult": 2.0 if r["cache_ttl"] == "1h" else 1.25}
    with open(out, "w") as f:
        json.dump({"measured_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
                   "note": "solved from observed tokens against Claude Code's own cost figure",
                   "table": table, "observations": rows}, f, indent=2, sort_keys=True)
        f.write("\n")
    print(f"\nwrote {os.path.basename(out)}")


if __name__ == "__main__":
    main()
