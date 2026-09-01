# qykw 独立审查内核实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将现有单文件审查器迁移为完全独立、可测试、首次 PR 仅自动触发一次的 qykw 审查内核。

**Architecture:** 使用 `tools/qykw/` 内的领域类型、确定性命令与权限策略、类型化 GitHub 网关、通用推理适配器、分片上下文、状态存储和发布器。GitHub Actions 只运行默认分支中的可信代码；第一阶段不启用代码修改能力。

**Tech Stack:** Python 3.11 标准库、`unittest`、GitHub Actions、TOML、coverage.py 7.16.0（仅开发与 CI）。

**Spec:** `docs/superpowers/specs/2026-09-02-qykw-independent-agent-design.md`

## Global Constraints

- qykw 是启元开物独立机器人；公开评论、日志和文档不得展示推理后台或模型名称。
- 每个非 Draft PR 仅在 `opened` 自动审查一次；Draft 仅在第一次 `ready_for_review` 审查；`synchronize` 不触发。
- 无命令的精确 `@qykw` 进入只读分析；未知或含糊命令不得提升为修改模式。
- 第一阶段解析 `修复`、`实现`，但返回 `capability_disabled`，不进行任何代码写入。
- 不执行 PR 代码，不直推默认分支，不 Approve、Merge、Delete 或 Force Push。
- 审查 workflow 的 authorize/publish/control job 只持有仓库限定、仅评论与 Reaction 所需的 `QYKW_REVIEW_TOKEN`；使用前必须确认其 authenticated login 恰为 `qykw`。analyze job 只持有推理凭据。两类凭据不得在同一 job，且整个 workflow 不得持有代码发布令牌。
- 只从默认分支读取 `AGENTS.md` 和 `.github/qykw.toml`；PR 内修改不能扩大权限。
- 运行时保持 Python 标准库依赖；coverage.py 只出现在开发依赖与 CI 门禁中。
- 本地 Windows 命令使用 `py -3`；CI 命令使用 `python`。
- 每个任务严格执行 RED → GREEN → 全量相关测试 → 独立提交，不 amend、不跳过 hooks。
- 所有提交使用仓库现有 `xyh` 身份，不添加任何共同作者或 AI/工具署名。
- 当前方案不需要域名、独立服务器、外部控制台或自建 webhook。

---

## File Map

| File | Responsibility |
| --- | --- |
| `tools/qykw/domain.py` | 不可变领域类型、枚举和协议 |
| `tools/qykw/config.py` | 环境变量与默认分支 TOML 的严格解析 |
| `tools/qykw/commands.py` | 精确提及和中文命令解析 |
| `tools/qykw/policy.py` | 命令授权、能力和安全边界 |
| `tools/qykw/triggers.py` | GitHub 事件标准化、首次触发和运行编号 |
| `tools/qykw/prompts.py` | qykw 身份、任务模板和 JSON Schema |
| `tools/qykw/provider.py` | 通用推理请求、最高推理档、网络和错误分类 |
| `tools/qykw/github.py` | GitHub 分页读取、Reaction、状态和 COMMENT review |
| `tools/qykw/context.py` | 文件清单、hunk、预算、分片和覆盖率 |
| `tools/qykw/advisory.py` | 分析/计划的只读结构化回答；帮助/状态/总结的确定性渲染 |
| `tools/qykw/review.py` | 分诊、深审、反证、严重度、行号与去重 |
| `tools/qykw/state.py` | qykw 自有状态标记、幂等和软取消 |
| `tools/qykw/publish.py` | 公开文本净化、总结和行内评论 |
| `tools/qykw/runner.py` | 依赖注入和运行状态机 |
| `tools/qykw/__main__.py` | Actions 入口和安全退出信息 |

## Shared Domain Contract

Task 1 必须一次性定义下列跨模块类型；后续任务只扩展行为，不另造同义字段：

```python
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

@dataclass(frozen=True)
class Actor:
    login: str
    permission: RepositoryPermission

@dataclass(frozen=True)
class AuthenticatedUser:
    login: str
    database_id: int

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
```

推理协议同样属于 Task 1 的共享契约，不得留给适配器临时造型：

```python
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
```

`InferenceError` 只包装 `InferenceFailure`，不得携带原始响应、请求正文或 Secret。`InferenceResponse` 只保存已按严格 Schema 校验的结构化值和安全用量元数据；`ProviderCapabilities` 必须显式证明上下文窗口、输出上限、结构化输出与 `maximum` 支持情况。

其余跨模块值对象固定为：

```python
class CommandRoute(Enum):
    DETERMINISTIC = "deterministic"
    ADVISORY = "advisory"
    REVIEW = "review"
    CHANGE = "change"

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
class DiffHunk:
    path: str
    previous_path: str | None
    header: str
    changed_lines: tuple[ChangedLine, ...]
    text: str

@dataclass(frozen=True)
class ChangedLine:
    path: str
    line: int
    side: DiffSide

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
```

### Task 1: 建立领域类型与严格配置

**Files:**
- Create: `tools/qykw/__init__.py`
- Create: `tools/qykw/domain.py`
- Create: `tools/qykw/config.py`
- Create: `tests/test_qykw_config.py`

**Interfaces:**
- Consumes: Python 3.11 `dataclasses`、`enum`、`typing.Protocol`、`tomllib`。
- Produces: `QykwConfig`、`CommandRequest`、`RunContext`、`RunRecord`、`Finding`、`ReviewResult`，供后续所有任务使用。

- [ ] **Step 1: 写配置 RED 测试**

```python
class TestQykwConfig(unittest.TestCase):
    def test_rejects_unknown_and_secret_fields(self) -> None:
        with self.assertRaises(ConfigError):
            parse_qykw_config({"version": 1, "api_key": "forbidden"})

    def test_accepts_confirmed_defaults(self) -> None:
        config = parse_qykw_config({"version": 1})
        self.assertTrue(config.review.auto_initial)
        self.assertFalse(config.review.auto_on_synchronize)
        self.assertEqual(config.language, "zh-CN")
```

- [ ] **Step 2: 运行 RED**

Run: `py -3 -m unittest tests.test_qykw_config -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'tools.qykw'`.

- [ ] **Step 3: 实现领域契约**

`domain.py` 首行使用 `from __future__ import annotations`，再一次性定义本计划“Shared Domain Contract”中的全部类型，避免跨段落前向引用失效。核心枚举与命令类型为：

```python
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

@dataclass(frozen=True)
class CommandRequest:
    name: CommandName
    argument: str
    mode: CommandMode

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
```

在 `config.py` 实现：

```python
@dataclass(frozen=True)
class AuthorizationConfig:
    code_writers: tuple[str, ...]

@dataclass(frozen=True)
class ReviewConfig:
    auto_initial: bool
    auto_on_synchronize: bool
    max_findings: int
    run_timeout_seconds: int

@dataclass(frozen=True)
class ContextConfig:
    safety_reserve_ratio: float
    max_chunk_ratio: float

@dataclass(frozen=True)
class CommandsConfig:
    enabled_commands: tuple[CommandName, ...]

@dataclass(frozen=True)
class VerificationConfig:
    required_checks: tuple[str, ...]
    profiles: tuple[str, ...]

@dataclass(frozen=True)
class QykwConfig:
    version: int
    language: str
    authorization: AuthorizationConfig
    review: ReviewConfig
    context: ContextConfig
    commands: CommandsConfig
    verification: VerificationConfig

def parse_qykw_config(data: Mapping[str, object]) -> QykwConfig: ...
def load_qykw_config(path: Path) -> QykwConfig: ...
```

只接受规格中声明的分组和键；校验 `version == 1`、比例在 `(0, 1)`、授权用户非空、命令来自固定集合。后续代码统一使用 `config.authorization.code_writers`、`config.review.*`、`config.context.*`、`config.commands.enabled_commands` 和 `config.verification.*`，不得再提供同义扁平属性。

- [ ] **Step 4: 运行 GREEN 与全量基线**

Run: `py -3 -m unittest tests.test_qykw_config -v`

Expected: PASS.

Run: `py -3 -m unittest discover -s tests -v`

Expected: 现有 298 项和新配置测试全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add tools/qykw/__init__.py tools/qykw/domain.py tools/qykw/config.py tests/test_qykw_config.py
git commit -m "feat: define qykw review domain"
```

### Task 2: 实现精确提及、命令与授权策略

**Files:**
- Create: `tools/qykw/commands.py`
- Create: `tools/qykw/policy.py`
- Create: `tests/test_qykw_commands.py`
- Create: `tests/test_qykw_policy.py`

**Interfaces:**
- Consumes: `CommandRequest`、`CommandMode`、`QykwConfig`。
- Produces: `parse_command()`、`authorize_command()`、`AuthorizationDecision`、`CommandRouter.resolve()`。

- [ ] **Step 1: 写解析和权限 RED 测试**

```python
class TestCommandParsing(unittest.TestCase):
    def test_exact_first_effective_mention_triggers(self) -> None:
        command = parse_command("@qykw 审查 安全")
        self.assertEqual(command, CommandRequest(CommandName.REVIEW, "安全", CommandMode.READ_ONLY))

    def test_quotes_code_and_similar_login_do_not_trigger(self) -> None:
        for body in (
            "> @qykw 审查",
            "`@qykw 审查`",
            "@qykw-old 审查",
            "@qy\u200bkw 审查",
            "＠qykw 审查",
            "mail@qykw.example",
        ):
            self.assertIsNone(parse_command(body))

    def test_ambiguous_write_request_stays_read_only(self) -> None:
        command = parse_command("@qykw 帮我改一下")
        self.assertEqual(command.mode, CommandMode.READ_ONLY)
        self.assertEqual(command.name, CommandName.ANALYZE)

    def test_only_first_effective_paragraph_can_trigger(self) -> None:
        self.assertIsNone(parse_command("普通正文\n\n@qykw 审查"))

    def test_every_command_has_one_route(self) -> None:
        self.assertEqual(set(CommandName), set(CommandRouter.ROUTES))
```

```python
class TestAuthorization(unittest.TestCase):
    def test_change_requires_configured_writer(self) -> None:
        decision = authorize_command(
            CommandRequest(CommandName.FIX, "QY-01", CommandMode.CHANGE),
            Actor("member", RepositoryPermission.WRITE),
            config_with_writers("xyh202131"),
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "change_actor_not_allowed")
```

- [ ] **Step 2: 运行 RED**

Run: `py -3 -m unittest tests.test_qykw_commands tests.test_qykw_policy -v`

Expected: FAIL because parser and policy are missing.

- [ ] **Step 3: 实现确定性解析和权限矩阵**

```python
def parse_command(body: str, bot_login: str = "qykw") -> CommandRequest | None: ...

def authorize_command(
    command: CommandRequest,
    actor: Actor,
    config: QykwConfig,
    *,
    run_trigger_actor: str | None = None,
) -> AuthorizationDecision: ...

class CommandRouter:
    ROUTES: Mapping[CommandName, CommandRoute]

    def resolve(self, command: CommandRequest) -> CommandRoute: ...
```

解析器跳过 HTML 注释、引用、围栏代码和行内代码；含零宽字符或全角 `＠` 的伪装提及不触发。使用账号边界而非子串匹配。路由表必须完整且不可由模型修改：帮助/状态/总结/停止走确定性 handler；分析/计划走只读 advisory；审查/复审走 review；修复/实现走 change。`修复`、`实现`为修改模式；第一阶段授权后仍返回 `capability_disabled`。`停止`只允许任务触发者或 `code_writers`。

- [ ] **Step 4: 运行 GREEN**

Run: `py -3 -m unittest tests.test_qykw_commands tests.test_qykw_policy -v`

Expected: PASS for大小写、全角符号、邮箱、引用、代码块、相似账号和权限矩阵。

- [ ] **Step 5: 提交**

```bash
git add tools/qykw/commands.py tools/qykw/policy.py tests/test_qykw_commands.py tests/test_qykw_policy.py
git commit -m "feat: add deterministic qykw command policy"
```

### Task 3: 标准化事件并限制首次自动审查

**Files:**
- Create: `tools/qykw/triggers.py`
- Create: `tests/test_qykw_triggers.py`

**Interfaces:**
- Consumes: GitHub 事件字典、`RunRecord`、`QykwConfig`、网关解析得到的 `PullRef`。
- Produces: 预鉴权 `EventContext`、`normalize_event()`、`decide_trigger()`、`build_run_context()`、`make_run_id()`。

- [ ] **Step 1: 写首次触发 RED 测试**

```python
def test_opened_non_draft_runs_once(self) -> None:
    event = pull_event("opened", draft=False, repository_id=7, head_sha="abc123")
    first = decide_trigger(event, existing_run=None, initial_review_completed=False, config=config())
    replay = decide_trigger(event, existing_run=run_for(first), initial_review_completed=False, config=config())
    self.assertTrue(first.run)
    self.assertFalse(replay.run)

def test_synchronize_never_auto_reviews(self) -> None:
    event = pull_event("synchronize", draft=False, repository_id=7, head_sha="def456")
    self.assertFalse(decide_trigger(event, existing_run=None, initial_review_completed=False, config=config()).run)
```

还要覆盖 Draft opened、第一次/重复 Ready、已成功/从未成功 reopened 和显式复审。
`issue_comment` 事件缺少 `issue.pull_request` 时必须 no-op，避免普通 Issue 评论误触发 PR 机器人。
评论事件夹具把 `GITHUB_SHA` 设为默认分支提交、PR Head 设为另一 SHA；断言 `EventContext.source_head_hint is None`，authorize 只使用 `get_pull_ref()` 的 PR Head，并把原 event/action 写入 `RunContext`。已关闭/合并 PR 或 ref 读取失败不得创建运行或添加 Reaction。

- [ ] **Step 2: 运行 RED**

Run: `py -3 -m unittest tests.test_qykw_triggers -v`

Expected: FAIL because trigger functions do not exist.

- [ ] **Step 3: 实现纯函数事件决策**

```python
def normalize_event(
    event_name: str,
    payload: Mapping[str, object],
    *,
    repository_id: int,
    repository: str,
    workflow_run_id: int | None = None,
) -> EventContext: ...

def build_run_context(event: EventContext, pull: PullRef) -> RunContext: ...

def decide_trigger(
    event: EventContext,
    *,
    existing_run: RunRecord | None,
    initial_review_completed: bool,
    config: QykwConfig,
) -> TriggerDecision: ...
```

`EventContext` 只表示可由事件 payload 确定的预鉴权事实。`issue_comment`/`pull_request_review_comment` 的 Actions SHA 不得当成 PR Head；这两类事件令 `source_head_hint=None`。自动 PR 事件只把 payload 的 `pull_request.head.sha` 保存为 hint。解析、权限和事件幂等通过后，authorize 必须调用默认分支可信 `GitHubGateway.get_pull_ref(pr_number)` 取得当前 open PR 的 source repository/Head 与 target base SHA/ref，再由 `build_run_context()` 固定完整运行上下文；`RunContext` 必须保留原 `event_name/event_action`。普通 Issue、已关闭/已合并 PR、仓库不匹配或无法取得完整 ref 均 no-op，不能回退到 `GITHUB_SHA`。

普通 Actions 上下文不依赖 webhook delivery header。自动事件的幂等键固定为 `repository_id + PR + action + source_head_hint`；评论命令固定为 `repository_id + comment kind + comment ID`；手动触发固定为 `repository_id + workflow_run_id`。运行号由 PR 号和幂等键摘要生成，不使用随机全局状态或 `run_attempt`。评论运行的 Head 只来自 `PullRef`，并在后续快照与发布前再次核对。

审查与修改工作流使用同一跨 workflow 并发组 `qykw-<repository_id>-pr-<pr_number>`，固定 `cancel-in-progress: false` 与 `queue: max`。默认单 pending 会让后到的 no-op workflow 替换真正的修改任务；`queue: max` 因此是必需门禁。在该 FIFO 串行边界内执行 `find_by_idempotency_key → create`；重复事件返回现有运行，避免两个 workflow 同时穿透非原子的评论状态存储。`停止`是唯一例外：由独立 `qykw-control.yml` 处理，不加入主 PR 工作并发组，因此可在长任务运行时写入软取消标记；它使用自己的 `qykw-control-<repository_id>-comment-<comment_id>` 并发组、`cancel-in-progress: false` 与 `queue: max`，串行化同一 comment 的 created/edited/重放事件。审查/修改 workflow 对 `停止`均 no-op。

- [ ] **Step 4: 运行 GREEN 并提交**

Run: `py -3 -m unittest tests.test_qykw_triggers -v`

Expected: PASS.

```bash
git add tools/qykw/triggers.py tests/test_qykw_triggers.py
git commit -m "feat: gate initial qykw reviews"
```

### Task 4: 建立独立身份、Schema 与推理协议

**Files:**
- Create: `tools/qykw/prompts.py`
- Create: `tools/qykw/provider.py`
- Create: `tests/test_qykw_prompts.py`
- Create: `tests/test_qykw_provider.py`

**Interfaces:**
- Consumes: `RunContext`、文件清单、上下文块和候选问题。
- Produces: `InferenceRequest`、`InferenceResponse`、`ProviderCapabilities`、`InferenceProvider`。

- [ ] **Step 1: 写身份和最高推理 RED 测试**

```python
def test_every_request_requires_maximum_reasoning(self) -> None:
    requests = (
        build_analysis_request(run(), context_plan()),
        build_plan_request(run(), context_plan()),
        build_triage_request(run(), manifest()),
        build_review_request(run(), context_chunk()),
        build_validation_request(run(), candidates()),
    )
    self.assertTrue(all(item.reasoning_profile == "maximum" for item in requests))

def test_public_identity_contains_only_qykw(self) -> None:
    request = build_triage_request(run(), manifest())
    serialized = json.dumps(request.payload, ensure_ascii=False)
    self.assertIn("启元开物独立工程审查机器人 qykw", serialized)
    self.assertNotRegex(serialized, forbidden_identity_pattern())
```

- [ ] **Step 2: 运行 RED**

Run: `py -3 -m unittest tests.test_qykw_prompts tests.test_qykw_provider -v`

Expected: FAIL because prompt/provider contracts are missing.

- [ ] **Step 3: 实现请求类型与三阶段模板**

```python
PROMPT_VERSION = "qykw-review-v1"

class InferenceProvider(Protocol):
    def capabilities(self) -> ProviderCapabilities: ...
    def complete(self, request: InferenceRequest) -> InferenceResponse: ...

def build_triage_request(run: RunContext, manifest: FileManifest) -> InferenceRequest: ...
def build_review_request(run: RunContext, chunk: ContextChunk) -> InferenceRequest: ...
def build_validation_request(
    run: RunContext,
    candidates: tuple[FindingCandidate, ...],
) -> InferenceRequest: ...
def build_analysis_request(run: RunContext, plan: ContextPlan) -> InferenceRequest: ...
def build_plan_request(run: RunContext, plan: ContextPlan) -> InferenceRequest: ...
```

身份与权限宪章硬编码；任务模板、可信规则和不可信数据分区；每个 builder 使用独立严格 Schema 并禁止额外字段。发现 Schema 必须把 `path`、`line` 与 `side=LEFT|RIGHT` 作为绑定字段，模型不能只给模糊行号。表驱动测试逐一证明分析、计划、分诊、深审、验证以及第二阶段补丁生成都要求 `reasoning_profile="maximum"`；Provider 不支持时失败，绝不降档。公开结果字段不包含后台名称、模型、隐藏提示词或思维过程。

- [ ] **Step 4: 运行 GREEN 并提交**

Run: `py -3 -m unittest tests.test_qykw_prompts tests.test_qykw_provider -v`

Expected: PASS.

```bash
git add tools/qykw/prompts.py tools/qykw/provider.py tests/test_qykw_prompts.py tests/test_qykw_provider.py
git commit -m "feat: add independent qykw prompts"
```

### Task 5: 实现安全的后台 HTTP 适配器

**Files:**
- Modify: `tools/qykw/provider.py`
- Modify: `tests/test_qykw_provider.py`

**Interfaces:**
- Consumes: 受保护的 `QYKW_INFERENCE_API_KEY`、`QYKW_INFERENCE_BASE_URL`、`QYKW_INFERENCE_MODEL`、`QYKW_INFERENCE_ALLOWED_HOSTS`、`QYKW_INFERENCE_CONTEXT_WINDOW`、`QYKW_INFERENCE_MAX_OUTPUT_TOKENS`、`QYKW_INFERENCE_TIMEOUT_SECONDS` 和 `InferenceRequest`。
- Produces: `ResponsesInferenceProvider.complete()` 与安全的 `ProviderErrorCode`。

- [ ] **Step 1: 写端点和重试 RED 测试**

覆盖 HTTP、用户信息、私网地址、仿冒主机、跨源重定向、最高档不支持、读取超时、DNS、TLS、429 和可能已被接收后的连接中断。另用 sentinel API key、代码、完整评论和完整响应捕获日志，断言它们均不存在；日志字段白名单只允许运行号、阶段、请求号、耗时、调用次数、Token 用量和通用错误码。

```python
def test_cross_origin_redirect_is_rejected(self) -> None:
    provider = provider_with_redirect("https://allowed.example", "https://other.example")
    with self.assertRaisesRegex(ProviderError, "endpoint_redirect_rejected"):
        provider.complete(request())

def test_read_timeout_is_not_retried(self) -> None:
    transport = TimeoutTransport()
    with self.assertRaisesRegex(ProviderError, "read_timeout"):
        provider(transport).complete(request())
    self.assertEqual(transport.calls, 1)
```

- [ ] **Step 2: 运行 RED**

Run: `py -3 -m unittest tests.test_qykw_provider -v`

Expected: FAIL on endpoint validation and retry behavior.

- [ ] **Step 3: 实现适配器**

```python
class ResponsesInferenceProvider:
    @classmethod
    def from_env(cls) -> "ResponsesInferenceProvider": ...

    def capabilities(self) -> ProviderCapabilities: ...

    def complete(self, request: InferenceRequest) -> InferenceResponse: ...
```

只允许 HTTPS 固定主机；禁止跨源重定向。DNS、TLS 握手和明确发送前连接失败最多一次重试；读取超时、证书错误、响应中断不重试。429 仅在运行截止时间允许时等待。

- [ ] **Step 4: 迁移已有 16 项网络测试并运行 GREEN**

将旧网络分类断言迁入 `tests/test_qykw_provider.py`，错误文案改为通用 qykw 代码。对异常链、HTTP body、URL query/header 和日志参数执行同一净化断言，防止 Secret 或原始输入通过 exception repr 泄露。

Run: `py -3 -m unittest tests.test_qykw_provider -v`

Expected: PASS.

- [ ] **Step 5: 提交**

```bash
git add tools/qykw/provider.py tests/test_qykw_provider.py
git commit -m "feat: secure qykw inference requests"
```

### Task 6: 实现类型化 GitHub 网关和完整分页

**Files:**
- Create: `tools/qykw/github.py`
- Create: `tests/test_qykw_github.py`

**Interfaces:**
- Consumes: GitHub API URL、只读/评论令牌和 PR 编号。
- Produces: `PullSnapshot`、`IssueComment`、`ReviewComment`、`GitHubGateway`。

- [ ] **Step 1: 写分页、固定 Head 和 Reaction RED 测试**

```python
def test_reads_beyond_first_page(self) -> None:
    gateway = fake_gateway_with_pages(page1=100, page2=1)
    self.assertEqual(len(gateway.list_issue_comments(53)), 101)
    self.assertEqual(len(gateway.list_changed_files(53)), 101)

def test_reaction_failure_is_non_blocking_warning(self) -> None:
    result = gateway_with_failed_reaction().try_add_reaction(trigger())
    self.assertEqual(result.warning_code, "reaction_failed")

def test_review_token_must_authenticate_as_qykw(self) -> None:
    with self.assertRaisesRegex(GitHubError, "bot_identity_mismatch"):
        gateway_authenticated_as("someone-else").assert_bot_identity("qykw")

def test_deleted_and_renamed_files_keep_both_sides(self) -> None:
    deleted, renamed = gateway_with_delete_and_rename().list_changed_files(53)
    self.assertIsNone(deleted.head_content)
    self.assertEqual(renamed.previous_path, "old/name.py")
    self.assertIsNotNone(renamed.base_mode)
    self.assertIsNotNone(renamed.head_mode)
```

- [ ] **Step 2: 运行 RED**

Run: `py -3 -m unittest tests.test_qykw_github -v`

Expected: FAIL because gateway is missing.

- [ ] **Step 3: 实现网关白名单**

```python
class GitHubGateway(Protocol):
    def get_pull_ref(self, pr_number: int) -> PullRef: ...
    def get_pull_snapshot(self, pr_number: int) -> PullSnapshot: ...
    def get_head_sha(self, pr_number: int) -> str: ...
    def get_actor_permission(self, login: str) -> RepositoryPermission: ...
    def get_authenticated_user(self) -> AuthenticatedUser: ...
    def assert_bot_identity(self, expected_login: str = "qykw") -> AuthenticatedUser: ...
    def try_add_reaction(
        self, trigger: TriggerRef, content: str = "laugh"
    ) -> ReactionResult: ...
    def list_issue_comments(self, pr_number: int) -> tuple[IssueComment, ...]: ...
    def list_review_comments(self, pr_number: int) -> tuple[ReviewComment, ...]: ...
    def list_changed_files(self, pr_number: int) -> tuple[ChangedFile, ...]: ...
    def list_check_runs(self, head_sha: str) -> tuple[CheckRun, ...]: ...
    def get_file_at_ref(self, path: str, ref: str) -> RepositoryFile | None: ...
    def get_default_branch_rules(self) -> tuple[RepositoryFile, ...]: ...
    def create_issue_comment(self, pr_number: int, body: str) -> int: ...
    def update_issue_comment(self, comment_id: int, body: str) -> None: ...
    def create_review(
        self,
        pr_number: int,
        *,
        head_sha: str,
        body: str,
        comments: tuple[InlineComment, ...],
    ) -> int: ...
```

接口中不得出现批准、合并、删除、强推或设置方法。任何 Reaction/评论写入前调用 `get_authenticated_user()` 并要求 login 恰为 `qykw`；不匹配时在零写入状态下失败。分页遵守 `Link` 响应头，不固定为前 100 条。所有读取只访问配置的 GitHub API origin、目标仓库及 PR 明确的 source repository/ref；评论中的 URL、图片和重定向不得成为抓取目标。`get_pull_ref()` 是评论事件固定当前 PR Head/base 的唯一来源，拒绝非 open PR 和仓库不匹配。`get_pull_snapshot()` 必须与 `RunContext` 已固定的 source Head 与 target base SHA/ref 完全一致，再组合完整 changed-files 分页、base/head 文件内容、check runs、默认分支的 `AGENTS.md`/`.github/qykw.toml` 和控制器按引用关系请求的相关文件；任何缺失均显式进入快照/覆盖率原因，不回退读取 PR 内规则或 `GITHUB_SHA`。

- [ ] **Step 4: 运行 GREEN 并提交**

Run: `py -3 -m unittest tests.test_qykw_github -v`

Expected: PASS.

```bash
git add tools/qykw/github.py tests/test_qykw_github.py
git commit -m "feat: add typed qykw GitHub gateway"
```

### Task 7: 构建完整上下文计划与覆盖率

**Files:**
- Create: `tools/qykw/context.py`
- Create: `tests/test_qykw_context.py`

**Interfaces:**
- Consumes: `PullSnapshot`、后台上下文窗口和仓库预算。
- Produces: `ContextPlan`、`ContextChunk`、`CoverageReport`、可评论行映射。

- [ ] **Step 1: 写大 PR RED 测试**

```python
def test_late_high_risk_file_is_not_silently_lost(self) -> None:
    snapshot = snapshot_with_101_files(last_path="auth/permissions.py")
    plan = build_context_plan(snapshot, **budget())
    self.assertIn("auth/permissions.py", plan.manifest.paths)
    self.assertTrue(plan.coverage.explains_every_file)

def test_each_chunk_respects_effective_budget(self) -> None:
    plan = build_context_plan(large_snapshot(), **budget())
    self.assertTrue(all(chunk.estimated_tokens <= plan.max_chunk_tokens for chunk in plan.chunks))

def test_deleted_line_is_commentable_on_left_side(self) -> None:
    plan = build_context_plan(snapshot_with_deleted_line("old.py", 7), **budget())
    self.assertIn(ChangedLine("old.py", 7, DiffSide.LEFT), plan.commentable_lines)

def test_rename_preserves_old_and_new_path_mapping(self) -> None:
    hunks = parse_hunks(renamed_file("old.py", "new.py"))
    self.assertTrue(all(h.previous_path == "old.py" for h in hunks))
```

- [ ] **Step 2: 运行 RED**

Run: `py -3 -m unittest tests.test_qykw_context -v`

Expected: FAIL because prefix truncation cannot meet coverage assertions.

- [ ] **Step 3: 实现文件清单、hunk 和预算算法**

```python
def estimate_tokens(text: str) -> int: ...
def parse_hunks(file: ChangedFile) -> tuple[DiffHunk, ...]: ...
def build_context_plan(
    snapshot: PullSnapshot,
    *,
    repository_limit: int,
    backend_context_window: int,
    output_reserve: int,
    safety_reserve_ratio: float,
    max_chunk_ratio: float,
) -> ContextPlan: ...
```

先列全文件，再风险排序；每个文件先获得最低预算。快照同时包含可信默认分支规则、PR base/head 的 blob、mode 与文本、`previous_path`、变更 patch、CI 结果和按导入/引用关系选出的相关代码。hunk 解析分别建立 `LEFT` 删除行与 `RIGHT` 新增行映射；重命名同时保留旧/新路径，后续 `FindingCandidate → InlineComment → create_review` 必须原样携带 `DiffSide`。二进制、生成文件、超大文件和敏感路径只能明确跳过并写入原因；任何无法读取的输入都进入 `CoverageReport`。

- [ ] **Step 4: 运行 GREEN 并提交**

Run: `py -3 -m unittest tests.test_qykw_context -v`

Expected: PASS for 10k+ 行、多文件、删除、重命名、mode 变化、超长评论、非法 hunk 和跨 PR 隔离。

```bash
git add tools/qykw/context.py tests/test_qykw_context.py
git commit -m "feat: plan complete qykw review context"
```

### Task 8: 实现分阶段审查与发现验证

**Files:**
- Create: `tools/qykw/advisory.py`
- Create: `tools/qykw/review.py`
- Create: `tests/test_qykw_advisory.py`
- Create: `tests/test_qykw_review.py`

**Interfaces:**
- Consumes: `RunContext`、`PullSnapshot`、`ContextPlan`、`InferenceProvider`。
- Produces: 分析/计划的结构化 `AdvisoryResult`，以及经过反证、严重度、行号和去重验证的 `ReviewResult`。

- [ ] **Step 1: 写问题验证 RED 测试**

```python
def test_invalid_candidates_do_not_consume_finding_limit(self) -> None:
    candidates = tuple(invalid_candidate(i) for i in range(20)) + (valid_candidate(),)
    findings = validate_findings(
        candidates,
        commentable_lines=frozenset({valid_line()}),
        max_findings=20,
    )
    self.assertEqual(findings, (expected_finding(),))

def test_p0_requires_catastrophic_concrete_path(self) -> None:
    finding = validate_findings((vague_p0(),), commentable_lines=lines(), max_findings=20)
    self.assertEqual(finding, ())

def test_help_status_summary_and_stop_do_not_call_provider(self) -> None:
    service = advisory_service(recording_provider())
    for name in (CommandName.HELP, CommandName.STATUS, CommandName.SUMMARY, CommandName.STOP):
        service.handle(run_with(name), context_plan())
    self.assertEqual(service.provider.calls, [])
```

- [ ] **Step 2: 运行 RED**

Run: `py -3 -m unittest tests.test_qykw_advisory tests.test_qykw_review -v`

Expected: FAIL because engine/validation are missing.

- [ ] **Step 3: 实现三阶段引擎**

```python
class ReviewEngine:
    def review(
        self,
        run: RunContext,
        snapshot: PullSnapshot,
        plan: ContextPlan,
    ) -> ReviewResult: ...

def validate_findings(
    candidates: Iterable[FindingCandidate],
    *,
    commentable_lines: frozenset[ChangedLine],
    max_findings: int,
) -> tuple[Finding, ...]: ...

class AdvisoryService:
    def handle(
        self,
        run: RunContext,
        plan: ContextPlan | None,
        record: RunRecord | None = None,
    ) -> AdvisoryResult: ...
    def help(self, run: RunContext) -> AdvisoryResult: ...
    def analyze(self, run: RunContext, plan: ContextPlan) -> AdvisoryResult: ...
    def plan(self, run: RunContext, plan: ContextPlan) -> AdvisoryResult: ...
    def status(self, run: RunContext, record: RunRecord) -> AdvisoryResult: ...
    def summary(self, run: RunContext, record: RunRecord) -> AdvisoryResult: ...
    def stop(self, run: RunContext, record: RunRecord) -> AdvisoryResult: ...
```

`help/status/summary/stop` 只使用确定性渲染和状态存储；`analyze/plan` 分别调用自己的严格 Schema，并只能生成只读字段。Review 执行分诊、分片深审、针对性反证和确定性校验。候选的 `path + line + side` 必须存在于 `commentable_lines` 后才计入 `max_findings`；同批发现按路径、行、side、失败模式稳定去重。

- [ ] **Step 4: 迁移旧结果解析和真实变更行测试，运行 GREEN**

Run: `py -3 -m unittest tests.test_qykw_advisory tests.test_qykw_review -v`

Expected: PASS for原始响应块、布尔行号、无内容、0/1/多问题及 P0/P1/P2 样例。

- [ ] **Step 5: 提交**

```bash
git add tools/qykw/advisory.py tools/qykw/review.py tests/test_qykw_advisory.py tests/test_qykw_review.py
git commit -m "feat: validate qykw review findings"
```

### Task 9: 实现状态、幂等与发布契约

**Files:**
- Create: `tools/qykw/state.py`
- Create: `tools/qykw/publish.py`
- Create: `tests/test_qykw_tasks.py`
- Create: `tests/test_qykw_publish.py`

**Interfaces:**
- Consumes: `RunContext`、`RunRecord`、`ReviewResult`、`GitHubGateway`。
- Produces: `GitHubCommentStateStore`、`ReviewPublisher`、`PublishResult`。

- [ ] **Step 1: 写幂等和输出 RED 测试**

```python
def test_summary_precedes_inline_comments(self) -> None:
    publisher = recording_publisher()
    publisher.publish_review(run(), review_with_two_findings())
    self.assertEqual(publisher.calls, ["update_summary", "create_review"])

def test_partial_inline_failure_keeps_completed_summary(self) -> None:
    result = publisher_with_inline_failure().publish_review(run(), review_with_two_findings())
    self.assertEqual(result.status, RunStatus.PARTIAL)
    self.assertIn("问题统计", result.summary_body)

def test_deleted_finding_publishes_left_side(self) -> None:
    publisher.publish_review(run(), review_with_deleted_finding("old.py", 7))
    self.assertEqual(publisher.gateway.review_comments[0].side, DiffSide.LEFT)
```

覆盖 qykw 自有标记、旧标记只读兼容、其他 author 伪造 marker/fingerprint、第 101 条评论、`find_latest_active()` 的阶段/时间排序、同 Head+path+line+side 行评去重、LEFT 删除行与 RIGHT 新增行、HTML/图片/外链/意外 mention 净化和软取消。竞态测试先追加 cancel marker，再让主 workflow 用取消前的旧 `RunRecord` 执行 `save()`；随后 `is_cancel_requested()` 仍必须为真。捕获 state/publish 日志并断言不包含完整评论、代码、模型字段、marker payload 或 token sentinel。

- [ ] **Step 2: 运行 RED**

Run: `py -3 -m unittest tests.test_qykw_tasks tests.test_qykw_publish -v`

Expected: FAIL because state/publisher are missing.

- [ ] **Step 3: 实现状态存储和发布器**

```python
class RunStateStore(Protocol):
    def find_by_idempotency_key(self, pr_number: int, key: str) -> RunRecord | None: ...
    def find_latest(self, pr_number: int) -> RunRecord | None: ...
    def find_latest_active(self, pr_number: int) -> RunRecord | None: ...
    def has_successful_initial_review(self, pr_number: int) -> bool: ...
    def create(self, record: RunRecord) -> bool: ...
    def save(self, record: RunRecord) -> None: ...
    def get(self, pr_number: int, run_id: str) -> RunRecord | None: ...
    def is_cancel_requested(self, pr_number: int, run_id: str) -> bool: ...
    def request_cancel(
        self,
        pr_number: int,
        run_id: str,
        *,
        stop_comment_id: int,
        actor_login: str,
    ) -> CancelRecord: ...

class ReviewPublisher:
    def acknowledge(self, run: RunContext) -> int: ...
    def publish_status(self, record: RunRecord) -> None: ...
    def publish_review(self, run: RunContext, result: ReviewResult) -> PublishResult: ...
```

`GitHubCommentStateStore` 只有在 `author_login == "qykw"` 时才解析 qykw marker、运行状态和行评 fingerprint；其他作者的相似 HTML 一律视为普通不可信文本。软取消使用独立 append-only cancel marker，以 stop comment ID 幂等并指向 target run；普通 `save()`/`publish_status()` 永不编辑或删除 cancel marker，`is_cancel_requested()` 每次分页聚合。总结由确定性渲染器生成；模型只提供字段。零问题写“未发现有充分证据的问题”，事件始终为 `COMMENT`。

- [ ] **Step 4: 运行 GREEN 并提交**

Run: `py -3 -m unittest tests.test_qykw_tasks tests.test_qykw_publish -v`

Expected: PASS.

```bash
git add tools/qykw/state.py tools/qykw/publish.py tests/test_qykw_tasks.py tests/test_qykw_publish.py
git commit -m "feat: publish idempotent qykw reviews"
```

### Task 10: 编排状态机、软取消和统一入口

**Files:**
- Create: `tools/qykw/runner.py`
- Create: `tools/qykw/__main__.py`
- Create: `tests/test_qykw_runner.py`
- Modify: `tools/minimax_review.py`

**Interfaces:**
- Consumes: 前九个任务产生的 config、gateway、state、provider、engine 和 publisher。
- Produces: `QykwRunner.handle()`、`python -m tools.qykw` 和临时兼容入口。

- [ ] **Step 1: 写状态顺序、Head 漂移和 Reaction 降级 RED 测试**

```python
def test_head_drift_prevents_inline_publish(self) -> None:
    runner = runner_with_heads("old", "new")
    outcome = runner.handle(event())
    self.assertEqual(outcome.status, RunStatus.STALE)
    self.assertNotIn("create_review", runner.gateway.write_calls)

def test_reaction_failure_does_not_stop_review(self) -> None:
    outcome = runner_with_failed_reaction().handle(event())
    self.assertEqual(outcome.status, RunStatus.COMPLETED)

def test_reaction_happens_only_after_parse_auth_and_idempotency(self) -> None:
    runner = recording_runner()
    for item in (invalid_mention(), unauthorized_command(), replayed_event()):
        runner.handle(item)
    self.assertEqual(runner.gateway.reaction_calls, [])
```

- [ ] **Step 2: 运行 RED**

Run: `py -3 -m unittest tests.test_qykw_runner -v`

Expected: FAIL because runner is missing.

- [ ] **Step 3: 实现依赖注入编排器**

```python
class QykwRunner:
    def handle(self, event: EventContext) -> RunOutcome: ...
```

运行器先执行事件标准化、精确解析、权限、事件幂等和共享并发边界；随后通过 `get_pull_ref()` 固定真实 PR Head/base 并构造 `RunContext`，全部通过后才添加 😄 并创建/更新运行。评论事件永不从 `GITHUB_SHA` 填充 Head。`CommandRouter` 的执行路径固定为：

- `帮助`：确定性命令表；不调用 Provider。
- `状态`：读取当前或最近运行；不调用 Provider。
- `总结`：根据已有结构化结果渲染；不调用 Provider。
- `停止`：校验触发者后只追加独立 `CancelRecord` marker；不调用 Provider。
- `分析`、`计划`：调用 `AdvisoryService` 对应严格 Schema，只发布总结评论。
- `审查`、`复审`：收集最新固定 Head 后进入 `ReviewEngine`；复审使用新的 Head 幂等键，不清除初审成功标记。
- `修复`、`实现`：第一阶段返回 `capability_disabled`；第二阶段启用后审查 runner no-op，由修改工作流处理。

审查路径严格执行 `accepted → acknowledged → collecting → analyzing → validating → publishing → completed`。每阶段及后台调用前后检查取消；任何发布前复核 actor 权限和 Head SHA。写入 `QYKW_STATE_PATH` 的 JSON 只包含结构化状态。

`python -m tools.qykw` 只接受控制器定义的 `--phase control|authorize|analyze|publish|record-failure`；每个 phase 从版本化 JSON artifact 读取前序结构化数据并复核 schema、run ID、幂等键和 Head。phase 不能来自评论或模型。`control` 只解析/鉴权/幂等处理 `停止`，调用 `find_latest_active()` 定位目标后以 stop comment ID 追加独立 `CancelRecord` marker，其他命令 no-op。`QykwRunner.handle()` 保留为本地/Fake 集成编排器，Actions 使用同一服务对象的分阶段方法，避免单 job 混合凭据。

将旧入口缩为薄委托：

```python
from tools.qykw.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: 运行新旧入口测试并提交**

Run: `py -3 -m unittest tests.test_qykw_runner tests.test_minimax_review -v`

Expected: PASS;旧测试由兼容映射维持。

```bash
git add tools/qykw/runner.py tools/qykw/__main__.py tools/minimax_review.py tests/test_qykw_runner.py
git commit -m "feat: orchestrate qykw review runs"
```

### Task 11: 原子迁移工作流与默认配置

**Files:**
- Create: `.github/qykw.toml`
- Create: `.github/workflows/qykw-review.yml`
- Create: `.github/workflows/qykw-control.yml`
- Create: `tests/test_qykw_workflow.py`
- Delete: `.github/workflows/minimax-review.yml`
- Delete: `tests/test_minimax_workflow.py`

**Interfaces:**
- Consumes: `python -m tools.qykw`、`QYKW_REVIEW_TOKEN`、`QYKW_INFERENCE_*` Secrets/Variables。
- Produces: 唯一启用的 qykw 审查工作流。

**Job Contract:**

1. 独立 `control` workflow：只监听两类评论并精确处理 `停止`；只持有 `QYKW_REVIEW_TOKEN`，无主 PR 工作并发组、推理密钥或发布令牌。它固定使用 `qykw-control-${{ github.repository_id }}-comment-${{ github.event.comment.id }}` 与 `cancel-in-progress: false`，使同一评论的 created/edited/重放串行但不等待长任务。解析、停止权限和 comment-ID 幂等通过后才添加 😄 并追加指向目标 run 的独立 `CancelRecord` marker；其他命令 no-op。
2. `authorize`：默认分支可信代码执行精确解析、权限、幂等、固定 Head、创建状态并添加 😄；只持有 `QYKW_REVIEW_TOKEN`，无推理密钥。对 `停止`和第二阶段启用后的 change 命令 no-op。
3. `analyze`：默认分支可信代码通过只读 GitHub API 收集快照并调用 Provider；只持有 `QYKW_INFERENCE_*` 和内置只读 GitHub token，不持有 `QYKW_REVIEW_TOKEN`，不执行 PR Head。
4. `publish`：重新校验权限和 Head，净化并发布总结/行评；只持有 `QYKW_REVIEW_TOKEN`，不持有推理密钥，不调用 Provider。
5. `record_failure`：更新已创建的 qykw 状态；只持有 `QYKW_REVIEW_TOKEN`，不得进入分析或代码发布路径。

jobs 之间的 artifacts 保留 1 天，只包含结构化 `RunContext`、快照清单/覆盖率、候选字段、验证结果和安全状态；不包含源代码全文、完整评论、原始模型响应、Secrets 或宿主环境。

review 与 control workflow 中每一个运行仓库 Python 的 job 都必须显式 checkout 可信控制器：

```yaml
with:
  ref: ${{ github.event.repository.default_branch }}
  path: controller
  persist-credentials: false
```

不得依赖 comment event 的默认 ref/SHA，也不得在持有 review token 或 inference key 的 job checkout PR Head。所有入口从 `controller` 目录运行。

- [ ] **Step 1: 写工作流 RED 测试**

```python
def test_only_initial_events_are_subscribed(self) -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    self.assertIn("opened", workflow)
    self.assertIn("ready_for_review", workflow)
    self.assertNotIn("synchronize", workflow)

def test_trusted_checkout_and_no_provider_identity(self) -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    self.assertIn("persist-credentials: false", workflow)
    self.assertIn("python -m tools.qykw", workflow)
    self.assertNotRegex(workflow, forbidden_identity_pattern())
```

还要断言 `timeout-minutes: 15`、`cancel-in-progress: false`、`queue: max`、review workflow 使用共享并发组 `qykw-${{ github.repository_id }}-pr-${{ github.event.pull_request.number || github.event.issue.number }}`、control workflow 使用独立 comment 组、所有 `uses:` 固定完整 commit SHA、每个 job 显式默认分支 `controller` checkout、1 天 artifacts 和 job 级最小权限。双 workflow 夹具先占用共享组，再同时排入 review no-op 与 change run，断言两者都保留且 change 最终执行。逐 job 断言 review token 与 inference key 永不交叉，publish 不调用 Provider、analyze 不调用评论写接口，control 只接受 `停止`且无效/未授权/重复停止不添加 Reaction。

- [ ] **Step 2: 运行 RED**

Run: `py -3 -m unittest tests.test_qykw_workflow -v`

Expected: FAIL because the qykw workflow does not exist.

- [ ] **Step 3: 创建配置与工作流并原子删除旧工作流**

`.github/qykw.toml` 使用规格中的已确认内容。工作流监听 `pull_request_target` 的 `opened`、`ready_for_review`、`reopened`，以及 `issue_comment`、`pull_request_review_comment` 的 `created`/`edited` 和手动触发；不监听 `synchronize`。评论事件只做无副作用的 job 预筛，最终精确解析在 Python 中完成。第一阶段对 `修复/实现`返回 `capability_disabled`；第二阶段上线后，审查工作流对这两类命令 no-op，由独立修改工作流处理，确保同一评论只有一个运行添加 Reaction。

保留：

```yaml
ref: ${{ github.event.repository.default_branch }}
persist-credentials: false
```

入口：

```yaml
run: python -m tools.qykw
```

authorize/publish/failure job 仅注入仓库限定的 `QYKW_REVIEW_TOKEN`（评论/Reaction 权限）；analyze job 仅注入 `QYKW_INFERENCE_*`。不引用第二阶段的 `QYKW_PUBLISH_TOKEN`。若现有令牌权限过宽，迁移工作流前先由仓库所有者创建最小权限令牌，代码不得自行调整 Secrets。

- [ ] **Step 4: 运行 GREEN 与全量测试**

Run: `py -3 -m unittest tests.test_qykw_workflow -v`

Expected: PASS.

Run: `py -3 -m unittest discover -s tests -v`

Expected: PASS.

- [ ] **Step 5: 提交**

```bash
git add .github/qykw.toml .github/workflows/qykw-review.yml .github/workflows/qykw-control.yml tests/test_qykw_workflow.py
git rm .github/workflows/minimax-review.yml tests/test_minimax_workflow.py
git commit -m "ci: migrate review workflow to qykw"
```

### Task 12: 集成验收、覆盖率、文档与旧身份清理

**Files:**
- Create: `tests/test_qykw_integration.py`
- Create: `tests/test_qykw_coverage.py`
- Modify: `tests/test_qykw_workflow.py`
- Create: `.coveragerc`
- Create: `requirements-dev.txt`
- Create: `tools/check_qykw_coverage.py`
- Create: `docs/qykw代码审查机器人.md`
- Modify: `.github/workflows/ci.yml`
- Modify: `README.md`
- Delete: `docs/MiniMax代码审查机器人.md`
- Delete: `tests/test_minimax_review.py`
- Delete: `tools/minimax_review.py`

**Interfaces:**
- Consumes: 第一阶段完整审查内核。
- Produces: 无网络集成验收、覆盖率门禁和唯一 qykw 文档。

- [ ] **Step 1: 写端到端 RED 测试**

```python
def test_initial_review_lifecycle(self) -> None:
    system = fake_system(opened_event())
    outcome = system.run()
    self.assertEqual(outcome.status, RunStatus.COMPLETED)
    self.assertEqual(system.provider.stages, ["triage", "review", "validation"])
    self.assertEqual(system.publisher.order, ["summary", "inline"])

def test_replayed_event_has_no_second_inference(self) -> None:
    system = fake_system(opened_event())
    system.run()
    system.run()
    self.assertEqual(system.provider.run_count, 1)
```

该夹具固定为单一上下文块，因此调用阶段恰为分诊、一次深审和一次反证验证。另以表驱动测试覆盖帮助、分析、计划、审查、复审、状态、总结、修复、实现、停止全部命令的 handler、Provider 调用次数和允许副作用；并覆盖非 Draft 首次、Draft→Ready、后续 push、Reaction 失败、分页、Head 漂移、后台失败和多行评论。并发取消夹具让 Provider 阻塞，独立 control lane 在主 workflow 仍运行时追加目标 run 的 `CancelRecord` marker，再让主 workflow 用取消前的旧 `RunRecord` 保存一次状态；释放 Provider 后的下一检查点调用 `is_cancel_requested()` 仍必须进入 `RunStatus.CANCELED`。这证明取消标记不会被旧状态覆盖，且 `停止`不会排队到任务结束。

- [ ] **Step 2: 运行 RED 与旧身份扫描**

Run: `py -3 -m unittest tests.test_qykw_integration -v`

Expected: FAIL until integration wiring is complete.

Run: `rg -n -uu --glob '!docs/superpowers/**' "MiniMax|minimax" tools tests .github docs README.md`

Expected: 命中仅待删除的旧入口、旧测试和旧文档。

- [ ] **Step 3: 添加覆盖率门禁和完成文档迁移**

`requirements-dev.txt`：

```text
coverage==7.16.0
```

`.coveragerc`：

```ini
[run]
branch = True
source = tools.qykw

[report]
show_missing = True
skip_covered = False
```

`tools/check_qykw_coverage.py` 读取 coverage.py 7.16 JSON，先要求 `meta.branch_coverage is true`，再分别检查 `totals.percent_statements_covered >= 95` 和 `totals.percent_branches_covered >= 90`，不使用混合 statements/branches 的 `percent_covered`，也不写回文件。`tests/test_qykw_coverage.py` 覆盖阈值恰好相等、各自低于阈值、缺少 branch 数据和零分支项目。

在 `.github/workflows/ci.yml` 增加无 Secrets 的 `qykw-coverage` job：保持 Python 3.11，运行 `python -m pip install --disable-pip-version-check -r requirements-dev.txt`，随后执行与 Step 4 完全相同的 branch coverage、JSON 和双阈值命令。`tests/test_qykw_workflow.py` 静态断言版本、安装来源、`--branch`、`--source=tools.qykw`、95/90 阈值和只读权限，避免门禁只存在于文档。

将旧测试仍有价值的断言迁入新测试后，删除旧入口、旧测试和旧文档。README 只链接 qykw 文档。

- [ ] **Step 4: 运行完整 GREEN 门禁**

```powershell
py -3 -m compileall -q agents core evalkit tools build_showcase.py cli.py config.py orchestrator.py server.py
py -3 -m unittest discover -s tests -v
py -3 -m coverage run --branch --source=tools.qykw -m unittest discover -s tests -p "test_qykw*.py" -v
py -3 -m coverage json -o qykw-coverage.json
py -3 tools/check_qykw_coverage.py qykw-coverage.json --line 95 --branch 90
rg -n -uu --glob '!docs/superpowers/**' "MiniMax|minimax" tools tests .github docs README.md
git diff --check
```

Expected:

- compileall exit 0。
- 所有测试 PASS，0 failures/errors。
- qykw 行覆盖率至少 95%，分支覆盖率至少 90%。
- 身份扫描无命中。
- `git diff --check` 无输出。

删除未跟踪的 `qykw-coverage.json`，不得提交覆盖率临时产物。

- [ ] **Step 5: 提交**

```bash
git add tests/test_qykw_integration.py tests/test_qykw_coverage.py tests/test_qykw_workflow.py .coveragerc requirements-dev.txt tools/check_qykw_coverage.py docs/qykw代码审查机器人.md .github/workflows/ci.yml README.md
git rm docs/MiniMax代码审查机器人.md tests/test_minimax_review.py tools/minimax_review.py
git commit -m "test: enforce qykw review acceptance gates"
```

## Stage 1 External Verification Gate

以下外部写入不属于普通 CI，必须在用户再次明确授权后执行：

1. 由仓库所有者配置仓库限定的 `QYKW_REVIEW_TOKEN` 与所需 `QYKW_INFERENCE_*`，替换过宽或旧名称 Secret；实现代码不自行修改 Secrets。
2. 推送功能分支并创建实现 PR。
3. 使用测试 PR 验证非 Draft 首次自动审查。
4. 追加提交并确认不会自动复审。
5. 使用 `@qykw 复审`验证显式复审。
6. 使用 `@qykw 状态`验证运行编号、Head 和覆盖率。
7. 重放事件并确认没有第二次推理或重复评论。

第一阶段通过上述门禁后，才能启用第二阶段授权修改模式。
