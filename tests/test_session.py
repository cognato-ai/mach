from __future__ import annotations

import tempfile
import gc
from pathlib import Path
import pytest
from mach.session import SessionStore, MachError

@pytest.fixture
def temp_repo():
    tmpdir = tempfile.TemporaryDirectory()
    repo_path = Path(tmpdir.name)
    
    import subprocess
    subprocess.run(["git", "init"], cwd=repo_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_path, capture_output=True, check=True)
    
    (repo_path / "README.md").write_text("Hello Mach", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo_path, capture_output=True, check=True)
    
    yield repo_path
    
    # Trigger garbage collection to close open SQLite connections on Windows
    gc.collect()
    try:
        tmpdir.cleanup()
    except PermissionError:
        pass

def test_session_store_init(temp_repo):
    store = SessionStore(temp_repo)
    mach_dir = store.init_repo()
    assert mach_dir.exists()
    assert (mach_dir / "sessions").exists()
    assert (mach_dir / "config").exists()

def test_start_and_end_session(temp_repo):
    store = SessionStore(temp_repo)
    
    # 1. Start session
    meta = store.start_session(agent="test-agent", task_desc="E2E test")
    session_id = meta["id"]
    assert session_id is not None
    assert meta["status"] == "active"
    assert meta["agent"] == "test-agent"
    
    # Get active session
    assert store.get_active_session_id() == session_id

    # 2. Record a step
    step_payload = store.record_step({
        "type": "input",
        "content": "User input step",
        "risk_level": "none"
    })
    assert step_payload["session_id"] == session_id
    assert step_payload["type"] == "input"

    # 3. Verify session
    verification = store.verify_session(session_id)
    assert verification["valid"] is True
    assert verification["steps"] == 1

    # 4. Show session
    session_data = store.show_session(session_id)
    assert session_data["meta"]["id"] == session_id
    assert len(session_data["steps"]) == 1

    # 5. End session
    ended_meta = store.end_session(session_id)
    assert ended_meta["status"] == "ended"
    assert store.get_active_session_id() is None

    # 6. Verify all
    all_verification = store.verify_all()
    assert len(all_verification) == 1
    assert all_verification[0]["valid"] is True
