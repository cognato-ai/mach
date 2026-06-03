from __future__ import annotations

DEFAULT_CONFIG = {
    "enabled": True,
    "api_base_url": "https://api.cognatoai.com",
    "auto_session": True,
    "idle_timeout_sec": None,
    "commit_closes_session": False,
    "auto_tracking": True,
    "use_tui": True,
    "db_enabled": False,
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
}


def merge_config(raw_config: dict) -> dict:
    merged = dict(DEFAULT_CONFIG)
    merged.update(raw_config)
    return merged
