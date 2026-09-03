# Universal Robots UR30 用户手册引用审计

## 证据身份

- 来源：[UR30 User Manual 718-786-00（PolyScope 5，SW 5.25）](https://www.universal-robots.com/manuals/EN/PDF/SW5_25/user-manual-UR30-PDF_online/718-786-00_UR30_User_Manual_en_Global.pdf)
- 补充版本：[UR30 User Manual 718-786-00（PolyScope 5，SW 5.20）](https://www.universal-robots.com/manuals/EN/PDF/SW5_20/user-manual-UR30-PDF_online/718-786-00_UR30_User_Manual_en_Global.pdf)
- 类型：机器人厂商官方用户手册
- 来源编号：`REF-ROB-007`
- 检索日期：2026-09-02
- Firecrawl 搜索任务：`01a0613b-bcc1-77c2-8cdc-6d9242b6e7f8`
- Firecrawl 提取文本 SHA-256（SW 5.25）：`0d2a66f22b22e8a00c1e06a65612e07a24ee5be8034ced80606df94d1d229a69`
- Firecrawl 多结果容器 SHA-256（SW 5.20）：`cf82f7c7902f29e307c012050cac48056fc8b48f95644afbc035ab32d9a9d7fe`
- 证据等级：A，但本轮机器审查不能替代人工核实

## 本轮定位范围

本轮只从可定位章节归纳短篇中文事实，没有复制或分发手册正文：

- §7.7 `Freedrive`：自由驱动、启用警告与 `Backdrive`；
- §7.8 `Power Down The Robot`：机械臂、控制箱关机和储能释放；
- §9.1 `Maximum Payload`：负载质量、重心（CoG）和惯量；
- §9.4 `Set Payload`：取放时的负载切换及 `Payload Transition Time`；
- §11.7 `Basic Program Nodes: Move`：`MoveP` 和 `Feature` 相关行为；
- SW 5.20 §2.2.1 `Three-Position Enabling Button`：三位使能按钮；
- SW 5.20 §8.9、§8.10.8：通用数字 I/O 与工具数字输出；
- SW 5.20 §9.2.3、§9.2.5：程序页与选中节点；
- SW 5.20 §9.2.7：`MoveJ`、`MoveL` 与 `MoveP` 的版本限定说明。

后续审查已将 SW 5.20 中有明确章节定位的 `MoveJ` 与 `MoveL` 事实分别纳入
`KB-104`、`KB-105`。两条均保留版本边界，不能当作对现有 `KB-009` 的通用替换。
SW 5.20 哈希对应多结果检索容器，正式定位仍以条目中的官方 URL 和章节为准。

## 适用边界

- 所有操作事实仅适用于条目明确列出的 UR30 与 PolyScope 5 / SW 5.20 或 SW 5.25 对应版本，不跨版本混用，也不外推到其他品牌或机型。
- 手册中的安全提示不能替代具体设备的风险评估、现场隔离和制造商支持。
- Firecrawl 提取文本用于定位与机器审查；正式知识条目保留原始 URL、章节和提取文本哈希，仍须人工回看原 PDF 才能改为 `verified=true`。
- 手册版权归权利人所有；知识库只保存自行改写的短篇技术要点，不保存或再分发手册全文。

## 审查结论

候选先经过来源事实审查和结构去重审查。超出原文的推断已删除，与现有知识重复的候选已淘汰。通过机器审查的条目可以作为 `sourced`、`verified=false` 的待核实材料入库，但不能用于答辩事实举证，也不能视为人工确认。
