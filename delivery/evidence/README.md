# 交付证据登记规则

本目录是 G0–G4 交付证据的统一入口。机器读取
[`index.json`](index.json)，人工查阅由它生成的 [`INDEX.md`](INDEX.md)。两份索引
不得分别手工维护；新增或更新证据时只修改 `index.json`，再运行生成命令。

```powershell
py -3 -X utf8 -m tools.evidence_index --write
py -3 -X utf8 -m tools.evidence_index --check
```

## 证据分类

| `category` | 含义 | 示例 |
|---|---|---|
| `specification` | 冻结要求、口径或验收规则 | 交付基线 |
| `raw-data` | 未汇总的样本级输入或输出 | 评测明细、红队明细 |
| `statistical-result` | 可由原始数据复算的汇总 | 指标摘要、统计报告 |
| `human-review` | 人工评分、签字或复核记录 | 双人盲评、来源核对 |
| `runtime-artifact` | 可执行或可操作的运行产物 | 在线 Demo 入口 |
| `presentation` | 面向评审的说明材料 | PDF、PPT、视频 |
| `package-control` | 封包、哈希与送达控制证据 | 文件清单、SHA-256 |

运行日志不是人工真值，统计报告也不是原始数据。一个文件只登记为它实际承担的
证据类型；需要互相追溯时使用 `source` 字段指向上游证据 ID 或生成命令。

## 状态

- `planned`：计划路径，文件可以尚不存在，不能作为完成证据。
- `working`：已有内容但仍会修改，不能称为最终版。
- `candidate`：绑定候选提交 SHA，等待人工终审。
- `approved`：已由负责人批准并绑定不可变提交 SHA。
- `superseded`：已被新证据替代；记录保留，不删除追溯关系。

状态只写在索引和元数据中，不写进文件名。禁止使用 `final-v2`、`最新版`、`副本`
等含义不稳定的名称。

## 路径和命名

正式新增的运行证据使用以下结构：

```text
delivery/evidence/<G0-G4>/<evidence-id>/<YYYYMMDDTHHMMSSZ>_<commit-sha12>/
├── metadata.json
├── raw/       # 原始输入与逐样本输出
├── result/    # 可复算汇总
└── media/     # 截图或录屏
```

- 证据 ID 格式为 `EV-G<门禁>-<类别>-<三位序号>`，例如
  `EV-G2-REDTEAM-001`。
- 时间使用 UTC 基本格式，提交号使用完整 Git SHA 的前 12 位。
- 同一次运行的原始数据、汇总和截图共用同一个运行目录，不覆盖旧目录。
- 路径必须相对仓库根目录，不得包含盘符、`..`、反斜杠、临时目录或个人目录。
- 候选证据一旦被引用，只能通过新运行目录替代；旧记录改为 `superseded`。

## 最小元数据

新生成的正式证据目录必须包含 `metadata.json`，至少记录：

```json
{
  "schema_version": 1,
  "evidence_id": "EV-G2-TESTS-001",
  "index_entry": "delivery/evidence/index.json#EV-G2-TESTS-001",
  "title": "完整单元测试",
  "gate": "G2",
  "category": "statistical-result",
  "status": "candidate",
  "owner": "github-login",
  "related_issues": [31],
  "created_at": "2026-09-02T12:30:00Z",
  "repo_commit": "40-character-git-sha",
  "command": "py -3 -X utf8 -m unittest discover -s tests -v",
  "inputs": [],
  "outputs": ["result/test-summary.json"],
  "visibility": "public",
  "limitations": "CI 通过不替代人工真值。"
}
```

截图另记浏览器和版本、视口、场景及脱敏情况；录屏另记编码、分辨率、时长和
完整观看结论；日志另记仓库相对工作目录、开始/结束时间、退出码、运行环境和
脱敏结果。任何字段未知时明确写 `unassigned` 或限制说明，不得猜测。

## 更新流程

1. 在 `index.json` 中分配唯一 ID，填写责任人、关联 Issue、状态和使用限制。
2. 生成证据时保留原始数据；汇总文件用 `source` 或元数据反向指向原始证据。
3. 运行 `python -m tools.evidence_index --write` 更新人工索引。
4. 运行 `python -m tools.evidence_index --check`，确认路径、状态和生成文件一致。
5. 到候选阶段再填 `repo_commit`，由负责人终审后把状态改为 `approved`。
6. 最终封包从索引选取获准证据，不把 `planned`、`working` 或 `superseded`
   条目冒充最终成果。
