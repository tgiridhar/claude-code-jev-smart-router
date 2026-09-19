# Jev choice probability calibration

`jev-latest` answers a `choice` question with a probability distribution over the
supplied options. This report measures whether those probabilities predict
correctness, how stable they are, and which parts of the request change them.
About 450 API calls on 2026-09-18, total cost under five cents.

## Summary

1. **Accuracy was 95% on 80 labelled items.** Brier score 0.0379, expected
   calibration error 0.0407.
2. **Predictions above 0.95 were correct 99% of the time.** Predictions between
   0.70 and 0.85 were correct 40% of the time.
3. **Identical requests returned different probabilities**, standard deviation
   0.03. The selected option changed only when the top two options sat within
   about 0.06 of each other.
4. **Reordering the same options moved the probability by up to 0.31.** Items at
   1.00 did not move. Rewording the instruction changed nothing measurable.
5. **Confidence ranked correct answers above incorrect ones, AUC 0.962.** Three
   of the four errors were the same mistake, a protist filed as a plant or a
   fungus.

![Stated probability against observed accuracy across four probability bands](img/reliability.svg)

71 of the 80 predictions landed above 0.95, and that band was accurate to within
one point of its stated probability. The 0.70 to 0.85 band was overconfident by
37 points across 5 items.

![Spread across four option orderings plotted against item uncertainty](img/ordering.svg)

Listing the same options in a different order moved 7 of 12 items beyond the 0.06
repeat-call noise floor. The 3 items at 1.00 moved by exactly 0.000.
Correlation between uncertainty and spread was 0.464.

Single changes measured on one contested item, a calzone: renaming an option
label while keeping its description moved the probability by 0.24, adding a
competing option moved it by 0.18, and 5 paraphrases of the instruction moved
it by 0.05, which is inside the noise.

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
compared against the 0.06 noise floor that variation implies.

## Limits

- One model version, one day, 80 items.
- Only the band above 0.95 holds enough items to measure. The 0.70 to 0.85
  result rests on 5 predictions.
- The item sets were written to be adversarial, so 95% understates accuracy on
  ordinary inputs.
- Three of the four errors were protists, and the item descriptions never
  mentioned cell structure. Part of that failure is question design rather than
  model error. The fourth error was an axolotl called a fish.
