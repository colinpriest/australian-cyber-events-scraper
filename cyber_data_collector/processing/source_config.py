"""What a source's search depends on, so a give-up can be reopened when it changes.

A source-month search that keeps failing is parked (:mod:`~cyber_data_collector.processing.retry_queue`).
Parking a month for good was the defect: a failure caused by an expired API
key, a request the code built wrongly, or a client library with a bug is
cured by changing that key, query or library - and nothing would ever look
again. So the configuration a source ran under is stored with the give-up, and
a month whose recorded configuration is no longer the current one is reopened
automatically.

The configuration of a source is a small mapping of named components to short
values:

* each **secret** the source reads (an API key, a search-engine id, a
  credentials path), stored as a truncated SHA-256 - the secret itself is
  never written to the database, only whether it changed;
* the source's **query version** (:data:`QUERY_VERSIONS`): bumped in this file
  whenever the search the source sends changes (its prompt, URL, parameters or
  parsing). It is the one component that needs a person to remember;
* the installed **client versions** (:data:`CLIENT_PACKAGES`) of the libraries
  the source calls through, read from the environment (``importlib.metadata``).

Components that cannot be read are recorded as ``unset`` / ``absent``, which is
itself a configuration (adding the key later changes it). A source not listed
in :data:`SOURCES` is recorded as ``unregistered``: it has no configuration to
change, so a configuration change never reopens it (a probe still can).
"""
from __future__ import annotations

import hashlib
import json
import os
from importlib import metadata
from typing import Dict, List, Mapping, Optional, Tuple

#: Environment variables each source reads (see the data sources and ConfigManager).
SOURCE_SECRETS: Dict[str, Tuple[str, ...]] = {
    "Perplexity": ("PERPLEXITY_API_KEY",),
    "GoogleSearch": ("GOOGLE_CUSTOMSEARCH_API_KEY", "GOOGLE_CUSTOMSEARCH_CX_KEY"),
    "GDELT": ("GOOGLE_CLOUD_PROJECT", "GOOGLE_APPLICATION_CREDENTIALS"),
    "WebberInsurance": (),
    "OAIC": (),
    "CyberDaily": (),
}

#: Bump a source's version when the search it sends or the way it reads the answer changes.
QUERY_VERSIONS: Dict[str, str] = {
    "Perplexity": "1",
    "GoogleSearch": "1",
    "GDELT": "1",
    "WebberInsurance": "1",
    "OAIC": "1",
    "CyberDaily": "1",
}

#: Distribution names of the libraries each source calls through.
CLIENT_PACKAGES: Dict[str, Tuple[str, ...]] = {
    "Perplexity": ("openai", "instructor"),
    "GoogleSearch": ("requests",),
    "GDELT": ("google-cloud-bigquery",),
    "WebberInsurance": ("requests", "beautifulsoup4"),
    "OAIC": ("requests", "beautifulsoup4", "playwright"),
    "CyberDaily": ("requests",),
}

SOURCES = tuple(SOURCE_SECRETS)
UNREGISTERED = "unregistered"
UNSET = "unset"
ABSENT = "absent"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


_DOTENV_LOADED = False


def _load_dotenv_once() -> None:
    """Read ``.env`` into the environment the way the pipeline does (``ConfigManager.load``), once.

    A command that only reports (``discovery_queue.py status`` / ``reopen``, the quality
    report) would otherwise see every key as unset and take that for a change.
    """
    global _DOTENV_LOADED
    if not _DOTENV_LOADED:
        from dotenv import load_dotenv

        load_dotenv(dotenv_path=".env", override=False)
        _DOTENV_LOADED = True


def current_components(source_type: str, environ: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """The configuration ``source_type`` would run under now, as named components.

    Without ``environ`` the process environment is used, after ``.env`` has been read.
    """
    if source_type not in SOURCE_SECRETS:
        return {"source": UNREGISTERED}
    if environ is None:
        _load_dotenv_once()
    env = os.environ if environ is None else environ
    components: Dict[str, str] = {}
    for name in SOURCE_SECRETS[source_type]:
        value = env.get(name)
        components[f"secret:{name}"] = _hash(value) if value else UNSET
    components["query"] = QUERY_VERSIONS[source_type]
    for package in CLIENT_PACKAGES[source_type]:
        try:
            components[f"client:{package}"] = metadata.version(package)
        except metadata.PackageNotFoundError:
            components[f"client:{package}"] = ABSENT
    return components


def encode(components: Mapping[str, str]) -> str:
    """The stored form of a configuration (stable JSON)."""
    return json.dumps(dict(components), sort_keys=True)


def decode(stored: Optional[str]) -> Optional[Dict[str, str]]:
    """A stored configuration, or None when none was recorded (a give-up from before this existed)."""
    if not stored:
        return None
    value = json.loads(stored)
    if not isinstance(value, dict):
        raise ValueError(f"stored source configuration is not a mapping: {stored[:80]!r}")
    return {str(k): str(v) for k, v in value.items()}


def changed_components(stored: Optional[Mapping[str, str]], current: Mapping[str, str]) -> List[str]:
    """Names of the components that differ between a stored configuration and the current one.

    A give-up with no recorded configuration differs in ``"(not recorded)"``:
    nothing can show the configuration is unchanged, so it is searched again once.
    """
    if stored is None:
        return ["(not recorded)"]
    if current.get("source") == UNREGISTERED:
        return [] if stored.get("source") == UNREGISTERED else ["source"]
    return sorted(name for name in set(stored) | set(current) if stored.get(name) != current.get(name))
