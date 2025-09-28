from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Optional

from dotenv import load_dotenv


class ConfigManager:
    """Utility class for loading environment configuration."""

    def __init__(self, env_path: str | os.PathLike[str] = ".env") -> None:
        self.env_path = Path(env_path)
        self._config: Dict[str, Optional[str]] = {}

    def load(self) -> Dict[str, Optional[str]]:
        """Load environment configuration from the provided .env file."""

        load_dotenv(dotenv_path=self.env_path, override=False)
        self._config = {
            "GDELT_PROJECT_ID": os.getenv("GDELT_PROJECT_ID"),
            "GOOGLE_CLOUD_PROJECT": os.getenv("GOOGLE_CLOUD_PROJECT"),
            "GOOGLE_APPLICATION_CREDENTIALS": os.getenv("GOOGLE_APPLICATION_CREDENTIALS"),
            "PERPLEXITY_API_KEY": os.getenv("PERPLEXITY_API_KEY"),
            "GOOGLE_CUSTOMSEARCH_API_KEY": os.getenv("GOOGLE_CUSTOMSEARCH_API_KEY"),
            "GOOGLE_CUSTOMSEARCH_CX_KEY": os.getenv("GOOGLE_CUSTOMSEARCH_CX_KEY"),
            "OPENAI_API_KEY": os.getenv("OPENAI_API_KEY"),
            "DATABASE_URL": os.getenv("DATABASE_URL", "sqlite:///cyber_events.db"),
        }
        return self._config.copy()

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Retrieve a configuration value with an optional default."""

        if not self._config:
            self.load()
        return self._config.get(key, default)
