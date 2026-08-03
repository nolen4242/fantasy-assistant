"""Calendar semantics: the period math every module now derives from."""
from datetime import date

from fantasy_assistant.graph.refdata import (next_open_period, period_dates,
                                             period_for_date)


class TestPeriodMath:
    def test_opening_short_period(self):
        assert period_for_date(date(2026, 3, 25)) == 1
        assert period_for_date(date(2026, 3, 29)) == 1

    def test_regular_weeks(self):
        assert period_for_date(date(2026, 3, 30)) == 2   # Monday starts p2
        assert period_for_date(date(2026, 4, 5)) == 2    # Sunday ends p2
        assert period_for_date(date(2026, 4, 6)) == 3

    def test_dates_roundtrip(self):
        for n in (2, 10, 20, 27):
            start, end = period_dates(n)
            assert period_for_date(start) == n
            assert period_for_date(end) == n
            assert (end - start).days == 6

    def test_next_open_period_monday_is_current(self):
        monday = date(2026, 8, 3)
        assert monday.weekday() == 0
        assert next_open_period(monday) == period_for_date(monday)

    def test_next_open_period_midweek_is_next(self):
        for d in (date(2026, 8, 4), date(2026, 8, 8), date(2026, 8, 9)):
            assert next_open_period(d) == period_for_date(d) + 1
