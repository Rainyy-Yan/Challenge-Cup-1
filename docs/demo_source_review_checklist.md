# P22：最终 Demo 知识来源人工核对表

本表覆盖在线 Demo 中 P-A、P-B、P-C 三个示例可展示的知识切片；
它们与 `data/demo_source_manifest.json` 的记录一一对应。项目负责人已于
2026-09-03 实际打开原始资料并逐条核对，结论已写回同一条
台账记录；对应原文片段保存在 `data/sources/slices/` 并由 SHA-256 固定。

每一条只需判断四件事：资料能否打开、定位是否正确、正文是否被支持、是否可作
比赛中的定位引用。若资料不支持或无法访问，选择“不通过”，不要为了凑数量保留。

| 切片 | 需要核对的 Demo 说法 | 打开原文 | 准确定位 | 人工结论 |
| --- | --- | --- | --- | --- |
| KB-001 | 工业机器人系统的本体、控制柜、示教器组成及典型六轴关节作用。 | [打开 Yaskawa 原文](https://knowledge.motoman.com/hc/en-us/articles/33604823339666-Beginner-s-Guide-to-Yaskawa-Motoman-Robot-Motion) | 网页标题 `The Robot's Joints` | 通过 |
| KB-002 | 控制柜的基本组成、执行控制命令的作用和现场安全边界。 | [打开 Yaskawa 原文](https://knowledge.motoman.com/hc/en-us/articles/33604823339666-Beginner-s-Guide-to-Yaskawa-Motoman-Robot-Motion) | 网页标题 `What Is an Industrial Robot, Really?` 下的 `Controller` | 通过 |
| KB-003 | 三位使能开关的三种状态，以及松开或按死会停止运动。 | [打开 PDF 第 10 页](https://library.e.abb.com/public/90cbf8987abdc5bcc12576cb00528dcf/3HAC17710-1_rev3_en_library.pdf#page=10) | `3HAC 17710-1 Rev.3`，§1.2.1 `Three position enabling device` | 通过 |
| KB-004 | ABB S4Cplus Automotive M2000 控制器的手动/自动模式和手动限速 250 mm/s。 | [打开 PDF 第 10 页](https://library.e.abb.com/public/90cbf8987abdc5bcc12576cb00528dcf/3HAC17710-1_rev3_en_library.pdf#page=10) | `3HAC 17710-1 Rev.3`，§1.2.1 `Selecting the operating mode / Reduced speed` | 通过 |
| KB-005 | TCP、工具坐标系及控制器差异的适用边界。 | [打开 PDF 第 507 页](https://library.e.abb.com/public/688894b98123f87bc1257cc50044e809/Technical%20reference%20manual_RAPID_3HAC16581-1_revJ_en.pdf#page=507) | `3HAC16581-1 Rev.J`，§1.182 `SToolTCPCalib` | 通过 |
| KB-006 | TCP 四点法的同一点、不同姿态要求。 | [打开 PDF 第 507 页](https://library.e.abb.com/public/688894b98123f87bc1257cc50044e809/Technical%20reference%20manual_RAPID_3HAC16581-1_revJ_en.pdf#page=507) | `3HAC16581-1 Rev.J`，§1.182 `SToolTCPCalib` | 通过 |
| KB-007 | TCP 标定后应验证精度；验收阈值按机型、工艺和现场质量要求确定。 | [打开 PDF 第 75 页](https://library.e.abb.com/public/0de290855a3c4f3e9276798bf0c69978/4GAA412009915_en_C_Installation%20Manual%20-%20Robotic%20Parcel%20Inductor.pdf?x-sign=Gt6dmMZkVQ9aH52DQKNz3KkgRXQ461bYSCev58qtS7X1qw0i20VUP5HdBEgXXTpM#page=75) | `4GAA412009915-001 Rev.C`，§4.2.5.1 `TCP of the calibration pen` | 通过 |
| KB-008 | 三点建立用户坐标系时三点不能共线及对应报警。 | [打开 PDF 第 55 页](https://www.motoman.com/getmedia/976B5A16-0A40-465B-A54B-831DD278FEB2/181288-1cd#page=55) | `HW1484476`，§5.1 `Alarm 4512`；同时核看 p.57 | 通过 |
| KB-009 | Yaskawa INFORM 中 MOVJ、MOVL、MOVC 的语义和安全使用边界。 | [打开 Yaskawa 原文](https://knowledge.motoman.com/hc/en-us/articles/33634309555730-Programming-Languages-Developer-Tools) | `INFORM > Common Instructions` 中的 `MOVJ / MOVL / MOVC` | 通过 |
| KB-011 | ABB RAPID 的停点、飞越点、`fine` 与 `z1` 语义。 | [打开 PDF 第 1733 页](https://library.e.abb.com/public/b227fcd260204c4dbeb8a58f8002fe64/Rapid_instructions.pdf#page=1733) | `3HAC050917-001 Rev.F`，§3.103 `zonedata`，pp.1733–1738 | 通过 |
| KB-012 | INFORM 行式语言及 `CALL JOB:name` 子程序调用。 | [打开 Yaskawa 原文](https://knowledge.motoman.com/hc/en-us/articles/33634309555730-Programming-Languages-Developer-Tools) | `INFORM > Language Characteristics / Common Instructions` | 通过 |
| KB-013 | INFORM 数字 I/O 指令及 PLC/外设握手用途。 | [打开 Yaskawa 原文](https://knowledge.motoman.com/hc/en-us/articles/33634309555730-Programming-Languages-Developer-Tools) | `INFORM > Common Instructions / When to Use` | 通过 |
| KB-017 | 报警必须按代码、机型、版本在官方资料中查询，不能强行驱动或旁路安全功能。 | [打开 FANUC MyPortal 页面](https://www.fanucamerica.com/support/myportal/robot-myportal-registration) | 网页 `FANUC Robotics MyPortal & iStore` 下的 `Alarm Code Lookup` | 通过 |
| KB-019 | 零点校对的概念、快速校对适用前提和事后验证要求。 | [打开 FANUC Quick Mastering 页面](https://techtransfer.fanucamerica.com/tech-transfer/quick-mastering-on-r-30ib-plus-using-power-off-position) | `What you'll learn` 与 `Requirements and considerations`；完整视频需登录 | 通过 |
| KB-020 | Yaskawa GP12 注脂前拆排脂塞的警告，以及周期按机型手册确定。 | [打开 Yaskawa GP12 保养资料](https://knowledge.motoman.com/hc/en-us/articles/23946539687319-Grease-Replenishment-PM-GP12-YR-1-06VXH12-F00) | `S-axis / L-axis / U-axis Grease Replenishment` 的步骤 3–6 | 通过 |
| KB-021 | 日常点检、周期维护和安全关键设备检查应按制造商计划及现场制度。 | [打开 OSHA 原文](https://www.osha.gov/enforcement/directives/std-01-12-002) | `Appendix A` > `Installation, Maintenance and Programming`，条目 8–9 | 通过；历史档案不可代替现行法规 |
| KB-022 | 实体隔离、联锁门及重新使能的安全边界。 | [打开 OSHA 原文](https://www.osha.gov/enforcement/directives/std-01-12-002) | `Appendix A` > `Guarding Methods` > `Interlocked Barrier Guard` | 通过；历史档案不可代替现行法规 |
| KB-023 | 示教进入工作范围时，人员授权、培训、低速和急停熟悉要求。 | [打开 OSHA 原文](https://www.osha.gov/enforcement/directives/std-01-12-002) | `Appendix A` > `Installation, Maintenance and Programming` 条目 11；`Training` 条目 6 | 通过；历史档案不可代替现行法规 |
| KB-024 | ABB RAPID 运动监控、碰撞检测与负载数据边界。 | [打开 PDF 第 145 页](https://library.e.abb.com/public/8dbf836be16446dc89a3af8a012099b5/3HAC050947%20TRM%20RAPID%20Overview%20RW%206-en.pdf#page=145) | `3HAC050947-001 Rev.X`，§2.6 `Motion supervision/collision detection` | 通过 |
| KB-026 | INFORM 的 MOVJ、MOVL、MOVC 选型边界。 | [打开 Yaskawa 原文](https://knowledge.motoman.com/hc/en-us/articles/33634309555730-Programming-Languages-Developer-Tools) | `INFORM > Common Instructions` | 通过 |

## 已排除，不再作为本表的审核对象

KB-015、KB-016、KB-018 的公开 FANUC PDF 均已失效。它们保留在原始知识库中以待
补充可访问的官方资料，但已标记 `demo_eligible: false`，不再进入在线服务或正式
展示；因此不能再用其历史 `verified` 字段作为本 Issue 的验收证据。

## 录入规则

复核结果已在 `data/demo_source_manifest.json` 中按以下规则填写：

- `reviewer`：实际核对资料的人员；
- `reviewed_on`：核对日期，格式 `YYYY-MM-DD`；
- `conclusion`：写明“支持 / 不支持 / 无法访问”及适用机型、数值或安全边界；
- `authorization`：例如“公开厂商资料，仅作定位引用；不复制大段原文”；
- `review_status`：所有字段完整且结论支持时才写 `human_verified`；不支持则写 `rejected`。

只有人工复核后，才可以把对应知识库切片的 `verified` 改为 `true`。机器、脚本和
模型都不得替代这一步。
