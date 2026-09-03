# Universal Robots Feature 与节拍优化资料引用审计

## 证据身份

本卡登记 2026-09-02 通过 Firecrawl 定位的三份 Universal Robots 官方资料。资料用于补齐工件坐标系、转弯区和节拍优化的薄弱切片，不改变“机器审查不能写入 `verified=true`”的边界。

| REF-ID | 官方资料 | 版本 / 类型 | Firecrawl 提取文本 SHA-256 |
|---|---|---|---|
| REF-ROB-015 | [Features](https://www.universal-robots.com/manuals/EN/HTML/SW5_25/Content/prod-usr-man/software/PolyScope/content/installation_g5/installation_features_en.htm) | PolyScope 5.25 在线手册 | `8c9c39a8cffe7e953748de7d4435e58f3ecec9b532d2b5ef398b5bebaafeae0c` |
| REF-ROB-016 | [Blending](https://www.universal-robots.com/manuals/EN/HTML/SW5_24/Content/prod-usr-man/software/PolyScope/content/BasicProgNodes/commandtab_way_blend.htm) | PolyScope 5.24 在线手册 | `70f92d9c62fe87eff2d85f5ff594f02c0f394d7ebaf40a89e56c80c2a653db44` |
| REF-ROB-017 | [UR Studio](https://www.universal-robots.com/products/ur-studio/) | 官方产品页，抓取于 2026-09-02 | `b1c641801b629bc72231f72bc0e3041198984d519e9cbe09e1fb8598231d61d3` |

## 采用范围

- `REF-ROB-015`：Feature 相对基座的六维位姿、工件或工作台参照、平面三点示教及按右手定则生成坐标轴。
- `REF-ROB-016`：Blending 在中间路点连续过渡、混合半径及进入半径后可能偏离原始路径的机制。
- `REF-ROB-017`：数字工作单元中测试程序、计算 cycle time，以及比较可达范围、碰撞、速度、布置和工作流变量。

UR30 机械臂主要部件继续引用 `REF-ROB-007`；关节手动零位流程继续引用 `REF-ROB-010`，不为同一文档重复分配来源号。

## 不能外推的部分

- 不把 PolyScope 5.24、5.25 的界面和行为无条件迁移到其他软件版本。
- 混合会允许轨迹在路点附近发生偏离；摘要不能替代应用风险评估、碰撞检查或现场试运行。
- UR Studio 页面描述的是仿真能力；仿真节拍、碰撞和布置结果不能替代真实工作站的安全验证与实际性能确认。
- 官方资料能够支持产品行为，不等于相应条目已经由项目领域人员人工核实。
- 只保存自行改写的短篇技术要点、原始 URL、章节和哈希，不复制或再分发手册全文。

## 审查状态

候选先经过事实蕴含、适用版本、知识点归属、重复性和结构门禁机器审查。通过记录仅以 `origin=sourced`、`verified=false` 进入知识库，人工逐条回看原资料后才能改变核实状态。
