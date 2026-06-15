from __future__ import annotations

import argparse
import sys
import getpass
import json
import time
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path

from mach.auth import save_token, logout
from mach.hooks import HookManager
from mach.tracker import TrackerService
from mach.session import SessionStore, MachError
from mach.models import RepositoryDetails
from mach.commands.base import (
    _choose_hook_agents,
    _choose_store_content,
    _require_auth_token,
    _api_base_url,
    _read_api_json,
    _auth_request,
    _repo_endpoint,
    _session_endpoint,
    _repo_identifiers,
    _normalize_repo_url,
    _repository_mismatches,
    _validate_repository_matches_git,
    _repo_allows_read,
    _session_repo_identifiers,
    _pull_session_details,
    _pull_remote_session_steps,
    _pull_remote_session_blobs,
    _require_tracked_repository,
    _validate_session_against_tracked_repo,
    _pull_repository,
    _push_reset,
    _format_push_step,
    _format_push_file_change,
    _collect_blob,
    _push_host_name,
    _agent_provider,
    emit,
)

def init_command(args: argparse.Namespace) -> None:
    store = SessionStore()
    mach_dir = store.init_repo()
    manager = HookManager()
    hook_agents = _choose_hook_agents(manager, args.hook_agents)
    store_content = _choose_store_content(args.store_content)
    config = store.update_config({"enabled": True, "hook_agents": hook_agents, "store_content": store_content})
    hook_results = manager.install(hook_agents) if hook_agents else {"installed": []}
    tracker = TrackerService()
    tracker.ensure_state()
    tracking = tracker.start_daemon() if config.get("auto_tracking", True) else tracker.status()
    print(f"Success: Mach initialized in {mach_dir}")

def enable_command(_: argparse.Namespace) -> None:
    store = SessionStore()
    config = store.update_config({"enabled": True})
    manager = HookManager()
    hook_results = manager.install(config.get("hook_agents") or manager.installable_agents())
    tracker = TrackerService()
    tracking = tracker.start_daemon() if config.get("auto_tracking", True) else tracker.status()
    print("Success: Mach tracking enabled.")

def disable_command(_: argparse.Namespace) -> None:
    store = SessionStore()
    config = store.update_config({"enabled": False})
    hook_results = HookManager().uninstall(config.get("hook_agents"))
    tracking = TrackerService().stop_daemon()
    print("Success: Mach tracking disabled.")

def login_command(args: argparse.Namespace) -> None:
    token = args.token
    if not token:
        token = getpass.getpass("Enter your Mach Personal Access Token: ").strip()
    
    if not token:
        print("Error: Token cannot be empty.", file=sys.stderr)
        sys.exit(1)

    save_token(token)
    print("Success: Logged in. Token saved globally to ~/.mach/credentials.json")

def logout_command(_: argparse.Namespace) -> None:
    logout()
    print("Success: Logged out.")

def push_command(args: argparse.Namespace) -> None:
    token = _require_auth_token()
    session_id = args.session_id

    if getattr(args, "reset", False) or getattr(args, "reset_to", None):
        _push_reset(session_id, reset_to=getattr(args, "reset_to", None))
        return

    print(f"Pushing session {session_id} to Mach Web...")
    
    store = SessionStore()
    try:
        from mach import __version__
        from mach.git_utils import current_branch, remote_origin_url, repository_name
        from mach.models import PushMerkle, PushMetadata, PushPayload, PushResponse, PushSessionMeta

        meta = store.read_session_meta(session_id)
        remote = meta.get("remote", {})
        git_info = remote.get("git") or {}
        mach_state = remote.get("mach") or {}
        current_remote_url = remote_origin_url(store.paths.repo_root)
        current_repository_label = repository_name(store.paths.repo_root) if current_remote_url else None
        remote_url = current_remote_url or git_info.get("url")
        repository_label = current_repository_label or git_info.get("repository_name") or repository_name(store.paths.repo_root)
        if remote_url != git_info.get("url") or repository_label != git_info.get("repository_name"):
            store.update_push_state(
                session_id,
                git_updates={
                    "url": remote_url,
                    "repository_name": repository_label,
                },
            )
            meta = store.read_session_meta(session_id)
            remote = meta.get("remote", {})
            git_info = remote.get("git") or {}
            mach_state = remote.get("mach") or {}
            remote_url = git_info.get("url")
            repository_label = git_info.get("repository_name")
        print(f"  Repository: {repository_label}")
        print("  Calculating Merkle deltas...")

        last_pushed_id = mach_state.get("last_pushed_step_id")
        session_dir = store.paths.sessions_dir / session_id
        steps_file = session_dir / "steps.jsonl"
        
        all_steps = []
        if steps_file.exists():
            with open(steps_file, "r", encoding="utf-8") as f:
                lines = f.readlines()
                
            for line in lines:
                if not line.strip(): continue
                step_data = json.loads(line)
                all_steps.append(step_data)
        
        steps_to_push = all_steps
        if last_pushed_id:
            for index, step_data in enumerate(all_steps):
                if step_data.get("id") == last_pushed_id:
                    steps_to_push = all_steps[index + 1:]
                    break
        
        if not steps_to_push:
            print(f"Success: Session {session_id} is already up-to-date.")
            return
            
        total_steps = len(steps_to_push)
        print(f"  Found {total_steps} unpushed steps. Uploading...")

        merkle_path = session_dir / "merkle.sig"
        merkle = {}
        if merkle_path.exists():
            with open(merkle_path, "r", encoding="utf-8") as f:
                merkle = json.load(f)

        risk_count = sum(len(step.get("risk_flags", [])) for step in all_steps)

        config = store.read_config()
        base_url = config.get("api_base_url", "http://localhost:8000").rstrip("/")
        endpoint = f"{base_url}/api/v1/sessions/sync/"

        BATCH_SIZE = 50
        pushed_count = 0

        for batch_start in range(0, total_steps, BATCH_SIZE):
            batch = steps_to_push[batch_start:batch_start + BATCH_SIZE]

            blobs: dict[str, str] = {}
            formatted_steps = []
            for step in batch:
                formatted_steps.append(_format_push_step(store, step, blobs))

            payload_obj = PushPayload(
                repository=remote_url or repository_label,
                meta=PushSessionMeta(
                    id=session_id,
                    agent=meta.get("agent", "unknown"),
                    agent_session_id=meta.get("agent_session_id"),
                    task_desc=meta.get("task_desc"),
                    started_at=meta.get("started_at", 0),
                    ended_at=meta.get("ended_at"),
                    status=meta.get("status", "active"),
                    branch=meta.get("branch") or current_branch(store.paths.repo_root) or "unknown",
                    pre_commit=meta.get("pre_commit"),
                    post_commit=meta.get("post_commit"),
                    step_count=len(all_steps),
                    risk_count=risk_count,
                    forked_from=meta.get("forked_from"),
                    head_step_id=meta.get("head_step_id"),
                ),
                merkle=PushMerkle(
                    root=merkle.get("root"),
                    steps=int(merkle.get("steps") or len(all_steps)),
                ),
                blobs=blobs,
                steps=formatted_steps,
                client_root=merkle.get("root"),
                metadata=PushMetadata(
                    cli_version=__version__,
                    pushed_from=_push_host_name(),
                ),
            )
            payload = payload_obj.to_dict()

            req = urllib.request.Request(
                endpoint,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {token}"
                },
                method="POST"
            )

            try:
                with urllib.request.urlopen(req) as response:
                    if response.status not in (200, 201):
                        print(f"\nError: Backend returned status {response.status}", file=sys.stderr)
                        sys.exit(1)

                    resp_body = response.read().decode("utf-8")
                    push_response = PushResponse.from_dict(json.loads(resp_body) if resp_body else {})
            except urllib.error.HTTPError as http_err:
                body = http_err.read().decode("utf-8", errors="replace")
                print(f"\nError: Backend returned status {http_err.code}", file=sys.stderr)
                if body:
                    print(body, file=sys.stderr)
                sys.exit(1)
            except urllib.error.URLError as req_err:
                print(f"\nError: Could not connect to backend ({endpoint}): {req_err}", file=sys.stderr)
                sys.exit(1)

            pushed_count += len(batch)
            percent = int((pushed_count / total_steps) * 100)
            sys.stdout.write(f"\r  Uploading: {percent:3d}% ({pushed_count}/{total_steps})")
            sys.stdout.flush()

            pushed_root = push_response.server_root_after or push_response.session.merkle_root or push_response.client_root
            pushed_at = push_response.created or push_response.session.synced_at
            store.update_push_state(
                session_id,
                git_updates={
                    "url": remote_url,
                    "repository_name": repository_label,
                },
                mach_updates={
                    "last_push_id": push_response.id,
                    "last_pushed_at": pushed_at,
                    "last_pushed_ts": int(time.time()),
                    "last_pushed_step_id": batch[-1].get("id"),
                    "pushed_root": pushed_root,
                    "server_session_id": push_response.session.id,
                    "server_root_before": push_response.server_root_before,
                    "server_root_after": push_response.server_root_after,
                    "blobs_received": push_response.blobs_received,
                    "steps_received": push_response.steps_received,
                },
                step_count=push_response.session.step_count,
                risk_count=push_response.session.risk_count,
            )

        print(f"\nSuccess: Synced session {session_id} to backend.")
        print(f"  Push ID: {push_response.id or 'unknown'}")
        print(f"  Steps sent: {pushed_count}; batches: {(total_steps + BATCH_SIZE - 1) // BATCH_SIZE}")
        if pushed_root:
            print(f"  Server root: {pushed_root}")
            
    except Exception as e:
        print(f"\nError: Failed to push session: {e}", file=sys.stderr)
        sys.exit(1)

def pull_command(args: argparse.Namespace) -> None:
    if args.session_id or args.session:
        print("Error: Session is currently not supported to pull. Use clone instead.")
        sys.exit(1)

    repository_name = args.repository or args.repository_name
    if not repository_name:
        print("Error: Provide a repository name to pull.", file=sys.stderr)
        sys.exit(1)

    _pull_repository(repository_name)

def clone_command(args: argparse.Namespace) -> None:
    if args.session_id:
        repository_name = args.clone_arg
        source_session_id = args.session_id
    else:
        repository_name = None
        source_session_id = args.clone_arg

    store = SessionStore()
    token = _require_auth_token()

    if repository_name:
        _pull_repository(repository_name)
        repository = _require_tracked_repository(store)
    else:
        repository = _require_tracked_repository(store)

    session_details = _validate_session_against_tracked_repo(store, source_session_id, token)

    print(f"Pulling remote session {source_session_id}...")
    remote_steps = _pull_remote_session_steps(store, source_session_id, token)
    remote_blobs = _pull_remote_session_blobs(store, source_session_id, token)
    result = store.clone_remote_session(source_session_id, session_details, remote_steps, remote_blobs)
    print(f"Success: Cloned session {source_session_id}.")
    print(f"  New session: {result['session_id']}")
    print(f"  Forked from: {result['forked_from']}")
    print(f"  Inherited steps: {result['step_count']}")
    print(f"  Blobs pulled: {result['blob_count']}")
    if result.get("last_pulled_step_id"):
        print(f"  Push cursor: {result['last_pulled_step_id']}")

def verify_command(args: argparse.Namespace) -> None:
    store = SessionStore()
    if args.session_id:
        emit(store.verify_session(args.session_id))
    else:
        emit(store.verify_all())

def fsck_command(_: argparse.Namespace) -> None:
    store = SessionStore()
    emit(store.fsck())

def internal_fix_command(args: argparse.Namespace) -> None:
    store = SessionStore()
    result = store.fix_sessions(session_id=args.session_id, apply=args.apply)
    action = "Applied" if args.apply else "Checked"
    target = args.session_id or "all sessions"
    print(f"{action} ledger fixes for {target}.")
    print(f"  Sessions checked: {result['sessions_checked']}")
    print(f"  Sessions changed: {result['sessions_changed']}")
    print(f"  Steps merged: {result['merged_steps']}")
    print(f"  Tool hashes normalized: {result['normalized_tool_hashes']}")
    print(f"  Linked list fields backfilled: {result['linked_list_fixes']}")
    print(f"  File changes backfilled: {result['backfilled_file_changes']}")

    changed_results = [item for item in result["results"] if item.get("changed")]
    for item in changed_results[:5]:
        parts = [
            f"{item['before_steps']} -> {item['after_steps']} steps",
            f"merged {item['merged_steps']}",
            f"tool hashes {item['normalized_tool_hashes']}"
        ]
        if item.get("linked_list_fixed"):
            parts.append("backfilled linked list fields")
        if item.get("backfilled_file_changes"):
            parts.append(f"backfilled {item['backfilled_file_changes']} file change(s)")
        print(f"  {item['session_id']}: {', '.join(parts)}")
    if len(changed_results) > 5:
        print(f"  ... {len(changed_results) - 5} more changed session(s)")

    if args.apply:
        fsck = store.fsck()
        print("  Rebuilt SQLite index.")
        print(f"  Sessions rebuilt: {fsck['sessions_rebuilt']}")
        print(f"  Steps rebuilt: {fsck['steps_rebuilt']}")
        if not fsck.get("ok"):
            print("Error: Ledger verification failed after applying fixes.", file=sys.stderr)
            sys.exit(1)
        print("Success: Ledger fixes applied.")
    else:
        print("Success: Dry run complete. Use `mach fix --apply` to rewrite ledgers.")

def update_command(_: argparse.Namespace) -> None:
    import subprocess
    install_dir = Path.home() / ".mach"
    if not install_dir.exists() or not (install_dir / ".git").exists():
        print("Error: Mach is not installed globally at ~/.mach or is not a git repository.", file=sys.stderr)
        sys.exit(1)
        
    print("Updating Mach...")
    try:
        subprocess.check_call(
            ["git", "pull", "origin", "master"],
            cwd=str(install_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        
        venv_pip = install_dir / "venv" / "bin" / "pip"
        if venv_pip.exists():
            subprocess.check_call(
                [str(venv_pip), "install", "--upgrade", "."],
                cwd=str(install_dir),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            
        print("Success: Mach updated successfully.")
    except subprocess.CalledProcessError:
        print("Error: Failed to update Mach.", file=sys.stderr)
        sys.exit(1)

def doctor_command(_: argparse.Namespace) -> None:
    store = SessionStore()
    fsck_res = store.fsck()
    tracker = TrackerService()
    if tracker.status().get("running"):
        tracker.stop_daemon()
    t_res = tracker.start_daemon()
    emit({"fsck": fsck_res, "tracker": t_res})

def on_commit_command(_: argparse.Namespace) -> None:
    store = SessionStore()
    emit(store.on_commit())
