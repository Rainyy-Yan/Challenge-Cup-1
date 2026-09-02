# qykw 实习任务领取操作指南

本文面向 `qiyuankaiwu/agentedu` 的仓库管理员、Issue 维护者和贡献者，说明如何
配置、使用和排查 qykw 实习任务领取工作流。该能力只依赖 GitHub Actions 和
GitHub REST API，**不需要新增域名、服务器、数据库或第三方任务平台**。

当前交付包含控制器、工作流和本地自动化测试。真实 GitHub 仓库演练仍是上线
门禁；本文末尾的演练清单保持未勾选，在逐项验证前不得宣称该能力已经上线或
生产可用。

## 仓库管理员上线前配置

### 预先创建四个标签

qykw 只使用以下四个固定标签，不会创建、重命名或覆盖标签：

| 标签 | 含义 | 谁负责设置 |
| --- | --- | --- |
| `intern:claimable` | Open Issue 当前允许领取 | 管理员创建；维护者将合适的 Issue 标为可领取 |
| `status:in-progress` | 唯一领取人正在处理 | qykw 在领取、释放和 PR 回退时维护 |
| `status:in-review` | 已关联领取人的活动 PR，正在审查 | qykw 在 PR 生命周期中维护 |
| `status:blocked` | Issue 暂停领取或释放 | 管理员创建并按人工判断设置或移除 |

上线前必须在仓库 Settings → Issues → Labels 中确认四个标签名称逐字匹配。
颜色和说明可以自行设置，但不要创建大小写或标点不同的近似标签。

一个初始可领取 Issue 应同时满足：

- Issue 是普通的 Open Issue，而不是 PR；
- 只有 `intern:claimable`，没有三个 `status:*` 标签；
- 没有 Assignee；
- 没有 qykw 已冻结的活动 PR 绑定。

### 配置 `QYKW_TOKEN`

在仓库 Settings → Secrets and variables → Actions 中创建 Repository secret：

```text
QYKW_TOKEN
```

Secret 必须是仓库限定、可撤销的 token，并满足以下条件：

- 调用 `GET /user` 时登录名精确为 `qykw`；控制器会在每次写入前校验身份；
- 仅能访问目标仓库，并能读取 Issue、Issue/PR 会话评论和 PR 状态；
- 能添加 😄 Reaction、增删单个 Assignee、增删上述固定标签、创建或更新
  Issue/PR 会话评论，以及关闭 Issue；
- 不把 token 写入源码、配置文件、Issue、PR、Actions 产物或日志。

工作流只在领取和联动 job 内把 `secrets.QYKW_TOKEN` 映射为运行时
`QYKW_INTERN_TOKEN`。`permissions` 只限制 GitHub 自动生成的 `GITHUB_TOKEN`，不会缩小
`QYKW_TOKEN` 自身的权限。因此还必须在 token 发行侧将它限制到本仓库。由于审查、领取和发布
阶段复用该 Secret，其权限是这些 GitHub 写入阶段所需权限的并集；job 隔离只减少暴露面。
若 token 不是 `qykw` 身份，控制器以 `bot_identity_mismatch` 停止，
不会继续修改 Issue。

还应确认仓库允许 `qykw` 给目标贡献者分配 Issue。若组织或仓库的 Assignee
资格策略拒绝该用户，领取会进入可安全重放的失败状态，而不会公开宣称领取成功。

### 工作流权限矩阵

`.github/workflows/qykw-intern.yml` 顶层固定为 `contents: none`。各 job 仅在自身
范围内提升 GitHub job token 权限：

| job | 事件与职责 | `contents` | `issues` | `pull-requests` | 运行时凭据 |
| --- | --- | ---: | ---: | ---: | --- |
| `issue_command` | 普通 Issue 的新评论命令 | `read` | `write` | 未授予 | `QYKW_INTERN_TOKEN`（来自 `QYKW_TOKEN`） |
| `resolve_pr` | 只读解析 PR 已冻结 marker 或规范 `Closes #N` | `read` | `read` | `read` | 仅 `GITHUB_TOKEN` |
| `reconcile_pr` | 同步 Issue–PR 生命周期 | `read` | `write` | `read` | `QYKW_INTERN_TOKEN`（来自 `QYKW_TOKEN`） |

工作流不需要 `contents: write`、`actions: write` 或管理权限。所有 checkout 都只
检出事件仓库的默认分支，设置 `persist-credentials: false`，不会检出或执行贡献者
PR Head。现有 `.github/workflows/qykw-review.yml` 继续负责同一 PR 事件的代码初审；
领取工作流只维护领取与审查状态，不重复触发或伪造审查。

`resolve_pr` 若既找不到可信冻结 marker，也找不到规范 `Closes #N`，会以成功状态返回
空的 Issue 输出；`reconcile_pr` 随即被跳过。因此仓库中与实习领取无关的普通 PR 是
安全 no-op，不会把预期的无关事件记录成 Actions 失败。

## 贡献者命令

只在普通 Open Issue 的新评论中使用以下精确命令：

```text
/intern-assign
/intern-unassign
/intern-status
```

- `/intern-assign`：领取当前 Issue。第一条合法领取按数值 `comment_id` 从小到大
  胜出，而不是按 Actions job 的启动时间判断。
- `/intern-unassign`：释放当前 Issue。仅唯一领取人或 `xyh202131` 可以执行；存在
  活动关联 PR 或 `status:in-review` 时不允许释放。
- `/intern-status`：只读查询当前状态，不修改 Assignee 或标签。

命令区分大小写，不接受参数、前后缀、全角替代或零宽字符。它必须是评论中第一条
非空、非引用、非代码、非 HTML 注释的可见文本，并独占该行；命令行之后可以写说明：

```text
/intern-assign

我计划先补回归测试，再提交实现。
```

引用、围栏/缩进/行内代码中的命令不会触发，PR 会话评论不会触发，编辑既有评论也
不会触发。控制器要求 webhook 中 `created_at == updated_at`，执行前还会重读触发评论，
核对作者、命令、创建时间且确认仍未编辑；因此先发普通文本再改成命令，或命令排队后
被修改，都不会执行。每个有效命令会先由 `qykw` 添加 😄 Reaction。对于领取和释放，Reaction
是后续写入的硬前置；失败时不修改 Assignee、标签或领取 marker。

## 状态与生命周期

| 事件 | 必要前置状态 | qykw 收敛后的状态 |
| --- | --- | --- |
| 维护者开放领取 | Open、无 Assignee | `intern:claimable` |
| `/intern-assign` | 可领取且未阻塞 | 唯一 Assignee 为命令作者；移除 `intern:claimable`；添加 `status:in-progress` |
| 合法 PR 首次关联 | PR 作者是唯一领取人；Issue 正在处理 | 在 Issue 和 PR 评论冻结同一绑定；移除 `status:in-progress`；添加 `status:in-review` |
| 已绑定 PR `edited` | 已有可信冻结绑定 | 继续使用冻结的 Issue，不因 PR 正文变化而换绑 |
| 已绑定 PR `reopened` | 原绑定仍一致 | 恢复或保持 `status:in-review` |
| PR 未合并关闭 | 已有可信冻结绑定 | 清除 Issue 侧活动绑定；移除 `status:in-review`；恢复 `status:in-progress`，Issue 保持 Open |
| PR 合并后关闭 | GitHub API 实时确认 `merged=true` | 移除进度/审查标签并关闭 Issue |
| `/intern-unassign` | 由领取人或 `xyh202131` 发起，且没有活动 PR/审查 | 移除 Assignee 和 `status:in-progress`；恢复 `intern:claimable` |
| 添加 `status:blocked` | 人工阻塞 | 新领取与释放停止；管理员排查并决定何时移除 |

一个 Issue 同一时间只能有一个活动 PR。工作流先以
`repository_id + event_name + pull_number` 串行化同一 PR 的完整 `resolve_pr → reconcile_pr`
流程，防止两个编辑事件在解析可变正文时交错；写入阶段再以
`repository_id + issue_number` 与 Issue 命令共用第二层互斥。Issue 评论事件在第一层按
Issue 编号、在写入层也按 Issue 编号排队。两个不同 PR 不能用各自 PR 编号绕过 Issue
级互斥。

## PR 正文写法

首次关联时，PR 正文必须在可见的非引用、非代码内容中恰好包含一个规范目标：

```markdown
## 变更说明

- 为领取的任务补充实现和测试。

Closes #17
```

`#17` 必须是当前 base 仓库中的普通 Issue，PR 作者必须与该 Issue 的唯一领取人
大小写不敏感相等。以下形式不会建立绑定：

- 没有 `Closes #N`，或出现两个及以上 Issue 引用；
- `Closes #017`、`Fixes #17`、`Resolves #17`、裸 `#17`；
- `Closes owner/repo#17`、URL、Markdown 链接；
- 引用或代码块中的 `Closes #17`；
- 目标是 PR、另一仓库的 Issue、非领取人的 Issue，或已有另一活动 PR。

PR 正文为 `null`、空字符串或不包含目标时属于无关 PR：只读解析成功但不输出 Issue
编号，写入 job 跳过；其他非字符串正文按无效 API 数据拒绝。

第一次合法关联后，绑定冻结为仓库、PR、Issue 和 PR 作者的组合。后续编辑 PR 正文
不能换绑；未合并关闭后，原领取人可以新建另一个合法 PR 再次关联。

只读解析 job 会把解析出的 Issue 编号作为并发键和
`QYKW_RESOLVED_ISSUE_NUMBER` 传给写入 job。写入前，控制器会重新读取 PR/marker 并
核对该编号；如果两阶段之间正文或绑定发生漂移，结果是安全 `conflict`（退出码 0），
且不会向解析出的 Issue 或新目标 Issue 写入。不要把该结果当作领取成功；管理员应
核对 PR 历史和 qykw marker，等待新的合法生命周期事件再收敛。

## 公开回复与冲突排查

正常结果包括：

```text
@用户名 已成功领取该 Issue。
该任务已由 @用户名 领取，请选择其他 Issue。
Issue 已成功释放。
Issue 当前可领取。
Issue 由 @用户名 处理中。
Issue 由 @用户名 提交，当前审查中。
```

出现以下回复时，qykw 会停止覆盖式写入，保留人工状态供管理员核对：

| 回复 | 优先核对 |
| --- | --- |
| `Issue 的可领取与进度标签冲突，已停止写入。` | `intern:claimable` 是否与进度/审查标签并存，或两个进度标签是否并存 |
| `Issue 的 Assignee 与领取状态冲突，已停止写入。` | Assignee 与领取/进度标签是否对应 |
| `Issue 已存在未经 qykw 领取流程确认的人工 Assignee，已停止写入。` | 领取前是否有人手工添加了与命令作者相同的 Assignee；先恢复无 Assignee 的可领取初态，再发新命令 |
| `Issue 存在多个 Assignee 冲突，已停止写入。` | 是否被人工设置了多个 Assignee |
| `Issue 审查标签与 Assignee 冲突，已停止写入。` | `status:in-review` 是否缺少唯一领取人 |
| `Issue 领取状态冲突，已停止写入。` | 领取中间状态是否被并发人工修改 |
| `Issue 的释放状态冲突，已停止写入。` | 无 Assignee 时的领取/进度标签是否矛盾 |
| `Issue 的实时 Assignee 与已固定领取人冲突，已停止写入。` | 实时 Assignee 是否仍为释放操作开始时冻结的领取人 |
| `Issue 的 Assignee 与进度标签冲突，已停止写入。` | 释放前是否仍为唯一 Assignee 且仅有 `status:in-progress` |
| `Issue 的 Assignee 在释放时发生冲突。` | 分步释放期间是否又被人工添加 Assignee |
| `Issue 状态冲突，请管理员核对 Assignee 和标签。` | `/intern-status` 看到的实时 Assignee 和四个受管标签 |

以下是安全拒绝，不应通过手工删除 qykw marker 强行绕过：

```text
Issue 已关闭，无法领取。
Issue 已阻塞，暂不可领取。
Issue 当前不可领取。
Issue 已关闭，无法释放。
Issue 已阻塞，已停止释放。
Issue 存在活动 PR 或正在审查，不允许释放。
@操作人 无权释放 @领取人 领取的 Issue。
```

排查时先保存现场，再同时核对 Issue 状态、全部 Assignee、四个受管标签，以及 Issue
和 PR 会话中由 `qykw` 发布的状态评论。不要只看标签，也不要删除、复制或手工改写
评论内的 `qykw-intern:v1` HTML marker；伪造、重复或互相矛盾的 marker 会被当作
冲突，而不是被自动覆盖。

## 幂等、失败与恢复

每个 Issue 命令使用以下幂等键：

```text
repository_id + issue_number + comment_id + operation
```

状态评论依次使用 `pending`、`reconciled`、`failed` 或 `conflict` 阶段。控制器每次
最多执行一个缺失写入，然后重新读取 GitHub 实时状态并验证后置条件。因此 Reaction、
Assignee、Label 或 Comment API 返回未知结果时，不会直接假定成功，也不会公开错误的
领取结果。

- `reconciled` 和 `conflict` 是 Issue 命令的终态；同一事件重放不会重复写入。
- `failed` 表示尚未确认收敛，公开评论为
  `处理暂时失败，重放同一事件将自动重试。`。在 Actions 中对失败 run 使用
  **Re-run jobs**，控制器会把同一 marker 转回 `pending`，重读并只补缺失步骤。
- PR 同步失败时，Issue/PR 评论使用 `处理暂时失败，等待安全重放。`；重跑同一
  `pull_request_target` 事件后仍以冻结 marker 为准，不以已经编辑的 PR 正文换绑。
- `conflict` 会以退出码 0 结束，避免无意义自动重试；必须由管理员先修复人工状态，
  再通过新的合法命令或新的 PR 生命周期事件触发重读。
- 操作性 `failed`、无效环境或 API 边界错误以退出码 1 和固定
  `::error title=qykw intern::<error_code>` annotation 结束。公开错误不包含 token、
  事件正文或 GitHub 响应正文。
- 若 Reaction 尚未成功，第一次命令不会创建领取 marker；恢复 GitHub API 或 token
  权限后重跑原 job 即可。不要通过人工补 Assignee 来模拟领取成功。

管理员只应修复已经确认的外部事实，例如移除误加的受管标签或恢复正确的唯一
Assignee；不要篡改 marker 来跳过状态机。若 marker 冲突无法判断来源，应暂停该
Issue 的自动操作、保存评论和 Actions 证据，再由仓库负责人决定恢复方案。

## 非目标

该工作流不会：

- 修改贡献者代码、checkout 或执行 PR Head；
- 批准、合并 PR，或向 PR 提交 review；
- 代替 `.github/workflows/qykw-review.yml` 的代码审查；
- 创建、重命名或覆盖标签，也不会删除意外 Assignee 来“修复”冲突；
- 接受跨仓库 Issue、任意 URL、任意 API 方法或动态标签；
- 新增域名、服务器、数据库、外部队列、推理调用或第三方任务系统；
- 保证多个 GitHub REST 写入具有数据库事务语义；
- 把本地测试通过等同于真实 GitHub 权限、并发和生命周期演练通过。

## 本地验证

运行时使用 Python 3.11 标准库。开发覆盖率依赖安装在 `requirements-dev.txt` 中；
先在仓库根目录执行：

```powershell
python -m pip install --disable-pip-version-check -r requirements-dev.txt
python -m unittest tests.test_qykw_intern_claim -v
python -m unittest discover -s tests -v
python -m coverage run --branch --source=tools.qykw -m unittest discover -s tests -p "test_qykw*.py" -v
python -m coverage json -o qykw-coverage.json
python tools/check_qykw_coverage.py qykw-coverage.json --line 95 --branch 90
python -m compileall -q tools tests
git diff --check
Remove-Item -LiteralPath qykw-coverage.json
```

覆盖率门禁分别要求语句覆盖率至少 95%、分支覆盖率至少 90%，不能用语句和分支
混合后的总百分比替代。完整测试在 Windows 上可能包含仓库已有并明确记录的平台
跳过；任何失败或新增未说明的跳过都必须先排查。

## 真实 GitHub 上线演练清单（尚未执行）

以下项目是人工上线门禁，当前全部保持未勾选：

- [ ] 在目标仓库确认四个精确标签均已创建，测试 Issue 初始状态只有
  `intern:claimable` 且无 Assignee。
- [ ] 确认 `QYKW_TOKEN` 只授权目标仓库、`GET /user` 返回 `qykw`，并验证
  Secret、日志和 Actions 产物均不泄露 token。
- [ ] 确认工作流已从默认分支运行，checkout 的 ref 是默认分支，且没有执行 PR Head。
- [ ] 在普通 Issue 验证三条精确命令、后续说明文本、😄 Reaction 和只读状态查询。
- [ ] 验证引用、代码、HTML 注释、参数、大小写变体、零宽字符、编辑评论和 PR 评论
  均不触发命令。
- [ ] 用两个账号近同时领取同一 Issue，确认较小合法 `comment_id` 胜出，另一账号收到
  `该任务已由 @用户名 领取，请选择其他 Issue。`。
- [ ] 重跑已成功领取的原 Actions job，确认 Assignee、标签和评论没有重复写入。
- [ ] 分别验证领取人、`xyh202131` 和无权限账号执行释放；验证活动 PR/审查期间拒绝释放。
- [ ] 临时制造可恢复 API 失败，确认不会误报成功；恢复后重跑原 job 并确认只补缺失步骤。
- [ ] 由唯一领取人创建正文仅含一个 `Closes #N` 的 PR，确认 Issue 和 PR marker 先固定，
  再进入 `status:in-review`。
- [ ] 验证错误作者、零/多个/非规范目标、跨仓目标、目标为 PR 和第二个活动 PR 均不写入。
- [ ] 编辑已绑定 PR 的正文为另一 Issue，确认绑定仍指向原 Issue。
- [ ] 未合并关闭 PR，确认原 Issue 恢复 `status:in-progress`；再打开或用新 PR 关联，确认
  生命周期仍按冻结绑定收敛。
- [ ] 合并 PR 后确认 GitHub API 实时 `merged=true`、Issue 关闭且审查标签移除；重跑关闭
  job 确认无重复写入。
- [ ] 确认 `.github/workflows/qykw-review.yml` 仍独立完成既有初审，领取工作流没有重复审查。
- [ ] 检查 Actions 权限、并发组和失败 annotation，确认没有新增域名、服务器、数据库、
  推理服务或外部任务平台依赖。
