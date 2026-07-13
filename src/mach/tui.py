"""
mach.tui — Interactive execution ledger dashboard.

Dense, high-contrast layout: sessions rail | timeline | live preview.
"""
from __future__ import annotations

import os
import re
import time as _time
from collections import Counter
from typing import Any

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Footer, Input, ListItem, ListView, Static

from mach.session import SessionStore

# ── helpers ──────────────────────────────────────────────────────────────────

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\\033\[[0-9;]*[A-Za-z]")


def _strip(text: str) -> str:
    return _ANSI_RE.sub("", str(text))


def _rel(ts: int) -> str:
    if not ts:
        return "—"
    d = max(0, int(_time.time()) - ts)
    if d < 60:
        return f"{d}s"
    if d < 3600:
        return f"{d // 60}m"
    if d < 86400:
        return f"{d // 3600}h"
    return f"{d // 86400}d"


def _abs_ts(ts: int) -> str:
    if not ts:
        return ""
    return _time.strftime("%b %d %H:%M", _time.localtime(ts))


def _coalesce(steps: list[dict]) -> list[dict]:
    out: list[dict] = []
    for step in steps:
        stype = step.get("type", "unknown")
        tool = step.get("tool")
        content = step.get("content", "")
        if tool:
            out.append(
                dict(
                    id=step["id"],
                    ts=step.get("ts", 0),
                    type="tool",
                    name=tool.get("name"),
                    category=tool.get("category", "exec"),
                    content=_strip(tool.get("content") or ""),
                    file_changes=step.get("file_changes"),
                    risk_flags=step.get("risk_flags"),
                    risk_level=step.get("risk_level"),
                    count=1,
                )
            )
            continue
        if stype == "reasoning" and out and out[-1]["type"] == "reasoning":
            out[-1]["content"] = (out[-1].get("content") or "") + (content or "")
            out[-1].update(id=step["id"], ts=step.get("ts", 0))
        else:
            out.append(
                dict(
                    id=step["id"],
                    ts=step.get("ts", 0),
                    type=stype,
                    content=_strip(content or ""),
                    file_changes=step.get("file_changes"),
                    risk_flags=step.get("risk_flags"),
                    risk_level=step.get("risk_level"),
                )
            )
    return out


# Step type → glyph + rich style
STEP_ICON = {
    "input": ("▶", "bold #34d399"),
    "reasoning": ("◆", "bold #c084fc"),
    "tool": ("⚙", "bold #fbbf24"),
    "output": ("◀", "bold #38bdf8"),
    "system_action": ("◇", "dim #94a3b8"),
}

TOOL_CAT_ICON = {"write": "✎", "read": "≡", "search": "⌕", "exec": "❯"}

# Agent brand colors
AGENT_COLOR = {
    "claude": "#f59e0b",
    "gemini": "#60a5fa",
    "codex": "#34d399",
    "copilot": "#a78bfa",
    "cursor": "#38bdf8",
    "workspace-observer": "#94a3b8",
    "workspace_observer": "#94a3b8",
}


def _agent_style(agent: str) -> str:
    return AGENT_COLOR.get(str(agent).lower(), "#e2e8f0")


def _short_id(value: str, prefix: str, size: int = 8) -> str:
    raw = str(value or "")
    if raw.startswith(prefix):
        raw = raw[len(prefix) :]
    return raw[:size]


def _short_commit(value: str | None) -> str:
    return (value or "—")[:7]


def _count_file_changes(step: dict[str, Any]) -> int:
    return len(step.get("file_changes") or [])


def _session_status(session: dict[str, Any]) -> str:
    return session.get("status") or ("active" if not session.get("ended_at") else "ended")


def _preview_text(step: dict[str, Any], width: int = 100) -> str:
    if step.get("type") == "tool":
        bits = [step.get("name", "?"), str(step.get("content") or "").strip().replace("\n", " ")]
        value = "  ".join(b for b in bits if b)
    else:
        value = str(step.get("content") or "").strip().replace("\n", " ")
    value = _strip(value)
    if not value:
        return "(no content stored)"
    return value if len(value) <= width else value[: width - 1] + "…"


# Shared dark theme tokens used across screens
_APP_CSS = """
/* ── palette ─────────────────────────────────────────── */
$bg: #0b0f14;
$surface: #111827;
$panel: #0f172a;
$border: #1e293b;
$border-hi: #334155;
$text: #e2e8f0;
$muted: #64748b;
$accent: #22d3ee;
$accent-dim: #0891b2;
$ok: #34d399;
$warn: #fbbf24;
$danger: #f87171;
$purple: #c084fc;

Screen {
    background: $bg;
    color: $text;
}

/* ── top bar ─────────────────────────────────────────── */
#topbar {
    dock: top;
    height: 3;
    background: $panel;
    border-bottom: tall $border-hi;
    padding: 0 2;
    layout: horizontal;
}
#brand {
    width: 1fr;
    height: 3;
    content-align: left middle;
}
#stats-bar {
    width: auto;
    height: 3;
    content-align: right middle;
}

/* ── main split ──────────────────────────────────────── */
#main {
    height: 1fr;
}
#rail {
    width: 36;
    min-width: 30;
    max-width: 42;
    height: 1fr;
    background: $surface;
    border-right: tall $border-hi;
}
#workspace {
    width: 1fr;
    height: 1fr;
    background: $bg;
}

.section-head {
    height: 1;
    background: $panel;
    color: $muted;
    padding: 0 1;
    text-style: bold;
    border-bottom: solid $border;
}

/* sessions */
ListView {
    height: 1fr;
    background: transparent;
    padding: 0;
    border: none;
    scrollbar-background: $surface;
    scrollbar-color: $border-hi;
}
ListView:focus {
    border: none;
}
ListItem {
    height: 5;
    padding: 0 1;
    background: transparent;
    border-bottom: solid $border;
}
ListItem.--highlight {
    background: #164e63 40%;
    border-left: outer $accent;
}
ListItem:hover {
    background: #1e293b 60%;
}

#session-detail {
    height: 9;
    background: $panel;
    border-top: solid $border-hi;
    padding: 1 1;
}

/* timeline */
#timeline-head {
    height: 1;
    background: $panel;
    border-bottom: solid $border;
    padding: 0 1;
}
#chips {
    height: 1;
    background: $surface;
    padding: 0 1;
    border-bottom: solid $border;
}
#step-search-input {
    height: 1;
    background: $surface;
    border: none;
    padding: 0 1;
    color: $text;
}
#step-search-input:focus {
    background: #164e63 30%;
}
#step-search-input > .input--placeholder {
    color: $muted;
}

DataTable {
    height: 1fr;
    background: transparent;
    padding: 0 0;
    scrollbar-background: $bg;
    scrollbar-color: $border-hi;
}
DataTable > .datatable--header {
    background: $panel;
    color: $muted;
    text-style: bold;
}
DataTable > .datatable--cursor {
    background: #164e63 55%;
}
DataTable > .datatable--hover {
    background: #1e293b 50%;
}
DataTable > .datatable--even-row {
    background: transparent;
}
DataTable > .datatable--odd-row {
    background: #0f172a 40%;
}

#preview {
    height: 6;
    background: $panel;
    border-top: solid $border-hi;
    padding: 0 1;
}

Footer {
    background: $panel;
    border-top: solid $border-hi;
    color: $muted;
    height: 1;
}
Footer > .footer--key {
    background: $accent-dim;
    color: $bg;
    text-style: bold;
}
Footer > .footer--description {
    color: $muted;
}

/* modal */
StepDetail {
    align: center middle;
    background: #000000 65%;
}
#modal-shell {
    width: 88%;
    height: 84%;
    background: $surface;
    border: tall $accent-dim;
    padding: 0;
}
#modal-top {
    height: auto;
    background: $panel;
    border-bottom: solid $border-hi;
    padding: 1 2;
}
#modal-scroll {
    height: 1fr;
    padding: 1 2;
    scrollbar-color: $border-hi;
}
#modal-bottom {
    height: 1;
    background: $panel;
    border-top: solid $border;
    padding: 0 2;
    content-align: left middle;
}

/* diff screen */
DiffScreen {
    background: $bg;
}
#diff-top {
    height: 3;
    background: $panel;
    border-bottom: tall $border-hi;
    padding: 0 2;
    content-align: left middle;
}
#diff-body {
    height: 1fr;
}
#diff-left {
    width: 40%;
    height: 1fr;
    background: $surface;
    border-right: tall $border-hi;
}
#diff-right {
    width: 1fr;
    height: 1fr;
    padding: 1 2;
    background: $bg;
}
#diff-bottom {
    height: 1;
    background: $panel;
    border-top: solid $border;
    padding: 0 2;
}
"""


# ══════════════════════════════════════════════════════════
#  Step Detail Modal
# ══════════════════════════════════════════════════════════


class StepDetail(ModalScreen[None]):
    BINDINGS = [Binding("escape,q", "dismiss", "Close", show=True)]

    def __init__(self, step: dict, agent: str) -> None:
        super().__init__()
        self.step = step
        self.agent = agent

    def compose(self) -> ComposeResult:
        s = self.step
        stype = s.get("type", "unknown")
        icon, ic = STEP_ICON.get(stype, ("·", "dim"))
        ts = s.get("ts", 0)
        acol = _agent_style(self.agent)

        with Vertical(id="modal-shell"):
            with Container(id="modal-top"):
                h = Text()
                h.append(f"{icon} ", style=ic)
                h.append(f"{stype.upper()}", style=ic)
                h.append("   ", style="dim")
                h.append(_short_id(str(s.get("id", "")), "step_", 14), style="#60a5fa")
                h.append("  ·  ", style="dim")
                h.append(self.agent, style=f"bold {acol}")
                if ts:
                    h.append(f"  ·  {_abs_ts(ts)}", style="#64748b")
                    h.append(f"  ({_rel(ts)} ago)", style="#475569")
                yield Static(h)

                if stype == "tool":
                    cat = s.get("category", "exec")
                    ci = TOOL_CAT_ICON.get(cat, "·")
                    count = s.get("count", 1)
                    t2 = Text()
                    t2.append(f"{ci} ", style="#fbbf24")
                    t2.append(str(s.get("name", "?")), style="bold #fbbf24")
                    t2.append(f"  {cat}", style="#64748b")
                    if count > 1:
                        t2.append(f"  ×{count}", style="#fbbf24")
                    risk = s.get("risk_level") or "none"
                    if risk and risk != "none":
                        t2.append(f"  ·  risk:{risk}", style="#f87171")
                    yield Static(t2)

            with VerticalScroll(id="modal-scroll"):
                content = _strip(s.get("content") or "").strip()
                if content:
                    for line in content.splitlines():
                        yield Static(Text(line, style="#e2e8f0"))
                else:
                    yield Static(Text("(no content stored)", style="italic #64748b"))

                fc = s.get("file_changes") or []
                if fc:
                    yield Static(Text(""))
                    sep = Text()
                    sep.append("── ", style="#334155")
                    sep.append("FILE CHANGES", style="bold #22d3ee")
                    sep.append(f"  {len(fc)}", style="#64748b")
                    yield Static(sep)
                    for ch in fc:
                        action = ch.get("action", "write")
                        fp = ch.get("file_path", "?")
                        added = ch.get("lines_added", 0) or 0
                        removed = ch.get("lines_removed", 0) or 0
                        astyle = {"write": "#34d399", "read": "#38bdf8", "delete": "#f87171"}.get(
                            action, "#64748b"
                        )
                        line = Text()
                        line.append(f"{action.upper():<6} ", style=f"bold {astyle}")
                        line.append(str(fp), style="#e2e8f0")
                        if added or removed:
                            line.append(f"  +{added}", style="#34d399")
                            line.append(f" -{removed}", style="#f87171")
                        yield Static(line)
                        for h in ch.get("hunks", []):
                            hs, he = h.get("from", 0), h.get("to", 0)
                            hl = Text()
                            hl.append(
                                f"       @@ -{hs},{max(1, he - hs + 1)} +{hs},{max(1, he - hs + 1)} @@",
                                style="#22d3ee",
                            )
                            yield Static(hl)

                flags = s.get("risk_flags") or []
                if flags:
                    yield Static(Text(""))
                    sep = Text()
                    sep.append("── ", style="#334155")
                    sep.append("RISK FLAGS", style="bold #f87171")
                    yield Static(sep)
                    for flag in flags:
                        if not isinstance(flag, dict):
                            continue
                        rl = Text()
                        rl.append(f"  {flag.get('severity', '?').upper():<8}", style="bold #f87171")
                        rl.append(str(flag.get("rule_id", "?")), style="#fbbf24")
                        if flag.get("explanation"):
                            rl.append(f"  {flag['explanation']}", style="#94a3b8")
                        yield Static(rl)

            foot = Text()
            foot.append(" esc ", style="bold #0b0f14 on #22d3ee")
            foot.append(" close", style="#64748b")
            yield Static(foot, id="modal-bottom")


# ══════════════════════════════════════════════════════════
#  Diff Screen
# ══════════════════════════════════════════════════════════


class DiffScreen(Screen):
    BINDINGS = [
        Binding("q,escape", "quit", "Close"),
        Binding("d", "quit", "Close"),
        Binding("r", "refresh", "Refresh"),
    ]

    def __init__(self, session_id: str, store: SessionStore) -> None:
        super().__init__()
        self.session_id = session_id
        self.store = store
        self.diff_data: dict = {}
        self.steps: list = []

    def compose(self) -> ComposeResult:
        self._load_diff_data()
        meta = self.diff_data.get("meta") or {}
        agent = str(meta.get("agent", "unknown"))
        sid = _short_id(str(meta.get("id", "")), "ses_")
        branch = str(meta.get("branch", "?"))
        acol = _agent_style(agent)

        header = Text()
        header.append(" ▸ ", style="bold #22d3ee")
        header.append("DIFF", style="bold #22d3ee")
        header.append(f"  {sid}", style="#60a5fa")
        header.append("  ·  ", style="#334155")
        header.append(agent, style=f"bold {acol}")
        header.append(f"  on {branch}", style="#22d3ee")
        header.append("  ·  ", style="#334155")
        header.append(f"{self.diff_data.get('files_changed', 0)} files", style="#e2e8f0")
        header.append(
            f"  +{self.diff_data.get('total_added', 0)}",
            style="#34d399",
        )
        header.append(
            f" -{self.diff_data.get('total_removed', 0)}",
            style="#f87171",
        )
        yield Static(header, id="diff-top")

        with Horizontal(id="diff-body"):
            with Vertical(id="diff-left"):
                yield Static(Text(" STEPS WITH FILE CHANGES", style="bold #64748b"), classes="section-head")
                yield DataTable(
                    id="diff-files-table",
                    cursor_type="row",
                    zebra_stripes=True,
                    show_cursor=True,
                )
            with VerticalScroll(id="diff-right"):
                yield Static(
                    Text("Select a step →", style="italic #64748b"),
                    id="diff-detail-content",
                )

        footer = Text()
        footer.append(" q/esc ", style="bold #0b0f14 on #22d3ee")
        footer.append(" close  ", style="#64748b")
        footer.append(" ↑↓ ", style="bold #0b0f14 on #334155")
        footer.append(" navigate", style="#64748b")
        yield Static(footer, id="diff-bottom")

    def on_mount(self) -> None:
        table = self.query_one("#diff-files-table", DataTable)
        table.add_columns("Step", "Tool", "Files", "+/−")
        self._populate_steps_table()

    def _load_diff_data(self) -> None:
        try:
            self.diff_data = self.store.session_diff(self.session_id)
        except Exception:
            self.diff_data = {
                "meta": {},
                "steps": [],
                "files": [],
                "files_changed": 0,
                "total_added": 0,
                "total_removed": 0,
                "tool_calls": 0,
                "tool_names": {},
            }

    def _populate_steps_table(self) -> None:
        table = self.query_one("#diff-files-table", DataTable)
        table.clear()
        self.steps = self.diff_data.get("steps") or []
        for step in self.steps:
            files = step.get("files") or []
            file_label = self._step_file_label(files)
            tool_or_type = step.get("tool_name") or step.get("step_type", "?")
            table.add_row(
                _short_id(str(step.get("step_id", "")), "step_", 10),
                str(tool_or_type),
                file_label,
                self._delta_label(step.get("lines_added", 0), step.get("lines_removed", 0)),
            )
        if self.steps:
            table.move_cursor(row=0)
            self._update_detail(0)

    def _detail_text(self, idx: int) -> Text:
        if not (0 <= idx < len(self.steps)):
            return Text("Select a step →", style="italic #64748b")
        step = self.steps[idx]
        files = step.get("files") or []

        detail = Text()
        step_id = str(step.get("step_id") or "")
        detail.append(_short_id(step_id, "step_", 16), style="bold #60a5fa")
        detail.append("  ")
        detail.append(str(step.get("step_type", "?")).upper(), style="bold #e2e8f0")
        if step.get("tool_name"):
            detail.append("  ")
            detail.append(step["tool_name"], style="#fbbf24")
        if step.get("ts"):
            detail.append("  ")
            detail.append(_abs_ts(int(step["ts"])), style="#64748b")
        detail.append("\n\n")

        detail.append("Files ", style="#64748b")
        detail.append(str(step.get("files_changed", len(files))), style="bold #e2e8f0")
        detail.append("   Lines ", style="#64748b")
        self._append_delta(detail, step.get("lines_added", 0), step.get("lines_removed", 0))
        detail.append("   Source ", style="#64748b")
        source = step.get("diff_source", "recorded")
        detail.append(source, style="#22d3ee" if source == "git" else "#e2e8f0")
        detail.append("\n")

        content = _strip(step.get("content") or "").strip()
        if content:
            detail.append("\n", style="")
            detail.append("CONTENT\n", style="bold #22d3ee")
            preview = content if len(content) <= 700 else content[:700].rstrip() + "\n…"
            for line in preview.splitlines():
                detail.append(f"  {line}\n", style="#94a3b8")

        if not files:
            detail.append("\nNo file details recorded for this step.", style="#64748b")
            return detail

        detail.append("\n", style="")
        detail.append("FILES\n", style="bold #22d3ee")
        for file_change in files:
            self._append_file_change(detail, file_change)
        return detail

    def _append_file_change(self, detail: Text, f: dict) -> None:
        action = f.get("action", "?")
        added = f.get("lines_added", 0)
        removed = f.get("lines_removed", 0)
        hunks = f.get("hunks", [])
        git_diff = f.get("git_diff") or ""
        diff_source = f.get("diff_source") or ("recorded" if hunks else "summary")

        detail.append("\n")
        detail.append(str(f.get("file_path", "?")), style="bold #e2e8f0")
        detail.append("  ")
        detail.append(str(action), style="#fbbf24")
        detail.append("  ")
        self._append_delta(detail, added, removed)
        detail.append("  ")
        detail.append(str(diff_source), style="#22d3ee" if diff_source == "git" else "#64748b")
        detail.append("\n")
        if hunks:
            detail.append("  Hunks\n", style="bold #64748b")
            for i, h in enumerate(hunks, 1):
                from_line = h.get("from", 0)
                to_line = h.get("to", 0)
                detail.append(f"    {i}. ", style="#475569")
                detail.append(f"@@ -{from_line} +{to_line} @@\n", style="#22d3ee")
        elif git_diff:
            detail.append("  Git diff\n", style="bold #22d3ee")
            self._append_patch(detail, git_diff)
        else:
            detail.append("  No hunk details recorded.\n", style="#64748b")

    def _append_delta(self, detail: Text, added: int, removed: int) -> None:
        detail.append(f"+{added} ", style="#34d399" if added else "#475569")
        detail.append(f"-{removed}", style="#f87171" if removed else "#475569")

    def _delta_label(self, added: int, removed: int) -> str:
        parts = []
        if added:
            parts.append(f"+{added}")
        if removed:
            parts.append(f"-{removed}")
        return " ".join(parts) or "·"

    def _step_file_label(self, files: list[dict]) -> str:
        if not files:
            return "·"
        if len(files) == 1:
            return str(files[0].get("file_path") or "?")
        return f"{len(files)} files"

    def _append_patch(self, detail: Text, patch: str, limit: int = 220) -> None:
        lines = patch.splitlines()
        for line in lines[:limit]:
            style = "#e2e8f0"
            if line.startswith("+++ ") or line.startswith("--- "):
                style = "#64748b"
            elif line.startswith("@@"):
                style = "#22d3ee"
            elif line.startswith("+"):
                style = "#34d399"
            elif line.startswith("-"):
                style = "#f87171"
            elif line.startswith("diff --git"):
                style = "bold #60a5fa"
            detail.append(line + "\n", style=style)
        if len(lines) > limit:
            detail.append(f"… {len(lines) - limit} more line(s)\n", style="#64748b")

    def action_quit(self) -> None:
        if self.app.__class__.__name__ == "DiffOnlyApp":
            self.app.exit()
            return
        self.app.pop_screen()

    def action_refresh(self) -> None:
        self._load_diff_data()
        self._populate_steps_table()

    @on(DataTable.RowHighlighted, "#diff-files-table")
    def on_file_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._update_detail(event.cursor_row)

    @on(DataTable.RowSelected, "#diff-files-table")
    def on_file_selected(self, event: DataTable.RowSelected) -> None:
        row = getattr(event, "cursor_row", None)
        if row is not None:
            self._update_detail(row)

    def _update_detail(self, row: int | None) -> None:
        if row is not None:
            self.query_one("#diff-detail-content", Static).update(self._detail_text(row))


# ══════════════════════════════════════════════════════════
#  Main App
# ══════════════════════════════════════════════════════════


class MachApp(App):
    TITLE = "mach"
    SUB_TITLE = "execution ledger"
    CSS = _APP_CSS

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("tab,right", "focus_steps", "Steps"),
        Binding("escape,left", "focus_sessions", "Sessions"),
        Binding("slash", "focus_search", "Search"),
        Binding("r", "refresh", "Refresh"),
        Binding("d", "diff", "Diff"),
        Binding("enter", "open_step", "Open", show=False),
    ]

    def __init__(
        self,
        store: SessionStore,
        initial_diff_session_id: str | None = None,
    ) -> None:
        super().__init__()
        self.store = store
        self.sessions: list[dict] = []
        self.steps: list[dict] = []
        self.visible_steps: list[dict] = []
        self.agent = "unknown"
        self.selected_session_id: str | None = None
        self._initial_diff_session_id = initial_diff_session_id

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Static(id="brand")
            yield Static(id="stats-bar")

        with Horizontal(id="main"):
            with Vertical(id="rail"):
                yield Static(Text(" SESSIONS", style="bold #64748b"), classes="section-head")
                yield ListView(id="session-list")
                yield Static(id="session-detail")

            with Vertical(id="workspace"):
                yield Static(id="timeline-head")
                yield Static(id="chips")
                yield Input(placeholder="  filter steps…  /", id="step-search-input")
                yield DataTable(
                    id="steps-table",
                    cursor_type="row",
                    zebra_stripes=True,
                    show_cursor=True,
                )
                yield Static(id="preview")

        yield Footer()

    # ── render helpers ───────────────────────────────────────────────────────

    def _brand_text(self) -> Text:
        repo = self.store.paths.repo_root.name or "."
        t = Text()
        t.append(" ⬡ ", style="bold #22d3ee")
        t.append("MACH", style="bold #f8fafc")
        t.append("  ledger", style="#64748b")
        t.append("  ·  ", style="#334155")
        t.append(repo, style="bold #e2e8f0")
        return t

    def _stats_text(self) -> Text:
        active = sum(1 for s in self.sessions if _session_status(s) == "active")
        agents = len({str(s.get("agent", "?")) for s in self.sessions}) if self.sessions else 0
        t = Text()
        t.append(" ● ", style="bold #34d399" if active else "#475569")
        t.append(f"{active} live", style="#34d399" if active else "#64748b")
        t.append("  ", style="")
        t.append(f"{len(self.sessions)} sessions", style="#38bdf8")
        t.append("  ", style="")
        t.append(f"{agents} agents", style="#fbbf24")
        t.append("  ", style="")
        return t

    def _session_card(self, s: dict) -> ListItem:
        sid = _short_id(str(s.get("id", "")), "ses_", 8)
        agent = str(s.get("agent", "?"))
        branch = str(s.get("branch", "?"))
        status = _session_status(s)
        n_steps = s.get("step_count", 0) or 0
        started = s.get("started_at", 0) or 0
        is_active = status == "active"
        acol = _agent_style(agent)
        commit = _short_commit(s.get("post_commit") or s.get("pre_commit"))
        risk = s.get("risk_count", 0) or 0
        task = str(s.get("task_desc") or "").strip().replace("\n", " ")
        if len(task) > 34:
            task = task[:33] + "…"

        line1 = Text()
        line1.append("● " if is_active else "○ ", style="bold #34d399" if is_active else "#475569")
        line1.append(sid, style="bold #f8fafc")
        line1.append("  ", style="")
        line1.append(commit, style="#fbbf24")

        line2 = Text()
        line2.append(f" {agent} ", style=f"bold #0b0f14 on {acol}")
        line2.append("  ", style="")
        line2.append(branch, style="#22d3ee")

        line3 = Text()
        line3.append(f"{n_steps} steps", style="#94a3b8")
        line3.append(" · ", style="#334155")
        line3.append(f"{_rel(started)} ago", style="#64748b")
        if risk:
            line3.append(" · ", style="#334155")
            line3.append(f"⚠ {risk}", style="#f87171")
        if is_active:
            line3.append(" · ", style="#334155")
            line3.append("LIVE", style="bold #34d399")

        line4 = Text()
        if task:
            line4.append(task, style="#64748b")
        else:
            line4.append("no task description", style="#334155")

        content = Text.assemble(line1, "\n", line2, "\n", line3, "\n", line4)
        return ListItem(Static(content))

    def _session_detail_text(self, session: dict | None) -> Text:
        if not session:
            return Text("Select a session to inspect metadata.", style="#64748b")

        status = _session_status(session)
        agent = str(session.get("agent", "unknown"))
        acol = _agent_style(agent)
        t = Text()
        t.append("SELECTED\n", style="bold #64748b")
        t.append(f"{agent.upper()} ", style=f"bold {acol}")
        t.append(
            "● active\n" if status == "active" else "○ ended\n",
            style="bold #34d399" if status == "active" else "#64748b",
        )
        t.append("branch  ", style="#475569")
        t.append(f"{session.get('branch', '?')}\n", style="#22d3ee")
        t.append("commit  ", style="#475569")
        t.append(_short_commit(session.get("pre_commit")), style="#fbbf24")
        t.append(" → ", style="#334155")
        post = _short_commit(session.get("post_commit"))
        t.append(post if post != "—" else "pending", style="#34d399" if post != "—" else "#64748b")
        t.append("\n")
        t.append("risk    ", style="#475569")
        risk = session.get("risk_count", 0) or 0
        t.append(str(risk), style="#f87171" if risk else "#34d399")
        t.append("\n")
        task = str(session.get("task_desc") or "—").strip().replace("\n", " ")
        if len(task) > 80:
            task = task[:79] + "…"
        t.append("task    ", style="#475569")
        t.append(task, style="#e2e8f0")
        return t

    def _timeline_head(self, sid: str = "", count: int = 0) -> Text:
        t = Text()
        t.append(" TIMELINE", style="bold #64748b")
        if sid:
            t.append(f"  {sid}", style="#60a5fa")
        if count:
            t.append(f"  ·  {count} events", style="#94a3b8")
        return t

    def _chips_text(self, session: dict | None, steps: list[dict]) -> Text:
        if not session:
            return Text("  —", style="#475569")
        counts = Counter(step.get("type", "unknown") for step in steps)
        tools = sum(step.get("count", 1) for step in steps if step.get("type") == "tool")
        files = sum(_count_file_changes(step) for step in steps)
        risks = sum(len(step.get("risk_flags") or []) for step in steps)
        t = Text()
        t.append("  ", style="")
        t.append(f" {counts.get('input', 0)} in ", style="bold #0b0f14 on #34d399")
        t.append(" ", style="")
        t.append(f" {counts.get('reasoning', 0)} think ", style="bold #0b0f14 on #c084fc")
        t.append(" ", style="")
        t.append(f" {tools} tools ", style="bold #0b0f14 on #fbbf24")
        t.append(" ", style="")
        t.append(f" {counts.get('output', 0)} out ", style="bold #0b0f14 on #38bdf8")
        t.append(" ", style="")
        t.append(f" {files} files ", style="bold #0b0f14 on #64748b")
        if risks:
            t.append(" ", style="")
            t.append(f" {risks} risk ", style="bold #0b0f14 on #f87171")
        return t

    def _preview_block(self, step: dict | None) -> Text:
        if not step:
            return Text(
                "\n  Navigate the timeline · Enter opens full detail · / filters\n",
                style="#64748b",
            )
        stype = step.get("type", "unknown")
        icon, ic = STEP_ICON.get(stype, ("·", "dim"))
        t = Text()
        t.append("\n  ", style="")
        t.append(f"{icon} ", style=ic)
        t.append(f"{stype.upper()}  ", style=ic)
        t.append(_preview_text(step, 90), style="#e2e8f0")
        t.append("\n  ", style="")
        if stype == "tool":
            t.append(f"[{step.get('category', 'exec')}]", style="#64748b")
            files = _count_file_changes(step)
            if files:
                t.append(f"  {files} file change(s)", style="#38bdf8")
            risk = step.get("risk_level") or "none"
            if risk != "none":
                t.append(f"  risk:{risk}", style="#f87171")
        t.append("   ⏎ detail", style="#475569")
        return t

    # ── lifecycle ────────────────────────────────────────────────────────────

    def on_mount(self) -> None:
        tt = self.query_one("#steps-table", DataTable)
        tt.add_columns(" ", "Type", "Summary", "Files", "When")
        self._load_sessions()
        self.query_one("#session-list", ListView).focus()
        if self._initial_diff_session_id:
            self.set_timer(0.05, self._open_initial_diff)

    def _open_initial_diff(self) -> None:
        sid = self._initial_diff_session_id
        if not sid:
            return
        for i, s in enumerate(self.sessions):
            if s.get("id") == sid:
                lv = self.query_one("#session-list", ListView)
                lv.index = i
                self._load_steps(s)
                break
        self.push_screen(DiffScreen(sid, self.store))

    def _load_sessions(self) -> None:
        self.sessions = self.store.list_sessions()
        lv = self.query_one("#session-list", ListView)
        lv.clear()
        for s in self.sessions:
            lv.append(self._session_card(s))

        self.query_one("#brand", Static).update(self._brand_text())
        self.query_one("#stats-bar", Static).update(self._stats_text())

        if self.sessions:
            self.selected_session_id = self.sessions[0].get("id")
            self._load_steps(self.sessions[0])
        else:
            self.steps = []
            self.visible_steps = []
            self.selected_session_id = None
            self.query_one("#timeline-head", Static).update(self._timeline_head())
            self.query_one("#chips", Static).update(self._chips_text(None, []))
            self.query_one("#session-detail", Static).update(self._session_detail_text(None))
            self.query_one("#preview", Static).update(self._preview_block(None))
            self.query_one("#steps-table", DataTable).clear()

    def _load_steps(self, session: dict) -> None:
        sid = session.get("id", "")
        self.agent = str(session.get("agent", "unknown"))
        self.selected_session_id = sid
        meta = session
        try:
            data = self.store.show_session(sid)
            self.steps = _coalesce(data["steps"])
            meta = data.get("meta") or session
        except Exception:
            self.steps = []

        short_id = _short_id(sid, "ses_")
        self.query_one("#timeline-head", Static).update(
            self._timeline_head(sid=short_id, count=len(self.steps))
        )
        self.query_one("#session-detail", Static).update(self._session_detail_text(meta))
        self.query_one("#chips", Static).update(self._chips_text(meta, self.steps))

        search_input = self.query_one("#step-search-input", Input)
        search_input.value = ""
        self._populate_table()

    def _populate_table(self, query: str = "") -> None:
        tt = self.query_one("#steps-table", DataTable)
        tt.clear()

        q = query.lower().strip()
        visible: list[dict] = []
        for step in self.steps:
            stype = step.get("type", "unknown")
            content = str(step.get("content") or "").lower()
            name = str(step.get("name") or "").lower()
            if q and q not in content and q not in name and q not in stype:
                continue
            visible.append(step)

        self.visible_steps = visible
        self.query_one("#timeline-head", Static).update(
            self._timeline_head(
                sid=_short_id(self.selected_session_id or "", "ses_"),
                count=len(visible),
            )
        )

        for step in visible:
            stype = step.get("type", "unknown")
            icon, ic = STEP_ICON.get(stype, ("·", "dim"))
            ts = step.get("ts", 0)

            icon_cell = Text(icon, style=ic)
            label_cell = Text(stype[:6].upper(), style=ic)

            if stype == "tool":
                count = step.get("count", 1)
                cat = step.get("category", "exec")
                ci = TOOL_CAT_ICON.get(cat, "·")
                detail = Text()
                detail.append(f"{ci} ", style="#fbbf24")
                detail.append(str(step.get("name", "?")), style="bold #fbbf24")
                tool_content = _strip(step.get("content") or "").strip().replace("\n", " ")
                if tool_content:
                    detail.append("  ", style="")
                    clipped = tool_content[:70] + "…" if len(tool_content) > 70 else tool_content
                    detail.append(clipped, style="#64748b")
                if count > 1:
                    detail.append(f"  ×{count}", style="#fbbf24")
            else:
                raw = _strip(step.get("content") or "").strip().replace("\n", " ")
                if not raw:
                    detail = Text("(empty)", style="italic #475569")
                else:
                    detail = Text(
                        raw[:110] + "…" if len(raw) > 110 else raw,
                        style="#e2e8f0",
                    )

            n_files = _count_file_changes(step)
            files_cell = Text(
                str(n_files) if n_files else "·",
                style="#38bdf8" if n_files else "#334155",
            )
            ts_cell = Text(_rel(ts) if ts else "", style="#64748b")
            tt.add_row(icon_cell, label_cell, detail, files_cell, ts_cell)

        self.query_one("#preview", Static).update(
            self._preview_block(visible[0] if visible else None)
        )

    # ── events ───────────────────────────────────────────────────────────────

    @on(Input.Changed, "#step-search-input")
    def on_search_changed(self, event: Input.Changed) -> None:
        self._populate_table(event.value)

    @on(ListView.Highlighted, "#session-list")
    def on_session_highlighted(self, event: ListView.Highlighted) -> None:
        if event.item is None:
            return
        idx = event.list_view.index
        if idx is not None and 0 <= idx < len(self.sessions):
            self._load_steps(self.sessions[idx])

    @on(ListView.Selected, "#session-list")
    def on_session_selected(self, event: ListView.Selected) -> None:
        self.query_one("#steps-table", DataTable).focus()

    @on(DataTable.RowHighlighted, "#steps-table")
    def on_step_highlighted(self, event: DataTable.RowHighlighted) -> None:
        row = event.cursor_row
        if row is not None and 0 <= row < len(self.visible_steps):
            self.query_one("#preview", Static).update(self._preview_block(self.visible_steps[row]))

    @on(DataTable.RowSelected, "#steps-table")
    def on_step_selected(self, event: DataTable.RowSelected) -> None:
        row = event.cursor_row
        if row is not None and 0 <= row < len(self.visible_steps):
            self.push_screen(StepDetail(self.visible_steps[row], self.agent))

    def action_focus_steps(self) -> None:
        self.query_one("#steps-table", DataTable).focus()

    def action_focus_search(self) -> None:
        self.query_one("#step-search-input", Input).focus()

    def action_focus_sessions(self) -> None:
        self.query_one("#session-list", ListView).focus()

    def action_refresh(self) -> None:
        self._load_sessions()
        self.notify("Refreshed", severity="information", timeout=1.5)

    def action_diff(self) -> None:
        if self.selected_session_id:
            self.push_screen(DiffScreen(self.selected_session_id, self.store))

    def action_open_step(self) -> None:
        table = self.query_one("#steps-table", DataTable)
        if not table.has_focus:
            return
        row = table.cursor_row
        if row is not None and 0 <= row < len(self.visible_steps):
            self.push_screen(StepDetail(self.visible_steps[row], self.agent))


class DiffOnlyApp(App):
    """Standalone TUI for session diff."""

    TITLE = "mach diff"
    CSS = _APP_CSS
    BINDINGS = [Binding("q,escape", "quit", "Quit")]

    def __init__(self, store: SessionStore, session_id: str) -> None:
        super().__init__()
        self.store = store
        self.session_id = session_id

    def on_mount(self) -> None:
        self.push_screen(DiffScreen(self.session_id, self.store))


def run_tui(store: SessionStore) -> None:
    MachApp(store).run()
