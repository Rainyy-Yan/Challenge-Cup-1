# ABB 机器人系统与坐标系资料引用审计

## 证据身份

本卡登记 2026-09-02 通过 Firecrawl 定位的两份 ABB 官方手册、两个官方产品页面和一篇官方功能文章，用于降低机器人知识库对单一厂商来源的依赖。机器提取和多智能体复核不等于人工核实，新条目仍保持 `verified=false`。

| REF-ID | 官方资料 | 版本 / 类型 | Firecrawl 提取文本 SHA-256 |
|---|---|---|---|
| REF-ROB-018 | [Operating manual - IRB 14000](https://library.e.abb.com/public/fc995c8b675940f585918b78a455ae78/3HAC052986%20OM%20IRB%2014000-en.pdf) | 3HAC052986-001 Revision J，RobotWare 6.16.02 | `5217dc74a12e06cd6d909254a6ccca02e144071a200f499b4009fbcabb942803` |
| REF-ROB-019 | [Product manual - IRB 4600](https://library.e.abb.com/public/5f92e5e4d7a44780aa108e4f9b5fd04d/3HAC033453%20PM%20IRB%204600-en.pdf) | 3HAC033453-001 Revision AL | `ff94262b246a296cbd0bb66639046cfa3654f8bc3842415ad27ac46217e63c55` |
| REF-ROB-020 | [BullsEye - tool center point calibration](https://new.abb.com/products/robotics/nl/equipment-ecosystem/arc-welding/bullseye) | ABB 弧焊设备生态产品页，抓取于 2026-09-02 | `5cc59fe22c0af3ace2c5f97dfc778ccee33162986ed9e3dadea67aa4eccb34bd` |
| REF-ROB-021 | [RobotStudio Suite](https://www.abb.com/global/en/areas/robotics/products/software/robotstudio-suite) | ABB 机器人软件产品页，抓取于 2026-09-02 | `a0df9f94f97326e7ca2dfabbcb4ffb8d9f416a27c52e158380bed13ccd2bc2f4` |
| REF-ROB-022 | [10 ways RobotStudio can help optimize robot performance](https://new.abb.com/news/detail/96221/10-ways-that-abbs-robotstudior-software-can-help-you-optimize-your-robots-performance) | ABB 官方功能文章，发布于 2022-10-21 | `83e0dea78adc99cb37fc1db05b419acaf507886444be8384d0cf2b6a292b26fc` |

## 采用范围

- `REF-ROB-018`：IRB 14000 手册中工具坐标系、工件坐标系和基座坐标系的定义及编程用途；线性运动对相近机器人构型的要求；数字输入切换碰撞对象时与路径规划不同步的边界。
- `REF-ROB-019`：IRB 4600 机械臂可配套的 IRC5、OmniCore 控制器系列及文档关系；维护周期的计量方式、异常事件检查；Axis Calibration 的逐轴工具边界、精校准与参考校准选择前提；RobotLoad 单项负载工况校核。
- `REF-ROB-020`：BullsEye 在 ABB 弧焊设备生态中执行自动 TCP 校准的产品能力。
- `REF-ROB-021`：RobotStudio Suite 的虚拟控制器、路径规划、无碰撞仿真、工作流和循环时间优化用途。
- `REF-ROB-022`：RobotStudio Signal Analyzer 的跨信号时序观察，以及虚拟控制器所使用的软件、程序和配置基础。

## 不能外推的部分

- 不把 IRB 14000 与 IRB 4600 的定义、版本或文档关系无条件迁移到其他 ABB 产品。
- IRB 14000 的 Collision Avoidance 不构成人员安全功能；碰撞对象的激活信号不能替代风险评估确定的安全级互锁或防护措施。
- IRB 4600 的维护周期、校准流程和负载校核结论均不外推为其他型号的通用数值或操作步骤；具体作业仍须结合完整产品手册、FlexPendant 指引、产品规格书及现场风险评估。
- Axis Calibration 期间机器人需要连接电源且可能发生不可预测运动；必须确保工作区无人，并由经 ABB 培训且具备相应知识的人员执行。手册中的接触力数值仅限该手册对应设备与方法。
- 坐标系定义不是 TCP 或工件坐标系的完整标定流程。
- BullsEye 页面不能证明任意末端工具都适用，也不提供通用标定步骤、精度保证或绕过风险评估与防护的授权。
- RobotStudio 的“无碰撞”仿真、虚拟控制器和跨信号分析是离线工程能力，不能证明真实工作站已经无碰撞、满足安全要求、达到目标节拍或通过投产验收；产品页中的百分比不作为通用性能承诺。
- Signal Analyzer 条目不证明任何 I/O 配置、互锁逻辑或现场时序已经验证。
- 官方手册或产品页能够支持对应产品事实，不等于条目已经由项目领域人员人工核实。
- 只保存自行改写的短篇技术要点、原始 URL、章节和哈希，不复制或再分发手册全文。

## 审查状态

候选已按事实蕴含、机型与软件版本、安全边界、知识点归属和重复性分开审查。通过记录仅以 `origin=sourced`、`verified=false` 进入知识库，人工逐条回看原资料后才能改变核实状态。
