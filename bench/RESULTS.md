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
| `router-3tier` | opus | haiku-4-5 / sonnet-5 / opus-5 | The shipped default ladder, on corrected prices. |
| `router-haiku-opus` | opus | haiku-4-5 / opus-5 | Middle rung deleted. jev_router.py:1700 anticipates exactly this change. |

Every arm runs through the router process, controls included, so all tokens are
counted by the same sniffer and priced from the same table. Each run gets its own
router process, working directory, trace directory and session id.

## Per task

### todo

The ask, verbatim:

```
Create todo.html in the current directory: a single self-contained HTML file implementing a todo list app.

Requirements:
- add a task, mark it complete, delete it
- filter by all / active / completed
- persist to localStorage across reloads
- show a count of remaining items
- edit an existing task's text

All HTML, CSS and JavaScript go in the one file. No external dependencies, no CDN links, no build step. When you are done, say DONE.
```

Caps: 30 turns, $2.00 budget, 420s wall clock.

| Arm | Requirements met | Wall | Cost | Perceived Opus | Measured Opus | Perceived saving | Measured saving | Quality |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `control-opus` | 9/9 | 38s | $0.2662 | $0.2662 | $0.2662 | 0.0% | 0.0% | 4/5 |
| `router-3tier` | 9/9 | 32s | $0.1107 | $0.2761 | $0.2662 | 59.9% | 58.4% | 4/5 |
| `router-haiku-opus` | **2/9** | 38s | $0.0496 | $0.2460 | $0.2662 | 79.8% | 81.4% | 3/5 |

Model mix on the routed arms:

- `router-3tier`: sonnet-5 x3
- `router-haiku-opus`: haiku-4-5 x3

Judge ranking, pass 1: `control-opus` > `router-3tier` > `router-haiku-opus`
Judge ranking, pass 2: `control-opus` > `router-3tier` > `router-haiku-opus`

Pairwise wins where both orderings agreed: `control-opus` 2, `router-3tier` 1, `router-haiku-opus` 0. Disagreements are counted as ties, not results.

### pelican

The ask, verbatim:

```
Create pelican.svg in the current directory: a single SVG file showing a pelican riding a bicycle.

Hand-author the SVG markup. No external images, no embedded raster data, no fonts. It should be recognisable as both a pelican and a bicycle. When you are done, say DONE.
```

Caps: 12 turns, $1.50 budget, 300s wall clock.

| Arm | Requirements met | Wall | Cost | Perceived Opus | Measured Opus | Perceived saving | Measured saving | Quality |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `control-opus` | 5/5 | 35s | $0.2201 | $0.2201 | $0.3565 | 0.0% | 38.3% | 4/5 |
| `router-3tier` | 5/5 | 25s | $0.0899 | $0.2244 | $0.3565 | 59.9% | 74.8% | 3/5 |
| `router-haiku-opus` | 5/5 | 51s | $0.2714 | $0.2713 | $0.3565 | -0.1% | 23.9% | 4/5 |

Model mix on the routed arms:

- `router-3tier`: sonnet-5 x3
- `router-haiku-opus`: opus-5 x3

Judge ranking, pass 1: `control-opus` > `router-haiku-opus` > `router-3tier`
Judge ranking, pass 2: `router-haiku-opus` > `control-opus` > `router-3tier`

Pairwise wins where both orderings agreed: `control-opus` 1, `router-3tier` 0, `router-haiku-opus` 1. Disagreements are counted as ties, not results.

### datasci

The ask, verbatim:

```
Do a small data analysis in the current directory. pandas is installed.

1. Write generate.py that creates sales.csv with 5000 rows of synthetic retail sales data: a date spread over two years, a region drawn from 5 values, a product category drawn from 8 values, units sold, unit price, and a discount percentage. Build in a seasonal revenue pattern and a handful of anomalous days. Run it.
2. Write analyze.py that loads sales.csv with pandas and answers:
   - the monthly revenue trend
   - the top 3 categories by revenue within each region
   - the relationship between discount percentage and units sold
   - which dates are anomalous, and on what basis
   Run it.
3. Write FINDINGS.md reporting what the analysis actually showed, with the numbers.

When you are done, say DONE.
```

Caps: 45 turns, $3.00 budget, 600s wall clock.

| Arm | Requirements met | Wall | Cost | Perceived Opus | Measured Opus | Perceived saving | Measured saving | Quality |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `control-opus` | 10/10 | 215s | $1.0397 | $1.0397 | $0.8573 | 0.0% | -21.3% | 5/5 |
| `router-3tier` | 10/10 | 107s | $0.2432 | $0.6068 | $0.8573 | 59.9% | 71.6% | 4/5 |
| `router-haiku-opus` | 10/10 | 128s | $0.6428 | $0.6426 | $0.8573 | -0.0% | 25.0% | 5/5 |

Model mix on the routed arms:

- `router-3tier`: sonnet-5 x10
- `router-haiku-opus`: opus-5 x8

Judge ranking, pass 1: `control-opus` > `router-haiku-opus` > `router-3tier`
Judge ranking, pass 2: `control-opus` > `router-haiku-opus` > `router-3tier`

Pairwise wins where both orderings agreed: `control-opus` 2, `router-3tier` 0, `router-haiku-opus` 1. Disagreements are counted as ties, not results.

## Aggregate

| Arm | Runs | Fully working builds | Total cost | Total measured Opus | Measured saving | Mean quality | Mean wall |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `control-opus` | 9 | 9/9 | $4.3873 | $4.4401 | 1.2% | 4.33 | 100s |
| `router-3tier` | 9 | 9/9 | $1.5655 | $4.4401 | 64.7% | 3.67 | 64s |
| `router-haiku-opus` | 9 | 8/9 | $4.9179 | $4.4401 | -10.8% | 4.00 | 124s |

## How far the dashboard overstates

Perceived saving against measured saving, per routed run:

| Run | Perceived saving | Measured saving | Overstated by |
| --- | --- | --- | --- |
| `datasci` / `router-3tier` | 60.0% | 39.0% | 21.0 points |
| `datasci` / `router-3tier` | 60.0% | 74.4% | -14.4 points |
| `datasci` / `router-3tier` | 59.9% | 71.6% | -11.7 points |
| `datasci` / `router-haiku-opus` | -0.0% | -42.3% | 42.3 points |
| `datasci` / `router-haiku-opus` | -0.0% | -104.2% | 104.2 points |
| `datasci` / `router-haiku-opus` | -0.0% | 25.0% | -25.0 points |
| `pelican` / `router-3tier` | 59.9% | 77.9% | -18.0 points |
| `pelican` / `router-3tier` | 59.9% | 75.5% | -15.6 points |
| `pelican` / `router-3tier` | 59.9% | 74.8% | -14.9 points |
| `pelican` / `router-haiku-opus` | -0.1% | -48.0% | 47.9 points |
| `pelican` / `router-haiku-opus` | -0.0% | 8.0% | -8.0 points |
| `pelican` / `router-haiku-opus` | -0.1% | 23.9% | -24.0 points |
| `todo` / `router-3tier` | 59.9% | 58.9% | 1.0 points |
| `todo` / `router-3tier` | 59.9% | 61.1% | -1.2 points |
| `todo` / `router-3tier` | 59.9% | 58.4% | 1.5 points |
| `todo` / `router-haiku-opus` | 79.9% | 80.4% | -0.5 points |
| `todo` / `router-haiku-opus` | 79.9% | 71.5% | 8.4 points |
| `todo` / `router-haiku-opus` | 79.8% | 81.4% | -1.6 points |

## Classifier

- 64 calls across 18 routed runs
- total classifier spend $0.0053
- median latency 270 ms

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
- Judging cost $2.1685, excluded from every figure above.
