# Does the confidence number mean anything?

`jev-latest` is a classification model from TypeSafe. You hand it some text and a
list of options; it hands back a probability for each option, and they sum to 1.

Software uses that probability to decide who handles a case. Above some cutoff
the computer acts on its own; below it, a person looks. Pick the cutoff too low
and wrong answers go out unchecked. Pick it too high and people do work the
machine could have done. So the cutoff is only as good as the probability, and
the probability is only useful if 0.75 really does mean right about three times
in four.

This report tests that on 80 questions with known answers. About 450 API calls
on 2026-09-18, costing under five cents in total.

## The test

Every call sends the same question and the same four options. Only the
description of the organism changes.

```json
{"model": "jev-latest",
 "state": "A long brown seaweed anchored to the sea bed that photosynthesises.",
 "questions": {"kingdom": {
    "type": "choice",
    "instructions": "Which kingdom does this organism belong to?",
    "criteria": {
      "animal":  "A multicellular organism that ingests other organisms and cannot photosynthesise",
      "plant":   "An organism that makes its own food by photosynthesis",
      "fungus":  "An organism that absorbs nutrients from its surroundings and does not photosynthesise",
      "neither": "Fits none of the above categories"}}}}
```

Three of the 80 descriptions, and what came back:

```
"A large tree rooted in soil that grows from an acorn and photosynthesises."
  -> plant 1.00   fungus 0.00   animal 0.00   neither 0.00
     Right. An oak is a plant and nothing else is close.

"A long brown seaweed anchored to the sea bed that photosynthesises."
  -> plant 0.75   neither 0.25  animal 0.00   fungus 0.00
     Wrong. Kelp is not a plant; it is more closely related to diatoms.
     Nothing in the answer warns you, and 0.75 clears a cutoff of 0.7.

"An orange threadlike vine with almost no chlorophyll that wraps around
 host plants and draws nutrients from them."
  -> plant 0.44   fungus 0.43   neither 0.13  animal 0.00
  -> fungus 0.47  plant 0.42    neither 0.11  animal 0.00
     The same request, sent twice. Dodder is a plant. The top two options
     are nearly tied, so the answer moves between calls.
```

## Findings

**1. It got 76 of the 80 right, which is 95%.**

> The four misses are in the table below. Each is a case where the obvious
> surface feature points the wrong way.
> **Ideal:** raw accuracy matters less than whether the model warns you when it
> is about to be wrong, which is finding 5. 95% with a reliable warning is worth
> more than 98% without one.

**2. When it reported above 0.95 it was right 99% of the time. When it reported
between 0.70 and 0.85 it was right 40% of the time.**

![Stated probability against observed accuracy across four probability bands](img/reliability.svg)

> The five items in that 0.70 to 0.85 band: kelp 0.70 wrong, diatom 0.77 wrong,
> slime mould 0.78 right, euglena 0.79 right, axolotl 0.79 wrong. A cutoff of 0.7
> would have let all three errors through.
> **Ideal:** accuracy that climbs steadily with the stated probability, so
> raising the cutoff is always safer. This one dips in the middle, so you cannot
> reason your way to a cutoff. Measure it on your own data.

**3. The same request sent twice returns different numbers**, varying by about
0.03. The chosen option only changes when the top two options are within about
0.06 of each other.

> Dodder, above, sits at plant 0.44 against fungus 0.43, a gap of 0.01. Across 20
> byte-identical calls it answered `fungus` 16 times and `plant` 4 times. A rule
> that escalates below 0.75 will fire inconsistently on anything sitting near
> 0.75.
> **Ideal:** an option to make runs repeatable. Until then, keep cutoffs at least
> 0.1 away from where your cases pile up, and do not store the probability
> anywhere that assumes it is fixed.

**4. Listing the same options in a different order changes the probability by up
to 0.31.** Rewording the question changes nothing measurable.

![Spread across four option orderings plotted against item uncertainty](img/ordering.svg)

> Tested on 12 items, using a separate food set as well as the organisms. The
> biggest swing was an open-faced tuna melt, asked whether it is a sandwich,
> wrap, taco, pastry or none of these: across four orderings of those same five
> options its probability of `none of these` read 0.91, 0.60, 0.84, 0.91. Seven
> of the 12 items moved more than run-to-run noise. The three items sitting at
> 1.00 did not move at all.
> **Ideal:** the order should not matter. Until it does not, treat the option
> order as part of the configuration: pin it, and re-test when it changes. Code
> that builds the option list from a database query or an unordered collection
> can reorder it between deployments, and nothing will error.

**5. Take one answer it got right and one it got wrong. The right one carries
the higher probability in 96% of such pairs.**

> That makes the probability a usable filter. Nine of the 80 cases came back
> below 0.95, and reviewing just those nine catches three of the four errors.
> The water mould is the one that escapes, at 0.96.
> Separately, three of the four errors are the same mistake. Kelp, the diatom
> and the water mould are all organisms that are none of animal, plant or fungus,
> and all three were filed as a plant or a fungus. Reviewing a random 10% would
> show one of them and read it as bad luck. Grouping the output by what the model
> answered shows three in a row.
> **Ideal:** errors scattered at random, which is what spot-checking assumes. A
> cluster means one blind spot can take out a whole class of input at once.

## The four errors

| Item | What it actually is | Model said | Correct answer | Probability | In the 9 reviewed |
| --- | --- | --- | --- | --- | --- |
| kelp | a large brown seaweed | plant | neither | 0.70 | yes |
| diatom | a single cell inside a glass-like shell | plant | neither | 0.77 | yes |
| axolotl | a salamander that keeps its gills for life | fish | amphibian | 0.79 | yes |
| water mould | a thread-growing organism that rots potatoes | fungus | neither | **0.96** | no |

Kelp, the diatom and the water mould belong to none of the three kingdoms
offered, and each was filed under the kingdom its appearance suggests. The water
mould is the dangerous one: at 0.96 it sits above any cutoff you would plausibly
set, so no threshold catches it.

One of the 76 correct answers deserves an asterisk. Dodder was scored right, but
across 20 identical calls it gave the right answer 4 times. Had the run drawn one
of the other 16, this report would say 75 of 80.

## Choosing a cutoff

Cases at or above the cutoff are handled automatically. Everything below goes to
a person.

| Cutoff | Handled automatically | Accuracy on those | Sent to a person | Wrong answers shipped |
| --- | --- | --- | --- | --- |
| 0.99 | 75% | **100%** | 25% | **0** |
| 0.95 | 89% | 98.6% | 11% | 1 |
| 0.85 | 92% | 98.6% | 8% | 1 |
| 0.70 | 99% | 94.9% | 1% | 4 |

At 0.99 the model handled three cases in four and got every one of them right.
Moving down to 0.95 covers 14 more cases out of 100 and lets one wrong answer
through. Moving from 0.85 to 0.70 covers 7 more and lets three more through, so
most of what that last step buys is errors.

## Method

Three sets of items with textbook answers, written so that the obvious feature
often points the wrong way: 35 organisms by kingdom, 22 substances by element,
compound or mixture, and 23 vertebrates by class. That is the 80. Each was sent
on its own, one question per request.

The option-order test in finding 4 used a separate set of 12 items, including
foods, because those produced more genuinely uncertain answers to move.

Run-to-run variation came from sending 20 byte-identical requests per item, which
gave a spread of about 0.03 and set 0.06 as the size below which a change means
nothing. Every sensitivity test changed one element of the request at a time, 3
repeats each, and was compared against that 0.06.

Two standard scores, for anyone who wants them.

Brier score 0.0379. This is the gap between the probability given to the correct
answer and a perfect 1.00, squared, averaged over all 80 items. Zero is perfect.
Spreading probability evenly across the options instead of choosing would score
0.55 here.

Expected calibration error 0.0407. This is the average distance between what a
band claimed and what it delivered, weighted by how many items sit in each band.
Zero means the stated probabilities match observed accuracy. It says nothing
about whether the answers are right, only about whether the confidence is
honest.

## Limits

- One model version, one day, 80 items.
- Only the band above 0.95 holds enough items to measure properly. It holds 71.
  The 0.70 to 0.85 result rests on 5 items, the 0.85 to 0.95 band on 3, and the
  band below 0.50 on 1.
- The items were written to be adversarial, so 95% understates accuracy on
  ordinary inputs and the cutoff table is correspondingly pessimistic.
- These are classification questions with clean, checkable answers. A real
  decision with vague categories and overlapping options will behave worse than
  this, not better.
