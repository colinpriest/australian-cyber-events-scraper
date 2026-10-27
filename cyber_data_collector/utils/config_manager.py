from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Dict, Optional, Union
from urllib.parse import urlparse

from dotenv import load_dotenv

logger = logging.getLogger(__name__)


DEFAULT_DB_PATH = "instance/cyber_events.db"


def resolve_db_path(explicit: Optional[Union[str, os.PathLike]] = None,
                    env_path: Union[str, os.PathLike] = ".env") -> str:
    """The database every component of one run must use.

    An explicit path wins; otherwise ``DATABASE_URL`` (from the environment or
    ``.env``); otherwise the default. Resolve it once at the entry point and
    pass the result down - a component that resolved its own path is how
    ``rebuild --db-path scratch.db`` came to wipe the default database.
    """
    if explicit:
        return str(explicit)
    return ConfigManager(env_path).load().get("DATABASE_PATH") or DEFAULT_DB_PATH


class ConfigManager:
    """Utility class for loading environment configuration."""

    def __init__(self, env_path: Union[str, os.PathLike] = ".env") -> None:
        self.env_path = Path(env_path)
        self._config: Dict[str, Optional[str]] = {}

    def load(self) -> Dict[str, Optional[str]]:
        """Load environment configuration from the provided .env file."""

        load_dotenv(dotenv_path=self.env_path, override=False)
        database_url = os.getenv("DATABASE_URL", "sqlite:///instance/cyber_events.db")
        self._config = {
            "GDELT_PROJECT_ID": os.getenv("GDELT_PROJECT_ID"),
            "GOOGLE_CLOUD_PROJECT": os.getenv("GOOGLE_CLOUD_PROJECT"),
            "GOOGLE_APPLICATION_CREDENTIALS": os.getenv("GOOGLE_APPLICATION_CREDENTIALS"),
            "PERPLEXITY_API_KEY": os.getenv("PERPLEXITY_API_KEY"),
            "GOOGLE_CUSTOMSEARCH_API_KEY": os.getenv("GOOGLE_CUSTOMSEARCH_API_KEY"),
            "GOOGLE_CUSTOMSEARCH_CX_KEY": os.getenv("GOOGLE_CUSTOMSEARCH_CX_KEY"),
            "OPENAI_API_KEY": os.getenv("OPENAI_API_KEY"),
            "DATABASE_URL": database_url,
            "DATABASE_PATH": self._resolve_database_path(database_url),
        }

        if not self._config.get("OPENAI_API_KEY"):
            logger.warning("Required configuration key OPENAI_API_KEY is not set")
        if not self._config.get("PERPLEXITY_API_KEY"):
            logger.info("Recommended configuration key PERPLEXITY_API_KEY is not set")

        return self._config.copy()

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Retrieve a configuration value with an optional default."""

        if not self._config:
            self.load()
        return self._config.get(key, default)

    @staticmethod
    def _resolve_database_path(database_url: Optional[str]) -> str:
        """Resolve a filesystem path from a database URL or path string."""

        if not database_url:
            return "instance/cyber_events.db"

        if database_url.startswith("sqlite:///"):
            return database_url.replace("sqlite:///", "", 1)

        if database_url.startswith("sqlite://"):
            return database_url.replace("sqlite://", "", 1)

        parsed = urlparse(database_url)
        # A drive letter ("D:/data/x.db") parses as a one-letter scheme.
        if parsed.scheme and len(parsed.scheme) > 1:
            # Never fall back to the default database: an operation meant for
            # another database would silently act on this one.
            raise ValueError(f"Unsupported DATABASE_URL {database_url!r}: only sqlite:/// URLs "
                             "or file paths are supported")

        return database_url
