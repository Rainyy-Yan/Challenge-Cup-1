# Universal Robots PolyScope X 与脚本资料引用审计

## 资料身份

本卡登记三项 Universal Robots 官方资料：

| REF-ID | 资料 | 版本 | 官方链接 |
|---|---|---|---|
| REF-ROB-024 | PolyScope 5 Script Directory | SW 5.20 | [script_directory_Poly5.pdf](https://www.universal-robots.com/manuals/EN/PDF/SW5_20/scriptmanualG5/script_directory_Poly5.pdf) |
| REF-ROB-025 | TCP Position | PolyScope X 10.12 | [TCP Position 页面](https://www.universal-robots.com/manuals/EN/HTML/SW10_12/Content/prod-usr-man/software/PolyScopeX/polyx-application/polyx-TCP_Position.htm) |
| REF-ROB-026 | Payload and Center of Gravity | PolyScope X 10.11 | [Payload Estimation 页面](https://www.universal-robots.com/manuals/EN/HTML/SW10_11/Content/prod-usr-man/software/PolyScopeX/polyx-application/polyx-TCP_Payload_CenterofGravity.htm) |

- 审查时 Firecrawl 容器（未随仓库分发）：`.firecrawl/search-round2-ur-coordinate-tcp-blend-20260902.json`、
  `.firecrawl/search-round2-ur-tcp-position-wizard-20260902.json`
- 抓取日期：2026-09-02
- 容器 SHA-256：
  `cf82f7c7902f29e307c012050cac48056fc8b48f95644afbc035ab32d9a9d7fe`、
  `ee15fe410c0aade7820f0c05d713fd7f04c4a6c5da71f9bc89eab65e495a449c`

Firecrawl JSON 是多结果容器，哈希覆盖整个容器而不是单独网页。因此每条切片
除哈希外必须同时保留官方 URL 和页面定位，不能只凭容器哈希识别来源。

## 本轮采用范围

| 切片 | 采用事实 |
|---|---|
| `KB-101` | PolyScope X 10.12 TCP 向导至少需要三个不同角度姿态，第四姿态用于校验 |
| `KB-112` | PolyScope X 10.11 负载估算向导采用四个不同姿态估算质量和重心 |
| `KB-114` | SW 5.20 URScript 线程允许 return，但返回值被丢弃 |

同批其他 UR 切片沿用各自原 REF-ID：`KB-099`、`KB-104` 至 `KB-108`、
`KB-110`、`KB-111`、`KB-113` 使用 `REF-ROB-007`；`KB-100` 使用
`REF-ROB-008`；`KB-102`、`KB-103` 使用 `REF-ROB-015`；`KB-109` 使用
`REF-ROB-009`。

## 引用边界

1. 三点是 PolyScope X 10.12 向导的最低要求，不能用来否定其他厂商或版本的
   四点标定流程，也不能外推到 `KB-006` 所引用的 ABB 固定工具 TCP 标定方法。
2. 四姿态负载估算是该向导的输入条件，不是负载或重心精度保证。
3. URScript 线程返回值与普通函数返回值不同；`run` 给出线程句柄，不返回线程
   内部计算结果。
4. 搜索容器中的资料版本不同，引用时必须逐条保留 SW 版本和官方 URL。

## 审查结论

独立审查发现并修正了三条候选：收窄 MoveL、MoveP 与旧切片的重复内容，并把
负载向导的证据摘录替换为缓存中的连续原句。修订后 16 条 UR 候选均通过机器
证据与边界审查，但全部保持 `verified=false`。
