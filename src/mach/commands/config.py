from __future__ import annotations

import argparse

from mach.session import SessionStore
from mach.hooks import HookManager
from mach.tracker import TrackerService
from mach.commands.base import emit

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
