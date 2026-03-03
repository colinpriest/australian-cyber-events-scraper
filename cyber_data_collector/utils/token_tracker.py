"""Centralised API token usage and cost tracker.

Import the module-level ``tracker`` singleton from anywhere in the pipeline
and call ``tracker.record(...)`` after each API call.  At the end of a run,
call ``tracker.log_report()`` for a human-readable summary.

Usage::

    from cyber_data_collector.utils.token_tracker import tracker

    # After an OpenAI / Perplexity API call:
    tracker.record("gpt-4o", response.usage.prompt_tokens,
                   response.usage.completion_tokens, context="enrichment")

    # At the end of the pipeline:
    tracker.log_report()
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Dict

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pricing per 1M tokens (USD) — updated March 2026
# ---------------------------------------------------------------------------
PRICING: Dict[str, Dict[str, float]] = {
    # OpenAI models
    "gpt-4o":       {"input": 2.50, "output": 10.00},
    "gpt-4o-mini":  {"input": 0.15, "output":  0.60},
    "gpt-3.5-turbo": {"input": 0.50, "output":  1.50},
    # Perplexity models
    "sonar-pro":    {"input": 3.00, "output": 15.00},
    "sonar":        {"input": 1.00, "output":  1.00},
}


@dataclass
class _ModelUsage:
    """Accumulator for a single model."""
    input_tokens: int = 0
    output_tokens: int = 0
    api_calls: int = 0


class TokenTracker:
    """Thread-safe tracker for API token usage across a pipeline run."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._usage: Dict[str, _ModelUsage] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record(
        self,
        model: str,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        context: str = "",
    ) -> None:
        """Record token usage for a single API call.

        Silently skips if token counts are ``None`` (e.g. usage data
        unavailable from the API response).
        """
        inp = input_tokens or 0
        out = output_tokens or 0
        if inp == 0 and out == 0:
            return

        with self._lock:
            if model not in self._usage:
                self._usage[model] = _ModelUsage()
            entry = self._usage[model]
            entry.input_tokens += inp
            entry.output_tokens += out
            entry.api_calls += 1

        if context:
            logger.debug(
                "Token usage [%s] %s: %s in / %s out",
                context, model, f"{inp:,}", f"{out:,}",
            )

    def report(self) -> Dict:
        """Return a summary dict of usage and costs."""
        with self._lock:
            models = {}
            total_input = 0
            total_output = 0
            total_cost = 0.0
            total_calls = 0

            for model, usage in sorted(self._usage.items()):
                prices = PRICING.get(model, {"input": 0.0, "output": 0.0})
                cost = (
                    usage.input_tokens * prices["input"] / 1_000_000
                    + usage.output_tokens * prices["output"] / 1_000_000
                )
                models[model] = {
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "api_calls": usage.api_calls,
                    "cost_usd": round(cost, 6),
                }
                total_input += usage.input_tokens
                total_output += usage.output_tokens
                total_cost += cost
                total_calls += usage.api_calls

            return {
                "models": models,
                "total_input_tokens": total_input,
                "total_output_tokens": total_output,
                "total_tokens": total_input + total_output,
                "total_cost_usd": round(total_cost, 4),
                "total_api_calls": total_calls,
            }

    def log_report(self) -> None:
        """Log a human-readable usage report at INFO level."""
        data = self.report()
        if not data["models"]:
            logger.info("No API token usage recorded for this run.")
            return

        lines = [
            "",
            "=" * 60,
            " API Token Usage & Cost Report",
            "=" * 60,
        ]
        for model, info in data["models"].items():
            lines.append(
                f"  {model:16s}  {info['input_tokens']:>10,} in / "
                f"{info['output_tokens']:>8,} out  "
                f"({info['api_calls']:>3} calls)  "
                f"= ${info['cost_usd']:.4f}"
            )
        lines.append("-" * 60)
        lines.append(
            f"  {'TOTAL':16s}  {data['total_input_tokens']:>10,} in / "
            f"{data['total_output_tokens']:>8,} out  "
            f"({data['total_api_calls']:>3} calls)  "
            f"= ${data['total_cost_usd']:.4f}"
        )
        lines.append("=" * 60)

        logger.info("\n".join(lines))

    def reset(self) -> None:
        """Reset all counters for a new pipeline run."""
        with self._lock:
            self._usage.clear()


# Module-level singleton
tracker = TokenTracker()
