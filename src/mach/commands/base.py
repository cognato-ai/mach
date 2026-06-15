from __future__ import annotations

import json
import sys
try:
    import termios
    import tty
except ImportError:
    termios = None
    tty = None
import getpass
import urllib.request
import urllib.error
import urllib.parse
import time
from pathlib import Path

from mach.auth import save_token, logout, get_token
from mach.config import DEFAULT_CONFIG
from mach.hooks import HookManager
from mach.models import PullSessionDetails, RepositoryDetails
from mach.session import MachError, SessionStore
from mach.ui import render_sessions_list, render_session_steps, render_session_details

STORE_CONTENT_TYPES = list(DEFAULT_CONFIG["store_content"])

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

    if not termios or not tty:
        print(prompt)
        print("Available choices:")
        for idx, choice in enumerate(choices, 1):
            status = "[x]" if choice["value"] in selected_values else "[ ]"
            print(f"  {idx}. {status} {choice['label']}")
        print("Enter comma-separated numbers to toggle, or just press Enter to confirm current selection:")
        try:
            val = input("> ").strip()
            if not val:
                return selected_values
            selected = set(selected_values)
            indices = [int(i.strip()) - 1 for i in val.split(",") if i.strip().isdigit()]
            for idx in indices:
                if 0 <= idx < len(choices):
                    choice_val = choices[idx]["value"]
                    if choice_val in selected:
                        selected.remove(choice_val)
                    else:
                        selected.add(choice_val)
            return [choice["value"] for choice in choices if choice["value"] in selected]
        except Exception:
            return selected_values

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

def _push_reset(session_id: str, reset_to: str | None = None) -> None:
    """Reset local Mach push-sync state so the session can be re-pushed."""
    store = SessionStore()
    meta = store.read_session_meta(session_id)
    if not meta:
        print(f"Error: Session {session_id} not found.", file=sys.stderr)
        sys.exit(1)

    if reset_to:
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
        from mach.models import MachSyncState
        store.update_push_state(
            session_id,
            mach_updates=MachSyncState().to_dict(),
        )
        print(f"Fully reset push state for {session_id}.")
        print(f"  All steps will be pushed on next `mach push`.")

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
    mapping = {
        "gemini": "google",
        "claude": "anthropic",
        "codex": "openai",
        "copilot": "github",
        "cursor": "anysphere",
    }
    return mapping.get(agent_name.lower(), "unknown")
