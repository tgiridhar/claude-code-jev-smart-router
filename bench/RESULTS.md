# Benchmark results

Produced by `bench/run_bench.py`.

**Every cost here is counted by the router itself.** The proxy sits in the request
path on every arm, controls included, and reads the token counts off the relayed
response. The client's own cost accounting is not used anywhere, because it keys
usage by the model it asked for rather than the one the router served, and so
prices Haiku and Sonnet tokens at Opus rates.

Two Opus numbers appear throughout, and the gap between them is the point:

- **Perceived Opus** is this run's own tokens extrapolated to Opus rates. It is
  what the router's dashboard reports, and it assumes the work would have taken
  the same tokens on Opus.
- **Measured Opus** is what a real pinned-Opus run of the same task actually
  consumed. No extrapolation.

Perceived is an upper bound: when a cheaper model needs more turns, those extra
turns get billed into it at Opus rates, and a cache rebuild forced by a model
switch lands there too. Both distortions flatter the router.

## Prices used

Measured rather than assumed. `bench/calibrate_prices.py` solves each rate from
observed token counts against the cost Claude Code computed for those same tokens.

| Model | Input $/MTok | Output $/MTok | Cache write | Cache read |
| --- | --- | --- | --- | --- |
| `claude-haiku-4-5-20251001` | 1 | 5 | 2x | 0.1x |
| `claude-opus-5` | 5 | 25 | 2x | 0.1x |
| `claude-sonnet-5` | 2 | 10 | 2x | 0.1x |

Two of these disagree with the router's shipped `ROUTER_PRICES` defaults:

- Sonnet 5 is **$2/$10**, not $3/$15. The shipped table overprices it by 50%.
- Claude Code writes its prompt cache with a **1-hour TTL**, which bills at **2.0x**
  the input rate. The shipped `ROUTER_CACHE_WRITE_MULT` of 1.25 is the 5-minute rate.

The TTL is not inferred from the price arithmetic. Captured off the wire, the request
Claude Code sends carries:

```json
"cache_control": {"type": "ephemeral", "ttl": "1h"}
```

and the API's own usage block in the response confirms where the tokens landed:

```json
"cache_creation": {"ephemeral_5m_input_tokens": 0,
                   "ephemeral_1h_input_tokens": 9206}
```

This is not only an accounting error. `switch_delta()` prices every routing decision
off that table, so a router on the shipped defaults believes the middle rung costs 3x
Haiku when it costs 2x, and believes a cache rebuild is cheaper than it is. Both push
it away from Sonnet. The router arms below run on corrected prices.

`ROUTER_CACHE_TTL_SAFE` inherits the same false premise. It is set to 240 seconds,
reasoned down from a 300-second cache lifetime. The real lifetime is 3600 seconds, so
the router treats a cache as cold after four minutes when it has another fifty-six
left, and prices a downgrade as if there were no warm cache to discard. That makes it
systematically too willing to switch models. The `todo` / `router-3tier` run below is
that failure in practice: cache hits fell from 80% to 35% and the cheaper model cost
24% more than Opus.

## Arms

| Arm | Client asks for | Router | Notes |
| --- | --- | --- | --- |
| `control-opus` | opus | disabled, pure passthrough | Pinned Opus. The ground truth the router's baseline_top is trying to estimate. |
| `control-haiku` | haiku | disabled, pure passthrough | Pinned Haiku. The floor: what the cheapest tier can and cannot do unaided. Without it there is no reference for what the money buys. |
| `router-3tier` | opus | haiku-4-5 / sonnet-5 / opus-5 | The shipped default ladder, on corrected prices. |
| `router-haiku-opus` | opus | haiku-4-5 / opus-5 | Middle rung deleted. jev_router.py:1700 anticipates exactly this change. |

Every arm runs through the router process, controls included, so all tokens are
counted by the same sniffer and priced from the same table. Each run gets its own
router process, working directory, trace directory and session id.

## Per task

### bugfind

The ask, verbatim:

```
orders.py in the current directory handles orders and refunds for a storefront. It is in production.

Review it for defects and write REVIEW.md.

For each defect give the function and line number, what goes wrong in concrete terms, and the fix. Rank by severity. Report only defects you can point at in the code, not general advice. Do not modify orders.py.

When you are done, say DONE.
```

Caps: 30 turns, $2.50 budget, 480s wall clock.

Median of 3 runs per arm. Perfect means every requirement met on every run.

| Arm | Score | Perfect runs | Wall | Cost | Measured Opus | Saving |
| --- | --- | --- | --- | --- | --- | --- |
| `control-opus` | 6/6 | 3/3 | 122s | $0.5486 | $0.5486 | 0.0% |
| `control-haiku` | **4/6** | 0/3 | 38s | $0.0622 | $0.5486 | 88.7% |
| `router-3tier` | **6/6** | 2/3 | 50s | $0.1196 | $0.5486 | 78.2% |
| `router-haiku-opus` | **6/6** | 2/3 | 138s | $0.5903 | $0.5486 | -7.6% |

Models the router served, summed over all runs of the cell:

- `router-3tier`: sonnet-5 x9
- `router-haiku-opus`: opus-5 x21

### secfind

The ask, verbatim:

```
app.py in the current directory is an internal Flask service that serves customer invoices and admin exports. Any authenticated employee can reach it.

Do a security review and write SECURITY.md.

For each finding give the function and line number, the concrete attack it enables, a severity, and a specific fix. Rank by severity. Report only issues you can point at in the code. Do not modify app.py.

When you are done, say DONE.
```

Caps: 35 turns, $2.50 budget, 540s wall clock.

Median of 3 runs per arm. Perfect means every requirement met on every run.

| Arm | Score | Perfect runs | Wall | Cost | Measured Opus | Saving |
| --- | --- | --- | --- | --- | --- | --- |
| `control-opus` | 6/6 | 3/3 | 120s | $0.5045 | $0.5045 | 0.0% |
| `control-haiku` | **5/6** | 1/3 | 32s | $0.0487 | $0.5045 | 90.3% |
| `router-3tier` | 6/6 | 3/3 | 38s | $0.1194 | $0.5045 | 76.3% |
| `router-haiku-opus` | 6/6 | 3/3 | 119s | $0.4762 | $0.5045 | 5.6% |

Models the router served, summed over all runs of the cell:

- `router-3tier`: sonnet-5 x9
- `router-haiku-opus`: opus-5 x15

### algo

The ask, verbatim:

```
spec.md in the current directory specifies a function. Implement it in schedules.py, exactly to the spec.

Read the rules carefully, including the daylight saving ones. Your code will be graded by a test suite you cannot see, covering empty input, unsorted input, touching intervals, zero-length intervals, invalid intervals, and behaviour across both daylight saving transitions.

Standard library only. When you are done, say DONE.
```

Caps: 35 turns, $2.50 budget, 540s wall clock.

Median of 3 runs per arm. Perfect means every requirement met on every run.

| Arm | Score | Perfect runs | Wall | Cost | Measured Opus | Saving |
| --- | --- | --- | --- | --- | --- | --- |
| `control-opus` | 20/20 | 3/3 | 48s | $0.2748 | $0.2748 | 0.0% |
| `control-haiku` | **20/20** | 2/3 | 44s | $0.0593 | $0.2748 | 78.4% |
| `router-3tier` | **20/20** | 2/3 | 37s | $0.1082 | $0.2748 | 60.6% |
| `router-haiku-opus` | 20/20 | 3/3 | 91s | $0.4274 | $0.2748 | -55.5% |

Models the router served, summed over all runs of the cell:

- `router-3tier`: sonnet-5 x12
- `router-haiku-opus`: opus-5 x13

## Aggregate

| Arm | Runs | Fully working builds | Total cost | Total measured Opus | Measured saving | Mean quality | Mean wall |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `control-opus` | 9 | 9/9 | $4.4986 | $3.9837 | -12.9% | - | 97s |
| `control-haiku` | 9 | 3/9 | $0.6318 | $3.9837 | 84.1% | - | 41s |
| `router-3tier` | 9 | 7/9 | $1.1527 | $3.9837 | 71.1% | - | 40s |
| `router-haiku-opus` | 9 | 8/9 | $4.6236 | $3.9837 | -16.1% | - | 120s |

## How far the dashboard overstates

Perceived saving against measured saving, per routed run:

| Run | Perceived saving | Measured saving | Overstated by |
| --- | --- | --- | --- |
| `algo` / `router-3tier` | 59.9% | 58.5% | 1.4 points |
| `algo` / `router-3tier` | 59.9% | 64.3% | -4.4 points |
| `algo` / `router-3tier` | 59.9% | 60.6% | -0.7 points |
| `algo` / `router-haiku-opus` | -0.0% | -55.5% | 55.5 points |
| `algo` / `router-haiku-opus` | -0.0% | -152.4% | 152.4 points |
| `algo` / `router-haiku-opus` | -0.1% | 4.1% | -4.2 points |
| `bugfind` / `router-3tier` | 60.0% | 56.8% | 3.2 points |
| `bugfind` / `router-3tier` | 60.0% | 78.4% | -18.4 points |
| `bugfind` / `router-3tier` | 60.0% | 78.2% | -18.2 points |
| `bugfind` / `router-haiku-opus` | -0.0% | -20.3% | 20.3 points |
| `bugfind` / `router-haiku-opus` | -0.0% | 14.9% | -14.9 points |
| `bugfind` / `router-haiku-opus` | -0.1% | -7.6% | 7.5 points |
| `secfind` / `router-3tier` | 59.9% | 76.8% | -16.9 points |
| `secfind` / `router-3tier` | 59.9% | 76.3% | -16.4 points |
| `secfind` / `router-3tier` | 59.9% | 76.0% | -16.1 points |
| `secfind` / `router-haiku-opus` | -0.1% | -13.2% | 13.1 points |
| `secfind` / `router-haiku-opus` | -0.1% | 6.0% | -6.1 points |
| `secfind` / `router-haiku-opus` | -0.1% | 5.6% | -5.7 points |

## Classifier

- 43 calls across 18 routed runs
- total classifier spend $0.0034
- median latency 262 ms

## What these numbers are not

- One run per cell. Per-task figures are noisy; only the aggregate and the direction
  of the tier verdict carry weight.
- Six tasks chosen by hand, not a sample of real work.
- Costs are list-price estimates on both sides. On a subscription no money changes
  hands per token, so treat these as a comparable unit, not a bill.
- The router only sees `/v1/messages`. Anything Claude Code does off that path is
  invisible to it.
- The judge is Opus scoring output that was sometimes produced by Opus. Blinding and
  order swapping are in place; self-preference is not otherwise controlled for.
