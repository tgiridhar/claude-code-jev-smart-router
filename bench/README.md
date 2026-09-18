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
| `tasks.py` | the six tasks, their caps, their objective checks and judge rubrics |
| `preflight.py` | setup checks that cost nothing, plus one live end-to-end request |
| `calibrate_prices.py` | solves the real per-model rates from observed tokens |
| `run_bench.py` | one run = one fresh router process + one `claude -p` |
| `collect.py` | traces and metrics into `results.json`, plus invariant checks |
| `judge.py` | blind absolute scoring and order-swapped ranking |
| `report.py` | `RESULTS.md`, with path scrubbing |

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
