"""Error types for failed fetches, and whether each is worth retrying.

A failure is recorded with its type, not only its message, wherever a
source-month or a record failure is stored (``SourceMonthDiscovery``,
``DiscoveryRecordState``, ``CyberDailyRefetches``), so retry policy and
monitoring can act on it:

==============  =============================================  ==============
Type            What happened                                  Retried?
==============  =============================================  ==============
``throttled``   HTTP 429 / 503: the site asked us to slow down  yes, backed off
``missing``     HTTP 404 / 410: the page is gone                no - reported
``network``     timeout, connection error, other 5xx            yes, backed off
``parse``       the answer was not the feed (malformed XML,     yes, backed off
                a challenge page, an empty or endless feed)
``incomplete``  a page was fetched but is cut off or too        yes, backed off
                short to judge
``other``       anything else (another 4xx, an unknown error)   yes, backed off
==============  =============================================  ==============

Every retried type has a cap; a failure that reaches it is given up and
reported like a ``missing`` one. ``parse`` is retried because the usual cause
is a challenge or error page served instead of the feed, which passes; one
that persists to the cap means the feed changed, and is reported.
"""
from __future__ import annotations

import asyncio
import re
from typing import Iterable, Optional

THROTTLED = "throttled"
MISSING = "missing"
NETWORK = "network"
PARSE = "parse"
INCOMPLETE = "incomplete"
OTHER = "other"

ERROR_TYPES = (THROTTLED, MISSING, NETWORK, PARSE, INCOMPLETE, OTHER)
#: Never retried: the thing asked for does not exist.
PERMANENT_TYPES = frozenset({MISSING})
#: Retried with back-off and a cap: the provider or the moment failed, not the request.
TEMPORARY_TYPES = frozenset({THROTTLED, NETWORK, PARSE, INCOMPLETE, OTHER})

_THROTTLED_STATUS = {429, 503}
_MISSING_STATUS = {404, 410}
_NETWORK_NAMES = {
    "Timeout", "TimeoutError", "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout", "TimeoutException",
    "ConnectionError", "ConnectError", "ConnectionResetError", "ConnectionAbortedError", "ConnectionRefusedError",
    "BrokenPipeError", "RemoteProtocolError", "ReadError", "NetworkError", "ChunkedEncodingError", "SSLError",
    "APITimeoutError", "APIConnectionError",
}
_PARSE_NAMES = {"ParseError", "XMLSyntaxError", "JSONDecodeError"}
#: "HTTP 404", "429 Client Error: ...", or a message that starts with the status.
_STATUS_LEADING = re.compile(r"^\s*(?:HTTP\s*)?([45]\d\d)\b|\bHTTP\s*([45]\d\d)\b|\b([45]\d\d)\s+(?:Client|Server)\s+Error",
                             re.IGNORECASE)
_THROTTLED_TEXT = ("too many requests", "rate limit", "service unavailable", "throttl", "quota")
_MISSING_TEXT = ("not found", "gone")
_NETWORK_TEXT = ("timed out", "timeout", "connection reset", "connection aborted", "connection refused",
                 "name resolution", "temporarily", "bad gateway", "gateway timeout", "network")
_PARSE_TEXT = ("did not parse", "not an rss feed", "feed item without", "unparseable", "no utc offset",
               "returned no items", "not finished after", "malformed")
_INCOMPLETE_TEXT = ("too short", "cut off", "insufficient content", "no text extracted", "block page")


def is_permanent(error_type: Optional[str]) -> bool:
    return error_type in PERMANENT_TYPES


def _from_status(status: int) -> str:
    if status in _THROTTLED_STATUS:
        return THROTTLED
    if status in _MISSING_STATUS:
        return MISSING
    if status >= 500:
        return NETWORK
    return OTHER


def _status_in_text(text: str) -> Optional[int]:
    match = _STATUS_LEADING.search(text or "")
    if not match:
        return None
    return int(next(group for group in match.groups() if group))


def error_type_from_text(text: Optional[str]) -> str:
    """The error type a stored or logged message describes (``'other'`` when it says nothing)."""
    lowered = (text or "").lower()
    status = _status_in_text(text or "")
    if status is not None:
        return _from_status(status)
    for error_type, fragments in ((THROTTLED, _THROTTLED_TEXT), (PARSE, _PARSE_TEXT),
                                  (INCOMPLETE, _INCOMPLETE_TEXT), (NETWORK, _NETWORK_TEXT),
                                  (MISSING, _MISSING_TEXT)):
        if any(fragment in lowered for fragment in fragments):
            return error_type
    return OTHER


def classify_error(exc: BaseException) -> str:
    """The error type of an exception, following its cause chain.

    An explicit ``error_type`` attribute (set by a data source that knows what
    failed) wins; then an HTTP status on the exception or its response; then
    the exception class; then its message.
    """
    seen = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        explicit = getattr(current, "error_type", None)
        if explicit in ERROR_TYPES:
            return explicit
        status = getattr(current, "status_code", None)
        if status is None:
            status = getattr(getattr(current, "response", None), "status_code", None)
        if isinstance(status, int) and status >= 400:
            return _from_status(status)
        names = {cls.__name__ for cls in type(current).__mro__}
        if isinstance(current, (asyncio.TimeoutError, TimeoutError, ConnectionError)) or names & _NETWORK_NAMES:
            return NETWORK
        if names & _PARSE_NAMES:
            return PARSE
        current = current.__cause__ or current.__context__
    return error_type_from_text(str(exc))


def deciding_type(error_types: Iterable[Optional[str]]) -> Optional[str]:
    """The type that decides what happens to a source-month with these failures.

    A permanent failure decides (searching again cannot recover it); otherwise
    the most frequent type, ties broken in :data:`ERROR_TYPES` order.
    """
    types = [t if t in ERROR_TYPES else OTHER for t in error_types]
    if not types:
        return None
    permanent = [t for t in types if t in PERMANENT_TYPES]
    if permanent:
        return permanent[0]
    return max(ERROR_TYPES, key=lambda t: (types.count(t), -ERROR_TYPES.index(t)))
