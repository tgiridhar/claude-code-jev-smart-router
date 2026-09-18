# Developer guide

## Prerequisites

- Python 3.12 or newer.
- A TypeSafe API key, from `console.typesafe.ai/settings/keys`. Without one,
  `classify()` returns `None` and every request forwards unrouted. The proxy still
  starts and still serves all endpoints.
- Claude Code, with either a subscription (Pro, Max, Team) or an API key.

## Install and run

```bash
pip install -r requirements.txt
export TYPESAFE_API_KEY=...
uvicorn jev_router:app --port 8787
```

Port 8787 is not hardcoded. Use any port and set `ANTHROPIC_BASE_URL` to match.

Run a single worker. Do not pass `--workers N`. Tier state, the classification
memo and the per-model output rates are in process memory, so multiple workers
route same-session requests to processes holding different state. See
[ARCHITECTURE.md](ARCHITECTURE.md#state).

Bind to loopback. The `/router/*` control endpoints have no authentication and no
CSRF token.

## Connecting Claude Code

### Subscription (Pro, Max, Team)

Set the base URL only.

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8787
claude
```

Do **not** set `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN`, and do not run
`/logout`. Setting `ANTHROPIC_BASE_URL` on its own does not replace the
subscription: requests route through the proxy while the saved claude.ai login
remains the active credential, so the plan's limits and billing apply. Setting a
gateway credential variable replaces the subscription, after which traffic bills
per token to the owner of that credential.

Two differences on this path:

- Routing conserves usage limit rather than reducing a billed amount. Measure it
  by how often the plan reaches its limits, not with `/cost`.
- Claude Code stops validating plan requirements behind a gateway. It will send a
  model the plan does not serve and the upstream will reject it. List only models
  the plan serves in `ROUTER_TIERS`.

The proxy forwards the OAuth bearer and `anthropic-beta` unmodified. On
subscription requests that header carries an OAuth capability; removing it returns
401.

### API key

```bash
export ANTHROPIC_API_KEY=sk-ant-...   # used only if the client sends no credential
export ANTHROPIC_BASE_URL=http://127.0.0.1:8787
claude
```

### Persisting the setting

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8787"
  }
}
```

in `~/.claude/settings.json`.

### Manual override via /model

By default every request is routed, so `/model opus` has no effect. Set a
sentinel to route only requests naming it:

```bash
export ROUTER_SENTINEL=auto
export ANTHROPIC_CUSTOM_MODEL_OPTION=auto
export ANTHROPIC_CUSTOM_MODEL_OPTION_NAME="Auto (Jev)"
```

`auto` appears as a row in the `/model` picker. Selecting it enables routing;
selecting any other model forwards that choice unchanged. Claude Code does not
validate `ANTHROPIC_CUSTOM_MODEL_OPTION`, so a non-model string is accepted.

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

The catch-all handles `/v1/models`, used for gateway model discovery. Claude Code
allows 3 seconds for that call and treats any redirect as failure, so serve it at
the base URL. The catch-all also handles `/v1/messages/count_tokens` and the
`HEAD /api/hello` connection-warming probe.

## Configuration

All 28 variables with their defaults. `.env.example` contains the same list in
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
| `ROUTER_RISK_SURFACE` | see ONTOLOGY.md | Regex defining a risk surface. Override per repository. |
| `ROUTER_CLASSIFY_TIMEOUT` | `2.0` | Seconds before a Jev call is abandoned and the turn forwards unrouted |
| `ROUTER_UPSTREAM` | `https://api.anthropic.com` | Where requests go |

### Cache cost

Verify the prices before relying on the arithmetic. They are list prices recorded
at the time of writing, not fetched live.

| Variable | Default | Does |
| --- | --- | --- |
| `ROUTER_PRICES` | Haiku 1/5, Sonnet 2/10, Opus 5/25 | `$`/MTok in and out per model. Verified against billed cost, not copied from a page. |
| `ROUTER_CACHE_WRITE_MULT` | `2.0` | Cache write multiple of base input price. Claude Code requests a **1-hour** cache, which bills at 2.0x. 1.25x is the 5-minute rate. |
| `ROUTER_CACHE_HIT_MULT` | `0.10` | Cache read multiple of base input price |
| `ROUTER_CACHE_TTL_SAFE` | `2880` | Seconds before the cache is treated as cold. Under the real 3600 on purpose: the TTL runs from the **start** of the touching request. |
| `ROUTER_SWITCH_HORIZON` | `3` | Turns over which a one-time cache rebuild must pay for itself |
| `ROUTER_VERBOSITY` | `{}` | Per-model output volume priors, used until enough turns are observed |
| `ROUTER_TURN_MULT` | `{}` | Per-model agentic turn count priors. A cheaper model that needs more turns is not cheaper. |
| `ROUTER_OUT_RATE_MIN_SAMPLES` | `8` | Turns of real usage needed per model before the learned output ratio beats the prior |
| `ROUTER_DOWNGRADE_PATIENCE` | `3` | **Fallback only.** Used before any usage telemetry exists. Once real numbers arrive, switches are priced, not counted. |
| `ROUTER_JEV_PRICE_IN` | `0.042` | Jev input `$`/MTok. Its output is treated as free. |

### Effort steering

Lowering effort on the same model leaves the prompt cache intact, so it reduces
cost without a cache rebuild. Disabled by default: the header is beta, and this is
the only feature that modifies `messages`.

| Variable | Default | Does |
| --- | --- | --- |
| `ROUTER_EFFORT_STEERING` | `0` | `1` enables per-message effort markers |
| `ROUTER_EFFORT_CAPABLE` | `["claude-opus-5","claude-fable-5-1","claude-mythos-5-1"]` | Models that accept per-message effort |
| `ROUTER_EFFORT_BETA` | `mid-conversation-output-config-2026-07-01` | The beta header token this needs |

Top-level effort is never changed, since that invalidates the cache in the same
way a model switch does. If the upstream rejects an effort marker with a 400,
steering is disabled process-wide and the request retries without markers.

### Utility pin

| Variable | Default | Does |
| --- | --- | --- |
| `ROUTER_UTILITY_MAX_TOKENS` | `1024` | Requests at or below this `max_tokens` are pinned, never classified, kept out of routing state |
| `ROUTER_UTILITY_MODEL` | middle rung of `ROUTER_TIERS` | Where they go |

Claude Code issues sidecar requests within a session: permission classifiers,
quota probes, topic detection. Unfiltered, they reset the continuation counter
each turn, preventing rechecks, and overwrite the session's token telemetry.

### Observability

| Variable | Default | Does |
| --- | --- | --- |
| `ROUTER_LOG_LEVEL` | `INFO` | Standard logging level, logger name `jev-router` |
| `ROUTER_TRACE_DIR` | `""` | Off. Set it to write one append-only JSONL per session. |

> `ROUTER_TIERS`, `ROUTER_PRICES`, `ROUTER_VERBOSITY`, `ROUTER_TURN_MULT` and
> `ROUTER_EFFORT_CAPABLE` are parsed as JSON at import time, unguarded. Malformed
> JSON in any of them crashes startup with a `JSONDecodeError`. That is
> deliberate: failing loudly at boot beats routing on a silently empty config.

## Dashboard

`GET /dashboard` serves a single HTML page with no build step, polling
`/router/metrics` every 2.5 seconds. Contents:

| Element | Shows |
| --- | --- |
| Decision rail | The last 30 minutes. Filled marks are switches, hollow marks are holds, height is the price tier. |
| Spend | Cost so far against a baseline of pinning the highest tier. |
| Gauges | Cache hit ratio, request counts per model, classifier p50 and p95 latency. |
| Decision log | The reason string and cost arithmetic for each selection. |
| Toggle | Disables routing without restarting. |

The spend comparison is an upper bound: it prices the weaker model's additional
turns at the highest tier. Use trace data for an accurate figure.

## Tracing and calibration

The thresholds in `pick_tier_v2` are defaults. Trace data allows replacing them
with values measured on a specific repository.

```bash
export ROUTER_TRACE_DIR=~/jev-traces    # NOT inside this repo
```

> Traces contain `user_preview`, a verbatim slice of each prompt, plus file paths
> and command output. Tracing is off by default. `*.jsonl` is gitignored in this
> repository. Do not point `ROUTER_TRACE_DIR` at the source tree.

One append-only file per session, three event types: `decision`, `usage`, `drop`.
Each `decision` carries the wanted tier, the chosen tier, the policy that decided,
the reasons, the classifier latency, and the ledger's outcome labels: last check
result, failure streak, repeated-failure count, `user_correcting`, trajectory.

The labels on a decision describe the state that the previous decision produced.
Join each decision to the labels on the following decisions of the same session
to obtain

```
P(check passes | tier, phase, demand)
```

Compare within the same phase and the same demand, varying only tier. The
output-rate ratio in `/router/metrics` is confounded, since tiers receive
different work, which is why it is clamped to `[0.5, 3.0]` and applied as a
correction.

## Modification points

**Change a routing decision.** Edit the relevant conditional in `pick_tier_v2`
(`jev_ontology.py`). The policy is a sequence of `if` statements rather than a
prompt, so changing behaviour means changing a condition. The 15 questions are
sent separately rather than as one combined question because the classifier is
calibrated per individual judgment.

**Update tool names.** `TOOL_KINDS`, `BASH_CLASSES`, `RISK_SURFACE`, `ROLE_SNIFF`,
`FAIL_TEXT` and `PASS_TEXT` are at the top of `jev_ontology.py`. Names change
between Claude Code releases: `Task` versus `Agent`, `TodoWrite` versus
`TaskCreate`, and current macOS and Linux builds omit `Grep` and `Glob`. Compare
against recorded traces after upgrading.

**Add or remove a tier.** Add the model to `ROUTER_TIERS` and give it an entry in
`ROUTER_PRICES`. A two-tier ladder is valid if trace data shows the middle tier
costs more per completed task than the highest one.

### Invariants

1. No failure in the classification path terminates a request. `classify()`,
   `safe_ledger()`, `trace()` and `record_usage()` each swallow all exceptions.
2. `extract_ledger` stays a pure function of the request body, with no
   accumulated state.
3. `cache_control` markers and the `system` array are forwarded unmodified.
   Merging blocks, stringifying `system` or reordering it disables prompt caching
   for the client.
4. A credential sent by the client is neither inspected nor replaced.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| Every request fails 401 on a subscription | `anthropic-beta` was stripped somewhere. It carries an OAuth capability. The proxy forwards it verbatim; check anything else in the path. |
| Upstream rejects the chosen model | The plan does not serve that model. Claude Code does not validate this behind a gateway. Remove it from `ROUTER_TIERS`. |
| Nothing is ever routed | No `TYPESAFE_API_KEY`, or `ROUTER_SENTINEL` is set and the request's model does not match it. Check `/router/status` and the `jev` block in `/router/metrics`. |
| Models switch constantly | The no-telemetry fallback path is active. Check whether usage counters appear in `/router/metrics`. If they do, read the arithmetic in the decision log. If they do not, increase `ROUTER_DOWNGRADE_PATIENCE`. |
| Effort steering stopped working mid-session | A 400 rejected an effort marker, so it disabled itself process-wide and retried plain. Check the log, and check `ROUTER_EFFORT_CAPABLE` against models that actually support it. |
| Costs increased | A known failure mode. See [ARCHITECTURE.md](ARCHITECTURE.md#cache-cost-arithmetic). Check `ROUTER_PRICES` against current list prices first; the breakeven calculation depends on them. |
| `/context` shows the wrong model | Known gap. `count_tokens` is forwarded without rewriting its model field. |
| `JSONDecodeError` at startup | Malformed JSON in one of the five JSON-valued env vars. |

## Protocol compliance

Verified behaviours and known gaps are listed in
[ARCHITECTURE.md](ARCHITECTURE.md#protocol-compliance), and the authoritative
version is `ROUTING NOTES` at the foot of `jev_router.py`.

There is no compliance test script in this repository. An earlier draft of the
README documented a `compliance_check.py`; that file does not exist. These
behaviours were verified by reading the implementation. Re-check after each Claude
Code release.

Anthropic does not endorse, maintain or audit third-party gateway products, and
does not support routing Claude Code to non-Claude models through any gateway.

## Smoke tests

None of these require an API key.

```bash
# ontology module, no API key required
python3 jev_ontology.py

# proxy startup with no configuration
python3 -m uvicorn jev_router:app --port 8787

curl -s localhost:8787/router/status
curl -s localhost:8787/router/metrics
curl -s -o /dev/null -w '%{http_code}\n' localhost:8787/dashboard
```

Expect `{"enabled": true}`, a JSON metrics snapshot with `requests_total: 0`, and
`200`. Use `GET` on `/dashboard`: it is registered for `GET` only, so a `HEAD`
falls through to the catch-all and gets forwarded upstream, which answers 404.
That is the catch-all behaving correctly, not a broken route.

For an end-to-end check, set the API key, point Claude Code at the proxy, and run
a sequence that crosses phases: ask a question, let it search, let it edit, then
let it run tests. Observe the tier on `/dashboard` and the arithmetic in the
decision log.
