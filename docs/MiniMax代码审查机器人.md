# qykw 代码审查机器人

机器人在 PR 创建、重新打开、转为 Ready 或推送新提交时运行，并由机器账号 `qykw` 发布中文审查。也可以在 PR 普通评论或行级评论中写 `@qykw 请重点检查权限边界` 主动触发；机器人会先在原评论添加 😄 reaction，再将该要求作为本次评审重点。

运行开始后，总结评论会先显示 `😄 正在审查，请稍候…`，完成后替换为评审总结。具体问题随后以 `COMMENT` 类型标注在对应变更行；多个问题会形成多条行级评论，不会批准、合并或直接修改代码。同一提交的相同问题不会重复发布。若审查失败，总结评论会替换为失败提示并引导查看 Actions 日志。
正式评论不展示底层模型名称。审查请求使用 Responses API，并固定设置 `reasoning.effort=high` 以强制启用 M3 的自适应思考。

## 配置

仓库必须设置以下 Actions Secrets。不要将密钥写入源码、工作流变量、Issue、PR 或日志。

```powershell
gh secret set MINIMAX_API_KEY --repo xyh202131/Challenge-Cup
gh secret set QYKW_TOKEN --repo xyh202131/Challenge-Cup
```

非敏感配置使用仓库变量：

- `MINIMAX_BASE_URL`：默认 `https://api.minimaxi.com/v1`
- `MINIMAX_MODEL`：默认 `MiniMax-M3`
- `MINIMAX_REVIEW_BOT_LOGIN`：`qykw`

可在 Actions 页面手动运行 `qykw review`，输入 PR 编号进行复查。

## 安全与成本边界

- 工作流使用 `pull_request_target`，但始终检出默认分支中的可信脚本；禁止改为检出 PR Head。
- `issue_comment` 和 `pull_request_review_comment` 仅在评论包含 `@qykw` 且作者不是机器人自身时运行；评论正文与 PR 差异都按不可信输入处理。
- `qykw` 是独立机器账号，当前工作流通过仓库 Secret `QYKW_TOKEN` 认证。令牌具备仓库管理权限，但脚本只读取差异并创建或更新审查评论；未经人工明确指令，不创建提交、不推送代码。
- MiniMax Key 仅作为 Bearer Token 发送给 MiniMax，不打印到日志。
- PR 差异会发送至 MiniMax API，私密或受限代码提交前应确认第三方处理边界。
- 单次最多发送 60,000 字符差异和 4,000 字符 @ 要求，输出上限为 4,096 Token；超出部分会明确截断。单次最多发布 20 个经真实 diff 行校验的问题。
- 模型结果属于辅助审查，不能替代测试、人工审核或安全检查。
