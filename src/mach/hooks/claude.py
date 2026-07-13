from __future__ import annotations

from pathlib import Path
from typing import Any

from mach.hooks.base import HookAdapter, HookDispatchResult
from mach.hooks.helpers import (
    command_name,
    first_present,
    merge_event_hooks,
    nested_first_present,
    read_json_file,
    strip_matching_commands,
    write_json_file,
    extract_tool_details,
)


class ClaudeHookAdapter(HookAdapter):
    name = "claude"
    support = "full"
    notes = "Claude Code exposes rich lifecycle and tool hooks."

    def __init__(self, repo_root: Path) -> None:
        super().__init__(repo_root)
        self.settings_path = repo_root / ".claude" / "settings.local.json"

    def install(self) -> dict[str, Any]:
        hooks = strip_matching_commands(read_json_file(self.settings_path), "mach hooks dispatch --agent claude")
        command = command_name()
        additions = {
            # passthrough: SessionStart stdout can inject handoff instructions into Claude.
            "SessionStart": [self._event_entry(
                f'{command} hooks dispatch --agent claude --event SessionStart --stdout-mode passthrough'
            )],
            "SessionEnd": [self._event_entry(f'{command} hooks dispatch --agent claude --event SessionEnd')],
            "UserPromptSubmit": [self._event_entry(f'{command} hooks dispatch --agent claude --event UserPromptSubmit')],
            "Stop": [self._event_entry(f'{command} hooks dispatch --agent claude --event Stop')],
            "SubagentStart": [self._event_entry(f'{command} hooks dispatch --agent claude --event SubagentStart')],
            "SubagentStop": [self._event_entry(f'{command} hooks dispatch --agent claude --event SubagentStop')],
            "TaskCreated": [self._event_entry(f'{command} hooks dispatch --agent claude --event TaskCreated')],
            "TaskCompleted": [self._event_entry(f'{command} hooks dispatch --agent claude --event TaskCompleted')],
            "PreToolUse": [self._tool_entry(f'{command} hooks dispatch --agent claude --event PreToolUse')],
            "PostToolUse": [self._tool_entry(f'{command} hooks dispatch --agent claude --event PostToolUse')],
            "PostToolUseFailure": [self._tool_entry(f'{command} hooks dispatch --agent claude --event PostToolUseFailure')],
        }
        merged = merge_event_hooks(hooks, additions)
        write_json_file(self.settings_path, merged)
        return {"agent": self.name, "installed": True, "path": str(self.settings_path), "support": self.support}

    def uninstall(self) -> dict[str, Any]:
        hooks = strip_matching_commands(read_json_file(self.settings_path), "mach hooks dispatch --agent claude")
        write_json_file(self.settings_path, hooks)
        return {"agent": self.name, "installed": False, "path": str(self.settings_path), "support": self.support}

    def status(self) -> dict[str, Any]:
        hooks = read_json_file(self.settings_path)
        serialized = str(hooks)
        return {
            "agent": self.name,
            "support": self.support,
            "configured": "mach hooks dispatch --agent claude" in serialized,
            "path": str(self.settings_path),
            "notes": self.notes,
        }

    def dispatch(self, event_name: str, payload: dict[str, Any], repo_root: Path) -> HookDispatchResult:
        session_id = first_present(payload, "session_id", "transcript_path")
        if event_name == "SessionEnd":
            return HookDispatchResult(
                handled=True,
                event={"kind": "session_end", "agent": self.name, "source_session_id": str(session_id) if session_id else None},
            )
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
            content = "Claude session started"
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
            prompt = first_present(payload, "prompt") or nested_first_present(payload, ("hook_input", "prompt"))
            return self._step_result(session_id, "input", prompt or "")
        if event_name in {"TaskCreated", "TaskCompleted", "SubagentStart", "SubagentStop"}:
            details = first_present(payload, "task", "description", "message") or event_name
            return self._step_result(session_id, "reasoning", str(details))
        if event_name in {"PreToolUse", "PostToolUse", "PostToolUseFailure"}:
            return self._tool_result(session_id, payload, event_name)
        if event_name == "Stop":
            content = first_present(payload, "response", "stop_reason", "message") or "Claude turn completed"
            return self._step_result(session_id, "output", str(content))
        return HookDispatchResult(handled=False)

    @staticmethod
    def _event_entry(command: str) -> dict[str, Any]:
        return {"hooks": [{"type": "command", "command": command}]}

    @staticmethod
    def _tool_entry(command: str) -> dict[str, Any]:
        return {"matcher": ".*", "hooks": [{"type": "command", "command": command}]}

    def _step_result(self, session_id: Any, step_type: str, content: str) -> HookDispatchResult:
        return HookDispatchResult(
            handled=True,
            event={
                "kind": "step",
                "agent": self.name,
                "source_session_id": str(session_id) if session_id else None,
                "step": {"type": step_type, "content": content},
            },
        )

    def _tool_result(self, session_id: Any, payload: dict[str, Any], event_name: str) -> HookDispatchResult:
        tool_name = str(first_present(payload, "tool_name", "tool") or "unknown")
        tool_input = first_present(payload, "tool_input", "tool_arguments", "arguments")
        tool_result = first_present(payload, "tool_result", "result", "error")
        content = f"{event_name}: {tool_name}"
        
        category, file_changes = extract_tool_details(self.repo_root, tool_name, tool_input)
        
        step: dict[str, Any] = {
            "type": "tool",
            "content": content,
            "tool": {
                "name": tool_name,
                "category": category,
                "content": str(tool_input or ""),
            },
        }
        if file_changes:
            step["file_changes"] = file_changes
        if tool_result is not None:
            step["tool_result"] = tool_result
        return HookDispatchResult(
            handled=True,
            event={
                "kind": "step",
                "agent": self.name,
                "source_session_id": str(session_id) if session_id else None,
                "step": step,
            },
        )
