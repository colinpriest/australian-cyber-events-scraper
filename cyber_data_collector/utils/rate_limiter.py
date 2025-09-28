from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from typing import DefaultDict, Dict, List, Optional


class RateLimiter:
    """Rate limiting for multiple APIs."""

    def __init__(self) -> None:
        self.limits: Dict[str, Dict[str, float]] = {
            "gdelt": {"per_minute": 60, "per_second": 1},
            "perplexity": {"per_minute": 50, "per_second": 1},
            "google_search": {"per_minute": 100, "per_second": 10},
            "webber": {"per_minute": 30, "per_second": 0.5},
            "openai": {"per_minute": 200, "per_second": 5},
        }
        self.request_history: DefaultDict[str, List[float]] = defaultdict(list)
        self.locks: DefaultDict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def set_limit(self, service: str, per_minute: Optional[float] = None, per_second: Optional[float] = None) -> None:
        limit = self.limits.setdefault(service, {"per_minute": 60, "per_second": 1})
        if per_minute is not None:
            limit["per_minute"] = per_minute
        if per_second is not None:
            limit["per_second"] = per_second

    async def wait(self, service: str) -> None:
        """Wait if necessary to respect rate limits."""

        async with self.locks[service]:
            now = time.time()
            history = self.request_history[service]

            # Clean old requests beyond 60 seconds
            history[:] = [req_time for req_time in history if now - req_time < 60]

            limit = self.limits.get(service)
            if not limit:
                history.append(now)
                return

            # Check per-minute limit
            if len(history) >= limit["per_minute"]:
                sleep_time = 60 - (now - history[0])
                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)
                    return await self.wait(service)

            # Check per-second limit
            recent_requests = [req_time for req_time in history if now - req_time < 1]
            if len(recent_requests) >= limit["per_second"]:
                await asyncio.sleep(1)
                return await self.wait(service)

            history.append(now)
