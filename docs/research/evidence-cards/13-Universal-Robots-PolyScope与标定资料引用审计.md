# Universal Robots PolyScope 与标定资料引用审计

## 证据身份

本卡登记 2026-09-02 通过 Firecrawl 定位的五份 Universal Robots 官方版本化资料。它们用于补充知识库薄弱知识点，不改变“机器审查不能写入 `verified=true`”的边界。

| REF-ID | 官方资料 | 版本 | Firecrawl 提取文本 SHA-256 |
|---|---|---|---|
| REF-ROB-008 | [TCP Configuration](https://www.universal-robots.com/manuals/EN/HTML/SW5_24/Content/prod-usr-man/software/PolyScope/content/installation_g5/installation_TCP_configuration_en.htm) | PolyScope 5.24 | `b23ed88b24316ed9f808c36ae7ffb98031b4cba479b160df3f9d00f34967f720` |
| REF-ROB-009 | [Robot Program Configuration](https://www.universal-robots.com/manuals/EN/HTML/SW5_24/Content/prod-usr-man/software/PolyScope/content/BasicProgNodes/commandtab_varinit_en.htm) | PolyScope 5.24 | `430537c3a1341a3479fe74ed4418a6fb978c0b8b7a33508f4ebea27dcd1191b5` |
| REF-ROB-010 | [Zeroing of Joints](https://www.universal-robots.com/manuals/EN/HTML/SW5_19/Content/prod-serv-man/E-series/serv-man-joint-zeroing.htm) | PolyScope 5.19 服务手册 | `3817c96a6766372273caeeac1998614ddd7290f75c18cff9c1cdeeb97215c46b` |
| REF-ROB-011 | [Kinematic Calibration User Manual](https://www.universal-robots.com/manuals/EN/PDF/SW5_25/rck-og-PDF_combined_online/UR%20Calibration%20Manual%20Combined.pdf) | SW 5.25 | `e65b5d9b9e052e7ebe7a81c0d11be1f517669805069c9dbb90c53f8262c7fa32` |
| REF-ROB-012 | [Variable Waypoint](https://www.universal-robots.com/manuals/EN/HTML/SW5_24/Content/prod-usr-man/software/PolyScope/content/BasicProgNodes/commandtab_waypoint_variable_en.htm) | PolyScope 5.24 | `6f766563379e0c9540026584cd027e5b6f0ae0deefb2bd6187ea0bb47415c1cc` |

同一官方页面在后续候选审查中重新抓取，缓存封装不同，因此本次储备另采用以下 SHA-256；旧哈希作为历史快照继续保留：

- `REF-ROB-008` Active TCP 定位：`cd62c376d0b54c918a926371404386cc2ddc10123666f8286f9da0b702dd739b`
- `REF-ROB-009` Set Initial Variable Value 定位：`eb0b344be4df62bebab05f93bfb3a08a352f3de374f0480edaf6c3359df2ae16`

## 采用范围

- `REF-ROB-008`：TCP 相对工具输出法兰的平移、旋转和零值含义。
- `REF-ROB-009`：主程序启动前序列与永久循环之间的执行次数关系。
- `REF-ROB-010`：关节更换后零位校对的自动、手动方法及权限边界。
- `REF-ROB-011`：运动学标定修正关节角与空间坐标关系的作用。
- `REF-ROB-012`：变量路点或相邻变量路点条件下不检查混合半径重叠的行为。

UR30 用户手册中与本批有关的 Move Tab、Feature、I/O、停止性能和机械臂检查内容继续引用 `REF-ROB-007`，不为同一文档重复分配来源号。

## 不能外推的部分

- 不把 PolyScope 5.19、5.24 与 SW 5.25 的界面或行为无条件迁移到其他版本。
- 零位校对和双机器人标定涉及 Expert mode、专用工装、维修权限及安全条件；知识库摘要不能替代服务手册或现场资质要求。
- 官方资料能够支持产品行为，不等于相应内容已经由项目领域人员核实。
- 只保存自行改写的短篇技术要点、原始 URL、章节和哈希，不复制或再分发手册全文。

## 审查状态

本轮候选先经过事实边界、适用版本、知识点归属和重复性机器审查。通过的记录仅以 `origin=sourced`、`verified=false` 进入知识库，人工逐条回看原资料后才能改变核实状态。
