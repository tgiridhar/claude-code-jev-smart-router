#!/usr/bin/env python3
"""
jev_router.py (v2) - a per-step model router for Claude Code, judged by Jev.

Claude Code speaks the Anthropic Messages API. This proxy sits in front of it:
it reads each incoming /v1/messages request, rebuilds a factual ledger of the
session from the request itself (jev_ontology.extract_ledger), asks Jev
(TypeSafe's System One model) a set of atomic questions about the agent's NEXT
step, picks a Claude model from that, and forwards the request upstream
unchanged apart from the `model` field.

    Claude Code --> jev_router --> ledger (pure code, <1ms)
                               --> Jev (judgment, ~100ms)
                               --> api.anthropic.com (chosen model)

What changed from v1
    v1 classified the newest MESSAGE (mechanical / local_change / ...). v2
    routes the NEXT STEP of a work unit on two axes: how much judgment it
    needs, and what an undetected mistake would cost. The ontology, the state
    sent to Jev and the tier policy live in jev_ontology.py; this file keeps
    the proxy, the cache economics, telemetry and the dashboard.

Files
    jev_router.py     this file
    jev_ontology.py   questions, ledger extraction, tier policy (same directory)

Run:
    pip install fastapi uvicorn httpx
    export TYPESAFE_API_KEY=...          # from console.typesafe.ai/settings/keys
    export ANTHROPIC_API_KEY=...         # forwarded upstream if the client sends none
    uvicorn jev_router:app --port 8787

Point Claude Code at it:
    export ANTHROPIC_BASE_URL=http://127.0.0.1:8787
    claude

Read ROUTING NOTES at the bottom of this file before trusting it with real
spend. The cache interaction is the part that decides whether this saves money
or costs you more.
"""

from __future__ import annotations

import hashlib
import re
import json
import logging
import os
import time
from collections import OrderedDict, defaultdict, deque
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse

from jev_ontology import (
    QUESTIONS_V2,
    STEP_QUESTIONS_V2,
    TOOL_KINDS,
    build_state_v2,
    extract_ledger,
    ledger_summary,
    outcome_labels,
    pick_tier_v2,
    recheck_reason,
    split_human_text,
)

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

UPSTREAM = os.environ.get("ROUTER_UPSTREAM", "https://api.anthropic.com")
TYPESAFE_URL = os.environ.get("TYPESAFE_URL", "https://api.typesafe.ai/v1/systemone")
TYPESAFE_KEY = os.environ.get("TYPESAFE_API_KEY", "")
TYPESAFE_MODEL = os.environ.get("TYPESAFE_MODEL", "jev-latest")

# The ladder, cheapest first. Index into this is the "tier".
TIERS: list[str] = json.loads(
    os.environ.get(
        "ROUTER_TIERS",
        '["claude-haiku-4-5", "claude-sonnet-5", "claude-opus-5"]',
    )
)

# Only route requests whose model field is this sentinel. Set to "" to route
# everything. Using a sentinel lets you keep `/model opus` working as an
# explicit override inside Claude Code.
ROUTE_SENTINEL = os.environ.get("ROUTER_SENTINEL", "")

# Classifier confidence gate. Shared with jev_ontology via the same env var.
MIN_CONFIDENCE = float(os.environ.get("ROUTER_MIN_CONFIDENCE", "0.45"))

# How many consecutive cheap verdicts before we actually step down a tier.
# Only used when no usage telemetry has been observed yet. See notes.
DOWNGRADE_PATIENCE = int(os.environ.get("ROUTER_DOWNGRADE_PATIENCE", "3"))

CLASSIFY_TIMEOUT = float(os.environ.get("ROUTER_CLASSIFY_TIMEOUT", "2.0"))

# Runtime kill switch, flipped via POST /router/enable | /router/disable |
# /router/toggle without restarting. Disabled means pure passthrough: no
# classification, no model rewrite. Usage telemetry keeps recording either
# way, so the dashboard stays comparable across on and off periods.
_control: dict[str, bool] = {"enabled": os.environ.get("ROUTER_ENABLED", "1") != "0"}

# Session tracing: set ROUTER_TRACE_DIR to a directory and every session gets
# an append-only JSONL file there -- one line per routing decision (now with
# outcome labels: last check result, failure streak, user corrections), one
# per completed response, one per dropped stream. Joined offline, these answer
# "did the tier that served these steps get the checks to pass" -- the
# calibration set for every threshold in jev_ontology.pick_tier_v2.
# Traces include short previews of your prompts, so they live on your disk
# only and the feature is off by default.
TRACE_DIR = os.environ.get("ROUTER_TRACE_DIR", "")


def trace(session_key: str, record: dict[str, Any]) -> None:
    if not TRACE_DIR:
        return
    try:
        os.makedirs(TRACE_DIR, exist_ok=True)
        sid = re.sub(r"[^A-Za-z0-9_.-]", "_", session_key.split("/")[0])[:80]
        record["at"] = round(time.time(), 3)
        with open(os.path.join(TRACE_DIR, sid + ".jsonl"), "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:  # noqa: BLE001 - tracing must never break a turn
        pass

# ---- cache economics ------------------------------------------------------
# $ per MTok. VERIFY against current pricing before trusting the breakeven
# math; override with ROUTER_PRICES='{"model": {"in": x, "out": y}, ...}'.
PRICES: dict[str, dict[str, float]] = json.loads(
    os.environ.get(
        "ROUTER_PRICES",
        json.dumps({
            "claude-haiku-4-5": {"in": 1.0, "out": 5.0},
            "claude-sonnet-5": {"in": 3.0, "out": 15.0},
            "claude-opus-5": {"in": 5.0, "out": 25.0},
        }),
    )
)
CACHE_WRITE_MULT = float(os.environ.get("ROUTER_CACHE_WRITE_MULT", "1.25"))
CACHE_HIT_MULT = float(os.environ.get("ROUTER_CACHE_HIT_MULT", "0.10"))

# The cache lives 5 minutes, measured from the START of the request that last
# touched it, and generation time counts against it. 240s is the safety margin
# version of "probably still warm".
CACHE_TTL_SAFE = float(os.environ.get("ROUTER_CACHE_TTL_SAFE", "240"))

# A one-time cache-rebuild cost buys savings on this many future turns. The
# recurring-vs-one-time asymmetry is why even a warm cache is worth breaking
# when the session has settled into cheap work.
SWITCH_HORIZON = float(os.environ.get("ROUTER_SWITCH_HORIZON", "3"))

# Output-token estimates for the three gen_volume levels Jev scores.
GEN_ESTIMATES = [400.0, 2000.0, 8000.0]

# ---- a tier is a price per token, not a cost per task ---------------------
# Artificial Analysis measured Sonnet 5 at max effort costing MORE per task
# than Opus 4.8 at the same $3/$15 vs $5/$25 list prices, purely because it
# emits more tokens and takes more agentic turns. So the breakeven below does
# not assume the target model will produce the same output as the current
# one. Two corrections, both per model:
#   output volume -- learned online: an EMA of output tokens per turn for each
#       model this router has actually served. Until both sides of a switch
#       have OUT_RATE_MIN_SAMPLES turns, ROUTER_VERBOSITY supplies a prior
#       ('{"claude-sonnet-5": 1.4}' = expect 1.4x the tokens), default 1.0.
#   turn count -- not observable per step, so config only: ROUTER_TURN_MULT
#       ('{"claude-haiku-4-5": 1.3}' = expect 1.3x the turns for the same
#       work). Get real values from your traces; default 1.0.
VERBOSITY: dict[str, float] = json.loads(os.environ.get("ROUTER_VERBOSITY", "{}"))
TURN_MULT: dict[str, float] = json.loads(os.environ.get("ROUTER_TURN_MULT", "{}"))
OUT_RATE_MIN_SAMPLES = int(os.environ.get("ROUTER_OUT_RATE_MIN_SAMPLES", "8"))
_out_rate: dict[str, dict[str, float]] = defaultdict(lambda: {"ema": 0.0, "n": 0})

# ---- effort steering (opt-in) ---------------------------------------------
# Changing top-level output_config.effort between requests invalidates the
# prompt cache, exactly like a model switch. Per-message effort (beta) does
# not: an empty role:"system" message carrying output_config.effort changes
# the level from the next user turn and leaves the cached prefix intact. It
# exists only on the models listed below; anything else 400s. When enabled,
# the router drops effort to medium/low for routine and rote steps WITHOUT
# leaving the model, which is the one saving on a warm session that costs
# nothing to take. It never raises effort above what the client set.
# Off by default: it is beta, and it is the only place this proxy edits
# `messages`.
EFFORT_STEERING = os.environ.get("ROUTER_EFFORT_STEERING", "0") == "1"
EFFORT_CAPABLE = tuple(json.loads(os.environ.get(
    "ROUTER_EFFORT_CAPABLE", '["claude-opus-5", "claude-fable-5-1", "claude-mythos-5-1"]')))
EFFORT_BETA = os.environ.get("ROUTER_EFFORT_BETA", "mid-conversation-output-config-2026-07-01")
EFFORT_RANK = {"low": 0, "medium": 1, "high": 2, "xhigh": 3, "max": 4}
_effort = {"off": not EFFORT_STEERING, "markers_sent": 0}

# Jev pricing: $/MTok input; output is free ("too cheap to meter").
JEV_PRICE_IN = float(os.environ.get("ROUTER_JEV_PRICE_IN", "0.042"))

# Mid-run rechecks are EVENT-driven in v2 (phase shift, repeated failure, new
# risk surface, skill load, edits piling up unchecked -- see
# jev_ontology.recheck_reason). This is only the fallback cadence for runs
# where no event fires. 0 disables the cadence; events still fire.
RECHECK_EVERY = int(os.environ.get("ROUTER_RECHECK_EVERY", "6"))

# Claude Code interleaves tiny sidecar calls (permission classifiers, quota
# probes, topic detection) into the same session: verdict-sized max_tokens,
# out=9-ish responses. They are not conversation turns. Requests at or below
# this max_tokens are pinned to ROUTER_UTILITY_MODEL, never classified, and
# kept OUT of the session's routing state -- otherwise they reset the
# continuation counter every turn (starving rechecks) and overwrite the
# session's prefix/output telemetry with their own.
UTILITY_MAX_TOKENS = int(os.environ.get("ROUTER_UTILITY_MAX_TOKENS", "1024"))
UTILITY_MODEL = os.environ.get(
    "ROUTER_UTILITY_MODEL", TIERS[1] if len(TIERS) > 1 else TIERS[0]
)


def price(model: str, kind: str) -> float:
    p = PRICES.get(model)
    if p is None:  # dated ids like claude-opus-5-20260115
        p = next((v for k, v in PRICES.items() if model.startswith(k)), None)
    return (p or {"in": 5.0, "out": 25.0})[kind]


def tier_of(model: str | None) -> int | None:
    """Tier index of a model id, tolerating dated suffixes."""
    if not model:
        return None
    for i, t in enumerate(TIERS):
        if model == t or model.startswith(t):
            return i
    return None


logging.basicConfig(
    level=os.environ.get("ROUTER_LOG_LEVEL", "INFO"),
    format="%(asctime)s  %(message)s",
)
log = logging.getLogger("jev-router")

app = FastAPI()


# --------------------------------------------------------------------------
# Money math
# --------------------------------------------------------------------------

def _lookup(table: dict[str, float], model: str) -> float:
    if model in table:
        return float(table[model])
    return float(next((v for k, v in table.items() if model.startswith(k)), 1.0))


def output_on(tgt: str, cur: str, pred_out: float) -> float:
    """Expected output tokens if `tgt` did the step `cur` would spend
    pred_out on. Observed ratio when both models have history, else the
    configured prior, else unchanged."""
    a, b = _out_rate[tgt], _out_rate[cur]
    if a["n"] >= OUT_RATE_MIN_SAMPLES and b["n"] >= OUT_RATE_MIN_SAMPLES and b["ema"] > 0:
        # Clamped: tiers serve different work, so the raw ratio is confounded
        # by routing itself. It is a correction, not a measurement.
        return pred_out * max(0.5, min(3.0, a["ema"] / b["ema"]))
    return pred_out * _lookup(VERBOSITY, tgt) / _lookup(VERBOSITY, cur)


def switch_delta(cur: str, tgt: str, prefix: float, pred_out: float) -> tuple[float, float]:
    """(per-turn saving, one-time cost) of moving cur -> tgt with the cache
    warm. Each side is priced as a full turn -- its own expected output at
    its own output price, plus a cache read of the prefix at its own input
    price -- and the target's turn is scaled by how many more turns it is
    expected to need. The saving can be NEGATIVE: a cheaper-per-token model
    that talks more and loops more is not cheaper, and then nothing moves."""
    def turn(model: str, out: float) -> float:
        return (out * price(model, "out") + prefix * CACHE_HIT_MULT * price(model, "in")) / 1e6
    more_turns = _lookup(TURN_MULT, tgt) / _lookup(TURN_MULT, cur)
    per_turn = turn(cur, pred_out) - more_turns * turn(tgt, output_on(tgt, cur, pred_out))
    one_time = prefix * CACHE_WRITE_MULT * price(tgt, "in") / 1e6
    return per_turn, one_time


# --------------------------------------------------------------------------
# Per-conversation state
#
# The session LEDGER is not stored: it is recomputed from each request's own
# message history. What lives here is only what the request cannot tell us --
# the tier we chose, cache telemetry from upstream, and three small v2 fields.
# A few kilobytes, deliberately in-process.
# --------------------------------------------------------------------------

_state: dict[str, dict[str, Any]] = defaultdict(
    lambda: {
        "tier": None,
        "cheap_streak": 0,
        "touched": time.time(),
        # cache telemetry, filled in from upstream usage as responses pass by
        "last_model": None,     # model that served the previous turn
        "last_at": 0.0,         # start time of the previous request
        "prefix_tokens": 0,     # cached prefix size: cache_read + cache_creation + input
        "out_avg": 0.0,         # decaying average of observed output tokens
        "out_recent": [],       # last few per-turn output counts, verbatim
        "run_len": 0,           # continuation turns since the last real user turn
        # v2
        "floor": 0,             # current safety floor; re-derived at each recheck
        "unit_floor": 0,        # floor that holds for the whole unit (user correction)
        "author_tier": None,    # highest tier that authored edits in this session
        "led_prev": None,       # ledger_summary of the previous turn, for event detection
        # effort steering
        "want_effort": None,    # latest recommendation: None | "medium" | "low"
        "markers": [],          # [(index in the client's messages, effort)] already injected
        "n_msgs": 0,            # history length last seen; shrinking means /compact
    }
)
STATE_MAX = 512


def _evict_state() -> None:
    if len(_state) <= STATE_MAX:
        return
    for key, _ in sorted(_state.items(), key=lambda kv: kv[1]["touched"])[
        : len(_state) - STATE_MAX
    ]:
        _state.pop(key, None)


def _text_of(message: dict[str, Any]) -> str:
    """Flatten a Messages-API content block list down to plain text."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(b.get("text", "")) for b in content
        if isinstance(b, dict) and b.get("type") == "text"
    )


def session_key(request: Request, body: dict[str, Any]) -> str:
    """
    State is tracked per session AND per agent. Claude Code labels every
    request with x-claude-code-session-id, and requests from spawned subagents
    additionally carry x-claude-code-agent-id, so the main thread and each
    subagent hold separate sticky tiers: a burst of cheap subagent traffic
    should never drag the main conversation's tier down, nor the reverse.
    Falls back to hashing the first user message when the headers are absent.
    """
    sid = request.headers.get("x-claude-code-session-id")
    aid = request.headers.get("x-claude-code-agent-id", "main")
    if sid:
        return f"{sid}/{aid}"
    for m in body.get("messages") or []:
        if m.get("role") == "user":
            return hashlib.sha1(_text_of(m).encode()).hexdigest()[:16]
    return "empty"


def is_agentic_continuation(body: dict[str, Any]) -> bool:
    """True when the newest user message carries only tool_result blocks (and
    harness reminders): Claude mid-task feeding results back, not the person
    asking for anything new. v1 held these blind; v2 still skips the
    human-turn questions but checks jev_ontology.recheck_reason on each one."""
    for m in reversed(body.get("messages") or []):
        if m.get("role") == "user":
            c = m.get("content")
            if not isinstance(c, list):
                return False
            text, _reminders = split_human_text(m)
            has_tool = any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in c
            )
            return has_tool and not text
    return False


def note_authorship(s: dict[str, Any], body: dict[str, Any]) -> None:
    """If the newest assistant message made an edit, the model that served the
    previous turn authored it. Feeds the 'reviewer >= author' rule."""
    for m in reversed(body.get("messages") or []):
        if m.get("role") != "assistant":
            continue
        c = m.get("content")
        edited = isinstance(c, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_use"
            and TOOL_KINDS.get(str(b.get("name"))) == "edit" for b in c
        )
        t = tier_of(s.get("last_model"))
        if edited and t is not None:
            s["author_tier"] = max(s.get("author_tier") or 0, t)
        return


def safe_ledger(body: dict[str, Any], is_subagent: bool) -> dict[str, Any] | None:
    try:
        return extract_ledger(body, is_subagent=is_subagent)
    except Exception as exc:  # noqa: BLE001 - extraction must never break a turn
        log.warning("ledger extraction failed (%s), holding", type(exc).__name__)
        return None


def apply_effort(body: dict[str, Any], s: dict[str, Any], model: str) -> bool:
    """Inject per-message effort markers into body["messages"]. Returns True
    when the request now needs the beta header.

    The cache only survives if the prefix is byte-identical from request to
    request, and Claude Code knows nothing about these markers. So every
    marker ever injected into this session is re-inserted at the same index
    on every later request; a new one is added only when the wanted level
    differs from the level currently in force, and only directly before the
    newest user message (where it takes effect immediately).

    Lost state (router restart) or rewritten history (/compact) costs one
    cache miss and then heals. A model that cannot take markers gets none:
    its cache is separate from the capable model's anyway."""
    if _effort["off"]:
        return False
    msgs = body.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return False
    n = len(msgs)
    if n < int(s.get("n_msgs") or 0):
        s["markers"] = []
    s["n_msgs"] = n
    if not model.startswith(EFFORT_CAPABLE):
        s["markers"] = []
        return False

    oc = body.get("output_config")
    baseline = str((oc.get("effort") if isinstance(oc, dict) else None) or "high")
    markers: list[tuple[int, str]] = [tuple(m) for m in s.get("markers") or []]
    in_force = markers[-1][1] if markers else baseline
    want = s.get("want_effort") or baseline
    if EFFORT_RANK.get(want, 2) > EFFORT_RANK.get(baseline, 2):
        want = baseline                      # the client's level is the ceiling
    if want != in_force and msgs[-1].get("role") == "user":
        markers.append((n - 1, want))
    s["markers"] = markers
    if not markers:
        return False
    for idx, eff in sorted(markers, reverse=True):
        if idx <= len(msgs):
            msgs.insert(idx, {"role": "system", "content": [],
                              "output_config": {"effort": eff}})
    _effort["markers_sent"] += 1
    return True


def with_beta(headers: dict[str, str], token: str) -> dict[str, str]:
    out = dict(headers)
    key = next((k for k in out if k.lower() == "anthropic-beta"), "anthropic-beta")
    have = [t.strip() for t in out.get(key, "").split(",") if t.strip()]
    if token not in have:
        have.append(token)
    out[key] = ",".join(have)
    return out


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


_memo: OrderedDict[str, dict[str, Any]] = OrderedDict()
MEMO_MAX = 64


async def classify(
    client: httpx.AsyncClient,
    state: str,
    questions: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not TYPESAFE_KEY:
        return None
    questions = questions or QUESTIONS_V2

    # Claude Code retries failed requests with an identical body. The memo
    # makes a retry classify identically without a second Jev call, so a retry
    # can never land on a different model than the attempt it is retrying.
    memo_key = hashlib.sha256(
        (",".join(sorted(questions)) + "|" + state).encode()
    ).hexdigest()
    if memo_key in _memo:
        _memo.move_to_end(memo_key)
        _metrics["jev"]["memo_hits"] += 1
        return _memo[memo_key]

    try:
        r = await client.post(
            TYPESAFE_URL,
            headers={
                "Authorization": f"Bearer {TYPESAFE_KEY}",
                "Content-Type": "application/json",
            },
            json={"state": state, "model": TYPESAFE_MODEL, "questions": questions},
            timeout=CLASSIFY_TIMEOUT,
        )
        if r.status_code != 200:
            log.warning("classifier returned %s, falling through", r.status_code)
            _metrics["jev"]["errors"] += 1
            return None
        payload = r.json()
        j = _metrics["jev"]
        j["calls"] += 1
        jin = int((payload.get("usage") or {}).get("input_tokens") or 0)
        j["input_tokens"] += jin
        j["cost"] += jin * JEV_PRICE_IN / 1e6
        answers = payload.get("answers")
        if answers:
            _memo[memo_key] = answers
            if len(_memo) > MEMO_MAX:
                _memo.popitem(last=False)
        return answers
    except Exception as exc:  # noqa: BLE001 - never let the classifier break a turn
        log.warning("classifier unavailable (%s), falling through", exc)
        _metrics["jev"]["errors"] += 1
        return None


# --------------------------------------------------------------------------
# Policy: what the work wants (jev_ontology) x what the cache allows (here)
# --------------------------------------------------------------------------


def predicted_output_tokens(answers: dict[str, Any], s: dict[str, Any]) -> float:
    """Jev's gut-check on generation volume, blended with what this session
    has actually been producing."""
    g = answers.get("gen_volume", {})
    idx = max(0, min(len(GEN_ESTIMATES) - 1, round(float(g.get("score", 1.0)))))
    est = GEN_ESTIMATES[idx]
    avg = float(s.get("out_avg") or 0.0)
    return 0.5 * (est + avg) if avg else est


def apply_cache_policy(key: str, want: int, answers: dict[str, Any]) -> tuple[int, str]:
    """
    Human-turn policy. Decide whether the wanted tier is worth acting on, in
    three regimes:

      COLD  -- no cache to lose (first turn, previous request older than the
               TTL, or telemetry absent after a restart). Routing is free:
               take the answer immediately in either direction. When
               telemetry never arrived, fall back to the patience counter.

      WARM, downgrade wanted -- run the breakeven via switch_delta. Switch
               when the recurring saving x SWITCH_HORIZON beats the one-time
               cache rebuild on the target.

      WARM, upgrade wanted -- immediate, always. Under-serving a hard step
               costs more than any token arithmetic saves. Safety floors from
               the ontology arrive here as upgrades, so they are never for
               sale; the caller has already clamped `want` to the floor.

    Prices are per-MTok; everything is divided by 1e6 consistently so units
    cancel. On a subscription this arithmetic approximates usage-limit weight
    rather than dollars; the comparison still points the same direction.
    """
    s = _state[key]
    s["touched"] = time.time()
    current = s["tier"]

    followup = float(answers.get("is_followup", {}).get("noul", 0.0))

    if current is None:
        s["tier"] = want
        return want, "first turn"

    # Continuity hold: "now do the same for the other file" reads as rote in
    # isolation but continues whatever came before. Quality, not cache, so it
    # applies warm or cold.
    if followup > 0.65 and want < current:
        return current, f"held (follow-up {followup:.2f})"

    if want > current:
        s["tier"] = want
        s["cheap_streak"] = 0
        return want, "upgraded"

    if want == current:
        s["cheap_streak"] = 0
        return current, "unchanged"

    # Downgrade wanted. Establish which regime we're in.
    have_telemetry = bool(s.get("prefix_tokens"))
    warm = (
        have_telemetry
        and s.get("last_model") == TIERS[current]
        and (time.time() - float(s.get("last_at") or 0.0)) < CACHE_TTL_SAFE
    )

    if not have_telemetry:
        s["cheap_streak"] += 1
        if s["cheap_streak"] >= DOWNGRADE_PATIENCE:
            s["tier"] = want
            s["cheap_streak"] = 0
            return want, f"downgraded after {DOWNGRADE_PATIENCE} (no telemetry)"
        return current, f"held (streak {s['cheap_streak']}/{DOWNGRADE_PATIENCE}, no telemetry)"

    if not warm:
        s["tier"] = want
        s["cheap_streak"] = 0
        return want, "downgraded (cache cold, routing free)"

    prefix = float(s["prefix_tokens"])
    pred_out = predicted_output_tokens(answers, s)
    per_turn, one_time = switch_delta(TIERS[current], TIERS[want], prefix, pred_out)

    if per_turn * SWITCH_HORIZON > one_time:
        s["tier"] = want
        s["cheap_streak"] = 0
        return want, (
            f"downgraded (warm but worth it: save ~${per_turn:.4f}/turn "
            f"vs ${one_time:.4f} once, prefix {prefix / 1000:.0f}k)"
        )
    return current, (
        f"held (warm cache wins: ${one_time:.4f} to move "
        f"vs ~${per_turn:.4f}/turn saved, prefix {prefix / 1000:.0f}k)"
    )


def step_policy(key: str, want: int | None, answers: dict[str, Any]) -> tuple[int, str]:
    """Mid-run policy. Same composition as the human turn -- the ontology says
    what the next step wants, the cache math says whether moving pays -- with
    two differences: there is no follow-up hold (there is no human message),
    and a downgrade considers every tier between `want` and current, taking
    the most profitable, never going below what the work wants."""
    s = _state[key]
    current = s["tier"]
    if want is None:
        return current, "held (nothing to decide: housekeeping or blocked on human)"
    want = max(want, int(s.get("floor") or 0))
    if want > current:
        s["tier"] = want
        s["cheap_streak"] = 0
        return want, "handed up mid-run"
    if want == current:
        return current, "held (step wants the current tier)"

    prefix = float(s.get("prefix_tokens") or 0)
    warm = (
        prefix > 0
        and s.get("last_model") == TIERS[current]
        and (time.time() - float(s.get("last_at") or 0.0)) < CACHE_TTL_SAFE
    )
    if prefix and not warm:
        s["tier"] = want
        return want, "stepped down mid-run (cache cold, routing free)"
    if not prefix:
        return current, "held (no telemetry yet, not guessing mid-run)"

    pred_out = predicted_output_tokens(answers, s)
    cur_m = TIERS[current]
    best, best_note = None, ""
    for t in range(want, current):
        per_turn, one_time = switch_delta(cur_m, TIERS[t], prefix, pred_out)
        gain = per_turn * SWITCH_HORIZON - one_time
        if gain > 0 and (best is None or gain > best[1]):
            best = (t, gain)
            best_note = f"~${per_turn:.4f}/turn vs ${one_time:.4f} once"
    if best is None:
        return current, (
            f"held (step wants {TIERS[want].replace('claude-', '')}, but no tier "
            f"clears the cache rebuild, prefix {prefix / 1000:.0f}k)"
        )
    s["tier"] = best[0]
    s["cheap_streak"] = 0
    return best[0], f"stepped down mid-run -> {TIERS[best[0]].replace('claude-', '')} ({best_note})"


# --------------------------------------------------------------------------
# Metrics
#
# Everything the dashboard shows, accumulated in memory. Dollar figures are
# "at configured prices": on an API key they approximate the bill, on a
# subscription they approximate usage-limit weight. The baseline is the same
# observed token mix repriced on the top tier. CAVEAT, direction known: a
# weaker model that needs more turns inflates the token mix, and the baseline
# then prices those extra tokens on the top tier too. "Saved" is therefore an
# upper bound; the outcome labels in the traces are how you find the truth.
# --------------------------------------------------------------------------

_metrics: dict[str, Any] = {
    "started_at": time.time(),
    "requests_total": 0,       # /v1/messages requests seen
    "routed": 0,               # a routing decision was made
    "passthrough": 0,          # forwarded untouched (sentinel miss / classifier down)
    "by_model": defaultdict(
        lambda: {"count": 0, "inp": 0, "cache_read": 0, "cache_create": 0,
                 "out": 0, "cost": 0.0}
    ),
    "spend": 0.0,
    "baseline_top": 0.0,
    "jev": {"calls": 0, "memo_hits": 0, "errors": 0, "input_tokens": 0, "cost": 0.0},
    "decisions": deque(maxlen=120),
    "classify_ms": deque(maxlen=300),
}


def usage_cost(model: str, inp: int, cache_read: int, cache_create: int, out: int) -> float:
    return (
        inp * price(model, "in")
        + cache_read * price(model, "in") * CACHE_HIT_MULT
        + cache_create * price(model, "in") * CACHE_WRITE_MULT
        + out * price(model, "out")
    ) / 1e6


def _metrics_snapshot() -> dict[str, Any]:
    return {
        "started_at": _metrics["started_at"],
        "now": time.time(),
        "upstream": UPSTREAM,
        "tiers": TIERS,
        "requests_total": _metrics["requests_total"],
        "routed": _metrics["routed"],
        "passthrough": _metrics["passthrough"],
        "by_model": {k: dict(v) for k, v in _metrics["by_model"].items()},
        "spend": _metrics["spend"],
        "baseline_top": _metrics["baseline_top"],
        "decisions": list(_metrics["decisions"]),
        "classify_ms": list(_metrics["classify_ms"]),
        "sessions": len(_state),
        "enabled": _control["enabled"],
        "jev": dict(_metrics["jev"]),
        "out_rate": {k: {"ema": round(v["ema"]), "n": v["n"]} for k, v in _out_rate.items()},
        "effort": {"steering": not _effort["off"], "requests_with_markers": _effort["markers_sent"]},
    }


# --------------------------------------------------------------------------
# Usage telemetry
#
# Every upstream response reports usage: input_tokens, cache_read_input_tokens,
# cache_creation_input_tokens (in the streaming case inside the message_start
# event) and a final output_tokens (in the last message_delta). The proxy reads
# these off the bytes it is already relaying -- so "was the previous response
# cached, and how big is the prefix" is measured, not guessed. Best-effort
# only: a parse failure just means the policy falls back to patience mode.
# --------------------------------------------------------------------------

_IN_RE = re.compile(rb'"input_tokens"\s*:\s*(\d+)')
_CR_RE = re.compile(rb'"cache_read_input_tokens"\s*:\s*(\d+)')
_CC_RE = re.compile(rb'"cache_creation_input_tokens"\s*:\s*(\d+)')
_OUT_RE = re.compile(rb'"output_tokens"\s*:\s*(\d+)')


class UsageSniffer:
    """Scans relayed chunks for usage numbers without altering or delaying
    them. The head of the stream (where message_start lives) is accumulated,
    because with small chunks a partial window would otherwise match
    input_tokens before the cache fields have arrived and under-count the
    prefix by orders of magnitude. Output tokens are taken as the last match
    seen, which lands on the final message_delta."""

    HEAD_MAX = 16384

    def __init__(self) -> None:
        self.head = b""
        self.tail = b""
        self.inp = self.cache_read = self.cache_create = 0
        self.out = 0

    def feed(self, chunk: bytes) -> None:
        if len(self.head) < self.HEAD_MAX:
            self.head = (self.head + chunk)[: self.HEAD_MAX]
            # First occurrence of each key belongs to message_start, which is
            # the first event on the stream; re-searching as bytes accumulate
            # converges on the complete values.
            m_in = _IN_RE.search(self.head)
            if m_in:
                self.inp = int(m_in.group(1))
            m_cr = _CR_RE.search(self.head)
            if m_cr:
                self.cache_read = int(m_cr.group(1))
            m_cc = _CC_RE.search(self.head)
            if m_cc:
                self.cache_create = int(m_cc.group(1))
        window = self.tail + chunk
        m_out = None
        for m_out in _OUT_RE.finditer(window):
            pass
        if m_out:
            self.out = int(m_out.group(1))
        self.tail = window[-1024:]

    @property
    def prefix_tokens(self) -> int:
        return self.inp + self.cache_read + self.cache_create


def record_usage(
    key: str,
    model: str,
    started_at: float,
    sniff: UsageSniffer,
    event: dict[str, Any] | None = None,
) -> None:
    try:
        s = _state[key]
        s["last_model"] = model
        # The cache TTL runs from the START of the request that touched it,
        # and generation time counts against it, so record the start, not now.
        s["last_at"] = started_at
        if sniff.prefix_tokens:
            s["prefix_tokens"] = sniff.prefix_tokens
        if sniff.out:
            prev = float(s.get("out_avg") or 0.0)
            s["out_avg"] = sniff.out if not prev else 0.5 * prev + 0.5 * sniff.out
            s["out_recent"] = (list(s.get("out_recent") or []) + [sniff.out])[-6:]
            if not key.endswith("/util"):
                r = _out_rate[model]
                r["ema"] = sniff.out if not r["n"] else 0.9 * r["ema"] + 0.1 * sniff.out
                r["n"] += 1

        # metrics
        m = _metrics["by_model"][model]
        m["count"] += 1
        m["inp"] += sniff.inp
        m["cache_read"] += sniff.cache_read
        m["cache_create"] += sniff.cache_create
        m["out"] += sniff.out
        cost = usage_cost(model, sniff.inp, sniff.cache_read, sniff.cache_create, sniff.out)
        m["cost"] += cost
        _metrics["spend"] += cost
        top = TIERS[-1]
        _metrics["baseline_top"] += usage_cost(
            top, sniff.inp, sniff.cache_read, sniff.cache_create, sniff.out
        )
        if event is not None:
            event["cost"] = round(cost, 6)
            event["out"] = sniff.out
            event["prefix"] = sniff.prefix_tokens
            event["cached_pct"] = (
                int(100 * sniff.cache_read / sniff.prefix_tokens)
                if sniff.prefix_tokens else 0
            )

        trace(key, {"ev": "usage", "turn_ts": started_at, "model": model,
            "inp": sniff.inp, "cache_read": sniff.cache_read,
            "cache_create": sniff.cache_create, "out": sniff.out,
            "cost": round(cost, 6), "seconds": round(time.time() - started_at, 1)})
        if sniff.prefix_tokens:
            log.info(
                "usage: prefix=%dk (cached %d%%) out=%d -> %s",
                sniff.prefix_tokens // 1000,
                int(100 * sniff.cache_read / max(1, sniff.prefix_tokens)),
                sniff.out,
                model,
            )
    except Exception:  # noqa: BLE001 - telemetry must never break the relay
        pass


# --------------------------------------------------------------------------
# Proxy
# --------------------------------------------------------------------------

HOP_BY_HOP = {
    "host", "content-length", "connection", "keep-alive", "transfer-encoding",
    "upgrade", "proxy-authenticate", "proxy-authorization", "te", "trailers",
    # Stripped on purpose: with it, the upstream gzips streams and the usage
    # sniffer goes blind -- turns then sit "in flight" forever on the
    # dashboard and never reach the money totals. Identity costs a little
    # bandwidth and buys working telemetry.
    "accept-encoding",
}


def forward_headers(request: Request) -> dict[str, str]:
    """
    Everything except hop-by-hop headers goes upstream untouched.

    anthropic-beta and anthropic-version must be forwarded verbatim, and never
    allowlisted by value: the set of capabilities grows with each Claude Code
    release, and a gateway pinned to the values it saw last month strips the
    next one. On a subscription login that header also carries the OAuth
    capability the upstream requires, and stripping it fails the request 401.

    The credential is left exactly as it arrived. We only supply a key when the
    client sent no credential at all -- injecting one alongside a subscription
    OAuth bearer would change who the request bills to.
    """
    out = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
    # Explicit, because httpx re-adds "gzip, deflate" when the header is
    # merely absent, and a gzipped stream blinds the usage sniffer.
    out["accept-encoding"] = "identity"
    present = {k.lower() for k in out}
    has_credential = "authorization" in present or "x-api-key" in present
    if not has_credential and os.environ.get("ANTHROPIC_API_KEY"):
        out["x-api-key"] = os.environ["ANTHROPIC_API_KEY"]
    return out


def strip_long_context(headers: dict[str, str]) -> dict[str, str] | None:
    """Behind a gateway, Claude Code skips plan checks and will attach the
    long-context beta a subscription doesn't include; the upstream then 400s
    every request. Returns headers without the context-1m token, or None if
    there was nothing to strip."""
    key = next((k for k in headers if k.lower() == "anthropic-beta"), None)
    if key is None:
        return None
    tokens = [t.strip() for t in headers[key].split(",") if t.strip()]
    kept = [t for t in tokens if not t.startswith("context-1m")]
    if len(kept) == len(tokens):
        return None
    out = dict(headers)
    if kept:
        out[key] = ",".join(kept)
    else:
        out.pop(key)
    return out


_LONG_CONTEXT_400 = b"long context beta"


def response_headers(upstream: httpx.Headers, *, decoded: bool) -> dict[str, str]:
    """
    Relay upstream response headers. When httpx has already decompressed the
    body for us, content-encoding and content-length no longer describe what we
    are sending and must be dropped, or the client tries to gunzip plain bytes.
    """
    drop = set(HOP_BY_HOP)
    if decoded:
        drop |= {"content-encoding", "content-length"}
    else:
        drop |= {"content-length"}
    return {k: v for k, v in upstream.items() if k.lower() not in drop}


def record_decision(key: str, event: dict[str, Any], extra: dict[str, Any]) -> None:
    """Every routing decision lands in exactly two places: the dashboard's
    ledger and the session trace."""
    _metrics["decisions"].append(event)
    trace(key, {"ev": "decision", **event, **extra})


async def route_decision(
    request: Request, body: dict[str, Any], started_at: float
) -> tuple[str | None, str, dict[str, Any] | None]:
    """Decide which model serves this request.

    Returns (chosen, telemetry_key, event). chosen None means forward the
    body untouched. The decision tree, in order:
      disabled or sentinel miss  -> passthrough (sentinel swapped for a real model)
      tiny max_tokens            -> utility sidecar: pinned, state untouched
      tool-results-only turn     -> hold the tier, UNLESS the ledger shows an
                                    event (phase shift, repeated failure, new
                                    risk surface, skill load) or the cadence
                                    is due: then ask Jev about the next step
      otherwise (human turn)     -> ask Jev, apply floor, apply cache policy
    """
    _metrics["requests_total"] += 1
    requested = str(body.get("model", ""))
    # Telemetry records regardless of routing, so cache warmth stays tracked
    # across manual /model use and disabled periods.
    key = session_key(request, body)
    is_subagent = "x-claude-code-agent-id" in request.headers
    agent = request.headers.get("x-claude-code-agent-id", "main")[:12]
    n_messages = len(body.get("messages") or [])

    def make_event(wanted: str, chosen: str, why: str, policy: str) -> dict[str, Any]:
        return {"ts": started_at, "requested": requested, "wanted": wanted,
                "chosen": chosen, "why": why, "policy": policy, "agent": agent}

    default_tier = TIERS[min(1, len(TIERS) - 1)]

    if not (_control["enabled"] and ((not ROUTE_SENTINEL) or requested == ROUTE_SENTINEL)):
        _metrics["passthrough"] += 1
        # The sentinel is not a real model; if routing is off but the picker
        # still says "auto", substitute rather than let a fake id 404 upstream.
        if ROUTE_SENTINEL and requested == ROUTE_SENTINEL:
            return default_tier, key, None
        return None, key, None

    mt = int(body.get("max_tokens") or 0)
    if 0 < mt <= UTILITY_MAX_TOKENS:
        # Sidecar verdict call: pin it, skip Jev, and rebucket its telemetry
        # so it cannot reset run_len or overwrite the main thread's stats.
        key = key + "/util"
        _metrics["routed"] += 1
        event = make_event(UTILITY_MODEL, UTILITY_MODEL,
                           f"utility call (max_tokens={mt})",
                           "pinned, session state untouched")
        record_decision(key, event, {"continuation": False, "n_messages": n_messages})
        return UTILITY_MODEL, key, event

    held_tier = _state[key]["tier"] if key in _state else None
    led = safe_ledger(body, is_subagent)
    if led is None:
        # Extraction failed: never guess. Hold what we have, else pass through.
        if held_tier is not None:
            return TIERS[held_tier], key, None
        _metrics["passthrough"] += 1
        return (default_tier if requested == ROUTE_SENTINEL and ROUTE_SENTINEL else None), key, None

    # ---------------- mid-run: tool results only ----------------------------
    if is_agentic_continuation(body) and held_tier is not None:
        s = _state[key]
        s["run_len"] = int(s.get("run_len", 0)) + 1
        s["touched"] = time.time()
        note_authorship(s, body)

        reason = recheck_reason(s.get("led_prev"), led, s["run_len"], RECHECK_EVERY)
        s["led_prev"] = ledger_summary(led)

        why, policy = "agentic continuation", "held (no event, no classify)"
        answers, dt, wanted = None, None, TIERS[held_tier]
        if reason:
            async with httpx.AsyncClient() as c:
                t0 = time.perf_counter()
                answers = await classify(
                    c, build_state_v2(led, s, mid_run=True), STEP_QUESTIONS_V2)
                dt = (time.perf_counter() - t0) * 1000
            if answers:
                _metrics["classify_ms"].append(round(dt))
                pick = pick_tier_v2(answers, led, len(TIERS), current=held_tier,
                                    author_tier=s.get("author_tier"))
                s["want_effort"] = pick["effort"]
                if pick["tier"] is not None:
                    # Re-derive the floor: it lifts when its cause does (the
                    # check went green, the failure stopped repeating). Only a
                    # user correction holds for the whole unit.
                    s["floor"] = max(pick["tier"] if pick["safety"] else 0,
                                     int(s.get("unit_floor") or 0))
                _, policy = step_policy(key, pick["tier"], answers)
                why = f"recheck ({reason}): " + ", ".join(pick["reasons"])
                if pick["tier"] is not None:
                    wanted = TIERS[pick["tier"]]
            else:
                policy = f"held (recheck due: {reason}; classifier unavailable)"
        chosen = TIERS[s["tier"]]
        _metrics["routed"] += 1
        event = make_event(wanted, chosen, why, policy)
        record_decision(key, event, {
            "continuation": True, "run_len": s["run_len"], "recheck": reason,
            "classify_ms": round(dt) if dt is not None else None,
            "n_messages": n_messages, "ledger": s["led_prev"],
            "labels": outcome_labels(led, answers)})
        if reason:
            log.info("%-22s %s (%s; %s)", chosen, "<-", why, policy)
        return chosen, key, event

    # ---------------- human turn (or first sight of a session) --------------
    s_view = _state[key] if key in _state else None
    async with httpx.AsyncClient() as c:
        t0 = time.perf_counter()
        answers = await classify(c, build_state_v2(led, s_view), QUESTIONS_V2)
        dt = (time.perf_counter() - t0) * 1000

    if answers:
        s = _state[key]
        note_authorship(s, body)
        s["led_prev"] = ledger_summary(led)
        pick = pick_tier_v2(answers, led, len(TIERS), current=s["tier"],
                            author_tier=s.get("author_tier"))
        s["want_effort"] = pick["effort"]
        why = ", ".join(pick["reasons"])

        if pick["tier"] is None:
            # Housekeeping / blocked on the human: not a step of work. Inherit
            # the session tier; with no session yet, the middle of the ladder.
            if s["tier"] is None:
                s["tier"] = min(1, len(TIERS) - 1)
            tier, hys, want = s["tier"], "held (not a step of work)", s["tier"]
        else:
            # A real request opens a new work unit: reset the run counter, and
            # the safety floor lives exactly as long as the unit that set it.
            s["run_len"] = 0
            s["unit_floor"] = pick["tier"] if pick["sticky"] else 0
            s["floor"] = pick["tier"] if pick["safety"] else 0
            want = pick["tier"]
            tier, hys = apply_cache_policy(key, want, answers)

        chosen = TIERS[tier]
        _metrics["routed"] += 1
        _metrics["classify_ms"].append(round(dt))
        event = make_event(TIERS[want], chosen, why, hys)
        record_decision(key, event, {
            "continuation": False, "classify_ms": round(dt),
            "n_messages": n_messages, "safety": pick["safety"],
            "user_preview": led["unit"]["latest_human_ask"][:140],
            "ledger": s["led_prev"], "labels": outcome_labels(led, answers)})
        log.info("%-22s %s (%s; %s) classify=%.0fms", chosen, "<-", why, hys, dt)
        return chosen, key, event

    # Classifier unavailable. A session that already has a tier keeps it --
    # bouncing to whatever the client requested would break the cache for
    # nothing. Otherwise forward as requested.
    if held_tier is not None:
        _metrics["passthrough"] += 1
        return TIERS[held_tier], key, None
    if requested == ROUTE_SENTINEL and ROUTE_SENTINEL:
        # Sentinel is not a real model; never let it reach the API.
        _metrics["passthrough"] += 1
        log.info("%s <- classifier unavailable, default tier", default_tier)
        return default_tier, key, None
    _metrics["passthrough"] += 1
    return None, key, None


@app.post("/v1/messages")
async def messages(request: Request):
    raw = await request.body()
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        body = None

    chosen = None
    telemetry_key = None
    event: dict[str, Any] | None = None
    started_at = time.time()
    if isinstance(body, dict):
        chosen, telemetry_key, event = await route_decision(request, body, started_at)
        if chosen is not None:
            body["model"] = chosen
            raw = json.dumps(body).encode()
        _evict_state()

    headers = forward_headers(request)
    # Effort steering: only on routed main-thread/subagent turns, never on
    # sidecars, and only with a plain copy kept for the 400 fallback.
    raw_plain, headers_plain = raw, headers
    if (isinstance(body, dict) and chosen is not None and event is not None
            and telemetry_key and not telemetry_key.endswith("/util")):
        if apply_effort(body, _state[telemetry_key], chosen):
            raw = json.dumps(body).encode()
            headers = with_beta(headers, EFFORT_BETA)

    def effort_rejected(status: int, content: bytes) -> bool:
        """The beta is unavailable or the model refuses markers: switch the
        feature off for this process and let the caller retry plain."""
        if status != 400 or raw is raw_plain:
            return False
        low = content.lower()
        if b"per-turn effort" in low or b"output_config" in low or b"role" in low and b"system" in low:
            _effort["off"] = True
            log.warning("upstream rejected per-message effort; steering disabled, retrying plain")
            return True
        return False
    streaming = bool(isinstance(body, dict) and body.get("stream"))
    url = f"{UPSTREAM}/v1/messages"
    # Inference posts to /v1/messages?beta=true; the query string has to survive.
    params = dict(request.query_params)

    def upstream_unreachable(exc: Exception) -> Response:
        log.warning("upstream connection failed before response: %s", type(exc).__name__)
        return Response(
            content=json.dumps({"type": "error", "error": {
                "type": "api_error",
                "message": f"jev-router: upstream connection failed ({type(exc).__name__}); safe to retry",
            }}),
            status_code=502,
            media_type="application/json",
        )

    if not streaming:
        try:
            async with httpx.AsyncClient(timeout=None) as c:
                r = await c.post(url, content=raw, headers=headers, params=params)
                if effort_rejected(r.status_code, r.content):
                    raw, headers = raw_plain, headers_plain
                    r = await c.post(url, content=raw, headers=headers, params=params)
                if r.status_code == 400 and _LONG_CONTEXT_400 in r.content.lower():
                    h2 = strip_long_context(headers)
                    if h2:
                        log.warning("plan rejected long-context beta; retrying once without it")
                        r = await c.post(url, content=raw, headers=h2, params=params)
        except httpx.HTTPError as exc:
            return upstream_unreachable(exc)
        if telemetry_key and r.status_code == 200:
            sniff = UsageSniffer()
            sniff.feed(r.content)
            record_usage(telemetry_key, chosen or str(body.get("model", "")), started_at, sniff, event)
        # Upstream errors are returned byte-for-byte. Claude Code's retry path
        # matches on the wording of these to decide whether to disable a
        # rejected capability and retry; rewrapping them in your own envelope
        # kills that recovery even if you keep the status code.
        return Response(
            content=r.content,
            status_code=r.status_code,
            headers=response_headers(r.headers, decoded=True),
        )

    # Streaming: open the upstream response first so its status and headers can
    # be relayed. Returning 200 around an upstream 429 or 500 -- which is what
    # you get if you only pass the body through -- hides the error from Claude
    # Code's retry logic entirely.
    client = httpx.AsyncClient(timeout=None)
    try:
        upstream = await client.send(
            client.build_request(
                "POST", url, content=raw, headers=headers, params=params
            ),
            stream=True,
        )
    except httpx.HTTPError as exc:
        await client.aclose()
        return upstream_unreachable(exc)

    if upstream.status_code == 400:
        body400 = await upstream.aread()
        await upstream.aclose()
        if effort_rejected(400, body400):
            raw, headers = raw_plain, headers_plain
            try:
                upstream = await client.send(
                    client.build_request("POST", url, content=raw, headers=headers, params=params),
                    stream=True,
                )
            except httpx.HTTPError as exc:
                await client.aclose()
                return upstream_unreachable(exc)
            body400 = await upstream.aread() if upstream.status_code == 400 else b""
            if upstream.status_code == 400:
                await upstream.aclose()

    if upstream.status_code == 400:
        h2 = strip_long_context(headers) if _LONG_CONTEXT_400 in body400.lower() else None
        if h2 is None:
            await client.aclose()
            return Response(content=body400, status_code=400,
                            media_type="application/json")
        log.warning("plan rejected long-context beta; retrying once without it")
        try:
            upstream = await client.send(
                client.build_request("POST", url, content=raw, headers=h2, params=params),
                stream=True,
            )
        except httpx.HTTPError as exc:
            await client.aclose()
            return upstream_unreachable(exc)

    async def relay():
        # Sniffing reads the raw relayed bytes, so it only works on an
        # unencoded stream; SSE from the API is normally identity-encoded.
        sniffable = telemetry_key and "content-encoding" not in upstream.headers
        sniff = UsageSniffer() if sniffable else None
        sent = 0
        t_rel = time.time()
        try:
            # aiter_raw, not aiter_bytes: no decoding, no re-chunking. Claude
            # Code counts every byte, SSE pings and comment lines included, and
            # aborts a stream that goes quiet for 300s. Buffering or dropping
            # pings makes it abort during long thinking pauses.
            async for chunk in upstream.aiter_raw():
                sent += len(chunk)
                if sniff is not None:
                    sniff.feed(chunk)
                yield chunk
        except BaseException as exc:  # noqa: BLE001 - diagnose, then re-raise
            # CancelledError here means the CLIENT went away (Claude Code
            # aborted or its watchdog fired); httpx errors mean the UPSTREAM
            # side died. Knowing which is the whole diagnosis.
            side = (
                "client closed"
                if exc.__class__.__name__ in ("CancelledError", "ClientDisconnect")
                else "upstream died"
            )
            note = f"{side}: {type(exc).__name__} after {time.time() - t_rel:.0f}s, {sent // 1024}KB relayed"
            if event is not None:
                event["drop"] = note
            if telemetry_key:
                trace(telemetry_key, {"ev": "drop", "turn_ts": started_at, "note": note})
            log.warning("stream aborted (%s)", note)
            raise
        finally:
            if sniff is not None and upstream.status_code == 200:
                record_usage(
                    telemetry_key,
                    chosen or str(body.get("model", "")),
                    started_at,
                    sniff,
                    event,
                )
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        relay(),
        status_code=upstream.status_code,
        headers=response_headers(upstream.headers, decoded=False),
        media_type=upstream.headers.get("content-type", "text/event-stream"),
    )


DASHBOARD_HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Jev Router</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Archivo:wdth,wght@62..125,300..800&display=swap" rel="stylesheet">
<style>
:root{
  color-scheme: light;
  --bg:#F4F6F8; --panel:#FFFFFF; --line:#D8DEE6; --line-soft:#E4E9EF;
  --ink:#1E2733; --muted:#5C6B80; --dim:#8B98A9;
  --haiku:#0C8A6C; --sonnet:#A87B0B; --opus:#CB4136;
  --saved:#0C8A6C;
}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--ink);
  font:400 14px/1.55 Archivo,"Avenir Next","Segoe UI",system-ui,sans-serif}
body{padding:28px clamp(20px,4vw,56px) 60px}
.num{font-variant-numeric:tabular-nums}

/* masthead */
header{display:flex;align-items:flex-end;justify-content:space-between;gap:24px;flex-wrap:wrap}
h1{margin:0;font-size:21px;font-weight:700;letter-spacing:.2px}
h1 span{color:var(--dim);font-weight:400}
.sub{color:var(--muted);font-size:13px;margin-top:3px}
.live{display:inline-block;width:7px;height:7px;border-radius:50%;
  background:var(--saved);margin-right:7px;vertical-align:1px}
.live.down{background:var(--opus)}
@media (prefers-reduced-motion:no-preference){
  .live{animation:pulse 2.4s ease-in-out infinite}
  @keyframes pulse{50%{opacity:.35}}
}
.switch{display:flex;align-items:center;gap:10px;font-size:14px;color:var(--muted)}
.switch b{color:var(--ink);font-weight:600}
.track{position:relative;width:46px;height:25px;border-radius:13px;background:var(--line);
  border:0;cursor:pointer;transition:background .15s;flex:none}
.track::after{content:"";position:absolute;top:3px;left:3px;width:19px;height:19px;
  border-radius:50%;background:#FFF;border:1.5px solid var(--dim);box-shadow:0 1px 2px rgba(30,39,51,.18);transition:transform .15s,border-color .15s}
.track[aria-checked="true"]{background:var(--saved)}
.track[aria-checked="true"]::after{transform:translateX(21px);border-color:#FFF}
.track:focus-visible{outline:2px solid var(--ink);outline-offset:3px}
.counts{color:var(--muted);font-size:13px;margin:14px 0 0}
.counts b{color:var(--ink);font-weight:600}

/* the rail */
#railwrap{margin:26px 0 8px;background:var(--panel);border:1px solid var(--line-soft);
  border-radius:10px;padding:18px 18px 8px}
#railcap{color:var(--dim);font-size:12.5px;padding:0 18px 26px}
#railcap i{font-style:normal;color:var(--muted)}
#rail{display:block;width:100%;height:178px}
.lanelbl{font:600 12px Archivo,sans-serif}
.tick{font:400 11px Archivo,sans-serif;fill:var(--dim)}
#railempty{fill:var(--muted);font:400 13.5px Archivo,sans-serif}
@media (prefers-reduced-motion:no-preference){
  .dot-new{animation:pop .35s ease-out}
  @keyframes pop{0%{transform:scale(.2);opacity:0}70%{transform:scale(1.25)}100%{transform:scale(1)}}
}
.dot-new{transform-origin:center;transform-box:fill-box}

/* body grid */
.grid{display:grid;grid-template-columns:290px 1fr;gap:0 52px;margin-top:26px}
@media (max-width:920px){.grid{grid-template-columns:1fr}}
h2{font-size:13px;font-weight:600;color:var(--muted);margin:26px 0 10px;
  padding-bottom:7px;border-bottom:1px solid var(--line-soft)}
.grid h2:first-child{margin-top:0}

/* money column */
#saved{font-size:46px;font-weight:750;line-height:1.05;color:var(--saved);letter-spacing:-.5px;
  display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}
#saved small{font-size:21px;font-weight:650;color:var(--saved);opacity:.85}
#savedpct{color:var(--muted);font-size:13.5px;margin-top:4px}
.duo{margin:14px 0 4px}
.duo .row{display:flex;align-items:baseline;justify-content:space-between;
  font-size:13px;color:var(--muted);margin-top:10px}
.duo .row b{color:var(--ink);font-weight:600}
.duo .bar{height:5px;border-radius:3px;background:var(--line-soft);margin-top:5px;overflow:hidden}
.duo .bar i{display:block;height:100%;border-radius:3px}
#bar-spent i{background:var(--ink)}
#bar-base i{background:var(--dim)}
.kv{display:flex;justify-content:space-between;align-items:baseline;
  font-size:13.5px;color:var(--muted);margin-top:9px}
.kv b{color:var(--ink);font-weight:600}
.gauge{height:5px;border-radius:3px;background:var(--line-soft);margin-top:6px}
.gauge i{display:block;height:100%;border-radius:3px;background:var(--haiku)}
.models .m{display:flex;align-items:center;gap:9px;margin-top:10px;font-size:13.5px}
.models .swatch{width:9px;height:9px;border-radius:50%;flex:none}
.models .name{width:74px;color:var(--ink);font-weight:600}
.models .bar{flex:1;height:5px;border-radius:3px;background:var(--line-soft)}
.models .bar i{display:block;height:100%;border-radius:3px}
.models .n{color:var(--muted);width:118px;text-align:right}

/* ledger */
#feed{list-style:none;margin:0;padding:0}
#feed li{display:flex;gap:12px;align-items:baseline;padding:8px 0;
  border-bottom:1px solid var(--line-soft);font-size:13.5px}
#feed .t{color:var(--dim);flex:none;width:66px}
#feed .chip{flex:none;width:70px;font-weight:650}
#feed .why{color:var(--muted);flex:1;min-width:0}
#feed .why em{font-style:normal;color:var(--ink)}
#feed .fig{color:var(--dim);flex:none;text-align:right;width:150px}
#feed li.held{opacity:.62}
#empty{color:var(--muted);font-size:14px;line-height:1.7;max-width:46ch;padding:14px 0}
#empty code{color:var(--ink);font-family:ui-monospace,monospace;font-size:12.5px}
@media (max-width:700px){
  #feed li{flex-wrap:wrap}
  #feed .fig{width:auto;margin-left:auto}
  #feed .why{order:4;flex-basis:100%;margin-top:2px}
  #railcap{padding:0 4px 20px}
  #saved{font-size:38px}
}
</style></head><body>

<header>
  <div>
    <h1><span class="live" id="live"></span>Jev Router <span>— switchboard for Claude Code</span></h1>
    <div class="sub" id="sub">connecting…</div>
  </div>
  <div class="switch"><b id="swlbl">Routing on</b>
    <button class="track" id="sw" role="switch" aria-checked="true" aria-label="Routing"></button>
  </div>
</header>
<p class="counts" id="counts"></p>

<div id="railwrap">
  <svg id="rail" aria-label="Routing decisions, last 30 minutes"></svg>
</div>
<p id="railcap">The last 30 minutes of routing. <i>Filled dots</i> are steps the policy placed or moved;
<i>hollow dots</i> are steps held where they were: no event worth a recheck, or a move the cache arithmetic refused.
Higher track, more expensive model.</p>

<div class="grid">
  <div>
    <h2>Money, at configured prices</h2>
    <div id="saved" class="num">$0.0000</div>
    <div id="savedpct">saved so far, net of the classifier</div>
    <div class="duo">
      <div class="row"><span>Spent through the router</span><b class="num" id="v-spent"></b></div>
      <div class="bar" id="bar-spent"><i></i></div>
      <div class="row"><span>Same tokens pinned to <span id="topname"></span></span><b class="num" id="v-base"></b></div>
      <div class="bar" id="bar-base"><i style="width:100%"></i></div>
    </div>
    <div class="kv"><span>Jev classifier</span><b class="num" id="v-jev"></b></div>
    <div class="kv" style="margin-top:2px;font-size:12.5px"><span id="jevdetail"></span></div>

    <h2>Cache</h2>
    <div class="kv"><span>Input served from cache</span><b class="num" id="v-cache"></b></div>
    <div class="gauge"><i id="g-cache"></i></div>

    <h2>Traffic by model</h2>
    <div class="models" id="models"></div>

    <h2>Classifier latency</h2>
    <div class="kv"><span>Median</span><b class="num" id="v-p50"></b></div>
    <div class="kv"><span>95th percentile</span><b class="num" id="v-p95"></b></div>
  </div>

  <div>
    <h2>Decision ledger</h2>
    <div id="empty" hidden>No routed turns yet. Send a prompt through Claude Code and each
      decision lands here with its reasoning.<br><br>
      If the request counter climbs while this stays empty, the classifier is being
      skipped — check <code>TYPESAFE_API_KEY</code> and <code>ROUTER_SENTINEL</code>.</div>
    <ul id="feed"></ul>
  </div>
</div>

<script>
const $=id=>document.getElementById(id);
const money=v=>"$"+v.toFixed(4);
const short=m=>(m||"").replace("claude-","").replace(/-\d.*$/,"");
const TIERC={haiku:"var(--haiku)",sonnet:"var(--sonnet)",opus:"var(--opus)"};
const color=m=>TIERC[short(m)]||"var(--muted)";
const esc=t=>String(t||"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
let seen=new Set(), enabled=true, firstPaint=true;

$("sw").addEventListener("click", async ()=>{
  const r=await fetch(enabled?"/router/disable":"/router/enable",{method:"POST"});
  enabled=(await r.json()).enabled; paintSwitch();
});
function paintSwitch(){
  $("sw").setAttribute("aria-checked", enabled);
  $("swlbl").textContent = enabled ? "Routing on" : "Routing off";
}

let lastData=null;
function rail(d){
  lastData=d;
  const svg=$("rail"), W=Math.max(360,svg.clientWidth||1000);
  svg.setAttribute("viewBox","0 0 "+W+" 178");
  const now=d.now, span=1800, x0=78, x1=W-16;
  const lanes=[...d.tiers].reverse();           // expensive on top
  const laneY=i=>34+i*(110/Math.max(1,lanes.length-1));
  let s="";
  lanes.forEach((t,i)=>{
    const y=laneY(i), c=color(t);
    s+=`<line x1="${x0}" y1="${y}" x2="${x1}" y2="${y}" stroke="${c}" stroke-opacity=".22" stroke-width="1.5"/>`;
    s+=`<text x="${x0-12}" y="${y+4}" text-anchor="end" fill="${c}" class="lanelbl">${short(t)}</text>`;
  });
  for(let m=30;m>=0;m-=10){
    const x=x0+(x1-x0)*(1-m/30);
    s+=`<line x1="${x}" y1="20" x2="${x}" y2="152" stroke="var(--line-soft)"/>`;
    s+=`<text x="${x}" y="170" text-anchor="middle" class="tick">${m?("−"+m+"m"):"now"}</text>`;
  }
  const dots=d.decisions.filter(e=>now-e.ts<=span);
  if(!dots.length){
    s+=`<text x="${(x0+x1)/2}" y="95" text-anchor="middle" id="railempty">no routed turns in the last 30 minutes</text>`;
  }
  for(const e of dots){
    const x=x0+(x1-x0)*(1-(now-e.ts)/span);
    const y=laneY(lanes.indexOf(e.chosen));
    if(isNaN(y))continue;
    const held=(e.policy||"").startsWith("held");
    const key=e.ts+e.chosen;
    const fresh=!firstPaint&&!seen.has(key); seen.add(key);
    const paint=held?`fill="var(--panel)" stroke="${color(e.chosen)}" stroke-width="2"`
                    :`fill="${color(e.chosen)}"`;
    s+=`<circle cx="${x}" cy="${y}" r="5.5" ${paint} class="${fresh?"dot-new":""}">`+
       `<title>${esc((e.why||"")+"; "+(e.policy||""))}</title></circle>`;
  }
  svg.innerHTML=s;
}

async function tick(){
  if(document.hidden)return;
  let d;
  try{ d=await (await fetch("/router/metrics")).json(); $("live").classList.remove("down"); }
  catch(e){ $("live").classList.add("down"); $("sub").textContent="metrics unreachable — is the router running?"; return; }
  enabled=d.enabled; paintSwitch();
  const up=Math.floor((d.now-d.started_at)/60);
  $("sub").textContent=`forwarding to ${d.upstream.replace("https://","")}, up ${up<60?up+" min":Math.floor(up/60)+" h "+(up%60)+" min"}`;
  $("counts").innerHTML=`<b class="num">${d.requests_total}</b> requests seen — `+
    `<b class="num">${d.routed}</b> routed, <b class="num">${d.passthrough}</b> passed through untouched`;

  rail(d); firstPaint=false;

  const saved=d.baseline_top-d.spend-d.jev.cost;
  const spct=d.baseline_top?Math.round(100*saved/d.baseline_top):0;
  $("saved").innerHTML=money(saved)+"<small>"+spct+"%</small>";
  $("savedpct").textContent="upper bound vs pinning everything to "+
    short(d.tiers[d.tiers.length-1])+", net of the classifier";
  $("v-spent").textContent=money(d.spend);
  $("v-base").textContent=money(d.baseline_top);
  $("topname").textContent=short(d.tiers[d.tiers.length-1]);
  $("bar-spent").firstElementChild.style.width=(d.baseline_top?100*d.spend/d.baseline_top:0)+"%";
  $("v-jev").textContent=money(d.jev.cost);
  $("jevdetail").textContent=d.jev.calls+" calls, "+d.jev.memo_hits+" answered from memo"+
    (d.jev.errors?", "+d.jev.errors+" errors":"");

  let pre=0,rd=0;
  for(const m of Object.values(d.by_model)){pre+=m.inp+m.cache_read+m.cache_create;rd+=m.cache_read;}
  const cp=pre?Math.round(100*rd/pre):0;
  $("v-cache").textContent=cp+"%"; $("g-cache").style.width=cp+"%";

  const rows=d.tiers.map(t=>[t,d.by_model[t]]).filter(([,m])=>m)
    .concat(Object.entries(d.by_model).filter(([k])=>!d.tiers.includes(k)));
  const maxc=Math.max(1,...rows.map(([,m])=>m.count));
  $("models").innerHTML=rows.map(([n,m])=>
    `<div class="m"><span class="swatch" style="background:${color(n)}"></span>`+
    `<span class="name">${short(n)}</span>`+
    `<span class="bar"><i style="width:${100*m.count/maxc}%;background:${color(n)}"></i></span>`+
    `<span class="n num">${m.count} · ${money(m.cost)}</span></div>`).join("")
    ||`<div class="kv"><span>Nothing served yet</span></div>`;

  const lat=d.classify_ms.slice().sort((a,b)=>a-b);
  const p=q=>lat.length?lat[Math.min(lat.length-1,Math.floor(q*lat.length))]:0;
  $("v-p50").textContent=p(.5)+" ms"; $("v-p95").textContent=p(.95)+" ms";

  const dec=d.decisions.slice().reverse().slice(0,40);
  $("empty").hidden=dec.length>0;
  $("feed").innerHTML=dec.map(e=>{
    const t=new Date(e.ts*1000).toLocaleTimeString([], {hour12:false});
    const held=(e.policy||"").startsWith("held");
    const elapsed=Math.round(d.now-e.ts);
    const fig=e.drop?e.drop:
      ([e.cached_pct!=null?e.cached_pct+"% cached":null,
        e.out!=null?e.out+" out":null,
        e.cost!=null?money(e.cost):null].filter(Boolean).join(", ")
       ||("in flight, "+elapsed+"s"));
    return `<li class="${held?"held":""}"><span class="t num">${t}</span>`+
      `<span class="chip" style="color:${color(e.chosen)}">${short(e.chosen)}</span>`+
      `<span class="why"><em>${esc(e.why)}</em> — ${esc(e.policy)}`+
      (e.agent&&e.agent!=="main"?` (agent ${esc(e.agent)})`:"")+`</span>`+
      `<span class="fig num"${e.drop?' style="color:var(--opus)"':""}>${esc(fig)}</span></li>`;
  }).join("");
}
addEventListener("resize",()=>{ if(lastData) rail(lastData); });
tick(); setInterval(tick,2500);
</script></body></html>
"""


@app.get("/dashboard")
async def dashboard():
    return Response(content=DASHBOARD_HTML, media_type="text/html")


@app.get("/router/status")
async def router_status():
    return Response(
        content=json.dumps({"enabled": _control["enabled"]}),
        media_type="application/json",
    )


@app.post("/router/enable")
async def router_enable():
    _control["enabled"] = True
    log.info("routing ENABLED via control endpoint")
    return Response(content='{"enabled": true}', media_type="application/json")


@app.post("/router/disable")
async def router_disable():
    _control["enabled"] = False
    log.info("routing DISABLED via control endpoint")
    return Response(content='{"enabled": false}', media_type="application/json")


@app.post("/router/toggle")
async def router_toggle():
    _control["enabled"] = not _control["enabled"]
    log.info("routing %s via control endpoint", "ENABLED" if _control["enabled"] else "DISABLED")
    return Response(
        content=json.dumps({"enabled": _control["enabled"]}),
        media_type="application/json",
    )


@app.get("/router/metrics")
async def router_metrics():
    return Response(
        content=json.dumps(_metrics_snapshot()),
        media_type="application/json",
        headers={"cache-control": "no-store"},
    )


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
)
async def passthrough(request: Request, path: str):
    """
    Everything else goes straight upstream, unexamined.

    Notably this covers:
      - /v1/models, for gateway model discovery. Claude Code gives it a 3s
        budget and treats any redirect as failure, so serve it at the base URL.
      - /v1/messages/count_tokens, which is optional. Note the model field in
        that body is NOT rewritten, so counts reflect the model Claude Code
        thinks it is using. Close enough for a context gauge.
      - HEAD /api/hello, a connection-warming probe. Harmless to reject, but
        cheaper to forward than to 405.
    """
    body = await request.body()
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.request(
            request.method,
            f"{UPSTREAM}/{path}",
            content=body,
            headers=forward_headers(request),
            params=request.query_params,
        )
    return Response(
        content=r.content,
        status_code=r.status_code,
        headers=response_headers(r.headers, decoded=True),
    )


# --------------------------------------------------------------------------
# ROUTING NOTES
#
# 1. Prompt caching is the whole economics of this.
#    Claude Code caches the system prompt and conversation prefix, and each
#    model has its own cache. Switching models mid-conversation means the new
#    model re-reads the entire prefix at full input price. On a long session
#    that prefix is the overwhelming majority of your tokens, so a naive
#    per-turn router can cost several times what pinning Opus costs. The
#    switch decisions are therefore priced, not counted: switch_delta() is
#    the single formula (used at human turns and mid-run alike), and
#    DOWNGRADE_PATIENCE only remains as the fallback when no usage telemetry
#    has been observed yet. Measure with /cost regardless.
#
# 2. What the work wants and what the cache allows are separate layers.
#    jev_ontology.pick_tier_v2 answers the first from Jev's judgment plus the
#    measured ledger: tier = cognitive demand x cost of an undetected error.
#    apply_cache_policy / step_policy answer the second. Upgrades are always
#    immediate. Safety picks (irreversible action, risk surface with no check
#    in the loop, thrashing, the user correcting the agent) also set a FLOOR
#    that no mid-run downgrade may cross. The floor is re-derived at every
#    recheck and lifts when its cause does; only a user correction holds it
#    for the whole work unit.
#
# 2b. A tier is a price per token, not a cost per task.
#    Artificial Analysis found Sonnet 5 at max effort costing ~15% MORE per
#    task than Opus 4.8 despite $3/$15 vs $5/$25, entirely from token volume,
#    with max effort taking ~6x the agentic turns of low. Read that carefully:
#    it is max effort (Claude Code defaults Sonnet 5 to high), it is against
#    Opus 4.8 rather than Opus 5, and it is their task mix, not yours. What
#    transfers is the structure: the ladder in ROUTER_TIERS is ordered by
#    list price, and nothing guarantees it is ordered by what a finished unit
#    of work costs. Consequences here:
#      - switch_delta prices each side with its own expected output volume
#        (learned per model, see /router/metrics "out_rate") and an optional
#        turn multiplier. If the "cheaper" model talks enough more, the
#        saving goes negative and the router stays put.
#      - the learned ratio is confounded, because tiers are handed different
#        work. It is clamped and treated as a correction. The clean number
#        comes from traces: same phase, same demand, different tier.
#      - effort is the cheaper lever. With ROUTER_EFFORT_STEERING=1 and a
#        model that supports per-message effort (Opus 5 today), rote and
#        routine steps run at low/medium effort on the SAME model with the
#        cache intact. Top-level effort changes are never made: they
#        invalidate the cache like a model switch.
#      - if your traces show the middle tier costing more per finished unit
#        than the top one, delete it from ROUTER_TIERS. A two-rung ladder
#        (haiku for rote loops, opus with effort steering for the rest) is a
#        legitimate outcome of measuring.
#
# 3. Rechecks are event-driven.
#    A tool-results-only turn is held without a Jev call unless the ledger
#    shows something happened: the phase shifted, a check failed twice, the
#    same failure came back a third time, a new risk surface was touched, a
#    skill loaded, or five edits piled up unchecked. RECHECK_EVERY is only
#    the fallback cadence. A recheck costs ~500 input tokens to Jev, a few
#    thousandths of a cent; the real cost is the ~100ms it adds to that turn.
#
# 4. Latency is additive but small.
#    Jev returns in roughly 70-500ms. Against a coding turn that runs tens of
#    seconds this is noise, but it lands on every human turn and every
#    recheck. Ledger extraction is a linear walk over the request's messages
#    and costs well under a millisecond per hundred messages.
#
# 5. The classifier failing should never fail the turn.
#    classify() swallows everything and returns None. A session that already
#    has a tier keeps it (bouncing to the requested model would break the
#    cache for nothing); a session without one forwards as requested. Ledger
#    extraction is wrapped the same way. Keep both that way.
#
# 6. Subagents are the cheapest place to route.
#    Subagent calls are short-lived and cache-cold, so switching models there
#    costs nothing in cache terms. They hold their own state (session/agent
#    key), their role is sniffed from `system`, and explore-type subagents are
#    capped at the middle tier: searching is cheap cognition, and only the
#    final result returns to the parent.
#
# 7. Jev reads text and structured state, not images.
#    Turns carrying screenshots are judged on their text and ledger alone. If
#    your workflow is screenshot-heavy, add a check for image blocks and pin
#    those upward.
#
# 8. The router observes; it does not gate.
#    A `git push`, a `terraform apply`, an MCP write: all appear in the ledger
#    only AFTER they ran. The `irreversible` question predicts the next step;
#    it cannot stop the last one. Gating belongs in a Claude Code PreToolUse
#    hook or permissions.deny, not in a model router.
#
# 9. Calibrate, do not trust.
#    Every threshold in pick_tier_v2 is a prior. With ROUTER_TRACE_DIR set,
#    each decision is written with outcome labels (last check result, failure
#    streak, repeated-failure count, user_correcting, trajectory). Join
#    decisions to the labels on the FOLLOWING decisions of the same session
#    and you get P(check passes | tier, phase, demand) for your repo -- and a
#    truer savings figure than the dashboard's, which prices a weaker model's
#    extra turns onto the baseline and is therefore an upper bound.
#
# 10. Subscription (Pro/Max/Team) logins work, and this is a documented path.
#    Set ANTHROPIC_BASE_URL and DO NOT set a gateway credential variable. Your
#    saved claude.ai login stays the active credential, and its usage limits
#    and billing apply. Setting a gateway credential instead replaces the
#    subscription entirely and bills per token to whoever owns that credential.
#    Two consequences specific to subscriptions:
#      - anthropic-beta carries an OAuth capability on these requests. Strip it
#        and every request fails 401. forward_headers() passes it verbatim.
#      - You are not saving dollars, you are conserving usage limit. Routing
#        easy steps to Haiku stretches how far your plan goes rather than
#        reducing a bill. Judge it on how often you hit limits, not on /cost.
#    Behind a gateway Claude Code stops checking plan requirements, so it will
#    happily send a model your plan can't serve and let the upstream reject it.
#    Keep ROUTER_TIERS to models your plan actually has.
#
# 11. Protocol compliance: what this does and doesn't do.
#      - anthropic-beta and anthropic-version forwarded verbatim, never
#        allowlisted by value (the capability set grows every release)
#      - the credential arrives and leaves untouched
#      - the ?beta=true query string survives
#      - the system array is passed through unchanged, so the attribution block
#        stays first and in its own entry and api.anthropic.com still strips it
#      - cache_control markers pass through wherever they appear
#      - responses stream with aiter_raw, so SSE pings and comment lines reach
#        Claude Code's 300-second byte watchdog unbuffered
#      - upstream status codes and error bodies relay unmodified on both the
#        streaming and non-streaming paths
#      - HEAD /api/hello and /v1/messages/count_tokens pass through
#    Known gaps, all deliberate:
#      - count_tokens is forwarded without rewriting its model field, so
#        /context counts reflect the model Claude Code thinks it is using
#      - only the Anthropic Messages format is served; the Bedrock and Agent
#        Platform formats are not
#      - the request body is buffered to classify it. Claude Code sends
#        complete bodies, so this costs nothing, but it is not a pure relay
#      - fast mode's availability check and the WebFetch domain safety check
#        call api.anthropic.com directly and never reach this proxy
# --------------------------------------------------------------------------
