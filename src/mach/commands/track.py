from __future__ import annotations

import argparse
from pathlib import Path

from mach.tracker import TrackerService
from mach.commands.base import emit

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
