"""A Laya-shaped projection of the router's questions and state.

WHY THIS EXISTS

The first Laya arm asked Laya the router's own 15 questions verbatim and it
answered badly in a specific, structured way (bench/calibrate_classifier.py):

  - `phase` collapsed onto `verify` for three of four items, including a
    read-only exploration and a three-failure debug loop.
  - every `noul` in the set came back between 0.65 and 0.99, so against a 0.5
    cut everything read true, which in this ontology means hard-or-risky,
    which means escalate. The `todo` run routed 100% to Opus.
  - ablating truncation direction and raising max_len to 1024 and 2048 left
    the noul probabilities identical to two decimals. The head was barely
    conditioning on the state at all.

That last point is the clue. Laya's own bundled router preset
(laya/presets.py:154) does not ask abstract questions over a wall of JSON. It
asks "How hard is `request` for a language model?" -- a short instruction that
names the exact field of the state it is about, over a small structured
object. Jev's ontology asks "The software-lifecycle phase of the agent's NEXT
step" over a 676-token situation report and never tells the model where to
look.

So the hypothesis this module tests is narrow and falsifiable:

    Laya's failure on router states is a prompt-and-state-shape mismatch,
    not an inability to make the judgment.

If it is right, re-projecting the same decision into Laya's idiom should move
accuracy without changing the model, the router, or the ontology's meaning. If
it is wrong, accuracy stays near chance and the honest conclusion is that this
checkpoint cannot drive this router without fine-tuning.

WHAT IS HELD FIXED

The answer contract is untouched, because the router's policy engine consumes
it and must keep working:

  - the same 15 question ids
  - the same types
  - the same option KEYS for every choice (pick_tier_v2 compares `phase`
    against PHASES by name, so those strings cannot move)
  - the same score arity, so the ordinal index still maps to the same levels

Only three things change, and all three are presentation:

  1. instructions are rewritten to name the state field they are about,
     in Laya's `backtick` idiom
  2. option descriptions are rewritten as concrete observable evidence
     ("only greps and file reads, nothing written yet") rather than abstract
     definitions ("Locating and reading")
  3. the state is projected into a small flat object whose keys are the ones
     the instructions name, and which fits the 512-token budget

This is not a thumb on the scale. It gives Laya the same information in the
form its own documented interface asks for. A fair test of a model is one
where the interface is not itself the obstacle. The comparison it supports is
"can Laya drive this router", not "can Laya answer Jev's exact prompts",
which the first arm already answered.

NOTHING HERE TOUCHES THE ROUTER. jev_router.py and jev_ontology.py are not
imported for anything but their question schema, are not modified, and the
shim only loads this module when explicitly asked for --ontology native.
"""

import json

# Option keys are load-bearing: pick_tier_v2 compares against these strings.
# Only the descriptions differ from jev_ontology.PHASES.
PHASE_EVIDENCE = {
    "clarify": "the agent still needs to ask the user what is wanted; the request is ambiguous",
    "explore": "only searching and reading so far: greps, file reads, symbol lookups, nothing written",
    "plan": "choosing an approach or breaking the work up, before any code is written",
    "implement": "writing or changing code, with the approach already settled",
    "debug": "a check has failed and the cause is not yet known; `last_check` is failed",
    "verify": "running tests or builds and reading the result, with nothing currently broken",
    "review": "judging existing code or a diff and writing findings, not changing it",
    "integrate": "committing, opening a PR, merging, releasing",
    "housekeeping": "renaming, formatting, moving files, dependency bumps, chores",
}

TRAJECTORY_EVIDENCE = {
    "progressing": "`last_check` passed, or each failure is a new and different one",
    "slow_convergence": "failures keep changing and shrinking, but over many rounds",
    "thrashing": "`same_failure_repeats` is 2 or more: the identical failure keeps returning",
    "drifting": "the work has wandered away from `request`",
    "blocked_on_human": "the agent cannot continue without an answer from the user",
    "just_started": "`steps_so_far` is 0 or 1; too early to tell",
}

REVIEW_EVIDENCE = {
    "not_review": "the next step is not a review at all",
    "conformance": "checking style, naming, formatting or convention conformance",
    "correctness": "checking whether the logic is right: edge cases, error handling, tests",
    "security_or_design": "checking for vulnerabilities, concurrency hazards or architectural fit",
}


def _q(t, instructions, criteria=None):
    q = {"type": t, "instructions": instructions}
    if criteria is not None:
        q["criteria"] = criteria
    return q


# Every instruction names the field of the projected state it is about. The
# noul instructions in particular are written as a concrete observable test,
# because the abstract phrasings are the ones that produced the flat 0.65-0.99
# band that ignored the input.
QUESTIONS_NATIVE = {
    "phase": _q("choice",
                "What is the coding agent about to do next? Use `phase_hint`, "
                "`tools_used`, `last_check` and `recent_steps`.",
                PHASE_EVIDENCE),
    "next_step_demand": _q("score",
                           "How much judgment does the agent's next step need, "
                           "given `request` and `recent_steps`?",
                           ["rote: the exact change is already spelled out in `request`",
                            "routine: ordinary skilled work with a known approach",
                            "hard: competing hypotheses, or the approach itself is unclear"]),
    "spec_completeness": _q("score",
                            "How much of the approach is already decided in `request` and `todos`?",
                            ["governed: an explicit plan or exact instruction covers the next step",
                             "the goal is clear but the method is open",
                             "outcome only: the path is left entirely to the agent"]),
    "cross_cutting": _q("noul",
                        "Does `files_edited` span several different modules that must be "
                        "kept consistent with each other? Answer no if only one file, or "
                        "one module, is involved."),
    "repo_specific": _q("noul",
                        "Does the next step depend on conventions particular to this "
                        "codebase, rather than general programming knowledge?"),
    "canonical": _q("noul",
                    "Is `request` asking for a well-known standard artifact, such as a "
                    "todo app, a CRUD form or a sorting routine, that has an obvious "
                    "conventional shape?"),
    "oracle_in_loop": _q("noul",
                         "Is there an automatic check in the loop that will catch a "
                         "mistake? Answer yes only if `checks_run` is 1 or more."),
    "risk_surface": _q("noul",
                       "Is `risk_flags` non-empty, or does `destructive_commands` contain "
                       "anything? Answer no when both are empty."),
    "irreversible": _q("noul",
                       "Would the next step be hard to undo: a push, a deploy, a migration, "
                       "a delete, or an outward message? Answer no for ordinary local edits."),
    "trajectory": _q("choice",
                     "How is the run going? Use `last_check`, `same_failure_repeats` "
                     "and `consecutive_failures`.",
                     TRAJECTORY_EVIDENCE),
    "unverified_confidence": _q("noul",
                                "Did the agent claim in `agent_said` that something works, "
                                "without a passing check in `last_check` to back it up?"),
    "user_correcting": _q("noul",
                          "Is the user in `request` telling the agent that what it just did "
                          "was wrong, or asking it to undo or redo something? Answer no for "
                          "a plain new request."),
    "review_depth": _q("choice",
                       "If the next step is a review, what kind is it? Use `request`.",
                       REVIEW_EVIDENCE),
    "gen_volume": _q("score",
                     "How much text or code will the next step produce?",
                     ["a few lines or a short answer",
                      "a normal file or function",
                      "a large file or several files at once"]),
    "is_followup": _q("noul",
                      "Is `request` a follow-up that continues earlier work in this same "
                      "session, rather than the first thing asked?"),
}

# Questions the router drops on a mid-run recheck. Mirrors
# jev_ontology._HUMAN_ONLY so a recheck asks the same subset.
HUMAN_ONLY = {"user_correcting", "is_followup", "spec_completeness"}
STEP_QUESTIONS_NATIVE = {k: v for k, v in QUESTIONS_NATIVE.items()
                         if k not in HUMAN_ONLY}


def parse_state(state):
    """Recover the ledger dict from what build_state_v2 emitted.

    build_state_v2 writes one prose line then a JSON object. Nothing in the
    router is changed to make this readable; the shim simply reads what it is
    already being sent, and falls back to the raw string if the shape is not
    what is expected.
    """
    if isinstance(state, dict):
        return state
    if not isinstance(state, str):
        return None
    i = state.find("{")
    if i < 0:
        return None
    try:
        return json.loads(state[i:])
    except Exception:
        return None


def project(state):
    """Flatten the router's situation report into the small named object the
    native questions refer to.

    Returns the original state unchanged if it cannot be parsed, so a shape
    change in the router degrades to the verbatim path rather than to
    nonsense.
    """
    d = parse_state(state)
    if not d:
        return state

    unit = d.get("unit") or {}
    act = d.get("activity") or {}
    ver = d.get("verification") or {}
    risk = d.get("risk") or {}
    steps = d.get("recent_steps") or []

    files = list((act.get("files_edited") or {}).keys())
    # Module = first path segment. Enough to tell one-file work from a change
    # that spans the tree, which is what cross_cutting is asking.
    modules = sorted({f.split("/")[0] for f in files if "/" in f} |
                     {f for f in files if "/" not in f})

    out = {
        "request": unit.get("latest_human_ask") or unit.get("opening_request") or "",
        "agent_said": unit.get("agent_last_remark") or "",
        "is_first_human_turn": (unit.get("human_turns") or 0) <= 1,
        "steps_so_far": unit.get("steps_in_unit") or 0,
        "todos": unit.get("todos"),
        "phase_hint": act.get("phase_hint_from_tools"),
        "tools_used": act.get("tool_kinds") or {},
        "files_edited": files,
        "modules_touched": modules,
        "lines_changed": act.get("lines_changed_est") or 0,
        "checks_run": ver.get("check_runs") or 0,
        "last_check": ver.get("last_check"),
        "consecutive_failures": ver.get("consecutive_failures") or 0,
        "same_failure_repeats": ver.get("same_failure_repeats") or 0,
        "risk_flags": risk.get("risk_surface_hits") or [],
        "destructive_commands": risk.get("outward_or_destructive_commands") or [],
        # The tail carries the most decision-relevant signal and is also what
        # right-truncation would have thrown away. Three is what fits.
        "recent_steps": steps[-3:],
    }
    return out


def translate(questions):
    """Map the router's question set onto the native one, preserving ids.

    Unknown ids pass through untouched, so a question added to the router's
    ontology later still gets asked rather than silently dropped.
    """
    return {k: QUESTIONS_NATIVE.get(k, v) for k, v in questions.items()}
