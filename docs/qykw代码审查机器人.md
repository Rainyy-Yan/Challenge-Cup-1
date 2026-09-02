# qykw 代码审查机器人

qykw 是启元开物独立工程机器人。默认审查通道只提供分析、计划、审查、状态和软停止能力，不执行 PR 代码，也不修改、批准或合并 PR。授权变更使用另一条隔离工作流；规则见 [qykw 授权变更通道](qykw-authorized-change.md)。

## 自动触发规则

- 非 Draft PR 在 `opened` 时自动审查一次。
- Draft PR 在第一次 `ready_for_review` 时自动审查一次。
- `reopened` 只在从未成功完成首次审查时补跑。
- 后续 `synchronize` 不自动触发；需要复查时使用 `@qykw 复审`。
- 同一事件或评论重放只返回既有运行，不重复推理或发布。

## 评论命令

命令必须出现在第一段有效正文中，并使用精确提及：

```text
@qykw <指令> [范围或要求]
```

| 指令 | 行为 |
| --- | --- |
| `帮助` | 返回命令和权限说明 |
| `分析 <问题>` | 只读分析固定 Head 上下文 |
| `计划 <需求>` | 输出只读实施建议和验证方案 |
| `审查 [范围]` | 执行分诊、深审和反证验证 |
| `复审 [范围]` | 对最新 Head 启动显式复审 |
| `状态` | 返回当前运行阶段和状态 |
| `总结` | 汇总结论、覆盖和限制 |
| `修复 <问题>`、`实现 <需求>` | 转入授权变更通道；仅配置的 writer 可发起 |
| `停止` | 为目标运行追加软取消标记 |

引用、围栏代码、行内代码、HTML 注释、邮箱、相似账号、全角符号和含零宽字符的伪装提及不会触发。未知或含糊命令保持只读，不能由推理结果提升权限。

## 输出与恢复

审查先更新一条总结，再发布经本地行号验证的 `COMMENT` 行评。总结包含运行编号、固定 Head、覆盖范围、验证说明和限制。零问题只表示未发现有充分证据的问题，不代表批准或绝对安全。

- Reaction 失败只记录警告，不阻断已授权任务。
- Head 漂移会将运行标记为 `stale`，不会发布旧行号评论。
- 后端或执行异常会将运行标记为 `FAILED`；`REVIEW`、`REREVIEW` 记录 `review_failed`，`ANALYZE`、`PLAN` 记录 `analyze_failed`，且不公开请求、响应或凭据。
- 推理供应商异常同时写入固定的 `inference_<类别>` Actions 错误码，便于区分超时、DNS、TLS、限流和无效响应；错误码来自受控枚举，不包含 URL、响应正文或凭据。
- 结构化结果不符合约束时不记作上述执行失败；运行会安全返回“审查未完成”和零条 findings。
- 推理适配器只使用标准 `input` 与 `instructions` 字段；内部 `maximum` 策略映射为后端接受的最高兼容档。完成响应的 `output_text` 会再次执行严格 JSON 解码和本地 Schema 校验，`incomplete`、`failed` 或用量异常均按无效结果关闭。
- 停止标记独立追加；并发中的旧状态保存不能覆盖它。
- 状态与历史评论使用分页读取，不能假定目标记录在前 100 条内。

## 双通道与工作流隔离

审查链路使用默认分支中的可信控制器，并拆分为 `authorize`、`analyze`、`publish` 和 `record_failure`。独立 `control` 工作流只处理精确的 `停止` 命令。所有 checkout 都禁用持久化凭据，不检出或执行 PR Head。

授权变更链路拆分为 `authorize-change`、`prepare-change`、`verify-change`、`publish-change` 和 `record-change-result`。它只接受 PR 评论中的 `修复` 或 `实现`，并同时校验配置 writer 与仓库写权限。验证固定 Head、运行 `full` 配置，并使用唯一的 digest-pinned `QYKW_VERIFICATION_IMAGE_REF`；发布阶段最多创建专用分支和 Draft PR，最终审查与合并始终由 `xyh202131` 完成。

- `authorize`、`publish`、`record_failure` 和 `control` 只把仓库 Secret `QYKW_TOKEN` 映射为运行时 `QYKW_REVIEW_TOKEN`。
- `analyze` 只持有只读仓库令牌与 `QYKW_INFERENCE_*` 配置。
- 审查通道的两类凭据不会进入同一个 job。
- 变更阶段的 GitHub 写入令牌与推理密钥不会进入同一个 job；审查和发布的运行时变量均来自同一仓库限定 `QYKW_TOKEN`，隔离的是暴露面而非底层 GitHub 权限。
- 阶段产物只保留 1 天，不能提升权限，也不包含完整评论、原始推理响应或 Secrets。

仓库所有者需要配置：

- Secret：仓库限定的 `QYKW_TOKEN` 与 `MINIMAX_API_KEY`。工作流仅在对应 job 中把它们映射为阶段所需的 `QYKW_REVIEW_TOKEN`、`QYKW_PUBLISH_TOKEN` 或 `QYKW_INFERENCE_API_KEY`；`verify` 不注入任何仓库 Secret，只用授予 `packages: read` 的内置 `github.token` 登录私有 GHCR，并继续按 digest 拉取和运行验证镜像。
- Variables：必填的 `QYKW_INFERENCE_MODEL`，以及 `QYKW_INFERENCE_BASE_URL`、`QYKW_INFERENCE_ALLOWED_HOSTS`、`QYKW_INFERENCE_CONTEXT_WINDOW`、`QYKW_INFERENCE_MAX_OUTPUT_TOKENS`、`QYKW_INFERENCE_TIMEOUT_SECONDS`；启用授权变更时还需配置 `QYKW_VERIFICATION_IMAGE_REF`。

这些值不得写入源码、配置文件、Issue、PR、Actions 产物或日志。`.github/qykw.toml` 只保存非敏感策略，并且只从默认分支读取。

## 本地验证

运行时模块仅依赖 Python 3.11 标准库。开发覆盖率工具通过固定版本安装：

```powershell
py -3 -m pip install --disable-pip-version-check -r requirements-dev.txt
py -3 -m unittest discover -s tests -v
py -3 -m coverage run --branch --source=tools.qykw -m unittest discover -s tests -p "test_qykw*.py" -v
py -3 -m coverage json -o qykw-coverage.json
py -3 tools/check_qykw_coverage.py qykw-coverage.json --line 95 --branch 90
```

门禁分别要求语句覆盖率至少 95%、分支覆盖率至少 90%，不使用语句与分支混合后的总百分比。

本地 Linux Docker 镜像构建、受限容器运行和真实 Issue 领取流程已通过，仓库已配置 digest-pinned GHCR 引用。授权变更仍需完成真实评论、权限、并发和 Draft PR 端到端验收；审查推理链路也应以实际 PR 的 qykw 终态与行评结果为准，不能用本地测试或单独的 CI 通过代替线上验收。
