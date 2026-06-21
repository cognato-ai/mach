from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path
from time import time
from typing import Any

from mach.config import DEFAULT_CONFIG, merge_config
from mach.db import init_db
from mach.git_utils import current_branch, head_commit, remote_origin_url, repository_name
from mach.locking import file_lock
from mach.models import (
    GitRemoteInfo,
    MachSyncState,
    RemoteInfo,
    RepositoryDetails,
)
from mach.repository import resolve_paths
from mach.utils import (
    ensure_json_file,
    read_json,
    write_json,
)

class MachError(RuntimeError):
    pass

class SessionBase:
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
        init_db(self.paths.db_path)
        return self.paths.mach_dir

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

    def _write_config(self, config: dict[str, Any]) -> None:
        write_json(self.paths.config_path, config)

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

    @staticmethod
    def _normalize_remote(raw: dict[str, Any]) -> dict[str, Any]:
        if not raw:
            return {"git": {}, "mach": {}}

        already_nested = "git" in raw or "mach" in raw

        if already_nested:
            return {
                "git": dict(raw.get("git") or {}),
                "mach": dict(raw.get("mach") or {}),
            }

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
            remote = self._normalize_remote(dict(meta.get("remote") or {}))
            if git_updates:
                remote["git"].update(git_updates)
            if mach_updates:
                remote["mach"].update(mach_updates)
            meta["remote"] = remote
            self._write_session_meta(meta)
            self._upsert_session_index(
                meta,
                step_count=step_count if step_count is not None else self._step_count(session_id),
                risk_count=risk_count if risk_count is not None else self._risk_count(session_id),
            )
            return meta

    def read_session_meta(self, session_id: str) -> dict[str, Any]:
        meta = read_json(self.paths.sessions_dir / session_id / "meta.json")
        if meta and "remote" in meta:
            meta["remote"] = self._normalize_remote(dict(meta.get("remote") or {}))
        return meta

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

    def _drop_agent_session_mapping_for_session(self, session_id: str) -> None:
        mappings = self._read_agent_sessions()
        updated = {
            key: value
            for key, value in mappings.items()
            if value != session_id
        }
        if updated != mappings:
            self._write_agent_sessions(updated)

    def _is_valid_session_id(self, session_id: str) -> bool:
        return session_id.startswith("ses_") and len(session_id) == 36

    def _session_ids(self) -> list[str]:
        if not self.paths.sessions_dir.exists():
            return []
        return [
            session_dir.name
            for session_dir in sorted(self.paths.sessions_dir.iterdir())
            if session_dir.is_dir()
        ]

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
