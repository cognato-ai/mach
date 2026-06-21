from __future__ import annotations

import argparse
import sys

from mach.session import MachError
from mach.config import DEFAULT_CONFIG

# Modular commands
from mach.commands.repo import (
    init_command,
    enable_command,
    disable_command,
    login_command,
    logout_command,
    push_command,
    pull_command,
    clone_command,
    verify_command,
    fsck_command,
    internal_fix_command,
    update_command,
    doctor_command,
    on_commit_command,
)
from mach.commands.session import (
    session_start,
    session_end,
    log_command,
    show_command,
    diff_command,
    rewind_command,
    resume_command,
    clean_command,
)
from mach.commands.ingest import (
    ingest_event_command,
    ingest_end_command,
    ingest_process_command,
)
from mach.commands.hooks import (
    hooks_install_command,
    hooks_uninstall_command,
    hooks_status_command,
    hooks_dispatch_command,
)
from mach.commands.config import (
    config_show_command,
    config_set_command,
)
from mach.commands.track import (
    track_start_command,
    track_stop_command,
    track_status_command,
    track_scan_command,
    track_run_command,
)

STORE_CONTENT_TYPES = list(DEFAULT_CONFIG["store_content"])


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

    diff_parser = subparsers.add_parser("diff", help="Show aggregate file changes for a session.")
    diff_parser.add_argument("session_id", nargs="?", help="Session ID to diff (default: active session).")
    diff_parser.add_argument("--json", action="store_true", help="Output raw JSON.")
    diff_parser.add_argument("--no-tui", action="store_true", help="Use static pager output instead of interactive TUI.")
    diff_parser.set_defaults(handler=diff_command)

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
