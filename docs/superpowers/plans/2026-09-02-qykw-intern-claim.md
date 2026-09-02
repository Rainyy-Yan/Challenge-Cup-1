# qykw Issue Auto-Claim Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 Challenge-Cup 的普通 Issue 增加 `/intern-assign`、`/intern-unassign` 和 `/intern-status`，并将领取状态安全联动到领取人创建的 PR。

**Architecture:** 使用一个独立、确定性的 Python 控制器处理 Issue 命令与 PR 生命周期；GitHub 评论中的版本化 marker 保存可恢复状态，实时 Assignee/标签是外部事实。每个写入阶段都先标记 pending、只写一次、再读取核对；现有 qykw 审查工作流继续处理同一 PR 事件。

**Tech Stack:** Python 3.11 标准库、`unittest`、GitHub Actions、GitHub REST API。

**Spec:** `docs/superpowers/specs/2026-09-02-qykw-intern-claim-design.md`

## Global Constraints

- 只处理普通 Issue 的 `issue_comment.created`；命令必须是第一条有效文本且独占该行。
- 只 checkout 默认分支中的可信控制器，绝不 checkout 或执行贡献者代码。
- 每个 Issue 串行执行，真实领取顺序仍以最小有效 `comment_id` 为准。
- Reaction 是领取和释放写操作的硬前置；失败时不修改 Assignee、标签或 marker。
- 多个 GitHub 写入不伪装成原子事务；公开成功前必须重读并验证全部后置条件。
- 只信任 `qykw` 创建的严格 `qykw-intern:v1` marker；同一幂等键只能有一个终态。
- 仅领取人或 `xyh202131` 可释放；活动 PR 审查期间不允许释放。
- 不新增外部服务、数据库、推理调用、任意 URL、任意 GitHub 方法或动态标签写入。
- 不批准、不合并 PR，不修改贡献者代码，不写默认分支。
- 维持 qykw 语句覆盖率至少 95%、分支覆盖率至少 90%。
- Git 提交只使用已配置的用户身份，不添加 Codex、OpenAI、AI 或共同作者署名。

---

### Task 1: Strict event and command protocol

**Files:**
- Create: `tools/qykw/intern_claim.py`
- Create: `tests/test_qykw_intern_claim.py`

**Interfaces:**
- Produces: `InternCommand`, `IssueCommentEvent`, `PullLifecycleEvent`, `parse_intern_command(body)`, `normalize_issue_comment_event(payload)`, `normalize_pull_event(payload)`, `parse_closing_issue(body)`.
- Consumes: Python mappings loaded from `GITHUB_EVENT_PATH`; no GitHub network objects.

- [ ] **Step 1: Write failing parser tests**

  Add `TestInternCommandParsing` and `TestInternEventNormalization` with literal fixtures. Assert exact acceptance of a command on the first visible line, optional later explanation, and rejection of arguments, quoted/code/comment commands, zero-width mutations, edited events, PR comments, repository mismatch, booleans as IDs, and sender/comment-author mismatch. Add `TestClosingIssueParsing` for exactly one visible `Closes #17` and rejection of duplicates, URLs, cross-repo forms, leading zeroes and fenced text.

- [ ] **Step 2: Verify RED**

  Run: `python -m unittest tests.test_qykw_intern_claim.TestInternCommandParsing tests.test_qykw_intern_claim.TestInternEventNormalization tests.test_qykw_intern_claim.TestClosingIssueParsing -v`  
  Expected: FAIL because `tools.qykw.intern_claim` or the named interfaces do not exist.

- [ ] **Step 3: Implement the immutable protocol types and pure parsers**

  Use these exact public shapes:

  ```python
  class InternCommand(str, Enum):
      ASSIGN = "/intern-assign"
      UNASSIGN = "/intern-unassign"
      STATUS = "/intern-status"

  @dataclass(frozen=True)
  class IssueCommentEvent:
      repository: str
      repository_id: int
      issue_number: int
      comment_id: int
      actor_login: str
      command: InternCommand

  @dataclass(frozen=True)
  class PullLifecycleEvent:
      repository: str
      repository_id: int
      pull_number: int
      action: str
  ```

  Implement Markdown filtering locally without importing the permissive qykw mention parser. Keep accepted PR actions to `opened`, `edited`, `ready_for_review`, `reopened`, and `closed`.

- [ ] **Step 4: Verify GREEN**

  Run the Step 2 command.  
  Expected: all parser tests PASS with no output beyond unittest results.

- [ ] **Step 5: Commit**

  ```bash
  git add tools/qykw/intern_claim.py tests/test_qykw_intern_claim.py
  git commit -m "feat(qykw): parse intern claim events"
  ```

### Task 2: Repository-bound GitHub gateway and marker codec

**Files:**
- Modify: `tools/qykw/intern_claim.py`
- Modify: `tests/test_qykw_intern_claim.py`

**Interfaces:**
- Consumes: Task 1 normalized events.
- Produces: `IssueSnapshot`, `PullSnapshot`, `InternRecord`, `InternGateway`, `HttpInternGateway`, `encode_marker(record)`, `decode_marker(body)`, `reduce_records(records)`; records can be read from both Issue and PR conversation comments.

- [ ] **Step 1: Write failing gateway and codec tests**

  Add literal, queue-backed transport tests for exact methods and paths: issue/comment/PR GET, paginated issue comments, mandatory Reaction POST, add/remove fixed labels, add/remove one Assignee, update/close Issue, create/update status comment. Verify same-origin pagination, no redirects, 2 MiB response bound, strict JSON, no token/body leakage, and rejection of generic DELETE or arbitrary label methods. Add marker tests for exact key sets, duplicate JSON keys, wrong bot, repository mismatch, immutable operation identity and deterministic reduction by comment ID.

- [ ] **Step 2: Verify RED**

  Run: `python -m unittest tests.test_qykw_intern_claim.TestInternMarkerCodec tests.test_qykw_intern_claim.TestInternGitHubGateway -v`  
  Expected: FAIL because the gateway and marker types are absent.

- [ ] **Step 3: Implement the narrow API boundary**

  Define an injectable transport with the existing qykw signature and expose only these protocol methods:

  ```python
  class InternGateway(Protocol):
      def assert_bot_identity(self, expected_login: str = "qykw") -> None: ...
      def get_issue(self, issue_number: int) -> IssueSnapshot: ...
      def list_issue_comments(self, issue_number: int) -> tuple[IssueComment, ...]: ...
      def list_pull_comments(self, pull_number: int) -> tuple[IssueComment, ...]: ...
      def get_pull(self, pull_number: int) -> PullSnapshot: ...
      def add_reaction(self, comment_id: int) -> None: ...
      def add_assignee(self, issue_number: int, login: str) -> None: ...
      def remove_assignee(self, issue_number: int, login: str) -> None: ...
      def add_label(self, issue_number: int, label: str) -> None: ...
      def remove_label(self, issue_number: int, label: str) -> None: ...
      def create_comment(self, issue_number: int, body: str) -> int: ...
      def update_comment(self, comment_id: int, body: str) -> None: ...
      def close_issue(self, issue_number: int) -> None: ...
  ```

  Hard-code the four allowed labels, validate all IDs/logins/repository paths, authenticate writes with `QYKW_INTERN_TOKEN`, and return fixed `InternError(code)` values.

- [ ] **Step 4: Verify GREEN**

  Run the Step 2 command.  
  Expected: gateway and marker tests PASS.

- [ ] **Step 5: Commit**

  ```bash
  git add tools/qykw/intern_claim.py tests/test_qykw_intern_claim.py
  git commit -m "feat(qykw): add intern GitHub state boundary"
  ```

### Task 3: Claim, release, status, and recovery state machine

**Files:**
- Modify: `tools/qykw/intern_claim.py`
- Modify: `tests/test_qykw_intern_claim.py`

**Interfaces:**
- Consumes: Task 2 `InternGateway` and records.
- Produces: `InternClaimService.handle_issue_event(event) -> InternOutcome`.

- [ ] **Step 1: Write failing state-machine tests**

  Use a stateful in-memory fake that exposes real Issue/record state. Cover single success, two contenders processed by comment ID, replay with zero duplicate writes, fixed already-claimed reply, closed/blocked/not-claimable/assigned conflict, Reaction failure before mutation, assign/label/comment failure recovery, unauthorized release, owner and `xyh202131` release, active-PR release rejection, repeated release, and status reports for claimable/in-progress/in-review/conflict.

- [ ] **Step 2: Verify RED**

  Run: `python -m unittest tests.test_qykw_intern_claim.TestInternClaimService -v`  
  Expected: FAIL because `InternClaimService` does not exist.

- [ ] **Step 3: Implement ordered reconciliation**

  Implement this fixed command sequence:

  ```text
  authenticate qykw
  → paginate comments and reduce trusted records
  → sort unprocessed valid commands by comment_id
  → add mandatory 😄
  → create/update pending marker
  → re-read Issue and records
  → perform at most one missing mutation
  → re-read and verify all postconditions
  → publish terminal success, rejection, conflict, or recoverable failure
  ```

  On assignment require exactly one Assignee equal to the claimant, no claimable label, and in-progress present before success. On release require no Assignee, claimable present, and both progress labels absent. Never overwrite unrelated labels or remove an unexpected Assignee.

- [ ] **Step 4: Verify GREEN and regression**

  Run: `python -m unittest tests.test_qykw_intern_claim.TestInternClaimService -v`  
  Expected: all claim service tests PASS.  
  Run: `python -m unittest discover -s tests -p "test_qykw*.py" -v`  
  Expected: all qykw tests PASS; existing platform skips remain skips.

- [ ] **Step 5: Commit**

  ```bash
  git add tools/qykw/intern_claim.py tests/test_qykw_intern_claim.py
  git commit -m "feat(qykw): reconcile intern issue claims"
  ```

### Task 4: Pull request lifecycle binding

**Files:**
- Modify: `tools/qykw/intern_claim.py`
- Modify: `tests/test_qykw_intern_claim.py`

**Interfaces:**
- Consumes: Task 1 `parse_closing_issue`, Task 2 snapshots/records, Task 3 service.
- Produces: `InternClaimService.handle_pull_event(event) -> InternOutcome`.

- [ ] **Step 1: Write failing lifecycle tests**

  Cover matching PR author and sole claimant; zero/multiple/invalid `Closes`; target is another PR; wrong repository/base; author mismatch; second active PR; frozen binding despite edited body; reopened bound PR; closed-unmerged restoration; merged Issue closure; duplicate event replay; missing/conflicting marker; and API failure recovery without changing the wrong Issue.

- [ ] **Step 2: Verify RED**

  Run: `python -m unittest tests.test_qykw_intern_claim.TestInternPullLifecycle -v`  
  Expected: FAIL because pull lifecycle handling is absent.

- [ ] **Step 3: Implement frozen Issue–PR binding**

  For first association, re-read PR and Issue, require one canonical target and author/claimant equality, then persist the immutable binding in both the Issue status and a PR status comment before moving `status:in-progress` to `status:in-review`. For later events, resolve and use only the stored PR-comment binding. On `closed`, re-read `merged`; close the Issue only when true, otherwise clear the active binding and restore in-progress. Do not call merge, approve, review submission, branch mutation, or code execution APIs.

- [ ] **Step 4: Verify GREEN**

  Run the Step 2 command.  
  Expected: all lifecycle tests PASS.

- [ ] **Step 5: Commit**

  ```bash
  git add tools/qykw/intern_claim.py tests/test_qykw_intern_claim.py
  git commit -m "feat(qykw): link intern claims to pull requests"
  ```

### Task 5: CLI and trusted GitHub Actions workflow

**Files:**
- Create: `.github/workflows/qykw-intern.yml`
- Modify: `tools/qykw/intern_claim.py`
- Modify: `tests/test_qykw_intern_claim.py`

**Interfaces:**
- Consumes: `GITHUB_EVENT_PATH`, `GITHUB_EVENT_NAME`, `GITHUB_ACTION`, `GITHUB_API_URL`, `GITHUB_REPOSITORY`, `QYKW_INTERN_TOKEN`.
- Produces: `python -m tools.qykw.intern_claim` exit status, a bounded resolver artifact, `issue_number` job output, and bounded GitHub annotations.

- [ ] **Step 1: Write failing CLI and workflow contract tests**

  Test safe environment validation, malformed/missing event files, bounded JSON input, PR resolver output validation, idempotent success/no-op exit 0 and typed operational failure exit 1. Parse the workflow as text/YAML-compatible data and assert the exact event types, Issue-command and resolved-PR write jobs both use per-Issue `queue: max` with `cancel-in-progress: false`, top-level `contents: none`, resolver has no write permission, mutation jobs have minimal permissions, default-branch checkout, `persist-credentials: false`, Python 3.11, full action SHA pins, no candidate checkout, and absence of inference/change secrets.

- [ ] **Step 2: Verify RED**

  Run: `python -m unittest tests.test_qykw_intern_claim.TestInternCli tests.test_qykw_intern_claim.TestInternWorkflow -v`  
  Expected: FAIL because the CLI/workflow do not yet satisfy the contract.

- [ ] **Step 3: Implement CLI and workflow**

  The mutating jobs must invoke only:

  ```yaml
  - name: Run qykw intern controller
    working-directory: controller
    env:
      QYKW_INTERN_TOKEN: ${{ secrets.QYKW_INTERN_TOKEN }}
    run: python -m tools.qykw.intern_claim --phase issue-command
  ```

  Checkout `${{ github.event.repository.default_branch }}` into `controller`. Use an Issue command job for non-PR comments. For `pull_request_target`, add a read-only `resolve_pr` job that invokes `--phase resolve-pr`, validates and exports the Issue number, followed by a `reconcile_pr` job that invokes `--phase reconcile-pr` and enters `qykw-intern-${{ github.repository_id }}-${{ needs.resolve_pr.outputs.issue_number }}` concurrency. Keep existing `qykw-review.yml` unchanged and document that its current initial-review subscription supplies code review for the same PR event.

- [ ] **Step 4: Verify GREEN**

  Run the Step 2 command.  
  Expected: all CLI/workflow tests PASS.

- [ ] **Step 5: Commit**

  ```bash
  git add .github/workflows/qykw-intern.yml tools/qykw/intern_claim.py tests/test_qykw_intern_claim.py
  git commit -m "ci(qykw): run intern claim lifecycle"
  ```

### Task 6: Operator documentation and complete verification

**Files:**
- Create: `docs/qykw-intern-claim.md`
- Modify: `tests/test_qykw_intern_claim.py` only if a behavior gap is found through a new failing test.
- Modify: `tools/qykw/intern_claim.py` only after that failing test demonstrates the gap.

**Interfaces:**
- Consumes: all prior task behavior and the approved spec.
- Produces: operator guide, complete test evidence and coverage evidence.

- [ ] **Step 1: Write the operator guide**

  Document exact commands, prerequisites for four labels and `QYKW_INTERN_TOKEN`, permission matrix, lifecycle table, PR body example, conflict messages, recovery semantics, non-goals, local test commands, and a clearly unchecked real-GitHub rollout checklist. State that no domain or server is required.

- [ ] **Step 2: Run focused and complete tests**

  Run: `python -m unittest tests.test_qykw_intern_claim -v`  
  Expected: all intern tests PASS.  
  Run: `python -m unittest discover -s tests -v`  
  Expected: all tests PASS with only documented platform skips.

- [ ] **Step 3: Enforce qykw coverage**

  Run: `python -m coverage run --branch --source=tools.qykw -m unittest discover -s tests -p "test_qykw*.py" -v`  
  Run: `python -m coverage json -o qykw-coverage.json`  
  Run: `python tools/check_qykw_coverage.py qykw-coverage.json --line 95 --branch 90`  
  Expected: statement coverage at least 95% and branch coverage at least 90%. Remove the generated `qykw-coverage.json` after recording the result.

- [ ] **Step 4: Compile and inspect the final diff**

  Run: `python -m compileall -q tools tests`  
  Run: `git diff --check`  
  Expected: both commands exit 0.

- [ ] **Step 5: Commit**

  ```bash
  git add docs/qykw-intern-claim.md tests/test_qykw_intern_claim.py tools/qykw/intern_claim.py
  git commit -m "docs(qykw): document intern claim workflow"
  ```
