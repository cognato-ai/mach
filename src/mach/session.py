from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path
from time import time
from typing import Any

from mach.config import DEFAULT_CONFIG, merge_config
from mach.git_utils import current_branch, head_commit, remote_origin_url, repository_name
from mach.locking import file_lock
from mach.merkle import chain_hash, hash_payload
from mach.models import (
    FileChange,
    GitRemoteInfo,
    MachSyncState,
    PullSessionDetails,
    RemoteInfo,
    RepositoryDetails,
    SessionMeta,
    Step,
    ToolCall,
)
from mach.repository import resolve_paths
from mach.risk import evaluate_step_risk
from mach.utils import (
    append_jsonl,
    ensure_json_file,
    read_json,
    read_jsonl,
    write_json,
)


class MachError(RuntimeError):
    pass


class SessionStore:
    def __init__(self, repo_root: Path | None = None) -> None:
        self.paths = resolve_paths(repo_root)

    def get_config(self) -> dict:
        if not self.paths.config_path.exists():
            return DEFAULT_CONFIG
        return merge_config(read_json(self.paths.config_path))

    def init_repo(self) -> Path:
        self.paths.mach_dir.mkdir(parents=True, exist_ok=True)
        self.paths.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.paths.pack_dir.mkdir(parents=True, exist_ok=True)
        self.paths.inbox_dir.mkdir(parents=True, exist_ok=True)
        self.paths.blobs_dir.mkdir(parents=True, exist_ok=True)
        ensure_json_file(self.paths.config_path, DEFAULT_CONFIG)
        ensure_json_file(self.paths.agent_sessions_path, {})
        ensure_json_file(self.paths.ingest_state_path, {"files": {}})
        self._write_config(merge_config(read_json(self.paths.config_path)))
        if not self.paths.head_path.exists():
            self.paths.head_path.write_text("", encoding="utf-8")
        return self.paths.mach_dir

    def start_session(self, agent: str = "unknown", task_desc: str | None = None) -> dict[str, Any]:
        self.init_repo()
        with file_lock(self.paths.lock_path):
            return self._start_session_unlocked(agent=agent, task_desc=task_desc)

    def _start_session_unlocked(self, agent: str = "unknown", task_desc: str | None = None) -> dict[str, Any]:
        active = self.get_active_session_id()
        if active:
            return self.read_session_meta(active)

        return self._create_session_unlocked(agent=agent, task_desc=task_desc)

    def _check_concurrent_sessions(self, pre_commit: str | None) -> None:
        if not pre_commit:
            return
        try:
            active_count = 0
            for session_id in self._session_ids():
                meta = self.read_session_meta(session_id)
                if meta.get("status") == "active" and meta.get("pre_commit") == pre_commit:
                    active_count += 1
            if active_count > 0:
                import sys
                print(
                    f"\033[93mWarning\033[0m: There are {active_count} other active AI session(s) "
                    "modifying this same commit state concurrently.",
                    file=sys.stderr,
                )
        except Exception:
            pass

    def _create_session_unlocked(self, agent: str = "unknown", task_desc: str | None = None, agent_session_id: str | None = None) -> dict[str, Any]:
        session_id = f"ses_{uuid.uuid4().hex}"
        session_dir = self.paths.sessions_dir / session_id
        session_dir.mkdir(parents=True, exist_ok=True)

        pre_commit = head_commit(self.paths.repo_root)
        self._check_concurrent_sessions(pre_commit)

        meta = SessionMeta(
            id=session_id,
            started_at=int(time()),
            ended_at=None,
            agent=agent,
            branch=current_branch(self.paths.repo_root) or "main",
            remote=RemoteInfo(
                git=GitRemoteInfo(
                    url=remote_origin_url(self.paths.repo_root),
                    repository_name=repository_name(self.paths.repo_root),
                ),
                mach=MachSyncState(),
            ),
            pre_commit=pre_commit,
            post_commit=None,
            task_desc=task_desc,
            status="active",
            agent_session_id=agent_session_id,
            forked_from=None,
        ).to_dict()
        meta["step_count"] = 0
        meta["risk_count"] = 0
        self._write_session_meta(meta)
        write_json(session_dir / "merkle.sig", {"root": None, "steps": 0})
        (session_dir / "steps.jsonl").touch()
        self.paths.head_path.write_text(session_id, encoding="utf-8")
        return meta

    def end_session(self, session_id: str | None = None) -> dict[str, Any]:
        self.init_repo()
        with file_lock(self.paths.lock_path):
            return self._end_session_unlocked(session_id=session_id)

    def _end_session_unlocked(self, session_id: str | None = None) -> dict[str, Any]:
        target_id = session_id or self.get_active_session_id()
        if not target_id:
            raise MachError("No active session to end.")

        meta = self.read_session_meta(target_id)
        if meta["status"] == "ended":
            return meta

        meta["ended_at"] = int(time())
        meta["status"] = "ended"
        meta["post_commit"] = head_commit(self.paths.repo_root)
        self._refresh_meta_counts(meta, target_id)
        self._write_session_meta(meta)
        self._drop_agent_session_mapping_for_session(target_id)
        if self.get_active_session_id() == target_id:
            self.paths.head_path.write_text("", encoding="utf-8")
        return meta

    def get_active_session_id(self) -> str | None:
        if not self.paths.head_path.exists():
            return None
        raw = self.paths.head_path.read_text(encoding="utf-8").strip()
        return raw or None

    def record_step(self, step_dict: dict[str, Any]) -> dict[str, Any]:
        self.init_repo()
        with file_lock(self.paths.lock_path):
            active = self.get_active_session_id()
            session_id = active
            if not session_id:
                session_id = self._start_session_unlocked().get("id")

            meta = self.read_session_meta(session_id)
            if meta["status"] != "active":
                meta = self._start_session_unlocked(agent=meta.get("agent") or "unknown")
                session_id = meta["id"]

            return self._record_step_for_session_unlocked(session_id, step_dict)

    def record_agent_step(
        self,
        agent: str,
        step_dict: dict[str, Any],
        source_session_id: str | None = None,
        task_desc: str | None = None,
        end_session: bool = False,
    ) -> dict[str, Any]:
        self.init_repo()
        with file_lock(self.paths.lock_path):
            session_id = self._ensure_agent_session_unlocked(
                agent=agent,
                source_session_id=source_session_id,
                task_desc=task_desc,
            )
            payload = self._record_step_for_session_unlocked(session_id, step_dict)
            if end_session:
                self._end_session_unlocked(session_id)
            return payload

    def end_agent_session(self, agent: str, source_session_id: str | None = None) -> dict[str, Any]:
        self.init_repo()
        with file_lock(self.paths.lock_path):
            mappings = self._read_agent_sessions()
            key = self._agent_session_key(agent, source_session_id)
            session_id = mappings.get(key)
            if not session_id:
                raise MachError(f"No active mapped session for {key}.")
            ended = self._end_session_unlocked(session_id)
            self._drop_agent_session_mapping_for_session(session_id)
            return ended

    def verify_session(self, session_id: str) -> dict[str, Any]:
        if session_id == "HEAD":
            active = self.get_active_session_id()
            if not active:
                raise MachError("No active session exists.")
            session_id = active
        session_dir = self.paths.sessions_dir / session_id
        if not session_dir.exists():
            raise MachError(f"Unknown session: {session_id}")
        steps = read_jsonl(session_dir / "steps.jsonl")
        expected = read_json(session_dir / "merkle.sig")
        root = None
        for step in steps:
            root = chain_hash(step, root)
        return {
            "session_id": session_id,
            "valid": root == expected.get("root") and len(steps) == expected.get("steps"),
            "computed_root": root,
            "stored_root": expected.get("root"),
            "steps": len(steps),
        }

    def verify_all(self) -> list[dict[str, Any]]:
        self.init_repo()
        results = []
        for session_id in self._session_ids():
            results.append(self.verify_session(session_id))
        return results

    def _is_valid_session_id(self, session_id: str) -> bool:
        return session_id.startswith("ses_") and len(session_id) == 36

    def session_diff(self, session_id: str | None = None) -> dict[str, Any]:
        self.init_repo()
        target_id = self.get_active_session_id() if session_id in (None, "HEAD") else session_id
        if not target_id:
            raise MachError("No session specified and no active session exists.")

        session_dir = self.paths.sessions_dir / target_id
        if not session_dir.exists():
            raise MachError(f"Unknown session: {target_id}")

        meta = self.read_session_meta(target_id)
        steps = read_jsonl(session_dir / "steps.jsonl")

        file_map: dict[str, dict[str, Any]] = {}
        step_map: dict[str, dict[str, Any]] = {}
        total_added = 0
        total_removed = 0
        tool_calls = 0
        tool_names: dict[str, int] = {}

        for step in steps:
            stype = step.get("type", "")
            if stype == "tool":
                tool_name = (step.get("tool") or {}).get("name", "unknown")
                tool_calls += 1
                tool_names[tool_name] = tool_names.get(tool_name, 0) + 1
                tool_data = {
                    "name": tool_name,
                    "category": (step.get("tool") or {}).get("category", "exec"),
                }
            else:
                tool_data = None

            for change in step.get("file_changes") or []:
                step_id = step.get("id", "?")
                fp = change.get("file_path", "?")
                action = change.get("action", "write")
                added = change.get("lines_added") or 0
                removed = change.get("lines_removed") or 0
                hunks = change.get("hunks") or []
                change_entry = {
                    "file_path": fp,
                    "action": action,
                    "lines_added": added,
                    "lines_removed": removed,
                    "hunks": hunks,
                    "is_new": action == "write" and removed == 0 and added > 0,
                    "diff_source": "recorded" if hunks else "summary",
                }

                if fp not in file_map:
                    file_map[fp] = {
                        "file_path": fp,
                        "action": action,
                        "lines_added": 0,
                        "lines_removed": 0,
                        "hunks": [],
                        "step_ids": [],
                        "tool_names": [],
                        "is_new": False,
                        "steps": [],
                    }
                entry = file_map[fp]
                if action == "delete":
                    entry["action"] = "delete"
                elif entry["action"] != "delete":
                    entry["action"] = action
                entry["lines_added"] += added
                entry["lines_removed"] += removed
                if hunks:
                    entry["hunks"].extend(hunks)
                if step.get("id") and step["id"] not in entry["step_ids"]:
                    entry["step_ids"].append(step["id"])
                # if stype == "tool" and tool_name not in entry["tool_names"]:
                #     entry["tool_names"].append(tool_name)
                if tool_data and tool_data["name"] not in entry["tool_names"]:
                    entry["tool_names"].append(tool_data["name"])
                if action == "write" and entry["lines_removed"] == 0 and added > 0:
                    entry["is_new"] = True

                # Track steps that touched this file
                if step_id not in entry["step_ids"]:
                    entry["step_ids"].append(step_id)
                if stype in {"tool", "input", "output", "reasoning"}:
                    step_info = {
                        "step_id": step_id,
                        "step_type": stype,
                        "ts": step.get("ts"),
                        "tool_name": tool_data["name"] if tool_data else None,
                    }
                    if step_info not in entry["steps"]:
                        entry["steps"].append(step_info)

                if step_id not in step_map:
                    content = (step.get("tool") or {}).get("content") if stype == "tool" else step.get("content")
                    step_map[step_id] = {
                        "step_id": step_id,
                        "step_type": stype,
                        "ts": step.get("ts"),
                        "tool_name": tool_data["name"] if tool_data else None,
                        "tool_category": tool_data["category"] if tool_data else None,
                        "content": content,
                        "files": [],
                        "files_changed": 0,
                        "lines_added": 0,
                        "lines_removed": 0,
                        "diff_source": "recorded",
                    }
                step_entry = step_map[step_id]
                step_entry["files"].append(change_entry)
                step_entry["files_changed"] = len({f["file_path"] for f in step_entry["files"]})
                step_entry["lines_added"] += added
                step_entry["lines_removed"] += removed
                if change_entry["diff_source"] != "recorded":
                    step_entry["diff_source"] = "summary"

                total_added += added
                total_removed += removed

        if not file_map:
            for item in self._git_diff_name_status(meta):
                fp = item["file_path"]
                added, removed = self._git_diff_numstat_for_file(meta, fp)
                file_map[fp] = {
                    "file_path": fp,
                    "action": item["action"],
                    "lines_added": added,
                    "lines_removed": removed,
                    "hunks": [],
                    "step_ids": [],
                    "tool_names": [],
                    "is_new": item["action"] == "write" and item.get("status") == "A",
                    "steps": [],
                    "git_diff": self._git_diff_for_file(meta, fp),
                    "diff_source": "git",
                }
                total_added += added
                total_removed += removed
            if file_map:
                step_map["git_diff"] = {
                    "step_id": "git_diff",
                    "step_type": "git",
                    "ts": None,
                    "tool_name": None,
                    "tool_category": None,
                    "content": "Working tree diff inferred from Git because no recorded file-change steps were found.",
                    "files": list(file_map.values()),
                    "files_changed": len(file_map),
                    "lines_added": total_added,
                    "lines_removed": total_removed,
                    "diff_source": "git",
                }

        files = sorted(file_map.values(), key=lambda f: f["file_path"])
        for f in files:
            if not f.get("hunks") and not f.get("git_diff"):
                git_diff = self._git_diff_for_file(meta, f["file_path"])
                if git_diff:
                    f["git_diff"] = git_diff
                    f["diff_source"] = "git"
            f.pop("step_ids", None)
        steps_changed = sorted(
            step_map.values(),
            key=lambda s: (s.get("ts") is None, s.get("ts") or 0, s.get("step_id") or ""),
        )
        for step in steps_changed:
            for f in step.get("files") or []:
                if not f.get("hunks") and not f.get("git_diff"):
                    git_diff = self._git_diff_for_file(meta, f["file_path"])
                    if git_diff:
                        f["git_diff"] = git_diff
                        f["diff_source"] = "git"

        return {
            "meta": meta,
            "steps_changed": len(steps_changed),
            "files_changed": len(files),
            "total_added": total_added,
            "total_removed": total_removed,
            "tool_calls": tool_calls,
            "tool_names": tool_names,
            "steps": steps_changed,
            "files": files,
        }

    def _git_diff_args_for_session(self, meta: dict[str, Any], file_path: str | None = None) -> list[str]:
        pre_commit = meta.get("pre_commit")
        post_commit = meta.get("post_commit")
        args = ["diff"]
        if pre_commit and post_commit and pre_commit != post_commit:
            args.extend([str(pre_commit), str(post_commit)])
        elif pre_commit:
            args.append(str(pre_commit))
        if file_path:
            args.extend(["--", file_path])
        return args

    def _run_git_capture(self, args: list[str]) -> str:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=self.paths.repo_root,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
        except OSError:
            return ""
        if result.returncode not in (0, 1):
            return ""
        return result.stdout

    def _git_diff_for_file(self, meta: dict[str, Any], file_path: str) -> str:
        return self._run_git_capture(self._git_diff_args_for_session(meta, file_path)).strip()

    def _git_diff_name_status(self, meta: dict[str, Any]) -> list[dict[str, str]]:
        args = self._git_diff_args_for_session(meta)
        args.insert(1, "--name-status")
        output = self._run_git_capture(args)
        files = []
        for line in output.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            status = parts[0]
            path = parts[-1] if len(parts) > 1 else ""
            if not path:
                continue
            action = "delete" if status.startswith("D") else "write"
            files.append({"file_path": path, "action": action, "status": status})
        return files

    def _git_diff_numstat_for_file(self, meta: dict[str, Any], file_path: str) -> tuple[int, int]:
        args = self._git_diff_args_for_session(meta, file_path)
        args.insert(1, "--numstat")
        output = self._run_git_capture(args)
        added = 0
        removed = 0
        for line in output.splitlines():
            parts = line.split("\t")
            if len(parts) < 3 or parts[0] == "-" or parts[1] == "-":
                continue
            added += int(parts[0])
            removed += int(parts[1])
        return added, removed

    def list_sessions(self) -> list[dict[str, Any]]:
        self.init_repo()
        sessions = []
        for session in os.scandir(self.paths.sessions_dir):
            if self._is_valid_session_id(session.name):
                sessions.append(self.read_session_meta(session.name))
        sessions.sort(key=lambda item: item.get("started_at") or 0, reverse=True)
        return sessions

    def show_session(self, session_id: str | None = None) -> dict[str, Any]:
        self.init_repo()
        target_id = self.get_active_session_id() if session_id in (None, "HEAD") else session_id
        if not target_id:
            raise MachError("No session specified and no active session exists.")
        meta = self.read_session_meta(target_id)
        session_dir = self.paths.sessions_dir / target_id
        steps = read_jsonl(session_dir / "steps.jsonl")
        
        # Hydrate steps with blob content
        for step in steps:
            if step.get("content") is None and step.get("content_hash"):
                blob_content = self._read_blob(step["content_hash"])
                if blob_content is not None:
                    step["content"] = blob_content
            if step.get("tool") and step["tool"].get("content") is None and step["tool"].get("content_hash"):
                blob_content = self._read_blob(step["tool"]["content_hash"])
                if blob_content is not None:
                    step["tool"]["content"] = blob_content

        return {
            "meta": meta,
            "merkle": read_json(session_dir / "merkle.sig"),
            "steps": steps,
        }

    def resume_branch(self, branch: str | None = None) -> dict[str, Any]:
        self.init_repo()
        with file_lock(self.paths.lock_path):
            target_branch = branch or current_branch(self.paths.repo_root)
            candidates = [
                meta
                for meta in (self.read_session_meta(sid) for sid in self._session_ids())
                if meta.get("branch") == target_branch
            ]
            candidates.sort(key=lambda item: item.get("started_at") or 0, reverse=True)
            if not candidates:
                raise MachError(f"No previous sessions found for branch: {target_branch}")

            meta = candidates[0]
            session_id = meta["id"]
            if meta["status"] != "active":
                meta["status"] = "active"
                meta["ended_at"] = None
                meta["post_commit"] = None
                self._refresh_meta_counts(meta, session_id)
                self._write_session_meta(meta)
                self._record_step_for_session_unlocked(session_id, {
                    "type": "system_action",
                    "content": f"Session resumed on branch {target_branch}",
                    "risk_level": "none",
                })
                meta = self.read_session_meta(session_id)

            self.paths.head_path.write_text(session_id, encoding="utf-8")

            agent = meta.get("agent")
            agent_sid = meta.get("agent_session_id")
            if agent:
                mappings = self._read_agent_sessions()
                key = self._agent_session_key(agent, agent_sid)
                mappings[key] = session_id
                self._write_agent_sessions(mappings)

            return {
                "status": "resumed",
                "session_id": session_id,
                "agent_session_id": agent_sid,
                "agent": agent,
                "metadata": meta,
            }

    def rewind(self, target: str) -> dict[str, Any]:
        self.init_repo()
        import subprocess
        with file_lock(self.paths.lock_path):
            active = self.get_active_session_id()
            if not active:
                raise MachError("No active session to rewind within.")
                
            try:
                subprocess.check_call(
                    ["git", "restore", "--source", target, "--", "."],
                    cwd=str(self.paths.repo_root),
                    stderr=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL
                )
            except subprocess.CalledProcessError:
                raise MachError(f"Failed to rewind working directory to {target} (is it a valid commit/branch?)")
                
            payload = self._record_step_for_session_unlocked(active, {
                "type": "system_action",
                "content": f"User rewound workspace state to {target}",
                "risk_level": "none",
            })
            return {"status": "rewound", "target": target, "step_recorded": payload}

    def clean(self, max_days: int = 7) -> dict[str, Any]:
        self.init_repo()
        import shutil
        with file_lock(self.paths.lock_path):
            active = self.get_active_session_id()
            cutoff = int(time()) - (max_days * 86400)
            cleaned = []

            for sid in self._session_ids():
                if sid == active:
                    continue
                meta = self.read_session_meta(sid)
                if meta.get("status") == "active":
                    continue
                if (meta.get("started_at") or 0) >= cutoff:
                    continue
                if meta.get("post_commit"):
                    continue
                sdir = self.paths.sessions_dir / sid
                if sdir.exists():
                    shutil.rmtree(sdir)
                cleaned.append(sid)
            return {"cleaned": len(cleaned), "session_ids": cleaned}

    def on_commit(self) -> dict[str, Any] | None:
        config = self.read_config()
        if not config.get("commit_closes_session", False):
            return None
        active = self.get_active_session_id()
        if not active:
            return None
        with file_lock(self.paths.lock_path):
            return self._end_session_unlocked(active)

    def read_config(self) -> dict[str, Any]:
        self.init_repo()
        return merge_config(read_json(self.paths.config_path))

    def update_config(self, updates: dict[str, Any]) -> dict[str, Any]:
        self.init_repo()
        with file_lock(self.paths.lock_path):
            current = merge_config(read_json(self.paths.config_path))
            current.update(updates)
            self._write_config(current)
            return current

    def read_tracked_repo(self) -> RepositoryDetails | None:
        self.init_repo()
        if not self.paths.tracked_repo_path.exists():
            return None
        return RepositoryDetails.from_dict(read_json(self.paths.tracked_repo_path))

    def write_tracked_repo(self, repository: RepositoryDetails) -> RepositoryDetails:
        self.init_repo()
        with file_lock(self.paths.lock_path):
            write_json(self.paths.tracked_repo_path, repository.to_dict())
            return repository

    # ── remote-format helpers ────────────────────────────────────────────────

    @staticmethod
    def _normalize_remote(raw: dict[str, Any]) -> dict[str, Any]:
        """Ensure the remote dict is in the canonical nested {git, mach} format.

        Handles three cases transparently:
          1. Already in new format  → strips any stale flat keys and returns clean dict.
          2. Old flat format        → migrates url/repository_name → git,
                                      all push-tracking fields → mach.
          3. Empty / None           → returns a zeroed-out nested dict.
        """
        if not raw:
            return {"git": {}, "mach": {}}

        already_nested = "git" in raw or "mach" in raw

        if already_nested:
            # Accept the nested sub-dicts, drop any leftover flat keys.
            return {
                "git": dict(raw.get("git") or {}),
                "mach": dict(raw.get("mach") or {}),
            }

        # Old flat format — split by concern.
        return {
            "git": {
                "url": raw.get("url"),
                "repository_name": raw.get("repository_name"),
            },
            "mach": {
                "last_push_id": raw.get("last_push_id"),
                "last_pushed_at": raw.get("last_pushed_at"),
                "last_pushed_ts": raw.get("last_pushed_ts", 0),
                "last_pushed_step_id": raw.get("last_pushed_step_id"),
                "pushed_root": raw.get("pushed_root"),
                "server_session_id": raw.get("server_session_id"),
                "server_root_before": raw.get("server_root_before"),
                "server_root_after": raw.get("server_root_after"),
                "blobs_received": raw.get("blobs_received"),
                "steps_received": raw.get("steps_received"),
                "last_pulled_at": raw.get("last_pulled_at"),
                "last_pulled_ts": raw.get("last_pulled_ts", 0),
                "last_pulled_step_id": raw.get("last_pulled_step_id"),
            },
        }

    def update_push_state(
        self,
        session_id: str,
        *,
        git_updates: dict[str, Any] | None = None,
        mach_updates: dict[str, Any] | None = None,
        step_count: int | None = None,
        risk_count: int | None = None,
    ) -> dict[str, Any]:
        self.init_repo()
        with file_lock(self.paths.lock_path):
            meta = self.read_session_meta(session_id)
            # Normalize to nested format — migrates old flat meta.json files
            # transparently on the first write after the refactor.
            remote = self._normalize_remote(dict(meta.get("remote") or {}))
            if git_updates:
                remote["git"].update(git_updates)
            if mach_updates:
                remote["mach"].update(mach_updates)
            meta["remote"] = remote
            if step_count is not None:
                meta["step_count"] = step_count
            if risk_count is not None:
                meta["risk_count"] = risk_count
            if step_count is None or risk_count is None:
                self._refresh_meta_counts(meta, session_id)
            self._write_session_meta(meta)
            return meta

    def clone_session(self, source_session_id: str) -> dict[str, Any]:
        self.init_repo()
        with file_lock(self.paths.lock_path):
            source_dir = self.paths.sessions_dir / source_session_id
            if not source_dir.exists():
                raise MachError(f"Unknown session: {source_session_id}")

            source_meta = self.read_session_meta(source_session_id)
            source_steps = read_jsonl(source_dir / "steps.jsonl")
            source_merkle = read_json(source_dir / "merkle.sig")

            clone_id = f"ses_{uuid.uuid4().hex}"
            clone_dir = self.paths.sessions_dir / clone_id
            clone_dir.mkdir(parents=True, exist_ok=False)

            now = int(time())
            remote = self._normalize_remote(dict(source_meta.get("remote") or {}))
            last_inherited_step_id: str | None = None
            id_map: dict[str, str] = {}
            cloned_steps: list[dict[str, Any]] = []

            for index, step in enumerate(source_steps, start=1):
                cloned = dict(step)
                original_step_id = str(cloned.get("id") or "")
                cloned_step_id = f"step_{uuid.uuid4().hex}"
                if original_step_id:
                    id_map[original_step_id] = cloned_step_id

                original_causes = list(cloned.get("caused_by") or [])
                cloned["id"] = cloned_step_id
                cloned["session_id"] = clone_id
                cloned["step_num"] = index
                cloned["_original_caused_by"] = original_causes
                cloned_steps.append(cloned)
                last_inherited_step_id = cloned_step_id

            for cloned in cloned_steps:
                caused_by = cloned.pop("_original_caused_by", [])
                mapped = [id_map.get(step_id, step_id) for step_id in caused_by if step_id]
                if not mapped and cloned["step_num"] > 1:
                    mapped = [cloned_steps[cloned["step_num"] - 2]["id"]]
                cloned["caused_by"] = mapped
                
                # Also map parent_step_id to the cloned step's parent
                old_parent = cloned.get("parent_step_id")
                if old_parent:
                    cloned["parent_step_id"] = id_map.get(old_parent, old_parent)

            mach_state = remote.setdefault("mach", {})
            mach_state.update({
                "last_pushed_step_id": last_inherited_step_id,
                "last_pushed_ts": now if last_inherited_step_id else 0,
                "last_pulled_step_id": last_inherited_step_id,
                "last_pulled_ts": now if last_inherited_step_id else 0,
                "last_pulled_at": str(now) if last_inherited_step_id else None,
                "forked_from_session_id": source_session_id,
                "forked_from_root": source_merkle.get("root"),
            })

            cloned_meta = dict(source_meta)
            cloned_meta.update({
                "id": clone_id,
                "started_at": now,
                "ended_at": None,
                "status": "active",
                "branch": current_branch(self.paths.repo_root),
                "pre_commit": head_commit(self.paths.repo_root),
                "post_commit": None,
                "forked_from": source_session_id,
                "remote": remote,
                "head_step_id": last_inherited_step_id,
                "step_count": len(cloned_steps),
                "risk_count": self._risk_count_from_steps(cloned_steps),
            })

            root = None
            for cloned in cloned_steps:
                append_jsonl(clone_dir / "steps.jsonl", cloned)
                root = chain_hash(cloned, root)
            if not cloned_steps:
                (clone_dir / "steps.jsonl").touch()
            self._write_session_meta(cloned_meta)
            write_json(clone_dir / "merkle.sig", {"root": root, "steps": len(cloned_steps)})

            self.paths.head_path.write_text(clone_id, encoding="utf-8")

            return {
                "cloned": True,
                "session_id": clone_id,
                "forked_from": source_session_id,
                "step_count": len(cloned_steps),
                "last_pulled_step_id": last_inherited_step_id,
                "metadata": cloned_meta,
            }

    def clone_remote_session(
        self,
        source_session_id: str,
        details: PullSessionDetails,
        source_steps: list[dict[str, Any]],
        source_blobs: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        self.init_repo()
        with file_lock(self.paths.lock_path):
            clone_id = f"ses_{uuid.uuid4().hex}"
            clone_dir = self.paths.sessions_dir / clone_id
            clone_dir.mkdir(parents=True, exist_ok=False)

            now = int(time())
            remote = RemoteInfo(
                git=GitRemoteInfo(
                    url=details.repository.remote_url or remote_origin_url(self.paths.repo_root),
                    repository_name=details.repository.name or repository_name(self.paths.repo_root),
                ),
                mach=MachSyncState(
                    server_session_id=details.id,
                    server_root_after=details.merkle_root,
                    last_pulled_at=details.synced_at or details.modified or details.created,
                    last_pulled_ts=now,
                ),
            ).to_dict()

            last_inherited_step_id: str | None = None
            id_map: dict[str, str] = {}
            cloned_steps: list[dict[str, Any]] = []
            blob_count = self._write_remote_blobs_unlocked(source_blobs or [])

            for index, step in enumerate(source_steps, start=1):
                tool_data = step.get("tool")
                fc_data = step.get("file_changes") or []
                caused_by = list(step.get("caused_by") or [])
                original_step_id = str(
                    step.get("mach_id")
                    or step.get("step_id")
                    or step.get("id")
                    or f"remote_step_{index}"
                )
                cloned_step_id = f"step_{uuid.uuid4().hex}"
                id_map[original_step_id] = cloned_step_id

                cloned = Step(
                    id=cloned_step_id,
                    session_id=clone_id,
                    step_num=index,
                    ts=int(step.get("ts") or step.get("timestamp") or now),
                    type=step.get("type") or step.get("step_type") or "output",
                    content_hash=step.get("content_hash"),
                    content=step.get("content"),
                    caused_by=[],
                    risk_level=step.get("risk_level") or "none",
                    risk_flags=list(step.get("risk_flags") or []),
                    tool=ToolCall.from_dict(tool_data) if isinstance(tool_data, dict) else None,
                    file_changes=[FileChange.from_dict(fc) for fc in fc_data],
                    commit_hash=step.get("commit_hash"),
                    parent_step_id=step.get("parent_step_id"),
                ).to_dict()
                cloned["_original_caused_by"] = caused_by
                cloned_steps.append(cloned)
                last_inherited_step_id = cloned_step_id

            for cloned in cloned_steps:
                caused_by = cloned.pop("_original_caused_by", [])
                mapped = [id_map.get(step_id, step_id) for step_id in caused_by if step_id]
                if not mapped and cloned["step_num"] > 1:
                    mapped = [cloned_steps[cloned["step_num"] - 2]["id"]]
                cloned["caused_by"] = mapped

                # Also map parent_step_id to the cloned step's parent
                old_parent = cloned.get("parent_step_id")
                if old_parent:
                    cloned["parent_step_id"] = id_map.get(old_parent, old_parent)

            mach_state = remote.setdefault("mach", {})
            mach_state.update({
                "last_pushed_step_id": last_inherited_step_id,
                "last_pushed_ts": now if last_inherited_step_id else 0,
                "last_pulled_step_id": last_inherited_step_id,
                "last_pulled_ts": now if last_inherited_step_id else 0,
                "last_pulled_at": details.synced_at or details.modified or details.created or str(now),
                "forked_from_session_id": source_session_id,
                "forked_from_root": details.merkle_root,
            })

            cloned_meta = SessionMeta(
                id=clone_id,
                started_at=now,
                ended_at=None,
                agent=details.agent_name or "unknown",
                branch=current_branch(self.paths.repo_root) or details.branch or "main",
                remote=RemoteInfo.from_dict(remote),
                pre_commit=head_commit(self.paths.repo_root),
                post_commit=None,
                task_desc=details.task_desc,
                status="active",
                agent_session_id=details.agent_session_id,
                forked_from=source_session_id,
                head_step_id=last_inherited_step_id,
                step_count=len(cloned_steps),
                risk_count=self._risk_count_from_steps(cloned_steps),
            ).to_dict()

            root = None
            for cloned in cloned_steps:
                append_jsonl(clone_dir / "steps.jsonl", cloned)
                root = chain_hash(cloned, root)
            if not cloned_steps:
                (clone_dir / "steps.jsonl").touch()
            self._write_session_meta(cloned_meta)
            write_json(clone_dir / "merkle.sig", {"root": root, "steps": len(cloned_steps)})

            self.paths.head_path.write_text(clone_id, encoding="utf-8")

            return {
                "cloned": True,
                "session_id": clone_id,
                "forked_from": source_session_id,
                "step_count": len(cloned_steps),
                "blob_count": blob_count,
                "last_pulled_step_id": last_inherited_step_id,
                "metadata": cloned_meta,
            }

    def _write_remote_blobs_unlocked(self, blobs: list[dict[str, Any]]) -> int:
        written = 0
        for blob in blobs:
            content_hash = blob.get("content_hash")
            content = blob.get("content")
            if not content_hash or content is None:
                continue
            self._write_blob(str(content_hash), str(content))
            written += 1
        return written

    def fsck(self) -> dict[str, Any]:
        """Verify JSONL ledgers, merkle roots, and meta counters. No secondary index."""
        self.init_repo()
        with file_lock(self.paths.lock_path):
            verification = []
            sessions_checked = 0
            steps_checked = 0
            meta_repaired = 0
            missing_blobs = 0

            for session_id in self._session_ids():
                result = self.verify_session(session_id)
                verification.append(result)

                session_dir = self.paths.sessions_dir / session_id
                meta = read_json(session_dir / "meta.json")
                steps = read_jsonl(session_dir / "steps.jsonl")
                step_count = len(steps)
                risk_count = self._risk_count_from_steps(steps)
                head_step_id = steps[-1]["id"] if steps else None

                if (
                    meta.get("step_count") != step_count
                    or meta.get("risk_count") != risk_count
                    or meta.get("head_step_id") != head_step_id
                ):
                    meta["step_count"] = step_count
                    meta["risk_count"] = risk_count
                    meta["head_step_id"] = head_step_id
                    if "remote" in meta:
                        meta["remote"] = self._normalize_remote(dict(meta.get("remote") or {}))
                    write_json(session_dir / "meta.json", meta)
                    meta_repaired += 1

                for step in steps:
                    content_hash = step.get("content_hash")
                    if content_hash and step.get("content") is None and self._read_blob(content_hash) is None:
                        missing_blobs += 1
                    tool = step.get("tool") or {}
                    tool_hash = tool.get("content_hash")
                    if tool_hash and tool.get("content") is None and self._read_blob(tool_hash) is None:
                        missing_blobs += 1

                sessions_checked += 1
                steps_checked += step_count

            active = self.get_active_session_id()
            if active and not (self.paths.sessions_dir / active).exists():
                self.paths.head_path.write_text("", encoding="utf-8")
                active = None

            return {
                "ok": all(item["valid"] for item in verification) and missing_blobs == 0,
                "sessions_checked": sessions_checked,
                "steps_checked": steps_checked,
                "meta_repaired": meta_repaired,
                "missing_blobs": missing_blobs,
                "active_session": active,
                "verification": verification,
            }

    def fix_sessions(self, session_id: str | None = None, *, apply: bool = False) -> dict[str, Any]:
        self.init_repo()
        with file_lock(self.paths.lock_path):
            session_ids = [session_id] if session_id else self._session_ids()
            results = []
            for sid in session_ids:
                session_dir = self.paths.sessions_dir / sid
                if not session_dir.exists():
                    raise MachError(f"Unknown session: {sid}")
                results.append(self._fix_session_chunks_unlocked(sid, apply=apply))
            return {
                "applied": apply,
                "sessions_checked": len(results),
                "sessions_changed": sum(1 for item in results if item["changed"]),
                "merged_steps": sum(item["merged_steps"] for item in results),
                "normalized_tool_hashes": sum(item["normalized_tool_hashes"] for item in results),
                "linked_list_fixes": sum(1 for item in results if item.get("linked_list_fixed")),
                "backfilled_file_changes": sum(item.get("backfilled_file_changes", 0) for item in results),
                "results": results,
            }

    def read_session_meta(self, session_id: str) -> dict[str, Any]:
        meta = read_json(self.paths.sessions_dir / session_id / "meta.json")
        if meta and "remote" in meta:
            meta["remote"] = self._normalize_remote(dict(meta.get("remote") or {}))
        return meta

    def _fix_session_chunks_unlocked(self, session_id: str, *, apply: bool) -> dict[str, Any]:
        session_dir = self.paths.sessions_dir / session_id
        steps = read_jsonl(session_dir / "steps.jsonl")
        normalized, id_map, merged_steps, normalized_tool_hashes, backfilled_file_changes = self._normalize_steps(steps)

        # Check if parent_step_id is missing on any step or if head_step_id is missing in session meta.
        # This allows the 'fix' command to act as a migration utility to backfill these fields!
        meta = self.read_session_meta(session_id)
        has_missing_linked_list_fields = False
        if not meta or not meta.get("head_step_id"):
            has_missing_linked_list_fields = True
        else:
            for step in steps:
                if "parent_step_id" not in step or step["parent_step_id"] is None:
                    # Let the first step have parent_step_id None, but any subsequent step must have parent_step_id set
                    if step.get("step_num", 1) > 1:
                        has_missing_linked_list_fields = True
                        break

        changed = merged_steps > 0 or normalized_tool_hashes > 0 or has_missing_linked_list_fields or backfilled_file_changes > 0

        if apply and changed:
            self._write_normalized_session_unlocked(session_id, normalized, id_map)

        return {
            "session_id": session_id,
            "before_steps": len(steps),
            "after_steps": len(normalized),
            "merged_steps": merged_steps,
            "normalized_tool_hashes": normalized_tool_hashes,
            "linked_list_fixed": has_missing_linked_list_fields,
            "backfilled_file_changes": backfilled_file_changes,
            "changed": changed,
        }

    def _normalize_steps(self, steps: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, str], int, int, int]:
        mergeable_types = {"input", "reasoning", "output"}
        normalized: list[dict[str, Any]] = []
        id_map: dict[str, str] = {}
        merged_steps = 0
        normalized_tool_hashes = 0
        backfilled_file_changes = 0

        for step in steps:
            current = dict(step)
            if self._normalize_tool_step_hash(current):
                normalized_tool_hashes += 1
            if self._backfill_tool_details(current):
                backfilled_file_changes += 1
            current_id = str(current.get("id") or "")
            if current_id:
                id_map[current_id] = current_id

            if (
                normalized
                and self._can_merge_step_chunks(normalized[-1], current, mergeable_types)
            ):
                target = normalized[-1]
                target_id = str(target.get("id") or "")
                if current_id and target_id:
                    id_map[current_id] = target_id
                target["_merged_content"] = self._step_text(target) + self._step_text(current)
                target.setdefault("_merged_from", []).append(current_id)
                merged_steps += 1
                continue

            normalized.append(current)

        for index, step in enumerate(normalized, start=1):
            step["step_num"] = index
            caused_by = []
            for cause in step.get("caused_by") or []:
                mapped = id_map.get(cause, cause)
                if mapped and mapped != step.get("id") and mapped not in caused_by:
                    caused_by.append(mapped)
            step["caused_by"] = caused_by

        return normalized, id_map, merged_steps, normalized_tool_hashes, backfilled_file_changes

    def _normalize_tool_step_hash(self, step: dict[str, Any]) -> bool:
        tool = step.get("tool")
        if step.get("type") != "tool" or not isinstance(tool, dict):
            return False

        tool_hash = tool.get("content_hash")
        tool_content = tool.get("content")
        if not tool_hash and tool_content is not None:
            tool_hash = hash_payload({"content": str(tool_content)})
            tool["content_hash"] = tool_hash

        if not tool_hash or step.get("content_hash") == tool_hash:
            return False

        step["content_hash"] = tool_hash
        step.pop("content", None)
        return True

    def _backfill_tool_details(self, step: dict[str, Any]) -> bool:
        if step.get("type") != "tool":
            return False
        tool = step.get("tool")
        if not isinstance(tool, dict):
            return False

        modified = False
        tool_name = tool.get("name", "")

        has_category = tool.get("category") not in (None, "", "exec")
        has_changes = bool(step.get("file_changes"))

        if has_category and has_changes:
            return False

        tool_content = tool.get("content")
        if tool_content is None and tool.get("content_hash"):
            tool_content = self._read_blob(tool["content_hash"])

        if tool_content is not None:
            from mach.hooks.helpers import extract_tool_details
            try:
                category, file_changes = extract_tool_details(self.paths.repo_root, tool_name, tool_content)
                if not has_category and category and category != tool.get("category"):
                    tool["category"] = category
                    modified = True
                if not has_changes and file_changes:
                    step["file_changes"] = file_changes
                    modified = True
            except Exception:
                pass
        return modified

    def _can_merge_step_chunks(self, previous: dict[str, Any], current: dict[str, Any], mergeable_types: set[str]) -> bool:
        step_type = previous.get("type")
        if step_type != current.get("type") or step_type not in mergeable_types:
            return False
        blocked_fields = ("tool", "file_changes", "risk_flags")
        return not any(previous.get(field) or current.get(field) for field in blocked_fields)

    def _step_text(self, step: dict[str, Any]) -> str:
        if "_merged_content" in step:
            return str(step.get("_merged_content") or "")
        if step.get("content") is not None:
            return str(step.get("content") or "")
        blob = self._read_blob(step.get("content_hash"))
        return blob or ""

    def _write_normalized_session_unlocked(
        self,
        session_id: str,
        steps: list[dict[str, Any]],
        id_map: dict[str, str],
    ) -> None:
        session_dir = self.paths.sessions_dir / session_id
        steps_path = session_dir / "steps.jsonl"
        merkle_path = session_dir / "merkle.sig"
        meta = self.read_session_meta(session_id)
        config = self.read_config()
        store_content = config.get("store_content", ["input", "output", "reasoning", "tool"])

        root = None
        steps_path.write_text("", encoding="utf-8")
        prev_step_id = None
        for step in steps:
            if "parent_step_id" not in step or step["parent_step_id"] is None:
                step["parent_step_id"] = prev_step_id

            content = step.pop("_merged_content", None)
            step.pop("_merged_from", None)
            if content is not None:
                content_hash = hash_payload({"content": content})
                step["content_hash"] = content_hash
                if step.get("type") == "system_action":
                    step["content"] = content
                else:
                    step.pop("content", None)
                    if step.get("type") in store_content:
                        self._write_blob(content_hash, content)

            append_jsonl(steps_path, step)
            root = chain_hash(step, root)
            prev_step_id = step["id"]

        remote = self._normalize_remote(dict(meta.get("remote") or {}))
        mach = remote.setdefault("mach", {})
        for key in ("last_pushed_step_id", "last_pulled_step_id"):
            value = mach.get(key)
            if value in id_map:
                mach[key] = id_map[value]
        meta["remote"] = remote
        meta["head_step_id"] = prev_step_id
        meta["step_count"] = len(steps)
        meta["risk_count"] = self._risk_count_from_steps(steps)
        self._write_session_meta(meta)
        write_json(merkle_path, {"root": root, "steps": len(steps)})

    def _merge_new_step_into_previous(
        self,
        previous: dict[str, Any],
        current: dict[str, Any],
        current_content: str,
        store_content: list[str],
    ) -> dict[str, Any]:
        merged = dict(previous)
        content = self._step_text(previous) + current_content
        content_hash = hash_payload({"content": content})
        merged["content_hash"] = content_hash
        merged["ts"] = current.get("ts", merged.get("ts"))
        if merged.get("type") == "system_action":
            merged["content"] = content
        else:
            merged.pop("content", None)
            if merged.get("type") in store_content:
                self._write_blob(content_hash, content)
        return merged

    def _rewrite_session_steps_unlocked(self, session_id: str, steps: list[dict[str, Any]]) -> None:
        session_dir = self.paths.sessions_dir / session_id
        steps_path = session_dir / "steps.jsonl"
        merkle_path = session_dir / "merkle.sig"

        root = None
        steps_path.write_text("", encoding="utf-8")
        prev_step_id = None
        for index, step in enumerate(steps, start=1):
            step["step_num"] = index
            if "parent_step_id" not in step or step["parent_step_id"] is None:
                step["parent_step_id"] = prev_step_id
            append_jsonl(steps_path, step)
            root = chain_hash(step, root)
            prev_step_id = step["id"]
        write_json(merkle_path, {"root": root, "steps": len(steps)})

        try:
            meta = self.read_session_meta(session_id)
            meta["head_step_id"] = prev_step_id
            meta["step_count"] = len(steps)
            meta["risk_count"] = self._risk_count_from_steps(steps)
            self._write_session_meta(meta)
        except Exception:
            pass

    def _write_config(self, config: dict[str, Any]) -> None:
        write_json(self.paths.config_path, config)

    def _write_session_meta(self, meta: dict[str, Any]) -> None:
        if "remote" in meta:
            meta["remote"] = self._normalize_remote(dict(meta.get("remote") or {}))
        write_json(self.paths.sessions_dir / meta["id"] / "meta.json", meta)

    def _read_agent_sessions(self) -> dict[str, str]:
        return read_json(self.paths.agent_sessions_path)

    def _write_agent_sessions(self, mappings: dict[str, str]) -> None:
        write_json(self.paths.agent_sessions_path, mappings)

    @staticmethod
    def _agent_session_key(agent: str, source_session_id: str | None) -> str:
        return f"{agent}:{source_session_id or 'default'}"

    def _ensure_agent_session_unlocked(
        self,
        agent: str,
        source_session_id: str | None = None,
        task_desc: str | None = None,
    ) -> str:
        mappings = self._read_agent_sessions()
        key = self._agent_session_key(agent, source_session_id)
        session_id = mappings.get(key)
        if session_id and (self.paths.sessions_dir / session_id / "meta.json").exists():
            meta = self.read_session_meta(session_id)
            if meta.get("status") == "active":
                self.paths.head_path.write_text(session_id, encoding="utf-8")
                return session_id

        meta = self._create_session_unlocked(agent=agent, task_desc=task_desc, agent_session_id=source_session_id)
        mappings[key] = meta["id"]
        self._write_agent_sessions(mappings)
        return meta["id"]

    def _write_blob(self, content_hash: str, content: str) -> None:
        if not content or not content_hash:
            return
        blob_path = self.paths.blobs_dir / content_hash[:2] / content_hash
        if not blob_path.exists():
            blob_path.parent.mkdir(parents=True, exist_ok=True)
            blob_path.write_text(content, encoding="utf-8")

    def _read_blob(self, content_hash: str) -> str | None:
        if not content_hash:
            return None
        blob_path = self.paths.blobs_dir / content_hash[:2] / content_hash
        if blob_path.exists():
            return blob_path.read_text(encoding="utf-8")
        return None

    def _drop_agent_session_mapping_for_session(self, session_id: str) -> None:
        mappings = self._read_agent_sessions()
        updated = {
            key: value
            for key, value in mappings.items()
            if value != session_id
        }
        if updated != mappings:
            self._write_agent_sessions(updated)

    def _record_step_for_session_unlocked(self, session_id: str, step_dict: dict[str, Any]) -> dict[str, Any]:
        meta = self.read_session_meta(session_id)
        session_dir = self.paths.sessions_dir / session_id
        steps_path = session_dir / "steps.jsonl"
        merkle_path = session_dir / "merkle.sig"

        existing_steps = read_jsonl(steps_path)
        prev_step_id = existing_steps[-1]["id"] if existing_steps else None
        step_num = len(existing_steps) + 1

        config = self.read_config()
        store_content = config.get("store_content", ["input", "output", "reasoning", "tool"])

        step_id = step_dict.get("id", f"step_{uuid.uuid4().hex}")
        ts = step_dict.get("ts", int(time()))
        step_type = step_dict.get("type", "output")
        raw_content = step_dict.get("content", "")
        raw_t_content = ""
        if step_type == "tool" and isinstance(step_dict.get("tool"), dict):
            raw_t_content = str(step_dict["tool"].get("content") or "")
            raw_content = raw_t_content or raw_content
        content_hash = hash_payload({"content": raw_content})

        final_content = None
        if step_type != "system_action" and step_type not in store_content:
            pass # discard content
        elif step_type != "system_action":
            if raw_content:
                self._write_blob(content_hash, raw_content)
        else:
            final_content = raw_content

        tool_obj = None
        if "tool" in step_dict:
            t = dict(step_dict["tool"])
            raw_t_content = str(t.get("content") or "")
            t_content_hash = content_hash if step_type == "tool" else hash_payload({"content": raw_t_content})
            
            if "tool" in store_content and raw_t_content:
                self._write_blob(t_content_hash, raw_t_content)
                
            from mach.models import ToolCall
            tool_obj = ToolCall(
                name=t.get("name", ""),
                category=t.get("category", "exec"),
                content_hash=t_content_hash,
                content=None
            )

        from mach.models import Step, FileChange

        fc_data = step_dict.get("file_changes", [])
        file_changes = [FileChange.from_dict(fc) for fc in fc_data] if fc_data else []

        risk_probe = {
            "type": step_type,
            "content": raw_content,
            "risk_level": step_dict.get("risk_level", "none"),
            "risk_flags": list(step_dict.get("risk_flags") or []),
            "tool": dict(step_dict["tool"]) if isinstance(step_dict.get("tool"), dict) else None,
            "file_changes": [
                {
                    "action": fc.action,
                    "file_path": fc.file_path,
                    "lines_added": fc.lines_added,
                    "lines_removed": fc.lines_removed,
                    "hunks": fc.hunks,
                }
                for fc in file_changes
            ],
        }
        risk_flags, risk_level = evaluate_step_risk(
            risk_probe,
            config,
            content_text=raw_content,
        )

        step_obj = Step(
            id=step_id,
            session_id=session_id,
            step_num=step_num,
            ts=ts,
            type=step_type,
            content_hash=content_hash,
            content=final_content,
            caused_by=step_dict.get("caused_by", [prev_step_id] if prev_step_id else []),
            risk_level=risk_level,
            risk_flags=risk_flags,
            tool=tool_obj,
            file_changes=file_changes,
            commit_hash=head_commit(self.paths.repo_root),
            parent_step_id=prev_step_id
        )

        payload = step_obj.to_dict()

        if existing_steps and self._can_merge_step_chunks(existing_steps[-1], payload, {"input", "reasoning", "output"}):
            merged_payload = self._merge_new_step_into_previous(existing_steps[-1], payload, raw_content, store_content)
            existing_steps[-1] = merged_payload
            self._rewrite_session_steps_unlocked(session_id, existing_steps)
            self.paths.head_path.write_text(session_id, encoding="utf-8")
            return merged_payload

        append_jsonl(steps_path, payload)

        merkle = read_json(merkle_path)
        root = chain_hash(payload, merkle.get("root"))
        merkle["root"] = root
        merkle["steps"] = step_num
        write_json(merkle_path, merkle)

        self.paths.head_path.write_text(session_id, encoding="utf-8")
        meta["head_step_id"] = step_id
        meta["step_count"] = step_num
        meta["risk_count"] = self._risk_count_from_steps(existing_steps + [payload])
        self._write_session_meta(meta)
        return payload

    def _session_ids(self) -> list[str]:
        if not self.paths.sessions_dir.exists():
            return []
        return [
            session_dir.name
            for session_dir in sorted(self.paths.sessions_dir.iterdir())
            if session_dir.is_dir() and self._is_valid_session_id(session_dir.name)
        ]

    @staticmethod
    def _risk_count_from_steps(steps: list[dict[str, Any]]) -> int:
        return sum(len(step.get("risk_flags") or []) for step in steps)

    def _step_count(self, session_id: str) -> int:
        steps_path = self.paths.sessions_dir / session_id / "steps.jsonl"
        if not steps_path.exists():
            return 0
        return len(read_jsonl(steps_path))

    def _refresh_meta_counts(self, meta: dict[str, Any], session_id: str) -> dict[str, Any]:
        steps = read_jsonl(self.paths.sessions_dir / session_id / "steps.jsonl")
        meta["step_count"] = len(steps)
        meta["risk_count"] = self._risk_count_from_steps(steps)
        if steps:
            meta["head_step_id"] = steps[-1].get("id")
        return meta
