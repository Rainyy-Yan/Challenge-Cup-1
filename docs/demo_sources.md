# Demo 知识来源登记（2026-09-01）

本登记覆盖当前在线 Demo 可暴露的全部知识切片；其中一部分尚未被三份预置画像的资源断言引用，但仍可能被后端检索器使用。页面中的“断言与溯源链”会显示同一条来源、定位和可点击的“打开原始来源”。

项目负责人 xyh202131 已于 2026-09-03 完成 20 条来源的逐条复核。每条来源的
必要原文短摘、忠实转述和适用边界已切分到
[`data/sources/slices/`](../data/sources/slices/README.md)，并由
[`data/sources/index.json`](../data/sources/index.json) 登记哈希。“已人工核实”
仍不等于可跨品牌、跨机型直接套用；任何现场动作均以设备制造商手册、现行法规
和现场风险评估为准。

| 切片 | 展示主张的适用边界 | 原始来源与定位 |
| --- | --- | --- |
| KB-001、KB-002 | 典型六轴系统、控制柜概念 | [Yaskawa Motoman 运动入门](https://knowledge.motoman.com/hc/en-us/articles/33604823339666-Beginner-s-Guide-to-Yaskawa-Motoman-Robot-Motion)，`What Is an Industrial Robot, Really?` 与 `The Robot’s Joints` |
| KB-003、KB-004 | ABB S4Cplus Automotive M2000 控制器的三位使能、手动/自动和限速示例 | [ABB Product specification — Controller S4Cplus Automotive M2000](https://library.e.abb.com/public/90cbf8987abdc5bcc12576cb00528dcf/3HAC17710-1_rev3_en_library.pdf)，§1.2.1，p.10 |
| KB-005 | ABB RAPID 的工具坐标概念 | [ABB Technical reference manual RAPID](https://library.e.abb.com/public/688894b98123f87bc1257cc50044e809/Technical%20reference%20manual_RAPID_3HAC16581-1_revJ_en.pdf)，§1.182，p.507 |
| KB-006 | ABB RAPID `SToolTCPCalib` 的固定工具 TCP 四点法与不同姿态要求；不作为通用 TCP 标定规程 | [ABB Technical reference manual RAPID](https://library.e.abb.com/public/688894b98123f87bc1257cc50044e809/Technical%20reference%20manual_RAPID_3HAC16581-1_revJ_en.pdf)，§1.182，p.507 |
| KB-007 | ABB Robotic Parcel Inductor 校准笔 TCP 的四点法与标定后验证；验收阈值为 Demo 的项目安全提示，并非该手册的通用数值标准 | [ABB Installation Manual – Robotic Parcel Inductor](https://library.e.abb.com/public/0de290855a3c4f3e9276798bf0c69978/4GAA412009915_en_C_Installation%20Manual%20-%20Robotic%20Parcel%20Inductor.pdf?x-sign=Gt6dmMZkVQ9aH52DQKNz3KkgRXQ461bYSCev58qtS7X1qw0i20VUP5HdBEgXXTpM)，§4.2.5.1，p.75 |
| KB-008 | Yaskawa 用户坐标系三点共线报警 | [Yaskawa Relative Job Function Instructions](https://www.motoman.com/getmedia/976B5A16-0A40-465B-A54B-831DD278FEB2/181288-1cd)，§5.1，p.55/57 |
| KB-009 | Yaskawa INFORM 的 MOVJ / MOVL / MOVC 语义 | [Yaskawa Motoman 编程工具说明](https://knowledge.motoman.com/hc/en-us/articles/33634309555730-Programming-Languages-Developer-Tools)，`INFORM > Common Instructions` |
| KB-011 | ABB RAPID 的停点、飞越点和 `zonedata` 语义 | [ABB RAPID Instructions](https://library.e.abb.com/public/b227fcd260204c4dbeb8a58f8002fe64/Rapid_instructions.pdf)，§3.103，pp.1733–1738 |
| KB-012、KB-013 | Yaskawa INFORM 的子程序调用和 I/O 指令语义 | [Yaskawa Motoman 编程工具说明](https://knowledge.motoman.com/hc/en-us/articles/33634309555730-Programming-Languages-Developer-Tools)，`INFORM > Language Characteristics / Common Instructions` |
| KB-017 | FANUC 报警按代码、机型与版本查询的边界 | [FANUC America MyPortal](https://www.fanucamerica.com/support/myportal/robot-myportal-registration)，`Why Register for MyPortal? > Alarm Code Lookup`。公开入口未提供可引用的 SRVO-005 通用恢复步骤，因此 Demo 已移除此类断言。 |
| KB-019 | FANUC R-30iB Plus 的 Quick Master 条件与验证 | [FANUC America Tech Transfer](https://techtransfer.fanucamerica.com/tech-transfer/quick-mastering-on-r-30ib-plus-using-power-off-position)，`What you’ll learn / Requirements and considerations`（完整页需登录） |
| KB-020 | Yaskawa GP12 注脂步骤和排脂塞警告 | [Yaskawa GP12 Grease Replenishment](https://knowledge.motoman.com/hc/en-us/articles/23946539687319-Grease-Replenishment-PM-GP12-YR-1-06VXH12-F00)，各轴步骤 3–6 |
| KB-021、KB-022、KB-023 | 点检原则、联锁隔离和带电示教人员要求 | [OSHA Guidelines For Robotics Safety](https://www.osha.gov/enforcement/directives/std-01-12-002)，Appendix A：`Installation, Maintenance and Programming`、`Guarding Methods`、`Training`。该页为历史档案，不能取代现行法规。 |
| KB-024 | ABB RAPID 的基于模型的运动监控、碰撞检测和负载数据边界 | [ABB RAPID Overview](https://library.e.abb.com/public/8dbf836be16446dc89a3af8a012099b5/3HAC050947%20TRM%20RAPID%20Overview%20RW%206-en.pdf)，§2.6，p.145 |
| KB-026 | Yaskawa INFORM 的 MOVJ / MOVL / MOVC 指令选择边界 | [Yaskawa Motoman 编程工具说明](https://knowledge.motoman.com/hc/en-us/articles/33634309555730-Programming-Languages-Developer-Tools)，`INFORM > Common Instructions` |

## 已从正式 Demo 排除的切片

| 切片 | 原因 | 当前处理 |
| --- | --- | --- |
| KB-015、KB-016 | FANUC `B-80687EN/15` 公开 PDF 链接已失效。 | 原始条目保留供内部复核，`demo_eligible: false`；不会进入在线服务或展示页。 |
| KB-018 | FANUC `B-84194EN/01` 公开 PDF 链接已失效。 | 原始条目保留供内部复核，`demo_eligible: false`；不会进入在线服务或展示页。 |

## 复核记录

- 已把原先无法定位的教材占位出处从当前 Demo 引用链中移除。
- 已删除无法由上述一手资料直接支撑的绝对数值、跨品牌命令语义和具体现场处置步骤。
- 正式 Demo 的 20 条记录已由项目负责人逐条核对并标记为 `human_verified`；
  KB-015、KB-016、KB-018 仍因来源失效保持在正式 Demo 之外。
- 每条正式记录都同时绑定仓库内来源片段与 SHA-256。缺文件或哈希不一致时，
  运行时会拒绝把该条来源发布为已核实。

## 可审计台账

当前在线 Demo 的范围不是全部 26 条知识库切片，而是后端公开给正式会话的
**20 条**。预置的 P-A、P-B、P-C 三个会话当前实际在资源断言中
引用其中 12 条：KB-001–KB-005、KB-009、KB-017、KB-019–KB-023；KB-006–KB-008、
KB-011–KB-013、KB-024、KB-026 虽未出现在预置资源中，仍会在在线生成时被检索，
因此也必须登记并接受同一套人工复核。其余切片不在正式 Demo 链路，不能作为本 Issue
的完成证据。

逐条台账保存在 [data/demo_source_manifest.json](../data/demo_source_manifest.json)。每行均包含：

- 实际出现该切片的预置画像（尚未在预置资源中出现时为空列表）；
- 原始资料名称、版本和页码/章节/条款定位；
- 可点击的原始资料地址；
- `review_status`、`reviewer`、`reviewed_on`、`conclusion`、`authorization` 五个
  人工复核字段。

当前 20 条均为 `human_verified`，记录了复核人、日期、正文/数值/适用边界结论、
引用边界、本地片段路径和哈希。新增或修改来源时仍必须重新执行人工核对；自动
连通性检查、抓取或模型判断不能替代项目负责人的最终确认。
