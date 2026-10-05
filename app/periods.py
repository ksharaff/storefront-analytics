"""Turn a dashboard period ("today", "previous_month", ...) into UTC ranges.

Every metric is "something about orders between start and end". This module
decides what start and end mean, once, so no endpoint re-derives it:

  * Periods are defined in the REPORTING timezone (Asia/Riyadh): "today" is
    the Riyadh calendar day, not the UTC one. The database stores UTC, so the
    boundaries are converted to UTC here and every query compares UTC to UTC.
  * Every range is half-open: start <= t < end. Adjacent periods never
    double-count an order placed exactly at midnight.
  * Each period also carries the PREVIOUS period it is compared against, and
    whether that comparison is possible at all (see `comparison_available`).

How the previous period is chosen
---------------------------------
"To-date" periods (today, current month, this year) are still running, so
comparing them against a whole previous period would be unfair: September 1–23
against all 31 days of August always looks like a drop. They are compared
against the SAME ELAPSED SPAN of the previous period instead: today until now
vs yesterday until the same clock time, Sept 1–23 vs Aug 1–23, and so on.

Completed periods (yesterday, previous month, last 7 days, a custom range)
are compared against the period of the same kind immediately before them.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.config import HISTORY_START, REPORT_TIMEZONE

# On Windows, ZoneInfo needs the `tzdata` package (in requirements.txt):
# Windows has no system timezone database for Python to read.
TZ = ZoneInfo(REPORT_TIMEZONE)

# The first moment we hold data for. The backfill imports from UTC midnight on
# HISTORY_START, so this is UTC — NOT Riyadh midnight, which is 3 hours
# earlier. A Riyadh period starting on 2026-01-01 therefore begins 3 hours
# before our data does; `complete` reports that honestly instead of hiding it.
DATA_START = datetime.strptime(HISTORY_START, "%Y-%m-%d").replace(tzinfo=timezone.utc)

PERIODS = ("today", "yesterday", "last_7_days", "current_month",
           "previous_month", "this_year", "custom")


@dataclass(frozen=True)
class Range:
    """A half-open UTC time range [start, end)."""
    start: datetime
    end: datetime

    def as_dict(self) -> dict:
        # Returned to the frontend in both zones: UTC is what was queried,
        # local is what a person reading the dashboard expects to see.
        return {
            "start_utc": self.start.isoformat(),
            "end_utc": self.end.isoformat(),
            "start_local": self.start.astimezone(TZ).isoformat(),
            "end_local": self.end.astimezone(TZ).isoformat(),
        }


@dataclass(frozen=True)
class Period:
    name: str
    current: Range
    previous: Range

    @property
    def complete(self) -> bool:
        """False if the current period starts before our data does, so its
        totals are missing orders (e.g. the first 3 Riyadh hours of 2026)."""
        return self.current.start >= DATA_START

    @property
    def comparison_available(self) -> bool:
        """The previous period must lie entirely inside imported data.
        A partially-covered previous period would make every % change look
        like growth, which is worse than showing no comparison at all."""
        return self.previous.start >= DATA_START

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "timezone": REPORT_TIMEZONE,
            "current": self.current.as_dict(),
            "previous": self.previous.as_dict(),
            "complete": self.complete,
            "comparison_available": self.comparison_available,
        }


# --- helpers -----------------------------------------------------------------

def _local_midnight(d: date) -> datetime:
    """Midnight at the start of day `d` in the reporting zone, as UTC."""
    return datetime.combine(d, time.min, tzinfo=TZ).astimezone(timezone.utc)


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _add_months(d: date, months: int) -> date:
    """First day of the month `months` away from d's month (negative = back)."""
    index = d.year * 12 + (d.month - 1) + months
    return date(index // 12, index % 12 + 1, 1)


def _same_span(prev_start: datetime, elapsed: timedelta, cap: datetime) -> Range:
    """[prev_start, prev_start + elapsed), never running past `cap`.
    The cap matters on month boundaries: March 1–31 elapsed is longer than
    all of February, and the comparison must not spill into March."""
    return Range(prev_start, min(prev_start + elapsed, cap))


# --- the one public function ------------------------------------------------

def resolve(name: str, now: datetime | None = None,
            start: date | None = None, end: date | None = None) -> Period:
    """Build the Period for `name`. `now` is injectable for tests.

    For "custom", `start` and `end` are local calendar dates, both INCLUSIVE —
    that is how a person picks a range in a date picker.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    today = now.astimezone(TZ).date()          # today's date in Riyadh
    today_start = _local_midnight(today)

    if name == "today":
        cur = Range(today_start, now)
        prev_start = _local_midnight(today - timedelta(days=1))
        prev = _same_span(prev_start, now - today_start, today_start)

    elif name == "yesterday":
        cur = Range(_local_midnight(today - timedelta(days=1)), today_start)
        prev = Range(_local_midnight(today - timedelta(days=2)), cur.start)

    elif name == "last_7_days":
        # The 7 COMPLETE days before today, so the number does not wobble as
        # today's orders trickle in. (Same convention as Google Analytics.)
        cur = Range(_local_midnight(today - timedelta(days=7)), today_start)
        prev = Range(_local_midnight(today - timedelta(days=14)), cur.start)

    elif name == "current_month":
        month_start = _local_midnight(_month_start(today))
        cur = Range(month_start, now)
        prev_start = _local_midnight(_add_months(today, -1))
        prev = _same_span(prev_start, now - month_start, month_start)

    elif name == "previous_month":
        cur = Range(_local_midnight(_add_months(today, -1)),
                    _local_midnight(_month_start(today)))
        prev = Range(_local_midnight(_add_months(today, -2)), cur.start)

    elif name == "this_year":
        year_start = _local_midnight(date(today.year, 1, 1))
        cur = Range(year_start, now)
        prev_start = _local_midnight(date(today.year - 1, 1, 1))
        prev = _same_span(prev_start, now - year_start, year_start)

    elif name == "custom":
        if start is None or end is None:
            raise ValueError("custom period needs both start and end dates")
        if end < start:
            raise ValueError("end date is before start date")
        cur = Range(_local_midnight(start), _local_midnight(end + timedelta(days=1)))
        length = cur.end - cur.start
        prev = Range(cur.start - length, cur.start)

    else:
        raise ValueError(f"unknown period {name!r}; expected one of {PERIODS}")

    return Period(name, cur, prev)
