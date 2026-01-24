import os
import tempfile
import unittest
from pathlib import Path

from cyber_data_collector.utils import ConfigManager


class ConfigManagerTests(unittest.TestCase):
    def setUp(self):
        self.env_path = Path(tempfile.mkstemp(suffix=".env")[1])
        self._original_db_url = os.environ.get("DATABASE_URL")

    def tearDown(self):
        if self._original_db_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = self._original_db_url
        if self.env_path.exists():
            self.env_path.unlink()

    def test_defaults_use_instance_db(self):
        manager = ConfigManager(self.env_path)
        config = manager.load()
        self.assertEqual(config["DATABASE_PATH"], "instance/cyber_events.db")

    def test_sqlite_url_is_resolved(self):
        os.environ["DATABASE_URL"] = "sqlite:///custom/path.db"
        manager = ConfigManager(self.env_path)
        config = manager.load()
        self.assertEqual(config["DATABASE_PATH"], "custom/path.db")


if __name__ == "__main__":
    unittest.main()
