import datetime as dt

from fptracker.dates import anchors_for_year, resolve_trip_instances, snap_to_weekday

D = dt.date

TRIP_A = {"id": "a", "year": "next",
          "return": {"anchor": "ash_wednesday", "offset_days": 1},
          "outbound": {"from": "return", "offset_months": -1, "weekday": "WED", "snap": "nearest"}}
TRIP_B = {"id": "b", "year": "next",
          "outbound": {"anchor": "carnival_tuesday", "offset_days": -6},
          "return": {"from": "outbound", "offset_months": 1, "weekday": "WED", "snap": "nearest"}}


def test_carnival_anchors_known_years():
    assert anchors_for_year(2027)["carnival_tuesday"] == D(2027, 2, 9)
    assert anchors_for_year(2028)["carnival_tuesday"] == D(2028, 2, 29)
    assert anchors_for_year(2026)["ash_wednesday"] == D(2026, 2, 18)


def test_snap():
    assert snap_to_weekday(D(2027, 1, 11), "WED") == D(2027, 1, 13)       # Mon -> Wed (2 fwd vs 5 back)
    assert snap_to_weekday(D(2027, 1, 15), "WED") == D(2027, 1, 13)       # Fri -> Wed (2 back)
    assert snap_to_weekday(D(2027, 1, 13), "WED", "next") == D(2027, 1, 13)
    assert snap_to_weekday(D(2027, 1, 14), "WED", "previous") == D(2027, 1, 13)


def test_trips_2027():
    [(y, out, ret)] = resolve_trip_instances(TRIP_A, D(2026, 11, 9))
    assert (y, out, ret) == (2027, D(2027, 1, 13), D(2027, 2, 11))
    assert out.weekday() == 2 and ret.weekday() == 3
    [(y, out, ret)] = resolve_trip_instances(TRIP_B, D(2026, 11, 9))
    assert (y, out, ret) == (2027, D(2027, 2, 3), D(2027, 3, 3))
    assert out.weekday() == 2 and ret.weekday() == 2


def test_rolls_to_next_carnival_after_departure():
    [(y, out, _)] = resolve_trip_instances(TRIP_A, D(2027, 1, 13))  # min_days_ahead=1 -> 2027 trip dropped
    assert y == 2028 and out == D(2028, 2, 2)


def test_fixed_dates_and_expiry():
    trip = {"id": "x", "outbound": {"date": "2026-12-18"}, "return": {"date": "2027-01-08"}}
    assert resolve_trip_instances(trip, D(2026, 11, 9)) == [(None, D(2026, 12, 18), D(2027, 1, 8))]
    assert resolve_trip_instances(trip, D(2026, 12, 18)) == []
