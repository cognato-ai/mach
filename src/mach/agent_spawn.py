"""Spawn vendor agent sessions and transfer Mach handoffs (Mach-owned, not user paste)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class SpawnResult:
    ok: bool
    agent: str
    vendor_session_id: str | None
    resume_command: str | None
    method: str
    detail: str = ""
    raw_stdout: str = ""
    raw_stderr: str = ""


def load_seed_prompt(handoff_path: Path, mach_session_id: str) -> str:
    path = handoff_path.resolve()
    return (
        "You are continuing a prior coding session recorded by Mach.\n"
        f"1. Read this handoff file fully (use the Read tool; chunk if large):\n   {path}\n"
        "2. Cover Metadata, Instructions, Task, and the full Session chat.\n"
        "3. Prefer the current working tree/git state if they conflict with older trail notes.\n"
        "4. When loaded, reply with exactly:\n"
        "   READY\n"
        "   - <one-line task restatement>\n"
        "   - <key files already touched>\n"
        "   - <suggested next step>\n"
        "5. Do NOT edit files or run destructive commands in this turn.\n"
        f"Mach session id: {mach_session_id}\n"
    )


class AgentSpawner:
    """Create a real vendor session with handoff already transferred."""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root.resolve()

    def spawn(
        self,
        agent: str,
        *,
        handoff_path: Path,
        mach_session_id: str,
        timeout_sec: int = 180,
    ) -> SpawnResult:
        agent = (agent or "").strip().lower()
        if agent == "claude":
            return self._spawn_claude(handoff_path, mach_session_id, timeout_sec)
        if agent == "codex":
            return self._spawn_codex(handoff_path, mach_session_id, timeout_sec)
        if agent == "gemini":
            return self._spawn_gemini(handoff_path, mach_session_id, timeout_sec)
        return SpawnResult(
            ok=False,
            agent=agent,
            vendor_session_id=None,
            resume_command=None,
            method="unsupported",
            detail=(
                f"Automatic spawn is not implemented for agent '{agent}'. "
                "Supported: claude, codex, gemini."
            ),
        )

    def resume_command(self, agent: str, vendor_session_id: str) -> str:
        agent = agent.strip().lower()
        if agent == "claude":
            return f"claude -r {vendor_session_id}"
        if agent == "codex":
            return f"codex resume {vendor_session_id}"
        if agent == "gemini":
            return f"gemini --resume {vendor_session_id}"
        return f"{agent} --resume {vendor_session_id}"

    # ── Claude Code ──────────────────────────────────────────────────────────

    def _spawn_claude(self, handoff_path: Path, mach_session_id: str, timeout_sec: int) -> SpawnResult:
        binary = shutil.which("claude")
        if not binary:
            return SpawnResult(
                ok=False,
                agent="claude",
                vendor_session_id=None,
                resume_command=None,
                method="missing_binary",
                detail="`claude` not found on PATH. Install Claude Code CLI.",
            )

        seed = load_seed_prompt(handoff_path, mach_session_id)

        # Preferred: print mode JSON returns session_id after one turn that loads handoff.
        cmd = [
            binary,
            "-p",
            seed,
            "--output-format",
            "json",
            "--allowedTools",
            "Read",
        ]
        before = time.time()
        proc = self._run(cmd, timeout_sec=timeout_sec)
        vendor_id = self._parse_claude_session_id(proc.stdout) or self._find_newest_claude_session(since=before - 2)

        if vendor_id:
            return SpawnResult(
                ok=True,
                agent="claude",
                vendor_session_id=vendor_id,
                resume_command=self.resume_command("claude", vendor_id),
                method="claude -p --output-format json",
                detail="Handoff load turn completed; session is ready to resume interactively.",
                raw_stdout=proc.stdout[-4000:],
                raw_stderr=proc.stderr[-2000:],
            )

        # Fallback: background agent (returns immediately with session id when supported).
        bg = self._run(
            [binary, "--bg", seed],
            timeout_sec=min(timeout_sec, 60),
        )
        vendor_id = (
            self._parse_claude_session_id(bg.stdout + "\n" + bg.stderr)
            or self._find_newest_claude_session(since=before - 2)
        )
        if vendor_id:
            return SpawnResult(
                ok=True,
                agent="claude",
                vendor_session_id=vendor_id,
                resume_command=self.resume_command("claude", vendor_id),
                method="claude --bg",
                detail="Background session started with handoff seed.",
                raw_stdout=bg.stdout[-4000:],
                raw_stderr=bg.stderr[-2000:],
            )

        return SpawnResult(
            ok=False,
            agent="claude",
            vendor_session_id=None,
            resume_command=None,
            method="claude_spawn_failed",
            detail=(
                "Could not capture Claude session id. "
                f"stdout/stderr tails:\n{proc.stdout[-500:]}\n{proc.stderr[-500:]}\n{bg.stderr[-500:]}"
            ),
            raw_stdout=proc.stdout[-4000:],
            raw_stderr=proc.stderr[-2000:],
        )

    @staticmethod
    def _parse_claude_session_id(text: str) -> str | None:
        if not text or not text.strip():
            return None
        # JSON object with session_id
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                sid = data.get("session_id") or data.get("sessionId")
                if sid:
                    return str(sid)
        except json.JSONDecodeError:
            pass
        # NDJSON / multi-line JSON
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                sid = data.get("session_id") or data.get("sessionId")
                if sid:
                    return str(sid)
        # Text patterns: session id printed by --bg
        patterns = [
            r"session[_\s-]?id[:\s]+([0-9a-fA-F-]{8,})",
            r"claude\s+-r\s+([^\s\"']+)",
            r"claude\s+--resume\s+([^\s\"']+)",
            r"claude\s+attach\s+([^\s\"']+)",
            r"Resume this session with:\s*claude\s+--resume\s+\"?([^\s\"]+)\"?",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.group(1).strip().strip("\"'")
        return None

    def _find_newest_claude_session(self, *, since: float) -> str | None:
        """Best-effort: newest transcript under ~/.claude/projects for this repo."""
        projects = Path.home() / ".claude" / "projects"
        if not projects.exists():
            return None
        # Claude encodes project path in directory name.
        repo_key = str(self.repo_root).replace("/", "-")
        candidates: list[Path] = []
        for path in projects.glob("**/*"):
            if not path.is_file():
                continue
            if path.suffix not in {".jsonl", ".json"}:
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime < since:
                continue
            # Prefer project dirs that look related to cwd.
            if repo_key in str(path) or self.repo_root.name in str(path):
                candidates.append(path)
        if not candidates:
            # Fall back to any recent session file.
            for path in projects.glob("**/*"):
                if path.is_file() and path.suffix in {".jsonl", ".json"}:
                    try:
                        if path.stat().st_mtime >= since:
                            candidates.append(path)
                    except OSError:
                        continue
        if not candidates:
            return None
        newest = max(candidates, key=lambda p: p.stat().st_mtime)
        # Session id is usually the filename stem.
        stem = newest.stem
        if stem and stem not in {"sessions", "history", "config"}:
            return stem
        return None

    # ── Codex ────────────────────────────────────────────────────────────────

    def _spawn_codex(self, handoff_path: Path, mach_session_id: str, timeout_sec: int) -> SpawnResult:
        binary = shutil.which("codex")
        if not binary:
            return SpawnResult(
                ok=False,
                agent="codex",
                vendor_session_id=None,
                resume_command=None,
                method="missing_binary",
                detail="`codex` not found on PATH. Install Codex CLI.",
            )
        seed = load_seed_prompt(handoff_path, mach_session_id)
        before = time.time()
        # codex exec is the scripted entrypoint on recent CLIs.
        proc = self._run([binary, "exec", seed], timeout_sec=timeout_sec)
        if proc.returncode != 0:
            proc = self._run([binary, seed], timeout_sec=timeout_sec)
        vendor_id = self._parse_generic_session_id(proc.stdout + "\n" + proc.stderr)
        if not vendor_id:
            vendor_id = self._find_newest_under(Path.home() / ".codex", since=before - 2)
        if vendor_id:
            return SpawnResult(
                ok=True,
                agent="codex",
                vendor_session_id=vendor_id,
                resume_command=self.resume_command("codex", vendor_id),
                method="codex exec",
                detail="Codex session spawned with handoff seed.",
                raw_stdout=proc.stdout[-4000:],
                raw_stderr=proc.stderr[-2000:],
            )
        return SpawnResult(
            ok=False,
            agent="codex",
            vendor_session_id=None,
            resume_command=None,
            method="codex_spawn_failed",
            detail="Could not capture Codex session id after spawn.",
            raw_stdout=proc.stdout[-4000:],
            raw_stderr=proc.stderr[-2000:],
        )

    # ── Gemini ───────────────────────────────────────────────────────────────

    def _spawn_gemini(self, handoff_path: Path, mach_session_id: str, timeout_sec: int) -> SpawnResult:
        binary = shutil.which("gemini")
        if not binary:
            return SpawnResult(
                ok=False,
                agent="gemini",
                vendor_session_id=None,
                resume_command=None,
                method="missing_binary",
                detail="`gemini` not found on PATH.",
            )
        seed = load_seed_prompt(handoff_path, mach_session_id)
        before = time.time()
        proc = self._run([binary, "-p", seed], timeout_sec=timeout_sec)
        if proc.returncode != 0:
            proc = self._run([binary, seed], timeout_sec=timeout_sec)
        vendor_id = self._parse_generic_session_id(proc.stdout + "\n" + proc.stderr)
        if not vendor_id:
            vendor_id = self._find_newest_under(Path.home() / ".gemini", since=before - 2)
        if vendor_id:
            return SpawnResult(
                ok=True,
                agent="gemini",
                vendor_session_id=vendor_id,
                resume_command=self.resume_command("gemini", vendor_id),
                method="gemini -p",
                detail="Gemini session spawned with handoff seed.",
                raw_stdout=proc.stdout[-4000:],
                raw_stderr=proc.stderr[-2000:],
            )
        return SpawnResult(
            ok=False,
            agent="gemini",
            vendor_session_id=None,
            resume_command=None,
            method="gemini_spawn_failed",
            detail="Could not capture Gemini session id after spawn.",
            raw_stdout=proc.stdout[-4000:],
            raw_stderr=proc.stderr[-2000:],
        )

    # ── helpers ──────────────────────────────────────────────────────────────

    def _run(self, cmd: list[str], *, timeout_sec: int) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                cmd,
                cwd=str(self.repo_root),
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            return subprocess.CompletedProcess(cmd, 124, stdout=stdout, stderr=stderr + "\n[timeout]")
        except FileNotFoundError:
            return subprocess.CompletedProcess(cmd, 127, stdout="", stderr="binary not found")

    @staticmethod
    def _parse_generic_session_id(text: str) -> str | None:
        if not text:
            return None
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                for key in ("session_id", "sessionId", "id", "thread_id"):
                    if data.get(key):
                        return str(data[key])
        except json.JSONDecodeError:
            pass
        match = re.search(r"session[_\s-]?id[:\s\"']+([A-Za-z0-9._-]{6,})", text, re.I)
        if match:
            return match.group(1)
        return None

    @staticmethod
    def _find_newest_under(root: Path, *, since: float) -> str | None:
        if not root.exists():
            return None
        newest: Path | None = None
        newest_mtime = since
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix not in {".jsonl", ".json"}:
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime >= newest_mtime:
                newest_mtime = mtime
                newest = path
        if newest is None:
            return None
        return newest.stem or None
