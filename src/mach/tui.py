"""
mach.tui — Informative execution ledger dashboard.

OpenCode-inspired multi-zone layout:
  sessions rail | timeline | context sidebar
Quiet monochrome + mint accent. Dense, keyboard-first.
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

# ── palette (quiet + mint) ───────────────────────────────────────────────────

FG = "#e8e6e3"
MUTED = "#8a8680"
DIM = "#4a4844"
FAINT = "#2e2c28"
ACCENT = "#a8e6cf"
BG = "#0c0c0b"
SURFACE = "#121211"
PANEL = "#161614"
BORDER = "#2a2826"
DANGER = "#e8a0a0"
OK = "#a8e6cf"
WARN = "#e8d5a0"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\\033\[[0-9;]*[A-Za-z]")

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
    if d < 86400 * 14:
        return f"{d // 86400}d"
    return _time.strftime("%b %d", _time.localtime(ts))


def _abs_ts(ts: int) -> str:
    if not ts:
        return "—"
    return _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(ts))


def _clock(ts: int) -> str:
    if not ts:
        return "—"
    return _time.strftime("%H:%M:%S", _time.localtime(ts))


def _coalesce(steps: list[dict]) -> list[dict]:
    out: list[dict] = []
    for step in steps:
        stype = step.get("type", "unknown")
        tool = step.get("tool")
        content = step.get("content", "")
        base = dict(
            id=step.get("id"),
            ts=step.get("ts", 0),
            type=stype,
            file_changes=step.get("file_changes"),
            risk_flags=step.get("risk_flags"),
            risk_level=step.get("risk_level"),
            commit_hash=step.get("commit_hash"),
            parent_step_id=step.get("parent_step_id"),
        )
        if tool:
            out.append(
                {
                    **base,
                    "type": "tool",
                    "name": tool.get("name"),
                    "category": tool.get("category", "exec"),
                    "content": _strip(tool.get("content") or ""),
                    "count": 1,
                }
            )
            continue
        if stype == "reasoning" and out and out[-1]["type"] == "reasoning":
            out[-1]["content"] = (out[-1].get("content") or "") + (content or "")
            out[-1].update(id=step.get("id"), ts=step.get("ts", 0))
        else:
            out.append({**base, "content": _strip(content or "")})
    return out


def _short_id(value: str, prefix: str, size: int = 8) -> str:
    raw = str(value or "")
    if raw.startswith(prefix):
        raw = raw[len(prefix) :]
    return raw[:size]


def _short_commit(value: str | None) -> str:
    return (value or "—")[:7]


def _count_files(step: dict[str, Any]) -> int:
    return len(step.get("file_changes") or [])


def _session_status(session: dict[str, Any]) -> str:
    return session.get("status") or ("active" if not session.get("ended_at") else "ended")


def _duration(session: dict[str, Any]) -> str:
    start = session.get("started_at") or 0
    end = session.get("ended_at") or int(_time.time())
    if not start:
        return "—"
    d = max(0, int(end) - int(start))
    if d < 60:
        return f"{d}s"
    if d < 3600:
        return f"{d // 60}m {d % 60}s"
    return f"{d // 3600}h {(d % 3600) // 60}m"


def _preview(step: dict[str, Any], width: int = 100) -> str:
    if step.get("type") == "tool":
        bits = [step.get("name", "?"), str(step.get("content") or "").strip().replace("\n", " ")]
        value = "  ".join(b for b in bits if b)
    else:
        value = str(step.get("content") or "").strip().replace("\n", " ")
    value = _strip(value)
    if not value:
        return "—"
    return value if len(value) <= width else value[: width - 1] + "…"


def _files_index(steps: list[dict]) -> list[tuple[str, str, int, int]]:
    """path -> (last_action, added, removed)"""
    acc: dict[str, list] = {}
    for step in steps:
        for ch in step.get("file_changes") or []:
            if not isinstance(ch, dict):
                continue
            path = str(ch.get("file_path") or "")
            if not path:
                continue
            entry = acc.setdefault(path, ["write", 0, 0])
            entry[0] = str(ch.get("action") or entry[0])
            entry[1] += int(ch.get("lines_added") or 0)
            entry[2] += int(ch.get("lines_removed") or 0)
    return sorted(((p, a, ad, rm) for p, (a, ad, rm) in acc.items()), key=lambda x: x[0])


def _tool_index(steps: list[dict]) -> list[tuple[str, int]]:
    c: Counter[str] = Counter()
    for step in steps:
        if step.get("type") == "tool":
            c[str(step.get("name") or "?")] += int(step.get("count") or 1)
    return c.most_common(12)


_APP_CSS = f"""
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

#status {{
    dock: top;
    height: 1;
    background: $panel;
    border-bottom: solid $border;
    padding: 0 1;
}}

#workspace {{
    height: 1fr;
}}

/* left: sessions */
#rail {{
    width: 34;
    min-width: 30;
    max-width: 40;
    height: 1fr;
    background: $surface;
    border-right: solid $border;
}}
.sec {{
    height: 1;
    padding: 0 1;
    color: $dim;
    background: $bg;
    border-bottom: solid $border;
}}
ListView {{
    height: 1fr;
    background: transparent;
    border: none;
    padding: 0;
    scrollbar-size: 1 1;
    scrollbar-background: $surface;
    scrollbar-color: $border;
}}
ListView:focus {{ border: none; }}
ListItem {{
    height: 4;
    padding: 0 1;
    background: transparent;
    color: $muted;
    border-bottom: solid $border;
}}
ListItem.--highlight {{
    background: $panel;
    color: $text;
    border-left: outer $accent;
}}
ListItem:hover {{ background: $panel; }}

#session-stats {{
    height: 7;
    background: $bg;
    border-top: solid $border;
    padding: 0 1;
    color: $muted;
}}

/* center: timeline */
#center {{
    width: 1fr;
    height: 1fr;
    background: $bg;
}}
#timeline-bar {{
    height: 1;
    padding: 0 1;
    color: $dim;
    background: $panel;
    border-bottom: solid $border;
}}
#filter {{
    height: 1;
    background: $surface;
    border: none;
    border-bottom: solid $border;
    padding: 0 1;
    color: $text;
}}
#filter:focus {{ background: $panel; }}
#filter > .input--placeholder {{ color: $dim; }}

DataTable {{
    height: 1fr;
    background: transparent;
    padding: 0;
    scrollbar-size: 1 1;
    scrollbar-background: $bg;
    scrollbar-color: $border;
}}
DataTable > .datatable--header {{
    background: $surface;
    color: $dim;
    text-style: none;
}}
DataTable > .datatable--cursor {{
    background: $panel;
    color: $text;
}}
DataTable > .datatable--hover {{ background: $surface; }}
DataTable > .datatable--even-row {{ background: transparent; }}
DataTable > .datatable--odd-row {{ background: transparent; }}

#peek {{
    height: 4;
    background: $surface;
    border-top: solid $border;
    padding: 0 1;
    color: $muted;
}}

/* right: context */
#ctx {{
    width: 36;
    min-width: 30;
    max-width: 44;
    height: 1fr;
    background: $surface;
    border-left: solid $border;
}}
#ctx-scroll {{
    height: 1fr;
    padding: 0 1 1 1;
    scrollbar-size: 1 1;
    scrollbar-color: $border;
}}

Footer {{
    background: $panel;
    border-top: solid $border;
    color: $dim;
    height: 1;
}}
Footer > .footer--key {{
    background: transparent;
    color: $accent;
    text-style: none;
}}
Footer > .footer--description {{ color: $dim; }}

/* modal */
StepDetail {{
    align: center middle;
    background: #000000 60%;
}}
#modal {{
    width: 88%;
    height: 84%;
    background: $surface;
    border: solid $border;
}}
#modal-h {{
    height: auto;
    padding: 1 2;
    border-bottom: solid $border;
}}
#modal-b {{
    height: 1fr;
    padding: 1 2;
    scrollbar-color: $border;
}}
#modal-f {{
    height: 1;
    padding: 0 2;
    border-top: solid $border;
    color: $dim;
}}

/* diff */
DiffScreen {{ background: $bg; }}
#diff-h {{
    height: 1;
    padding: 0 1;
    background: $panel;
    border-bottom: solid $border;
    color: $muted;
}}
#diff-split {{ height: 1fr; }}
#diff-l {{
    width: 40%;
    height: 1fr;
    background: $surface;
    border-right: solid $border;
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
    background: $panel;
}}
"""


# ══════════════════════════════════════════════════════════
#  Step detail modal
# ══════════════════════════════════════════════════════════


class StepDetail(ModalScreen[None]):
    BINDINGS = [Binding("escape,q", "dismiss", "close", show=True)]

    def __init__(self, step: dict, agent: str, session: dict | None = None) -> None:
        super().__init__()
        self.step = step
        self.agent = agent
        self.session = session or {}

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
            h.append(_short_id(str(s.get("id", "")), "step_", 14), style=MUTED)
            h.append("  ·  ", style=DIM)
            h.append(self.agent, style=MUTED)
            if ts:
                h.append(f"  ·  {_abs_ts(ts)}", style=DIM)
            if s.get("commit_hash"):
                h.append(f"  ·  {_short_commit(s.get('commit_hash'))}", style=DIM)
            yield Static(h, id="modal-h")

            with VerticalScroll(id="modal-b"):
                # meta block
                m = Text()
                m.append("session  ", style=DIM)
                m.append(_short_id(str(self.session.get("id", "")), "ses_", 10), style=MUTED)
                m.append("  parent  ", style=DIM)
                parent = s.get("parent_step_id")
                m.append(_short_id(str(parent), "step_", 10) if parent else "—", style=MUTED)
                risk = s.get("risk_level") or "none"
                m.append("  risk  ", style=DIM)
                m.append(str(risk), style=DANGER if risk != "none" else MUTED)
                yield Static(m)
                yield Static(Text(""))

                if stype == "tool":
                    t = Text()
                    t.append(str(s.get("name", "?")), style=f"bold {FG}")
                    t.append(f"  ·  {s.get('category', 'exec')}", style=MUTED)
                    if s.get("count", 1) > 1:
                        t.append(f"  ×{s['count']}", style=MUTED)
                    yield Static(t)
                    yield Static(Text(""))

                content = _strip(s.get("content") or "").strip()
                yield Static(Text("content", style=DIM))
                if content:
                    for line in content.splitlines():
                        yield Static(Text(line, style=FG))
                else:
                    yield Static(Text("—", style=DIM))

                fc = s.get("file_changes") or []
                if fc:
                    yield Static(Text(""))
                    yield Static(Text(f"files  {len(fc)}", style=DIM))
                    for ch in fc:
                        action = ch.get("action", "write")
                        fp = ch.get("file_path", "?")
                        added = ch.get("lines_added", 0) or 0
                        removed = ch.get("lines_removed", 0) or 0
                        line = Text()
                        line.append(f"{action:<6} ", style=MUTED)
                        line.append(str(fp), style=FG)
                        if added or removed:
                            line.append(f"  +{added}", style=OK if added else DIM)
                            line.append(f" -{removed}", style=DANGER if removed else DIM)
                        yield Static(line)
                        for hunk in ch.get("hunks", []):
                            hs, he = hunk.get("from", 0), hunk.get("to", 0)
                            yield Static(
                                Text(
                                    f"       @@ -{hs},{max(1, he - hs + 1)} +{hs},{max(1, he - hs + 1)} @@",
                                    style=DIM,
                                )
                            )

                flags = s.get("risk_flags") or []
                if flags:
                    yield Static(Text(""))
                    yield Static(Text(f"risk flags  {len(flags)}", style=DIM))
                    for flag in flags:
                        if not isinstance(flag, dict):
                            continue
                        rl = Text()
                        rl.append(f"{str(flag.get('severity', '?')):<8} ", style=DANGER)
                        rl.append(str(flag.get("rule_id", "?")), style=FG)
                        if flag.get("explanation"):
                            rl.append(f"  {flag['explanation']}", style=MUTED)
                        yield Static(rl)

            yield Static(Text("esc close", style=DIM), id="modal-f")


# ══════════════════════════════════════════════════════════
#  Diff screen
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
        h = Text()
        h.append("diff", style=MUTED)
        h.append(f"  {_short_id(str(meta.get('id', '')), 'ses_')}", style=FG)
        h.append(f"  {meta.get('agent', '?')}", style=MUTED)
        h.append(f"  {meta.get('branch', '?')}", style=DIM)
        h.append(f"  ·  {self.diff_data.get('files_changed', 0)} files", style=DIM)
        h.append(f"  +{self.diff_data.get('total_added', 0)}", style=OK)
        h.append(f" -{self.diff_data.get('total_removed', 0)}", style=DANGER)
        yield Static(h, id="diff-h")

        with Horizontal(id="diff-split"):
            with Vertical(id="diff-l"):
                yield Static(Text("steps with file changes", style=DIM), classes="sec")
                yield DataTable(
                    id="diff-files-table",
                    cursor_type="row",
                    zebra_stripes=False,
                    show_cursor=True,
                )
            with VerticalScroll(id="diff-r"):
                yield Static(Text("select a step", style=DIM), id="diff-detail-content")

        yield Static(Text("q close  ·  ↑↓ navigate  ·  r refresh", style=DIM), id="diff-f")

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
            label = str(files[0].get("file_path") or "?") if len(files) == 1 else (
                f"{len(files)} files" if files else "·"
            )
            added, removed = step.get("lines_added", 0), step.get("lines_removed", 0)
            delta = " ".join(
                p for p in ([f"+{added}"] if added else []) + ([f"-{removed}"] if removed else [])
            ) or "·"
            table.add_row(
                _short_id(str(step.get("step_id", "")), "step_", 8),
                str(tool),
                label,
                delta,
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
        d.append(f"  {step.get('step_type', '?')}", style=FG)
        if step.get("tool_name"):
            d.append(f"  {step['tool_name']}", style=MUTED)
        if step.get("ts"):
            d.append(f"  {_abs_ts(int(step['ts']))}", style=DIM)
        d.append("\n\n")
        d.append(f"{step.get('files_changed', len(files))} files  ", style=MUTED)
        d.append(f"+{step.get('lines_added', 0)} ", style=OK)
        d.append(f"-{step.get('lines_removed', 0)}", style=DANGER)
        d.append(f"  {step.get('diff_source', 'recorded')}", style=DIM)
        d.append("\n")

        content = _strip(step.get("content") or "").strip()
        if content:
            d.append("\n")
            preview = content if len(content) <= 700 else content[:700].rstrip() + "\n…"
            for line in preview.splitlines():
                d.append(f"{line}\n", style=MUTED)

        if not files:
            d.append("\nno file details", style=DIM)
            return d

        d.append("\n")
        for f in files:
            d.append("\n")
            d.append(str(f.get("file_path", "?")), style=FG)
            d.append(f"  {f.get('action', '?')}  ", style=MUTED)
            d.append(f"+{f.get('lines_added', 0)} ", style=OK)
            d.append(f"-{f.get('lines_removed', 0)}\n", style=DANGER)
            hunks = f.get("hunks") or []
            git_diff = f.get("git_diff") or ""
            if hunks:
                for h in hunks:
                    d.append(f"  @@ -{h.get('from', 0)} +{h.get('to', 0)} @@\n", style=DIM)
            elif git_diff:
                for i, line in enumerate(git_diff.splitlines()[:160]):
                    style = MUTED
                    if line.startswith("+") and not line.startswith("+++"):
                        style = OK
                    elif line.startswith("-") and not line.startswith("---"):
                        style = DANGER
                    elif line.startswith("@@"):
                        style = DIM
                    d.append(line + "\n", style=style)
                if len(git_diff.splitlines()) > 160:
                    d.append("…\n", style=DIM)
            else:
                d.append("  —\n", style=DIM)
        return d

    def action_quit(self) -> None:
        if self.app.__class__.__name__ == "DiffOnlyApp":
            self.app.exit()
            return
        self.app.pop_screen()

    def action_refresh(self) -> None:
        self._load_diff_data()
        self._populate_steps_table()

    @on(DataTable.RowHighlighted, "#diff-files-table")
    def on_row_hi(self, event: DataTable.RowHighlighted) -> None:
        self._update_detail(event.cursor_row)

    @on(DataTable.RowSelected, "#diff-files-table")
    def on_row_sel(self, event: DataTable.RowSelected) -> None:
        row = getattr(event, "cursor_row", None)
        if row is not None:
            self._update_detail(row)

    def _update_detail(self, row: int | None) -> None:
        if row is not None:
            self.query_one("#diff-detail-content", Static).update(self._detail_text(row))


# ══════════════════════════════════════════════════════════
#  Main app — 3-pane OpenCode-style
# ══════════════════════════════════════════════════════════


class MachApp(App):
    TITLE = "mach"
    CSS = _APP_CSS

    BINDINGS = [
        Binding("q", "quit", "quit"),
        Binding("tab", "cycle_focus", "pane"),
        Binding("right", "focus_steps", "steps", show=False),
        Binding("left,escape", "focus_sessions", "sessions", show=False),
        Binding("slash", "focus_search", "filter"),
        Binding("r", "refresh", "refresh"),
        Binding("d", "diff", "diff"),
        Binding("i", "focus_ctx", "info", show=False),
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
        self.selected_session: dict | None = None
        self.selected_session_id: str | None = None
        self.selected_step: dict | None = None
        self._initial_diff_session_id = initial_diff_session_id
        self._focus_order = ("session-list", "steps-table", "filter")
        self._focus_idx = 0

    def compose(self) -> ComposeResult:
        yield Static(id="status")
        with Horizontal(id="workspace"):
            # LEFT — sessions
            with Vertical(id="rail"):
                yield Static(Text("sessions", style=DIM), classes="sec")
                yield ListView(id="session-list")
                yield Static(id="session-stats")

            # CENTER — timeline
            with Vertical(id="center"):
                yield Static(id="timeline-bar")
                yield Input(placeholder="filter steps…  /", id="filter")
                yield DataTable(
                    id="steps-table",
                    cursor_type="row",
                    zebra_stripes=False,
                    show_cursor=True,
                )
                yield Static(id="peek")

            # RIGHT — context / inspector
            with Vertical(id="ctx"):
                yield Static(Text("context", style=DIM), classes="sec")
                with VerticalScroll(id="ctx-scroll"):
                    yield Static(id="ctx-body")

        yield Footer()

    # ── rendering helpers ────────────────────────────────────────────────────

    def _status_text(self) -> Text:
        repo = str(self.store.paths.repo_root)
        short_repo = self.store.paths.repo_root.name or "."
        active = sum(1 for s in self.sessions if _session_status(s) == "active")
        agents = sorted({str(s.get("agent", "?")) for s in self.sessions}) if self.sessions else []
        t = Text()
        t.append("mach", style=f"bold {FG}")
        t.append("  ", style="")
        t.append(short_repo, style=MUTED)
        t.append("  ·  ", style=DIM)
        t.append(f"{len(self.sessions)} sess", style=MUTED)
        if active:
            t.append(f"  ·  {active} live", style=ACCENT)
        if agents:
            t.append(f"  ·  {', '.join(agents[:4])}", style=DIM)
            if len(agents) > 4:
                t.append(f"+{len(agents) - 4}", style=DIM)
        # path on the right-ish via trailing spaces is hard; keep compact
        if len(repo) < 48:
            t.append(f"  ·  {repo}", style=FAINT)
        return t

    def _session_item(self, s: dict) -> ListItem:
        sid = _short_id(str(s.get("id", "")), "ses_", 8)
        agent = str(s.get("agent", "?"))
        branch = str(s.get("branch", "?"))
        live = _session_status(s) == "active"
        n = s.get("step_count", 0) or 0
        risk = s.get("risk_count", 0) or 0
        started = s.get("started_at", 0) or 0
        task = str(s.get("task_desc") or "").strip().replace("\n", " ")
        if len(task) > 36:
            task = task[:35] + "…"

        l1 = Text()
        l1.append("● " if live else "○ ", style=ACCENT if live else DIM)
        l1.append(sid, style=FG if live else MUTED)
        l1.append(f"  {agent}", style=MUTED)
        l1.append(f"  {_rel(started)}", style=DIM)

        l2 = Text()
        l2.append(f"  {branch}", style=DIM)
        l2.append(f"  ·  {n} steps", style=DIM)
        l2.append(f"  ·  {_duration(s)}", style=DIM)
        if risk:
            l2.append(f"  ·  !{risk}", style=DANGER)
        if live:
            l2.append("  ·  live", style=ACCENT)

        l3 = Text()
        l3.append(f"  {task or '—'}", style=FAINT)

        return ListItem(Static(Text.assemble(l1, "\n", l2, "\n", l3)))

    def _session_stats_text(self, s: dict | None) -> Text:
        if not s:
            return Text("\nselect a session", style=DIM)
        t = Text()
        t.append("\n")
        t.append(_session_status(s), style=ACCENT if _session_status(s) == "active" else MUTED)
        t.append(f"  {s.get('agent', '?')}\n", style=MUTED)
        t.append("branch  ", style=DIM)
        t.append(f"{s.get('branch', '?')}\n", style=MUTED)
        t.append("commit  ", style=DIM)
        t.append(_short_commit(s.get("pre_commit")), style=MUTED)
        t.append(" → ", style=DIM)
        post = _short_commit(s.get("post_commit"))
        t.append(f"{post if post != '—' else '…'}\n", style=MUTED)
        t.append("span    ", style=DIM)
        t.append(f"{_duration(s)}\n", style=MUTED)
        t.append("id      ", style=DIM)
        t.append(_short_id(str(s.get("id", "")), "ses_", 12), style=FAINT)
        return t

    def _timeline_bar_text(self) -> Text:
        t = Text()
        t.append("timeline", style=DIM)
        if self.selected_session_id:
            t.append(f"  {_short_id(self.selected_session_id, 'ses_')}", style=MUTED)
        if self.visible_steps or self.steps:
            c = Counter(s.get("type", "?") for s in self.steps)
            tools = sum(1 for s in self.steps if s.get("type") == "tool")
            files = sum(_count_files(s) for s in self.steps)
            risks = sum(len(s.get("risk_flags") or []) for s in self.steps)
            t.append(f"  ·  {len(self.visible_steps)}/{len(self.steps)}", style=DIM)
            t.append(f"  ·  {c.get('input', 0)} in", style=MUTED)
            t.append(f"  {tools} tools", style=MUTED)
            t.append(f"  {c.get('output', 0)} out", style=MUTED)
            t.append(f"  {files} file-ev", style=MUTED)
            if risks:
                t.append(f"  !{risks}", style=DANGER)
        return t

    def _peek_text(self) -> Text:
        step = self.selected_step
        if not step:
            return Text(
                "\n  ↑↓ navigate  ·  enter open  ·  / filter  ·  d diff  ·  tab panes\n",
                style=DIM,
            )
        stype = step.get("type", "unknown")
        glyph = STEP_GLYPH.get(stype, "·")
        t = Text()
        t.append("\n  ")
        t.append(f"{glyph} ", style=ACCENT)
        t.append(f"{STEP_LABEL.get(stype, stype)}  ", style=MUTED)
        t.append(_preview(step, 100), style=FG)
        t.append("\n  ")
        meta_bits = []
        if stype == "tool":
            meta_bits.append(str(step.get("category") or "exec"))
            if step.get("name"):
                meta_bits.append(str(step["name"]))
        n_files = _count_files(step)
        if n_files:
            meta_bits.append(f"{n_files} files")
        risk = step.get("risk_level") or "none"
        if risk != "none":
            meta_bits.append(f"risk:{risk}")
        if step.get("ts"):
            meta_bits.append(_clock(int(step["ts"])))
        t.append("  ·  ".join(meta_bits) if meta_bits else "—", style=DIM)
        t.append("\n")
        return t

    def _ctx_text(self) -> Text:
        s = self.selected_session
        t = Text()
        if not s:
            t.append("\nno session selected\n", style=DIM)
            return t

        # session block
        t.append("\n")
        t.append("SESSION\n", style=DIM)
        t.append(str(s.get("id", "")), style=MUTED)
        t.append("\n")
        t.append(f"{s.get('agent', '?')}", style=FG)
        t.append(f"  ·  {_session_status(s)}\n", style=ACCENT if _session_status(s) == "active" else MUTED)
        if s.get("task_desc"):
            task = str(s["task_desc"]).strip().replace("\n", " ")
            if len(task) > 120:
                task = task[:119] + "…"
            t.append(f"{task}\n", style=MUTED)
        t.append("\n")
        t.append("branch   ", style=DIM)
        t.append(f"{s.get('branch', '?')}\n", style=MUTED)
        t.append("pre      ", style=DIM)
        t.append(f"{_short_commit(s.get('pre_commit'))}\n", style=MUTED)
        t.append("post     ", style=DIM)
        t.append(f"{_short_commit(s.get('post_commit'))}\n", style=MUTED)
        t.append("started  ", style=DIM)
        t.append(f"{_abs_ts(int(s.get('started_at') or 0))}\n", style=MUTED)
        if s.get("ended_at"):
            t.append("ended    ", style=DIM)
            t.append(f"{_abs_ts(int(s['ended_at']))}\n", style=MUTED)
        t.append("duration ", style=DIM)
        t.append(f"{_duration(s)}\n", style=MUTED)
        t.append("steps    ", style=DIM)
        t.append(f"{s.get('step_count') or len(self.steps)}\n", style=MUTED)
        risk_n = s.get("risk_count") or sum(len(x.get("risk_flags") or []) for x in self.steps)
        t.append("risk     ", style=DIM)
        t.append(f"{risk_n}\n", style=DANGER if risk_n else MUTED)
        if s.get("forked_from"):
            t.append("forked   ", style=DIM)
            t.append(f"{_short_id(str(s['forked_from']), 'ses_', 12)}\n", style=MUTED)

        # tools
        tools = _tool_index(self.steps)
        t.append("\n")
        t.append("TOOLS\n", style=DIM)
        if not tools:
            t.append("—\n", style=FAINT)
        else:
            for name, n in tools:
                t.append(f"{n:>3}  ", style=MUTED)
                t.append(f"{name}\n", style=FG)

        # files
        files = _files_index(self.steps)
        t.append("\n")
        t.append(f"FILES  {len(files)}\n", style=DIM)
        if not files:
            t.append("—\n", style=FAINT)
        else:
            for path, action, added, removed in files[:24]:
                short = path if len(path) <= 28 else "…" + path[-27:]
                t.append(f"{action[:1]} ", style=DIM)
                t.append(f"{short}", style=FG)
                if added or removed:
                    t.append(f"  +{added}", style=OK if added else DIM)
                    t.append(f"-{removed}", style=DANGER if removed else DIM)
                t.append("\n")
            if len(files) > 24:
                t.append(f"… +{len(files) - 24} more\n", style=DIM)

        # selected step
        step = self.selected_step
        t.append("\n")
        t.append("FOCUS\n", style=DIM)
        if not step:
            t.append("no step focused\n", style=FAINT)
        else:
            stype = step.get("type", "?")
            t.append(f"{STEP_LABEL.get(stype, stype)}  ", style=ACCENT)
            t.append(f"{_short_id(str(step.get('id', '')), 'step_', 10)}\n", style=MUTED)
            if stype == "tool":
                t.append(f"{step.get('name', '?')}  ", style=FG)
                t.append(f"{step.get('category', '')}\n", style=DIM)
            body = _preview(step, 140)
            t.append(f"{body}\n", style=MUTED)
            if _count_files(step):
                t.append(f"{_count_files(step)} file change(s)\n", style=DIM)
            if (step.get("risk_level") or "none") != "none":
                t.append(f"risk {step.get('risk_level')}\n", style=DANGER)

        t.append("\n")
        t.append("keys  / filter  d diff  r refresh  q quit\n", style=FAINT)
        return t

    # ── data load ────────────────────────────────────────────────────────────

    def on_mount(self) -> None:
        tt = self.query_one("#steps-table", DataTable)
        tt.add_columns(" ", "type", "summary", "files", "time")
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

    def _refresh_chrome(self) -> None:
        self.query_one("#status", Static).update(self._status_text())
        self.query_one("#timeline-bar", Static).update(self._timeline_bar_text())
        self.query_one("#session-stats", Static).update(self._session_stats_text(self.selected_session))
        self.query_one("#peek", Static).update(self._peek_text())
        self.query_one("#ctx-body", Static).update(self._ctx_text())

    def _load_sessions(self) -> None:
        self.sessions = self.store.list_sessions()
        lv = self.query_one("#session-list", ListView)
        lv.clear()
        for s in self.sessions:
            lv.append(self._session_item(s))

        if self.sessions:
            self._load_steps(self.sessions[0])
        else:
            self.steps = []
            self.visible_steps = []
            self.selected_session = None
            self.selected_session_id = None
            self.selected_step = None
            self.query_one("#steps-table", DataTable).clear()
            self._refresh_chrome()

    def _load_steps(self, session: dict) -> None:
        sid = session.get("id", "")
        self.agent = str(session.get("agent", "unknown"))
        self.selected_session_id = sid
        self.selected_session = session
        try:
            data = self.store.show_session(sid)
            self.steps = _coalesce(data["steps"])
            self.selected_session = data.get("meta") or session
        except Exception:
            self.steps = []
        self.query_one("#filter", Input).value = ""
        self.selected_step = None
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
                # also match file paths
                paths = " ".join(
                    str(fc.get("file_path") or "")
                    for fc in (step.get("file_changes") or [])
                    if isinstance(fc, dict)
                ).lower()
                if q not in paths:
                    continue
            visible.append(step)
        self.visible_steps = visible

        for step in visible:
            stype = step.get("type", "unknown")
            glyph = STEP_GLYPH.get(stype, "·")
            label = STEP_LABEL.get(stype, stype)
            ts = step.get("ts", 0)
            if stype == "tool":
                name = str(step.get("name", "?"))
                body = _strip(step.get("content") or "").strip().replace("\n", " ")
                summary = name if not body else f"{name}  {body[:56]}{'…' if len(body) > 56 else ''}"
            else:
                raw = _strip(step.get("content") or "").strip().replace("\n", " ")
                summary = raw[:88] + ("…" if len(raw) > 88 else "") if raw else "—"

            n_files = _count_files(step)
            risk = step.get("risk_level") or "none"
            files_cell = Text(str(n_files) if n_files else "", style=MUTED if n_files else FAINT)
            if risk != "none":
                files_cell = Text(f"{n_files or '·'} !", style=DANGER)

            tt.add_row(
                Text(glyph, style=MUTED),
                Text(label, style=MUTED),
                Text(summary, style=FG),
                files_cell,
                Text(_clock(ts) if ts else "", style=DIM),
            )

        self.selected_step = visible[0] if visible else None
        self._refresh_chrome()

    # ── events ───────────────────────────────────────────────────────────────

    @on(Input.Changed, "#filter")
    def on_filter(self, event: Input.Changed) -> None:
        self._populate_table(event.value)

    @on(ListView.Highlighted, "#session-list")
    def on_session_hi(self, event: ListView.Highlighted) -> None:
        if event.item is None:
            return
        idx = event.list_view.index
        if idx is not None and 0 <= idx < len(self.sessions):
            self._load_steps(self.sessions[idx])

    @on(ListView.Selected, "#session-list")
    def on_session_sel(self, event: ListView.Selected) -> None:
        self.query_one("#steps-table", DataTable).focus()

    @on(DataTable.RowHighlighted, "#steps-table")
    def on_step_hi(self, event: DataTable.RowHighlighted) -> None:
        row = event.cursor_row
        if row is not None and 0 <= row < len(self.visible_steps):
            self.selected_step = self.visible_steps[row]
            self.query_one("#peek", Static).update(self._peek_text())
            self.query_one("#ctx-body", Static).update(self._ctx_text())

    @on(DataTable.RowSelected, "#steps-table")
    def on_step_sel(self, event: DataTable.RowSelected) -> None:
        row = event.cursor_row
        if row is not None and 0 <= row < len(self.visible_steps):
            self.push_screen(
                StepDetail(self.visible_steps[row], self.agent, self.selected_session)
            )

    def action_cycle_focus(self) -> None:
        order = ["session-list", "steps-table", "filter"]
        # find current
        focused = self.focused
        cur = 0
        if focused is not None:
            for i, name in enumerate(order):
                try:
                    if self.query_one(f"#{name}") is focused or (
                        hasattr(focused, "id") and focused.id == name
                    ):
                        cur = i
                        break
                except Exception:
                    pass
        nxt = order[(cur + 1) % len(order)]
        self.query_one(f"#{nxt}").focus()

    def action_focus_steps(self) -> None:
        self.query_one("#steps-table", DataTable).focus()

    def action_focus_search(self) -> None:
        self.query_one("#filter", Input).focus()

    def action_focus_sessions(self) -> None:
        self.query_one("#session-list", ListView).focus()

    def action_focus_ctx(self) -> None:
        self.query_one("#ctx-scroll", VerticalScroll).focus()

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
