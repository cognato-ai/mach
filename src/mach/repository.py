from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from mach.models import MachPaths


def resolve_paths(repo_root: Path | None = None) -> MachPaths:
    root = (repo_root or Path.cwd()).resolve()
    
    try:
        git_dir_raw = subprocess.check_output(
            ["git", "rev-parse", "--absolute-git-dir"],
            cwd=str(root),
            stderr=subprocess.DEVNULL,
            text=True
        ).strip()
        mach_dir = Path(git_dir_raw) / "mach"
    except (subprocess.CalledProcessError, FileNotFoundError):
        mach_dir = root / ".mach"

    root_hash = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:8]

    return MachPaths(
        repo_root=root,
        mach_dir=mach_dir,
        config_path=mach_dir / "config",
        head_path=mach_dir / f"HEAD_{root_hash}",
        sessions_dir=mach_dir / "sessions",
        pack_dir=mach_dir / "pack",
        blobs_dir=mach_dir / "blobs",
        lock_path=mach_dir / ".lock",
        agent_sessions_path=mach_dir / "agent_sessions.json",
        inbox_dir=mach_dir / "inbox",
        ingest_state_path=mach_dir / "ingest_state.json",
        tracker_state_path=mach_dir / "tracker_state.json",
        tracker_pid_path=mach_dir / "tracker.pid",
        tracker_log_path=mach_dir / "tracker.log",
        tracker_lock_path=mach_dir / ".tracker.lock",
        tracked_repo_path=mach_dir / "tracked_repo.json",
    )
