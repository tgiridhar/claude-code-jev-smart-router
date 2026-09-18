# Developer guide

## Prerequisites

- Python 3.12 or newer.
- A TypeSafe key for Jev, from `console.typesafe.ai/settings/keys`. **Without it
  the router runs fine and routes nothing:** `classify()` returns `None` and every
  request forwards untouched. That is the designed degradation, not a bug.
- Claude Code, with either a subscription (Pro, Max, Team) or an API key.

## Install and run

```bash
pip install -r requirements.txt
export TYPESAFE_API_KEY=...
uvicorn jev_router:app --port 8787
```

Port 8787 is convention, not code. Nothing binds it internally, so use whatever
you like and match `ANTHROPIC_BASE_URL` to it.

**Run one worker.** Do not pass `--workers N`. Sticky tiers, the memo and the
learned output rates all live in process memory, so multiple workers means
same-session requests land on processes with different state and hysteresis
quietly stops working. See the Redis note in
[ARCHITECTURE.md](ARCHITECTURE.md#state) for when that changes.

**Bind to loopback.** The control endpoints have no authentication and no CSRF
token. Anyone who can reach `/router/disable` can turn routing off.

## Wiring Claude Code

### On a subscription (Pro, Max, Team)

Set the base URL and nothing else.

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8787
claude
```

Do **not** set `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN`, and do not run
`/logout`. Setting `ANTHROPIC_BASE_URL` on its own does not replace the
subscription: requests route through the proxy while your saved claude.ai login
stays the active credential, so your plan's limits and billing apply. Setting a
gateway credential variable is what replaces the subscription, and then traffic
bills per token to whoever owns that credential.

Two things are different on this path:

- **You are conserving usage limit, not reducing a bill.** Routing easy steps to
  Haiku stretches how far your plan goes. Judge it on how often you hit limits,
  not on `/cost`.
- **Claude Code stops checking plan requirements behind a gateway.** It will
  happily send a model your plan cannot serve and let the upstream reject it.
  Keep `ROUTER_TIERS` to models you actually have.

The proxy relays the OAuth bearer and `anthropic-beta` untouched, which this path
requires: that header carries an OAuth capability on subscription requests, and
stripping it fails every one of them with a 401.

### With an API key

```bash
export ANTHROPIC_API_KEY=sk-ant-...   # used only if the client sends no credential
export ANTHROPIC_BASE_URL=http://127.0.0.1:8787
claude
```

### Persisting it

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8787"
  }
}
```

in `~/.claude/settings.json`.

### Keeping `/model` as a manual override

By default every request is routed, which means `/model opus` stops meaning
anything. To keep it working, set a sentinel and route only requests that ask for
it:

```bash
export ROUTER_SENTINEL=auto
export ANTHROPIC_CUSTOM_MODEL_OPTION=auto
export ANTHROPIC_CUSTOM_MODEL_OPTION_NAME="Auto (Jev)"
```

`auto` then appears as a row in the `/model` picker. Pick it and you get routing.
Pick anything else and the proxy passes your choice straight through. Claude Code
skips validation on `ANTHROPIC_CUSTOM_MODEL_OPTION`, so a non-model string like
`auto` is fine there.

## Endpoints

| Method | Path | Does |
| --- | --- | --- |
| POST | `/v1/messages` | the routed path |
| GET | `/dashboard` | self-contained HTML, polls metrics every 2.5s |
| GET | `/router/status` | current enabled state |
| POST | `/router/enable` | turn routing on |
| POST | `/router/disable` | turn routing off, passthrough everything |
| POST | `/router/toggle` | flip it |
| GET | `/router/metrics` | JSON snapshot, `cache-control: no-store` |
| any | `/{path}` | forwarded upstream unchanged |

The catch-all matters more than it looks. It covers `/v1/models` for gateway model
discovery, which Claude Code gives a 3 second budget and treats any redirect as
failure, so serve it at the base URL. It also covers
`/v1/messages/count_tokens` and the `HEAD /api/hello` connection-warming probe.

## Configuration

All 28 variables, with the code's own defaults. `.env.example` is the same list in
copyable form.

### Jev

| Variable | Default | Does |
| --- | --- | --- |
| `TYPESAFE_API_KEY` | `""` | The key. Empty means no classification at all. |
| `TYPESAFE_URL` | `https://api.typesafe.ai/v1/systemone` | Judge endpoint |
| `TYPESAFE_MODEL` | `jev-latest` | Pin a Jev version here |

### Routing policy

| Variable | Default | Does |
| --- | --- | --- |
| `ROUTER_TIERS` | `["claude-haiku-4-5","claude-sonnet-5","claude-opus-5"]` | The ladder, cheapest first. The index is the tier. |
| `ROUTER_MIN_CONFIDENCE` | `0.45` | Below this, Jev's answer is not trusted and the tool-derived hint is used |
| `ROUTER_ENABLED` | `1` | `0` starts in passthrough. Also flippable at runtime. |
| `ROUTER_SENTINEL` | `""` | Only route requests naming this model. Empty routes everything. |
| `ROUTER_RECHECK_EVERY` | `6` | Fallback recheck cadence in turns. `0` disables it; events still fire. |
| `ROUTER_RISK_SURFACE` | a long regex | What counts as a risk surface. Override for your repo. |
| `ROUTER_CLASSIFY_TIMEOUT` | `2.0` | Seconds before a Jev call is abandoned and the turn forwards unrouted |
| `ROUTER_UPSTREAM` | `https://api.anthropic.com` | Where requests go |

### Cache economics

This group decides whether routing saves money. **Verify the prices before
trusting the arithmetic**; they are list prices at the time of writing, not a
live feed.

| Variable | Default | Does |
| --- | --- | --- |
| `ROUTER_PRICES` | Haiku 1/5, Sonnet 3/15, Opus 5/25 | `$`/MTok in and out per model |
| `ROUTER_CACHE_WRITE_MULT` | `1.25` | Cache write multiple of base input price |
| `ROUTER_CACHE_HIT_MULT` | `0.10` | Cache read multiple of base input price |
| `ROUTER_CACHE_TTL_SAFE` | `240` | Seconds before the cache is treated as cold. Under the real 300 on purpose: the TTL runs from the **start** of the touching request. |
| `ROUTER_SWITCH_HORIZON` | `3` | Turns over which a one-time cache rebuild must pay for itself |
| `ROUTER_VERBOSITY` | `{}` | Per-model output volume priors, used until enough turns are observed |
| `ROUTER_TURN_MULT` | `{}` | Per-model agentic turn count priors. A cheaper model that needs more turns is not cheaper. |
| `ROUTER_OUT_RATE_MIN_SAMPLES` | `8` | Turns of real usage needed per model before the learned output ratio beats the prior |
| `ROUTER_DOWNGRADE_PATIENCE` | `3` | **Fallback only.** Used before any usage telemetry exists. Once real numbers arrive, switches are priced, not counted. |
| `ROUTER_JEV_PRICE_IN` | `0.042` | Jev input `$`/MTok. Its output is treated as free. |

### Effort steering

The cheap lever. Lowering effort on the **same** model leaves the prompt cache
intact, so it buys you savings without paying a rebuild. Off by default because it
is beta, and because it is the only place this proxy edits `messages`.

| Variable | Default | Does |
| --- | --- | --- |
| `ROUTER_EFFORT_STEERING` | `0` | `1` enables per-message effort markers |
| `ROUTER_EFFORT_CAPABLE` | `["claude-opus-5","claude-fable-5-1","claude-mythos-5-1"]` | Models that accept per-message effort |
| `ROUTER_EFFORT_BETA` | `mid-conversation-output-config-2026-07-01` | The beta header token this needs |

Top-level effort changes are never made. Those invalidate the cache exactly like a
model switch. If the upstream rejects an effort marker with a 400, steering
disables itself process-wide and the request retries plain.

### Utility pin

| Variable | Default | Does |
| --- | --- | --- |
| `ROUTER_UTILITY_MAX_TOKENS` | `1024` | Requests at or below this `max_tokens` are pinned, never classified, kept out of routing state |
| `ROUTER_UTILITY_MODEL` | middle rung of `ROUTER_TIERS` | Where they go |

Claude Code interleaves sidecar calls into the session: permission classifiers,
quota probes, topic detection. They are not conversation turns, and left
unfiltered they starve rechecks and corrupt the session's telemetry.

### Observability

| Variable | Default | Does |
| --- | --- | --- |
| `ROUTER_LOG_LEVEL` | `INFO` | Standard logging level, logger name `jev-router` |
| `ROUTER_TRACE_DIR` | `""` | Off. Set it to write one append-only JSONL per session. |

> `ROUTER_TIERS`, `ROUTER_PRICES`, `ROUTER_VERBOSITY`, `ROUTER_TURN_MULT` and
> `ROUTER_EFFORT_CAPABLE` are parsed as JSON at import time, unguarded. Malformed
> JSON in any of them crashes startup with a `JSONDecodeError`. That is
> deliberate: failing loudly at boot beats routing on a silently empty config.

## Reading the dashboard

`GET /dashboard`. One self-contained page, no build step, polling
`/router/metrics` every 2.5 seconds.

- **The rail** is the last 30 minutes of decisions. Filled means the model moved,
  hollow means it held. Height is the price tier. A rail of mostly hollow marks is
  the cache policy working, not the router failing.
- **Money** shows spend against a pin-the-top-tier baseline.
- **Cache hit gauge**, traffic by model, classifier p50 and p95.
- **The decision feed** prints the reasoning for each pick and the arithmetic for
  each hold. This is the thing to read when a decision surprises you.
- The toggle turns routing off without restarting.

**The savings figure is an upper bound.** It prices the weaker model's extra
turns at the top tier, which flatters the router. The honest number comes from
traces.

## Tracing and calibration

Every threshold in `pick_tier_v2` is a prior. This is how you replace them with
numbers from your own repo.

```bash
export ROUTER_TRACE_DIR=~/jev-traces    # NOT inside this repo
```

> Traces contain `user_preview`, a short verbatim slice of your prompts, plus file
> paths and command output. They are local-only and the feature is off by
> default. `*.jsonl` is gitignored in this repo for exactly that reason. Do not
> commit them and do not point `ROUTER_TRACE_DIR` at the source tree.

One append-only file per session, three event types: `decision`, `usage`, `drop`.
Each `decision` carries the wanted tier, the chosen tier, the policy that decided,
the reasons, the classifier latency, and the ledger's outcome labels: last check
result, failure streak, repeated-failure count, `user_correcting`, trajectory.

The calibration join: the labels on a decision describe the state that the
**previous** decision led to. So join each decision to the labels on the
following decisions of the same session, and you get

```
P(check passes | tier, phase, demand)
```

for your repo. That is the number that tells you whether Sonnet really does handle
your debug turns, rather than whether it felt like it did.

The clean comparison is same phase, same demand, different tier. The learned
output-rate ratio in `/router/metrics` is confounded, because tiers get handed
different work, which is why the code clamps it to `[0.5, 3.0]` and treats it as a
correction rather than a measurement.

## Extending it

**Correct a routing decision.** Change a branch in `pick_tier_v2` in
`jev_ontology.py`. The weighting is deliberately a block of ordinary `if`
statements: when you decide Sonnet handles your investigations fine, you edit a
condition rather than rewrite a prompt. The 15 questions are separate rather than
one "which model should handle this" because Jev is calibrated per atomic
judgment, and decomposing keeps each one reliable.

**Fix tool-name drift.** `TOOL_KINDS`, `BASH_CLASSES`, `RISK_SURFACE`,
`ROLE_SNIFF`, `FAIL_TEXT` and `PASS_TEXT` all live at the top of
`jev_ontology.py` so names can be corrected without touching logic. Tool names do
drift: `Task` versus `Agent`, `TodoWrite` versus `TaskCreate`, and current macOS
and Linux builds dropping `Grep` and `Glob` in favour of searching through Bash.
Check your traces against the table after a Claude Code upgrade.

**Add or remove a rung.** Put the model in `ROUTER_TIERS` and give it an entry in
`ROUTER_PRICES`. If your traces show the middle tier costing more per finished
unit of work than the top one, delete it. A two-rung ladder, Haiku for rote loops
and Opus with effort steering for everything else, is a legitimate outcome of
measuring rather than a degenerate case.

### Invariants worth keeping

1. **Nothing in the classification path may fail a turn.** `classify()`,
   `safe_ledger()`, `trace()` and `record_usage()` each swallow every exception on
   purpose. A classifier outage should cost you routing, not your session.
2. **The ledger stays a pure function of the request.** No memory, no
   accumulation. That is what makes it restart-safe and drift-proof.
3. **`cache_control` and the `system` array pass through untouched.** Merging
   blocks, stringifying `system` or reordering it is how gateways silently disable
   prompt caching for their users.
4. **The credential is never inspected or replaced** when the client sent one.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| Every request fails 401 on a subscription | `anthropic-beta` was stripped somewhere. It carries an OAuth capability. The proxy forwards it verbatim; check anything else in the path. |
| Upstream rejects the chosen model | Your plan does not serve it. Behind a gateway Claude Code stops checking, so trim `ROUTER_TIERS`. |
| Nothing is ever routed | No `TYPESAFE_API_KEY`, or `ROUTER_SENTINEL` is set and the request's model does not match it. Check `/router/status` and the `jev` block in `/router/metrics`. |
| Models switch constantly | You are on the no-telemetry fallback path. Confirm usage is being observed in `/router/metrics`; if it is, read the arithmetic in the decision feed. If it is not, widen `ROUTER_DOWNGRADE_PATIENCE`. |
| Effort steering stopped working mid-session | A 400 rejected an effort marker, so it disabled itself process-wide and retried plain. Check the log, and check `ROUTER_EFFORT_CAPABLE` against models that actually support it. |
| Costs went **up** | The expected failure mode. Read [ARCHITECTURE.md](ARCHITECTURE.md#why-the-cache-is-the-whole-economics). Verify `ROUTER_PRICES` against current list prices first, since the whole breakeven depends on them. |
| `/context` shows the wrong model | Known gap. `count_tokens` is forwarded without rewriting its model field. |
| `JSONDecodeError` at startup | Malformed JSON in one of the five JSON-valued env vars. |

## Protocol compliance

What holds and what deliberately does not is in
[ARCHITECTURE.md](ARCHITECTURE.md#protocol-compliance), and the authoritative
version is `ROUTING NOTES` at the foot of `jev_router.py`.

**There is no compliance test script in this proof of concept.** An earlier draft
of the README documented a `compliance_check.py`; it is not part of this repo. The
compliance claims are verified by reading, not by CI. Claude Code gains
capabilities every release, so re-check after upgrading rather than assuming they
still hold.

Anthropic does not endorse, maintain or audit third-party gateway products, and
does not support routing Claude Code to non-Claude models through any gateway.

## Smoke tests

None of these need a TypeSafe key.

```bash
# the ontology's deterministic half, end to end
python3 jev_ontology.py

# the proxy boots with no configuration at all
python3 -m uvicorn jev_router:app --port 8787

curl -s localhost:8787/router/status
curl -s localhost:8787/router/metrics
curl -s -o /dev/null -w '%{http_code}\n' localhost:8787/dashboard
```

Expect `{"enabled": true}`, a JSON metrics snapshot with `requests_total: 0`, and
`200`. Use `GET` on `/dashboard`: it is registered for `GET` only, so a `HEAD`
falls through to the catch-all and gets forwarded upstream, which answers 404.
That is the catch-all behaving correctly, not a broken route.

For a real end-to-end check, set your key, point Claude Code at the proxy, and run
a few turns that cross phases: ask a question, let it explore, let it edit, then
let it run the tests. Watch the tier move on `/dashboard` and watch the held
decisions print their arithmetic.
