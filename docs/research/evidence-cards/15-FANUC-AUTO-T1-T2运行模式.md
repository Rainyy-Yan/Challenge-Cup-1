# FANUC AUTO、T1、T2 运行模式

## 证据身份

- 来源：[FANUC External Mode Select](https://www.fanucamerica.com/products/software/robot/external-mode-select)
- 来源编号：`REF-ROB-014`
- 类型：厂商官方产品与安全集成说明
- 抓取日期：2026-09-02
- Firecrawl Scrape ID：`01a061ff-737b-725c-833a-254e4a11c022`
- Firecrawl 提取 SHA-256：`1089a916429f4bb9ee675af0477d950efab324e927a0b8512f3e0a639b4cbd90`
- 证据等级：B；厂商一手资料，但不是跨品牌安全标准

## 可采用结论

FANUC 官方页面确认，External Mode Select 可通过外部安全系统为相关 FANUC 控制器选择 `AUTO`、`T1` 或 `T2` 运行模式，并与 Dual Check Safety 集成。

## 适用边界

- 该结论只适用于页面描述的 FANUC 产品和选件，不能外推到所有工业机器人。
- 页面没有给出 T1/T2 的速度上限，也没有规定 T2 操作者必须持有何种资格。
- 产品说明不足以替代设备操作手册、风险评估和适用安全标准。

## 项目采用

已作为品牌专用运行模式案例写入 `KB-080`；不能用于恢复 `KB-004` 的通用安全断言。
