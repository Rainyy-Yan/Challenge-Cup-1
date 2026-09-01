# qykw 代码审查机器人

机器人在 PR 创建、重新打开、转为 Ready 或推送新提交时运行，并由 `qykw[bot]` 发布或更新一条中文审查评论。它只读取 PR 差异，不检出或执行待审分支代码。

运行开始后，同一条评论会先显示 `😄 正在审查，请稍候…`，完成后替换为正式结果；若审查失败，则替换为失败提示并引导查看 Actions 日志。

## 配置

仓库必须设置以下 Actions Secrets。不要将密钥写入源码、工作流变量、Issue、PR 或日志。

```powershell
gh secret set MINIMAX_API_KEY --repo xyh202131/Challenge-Cup
gh secret set MINIMAX_REVIEW_APP_PRIVATE_KEY --repo xyh202131/Challenge-Cup
```

非敏感配置使用仓库变量：

- `MINIMAX_BASE_URL`：默认 `https://api.minimaxi.com/v1`
- `MINIMAX_MODEL`：默认 `MiniMax-M3`
- `MINIMAX_REVIEW_APP_CLIENT_ID`：GitHub App Client ID
- `MINIMAX_REVIEW_BOT_LOGIN`：`qykw[bot]`

可在 Actions 页面手动运行 `qykw review`，输入 PR 编号进行复查。

## 安全与成本边界

- 工作流使用 `pull_request_target`，但始终检出默认分支中的可信脚本；禁止改为检出 PR Head。
- GitHub App 仅安装到 `Challenge-Cup`。App 具备 Contents 和 Pull Requests 写权限，但当前脚本只读取差异并创建或更新审查评论；未经人工明确指令，不创建提交、不推送代码。
- MiniMax Key 仅作为 Bearer Token 发送给 MiniMax，不打印到日志。
- PR 差异会发送至 MiniMax API，私密或受限代码提交前应确认第三方处理边界。
- 单次最多发送 60,000 字符差异，输出上限为 4,096 Token；超出部分会明确截断。
- 模型结果属于辅助审查，不能替代测试、人工审核或安全检查。
