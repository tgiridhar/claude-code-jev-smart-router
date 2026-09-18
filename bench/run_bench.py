#!/usr/bin/env python3
"""Benchmark orchestrator.

One run = one fresh router process + one `claude -p` invocation, fully isolated.

Isolation matters because the router keeps process-global state that is not
partitioned by session: _metrics has no reset endpoint, _out_rate is a learned
output-volume EMA that changes later routing decisions, _memo is the classifier
memo, and the effort off-latch is permanent once tripped. Reusing a router
process across runs would leak routing behaviour from one experiment into the
next. So every run gets its own process on its own port, and is killed after.

Runs execute serially. Parallel runs would share upstream prompt cache state
and contend for rate limits.
"""

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

import arms as arms_mod
import tasks as tasks_mod

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BENCH = os.path.join(REPO, "bench")
RUNS = os.path.join(BENCH, "runs")

CLAUDE = shutil.which("claude") or "claude"


def load_dotenv():
    """Read TYPESAFE_API_KEY and friends from the repo's .env if present.

    .env is gitignored. A shell export does not survive between tool calls in
    an automated session, so a file is the practical way to supply the key.
    Existing environment variables win.
    """
    p = os.path.join(REPO, ".env")
    if not os.path.exists(p):
        return
    with open(p) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v


load_dotenv()


# ---------------------------------------------------------------------------
# environment hygiene
# ---------------------------------------------------------------------------

# Credentials, not configuration. These must survive scrubbing or a machine
# that authenticates by API key rather than by stored OAuth credentials loses
# auth on every child and the whole matrix fails.
AUTH_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def child_env(base_url, extra=None):
    """Env for the `claude` child.

    Strips CLAUDE* and the ANTHROPIC* configuration variables inherited from
    the parent, keeping the auth ones. A Claude Code session exports
    CLAUDECODE, CLAUDE_CODE_*, CLAUDE_PID and CLAUDE_EFFORT, and a child
    `claude` would otherwise inherit all of them. CLAUDE_EFFORT in particular
    would silently change token spend, and ANTHROPIC_MODEL would override the
    arm's model.
    """
    e = {k: v for k, v in os.environ.items()
         if not (k.startswith("CLAUDE")
                 or (k.startswith("ANTHROPIC") and k not in AUTH_VARS))}
    if base_url:
        e["ANTHROPIC_BASE_URL"] = base_url
    if extra:
        e.update(extra)
    return e


def router_env(arm, trace_dir):
    e = {k: v for k, v in os.environ.items() if not k.startswith("ROUTER_")}
    e.update(arm.env())
    e["ROUTER_TRACE_DIR"] = trace_dir
    return e


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# router lifecycle
# ---------------------------------------------------------------------------

def start_router(arm, port, trace_dir, log_path):
    log = open(log_path, "wb")
    p = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "jev_router:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=REPO, env=router_env(arm, trace_dir), stdout=log, stderr=subprocess.STDOUT,
    )
    for _ in range(120):
        if p.poll() is not None:
            log.close()
            raise RuntimeError(
                f"router exited with code {p.returncode} before becoming ready. "
                f"See {os.path.basename(log_path)}")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/router/status", timeout=1) as r:
                json.loads(r.read())
                return p, log
        except Exception:
            time.sleep(0.25)
    p.kill()
    log.close()
    raise RuntimeError("router did not become ready within 30s")


def get_json(port, path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {e}"}


def stop_router(p, log):
    p.terminate()
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait(timeout=5)
    log.close()


# ---------------------------------------------------------------------------
# one run
# ---------------------------------------------------------------------------

def claude_argv(task, arm, session_id):
    """The client command line.

    Configuration hygiene, all of it deliberate:
      --setting-sources project  drops ~/.claude/settings.json. That file sets
          effortLevel xhigh globally but claude-opus-5 to high via modelSettings,
          so a pinned-Opus control would run at LOWER effort than router arms
          running Sonnet and Haiku. That asymmetry alone could produce the
          result. It also drops 9 enabled plugins. No project settings exist
          in the run directory, so the effective answer is "none".
      --effort medium           passed identically to every arm.
      --session-id              the router names trace files after the
          x-claude-code-session-id header, so this makes the trace file
          predictable and joins the client envelope to the router's records.
      never --model opusplan, never --permission-mode plan. Both resolve to
          two models in one run.
    """
    return [
        CLAUDE, "-p", task.prompt,
        "--output-format", "json",
        "--model", arm.model,
        "--effort", "medium",
        "--setting-sources", "project",
        "--permission-mode", "bypassPermissions",
        "--max-turns", str(task.max_turns),
        "--max-budget-usd", str(task.budget_usd),
        "--session-id", session_id,
    ]


def one_run(task, arm, rep, stamp_dir, dry_run=False):
    run_id = f"{task.name}__{arm.name}__{rep}"
    rd = os.path.join(stamp_dir, run_id)
    work = os.path.join(rd, "work")
    traces = os.path.join(rd, "traces")
    session_id = str(uuid.uuid4())
    argv = claude_argv(task, arm, session_id)

    if dry_run:
        port = "PORT"
        print(f"\n=== {run_id} ===")
        print(f"  cwd          {os.path.join('bench/runs/<stamp>', run_id, 'work')}")
        print(f"  session-id   {session_id}")
        print(f"  router env   {json.dumps(arm.env(), sort_keys=True)}")
        print(f"  needs key    {arm.needs_key}")
        print(f"  seeded       {[] if not task.setup else 'yes'}")
        print(f"  argv         {' '.join(repr(a) if ' ' in a else a for a in argv[:2])} "
              f"<prompt {len(task.prompt)} chars> {' '.join(argv[3:])}")
        stripped = sorted(k for k in os.environ
                          if k.startswith('CLAUDE') or k.startswith('ANTHROPIC'))
        print(f"  env stripped {stripped}")
        print(f"  timeout      {task.timeout_s}s")
        return None

    os.makedirs(work, exist_ok=True)
    os.makedirs(traces, exist_ok=True)
    seeded = task.setup(work) if task.setup else []

    port = free_port()
    meta = {
        "run_id": run_id, "task": task.name, "arm": arm.name, "rep": rep,
        "session_id": session_id, "port": port, "seeded": seeded,
        "client_model": arm.model, "router_env": arm.env(),
        "max_turns": task.max_turns, "budget_usd": task.budget_usd,
        "timeout_s": task.timeout_s,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }

    proc, log = start_router(arm, port, traces, os.path.join(rd, "router.log"))
    t0 = time.time()
    envelope, timed_out, stderr_tail = None, False, ""
    try:
        r = subprocess.run(
            argv, cwd=work, env=child_env(f"http://127.0.0.1:{port}"),
            capture_output=True, text=True, timeout=task.timeout_s,
        )
        stderr_tail = (r.stderr or "")[-2000:]
        try:
            envelope = json.loads(r.stdout)
        except Exception:
            envelope = {"_unparsed_stdout": (r.stdout or "")[-4000:],
                        "_returncode": r.returncode}
    except subprocess.TimeoutExpired as e:
        timed_out = True
        envelope = {"_timeout": task.timeout_s,
                    "_partial_stdout": (e.stdout or b"").decode(errors="replace")[-4000:]}
    finally:
        wall = time.time() - t0
        metrics = get_json(port, "/router/metrics")
        status = get_json(port, "/router/status")
        stop_router(proc, log)

    meta.update({"wall_s": round(wall, 1), "timed_out": timed_out,
                 "ended_at": datetime.now(timezone.utc).isoformat()})
    if stderr_tail.strip():
        meta["stderr_tail"] = stderr_tail

    try:
        check = task.check(work)
    except Exception as e:
        check = {"ok": False, "why": f"check raised {type(e).__name__}: {e}"}

    write(os.path.join(rd, "meta.json"), meta)
    write(os.path.join(rd, "result.json"), envelope)
    write(os.path.join(rd, "metrics.json"), metrics)
    write(os.path.join(rd, "status.json"), status)
    write(os.path.join(rd, "check.json"), check)

    cost = (metrics or {}).get("spend")
    print(f"  {run_id:42} {wall:6.1f}s  "
          f"spend={cost if cost is None else format(cost, '.4f'):>9}  "
          f"check={'PASS' if check.get('ok') else 'FAIL'}"
          f"{'  TIMEOUT' if timed_out else ''}")
    if not check.get("ok"):
        print(f"      check: {check.get('why')}")
    return rd


def write(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
        f.write("\n")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Run the Jev router benchmark matrix.")
    ap.add_argument("--tasks", default="", help="comma separated task names (default: all)")
    ap.add_argument("--arms", default="", help="comma separated arm names (default: the 4 main arms)")
    ap.add_argument("--pilot", action="store_true", help=f"shorthand for --tasks {','.join(tasks_mod.PILOT)}")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--dry-run", action="store_true",
                    help="print the exact command line and env for every cell, run nothing")
    ap.add_argument("--stamp", default="", help="reuse an existing runs/<stamp> directory")
    a = ap.parse_args()

    names = tasks_mod.PILOT if a.pilot else (
        [s.strip() for s in a.tasks.split(",") if s.strip()] or list(tasks_mod.BY_NAME))
    arm_names = [s.strip() for s in a.arms.split(",") if s.strip()] or list(arms_mod.MAIN)

    unknown = [n for n in names if n not in tasks_mod.BY_NAME] + \
              [n for n in arm_names if n not in arms_mod.BY_NAME]
    if unknown:
        sys.exit(f"unknown task or arm: {unknown}")

    sel_tasks = [tasks_mod.BY_NAME[n] for n in names]
    sel_arms = [arms_mod.BY_NAME[n] for n in arm_names]

    # Fail fast on the missing classifier key. Without it classify() never
    # fires and the router degrades to clean passthrough (jev_router.py:76,
    # used at 489), which would silently turn every router arm into another
    # control arm and produce a benchmark showing no savings and no difference
    # between tier configurations.
    need_key = [x.name for x in sel_arms if x.needs_key]
    if need_key and not os.environ.get("TYPESAFE_API_KEY") and not a.dry_run:
        sys.exit(
            f"TYPESAFE_API_KEY is not set, but these arms need it: {need_key}\n"
            "Without it the router passes everything through and those arms become\n"
            "duplicate controls. Export the key, or run only the control arms with\n"
            "  --arms control-opus,control-sonnet")

    stamp = a.stamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    stamp_dir = os.path.join(RUNS, stamp)
    if not a.dry_run:
        os.makedirs(stamp_dir, exist_ok=True)

    cells = [(t, arm, rep) for t in sel_tasks for arm in sel_arms for rep in range(1, a.reps + 1)]
    budget = sum(t.budget_usd for t, _, _ in cells)
    minutes = sum(t.timeout_s for t, _, _ in cells) / 60.0

    print(f"{len(cells)} runs: {len(sel_tasks)} tasks x {len(sel_arms)} arms x {a.reps} rep(s)")
    print(f"worst case {minutes:.0f} min wall clock, {budget:.2f} USD of budget caps")
    if a.dry_run:
        for t, arm, rep in cells:
            one_run(t, arm, rep, stamp_dir, dry_run=True)
        print(f"\ndry run, nothing executed. would write to bench/runs/{stamp}/")
        return

    print(f"writing to bench/runs/{stamp}/\n")
    for i, (t, arm, rep) in enumerate(cells, 1):
        print(f"[{i}/{len(cells)}] {t.name} / {arm.name}")
        try:
            one_run(t, arm, rep, stamp_dir)
        except Exception as e:
            print(f"  FAILED: {type(e).__name__}: {e}")
        time.sleep(2)

    print(f"\ndone. collect with:\n  python3 bench/collect.py {stamp}")


if __name__ == "__main__":
    main()
