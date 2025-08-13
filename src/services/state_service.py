import os
import json
import shutil
import logging
from datetime import datetime

STATE_FILE = "state.json"
STATE_VERSION = 3
LOGGER = logging.getLogger("wireproxy_gui")

class StateService:
    def __init__(self, state_file=STATE_FILE):
        self.state_file = state_file
        self.state = self.load_state()

    def load_state(self):
        default_state = {
            "version": STATE_VERSION,
            "profiles": [],
            "port_limit": 10,
            "wireproxy_path": None,
            "proxy_type": "socks",
            "logging_enabled": True,
        }
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                
                if not isinstance(data, dict):
                    LOGGER.warning(f"{self.state_file} is not a dict. Using default state.")
                    return default_state

                data.setdefault("version", 0)
                if int(data.get("version", 0) or 0) < STATE_VERSION:
                    LOGGER.info("Detected old state.json version. Migrating…")
                    data = self.migrate_state(data)

                for k, v in default_state.items():
                    data.setdefault(k, v)

                for p in data.get("profiles", []):
                    p.setdefault("proxy_port", None)
                    p.setdefault("pid", None)
                    p.setdefault("running", False)
                    p.setdefault("conf_path", None)
                    p.setdefault("last_port", None)
                
                LOGGER.debug(f"State loaded: port_limit={data.get('port_limit')}, proxy_type={data.get('proxy_type')}, profiles={len(data.get('profiles', []))}")
                return data
            except Exception:
                LOGGER.exception(f"Error reading {self.state_file}. Using default state.")
                return default_state
        return default_state

    def save_state(self):
        try:
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
        except Exception:
            LOGGER.exception("Failed to save state.")

    def migrate_state(self, data: dict) -> dict:
        """Upgrade old state to new schema version. Backs up the current file before writing."""
        try:
            if os.path.exists(self.state_file):
                ts = datetime.now().strftime("%Y%m%d-%H%M%S")
                shutil.copy(self.state_file, f"{self.state_file}.bak-{ts}")
        except Exception:
            LOGGER.exception("Failed to create state backup during migration.")

        current = int(data.get("version", 0) or 0)
        
        while current < STATE_VERSION:
            if current < 1:
                current = 1
                continue
            if current < 2:
                data.setdefault("proxy_type", "socks")
                current = 2
                continue
            if current < 3:
                data.setdefault("logging_enabled", True)
                current = 3
                continue
            break

        data["version"] = STATE_VERSION
        try:
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            LOGGER.exception("Failed to save migrated state.")
        return data

    def get_state(self):
        return self.state

    def set_state(self, new_state):
        self.state = new_state
        self.save_state()
