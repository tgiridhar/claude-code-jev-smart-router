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


class Arm:
    def __init__(self, name, model, router_env, needs_key, note):
        self.name = name
        self.model = model          # what the client asks for
        self.router_env = router_env
        self.needs_key = needs_key  # does this arm need TYPESAFE_API_KEY
        self.note = note

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
            **PRICED,
        },
        needs_key=True,
        note="The shipped default ladder, on corrected prices.",
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
