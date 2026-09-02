# qykw 代码审查机器人

qykw 是启元开物独立工程审查机器人。第一阶段只提供分析、计划、审查、状态和软停止能力；它不会执行 PR 代码、修改代码、批准或合并 PR，也不会直接推送任何分支。

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

| 指令 | 第一阶段行为 |
| --- | --- |
| `帮助` | 返回命令和权限说明 |
| `分析 <问题>` | 只读分析固定 Head 上下文 |
| `计划 <需求>` | 输出只读实施建议和验证方案 |
| `审查 [范围]` | 执行分诊、深审和反证验证 |
| `复审 [范围]` | 对最新 Head 启动显式复审 |
| `状态` | 返回当前运行阶段和状态 |
| `总结` | 汇总结论、覆盖和限制 |
| `修复`、`实现` | 返回 `capability_disabled`，不写代码 |
| `停止` | 为目标运行追加软取消标记 |

引用、围栏代码、行内代码、HTML 注释、邮箱、相似账号、全角符号和含零宽字符的伪装提及不会触发。未知或含糊命令保持只读，不能由推理结果提升权限。

## 输出与恢复

审查先更新一条总结，再发布经本地行号验证的 `COMMENT` 行评。总结包含运行编号、固定 Head、覆盖范围、验证说明和限制。零问题只表示未发现有充分证据的问题，不代表批准或绝对安全。

- Reaction 失败只记录警告，不阻断已授权任务。
- Head 漂移会将运行标记为 `stale`，不会发布旧行号评论。
- 后台或结构化结果失败会保留通用错误码，不公开请求、响应或凭据。
- 停止标记独立追加；并发中的旧状态保存不能覆盖它。
- 状态与历史评论使用分页读取，不能假定目标记录在前 100 条内。

## 工作流隔离

审查链路使用默认分支中的可信控制器，并拆分为 `authorize`、`analyze`、`publish` 和 `record_failure`。独立 `control` 工作流只处理精确的 `停止` 命令。所有 checkout 都禁用持久化凭据，不检出或执行 PR Head。

- `authorize`、`publish`、`record_failure` 和 `control` 只持有仓库限定的 `QYKW_REVIEW_TOKEN`。
- `analyze` 只持有只读仓库令牌与 `QYKW_INFERENCE_*` 配置。
- 两类凭据不会进入同一个 job。
- 工作流不引用代码发布令牌，阶段产物只保留 1 天且不包含代码全文、完整评论、原始响应或 Secrets。

仓库所有者需要配置：

- Secret：`QYKW_REVIEW_TOKEN`、`QYKW_INFERENCE_API_KEY`。
- Variables：`QYKW_INFERENCE_BASE_URL`、`QYKW_INFERENCE_ALLOWED_HOSTS`、`QYKW_INFERENCE_CONTEXT_WINDOW`、`QYKW_INFERENCE_MAX_OUTPUT_TOKENS`、`QYKW_INFERENCE_TIMEOUT_SECONDS`，以及运行时所需的后端选择值。

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
