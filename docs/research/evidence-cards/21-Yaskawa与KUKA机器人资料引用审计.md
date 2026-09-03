# Yaskawa 与 KUKA 机器人资料引用审计

## 资料身份

### Yaskawa Motoman

- 资料：*YRC1000 OPTIONS INSTRUCTIONS FOR INFORM LANGUAGE*
- 手册号：178649-1CD
- 官方链接：[Yaskawa Motoman 手册](https://www.motoman.com/getmedia/346F8450-7888-448E-A145-6BAA3B894B74/178649-1CD)
- 审查时 Firecrawl 底稿（未随仓库分发）：`.firecrawl/round10-diverse-yaskawa-inform-manual-178649.md`
- SHA-256：`966e541064be7f4b070538071d1e77b7dd0b642f4cf8207fa6725542b5abab36`

### KUKA

- 资料：KUKA smartPAD solutions 产品页
- 官方链接：[smartPAD robot teach pendant](https://www.kuka.com/en-us/products/robotics-systems/robot-controllers/smartpad-robot-teach-pendant)
- 审查时 Firecrawl 底稿（未随仓库分发）：`.firecrawl/round10-diverse-kuka-smartpad.md`
- SHA-256：`48b33bb09c17097ff05897257b60e2996fb384e31838acc606f86f1e433244f7`
- 抓取日期：2026-09-02

Yaskawa 来源是技术手册，按 A 级厂商资料使用；KUKA 来源是产品营销页，只按 B
级产品说明使用。两者的证据强度不能互换。

## 本轮采用范围

| 切片 | 来源 | 采用事实 |
|---|---|---|
| `KB-115` | Yaskawa | MOVL 的 CR 参数指定圆角半径并形成圆弧插补 |
| `KB-116` | Yaskawa | 使用 CR 时下一运动指令必须位于同一作业 |
| `KB-117` | Yaskawa | 带参数 CALL 最多传递 8 个参数 |
| `KB-118` | Yaskawa | DOUT 控制 GP 通用输出信号通断 |
| `KB-119` | Yaskawa | 多信号条件分支先用 DIN 同批取样 |
| `KB-120` | KUKA | smartPAD pro 急停按钮照明的产品页指示含义 |

## 引用边界

1. Yaskawa 的 CR 数值表经 Firecrawl 提取后有列折行；本轮未把范围数值写入正式
   切片，人工需要时应回看 PDF 第 2-229 页表格。
2. 手册属于 YRC1000 OPTIONS 指令资料，不能证明任意现场控制器已经安装或启用
   对应选件。
3. DIN 条目是程序取样一致性规则，不是安全 I/O、原子总线事务或人员安全互锁。
4. KUKA 页面中的 “safe connection” 保留为页面术语；产品页不足以证明安全回路
   已验收，也不提供 PL、SIL、停机类别、响应时间或人员防护有效性。

## 排除项

本轮 Firecrawl 搜寻还涉及 KUKA KRL 精确资料、FANUC SRVO 报警正文和图片型
Yaskawa 报警页面。由于未取得可定位的官方正文，全部排除，没有用论坛、第三方
维修站或搜索摘要补足数量。

## 审查结论

六条候选均经独立机器复核 URL、定位、底稿、SHA、知识点归属和重复性。
KUKA 条目按审查意见收窄营销表述后入库。全部保持 `verified=false`，等待人工审核。
