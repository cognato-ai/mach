from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from mach.git_utils import current_branch, head_commit
from mach.ingest import EventInboxService
from mach.locking import file_lock
from mach.repository import resolve_paths
from mach.session import MachError, SessionStore
from mach.utils import hash_file, read_json, write_json


class TrackerService:
    def __init__(self, repo_root: Path | None = None) -> None:
        self.paths = resolve_paths(repo_root)
        self.store = SessionStore(self.paths.repo_root)
        self.ingest = EventInboxService(self.paths.repo_root)

    def prepare_state(self) -> dict[str, Any]:
        self.store.init_repo()
        with file_lock(self.paths.tracker_lock_path):
            state = self._build_state(last_activity_at=None)
            write_json(self.paths.tracker_state_path, state)
            return state

    def ensure_state(self) -> dict[str, Any]:
        with file_lock(self.paths.tracker_lock_path):
            if self.paths.tracker_state_path.exists():
                return read_json(self.paths.tracker_state_path)
            state = self._build_state(last_activity_at=None)
            write_json(self.paths.tracker_state_path, state)
            return state

    def scan_once(self) -> dict[str, Any]:
        self.store.init_repo()
        with file_lock(self.paths.tracker_lock_path):
            if not self.paths.tracker_state_path.exists():
                state = self._build_state(last_activity_at=None)
                write_json(self.paths.tracker_state_path, state)
                return {
                    "ok": True,
                    "initialized_state": True,
                    "files_observed": len(state["files"]),
                    "events": [],
                }

            previous = read_json(self.paths.tracker_state_path)
            current = self._build_state(last_activity_at=previous.get("last_activity_at"))

            ingested = self.ingest.process_pending_events()
            file_changes = self._diff_files(previous.get("files", {}), current["files"])
            git_change = self._diff_git(previous.get("git", {}), current["git"])
            now = int(time.time())
            events: list[dict[str, Any]] = list(ingested.get("events", []))

            if ingested.get("processed"):
                current["last_activity_at"] = now

            if file_changes:
                step = self.store.record_agent_step(
                    agent="workspace-observer",
                    source_session_id="workspace",
                    task_desc="Automatically observed repository activity",
                    step_dict=self._workspace_step(file_changes),
                )
                current["last_activity_at"] = now
                events.append(
                    {
                        "kind": "workspace_change",
                        "step_id": step["id"],
                        "files_changed": len(file_changes),
                    }
                )

            if git_change:
                step = self.store.record_agent_step(
                    agent="workspace-observer",
                    source_session_id="workspace",
                    task_desc="Automatically observed repository activity",
                    step_dict=self._git_step(git_change),
                )
                current["last_activity_at"] = now
                events.append(
                    {
                        "kind": "git_change",
                        "step_id": step["id"],
                        "git": git_change,
                    }
                )
                if git_change.get("head_changed") and self.store.read_config().get("commit_closes_session", False):
                    ended = self.store.end_session()
                    current["last_activity_at"] = None
                    events.append({"kind": "session_closed", "session_id": ended["id"], "reason": "commit"})

            idle_timeout = self.store.read_config().get("idle_timeout_sec")
            active = self.store.get_active_session_id()
            if (
                idle_timeout
                and active
                and current.get("last_activity_at")
                and now - int(current["last_activity_at"]) >= int(idle_timeout)
            ):
                ended = self.store.end_session(active)
                current["last_activity_at"] = None
                events.append({"kind": "session_closed", "session_id": ended["id"], "reason": "idle_timeout"})

            write_json(self.paths.tracker_state_path, current)
            return {
                "ok": True,
                "files_observed": len(current["files"]),
                "active_session": self.store.get_active_session_id(),
                "events": events,
            }

    def run_loop(self, once: bool = False) -> dict[str, Any]:
        self.store.init_repo()
        if once:
            return self.scan_once()

        self._write_pid()
        should_stop = {"value": False}

        def stop_handler(signum: int, frame: Any) -> None:
            del signum, frame
            should_stop["value"] = True

        signal.signal(signal.SIGTERM, stop_handler)
        signal.signal(signal.SIGINT, stop_handler)

        try:
            result: dict[str, Any] = {"ok": True, "events": []}
            while not should_stop["value"]:
                result = self.scan_once()
                interval = max(float(self.store.read_config().get("poll_interval_sec", 2)), 0.5)
                time.sleep(interval)
            result["stopped"] = True
            return result
        finally:
            self._cleanup_pid()

    def start_daemon(self) -> dict[str, Any]:
        self.store.init_repo()
        state = self.status()
        if state["running"]:
            return state

        self.ensure_state()

        env = os.environ.copy()
        module_root = str(Path(__file__).resolve().parents[1])
        existing_pythonpath = env.get("PYTHONPATH")
        env["PYTHONPATH"] = module_root if not existing_pythonpath else f"{module_root}{os.pathsep}{existing_pythonpath}"
        env["PYTHONUNBUFFERED"] = "1"

        with self.paths.tracker_log_path.open("a", encoding="utf-8") as log_handle:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "mach.cli",
                    "track",
                    "run",
                    "--repo-root",
                    str(self.paths.repo_root),
                ],
                cwd=self.paths.repo_root,
                env=env,
                stdout=log_handle,
                stderr=log_handle,
                start_new_session=True,
                close_fds=True,
            )

        status = self.status()
        for _ in range(20):
            if process.poll() is not None:
                break
            status = self.status()
            if status["running"]:
                break
            time.sleep(0.1)

        if not status["running"] and process.poll() is not None:
            raise MachError("Tracker process exited before becoming ready. Check .mach/tracker.log.")

        status["spawned_pid"] = process.pid
        return status

    def stop_daemon(self) -> dict[str, Any]:
        status = self.status()
        if not status["running"]:
            self._cleanup_pid()
            return status

        pid = int(status["pid"])
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            self._cleanup_pid()
            return self.status()
        for _ in range(20):
            time.sleep(0.1)
            if not self._pid_running(pid):
                break
        self._cleanup_pid()
        return self.status()

    def status(self) -> dict[str, Any]:
        pid = self._read_pid()
        running = bool(pid and self._pid_running(pid))
        if pid and not running:
            self._cleanup_pid()
            pid = None
        state = read_json(self.paths.tracker_state_path) if self.paths.tracker_state_path.exists() else None
        return {
            "running": running,
            "pid": pid,
            "log_path": str(self.paths.tracker_log_path),
            "state_path": str(self.paths.tracker_state_path),
            "active_session": self.store.get_active_session_id(),
            "files_observed": len(state.get("files", {})) if state else 0,
            "last_activity_at": state.get("last_activity_at") if state else None,
        }

    def _build_state(self, last_activity_at: int | None) -> dict[str, Any]:
        files = {}
        config = self.store.read_config()
        now = int(time.time())
        for file_path in self._iter_repo_files(config.get("ignore_paths", [])):
            relative = file_path.relative_to(self.paths.repo_root).as_posix()
            stat = file_path.stat()
            files[relative] = {
                "sha256": hash_file(file_path),
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
        return {
            "version": 1,
            "repo_root": str(self.paths.repo_root),
            "scanned_at": now,
            "last_activity_at": last_activity_at,
            "git": {
                "branch": current_branch(self.paths.repo_root),
                "head": head_commit(self.paths.repo_root),
            },
            "files": files,
        }

    def _iter_repo_files(self, ignore_paths: list[str]) -> list[Path]:
        ignored = {item.strip("/").replace("\\", "/") for item in ignore_paths}
        files: list[Path] = []
        for root, dirnames, filenames in os.walk(self.paths.repo_root):
            root_path = Path(root)
            relative_root = root_path.relative_to(self.paths.repo_root).as_posix()
            if relative_root == ".":
                relative_root = ""

            dirnames[:] = [
                dirname
                for dirname in dirnames
                if not self._is_ignored(
                    f"{relative_root}/{dirname}".strip("/"),
                    ignored,
                )
            ]

            for filename in filenames:
                relative_path = f"{relative_root}/{filename}".strip("/")
                if self._is_ignored(relative_path, ignored):
                    continue
                files.append(root_path / filename)
        return sorted(files)

    def _diff_files(self, previous: dict[str, Any], current: dict[str, Any]) -> list[dict[str, Any]]:
        changes: list[dict[str, Any]] = []
        for relative in sorted(set(previous) | set(current)):
            before = previous.get(relative)
            after = current.get(relative)
            if before is None and after is not None:
                changes.append(
                    {
                        "file_path": relative,
                        "action": "write",
                        "before_blob": None,
                        "after_blob": after["sha256"],
                        "lines_added": None,
                        "lines_removed": None,
                        "hunks": [],
                    }
                )
            elif before is not None and after is None:
                changes.append(
                    {
                        "file_path": relative,
                        "action": "delete",
                        "before_blob": before["sha256"],
                        "after_blob": None,
                        "lines_added": None,
                        "lines_removed": None,
                        "hunks": [],
                    }
                )
            elif before is not None and after is not None and before["sha256"] != after["sha256"]:
                changes.append(
                    {
                        "file_path": relative,
                        "action": "write",
                        "before_blob": before["sha256"],
                        "after_blob": after["sha256"],
                        "lines_added": None,
                        "lines_removed": None,
                        "hunks": [],
                    }
                )
        return changes

    @staticmethod
    def _diff_git(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any] | None:
        branch_before = previous.get("branch")
        branch_after = current.get("branch")
        head_before = previous.get("head")
        head_after = current.get("head")
        if branch_before == branch_after and head_before == head_after:
            return None
        return {
            "branch_before": branch_before,
            "branch_after": branch_after,
            "head_before": head_before,
            "head_after": head_after,
            "branch_changed": branch_before != branch_after,
            "head_changed": head_before != head_after,
        }

    def _workspace_step(self, file_changes: list[dict[str, Any]]) -> dict[str, Any]:
        summary = ", ".join(
            f"{change['action']}:{change['file_path']}" for change in file_changes[:5]
        )
        if len(file_changes) > 5:
            summary = f"{summary}, +{len(file_changes) - 5} more"

        # Risk scoring is enterprise/backend-owned; local capture stays factual only.
        return {
            "type": "tool",
            "content": f"Observed {len(file_changes)} workspace change(s): {summary}",
            "risk_level": "none",
            "tool": {
                "name": "workspace_observer",
                "category": "write",
                "content": summary,
            },
            "file_changes": file_changes,
        }

    @staticmethod
    def _git_step(change: dict[str, Any]) -> dict[str, Any]:
        parts = []
        if change.get("branch_changed"):
            parts.append(
                f"branch {change.get('branch_before') or 'none'} -> {change.get('branch_after') or 'none'}"
            )
        if change.get("head_changed"):
            before = (change.get("head_before") or "none")[:12]
            after = (change.get("head_after") or "none")[:12]
            parts.append(f"HEAD {before} -> {after}")
        summary = ", ".join(parts) or "git state changed"
        return {
            "type": "tool",
            "content": f"Observed git state change: {summary}",
            "risk_level": "none",
            "tool": {
                "name": "git_observer",
                "category": "read",
                "content": summary,
            },
        }

    @staticmethod
    def _is_ignored(relative_path: str, ignored: set[str]) -> bool:
        return any(
            relative_path == ignored_path or relative_path.startswith(f"{ignored_path}/")
            for ignored_path in ignored
        )

    def _write_pid(self) -> None:
        if self.status()["running"]:
            raise MachError("Tracker is already running.")
        write_json(
            self.paths.tracker_pid_path,
            {
                "pid": os.getpid(),
                "repo_root": str(self.paths.repo_root),
                "started_at": int(time.time()),
            },
        )

    def _read_pid(self) -> int | None:
        if not self.paths.tracker_pid_path.exists():
            return None
        try:
            payload = read_json(self.paths.tracker_pid_path)
        except Exception:
            return None
        pid = payload.get("pid")
        return int(pid) if pid else None

    def _cleanup_pid(self) -> None:
        if self.paths.tracker_pid_path.exists():
            self.paths.tracker_pid_path.unlink()

    @staticmethod
    def _pid_running(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
