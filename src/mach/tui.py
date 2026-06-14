"""
mach.tui — Premium interactive terminal dashboard.

Native terminal aesthetics.
Split-screen: sessions (left) | timeline (right). Enter → step detail modal.
"""
from __future__ import annotations

import os
import re
import time as _time
from collections import Counter
from typing import Any

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Footer, ListItem, ListView, Static, Input
from rich.text import Text

from mach.session import SessionStore


# ══════════════════════════════════════════════════════════
#  Constants & helpers
# ══════════════════════════════════════════════════════════

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\\033\[[0-9;]*[A-Za-z]")


def _strip(text: str) -> str:
    return _ANSI_RE.sub("", str(text))


def _rel(ts: int) -> str:
    if not ts:
        return "?"
    d = max(0, int(_time.time()) - ts)
    if d < 60:
        return f"{d}s ago"
    if d < 3600:
        return f"{d // 60}m ago"
    if d < 86400:
        return f"{d // 3600}h ago"
    return f"{d // 86400}d ago"


def _abs_ts(ts: int) -> str:
    if not ts:
        return ""
    return _time.strftime("%b %d  %H:%M", _time.localtime(ts))


def _coalesce(steps: list[dict]) -> list[dict]:
    out: list[dict] = []
    for step in steps:
        stype = step.get("type", "unknown")
        tool = step.get("tool")
        content = step.get("content", "")
        if tool:
            name = tool.get("name")
            out.append(dict(
                id=step["id"], ts=step.get("ts", 0), type="tool",
                name=name, category=tool.get("category", "exec"),
                content=_strip(tool.get("content") or ""),
                file_changes=step.get("file_changes"), count=1,
            ))
            continue
        if stype == "reasoning" and out and out[-1]["type"] == "reasoning":
            out[-1]["content"] = (out[-1].get("content") or "") + (content or "")
            out[-1].update(id=step["id"], ts=step.get("ts", 0))
        else:
            out.append(dict(id=step["id"], ts=step.get("ts", 0),
                            type=stype, content=_strip(content or "")))
    return out


# visual config using native terminal colors
STEP_ICON = {
    "input":         ("▸", "bold cyan"),
    "reasoning":     ("◆", "bold magenta"),
    "tool":          ("⬡", "bold yellow"),
    "output":        ("◀", "bold green"),
    "system_action": ("·", "dim"),
}
TOOL_CAT_ICON = {"write": "✎", "read": "≡", "search": "⌕", "exec": "❯"}
AGENT_COLOR = {
    "gemini":  "cyan",
    "claude":  "yellow",
    "codex":   "green",
    "copilot": "magenta",
    "cursor":  "blue",
}

ACCENT_STYLES = {
    "blue": ("blue", "bold blue"),
    "cyan": ("cyan", "bold cyan"),
    "green": ("green", "bold green"),
    "yellow": ("yellow", "bold yellow"),
    "magenta": ("magenta", "bold magenta"),
}


def _short_id(value: str, prefix: str, size: int = 12) -> str:
    return value.replace(prefix, "")[:size]


def _short_commit(value: str | None) -> str:
    return (value or "?")[:7]


def _count_file_changes(step: dict[str, Any]) -> int:
    return len(step.get("file_changes") or [])


def _session_status(session: dict[str, Any]) -> str:
    return session.get("status") or ("active" if not session.get("ended_at") else "ended")


def _accent_from_env() -> str:
    override = os.environ.get("MACH_TUI_ACCENT", "").strip().lower()
    if override in ACCENT_STYLES:
        return override

    term_program = os.environ.get("TERM_PROGRAM", "").lower()
    if "apple_terminal" in term_program:
        return "green"
    if "vscode" in term_program:
        return "blue"
    if "warp" in term_program:
        return "yellow"
    if "wezterm" in term_program:
        return "cyan"

    if os.environ.get("KITTY_WINDOW_ID"):
        return "magenta"
    if os.environ.get("WT_SESSION"):
        return "blue"
    if os.environ.get("ITERM_PROFILE"):
        return "cyan"
    return "cyan"


def _preview_text(step: dict[str, Any], width: int = 136) -> str:
    if step.get("type") == "tool":
        tool_bits = [
            step.get("name", "?"),
            str(step.get("content") or "").strip().replace("\n", " "),
        ]
        value = "  ".join(bit for bit in tool_bits if bit)
    else:
        value = str(step.get("content") or "").strip().replace("\n", " ")
    value = _strip(value)
    if not value:
        return "(content redacted)"
    return value if len(value) <= width else value[: width - 1] + "…"


# ══════════════════════════════════════════════════════════
#  Step Detail Modal
# ══════════════════════════════════════════════════════════

class StepDetail(ModalScreen[None]):
    BINDINGS = [Binding("escape,q", "dismiss", "Close")]

    DEFAULT_CSS = """
    StepDetail {
        align: center middle;
        background: $background 70%;
    }
    #modal-outer {
        width: 85%;
        height: 82%;
        background: $surface;
        border: round $primary;
        padding: 0;
    }
    #modal-header {
        height: auto;
        background: $panel;
        border-bottom: solid $primary;
        padding: 1 2;
    }
    #modal-body {
        height: 1fr;
        padding: 1 2;
    }
    #modal-footer-bar {
        height: 1;
        background: $panel;
        border-top: solid $primary;
        padding: 0 2;
        content-align: left middle;
    }
    """

    def __init__(self, step: dict, agent: str) -> None:
        super().__init__()
        self.step = step
        self.agent = agent

    def compose(self) -> ComposeResult:
        s = self.step
        stype = s.get("type", "unknown")
        icon, ic = STEP_ICON.get(stype, ("·", "dim"))
        label = stype.upper()
        ts = s.get("ts", 0)
        acol = AGENT_COLOR.get(self.agent.lower(), "white")

        with Vertical(id="modal-outer"):
            # ── Header ──
            with Container(id="modal-header"):
                h = Text()
                h.append(f"  {icon} ", style=ic)
                h.append(f"{label}", style=f"bold {ic.split()[-1] if ' ' in ic else ic}")
                h.append("   ", style="dim")
                h.append("step:", style="dim")
                h.append(f" {s.get('id', '?')}", style="blue")
                h.append("   agent:", style="dim")
                h.append(f" {self.agent}", style=f"bold {acol}")
                if ts:
                    h.append(f"   {_abs_ts(ts)}", style="dim")
                    h.append(f"  ({_rel(ts)})", style="dim")
                yield Static(h)

                if stype == "tool":
                    cat = s.get("category", "exec")
                    ci = TOOL_CAT_ICON.get(cat, "·")
                    count = s.get("count", 1)
                    t2 = Text()
                    t2.append(f"  {ci} ", style="yellow")
                    t2.append(s.get("name", "?"), style="bold yellow")
                    t2.append(f"  [{cat}]", style="dim")
                    if count > 1:
                        t2.append(f"  ×{count} calls", style="dim yellow")
                    yield Static(t2)

            # ── Body ──
            with VerticalScroll(id="modal-body"):
                content = _strip(s.get("content") or "").strip()
                if content:
                    yield Static(Text(content))
                else:
                    yield Static(Text("  (no content stored — privacy policy redacted)", style="dim italic"))

                fc = s.get("file_changes")
                if fc:
                    yield Static(Text(""))
                    sep = Text()
                    sep.append("  ── ", style="dim")
                    sep.append("File Changes", style="bold")
                    sep.append(f"  ({len(fc)} file{'s' if len(fc) != 1 else ''})", style="dim")
                    yield Static(sep)
                    yield Static(Text(""))
                    for ch in fc:
                        action = ch.get("action", "write")
                        fp = ch.get("file_path", "?")
                        added = ch.get("lines_added", 0) or 0
                        removed = ch.get("lines_removed", 0) or 0
                        astyle = {"write": "green", "read": "cyan",
                                  "delete": "red"}.get(action, "dim")
                        line = Text()
                        line.append(f"  {action.upper():<6}", style=f"bold {astyle}")
                        line.append(f"  {fp}", style="white")
                        if added or removed:
                            line.append(f"  +{added}", style="green")
                            line.append(f"  -{removed}", style="red")
                        yield Static(line)
                        for h in ch.get("hunks", []):
                            hs, he = h.get("from", 0), h.get("to", 0)
                            hl = Text()
                            hl.append(f"        @@ -{hs},{max(1,he-hs+1)} +{hs},{max(1,he-hs+1)} @@",
                                      style="cyan")
                            yield Static(hl)

            # ── Footer ──
            foot = Text()
            foot.append("  ESC ", style="bold yellow")
            foot.append("close", style="dim")
            yield Static(foot, id="modal-footer-bar")


# ══════════════════════════════════════════════════════════
#  Diff Screen
# ══════════════════════════════════════════════════════════

class DiffScreen(Screen):
    BINDINGS = [
        Binding("q,escape", "quit", "Close"),
        Binding("d", "quit", "Close"),
        Binding("r", "refresh", "Refresh"),
    ]

    DEFAULT_CSS = """
    DiffScreen {
        background: $background;
    }
    #diff-container {
        height: 1fr;
    }
    #files-pane {
        width: 42%;
        height: 1fr;
        border-right: tall $primary;
        background: transparent;
    }
    .pane-title {
        height: 2;
        padding: 0 2;
        border-bottom: solid $primary;
        background: $panel;
        content-align: left middle;
    }
    DataTable {
        height: 1fr;
        background: transparent;
        padding: 0 1;
    }
    #details-pane {
        width: 1fr;
        height: 1fr;
        padding: 0 2 1 2;
        background: transparent;
    }
    #diff-header {
        height: 3;
        background: $panel;
        border-bottom: solid $primary;
        padding: 0 2;
        content-align: left middle;
    }
    #diff-footer {
        height: 1;
        background: $panel;
        border-top: solid $primary;
        padding: 0 2;
        content-align: left middle;
    }
    """

    def __init__(self, session_id: str, store: SessionStore) -> None:
        super().__init__()
        self.session_id = session_id
        self.store = store
        self.diff_data: dict = {}
        self.files: list = []

    def compose(self) -> ComposeResult:
        self._load_diff_data()

        meta = self.diff_data.get("meta") or {}
        agent = str(meta.get("agent", "unknown"))
        sid = _short_id(str(meta.get("id", "")), "ses_")
        branch = str(meta.get("branch", "?"))

        header = Text()
        header.append("\u25e1 ", style="bold white")
        header.append("DIFF", style="bold cyan")
        header.append(f"  {sid}", style="blue")
        header.append("  ", style="dim")
        header.append(f"{agent}  ", style="bold yellow")
        header.append(f"on {branch}", style="cyan")

        yield Static(header, id="diff-header")

        with Horizontal(id="diff-container"):
            with Vertical(id="files-pane"):
                yield Static("Changed Files / Steps", classes="pane-title")
                yield DataTable(id="diff-files-table", cursor_type="row",
                                zebra_stripes=True, show_cursor=True)

            with VerticalScroll(id="details-pane"):
                detail_placeholder = Text("Select a file to see details", style="dim italic")
                yield Static(detail_placeholder, id="diff-detail-content")

        footer = Text()
        footer.append("q/esc/d ", style="bold yellow")
        footer.append("close  \u00b7  ", style="dim")
        footer.append("\u2191\u2193 ", style="bold yellow")
        footer.append("navigate  \u00b7  ", style="dim")
        footer.append("enter ", style="bold yellow")
        footer.append("focus", style="dim")
        yield Static(footer, id="diff-footer")

    def on_mount(self) -> None:
        table = self.query_one("#diff-files-table", DataTable)
        table.add_columns("Type", "File", "+/-", "Source")
        self._populate_files_table()

    def _load_diff_data(self) -> None:
        try:
            self.diff_data = self.store.session_diff(self.session_id)
        except Exception:
            self.diff_data = {"meta": {}, "files": [], "files_changed": 0,
                              "total_added": 0, "total_removed": 0,
                              "tool_calls": 0, "tool_names": {}}

    def _populate_files_table(self) -> None:
        table = self.query_one("#diff-files-table", DataTable)
        table.clear()
        self.files = self.diff_data.get("files", [])
        for f in self.files:
            action = f.get("action", "?")
            fp = f.get("file_path", "?")
            added = f.get("lines_added", 0)
            removed = f.get("lines_removed", 0)
            source = f.get("diff_source") or ("recorded" if f.get("hunks") or f.get("steps") else "summary")
            icon = {"write": "edit", "read": "read", "delete": "del"}.get(action, "mod")
            delta = []
            if added:
                delta.append(f"+{added}")
            if removed:
                delta.append(f"-{removed}")
            table.add_row(icon, fp, " ".join(delta) or "-", source)
        if self.files:
            table.move_cursor(row=0)
            self._update_detail(0)

    def _detail_text(self, idx: int) -> Text:
        if not (0 <= idx < len(self.files)):
            return Text("Select a file to see details", style="dim italic")
        f = self.files[idx]

        action = f.get("action", "?")
        added = f.get("lines_added", 0)
        removed = f.get("lines_removed", 0)
        hunks = f.get("hunks", [])
        steps = f.get("steps", [])
        tool_names = f.get("tool_names", [])
        git_diff = f.get("git_diff") or ""
        diff_source = f.get("diff_source") or ("recorded" if hunks else "summary")

        detail = Text()
        detail.append(f.get("file_path", "?"), style="bold white")
        detail.append("  ")
        detail.append(diff_source, style="dim cyan" if diff_source == "git" else "dim")
        detail.append("\n\n")

        detail.append("Action:  ", style="dim")
        detail.append(action, style="bold yellow")
        detail.append("    ")

        detail.append("Lines:  ", style="dim")
        detail.append(f"+{added} ", style="green" if added else "dim")
        detail.append(f"-{removed}", style="red" if removed else "dim")
        detail.append("\n")

        if tool_names:
            detail.append("\nTools:\n", style="bold")
            for name in tool_names:
                detail.append(f"  {name}\n", style="yellow")

        if steps:
            detail.append("\nSteps touching this file:\n", style="bold")
            for step in steps:
                stype = step.get("step_type", "?")
                tool_name = step.get("tool_name")
                ts = step.get("ts")
                step_id = str(step.get("step_id") or "")
                detail.append("  ")
                detail.append(_short_id(step_id, "step_", 10), style="blue")
                detail.append(f"  {stype}", style="dim")
                if tool_name:
                    detail.append(f"  {tool_name}", style="yellow")
                if ts:
                    detail.append(f"  {_abs_ts(int(ts))}", style="dim")
                detail.append("\n")

        if hunks:
            detail.append("\nRecorded hunks:\n", style="bold")
            for i, h in enumerate(hunks, 1):
                from_line = h.get("from", 0)
                to_line = h.get("to", 0)
                detail.append(f"  hunk {i}: ", style="dim")
                detail.append(f"@@ -{from_line} +{to_line} @@\n", style="cyan")
        elif git_diff:
            detail.append("\nGit diff fallback:\n", style="bold cyan")
            self._append_patch(detail, git_diff)
        else:
            detail.append("\nNo hunk details were recorded for this file.", style="dim")

        return detail

    def _append_patch(self, detail: Text, patch: str, limit: int = 220) -> None:
        lines = patch.splitlines()
        for line in lines[:limit]:
            style = "white"
            if line.startswith("+++ ") or line.startswith("--- "):
                style = "dim"
            elif line.startswith("@@"):
                style = "cyan"
            elif line.startswith("+"):
                style = "green"
            elif line.startswith("-"):
                style = "red"
            elif line.startswith("diff --git"):
                style = "bold blue"
            detail.append(line + "\n", style=style)
        if len(lines) > limit:
            detail.append(f"... {len(lines) - limit} more line(s)\n", style="dim")

    def action_quit(self) -> None:
        if self.app.__class__.__name__ == "DiffOnlyApp":
            self.app.exit()
            return
        self.app.pop_screen()

    def action_refresh(self) -> None:
        self._load_diff_data()
        self._populate_files_table()

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


class MachApp(App):
    TITLE = "mach"
    SUB_TITLE = "execution ledger"

    CSS = """
    Screen {
        background: transparent;
    }
    #hero {
        dock: top;
        height: 4;
        border-bottom: tall $primary;
        padding: 0 2;
    }
    #hero-left, #hero-right {
        height: 4;
        content-align: left middle;
    }
    #hero-left {
        width: 1fr;
    }
    #hero-right {
        width: 38;
        content-align: right middle;
    }
    #split {
        margin: 1 0 0 0;
    }
    .pane-title {
        height: 2;
        padding: 0 1;
        border-bottom: solid $primary;
        content-align: left middle;
    }
    .subpanel {
        height: auto;
        border-top: solid $surface-lighten-1;
        padding: 1 1 1 1;
    }
    #split {
        height: 1fr;
    }
    #session-pane {
        width: 42;
        min-width: 34;
        height: 1fr;
        border-right: tall $primary;
        background: transparent;
    }
    ListView {
        height: 1fr;
        background: transparent;
        padding: 0;
        border: none;
    }
    ListView:focus {
        border: none;
    }
    ListItem {
        height: 6;
        padding: 1 1;
        border-bottom: solid $surface-lighten-1;
        background: transparent;
    }
    ListItem.--highlight {
        background: $boost;
        border-left: thick $accent;
    }
    ListItem:hover {
        background: $boost;
    }
    #steps-pane {
        width: 1fr;
        height: 1fr;
        background: transparent;
    }
    DataTable {
        height: 1fr;
        background: transparent;
        padding: 0 1;
    }
    DataTable > .datatable--header {
        background: transparent;
        text-style: bold;
    }
    DataTable > .datatable--cursor {
        background: $boost;
    }
    DataTable > .datatable--even-row {
        background: transparent;
    }
    Footer {
        background: transparent;
        border-top: solid $primary;
        height: 1;
    }
    Footer > .footer--key {
        background: $primary;
    }
    #step-search-input {
        border: none;
        background: $boost;
        height: 2;
        margin: 0;
        padding: 0 1;
    }
    #step-search-input:focus {
        border: none;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("tab,right", "focus_steps", "→ Steps", show=True),
        Binding("escape,left", "focus_sessions", "← Sessions", show=True),
        Binding("slash", "focus_search", "/ Search", show=True),
        Binding("r", "refresh", "Refresh", show=True),
        Binding("d", "diff", "Diff", show=True),
    ]

    def __init__(self, store: SessionStore, initial_diff_session_id: str | None = None) -> None:
        super().__init__()
        self.store = store
        self.sessions: list[dict] = []
        self.steps: list[dict] = []
        self.agent = "unknown"
        self.selected_session_id: str | None = None
        self.accent_name = _accent_from_env()
        self.accent, self.accent_bold = ACCENT_STYLES[self.accent_name]
        self._initial_diff_session_id = initial_diff_session_id

    def compose(self) -> ComposeResult:
        with Horizontal(id="hero"):
            yield Static(id="hero-left")
            yield Static(id="hero-right")

        with Horizontal(id="split"):
            with Vertical(id="session-pane"):
                yield Static(id="session-pane-title", classes="pane-title")
                yield ListView(id="session-list")
                yield Static(id="session-meta", classes="subpanel")

            with Vertical(id="steps-pane"):
                yield Static(id="steps-pane-title", classes="pane-title")
                yield Input(placeholder="Search steps... (press '/')", id="step-search-input")
                yield Static(id="steps-summary", classes="subpanel")
                yield DataTable(id="steps-table", cursor_type="row",
                                zebra_stripes=True, show_cursor=True)
                yield Static(id="step-preview", classes="subpanel")

        yield Footer()

    def _sessions_title(self) -> Text:
        t = Text()
        t.append(" Sessions", style=self.accent_bold)
        if self.sessions:
            t.append(f"  {len(self.sessions)}", style="dim yellow")
        return t

    def _steps_title(self, label: str = "Timeline", count: int = 0,
                     sid: str = "") -> Text:
        t = Text()
        t.append(f" {label}", style=self.accent_bold)
        if sid:
            t.append(f"  {sid}", style="dim")
        if count:
            t.append(f"  {count} steps", style="dim yellow")
        return t

    def _header_title(self) -> Text:
        active = sum(1 for session in self.sessions if _session_status(session) == "active")
        agents = len({str(session.get("agent", "unknown")) for session in self.sessions}) if self.sessions else 0
        repo = self.store.paths.repo_root.name or "."

        t = Text()
        t.append("⬡ ", style=self.accent_bold)
        t.append("MACH", style="bold white")
        t.append("  execution ledger", style="dim")
        t.append("   ", style="dim")
        t.append(repo, style="bold")
        t.append("  ", style="dim")
        t.append(f"{active} active", style="green")
        t.append("  ", style="dim")
        t.append(f"{len(self.sessions)} sessions", style="cyan")
        t.append("  ", style="dim")
        t.append(f"{agents} agents", style="yellow")
        return t

    def _header_meta(self) -> Text:
        term_program = os.environ.get("TERM_PROGRAM", "terminal")
        profile = (
            os.environ.get("ITERM_PROFILE")
            or os.environ.get("TERM_PROFILE")
            or "terminal profile"
        )
        t = Text()
        t.append(term_program, style=self.accent_bold)
        t.append("  ", style="dim")
        t.append(profile, style="dim")
        return t

    def _session_meta_text(self, session: dict | None) -> Text:
        if not session:
            return Text("Pick a session to inspect its branch, commit range, and activity profile.", style="dim")

        status = _session_status(session)
        started = session.get("started_at", 0) or 0
        ended = session.get("ended_at", 0) or 0
        risk_count = session.get("risk_count", 0) or 0
        branch = str(session.get("branch", "?"))
        task = session.get("task_desc") or "No task description recorded."
        pre_commit = _short_commit(session.get("pre_commit"))
        post_commit = _short_commit(session.get("post_commit"))

        t = Text()
        t.append("Selected session\n", style=self.accent_bold)
        t.append(f"{str(session.get('agent', 'unknown')).upper()} ", style=f"bold {AGENT_COLOR.get(str(session.get('agent', '')).lower(), 'white')}")
        t.append(f"{status}", style="bold green" if status == "active" else "dim")
        t.append(f"  on {branch}\n", style="cyan")
        t.append("Started  ", style="dim")
        t.append(_abs_ts(started), style="white")
        if ended:
            t.append("\nEnded    ", style="dim")
            t.append(_abs_ts(ended), style="white")
        t.append("\nCommits  ", style="dim")
        t.append(pre_commit, style="yellow")
        t.append(" -> ", style="dim")
        t.append(post_commit if post_commit != "?" else "pending", style="green" if post_commit != "?" else "dim")
        t.append("\nRisk     ", style="dim")
        t.append(str(risk_count), style="red" if risk_count else "green")
        t.append("\nTask     ", style="dim")
        t.append(str(task), style="white")
        return t

    def _steps_summary_text(self, session: dict | None, steps: list[dict]) -> Text:
        if not session:
            return Text("No session selected.", style="dim")

        counts = Counter(step.get("type", "unknown") for step in steps)
        tool_calls = sum(step.get("count", 1) for step in steps if step.get("type") == "tool")
        file_events = sum(_count_file_changes(step) for step in steps)
        status = _session_status(session)

        t = Text()
        t.append("Overview  ", style=self.accent_bold)
        t.append(status, style="green" if status == "active" else "dim")
        t.append("   ")
        t.append(f"{counts.get('input', 0)} in", style="green")
        t.append("   ")
        t.append(f"{counts.get('reasoning', 0)} think", style="magenta")
        t.append("   ")
        t.append(f"{tool_calls} tools", style="yellow")
        t.append("   ")
        t.append(f"{counts.get('output', 0)} out", style="cyan")
        t.append("   ")
        t.append(f"{file_events} file events", style="blue")
        return t

    def _step_preview_text(self, step: dict | None) -> Text:
        if not step:
            return Text("Move through the timeline to preview the selected step here. Press Enter for the full detail view.", style="dim")

        stype = step.get("type", "unknown")
        icon, ic = STEP_ICON.get(stype, ("·", "dim"))
        t = Text()
        t.append(f"{icon} ", style=ic)
        t.append(f"{stype.upper()}  ", style=ic)
        t.append(_preview_text(step), style="white")
        if stype == "tool":
            count = step.get("count", 1)
            category = step.get("category", "exec")
            files = _count_file_changes(step)
            t.append("  ", style="dim")
            t.append(f"[{category}]", style="dim")
            if count > 1:
                t.append(f"  x{count}", style="yellow")
            if files:
                t.append(f"  {files} file changes", style="blue")
        return t

    def on_mount(self) -> None:
        tt = self.query_one("#steps-table", DataTable)
        tt.add_columns(" ", "Event", "Summary", "Files", "When")
        self._load_sessions()
        self._refresh_header()
        self.query_one("#session-list", ListView).focus()
        if self._initial_diff_session_id:
            self.set_timer(0.05, self._open_initial_diff)

    def _open_initial_diff(self) -> None:
        sid = self._initial_diff_session_id
        if not sid:
            return
        # Select matching session in the list so focus is correct
        for i, s in enumerate(self.sessions):
            if s.get("id") == sid:
                lv = self.query_one("#session-list", ListView)
                lv.index = i
                self._load_steps(s)
                break
        self.push_screen(DiffScreen(sid, self.store))

    def _refresh_header(self) -> None:
        self.query_one("#hero-left", Static).update(self._header_title())
        self.query_one("#hero-right", Static).update(self._header_meta())

    def _load_sessions(self) -> None:
        self.sessions = self.store.list_sessions()
        lv = self.query_one("#session-list", ListView)
        lv.clear()
        for s in self.sessions:
            lv.append(self._make_session_item(s))
        self.query_one("#session-pane-title", Static).update(self._sessions_title())
        self._refresh_header()
        if self.sessions:
            self.selected_session_id = self.sessions[0].get("id")
            self._load_steps(self.sessions[0])
        else:
            self.steps = []
            self.selected_session_id = None
            self.query_one("#steps-pane-title", Static).update(self._steps_title())
            self.query_one("#session-meta", Static).update(self._session_meta_text(None))
            self.query_one("#steps-summary", Static).update(self._steps_summary_text(None, []))
            self.query_one("#step-preview", Static).update(self._step_preview_text(None))

    def _make_session_item(self, s: dict) -> ListItem:
        sid = _short_id(str(s.get("id", "")), "ses_", size=10)
        agent = str(s.get("agent", "?"))
        branch = str(s.get("branch", "?"))
        status = _session_status(s)
        n_steps = s.get("step_count", 0)
        started = s.get("started_at", 0)
        is_active = status == "active"
        acol = AGENT_COLOR.get(agent.lower(), "white")
        commit = _short_commit(s.get("post_commit") or s.get("pre_commit"))
        risk = s.get("risk_count", 0) or 0

        line1 = Text()
        line1.append("● " if is_active else "○ ", style="bold green" if is_active else "dim")
        line1.append(sid, style="bold white")
        line1.append("  ", style="dim")
        line1.append(commit, style="yellow")

        line2 = Text()
        line2.append(agent, style=f"bold {acol}")
        line2.append("  on ", style="dim")
        line2.append(branch, style="cyan")

        line3 = Text()
        line3.append(f"{n_steps} steps", style="dim")
        line3.append("  ·  ", style="dim")
        line3.append(_rel(started), style="dim")
        if risk:
            line3.append("  ·  ", style="dim")
            line3.append(f"{risk} risk", style="red")

        line4 = Text("active now" if is_active else "completed", style="green" if is_active else "dim")

        content = Text.assemble(line1, "\n", line2, "\n", line3, "\n", line4)
        return ListItem(Static(content))

    def _load_steps(self, session: dict) -> None:
        sid = session.get("id", "")
        self.agent = str(session.get("agent", "unknown"))
        self.selected_session_id = sid
        try:
            data = self.store.show_session(sid)
            self.steps = _coalesce(data["steps"])
        except Exception:
            self.steps = []

        tt = self.query_one("#steps-table", DataTable)
        tt.clear()

        short_id = _short_id(sid, "ses_")
        self.query_one("#steps-pane-title", Static).update(
            self._steps_title(label="Timeline", sid=short_id, count=len(self.steps))
        )
        self.query_one("#session-meta", Static).update(self._session_meta_text(data["meta"] if "data" in locals() else session))
        self.query_one("#steps-summary", Static).update(self._steps_summary_text(data["meta"] if "data" in locals() else session, self.steps))

        # Clear search input on new session
        search_input = self.query_one("#step-search-input", Input)
        search_input.value = ""
        self._populate_table()

    def _populate_table(self, query: str = "") -> None:
        tt = self.query_one("#steps-table", DataTable)
        tt.clear()
        
        q = query.lower().strip()
        visible_steps = []

        for step in self.steps:
            stype = step.get("type", "unknown")
            content = str(step.get("content") or "").lower()
            tool_content = str(step.get("tool", {}).get("content") or "").lower()
            name = str(step.get("tool", {}).get("name") or "").lower()

            if q and q not in content and q not in tool_content and q not in name and q not in stype:
                continue
            
            visible_steps.append(step)

        self.query_one("#steps-pane-title", Static).update(
            self._steps_title(label="Timeline", sid=_short_id(self.selected_session_id or "", "ses_"), count=len(visible_steps))
        )

        for step in visible_steps:
            stype = step.get("type", "unknown")
            icon, ic = STEP_ICON.get(stype, ("·", "dim"))
            ts = step.get("ts", 0)

            icon_cell = Text(icon, style=ic)
            label_cell = Text(stype.upper(), style=ic + " bold" if "bold" not in ic else ic)

            if stype == "tool":
                count = step.get("count", 1)
                cat = step.get("category", "exec")
                ci = TOOL_CAT_ICON.get(cat, "·")
                detail = Text()
                detail.append(f"{ci} ", style="yellow")
                detail.append(step.get("name", "?"), style="bold yellow")
                tool_content = _strip(step.get("content") or "").strip().replace("\n", " ")
                if tool_content:
                    detail.append("  ", style="dim")
                    detail.append(tool_content[:76] + "…" if len(tool_content) > 76 else tool_content, style="dim")
                if count > 1:
                    detail.append(f"  x{count}", style="bold yellow")
            else:
                raw = _strip(step.get("content") or "").strip().replace("\n", " ")
                if not raw:
                    detail = Text("(redacted)", style="dim italic")
                else:
                    detail = Text(raw[:120] + "…" if len(raw) > 120 else raw)

            files_cell = Text(str(_count_file_changes(step)) if _count_file_changes(step) else "·", style="blue" if _count_file_changes(step) else "dim")
            ts_cell = Text(_abs_ts(ts), style="dim") if ts else Text("")
            tt.add_row(icon_cell, label_cell, detail, files_cell, ts_cell)

        self.query_one("#step-preview", Static).update(self._step_preview_text(visible_steps[0] if visible_steps else None))

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
        if row is not None and 0 <= row < len(self.steps):
            self.query_one("#step-preview", Static).update(self._step_preview_text(self.steps[row]))

    @on(DataTable.RowSelected, "#steps-table")
    def on_step_selected(self, event: DataTable.RowSelected) -> None:
        row = event.cursor_row
        if row is not None and 0 <= row < len(self.steps):
            self.push_screen(StepDetail(self.steps[row], self.agent))

    def action_focus_steps(self) -> None:
        self.query_one("#steps-table", DataTable).focus()

    def action_focus_search(self) -> None:
        self.query_one("#step-search-input", Input).focus()

    def action_focus_sessions(self) -> None:
        self.query_one("#session-list", ListView).focus()

    def action_refresh(self) -> None:
        self._load_sessions()
        self.notify("Sessions refreshed", severity="information", timeout=2)

    def action_diff(self) -> None:
        if self.selected_session_id:
            self.push_screen(DiffScreen(self.selected_session_id, self.store))


class DiffOnlyApp(App):
    """Standalone TUI app that shows only the diff screen for a session."""

    TITLE = "mach diff"
    BINDINGS = [Binding("q,escape", "quit", "Quit")]

    DEFAULT_CSS = """
    Screen {
        background: $background;
    }
    """

    def __init__(self, store: SessionStore, session_id: str) -> None:
        super().__init__()
        self.store = store
        self.session_id = session_id

    def on_mount(self) -> None:
        self.push_screen(DiffScreen(self.session_id, self.store))


def run_tui(store: SessionStore) -> None:
    MachApp(store).run()
