#!/usr/bin/env python3
"""Calibration and integration validation for a classifier behind the router.

docs/CALIBRATION.md asks of Jev: does the probability attached to an answer
predict whether that answer is right? This asks the same of Laya, and asks
first whether the Laya integration is faithful at all, because a broken
adapter and a poorly fitted model look identical from the outside -- both
produce wrong answers at low confidence.

Three layers, cheapest first. Each has to pass before the next means anything.

  A. FIDELITY     Does bench/laya_shim.py return what an in-process laya call
                  returns? Same item, both paths, compared field by field.
                  Isolates my HTTP/parsing code from the model. If this fails,
                  nothing else here is about Laya.

  B. SANITY       Can the model answer items whose correct answer is not in
                  doubt, using laya's own bundled question presets? Laya
                  publishes 0.9912 accuracy on its "intent and routing"
                  family. A model that cannot clear a wide margin on
                  unambiguous items of its own kind is being mishandled, not
                  merely out of distribution.

  C. CALIBRATION  On real router states built by jev_ontology, with ground
                  truth chosen so that a competent reader of the state cannot
                  reasonably disagree: accuracy, Brier, ECE, a threshold
                  sweep, and run-to-run stability. Both engines, same items.

Layer C is the one that decides whether a classifier can drive this router.
Layers A and B exist so that a bad C result can be attributed.

    python3 bench/calibrate_classifier.py --shim-port 8781
    python3 bench/calibrate_classifier.py --shim-port 8781 --engines laya
"""

import argparse
import json
import math
import os
import statistics
import sys
import time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import jev_ontology as O  # noqa: E402

TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"


def load_key():
    p = os.path.join(REPO, ".env")
    if os.environ.get("TYPESAFE_API_KEY"):
        return os.environ["TYPESAFE_API_KEY"]
    if os.path.exists(p):
        for line in open(p):
            if line.startswith("TYPESAFE_API_KEY"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def post(url, body, headers, timeout=60):
    r = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
    t0 = time.time()
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.loads(resp.read()), (time.time() - t0) * 1000.0


def ask_laya(port, state, questions):
    d, ms = post(f"http://127.0.0.1:{port}/v1/systemone",
                 {"state": state, "model": "laya", "questions": questions},
                 {"Content-Type": "application/json"})
    return d["answers"], ms


def ask_jev(key, state, questions):
    d, ms = post(TYPESAFE_URL,
                 {"state": state, "model": "jev-latest", "questions": questions},
                 {"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    return d["answers"], ms


# ---------------------------------------------------------------------------
# items: real router states with ground truth that is not a matter of opinion
# ---------------------------------------------------------------------------

def tu(i, name, **inp):
    return {"type": "tool_use", "id": i, "name": name, "input": inp}


def tr(i, text, err=False):
    return {"type": "tool_result", "tool_use_id": i, "content": text, "is_error": err}


def body(msgs, tools=("Bash", "Read", "Edit", "Write", "Grep")):
    return {"tools": [{"name": n} for n in tools],
            "thinking": {"type": "enabled", "budget_tokens": 8000},
            "messages": msgs}


def user(t):
    return {"role": "user", "content": [{"type": "text", "text": t}]}


FAIL = ("FAILED tests/test_refund.py::test_partial - AssertionError: 1200 != 1250\n"
        "1 failed, 41 passed")
PASS = "41 passed in 2.11s"


def items():
    """Labelled router states.

    Ground truth is only asserted where the state makes the answer plain. A
    run that has only searched and read, with no edit, is exploring. A run
    whose identical test failure has repeated three times is thrashing. Items
    where a thoughtful reader could argue for a second label are not scored,
    because a disagreement there measures the rubric rather than the model.
    """
    I = []

    def add(name, msgs, mid_run, truth, sess=None):
        led = O.extract_ledger(body(msgs))
        state = O.build_state_v2(led, sess or {"last_model": "claude-sonnet-5",
                                               "author_tier": 1}, mid_run=mid_run)
        I.append({"name": name, "state": state, "truth": truth, "ledger": led})

    # --- phase: explore. Only searching and reading, nothing written. ---
    add("explore/search-only", [
        user("where does the billing code handle proration?"),
        {"role": "assistant", "content": [tu("1", "Grep", pattern="proration")]},
        {"role": "user", "content": [tr("1", "billing/proration.py:14\nbilling/invoice.py:88")]},
        {"role": "assistant", "content": [tu("2", "Read", file_path="billing/proration.py")]},
        {"role": "user", "content": [tr("2", "def prorate(...): ...")]},
        {"role": "assistant", "content": [tu("3", "Read", file_path="billing/invoice.py")]},
        {"role": "user", "content": [tr("3", "def invoice(...): ...")]},
    ], True, {"phase": "explore"})

    # --- phase: debug + trajectory: thrashing. Same failure three times. ---
    add("debug/thrash-x3", [
        user("partial refunds are off by the tax amount, fix it"),
        {"role": "assistant", "content": [tu("1", "Edit", file_path="refund.py",
                                             old_string="a", new_string="b")]},
        {"role": "user", "content": [tr("1", "ok")]},
        {"role": "assistant", "content": [tu("2", "Bash", command="pytest -q")]},
        {"role": "user", "content": [tr("2", FAIL)]},
        {"role": "assistant", "content": [tu("3", "Edit", file_path="refund.py",
                                             old_string="b", new_string="c")]},
        {"role": "user", "content": [tr("3", "ok")]},
        {"role": "assistant", "content": [tu("4", "Bash", command="pytest -q")]},
        {"role": "user", "content": [tr("4", FAIL)]},
        {"role": "assistant", "content": [tu("5", "Edit", file_path="refund.py",
                                             old_string="c", new_string="d")]},
        {"role": "user", "content": [tr("5", "ok")]},
        {"role": "assistant", "content": [tu("6", "Bash", command="pytest -q")]},
        {"role": "user", "content": [tr("6", FAIL)]},
    ], True, {"phase": "debug", "trajectory": "thrashing"})

    # --- trajectory: progressing. Edits then a green suite. ---
    add("verify/green-after-edit", [
        user("add a discount field to the invoice model"),
        {"role": "assistant", "content": [tu("1", "Edit", file_path="billing/invoice.py",
                                             old_string="x", new_string="y")]},
        {"role": "user", "content": [tr("1", "ok")]},
        {"role": "assistant", "content": [tu("2", "Bash", command="pytest -q")]},
        {"role": "user", "content": [tr("2", PASS)]},
    ], True, {"trajectory": "progressing"})

    # --- phase: implement. A plain build request, no tools run yet. ---
    add("implement/fresh-build", [
        user("write a single-file HTML todo app with add, complete and delete"),
    ], False, {"phase": "implement"})

    # --- phase: clarify. A question, not an instruction. ---
    add("clarify/question", [
        user("what's the difference between the two refund paths in this repo?"),
    ], False, {"phase": "explore"})

    # --- user_correcting: the human says it is wrong. ---
    add("correcting/explicit", [
        user("add a discount field"),
        {"role": "assistant", "content": [tu("1", "Edit", file_path="m.py",
                                             old_string="x", new_string="y")]},
        {"role": "user", "content": [tr("1", "ok")]},
        user("no, that's wrong, you edited the wrong file, revert it"),
    ], False, {"user_correcting": True}),

    # --- user_correcting: false. Plain forward request. ---
    add("correcting/none", [
        user("write a single-file HTML todo app with add, complete and delete"),
    ], False, {"user_correcting": False})

    # --- risk_surface: true. Payments plus destructive command. ---
    add("risk/payments-drop", [
        user("clean up the old payment rows"),
        {"role": "assistant", "content": [tu("1", "Bash",
                                             command="psql -c 'DROP TABLE payments_old'")]},
        {"role": "user", "content": [tr("1", "DROP TABLE")]},
    ], True, {"risk_surface": True})

    # --- risk_surface: false. Docs typo. ---
    add("risk/readme-typo", [
        user("fix the typo in the README heading"),
        {"role": "assistant", "content": [tu("1", "Edit", file_path="README.md",
                                             old_string="teh", new_string="the")]},
        {"role": "user", "content": [tr("1", "ok")]},
    ], True, {"risk_surface": False})

    # --- cross_cutting: false. One tiny edit in one file. ---
    add("cross/single-typo", [
        user("fix the typo in the README heading"),
        {"role": "assistant", "content": [tu("1", "Edit", file_path="README.md",
                                             old_string="teh", new_string="the")]},
        {"role": "user", "content": [tr("1", "ok")]},
    ], True, {"cross_cutting": False})

    # --- cross_cutting: true. Rename touching many modules. ---
    add("cross/rename-many", [
        user("rename the User.email field to contact_email everywhere and keep "
             "the API, the serializers and the migrations consistent"),
        {"role": "assistant", "content": [tu("1", "Edit", file_path="models/user.py",
                                             old_string="email", new_string="contact_email")]},
        {"role": "user", "content": [tr("1", "ok")]},
        {"role": "assistant", "content": [tu("2", "Edit", file_path="api/serializers.py",
                                             old_string="email", new_string="contact_email")]},
        {"role": "user", "content": [tr("2", "ok")]},
        {"role": "assistant", "content": [tu("3", "Edit", file_path="migrations/0009.py",
                                             old_string="email", new_string="contact_email")]},
        {"role": "user", "content": [tr("3", "ok")]},
    ], True, {"cross_cutting": True})

    # --- next_step_demand: low. Mechanical, fully specified. ---
    add("demand/rote", [
        user("in README.md change the word 'teh' to 'the' on line 3, nothing else"),
    ], False, {"next_step_demand": "low"})

    # --- next_step_demand: high. Open design under contention. ---
    add("demand/hard", [
        user("our checkout deadlocks under load about once a day and we cannot "
             "reproduce it locally; work out what is happening and propose a fix"),
    ], False, {"next_step_demand": "high"})

    return I


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------

def graded(answers, truth):
    """Yield (question, correct: bool, confidence, detail) per labelled field."""
    out = []
    for q, want in truth.items():
        a = answers.get(q) or {}
        qt = (O.QUESTIONS_V2.get(q) or {}).get("type")
        if qt == "choice":
            got = a.get("choice")
            conf = float(a.get("confidence", 0.0))
            out.append((q, got == want, conf, f"{got} (want {want})"))
        elif qt == "noul":
            p = float(a.get("noul", 0.5))
            got = p >= 0.5
            # A noul's own probability is its confidence in the side it picked.
            conf = p if got else 1.0 - p
            out.append((q, got == bool(want), conf,
                        f"{p:.2f} -> {got} (want {want})"))
        elif qt == "score":
            s = float(a.get("score", 1.0))
            k = len(O.QUESTIONS_V2[q]["criteria"])
            got = "low" if s < (k - 1) / 2.0 - 0.25 else (
                "high" if s > (k - 1) / 2.0 + 0.25 else "mid")
            conf = float(a.get("confidence", 0.0))
            out.append((q, got == want, conf, f"{s:.2f} -> {got} (want {want})"))
    return out


def ece(rows, bins=5):
    """Expected calibration error over |confidence - accuracy| per bin."""
    if not rows:
        return None
    e, n = 0.0, len(rows)
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        sel = [r for r in rows if (lo <= r[2] < hi) or (b == bins - 1 and r[2] == 1.0)]
        if sel:
            acc = sum(1 for r in sel if r[1]) / len(sel)
            cf = sum(r[2] for r in sel) / len(sel)
            e += (len(sel) / n) * abs(cf - acc)
    return e


def brier(rows):
    return statistics.mean((r[2] - (1.0 if r[1] else 0.0)) ** 2 for r in rows) if rows else None


def sweep(rows, cuts=(0.9, 0.8, 0.7, 0.6, 0.45)):
    out = []
    for c in cuts:
        auto = [r for r in rows if r[2] >= c]
        wrong = sum(1 for r in auto if not r[1])
        out.append((c, len(auto), len(rows) - len(auto), wrong))
    return out


# ---------------------------------------------------------------------------
# layers
# ---------------------------------------------------------------------------

def layer_a(port, its):
    """Shim output must equal an in-process laya call, modulo the documented
    confidence remap. Anything else is a bug in laya_shim.py."""
    print("\n" + "=" * 78)
    print("LAYER A  shim fidelity: bench/laya_shim.py vs in-process laya")
    print("=" * 78)
    try:
        import laya
        import laya.agent as la
        from laya.common import build_sequence as _bs

        def left(tok, state, q, max_len=512, head_max_len=192,
                 option_order=None, truncate_left=False):
            return _bs(tok, state, q, max_len, head_max_len, option_order, True)
        la.build_sequence = left
        ag = laya.load("convaiinnovations/laya")
    except Exception as e:
        print(f"  SKIP: laya not importable in this interpreter ({e})")
        print("  Run this script with the LAYA_PYTHON interpreter to check layer A.")
        return None

    mismatches, checked = [], 0
    for it in its[:6]:
        direct = ag.predict(it["state"], O.QUESTIONS_V2)["answers"]
        viashim, _ = ask_laya(port, it["state"], O.QUESTIONS_V2)
        for q, d in direct.items():
            s = viashim.get(q) or {}
            t = d.get("type")
            dv, sv = d.get(t), s.get(t)
            checked += 1
            same = (dv == sv if t == "choice"
                    else abs(float(dv) - float(sv)) < 1e-6)
            if not same:
                mismatches.append(f"{it['name']}/{q}: direct={dv} shim={sv}")
            # confidence must be the documented remap, not the raw entropy
            probs = d.get("probabilities")
            if probs:
                want = round(max(float(v) for v in probs.values()), 4)
                if abs(float(s.get("confidence", -1)) - want) > 1e-6:
                    mismatches.append(
                        f"{it['name']}/{q}: confidence {s.get('confidence')} "
                        f"!= max(p) {want}")
    print(f"  compared {checked} answers over {min(6, len(its))} states")
    if mismatches:
        print(f"  FAIL: {len(mismatches)} mismatches")
        for m in mismatches[:10]:
            print(f"    {m}")
        return False
    print("  PASS: shim answers are identical to in-process, confidence is max(p)")
    return True


def layer_b(port):
    """Laya's own presets on items whose answer is not in doubt."""
    print("\n" + "=" * 78)
    print("LAYER B  sanity: laya's own question presets on unambiguous items")
    print("=" * 78)
    qs = {
        "category": {
            "type": "choice",
            "instructions": "Which queue owns this ticket?",
            "criteria": {
                "billing": "invoices, refunds, charges, payment methods",
                "infrastructure": "outages, latency, servers, databases",
                "security": "breaches, vulnerabilities, leaked credentials",
            },
        },
        "urgent": {"type": "noul",
                   "instructions": "Is the customer reporting a total, ongoing outage?"},
    }
    cases = [
        ("I was charged twice for my invoice this month, please refund one.",
         "billing", False),
        ("Our entire production cluster is down and every request 500s right now.",
         "infrastructure", True),
        ("We found an API key committed in your public repo, it is still valid.",
         "security", False),
        ("The refund you issued went to the wrong card, can you redo it?",
         "billing", False),
        ("Database replica lag has been climbing for an hour and queries time out.",
         "infrastructure", False),
        ("Someone accessed my account from another country and changed my password.",
         "security", False),
    ]
    ok = 0
    rows = []
    for text, want_cat, want_urgent in cases:
        a, _ = ask_laya(port, text, qs)
        got = a["category"]["choice"]
        conf = a["category"]["confidence"]
        u = a["urgent"]["noul"] >= 0.5
        good = got == want_cat
        ok += good
        rows.append((None, good, float(conf), None))
        print(f"  {'ok ' if good else 'BAD'} {got:16s} p={conf:.2f} "
              f"urgent={u!s:5s}(want {want_urgent}) :: {text[:52]}")
    acc = ok / len(cases)
    print(f"\n  accuracy {ok}/{len(cases)} = {acc:.0%}  (laya publishes 0.9912 "
          f"on its intent-and-routing family)")
    if acc < 0.8:
        print("  FAIL: cannot clear unambiguous items of its own kind -- "
              "suspect the integration, not the fit")
        return False
    print("  PASS: the model answers its own kind of question correctly through the shim")
    return True


def layer_c(port, key, engines, its, reps):
    print("\n" + "=" * 78)
    print("LAYER C  calibration on router states (jev_ontology ground truth)")
    print("=" * 78)
    results = {}
    for eng in engines:
        rows, lat, per_item = [], [], []
        for it in its:
            try:
                if eng == "laya":
                    a, ms = ask_laya(port, it["state"], O.QUESTIONS_V2)
                else:
                    a, ms = ask_jev(key, it["state"], O.QUESTIONS_V2)
            except Exception as e:
                print(f"  {eng} failed on {it['name']}: {e}")
                continue
            lat.append(ms)
            g = graded(a, it["truth"])
            rows.extend(g)
            per_item.append((it["name"], g))
        results[eng] = {"rows": rows, "lat": lat, "per_item": per_item}

    for eng in engines:
        r = results[eng]["rows"]
        if not r:
            continue
        acc = sum(1 for x in r if x[1]) / len(r)
        print(f"\n--- {eng}: {sum(1 for x in r if x[1])}/{len(r)} correct "
              f"({acc:.0%}), Brier {brier(r):.4f}, ECE {ece(r):.4f}, "
              f"median {statistics.median(results[eng]['lat']):.0f} ms")
        print(f"    {'item':26s} {'question':20s} {'result':34s}")
        for name, g in results[eng]["per_item"]:
            for q, good, conf, detail in g:
                print(f"    {name:26s} {q:20s} {'ok ' if good else 'BAD'} "
                      f"{detail:28s} p={conf:.2f}")

    print(f"\n{'threshold sweep':-^78}")
    print(f"  {'engine':6s} {'cutoff':>7s} {'automatic':>10s} {'to fallback':>12s} {'wrong shipped':>14s}")
    for eng in engines:
        for c, auto, held, wrong in sweep(results[eng]["rows"]):
            print(f"  {eng:6s} {c:7.2f} {auto:10d} {held:12d} {wrong:14d}")

    # Stability: the same bytes, several times.
    if reps > 1:
        print(f"\n{'stability: identical request x' + str(reps):-^78}")
        it = its[1]  # the thrashing item
        for eng in engines:
            vals = []
            for _ in range(reps):
                try:
                    a, _ = (ask_laya(port, it["state"], O.QUESTIONS_V2) if eng == "laya"
                            else ask_jev(key, it["state"], O.QUESTIONS_V2))
                    vals.append(float((a.get("phase") or {}).get("confidence", 0.0)))
                except Exception:
                    pass
            if len(vals) > 1:
                print(f"  {eng:6s} phase confidence sd {statistics.pstdev(vals):.4f} "
                      f"over {len(vals)} identical calls "
                      f"(min {min(vals):.3f}, max {max(vals):.3f})")
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shim-port", type=int, default=8781)
    ap.add_argument("--engines", default="jev,laya")
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--layers", default="abc")
    a = ap.parse_args()

    engines = [s.strip() for s in a.engines.split(",") if s.strip()]
    key = load_key()
    if "jev" in engines and not key:
        sys.exit("no TYPESAFE_API_KEY for the jev engine")
    its = items()
    print(f"{len(its)} labelled router states, "
          f"{sum(len(i['truth']) for i in its)} labelled answers")

    if "a" in a.layers:
        if layer_a(a.shim_port, its) is False:
            sys.exit("\nLayer A failed: the shim does not faithfully relay laya. "
                     "Fix that before reading any calibration number.")
    if "b" in a.layers:
        if layer_b(a.shim_port) is False:
            sys.exit("\nLayer B failed: suspect the integration.")
    if "c" in a.layers:
        layer_c(a.shim_port, key, engines, its, a.reps)


if __name__ == "__main__":
    main()
