from __future__ import annotations

from pathlib import Path
from typing import Any

from mach.hooks.base import HookAdapter, HookDispatchResult
from mach.hooks.helpers import (
    command_name,
    first_present,
    latest_user_message,
    merge_event_hooks,
    nested_first_present,
    read_json_file,
    strip_matching_commands,
    write_json_file,
    extract_tool_details,
)


class GeminiHookAdapter(HookAdapter):
    name = "gemini"
    support = "full"
    notes = "Gemini CLI exposes command hooks in .gemini/settings.json."

    def __init__(self, repo_root: Path) -> None:
        super().__init__(repo_root)
        self.path = repo_root / ".gemini" / "settings.json"

    def install(self) -> dict[str, Any]:
        settings = strip_matching_commands(read_json_file(self.path), "mach hooks dispatch --agent gemini")
        command = command_name()
        additions = {
            "SessionStart": [self._entry(f'{command} hooks dispatch --agent gemini --event SessionStart --stdout-mode empty-json')],
            "BeforeAgent": [self._entry(f'{command} hooks dispatch --agent gemini --event BeforeAgent --stdout-mode empty-json')],
            "BeforeTool": [self._tool_entry(f'{command} hooks dispatch --agent gemini --event BeforeTool --stdout-mode empty-json')],
            "AfterTool": [self._tool_entry(f'{command} hooks dispatch --agent gemini --event AfterTool --stdout-mode empty-json')],
            "AfterModel": [self._entry(f'{command} hooks dispatch --agent gemini --event AfterModel --stdout-mode empty-json')],
            "AfterAgent": [self._entry(f'{command} hooks dispatch --agent gemini --event AfterAgent --stdout-mode empty-json')],
            "SessionEnd": [self._entry(f'{command} hooks dispatch --agent gemini --event SessionEnd --stdout-mode empty-json')],
        }
        write_json_file(self.path, merge_event_hooks(settings, additions))
        return {"agent": self.name, "installed": True, "path": str(self.path), "support": self.support}

    def uninstall(self) -> dict[str, Any]:
        settings = strip_matching_commands(read_json_file(self.path), "mach hooks dispatch --agent gemini")
        write_json_file(self.path, settings)
        return {"agent": self.name, "installed": False, "path": str(self.path), "support": self.support}

    def status(self) -> dict[str, Any]:
        settings = read_json_file(self.path)
        return {
            "agent": self.name,
            "support": self.support,
            "configured": "mach hooks dispatch --agent gemini" in str(settings),
            "path": str(self.path),
            "notes": self.notes,
        }

    def dispatch(self, event_name: str, payload: dict[str, Any], repo_root: Path) -> HookDispatchResult:
        session_id = first_present(payload, "session_id", "sessionId")
        if event_name == "SessionEnd":
            return HookDispatchResult(
                handled=True,
                emitted_output="{}",
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
            content = "Gemini session started"
            if inject:
                content = f"{content}\n\n{inject}"
            # Gemini expects JSON on stdout for empty-json mode; keep "{}" and log inject in step.
            return HookDispatchResult(
                handled=True,
                emitted_output="{}",
                event={
                    "kind": "step",
                    "agent": self.name,
                    "source_session_id": str(session_id) if session_id else None,
                    "step": {"type": "system_action", "content": content},
                },
            )
        if event_name == "UserPromptSubmit":
            prompt = first_present(payload, "prompt") or nested_first_present(payload, ("hook_input", "prompt"))
            return self._step(session_id, "input", str(prompt or ""), "{}")
        if event_name == "BeforeAgent":
            messages = nested_first_present(payload, ("llm_request", "messages")) or []
            prompt = latest_user_message(messages) if isinstance(messages, list) else None
            return self._step(session_id, "input", str(prompt or ""), "{}")
        if event_name == "AfterModel":
            response = nested_first_present(payload, ("llm_response", "text"), ("llm_response", "content"))
            return self._step(session_id, "reasoning", str(response or "Gemini model step completed"), "{}")
        if event_name == "AfterAgent":
            output = first_present(payload, "prompt_response", "response")
            return self._step(session_id, "output", str(output or ""), "{}")
        if event_name in {"BeforeTool", "AfterTool"}:
            tool_name = str(first_present(payload, "tool_name", "toolName") or "unknown")
            tool_input = first_present(payload, "tool_input", "toolInput")
            tool_output = first_present(payload, "tool_output", "toolOutput", "result")
            
            category, file_changes = extract_tool_details(repo_root, tool_name, tool_input)
            
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
                emitted_output="{}",
                event={"kind": "step", "agent": self.name, "source_session_id": str(session_id) if session_id else None, "step": step},
            )
        return HookDispatchResult(handled=False, emitted_output="{}")

    @staticmethod
    def _entry(command: str) -> dict[str, Any]:
        return {"matcher": "*", "hooks": [{"type": "command", "command": command}]}

    @staticmethod
    def _tool_entry(command: str) -> dict[str, Any]:
        return {"matcher": "*", "hooks": [{"type": "command", "command": command}]}

    def _step(self, session_id: Any, step_type: str, content: str, emitted_output: str) -> HookDispatchResult:
        return HookDispatchResult(
            handled=True,
            emitted_output=emitted_output,
            event={
                "kind": "step",
                "agent": self.name,
                "source_session_id": str(session_id) if session_id else None,
                "step": {"type": step_type, "content": content},
            },
        )
