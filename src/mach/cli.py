from __future__ import annotations

import argparse
import json
import sys
import pydoc
import termios
import tty
import getpass
import urllib.request
import urllib.error
import urllib.parse
import time
from pathlib import Path

from mach.auth import save_token, logout, get_token

from mach.config import DEFAULT_CONFIG
from mach.hooks import HookManager
from mach.ingest import EventInboxService
from mach.models import PullSessionDetails, RepositoryDetails
from mach.session import MachError, SessionStore
from mach.tracker import TrackerService

from mach.ui import render_sessions_list, render_session_steps, render_session_details

STORE_CONTENT_TYPES = list(DEFAULT_CONFIG["store_content"])

# Backward-compatible aliases used by log_command / show_command
def format_sessions_list(sessions: list[dict]) -> str:
    return render_sessions_list(sessions)

def format_session_steps(data: dict, oneline: bool = False, patch: bool = False) -> str:
    return render_session_steps(data, oneline=oneline, patch=patch)

def format_session_details(data: dict, patch: bool = False) -> str:
    return render_session_details(data, patch=patch)
def emit(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _select_from_terminal(
    prompt: str,
    choices: list[dict[str, str]],
    selected_values: list[str],
) -> list[str]:
    if not choices:
        return []

    selected = set(selected_values)
    cursor = 0
    line_count = len(choices) + 3
    rendered = False

    def render() -> None:
        nonlocal rendered
        if rendered:
            sys.stderr.write(f"\x1b[{line_count}F")
        sys.stderr.write(f"\x1b[2K\r{prompt}\n")
        sys.stderr.write("\x1b[2K\rUse Up/Down to move, Space to select, Enter when done.\n")
        sys.stderr.write("\x1b[2K\r\n")
        for index, choice in enumerate(choices):
            pointer = ">" if index == cursor else " "
            mark = "[x]" if choice["value"] in selected else "[ ]"
            sys.stderr.write(f"\x1b[2K\r{pointer} {mark} {choice['label']}\n")
        sys.stderr.flush()
        rendered = True

    def read_key() -> str:
        char = sys.stdin.read(1)
        if char == "\x03":
            raise KeyboardInterrupt
        if char == "\x1b":
            suffix = sys.stdin.read(2)
            if suffix == "[A":
                return "up"
            if suffix == "[B":
                return "down"
            return "escape"
        if char in {"\r", "\n"}:
            return "enter"
        if char == " ":
            return "space"
        return char

    old_settings = termios.tcgetattr(sys.stdin)
    sys.stderr.write("\x1b[?25l")
    try:
        tty.setraw(sys.stdin.fileno())
        render()
        while True:
            key = read_key()
            if key == "up":
                cursor = (cursor - 1) % len(choices)
            elif key == "down":
                cursor = (cursor + 1) % len(choices)
            elif key == "space":
                value = choices[cursor]["value"]
                if value in selected:
                    selected.remove(value)
                else:
                    selected.add(value)
            elif key == "enter":
                break
            render()
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
        sys.stderr.write("\x1b[?25h")
        sys.stderr.flush()

    sys.stderr.write("\n")
    sys.stderr.flush()
    return [choice["value"] for choice in choices if choice["value"] in selected]


def _choose_hook_agents(manager: HookManager, requested_agents: str | None = None) -> list[str]:
    if requested_agents is not None:
        return [agent for agent in requested_agents.split(",") if agent]

    default_agents = manager.available_agents()
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        return default_agents

    choices = [
        {
            "value": name,
            "label": f"{name} ({manager.adapters[name].support})",
        }
        for name in manager.available_agents()
    ]
    return _select_from_terminal("Select agent hooks to install:", choices, default_agents)


def _choose_store_content(requested_content: str | None = None) -> list[str]:
    if requested_content is not None:
        return [step_type.strip() for step_type in requested_content.split(",") if step_type.strip()]

    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        return list(STORE_CONTENT_TYPES)

    choices = [
        {
            "value": step_type,
            "label": step_type,
        }
        for step_type in STORE_CONTENT_TYPES
    ]
    return _select_from_terminal("Select step content to store:", choices, list(STORE_CONTENT_TYPES))


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


def _require_auth_token() -> str:
    token = get_token()
    if not token:
        print("Error: You must log in first. Run: mach login", file=sys.stderr)
        sys.exit(1)
    return token


def _api_base_url(store: SessionStore) -> str:
    config = store.read_config()
    return config.get("api_base_url", "http://localhost:8000").rstrip("/")


def _read_api_json(req: urllib.request.Request, context: str) -> dict:
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as http_err:
        body = http_err.read().decode("utf-8", errors="replace")
        if http_err.code in (401, 403):
            print(f"Error: Access denied while {context}.", file=sys.stderr)
        elif http_err.code == 404:
            print(f"Error: Not found while {context}.", file=sys.stderr)
        else:
            print(f"Error: Backend returned status {http_err.code} while {context}.", file=sys.stderr)
        if body:
            print(body, file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as req_err:
        print(f"Error: Could not connect to backend while {context}: {req_err}", file=sys.stderr)
        sys.exit(1)


def _auth_request(url: str, token: str, method: str = "GET") -> urllib.request.Request:
    return urllib.request.Request(
        url,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method=method,
    )


def _repo_endpoint(base_url: str, repository_name: str) -> str:
    return f"{base_url}/api/v1/repositories/pull/{urllib.parse.quote(repository_name, safe='')}/"


def _session_endpoint(base_url: str, session_id: str) -> str:
    return f"{base_url}/api/v1/sessions/{urllib.parse.quote(session_id, safe='')}/"


def _repo_identifiers(repository: RepositoryDetails | dict) -> set[str]:
    repo = repository.to_dict() if isinstance(repository, RepositoryDetails) else repository
    identifiers = set()
    for key in ("id", "name", "repository_name", "full_name", "repository", "url", "remote_url", "external_id"):
        value = repo.get(key)
        if value is not None:
            identifiers.add(str(value))
    metadata = repo.get("metadata")
    if isinstance(metadata, dict):
        identifiers.update(str(value) for value in metadata.values() if value is not None)
    return {value for value in identifiers if value}


def _normalize_repo_url(url: str | None) -> str | None:
    if not url:
        return None
    value = url.strip().lower()
    if value.endswith(".git"):
        value = value[:-4]
    if value.startswith("git@") and ":" in value:
        host, path = value[4:].split(":", 1)
        value = f"{host}/{path}"
    elif "://" in value:
        parsed = urllib.parse.urlparse(value)
        value = f"{parsed.netloc}{parsed.path}"
    return value.strip("/")


def _repository_mismatches(expected: RepositoryDetails, actual: RepositoryDetails) -> list[str]:
    mismatches = []
    if expected.id and actual.id and expected.id != actual.id:
        mismatches.append(f"id {actual.id!r} does not match tracked id {expected.id!r}")
    if expected.name and actual.name and expected.name != actual.name:
        mismatches.append(f"name {actual.name!r} does not match tracked name {expected.name!r}")

    expected_url = _normalize_repo_url(expected.remote_url)
    actual_url = _normalize_repo_url(actual.remote_url)
    if expected_url and actual_url and expected_url != actual_url:
        mismatches.append(f"remote URL {actual.remote_url!r} does not match tracked remote URL {expected.remote_url!r}")

    if expected.external_id and actual.external_id and expected.external_id != actual.external_id:
        mismatches.append(f"external id {actual.external_id!r} does not match tracked external id {expected.external_id!r}")
    return mismatches


def _validate_repository_matches_git(repository: RepositoryDetails, store: SessionStore) -> None:
    from mach.git_utils import remote_origin_url, repository_name

    local_name = repository_name(store.paths.repo_root)
    if repository.name and local_name and repository.name != local_name:
        print("Error: Pulled repository does not match this Git checkout.", file=sys.stderr)
        print(f"  Git repository: {local_name}", file=sys.stderr)
        print(f"  Pulled repository: {repository.name}", file=sys.stderr)
        sys.exit(1)

    local_url = remote_origin_url(store.paths.repo_root)
    local_url_norm = _normalize_repo_url(local_url)
    remote_url_norm = _normalize_repo_url(repository.remote_url)
    if remote_url_norm and not local_url_norm:
        print("Error: Pulled repository has a remote URL, but this checkout has no origin remote.", file=sys.stderr)
        print(f"  Pulled remote URL: {repository.remote_url}", file=sys.stderr)
        sys.exit(1)
    if local_url_norm and remote_url_norm and local_url_norm != remote_url_norm:
        print("Error: Pulled repository remote does not match this Git checkout.", file=sys.stderr)
        print(f"  Git remote URL: {local_url}", file=sys.stderr)
        print(f"  Pulled remote URL: {repository.remote_url}", file=sys.stderr)
        sys.exit(1)


def _repo_allows_read(repository: RepositoryDetails | dict) -> bool:
    repo = repository.to_dict() if isinstance(repository, RepositoryDetails) else repository
    if repo.get("is_active") is False:
        return False

    permissions = repo.get("permissions")
    if isinstance(permissions, dict):
        for key in ("read", "pull", "admin", "write"):
            if permissions.get(key):
                return True
        if any(key in permissions for key in ("read", "pull", "admin", "write")):
            return False

    for key in ("can_read", "has_read_access", "read_access"):
        if key in repo:
            return bool(repo.get(key))

    role = str(repo.get("role") or repo.get("access") or repo.get("permission") or "").lower()
    if role:
        return role in {"read", "reader", "pull", "write", "maintain", "admin", "owner"}

    return True


def _session_repo_identifiers(meta: dict) -> set[str]:
    remote = meta.get("remote") or {}
    git_info = remote.get("git") or remote
    return {
        str(value)
        for value in (
            git_info.get("url"),
            git_info.get("repository_name"),
            meta.get("repository"),
            meta.get("repository_name"),
        )
        if value
    }


def _pull_session_details(store: SessionStore, session_id: str, token: str) -> PullSessionDetails:
    base_url = _api_base_url(store)
    payload = _read_api_json(
        _auth_request(_session_endpoint(base_url, session_id), token),
        f"pulling session {session_id}",
    )
    details = PullSessionDetails.from_dict(payload)
    if not details.session_id:
        print(f"Error: Backend returned incomplete session metadata for '{session_id}'.", file=sys.stderr)
        sys.exit(1)
    if details.session_id != session_id:
        print("Error: Pulled session id does not match the requested session.", file=sys.stderr)
        print(f"  Requested: {session_id}", file=sys.stderr)
        print(f"  Pulled: {details.session_id}", file=sys.stderr)
        sys.exit(1)
    if not details.repository.id or not details.repository.name:
        print(f"Error: Backend returned incomplete repository metadata for session '{session_id}'.", file=sys.stderr)
        sys.exit(1)
    return details


def _pull_remote_session_steps(store: SessionStore, session_id: str, token: str) -> list[dict]:
    base_url = _api_base_url(store)
    steps_base = f"{base_url}/api/v1/sessions/{urllib.parse.quote(session_id, safe='')}/steps"
    page_size = 50
    page = 1
    steps: list[dict] = []

    while True:
        url = f"{steps_base}?steps_after=0&size={page_size}&page={page}"
        data = _read_api_json(
            _auth_request(url, token),
            f"pulling steps for session {session_id}",
        )

        if isinstance(data, list):
            raw_steps = data
            has_next = False
        else:
            raw_steps = data.get("results") or data.get("steps") or []
            has_next = bool(data.get("next"))

        steps.extend(raw_steps)
        fetched = len(steps)
        total = data.get("count") if isinstance(data, dict) else None
        total_str = f"/{total}" if total is not None else ""
        sys.stdout.write(f"\r  Pulling remote steps: {fetched}{total_str}")
        sys.stdout.flush()

        if not has_next or not raw_steps:
            break
        page += 1

    print()
    return steps


def _pull_remote_session_blobs(store: SessionStore, session_id: str, token: str) -> list[dict]:
    base_url = _api_base_url(store)
    blobs_base = f"{base_url}/api/v1/sessions/{urllib.parse.quote(session_id, safe='')}/blobs"
    page_size = 50
    page = 1
    blobs: list[dict] = []

    while True:
        url = f"{blobs_base}?size={page_size}&page={page}"
        data = _read_api_json(
            _auth_request(url, token),
            f"pulling blobs for session {session_id}",
        )

        if isinstance(data, list):
            raw_blobs = data
            has_next = False
        else:
            raw_blobs = data.get("results") or data.get("blobs") or []
            has_next = bool(data.get("next"))

        blobs.extend(raw_blobs)
        fetched = len(blobs)
        total = data.get("count") if isinstance(data, dict) else None
        total_str = f"/{total}" if total is not None else ""
        sys.stdout.write(f"\r  Pulling remote blobs: {fetched}{total_str}")
        sys.stdout.flush()

        if not has_next or not raw_blobs:
            break
        page += 1

    print()
    return blobs


def _require_tracked_repository(store: SessionStore) -> RepositoryDetails:
    repository = store.read_tracked_repo()
    if not repository:
        print("Error: No tracked repository is configured. Run `mach pull --repository <repository_name>` first.", file=sys.stderr)
        sys.exit(1)
    if repository.is_active is False:
        print("Error: Tracked repository is not active.", file=sys.stderr)
        sys.exit(1)
    if not _repo_allows_read(repository):
        print("Error: Tracked repository metadata does not grant read access.", file=sys.stderr)
        sys.exit(1)
    return repository


def _validate_session_against_tracked_repo(store: SessionStore, session_id: str, token: str) -> PullSessionDetails:
    repository = _require_tracked_repository(store)
    local_meta = store.read_session_meta(session_id)
    repo_ids = _repo_identifiers(repository)
    session_repo_ids = _session_repo_identifiers(local_meta)
    if repo_ids and session_repo_ids and repo_ids.isdisjoint(session_repo_ids):
        print("Error: Session does not belong to the tracked repository.", file=sys.stderr)
        print(f"  Tracked repo: {', '.join(sorted(repo_ids)[:3])}", file=sys.stderr)
        print(f"  Session repo: {', '.join(sorted(session_repo_ids)[:3])}", file=sys.stderr)
        sys.exit(1)

    session_details = _pull_session_details(store, session_id, token)
    mismatches = _repository_mismatches(repository, session_details.repository)
    if mismatches:
        print("Error: Remote session belongs to a different repository than the tracked repo.", file=sys.stderr)
        for mismatch in mismatches:
            print(f"  {mismatch}", file=sys.stderr)
        sys.exit(1)
    return session_details


def _pull_repository(repository_name: str) -> None:
    token = _require_auth_token()
    store = SessionStore()
    store.init_repo()
    base_url = _api_base_url(store)
    payload = _read_api_json(
        _auth_request(_repo_endpoint(base_url, repository_name), token),
        f"pulling repository {repository_name}",
    )
    repository = RepositoryDetails.from_dict({
        **payload,
        "pulled_at": int(time.time()),
        "api_base_url": base_url,
    })

    if repository.is_active is False:
        print(f"Error: Repository '{repository_name}' is not active.", file=sys.stderr)
        sys.exit(1)

    if not _repo_allows_read(repository):
        print(f"Error: Your token does not have read access to repository '{repository_name}'.", file=sys.stderr)
        sys.exit(1)

    if not repository.id or not repository.name:
        print(f"Error: Backend returned incomplete repository metadata for '{repository_name}'.", file=sys.stderr)
        sys.exit(1)

    _validate_repository_matches_git(repository, store)
    store.write_tracked_repo(repository)
    display_name = repository.name or repository_name
    print(f"Success: Tracking repository {display_name}.")
    print(f"  ID: {repository.id}")
    if repository.default_branch:
        print(f"  Default branch: {repository.default_branch}")
    print(f"  Metadata: {store.paths.tracked_repo_path}")


def push_command(args: argparse.Namespace) -> None:
    token = _require_auth_token()

    session_id = args.session_id

    # Handle --reset or --reset-to: clear or rewind the local push tracking
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

        # Determine what needs to be pushed
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

        # Read merkle root for client_root
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

            # Update local tracking after each successful batch (resumable on failure)
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


def _push_reset(session_id: str, reset_to: str | None = None) -> None:
    """Reset local Mach push-sync state so the session can be re-pushed."""
    store = SessionStore()
    meta = store.read_session_meta(session_id)
    if not meta:
        print(f"Error: Session {session_id} not found.", file=sys.stderr)
        sys.exit(1)

    if reset_to:
        # Validate that the step_id actually exists in the session
        steps_file = store.paths.sessions_dir / session_id / "steps.jsonl"
        step_ids = []
        if steps_file.exists():
            with open(steps_file, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    step_ids.append(json.loads(line).get("id"))

        if reset_to not in step_ids:
            print(f"Error: Step '{reset_to}' not found in session {session_id}.", file=sys.stderr)
            print(f"  Available steps: {', '.join(step_ids[:5])}{'...' if len(step_ids) > 5 else ''}", file=sys.stderr)
            sys.exit(1)

        store.update_push_state(
            session_id,
            mach_updates={"last_pushed_step_id": reset_to},
        )
        print(f"Reset push state for {session_id} to step {reset_to}.")
        print(f"  Steps after '{reset_to}' will be pushed on next `mach push`.")
    else:
        # Full reset — wipe all Mach sync state
        from mach.models import MachSyncState
        store.update_push_state(
            session_id,
            mach_updates=MachSyncState().to_dict(),
        )
        print(f"Fully reset push state for {session_id}.")
        print(f"  All steps will be pushed on next `mach push`.")


def pull_command(args: argparse.Namespace) -> None:
    """Reconcile local push-tracking with the server by paginating through the
    server's step listing.  Does NOT modify local session steps — only updates
    the local Mach sync cursor so the next `mach push` sends the right delta.
    """
    PULL_PAGE_SIZE = 50  # steps per request — keeps server load reasonable

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
        # TODO: infer the repository from local Git remotes when multiple tracked
        # remotes are supported and the backend can resolve ambiguity safely.
        repository = _require_tracked_repository(store)

    session_details = _pull_session_details(store, source_session_id, token)
    mismatches = _repository_mismatches(repository, session_details.repository)
    if mismatches:
        print("Error: Remote session belongs to a different repository than the tracked repo.", file=sys.stderr)
        for mismatch in mismatches:
            print(f"  {mismatch}", file=sys.stderr)
        sys.exit(1)

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


# This is old pull command (replacement for pull_command) this pulls sessions as well as repository
# Currently we don't support pulling session
def __pull_command(args: argparse.Namespace) -> None:
    """Reconcile local push-tracking with the server by paginating through the
    server's step listing.  Does NOT modify local session steps — only updates
    the local Mach sync cursor so the next `mach push` sends the right delta.
    """
    PULL_PAGE_SIZE = 50  # steps per request — keeps server load reasonable

    if args.repository and (args.session_id or args.session):
        print("Error: Use either --repository or --session, not both.", file=sys.stderr)
        sys.exit(1)

    if args.repository:
        _pull_repository(args.repository)
        return

    token = _require_auth_token()

    session_id = args.session or args.session_id
    if not session_id:
        print("Error: Provide a session with `mach pull --session <session_id>`.", file=sys.stderr)
        print("       To track a repository, use `mach pull --repository <repository_name>`.", file=sys.stderr)
        sys.exit(1)

    store = SessionStore()

    # ── 1. Verify the session exists locally ────────────────────────────────
    try:
        meta = store.read_session_meta(session_id)
    except Exception:
        meta = None
    if not meta:
        print(f"Error: Session '{session_id}' not found locally.", file=sys.stderr)
        sys.exit(1)

    _validate_session_against_tracked_repo(store, session_id, token)

    base_url = _api_base_url(store)
    steps_base = f"{base_url}/api/v1/sessions/{session_id}/steps"

    # ── 2. Load local step index ─────────────────────────────────────────────
    steps_file = store.paths.sessions_dir / session_id / "steps.jsonl"
    local_steps: list[dict] = []
    if steps_file.exists():
        with open(steps_file, "r", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                s = json.loads(line)
                local_steps.append({"id": s.get("id"), "step_num": s.get("step_num")})

    local_total = len(local_steps)
    local_ids: set[str] = {s["id"] for s in local_steps if s.get("id")}

    print(f"Pulling sync state for session {session_id}...")
    print(f"  Local steps : {local_total}")

    # ── 3. Paginate server steps ─────────────────────────────────────────────
    from mach.models import PullStepsPage

    server_records: list = []   # list[PullStepRecord]
    server_total: int | None = None
    page = 1

    while True:
        url = (
            f"{steps_base}"
            f"?steps_after=0&size={PULL_PAGE_SIZE}&page={page}"
        )
        req = urllib.request.Request(
            url,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            method="GET",
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8")
                data = json.loads(body) if body else {}
        except urllib.error.HTTPError as http_err:
            if http_err.code == 404:
                # ── Case: session does not exist on server ───────────────────
                print(f"  Session not found on server (404).")
                print(f"  Clearing sync cursor so all {local_total} local step(s) will be pushed.")
                _push_reset(session_id, reset_to=None)
                return
            err_body = http_err.read().decode("utf-8", errors="replace")
            print(f"Error: Server returned {http_err.code}", file=sys.stderr)
            if err_body:
                print(err_body, file=sys.stderr)
            sys.exit(1)
        except urllib.error.URLError as url_err:
            print(f"Error: Could not connect to server ({steps_base}): {url_err}", file=sys.stderr)
            sys.exit(1)

        pulled = PullStepsPage.from_dict(data, page=page, size=PULL_PAGE_SIZE)

        # Capture total from first page
        if server_total is None and pulled.count is not None:
            server_total = pulled.count

        server_records.extend(pulled.results)

        fetched = len(server_records)
        total_str = f"/{server_total}" if server_total is not None else ""
        sys.stdout.write(f"\r  Fetching from server: {fetched}{total_str} steps")
        sys.stdout.flush()

        # Stop when there are no more pages or the page came back empty
        if not pulled.has_next or not pulled.results:
            break
        page += 1

    print()  # newline after the inline progress

    server_total_actual = server_total if server_total is not None else len(server_records)
    print(f"  Server steps: {server_total_actual}")

    # ── 4. Analyse gaps ──────────────────────────────────────────────────────
    # Ordered list of mach_ids as the server knows them
    server_mach_ids: list[str] = [r.mach_id for r in server_records if r.mach_id]
    server_id_set: set[str] = set(server_mach_ids)

    # Steps on server that also exist locally (preserving server order)
    matched = [sid for sid in server_mach_ids if sid in local_ids]

    # Steps on server NOT in local (unusual — e.g. pushed from another machine)
    server_only = [sid for sid in server_mach_ids if sid not in local_ids]

    # Local steps missing from server (these still need to be pushed)
    local_only = [s["id"] for s in local_steps if s.get("id") and s["id"] not in server_id_set]

    # Last step that both sides agree on → becomes the new sync cursor
    last_synced_id: str | None = matched[-1] if matched else None

    # ── 5. Print clear summary ───────────────────────────────────────────────
    print()

    if server_total_actual == 0:
        # ── Case: session exists on server but has no steps yet ──────────────
        print("  Server has the session but no steps yet.")
        print(f"  Clearing sync cursor — run `mach push {session_id}` to push all {local_total} steps.")
        _push_reset(session_id, reset_to=None)
        return

    if server_only:
        print(f"  ⚠  {len(server_only)} step(s) on server not found locally.")
        print(f"     Another machine may have pushed to this session.")
        sample = ", ".join(server_only[:3])
        suffix = "..." if len(server_only) > 3 else ""
        print(f"     Server-only: {sample}{suffix}")

    if local_only:
        print(f"  ↑  {len(local_only)} local step(s) not on server yet.")
    elif not server_only:
        # ── Case: fully in sync ──────────────────────────────────────────────
        print("  ✓  Fully in sync — local and server steps match.")

    # ── 6. Update Mach sync state ─────────────────────────────────────────────
    if last_synced_id:
        store.update_push_state(
            session_id,
            mach_updates={
                "last_pushed_step_id": last_synced_id,
                "last_pushed_ts": int(time.time()),
            },
            step_count=server_total_actual,
        )
        print(f"  Sync cursor → {last_synced_id}")
    else:
        # Nothing in common — treat as a full reset
        print("  No overlapping steps found between local and server.")
        print("  Clearing sync cursor for a full re-push.")
        _push_reset(session_id, reset_to=None)

    if local_only:
        print(f"\n  Run `mach push {session_id}` to push {len(local_only)} remaining step(s).")


def _format_push_step(store: SessionStore, step: dict, blobs: dict[str, str]) -> dict:
    content_hash = step.get("content_hash")
    _collect_blob(store, blobs, content_hash, step.get("content"))

    tool = step.get("tool")
    formatted_tool = None
    if tool:
        tool_hash = tool.get("content_hash")
        _collect_blob(store, blobs, tool_hash, tool.get("content"))
        formatted_tool = {
            "name": tool.get("name"),
            "category": tool.get("category", "exec"),
            "content_hash": tool_hash,
            "content": tool.get("content") or blobs.get(tool_hash) if tool_hash else None,
        }

    payload = {
        "id": step.get("id"),
        "step_num": step.get("step_num"),
        "timestamp": step.get("ts"),
        "type": step.get("type"),
        "content_hash": content_hash,
        "content": step.get("content") or blobs.get(content_hash) if content_hash else None,
        "commit_hash": step.get("commit_hash"),
        "caused_by": step.get("caused_by", []),
        "risk_level": step.get("risk_level", "none"),
        "tool": formatted_tool,
        "file_changes": [_format_push_file_change(store, change) for change in step.get("file_changes", [])],
        "risk_flags": step.get("risk_flags", []),
        "parent_step_id": step.get("parent_step_id"),
    }
    return {key: value for key, value in payload.items() if value is not None}


def _format_push_file_change(store: SessionStore, change: dict) -> dict:
    formatted = dict(change)
    file_path = formatted.get("file_path")
    if file_path:
        path = Path(file_path)
        if path.is_absolute():
            try:
                formatted["file_path"] = str(path.relative_to(store.paths.repo_root))
            except ValueError:
                formatted["file_path"] = str(path)
    return formatted


def _collect_blob(store: SessionStore, blobs: dict[str, str], content_hash: str | None, inline_content: str | None = None) -> None:
    if not content_hash:
        return
    content = inline_content if inline_content is not None else store._read_blob(content_hash)
    if content is not None:
        blobs[content_hash] = content


def _push_host_name() -> str:
    try:
        import socket
        return socket.gethostname()
    except Exception:
        return sys.platform


def _agent_provider(agent_name: str) -> str:
    """Map agent name to its provider for the push payload."""
    mapping = {
        "gemini": "google",
        "claude": "anthropic",
        "codex": "openai",
        "copilot": "github",
        "cursor": "anysphere",
    }
    return mapping.get(agent_name.lower(), "unknown")




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
        
        # Also update the python environment if it exists
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


def config_show_command(args: argparse.Namespace) -> None:
    config = SessionStore().read_config()
    if getattr(args, "json", False):
        emit(config)
    else:
        print("Mach Configuration:")
        print("-" * 50)
        for key, value in sorted(config.items()):
            print(f"{key:25} : {value}")


def config_set_command(args: argparse.Namespace) -> None:
    store = SessionStore()
    current = store.read_config()
    updates = {}

    if args.enable:
        updates["enabled"] = True
    if args.disable:
        updates["enabled"] = False
    if args.auto_tracking is not None:
        updates["auto_tracking"] = args.auto_tracking == "true"
    if args.commit_closes_session is not None:
        updates["commit_closes_session"] = args.commit_closes_session == "true"
    if args.idle_timeout_sec is not None:
        updates["idle_timeout_sec"] = None if args.idle_timeout_sec == "none" else int(args.idle_timeout_sec)
    if args.poll_interval_sec is not None:
        updates["poll_interval_sec"] = float(args.poll_interval_sec)
    if args.store_content is not None:
        updates["store_content"] = [t.strip() for t in args.store_content.split(",") if t.strip()]
    if args.use_tui is not None:
        updates["use_tui"] = args.use_tui == "true"
    if args.db_enabled is not None:
        updates["db_enabled"] = args.db_enabled == "true"

    hook_agents = list(current.get("hook_agents") or [])
    if args.hook_agents is not None:
        hook_agents = [agent for agent in args.hook_agents.split(",") if agent]
    if args.add_agent:
        for agent in args.add_agent:
            if agent not in hook_agents:
                hook_agents.append(agent)
    if args.remove_agent:
        hook_agents = [agent for agent in hook_agents if agent not in set(args.remove_agent)]
    if args.hook_agents is not None or args.add_agent or args.remove_agent:
        updates["hook_agents"] = hook_agents

    config = store.update_config(updates) if updates else current

    manager = HookManager()
    hook_result = None
    tracking_result = None

    if args.refresh_hooks:
        hook_result = manager.uninstall(["all"])
        hook_result = {
            "uninstalled": hook_result["uninstalled"],
            "installed": manager.install(config.get("hook_agents") or []).get("installed", []),
        }
    elif args.apply and config.get("enabled", True):
        hook_result = manager.install(config.get("hook_agents") or [])
    elif args.apply and not config.get("enabled", True):
        hook_result = manager.uninstall(["all"])

    if args.apply:
        tracker = TrackerService()
        if config.get("enabled", True) and config.get("auto_tracking", True):
            tracker.ensure_state()
            tracking_result = tracker.start_daemon()
        else:
            tracking_result = tracker.stop_daemon()

    print("Success: Configuration applied.")


def session_start(args: argparse.Namespace) -> None:
    store = SessionStore()
    emit(store.start_session(agent=args.agent, task_desc=args.task_desc))


def session_end(args: argparse.Namespace) -> None:
    store = SessionStore()
    emit(store.end_session(session_id=args.session_id))


def log_command(args: argparse.Namespace) -> None:
    store = SessionStore()
    if hasattr(args, "session_id") and args.session_id:
        data = store.show_session(session_id=args.session_id)
        if getattr(args, "json", False):
            emit(data)
        elif getattr(args, "content", False):
            pydoc.pager(format_session_details(data, patch=getattr(args, "patch", False)))
        else:
            pydoc.pager(format_session_steps(data, oneline=getattr(args, "oneline", False), patch=getattr(args, "patch", False)))
    else:
        sessions = store.list_sessions()
        import sys
        if getattr(args, "json", False):
            emit(sessions)
        elif sys.stdout.isatty() and not getattr(args, "no_tui", False) and store.get_config().get("use_tui", True):
            from mach.tui import run_tui
            run_tui(store)
        else:
            pydoc.pager(format_sessions_list(sessions))


def show_command(args: argparse.Namespace) -> None:
    store = SessionStore()
    data = store.show_session(session_id=args.session_id)
    if getattr(args, "json", False):
        emit(data)
    else:
        pydoc.pager(format_session_details(data, patch=getattr(args, "patch", False)))


def verify_command(args: argparse.Namespace) -> None:
    store = SessionStore()
    if args.session_id:
        emit(store.verify_session(args.session_id))
    else:
        emit(store.verify_all())


def on_commit_command(_: argparse.Namespace) -> None:
    store = SessionStore()
    emit(store.on_commit())


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

    changed_results = [item for item in result["results"] if item.get("changed")]
    for item in changed_results[:5]:
        print(
            f"  {item['session_id']}: "
            f"{item['before_steps']} -> {item['after_steps']} steps, "
            f"merged {item['merged_steps']}, "
            f"tool hashes {item['normalized_tool_hashes']}"
        )
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


def rewind_command(args: argparse.Namespace) -> None:
    store = SessionStore()
    emit(store.rewind(target=args.target))


def resume_command(args: argparse.Namespace) -> None:
    store = SessionStore()
    emit(store.resume_branch(branch=args.branch))


def clean_command(args: argparse.Namespace) -> None:
    store = SessionStore()
    emit(store.clean(max_days=int(args.max_days)))


def doctor_command(_: argparse.Namespace) -> None:
    store = SessionStore()
    fsck_res = store.fsck()
    tracker = TrackerService()
    if tracker.status().get("running"):
        tracker.stop_daemon()
    t_res = tracker.start_daemon()
    emit({"fsck": fsck_res, "tracker": t_res})


def ingest_event_command(args: argparse.Namespace) -> None:
    ingest = EventInboxService()
    step: dict[str, object] = {
        "type": args.step_type,
        "content": args.content,
    }
    if args.tool_name or args.tool_category or args.tool_content:
        step["tool"] = {
            "name": args.tool_name,
            "category": args.tool_category,
            "content": args.tool_content,
        }
    if args.risk_level:
        step["risk_level"] = args.risk_level

    payload = {
        "kind": "step",
        "agent": args.agent,
        "source_session_id": args.source_session_id,
        "task_desc": args.task_desc,
        "end_session": args.end_session,
        "step": step,
    }
    result = ingest.enqueue_event(payload, stream=args.stream)
    if args.process_now:
        result["processed"] = ingest.process_pending_events()
    emit(result)


def ingest_end_command(args: argparse.Namespace) -> None:
    ingest = EventInboxService()
    result = ingest.enqueue_event(
        {
            "kind": "session_end",
            "agent": args.agent,
            "source_session_id": args.source_session_id,
        },
        stream=args.stream,
    )
    if args.process_now:
        result["processed"] = ingest.process_pending_events()
    emit(result)


def ingest_process_command(_: argparse.Namespace) -> None:
    ingest = EventInboxService()
    emit(ingest.process_pending_events())


def hooks_install_command(args: argparse.Namespace) -> None:
    manager = HookManager()
    emit(manager.install(args.agents))


def hooks_uninstall_command(args: argparse.Namespace) -> None:
    manager = HookManager()
    emit(manager.uninstall(args.agents))


def hooks_status_command(args: argparse.Namespace) -> None:
    manager = HookManager()
    emit(manager.status(args.agents))


def hooks_dispatch_command(args: argparse.Namespace) -> None:
    repo_root = Path(args.repo_root) if args.repo_root else None
    raw_payload = sys.stdin.read()
    try:
        manager = HookManager(repo_root=repo_root)
        result = manager.dispatch(
            agent=args.agent,
            event_name=args.event,
            raw_payload=raw_payload,
            repo_root=repo_root,
        )
    except Exception:
        if args.stdout_mode == "empty-json":
            sys.stdout.write("{}")
            return
        raise
    if args.stdout_mode == "empty-json":
        sys.stdout.write(result.emitted_output or "{}")
    elif args.stdout_mode == "passthrough" and result.emitted_output:
        sys.stdout.write(result.emitted_output)


def track_start_command(_: argparse.Namespace) -> None:
    tracker = TrackerService()
    emit(tracker.start_daemon())


def track_stop_command(_: argparse.Namespace) -> None:
    tracker = TrackerService()
    emit(tracker.stop_daemon())


def track_status_command(_: argparse.Namespace) -> None:
    tracker = TrackerService()
    emit(tracker.status())


def track_scan_command(_: argparse.Namespace) -> None:
    tracker = TrackerService()
    emit(tracker.scan_once())


def track_run_command(args: argparse.Namespace) -> None:
    tracker = TrackerService(repo_root=Path(args.repo_root))
    emit(tracker.run_loop(once=args.once))


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="mach",
        description="Local-first execution logging for AI agents.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Initialize .mach metadata.")
    init_parser.add_argument("--hook-agents", help="Comma-separated agents to install without prompting.")
    init_parser.add_argument("--store-content", help="Comma-separated step types to store content for without prompting.")
    init_parser.set_defaults(handler=init_command)

    enable_parser = subparsers.add_parser("enable", help="Enable Mach hooks and background tracking in this repo.")
    enable_parser.set_defaults(handler=enable_command)

    disable_parser = subparsers.add_parser("disable", help="Disable Mach hooks and background tracking in this repo.")
    disable_parser.set_defaults(handler=disable_command)

    session_parser = subparsers.add_parser("session", help="Manage sessions.")
    session_subparsers = session_parser.add_subparsers(dest="session_command", required=True)

    session_start_parser = session_subparsers.add_parser("start", help="Start a session.")
    session_start_parser.add_argument("--agent", default="unknown")
    session_start_parser.add_argument("--task-desc")
    session_start_parser.set_defaults(handler=session_start)

    session_end_parser = session_subparsers.add_parser("end", help="End a session.")
    session_end_parser.add_argument("session_id", nargs="?")
    session_end_parser.set_defaults(handler=session_end)

    log_parser = subparsers.add_parser("log", help="List known sessions or view a specific session.")
    log_parser.add_argument("session_id", nargs="?", help="Specific session ID to view.")
    log_parser.add_argument("--json", action="store_true", help="Output raw JSON.")
    log_parser.add_argument("--content", action="store_true", help="Show full content transcript instead of summary.")
    log_parser.add_argument("--oneline", action="store_true", help="Format steps as a single line.")
    log_parser.add_argument("--patch", "-p", action="store_true", help="Show file changes and hunks.")
    log_parser.add_argument("--no-tui", action="store_true", help="Use static pager output instead of interactive TUI.")
    log_parser.set_defaults(handler=log_command)

    show_parser = subparsers.add_parser("show", help="Show a session.")
    show_parser.add_argument("session_id", nargs="?", help="Session ID to show.")
    show_parser.add_argument("--json", action="store_true", help="Output raw JSON.")
    show_parser.add_argument("--patch", "-p", action="store_true", help="Show file changes and hunks.")
    show_parser.set_defaults(handler=show_command)

    verify_parser = subparsers.add_parser("verify", help="Verify Merkle integrity.")
    verify_parser.add_argument("session_id", nargs="?")
    verify_parser.set_defaults(handler=verify_command)

    fsck_parser = subparsers.add_parser("fsck", help="Rebuild the SQLite index from JSONL logs.")
    fsck_parser.set_defaults(handler=fsck_command)

    fix_parser = subparsers.add_parser("fix", help="Normalize session ledgers.")
    fix_parser.add_argument("session_id", nargs="?")
    fix_parser.add_argument("--apply", action="store_true", help="Rewrite session ledgers. Without this, only report changes.")
    fix_parser.set_defaults(handler=internal_fix_command)

    rewind_parser = subparsers.add_parser("rewind", help="Rewind workspace to target commit in append-only mode.")
    rewind_parser.add_argument("target", help="Commit hash or branch name to rewind to.")
    rewind_parser.set_defaults(handler=rewind_command)

    resume_parser = subparsers.add_parser("resume", help="Resume latest session on active branch.")
    resume_parser.add_argument("branch", nargs="?", help="Specific branch to resume on.")
    resume_parser.set_defaults(handler=resume_command)

    clean_parser = subparsers.add_parser("clean", help="Clean orphaned AI sessions.")
    clean_parser.add_argument("--max-days", default=7, type=int, help="Delete sessions older than max days without a commit.")
    clean_parser.set_defaults(handler=clean_command)

    doctor_parser = subparsers.add_parser("doctor", help="Fix broken sessions and restart trackers.")
    doctor_parser.set_defaults(handler=doctor_command)

    on_commit_parser = subparsers.add_parser("on-commit", help="Close active session after a commit.")
    on_commit_parser.set_defaults(handler=on_commit_command)

    ingest_parser = subparsers.add_parser("ingest", help="Ingest AI-agent events into Mach.")
    ingest_subparsers = ingest_parser.add_subparsers(dest="ingest_command", required=True)

    ingest_event_parser = ingest_subparsers.add_parser("event", help="Queue a structured AI activity event.")
    ingest_event_parser.add_argument("--agent", required=True)
    ingest_event_parser.add_argument("--source-session-id")
    ingest_event_parser.add_argument("--task-desc")
    ingest_event_parser.add_argument("--type", dest="step_type", required=True)
    ingest_event_parser.add_argument("--content", default="")
    ingest_event_parser.add_argument("--tool-name")
    ingest_event_parser.add_argument("--tool-category")
    ingest_event_parser.add_argument("--tool-content")
    ingest_event_parser.add_argument("--risk-level")
    ingest_event_parser.add_argument("--stream", default="events")
    ingest_event_parser.add_argument("--end-session", action="store_true")
    ingest_event_parser.add_argument("--process-now", action="store_true")
    ingest_event_parser.set_defaults(handler=ingest_event_command)

    ingest_end_parser = ingest_subparsers.add_parser("end", help="Queue an agent session end event.")
    ingest_end_parser.add_argument("--agent", required=True)
    ingest_end_parser.add_argument("--source-session-id")
    ingest_end_parser.add_argument("--stream", default="events")
    ingest_end_parser.add_argument("--process-now", action="store_true")
    ingest_end_parser.set_defaults(handler=ingest_end_command)

    ingest_process_parser = ingest_subparsers.add_parser("process", help="Process queued AI events now.")
    ingest_process_parser.set_defaults(handler=ingest_process_command)

    hooks_parser = subparsers.add_parser("hooks", help="Install and manage agent hook integrations.")
    hooks_subparsers = hooks_parser.add_subparsers(dest="hooks_command", required=True)

    hooks_install_parser = hooks_subparsers.add_parser("install", help="Install Mach hooks for supported agents.")
    hooks_install_parser.add_argument("agents", nargs="*", default=["all"])
    hooks_install_parser.set_defaults(handler=hooks_install_command)

    hooks_uninstall_parser = hooks_subparsers.add_parser("uninstall", help="Remove Mach hooks for agents.")
    hooks_uninstall_parser.add_argument("agents", nargs="*", default=["all"])
    hooks_uninstall_parser.set_defaults(handler=hooks_uninstall_command)

    hooks_status_parser = hooks_subparsers.add_parser("status", help="Show hook installation status.")
    hooks_status_parser.add_argument("agents", nargs="*", default=["all"])
    hooks_status_parser.set_defaults(handler=hooks_status_command)

    hooks_dispatch_parser = hooks_subparsers.add_parser("dispatch", help="Internal: receive a vendor hook payload on stdin.")
    hooks_dispatch_parser.add_argument("--agent", required=True)
    hooks_dispatch_parser.add_argument("--event", required=True)
    hooks_dispatch_parser.add_argument("--repo-root", default=".")
    hooks_dispatch_parser.add_argument("--stdout-mode", choices=["silent", "empty-json", "passthrough"], default="silent")
    hooks_dispatch_parser.set_defaults(handler=hooks_dispatch_command)

    config_parser = subparsers.add_parser("config", help="Show or update Mach configuration.")
    config_subparsers = config_parser.add_subparsers(dest="config_command", required=True)

    config_show_parser = config_subparsers.add_parser("show", help="Show merged Mach config.")
    config_show_parser.add_argument("--json", action="store_true", help="Output raw JSON.")
    config_show_parser.set_defaults(handler=config_show_command)

    config_set_parser = config_subparsers.add_parser("set", help="Update Mach config values.")
    config_set_parser.add_argument("--enable", action="store_true")
    config_set_parser.add_argument("--disable", action="store_true")
    config_set_parser.add_argument("--auto-tracking", choices=["true", "false"])
    config_set_parser.add_argument("--commit-closes-session", choices=["true", "false"])
    config_set_parser.add_argument("--idle-timeout-sec")
    config_set_parser.add_argument("--poll-interval-sec")
    config_set_parser.add_argument("--hook-agents")
    config_set_parser.add_argument("--add-agent", action="append")
    config_set_parser.add_argument("--remove-agent", action="append")
    config_set_parser.add_argument("--store-content", help="Comma-separated step types to store content for (e.g. input,reasoning,tool,output)")
    config_set_parser.add_argument("--use-tui", choices=["true", "false"])
    config_set_parser.add_argument("--db-enabled", choices=["true", "false"])
    config_set_parser.add_argument("--apply", action="store_true", help="Apply current config to hooks and tracker after updating.")
    config_set_parser.add_argument("--refresh-hooks", action="store_true", help="Reinstall managed hooks after updating config.")
    config_set_parser.set_defaults(handler=config_set_command)

    track_parser = subparsers.add_parser("track", help="Manage automatic repository tracking.")
    track_subparsers = track_parser.add_subparsers(dest="track_command", required=True)

    track_start_parser = track_subparsers.add_parser("start", help="Start the background tracker.")
    track_start_parser.set_defaults(handler=track_start_command)

    track_stop_parser = track_subparsers.add_parser("stop", help="Stop the background tracker.")
    track_stop_parser.set_defaults(handler=track_stop_command)

    track_status_parser = track_subparsers.add_parser("status", help="Show tracker status.")
    track_status_parser.set_defaults(handler=track_status_command)

    track_scan_parser = track_subparsers.add_parser("scan", help="Run one tracking scan immediately.")
    track_scan_parser.set_defaults(handler=track_scan_command)

    track_run_parser = track_subparsers.add_parser("run", help="Run the tracker loop.")
    track_run_parser.add_argument("--repo-root", default=".")
    track_run_parser.add_argument("--once", action="store_true")
    track_run_parser.set_defaults(handler=track_run_command)

    # Alias `mach session <id>` to `mach show <id>` implicitly.
    if len(sys.argv) >= 3 and sys.argv[1] == "session" and sys.argv[2] not in ("start", "end", "-h", "--help"):
        sys.argv[1] = "show"

    login_parser = subparsers.add_parser("login", help="Authenticate with the Mach web platform.")
    login_parser.add_argument("--token", help="Your Personal Access Token.")
    login_parser.set_defaults(handler=login_command)

    logout_parser = subparsers.add_parser("logout", help="Log out of the Mach web platform.")
    logout_parser.set_defaults(handler=logout_command)

    push_parser = subparsers.add_parser("push", help="Push a session to the Mach web platform.")
    push_parser.add_argument("session_id", help="The ID of the session to push.")
    push_parser.add_argument("--reset", action="store_true", help="Reset local push tracking so the session can be re-pushed.")
    push_parser.add_argument("--reset-to", metavar="STEP_ID", help="Reset push state to a specific step ID (re-push steps after it).")
    push_parser.set_defaults(handler=push_command)

    pull_parser = subparsers.add_parser("pull", help="Pull repository metadata or reconcile session tracking.")
    # pull_parser.add_argument("session_id", nargs="?", help="The ID of the session to check.") # Uncomment this and remove the repository_name arg when we support pulling sessions
    pull_parser.add_argument("repository_name", nargs="?", help="The name of the repository to pull.")
    pull_parser.add_argument("-r", "--repository", metavar="repository_name", help="Repository name to track as the trust boundary.")
    pull_parser.add_argument("-s", "--session", help="The ID of the session to check.")
    pull_parser.set_defaults(handler=pull_command, repository=None, repository_name=None, session=None, session_id=None)

    clone_parser = subparsers.add_parser("clone", help="Clone a remote session into a new local fork.")
    clone_parser.add_argument("clone_arg", metavar="repository_or_session", help="Repository name, or the session ID when a repo is already tracked.")
    clone_parser.add_argument("session_id", nargs="?", help="The session ID to clone.")
    clone_parser.set_defaults(handler=clone_command)

    update_parser = subparsers.add_parser("update", help="Update the global Mach installation to the latest version.")
    update_parser.set_defaults(handler=update_command)

    try:
        args = parser.parse_args()
        args.handler(args)
    except MachError as exc:
        raise SystemExit(f"Error: {exc}") from exc


if __name__ == "__main__":
    main()
