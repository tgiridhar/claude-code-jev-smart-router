"""Benchmark arms.

Every arm runs the client through the router process, including the controls.
Disabled routing is a pure passthrough that still records usage and still
writes ev:"usage" traces (jev_router.py:972-979), so control and treatment
tokens are counted by the same sniffer and priced with the same table.

ROUTER_SENTINEL is forced empty everywhere. With a sentinel set, the disabled
path still rewrites the model to the middle tier (jev_router.py:963-979), which
would silently corrupt the controls.
"""

HAIKU = "claude-haiku-4-5"
SONNET = "claude-sonnet-5"
OPUS = "claude-opus-5"

# Measured, not assumed. calibrate_prices.py solves these from observed token
# counts against Claude Code's own cost figure. See prices.json.
#
# Two corrections against the router's shipped defaults:
#   - Sonnet 5 is $2/$10 per MTok, not $3/$15. The shipped table overprices it
#     by 50%.
#   - Claude Code writes its prompt cache with a 1-hour TTL, which bills at
#     2.0x the input rate. The shipped ROUTER_CACHE_WRITE_MULT of 1.25 is the
#     5-minute rate.
#
# This is not cosmetic. switch_delta() prices every routing decision off this
# table, so a router running the shipped defaults believes the middle rung
# costs 3x Haiku when it costs 2x, and believes a cache rebuild is cheaper
# than it is. Both push it away from Sonnet, which is the hypothesis under
# test. Benchmarking the stale table would measure a misconfigured router.
# Nothing is overridden here any more. The corrections these arms used to
# carry were folded into jev_router.py itself, so the benchmark now measures
# the router exactly as it ships.
PRICED = {}


# Both classifier arms get a classify timeout well above either engine's
# latency, so neither can be silently degraded by a timeout.
#
# classify() swallows a timeout and returns None (jev_router.py:519), which is
# indistinguishable from passthrough: the arm keeps running and quietly stops
# being a routed arm at all. Jev answers in ~275 ms over the network and never
# approaches the 2.0 s default. Laya, running on CPU/MPS on this machine, takes
# ~2.1-3.0 s per call and would time out on nearly every request. That is
# hardware, not the model: Laya's published figure is 32.8 ms on a single GPU.
#
# Raising the ceiling for both arms measures classification quality rather than
# the host's torch throughput. Latency is reported separately instead of being
# allowed to silently null out one arm's classifier.
CLASSIFY_TIMEOUT = {"ROUTER_CLASSIFY_TIMEOUT": "20"}


class Arm:
    def __init__(self, name, model, router_env, needs_key, note, uses_laya=None):
        self.name = name
        self.model = model          # what the client asks for
        self.router_env = router_env
        self.needs_key = needs_key  # does this arm need TYPESAFE_API_KEY
        self.note = note
        # None, or the laya_shim --ontology this arm needs ("jev" | "native").
        # run_bench starts one shim per distinct value.
        self.uses_laya = uses_laya

    def env(self):
        e = {"ROUTER_SENTINEL": "", "ROUTER_LOG_LEVEL": "INFO"}
        e.update(self.router_env)
        return e

    def __repr__(self):
        return f"<Arm {self.name}>"


ARMS = [
    Arm(
        "control-opus",
        model="opus",
        router_env={"ROUTER_ENABLED": "0"},
        needs_key=False,
        note="Pinned Opus. The ground truth the router's baseline_top is trying to estimate.",
    ),
    Arm(
        "control-haiku",
        model="haiku",
        router_env={"ROUTER_ENABLED": "0"},
        needs_key=False,
        note=("Pinned Haiku. The floor: what the cheapest tier can and cannot do "
              "unaided. Without it there is no reference for what the money buys."),
    ),
    Arm(
        "router-3tier",
        model="opus",
        router_env={
            "ROUTER_ENABLED": "1",
            "ROUTER_TIERS": f'["{HAIKU}","{SONNET}","{OPUS}"]',
            **CLASSIFY_TIMEOUT,
            **PRICED,
        },
        needs_key=True,
        note="The shipped default ladder, on corrected prices.",
    ),
    Arm(
        "router-3tier-laya-native",
        model="opus",
        router_env={
            "ROUTER_ENABLED": "1",
            "ROUTER_TIERS": f'["{HAIKU}","{SONNET}","{OPUS}"]',
            "TYPESAFE_MODEL": "laya-native",
            "ROUTER_JEV_PRICE_IN": "0",
            **CLASSIFY_TIMEOUT,
            **PRICED,
        },
        needs_key=False,
        uses_laya="native",
        note=("Same Laya checkpoint, same ladder, but the router's questions "
              "and state are re-projected into laya's own documented idiom by "
              "bench/laya_native.py: instructions that name the state field "
              "they are about, option descriptions written as observable "
              "evidence, and a compact structured state. Question ids, types "
              "and choice option keys are unchanged, so the router's policy "
              "engine is untouched. Raises calibration accuracy from 5/14 to "
              "9/14; see bench/calibrate_classifier.py."),
    ),
    Arm(
        "router-3tier-laya",
        model="opus",
        router_env={
            "ROUTER_ENABLED": "1",
            "ROUTER_TIERS": f'["{HAIKU}","{SONNET}","{OPUS}"]',
            # TYPESAFE_URL is injected by run_bench once the shim has a port.
            "TYPESAFE_MODEL": "laya",
            # A self-hosted model has no per-call price. Hardware cost is real
            # but is not a per-request cost and does not belong in the
            # classifier column, which exists to be compared against the
            # inference it routes.
            "ROUTER_JEV_PRICE_IN": "0",
            **CLASSIFY_TIMEOUT,
            **PRICED,
        },
        needs_key=False,  # talks to the local shim, not api.typesafe.ai
        uses_laya="jev",
        note=("Identical ladder to router-3tier; only the classifier differs. "
              "Laya (convaiinnovations/laya, ModernBERT-large 421M, Apache 2.0) "
              "served locally by bench/laya_shim.py on the TypeSafe wire "
              "protocol, so the router itself is unmodified."),
    ),
    Arm(
        "router-haiku-opus",
        model="opus",
        router_env={
            "ROUTER_ENABLED": "1",
            "ROUTER_TIERS": f'["{HAIKU}","{OPUS}"]',
            **PRICED,
        },
        needs_key=True,
        note="Middle rung deleted. jev_router.py:1700 anticipates exactly this change.",
    ),
]

BY_NAME = {a.name: a for a in ARMS}

# Arms whose output is compared against each other by the judge. The control
# arms are in here too: if a routed run beats pinned Sonnet on quality and
# price, that is the result worth having.
JUDGED = [a.name for a in ARMS]

# Pinned Opus is the yardstick. The two ladders answer whether the middle rung
# earns its place.
# Arms kept runnable for reproducibility but left out of reports. The question
# they answered is closed, and carrying a losing configuration through every
# document invites the reader to weigh it again.
RETIRED = ["router-haiku-opus"]

# The two-rung Haiku/Opus ladder is settled and out of the default matrix. It
# cost 10.8% MORE than pinned Opus over 9 runs, because with no middle rung it
# falls back to Opus on anything Haiku cannot take and adds classifier overhead
# on top. The arm definition stays for reproducibility; pass it explicitly with
# --arms if you want to re-measure it.
MAIN = ["control-opus", "control-haiku", "router-3tier"]

# Classifier comparison: one ladder, two engines deciding it. Both arms are
# ROUTER_ENABLED=1 on the same three rungs with the same prices and the same
# timeout, so the only difference between them is which model answers the 15
# questions. No pinned controls: the question here is not what routing is worth
# against a fixed model, which MAIN already answers, but whether Jev and Laya
# route the same work the same way.
CLASSIFIER = ["router-3tier", "router-3tier-laya", "router-3tier-laya-native"]
