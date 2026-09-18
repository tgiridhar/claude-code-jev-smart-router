"""Hidden grading suite for merge_schedules. Never shown to the author."""
import pytest
from schedules import merge_schedules

NY = "America/New_York"
UTC = "UTC"


def test_empty():
    assert merge_schedules([], NY) == []

def test_single():
    assert merge_schedules([("2026-01-05T09:00:00", "2026-01-05T10:00:00")], NY) == \
        [("2026-01-05T09:00:00", "2026-01-05T10:00:00")]

def test_simple_overlap():
    assert merge_schedules([("2026-01-05T09:00:00", "2026-01-05T10:30:00"),
                            ("2026-01-05T10:00:00", "2026-01-05T11:00:00")], NY) == \
        [("2026-01-05T09:00:00", "2026-01-05T11:00:00")]

def test_touching_merges():
    assert merge_schedules([("2026-01-05T09:00:00", "2026-01-05T10:00:00"),
                            ("2026-01-05T10:00:00", "2026-01-05T11:00:00")], NY) == \
        [("2026-01-05T09:00:00", "2026-01-05T11:00:00")]

def test_gap_does_not_merge():
    assert merge_schedules([("2026-01-05T09:00:00", "2026-01-05T10:00:00"),
                            ("2026-01-05T10:00:01", "2026-01-05T11:00:00")], NY) == \
        [("2026-01-05T09:00:00", "2026-01-05T10:00:00"),
         ("2026-01-05T10:00:01", "2026-01-05T11:00:00")]

def test_unsorted_input():
    assert merge_schedules([("2026-01-05T14:00:00", "2026-01-05T15:00:00"),
                            ("2026-01-05T09:00:00", "2026-01-05T10:00:00"),
                            ("2026-01-05T11:00:00", "2026-01-05T12:00:00")], NY) == \
        [("2026-01-05T09:00:00", "2026-01-05T10:00:00"),
         ("2026-01-05T11:00:00", "2026-01-05T12:00:00"),
         ("2026-01-05T14:00:00", "2026-01-05T15:00:00")]

def test_full_containment():
    assert merge_schedules([("2026-01-05T09:00:00", "2026-01-05T17:00:00"),
                            ("2026-01-05T11:00:00", "2026-01-05T12:00:00")], NY) == \
        [("2026-01-05T09:00:00", "2026-01-05T17:00:00")]

def test_identical_intervals():
    a = ("2026-01-05T09:00:00", "2026-01-05T10:00:00")
    assert merge_schedules([a, a, a], NY) == [a]

def test_chain_merges_transitively():
    assert merge_schedules([("2026-01-05T09:00:00", "2026-01-05T10:00:00"),
                            ("2026-01-05T09:30:00", "2026-01-05T11:00:00"),
                            ("2026-01-05T10:45:00", "2026-01-05T13:00:00")], NY) == \
        [("2026-01-05T09:00:00", "2026-01-05T13:00:00")]

def test_zero_duration_kept():
    assert merge_schedules([("2026-01-05T09:00:00", "2026-01-05T09:00:00")], NY) == \
        [("2026-01-05T09:00:00", "2026-01-05T09:00:00")]

def test_zero_duration_absorbed():
    assert merge_schedules([("2026-01-05T09:00:00", "2026-01-05T11:00:00"),
                            ("2026-01-05T10:00:00", "2026-01-05T10:00:00")], NY) == \
        [("2026-01-05T09:00:00", "2026-01-05T11:00:00")]

def test_zero_duration_touching_start():
    assert merge_schedules([("2026-01-05T09:00:00", "2026-01-05T09:00:00"),
                            ("2026-01-05T09:00:00", "2026-01-05T10:00:00")], NY) == \
        [("2026-01-05T09:00:00", "2026-01-05T10:00:00")]

def test_inverted_raises():
    with pytest.raises(ValueError):
        merge_schedules([("2026-01-05T11:00:00", "2026-01-05T10:00:00")], NY)

def test_inverted_raises_among_valid():
    with pytest.raises(ValueError):
        merge_schedules([("2026-01-05T09:00:00", "2026-01-05T10:00:00"),
                         ("2026-01-05T15:00:00", "2026-01-05T14:00:00")], NY)

def test_spring_forward_wall_gap_is_zero_real_gap():
    # 2026-03-08 in New York: 02:00 -> 03:00, so 02:00:00 does not exist and
    # resolves to the same instant as 03:00:00. The wall clock shows an hour
    # between these two, real time shows none, so they touch and merge.
    assert merge_schedules([("2026-03-08T00:00:00", "2026-03-08T02:00:00"),
                            ("2026-03-08T03:00:00", "2026-03-08T04:00:00")], NY) == \
        [("2026-03-08T00:00:00", "2026-03-08T04:00:00")]

def test_same_strings_do_not_merge_in_utc():
    # UTC has no transition, so the identical wall-clock strings are a real hour
    # apart and must not merge. Comparing strings instead of instants fails one
    # of these two tests whichever way it guesses.
    assert merge_schedules([("2026-03-08T00:00:00", "2026-03-08T02:00:00"),
                            ("2026-03-08T03:00:00", "2026-03-08T04:00:00")], UTC) == \
        [("2026-03-08T00:00:00", "2026-03-08T02:00:00"),
         ("2026-03-08T03:00:00", "2026-03-08T04:00:00")]

def test_fall_back_ambiguous_resolves_to_first():
    # 2026-11-01 in New York: 02:00 -> 01:00. 01:30 happens twice; take the first.
    # 00:30-01:30 (first) and 01:30(first)-02:30 therefore touch and merge.
    assert merge_schedules([("2026-11-01T00:30:00", "2026-11-01T01:30:00"),
                            ("2026-11-01T01:30:00", "2026-11-01T02:30:00")], NY) == \
        [("2026-11-01T00:30:00", "2026-11-01T02:30:00")]

def test_across_midnight():
    assert merge_schedules([("2026-01-05T23:00:00", "2026-01-06T01:00:00"),
                            ("2026-01-06T00:30:00", "2026-01-06T02:00:00")], NY) == \
        [("2026-01-05T23:00:00", "2026-01-06T02:00:00")]

def test_many_intervals_stay_sorted():
    out = merge_schedules([("2026-01-0%dT08:00:00" % d, "2026-01-0%dT09:00:00" % d)
                           for d in (5, 3, 7, 1, 9)], NY)
    assert out == sorted(out)
    assert len(out) == 5

def test_output_format_no_microseconds():
    out = merge_schedules([("2026-01-05T09:00:00", "2026-01-05T10:00:00")], NY)
    assert all("." not in s and "+" not in s and len(s) == 19 for iv in out for s in iv)
