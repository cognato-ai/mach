from __future__ import annotations

import argparse

from mach.ingest import EventInboxService
from mach.commands.base import emit

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
