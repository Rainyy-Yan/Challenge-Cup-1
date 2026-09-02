# qykw 授权修改模式实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在独立审查内核稳定后，为 qykw 增加仅由授权用户显式触发、无密钥验证、只创建 Draft PR 的受控代码修改能力。

**Architecture:** 修改模式沿用第一阶段的命令、权限、运行状态和 GitHub 读取接口，但将补丁生成、候选验证和 GitHub 发布拆成互不共享凭据的 Actions jobs。模型只生成结构化文本替换；可信控制器决定路径、测试方案、分支和发布动作。

**Tech Stack:** Python 3.11 标准库、`unittest`、Docker 无网络验证沙箱、GitHub Actions、GitHub Git Data API、coverage.py 7.16.0（沿用第一阶段开发依赖）。

**Spec:** `docs/superpowers/specs/2026-09-02-qykw-independent-agent-design.md`

**Prerequisite:** `docs/superpowers/plans/2026-09-02-qykw-independent-review-core.md` 已全部完成，审查链路通过真实测试 PR，`RunContext`、`RunStateStore`、`InferenceProvider` 和精确命令鉴权已经稳定。

## Global Constraints

- 只接受 PR 评论中授权用户发出的精确 `@qykw 修复 <要求>` 或 `@qykw 实现 <需求>`；自然语言分析不得升级为写操作。
- 触发时固定原 PR 的 Head SHA 和 base ref。新 Draft PR 以原 base ref 为目标，内容包含原 PR 变更及 qykw 补丁。
- 分支固定为 `qykw/<run-id-lower>-fix` 或 `qykw/<run-id-lower>-implement`；不写原 PR 分支或默认分支。
- 不 Approve、Merge、Close、Delete、Force Push，不修改仓库设置、Secrets、规则集、自身工作流或策略文件。
- 模型不得选择验证命令、路径权限、分支、提交身份、令牌或 GitHub 写操作。
- `QYKW_REVIEW_TOKEN` 只允许评论/Reaction；`QYKW_PUBLISH_TOKEN` 仅限当前仓库的内容和 PR 发布。两者不得在同一 job 或候选代码环境中出现。
- 测试失败、摘要不符和策略拒绝在任何 Git 对象写入前终止；在 ref 写入截止点前观察到的取消、Head/base 漂移或授权撤销必须阻止分支。若变化发生于 ref 成功之后，禁止继续创建 PR，保留孤立分支并明确报告，不自动删除或伪称零写入。
- Git Data API 的 blob/tree/commit/ref/PR 写入不是原子事务：ref 前失败可能留下不可达 Git 对象，ref 后 PR 失败会留下孤立 qykw 分支。两者都记录 `partial` 和已知对象，交由人类处理，不自动删除或伪称已回滚。
- 发布的提交仅使用 qykw 机器账号身份，不添加用户、Codex、OpenAI、模型或工具共同作者。
- 本计划 Task 1–7 的人工实施提交使用仓库现有 `xyh` identity，不添加 Codex、OpenAI、模型、工具、AI 或任何 `Co-Authored-By`；这与运行时 qykw 生成提交的机器账号身份严格区分。
- 本方案继续使用 GitHub Actions 和 GitHub API，不需要域名、独立服务器或常驻 webhook。

---

## File Map

| File | Responsibility |
| --- | --- |
| `tools/qykw/change.py` | 修改领域契约、结构化补丁生成和摘要 |
| `tools/qykw/patches.py` | 路径复核、基线哈希校验和确定性文本替换 |
| `tools/qykw/verification.py` | 可信验证 Profile、命令结果和证明 |
| `tools/qykw/sandbox.py` | 无密钥、无网络、受限资源的候选代码执行 |
| `tools/qykw/verify.Dockerfile` | 固定验证运行时，不携带仓库或凭据 |
| `tools/qykw/change_publish.py` | Git Data API 提交、分支和 Draft PR 发布 |
| `.github/workflows/qykw-change.yml` | 鉴权、生成、验证、发布的 job 级隔离 |
| `.github/workflows/qykw-review.yml` | 对修改命令 no-op，并与修改 workflow 共享串行组 |
| `docs/qykw-authorized-change.md` | 授权修改命令、边界和失败恢复说明 |

### Task 1: 锁定修改领域契约与策略

**Files:**
- Create: `tools/qykw/change.py`
- Modify: `tools/qykw/policy.py`
- Modify: `tools/qykw/state.py`
- Create: `tests/test_qykw_change.py`

**Interfaces:**

```python
class ChangeKind(str, Enum):
    FIX = "fix"
    IMPLEMENT = "implement"

@dataclass(frozen=True)
class ChangeRequest:
    context: RunContext
    kind: ChangeKind
    instruction: str
    source_repository: str
    target_repository: str
    source_head_sha: str
    target_base_sha: str
    target_base_ref: str
    verification_profile: str

@dataclass(frozen=True)
class TextEdit:
    before: str
    after: str

@dataclass(frozen=True)
class FilePatch:
    path: str
    base_sha256: str | None
    create: bool
    edits: tuple[TextEdit, ...]

@dataclass(frozen=True)
class PatchManifest:
    schema_version: int
    run_id: str
    source_repository: str
    target_repository: str
    source_pr_number: int
    source_head_sha: str
    target_base_sha: str
    target_base_ref: str
    verification_profile: str
    files: tuple[FilePatch, ...]
    digest: str

@dataclass(frozen=True)
class FileDigest:
    path: str
    mode: str
    sha256: str

@dataclass(frozen=True)
class PreparedWorkspace:
    root: Path
    source_head_sha: str
    source_files: tuple[FileDigest, ...]

@dataclass(frozen=True)
class CommandResult:
    name: str
    argv_digest: str
    exit_code: int | None
    timed_out: bool
    duration_ms: int
    output_digest: str
    output_excerpt: str

@dataclass(frozen=True)
class VerificationAttestation:
    schema_version: int
    workflow_run_id: int
    run_id: str
    source_repository: str
    source_head_sha: str
    target_repository: str
    target_base_sha: str
    target_base_ref: str
    manifest_digest: str
    profile: str
    image_digest: str
    output_tree_digest: str
    workspace_tree_digest: str
    output_files: tuple[FileDigest, ...]
    success: bool
    canceled: bool
    results: tuple[CommandResult, ...]

@dataclass(frozen=True)
class AppliedPatch:
    files: tuple[FileDigest, ...]
    output_tree_digest: str
    workspace_tree_digest: str

@dataclass(frozen=True)
class CommitIdentity:
    login: str
    name: str
    email: str

@dataclass(frozen=True)
class SourceBlob:
    path: str
    mode: str
    content: bytes
    git_sha: str

@dataclass(frozen=True)
class SourceTreeEntry:
    path: str
    mode: str
    kind: str
    git_sha: str

@dataclass(frozen=True)
class PublishedFile:
    path: str
    mode: str
    content: bytes
    sha256: str

@dataclass(frozen=True)
class GitTreeEntry:
    path: str
    mode: str
    blob_sha: str

class WriteState(str, Enum):
    NOT_CREATED = "not_created"
    CREATED = "created"
    UNKNOWN = "unknown"

class PublicationStage(str, Enum):
    PREFLIGHT = "preflight"
    BLOBS = "blobs"
    TREE = "tree"
    COMMIT = "commit"
    REF = "ref"
    PULL = "pull"
    COMPLETED = "completed"

class WriteKind(str, Enum):
    BLOB = "blob"
    TREE = "tree"
    COMMIT = "commit"
    REF = "ref"
    PULL = "pull"

@dataclass(frozen=True)
class WriteReceipt:
    kind: WriteKind
    target: str
    object_id: str | None
    state: WriteState

@dataclass(frozen=True)
class PublishedCommit:
    commit_sha: str
    tree_sha: str

@dataclass(frozen=True)
class PublicationRequest:
    change: ChangeRequest
    manifest: PatchManifest
    attestation: VerificationAttestation
    branch_name: str
    title: str
    body: str

@dataclass(frozen=True)
class ChangePublication:
    stage: PublicationStage
    branch_name: str
    branch_state: WriteState
    pull_state: WriteState
    commit_sha: str | None
    pull_number: int | None
    receipts: tuple[WriteReceipt, ...]
    partial: bool
    error_code: str | None
```

- [ ] **Step 1: 写危险请求和路径策略 RED 测试**

```powershell
py -3 -m unittest tests.test_qykw_change.TestChangePolicy -v
```

Expected: FAIL with missing change contracts or dangerous paths being accepted.

覆盖未授权用户、非 `修复/实现` 命令、绝对路径、`..`、反斜杠混淆、大小写敏感目录绕过、符号链接、子模块、二进制、删除、空覆盖、超限文件和敏感路径。

- [ ] **Step 2: 定义不可变修改契约**

增加 `ChangeKind`、上述数据类，以及：

```python
class PatchGenerator(Protocol):
    def generate(
        self,
        request: ChangeRequest,
        snapshot: PullSnapshot,
        state_store: RunStateStore,
    ) -> PatchManifest: ...

class SandboxVerifier(Protocol):
    def verify(
        self,
        request: ChangeRequest,
        manifest: PatchManifest,
        workspace: PreparedWorkspace,
    ) -> VerificationAttestation: ...

class ChangePublisher(Protocol):
    def publish(self, request: PublicationRequest) -> ChangePublication: ...

class ChangePolicy(Protocol):
    def validate_request(self, request: ChangeRequest, snapshot: PullSnapshot) -> None: ...
    def validate_manifest(self, request: ChangeRequest, manifest: PatchManifest) -> None: ...
```

- [ ] **Step 3: 实现确定性修改策略**

拒绝 `.git/**`、`.github/**`、`CODEOWNERS`、根目录及嵌套 `AGENTS.md`、`tools/qykw/**`、`tools/check_qykw_coverage.py`、`.coveragerc`、`requirements-dev.txt`、权限文件、Secrets 引用和配置声明外的 Profile。默认验证 Profile 固定为 `full`；只能由默认分支可信策略缩小，模型和评论均不能选择。将路径标准化与授权结论固化为控制器结果，模型无权覆盖。

- [ ] **Step 4: 复用并验证无参数软停止定位**

复用第一阶段 `RunStateStore` 已有接口：

```python
def find_latest_active(self, pr_number: int) -> RunRecord | None: ...
def is_cancel_requested(self, pr_number: int, run_id: str) -> bool: ...
def request_cancel(
    self,
    pr_number: int,
    run_id: str,
    *,
    stop_comment_id: int,
    actor_login: str,
) -> CancelRecord: ...
```

只允许任务触发者或配置中的授权用户停止。停止以 stop comment ID 幂等追加独立 `CancelRecord` marker；主运行的旧 `RunRecord.save()` 不得覆盖它，后续检查必须调用 `is_cancel_requested()` 聚合 qykw 自有 marker。不把“停止已请求”表述成正在传输的请求已被强制终止。

- [ ] **Step 5: 运行 GREEN 与回归测试**

```powershell
py -3 -m unittest tests.test_qykw_change tests.test_qykw_policy tests.test_qykw_tasks -v
```

- [ ] **Step 6: 提交**

```bash
git add tools/qykw/change.py tools/qykw/policy.py tools/qykw/state.py tests/test_qykw_change.py
git commit -m "feat: define qykw authorized change contracts"
```

### Task 2: 生成确定性结构化补丁

**Files:**
- Modify: `tools/qykw/change.py`
- Modify: `tools/qykw/prompts.py`
- Modify: `tests/test_qykw_change.py`

**Interfaces:**

```python
def prepare_change(
    request: ChangeRequest,
    snapshot: PullSnapshot,
    provider: InferenceProvider,
    policy: ChangePolicy,
    state_store: RunStateStore,
) -> PatchManifest: ...

def canonical_manifest_bytes(
    manifest: PatchManifest, *, include_digest: bool
) -> bytes: ...

def compute_manifest_digest(manifest: PatchManifest) -> str: ...
```

- [ ] **Step 1: 写结构化输出 RED 测试**

```powershell
py -3 -m unittest tests.test_qykw_change.TestPatchGeneration -v
```

Expected: FAIL because structured patch generation and canonical digesting are absent.

覆盖提示注入、额外 JSON 字段、重复 `before`、伪造 Profile、错误 base 哈希、非 UTF-8、空修改、顺序不稳定和摘要变化。阻塞 Provider 夹具在调用期间由独立 control lane 追加取消 marker；断言 Provider 调用前已检查一次、返回后再次检查并拒绝 Manifest，且 artifact 写出前第三次检查。

- [ ] **Step 2: 增加补丁生成提示词与严格 Schema**

模型只返回目标路径、可选文件基线哈希和精确 `before`/`after` UTF-8 文本。系统上下文单独声明固定 source/target repository、source Head SHA、target base SHA/ref、允许文件、修改目的和不可覆盖的权限边界；不要求输出隐藏思维过程。

- [ ] **Step 3: 强制最高推理能力**

所有补丁生成请求使用 `reasoning_profile="maximum"`。Provider 不支持最高档、结构化输出或所需上下文窗口时直接失败，不静默降级。`prepare_change()` 在 Provider 调用前、调用返回后和返回 Manifest/写 artifact 前分别调用 `state_store.is_cancel_requested(pr_number, run_id)`；取消后不得继续校验、持久化或发布模型结果。

- [ ] **Step 4: 校验并规范化 Manifest**

已有文件要求 `create=False`、`base_sha256` 匹配，且每个非空 `before` 在前一编辑结果中恰好出现一次。新文件要求 `create=True`、路径原先不存在、`base_sha256=None`，并且只有一个 `TextEdit(before="", after=<非空 UTF-8 文本>)`；mode 固定 `100644`。禁止删除、空文件、可执行位，以及模型声明验证命令、分支、身份或发布动作。按路径与编辑顺序生成规范 JSON，并使用 SHA-256 计算稳定摘要。

- [ ] **Step 5: 运行 GREEN 与回归测试**

```powershell
py -3 -m unittest tests.test_qykw_change tests.test_qykw_prompts tests.test_qykw_provider -v
```

- [ ] **Step 6: 提交**

```bash
git add tools/qykw/change.py tools/qykw/prompts.py tests/test_qykw_change.py
git commit -m "feat: generate deterministic qykw patch manifests"
```

### Task 3: 安全应用补丁并定义可信验证 Profile

**Files:**
- Create: `tools/qykw/patches.py`
- Create: `tools/qykw/verification.py`
- Create: `tests/test_qykw_verification.py`

**Interfaces:**

```python
def materialize_workspace(
    source_root: Path,
    *,
    source_head_sha: str,
    tracked_files: tuple[FileDigest, ...],
    destination: Path,
) -> PreparedWorkspace: ...

def apply_patch_manifest(
    manifest: PatchManifest, workspace: PreparedWorkspace
) -> AppliedPatch: ...

def compute_workspace_tree_digest(files: tuple[FileDigest, ...]) -> str: ...

@dataclass(frozen=True)
class VerificationCommand:
    name: str
    argv: tuple[str, ...]
    env: tuple[tuple[str, str], ...] = ()

@dataclass(frozen=True)
class VerificationProfile:
    name: str
    commands: tuple[VerificationCommand, ...]

def get_verification_profile(name: str) -> VerificationProfile: ...
```

- [ ] **Step 1: 写补丁应用和 Profile RED 测试**

```powershell
py -3 -m unittest tests.test_qykw_verification.TestPatchApplication tests.test_qykw_verification.TestProfiles -v
```

Expected: FAIL because patch replay and trusted Profile lookup are absent.

覆盖 TOCTOU 文件替换、符号链接、错误文件哈希、匹配零次或多次、换行保持、文件创建冲突、任意命令注入、未知 Profile、伪造 workspace root/tracked list，以及受信任 tracked-path 基线与应用后 `workspace_tree_digest`。

- [ ] **Step 2: 实现安全补丁应用**

默认分支宿主控制器从固定 Head checkout 的 `git ls-tree`/blob 内容生成 `tracked_files`，调用 `materialize_workspace()` 复制到新建临时目录并排除 `.git`；`PreparedWorkspace` 只在进程内构造，不能从评论、模型或 artifact 反序列化 root/tracked list。`source_head_sha` 必须匹配 Manifest。在 `workspace.root` 内重新解析每个路径，拒绝符号链接和越界；读取后复核 SHA-256；按 Manifest 顺序执行精确文本替换；最终按 `path + mode + sha256` 排序生成 Manifest 路径的 `FileDigest` 清单和 `output_tree_digest`。同时严格使用 `workspace.source_files` 对完整受跟踪工作区调用共享纯函数 `compute_workspace_tree_digest()`；该函数按规范化 `path + mode + sha256` 排序并使用版本化域分隔生成 `workspace_tree_digest`。发布 job 必须使用同一函数重放 Manifest 并得到相同的输出与完整工作区摘要。

- [ ] **Step 3: 固定可信验证 Profile**

- `backend`：`python -m compileall -q agents core evalkit tools build_showcase.py cli.py config.py orchestrator.py server.py`，再运行 `python -m unittest discover -s tests -v`。
- `frontend`：依次运行 `node --check web/engine.js`、`python -m unittest tests.test_parity -v`、`python -m evalkit.snapshot --out /tmp/qykw-snapshot.json`、固定 `python -c` 断言 sessions 为 `P-A/P-B/P-C` 且 items/kb 非空、`python build_showcase.py`；生成物是否干净由容器外控制器对固定 tracked-path 清单重新哈希判断，不调用或信任候选 `.git/index`。
- `full`：依次运行 backend 与 frontend 的去重命令集合，再运行镜像内可信 `/opt/qykw/verify_smoke.py /workspace 8765`，启动候选 `server.py` 并仅通过容器 loopback 校验首页 DOCTYPE。

snapshot 断言的固定 argv 为：

```python
(
    "python",
    "-c",
    "import json; data=json.load(open('/tmp/qykw-snapshot.json', encoding='utf-8')); "
    "assert set(data['sessions']) == {'P-A', 'P-B', 'P-C'}; "
    "assert data['items'] and data['kb']",
)
```

命令全部保存为固定 argv 元组并以 `shell=False` 执行；snapshot 输出固定在容器 `/tmp`，不读取用户提供的环境变量。配置只能选择 Profile 名称，不能提供命令、环境变量或 shell 片段。

- [ ] **Step 4: 运行 GREEN 与全量回归**

```powershell
py -3 -m unittest tests.test_qykw_verification -v
py -3 -m unittest discover -s tests -v
```

- [ ] **Step 5: 提交**

```bash
git add tools/qykw/patches.py tools/qykw/verification.py tests/test_qykw_verification.py
git commit -m "feat: add trusted qykw verification profiles"
```

### Task 4: 实现无密钥、无网络验证和软取消

**Files:**
- Create: `tools/qykw/sandbox.py`
- Create: `tools/qykw/verify.Dockerfile`
- Create: `tools/qykw/verify_smoke.py`
- Modify: `tools/qykw/verification.py`
- Modify: `tools/qykw/__main__.py`
- Modify: `tests/test_qykw_verification.py`

**Interfaces:**

```python
class CommandExecutor(Protocol):
    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout_seconds: int,
        output_limit_bytes: int,
    ) -> CommandResult: ...

def verify_change(
    request: ChangeRequest,
    manifest: PatchManifest,
    workspace: PreparedWorkspace,
    executor: CommandExecutor,
    state_store: RunStateStore,
) -> VerificationAttestation: ...
```

- [ ] **Step 1: 写隔离和取消 RED 测试**

```powershell
py -3 -m unittest tests.test_qykw_verification.TestSandbox tests.test_qykw_verification.TestCancellation -v
```

Expected: FAIL because the isolated executor and cancellation checkpoints are absent.

断言容器参数、环境变量白名单、Secret 清除、Docker socket 缺失、网络关闭、资源上限、日志截断和每个检查点的取消行为。增加恶意候选测试：一条尝试修改 patched source，一条尝试修改 `.git/index`；前者必须因测试后摘要漂移失败，后者必须因候选工作区根本不含 `.git` 失败且不能伪造生成物结果。

- [ ] **Step 2: 构建固定验证镜像**

镜像仅包含项目测试所需的 Python 3.11、Node 24、Git 和固定工具，不复制仓库内容，不声明 Secrets。`FROM` 必须使用官方镜像的版本加 `@sha256` 摘要，系统包固定版本，禁止 `curl | sh`、浮动 latest 和运行时下载安装。静态测试拒绝未固定的 base 或包。仅把默认分支的 `verify_smoke.py` 复制到镜像 `/opt/qykw/`，候选 PR 无法覆盖该脚本。Dockerfile 与控制器从默认分支可信 checkout 构建；实际镜像摘要记录到证明中。

- [ ] **Step 3: 实现受限容器执行器**

启动参数至少包含：

```text
--network none
--read-only
--tmpfs /tmp
--cap-drop ALL
--security-opt no-new-privileges
```

控制器从固定 PR Head 的受信任 tracked-path 清单调用 `materialize_workspace()`，得到显式 `PreparedWorkspace` 后应用 Manifest；物化目录不包含 `.git`，仅 `workspace.root` 挂载为可写，所有 `CommandExecutor.run(cwd=...)` 固定使用该 root。用于取得固定 Head 的 checkout 使用 `persist-credentials: false`，但 checkout 本身不挂入容器。不挂载默认分支控制器目录、Docker socket、宿主主目录、Git 凭据或 Actions 工作目录的其他部分。显式设置 CPU、内存、进程数、单命令超时和输出字节上限。

- [ ] **Step 4: 在可信边界检查软取消**

容器外控制器使用 job 内置的只读 `issues/pull-requests` token，在应用补丁前、每条固定命令前后和生成证明前调用 `state_store.is_cancel_requested(pr_number, run_id)`，分页聚合仅由 qykw author 写入、指向该 run 的独立 `CancelRecord` marker；该只读 token 不传入容器。独立 `qykw-control.yml` 不受普通 PR 工作流并发组阻塞，因此运行中可更新标记。长命令只在返回或超时后响应软取消，不声称瞬时中止。

- [ ] **Step 5: 生成可验证证明**

应用 Manifest 后先冻结初始 `output_tree_digest` 和完整受跟踪工作区的 `workspace_tree_digest`。每条固定命令返回后，容器外控制器只根据显式 `PreparedWorkspace.root/source_files` 重新计算 `path + mode + sha256`；任何 Manifest 路径或其他受跟踪文件被候选测试新增、删除、改写或改 mode 都立即失败。全部命令结束后必须再次复核两个摘要与应用后初值完全相同，才允许 `success=True`；证明只记录这次测试后摘要。由此不会出现“测试 A、发布 B”，也不依赖候选 Git index 或未声明全局状态。

证明只包含 Schema 版本、workflow run、运行编号、source/target repository、source Head SHA、target base SHA/ref、Manifest 摘要、Profile、镜像摘要、`output_tree_digest`、`workspace_tree_digest`、文件摘要、命令标识、退出码、耗时、成功与取消状态。可信控制器在候选启动前把固定镜像摘要与 Profile 命令清单写入候选不可访问的 runtime metadata，供 publish job 独立交叉验证。`output_excerpt` 不是原始 stdout/stderr：控制器只保留测试名、`Ran/FAILED/ERROR` 计数、错误类型和已净化路径的 2 KiB 摘要；其余内容只计算 SHA-256 后丢弃。测试用代码/Secret/完整路径 sentinel 证明日志、artifact 和状态评论均不包含源代码、Secrets、完整环境变量或原始命令输出。

- [ ] **Step 6: 运行 GREEN 与回归测试**

```powershell
py -3 -m unittest tests.test_qykw_verification tests.test_qykw_runner -v
```

- [ ] **Step 7: 提交**

```bash
git add tools/qykw/sandbox.py tools/qykw/verify.Dockerfile tools/qykw/verify_smoke.py tools/qykw/verification.py tools/qykw/__main__.py tests/test_qykw_verification.py
git commit -m "feat: isolate qykw change verification"
```

### Task 5: 通过 Git Data API 发布 Draft PR

**Files:**
- Create: `tools/qykw/change_publish.py`
- Create: `tests/test_qykw_change_publish.py`

**Interfaces:**

```python
class ChangeGitHubGateway(Protocol):
    def get_pull_snapshot(self, pr_number: int) -> PullSnapshot: ...
    def get_actor_permission(self, login: str) -> RepositoryPermission: ...
    def get_authenticated_user(self) -> AuthenticatedUser: ...
    def commit_exists(self, repository: str, commit_sha: str) -> bool: ...
    def get_commit_tree_sha(self, repository: str, commit_sha: str) -> str: ...
    def list_tree_entries(self, repository: str, commit_sha: str) -> tuple[SourceTreeEntry, ...]: ...
    def get_changed_paths(self, repository: str, base_sha: str, head_sha: str) -> tuple[str, ...]: ...
    def get_blob_at_commit(self, repository: str, commit_sha: str, path: str) -> SourceBlob: ...
    def branch_exists(self, repository: str, branch_name: str) -> bool: ...
    def get_ref_target(self, repository: str, branch_name: str) -> str | None: ...
    def find_draft_pull_by_run_marker(
        self,
        repository: str,
        *,
        branch_name: str,
        head_sha: str,
        base_ref: str,
        run_id: str,
    ) -> int | None: ...
    def create_blob(self, *, repository: str, content: bytes) -> str: ...
    def create_tree(
        self,
        *,
        repository: str,
        base_tree_sha: str,
        entries: tuple[GitTreeEntry, ...],
    ) -> str: ...
    def create_commit(
        self,
        *,
        repository: str,
        parent_sha: str,
        tree_sha: str,
        message: str,
        identity: CommitIdentity,
    ) -> PublishedCommit: ...
    def create_ref(self, *, repository: str, branch_name: str, commit_sha: str) -> None: ...
    def create_draft_pull_request(
        self,
        *,
        repository: str,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> int: ...

def publish_verified_change(
    request: PublicationRequest,
    gateway: ChangeGitHubGateway,
    state_store: RunStateStore,
) -> ChangePublication: ...

def validate_attestation(
    request: PublicationRequest,
    *,
    expected_workflow_run_id: int,
    profile: VerificationProfile,
    expected_image_digest: str,
    replayed_patch: AppliedPatch,
) -> None: ...
```

- [ ] **Step 1: 写零代码/分支/PR 写入预检 RED 测试**

```powershell
py -3 -m unittest tests.test_qykw_change_publish -v
```

Expected: FAIL because publish preflight and the write-call guard are absent.

分别覆盖 Manifest 摘要不符、证明失败、运行取消、Head 漂移、base 不符、source Head 无法作为目标仓库 parent、授权撤销、路径策略失败、非法分支名和已有分支；每种场景均断言 blob/tree/commit/ref/PR 写调用为零。对 Attestation 的 schema/workflow run/run ID、source/target repository、source Head、target base SHA/ref、Manifest、Profile、镜像摘要、结果顺序/名称/argv digest/退出码/超时、`output_files`、output/workspace 摘要、`success` 和 `canceled` 逐字段篡改，每个负例都必须在首个 blob 写入前失败。另将证明里的 `workspace_tree_digest` 替换为遗漏或篡改未修改 sentinel 后的摘要，同样要求零写入。已有 qykw 状态评论仍可安全更新，不把它混同为代码写入。成功夹具在 source Head 额外放一个未触及 sentinel 文件，发布后断言 sentinel 仍存在且最终提交相对 parent 的 diff 路径集合严格等于 Manifest 路径集合。

阶段竞态夹具在 commit 创建后分别注入取消、授权撤销、source Head/base 漂移和分支抢占，断言不创建 ref/PR 且 receipts 保留不可达对象；在 ref 创建后注入取消，断言不创建 PR 并报告孤立分支。表驱动网络夹具让 blob/tree/commit/ref/PR 各自在“请求可能已接收”后断连，断言写调用恰好一次，只读 reconciliation 不触发重写，并正确得到 `CREATED`、`NOT_CREATED` 或 `UNKNOWN`。

- [ ] **Step 2: 实现一次性发布预检**

先重新读取原 PR，复核触发者权限、source repository/Head SHA、target repository/base SHA/ref、运行状态、独立取消 marker、Manifest 摘要、允许路径、分支名和分支不存在。目标仓库必须能通过 `commit_exists(target_repository, source_head_sha)` 解析原 Head；第一版对无法作为目标仓库 parent 的跨仓 PR 安全失败为 `source_head_not_publishable`，不尝试扩大 token 仓库范围或重建历史。`get_blob_at_commit()`、`list_tree_entries()` 与 `get_commit_tree_sha()` 的 repository 参数固定为 target repository，commit 固定为 source Head，二者不能来自评论、模型或 Manifest。发布器完整枚举 parent tree，任何 `truncated`、分页不完整、重复/冲突路径或不支持的 tree kind 都 fail closed；它读取受跟踪 blob、复核 Git mode 与文件基线哈希，在内存中确定性应用 Manifest，并得到 `replayed_patch`。

随后在任何 Git 写入前调用 `validate_attestation()`：要求 schema 版本和当前 workflow run 一致；run/source/target/base/ref/Manifest 字段逐一与 `ChangeRequest`/`PatchManifest` 一致；Profile 必须是可信默认分支选择且命令结果与 `VerificationProfile.commands` 数量、顺序、名称和规范 argv digest 完全一致；镜像摘要必须等于本次 workflow 构建并固定的受信任镜像；每条命令均未超时且退出码为零；`output_files`、output/workspace 摘要必须等于 `replayed_patch`；并要求 `success=True`、`canceled=False`。任何额外、缺失或不一致字段均 fail closed、零 blob 写入。候选测试期间产生的其他文件变化不得进入发布内容。所有条件满足后才进入不可回退的外部写入区。

- [ ] **Step 3: 创建不可覆盖的 qykw 分支和提交**

先用 `get_commit_tree_sha(target_repository, source_head_sha)` 读取 parent commit 的完整 tree SHA。依次调用 GitHub Git Data API 为 Manifest 输出创建 blob，再以该 parent tree 作为 `base_tree_sha` 创建 tree，且 entries 的路径集合严格等于 Manifest 路径集合；随后创建 commit。绝不从仅含 `PublishedFile` 的空 tree 起步，否则会隐式删除未列文件。parent 固定为触发时原 PR source Head SHA；ref 只能创建，不能更新或强推。创建 commit 后、创建 ref 前，发布器完整读取新 commit tree 和 blob，以共享函数重算 `workspace_tree_digest`，并用 `get_changed_paths()` 复核相对 parent 的路径集合；两者必须分别等于证明摘要和 Manifest 路径。`list_tree_entries()`/`get_changed_paths()` 必须由完整 tree 清单计算；GitHub 返回 `truncated` 或任何分页不完整时 fail closed，不能依赖会截断文件列表的 compare 响应。测试夹具还要读取未触及 sentinel 证明其继承；不满足时停止在 ref 前。发布器调用 `get_authenticated_user()`，只接受配置的 qykw login，并将 author/committer 固定为 `name=<login>`、`email=<database_id>+<login>@users.noreply.github.com`。提交消息拒绝 `Co-Authored-By`、工具或模型署名。协议不提供删除、合并、批准、设置或 ref 更新方法。

在最后一次 `create_ref()` 之前，重新调用 `state_store.is_cancel_requested()`、`get_actor_permission()`、`get_pull_snapshot()`、`get_ref_target()`，并复核 source Head、target base SHA/ref、授权和分支仍不存在。任一变化都不创建 ref，返回含已知不可达对象的 partial。ref 已确认创建后、调用 `create_draft_pull_request()` 前再次检查取消、授权、source Head 和 target base；若此时变化，保留孤立分支、停止 PR 创建并记录“已越过分支写入截止点”。

- [ ] **Step 4: 创建面向原 base 的 Draft PR**

PR 描述包含源 PR、运行编号、固定 Head、修改摘要、验证 Profile、验证结果、限制和“等待 `xyh202131` 审查”。净化模型文本、HTML、图片、外链和意外 mention。

- [ ] **Step 5: 处理非原子发布失败**

每次 blob/tree/commit/ref/PR 写调用都先把当前 `PublicationStage` 和目标写入本地 receipt，再调用一次；收到成功响应才将该 `WriteReceipt.state` 标为 `CREATED` 并记录全部已知对象 ID。调用一旦因连接中断、读取超时或响应不完整而结果不确定，绝不重放写请求：只允许通过对象 SHA、精确 ref target，以及同时匹配 qykw author、run marker、head ref/commit 与 base 的 Draft PR 做有界只读 reconciliation。能确认存在则标 `CREATED`，确认未发出或权威读取确认不存在才标 `NOT_CREATED`，仍无法判定则标 `UNKNOWN`。不得把“请求报错”直接等同为“未创建”。

commit 已创建但完整 tree/diff/`workspace_tree_digest` 复核失败，或 ref 前取消、授权/Head/base/分支发生变化时，返回 `partial=True`、`branch_state=NOT_CREATED`、已知 receipts 和相应错误码；ref 结果未知则 `branch_state=UNKNOWN`；ref 已创建但 PR 前检查失败时返回 `branch_state=CREATED`、`pull_state=NOT_CREATED`；PR 响应不确定且 reconciliation 无结论时返回 `pull_state=UNKNOWN`。所有结果携带精确 stage、blob/tree/commit SHA 中已知部分和 error code，交给后续只持有评论令牌的 `record_result` job 更新既有状态评论，明确区分不可达 Git 对象、未知写入和孤立分支；发布 job 本身不得持有评论令牌。禁止自动删除、覆盖或重试任何写入阶段。

- [ ] **Step 6: 运行 GREEN 与回归测试**

```powershell
py -3 -m unittest tests.test_qykw_change_publish tests.test_qykw_publish tests.test_qykw_tasks -v
```

- [ ] **Step 7: 提交**

```bash
git add tools/qykw/change_publish.py tests/test_qykw_change_publish.py
git commit -m "feat: publish verified qykw changes as draft pull requests"
```

### Task 6: 配置 Actions 凭据和执行隔离

**Files:**
- Create: `.github/workflows/qykw-change.yml`
- Modify: `.github/workflows/qykw-review.yml`
- Modify: `tools/qykw/__main__.py`
- Create: `tests/test_qykw_change_workflow.py`

**Job Contract:**

所有修改 jobs 固定运行在 `ubuntu-latest`；无网络验证只提供 Linux 预发布证明。Draft PR 创建后仍由仓库现有 CI 执行 Ubuntu/Windows 后端、前端生成物和服务 smoke 检查，qykw 不绕过或代替分支保护。

1. `authorize` 固定调用 `python -m tools.qykw --phase authorize-change`：显式 checkout 默认分支到 `controller` 后执行精确解析、权限、幂等、固定 Head、状态和 😄；只持有 `QYKW_REVIEW_TOKEN`，无推理密钥和发布令牌，不执行候选代码。
2. `prepare` 固定调用 `--phase prepare-change`：显式 checkout 默认分支到 `controller`，使用内置只读 GitHub token 查询快照/取消 marker，并用推理密钥生成结构化 Manifest；只读 token 不传给 Provider，不得引用任何 qykw 写令牌或 checkout PR Head。Provider 前后和 artifact 写出前均执行软取消检查。
3. `verify` 固定调用 `--phase verify-change`：无 qykw Secrets；分别 checkout 默认分支 `controller` 和固定 PR Head `candidate-source`，两者均 `persist-credentials: false`。宿主控制器从 `candidate-source` 的固定 Head/tracked-path 清单物化不含 `.git` 的临时候选目录；只有该临时候选目录挂入无网络容器，两个 checkout 和 controller artifact 目录都不挂载。宿主只使用内置只读 GitHub token 检查取消，且不传入容器。
4. `publish` 固定调用 `--phase publish-change`：显式 checkout 默认分支可信控制器，只持有当前仓库限定的 `QYKW_PUBLISH_TOKEN`，不持有评论令牌或推理密钥，不 checkout 或运行候选代码，只消费 Manifest 与成功证明。
5. `record_result` 固定调用 `--phase record-change-result`：以 `if: always()` 在 publish、verify 或 prepare 后运行，显式 checkout 默认分支可信控制器，只持有 `QYKW_REVIEW_TOKEN`；它从受信任 job outcome 与已校验的最小结构化 artifact 渲染 success/partial/failure/canceled 状态并更新该运行已有评论，不生成分支或 PR，也不持有推理或发布令牌。

第二阶段只把上述五个字面量加入 CLI phase 白名单。每个 phase 有独立版本化输入/输出 Schema 和环境变量 allowlist，启动时同时拒绝缺失的必需凭据与任何不属于本 phase 的 `QYKW_*` 凭据。workflow YAML 直接写死 phase；artifact、评论、Manifest 和模型字段都不能选择或改写 phase。审查 `publish` 与修改 `publish-change` 是两个互不分派的 handler，禁止在通用 publish 内根据 artifact 内容选择评论或代码写能力。

- [ ] **Step 1: 写工作流静态 RED 测试**

```powershell
py -3 -m unittest tests.test_qykw_change_workflow -v
```

Expected: FAIL because the separated jobs, permissions and shared concurrency group are absent.

逐 job 检查 `permissions`、Secrets、依赖顺序、固定 phase、phase Schema/环境 allowlist、所有 `uses:` 固定完整 commit SHA、每个 secret-bearing job 显式 `ref: ${{ github.event.repository.default_branch }}`/`path: controller`/`persist-credentials: false`、`--network none`、Artifact 保留期、禁止命令，以及与审查工作流完全相同的并发组 `qykw-${{ github.repository_id }}-pr-${{ github.event.pull_request.number || github.event.issue.number }}`、`cancel-in-progress: false` 和 `queue: max`。双 workflow 排队测试先占用共享组，再让 review no-op 与 change run 同时 pending，证明二者不会互相替换且 change 最终执行。负例把 `prepare-change` artifact 送入 `publish-change`、在 artifact 中伪造 phase、或向任一 job 注入禁止的 qykw token，均须在副作用前失败。工作流只监听 `issue_comment` 与 `pull_request_review_comment` 的 `created`/`edited`；不监听 PR opened、push 或 `synchronize`。

- [ ] **Step 2: 实现授权 job**

只在精确授权修改命令后创建运行，固定评论 ID、actor、PR、source Head SHA、target base SHA/ref、Profile 和幂等键。Reaction 失败只记录 warning。`concurrency.cancel-in-progress` 保持 `false`，事件重放返回既有运行。

- [ ] **Step 3: 实现 prepare 与 verify job 边界**

prepare 产物只含结构化 request 和 Manifest；Manifest 中不可避免的 `before/after` 精确片段是唯一允许的源码内容，不上传未修改文件或完整仓库快照。verify job 分别 checkout 默认分支控制器和固定 PR Head 的 `candidate-source`，二者均 `persist-credentials: false`；宿主控制器从固定 Head 物化不含 `.git` 的临时候选目录，只将该临时目录挂入沙箱。两个 checkout 与 controller artifact 目录不挂载，且不向容器传入任何 token 或 Secret。候选启动前，控制器在挂载范围外生成只读 runtime metadata（workflow run、固定镜像摘要、Profile 和规范命令摘要）；Attestation 在命令完成后由同一容器外控制器生成，publish 同时校验两类 artifact 的 provenance 与字段。Artifacts 保留 1 天，不包含原始模型响应、完整评论、Secrets、宿主环境或 Manifest 之外的源码。

- [ ] **Step 4: 实现 publish 与失败记录边界**

`publish-change` 不运行仓库测试，不调用推理后台；只有 `validate_attestation()` 对同一 workflow run、Schema、run/source/target/base/ref、Manifest、可信 Profile/镜像、完整有序命令结果、`output_files`、output/workspace 摘要、`success=True` 与 `canceled=False` 全部验证通过才可运行。`record-change-result` 不信任自由文本 job 输出，只消费白名单枚举、对象 ID、三态写入结果和摘要。`record_result` job 用最小评论权限更新最终状态，且没有发布令牌时不得尝试写分支。

- [ ] **Step 5: 原子切换修改命令路由所有权**

不新增或修改 TOML 开关键；规格中的命令和 `code_writers = ["xyh202131"]` 保持不变。与修改工作流同一提交更新审查入口：`CommandMode.CHANGE` 在审查 workflow 明确 no-op，在修改 workflow 才执行鉴权和创建运行。静态与集成测试断言一条授权 `修复/实现` 评论只产生一个 RunRecord、一个 😄 和一条状态评论。

- [ ] **Step 6: 运行 GREEN、审查工作流回归和全量测试**

```powershell
py -3 -m unittest tests.test_qykw_change_workflow tests.test_qykw_workflow -v
py -3 -m unittest discover -s tests -v
```

- [ ] **Step 7: 提交**

```bash
git add .github/workflows/qykw-change.yml .github/workflows/qykw-review.yml tools/qykw/__main__.py tests/test_qykw_change_workflow.py
git commit -m "ci: isolate qykw authorized change jobs"
```

### Task 7: 完成生命周期集成、文档和真实验收门禁

**Files:**
- Create: `tests/test_qykw_change_integration.py`
- Create: `tests/fixtures/qykw_change/sample.py`
- Create: `docs/qykw-authorized-change.md`
- Modify: `README.md`

- [ ] **Step 1: 写端到端 Fake 集成 RED 测试**

```powershell
py -3 -m unittest tests.test_qykw_change_integration -v
```

Expected: FAIL because the authorized change lifecycle is not wired end to end.

用 Fake Gateway、Provider、Executor 和 StateStore 覆盖：成功修复、成功实现、未授权、测试失败、Head 漂移、授权撤销、取消、输出/完整工作区摘要不符、重复事件、发布前分支冲突、commit 后竞态、ref 后取消，以及 blob/tree/commit/ref/PR 的确定失败与不确定响应。
同一 `修复/实现` 评论同时送入审查与修改 workflow 时，断言审查入口 no-op，且全系统恰好创建一个 RunRecord、一个 😄、一次补丁生成、一次验证和一个 Draft PR。

- [ ] **Step 2: 串联完整修改生命周期**

固定顺序：

```text
accepted → acknowledged → collecting → analyzing
→ validating → testing → publishing → completed
```

同一评论重放不得重复推理、验证、分支或 PR。所有在首次 ref/PR 调用前终止的失败场景断言对应写调用为零；进入 ref/PR 写阶段后的确定失败或不确定响应按 receipts 断言每种写操作最多一次、绝不重试，并允许 reconciliation 后仍为 `UNKNOWN`。

- [ ] **Step 3: 验证发布契约和身份**

成功场景恰好创建一个 Draft PR；target base、source Head parent、分支名、状态评论、运行编号和验证摘要正确。Fake authenticated user 固定为 `login="qykw"`、`database_id=12345`，断言 author/committer 均为 `qykw <12345+qykw@users.noreply.github.com>`；提交消息不得包含用户、Codex、OpenAI、模型、工具或任何 `Co-Authored-By` trailer。

- [ ] **Step 4: 编写用户和运维文档**

说明授权人、精确命令、默认只读、分支命名、Draft PR、固定验证 Profile、停止语义、跨仓 source Head 第一版限制、失败零代码写入边界、不可达对象/孤立分支的人工处理、所需 Secrets 和不需要域名。不得公开推理后台或模型名称。

- [ ] **Step 5: 运行全量安全和覆盖率门禁**

```powershell
py -3 -m compileall -q agents core evalkit tools build_showcase.py cli.py config.py orchestrator.py server.py
py -3 -m unittest discover -s tests -v
py -3 -m coverage run --branch --source=tools.qykw -m unittest discover -s tests -p "test_qykw*.py" -v
py -3 -m coverage json -o qykw-coverage.json
py -3 tools/check_qykw_coverage.py qykw-coverage.json --line 95 --branch 90
```

完成后删除未跟踪的 `qykw-coverage.json`，不得提交测试产物。

- [ ] **Step 6: 执行真实 GitHub 验收门禁**

另获一次明确外部写入授权后，先由仓库所有者配置仅限当前仓库的 `QYKW_PUBLISH_TOKEN`，再在专用测试 PR 依次验证：授权修复成功、未授权请求、测试失败、`停止`、重复评论和 Head 漂移。实现代码不自行修改 Secrets；真实测试不并入普通 CI，不自动合并或删除产生的 PR/分支。

- [ ] **Step 7: 提交**

```bash
git add tests/test_qykw_change_integration.py tests/fixtures/qykw_change/sample.py docs/qykw-authorized-change.md README.md
git commit -m "test: cover qykw authorized change lifecycle"
```

## Completion Gate

- 第一阶段真实测试 PR 已通过，第二阶段的 `修复/实现` 才能启用。
- 每个任务必须先看到指定 RED，再做最小实现并取得 GREEN；每项独立提交。
- 所有测试与覆盖率门槛通过后，仍需 `xyh202131` 审查生成的 Draft PR。
- 未经额外明确授权，不推送计划分支、不创建测试 PR、不调整仓库 Secrets 或规则集。
- 最终公开输出只能使用 qykw 身份，并明确代码修改来自授权命令、经过何种验证及仍有哪些限制。
