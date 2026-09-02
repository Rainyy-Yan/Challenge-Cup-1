# Demo 知识来源登记（2026-09-01）

本登记只覆盖当前 `web/snapshot.json` 三个画像实际会显示的知识切片。页面中的“断言与溯源链”会显示同一条来源、定位和可点击的“打开原始来源”。

未标记为“已人工核实”的内容仍须由项目负责人逐条复核；“真实来源”不等同于可跨品牌、跨机型直接套用。任何现场动作均以设备制造商手册、现行法规和现场风险评估为准。

| 切片 | 展示主张的适用边界 | 原始来源与定位 |
| --- | --- | --- |
| KB-001、KB-002 | 典型六轴系统、控制柜概念 | [Yaskawa Motoman 运动入门](https://knowledge.motoman.com/hc/en-us/articles/33604823339666-Beginner-s-Guide-to-Yaskawa-Motoman-Robot-Motion)，`What Is an Industrial Robot, Really?` 与 `The Robot’s Joints` |
| KB-003、KB-004 | ABB S4Cplus Automotive M2000 控制器的三位使能、手动/自动和限速示例 | [ABB Product specification — Controller S4Cplus Automotive M2000](https://library.e.abb.com/public/90cbf8987abdc5bcc12576cb00528dcf/3HAC17710-1_rev3_en_library.pdf)，§1.2.1，p.10 |
| KB-005 | ABB RAPID 的工具坐标概念 | [ABB Technical reference manual RAPID](https://library.e.abb.com/public/688894b98123f87bc1257cc50044e809/Technical%20reference%20manual_RAPID_3HAC16581-1_revJ_en.pdf)，§1.182，p.507 |
| KB-006 | ABB RAPID `SToolTCPCalib` 的固定工具 TCP 四点法与不同姿态要求；不作为通用 TCP 标定规程 | [ABB Technical reference manual RAPID](https://library.e.abb.com/public/688894b98123f87bc1257cc50044e809/Technical%20reference%20manual_RAPID_3HAC16581-1_revJ_en.pdf)，§1.182，p.507 |
| KB-007 | ABB Robotic Parcel Inductor 校准笔 TCP 的四点法与标定后验证；验收阈值为 Demo 的项目安全提示，并非该手册的通用数值标准 | [ABB Installation Manual – Robotic Parcel Inductor](https://library.e.abb.com/public/0de290855a3c4f3e9276798bf0c69978/4GAA412009915_en_C_Installation%20Manual%20-%20Robotic%20Parcel%20Inductor.pdf?x-sign=Gt6dmMZkVQ9aH52DQKNz3KkgRXQ461bYSCev58qtS7X1qw0i20VUP5HdBEgXXTpM)，§4.2.5.1，p.75 |
| KB-008 | Yaskawa 用户坐标系三点共线报警 | [Yaskawa Relative Job Function Instructions](https://www.motoman.com/getmedia/976B5A16-0A40-465B-A54B-831DD278FEB2/181288-1cd)，§5.1，p.55/57 |
| KB-009 | Yaskawa INFORM 的 MOVJ / MOVL / MOVC 语义 | [Yaskawa Motoman 编程工具说明](https://knowledge.motoman.com/hc/en-us/articles/33634309555730-Programming-Languages-Developer-Tools)，`INFORM > Common Instructions` |
| KB-017 | FANUC 报警按代码、机型与版本查询的边界 | [FANUC America MyPortal](https://www.fanucamerica.com/support/myportal/robot-myportal-registration)，`Why Register for MyPortal? > Alarm Code Lookup`。公开入口未提供可引用的 SRVO-005 通用恢复步骤，因此 Demo 已移除此类断言。 |
| KB-019 | FANUC R-30iB Plus 的 Quick Master 条件与验证 | [FANUC America Tech Transfer](https://techtransfer.fanucamerica.com/tech-transfer/quick-mastering-on-r-30ib-plus-using-power-off-position)，`What you’ll learn / Requirements and considerations`（完整页需登录） |
| KB-020 | Yaskawa GP12 注脂步骤和排脂塞警告 | [Yaskawa GP12 Grease Replenishment](https://knowledge.motoman.com/hc/en-us/articles/23946539687319-Grease-Replenishment-PM-GP12-YR-1-06VXH12-F00)，各轴步骤 3–6 |
| KB-021、KB-022、KB-023 | 点检原则、联锁隔离和带电示教人员要求 | [OSHA Guidelines For Robotics Safety](https://www.osha.gov/enforcement/directives/std-01-12-002)，Appendix A：`Installation, Maintenance and Programming`、`Guarding Methods`、`Training`。该页为历史档案，不能取代现行法规。 |

## 已从正式 Demo 排除的切片

| 切片 | 原因 | 当前处理 |
| --- | --- | --- |
| KB-015、KB-016 | FANUC `B-80687EN/15` 公开 PDF 链接已失效。 | 原始条目保留供内部复核，`demo_eligible: false`；不会进入在线服务、离线快照或展示页。 |
| KB-018 | FANUC `B-84194EN/01` 公开 PDF 链接已失效。 | 原始条目保留供内部复核，`demo_eligible: false`；不会进入在线服务、离线快照或展示页。 |

## 复核记录

- 已把原先无法定位的教材占位出处从当前 Demo 引用链中移除。
- 已删除无法由上述一手资料直接支撑的绝对数值、跨品牌命令语义和具体现场处置步骤。
- 原始知识库保留了既有的 `verified: true` 历史记录（KB-004、KB-015、KB-016、KB-017、KB-018）；本次没有把任何新条目标为“已人工核实”。其中 KB-015、KB-016、KB-018 已因来源失效从正式 Demo 排除。

## 可审计台账

当前静态 Demo 的范围不是全部 26 条知识库切片，而是 `web/snapshot.json` 的
P-A、P-B、P-C 三个会话中实际被资源断言引用的 **15 条**：KB-001–KB-009、
KB-017、KB-019–KB-023。KB-010–KB-016、KB-018、KB-024–KB-026 不在本次正式
演示链路；它们不能作为本 Issue 的完成证据。

逐条台账保存在 [data/demo_source_manifest.json](../data/demo_source_manifest.json)。每行均包含：

- 实际出现的画像；
- 原始资料名称、版本和页码/章节/条款定位；
- 可点击的原始资料地址；
- `review_status`、`reviewer`、`reviewed_on`、`conclusion`、`authorization` 五个
  人工复核字段。

`pending_manual_review` 表示已有可回溯的候选一手来源、但尚未由人逐字核对；
`legacy_verified_record_pending` 表示知识库已有历史 `verified: true`，但复核人、
日期、结论或引用/授权边界尚未补录。它**不是**新的机器核实结论，更不能用来把
任何条目提升为 `verified`。

人工复核时，项目成员应在同一条记录中填写复核人、日期、对正文/数值/适用边界
的结论，以及“可仅作定位引用 / 已取得使用授权 / 不可用于提交”等授权边界；若
原资料无法打开或不支持断言，应将结论写为不通过，并从正式 Demo 链路移除。厂商
站点可能限制自动访问，因此自动连通性检查不构成人工核实，也不能替代实际打开原文
进行抽查。
