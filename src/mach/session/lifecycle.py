from __future__ import annotations

import os
import shutil
import uuid
from time import time
from typing import Any

from mach.db import connect, reset_db
from mach.git_utils import current_branch, head_commit, remote_origin_url, repository_name
from mach.merkle import chain_hash, hash_payload
from mach.models import (
    FileChange,
    GitRemoteInfo,
    MachSyncState,
    PullSessionDetails,
    RemoteInfo,
    SessionMeta,
    Step,
    ToolCall,
)
from mach.utils import (
    append_jsonl,
    read_json,
    read_jsonl,
    write_json,
)
from mach.session.fix import SessionFixMixin
from mach.session.base import MachError

class SessionLifecycleMixin(SessionFixMixin):
    def start_session(self, agent: str = "unknown", task_desc: str | None = None) -> dict[str, Any]:
        self.init_repo()
        with self.file_lock_context():
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
            with connect(self.paths.db_path) as conn:
                row = conn.execute(
                    "SELECT COUNT(*) as c FROM sessions WHERE ended_at IS NULL AND pre_commit = ?",
                    (pre_commit,)
                ).fetchone()
                if row and row["c"] > 0:
                    import sys
                    print(f"\033[93mWarning\033[0m: There are {row['c']} other active AI session(s) modifying this same commit state concurrently.", file=sys.stderr)
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
        self._write_session_meta(meta)
        write_json(session_dir / "merkle.sig", {"root": None, "steps": 0})
        (session_dir / "steps.jsonl").touch()
        self.paths.head_path.write_text(session_id, encoding="utf-8")
        self._upsert_session_index(meta, step_count=0, risk_count=0)
        return meta

    def end_session(self, session_id: str | None = None) -> dict[str, Any]:
        self.init_repo()
        with self.file_lock_context():
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
        self._write_session_meta(meta)
        self._drop_agent_session_mapping_for_session(target_id)
        if self.get_active_session_id() == target_id:
            self.paths.head_path.write_text("", encoding="utf-8")
        self._upsert_session_index(
            meta,
            step_count=self._step_count(target_id),
            risk_count=self._risk_count(target_id),
        )
        return meta

    def get_active_session_id(self) -> str | None:
        if not self.paths.head_path.exists():
            return None
        raw = self.paths.head_path.read_text(encoding="utf-8").strip()
        return raw or None

    def record_step(self, step_dict: dict[str, Any]) -> dict[str, Any]:
        self.init_repo()
        with self.file_lock_context():
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
        with self.file_lock_context():
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
        with self.file_lock_context():
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
                if tool_data and tool_data["name"] not in entry["tool_names"]:
                    entry["tool_names"].append(tool_data["name"])
                if action == "write" and entry["lines_removed"] == 0 and added > 0:
                    entry["is_new"] = True

                if step_id not in entry["step_ids"]:
                    entry["step_ids"].append(step_id)
                if stype in {"tool", "input", "output", "reasoning"}:
                    step_info = {
                        "step_id": step_id,
                        "step_type": stype,
                        "ts": step.get("ts"),
                        "tool_name": tool_name if stype == "tool" else None,
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

    def list_sessions(self) -> list[dict[str, Any]]:
        self.init_repo()
        sessions = []
        for session in os.scandir(self.paths.sessions_dir):
            if self._is_valid_session_id(session.name):
                sessions.append(self.read_session_meta(session.name))
        return sessions

    def show_session(self, session_id: str | None = None) -> dict[str, Any]:
        self.init_repo()
        target_id = self.get_active_session_id() if session_id in (None, "HEAD") else session_id
        if not target_id:
            raise MachError("No session specified and no active session exists.")
        meta = self.read_session_meta(target_id)
        session_dir = self.paths.sessions_dir / target_id
        steps = read_jsonl(session_dir / "steps.jsonl")
        
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
        with self.file_lock_context():
            target_branch = branch or current_branch(self.paths.repo_root)
            with connect(self.paths.db_path) as conn:
                row = conn.execute(
                    "SELECT id FROM sessions WHERE branch = ? ORDER BY started_at DESC LIMIT 1",
                    (target_branch,)
                ).fetchone()
            
            if not row:
                raise MachError(f"No previous sessions found for branch: {target_branch}")
            
            session_id = row["id"]
            meta = self.read_session_meta(session_id)
            if meta["status"] != "active":
                meta["status"] = "active"
                meta["ended_at"] = None
                meta["post_commit"] = None
                self._write_session_meta(meta)
                self._upsert_session_index(meta, self._step_count(session_id), self._risk_count(session_id))
                self._record_step_for_session_unlocked(session_id, {
                    "type": "system_action",
                    "content": f"Session resumed on branch {target_branch}",
                    "risk_level": "none",
                })
            
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
                "metadata": meta
            }

    def rewind(self, target: str) -> dict[str, Any]:
        self.init_repo()
        import subprocess
        with self.file_lock_context():
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
        with self.file_lock_context():
            active = self.get_active_session_id()
            cutoff = int(time()) - (max_days * 86400)
            cleaned = []
            
            with connect(self.paths.db_path) as conn:
                rows = conn.execute(
                    "SELECT id FROM sessions WHERE status != 'active' AND started_at < ? AND post_commit IS NULL",
                    (cutoff,)
                ).fetchall()
                
                for r in rows:
                    sid = r["id"]
                    if sid == active:
                        continue
                    sdir = self.paths.sessions_dir / sid
                    if sdir.exists():
                        shutil.rmtree(sdir)
                    conn.execute("DELETE FROM risk_flags WHERE step_id IN (SELECT id FROM steps WHERE session_id=?)", (sid,))
                    conn.execute("DELETE FROM file_changes WHERE step_id IN (SELECT id FROM steps WHERE session_id=?)", (sid,))
                    conn.execute("DELETE FROM tools WHERE step_id IN (SELECT id FROM steps WHERE session_id=?)", (sid,))
                    conn.execute("DELETE FROM steps WHERE session_id=?", (sid,))
                    conn.execute("DELETE FROM sessions WHERE id=?", (sid,))
                    cleaned.append(sid)
            return {"cleaned": len(cleaned), "session_ids": cleaned}

    def on_commit(self) -> dict[str, Any] | None:
        config = self.read_config()
        if not config.get("commit_closes_session", False):
            return None
        active = self.get_active_session_id()
        if not active:
            return None
        with self.file_lock_context():
            return self._end_session_unlocked(active)

    def clone_session(self, source_session_id: str) -> dict[str, Any]:
        self.init_repo()
        with self.file_lock_context():
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
                
                old_parent = cloned.get("parent_step_id")
                if old_parent:
                    cloned["parent_step_id"] = id_map.get(old_parent, old_parent)

            mach_state = remote.setdefault("mach", {})
            mach_state.update({
                "last_pushed_step_id": last_inherited_step_id,
                "last_pushed_ts": now if last_inherited_step_id else 0,
                "last_pulled_step_id": last_inherited_step_id,
                "last_pulled_ts": now if last_inherited_step_id else 0,
                "last_pulled_at": now if last_inherited_step_id else None,
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
            self._upsert_session_index(
                cloned_meta,
                step_count=len(cloned_steps),
                risk_count=sum(len(step.get("risk_flags", [])) for step in cloned_steps),
            )
            for cloned in cloned_steps:
                self._insert_step(cloned)

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
        with self.file_lock_context():
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
            self._upsert_session_index(
                cloned_meta,
                step_count=len(cloned_steps),
                risk_count=sum(len(step.get("risk_flags", [])) for step in cloned_steps),
            )
            for cloned in cloned_steps:
                self._insert_step(cloned)

            return {
                "cloned": True,
                "session_id": clone_id,
                "forked_from": source_session_id,
                "step_count": len(cloned_steps),
                "blob_count": blob_count,
                "last_pulled_step_id": last_inherited_step_id,
                "metadata": cloned_meta,
            }

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
            pass
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
                
            tool_obj = ToolCall(
                name=t.get("name", ""),
                category=t.get("category", "exec"),
                content_hash=t_content_hash,
                content=None
            )

        fc_data = step_dict.get("file_changes", [])
        file_changes = [FileChange.from_dict(fc) for fc in fc_data] if fc_data else []

        step_obj = Step(
            id=step_id,
            session_id=session_id,
            step_num=step_num,
            ts=ts,
            type=step_type,
            content_hash=content_hash,
            content=final_content,
            caused_by=step_dict.get("caused_by", [prev_step_id] if prev_step_id else []),
            risk_level=step_dict.get("risk_level", "none"),
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
            self._upsert_session_index(
                meta,
                step_count=len(existing_steps),
                risk_count=self._risk_count(session_id),
            )
            return merged_payload

        append_jsonl(steps_path, payload)

        merkle = read_json(merkle_path)
        root = chain_hash(payload, merkle.get("root"))
        merkle["root"] = root
        merkle["steps"] = step_num
        write_json(merkle_path, merkle)

        self.paths.head_path.write_text(session_id, encoding="utf-8")
        self._insert_step(payload)
        meta["head_step_id"] = step_id
        self._write_session_meta(meta)
        self._upsert_session_index(
            meta,
            step_count=step_num,
            risk_count=self._risk_count(session_id),
        )
        return payload
