from __future__ import annotations

"""Immutable domain contracts shared by qykw modules."""

from dataclasses import dataclass
from enum import Enum
from typing import Mapping


class RepositoryPermission(Enum):
    NONE = "none"
    READ = "read"
    TRIAGE = "triage"
    WRITE = "write"
    MAINTAIN = "maintain"
    ADMIN = "admin"


class Severity(Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"


class DiffSide(str, Enum):
    LEFT = "LEFT"
    RIGHT = "RIGHT"


class ContextChunkKind(str, Enum):
    DIFF = "DIFF"
    TRIAGE = "TRIAGE"
    REFERENCE = "REFERENCE"


class CommandName(Enum):
    HELP = "帮助"
    ANALYZE = "分析"
    PLAN = "计划"
    REVIEW = "审查"
    REREVIEW = "复审"
    STATUS = "状态"
    SUMMARY = "总结"
    FIX = "修复"
    IMPLEMENT = "实现"
    STOP = "停止"


class CommandMode(Enum):
    READ_ONLY = "read_only"
    CHANGE = "change"


class RunStage(Enum):
    ACCEPTED = "accepted"
    ACKNOWLEDGED = "acknowledged"
    COLLECTING = "collecting"
    ANALYZING = "analyzing"
    VALIDATING = "validating"
    TESTING = "testing"
    PUBLISHING = "publishing"
    COMPLETED = "completed"


class RunStatus(Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELED = "canceled"
    STALE = "stale"


class CommentKind(Enum):
    ISSUE = "issue"
    REVIEW = "review"


class InferenceErrorCode(str, Enum):
    CAPABILITY_UNSUPPORTED = "capability_unsupported"
    INVALID_CONFIG = "invalid_config"
    DNS_ERROR = "dns_error"
    TLS_ERROR = "tls_error"
    CONNECTION_ERROR = "connection_error"
    READ_TIMEOUT = "read_timeout"
    RATE_LIMITED = "rate_limited"
    RESPONSE_INTERRUPTED = "response_interrupted"
    INVALID_RESPONSE = "invalid_response"
    DEADLINE_EXCEEDED = "deadline_exceeded"


class CommandRoute(Enum):
    DETERMINISTIC = "deterministic"
    ADVISORY = "advisory"
    REVIEW = "review"
    CHANGE = "change"


@dataclass(frozen=True)
class Actor:
    login: str
    permission: RepositoryPermission


@dataclass(frozen=True)
class AuthenticatedUser:
    login: str
    database_id: int


@dataclass(frozen=True)
class CommandRequest:
    name: CommandName
    argument: str
    mode: CommandMode


@dataclass(frozen=True)
class EventContext:
    repository_id: int
    repository: str
    pr_number: int
    event_name: str
    action: str
    actor_login: str
    source_head_hint: str | None
    idempotency_key: str
    command: CommandRequest
    trigger_comment_id: int | None = None
    trigger_comment_kind: CommentKind | None = None


@dataclass(frozen=True)
class RunContext:
    run_id: str
    idempotency_key: str
    repository_id: int
    repository: str
    pr_number: int
    event_name: str
    event_action: str
    source_repository: str
    source_head_sha: str
    target_base_sha: str
    target_base_ref: str
    command: CommandRequest
    trigger_actor: str
    trigger_comment_id: int | None = None
    trigger_comment_kind: CommentKind | None = None


@dataclass(frozen=True)
class PullRef:
    number: int
    state: str
    draft: bool
    source_repository: str
    source_head_sha: str
    target_repository: str
    target_base_sha: str
    target_base_ref: str


@dataclass(frozen=True)
class ChangedFile:
    path: str
    previous_path: str | None
    status: str
    base_sha: str | None
    head_sha: str | None
    base_mode: str | None
    head_mode: str | None
    base_content: str | None
    head_content: str | None
    patch: str | None
    binary: bool
    generated: bool
    additions: int
    deletions: int


@dataclass(frozen=True)
class RepositoryFile:
    path: str
    ref: str
    sha: str
    content: str
    purpose: str


@dataclass(frozen=True)
class CheckRun:
    name: str
    status: str
    conclusion: str | None


@dataclass(frozen=True)
class PullSnapshot:
    number: int
    state: str
    draft: bool
    source_repository: str
    source_head_sha: str
    target_repository: str
    target_base_sha: str
    target_base_ref: str
    title: str
    body: str
    changed_files: tuple[ChangedFile, ...]
    trusted_rules: tuple[RepositoryFile, ...]
    related_files: tuple[RepositoryFile, ...]
    checks: tuple[CheckRun, ...]


@dataclass(frozen=True)
class FindingCandidate:
    path: str
    line: int
    side: DiffSide
    severity: Severity
    failure_path: str
    impact: str
    evidence: str
    suggestion: str
    verification: str


@dataclass(frozen=True)
class Finding(FindingCandidate):
    fingerprint: str


@dataclass(frozen=True)
class CoverageReport:
    total_files: int
    reviewed_files: int
    total_hunks: int
    reviewed_hunks: int
    omissions: tuple[str, ...]
    explains_every_file: bool


@dataclass(frozen=True)
class ReviewResult:
    conclusion: str
    findings: tuple[Finding, ...]
    coverage: CoverageReport
    validation_notes: tuple[str, ...]
    limitations: tuple[str, ...]


@dataclass(frozen=True)
class RunRecord:
    context: RunContext
    stage: RunStage
    status: RunStatus
    prompt_version: str
    summary_comment_id: int | None
    initial_review: bool
    coverage: CoverageReport | None
    warning_codes: tuple[str, ...]
    error_code: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class CancelRecord:
    pr_number: int
    target_run_id: str
    stop_comment_id: int
    actor_login: str
    created_at: str


@dataclass(frozen=True)
class RunOutcome:
    run_id: str
    status: RunStatus
    stage: RunStage
    error_code: str | None


@dataclass(frozen=True)
class ProviderCapabilities:
    context_window: int
    max_output_tokens: int
    structured_output: bool
    supported_reasoning_profiles: frozenset[str]


@dataclass(frozen=True)
class InferenceUsage:
    input_tokens: int | None
    output_tokens: int | None


@dataclass(frozen=True)
class InferenceRequest:
    run_id: str
    stage: RunStage
    prompt_version: str
    reasoning_profile: str
    deadline_seconds: int
    max_output_tokens: int
    idempotency_key: str
    schema_name: str
    schema: Mapping[str, object]
    payload: Mapping[str, object]


@dataclass(frozen=True)
class InferenceResponse:
    request_id: str | None
    value: Mapping[str, object]
    usage: InferenceUsage


@dataclass(frozen=True)
class InferenceFailure:
    code: InferenceErrorCode
    retryable: bool
    request_may_have_been_accepted: bool


class InferenceError(RuntimeError):
    """A safe inference failure without request or response contents."""

    def __init__(self, failure: InferenceFailure) -> None:
        super().__init__(failure.code.value)
        self.failure = failure


@dataclass(frozen=True)
class AuthorizationDecision:
    allowed: bool
    reason: str


@dataclass(frozen=True)
class TriggerDecision:
    run: bool
    reason: str
    idempotency_key: str


@dataclass(frozen=True)
class TriggerRef:
    kind: str
    node_id: int


@dataclass(frozen=True)
class ReactionResult:
    warning_code: str | None


@dataclass(frozen=True)
class IssueComment:
    comment_id: int
    author_login: str
    body: str
    updated_at: str


@dataclass(frozen=True)
class ReviewComment(IssueComment):
    path: str
    line: int
    side: DiffSide


@dataclass(frozen=True)
class InlineComment:
    path: str
    line: int
    side: DiffSide
    body: str
    fingerprint: str


@dataclass(frozen=True)
class ChangedLine:
    path: str
    line: int
    side: DiffSide


@dataclass(frozen=True)
class DiffHunk:
    path: str
    previous_path: str | None
    header: str
    changed_lines: tuple[ChangedLine, ...]
    text: str


@dataclass(frozen=True)
class FileManifest:
    paths: tuple[str, ...]
    risk_order: tuple[str, ...]


@dataclass(frozen=True)
class ContextChunk:
    chunk_id: str
    paths: tuple[str, ...]
    text: str
    estimated_tokens: int
    kind: ContextChunkKind = ContextChunkKind.DIFF


@dataclass(frozen=True)
class ContextPlan:
    repository: str
    pr_number: int
    source_head_sha: str
    run_id: str
    manifest: FileManifest
    chunks: tuple[ContextChunk, ...]
    coverage: CoverageReport
    commentable_lines: frozenset[ChangedLine]
    max_chunk_tokens: int
    effective_input_budget_tokens: int


@dataclass(frozen=True)
class AdvisoryResult:
    title: str
    body: str
    evidence: tuple[str, ...]
    limitations: tuple[str, ...]


@dataclass(frozen=True)
class PublishResult:
    status: RunStatus
    summary_comment_id: int
    summary_body: str
    review_id: int | None
    published_fingerprints: tuple[str, ...]
    warning_codes: tuple[str, ...]
