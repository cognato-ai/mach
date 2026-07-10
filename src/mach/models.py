from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Literal, Optional, Any

StepType = Literal["input", "reasoning", "tool", "output", "system_action"]
RiskLevel = Literal["none", "low", "medium", "high", "critical"]


@dataclass
class MachPaths:
    repo_root: Path
    mach_dir: Path
    config_path: Path
    head_path: Path
    sessions_dir: Path
    pack_dir: Path
    blobs_dir: Path
    lock_path: Path
    agent_sessions_path: Path
    inbox_dir: Path
    ingest_state_path: Path
    tracker_state_path: Path
    tracker_pid_path: Path
    tracker_log_path: Path
    tracker_lock_path: Path
    tracked_repo_path: Path

@dataclass
class FileChange:
    action: Literal["write", "read", "delete"]
    file_path: str
    lines_added: int = 0
    lines_removed: int = 0
    hunks: list[dict] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FileChange":
        return cls(
            action=data.get("action", "write"),
            file_path=data.get("file_path", "?"),
            lines_added=data.get("lines_added", 0),
            lines_removed=data.get("lines_removed", 0),
            hunks=data.get("hunks", [])
        )

@dataclass
class ToolCall:
    name: str
    category: str = "exec"
    content_hash: Optional[str] = None
    content: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ToolCall":
        return cls(
            name=data.get("name", ""),
            category=data.get("category", "exec"),
            content_hash=data.get("content_hash"),
            content=data.get("content")
        )

@dataclass
class Step:
    id: str
    session_id: str
    step_num: int
    ts: int
    type: StepType
    content_hash: Optional[str] = None
    content: Optional[str] = None
    caused_by: list[str] = field(default_factory=list)
    risk_level: RiskLevel = "none"
    # Local RiskEngine + optional enterprise/backend annotations (source=external).
    risk_flags: list[dict[str, Any]] = field(default_factory=list)
    tool: Optional[ToolCall] = None
    file_changes: list[FileChange] = field(default_factory=list)
    commit_hash: Optional[str] = None
    parent_step_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict, dropping nulls to keep JSONL clean."""
        d = asdict(self)
        if self.tool is None:
            d.pop("tool", None)
        if not self.file_changes:
            d.pop("file_changes", None)
        if not self.risk_flags:
            d.pop("risk_flags", None)
        return {k: v for k, v in d.items() if v is not None}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Step":
        tool_data = data.get("tool")
        tool = ToolCall.from_dict(tool_data) if tool_data else None
        
        fc_data = data.get("file_changes", [])
        file_changes = [FileChange.from_dict(fc) for fc in fc_data] if fc_data else []

        return cls(
            id=data.get("id", ""),
            session_id=data.get("session_id", ""),
            step_num=data.get("step_num", 0),
            ts=data.get("ts", 0),
            type=data.get("type", "output"),
            content_hash=data.get("content_hash"),
            content=data.get("content"),
            caused_by=data.get("caused_by", []),
            risk_level=data.get("risk_level", "none"),
            risk_flags=list(data.get("risk_flags") or []),
            tool=tool,
            file_changes=file_changes,
            commit_hash=data.get("commit_hash"),
            parent_step_id=data.get("parent_step_id")
        )

@dataclass
class GitRemoteInfo:
    """Static facts about the Git remote — read from local git config."""
    url: Optional[str] = None
    repository_name: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GitRemoteInfo":
        return cls(
            url=data.get("url"),
            repository_name=data.get("repository_name"),
        )


@dataclass
class MachSyncState:
    """Mutable push-sync state written by `mach push` / `mach pull`."""
    last_push_id: Optional[str] = None
    last_pushed_at: Optional[str] = None
    last_pushed_ts: int = 0
    last_pushed_step_id: Optional[str] = None
    pushed_root: Optional[str] = None
    server_session_id: Optional[str] = None
    server_root_before: Optional[str] = None
    server_root_after: Optional[str] = None
    blobs_received: Optional[int] = None
    steps_received: Optional[int] = None
    last_pulled_at: Optional[str] = None
    last_pulled_ts: int = 0
    last_pulled_step_id: Optional[str] = None

    def reset(self) -> "MachSyncState":
        """Return a clean MachSyncState (forces a full re-push next time)."""
        return MachSyncState()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MachSyncState":
        return cls(
            last_push_id=data.get("last_push_id"),
            last_pushed_at=data.get("last_pushed_at"),
            last_pushed_ts=data.get("last_pushed_ts", 0),
            last_pushed_step_id=data.get("last_pushed_step_id"),
            pushed_root=data.get("pushed_root"),
            server_session_id=data.get("server_session_id"),
            server_root_before=data.get("server_root_before"),
            server_root_after=data.get("server_root_after"),
            blobs_received=data.get("blobs_received"),
            steps_received=data.get("steps_received"),
            last_pulled_at=data.get("last_pulled_at"),
            last_pulled_ts=data.get("last_pulled_ts", 0),
            last_pulled_step_id=data.get("last_pulled_step_id"),
        )


@dataclass
class RemoteInfo:
    """Top-level remote block stored in meta.json under the 'remote' key."""
    git: GitRemoteInfo = field(default_factory=GitRemoteInfo)
    mach: MachSyncState = field(default_factory=MachSyncState)

    def to_dict(self) -> dict[str, Any]:
        return {
            "git": self.git.to_dict(),
            "mach": self.mach.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RemoteInfo":
        # Backward-compat: old flat format had url / last_pushed_step_id at top level.
        if "url" in data or "last_pushed_step_id" in data:
            return cls(
                git=GitRemoteInfo.from_dict(data),
                mach=MachSyncState.from_dict(data),
            )
        return cls(
            git=GitRemoteInfo.from_dict(data.get("git") or {}),
            mach=MachSyncState.from_dict(data.get("mach") or {}),
        )


@dataclass
class RepositoryDetails:
    id: str
    name: str
    remote_url: Optional[str] = None
    provider: Optional[str] = None
    external_id: Optional[str] = None
    default_branch: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    is_active: bool = True
    created: Optional[str] = None
    modified: Optional[str] = None
    pulled_at: Optional[int] = None
    api_base_url: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RepositoryDetails":
        raw = data.get("repo") if isinstance(data.get("repo"), dict) else data
        return cls(
            id=str(raw.get("id") or ""),
            name=str(raw.get("name") or data.get("repository_name") or ""),
            remote_url=raw.get("remote_url"),
            provider=raw.get("provider"),
            external_id=raw.get("external_id"),
            default_branch=raw.get("default_branch"),
            metadata=raw.get("metadata") or {},
            is_active=raw.get("is_active", True),
            created=raw.get("created"),
            modified=raw.get("modified"),
            pulled_at=data.get("pulled_at") or data.get("fetched_at"),
            api_base_url=data.get("api_base_url"),
        )


@dataclass
class SessionMeta:
    id: str
    agent: str
    agent_session_id: Optional[str] = None
    task_desc: Optional[str] = None
    started_at: int = 0
    ended_at: Optional[int] = None
    status: Literal["active", "ended"] = "active"
    branch: str = "main"
    pre_commit: Optional[str] = None
    post_commit: Optional[str] = None
    step_count: int = 0
    risk_count: int = 0
    forked_from: Optional[str] = None
    remote: Optional[RemoteInfo] = None
    head_step_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if self.remote is None:
            d.pop("remote", None)
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionMeta":
        return cls(
            id=data.get("id", ""),
            agent=data.get("agent", "unknown"),
            agent_session_id=data.get("agent_session_id"),
            task_desc=data.get("task_desc"),
            started_at=data.get("started_at", 0),
            ended_at=data.get("ended_at"),
            status=data.get("status", "active"),
            branch=data.get("branch", "main"),
            pre_commit=data.get("pre_commit"),
            post_commit=data.get("post_commit"),
            step_count=data.get("step_count", 0),
            risk_count=data.get("risk_count", 0),
            forked_from=data.get("forked_from"),
            remote=RemoteInfo.from_dict(data["remote"]) if "remote" in data else None,
            head_step_id=data.get("head_step_id")
        )

@dataclass
class PushSessionMeta:
    id: str
    agent: str
    agent_session_id: Optional[str]
    task_desc: Optional[str]
    started_at: int
    ended_at: Optional[int]
    status: str
    branch: str
    pre_commit: Optional[str]
    post_commit: Optional[str]
    step_count: int
    risk_count: int
    forked_from: Optional[str] = None
    head_step_id: Optional[str] = None



@dataclass
class PushMerkle:
    root: Optional[str]
    steps: int


@dataclass
class PushMetadata:
    cli_version: str
    pushed_from: str


@dataclass
class PushPayload:
    repository: Optional[str]
    meta: PushSessionMeta
    merkle: PushMerkle
    blobs: dict[str, str]
    steps: list[dict[str, Any]]
    client_root: Optional[str]
    metadata: PushMetadata

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PushSessionResponse:
    id: Optional[str]
    local_session_id: Optional[str]
    synced_at: Optional[str]
    merkle_root: Optional[str]
    merkle_steps: Optional[int]
    step_count: Optional[int]
    risk_count: Optional[int]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PushSessionResponse":
        return cls(
            id=data.get("id"),
            local_session_id=data.get("local_session_id"),
            synced_at=data.get("synced_at"),
            merkle_root=data.get("merkle_root"),
            merkle_steps=data.get("merkle_steps"),
            step_count=data.get("step_count"),
            risk_count=data.get("risk_count"),
        )


@dataclass
class PushResponse:
    id: Optional[str]
    session: PushSessionResponse
    client_root: Optional[str]
    server_root_before: Optional[str]
    server_root_after: Optional[str]
    blobs_received: int
    steps_received: int
    created: Optional[str]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PushResponse":
        return cls(
            id=data.get("id"),
            session=PushSessionResponse.from_dict(data.get("session", {})),
            client_root=data.get("client_root"),
            server_root_before=data.get("server_root_before"),
            server_root_after=data.get("server_root_after"),
            blobs_received=int(data.get("blobs_received") or 0),
            steps_received=int(data.get("steps_received") or 0),
            created=data.get("created"),
        )


@dataclass
class PullSessionDetails:
    id: Optional[str]
    session_id: str
    agent_name: Optional[str]
    agent_session_id: Optional[str]
    task_desc: Optional[str]
    repository: RepositoryDetails
    branch: Optional[str]
    pre_commit: Optional[str]
    post_commit: Optional[str]
    status: Optional[str]
    started_at: Optional[int]
    ended_at: Optional[int]
    merkle_root: Optional[str]
    merkle_steps: Optional[int]
    step_count: int
    risk_count: int
    last_step: dict[str, Any] | None = None
    synced_at: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    is_active: bool = True
    created: Optional[str] = None
    modified: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PullSessionDetails":
        return cls(
            id=data.get("id"),
            session_id=str(data.get("session_id") or ""),
            agent_name=data.get("agent_name"),
            agent_session_id=data.get("agent_session_id"),
            task_desc=data.get("task_desc"),
            repository=RepositoryDetails.from_dict(data.get("repository") or {}),
            branch=data.get("branch"),
            pre_commit=data.get("pre_commit"),
            post_commit=data.get("post_commit"),
            status=data.get("status"),
            started_at=data.get("started_at"),
            ended_at=data.get("ended_at"),
            merkle_root=data.get("merkle_root"),
            merkle_steps=data.get("merkle_steps"),
            step_count=int(data.get("step_count") or 0),
            risk_count=int(data.get("risk_count") or 0),
            last_step=data.get("last_step"),
            synced_at=data.get("synced_at"),
            metadata=data.get("metadata") or {},
            is_active=data.get("is_active", True),
            created=data.get("created"),
            modified=data.get("modified"),
        )


@dataclass
class PullStepRecord:
    """A single step record as returned by the server's step-listing endpoint."""
    server_id: Optional[str]    # server's own UUID for this push record
    mach_id: Optional[str]      # original local step id (e.g. step_xxxx)
    step_num: Optional[int]
    step_type: Optional[str]
    created: Optional[str]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PullStepRecord":
        return cls(
            server_id=data.get("id"),
            mach_id=data.get("mach_id") or data.get("step_id"),
            step_num=data.get("step_num"),
            step_type=data.get("type"),
            created=data.get("created"),
        )


@dataclass
class PullStepsPage:
    """One page from GET /api/v1/sessions/{id}/steps (Django DRF pagination)."""
    results: list[PullStepRecord]
    count: Optional[int]    # total steps on server across all pages
    pages: Optional[int]    # total number of pages
    page: int               # current page (1-indexed)
    size: int               # page size requested
    has_next: bool          # True when a next page exists

    @classmethod
    def from_dict(cls, data: dict[str, Any], page: int, size: int) -> "PullStepsPage":
        raw = data.get("results") or []
        return cls(
            results=[PullStepRecord.from_dict(r) for r in raw],
            count=data.get("count"),
            pages=data.get("pages"),
            page=page,
            size=size,
            has_next=bool(data.get("next")),
        )
