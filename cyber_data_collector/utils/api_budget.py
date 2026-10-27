"""Per-run request budgets for the paid APIs, enforced before each request leaves.

Token tracking reports spend after the fact, and the GPT-5 snapshots have no
listed price, so a dollar cap could not be enforced. A request cap can: every
OpenAI, Perplexity and TypeSafe call - from any thread, including SDK retries
and fallback searches - passes through ``httpx``, ``httpx2`` or ``requests``,
and ``install`` hooks their ``send`` so each request to a budgeted host first
reserves one unit. When a provider's budget is spent the request is refused
with :class:`BudgetExhausted` before anything is sent.

Unfinished work is not lost: every paid step stores what it has done (records
enriched, judgements in ``ModelJudgements``, timelines, corroborations) and
picks up the rest on the next run. Code that counts failed attempts must not
count a refusal - see :func:`is_budget_refusal`.

Limits come from ``API_BUDGET_OPENAI`` / ``API_BUDGET_PERPLEXITY`` /
``API_BUDGET_TYPESAFE`` (requests per run; unset or 0 = unlimited) or
``--api-budget openai=2000 perplexity=800``.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Dict, Iterable, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

HOSTS = {
    "api.openai.com": "openai",
    "api.perplexity.ai": "perplexity",
    "api.typesafe.ai": "typesafe",
}
PROVIDERS = tuple(sorted(set(HOSTS.values())))


class BudgetExhausted(RuntimeError):
    """A paid request was refused because its provider's run budget is spent."""


def is_budget_refusal(exc: BaseException) -> bool:
    """True when ``exc`` (or anything it wraps) is a budget refusal, not a real failure."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, BudgetExhausted) or "API budget exhausted" in str(exc):
            return True
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return False


class ApiBudget:
    """Thread-safe request counters with optional per-provider limits."""

    def __init__(self, limits: Optional[Dict[str, Optional[int]]] = None) -> None:
        self._lock = threading.Lock()
        self.limits: Dict[str, Optional[int]] = {p: None for p in PROVIDERS}
        self.used: Dict[str, int] = {p: 0 for p in PROVIDERS}
        self.refused: Dict[str, int] = {p: 0 for p in PROVIDERS}
        self.set_limits(limits or {})

    def set_limits(self, limits: Dict[str, Optional[int]]) -> None:
        with self._lock:
            for provider, limit in limits.items():
                if provider not in self.limits:
                    raise ValueError(f"unknown provider {provider!r}; expected one of {PROVIDERS}")
                self.limits[provider] = limit if limit else None

    def reserve(self, provider: str) -> None:
        """Take one request from ``provider``'s budget, or raise BudgetExhausted."""
        with self._lock:
            limit = self.limits.get(provider)
            if limit is not None and self.used[provider] >= limit:
                self.refused[provider] += 1
                first = self.refused[provider] == 1
            else:
                self.used[provider] += 1
                return
        if first:
            logger.warning("API budget exhausted for %s (%d requests); further %s calls are refused "
                           "and left for the next run", provider, limit, provider)
        raise BudgetExhausted(f"API budget exhausted for {provider} ({limit} requests this run)")

    def exhausted(self) -> Dict[str, int]:
        """Providers that refused requests this run, with how many."""
        return {p: n for p, n in self.refused.items() if n}

    def summary(self) -> str:
        parts = []
        for p in PROVIDERS:
            limit = self.limits[p]
            parts.append(f"{p} {self.used[p]}" + (f"/{limit}" if limit else "")
                         + (f" ({self.refused[p]} refused)" if self.refused[p] else ""))
        return "API requests: " + ", ".join(parts)

    def reset(self) -> None:
        with self._lock:
            self.used = {p: 0 for p in PROVIDERS}
            self.refused = {p: 0 for p in PROVIDERS}


budget = ApiBudget()
_installed = False
_install_lock = threading.Lock()


def provider_for(url: object) -> Optional[str]:
    host = (urlparse(str(url)).hostname or "").lower()
    return HOSTS.get(host)


def _guard(url: object) -> None:
    provider = provider_for(url)
    if provider:
        budget.reserve(provider)


def install() -> None:
    """Hook the HTTP libraries' send methods (idempotent)."""
    global _installed
    with _install_lock:
        if _installed:
            return
        for module_name in ("httpx", "httpx2"):
            try:
                module = __import__(module_name)
            except ImportError:
                continue
            for cls_name in ("Client", "AsyncClient"):
                cls = getattr(module, cls_name, None)
                if cls is not None:
                    _wrap_send(cls, is_async=cls_name == "AsyncClient")
        try:
            import requests
            _wrap_send(requests.Session, is_async=False)
        except ImportError:
            pass
        _installed = True


def _wrap_send(cls: type, is_async: bool) -> None:
    original = cls.send
    if getattr(original, "_api_budget_wrapped", False):
        return
    if is_async:
        async def send(self, request, *args, **kwargs):
            _guard(request.url)
            return await original(self, request, *args, **kwargs)
    else:
        def send(self, request, *args, **kwargs):
            _guard(request.url)
            return original(self, request, *args, **kwargs)
    send._api_budget_wrapped = True
    cls.send = send


def limits_from_env() -> Dict[str, Optional[int]]:
    limits: Dict[str, Optional[int]] = {}
    for provider in PROVIDERS:
        value = os.getenv(f"API_BUDGET_{provider.upper()}")
        if value:
            limits[provider] = int(value)
    return limits


def parse_limits(items: Iterable[str]) -> Dict[str, Optional[int]]:
    """``["openai=2000", "perplexity=800"]`` -> limits."""
    limits: Dict[str, Optional[int]] = {}
    for item in items:
        provider, _, value = item.partition("=")
        limits[provider.strip().lower()] = int(value)
    return limits


def configure(cli_items: Optional[Iterable[str]] = None) -> ApiBudget:
    """Set limits from the environment, then the command line, and install the hooks."""
    budget.set_limits(limits_from_env())
    if cli_items:
        budget.set_limits(parse_limits(cli_items))
    install()
    return budget


def ensure_configured() -> ApiBudget:
    """Configure from the environment unless this process already has a budget.

    For entry points that can also run inside the pipeline (``dedup_admin``):
    called in-process they keep the pipeline's limits and counts.
    """
    if not _installed:
        configure()
    return budget
