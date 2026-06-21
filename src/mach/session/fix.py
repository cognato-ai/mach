from __future__ import annotations

from typing import Any

from mach.merkle import chain_hash, hash_payload
from mach.utils import read_jsonl, append_jsonl, write_json
from mach.session.index import SessionIndexMixin
from mach.session.base import MachError

class SessionFixMixin(SessionIndexMixin):
    def fix_sessions(self, session_id: str | None = None, *, apply: bool = False) -> dict[str, Any]:
        self.init_repo()
        with self.file_lock_context():
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

    def _fix_session_chunks_unlocked(self, session_id: str, *, apply: bool) -> dict[str, Any]:
        session_dir = self.paths.sessions_dir / session_id
        steps = read_jsonl(session_dir / "steps.jsonl")
        normalized, id_map, merged_steps, normalized_tool_hashes, backfilled_file_changes = self._normalize_steps(steps)

        meta = self.read_session_meta(session_id)
        has_missing_linked_list_fields = False
        if not meta or not meta.get("head_step_id"):
            has_missing_linked_list_fields = True
        else:
            for step in steps:
                if "parent_step_id" not in step or step["parent_step_id"] is None:
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
        self._write_session_meta(meta)
        write_json(merkle_path, {"root": root, "steps": len(steps)})
        self._replace_session_steps_in_index(session_id, steps)

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
            self._write_session_meta(meta)
            self._upsert_session_index(meta, len(steps), self._risk_count(session_id))
        except Exception:
            pass

        self._replace_session_steps_in_index(session_id, steps)

    def file_lock_context(self):
        from mach.locking import file_lock
        return file_lock(self.paths.lock_path)
