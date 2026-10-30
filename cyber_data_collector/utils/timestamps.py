"""Timestamps written to decision ledgers, and reading the ones written before they had an offset.

Ledger rows (``ModelJudgements``, ``VictimAttributionActions``, ``DedupOverrides`` ...) are written with
:func:`utc_now_iso`: UTC, second resolution, with an explicit ``+00:00`` offset, so a reader never has to guess
the zone. Older rows carry no offset and were written by two conventions - SQLite ``CURRENT_TIMESTAMP`` (UTC)
or Python ``datetime.now()`` (the writing machine's local time) - so :func:`to_utc` takes the convention to apply
to a stamp without an offset; ``dedup/input_history.py`` declares it per table.

Stamps of different conventions do not sort as strings (a local ``16:33`` written before a UTC ``06:40`` sorts
after it), so "the newest row" is the highest ``rowid``, never the largest stamp.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Optional, Union

NAIVE_UTC = "utc"
NAIVE_LOCAL = "local"

_STAMP = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})"
    r"(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d{1,9}))?)?)?"
    r"\s*(Z|[+-]\d{2}(?::?\d{2})?)?$", re.IGNORECASE)


def utc_now_iso() -> str:
    """Now, in UTC, as ``YYYY-MM-DDTHH:MM:SS+00:00`` - the one format every ledger writer uses."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_stamp(value: Union[str, date, datetime, None]) -> Optional[datetime]:
    """A stored stamp as a ``datetime``: aware when it carries an offset, naive when it does not.

    Accepts ``T`` or space separators, any fractional precision, ``Z`` and ``+HH:MM`` / ``+HHMM`` / ``+HH``
    offsets, and a bare date (midnight). Returns ``None`` for anything else (including ``None``), never a guess.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    match = _STAMP.match(str(value).strip())
    if not match:
        return None
    year, month, day, hour, minute, second, fraction, offset = match.groups()
    try:
        moment = datetime(int(year), int(month), int(day), int(hour or 0), int(minute or 0), int(second or 0),
                          int((fraction or "0")[:6].ljust(6, "0")))
    except ValueError:
        return None
    if offset is None:
        return moment
    if offset.upper() == "Z":
        return moment.replace(tzinfo=timezone.utc)
    digits = offset[1:].replace(":", "")
    delta = timedelta(hours=int(digits[:2]), minutes=int(digits[2:4] or 0))
    if delta >= timedelta(hours=24):
        return None
    return moment.replace(tzinfo=timezone(delta if offset[0] == "+" else -delta))


def to_utc(moment: datetime, naive_as: str, local_zone: Optional[tzinfo] = None) -> datetime:
    """``moment`` in UTC (aware). An explicit offset is honoured; a naive value is read as ``naive_as``.

    ``naive_as`` is :data:`NAIVE_UTC` or :data:`NAIVE_LOCAL`. Local time is converted with ``local_zone`` when
    given (a ``zoneinfo`` zone applies the rules for that date), else with the system's local-time rules for
    that date.
    """
    if moment.tzinfo is not None:
        return moment.astimezone(timezone.utc)
    if naive_as == NAIVE_UTC:
        return moment.replace(tzinfo=timezone.utc)
    if naive_as == NAIVE_LOCAL:
        if local_zone is not None:
            return moment.replace(tzinfo=local_zone).astimezone(timezone.utc)
        return moment.astimezone(timezone.utc)          # a naive datetime is taken as system local time
    raise ValueError(f"unknown convention for a stamp without an offset: {naive_as!r}")


def local_zone_label(local_zone: Optional[tzinfo] = None) -> str:
    """How local time is read, for reports: the zone given, or the system's with its offset now."""
    now = datetime.now(local_zone) if local_zone is not None else datetime.now().astimezone()
    offset = now.utcoffset() or timedelta(0)
    sign = "-" if offset < timedelta(0) else "+"
    minutes = abs(int(offset.total_seconds())) // 60
    name = str(local_zone) if local_zone is not None else (now.tzname() or "system local time")
    return f"{name} (UTC{sign}{minutes // 60:02d}:{minutes % 60:02d}{' now' if local_zone is None else ''})"
