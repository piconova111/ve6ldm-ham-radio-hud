"""Upcoming big phone contests, worked out from each contest's date rule (times in UTC).

A "full weekend" is one where both the Saturday and the Sunday fall in the month.
"""
import calendar
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
WEEKEND = 48 * 60 - 1  # 00:00 Saturday to 23:59 Sunday, in minutes


def _full_weekend_saturdays(year, month):
    last_day = calendar.monthrange(year, month)[1]
    return [d for d in range(1, last_day) if calendar.weekday(year, month, d) == 5]


def _weekend(year, month, which, start_hour, minutes):
    start = datetime(year, month, _full_weekend_saturdays(year, month)[which], start_hour, tzinfo=UTC)
    return start, start + timedelta(minutes=minutes)


CONTESTS = [
    ("ARRL International DX Contest, phone", lambda y: _weekend(y, 3, 0, 0, WEEKEND),
     "You work stations outside Canada and the US."),
    ("CQ WPX Contest, phone", lambda y: _weekend(y, 3, -1, 0, WEEKEND),
     "Every new prefix counts, so unusual calls are in demand."),
    ("ARRL Field Day", lambda y: _weekend(y, 6, 3, 18, 27 * 60 - 1),
     "Portable stations set up all over North America."),
    ("RAC Canada Day Contest", lambda y: (datetime(y, 7, 1, tzinfo=UTC), datetime(y, 7, 1, 23, 59, tzinfo=UTC)),
     "Everyone is looking for Canadian stations today."),
    ("IARU HF World Championship", lambda y: _weekend(y, 7, 1, 12, 24 * 60 - 1),
     "Everyone works everyone, worldwide."),
    ("CQ World Wide DX Contest, phone", lambda y: _weekend(y, 10, -1, 0, WEEKEND),
     "Expect 20 m to be packed all weekend."),
    ("ARRL 10-Meter Contest", lambda y: _weekend(y, 12, 1, 0, WEEKEND),
     "All on 10 m, busy if the band is open."),
]


def upcoming(now=None, count=3):
    now = now or datetime.now(UTC)
    events = []
    for year in (now.year, now.year + 1):
        for name, rule, note in CONTESTS:
            start, end = rule(year)
            if end >= now:
                events.append((start, end, name, note))
    events.sort()
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return [{"name": name, "note": note, "start": start.strftime(fmt), "end": end.strftime(fmt),
             "on_now": start <= now <= end} for start, end, name, note in events[:count]]
