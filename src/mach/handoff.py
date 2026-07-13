"""Deterministic structured handoff documents from Mach sessions (no AI)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import time
from typing import Any, Callable

from mach.git_utils import current_branch, head_commit
from mach.utils import read_json, read_jsonl


def _utc_iso(ts: int | None = None) -> str:
    value = int(ts if ts is not None else time())
    return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _truncate(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    if limit < 40:
        return text[:limit]
    head = max(limit // 2, 1)
    tail = max(limit - head - 20, 0)
    omitted = len(text) - head - tail
    return f"{text[:head]}\n\n…[truncated {omitted} chars]…\n\n{text[-tail:] if tail else ''}"


@dataclass
class HandoffResult:
    path: Path
    session_id: str
    bytes_written: int
    step_count: int
    title: str
    seed_prompt: str
    metadata: dict[str, Any]


class HandoffWriter:
    """Build a full-chat handoff markdown file for agent resume."""

    def __init__(
        self,
        *,
        repo_root: Path,
        sessions_dir: Path,
        handoffs_dir: Path,
        read_blob: Callable[[str | None], str | None],
    ) -> None:
        self.repo_root = repo_root
        self.sessions_dir = sessions_dir
        self.handoffs_dir = handoffs_dir
        self.read_blob = read_blob

    def write(
        self,
        session_id: str,
        *,
        target_agent: str,
        max_step_chars: int = 50_000,
    ) -> HandoffResult:
        session_dir = self.sessions_dir / session_id
        if not session_dir.exists():
            raise FileNotFoundError(f"Unknown session: {session_id}")

        meta = read_json(session_dir / "meta.json")
        steps = read_jsonl(session_dir / "steps.jsonl")
        title = self._title(meta, session_id)
        current = head_commit(self.repo_root)
        branch = current_branch(self.repo_root) or meta.get("branch") or "unknown"

        body = self._render(
            session_id=session_id,
            meta=meta,
            steps=steps,
            title=title,
            target_agent=target_agent,
            branch=branch,
            current_commit=current,
            max_step_chars=max_step_chars,
        )

        self.handoffs_dir.mkdir(parents=True, exist_ok=True)
        stamp = int(time())
        path = self.handoffs_dir / f"{session_id}_{stamp}.md"
        path.write_text(body, encoding="utf-8")

        seed = self.build_seed_prompt(
            handoff_path=path,
            session_id=session_id,
            task=meta.get("task_desc"),
            target_agent=target_agent,
        )
        return HandoffResult(
            path=path,
            session_id=session_id,
            bytes_written=path.stat().st_size,
            step_count=len(steps),
            title=title,
            seed_prompt=seed,
            metadata={
                "mach_session_id": session_id,
                "target_agent": target_agent,
                "branch": branch,
                "pre_commit": meta.get("pre_commit"),
                "current_commit": current,
                "repo_root": str(self.repo_root),
                "handoff_path": str(path),
                "step_count": len(steps),
                "task_desc": meta.get("task_desc"),
            },
        )

    @staticmethod
    def build_seed_prompt(
        *,
        handoff_path: Path,
        session_id: str,
        task: str | None,
        target_agent: str,
    ) -> str:
        task_line = (task or "Continue the prior coding work described in the handoff.").strip()
        return (
            "You are continuing a prior coding session recorded by Mach.\n\n"
            f"1. Open and fully read this handoff file (in chunks if it is large):\n"
            f"   {handoff_path.resolve()}\n"
            "2. Start with Metadata and Instructions, then the full Session chat.\n"
            "3. Check git status and the working tree before editing; prefer current files if they conflict with older trail notes.\n"
            "4. Continue the task. Do not restart from scratch unless the work is broken.\n\n"
            f"Task: {task_line}\n"
            f"Mach session id (keep logging continuity): {session_id}\n"
            f"Target agent: {target_agent}\n"
        )

    def _title(self, meta: dict[str, Any], session_id: str) -> str:
        task = (meta.get("task_desc") or "").strip()
        if task:
            short = task.splitlines()[0][:80]
            return f"Mach Handoff: {short}"
        return f"Mach Handoff: {session_id}"

    def _step_text(self, step: dict[str, Any]) -> str:
        stype = step.get("type")
        if stype == "tool":
            tool = step.get("tool") or {}
            body = tool.get("content")
            if body is None and tool.get("content_hash"):
                body = self.read_blob(tool.get("content_hash"))
            if body is None:
                body = step.get("content")
            if body is None and step.get("content_hash"):
                body = self.read_blob(step.get("content_hash"))
            return str(body or "")
        body = step.get("content")
        if body is None and step.get("content_hash"):
            body = self.read_blob(step.get("content_hash"))
        return str(body or "")

    def _render(
        self,
        *,
        session_id: str,
        meta: dict[str, Any],
        steps: list[dict[str, Any]],
        title: str,
        target_agent: str,
        branch: str,
        current_commit: str | None,
        max_step_chars: int,
    ) -> str:
        lines: list[str] = [
            f"# {title}",
            "",
            "## Metadata",
            f"- mach_session_id: `{session_id}`",
            f"- forked_from: `{meta.get('forked_from') or 'n/a'}`",
            f"- original_agent: `{meta.get('agent') or 'unknown'}`",
            f"- target_agent: `{target_agent}`",
            f"- repo_root: `{self.repo_root}`",
            f"- branch: `{branch}`",
            f"- pre_commit: `{meta.get('pre_commit') or 'n/a'}`",
            f"- post_commit: `{meta.get('post_commit') or 'n/a'}`",
            f"- current_commit: `{current_commit or 'n/a'}`",
            f"- status: `{meta.get('status') or 'unknown'}`",
            f"- started_at: `{_utc_iso(meta.get('started_at')) if meta.get('started_at') else 'n/a'}`",
            f"- ended_at: `{_utc_iso(meta.get('ended_at')) if meta.get('ended_at') else 'n/a'}`",
            f"- step_count: `{len(steps)}`",
            f"- handoff_generated_at: `{_utc_iso()}`",
            f"- handoff_version: `1`",
            "",
            "## Instructions for the agent",
            "You are continuing a prior coding session recorded by Mach.",
            "1. Read this entire file. If it is large, read it in chunks (by section or line ranges) until you have covered **Session chat**.",
            "2. Treat the chat trail as prior conversation and tool activity.",
            "3. Prefer the current working tree and git state if they disagree with older trail content.",
            "4. Continue the **Task** below; do not redo completed work unless it is broken.",
            f"5. Keep Mach session continuity: `{session_id}`.",
            "",
            "## Task",
            (meta.get("task_desc") or "(no task description recorded — infer from Session chat)").strip(),
            "",
            "## Files touched (index)",
        ]

        file_map: dict[str, dict[str, Any]] = {}
        for step in steps:
            for change in step.get("file_changes") or []:
                if not isinstance(change, dict):
                    continue
                path = str(change.get("file_path") or "")
                if not path:
                    continue
                entry = file_map.setdefault(
                    path,
                    {"actions": set(), "added": 0, "removed": 0},
                )
                entry["actions"].add(str(change.get("action") or "write"))
                entry["added"] += int(change.get("lines_added") or 0)
                entry["removed"] += int(change.get("lines_removed") or 0)

        if not file_map:
            lines.append("_No recorded file_changes in this session._")
        else:
            lines.append("| path | actions | +/- |")
            lines.append("|---|---|---|")
            for path in sorted(file_map):
                entry = file_map[path]
                actions = ",".join(sorted(entry["actions"]))
                lines.append(f"| `{path}` | {actions} | +{entry['added']}/-{entry['removed']} |")

        lines.extend(["", "## Session chat (full)", ""])

        for step in steps:
            stype = str(step.get("type") or "unknown")
            step_id = step.get("id") or "?"
            ts = step.get("ts")
            ts_label = _utc_iso(ts) if ts else "n/a"
            header = f"### [{stype}] `{step_id}` · {ts_label}"
            if stype == "tool":
                tool = step.get("tool") or {}
                name = tool.get("name") or "tool"
                category = tool.get("category") or "exec"
                header = f"### [tool:{name}] `{step_id}` · {ts_label} · category={category}"
            lines.append(header)

            files = [
                str(fc.get("file_path"))
                for fc in (step.get("file_changes") or [])
                if isinstance(fc, dict) and fc.get("file_path")
            ]
            if files:
                lines.append(f"_files: {', '.join(files[:20])}" + (" …" if len(files) > 20 else "") + "_")

            risk_flags = step.get("risk_flags") or []
            if risk_flags:
                ids = ", ".join(
                    str(flag.get("rule_id") or "?")
                    for flag in risk_flags
                    if isinstance(flag, dict)
                )
                if ids:
                    lines.append(f"_risk: {ids}_")

            text = _truncate(self._step_text(step), max_step_chars)
            if text.strip():
                lines.append("")
                lines.append("```")
                lines.append(text.rstrip())
                lines.append("```")
            lines.append("")

        lines.append("## End of handoff")
        lines.append(f"Continue work for Mach session `{session_id}`.")
        lines.append("")
        return "\n".join(lines)
