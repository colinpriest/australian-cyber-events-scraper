"""Error types for failed fetches, and whether each is worth retrying.

A failure is recorded with its type, not only its message, wherever a
source-month or a record failure is stored (``SourceMonthDiscovery``,
``DiscoveryRecordState``, ``CyberDailyRefetches``), so retry policy and
monitoring can act on it:

==============  =============================================  ======================
Type            What happened                                  Class / retried?
==============  =============================================  ======================
``throttled``   HTTP 429 / 503: the site asked us to slow down  temporary, backed off
``missing``     HTTP 404 / 410: the page is gone                permanent, never again
``network``     timeout, connection error, other 5xx            temporary, backed off
``parse``       the answer was not the feed (malformed XML,     temporary, backed off
                a challenge page, an empty or endless feed)
``incomplete``  a page was fetched but is cut off or too        temporary, backed off
                short to judge
``other``       a refusal that is not a missing page (another   unknown, long back-off
                4xx: 400, 401, 403 ...)
``unknown``     a failure nothing recognised                    unknown, long back-off
==============  =============================================  ======================

Three classes (:func:`failure_class`) decide what happens to a source-month:

* **permanent** (``missing``): the thing asked for does not exist. Given up at
  once, never searched again by a probe or a configuration change, and
  reported until a person releases it.
* **temporary** (the provider or the moment failed): backed off (30 minutes
  doubling to a day); after repeated failures the month is *parked*.
* **unknown** (not recognised as either): not assumed permanent. Backed off
  from 6 hours doubling to a week, then parked.

A parked month is not searched on its own, but it is not abandoned: the source
is probed (one month, after a week, then every 2 and 4 weeks while the probe
fails) and a successful probe reopens every parked month of that source. A
parked month also reopens when the configuration it failed under changes (the
source's API key, query version or client version; see
:mod:`~cyber_data_collector.processing.source_config`). ``parse`` is retried
because the usual cause is a challenge or error page served instead of the
feed, which passes; one that persists means the feed changed, and is reported.
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
UNKNOWN = "unknown"

ERROR_TYPES = (THROTTLED, MISSING, NETWORK, PARSE, INCOMPLETE, OTHER, UNKNOWN)
#: Never retried: the thing asked for does not exist.
PERMANENT_TYPES = frozenset({MISSING})
#: Retried with back-off: the provider or the moment failed, not the request.
TEMPORARY_TYPES = frozenset({THROTTLED, NETWORK, PARSE, INCOMPLETE})
#: Not recognised as temporary or permanent: retried with a long back-off, never assumed permanent.
UNKNOWN_TYPES = frozenset({OTHER, UNKNOWN})

#: The three classes of failure (:func:`failure_class`).
CLASS_PERMANENT = "permanent"
CLASS_TEMPORARY = "temporary"
CLASS_UNKNOWN = "unknown"

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


def failure_class(error_type: Optional[str]) -> str:
    """``'permanent'``, ``'temporary'`` or ``'unknown'`` for an error type.

    A type this module does not know (an old stored value, a typo) is
    ``'unknown'``: never presumed permanent, never presumed to pass.
    """
    if error_type in PERMANENT_TYPES:
        return CLASS_PERMANENT
    if error_type in TEMPORARY_TYPES:
        return CLASS_TEMPORARY
    return CLASS_UNKNOWN


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
    """The error type a stored or logged message describes (``'unknown'`` when it says nothing)."""
    lowered = (text or "").lower()
    status = _status_in_text(text or "")
    if status is not None:
        return _from_status(status)
    for error_type, fragments in ((THROTTLED, _THROTTLED_TEXT), (PARSE, _PARSE_TEXT),
                                  (INCOMPLETE, _INCOMPLETE_TEXT), (NETWORK, _NETWORK_TEXT),
                                  (MISSING, _MISSING_TEXT)):
        if any(fragment in lowered for fragment in fragments):
            return error_type
    return UNKNOWN


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
    types = [t if t in ERROR_TYPES else UNKNOWN for t in error_types]
    if not types:
        return None
    permanent = [t for t in types if t in PERMANENT_TYPES]
    if permanent:
        return permanent[0]
    return max(ERROR_TYPES, key=lambda t: (types.count(t), -ERROR_TYPES.index(t)))
