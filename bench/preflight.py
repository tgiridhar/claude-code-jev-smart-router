#!/usr/bin/env python3
"""Preflight checks. Everything except --live costs nothing.

Run this before spending two hours of API budget.
"""

import itertools
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import arms as arms_mod
import run_bench as rb
import tasks as tasks_mod

PASS, FAIL = "PASS", "FAIL"
results = []


def check(name, fn):
    try:
        ok, detail = fn()
    except Exception as e:
        ok, detail = False, f"{type(e).__name__}: {e}"
    results.append((name, ok, detail))
    print(f"  [{PASS if ok else FAIL}] {name}")
    for line in str(detail).splitlines():
        print(f"         {line}")
    return ok


# ---------------------------------------------------------------------------

def t_tier_ladder():
    """Sweep pick_tier_v2 over both ladder shapes.

    The point of the router-haiku-opus arm is a two-rung ROUTER_TIERS. Confirm
    every reachable tier index is in range for n_tiers=2, since an out-of-range
    index would throw inside a live request and the arm would be worthless.
    """
    import jev_ontology as o

    led = o.extract_ledger({"tools": [{"name": n} for n in ("Bash", "Read", "Edit")],
                            "messages": [{"role": "user", "content": "do the thing"}]})
    phases = list(o.PHASES)
    out = {}
    for n_tiers in (2, 3):
        seen, errs = set(), []
        for phase, demand, risk, oracle, traj in itertools.product(
                phases, (0, 1, 2), (0.0, 0.9), (0.0, 0.9),
                ("just_started", "progressing", "thrashing", "drifting")):
            ans = {
                "phase": {"choice": phase, "confidence": 0.9},
                "next_step_demand": {"score": demand, "confidence": 0.9},
                "spec_completeness": {"score": 1, "confidence": 0.9},
                "gen_volume": {"score": 1, "confidence": 0.9},
                "trajectory": {"choice": traj, "confidence": 0.9},
                "review_depth": {"choice": "not_review", "confidence": 0.9},
                "risk_surface": {"noul": risk, "confidence": 0.9},
                "oracle_in_loop": {"noul": oracle, "confidence": 0.9},
                "irreversible": {"noul": 0.1, "confidence": 0.9},
                "cross_cutting": {"noul": 0.3, "confidence": 0.9},
                "repo_specific": {"noul": 0.3, "confidence": 0.9},
                "canonical": {"noul": 0.3, "confidence": 0.9},
                "unverified_confidence": {"noul": 0.2, "confidence": 0.9},
                "user_correcting": {"noul": 0.1, "confidence": 0.9},
                "is_followup": {"noul": 0.1, "confidence": 0.9},
            }
            for cur in range(n_tiers):
                try:
                    p = o.pick_tier_v2(ans, led, n_tiers, current=cur, author_tier=0)
                except Exception as e:
                    errs.append(f"{phase}/{demand}/{risk}/{traj}: {type(e).__name__}: {e}")
                    continue
                t = p["tier"]
                if t is None:
                    seen.add("HOLD")
                elif not (0 <= t < n_tiers):
                    errs.append(f"{phase}/{demand} -> tier {t} out of range for {n_tiers}")
                else:
                    seen.add(t)
        if errs:
            return False, f"n_tiers={n_tiers}: {len(errs)} problems\n" + "\n".join(errs[:5])
        out[n_tiers] = sorted(seen, key=str)
    return True, f"n_tiers=2 reaches {out[2]}, n_tiers=3 reaches {out[3]} (HOLD = hold session tier)"


def t_router_starts():
    """Every arm's router env must boot. JSON-in-env vars are parsed unguarded
    at import, so bad JSON in ROUTER_TIERS fails startup rather than degrading."""
    msgs = []
    for arm in arms_mod.ARMS:
        with tempfile.TemporaryDirectory() as td:
            port = rb.free_port()
            log = os.path.join(td, "r.log")
            try:
                p, lg = rb.start_router(arm, port, os.path.join(td, "tr"), log)
            except Exception as e:
                return False, f"{arm.name}: {e}"
            st = rb.get_json(port, "/router/status")
            mt = rb.get_json(port, "/router/metrics")
            rb.stop_router(p, lg)
            want = json.loads(arm.env().get("ROUTER_TIERS", "null")) if "ROUTER_TIERS" in arm.env() else None
            got = mt.get("tiers")
            if want and got != want:
                return False, f"{arm.name}: tiers {got} != {want}"
            if st.get("enabled") is not (arm.router_env.get("ROUTER_ENABLED") != "0"):
                return False, f"{arm.name}: enabled={st.get('enabled')} unexpected"
            msgs.append(f"{arm.name}: enabled={st.get('enabled')} tiers={got}")
    return True, "\n".join(msgs)


def t_env_scrubbed():
    e = rb.child_env("http://127.0.0.1:1234")
    leaked = sorted(k for k in e if k.startswith("CLAUDE") or
                    (k.startswith("ANTHROPIC") and k != "ANTHROPIC_BASE_URL"))
    if leaked:
        return False, f"leaked into child env: {leaked}"
    if e.get("ANTHROPIC_BASE_URL") != "http://127.0.0.1:1234":
        return False, "ANTHROPIC_BASE_URL not set on child env"
    return True, f"ANTHROPIC_BASE_URL set, {len([k for k in os.environ if k.startswith(('CLAUDE','ANTHROPIC'))])} parent vars stripped"


def t_no_settings_env_block():
    """Settings `env` blocks are Object.assign-ed onto process.env at startup,
    so a settings env would silently beat the shell ANTHROPIC_BASE_URL."""
    bad = []
    for p in (os.path.expanduser("~/.claude/settings.json"),
              os.path.expanduser("~/.claude/settings.local.json"),
              "/Library/Application Support/ClaudeCode/managed-settings.json",
              os.path.join(rb.REPO, ".claude/settings.json"),
              os.path.join(rb.REPO, ".claude/settings.local.json")):
        if not os.path.exists(p):
            continue
        try:
            d = json.load(open(p))
        except Exception as e:
            bad.append(f"{os.path.basename(p)}: unparseable ({e})")
            continue
        if "env" in d:
            keys = sorted(d["env"])
            if any(k.startswith("ANTHROPIC") for k in keys):
                bad.append(f"{os.path.basename(p)}: env block sets {keys}, WILL override the shell")
            else:
                bad.append(f"{os.path.basename(p)}: env block sets {keys} (harmless here)")
    return (not any("WILL override" in b for b in bad)), "\n".join(bad) or "no settings file defines an env block"


def t_key_present():
    need = [a.name for a in arms_mod.ARMS if a.needs_key]
    if os.environ.get("TYPESAFE_API_KEY"):
        return True, f"TYPESAFE_API_KEY present, {len(need)} router arms can run"
    return False, (f"TYPESAFE_API_KEY not set. These arms would silently become duplicate\n"
                   f"controls: {need}. Export it before running the full matrix.")


def t_seed_files():
    missing = []
    for t in tasks_mod.TASKS:
        if not t.setup:
            continue
        with tempfile.TemporaryDirectory() as td:
            try:
                got = t.setup(td)
            except Exception as e:
                missing.append(f"{t.name}: {e}")
                continue
            for g in got:
                if not os.path.exists(os.path.join(td, g)):
                    missing.append(f"{t.name}: {g} not copied")
    return (not missing), "\n".join(missing) or "seed files copy cleanly into a temp dir"


def t_live():
    """One real request through a disabled router. Costs a fraction of a cent.

    Validates the whole client path at once: auth survives --setting-sources
    project, bypassPermissions does not hang without a TTY, ANTHROPIC_BASE_URL
    reaches the router, and --session-id lands in the x-claude-code-session-id
    header so the trace filename is predictable.
    """
    arm = arms_mod.Arm("preflight", "haiku", {"ROUTER_ENABLED": "0"}, False, "")
    with tempfile.TemporaryDirectory() as td:
        traces = os.path.join(td, "tr")
        port = rb.free_port()
        sid = str(uuid.uuid4())
        p, lg = rb.start_router(arm, port, traces, os.path.join(td, "r.log"))
        try:
            t0 = time.time()
            r = subprocess.run(
                [rb.CLAUDE, "-p", "Reply with exactly the word OK and nothing else.",
                 "--output-format", "json", "--model", "haiku", "--effort", "medium",
                 "--setting-sources", "project", "--permission-mode", "bypassPermissions",
                 "--tools", "", "--max-turns", "2", "--session-id", sid],
                cwd=td, env=rb.child_env(f"http://127.0.0.1:{port}"),
                capture_output=True, text=True, timeout=180)
            wall = time.time() - t0
            mt = rb.get_json(port, "/router/metrics")
        finally:
            rb.stop_router(p, lg)

        if r.returncode != 0:
            return False, f"claude exited {r.returncode}\nstderr: {(r.stderr or '')[-600:]}"
        try:
            env = json.loads(r.stdout)
        except Exception:
            return False, f"stdout not JSON: {(r.stdout or '')[-600:]}"

        notes = [
            f"reply={env.get('result', '')!r} is_error={env.get('is_error')}",
            f"total_cost_usd={env.get('total_cost_usd')} duration_ms={env.get('duration_ms')} turns={env.get('num_turns')}",
            f"modelUsage keys={sorted(env.get('modelUsage', {}))}",
            f"router requests_total={mt.get('requests_total')} passthrough={mt.get('passthrough')} "
            f"routed={mt.get('routed')} spend={mt.get('spend')}",
            f"wall={wall:.1f}s",
        ]
        if mt.get("requests_total", 0) < 1:
            return False, "router saw no requests: ANTHROPIC_BASE_URL did not reach the client\n" + "\n".join(notes)
        files = sorted(os.listdir(traces)) if os.path.isdir(traces) else []
        notes.append(f"trace files={files}")
        if f"{sid}.jsonl" not in files:
            return False, (f"trace file is not {sid}.jsonl, so --session-id does not reach the\n"
                           f"x-claude-code-session-id header and runs cannot be joined\n" + "\n".join(notes))
        if mt.get("routed", 0) != 0:
            return False, "disabled router still routed a request\n" + "\n".join(notes)
        cb = {m: u.get("costBasis") for m, u in (env.get("modelUsage") or {}).items()}
        notes.append(f"costBasis={cb}")
        return True, "\n".join(notes)


# ---------------------------------------------------------------------------

def main():
    live = "--live" in sys.argv
    print("preflight, no API spend:\n")
    check("two-rung and three-rung tier ladders stay in range", t_tier_ladder)
    check("every arm's router env boots and reports the right tiers", t_router_starts)
    check("CLAUDE*/ANTHROPIC* stripped from the child env", t_env_scrubbed)
    check("no settings file overrides ANTHROPIC_BASE_URL", t_no_settings_env_block)
    check("seed files copy into a run directory", t_seed_files)
    check("TYPESAFE_API_KEY present for the router arms", t_key_present)
    if live:
        print("\nlive check, costs a fraction of a cent:\n")
        check("one real request end to end through a disabled router", t_live)
    else:
        print("\n  (skipped the live end-to-end check, pass --live to run it)")

    bad = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} passed")
    if bad:
        print("failed: " + ", ".join(bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
