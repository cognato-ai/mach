from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mach.hooks import HookManager
from mach.commands.base import emit

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
