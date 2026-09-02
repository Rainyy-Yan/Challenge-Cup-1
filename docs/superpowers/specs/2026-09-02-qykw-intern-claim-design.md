# qykw Issue 自动领取设计

状态：已确认  
日期：2026-09-02  
负责人：启元开物  
最终审稿人：`xyh202131`

## 1. 目标与边界

贡献者可在普通 Open Issue 下使用 `/intern-assign` 领取任务，使用
`/intern-unassign` 释放任务，使用 `/intern-status` 查询状态。一个 Issue
同一时间只有一个领取人。qykw 负责 Assignee、标签、Issue–PR 关联和审查状态，
不替贡献者修改代码，不批准或合并 PR。

系统只依赖 GitHub Actions 和 GitHub REST API，不新增域名、服务器、数据库或
第三方任务平台。不 checkout 或执行贡献者分支代码；工作流只 checkout 默认分支
中的可信控制器。

## 2. 命令协议

只处理 `issue_comment.created`，并拒绝 PR 会话。命令必须是第一条非空、非引用、
非代码、非 HTML 注释文本，且独占该行；后续文本可作为说明但不参与命令解析。
命令区分大小写，不接受参数、前后缀、全角替代、零宽字符或编辑后的评论。

```text
/intern-assign
/intern-unassign
/intern-status
```

每个有效命令先给触发评论添加 😄。领取和释放把 Reaction 成功作为前置条件；
Reaction 失败时不修改 Assignee、标签或领取状态。状态查询不修改 Issue。

## 3. 状态和标签

```text
intern:claimable → status:in-progress → status:in-review → Issue closed
                           ↓
                    intern:claimable
```

- 可领取：Issue 为 Open，带 `intern:claimable`，不带 `status:blocked`，且没有
  Assignee 或有效领取标记。
- 领取成功：唯一 Assignee 为领取人；移除 `intern:claimable`；添加
  `status:in-progress`。
- 进入审查：移除 `status:in-progress`；添加 `status:in-review`。
- 释放：仅在没有活动关联 PR 时允许；移除领取人和状态标签，恢复
  `intern:claimable`。
- PR 未合并关闭：清除活动 PR 绑定，恢复 `status:in-progress`。
- PR 合并：关闭 Issue，并移除 `status:in-review`。

标签必须由仓库管理员预先创建。qykw 不创建、重命名或覆盖其他标签。
Assignee 多于一人、Assignee 与领取标记不一致或人工修改造成冲突时，qykw
停止写入并发布明确的冲突说明，不自动删除人工设置。

## 4. 并发、幂等与恢复

工作流使用每个 Issue 独立的并发组：

```yaml
concurrency:
  group: qykw-intern-${{ github.repository_id }}-${{ github.event.issue.number || github.event.pull_request.number }}
  queue: max
  cancel-in-progress: false
```

GitHub 的运行开始顺序不等于评论创建顺序，因此处理器必须分页读取全部评论，
严格解析命令，并按数值 `comment_id` 从小到大处理尚未终结的操作。第一条合法
领取命令胜出；后续领取统一回复：

```text
该任务已由 @用户名 领取，请选择其他 Issue。
```

每个操作使用 `repository_id + issue_number + comment_id + operation` 作为幂等键。
qykw 状态评论包含版本化 HTML marker，固定仓库、Issue、触发评论、执行者、操作、
领取人、关联 PR 和阶段。只信任登录名为 `qykw` 的严格 marker。

GitHub REST 的 Reaction、Assignee、Label 和 Comment 写入没有跨资源事务。实现采用
`pending → 写入一次 → 重新读取 → reconciled/failed/conflict`：不误报成功，事件
重放不重复写，未知结果只做读取核对，后续运行自动收敛。允许短暂中间状态，但
任何中间状态都不被公开宣称为领取成功。

## 5. PR 关联与审查

`pull_request_target` 监听 `opened`、`edited`、`ready_for_review`、`reopened` 和
`closed`。控制器从可信 API 重新读取 PR；不使用事件正文作为最终事实。

PR 正文仅接受非引用、非代码块中的一个规范 `Closes #N`。拒绝零、重复或多个
目标、跨仓引用、URL、裸 `#N` 和 `Fixes/Resolves`。目标必须是当前 base 仓库的
普通 Issue；PR 作者必须与唯一领取人大小写不敏感相等。

第一次合法关联后冻结 `repository_id + PR number + Issue number + PR author`。
一个 Issue 只能有一个活动 PR。后续正文编辑不能换绑；未合并关闭后，原领取人
可以用新 PR 再次关联。PR 合并状态以关闭事件后的实时 API 结果为准。

代码审查继续由现有 `qykw-review.yml` 对同一可信 PR 事件触发，并依赖其既有初审
幂等状态；领取工作流不伪造 `@qykw` 评论、不重复实现推理链路，也不向贡献者代码
暴露审查或 Issue 写入凭据。

## 6. 权限与安全

工作流顶层为 `contents: none`。Issue 命令 job 使用 `contents: read` 和
`issues: write`；PR 生命周期 job 额外使用 `pull-requests: read`。不需要
`contents: write`、`pull-requests: write`、`actions: write` 或管理权限。

所有 Actions 使用完整提交 SHA；checkout 固定默认分支并设置
`persist-credentials: false`。API 网关只构造当前 GitHub origin 和当前仓库下的固定
路径，拒绝重定向、超大响应、错误 JSON、仓库错配和不安全方法。公开错误只包含
固定错误码，不泄露 token、事件正文或响应正文。

仅当前领取人或 `xyh202131` 可执行 `/intern-unassign`；管理者身份以精确登录名
匹配，不能由评论参数指定其他用户。

## 7. 交付与验收

主要交付文件：

```text
.github/workflows/qykw-intern.yml
tools/qykw/intern_claim.py
tests/test_qykw_intern_claim.py
docs/qykw-intern-claim.md
```

验收覆盖命令边界、单人领取、并发首评者、事件重放、关闭/阻塞/PR 评论拒绝、
释放权限、Reaction 与 API 失败恢复、PR 作者核对、单一 `Closes`、进入审查、
未合并回退和合并关闭。全部现有测试通过；qykw 维持语句覆盖率至少 95%、分支
覆盖率至少 90%。真实 GitHub 演练和标签创建属于上线门禁，不在本地测试中伪装完成。
