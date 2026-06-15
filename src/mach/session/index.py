from __future__ import annotations

import uuid
from typing import Any

from mach.db import connect
from mach.utils import canonical_json
from mach.session.base import SessionBase

class SessionIndexMixin(SessionBase):
    def _upsert_session_index(self, meta: dict[str, Any], step_count: int, risk_count: int) -> None:
        if not self.get_config().get("db_enabled", True):
            return
        with connect(self.paths.db_path) as conn:
            conn.execute(
                """
                INSERT INTO sessions (
                  id, started_at, ended_at, agent, branch, pre_commit, post_commit,
                  step_count, risk_count, forked_from, synced_at, agent_session_id, head_step_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  started_at=excluded.started_at,
                  ended_at=excluded.ended_at,
                  agent=excluded.agent,
                  branch=excluded.branch,
                  pre_commit=excluded.pre_commit,
                  post_commit=excluded.post_commit,
                  step_count=excluded.step_count,
                  risk_count=excluded.risk_count,
                  forked_from=excluded.forked_from,
                  synced_at=excluded.synced_at,
                  agent_session_id=excluded.agent_session_id,
                  head_step_id=excluded.head_step_id
                """,
                (
                    meta["id"],
                    meta["started_at"],
                    meta["ended_at"],
                    meta["agent"],
                    meta["branch"],
                    meta["pre_commit"],
                    meta["post_commit"],
                    step_count,
                    risk_count,
                    meta.get("forked_from"),
                    (meta.get("remote") or {}).get("mach", {}).get("last_pushed_ts"),
                    meta.get("agent_session_id"),
                    meta.get("head_step_id"),
                ),
            )

    def _insert_step(self, payload: dict[str, Any]) -> None:
        if not self.get_config().get("db_enabled", True):
            return
        with connect(self.paths.db_path) as conn:
            conn.execute(
                """
                INSERT INTO steps (
                  id, session_id, step_num, ts, type, content, content_hash, caused_by, risk_level, parent_step_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  parent_step_id=excluded.parent_step_id
                """,
                (
                    payload["id"],
                    payload["session_id"],
                    payload["step_num"],
                    payload["ts"],
                    payload["type"],
                    payload.get("content"),
                    payload.get("content_hash"),
                    canonical_json(payload.get("caused_by", [])),
                    payload.get("risk_level", "none"),
                    payload.get("parent_step_id"),
                ),
            )

            tool_payload = payload.get("tool")
            if tool_payload:
                conn.execute(
                    """
                    INSERT INTO tools (id, step_id, name, category, content, content_hash)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"tool_{uuid.uuid4().hex}",
                        payload["id"],
                        tool_payload.get("name"),
                        tool_payload.get("category"),
                        tool_payload.get("content"),
                        tool_payload.get("content_hash"),
                    ),
                )

            for change in payload.get("file_changes", []):
                conn.execute(
                    """
                    INSERT INTO file_changes (
                      id, step_id, file_path, action, before_blob, after_blob,
                      lines_added, lines_removed, hunks, sensitivity
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"fc_{uuid.uuid4().hex}",
                        payload["id"],
                        change.get("file_path"),
                        change.get("action"),
                        change.get("before_blob"),
                        change.get("after_blob"),
                        change.get("lines_added"),
                        change.get("lines_removed"),
                        canonical_json(change.get("hunks", [])),
                        change.get("sensitivity", "none"),
                    ),
                )

            for flag in payload.get("risk_flags", []):
                conn.execute(
                    """
                    INSERT INTO risk_flags (id, step_id, rule_id, severity, explanation, resolved)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"rf_{uuid.uuid4().hex}",
                        payload["id"],
                        flag.get("rule_id"),
                        flag.get("severity"),
                        flag.get("explanation"),
                        1 if flag.get("resolved") else 0,
                    ),
                )

    def _replace_session_steps_in_index(self, session_id: str, steps: list[dict[str, Any]]) -> None:
        if not self.get_config().get("db_enabled", True):
            return
        with connect(self.paths.db_path) as conn:
            conn.execute("DELETE FROM risk_flags WHERE step_id IN (SELECT id FROM steps WHERE session_id=?)", (session_id,))
            conn.execute("DELETE FROM file_changes WHERE step_id IN (SELECT id FROM steps WHERE session_id=?)", (session_id,))
            conn.execute("DELETE FROM tools WHERE step_id IN (SELECT id FROM steps WHERE session_id=?)", (session_id,))
            conn.execute("DELETE FROM steps WHERE session_id=?", (session_id,))
        for step in steps:
            self._insert_step(step)

    def _step_count(self, session_id: str) -> int:
        try:
            with connect(self.paths.db_path) as conn:
                row = conn.execute(
                    "SELECT COUNT(*) AS count FROM steps WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
            return int(row["count"]) if row else 0
        except Exception:
            return 0

    def _risk_count(self, session_id: str) -> int:
        try:
            with connect(self.paths.db_path) as conn:
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM risk_flags rf
                    JOIN steps s ON s.id = rf.step_id
                    WHERE s.session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
            return int(row["count"]) if row else 0
        except Exception:
            return 0
