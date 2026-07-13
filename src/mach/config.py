from __future__ import annotations

DEFAULT_RISK_CONFIG = {
    "enabled": True,
    # rule_id values to skip (built-in or custom)
    "disabled_rules": [],
    # Custom additions: [{pattern|patterns, severity?, rule_id?, explanation?}, ...]
    "extra_path_patterns": [],
    "extra_content_patterns": [],
    "extra_command_patterns": [],
}

DEFAULT_CONFIG = {
    "enabled": True,
    "api_base_url": "https://api.cognatoai.com",
    "auto_session": True,
    "idle_timeout_sec": None,
    "commit_closes_session": False,
    "auto_tracking": True,
    "use_tui": True,
    "hook_agents": ["claude", "codex", "copilot", "cursor", "gemini"],
    "poll_interval_sec": 2,
    "ignore_paths": [
        ".git",
        ".mach",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        "node_modules",
        "dist",
        "build",
    ],
    "store_content": ["input", "output", "reasoning", "tool"],
    "risk": dict(DEFAULT_RISK_CONFIG),
}

# Keys removed from the product; stripped on read so old configs stay clean.
_DEPRECATED_CONFIG_KEYS = frozenset({"db_enabled"})


def merge_config(raw_config: dict) -> dict:
    raw = dict(raw_config or {})
    raw_risk = raw.pop("risk", None)
    merged = dict(DEFAULT_CONFIG)
    merged.update(raw)
    risk = dict(DEFAULT_RISK_CONFIG)
    if isinstance(raw_risk, dict):
        risk.update(raw_risk)
    elif isinstance(merged.get("risk"), dict):
        # Defend against callers that put a partial risk dict under another path.
        risk.update(merged["risk"])
    # Normalize list fields so config edits never leave bare scalars.
    for key in ("disabled_rules", "extra_path_patterns", "extra_content_patterns", "extra_command_patterns"):
        value = risk.get(key)
        if value is None:
            risk[key] = []
        elif not isinstance(value, list):
            risk[key] = [value]
    risk["enabled"] = bool(risk.get("enabled", True))
    merged["risk"] = risk
    for key in _DEPRECATED_CONFIG_KEYS:
        merged.pop(key, None)
    return merged
