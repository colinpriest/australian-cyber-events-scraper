"""Which language models the pipeline uses, and how each is called.

Every LLM call in the project takes its model from here, so a replication
needs to know only these three settings - and every stored verdict records the
model that produced it.

Models are parameters, not constants:

* ``OPENAI_MODEL``      - judgments that need the strongest model
  (adjudication, partitions, risk classification, vision extraction);
* ``OPENAI_FAST_MODEL`` - high-volume, simpler passes (filtering, page and
  entity classification, summaries);
* ``TYPESAFE_MODEL``    - Jev, for bounded judgments that return
  probabilities (see :mod:`cyber_data_collector.utils.jev`).

Each may be set in ``.env`` or with the ``--openai-model`` /
``--openai-fast-model`` / ``--jev-model`` options of ``run_full_pipeline.py``
and ``scripts/dedup_admin.py`` (:func:`add_model_arguments`). The defaults are
**dated snapshots**, never aliases: ``gpt-5.5`` or ``jev-latest`` can change
underneath the code, a dated snapshot cannot. Values are read at call time, so
an option parsed after import still applies everywhere.

GPT-4o and GPT-4o-mini were the models until 2026-10; GPT-4o-mini is withdrawn
in January 2027.
"""

from __future__ import annotations

import argparse
import os
from typing import Any, Dict, Optional

DEFAULT_OPENAI_MODEL = "gpt-5.5-2026-04-23"
DEFAULT_OPENAI_FAST_MODEL = "gpt-5.4-mini-2026-03-17"
DEFAULT_TYPESAFE_MODEL = "jev-1.14.0"


def openai_model() -> str:
    """The strong OpenAI model (``OPENAI_MODEL``)."""
    return os.getenv("OPENAI_MODEL") or DEFAULT_OPENAI_MODEL


def openai_fast_model() -> str:
    """The fast OpenAI model (``OPENAI_FAST_MODEL``)."""
    return os.getenv("OPENAI_FAST_MODEL") or DEFAULT_OPENAI_FAST_MODEL


def typesafe_model() -> str:
    """The Jev model (``TYPESAFE_MODEL``)."""
    return os.getenv("TYPESAFE_MODEL") or DEFAULT_TYPESAFE_MODEL


def chat_params(model: str, max_tokens: Optional[int] = None) -> Dict[str, Any]:
    """Keyword arguments for ``chat.completions.create`` on ``model``.

    GPT-5 models reject ``max_tokens`` (it is ``max_completion_tokens``) and
    accept ``temperature=0`` only with ``reasoning_effort="none"``. Both are
    set here so call sites need not know: no hidden reasoning, and the most
    repeatable sampling the model allows. GPT-4-era models get the parameters
    they understand, so an older snapshot can still be passed for comparison.
    """
    params: Dict[str, Any] = {"model": model, "temperature": 0.0}
    if model.startswith(("gpt-5", "o1", "o3", "o4")):
        params["reasoning_effort"] = "none"
        if max_tokens is not None:
            params["max_completion_tokens"] = max_tokens
    elif max_tokens is not None:
        params["max_tokens"] = max_tokens
    return params


def add_model_arguments(parser: argparse.ArgumentParser) -> None:
    """Add ``--openai-model``, ``--openai-fast-model`` and ``--jev-model``."""
    parser.add_argument("--openai-model", default=None,
                        help=f"Strong OpenAI model (default: $OPENAI_MODEL or {DEFAULT_OPENAI_MODEL})")
    parser.add_argument("--openai-fast-model", default=None,
                        help=f"Fast OpenAI model (default: $OPENAI_FAST_MODEL or {DEFAULT_OPENAI_FAST_MODEL})")
    parser.add_argument("--jev-model", default=None,
                        help=f"TypeSafe Jev model (default: $TYPESAFE_MODEL or {DEFAULT_TYPESAFE_MODEL})")


def apply_model_arguments(args: argparse.Namespace) -> None:
    """Make the parsed model options visible to every module."""
    for attr, var in (("openai_model", "OPENAI_MODEL"), ("openai_fast_model", "OPENAI_FAST_MODEL"),
                      ("jev_model", "TYPESAFE_MODEL")):
        value = getattr(args, attr, None)
        if value:
            os.environ[var] = value


def describe() -> str:
    """One line naming the models in effect, for logs and reports."""
    return (f"OpenAI={openai_model()} fast={openai_fast_model()} Jev={typesafe_model()}")
