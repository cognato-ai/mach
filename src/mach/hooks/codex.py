from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from mach.hooks.base import HookAdapter, HookDispatchResult
from mach.hooks.helpers import command_name, first_present, merge_event_hooks, read_json_file, strip_matching_commands, write_json_file, extract_tool_details

CODEX_MARKER_START = "# BEGIN MACH CODEX HOOKS"
CODEX_MARKER_END = "# END MACH CODEX HOOKS"


class CodexHookAdapter(HookAdapter):
    name = "codex"
    support = "partial"
    notes = "Codex CLI currently exposes a narrower hook surface than Claude/Copilot."

    def __init__(self, repo_root: Path) -> None:
        super().__init__(repo_root)
        codex_home = Path(os.environ.get("MACH_CODEX_HOME", str(Path.home() / ".codex")))
        self.hooks_path = codex_home / "hooks.json"
        self.config_path = codex_home / "config.toml"

    def install(self) -> dict[str, Any]:
        command = command_name()
        hooks = strip_matching_commands(read_json_file(self.hooks_path), "mach hooks dispatch --agent codex")
        additions = {
            "SessionStart": [self._entry(f'{command} hooks dispatch --agent codex --event SessionStart')],
            "SessionEnd": [self._entry(f'{command} hooks dispatch --agent codex --event SessionEnd')],
            "UserPromptSubmit": [self._entry(f'{command} hooks dispatch --agent codex --event UserPromptSubmit')],
            "PreToolUse": [self._entry(f'{command} hooks dispatch --agent codex --event PreToolUse')],
            "PostToolUse": [self._entry(f'{command} hooks dispatch --agent codex --event PostToolUse')],
            "Stop": [self._entry(f'{command} hooks dispatch --agent codex --event Stop')],
        }
        write_json_file(self.hooks_path, merge_event_hooks(hooks, additions))
        self._enable_feature()
        return {
            "agent": self.name,
            "installed": True,
            "path": str(self.hooks_path),
            "config_path": str(self.config_path),
            "support": self.support,
        }

    def uninstall(self) -> dict[str, Any]:
        hooks = strip_matching_commands(read_json_file(self.hooks_path), "mach hooks dispatch --agent codex")
        write_json_file(self.hooks_path, hooks)
        self._disable_feature()
        return {
            "agent": self.name,
            "installed": False,
            "path": str(self.hooks_path),
            "config_path": str(self.config_path),
            "support": self.support,
        }

    def status(self) -> dict[str, Any]:
        hooks = read_json_file(self.hooks_path)
        config_text = self.config_path.read_text(encoding="utf-8") if self.config_path.exists() else ""
        return {
            "agent": self.name,
            "support": self.support,
            "configured": "mach hooks dispatch --agent codex" in str(hooks),
            "hooks_path": str(self.hooks_path),
            "config_path": str(self.config_path),
            "feature_enabled": "codex_hooks = true" in config_text,
            "notes": self.notes,
        }

    def dispatch(self, event_name: str, payload: dict[str, Any], repo_root: Path) -> HookDispatchResult:
        session_id = first_present(payload, "session_id", "sessionId")
        if event_name == "SessionStart":
            inject = ""
            try:
                from mach.resume_flow import ResumeService
                from mach.session import SessionStore

                bind = ResumeService(SessionStore(repo_root)).on_agent_session_start(
                    self.name,
                    str(session_id) if session_id else None,
                )
                inject = bind.get("inject") or ""
            except Exception:
                inject = ""
            content = "Codex session started"
            if inject:
                content = f"{content}\n\n{inject}"
            return HookDispatchResult(
                handled=True,
                emitted_output=inject,
                event={
                    "kind": "step",
                    "agent": self.name,
                    "source_session_id": str(session_id) if session_id else None,
                    "step": {"type": "system_action", "content": content},
                },
            )
        if event_name == "UserPromptSubmit":
            prompt = first_present(payload, "prompt", "content")
            return self._step(session_id, "input", str(prompt or ""))
        if event_name in {"PreToolUse", "PostToolUse"}:
            tool_name = str(first_present(payload, "tool_name", "toolName") or "unknown")
            tool_input = first_present(payload, "tool_input", "toolInput", "arguments")
            tool_output = first_present(payload, "tool_result", "toolOutput", "result", "error")
            
            category, file_changes = extract_tool_details(self.repo_root, tool_name, tool_input)
            
            step: dict[str, Any] = {
                "type": "tool",
                "content": f"{event_name}: {tool_name}",
                "tool": {
                    "name": tool_name,
                    "category": category,
                    "content": str(tool_input or ""),
                },
            }
            if file_changes:
                step["file_changes"] = file_changes
            if tool_output is not None:
                step["tool_result"] = tool_output
            return HookDispatchResult(
                handled=True,
                event={"kind": "step", "agent": self.name, "source_session_id": session_id, "step": step},
            )
        if event_name == "Stop":
            return HookDispatchResult(handled=True, event={"kind": "session_end", "agent": self.name, "source_session_id": session_id})
        return HookDispatchResult(handled=False)

    @staticmethod
    def _entry(command: str) -> dict[str, Any]:
        return {"hooks": [{"type": "command", "command": command}]}

    def _enable_feature(self) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        text = self.config_path.read_text(encoding="utf-8") if self.config_path.exists() else ""
        text = self._remove_managed_block(text)
        block = f"{CODEX_MARKER_START}\n[features]\ncodex_hooks = true\n{CODEX_MARKER_END}\n"
        if "[features]" not in text:
            updated = f"{text.rstrip()}\n\n{block}" if text.strip() else block
        elif "codex_hooks" not in text:
            updated = text.replace("[features]\n", f"[features]\n# managed by Mach\ncodex_hooks = true\n", 1)
        else:
            updated = text
        self.config_path.write_text(updated, encoding="utf-8")

    def _disable_feature(self) -> None:
        if not self.config_path.exists():
            return
        text = self.config_path.read_text(encoding="utf-8")
        text = self._remove_managed_block(text)
        text = text.replace("# managed by Mach\ncodex_hooks = true\n", "")
        self.config_path.write_text(text, encoding="utf-8")

    @staticmethod
    def _remove_managed_block(text: str) -> str:
        if CODEX_MARKER_START not in text:
            return text
        before, _, rest = text.partition(CODEX_MARKER_START)
        _, _, after = rest.partition(CODEX_MARKER_END)
        return f"{before.rstrip()}\n{after.lstrip()}".strip() + ("\n" if before or after else "")

    def _step(self, session_id: Any, step_type: str, content: str) -> HookDispatchResult:
        return HookDispatchResult(
            handled=True,
            event={
                "kind": "step",
                "agent": self.name,
                "source_session_id": str(session_id) if session_id else None,
                "step": {"type": step_type, "content": content},
            },
        )
