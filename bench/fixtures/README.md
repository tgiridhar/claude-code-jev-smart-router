# Grading fixtures

**These files contain deliberate defects. Do not fix them.**

Each task seeds only its subject file into the run's working directory. The
answer key stays here, so the agent under test cannot read it.

| Directory | Seeded into the run | Answer key, never seeded |
| --- | --- | --- |
| `bugfind/` | `orders.py` | `planted.json`, 6 defects |
| `secfind/` | `app.py` | `planted.json`, 6 vulnerabilities |
| `algo/` | `spec.md` | `test_hidden.py`, 20 cases |

`secfind/app.py` contains a credential-shaped string. It is fabricated, has
never been valid anywhere, and is planted so a review has something real to
find. Same for the SQL injection, the path traversal, the IDOR, the timing-unsafe
comparison and the MD5 password hashing.

## Why these exist

The other tasks ask for something to be built and are scored by driving the
result. These are scored against ground truth that was written first, so the
score is a count with nothing subjective in it, and a weaker model can fail
outright rather than merely produce something rougher.

## Grading

`fr/findings.py` counts a defect as found only when the review names the
relevant symbol **and** a phrase specific to that defect class within the same
passage. Both halves matter. Calibration, which is worth re-running after any
edit to a keyword list:

```
a review restating each planted defect      6/6
a review naming every function and no bug   0/6
```

The keyword lists must describe the **defect**, not the surrounding code. An
early version listed bare `retry` for the double-charge bug, and a review that
merely discussed the retry loop scored a hit for a defect it never found.

`fr/algo.py` copies the candidate `schedules.py` next to `test_hidden.py` in a
temporary directory and runs pytest. Score is cases passed out of 20. The suite
is satisfiable: a reference solution passes all 20.
