# Benchmark harness

Measures what the router actually costs against what it claims to save, and
scores whether the cheaper models degrade the output.

The README's published savings figures come from the router's own estimate,
which its source calls an upper bound. This harness runs the same task pinned
to Opus and compares, which nobody had done.

## Running it

```
export TYPESAFE_API_KEY=...            # router arms are meaningless without it

python3 bench/preflight.py --live      # checks the setup, costs under a cent
python3 bench/calibrate_prices.py      # measures the real per-model rates
python3 bench/run_bench.py --pilot     # 2 tasks x 4 arms, about 50 minutes
python3 bench/collect.py <stamp>
python3 bench/judge.py <stamp>
python3 bench/report.py <stamp>        # writes bench/RESULTS.md
```

`run_bench.py --dry-run` prints the exact command line and environment for
every cell without executing anything. Use it before spending budget.

Full matrix is `run_bench.py` with no `--tasks`: 6 tasks x 4 arms, about two
hours. Runs are serial by design; parallel runs would share upstream prompt
cache state and contend for rate limits.

## What each piece does

| File | Job |
| --- | --- |
| `arms.py` | the router configurations under test |
| `tasks.py` | the tasks, their caps, their objective checks and judge rubrics |
| `fixtures/` | subject files with deliberately planted defects, plus the answer keys |
| `preflight.py` | setup checks that cost nothing, plus one live end-to-end request |
| `calibrate_prices.py` | solves the real per-model rates from observed tokens |
| `run_bench.py` | one run = one fresh router process + one `claude -p` |
| `collect.py` | traces and metrics into `results.json`, plus invariant checks |
| `judge.py` | blind absolute scoring and order-swapped ranking |
| `report.py` | `RESULTS.md`, with path scrubbing |
| `laya_shim.py` | serves a local Laya checkpoint on the TypeSafe wire protocol |
| `report_classifier.py` | `CLASSIFIER.md`, for the Jev-vs-Laya comparison |

## Task kinds

**Built from scratch** (`todo`, `pelican`, `datasci`, `pacman`): scored by
driving the result. A browser uses the app, the SVG is opened, the analysis
scripts are executed.

**Scored against ground truth** (`bugfind`, `secfind`, `algo`): the answer was
written before the task. `bugfind` and `secfind` plant defects in a subject file
and count how many the review finds. `algo` grades an implementation against 20
pytest cases the author never sees. Nothing subjective, and a weaker model can
fail outright instead of merely producing something rougher. See
[`fixtures/README.md`](fixtures/README.md).

**Prose** (`docs`, `secreview`): judged only. Useful for reading quality, not for
measuring whether the work got done.

Note that open-ended tasks are poor cost comparators. On `secreview` both
routed ladders picked Opus for everything and the cost differences were entirely
run-to-run variance in how many turns the agent chose to take.

## Comparing classifiers: Jev against Laya

[Laya](https://laya.convaiinnovations.com/) (`convaiinnovations/laya`,
ModernBERT-large 421M, Apache 2.0) is an open-weight System 1 decision engine
with the same three primitives as Jev -- `choice`, `score`, `noul` -- and a
response envelope already shaped the way `classify()` parses one. Because
`TYPESAFE_URL` is read from the environment (`jev_router.py:75`), swapping the
classifier needs no change to the router at all: `laya_shim.py` serves a local
checkpoint on the same wire protocol and the arm points at it.

```
export LAYA_PYTHON=/path/to/venv/bin/python     # a python with `pip install laya`
python3 bench/run_bench.py --classifier --tasks todo
python3 bench/collect.py <stamp>
python3 bench/report_classifier.py <stamp>      # writes CLASSIFIER.md
```

`--classifier` runs `router-3tier` and `router-3tier-laya`: the same ladder,
the same prices, the same client model and effort, differing only in which
model answers the 15 questions. No pinned controls, because the question is
not what routing saves -- `RESULTS.md` answers that -- but whether the two
engines route the same work the same way. `report.py` is the wrong reporter
for these runs; it computes savings against `control-opus`, which is not in
the matrix.

One shim serves the whole matrix, while the router still gets a fresh process
per run. That asymmetry is deliberate. The router is restarted because it
carries process-global state across runs; the shim carries none, every request
being one stateless forward pass. Loading the checkpoint takes about 35s, so
per-run restarts would cost more wall clock than the runs.

**Three things make a naive swap measure the adapter instead of the model.**
All are handled in `laya_shim.py`, and `CLASSIFIER.md` restates them next to
the numbers.

**Laya sees about half the state, and that turns out not to be the problem.**
Its English checkpoint sets `max_len` 512 and `head_max_len` 192, leaving
roughly 320 tokens for state. The router's state is 676 tokens on the
seven-step demo in `jev_ontology.__main__` and grows with the run. Laya
truncates from the right by default (`laya/common.py:84`), discarding the
`verification`, `risk`, `recent_steps` and `router` blocks that decide the
tier, so the shim truncates from the left instead.

That was the obvious suspect and the ablation clears it. Across left and right
truncation at 512, and left truncation at 1024 and 2048, choice accuracy moves
only from 2/8 to 3/8 and noul accuracy stays at 3/6 -- with the noul
probabilities identical to two decimals in every configuration (0.66, 0.65,
0.75, 0.65, 0.82). Quadrupling the context window changes nothing, which means
the noul head is barely conditioning on the state for these questions. The
budget is a real limitation and is not the binding one.

**Confidence is a different quantity in each engine.** Jev reports a
calibrated peak probability; Laya reports normalized Shannon entropy,
`1 - H(p)/log(k)` (`laya/common.py:200`). Left alone, all six of Laya's
choice and score confidences on a representative state landed between 0.01
and 0.36, entirely below `ROUTER_MIN_CONFIDENCE` (0.45). The router would
have discarded every one and fallen back to the tool-derived phase hint, so
the arm would not have been testing a classifier at all. The shim reports
`max(probabilities)` instead, leaving the distributions untouched. Pass
`--conf native` to measure the drop-in as it ships.

**A classify timeout is indistinguishable from passthrough.** `classify()`
swallows a timeout and returns `None` (`jev_router.py:519`), so an arm that
times out keeps running and quietly stops being routed. Jev answers in about
275 ms over the network; Laya on CPU/MPS on this machine takes 2-3 s and would
have exceeded the 2.0 s default on nearly every call. Both arms therefore set
`ROUTER_CLASSIFY_TIMEOUT=20`. Laya's published figure is 32.8 ms on a single
GPU, so the classify latency column describes this host, not the model.

### Validating the swap before believing any number

A broken adapter and a badly fitted model look the same from outside: both
produce wrong answers at low confidence. `calibrate_classifier.py` separates
them, cheapest layer first, and each layer has to pass before the next means
anything.

```
python3 bench/calibrate_classifier.py --shim-port 8781
```

**A, fidelity.** Every answer the shim returns is compared against an
in-process `laya.predict` on the same state, field by field, and the
confidence is checked against the documented `max(probabilities)` remap. 90
answers over 6 states, all identical. My HTTP and parsing code is not the
variable.

**B, sanity.** Laya's own kind of question -- ticket routing, three queues,
plus an outage `noul` -- on items whose answer is not in doubt. 6 of 6, with
the clear cases at 0.97 and 0.94. The model works through this code path.

**C, calibration.** 13 router states built by `jev_ontology`, 14 labelled
answers, ground truth restricted to states where a careful reader cannot
reasonably disagree: a run that has only searched and read is exploring, a run
whose identical test failure has repeated three times is thrashing.

| | accuracy | Brier | ECE | median latency | sd over 5 identical calls |
| --- | --- | --- | --- | --- | --- |
| Jev | **14/14** | 0.043 | 0.110 | 247 ms | 0.012 |
| Laya | **5/14** | 0.277 | 0.282 | 2298 ms | **0.000** |

Laya's errors are systematic, not noisy. Its `phase` answers collapse onto
`verify` for three of four items -- explore, debug and clarify all read as
`verify` -- and every `noul` it returned across the whole set landed between
0.65 and 0.99, so against a 0.5 cut everything reads true. In this ontology
"true" on `cross_cutting`, `risk_surface` and `irreversible` means hard or
risky, which means escalate, which is exactly the all-Opus routing the `todo`
run produced. The benchmark result and the calibration result are the same
finding seen twice.

The threshold sweep is what rules Laya out for this router specifically, which
gates every answer on confidence:

| cutoff | Jev automatic / wrong | Laya automatic / wrong |
| --- | --- | --- |
| 0.90 | 11 / **0** | 1 / 0 |
| 0.80 | 12 / **0** | 3 / 2 |
| 0.60 | 12 / **0** | 7 / 4 |
| 0.45 | 13 / **0** | 7 / 4 |

There is no cutoff that buys useful automation from Laya here. At 0.90 it
answers one question in fourteen; by 0.80 it is already shipping wrong
answers. Jev clears 13 of 14 at the router's shipped 0.45 without shipping
one. Confidence gating cannot rescue a classifier whose confidence does not
separate its right answers from its wrong ones.

One thing Laya wins outright: it is deterministic. Five identical calls moved
its confidence by 0.0000, against Jev's 0.0117. `docs/CALIBRATION.md` records
that Jev's jitter is large enough to flip an item across a threshold on
retry. A local forward pass has no such failure mode.

**Read the size of this honestly.** 14 labelled answers is a pilot;
`docs/CALIBRATION.md` used 80 items and about 500 calls. The items are
synthetic and I wrote both them and their labels, so Jev scoring 14/14 partly
reflects that this ontology and Jev were developed against each other. The
claims that survive that caveat are the structural ones: the mode collapse
onto `verify`, the noul band that does not move with the input, and the
absence of a usable threshold.

### Designing a router test Laya can actually sit

The calibration above says what Laya got wrong but not why. The clue is in the
ablation: giving it four times the context changed nothing, so it was not
starved of information. Laya's own bundled router preset
(`laya/presets.py:154`) does not ask abstract questions over a wall of JSON.
It asks ``How hard is `request` for a language model?`` -- a short instruction
naming the exact field of a small structured state. Jev's ontology asks "The
software-lifecycle phase of the agent's NEXT step" over a 676-token situation
report and never says where to look.

`laya_native.py` tests that as a falsifiable hypothesis: **Laya's failure here
is a prompt-and-state-shape mismatch, not an inability to make the judgment.**
It changes three things, all presentation:

1. instructions name the state field they are about, in Laya's backtick idiom
2. option descriptions become observable evidence ("only greps and file reads,
   nothing written yet") rather than definitions ("Locating and reading")
3. the state becomes a small flat object keyed by the names the instructions
   use, which also fits the 512-token budget

What is held fixed is the answer contract, because the router's policy engine
consumes it: the same 15 question ids, the same types, the same choice option
keys (`pick_tier_v2` compares `phase` against `PHASES` by name), the same
score arity. This is not a thumb on the scale -- it hands Laya the same
information in the form its own documented interface asks for.

```
python3 bench/run_bench.py --classifier --tasks todo    # all three arms
python3 bench/calibrate_classifier.py --shim-port 8782  # against a native shim
```

| ontology | accuracy | Brier | ECE | median latency |
| --- | --- | --- | --- | --- |
| Jev's questions verbatim | 5/14 (36%) | 0.277 | 0.282 | 2146 ms |
| **native projection** | **9/14 (64%)** | 0.296 | 0.304 | 1527 ms |
| *(Jev itself, for scale)* | *14/14* | *0.043* | *0.110* | *247 ms* |

The hypothesis is substantially confirmed. The `verify` mode collapse breaks:
`explore/search-only` and `clarify/question` both become correct, taking
`phase` from 1/4 to 3/4. More tellingly, `user_correcting` on a plain forward
request moves from 0.65 (wrong) to 0.23 (right) -- the first time any noul in
the set fell below 0.5. The flat 0.65-0.99 band was an artifact of questions
that never told the model what to read, not a stuck head.

**It is still not usable in this router, and the reason is the middle two
columns.** Accuracy went up while Brier and ECE went slightly *down*. Laya is
now right more often but its confidence still does not track whether it is
right: correct answers land at p=0.28-0.33 while wrong ones land at
p=0.80-0.89. So the threshold sweep stays broken -- at the router's shipped
0.45 cutoff the native projection automates 10 of 14 and ships 4 wrong, where
Jev automates 13 and ships none. A router that gates every answer on
confidence cannot use a classifier whose confidence is uninformative, however
good its top-1 accuracy gets.

The failure mode also inverts, which is worth knowing before trusting it.
Verbatim Laya said true to everything and routed 100% to Opus. The native
projection stops over-escalating, but on the thrashing state it now answers
`blocked_on_human` and `pick_tier_v2` returns `hold` where Jev returns tier 2
with a safety flag. Over-escalation wastes money; under-escalation ships worse
work. Neither is safe yet.

That leaves fine-tuning as the only remaining path, which is what the residual
errors point at: `cross_cutting` and `risk_surface` still read true on a
one-line README typo. Those are learned priors, not prompt problems, and Laya
ships a fine-tuning path for exactly this.

**Laya is zero-shot here and Jev is not.** Jev is the classifier this ontology
was written against and whose thresholds it was tuned on. The Laya checkpoint
is a general decision model whose published examples are ticket triage, spam
and moderation; it has never seen a router state. A gap is therefore mostly a
statement about fit, not capacity. Laya ships a fine-tuning path, and a fitted
Laya is the comparison this one does not make.

Cost: the Laya arm sets `ROUTER_JEV_PRICE_IN=0`, because a self-hosted model
has no per-call price. Hardware cost is real, is not per-request, and is not
in that column.

## Why every arm goes through the router

Including the pinned controls. `ROUTER_ENABLED=0` is a pure passthrough that
still records usage and still writes `ev:"usage"` traces, so control and
treatment tokens are counted by the same sniffer and priced from the same
table.

`ROUTER_SENTINEL` is forced empty on every arm. With a sentinel set, the
disabled path still rewrites the model to the middle tier, which would
silently corrupt the controls.

## Isolation

One fresh router process per run, on its own port, killed afterwards. The
router keeps process-global state that is not partitioned by session:
`_metrics` has no reset endpoint, `_out_rate` is a learned output-volume EMA
that changes later routing decisions, `_memo` is the classifier memo, and the
effort off-latch is permanent once tripped. Reusing a process would leak
routing behaviour between experiments.

Each run also gets its own working directory, its own `ROUTER_TRACE_DIR` well
away from `~/.jev-router/traces`, and a fresh UUID passed as `--session-id`.
The router names trace files after the `x-claude-code-session-id` header, so
that UUID is what joins the client's result envelope to the router's records.

## Configuration hazards

These will silently corrupt a benchmark. All of them are handled, and
`preflight.py` checks the ones it can.

**User settings bias the comparison.** `~/.claude/settings.json` here sets
`effortLevel: xhigh` globally but `claude-opus-5` to `high` via
`modelSettings`. A pinned-Opus control would therefore run at *lower* effort
than router arms running Sonnet and Haiku. That asymmetry alone could produce
the result. Every run passes `--setting-sources project`, which drops user
settings entirely, and an explicit identical `--effort`.

**Settings beat the shell.** Claude Code `Object.assign`s settings `env`
blocks onto `process.env` at startup, so a settings file that defines
`ANTHROPIC_BASE_URL` would silently override the per-invocation one and the
runs would bypass the router without any error. No settings file here defines
an `env` block; `preflight.py` checks that it stays that way.

**Inherited environment.** A `claude` launched from inside a Claude Code
session inherits `CLAUDECODE`, `CLAUDE_CODE_*`, `CLAUDE_PID` and
`CLAUDE_EFFORT`. The last one would change token spend, and `ANTHROPIC_MODEL`
would override the arm's model. Every `CLAUDE*` and every `ANTHROPIC*`
configuration variable is stripped from the child environment.

`ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` are deliberately kept. They are
credentials, not configuration. On a machine that authenticates by API key
rather than by stored OAuth credentials, stripping them would fail every run
with an auth error.

**Two models in one run.** Never `--model opusplan`, never
`--permission-mode plan`, never `--fallback-model`. Each resolves to more than
one model in a single run. `collect.py` reports `cc_models` so this is visible
if it ever happens.

**The classifier key.** Without `TYPESAFE_API_KEY` the router passes
everything through and the router arms become duplicate controls, producing a
clean-looking benchmark that shows no savings and no difference between tier
configurations. `run_bench.py` refuses to start those arms without it.

## Prices

`calibrate_prices.py` does not trust any table. It sends one tiny request per
model, reads the exact token counts out of the router's trace and the cost out
of Claude Code's `modelUsage`, and solves for the input rate. Every pricing
tier in Claude Code's catalog has the same internal structure (output 5x
input, cache read 0.1x, cache write 1.25x at 5-minute TTL or 2.0x at 1-hour),
so one observation leaves a single unknown.

It found two errors in the router's shipped defaults. Sonnet 5 is $2/$10 per
MTok, not $3/$15. And Claude Code buys a 1-hour prompt cache, which bills at
2.0x input, not the 1.25x of the 5-minute cache.

Both matter beyond accounting, because `switch_delta()` prices every routing
decision off that table. The correction lives in `arms.py` as `PRICED`.

## Permissions

Runs use `--permission-mode bypassPermissions` inside a fresh empty working
directory created per run. Tasks that need to read source get a **copy**
seeded into that directory, never a path into the repository, so nothing a
task does can reach the real tree.

## What is committed

`RESULTS.md` and the harness. Not `runs/`, which holds the raw artifacts,
traces, prompts and outputs. `report.py` refuses to write a file containing a
path from the author's disk.
