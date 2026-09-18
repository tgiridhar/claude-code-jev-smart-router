# `merge_schedules`

Implement this in `schedules.py`. Signature:

```python
def merge_schedules(intervals: list[tuple[str, str]], tz: str) -> list[tuple[str, str]]:
```

## Input

`intervals` is a list of `(start, end)` pairs. Each is an ISO 8601 local
wall-clock timestamp with no offset, for example `"2026-03-08T01:30:00"`.

`tz` is an IANA timezone name, for example `"America/New_York"`. Interpret every
timestamp as local time in that zone. Use `zoneinfo` from the standard library.

## Output

The same intervals merged, as a list of `(start, end)` ISO 8601 local
wall-clock strings in the same format as the input, sorted ascending by start.

## Rules

1. Two intervals merge when they overlap **or touch**. `09:00-10:00` and
   `10:00-11:00` merge into `09:00-11:00`.
2. Input is not sorted. Do not assume it is.
3. An interval whose end equals its start has zero duration. Keep it, and merge
   it into any interval that contains or touches it.
4. An interval whose end is **before** its start is invalid: raise `ValueError`.
5. An empty input list returns an empty list.
6. Merging is by real elapsed time, not by wall-clock string. Two intervals that
   look adjacent as text may not be adjacent in real time, and vice versa,
   because of daylight saving transitions. Convert to absolute time to compare.
7. On a spring-forward transition some wall-clock times do not exist. On a
   fall-back transition some occur twice; resolve an ambiguous local time to the
   **first** (pre-transition) occurrence.
8. Output strings use `"%Y-%m-%dT%H:%M:%S"`, no offset, no microseconds.

## Example

```python
merge_schedules([("2026-01-05T10:00:00", "2026-01-05T11:00:00"),
                 ("2026-01-05T09:00:00", "2026-01-05T10:00:00")],
                "America/New_York")
# -> [("2026-01-05T09:00:00", "2026-01-05T11:00:00")]
```
