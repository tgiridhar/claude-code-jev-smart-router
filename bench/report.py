#!/usr/bin/env python3
"""results.json + judgments.json -> RESULTS.md.

Publishes prompts, durations, costs and judge verdicts. Never task outputs,
never traces, never absolute paths.
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import arms as arms_mod
import run_bench as rb
import tasks as tasks_mod

HERE = os.path.dirname(os.path.abspath(__file__))


def pct(x):
    return "-" if x is None else f"{x:.1f}%"


def usd(x):
    return "-" if x is None else f"${x:.4f}"


def scrub(text):
    """Nothing in a published file may name a path on the author's disk."""
    text = re.sub(r"/Users/[^\s\"'`)\]|]+", "<path>", text)
    text = re.sub(r"/private/tmp/[^\s\"'`)\]|]+", "<path>", text)
    return text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stamp")
    ap.add_argument("--out", default=os.path.join(HERE, "RESULTS.md"))
    a = ap.parse_args()

    rd = os.path.join(rb.RUNS, a.stamp)
    results = json.load(open(os.path.join(rd, "results.json")))
    jpath = os.path.join(rd, "judgments.json")
    judg = json.load(open(jpath)) if os.path.exists(jpath) else {"tasks": []}
    jby = {t["task"]: t for t in judg.get("tasks", [])}

    runs = results["runs"]
    tasks_seen = sorted({r["task"] for r in runs}, key=lambda n: [t.name for t in tasks_mod.TASKS].index(n))
    arms_seen = [a_ for a_ in [x.name for x in arms_mod.ARMS] if any(r["arm"] == a_ for r in runs)]
    by = {(r["task"], r["arm"]): r for r in runs}

    L = []
    w = L.append
    w("# Benchmark results\n")
    w("Produced by `bench/run_bench.py`.\n")
    w("**Every cost here is counted by the router itself.** The proxy sits in the request")
    w("path on every arm, controls included, and reads the token counts off the relayed")
    w("response. The client's own cost accounting is not used anywhere, because it keys")
    w("usage by the model it asked for rather than the one the router served, and so")
    w("prices Haiku and Sonnet tokens at Opus rates.\n")
    w("Two Opus numbers appear throughout, and the gap between them is the point:\n")
    w("- **Perceived Opus** is this run's own tokens extrapolated to Opus rates. It is")
    w("  what the router's dashboard reports, and it assumes the work would have taken")
    w("  the same tokens on Opus.")
    w("- **Measured Opus** is what a real pinned-Opus run of the same task actually")
    w("  consumed. No extrapolation.\n")
    w("Perceived is an upper bound: when a cheaper model needs more turns, those extra")
    w("turns get billed into it at Opus rates, and a cache rebuild forced by a model")
    w("switch lands there too. Both distortions flatter the router.\n")

    # ---- the price finding -------------------------------------------------
    w("## Prices used\n")
    w("Measured rather than assumed. `bench/calibrate_prices.py` solves each rate from")
    w("observed token counts against the cost Claude Code computed for those same tokens.\n")
    w("| Model | Input $/MTok | Output $/MTok | Cache write | Cache read |")
    w("| --- | --- | --- | --- | --- |")
    for m, p in sorted(results["prices"].items()):
        w(f"| `{m}` | {p['in']:g} | {p['out']:g} | {p['cache_write_mult']:g}x | {p['cache_read_mult']:g}x |")
    w("")
    w("Two of these disagree with the router's shipped `ROUTER_PRICES` defaults:\n")
    w("- Sonnet 5 is **$2/$10**, not $3/$15. The shipped table overprices it by 50%.")
    w("- Claude Code writes its prompt cache with a **1-hour TTL**, which bills at **2.0x**")
    w("  the input rate. The shipped `ROUTER_CACHE_WRITE_MULT` of 1.25 is the 5-minute rate.\n")
    w("The TTL is not inferred from the price arithmetic. Captured off the wire, the request")
    w("Claude Code sends carries:\n")
    w("```json")
    w('"cache_control": {"type": "ephemeral", "ttl": "1h"}')
    w("```\n")
    w("and the API's own usage block in the response confirms where the tokens landed:\n")
    w("```json")
    w('"cache_creation": {"ephemeral_5m_input_tokens": 0,')
    w('                   "ephemeral_1h_input_tokens": 9206}')
    w("```\n")
    w("This is not only an accounting error. `switch_delta()` prices every routing decision")
    w("off that table, so a router on the shipped defaults believes the middle rung costs 3x")
    w("Haiku when it costs 2x, and believes a cache rebuild is cheaper than it is. Both push")
    w("it away from Sonnet. The router arms below run on corrected prices.\n")
    w("`ROUTER_CACHE_TTL_SAFE` inherits the same false premise. It is set to 240 seconds,")
    w("reasoned down from a 300-second cache lifetime. The real lifetime is 3600 seconds, so")
    w("the router treats a cache as cold after four minutes when it has another fifty-six")
    w("left, and prices a downgrade as if there were no warm cache to discard. That makes it")
    w("systematically too willing to switch models. The `todo` / `router-3tier` run below is")
    w("that failure in practice: cache hits fell from 80% to 35% and the cheaper model cost")
    w("24% more than Opus.\n")

    # ---- arms --------------------------------------------------------------
    w("## Arms\n")
    w("| Arm | Client asks for | Router | Notes |")
    w("| --- | --- | --- | --- |")
    for n in arms_seen:
        arm = arms_mod.BY_NAME[n]
        tiers = json.loads(arm.router_env.get("ROUTER_TIERS", "null") or "null")
        desc = "disabled, pure passthrough" if arm.router_env.get("ROUTER_ENABLED") == "0" else \
            " / ".join(t.replace("claude-", "") for t in tiers)
        w(f"| `{n}` | {arm.model} | {desc} | {arm.note} |")
    w("")
    w("Every arm runs through the router process, controls included, so all tokens are")
    w("counted by the same sniffer and priced from the same table. Each run gets its own")
    w("router process, working directory, trace directory and session id.\n")

    # ---- per task ----------------------------------------------------------
    w("## Per task\n")
    for tn in tasks_seen:
        task = tasks_mod.BY_NAME[tn]
        w(f"### {tn}\n")
        w("The ask, verbatim:\n")
        w("```")
        for line in task.prompt.strip().splitlines():
            w(line)
        w("```\n")
        w(f"Caps: {task.max_turns} turns, ${task.budget_usd:.2f} budget, {task.timeout_s}s wall clock.\n")
        w("| Arm | Requirements met | Wall | Cost | Perceived Opus | Measured Opus | Perceived saving | Measured saving | Quality |")
        w("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        j = jby.get(tn, {})
        absol = j.get("absolute", {})
        for an in arms_seen:
            r = by.get((tn, an))
            if not r:
                continue
            sc = absol.get(an, {})
            jt = f"{sc['overall']}/5" if sc else "-"
            frs = (f"{r['fr_met']}/{r['fr_total']}" if r.get("fr_total") else "-")
            if r.get("fr_total") and r["fr_met"] != r["fr_total"]:
                frs = f"**{frs}**"
            w(f"| `{an}` | {frs} | {r['wall_s']:.0f}s | "
              f"{usd(r['actual'] + r['jev_cost'])} | {usd(r['perceived_opus'])} | "
              f"{usd(r['measured_opus'])} | {pct(r['perceived_saving_pct'])} | "
              f"{pct(r['measured_saving_pct'])} | {jt} |")
        w("")
        mixes = [(an, by[(tn, an)]["model_mix"]) for an in arms_seen
                 if (tn, an) in by and not an.startswith("control")]
        if mixes:
            w("Model mix on the routed arms:\n")
            for an, mix in mixes:
                parts = [f"{m.replace('claude-', '').rsplit('-20', 1)[0]} x{v['calls']}"
                         for m, v in sorted(mix.items())]
                w(f"- `{an}`: {', '.join(parts)}")
            w("")
        if j.get("rankings"):
            for rk in j["rankings"]:
                w(f"Judge ranking, pass {rk['pass']}: " +
                  " > ".join(f"`{x}`" for x in rk["ranked"]))
            w("")
            if j.get("pairwise_wins"):
                w(f"Pairwise wins where both orderings agreed: "
                  f"{', '.join(f'`{k}` {v}' for k, v in sorted(j['pairwise_wins'].items()))}. "
                  f"Disagreements are counted as ties, not results.\n")

    # ---- aggregate ---------------------------------------------------------
    w("## Aggregate\n")
    w("| Arm | Runs | Fully working builds | Total cost | Total measured Opus | Measured saving | Mean quality | Mean wall |")
    w("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for an in arms_seen:
        rs = [r for r in runs if r["arm"] == an]
        if not rs:
            continue
        tot = sum(r["actual"] + r["jev_cost"] for r in rs)
        truth = sum(r["measured_opus"] for r in rs if r["measured_opus"])
        ok = sum(1 for r in rs if r.get("fr_total") and r["fr_met"] == r["fr_total"])
        scores = [jby[r["task"]]["absolute"][an]["overall"]
                  for r in rs if r["task"] in jby and an in jby[r["task"]].get("absolute", {})]
        save = f"{100.0 * (truth - tot) / truth:.1f}%" if truth else "-"
        w(f"| `{an}` | {len(rs)} | {ok}/{len(rs)} | {usd(tot)} | {usd(truth) if truth else '-'} | "
          f"{save} | {sum(scores)/len(scores):.2f} | {sum(r['wall_s'] for r in rs)/len(rs):.0f}s |"
          if scores else
          f"| `{an}` | {len(rs)} | {ok}/{len(rs)} | {usd(tot)} | {usd(truth) if truth else '-'} | "
          f"{save} | - | {sum(r['wall_s'] for r in rs)/len(rs):.0f}s |")
    w("")

    # ---- overstatement -----------------------------------------------------
    over = [r for r in runs if r.get("overstatement_pts") is not None and not r["arm"].startswith("control")]
    if over:
        w("## How far the dashboard overstates\n")
        w("Perceived saving against measured saving, per routed run:\n")
        w("| Run | Perceived saving | Measured saving | Overstated by |")
        w("| --- | --- | --- | --- |")
        for r in sorted(over, key=lambda x: (x["task"], x["arm"])):
            w(f"| `{r['task']}` / `{r['arm']}` | {pct(r['perceived_saving_pct'])} | "
              f"{pct(r['measured_saving_pct'])} | {r['overstatement_pts']:.1f} points |")
        w("")

    # ---- classifier --------------------------------------------------------
    rr = [r for r in runs if r["jev_calls"]]
    if rr:
        w("## Classifier\n")
        lat = [r["classify_ms_median"] for r in rr if r["classify_ms_median"]]
        w(f"- {sum(r['jev_calls'] for r in rr)} calls across {len(rr)} routed runs")
        w(f"- total classifier spend {usd(sum(r['jev_cost'] for r in rr))}")
        if lat:
            w(f"- median latency {sum(lat)/len(lat):.0f} ms")
        w("")

    # ---- limitations -------------------------------------------------------
    w("## What these numbers are not\n")
    w("- One run per cell. Per-task figures are noisy; only the aggregate and the direction")
    w("  of the tier verdict carry weight.")
    w("- Six tasks chosen by hand, not a sample of real work.")
    w("- Costs are list-price estimates on both sides. On a subscription no money changes")
    w("  hands per token, so treat these as a comparable unit, not a bill.")
    w("- The router only sees `/v1/messages`. Anything Claude Code does off that path is")
    w("  invisible to it.")
    w("- The judge is Opus scoring output that was sometimes produced by Opus. Blinding and")
    w("  order swapping are in place; self-preference is not otherwise controlled for.")
    if judg.get("total_cost"):
        w(f"- Judging cost {usd(judg['total_cost'])}, excluded from every figure above.")
    w("")

    text = scrub("\n".join(L))
    leaks = re.findall(r"/Users/|/private/tmp/", text)
    if leaks:
        sys.exit(f"refusing to write: {len(leaks)} path leaks survived scrubbing")
    with open(a.out, "w") as f:
        f.write(text)
    print(f"wrote {os.path.relpath(a.out, os.path.dirname(HERE))} ({len(text)} bytes, no path leaks)")


if __name__ == "__main__":
    main()
