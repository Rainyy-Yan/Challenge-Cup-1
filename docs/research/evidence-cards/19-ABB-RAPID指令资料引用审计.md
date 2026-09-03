# ABB RAPID 指令资料引用审计

## 资料身份

- 资料：*RAPID Instructions, Functions and Data types*
- 厂商：ABB Robotics
- 文档号：3HAC065038-001 Revision Q
- 软件边界：RobotWare 7.17
- 官方链接：[ABB Library 下载页](https://search.abb.com/library/Download.aspx?DocumentID=3HAC065038-001&LanguageCode=en&DocumentPartId=&Action=Launch&DocumentRevisionId=Q)
- 审查时 Firecrawl 底稿（未随仓库分发）：`.firecrawl/round10-abb-rapid-instructions-20260902.md`
- 抓取日期：2026-09-02
- SHA-256：`f31f6c5353334687c64c63e833435d2ff29149366e1f54ef393ce5783e16792b`

该底稿由 ABB 官方下载入口完整抓取，文档首页、文档号、修订版和 RobotWare
版本均可在正文定位。它作为 A 级厂商技术手册使用，但只能证明相应版本中
指令、函数和数据类型的定义，不能直接证明某一现场控制器已安装、配置或正确
执行相关功能。

## 本轮采用范围

| 切片 | 定位 | 采用事实 |
|---|---|---|
| `KB-095` | §1.61 CornerPathWarning | 关闭警告不阻止 corner path failure 把 fly-by 点按 fine 点执行 |
| `KB-096` | §1.201 RETURN | procedure 中执行 RETURN 后从调用点之后继续 |
| `KB-097` | §1.223 SetDO | 默认设置输出后立即推进，不等待物理通道完成 |
| `KB-098` | §1.310 WaitDI | 按目标值等待数字输入置位或复位 |

## 引用边界

1. `CornerPathWarning` 控制告警显示，不等于关闭路径转换，也不保证连续过点。
2. `RETURN` 的 procedure、function、trap 和 main 行为必须分开，不可合并成
   一个无条件规则。
3. `SetDO`、`WaitDI` 是普通程序 I/O 语义，不是安全 I/O 认证或人员防护证明。
4. 指令可用性、可选参数和行为应绑定 RobotWare 版本；升级或降级时重新查手册。

## 审查结论

本轮四条候选均经独立机器复核逐条回看底稿、核对定位、版本、SHA、知识点归属
和现有切片重复性。结论只代表机器审查通过，全部保持 `verified=false`，等待
人工复核原始手册。
