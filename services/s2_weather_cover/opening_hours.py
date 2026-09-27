"""Is a building open? Just enough of OSM ``opening_hours`` to answer that.

The full grammar (holidays, week numbers, sunrise offsets) is large, and around
FIU only two buildings carry the tag at all -- ``24/7`` and ``06:30-22:00``. So
this reads the common shapes and returns ``None`` for anything else, which the
caller treats as "not tagged" and answers with the default window:

    24/7
    06:30-22:00                       every day
    Mo-Fr 07:00-22:00; Sa 08:00-17:00 day ranges, ``;``-separated rules
    Mo,We 09:00-12:00,13:00-17:00     day lists, several spans
    Su off                            closed that day

Later rules override earlier ones for the days they name, as in OSM.
"""

from __future__ import annotations

import re
from datetime import datetime, time

_DAYS = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")
_SPAN = re.compile(r"^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$")


def _days(spec: str) -> set[int] | None:
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            if a not in _DAYS or b not in _DAYS:
                return None
            i, j = _DAYS.index(a), _DAYS.index(b)
            out.update(range(i, j + 1) if i <= j else [*range(i, 7), *range(0, j + 1)])
        elif part in _DAYS:
            out.add(_DAYS.index(part))
        else:
            return None
    return out


def _spans(spec: str) -> list[tuple[time, time]] | None:
    """``"07:00-22:00,23:00-24:00"`` -> spans. ``24:00`` means end of day."""
    out = []
    for part in spec.split(","):
        m = _SPAN.match(part.strip())
        if not m:
            return None
        h1, m1, h2, m2 = (int(g) for g in m.groups())
        if h1 > 23 or h2 > 24 or m1 > 59 or m2 > 59:
            return None
        end = time(23, 59, 59) if (h2, m2) == (24, 0) else time(h2, m2)
        out.append((time(h1, m1), end))
    return out


def is_open(spec: str | None, local: datetime) -> bool | None:
    """Open at ``local`` (a local wall-clock time), or None if unparseable."""
    if not spec or not spec.strip():
        return None
    spec = spec.strip()
    if spec == "24/7":
        return True

    week: dict[int, list[tuple[time, time]]] = {}
    for rule in spec.split(";"):
        rule = rule.strip()
        if not rule:
            continue
        head, _, tail = rule.partition(" ")
        if _SPAN.match(head.split(",")[0]):
            days, body = set(range(7)), rule  # no day selector: every day
        else:
            days, body = _days(head), tail.strip()
            if days is None:
                return None
        if body == "off" or body == "closed":
            spans: list[tuple[time, time]] = []
        else:
            parsed = _spans(body)
            if parsed is None:
                return None
            spans = parsed
        for d in days:
            week[d] = spans

    now = local.time()
    for start, end in week.get(local.weekday(), []):
        if start <= end:
            if start <= now < end or (end == time(23, 59, 59) and now >= start):
                return True
        elif now >= start or now < end:  # span crosses midnight
            return True
    return False


def in_default_window(local: datetime, window: tuple[str, str]) -> bool:
    """The fallback for untagged buildings: open between ``window`` times."""
    (h1, m1), (h2, m2) = (tuple(int(p) for p in t.split(":")) for t in window)
    return time(h1, m1) <= local.time() < time(h2, m2)


__all__ = ["is_open", "in_default_window"]
