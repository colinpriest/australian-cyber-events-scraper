"""Put untrusted text into generated HTML safely.

Dashboards embed database content - scraped titles, organisation names,
descriptions, URLs - into pages people open and share. Two embeddings are not
made safe by escaping cell text later:

- JSON inside ``<script>``: ``json.dumps`` leaves ``</script>`` intact, so a
  title containing it ends the script and whatever follows runs as page code.
- URLs in ``href``: a ``javascript:`` URL runs when clicked, escaped or not.
"""

from __future__ import annotations

import json
from typing import Any, Optional
from urllib.parse import urlparse

_SCRIPT_UNSAFE = {
    "<": "\\u003c",
    ">": "\\u003e",
    "&": "\\u0026",
    " ": "\\u2028",   # line separators end a JavaScript string literal in older engines
    " ": "\\u2029",
}


def script_json(obj: Any, **kwargs: Any) -> str:
    """JSON that is safe inside a ``<script>`` element.

    The same value as ``json.dumps(obj, **kwargs)`` once parsed, with every
    character that could close the element or start markup written as a
    ``\\u`` escape.
    """
    text = json.dumps(obj, **kwargs)
    for char, escaped in _SCRIPT_UNSAFE.items():
        text = text.replace(char, escaped)
    return text


def safe_url(url: Optional[str]) -> Optional[str]:
    """The URL if it is http(s), else None - for use in ``href`` / ``src``."""
    if not url:
        return None
    candidate = str(url).strip()
    if urlparse(candidate).scheme.lower() in ("http", "https"):
        return candidate
    return None
