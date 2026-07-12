"""Resume orchestration: handoff → spawn agent session → bind → resume command."""

from __future__ import annotations

import sys
from pathlib import Path
from time import time
from typing import Any, TextIO

from mach.agent_spawn import AgentSpawner
from mach.git_utils import head_commit
from mach.handoff import HandoffWriter
from mach.session import MachError, SessionStore
from mach.utils import read_json, write_json


def pending_resume_path(mach_dir: Path) -> Path:
    return mach_dir / "pending_resume.json"


def handoffs_dir(mach_dir: Path) -> Path:
    return mach_dir / "handoffs"


class ResumeService:
    def __init__(self, store: SessionStore | None = None) -> None:
        self.store = store or SessionStore()

    def pending_path(self) -> Path:
        return pending_resume_path(self.store.paths.mach_dir)

    def read_pending(self) -> dict[str, Any] | None:
        path = self.pending_path()
        if not path.exists():
            return None
        try:
            data = read_json(path)
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def write_pending(self, payload: dict[str, Any]) -> None:
        write_json(self.pending_path(), payload)

    def clear_pending(self) -> None:
        path = self.pending_path()
        if path.exists():
            path.unlink()

    def status(self) -> dict[str, Any]:
        pending = self.read_pending()
        active = self.store.get_active_session_id()
        return {
            "pending": pending,
            "active_session": active,
            "active_meta": self.store.read_session_meta(active) if active else None,
        }

    def prepare(
        self,
        session_id: str | None = None,
        *,
        agent: str,
        branch: str | None = None,
        task_desc: str | None = None,
        progress: bool = True,
        stream: TextIO | None = None,
        spawn: bool = True,
        spawn_timeout_sec: int = 180,
    ) -> dict[str, Any]:
        """Create handoff, spawn agent session with handoff, bind ids, return resume cmd."""
        out = stream or sys.stderr
        steps_total = 4

        def step(n: int, title: str, detail: str = "") -> None:
            if not progress:
                return
            out.write(f"[{n}/{steps_total}] {title}\n")
            if detail:
                for line in detail.splitlines() or [""]:
                    out.write(f"      {line}\n")
            out.flush()

        self.store.init_repo()
        source_id = self.store._resolve_resume_source(session_id=session_id, branch=branch)
        source_meta = self.store.read_session_meta(source_id)
        target_agent = (agent or source_meta.get("agent") or "unknown").strip().lower() or "unknown"

        # 1) Handoff document (full chat, structured, no AI)
        step(1, "Creating handoff", f"session {source_id}")
        writer = HandoffWriter(
            repo_root=self.store.paths.repo_root,
            sessions_dir=self.store.paths.sessions_dir,
            handoffs_dir=handoffs_dir(self.store.paths.mach_dir),
            read_blob=self.store._read_blob,
        )
        handoff = writer.write(source_id, target_agent=target_agent)
        size_kb = max(handoff.bytes_written / 1024.0, 0.01)
        step(
            1,
            "Creating handoff",
            f"{handoff.path} ({size_kb:.1f} KiB, {handoff.step_count} steps)",
        )

        # 2) Activate Mach session for this agent
        step(2, "Activating Mach session", f"agent={target_agent}")
        from mach.locking import file_lock

        with file_lock(self.store.paths.lock_path):
            self.store._activate_session_unlocked(
                source_id,
                agent=target_agent,
                source_session_id=None,
                task_desc=task_desc,
                resume_pending=True,
            )
            self.store._record_step_for_session_unlocked(
                source_id,
                {
                    "type": "system_action",
                    "content": (
                        f"Resume: handoff written for agent {target_agent}.\n"
                        f"Handoff: {handoff.path}"
                    ),
                    "risk_level": "none",
                },
            )
            meta = self.store.read_session_meta(source_id)

        pending = {
            "v": 1,
            "status": "pending",
            "mach_session_id": source_id,
            "agent": target_agent,
            "handoff_path": str(handoff.path.resolve()),
            "seed_prompt": handoff.seed_prompt,
            "created_at": int(time()),
            "vendor_session_id": None,
            "current_commit": head_commit(self.store.paths.repo_root),
        }
        self.write_pending(pending)

        # 3) Spawn agent session — Mach owns handoff transfer (not the user)
        spawn_result = None
        if spawn:
            step(3, "Spawning agent session", f"transferring handoff into {target_agent}…")
            spawner = AgentSpawner(self.store.paths.repo_root)
            spawn_result = spawner.spawn(
                target_agent,
                handoff_path=handoff.path,
                mach_session_id=source_id,
                timeout_sec=spawn_timeout_sec,
            )
            if not spawn_result.ok or not spawn_result.vendor_session_id:
                step(3, "Spawn failed", spawn_result.detail if spawn_result else "unknown error")
                raise MachError(
                    f"Failed to spawn {target_agent} session with handoff transfer.\n"
                    f"{spawn_result.detail if spawn_result else ''}\n"
                    "Install/auth the agent CLI, then retry `mach resume`.\n"
                    "Mach will not ask you to paste the handoff manually."
                )

            vendor_id = spawn_result.vendor_session_id
            # Link immediately (hooks may also re-bind on next SessionStart).
            bind = self.on_agent_session_start(target_agent, vendor_id)
            step(
                3,
                "Spawned agent session",
                f"vendor_id={vendor_id} via {spawn_result.method}",
            )
            resume_cmd = spawn_result.resume_command or bind.get("resume_command")
        else:
            step(3, "Spawn skipped", "--no-spawn (prepare handoff only)")
            resume_cmd = None
            vendor_id = None

        # 4) Ready
        step(4, "Ready", resume_cmd or "pending link")
        meta = self.store.read_session_meta(source_id)
        pending = self.read_pending() or pending

        result = {
            "status": "ready" if vendor_id else "prepared",
            "session_id": source_id,
            "agent": target_agent,
            "previous_agent": source_meta.get("agent"),
            "handoff_path": str(handoff.path.resolve()),
            "handoff_bytes": handoff.bytes_written,
            "step_count": handoff.step_count,
            "title": handoff.title,
            "seed_prompt": handoff.seed_prompt,
            "vendor_session_id": vendor_id,
            "resume_command": resume_cmd,
            "spawn": {
                "ok": bool(spawn_result and spawn_result.ok),
                "method": spawn_result.method if spawn_result else None,
                "detail": spawn_result.detail if spawn_result else None,
            }
            if spawn
            else None,
            "pending": pending,
            "metadata": meta,
            "steps": [
                {"id": 1, "name": "create_handoff", "path": str(handoff.path)},
                {"id": 2, "name": "activate_mach_session", "session_id": source_id},
                {
                    "id": 3,
                    "name": "spawn_agent",
                    "vendor_session_id": vendor_id,
                    "method": spawn_result.method if spawn_result else None,
                },
                {"id": 4, "name": "ready", "resume_command": resume_cmd},
            ],
        }
        return result

    def on_agent_session_start(
        self,
        agent: str,
        vendor_session_id: str | None,
    ) -> dict[str, Any]:
        """Bind pending resume + SessionStart inject text (handoff path, not full paste)."""
        pending = self.read_pending()
        inject = ""
        linked = False
        if not pending or pending.get("status") not in {"pending", "linked"}:
            return {"linked": False, "inject": "", "pending": None}

        if pending.get("agent") != agent:
            return {"linked": False, "inject": "", "pending": pending}

        mach_session_id = str(pending.get("mach_session_id") or "")
        if not mach_session_id or not (self.store.paths.sessions_dir / mach_session_id).exists():
            return {"linked": False, "inject": "", "pending": pending}

        from mach.locking import file_lock
        from mach.agent_spawn import AgentSpawner

        with file_lock(self.store.paths.lock_path):
            self.store._activate_session_unlocked(
                mach_session_id,
                agent=agent,
                source_session_id=str(vendor_session_id) if vendor_session_id else None,
                resume_pending=False,
            )
            linked = True

        handoff_path = pending.get("handoff_path") or ""
        # Keep inject short — full history lives in the handoff file already loaded by spawn.
        inject = (
            "# Mach resume\n"
            f"Continuing Mach session `{mach_session_id}`.\n"
            f"Handoff file (read if you still need details): `{handoff_path}`\n"
        )
        if vendor_session_id:
            inject += f"Vendor session: `{vendor_session_id}`\n"

        pending["status"] = "linked"
        pending["vendor_session_id"] = vendor_session_id
        pending["linked_at"] = int(time())
        pending["resume_command"] = AgentSpawner(self.store.paths.repo_root).resume_command(
            agent, vendor_session_id or "<unknown>"
        )
        self.write_pending(pending)

        return {
            "linked": linked,
            "inject": inject,
            "pending": pending,
            "mach_session_id": mach_session_id,
            "vendor_session_id": vendor_session_id,
            "resume_command": pending.get("resume_command"),
        }


def print_resume_ready(result: dict[str, Any], stream: TextIO | None = None) -> None:
    out = stream or sys.stdout
    out.write("\n")
    if result.get("status") == "ready" and result.get("resume_command"):
        out.write("Success: Agent session ready with handoff transferred.\n")
    else:
        out.write("Success: Resume prepared.\n")
    out.write(f"  Mach session   : {result['session_id']}\n")
    out.write(f"  Agent          : {result['agent']}\n")
    out.write(f"  Handoff        : {result['handoff_path']}\n")
    out.write(f"  Steps          : {result['step_count']}\n")
    if result.get("vendor_session_id"):
        out.write(f"  Agent session  : {result['vendor_session_id']}\n")
    if result.get("spawn") and result["spawn"].get("method"):
        out.write(f"  Spawn method   : {result['spawn']['method']}\n")
    out.write("\n")
    if result.get("resume_command"):
        out.write("Continue in your terminal:\n")
        out.write(f"  {result['resume_command']}\n")
        out.write("\n")
        out.write("The agent session already has the handoff. You do not need to paste context.\n")
    else:
        out.write("No vendor session id yet. Run: mach resume --status\n")
    out.flush()
