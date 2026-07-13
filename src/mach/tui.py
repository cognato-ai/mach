"""
mach.tui — Minimal execution ledger dashboard.

Quiet monochrome UI with a single accent. Inspired by clean CLI shells:
sparse chrome, text-first, unique selection language.
"""
from __future__ import annotations

import re
import time as _time
from collections import Counter
from typing import Any

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Footer, Input, ListItem, ListView, Static

from mach.session import SessionStore

# ── helpers ──────────────────────────────────────────────────────────────────

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\\033\[[0-9;]*[A-Za-z]")

# Palette — monochrome + one accent (mach mint)
FG = "#e8e6e3"
MUTED = "#6b6860"
DIM = "#3d3b38"
ACCENT = "#a8e6cf"  # soft mint — unique, calm
ACCENT_HI = "#7dd3b0"
BG = "#0c0c0b"
SURFACE = "#121211"
PANEL = "#161614"
BORDER = "#242422"
DANGER = "#e8a0a0"
OK = "#a8e6cf"


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


# Minimal glyphs — same weight, accent only when selected/active
STEP_GLYPH = {
    "input": "›",
    "reasoning": "·",
    "tool": "›",
    "output": "‹",
    "system_action": "·",
}

STEP_LABEL = {
    "input": "in",
    "reasoning": "think",
    "tool": "tool",
    "output": "out",
    "system_action": "sys",
}


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


def _preview_text(step: dict[str, Any], width: int = 96) -> str:
    if step.get("type") == "tool":
        bits = [step.get("name", "?"), str(step.get("content") or "").strip().replace("\n", " ")]
        value = "  ".join(b for b in bits if b)
    else:
        value = str(step.get("content") or "").strip().replace("\n", " ")
    value = _strip(value)
    if not value:
        return "—"
    return value if len(value) <= width else value[: width - 1] + "…"


_APP_CSS = f"""
/* quiet monochrome + one accent */
$bg: {BG};
$surface: {SURFACE};
$panel: {PANEL};
$border: {BORDER};
$text: {FG};
$muted: {MUTED};
$dim: {DIM};
$accent: {ACCENT};

Screen {{
    background: $bg;
    color: $text;
}}

#chrome {{
    dock: top;
    height: 1;
    background: $bg;
    color: $muted;
    padding: 0 1;
}}

#body {{
    height: 1fr;
}}

#rail {{
    width: 32;
    min-width: 28;
    max-width: 38;
    height: 1fr;
    background: $surface;
    border-right: solid $border;
}}

#main {{
    width: 1fr;
    height: 1fr;
    background: $bg;
}}

.quiet-label {{
    height: 1;
    color: $dim;
    padding: 0 1;
    text-style: none;
}}

ListView {{
    height: 1fr;
    background: transparent;
    border: none;
    padding: 0;
    scrollbar-background: $surface;
    scrollbar-color: $border;
    scrollbar-size: 1 1;
}}
ListView:focus {{
    border: none;
}}
ListItem {{
    height: 3;
    padding: 0 1;
    background: transparent;
    color: $muted;
}}
ListItem.--highlight {{
    background: $panel;
    color: $text;
    border-left: outer $accent;
}}
ListItem:hover {{
    background: $panel;
}}

#meta {{
    height: 5;
    background: $bg;
    border-top: solid $border;
    padding: 0 1;
    color: $muted;
}}

#head {{
    height: 1;
    color: $dim;
    padding: 0 1;
}}

#filter {{
    height: 1;
    background: $bg;
    border: none;
    padding: 0 1;
    color: $text;
}}
#filter:focus {{
    background: $surface;
    color: $text;
}}
#filter > .input--placeholder {{
    color: $dim;
}}

DataTable {{
    height: 1fr;
    background: transparent;
    padding: 0;
    scrollbar-background: $bg;
    scrollbar-color: $border;
    scrollbar-size: 1 1;
}}
DataTable > .datatable--header {{
    background: $bg;
    color: $dim;
    text-style: none;
}}
DataTable > .datatable--cursor {{
    background: $panel;
    color: $text;
    text-style: none;
}}
DataTable > .datatable--hover {{
    background: $surface;
}}
DataTable > .datatable--even-row {{
    background: transparent;
}}
DataTable > .datatable--odd-row {{
    background: transparent;
}}

#peek {{
    height: 3;
    background: $surface;
    border-top: solid $border;
    padding: 0 1;
    color: $muted;
}}

Footer {{
    background: $bg;
    border-top: solid $border;
    color: $dim;
    height: 1;
}}
Footer > .footer--key {{
    background: transparent;
    color: $accent;
    text-style: none;
}}
Footer > .footer--description {{
    color: $dim;
}}

/* modal */
StepDetail {{
    align: center middle;
    background: #000000 55%;
}}
#modal {{
    width: 86%;
    height: 82%;
    background: $surface;
    border: solid $border;
    padding: 0;
}}
#modal-h {{
    height: auto;
    padding: 1 2;
    border-bottom: solid $border;
    color: $text;
}}
#modal-b {{
    height: 1fr;
    padding: 1 2;
    color: $text;
    scrollbar-color: $border;
}}
#modal-f {{
    height: 1;
    padding: 0 2;
    border-top: solid $border;
    color: $dim;
}}

/* diff */
DiffScreen {{
    background: $bg;
}}
#diff-h {{
    height: 1;
    padding: 0 1;
    color: $muted;
    border-bottom: solid $border;
}}
#diff-split {{
    height: 1fr;
}}
#diff-l {{
    width: 38%;
    height: 1fr;
    border-right: solid $border;
    background: $surface;
}}
#diff-r {{
    width: 1fr;
    height: 1fr;
    padding: 1 2;
    background: $bg;
}}
#diff-f {{
    height: 1;
    padding: 0 1;
    border-top: solid $border;
    color: $dim;
}}
"""


# ══════════════════════════════════════════════════════════
#  Step detail
# ══════════════════════════════════════════════════════════


class StepDetail(ModalScreen[None]):
    BINDINGS = [Binding("escape,q", "dismiss", "close", show=True)]

    def __init__(self, step: dict, agent: str) -> None:
        super().__init__()
        self.step = step
        self.agent = agent

    def compose(self) -> ComposeResult:
        s = self.step
        stype = s.get("type", "unknown")
        glyph = STEP_GLYPH.get(stype, "·")
        label = STEP_LABEL.get(stype, stype)
        ts = s.get("ts", 0)

        with Vertical(id="modal"):
            h = Text()
            h.append(f"{glyph} ", style=ACCENT)
            h.append(label, style=f"bold {FG}")
            h.append("  ", style="")
            h.append(_short_id(str(s.get("id", "")), "step_", 12), style=MUTED)
            h.append("  ·  ", style=DIM)
            h.append(self.agent, style=MUTED)
            if ts:
                h.append(f"  ·  {_abs_ts(ts)}", style=DIM)
            yield Static(h, id="modal-h")

            with VerticalScroll(id="modal-b"):
                if stype == "tool":
                    t = Text()
                    t.append(str(s.get("name", "?")), style=f"bold {FG}")
                    t.append(f"  {s.get('category', 'exec')}", style=MUTED)
                    risk = s.get("risk_level") or "none"
                    if risk != "none":
                        t.append(f"  risk:{risk}", style=DANGER)
                    yield Static(t)
                    yield Static(Text(""))

                content = _strip(s.get("content") or "").strip()
                if content:
                    for line in content.splitlines():
                        yield Static(Text(line, style=FG))
                else:
                    yield Static(Text("—", style=DIM))

                fc = s.get("file_changes") or []
                if fc:
                    yield Static(Text(""))
                    yield Static(Text("files", style=MUTED))
                    for ch in fc:
                        action = ch.get("action", "write")
                        fp = ch.get("file_path", "?")
                        added = ch.get("lines_added", 0) or 0
                        removed = ch.get("lines_removed", 0) or 0
                        line = Text()
                        line.append(f"{action}  ", style=MUTED)
                        line.append(str(fp), style=FG)
                        if added or removed:
                            line.append(f"  +{added}", style=OK if added else DIM)
                            line.append(f" -{removed}", style=DANGER if removed else DIM)
                        yield Static(line)
                        for hunk in ch.get("hunks", []):
                            hs, he = hunk.get("from", 0), hunk.get("to", 0)
                            yield Static(
                                Text(
                                    f"  @@ -{hs},{max(1, he - hs + 1)} +{hs},{max(1, he - hs + 1)} @@",
                                    style=DIM,
                                )
                            )

                flags = s.get("risk_flags") or []
                if flags:
                    yield Static(Text(""))
                    yield Static(Text("risk", style=MUTED))
                    for flag in flags:
                        if not isinstance(flag, dict):
                            continue
                        rl = Text()
                        rl.append(f"{flag.get('severity', '?')}  ", style=DANGER)
                        rl.append(str(flag.get("rule_id", "?")), style=FG)
                        if flag.get("explanation"):
                            rl.append(f"  {flag['explanation']}", style=MUTED)
                        yield Static(rl)

            yield Static(Text("esc  close", style=DIM), id="modal-f")


# ══════════════════════════════════════════════════════════
#  Diff
# ══════════════════════════════════════════════════════════


class DiffScreen(Screen):
    BINDINGS = [
        Binding("q,escape", "quit", "close"),
        Binding("d", "quit", "close"),
        Binding("r", "refresh", "refresh"),
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

        h = Text()
        h.append("diff", style=MUTED)
        h.append(f"  {sid}", style=FG)
        h.append(f"  {agent}", style=MUTED)
        h.append(f"  {branch}", style=DIM)
        h.append(f"  ·  {self.diff_data.get('files_changed', 0)} files", style=DIM)
        h.append(f"  +{self.diff_data.get('total_added', 0)}", style=OK)
        h.append(f" -{self.diff_data.get('total_removed', 0)}", style=DANGER)
        yield Static(h, id="diff-h")

        with Horizontal(id="diff-split"):
            with Vertical(id="diff-l"):
                yield Static(Text("steps", style=DIM), classes="quiet-label")
                yield DataTable(
                    id="diff-files-table",
                    cursor_type="row",
                    zebra_stripes=False,
                    show_cursor=True,
                )
            with VerticalScroll(id="diff-r"):
                yield Static(Text("select a step", style=DIM), id="diff-detail-content")

        yield Static(Text("q close  ·  ↑↓ navigate", style=DIM), id="diff-f")

    def on_mount(self) -> None:
        table = self.query_one("#diff-files-table", DataTable)
        table.add_columns("step", "tool", "files", "+/−")
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
            }

    def _populate_steps_table(self) -> None:
        table = self.query_one("#diff-files-table", DataTable)
        table.clear()
        self.steps = self.diff_data.get("steps") or []
        for step in self.steps:
            files = step.get("files") or []
            tool = step.get("tool_name") or step.get("step_type", "?")
            table.add_row(
                _short_id(str(step.get("step_id", "")), "step_", 8),
                str(tool),
                self._step_file_label(files),
                self._delta_label(step.get("lines_added", 0), step.get("lines_removed", 0)),
            )
        if self.steps:
            table.move_cursor(row=0)
            self._update_detail(0)

    def _detail_text(self, idx: int) -> Text:
        if not (0 <= idx < len(self.steps)):
            return Text("select a step", style=DIM)
        step = self.steps[idx]
        files = step.get("files") or []
        d = Text()
        d.append(_short_id(str(step.get("step_id") or ""), "step_", 14), style=MUTED)
        d.append("  ")
        d.append(str(step.get("step_type", "?")), style=FG)
        if step.get("tool_name"):
            d.append(f"  {step['tool_name']}", style=MUTED)
        if step.get("ts"):
            d.append(f"  {_abs_ts(int(step['ts']))}", style=DIM)
        d.append("\n\n")
        d.append(f"{step.get('files_changed', len(files))} files  ", style=MUTED)
        self._append_delta(d, step.get("lines_added", 0), step.get("lines_removed", 0))
        d.append(f"  {step.get('diff_source', 'recorded')}", style=DIM)
        d.append("\n")

        content = _strip(step.get("content") or "").strip()
        if content:
            d.append("\n")
            preview = content if len(content) <= 600 else content[:600].rstrip() + "\n…"
            for line in preview.splitlines():
                d.append(f"{line}\n", style=MUTED)

        if not files:
            d.append("\nno file details", style=DIM)
            return d

        d.append("\n")
        for f in files:
            self._append_file_change(d, f)
        return d

    def _append_file_change(self, d: Text, f: dict) -> None:
        d.append("\n")
        d.append(str(f.get("file_path", "?")), style=FG)
        d.append(f"  {f.get('action', '?')}  ", style=MUTED)
        self._append_delta(d, f.get("lines_added", 0), f.get("lines_removed", 0))
        d.append("\n")
        hunks = f.get("hunks") or []
        git_diff = f.get("git_diff") or ""
        if hunks:
            for i, h in enumerate(hunks, 1):
                d.append(
                    f"  @@ -{h.get('from', 0)} +{h.get('to', 0)} @@\n",
                    style=DIM,
                )
        elif git_diff:
            self._append_patch(d, git_diff)
        else:
            d.append("  —\n", style=DIM)

    def _append_delta(self, d: Text, added: int, removed: int) -> None:
        d.append(f"+{added} ", style=OK if added else DIM)
        d.append(f"-{removed}", style=DANGER if removed else DIM)

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

    def _append_patch(self, d: Text, patch: str, limit: int = 180) -> None:
        lines = patch.splitlines()
        for line in lines[:limit]:
            style = MUTED
            if line.startswith("+") and not line.startswith("+++"):
                style = OK
            elif line.startswith("-") and not line.startswith("---"):
                style = DANGER
            elif line.startswith("@@"):
                style = DIM
            d.append(line + "\n", style=style)
        if len(lines) > limit:
            d.append(f"… {len(lines) - limit} more\n", style=DIM)

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
#  Main
# ══════════════════════════════════════════════════════════


class MachApp(App):
    TITLE = "mach"
    CSS = _APP_CSS

    BINDINGS = [
        Binding("q", "quit", "quit"),
        Binding("tab,right", "focus_steps", "steps"),
        Binding("escape,left", "focus_sessions", "sessions"),
        Binding("slash", "focus_search", "filter"),
        Binding("r", "refresh", "refresh"),
        Binding("d", "diff", "diff"),
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
        yield Static(id="chrome")
        with Horizontal(id="body"):
            with Vertical(id="rail"):
                yield Static(Text("sessions", style=DIM), classes="quiet-label")
                yield ListView(id="session-list")
                yield Static(id="meta")
            with Vertical(id="main"):
                yield Static(id="head")
                yield Input(placeholder="filter…", id="filter")
                yield DataTable(
                    id="steps-table",
                    cursor_type="row",
                    zebra_stripes=False,
                    show_cursor=True,
                )
                yield Static(id="peek")
        yield Footer()

    # ── render ───────────────────────────────────────────────────────────────

    def _chrome_text(self) -> Text:
        repo = self.store.paths.repo_root.name or "."
        active = sum(1 for s in self.sessions if _session_status(s) == "active")
        t = Text()
        t.append("mach", style=f"bold {FG}")
        t.append("  ·  ", style=DIM)
        t.append(repo, style=MUTED)
        t.append("  ·  ", style=DIM)
        t.append(f"{len(self.sessions)}", style=MUTED)
        t.append(" sessions", style=DIM)
        if active:
            t.append("  ·  ", style=DIM)
            t.append(f"{active} live", style=ACCENT)
        return t

    def _session_row(self, s: dict) -> ListItem:
        sid = _short_id(str(s.get("id", "")), "ses_", 7)
        agent = str(s.get("agent", "?"))
        branch = str(s.get("branch", "?"))
        status = _session_status(s)
        n = s.get("step_count", 0) or 0
        started = s.get("started_at", 0) or 0
        live = status == "active"
        risk = s.get("risk_count", 0) or 0

        line1 = Text()
        line1.append("● " if live else "  ", style=ACCENT if live else DIM)
        line1.append(sid, style=FG if live else MUTED)
        line1.append(f"  {agent}", style=MUTED)
        line1.append(f"  {_rel(started)}", style=DIM)

        line2 = Text()
        line2.append(f"  {branch}", style=DIM)
        line2.append(f"  {n} steps", style=DIM)
        if risk:
            line2.append(f"  !{risk}", style=DANGER)

        return ListItem(Static(Text.assemble(line1, "\n", line2)))

    def _meta_text(self, session: dict | None) -> Text:
        if not session:
            return Text("\nselect a session", style=DIM)
        t = Text()
        t.append("\n")
        t.append(_session_status(session), style=ACCENT if _session_status(session) == "active" else MUTED)
        t.append(f"  {session.get('branch', '?')}\n", style=MUTED)
        t.append(_short_commit(session.get("pre_commit")), style=DIM)
        t.append(" → ", style=DIM)
        post = _short_commit(session.get("post_commit"))
        t.append(post if post != "—" else "…", style=MUTED)
        task = str(session.get("task_desc") or "").strip().replace("\n", " ")
        if task:
            if len(task) > 42:
                task = task[:41] + "…"
            t.append(f"\n{task}", style=DIM)
        return t

    def _head_text(self, sid: str = "", count: int = 0, session: dict | None = None) -> Text:
        t = Text()
        t.append("timeline", style=DIM)
        if sid:
            t.append(f"  {sid}", style=MUTED)
        if count:
            t.append(f"  {count}", style=DIM)
        if session and self.steps:
            c = Counter(s.get("type", "?") for s in self.steps)
            tools = sum(1 for s in self.steps if s.get("type") == "tool")
            t.append(
                f"  ·  {c.get('input', 0)} in  {tools} tools  {c.get('output', 0)} out",
                style=DIM,
            )
        return t

    def _peek_text(self, step: dict | None) -> Text:
        if not step:
            return Text("\n  ↑↓ move  ·  enter open  ·  / filter  ·  d diff\n", style=DIM)
        stype = step.get("type", "unknown")
        glyph = STEP_GLYPH.get(stype, "·")
        t = Text()
        t.append("\n  ")
        t.append(f"{glyph} ", style=ACCENT)
        t.append(f"{STEP_LABEL.get(stype, stype)}  ", style=MUTED)
        t.append(_preview_text(step, 88), style=FG)
        t.append("\n")
        return t

    # ── lifecycle ────────────────────────────────────────────────────────────

    def on_mount(self) -> None:
        tt = self.query_one("#steps-table", DataTable)
        tt.add_columns(" ", "type", "summary", "", "")
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
                self.query_one("#session-list", ListView).index = i
                self._load_steps(s)
                break
        self.push_screen(DiffScreen(sid, self.store))

    def _load_sessions(self) -> None:
        self.sessions = self.store.list_sessions()
        lv = self.query_one("#session-list", ListView)
        lv.clear()
        for s in self.sessions:
            lv.append(self._session_row(s))

        self.query_one("#chrome", Static).update(self._chrome_text())

        if self.sessions:
            self.selected_session_id = self.sessions[0].get("id")
            self._load_steps(self.sessions[0])
        else:
            self.steps = []
            self.visible_steps = []
            self.selected_session_id = None
            self.query_one("#head", Static).update(self._head_text())
            self.query_one("#meta", Static).update(self._meta_text(None))
            self.query_one("#peek", Static).update(self._peek_text(None))
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

        self.query_one("#meta", Static).update(self._meta_text(meta))
        self.query_one("#filter", Input).value = ""
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

        sid = _short_id(self.selected_session_id or "", "ses_")
        session = next((s for s in self.sessions if s.get("id") == self.selected_session_id), None)
        self.query_one("#head", Static).update(
            self._head_text(sid=sid, count=len(visible), session=session)
        )

        for step in visible:
            stype = step.get("type", "unknown")
            glyph = STEP_GLYPH.get(stype, "·")
            label = STEP_LABEL.get(stype, stype)
            ts = step.get("ts", 0)

            if stype == "tool":
                name = str(step.get("name", "?"))
                body = _strip(step.get("content") or "").strip().replace("\n", " ")
                summary = name if not body else f"{name}  {body[:64]}{'…' if len(body) > 64 else ''}"
            else:
                raw = _strip(step.get("content") or "").strip().replace("\n", " ")
                summary = raw[:96] + ("…" if len(raw) > 96 else "") if raw else "—"

            n_files = _count_file_changes(step)
            files = str(n_files) if n_files else ""
            when = _rel(ts) if ts else ""

            tt.add_row(
                Text(glyph, style=MUTED),
                Text(label, style=MUTED),
                Text(summary, style=FG),
                Text(files, style=DIM),
                Text(when, style=DIM),
            )

        self.query_one("#peek", Static).update(
            self._peek_text(visible[0] if visible else None)
        )

    # ── events ───────────────────────────────────────────────────────────────

    @on(Input.Changed, "#filter")
    def on_filter_changed(self, event: Input.Changed) -> None:
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
            self.query_one("#peek", Static).update(self._peek_text(self.visible_steps[row]))

    @on(DataTable.RowSelected, "#steps-table")
    def on_step_selected(self, event: DataTable.RowSelected) -> None:
        row = event.cursor_row
        if row is not None and 0 <= row < len(self.visible_steps):
            self.push_screen(StepDetail(self.visible_steps[row], self.agent))

    def action_focus_steps(self) -> None:
        self.query_one("#steps-table", DataTable).focus()

    def action_focus_search(self) -> None:
        self.query_one("#filter", Input).focus()

    def action_focus_sessions(self) -> None:
        self.query_one("#session-list", ListView).focus()

    def action_refresh(self) -> None:
        self._load_sessions()
        self.notify("refreshed", timeout=1)

    def action_diff(self) -> None:
        if self.selected_session_id:
            self.push_screen(DiffScreen(self.selected_session_id, self.store))


class DiffOnlyApp(App):
    TITLE = "mach diff"
    CSS = _APP_CSS
    BINDINGS = [Binding("q,escape", "quit", "quit")]

    def __init__(self, store: SessionStore, session_id: str) -> None:
        super().__init__()
        self.store = store
        self.session_id = session_id

    def on_mount(self) -> None:
        self.push_screen(DiffScreen(self.session_id, self.store))


def run_tui(store: SessionStore) -> None:
    MachApp(store).run()
