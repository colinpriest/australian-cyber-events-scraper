"""One retry policy for the fast OpenAI calls (classification, entity extraction), with timeouts that stop the request.

The classifier and the entity extractor used to wrap a synchronous instructor call in
``asyncio.wait_for(asyncio.to_thread(...), timeout=60)``. On timeout the await was abandoned
but the thread and its HTTP request kept running - holding a concurrency slot and spending
tokens - and the retry then started a second request on top. Instructor's ``max_retries=2``
(which, given an int, retries *any* exception) and the OpenAI SDK's own default of 2 retries
multiplied the attempts again, none of them aware of the others: one slow call could become up
to 18 requests.

Now each layer has one job:

* **The HTTP request carries its own timeout.** The client is made with an httpx timeout
  (:data:`REQUEST_TIMEOUT`) and ``max_retries=0``, so a request that does not answer is
  cancelled by httpx and raises ``openai.APITimeoutError``; the thread it ran in ends with it.
* **Instructor re-asks only on an invalid answer** (:func:`validation_retries`): a reply that
  does not validate against the response model is sent back once with the error. An API error is
  not instructor's to retry and propagates unwrapped.
* **One loop retries transient failures** (:func:`call_with_retries`): a timeout, a connection
  error, a rate limit or a 5xx, with bounded exponential back-off, at most :data:`MAX_ATTEMPTS`
  requests. Anything else (a 400, a validation failure, a bug) propagates at once. The last
  transient error is raised as it is, so the retry queue classes it as temporary
  (:func:`processing.retry_queue.classify_failure`) and the fetch-error types as ``network``.
"""

from __future__ import annotations

import asyncio
import logging
from json import JSONDecodeError
from typing import Any, Awaitable, Callable, Optional, TypeVar

import httpx
import openai
from pydantic import ValidationError

T = TypeVar("T")

#: Per-request HTTP timeout: connecting gets 10 s, every other phase (each read, write, pool wait) 60 s.
REQUEST_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
#: Requests per call, the first included, before a transient failure is raised.
MAX_ATTEMPTS = 3
#: Back-off before retry n (1-based) is ``BACKOFF_BASE_SECONDS * 2 ** (n - 1)``, capped at BACKOFF_MAX_SECONDS.
BACKOFF_BASE_SECONDS = 2.0
BACKOFF_MAX_SECONDS = 30.0
#: Requests instructor may make for one call when the answer does not validate (one re-ask).
VALIDATION_ATTEMPTS = 2


def openai_client(api_key: str, timeout: httpx.Timeout = REQUEST_TIMEOUT,
                  http_client: Optional[httpx.Client] = None) -> openai.OpenAI:
    """An OpenAI client whose requests time out at the HTTP level and are never retried by the SDK.

    ``http_client`` is for tests (an ``httpx.MockTransport``); the timeout applies to it as well.
    """
    return openai.OpenAI(api_key=api_key, timeout=timeout, max_retries=0, http_client=http_client)


def validation_retries(attempts: int = VALIDATION_ATTEMPTS):
    """Instructor's ``max_retries``: re-ask on an answer that does not validate, never on an API error.

    Given an int, instructor retries every exception - a timeout included - on top of the caller's
    own retries. A ``tenacity.Retrying`` limited to validation failures leaves API errors to
    :func:`call_with_retries`; ``reraise`` makes a final validation failure raise as itself.
    """
    from instructor.core.exceptions import ValidationError as InstructorValidationError
    from tenacity import Retrying, retry_if_exception_type, stop_after_attempt

    return Retrying(stop=stop_after_attempt(attempts), reraise=True,
                    retry=retry_if_exception_type((ValidationError, JSONDecodeError, InstructorValidationError)))


def is_transient(exc: BaseException) -> bool:
    """A failure that may succeed unchanged: a timeout, a connection error, a rate limit, a 5xx."""
    if isinstance(exc, (openai.APITimeoutError, openai.APIConnectionError, openai.RateLimitError)):
        return True
    return isinstance(exc, openai.APIStatusError) and exc.status_code >= 500


def backoff_seconds(retry: int) -> float:
    """The wait before retry ``retry`` (1 = the second request)."""
    return min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * 2 ** (retry - 1))


async def call_with_retries(call: Callable[[], T], *, what: str, logger: logging.Logger,
                            attempts: int = MAX_ATTEMPTS,
                            sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep) -> T:
    """Run the synchronous ``call`` in a thread, retrying transient failures; one request per attempt.

    The request's own HTTP timeout (:func:`openai_client`) ends a hung attempt, so nothing is
    abandoned while still running. A non-transient error is raised at once; the last transient one
    after ``attempts`` requests, unchanged.
    """
    last: Optional[BaseException] = None
    for attempt in range(1, attempts + 1):
        try:
            return await asyncio.to_thread(call)
        except Exception as exc:  # noqa: BLE001 - re-raised unless transient and attempts remain
            if not is_transient(exc):
                raise
            last = exc
            if attempt == attempts:
                logger.warning("%s failed after %d attempt(s): %s: %s", what, attempts, type(exc).__name__, exc)
                raise
            wait = backoff_seconds(attempt)
            logger.warning("%s: %s on attempt %d/%d (%s); retrying in %.0fs", what, type(exc).__name__, attempt,
                           attempts, exc, wait)
            await sleep(wait)
    assert last is not None   # unreachable: the loop returns or raises
    raise last
