#!/usr/bin/env python3
"""
jev_ontology.py - ontology v2 for jev_router: what Jev is asked, and what it sees.

Drop-in replacement for QUESTIONS / build_state / pick_tier / the recheck
cadence in jev_router.py. The cache economics (switch_delta,
apply_cache_policy) stay exactly as they are; this module only decides what
tier the work WANTS and why.

Design rules
------------
1. Code extracts facts, Jev makes judgments. Anything computable from the
   request body -- which tools ran, what class of bash command, whether the
   tests passed, which skills loaded, which MCP servers were written to, how
   many edits since the last check -- is computed here and handed to Jev as
   structured state. Jev is never asked something a regex already knows.

2. The ledger is recomputed from the request, not held in memory. Claude Code
   sends the full message history on every call, so extract_ledger(body) is a
   pure function: it survives router restarts and cannot drift from reality.
   (After a /compact the history shrinks and so does the ledger. Acceptable.)

3. The unit being routed is the NEXT STEP of a work unit, not a message.
   Difficulty is discovered mid-loop, so the same questions are asked at human
   turns and at mid-run rechecks, and rechecks are event-driven (phase shift,
   repeated failure, first touch of a risk surface, skill load), with the old
   every-N cadence only as a fallback.

4. Tier = cognitive demand x cost of an undetected error. A cheap model is
   safe when a check in the loop catches its mistakes and dangerous when
   nothing does. So "is there an oracle", "is this reversible" and "is this a
   risk surface" are first-class axes, not afterthoughts.

5. A model tier is a price per token, not a cost per task. Effort decides how
   many tokens and agentic turns a step burns, so the policy returns an effort
   recommendation next to the tier, and the router prices model switches with
   each model's OBSERVED output volume rather than assuming it is the same.

6. Signals Claude Code already emits are read, not stripped: plan mode and
   todo state in <system-reminder> blocks, the subagent's role in `system`,
   the thinking budget, Skill loads, mcp__server__tool names.

ASSUMPTIONS TO VERIFY AGAINST YOUR OWN TRACES: tool names and input shapes
drift between Claude Code releases (Task vs Agent, TodoWrite vs
TaskCreate/TaskUpdate, and on macOS/Linux current builds drop Grep/Glob and
search through Bash with find/grep). Everything name-dependent lives in the
tables below so it can be corrected without touching logic.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from typing import Any

MIN_CONFIDENCE = float(os.environ.get("ROUTER_MIN_CONFIDENCE", "0.45"))

# --------------------------------------------------------------------------
# Tables: everything that depends on Claude Code's naming
# --------------------------------------------------------------------------

TOOL_KINDS: dict[str, str] = {
    "Read": "read", "NotebookRead": "read", "LS": "search", "Glob": "search",
    "Grep": "search", "ToolSearch": "meta",
    "Edit": "edit", "MultiEdit": "edit", "Write": "edit", "NotebookEdit": "edit",
    "Bash": "bash", "PowerShell": "bash", "BashOutput": "bash", "KillShell": "meta",
    "Task": "subagent", "Agent": "subagent", "TaskOutput": "meta", "TaskStop": "meta",
    "TodoWrite": "todo", "TaskCreate": "todo", "TaskUpdate": "todo", "TaskList": "todo",
    "Skill": "skill", "SlashCommand": "skill",
    "EnterPlanMode": "plan", "ExitPlanMode": "plan",
    "AskUserQuestion": "ask", "WebFetch": "web", "WebSearch": "web",
}
LSP_HINT = re.compile(r"lsp|language.?server|definition|references|diagnostic", re.I)

# Bash classes, checked in this order: the most consequential match wins.
BASH_CLASSES: list[tuple[str, re.Pattern[str]]] = [
    ("destructive", re.compile(
        r"\brm\s+-[a-z]*r|git\s+reset\s+--hard|git\s+clean\s+-|push\s+.*(--force|-f\b)"
        r"|\bdrop\s+(table|database)|\btruncate\s+table|\bmkfs|\bdd\s+if=", re.I)),
    ("outward", re.compile(
        r"git\s+push|gh\s+(pr\s+(create|merge|close)|release|issue\s+(create|close))"
        r"|npm\s+publish|twine\s+upload|docker\s+push|kubectl\s+(apply|delete|rollout|scale)"
        r"|terraform\s+(apply|destroy)|helm\s+(install|upgrade|uninstall)"
        r"|\b(aws|gcloud|az)\s+\S+\s+(create|delete|update|deploy)"
        r"|curl\s+.*-X\s*(POST|PUT|PATCH|DELETE)|\bssh\s|\bscp\s", re.I)),
    ("verify", re.compile(
        r"\b(pytest|tox|jest|vitest|mocha|rspec|phpunit|ctest|mypy|pyright|ruff|flake8"
        r"|pylint|eslint|tsc)\b|go\s+(test|vet|build)|cargo\s+(test|check|clippy|build)"
        r"|(npm|pnpm|yarn|bun)\s+(run\s+)?(test|lint|build|typecheck|check)"
        r"|make\s+(test|check|lint|build)|(gradle|gradlew|mvn)\b.*(test|build|verify)"
        r"|bazel\s+(test|build)|dotnet\s+(test|build)", re.I)),
    ("vcs_write", re.compile(
        r"git\s+(add|commit|merge|rebase|checkout|switch|stash|cherry-pick|tag|restore|apply|mv|rm)\b", re.I)),
    ("package", re.compile(
        r"\b(pip|pip3|uv|npm|pnpm|yarn|bun|cargo|brew|apt|apt-get|gem|composer)\s+"
        r"(install|add|remove|update|upgrade|sync)\b|go\s+(get|mod\s+tidy)", re.I)),
    ("vcs_read", re.compile(
        r"git\s+(status|diff|log|show|blame|branch|ls-files|rev-parse|grep)\b|gh\s+pr\s+(view|diff|list|checks)", re.I)),
    ("search", re.compile(
        r"^\s*(grep|egrep|fgrep|rg|ugrep|ag|find|bfs|fd|ls|tree|cat|nl|bat|batcat|head|tail"
        r"|wc|jq|stat|file|which|sed\s+-n|awk)\b", re.I)),
    ("run", re.compile(
        r"^\s*(python3?|node|deno|bun|bash|sh|ruby|php|java|\./|go\s+run|cargo\s+run"
        r"|docker\s+(run|compose)|make\b)", re.I)),
]

RISK_SURFACE = re.compile(
    os.environ.get(
        "ROUTER_RISK_SURFACE",
        r"auth|login|session|token|secret|credential|passw|crypto|encrypt|permission|rbac|\biam\b"
        r"|payment|billing|invoice|checkout|ledger|refund"
        r"|migrat|schema|\.sql\b|alembic"
        r"|terraform|\.tf\b|k8s|kube|helm|dockerfile|\.github/workflows|deploy|infra"
        r"|mutex|\block\b|concurren|race|thread|transaction",
    ),
    re.I,
)

MCP_WRITE_VERB = re.compile(
    r"(^|_)(create|update|delete|remove|send|post|write|merge|close|comment|assign|publish"
    r"|deploy|execute|run|insert|set|add|move|transition|upload|approve)", re.I)

FAIL_TEXT = re.compile(
    r"Traceback \(most recent|^\s*(E\s{2,}|FAIL|FAILED|ERROR)\b|\berror(\[\w+\])?:|npm ERR!"
    r"|panic:|AssertionError|exit code [1-9]|Exit status [1-9]|\b[1-9]\d* (failed|errors?)\b"
    r"|command not found|No such file or directory", re.M)
PASS_TEXT = re.compile(r"\b(\d+ passed|all tests passed|0 errors|\bok\b|PASS\b|success)", re.I)

_SYSREM_RE = re.compile(r"<system-reminder>(.*?)</system-reminder>", re.S)
_WRAP_RE = re.compile(r"</?(?:session|transcript)>")
PLAN_MODE_RE = re.compile(r"plan mode (is |still )*active", re.I)

ROLE_SNIFF: list[tuple[str, re.Pattern[str]]] = [
    ("explore", re.compile(r"file search specialist|read-only|exploring codebases|do not (edit|modify)", re.I)),
    ("plan", re.compile(r"software architect|planning specialist|implementation plan", re.I)),
    ("review", re.compile(r"code review|reviewer|security review|audit", re.I)),
]


# --------------------------------------------------------------------------
# The ontology
#
# Nine orthogonal axes. Each question is atomic: one gut-check. Weighing them
# against each other happens in pick_tier_v2, in code you can read.
#
#   WHAT kind of step     phase
#   HOW hard is it        next_step_demand, cross_cutting, repo_specific, canonical
#   HOW governed is it    spec_completeness
#   WHAT if it's wrong    oracle_in_loop, risk_surface, irreversible
#   HOW is it going       trajectory, unverified_confidence, user_correcting
#   review only           review_depth
#   economics             gen_volume, is_followup
# --------------------------------------------------------------------------

PHASES = {
    "clarify": (
        "Requirements are being established: the agent should ask, restate, or "
        "pin down what is wanted before doing anything"
    ),
    "explore": (
        "Locating and reading: grep, find, file reads, symbol lookups, MCP or "
        "doc queries, to learn where things are and how they work"
    ),
    "plan": (
        "Deciding the approach: architecture, trade-offs, a multi-step plan, "
        "decomposing work, choosing what to delegate to subagents"
    ),
    "implement": "Writing or changing code according to an approach that already exists",
    "debug": (
        "A check has failed or behaviour is wrong, and the cause is not yet "
        "known: forming and testing hypotheses"
    ),
    "verify": (
        "Running tests, builds, linters or type checks and reading the result, "
        "with no diagnosis needed yet"
    ),
    "review": (
        "Judging existing code or a diff: correctness, security, design, "
        "conformance to conventions; writing review findings"
    ),
    "integrate": (
        "Wrapping up: commits, PR descriptions, changelogs, docs, ticket "
        "updates, pushing or deploying"
    ),
    "housekeeping": (
        "Not a step of work: a skill or tool schema loading, an attachment "
        "arriving, a session resuming, a bare acknowledgement"
    ),
}

QUESTIONS_V2: dict[str, Any] = {
    "phase": {
        "type": "choice",
        "instructions": "The software-lifecycle phase of the agent's NEXT step",
        "criteria": PHASES,
    },
    "next_step_demand": {
        "type": "score",
        "instructions": "How much judgment the agent's NEXT step needs",
        "criteria": [
            "Rote: the action is fully determined by what is already on the page",
            "Routine: skilled but conventional work inside an understood approach",
            "Hard: subtle reasoning, competing hypotheses, or the approach itself is in question",
        ],
    },
    "spec_completeness": {
        "type": "score",
        "instructions": "How much of the approach is already decided",
        "criteria": [
            "Governed: an explicit plan, todo list, spec or exact instruction covers the next step",
            "Goal clear, method open",
            "Outcome only: the path is left entirely to the agent",
        ],
    },
    "cross_cutting": {
        "type": "noul",
        "instructions": (
            "Doing this correctly requires keeping several modules or files "
            "mutually consistent at once (not merely reading many files)"
        ),
    },
    "repo_specific": {
        "type": "noul",
        "instructions": (
            "Doing this correctly depends on conventions, architecture or "
            "history particular to this codebase rather than general knowledge"
        ),
    },
    "canonical": {
        "type": "noul",
        "instructions": (
            "The thing being built is a well-known, canonical artifact with "
            "countless established implementations"
        ),
    },
    "oracle_in_loop": {
        "type": "noul",
        "instructions": (
            "If the next step were done wrong, a check the agent is already "
            "running in this session (tests, build, type checker, linter, hook) "
            "would reveal it promptly"
        ),
    },
    "risk_surface": {
        "type": "noul",
        "instructions": (
            "The work touches code where a subtle mistake is costly: "
            "authentication, money, data migrations, concurrency, "
            "infrastructure, deletion of data, or a public API contract"
        ),
    },
    "irreversible": {
        "type": "noul",
        "instructions": (
            "The next step is likely to act outside the local working tree in a "
            "way that cannot simply be undone: pushing, deploying, publishing, "
            "writing to an external system through an MCP tool, deleting data"
        ),
    },
    "trajectory": {
        "type": "choice",
        "instructions": "How the current run of work is going",
        "criteria": {
            "progressing": "Each step builds on the last; checks are passing or failures are new ones",
            "slow_convergence": "Failures are changing and shrinking, but it is taking many rounds",
            "thrashing": "The same failure, or the same file, keeps coming back; the approach is not working",
            "drifting": "The work has wandered away from what the user asked for",
            "blocked_on_human": "The agent cannot proceed without a decision or information from the user",
            "just_started": "Too early to tell",
        },
    },
    "unverified_confidence": {
        "type": "noul",
        "instructions": (
            "The agent's latest remark claims success or asserts facts about "
            "the code that nothing in the recorded steps actually checked"
        ),
    },
    "user_correcting": {
        "type": "noul",
        "instructions": (
            "The user's newest message rejects, corrects or expresses "
            "dissatisfaction with what the agent just did"
        ),
    },
    "review_depth": {
        "type": "choice",
        "instructions": "If the next step is reviewing code, the depth of review called for",
        "criteria": {
            "not_review": "The next step is not a review",
            "conformance": "Style, naming, formatting, checklist or convention conformance",
            "correctness": "Whether the logic is right: edge cases, error handling, tests",
            "security_or_design": "Vulnerabilities, concurrency hazards, architectural fit, API design",
        },
    },
    "gen_volume": {
        "type": "score",
        "instructions": "How much text or code the next step will generate",
        "criteria": [
            "A brief reply, a tool call, or a small edit",
            "A screenful of code or prose, or edits across a few files",
            "Extensive generation: large new files, many files, or a long document",
        ],
    },
    "is_followup": {
        "type": "noul",
        "instructions": (
            "The user's newest message is a small continuation or confirmation "
            "of the work immediately preceding it rather than a new task"
        ),
    },
}

# Mid-run rechecks have no new human message, so the three human-turn
# questions are dropped. Everything else is asked again: Jev answers in
# parallel and output is free, so there is no reason to ask less.
_HUMAN_ONLY = {"user_correcting", "is_followup", "spec_completeness"}
STEP_QUESTIONS_V2 = {k: v for k, v in QUESTIONS_V2.items() if k not in _HUMAN_ONLY}


# --------------------------------------------------------------------------
# Ledger extraction: facts from the request body
# --------------------------------------------------------------------------

def _blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    c = message.get("content")
    if isinstance(c, str):
        return [{"type": "text", "text": c}]
    return [b for b in c if isinstance(b, dict)] if isinstance(c, list) else []


def _result_text(block: dict[str, Any]) -> str:
    inner = block.get("content")
    if isinstance(inner, str):
        return inner
    if isinstance(inner, list):
        return "\n".join(str(b.get("text", "")) for b in inner
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def split_human_text(message: dict[str, Any]) -> tuple[str, list[str]]:
    """(the person's words, the system-reminder payloads). Reminders are
    harness signal -- plan mode, todo nudges, CLAUDE.md -- so they are parsed,
    not thrown away; only the person's words are shown to Jev as the ask."""
    raw = "\n".join(str(b.get("text", "")) for b in _blocks(message) if b.get("type") == "text")
    reminders = _SYSREM_RE.findall(raw)
    text = _WRAP_RE.sub(" ", _SYSREM_RE.sub(" ", raw))
    return " ".join(text.split()), reminders


def classify_bash(command: str) -> str:
    """Most consequential class across the segments of a compound command."""
    segments = [s for s in re.split(r"&&|\|\||;|\n", command) if s.strip()]
    found = set()
    for seg in segments:
        # a pipeline is classified by its head: `pytest | tail` is a verify
        head = seg.split("|")[0]
        for name, rx in BASH_CLASSES:
            if rx.search(head):
                found.add(name)
                break
    for name, _ in BASH_CLASSES:
        if name in found:
            return name
    return "other"


def _failure_signature(text: str) -> str:
    """Stable fingerprint of a failure, so 'the same error again' is a fact
    rather than something Jev has to infer from excerpts."""
    m = FAIL_TEXT.search(text)
    if not m:
        return ""
    line_start = text.rfind("\n", 0, m.start()) + 1
    line_end = text.find("\n", m.end())
    line = text[line_start: line_end if line_end != -1 else None]
    norm = re.sub(r"0x[0-9a-f]+|\d+", "N", line.lower())
    norm = re.sub(r"(/[\w.\-]+)+", "/P", norm)
    return hashlib.sha1(norm.strip().encode()).hexdigest()[:8]


def _tail(text: str, n: int) -> str:
    """Failures live at the END of tool output; the old router kept the head."""
    text = " ".join(text.split())
    return text if len(text) <= n else "…" + text[-n:]


def _snip(text: str, budget: int) -> str:
    if len(text) <= budget:
        return text
    head = int(budget * 0.65)
    return text[:head] + " [...] " + text[-(budget - head - 7):]


def extract_ledger(body: dict[str, Any], *, is_subagent: bool = False) -> dict[str, Any]:
    """Pure function: request body -> situation ledger. No Jev, no state."""
    messages = body.get("messages") or []

    # ---- harness: what the request itself declares -------------------------
    tool_names = [str(t.get("name")) for t in (body.get("tools") or [])
                  if isinstance(t, dict) and t.get("name")]
    mcp_available = sorted({n.split("__")[1] for n in tool_names
                            if n.startswith("mcp__") and n.count("__") >= 2})
    system = body.get("system")
    system_text = system if isinstance(system, str) else " ".join(
        str(b.get("text", "")) for b in (system or []) if isinstance(b, dict))
    role = "main"
    if is_subagent:
        role = next((r for r, rx in ROLE_SNIFF if rx.search(system_text[:4000])), "subagent")
    thinking = body.get("thinking") or {}
    think_budget = int(thinking.get("budget_tokens") or 0) if isinstance(thinking, dict) else 0

    # ---- walk the history ---------------------------------------------------
    pending: dict[str, dict[str, Any]] = {}   # tool_use_id -> step
    steps: list[dict[str, Any]] = []
    opening = latest_ask = last_agent = ""
    human_turns = 0
    unit_start = 0                            # index into steps where the current unit began
    plan_mode = claude_md = False
    todos: dict[str, int] = {}
    skills: list[str] = []

    for m in messages:
        role_m = m.get("role")
        if role_m == "assistant":
            texts = []
            for b in _blocks(m):
                if b.get("type") == "text":
                    texts.append(str(b.get("text", "")))
                elif b.get("type") == "tool_use":
                    name = str(b.get("name", ""))
                    inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                    step = _step_from_tool_use(name, inp)
                    pending[str(b.get("id"))] = step
                    steps.append(step)
                    if step["kind"] == "todo":
                        todos = _todo_state(name, inp, todos)
                    if step["kind"] == "skill":
                        skills.append(step["target"])
                    if name == "ExitPlanMode":
                        plan_mode = False
                    if name == "EnterPlanMode":
                        plan_mode = True
            if texts:
                last_agent = "\n".join(texts)
        elif role_m == "user":
            for b in _blocks(m):
                if b.get("type") == "tool_result":
                    step = pending.pop(str(b.get("tool_use_id")), None)
                    if step is not None:
                        _attach_result(step, _result_text(b), bool(b.get("is_error")))
            text, reminders = split_human_text(m)
            for r in reminders:
                if PLAN_MODE_RE.search(r):
                    plan_mode = True
                if "CLAUDE.md" in r or "claudeMd" in r:
                    claude_md = True
            if text:
                human_turns += 1
                opening = opening or text
                latest_ask = text
                unit_start = len(steps)

    unit = steps[unit_start:]
    recent = steps[-8:]

    # ---- aggregates over the current work unit ------------------------------
    kinds = Counter(s["kind"] for s in unit)
    bash_classes = Counter(s["bash"] for s in unit if s.get("bash"))
    edited = Counter(s["target"] for s in unit if s["kind"] == "edit" and s["target"])
    lines_changed = sum(int(s.get("lines", 0)) for s in unit if s["kind"] == "edit")

    verifies = [s for s in unit if s.get("bash") == "verify" and "failed" in s]
    fail_streak = 0
    for s in reversed(verifies):
        if s["failed"]:
            fail_streak += 1
        else:
            break
    # Only failures since the last PASSING check count as "the same failure
    # again": once the check goes green the thrash is over.
    last_pass = max((i for i, s in enumerate(unit)
                     if s.get("bash") == "verify" and s.get("failed") is False), default=-1)
    sigs = Counter(s["sig"] for s in unit[last_pass + 1:] if s.get("sig"))
    edits_since_verify = 0
    for s in reversed(unit):
        if s.get("bash") == "verify":
            break
        if s["kind"] == "edit":
            edits_since_verify += 1

    searches = [s for s in unit if s["kind"] == "search" or s.get("bash") == "search"]
    big_results = sum(1 for s in searches if s.get("result_kb", 0) >= 8)

    risk_hits: list[str] = []
    for s in unit:
        probe = f"{s.get('target', '')} {s.get('cmd', '')}"
        m_r = RISK_SURFACE.search(probe)
        touches = s["kind"] == "edit" or (s["kind"] == "mcp" and s.get("mcp_write")) or (
            s["kind"] == "bash" and s.get("bash") not in ("search", "vcs_read", "verify"))
        if m_r and touches:
            hit = f"{m_r.group(0).lower()} ({s['kind']}: {_snip(s.get('target') or s.get('cmd', ''), 50)})"
            if hit not in risk_hits:
                risk_hits.append(hit)
    ask_risk = RISK_SURFACE.search(latest_ask)
    if ask_risk:
        risk_hits.append(f"{ask_risk.group(0).lower()} (in the user's ask)")

    outward = [_snip(s["cmd"], 70) for s in unit if s.get("bash") in ("outward", "destructive")]
    mcp_writes = [s["target"] for s in unit if s["kind"] == "mcp" and s.get("mcp_write")]
    mcp_used = sorted({s["target"].split("__")[1] for s in unit
                       if s["kind"] == "mcp" and s["target"].count("__") >= 2})
    subagents = [s["target"] for s in unit if s["kind"] == "subagent"]

    phase_hint = _phase_hint(recent, plan_mode, kinds, fail_streak, edited)

    return {
        "harness": {
            "agent_role": role,
            "plan_mode": plan_mode,
            "thinking_budget": think_budget,
            "claude_md_loaded": claude_md,
            "lsp_available": any(LSP_HINT.search(n) for n in tool_names),
            "mcp_servers_available": mcp_available[:12],
            "skills_loaded": skills[-6:],
        },
        "unit": {
            "opening_request": _snip(opening, 300),
            "latest_human_ask": _snip(latest_ask, 1500),
            "agent_last_remark": _snip(" ".join(last_agent.split()), 400),
            "human_turns": human_turns,
            "steps_in_unit": len(unit),
            "todos": todos or None,
        },
        "activity": {
            "phase_hint_from_tools": phase_hint,
            "tool_kinds": dict(kinds),
            "bash_classes": dict(bash_classes),
            "files_edited": dict(edited.most_common(6)),
            "max_edits_to_one_file": max(edited.values(), default=0),
            "lines_changed_est": lines_changed,
            "searches": len(searches),
            "searches_with_large_results": big_results,
            "subagents_spawned": subagents[-4:],
            "mcp_servers_used": mcp_used,
        },
        "verification": {
            "check_runs": len(verifies),
            "last_check": ("failed" if verifies[-1]["failed"] else "passed") if verifies else "never run",
            "consecutive_failures": fail_streak,
            "same_failure_repeats": max(sigs.values(), default=0),
            "edits_since_last_check": edits_since_verify,
        },
        "risk": {
            "risk_surface_hits": risk_hits[:6],
            "outward_or_destructive_commands": outward[-4:],
            "mcp_write_calls": mcp_writes[-4:],
        },
        "recent_steps": [_step_line(s) for s in recent],
    }


def _step_from_tool_use(name: str, inp: dict[str, Any]) -> dict[str, Any]:
    if name.startswith("mcp__"):
        tool = name.split("__")[-1]
        return {"kind": "mcp", "target": name, "mcp_write": bool(MCP_WRITE_VERB.search(tool))}
    kind = TOOL_KINDS.get(name, "lsp" if LSP_HINT.search(name) else "other")
    step: dict[str, Any] = {"kind": kind, "target": ""}
    if kind == "bash":
        cmd = str(inp.get("command", ""))
        step.update(cmd=cmd, bash=classify_bash(cmd) if cmd else "other")
    elif kind in ("read", "edit"):
        step["target"] = str(inp.get("file_path") or inp.get("notebook_path") or "")
        new = inp.get("new_string") or inp.get("content") or inp.get("new_source") or ""
        if isinstance(inp.get("edits"), list):
            new = "\n".join(str(e.get("new_string", "")) for e in inp["edits"] if isinstance(e, dict))
        step["lines"] = str(new).count("\n") + 1 if new else 0
    elif kind == "search":
        step["target"] = str(inp.get("pattern") or inp.get("path") or "")
    elif kind == "skill":
        step["target"] = str(inp.get("skill") or inp.get("command") or inp.get("name") or "?")
    elif kind == "subagent":
        step["target"] = str(inp.get("subagent_type") or "general-purpose")
    return step


def _attach_result(step: dict[str, Any], text: str, is_error: bool) -> None:
    step["result_kb"] = len(text) // 1024
    failed = is_error or bool(FAIL_TEXT.search(text))
    if step.get("bash") == "verify" and failed and PASS_TEXT.search(text) and not is_error:
        # "12 passed, 0 errors" style output that also trips a fail keyword
        failed = bool(re.search(r"\b[1-9]\d* (failed|errors?)\b|Traceback|FAILED", text))
    if step["kind"] in ("bash", "mcp", "edit") or is_error:
        step["failed"] = failed
        if failed:
            step["sig"] = _failure_signature(text) or "err"
            step["tail"] = _tail(text, 200)


def _todo_state(name: str, inp: dict[str, Any], prev: dict[str, int]) -> dict[str, int]:
    if isinstance(inp.get("todos"), list):  # TodoWrite: full replacement
        st = Counter(str(t.get("status", "pending")) for t in inp["todos"] if isinstance(t, dict))
        return {"total": sum(st.values()), "done": st.get("completed", 0),
                "in_progress": st.get("in_progress", 0)}
    out = dict(prev) or {"total": 0, "done": 0, "in_progress": 0}
    if name == "TaskCreate":
        out["total"] += 1
    elif name == "TaskUpdate" and inp.get("status") == "completed":
        out["done"] += 1
    return out


def _phase_hint(recent: list[dict[str, Any]], plan_mode: bool, kinds: Counter,
                fail_streak: int, edited: Counter) -> str:
    if plan_mode:
        return "plan"
    if not recent:
        return "unknown (no steps yet)"
    last = recent[-1]
    if last["kind"] == "skill":
        return "housekeeping (skill just loaded)"
    if fail_streak or last.get("failed"):
        return "debug"
    if last.get("bash") in ("outward", "vcs_write"):
        return "integrate"
    looked_at_diff = any("diff" in s.get("cmd", "") for s in recent if s.get("bash") == "vcs_read")
    if looked_at_diff and not edited:
        return "review"
    tally = Counter()
    for s in recent[-5:]:
        if s["kind"] in ("read", "search", "lsp", "web") or s.get("bash") in ("search", "vcs_read"):
            tally["explore"] += 1
        elif s["kind"] == "edit":
            tally["implement"] += 1
        elif s.get("bash") == "verify":
            tally["verify"] += 1
        elif s["kind"] == "mcp":
            tally["integrate" if s.get("mcp_write") else "explore"] += 1
    return tally.most_common(1)[0][0] if tally else "unknown"


def _step_line(s: dict[str, Any]) -> str:
    if s["kind"] == "bash":
        line = f"bash[{s.get('bash')}] `{_snip(s.get('cmd', ''), 80)}`"
    elif s["kind"] == "mcp":
        line = f"mcp[{'write' if s.get('mcp_write') else 'read'}] {s['target']}"
    else:
        line = f"{s['kind']} {_snip(s.get('target', ''), 70)}".strip()
        if s["kind"] == "edit" and s.get("lines"):
            line += f" (~{s['lines']} lines)"
    if s.get("failed"):
        line += f" => FAILED sig={s.get('sig')} :: {s.get('tail', '')}"
    elif "failed" in s and s.get("bash") == "verify":
        line += " => passed"
    elif s.get("result_kb", 0) >= 4:
        line += f" => {s['result_kb']}KB returned"
    return line


# --------------------------------------------------------------------------
# What gets sent to Jev
# --------------------------------------------------------------------------

def build_state_v2(ledger: dict[str, Any], session: dict[str, Any] | None = None,
                   *, mid_run: bool = False) -> str:
    """One situation report, used for both human turns and mid-run rechecks.
    `session` is the router's per-session dict; it contributes the things only
    the router knows (current model, cache warmth, recent output sizes)."""
    doc = dict(ledger)
    if session:
        doc["router"] = {
            "current_model": session.get("last_model") or "none yet",
            "highest_tier_that_authored_edits": session.get("author_tier"),
            "recent_output_tokens_per_turn": list(session.get("out_recent") or []),
        }
    framing = (
        "A coding agent (Claude Code) is mid-run with no new human message. "
        "Judge its NEXT step from this situation report."
        if mid_run else
        "A coding agent (Claude Code) has just received the human message in "
        "unit.latest_human_ask. Judge the NEXT step it must take. Fields under "
        "activity, verification and risk are measured facts from the session, "
        "not opinions."
    )
    return framing + "\n" + json.dumps(doc, separators=(",", ":"), ensure_ascii=False)


# --------------------------------------------------------------------------
# Policy: answers + ledger -> wanted tier
# --------------------------------------------------------------------------

def _a(answers: dict[str, Any], q: str, field: str, default: float) -> tuple[float, float]:
    d = answers.get(q) or {}
    try:
        return float(d.get(field, default)), float(d.get("confidence", 1.0 if field == "noul" else 0.0))
    except (TypeError, ValueError):
        return default, 0.0


def pick_tier_v2(answers: dict[str, Any], ledger: dict[str, Any], n_tiers: int,
                 *, current: int | None = None, author_tier: int | None = None) -> dict[str, Any]:
    """Returns {"tier", "reasons", "safety", "sticky", "effort"}.
    effort is None (leave the client's effort level alone), "medium" or "low".
    tier None means HOLD the session tier (housekeeping / blocked on human).
    safety True means the pick is a floor set by error cost: the cache policy
    must not hold it down and must not downgrade past it. The floor is
    re-derived at every recheck, so it lifts when its cause does (a check
    starts running, the failure stops repeating). sticky True means the floor
    holds for the whole work unit: the user corrected the agent, and no
    mid-run evidence can take that back."""
    top, mid = n_tiers - 1, min(1, n_tiers - 1)
    why: list[str] = []
    ver, risk_l, har = ledger["verification"], ledger["risk"], ledger["harness"]

    # ---- phase: Jev's call, falling back to the tool-derived hint ----------
    ph = answers.get("phase") or {}
    phase, ph_conf = str(ph.get("choice", "")), float(ph.get("confidence", 0.0))
    hint = str(ledger["activity"]["phase_hint_from_tools"]).split(" ")[0]
    if ph_conf < MIN_CONFIDENCE and hint in PHASES:
        why.append(f"phase unsure ({ph_conf:.2f}), using tool hint")
        phase = hint
    traj = str((answers.get("trajectory") or {}).get("choice", "just_started"))
    if phase == "housekeeping" or traj == "blocked_on_human":
        return {"tier": None, "reasons": [f"hold: {phase or traj}"], "safety": False, "sticky": False,
                "effort": None}

    base = {"clarify": mid, "explore": 0, "plan": top, "implement": mid, "debug": mid,
            "verify": 0, "review": mid, "integrate": 0}.get(phase, mid)
    why.append(f"{phase or 'unknown'} phase")

    # ---- cognitive demand: at most one step either way ----------------------
    demand, d_conf = _a(answers, "next_step_demand", "score", 1.0)
    cross, _ = _a(answers, "cross_cutting", "noul", 0.0)
    repo, _ = _a(answers, "repo_specific", "noul", 0.0)
    spec, s_conf = _a(answers, "spec_completeness", "score", 1.0)
    adj = 0
    if d_conf >= MIN_CONFIDENCE and demand > 1.4:
        adj, _ = 1, why.append(f"hard step ({demand:.1f})")
    elif d_conf >= MIN_CONFIDENCE and demand < 0.6:
        adj, _ = -1, why.append(f"rote step ({demand:.1f})")
    if cross > 0.7 and adj < 1:
        adj, _ = adj + 1, why.append("cross-cutting")
    if phase == "implement" and s_conf >= MIN_CONFIDENCE and spec > 1.4 and adj < 1:
        adj, _ = adj + 1, why.append("implementing without a plan")
    if phase == "explore" and ledger["activity"]["searches_with_large_results"] >= 2 \
            and not har["lsp_available"] and adj < 1:
        adj, _ = adj + 1, why.append("noisy search, no LSP: triage needs judgment")
    tier = max(0, min(top, base + max(-1, min(1, adj))))
    if repo > 0.7 and phase in ("implement", "debug", "review") and tier < mid:
        tier, _ = mid, why.append("repo-specific knowledge")

    # ---- cost of an undetected error: floors, not nudges --------------------
    safety = False
    # no answer from Jev: fall back on the fact of whether checks are running
    oracle, _ = _a(answers, "oracle_in_loop", "noul", 0.6 if ver["check_runs"] else 0.0)
    if ver["check_runs"] == 0:
        oracle = min(oracle, 0.3)          # a check nobody has run is not an oracle
    risk, _ = _a(answers, "risk_surface", "noul", 0.0)
    risk = max(risk, 0.75 if risk_l["risk_surface_hits"] else 0.0)
    irrev, _ = _a(answers, "irreversible", "noul", 0.0)
    irrev = max(irrev, 0.75 if (risk_l["outward_or_destructive_commands"]
                                or risk_l["mcp_write_calls"]) and phase == "integrate" else 0.0)

    if irrev > 0.6:
        floor = top if risk > 0.6 else mid
        if tier < floor:
            tier, safety = floor, True
            why.append("irreversible action" + (" on a risk surface" if floor == top else ""))
    if risk > 0.6 and oracle < 0.4 and phase in ("implement", "debug", "review", "plan"):
        floor = top if demand >= 1.0 else mid
        if tier < floor:
            tier, safety = floor, True
            why.append("risk surface with no check in the loop")
    elif oracle > 0.7 and risk <= 0.6 and demand < 1.0 and tier > 0 \
            and phase in ("implement", "debug", "verify"):
        tier -= 1
        why.append(f"errors get caught (oracle {oracle:.2f}): discount")

    # ---- how it is going -----------------------------------------------------
    up_from = max(tier, current if current is not None else tier)
    if ver["same_failure_repeats"] >= 3 or traj == "thrashing":
        tier, safety = min(top, up_from + 1), True
        why.append(f"thrashing (same failure x{ver['same_failure_repeats']})")
    elif traj == "drifting":
        tier = min(top, up_from + 1)
        why.append("drifting from the request")
    unv, _ = _a(answers, "unverified_confidence", "noul", 0.0)
    if unv > 0.7 and ver["edits_since_last_check"] >= 3:
        tier = min(top, max(tier, mid))
        why.append(f"claims unverified, {ver['edits_since_last_check']} edits unchecked")
    corr, _ = _a(answers, "user_correcting", "noul", 0.0)
    sticky = False
    if corr > 0.65:
        tier, safety, sticky = min(top, up_from + 1), True, True
        why.append("user is correcting the agent")

    # ---- review: the reviewer must not be weaker than the author -------------
    depth = str((answers.get("review_depth") or {}).get("choice", "not_review"))
    if phase == "review":
        if depth == "security_or_design":
            tier, _ = top, why.append("security/design review")
        elif depth == "conformance" and risk <= 0.6:
            tier, _ = min(tier, mid), why.append("conformance review")
        if author_tier is not None and tier < author_tier:
            tier, _ = author_tier, why.append("reviewer >= author")

    # ---- explicit human intent and caps --------------------------------------
    if har["plan_mode"] and tier < top:
        tier, _ = top, why.append("plan mode")
    if har["thinking_budget"] >= 16000 and tier < mid:
        tier, _ = mid, why.append("user asked for extended thinking")
    canon, _ = _a(answers, "canonical", "noul", 0.0)
    if canon > 0.7 and not safety and tier >= top and top >= 1:
        tier, _ = top - 1, why.append(f"canonical build, capped ({canon:.2f})")
    if har["agent_role"] == "explore" and not safety:
        tier, _ = min(tier, mid), why.append("explore subagent, capped")

    # ---- effort: the second lever ------------------------------------------
    # Model choice sets price per token; effort sets how many tokens and how
    # many agentic turns get spent. None means "leave the client's level
    # alone". Only ever recommends going DOWN, and never while anything above
    # flagged risk: a hard or dangerous step keeps whatever the user set.
    effort = None
    calm = not safety and not har["plan_mode"] and traj not in ("thrashing", "drifting")
    if calm and d_conf >= MIN_CONFIDENCE and unv <= 0.7:
        if demand < 0.6 and phase in ("explore", "verify", "integrate", "implement"):
            effort = "low"
        elif demand <= 1.2 and phase in ("explore", "verify", "integrate", "implement", "clarify"):
            effort = "medium"
    if effort:
        why.append(f"effort {effort}")

    return {"tier": max(0, min(top, tier)), "reasons": why, "safety": safety,
            "sticky": sticky, "effort": effort}


# --------------------------------------------------------------------------
# Event-driven rechecks
# --------------------------------------------------------------------------

def ledger_summary(ledger: dict[str, Any]) -> dict[str, Any]:
    """The few fields worth remembering between turns, to detect events."""
    return {
        "phase_hint": str(ledger["activity"]["phase_hint_from_tools"]).split(" ")[0],
        "fail_streak": ledger["verification"]["consecutive_failures"],
        "repeats": ledger["verification"]["same_failure_repeats"],
        "risk": len(ledger["risk"]["risk_surface_hits"]),
        "skills": len(ledger["harness"]["skills_loaded"]),
        "unchecked": ledger["verification"]["edits_since_last_check"],
    }


def recheck_reason(prev: dict[str, Any] | None, ledger: dict[str, Any],
                   run_len: int, every: int = 6) -> str | None:
    """Why a mid-run recheck is due, or None. Events first, cadence last."""
    now = ledger_summary(ledger)
    if prev:
        if now["phase_hint"] != prev["phase_hint"] and "unknown" not in (
                now["phase_hint"], prev["phase_hint"]):
            return f"phase shift {prev['phase_hint']} -> {now['phase_hint']}"
        if now["fail_streak"] >= 2 > prev["fail_streak"]:
            return "second consecutive failed check"
        if now["repeats"] >= 3 > prev["repeats"]:
            return "same failure a third time"
        if now["risk"] > prev["risk"]:
            return "new risk surface touched"
        if now["skills"] > prev["skills"]:
            return "skill loaded"
        if now["unchecked"] >= 5 > prev["unchecked"]:
            return "five edits without a check"
    if every > 0 and run_len > 0 and run_len % every == 0:
        return f"cadence (turn {run_len})"
    return None


def outcome_labels(ledger: dict[str, Any], answers: dict[str, Any] | None) -> dict[str, Any]:
    """Write this into the trace next to every decision. Joined offline with
    the tier that served the preceding steps, it answers the question the v1
    router could not: P(check passes | tier, phase, demand) per repo. That is
    the calibration set for every threshold in pick_tier_v2."""
    v = ledger["verification"]
    return {
        "last_check": v["last_check"],
        "fail_streak": v["consecutive_failures"],
        "same_failure_repeats": v["same_failure_repeats"],
        "user_correcting": (answers or {}).get("user_correcting", {}).get("noul"),
        "trajectory": (answers or {}).get("trajectory", {}).get("choice"),
    }


# --------------------------------------------------------------------------
# INTEGRATION
#   jev_router.py (v2, same directory) wires this module in: route_decision
#   builds the ledger, asks QUESTIONS_V2 on human turns and STEP_QUESTIONS_V2
#   when recheck_reason fires mid-run, feeds pick_tier_v2's tier through the
#   cache policy, keeps the safety floor, tracks author_tier, and writes
#   outcome_labels into the trace.
#
# LIMITS, stated plainly
#   - The router sees history. A `git push` appears only after it ran; the
#     `irreversible` question predicts the next step, it cannot gate the last.
#     Gating belongs in a Claude Code PreToolUse hook, not in a model router.
#   - FAIL_TEXT / PASS_TEXT are heuristics over arbitrary tool output. Wrong
#     often enough that same_failure_repeats needs 3, not 2.
#   - Subagent role is sniffed from system-prompt wording and will rot.
#   - Every threshold above is a prior. outcome_labels exists so they stop
#     being priors.
# --------------------------------------------------------------------------

if __name__ == "__main__":
    def tu(i, name, **inp):
        return {"type": "tool_use", "id": i, "name": name, "input": inp}

    def tr(i, text, err=False):
        return {"type": "tool_result", "tool_use_id": i, "content": text, "is_error": err}

    fail = "FAILED tests/test_refund.py::test_partial - AssertionError: 1200 != 1250\n1 failed, 41 passed"
    demo = {
        "tools": [{"name": n} for n in ("Bash", "Read", "Edit", "Skill", "mcp__jira__create_issue",
                                        "mcp__jira__search")],
        "thinking": {"type": "enabled", "budget_tokens": 8000},
        "messages": [
            {"role": "user", "content": [{"type": "text", "text":
                "<system-reminder>Contents of CLAUDE.md ...</system-reminder>"
                "partial refunds are off by the tax amount, fix it"}]},
            {"role": "assistant", "content": [tu("1", "Bash", command="rg -n 'def refund' services/payments")]},
            {"role": "user", "content": [tr("1", "services/payments/refund.py:88:def refund(")]},
            {"role": "assistant", "content": [tu("2", "Read", file_path="services/payments/refund.py")]},
            {"role": "user", "content": [tr("2", "...")]},
            {"role": "assistant", "content": [tu("3", "Edit", file_path="services/payments/refund.py",
                                                 old_string="a", new_string="b\nc")]},
            {"role": "user", "content": [tr("3", "ok")]},
            {"role": "assistant", "content": [tu("4", "Bash", command="cd services/payments && pytest -q | tail -5")]},
            {"role": "user", "content": [tr("4", fail)]},
            {"role": "assistant", "content": [tu("5", "Edit", file_path="services/payments/refund.py",
                                                 old_string="b", new_string="d")]},
            {"role": "user", "content": [tr("5", "ok")]},
            {"role": "assistant", "content": [tu("6", "Bash", command="cd services/payments && pytest -q | tail -5")]},
            {"role": "user", "content": [tr("6", fail)]},
            {"role": "assistant", "content": [{"type": "text", "text": "Fixed the rounding; should pass now."},
                                              tu("7", "Bash", command="cd services/payments && pytest -q | tail -5")]},
            {"role": "user", "content": [tr("7", fail)]},
        ],
    }
    led = extract_ledger(demo)
    state = build_state_v2(led, {"last_model": "claude-haiku-4-5", "author_tier": 0}, mid_run=True)
    print(state)
    print(f"\nstate: {len(state)} chars (~{len(state) // 4} tokens, "
          f"~${len(state) / 4 * 0.042 / 1e6:.6f} per Jev call)")
    print("recheck:", recheck_reason({"phase_hint": "implement", "fail_streak": 1, "repeats": 2,
                                      "risk": 1, "skills": 0, "unchecked": 0}, led, run_len=7))
    # No Jev here: show what the deterministic half alone concludes.
    print("policy :", pick_tier_v2({}, led, 3, current=0, author_tier=0))
