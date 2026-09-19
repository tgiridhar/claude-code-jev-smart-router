#!/usr/bin/env python3
"""TypeSafe wire-protocol shim backed by a local Laya checkpoint.

Laya (convaiinnovations/laya) is an open-weight System 1 decision engine with
the same three primitives as Jev -- choice, score, noul -- and a return
envelope that is already shape-compatible with what jev_router.classify()
parses:

    {"answers": {qid: {"choice"|"score"|"noul": ..., "confidence": ...}},
     "usage": {"input_tokens": n}}

So the router needs no changes at all. TYPESAFE_URL is read from the
environment (jev_router.py:75), so pointing an arm at this shim swaps the
classifier and nothing else.

This process must run under a Python that has laya installed (torch,
transformers, numpy). The router itself does not need those.

    python3 bench/laya_shim.py --port 8765

Two deliberate adaptations, both about making the comparison fair rather than
about making Laya look good. Each is logged at startup and recorded in
/health so the results can state what was done.

CONFIDENCE (--conf maxprob, the default)
    Jev reports confidence as a calibrated peak-probability: 0.97 for a phase
    it is sure of. Laya reports normalized Shannon entropy, 1 - H(p)/log(k)
    (laya/common.py:200), which is a different quantity on a different scale.
    On a representative router state all six of Laya's choice/score
    confidences came back between 0.01 and 0.36, i.e. entirely below the
    router's ROUTER_MIN_CONFIDENCE default of 0.45. The router would have
    discarded every one of them and fallen back to the tool-derived phase
    hint, so the arm would not have been testing a classifier at all.

    Recomputing confidence as max(probabilities) puts both engines on the
    quantity the threshold was written for. The underlying distribution is
    untouched; only the scalar summary changes.

    Pass --conf native to keep Laya's entropy confidence and measure the
    drop-in as it ships.

TRUNCATION (--truncate-left, the default)
    Laya's English checkpoint sets max_len 512 and head_max_len 192, leaving
    about 320 tokens for state. The router's state runs 676 tokens on the
    seven-step demo in jev_ontology.__main__ and grows with the run. Laya
    truncates from the right by default (laya/common.py:84), which drops the
    tail of the state JSON -- the verification, risk, recent_steps and router
    blocks. Those are the routing-critical fields: on that demo state the
    right-truncated model missed three repeated identical test failures and
    reported trajectory=blocked_on_human where Jev reported thrashing.

    Truncating from the left instead keeps the tail and drops the harness and
    unit preamble. Neither choice shows Laya the whole state; this one at
    least spends the budget on the part that decides the tier.

    Pass --no-truncate-left for the shipped right-truncation.

Cost: nothing is charged per call. The arm sets ROUTER_JEV_PRICE_IN=0 so the
classifier line reads $0, which is the correct marginal cost of a self-hosted
model. Hardware cost is real but is not a per-request cost and does not belong
in that column. input_tokens is still reported honestly.
"""

import argparse
import json
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

AGENT = None
CONF_MODE = "maxprob"
ONTOLOGY = "jev"   # "jev" = ask the router's questions verbatim (default)
NATIVE = None      # bench.laya_native, imported only for --ontology native
LOCK = threading.Lock()
STATS = {"calls": 0, "errors": 0, "input_tokens": 0, "total_ms": 0.0}


def load_agent(model, subfolder, device, truncate_left):
    """Load the checkpoint and, if asked, force left truncation."""
    import laya
    import laya.agent as la
    from laya.common import build_sequence as _bs

    if truncate_left:
        def build_sequence_left(tok, state, q, max_len=512, head_max_len=192,
                                option_order=None, truncate_left=False):
            return _bs(tok, state, q, max_len, head_max_len, option_order, True)
        la.build_sequence = build_sequence_left

    return laya.load(model, device=device, subfolder=subfolder)


def restate_confidence(answers):
    """Replace Laya's entropy confidence with max(probabilities).

    noul answers carry no probabilities map; Laya already reports
    max(p, 1-p) for them, which is the peak probability and so is already on
    the quantity we want. Jev omits noul confidence entirely and the router
    defaults it to 1.0 (jev_ontology.py:699), so either way the gate passes.
    """
    for a in answers.values():
        probs = a.get("probabilities")
        if probs:
            a["confidence"] = round(max(float(v) for v in probs.values()), 4)
    return answers


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep the bench logs readable
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/") != "/health":
            return self._send(404, {"error": "not found"})
        with LOCK:
            s = dict(STATS)
        s["avg_ms"] = round(s["total_ms"] / s["calls"], 1) if s["calls"] else None
        s["conf_mode"] = CONF_MODE
        s["ontology"] = ONTOLOGY
        s["ready"] = AGENT is not None
        return self._send(200, s)

    def do_POST(self):
        if "/systemone" not in self.path:
            return self._send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
            state, questions = req.get("state") or "", req.get("questions") or {}
            if not questions:
                return self._send(400, {"error": "no questions"})

            # --ontology native re-projects the same decision into the shape
            # laya's own documented interface asks for. Question ids, types
            # and choice option keys are preserved, so what comes back is
            # still the contract the router's policy engine consumes.
            asked, st = questions, state
            if ONTOLOGY == "native" and NATIVE is not None:
                asked = NATIVE.translate(questions)
                st = NATIVE.project(state)

            t0 = time.time()
            # The model is a single torch module; serialize calls through it.
            with LOCK:
                out = AGENT.predict(st, asked)
            ms = (time.time() - t0) * 1000.0

            answers = out.get("answers") or {}
            if CONF_MODE == "maxprob":
                answers = restate_confidence(answers)
            usage = out.get("usage") or {}

            with LOCK:
                STATS["calls"] += 1
                STATS["total_ms"] += ms
                STATS["input_tokens"] += int(usage.get("input_tokens") or 0)

            return self._send(200, {
                "model": req.get("model") or "laya",
                "answers": answers,
                "usage": usage,
                "latency_ms": round(ms, 1),
            })
        except Exception as exc:  # noqa: BLE001
            with LOCK:
                STATS["errors"] += 1
            traceback.print_exc()
            return self._send(500, {"error": str(exc)})


def main():
    global AGENT, CONF_MODE, ONTOLOGY, NATIVE
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--model", default=os.environ.get("LAYA_MODEL", "convaiinnovations/laya"))
    ap.add_argument("--subfolder", default=os.environ.get("LAYA_SUBFOLDER") or None)
    ap.add_argument("--device", default=os.environ.get("LAYA_DEVICE") or None)
    ap.add_argument("--conf", choices=("maxprob", "native"), default="maxprob")
    ap.add_argument("--ontology", choices=("jev", "native"),
                    default=os.environ.get("LAYA_ONTOLOGY", "jev"),
                    help="jev (default): ask the router's questions verbatim. "
                         "native: re-project them into laya's idiom, same ids "
                         "and same choice keys.")
    ap.add_argument("--truncate-left", dest="tl", action="store_true", default=True)
    ap.add_argument("--no-truncate-left", dest="tl", action="store_false")
    a = ap.parse_args()

    CONF_MODE = a.conf
    ONTOLOGY = a.ontology
    if ONTOLOGY == "native":
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import laya_native
        NATIVE = laya_native

    print(f"[laya-shim] loading {a.model}"
          f"{'/' + a.subfolder if a.subfolder else ''} "
          f"conf={a.conf} ontology={a.ontology} truncate_left={a.tl}", flush=True)
    AGENT = load_agent(a.model, a.subfolder, a.device, a.tl)

    # One warm forward pass. The first call pays lazy init that would
    # otherwise land on the first routed request and risk the classify
    # timeout.
    AGENT.predict("warmup", {"q": {"type": "noul", "instructions": "ready"}})
    print(f"[laya-shim] ready on 127.0.0.1:{a.port}", flush=True)

    ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()


if __name__ == "__main__":
    sys.exit(main())
