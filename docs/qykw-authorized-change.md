# qykw 授权变更通道

qykw 的变更能力与代码审查完全分离。它只响应 PR 的 Issue 评论或 Review 评论中的精确命令：

```text
@qykw 修复 <明确问题>
@qykw 实现 <明确需求>
@qykw 停止
```

`修复` 和 `实现` 仅允许 `.github/qykw.toml` 中配置的 writer 发起，并要求该账号在仓库拥有
write、maintain 或 admin 权限。`停止` 追加独立取消标记，不删除已经创建的对象。

## 五阶段执行链

1. `authorize-change`：从默认分支的可信控制器解析事件，验证命令、writer、权限、PR 与固定 Head，并在确认后标记正在工作。
2. `prepare-change`：只读获取完整源码树，向推理服务发送经过筛选的上下文，生成受路径、大小和文件数限制的补丁清单。
3. `verify-change`：使用权限仅为 `contents: read`、`issues: read`、`packages: read` 的内置 `github.token` 登录私有 GHCR，在固定源码 SHA 上重放补丁，并用 `full` 配置执行后端、前端和冒烟检查。容器镜像仅由 `QYKW_VERIFICATION_IMAGE_REF` 提供，必须是 `registry/image@sha256:<digest>` 形式；容器无网络、只读挂载源码且不继承宿主环境。
4. `publish-change`：重新验证请求、补丁和证明后，以 qykw 身份创建内容对象、专用分支和 Draft PR。不会更新已有引用，也不会合并、批准或删除。
5. `record-change-result`：根据可信 job 结果记录 completed、partial、failed 或 canceled，并公开不含敏感数据的状态。

审查与变更工作流共享同一 PR 串行队列，避免评论、状态和发布相互越过。各 job 独立注入所需环境变量，
GitHub 写入令牌与推理密钥不会共存；checkout 均设置 `persist-credentials: false`；阶段制品只保留
1 天，不能作为权限或运行时事实的信任根。

仓库只需配置 `QYKW_TOKEN` 与 `MINIMAX_API_KEY`。工作流按 job 分别映射为运行时
`QYKW_REVIEW_TOKEN`、`QYKW_PUBLISH_TOKEN` 或 `QYKW_INFERENCE_API_KEY`；`verify-change`
只使用内置 job token 读取源码、Issue 和私有 GHCR 包，以及非敏感镜像变量，不接收上述两个仓库
Secret。GHCR 登录通过 `--password-stdin` 完成，后续容器仍只接受 digest-pinned 镜像引用。审查与发布复用同一个
`QYKW_TOKEN`，因此这里隔离的是 job 暴露面，而不是底层 GitHub 权限；该 token 必须持有所有 qykw
写入阶段所需权限的并集，并继续限制在本仓库。

## 人工门禁与故障边界

机器人只能创建 Draft PR。`xyh202131` 是最终审稿人，负责核对变更、CI、风险说明并决定是否合并。
网络中断或返回不确定时，发布流程停止并通过只读查询恢复；可能残留不可达的 Git 对象、已创建的
专用分支或 Draft PR，机器人不会自动清理。凭据不得写入源码、评论、日志或 Actions 制品。

当前仅完成代码、本地单元测试和静态工作流检查。Ubuntu 上的 digest-pinned Docker 镜像构建/运行，
以及真实 GitHub 仓库中的评论、权限、并发和 Draft PR 端到端测试仍是未完成门禁。在两项均通过前，
不得宣称授权变更通道已经上线或生产可用。
