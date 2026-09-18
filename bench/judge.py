#!/usr/bin/env python3
"""Blind LLM judging of the artifacts a run produced.

Two passes per task:

  absolute  each artifact scored on its own against the task rubric, one call
            per artifact, judge never sees the others.
  ranking   all artifacts in one call, ranked. Run twice with independently
            shuffled presentation order. If the two orderings disagree about
            a pair, that pair is recorded as a tie rather than a result, which
            is how position bias gets absorbed instead of reported.

The judge never learns which arm produced what. Labels are letters assigned
per call. Absolute paths are scrubbed out of every artifact before it is sent.
The judge runs straight to the API, not through the router, so judging never
lands in the savings arithmetic.
"""

import argparse
import itertools
import json
import os
import random
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_bench as rb
import tasks as tasks_mod

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_CHARS = 60000

CRITERIA = ["correctness", "completeness", "craft"]

ABS_SCHEMA = {
    "type": "object",
    "properties": {
        "correctness": {"type": "integer", "minimum": 0, "maximum": 5},
        "completeness": {"type": "integer", "minimum": 0, "maximum": 5},
        "craft": {"type": "integer", "minimum": 0, "maximum": 5},
        "overall": {"type": "integer", "minimum": 0, "maximum": 5},
        "notes": {"type": "string"},
    },
    "required": ["correctness", "completeness", "craft", "overall", "notes"],
    "additionalProperties": False,
}


def rank_schema(labels):
    return {
        "type": "object",
        "properties": {
            "ranking": {"type": "array", "items": {"type": "string", "enum": labels},
                        "minItems": len(labels), "maxItems": len(labels)},
            "reason": {"type": "string"},
        },
        "required": ["ranking", "reason"],
        "additionalProperties": False,
    }


def scrub(text):
    text = re.sub(r"/Users/[^\s\"'`)\]]+", "<path>", text)
    text = re.sub(r"/private/tmp/[^\s\"'`)\]]+", "<path>", text)
    return text


def read_artifact(stamp, run_id, artifact):
    p = os.path.join(rb.RUNS, stamp, run_id, "work", artifact)
    if not os.path.exists(p):
        return None
    with open(p, errors="replace") as f:
        t = f.read()
    t = scrub(t)
    if len(t) > MAX_CHARS:
        t = t[:MAX_CHARS] + f"\n\n[truncated at {MAX_CHARS} characters for judging]"
    return t


def ask(prompt, schema, timeout=420):
    argv = [rb.CLAUDE, "-p", prompt, "--output-format", "json", "--model", "opus",
            "--effort", "medium", "--setting-sources", "project", "--tools", "",
            # Structured output arrives as a tool call, so the run needs turns to
            # deliberate, emit it and close. Opus takes 6 at medium effort; too
            # low a cap returns error_max_turns with stop_reason tool_use, no
            # result, and the call still bills.
            "--max-turns", "10", "--json-schema", json.dumps(schema)]
    r = subprocess.run(argv, cwd=HERE, env=rb.child_env(None),
                       capture_output=True, text=True, timeout=timeout)
    try:
        env = json.loads(r.stdout)
    except Exception:
        return None, 0.0, f"unparseable stdout: {(r.stdout or '')[-300:]}"
    cost = env.get("total_cost_usd", 0.0) or 0.0
    so = env.get("structured_output")
    if so:
        return so, cost, None
    try:
        return json.loads(env.get("result", "")), cost, None
    except Exception:
        return None, cost, (f"no structured output; subtype={env.get('subtype')} "
                            f"stop_reason={env.get('stop_reason')} "
                            f"errors={env.get('errors')} result={str(env.get('result'))[:200]}")


def judge_task(stamp, task, entries, rng):
    """entries: list of (arm, artifact_text)."""
    out = {"task": task.name, "absolute": {}, "rankings": [], "cost": 0.0, "errors": []}

    for arm, text in entries:
        prompt = (
            f"You are grading one submission against a brief. Be strict and specific.\n\n"
            f"THE BRIEF GIVEN TO THE AUTHOR:\n{task.prompt}\n\n"
            f"WHAT TO WEIGH:\n{task.rubric}\n\n"
            f"Score 0 to 5 on each of: correctness (is it right, does it work, are claims true), "
            f"completeness (is the whole brief covered), craft (organisation, clarity, restraint). "
            f"Then an overall 0 to 5. notes must be under 500 characters: state the decisive "
            f"reasons only, no preamble, no restating the brief.\n\n"
            f"SUBMISSION:\n```\n{text}\n```"
        )
        res, cost, err = ask(prompt, ABS_SCHEMA)
        out["cost"] += cost
        if err:
            out["errors"].append(f"absolute/{arm}: {err}")
        else:
            out["absolute"][arm] = res

    for pass_no in (1, 2):
        order = list(entries)
        rng.shuffle(order)
        labels = [chr(ord("A") + i) for i in range(len(order))]
        body = "\n\n".join(
            f"=== SUBMISSION {lab} ===\n```\n{text}\n```"
            for lab, (_, text) in zip(labels, order))
        prompt = (
            f"You are ranking {len(order)} submissions against the same brief. "
            f"They were produced independently. Rank them best to worst.\n\n"
            f"THE BRIEF:\n{task.prompt}\n\nWHAT TO WEIGH:\n{task.rubric}\n\n"
            f"Return ranking as a list of labels, best first. Keep reason to about 60 "
            f"words: name what separates them, no preamble.\n\n"
            f"{body}")
        res, cost, err = ask(prompt, rank_schema(labels), timeout=600)
        out["cost"] += cost
        if err:
            out["errors"].append(f"ranking/{pass_no}: {err}")
            continue
        mapping = {lab: arm for lab, (arm, _) in zip(labels, order)}
        out["rankings"].append({
            "pass": pass_no,
            "presented": [mapping[l] for l in labels],
            "ranked": [mapping[l] for l in res["ranking"] if l in mapping],
            "reason": res.get("reason", ""),
        })
    return out


def consensus(rankings, arms):
    """Pairwise winner only where both passes agree. Disagreement is a tie."""
    if len(rankings) < 2:
        return {}, {}
    pos = [{a: r["ranked"].index(a) for a in arms if a in r["ranked"]} for r in rankings]
    wins, ties = {a: 0 for a in arms}, {a: 0 for a in arms}
    for x, y in itertools.combinations(arms, 2):
        if not all(x in p and y in p for p in pos):
            continue
        v = [(p[x] < p[y]) for p in pos]
        if all(v):
            wins[x] += 1
        elif not any(v):
            wins[y] += 1
        else:
            ties[x] += 1
            ties[y] += 1
    return wins, ties


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stamp")
    ap.add_argument("--seed", type=int, default=20260918)
    a = ap.parse_args()

    res_path = os.path.join(rb.RUNS, a.stamp, "results.json")
    if not os.path.exists(res_path):
        sys.exit(f"no results.json for {a.stamp}. Run collect.py first.")
    results = json.load(open(res_path))
    rng = random.Random(a.seed)

    by_task = {}
    for r in results["runs"]:
        by_task.setdefault(r["task"], []).append(r)

    out = {"stamp": a.stamp, "seed": a.seed, "tasks": [], "total_cost": 0.0}
    for tname, runs in sorted(by_task.items()):
        task = tasks_mod.BY_NAME[tname]
        # With repeats, judge one representative run per arm: the median-cost
        # one. Judging all of them would blow up the ranking prompt and weight
        # the result toward whichever arm happened to be run most.
        by_arm = {}
        for r in runs:
            by_arm.setdefault(r["arm"], []).append(r)
        picked = []
        for arm, rs in sorted(by_arm.items()):
            rs.sort(key=lambda x: x["actual"])
            picked.append(rs[len(rs) // 2])

        entries = []
        for r in picked:
            text = read_artifact(a.stamp, r["run_id"], task.artifact)
            if text is None:
                print(f"  {tname}/{r['arm']}: no artifact, excluded from judging")
                continue
            entries.append((r["arm"], text))
        if len(entries) < 2:
            print(f"  {tname}: fewer than 2 artifacts, skipping")
            continue
        print(f"judging {tname}: {[a_ for a_, _ in entries]}")
        j = judge_task(a.stamp, task, entries, rng)
        arms_ = [a_ for a_, _ in entries]
        w, t = consensus(j["rankings"], arms_)
        j["pairwise_wins"], j["pairwise_ties"] = w, t
        out["tasks"].append(j)
        out["total_cost"] += j["cost"]
        print(f"  cost ${j['cost']:.4f}  wins {w}  ties {t}")
        for e in j["errors"]:
            print(f"  ERROR {e}")

    path = os.path.join(rb.RUNS, a.stamp, "judgments.json")
    with open(path, "w") as f:
        json.dump(out, f, indent=2, sort_keys=True)
        f.write("\n")
    print(f"\njudging cost ${out['total_cost']:.4f}, written to bench/runs/{a.stamp}/judgments.json")


if __name__ == "__main__":
    main()
