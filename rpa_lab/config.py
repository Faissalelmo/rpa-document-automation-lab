import json
import os

from .paths import CONFIG


def load_config() -> dict:
    """Read config/settings.json; any key can be overridden with an env var RPA_<KEY> (like an Orchestrator asset)."""
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    cfg.pop("_comment", None)
    for key, value in list(cfg.items()):
        env = os.environ.get(f"RPA_{key.upper()}")
        if env is not None:
            cfg[key] = type(value)(env) if not isinstance(value, bool) else env.lower() == "true"
    return cfg
