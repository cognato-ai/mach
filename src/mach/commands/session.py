from __future__ import annotations

import argparse
import sys
import pydoc

from mach.session import SessionStore
from mach.ui import render_session_diff
from mach.commands.base import (
    emit,
    format_session_details,
    format_session_steps,
    format_sessions_list,
)

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

def diff_command(args: argparse.Namespace) -> None:
    store = SessionStore()
    session_id = getattr(args, "session_id", None)
    if getattr(args, "json", False):
        data = store.session_diff(session_id=session_id)
        emit(data)
    elif sys.stdout.isatty() and not getattr(args, "no_tui", False) and store.get_config().get("use_tui", True):
        target_id = store.session_diff(session_id=session_id)["meta"].get("id") or session_id
        from mach.tui import DiffOnlyApp
        DiffOnlyApp(store, target_id).run()
    else:
        data = store.session_diff(session_id=session_id)
        print(render_session_diff(data))

def rewind_command(args: argparse.Namespace) -> None:
    store = SessionStore()
    emit(store.rewind(target=args.target))

def resume_command(args: argparse.Namespace) -> None:
    store = SessionStore()
    emit(store.resume_branch(branch=args.branch))

def clean_command(args: argparse.Namespace) -> None:
    store = SessionStore()
    emit(store.clean(max_days=int(args.max_days)))
