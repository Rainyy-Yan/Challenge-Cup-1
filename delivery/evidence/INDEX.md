# G0–G4 交付证据索引

> 本文件由 `python -m tools.evidence_index --write` 从 `index.json` 生成，
> 请勿手工编辑。状态定义和命名规则见 [README.md](README.md)。

- 项目：`XH-202630`
- 索引日期：`2026-09-03`
- `planned` 仅表示计划路径，不能作为完成证据。

| ID | 门禁 | 类型 | 证据 | 状态 | 仓库路径 | 来源/生成方式 | 关联 Issue | 责任人 | 使用限制 |
|---|---|---|---|---|---|---|---|---|---|
| EV-G0-BASELINE-001 | G0 | 规范 | 比赛整体交付结果要求 | 工作稿 | [docs/比赛整体交付结果要求.md](../../docs/%E6%AF%94%E8%B5%9B%E6%95%B4%E4%BD%93%E4%BA%A4%E4%BB%98%E7%BB%93%E6%9E%9C%E8%A6%81%E6%B1%82.md) | repository | [#1](https://github.com/qiyuankaiwu/Challenge-Cup/issues/1)、[#4](https://github.com/qiyuankaiwu/Challenge-Cup/issues/4)、[#5](https://github.com/qiyuankaiwu/Challenge-Cup/issues/5) | unassigned | 仓库内冻结验收基线；正式比赛方案仍须使用稳定且获授权的评审路径。 |
| EV-G0-TASKBOOK-MAP-001 | G0 | 规范 | 官方任务书落地矩阵 | 工作稿 | [docs/官方任务书落地矩阵.md](../../docs/%E5%AE%98%E6%96%B9%E4%BB%BB%E5%8A%A1%E4%B9%A6%E8%90%BD%E5%9C%B0%E7%9F%A9%E9%98%B5.md) | official taskbook pages 4-10 and repository evidence index | [#65](https://github.com/qiyuankaiwu/Challenge-Cup/issues/65) | xyh202131 | 实现证据为当前工作稿；外部人工签核和提交回执尚未形成，不能据此宣称官方验收完成。 |
| EV-G1-ONLINE-DEMO-001 | G1 | 运行产物 | 在线 Demo 主入口 | 工作稿 | [web/index.html](../../web/index.html) | py -3 server.py -&gt; GET / | [#20](https://github.com/qiyuankaiwu/Challenge-Cup/issues/20) | unassigned | 需要本地后端和模型配置；入口存在不等于已完成真模型链录屏验收。 |
| EV-G2-EVAL-RAW-001 | G2 | 原始数据 | 批量评测样本级明细 | 工作稿 | [evalkit/report/cases.json](../../evalkit/report/cases.json) | python -m evalkit.run\_eval --n 50 | [#25](https://github.com/qiyuankaiwu/Challenge-Cup/issues/25)、[#30](https://github.com/qiyuankaiwu/Challenge-Cup/issues/30)、[#37](https://github.com/qiyuankaiwu/Challenge-Cup/issues/37) | unassigned | 规则评测数据，不是独立人工真值。 |
| EV-G2-EVAL-SUMMARY-001 | G2 | 统计结果 | 批量评测统计摘要 | 工作稿 | [evalkit/report/summary.json](../../evalkit/report/summary.json) | EV-G2-EVAL-RAW-001 | [#30](https://github.com/qiyuankaiwu/Challenge-Cup/issues/30)、[#37](https://github.com/qiyuankaiwu/Challenge-Cup/issues/37) | unassigned | 适配值表示规则一致性，不得表述为独立效果准确率。 |
| EV-G2-FORMAL-SCORECARD-APPROVED-001 | G2 | 统计结果 | 经人工签字批准的正式指标计分卡 | 计划 | `delivery/evidence/G2/EV-G2-FORMAL-SCORECARD-APPROVED-001/UTC\_TIMESTAMP\_CANDIDATE\_SHA/result/scorecard.json` | externally signed frozen formal truth, independent reviews, and adjudication | [#67](https://github.com/qiyuankaiwu/Challenge-Cup/issues/67) | unassigned | 外部独立评分、仲裁、签字和冻结真值尚未实际形成；每次冻结必须创建新的不可变运行目录，旧记录改为 superseded，禁止原地提升此计划路径；本条不能作为正式指标或官方验收已完成的证明。 |
| EV-G2-FORMAL-SCORECARD-IMPLEMENTATION-001 | G2 | 运行产物 | 正式指标离线评分器与 manifest/共同 pair 门禁实现 | 工作稿 | [evalkit/formal\_scorecard.py](../../evalkit/formal_scorecard.py) | py -3 -X utf8 -m evalkit.formal\_scorecard --truth &lt;formal\_truth.json&gt; --out &lt;report-directory&gt; | [#67](https://github.com/qiyuankaiwu/Challenge-Cup/issues/67) | xyh202131 | 评分器只验证冻结输入、hashed manifest/ownership/reference 闭集和共同有效双评 pair，并计算证据门禁；它不生成或认证人工真值、外部来源/摘录、签字、身份、仲裁或官方达标结论。 |
| EV-G2-FORMAL-SCORECARD-TESTS-001 | G2 | 运行产物 | 正式评分器公式与命令行测试 | 工作稿 | [tests/test\_formal\_scorecard.py](../../tests/test_formal_scorecard.py) | py -3 -X utf8 -m unittest tests.test\_formal\_scorecard -v | [#67](https://github.com/qiyuankaiwu/Challenge-Cup/issues/67) | xyh202131 | 自动测试覆盖实现与错误路径，不替代独立人工审阅、仲裁或正式赛事评分。 |
| EV-G2-FORMAL-TRUTH-CONTRACT-001 | G2 | 规范 | 正式独立真值与哈希 manifest 契约模板 | 工作稿 | [data/evaluation/formal\_truth.template.json](../../data/evaluation/formal_truth.template.json) | data/evaluation/README.md | [#67](https://github.com/qiyuankaiwu/Challenge-Cup/issues/67) | xyh202131 | 模板刻意保持 draft 且无人工身份、标签或结论；它定义 version-1 hashed manifest、反向 ownership、五类引用闭集与共同有效双评 pair 边界，但运行后应为 not\_assessable，不能作为正式指标证据。 |
| EV-G2-REDTEAM-RAW-001 | G2 | 原始数据 | H1–H6 红队样本级结果 | 工作稿 | [evalkit/report\_redteam/redteam.json](../../evalkit/report_redteam/redteam.json) | python -m evalkit.redteam | [#34](https://github.com/qiyuankaiwu/Challenge-Cup/issues/34)、[#37](https://github.com/qiyuankaiwu/Challenge-Cup/issues/37) | unassigned | 仅覆盖仓库固定样本，不代表开放世界语义检出率。 |
| EV-G2-REDTEAM-REPORT-001 | G2 | 统计结果 | H1–H6 红队复测说明 | 工作稿 | [docs/redteam\_retest\_2026-09-02.md](../../docs/redteam_retest_2026-09-02.md) | EV-G2-REDTEAM-RAW-001 | [#34](https://github.com/qiyuankaiwu/Challenge-Cup/issues/34)、[#37](https://github.com/qiyuankaiwu/Challenge-Cup/issues/37) | unassigned | 结果依赖当前正式 Demo 来源集合；来源变化后必须重绑夹具。 |
| EV-G2-SOURCE-REVIEW-001 | G2 | 人工复核 | 最终 Demo 知识来源人工核对表 | 工作稿 | [docs/demo\_source\_review\_checklist.md](../../docs/demo_source_review_checklist.md) | data/demo\_source\_manifest.json and data/sources/index.json | [#22](https://github.com/qiyuankaiwu/Challenge-Cup/issues/22)、[#23](https://github.com/qiyuankaiwu/Challenge-Cup/issues/23)、[#24](https://github.com/qiyuankaiwu/Challenge-Cup/issues/24) | xyh202131 | xyh202131 已于 2026-09-03 复核 20 条正式来源；仓库仅保留项目实际引用的短摘、忠实转述、定位和哈希，不重新分发完整第三方手册。 |
| EV-G3-TECH-PDF-001 | G3 | 展示材料 | 技术解读与交接说明 PDF | 工作稿 | [docs/XH-202630技术解读与交接说明.pdf](../../docs/XH-202630%E6%8A%80%E6%9C%AF%E8%A7%A3%E8%AF%BB%E4%B8%8E%E4%BA%A4%E6%8E%A5%E8%AF%B4%E6%98%8E.pdf) | docs/book/main.tex | [#38](https://github.com/qiyuankaiwu/Challenge-Cup/issues/38)、[#39](https://github.com/qiyuankaiwu/Challenge-Cup/issues/39)、[#45](https://github.com/qiyuankaiwu/Challenge-Cup/issues/45) | unassigned | 尚未被 #38 确认为唯一权威技术 PDF。 |
| EV-G4-PACKAGE-CHECKSUM-001 | G4 | 封包控制 | 候选提交包哈希清单 | 计划 | `delivery/package/作品提交包/CHECKSUMS.sha256` | candidate package contents | [#46](https://github.com/qiyuankaiwu/Challenge-Cup/issues/46)、[#48](https://github.com/qiyuankaiwu/Challenge-Cup/issues/48)、[#49](https://github.com/qiyuankaiwu/Challenge-Cup/issues/49) | unassigned | 候选包尚未生成；不得把本条计划记录当作交付完成证据。 |

## 查证顺序

1. 从本表按门禁或 Issue 找到证据 ID。
2. 打开仓库路径；统计结果继续按 `source` 字段回到原始证据或生成命令。
3. 只有状态为 `approved` 且绑定提交 SHA 的记录可进入最终交付清单。
