# Jev choice probability calibration

`jev-latest` answers a `choice` question with a probability distribution over the
options you supply. If you are automating a decision, that probability is what
decides whether a case is handled or sent to a person, so it needs to mean what
it says. This report measures whether it does. 80 labelled items with textbook
ground truth, about 450 API calls, 2026-09-18, under five cents.

## What a call looks like

Three real items from the set, same question, same four options.

```
"A large tree rooted in soil that grows from an acorn and photosynthesises."
  -> plant 1.00   fungus 0.00   animal 0.00   neither 0.00      correct

"A long brown seaweed anchored to the sea bed that photosynthesises."
  -> plant 0.75   neither 0.25  animal 0.00   fungus 0.00       WRONG, kelp is a chromist

"An orange threadlike vine with almost no chlorophyll that wraps around
 host plants and draws nutrients from them."
  -> plant 0.44   fungus 0.43   neither 0.13  animal 0.00       coin flip
  -> fungus 0.47  plant 0.42    neither 0.11  animal 0.00       same request, later call
```

The oak is the easy case. Kelp is the dangerous one: confidently wrong, and 0.75
is high enough that a naive threshold would have shipped it.

Dodder is a genuine coin flip and the model says so. The two lines above are the
same request sent twice. Over 20 identical calls it answered `fungus` 16 times
and `plant` 4 times, so the correct answer is the one it usually misses. Both the
instability and the near-tie are visible in the distribution before the answer is
used for anything.

Kelp reads 0.70 later in this report. Same request, a different call, same
effect.

## Findings

**1. Accuracy was 95% on 80 labelled items.** Brier score 0.0379, expected
calibration error 0.0407.

> The four misses were kelp and a diatom called plants, a water mould called a
> fungus, and an axolotl called a fish. All four are cases where the obvious
> surface feature points the wrong way: kelp photosynthesises, a water mould
> grows threads, an axolotl has gills.
> **Ideal:** raw accuracy matters less than whether the model flags its own
> mistakes, which is finding 5. 95% with a reliable warning beats 98% without.

**2. Predictions above 0.95 were correct 99% of the time. Predictions between
0.70 and 0.85 were correct 40% of the time.**

![Stated probability against observed accuracy across four probability bands](img/reliability.svg)

> Every item in that band, in order: kelp 0.70 wrong, diatom 0.77 wrong, slime
> mould 0.78 right, euglena 0.79 right, axolotl 0.79 wrong. The obvious
> threshold, act when the model is 70% sure, would have shipped all three of
> those errors.
> **Ideal:** a curve that rises the whole way, so raising the bar is always
> safer. This one dips in the middle, which means you cannot pick a threshold by
> intuition. Measure it on your own data before wiring it to anything.

**3. Identical requests returned different probabilities**, standard deviation
0.03. The selected option changed only when the top two options sat within about
0.06 of each other.

> Send the same case twice and you get a different number. The dodder above sits
> at plant 0.44 against fungus 0.43. Across 20 byte-identical calls it answered
> `fungus` 16 times and `plant` 4 times. A rule like "escalate below 0.75" fires
> inconsistently for anything parked near 0.75.
> **Ideal:** a seed parameter for reproducible runs. Until then keep thresholds
> at least 0.1 clear of where your traffic clusters, and never treat the
> probability as a stable value to cache, diff or key on.

**4. Reordering the same options moved the probability by up to 0.31.** Items at
1.00 did not move. Rewording the instruction changed nothing measurable.

![Spread across four option orderings plotted against item uncertainty](img/ordering.svg)

> The worst case was a calzone, asked whether it is a sandwich, wrap, taco,
> pastry or none of these. Across four orderings of those same five options its
> probability of `pastry` read 0.75, 0.57, 0.67 and 0.77. If the criteria map is
> built from a database query, a Python set, or JSON from a source you do not
> control, that order can change between deploys. Nothing errors and no test
> fails.
> **Ideal:** order invariance. Until then, freeze the option order as part of the
> prompt version and re-validate when it changes, like any other config.

**5. Confidence ranked correct answers above incorrect ones, AUC 0.962.** Three
of the four errors were the same mistake: a protist filed as a plant or a fungus.

> Confidence is a good triage signal. Reviewing the 11% of cases that fell below
> 0.95 catches three of the four errors. But kelp, the diatom and the water mould
> are one mistake repeated: a protist that looks like a plant or a fungus. A
> random 10% audit would probably see one of them and call it bad luck. An audit
> grouped by predicted category shows three in a row.
> **Ideal:** errors spread randomly, which is what sampling assumes. Clustered
> errors mean one blind spot can take out an entire class of input.

## Choosing a threshold

Every case above the threshold is handled automatically; everything below goes to
a person.

| Act if P >= | Handled automatically | Accuracy on those | Sent to a person | Wrong answers shipped |
| --- | --- | --- | --- | --- |
| 0.99 | 75% | **100%** | 25% | **0** |
| 0.95 | 89% | 98.6% | 11% | 1 |
| 0.85 | 92% | 98.6% | 8% | 1 |
| 0.70 | 99% | 94.9% | 1% | 4 |

At 0.99 this model automated three decisions in four and got all of them right.
Dropping the bar to 0.95 buys 14 more points of volume for one wrong answer in
71. Below 0.85 the extra volume is paid for entirely in errors.

The one error above 0.95 was a water mould called a fungus at 0.96. Oomycetes
grow threads and absorb nutrients exactly like fungi, so no threshold would have
caught it. That is the residual risk a confidence gate does not remove.

## Method

Three labelled sets with textbook ground truth, written so that intuition often
points the wrong way: 35 organisms by kingdom, 22 substances by element, compound
or mixture, and 23 vertebrates by class. Each item was sent alone, one question
per request.

```json
{"model": "jev-latest",
 "state": "A long brown seaweed anchored to the sea bed that photosynthesises.",
 "questions": {"x": {"type": "choice",
   "instructions": "Which kingdom does this organism belong to?",
   "criteria": {"animal": "...", "plant": "...", "fungus": "...", "neither": "..."}}}}
```

Repeat variation came from 20 byte-identical requests per item. Sensitivity came
from changing one element of the request at a time, 3 repeats per condition,
measured against the 0.06 noise floor that variation implies.

## Limits

- One model version, one day, 80 items.
- Only the band above 0.95 holds enough items to measure. The 0.70 to 0.85
  result rests on 5 predictions.
- The item sets were written to be adversarial, so 95% understates accuracy on
  ordinary inputs, and the threshold table is correspondingly pessimistic.
- These are classification items with clean answers. A business decision with
  vague categories and overlapping options will behave worse, not better.
