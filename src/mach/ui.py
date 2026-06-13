"""
mach.ui — Premium terminal rendering for mach CLI.

Pure stdlib ANSI. Zero external dependencies.
Design language: git-native, information-dense, audit-first.
"""
from __future__ import annotations

import os
import shutil
import time as _time
from typing import Any


# ---------------------------------------------------------------------------
# ANSI palette
# ---------------------------------------------------------------------------
class C:
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"
    ITALIC  = "\033[3m"

    # Foreground
    BLACK   = "\033[30m"
    RED     = "\033[31m"
    GREEN   = "\033[32m"
    YELLOW  = "\033[33m"
    BLUE    = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN    = "\033[36m"
    WHITE   = "\033[37m"

    # Bright foreground
    BRED    = "\033[91m"
    BGREEN  = "\033[92m"
    BYELLOW = "\033[93m"
    BBLUE   = "\033[94m"
    BMAGENTA= "\033[95m"
    BCYAN   = "\033[96m"
    BWHITE  = "\033[97m"

    # Background
    BG_BLACK  = "\033[40m"
    BG_BLUE   = "\033[44m"
    BG_CYAN   = "\033[46m"

    @staticmethod
    def rgb(r: int, g: int, b: int) -> str:
        return f"\033[38;2;{r};{g};{b}m"

    @staticmethod
    def bg_rgb(r: int, g: int, b: int) -> str:
        return f"\033[48;2;{r};{g};{b}m"


# ---------------------------------------------------------------------------
# Step type metadata
# ---------------------------------------------------------------------------
STEP_META: dict[str, dict[str, str]] = {
    "input":         {"icon": "▶", "label": "INPUT",    "color": C.BGREEN},
    "reasoning":     {"icon": "◈", "label": "THINK",    "color": C.BCYAN},
    "tool":          {"icon": "⚙", "label": "TOOL",     "color": C.BYELLOW},
    "output":        {"icon": "◀", "label": "OUTPUT",   "color": C.BBLUE},
    "system_action": {"icon": "⬡", "label": "SYSTEM",   "color": C.DIM},
    "unknown":       {"icon": "·", "label": "STEP",     "color": C.DIM},
}

TOOL_CATEGORY_ICON: dict[str, str] = {
    "write":  "✎",
    "read":   "≡",
    "search": "⌕",
    "exec":   "❯",
}

AGENT_COLOR: dict[str, str] = {
    "gemini":  C.rgb(66, 133, 244),   # Google blue
    "claude":  C.rgb(205, 127, 50),   # Anthropic amber
    "codex":   C.rgb(16, 185, 129),   # OpenAI teal
    "copilot": C.rgb(139, 92, 246),   # GitHub purple
    "unknown": C.DIM,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _term_width() -> int:
    return shutil.get_terminal_size((100, 40)).columns


def _agent_color(agent: str) -> str:
    return AGENT_COLOR.get(str(agent).lower(), C.DIM)


def _step_meta(stype: str) -> dict[str, str]:
    return STEP_META.get(stype, STEP_META["unknown"])


def _fmt_time(ts: int) -> str:
    return _time.strftime("%a %b %d %H:%M:%S %Y", _time.localtime(ts))


def _fmt_rel_time(ts: int) -> str:
    delta = int(_time.time()) - ts
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def _truncate(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    return text[:width - 1] + "…"


def _hr(char: str = "─", color: str = C.DIM) -> str:
    return f"{color}{char * _term_width()}{C.RESET}"


def _pad_right(text: str, width: int, fill: str = " ") -> str:
    visible = _strip_ansi_len(text)
    return text + fill * max(0, width - visible)


def _strip_ansi_len(text: str) -> int:
    """Approximate visible length stripping ANSI codes."""
    import re
    return len(re.sub(r"\033\[[0-9;]*m", "", text))


def _badge(text: str, fg: str = C.BLACK, bg: str = C.bg_rgb(80, 80, 80)) -> str:
    return f"{bg}{fg} {text} {C.RESET}"


# ---------------------------------------------------------------------------
# Session list  (mach log)
# ---------------------------------------------------------------------------
def render_sessions_list(sessions: list[dict]) -> str:
    if not sessions:
        return (
            f"\n  {C.DIM}No sessions recorded yet.{C.RESET}\n"
            f"  {C.DIM}Run an agent with mach hooks installed to begin tracking.{C.RESET}\n"
        )

    out: list[str] = []
    width = _term_width()

    # Header bar
    out.append("")
    out.append(
        f"{C.BOLD}{C.BWHITE}  ⬡ MACH{C.RESET}"
        f"  {C.DIM}execution ledger{C.RESET}"
    )
    out.append(_hr())
    out.append("")

    for s in sessions:
        sid = str(s.get("id", ""))
        short_id = sid.replace("ses_", "")[:12]
        status = s.get("status") or ("active" if s.get("ended_at") is None else "ended")
        is_active = status == "active"
        agent = str(s.get("agent", "unknown"))
        branch = str(s.get("branch", "?"))
        step_count = s.get("step_count") or 0
        started = s.get("started_at") or 0
        pre_commit = str(s.get("pre_commit") or "")[:7]
        post_commit = str(s.get("post_commit") or "")[:7] if s.get("post_commit") else None

        acol = _agent_color(agent)
        status_badge = (
            f"{C.BGREEN}● active{C.RESET}" if is_active
            else f"{C.DIM}○ ended {C.RESET}"
        )

        commit_str = (
            f"{C.DIM}{pre_commit}{C.RESET} {C.DIM}→{C.RESET} {C.BGREEN}{post_commit}{C.RESET}"
            if post_commit
            else f"{C.DIM}{pre_commit}{C.RESET} {C.DIM}→ pending{C.RESET}"
        )

        rel_time = _fmt_rel_time(started) if started else "?"
        abs_time = _fmt_time(started) if started else "?"

        # Commit graph line (like git log --graph)
        graph_col = f"{C.BYELLOW}*{C.RESET}" if is_active else f"{C.DIM}*{C.RESET}"

        out.append(
            f"  {graph_col} {C.YELLOW}{short_id}{C.RESET}"
            f"  {acol}{C.BOLD}{agent}{C.RESET}"
            f"  {status_badge}"
            f"  {C.DIM}on{C.RESET} {C.CYAN}{branch}{C.RESET}"
        )
        out.append(
            f"  {C.DIM}│{C.RESET}  {C.DIM}commits:{C.RESET} {commit_str}"
            f"  {C.DIM}steps:{C.RESET} {C.BOLD}{step_count}{C.RESET}"
        )
        out.append(
            f"  {C.DIM}│{C.RESET}  {C.DIM}{abs_time}  ({rel_time}){C.RESET}"
        )
        out.append(f"  {C.DIM}│{C.RESET}")

    out.append(_hr())
    out.append(
        f"  {C.DIM}{len(sessions)} session(s)  ·  "
        f"mach log <id>  ·  mach log <id> --oneline  ·  mach log <id> -p{C.RESET}"
    )
    out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Session step log  (mach log <id>)
# ---------------------------------------------------------------------------
def render_session_steps(data: dict, oneline: bool = False, patch: bool = False) -> str:
    meta = data["meta"]
    steps = data["steps"]
    agent = str(meta.get("agent", "unknown"))
    session_id = str(meta.get("id", ""))
    branch = str(meta.get("branch", "?"))
    status = str(meta.get("status", "ended"))
    pre_commit = str(meta.get("pre_commit") or "")[:7]
    post_commit = str(meta.get("post_commit") or "")[:7] if meta.get("post_commit") else None
    acol = _agent_color(agent)

    out: list[str] = []

    if not oneline:
        # Session header
        out.append("")
        out.append(_hr("━"))
        commit_str = (
            f"{pre_commit} → {post_commit}" if post_commit
            else f"{pre_commit} → (pending)"
        )
        status_str = f"{C.BGREEN}active{C.RESET}" if status == "active" else f"{C.DIM}ended{C.RESET}"
        out.append(
            f"  {C.BOLD}{C.BWHITE}session{C.RESET}"
            f"  {C.YELLOW}{session_id.replace('ses_', '')[:16]}{C.RESET}"
            f"  {C.DIM}|{C.RESET}"
            f"  {acol}{C.BOLD}{agent}{C.RESET}"
            f"  {C.DIM}on{C.RESET} {C.CYAN}{branch}{C.RESET}"
            f"  {C.DIM}|{C.RESET}"
            f"  {status_str}"
        )
        out.append(
            f"  {C.DIM}commits: {commit_str}{C.RESET}"
        )
        out.append(_hr("━"))
        out.append("")

    coalesced = _coalesce_steps(steps)

    if not coalesced:
        out.append(f"  {C.DIM}No steps recorded in this session.{C.RESET}\n")
        return "\n".join(out)

    for c in reversed(coalesced):
        stype = c["type"]
        m = _step_meta(stype)
        icon = m["icon"]
        label = m["label"]
        color = m["color"]
        sid_short = c["id"].replace("step_", "")[:7]
        ts = c.get("ts", 0)

        if oneline:
            content_preview = _build_content_preview(c)
            out.append(
                f"{C.DIM}{sid_short}{C.RESET}"
                f" {color}{icon} {label:<6}{C.RESET}"
                f" {content_preview}"
            )
        else:
            ts_str = _fmt_time(ts)
            rel = _fmt_rel_time(ts) if ts else ""
            agent_display = "user" if stype == "input" else agent

            # Step header line (git commit style)
            out.append(
                f"{C.YELLOW}step {sid_short}{C.RESET}"
                f"  {color}{C.BOLD}{icon} {label}{C.RESET}"
                f"  {acol}{agent_display}{C.RESET}"
            )
            out.append(f"{C.DIM}Date:  {ts_str}  ({rel}){C.RESET}")
            out.append("")

            _render_step_body(out, c, patch=patch)
            out.append("")

    if not oneline:
        out.append(_hr())
        out.append(
            f"  {C.DIM}Showing {len(coalesced)} step group(s)  ·  "
            f"--oneline  ·  --patch / -p  ·  --content{C.RESET}"
        )
        out.append("")

    return "\n".join(out)


# ---------------------------------------------------------------------------
# Full content transcript  (mach log <id> --content)
# ---------------------------------------------------------------------------
def render_session_details(data: dict, patch: bool = False) -> str:
    meta = data["meta"]
    steps = data["steps"]
    agent = str(meta.get("agent", "unknown"))
    session_id = str(meta.get("id", ""))
    acol = _agent_color(agent)

    out: list[str] = []
    out.append("")
    out.append(_hr("━"))
    out.append(
        f"  {C.BOLD}{C.BWHITE}⬡ mach transcript{C.RESET}"
        f"  {C.YELLOW}{session_id.replace('ses_', '')[:20]}{C.RESET}"
        f"  {acol}{C.BOLD}{agent}{C.RESET}"
    )
    out.append(_hr("━"))
    out.append("")

    coalesced = _coalesce_steps(steps)

    for c in coalesced:
        stype = c["type"]
        m = _step_meta(stype)
        icon = m["icon"]
        label = m["label"]
        color = m["color"]

        out.append(
            f"{color}{C.BOLD}{icon} {label}{C.RESET}"
        )
        out.append(_hr("─"))

        _render_step_body(out, c, full_content=True, patch=patch, indent="  ")
        out.append("")

    out.append(_hr())
    out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _coalesce_steps(steps: list[dict]) -> list[dict]:
    """Merge consecutive same-type steps; collapse repeated same-name tool calls."""
    coalesced: list[dict] = []
    for step in steps:
        stype = step.get("type", "unknown")
        content = step.get("content", "")
        tool = step.get("tool")

        if tool:
            tool_name = tool.get("name")
            if (coalesced
                    and coalesced[-1]["type"] == "tool"
                    and coalesced[-1].get("name") == tool_name):
                coalesced[-1]["id"] = step["id"]
                coalesced[-1]["ts"] = step["ts"]
                coalesced[-1]["count"] = coalesced[-1].get("count", 1) + 1
            else:
                coalesced.append({
                    "id": step["id"],
                    "ts": step.get("ts", 0),
                    "type": "tool",
                    "name": tool_name,
                    "category": tool.get("category", "exec"),
                    "content": tool.get("content", ""),
                    "file_changes": step.get("file_changes"),
                    "count": 1,
                })
            continue

        if coalesced and coalesced[-1]["type"] == stype and stype != "tool":
            coalesced[-1]["content"] = (coalesced[-1].get("content") or "") + (content or "")
            coalesced[-1]["id"] = step["id"]
            coalesced[-1]["ts"] = step.get("ts", 0)
        else:
            coalesced.append({
                "id": step["id"],
                "ts": step.get("ts", 0),
                "type": stype,
                "content": content or "",
            })

    return coalesced


def _build_content_preview(c: dict) -> str:
    stype = c["type"]
    if stype == "tool":
        count = c.get("count", 1)
        cat = c.get("category", "exec")
        cat_icon = TOOL_CATEGORY_ICON.get(cat, "·")
        count_str = f" ×{count}" if count > 1 else ""
        return f"{cat_icon} {c.get('name', 'unknown')}{count_str}"
    text = str(c.get("content") or "").strip()
    first = text.split("\n")[0]
    return _truncate(first, 72) if first else f"{C.DIM}(empty){C.RESET}"


def _render_step_body(
    out: list[str],
    c: dict,
    full_content: bool = False,
    patch: bool = False,
    indent: str = "    ",
) -> None:
    stype = c["type"]

    if stype == "tool":
        count = c.get("count", 1)
        cat = c.get("category", "exec")
        cat_icon = TOOL_CATEGORY_ICON.get(cat, "·")
        count_str = f"  {C.DIM}(×{count} calls){C.RESET}" if count > 1 else ""
        out.append(
            f"{indent}{C.BOLD}{cat_icon}  {c.get('name', 'unknown')}{C.RESET}{count_str}"
            f"  {C.DIM}[{cat}]{C.RESET}"
        )
        if full_content:
            tool_content = str(c.get("content") or "").strip()
            if tool_content and tool_content != "None":
                out.append(f"{indent}{C.DIM}args: {_truncate(tool_content, 120)}{C.RESET}")

        fc = c.get("file_changes")
        if fc and patch:
            _render_patch_hunks(out, fc, indent)
        elif fc and full_content:
            _render_file_changes_summary(out, fc, indent)
    else:
        text = str(c.get("content") or "").strip()
        if not text:
            out.append(f"{indent}{C.DIM}(no content stored){C.RESET}")
            return
        if full_content:
            for line in text.split("\n"):
                out.append(f"{indent}{line}")
        else:
            first_line = text.split("\n")[0]
            preview = _truncate(first_line, _term_width() - len(indent) - 4)
            if len(text) > len(first_line):
                preview += f"  {C.DIM}[+{text.count(chr(10))} lines]{C.RESET}"
            out.append(f"{indent}{preview}")


def render_session_diff(data: dict) -> str:
    meta = data["meta"]
    files = data["files"]
    agent = str(meta.get("agent", "unknown"))
    session_id = str(meta.get("id", ""))
    branch = str(meta.get("branch", "?"))
    files_changed = data["files_changed"]
    total_added = data["total_added"]
    total_removed = data["total_removed"]
    tool_calls = data["tool_calls"]
    tool_names = data.get("tool_names") or {}
    acol = _agent_color(agent)

    out: list[str] = []
    out.append("")
    out.append(_hr("━"))
    out.append(
        f"  {C.BOLD}{C.BWHITE}⬡ mach diff{C.RESET}"
        f"  {C.YELLOW}{session_id.replace('ses_', '')[:16]}{C.RESET}"
        f"  {acol}{C.BOLD}{agent}{C.RESET}"
        f"  {C.DIM}on{C.RESET} {C.CYAN}{branch}{C.RESET}"
    )
    out.append(_hr("━"))
    out.append("")

    if not files:
        out.append(f"  {C.DIM}No file changes recorded in this session.{C.RESET}")
        out.append("")
        out.append(_hr())
        out.append("")
        return "\n".join(out)

    summary_parts = [f"{C.BOLD}{files_changed}{C.RESET} file(s) changed"]
    if total_added:
        summary_parts.append(f"{C.BGREEN}+{total_added}{C.RESET} insertions(+)")
    if total_removed:
        summary_parts.append(f"{C.BRED}-{total_removed}{C.RESET} deletions(-)")
    out.append(f"  {'  '.join(summary_parts)}")
    if tool_calls:
        top_tools = sorted(tool_names.items(), key=lambda x: -x[1])[:5]
        tool_str = "  ".join(f"{C.DIM}{name}{C.RESET}×{count}" for name, count in top_tools)
        out.append(f"  {C.DIM}tool calls: {tool_str}{C.RESET}")
    out.append("")
    out.append(_hr("─"))
    out.append("")

    for f in files:
        fp = f["file_path"]
        added = f.get("lines_added", 0)
        removed = f.get("lines_removed", 0)
        is_new = f.get("is_new", False)
        tools = f.get("tool_names") or []
        fname = fp.split("/")[-1]
        dirname = "/".join(fp.split("/")[:-1])

        stats = ""
        if added:
            stats += f" {C.BGREEN}+{added}{C.RESET}"
        if removed:
            stats += f" {C.BRED}-{removed}{C.RESET}"

        if is_new:
            badge = f" {C.BGREEN}(new){C.RESET}"
        elif f.get("action") == "delete":
            badge = f" {C.BRED}(deleted){C.RESET}"
        else:
            badge = ""

        tool_annotation = ""
        if tools:
            tool_annotation = f"  {C.DIM}({', '.join(tools)}){C.RESET}"

        dir_prefix = f"{C.DIM}{dirname}/{C.RESET}" if dirname else ""
        out.append(
            f"  {C.BOLD}{dir_prefix}{fname}{C.RESET}{badge}{stats}{tool_annotation}"
        )

        hunks = f.get("hunks") or []
        if hunks:
            for h in hunks[:8]:
                from_line = h.get("from", 0)
                to_line = h.get("to", 0)
                out.append(
                    f"    {C.DIM}@@ -{from_line} +{to_line} @@{C.RESET}"
                )
            if len(hunks) > 8:
                out.append(f"    {C.DIM}... {len(hunks) - 8} more hunk(s){C.RESET}")

    out.append("")
    out.append(_hr())
    out.append(
        f"  {C.DIM}{files_changed} file(s)  ·  +{total_added} -{total_removed}  ·  mach diff <id> --json{C.RESET}"
    )
    out.append("")
    return "\n".join(out)


def _render_file_changes_summary(out: list[str], fc: list[dict], indent: str) -> None:
    out.append(f"{indent}{C.DIM}file changes:{C.RESET}")
    for change in fc:
        action = change.get("action", "write")
        fp = change.get("file_path", "unknown")
        fname = fp.split("/")[-1]
        added = change.get("lines_added", 0) or 0
        removed = change.get("lines_removed", 0) or 0
        action_color = {
            "write": C.BGREEN,
            "read": C.BCYAN,
            "delete": C.BRED,
        }.get(action, C.DIM)
        stats = ""
        if added or removed:
            stats = f"  {C.BGREEN}+{added}{C.RESET} {C.BRED}-{removed}{C.RESET}"
        out.append(
            f"{indent}  {action_color}{action:<6}{C.RESET}"
            f"  {C.BOLD}{fname}{C.RESET}"
            f"  {C.DIM}{fp}{C.RESET}"
            f"{stats}"
        )


def _render_patch_hunks(out: list[str], fc: list[dict], indent: str) -> None:
    for change in fc:
        fp = change.get("file_path", "unknown")
        fname = fp.split("/")[-1]
        hunks = change.get("hunks") or []
        out.append(f"{indent}{C.DIM}diff --mach a/{fname} b/{fname}{C.RESET}")
        out.append(f"{indent}{C.DIM}--- a/{fname}{C.RESET}")
        out.append(f"{indent}{C.DIM}+++ b/{fname}{C.RESET}")
        for h in hunks:
            start = h.get("from", 0)
            end = h.get("to", 0)
            count = max(1, end - start + 1)
            out.append(
                f"{indent}{C.CYAN}@@ -{start},{count} +{start},{count} @@{C.RESET}"
            )
        if not hunks:
            added = change.get("lines_added", 0) or 0
            removed = change.get("lines_removed", 0) or 0
            out.append(
                f"{indent}{C.DIM}  +{added} lines  -{removed} lines{C.RESET}"
            )
