"""知识库导入与校验。

    python3 -m tools.kb_import --check  data/incoming/*.md
    python3 -m tools.kb_import --apply  data/incoming/*.md

为什么需要这个工具：

骨架版到完整版之间差的是内容 —— 26 条切片要扩到 300 条以上。
这件事只能由懂领域、手上有真实资料的人来做，代劳不了。
但可以把它从"手改 JSONL、错了没人知道"变成"有格式、有校验、有门禁"。

**校验在入库时做，不在事后做。** 事后校验的问题是：一条不合规的切片
一旦进了库，就会被检索到、被生成引用、被审核认证为"有依据"，
再想揪出来得翻整条链路。入口拦住只需要一次拒绝。

────────────────────────────────────────────────────────────────────
输入格式：Markdown，一条切片一个二级标题
────────────────────────────────────────────────────────────────────

    ## 示教器三位使能开关
    - kp: KP-02
    - source: FANUC 操作说明书 B-83284CM 第 3.2 节
    - verified: true

    示教器上的三位使能开关有松开、中间位、按死三种状态……

选 Markdown 而不是 CSV 或 Excel，理由是正文里逗号、引号、换行都很常见，
CSV 转义一旦出错就是静默截断，而 Markdown 不会。
录入的人也更容易在编辑器里直接读写。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import config

# ---- 规范阈值。改动请同步 docs/知识库切片规范.md ----
#
# 这组数字是被现实修正过的。规范初稿写的是"150 到 300 字"，
# 但写完导入器一测才发现：**骨架版 26 条切片全部落在 71 到 104 字，
# 没有一条符合我们自己写的规范**。
#
# 两种改法：把切片写长，或者把阈值改对。选后者，理由是
# "一条切片只讲一件事"这条更根本 —— 一个工业事实（一个报警码的含义、
# 一条公差要求）用中文讲清楚通常就是八九十字，硬凑到 150 字只能靠注水，
# 而注水的句子会拉低审核的证据覆盖率，反过来伤害幻觉检测。
#
# 所以下限定在 60：低于此值确实上下文不足；理想区间放到 80 到 200。
# 上限保留 400，超过基本就是塞了多个事实。
LEN_MIN = 60
LEN_MAX = 400
LEN_IDEAL = (80, 200)
PLACEHOLDER = "【待核实·占位出处】"

# 约数措辞。规范要求数值写全，写约数等于把这个数从知识库里删掉。
_VAGUE = re.compile(r"约|大约|左右|上下|若干|一些|不等|视情况|酌情")
# 观点性措辞。切片只放可核验的事实。
_OPINION = re.compile(r"我们认为|个人认为|建议采用|最好是|应该说|不妨")

SEVERITY_ORDER = {"error": 0, "warn": 1, "info": 2}


@dataclass
class Issue:
    severity: str
    code: str
    detail: str


@dataclass
class Draft:
    id: str
    kp: str
    title: str
    source: str
    text: str
    verified: bool = False
    source_note: str = ""
    issues: list[Issue] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return any(i.severity == "error" for i in self.issues)


# ============================================================
# 解析
# ============================================================

_META = re.compile(r"^\s*[-*]\s*(\w+)\s*[:：]\s*(.+?)\s*$")


def parse_markdown(text: str, src_name: str) -> list[Draft]:
    """把一个 Markdown 文件切成若干草稿条目。"""
    drafts: list[Draft] = []
    blocks = re.split(r"^##\s+", text, flags=re.M)[1:]
    for i, blk in enumerate(blocks, 1):
        lines = blk.splitlines()
        title = lines[0].strip() if lines else ""
        meta: dict[str, str] = {}
        body: list[str] = []
        in_meta = True
        for ln in lines[1:]:
            m = _META.match(ln)
            if in_meta and m:
                meta[m.group(1).lower()] = m.group(2)
                continue
            if ln.strip():
                in_meta = False
            body.append(ln)
        d = Draft(
            id=meta.get("id", ""),
            kp=meta.get("kp", "").strip(),
            title=title,
            source=meta.get("source", "").strip(),
            text=" ".join(x.strip() for x in body if x.strip()),
            verified=str(meta.get("verified", "")).strip().lower() in ("true", "yes", "1", "是"),
            source_note=meta.get("note", "").strip(),
        )
        if not d.title:
            d.issues.append(Issue("error", "NOTITLE", f"{src_name} 第 {i} 段缺少标题"))
        drafts.append(d)
    return drafts


# ============================================================
# 校验
# ============================================================

def validate(d: Draft, kps: set[str], used_ids: set[str],
             existing_texts: dict[str, str]) -> None:
    """逐条校验。error 级一律拒收，warn 级放行但记账。"""

    # ---- 必填 ----
    if not d.kp:
        d.issues.append(Issue("error", "NOKP", "缺少 kp 字段"))
    elif d.kp not in kps:
        d.issues.append(Issue("error", "BADKP",
                              f"知识点 {d.kp} 不在知识点表里。"
                              "归属错了检索的硬过滤就失效了，必须先建知识点"))
    if not d.source:
        d.issues.append(Issue("error", "NOSOURCE", "缺少 source 字段"))
    if not d.text:
        d.issues.append(Issue("error", "NOTEXT", "正文为空"))
        return

    # ---- 溯源纪律：这一组是本工具存在的主要理由 ----
    if not d.verified and PLACEHOLDER not in d.source:
        d.issues.append(Issue(
            "error", "FAKESOURCE",
            f"未核实却给了看似真实的出处「{d.source[:28]}」。"
            f"未核实的必须加前缀「{PLACEHOLDER}」—— "
            "一个像真的假出处会让所有人误以为内容有据可查，比明显编造更危险"))
    if d.verified and PLACEHOLDER in d.source:
        d.issues.append(Issue("error", "CONTRADICT",
                              "标了 verified 却带着待核实前缀，二者必居其一"))
    if d.verified and len(d.source) < 12:
        d.issues.append(Issue(
            "error", "THINSOURCE",
            f"标为已核实但出处只有「{d.source}」。"
            "已核实的出处要具体到手册编号或标准条款，答辩要能当场翻到"))

    # ---- 长度 ----
    n = len(d.text)
    if n < LEN_MIN:
        d.issues.append(Issue("error", "TOOSHORT",
                              f"正文 {n} 字，少于 {LEN_MIN} 字上下文不足，模型容易脑补"))
    elif n > LEN_MAX:
        d.issues.append(Issue("error", "TOOLONG",
                              f"正文 {n} 字，超过 {LEN_MAX} 字。"
                              "一条塞多个事实时审核无法定位到句，请拆开"))
    elif not (LEN_IDEAL[0] <= n <= LEN_IDEAL[1]):
        d.issues.append(Issue("info", "LENOFF",
                              f"正文 {n} 字，理想区间 {LEN_IDEAL[0]}-{LEN_IDEAL[1]}"))

    # ---- 内容质量 ----
    if _VAGUE.search(d.text):
        w = _VAGUE.search(d.text).group(0)
        d.issues.append(Issue(
            "warn", "VAGUE",
            f"出现约数措辞「{w}」。数值必须写全 —— "
            "写成约数等于把这个数从知识库里删掉，后面带数字的生成会被全部拦下"))
    if _OPINION.search(d.text):
        w = _OPINION.search(d.text).group(0)
        d.issues.append(Issue("warn", "OPINION",
                              f"出现观点性措辞「{w}」，切片只放可核验的事实"))

    # 一条只讲一件事：句子过多通常意味着塞了多个事实
    sents = [s for s in re.split(r"[。！？]", d.text) if len(s.strip()) >= 8]
    if len(sents) > 5:
        d.issues.append(Issue("warn", "MULTIFACT",
                              f"正文含 {len(sents)} 个句子，可能塞了多个事实，建议拆分"))

    # ---- id 唯一且不复用 ----
    if d.id:
        if d.id in used_ids:
            d.issues.append(Issue("error", "DUPID",
                                  f"编号 {d.id} 与已有切片重复。"
                                  "编号永不复用 —— 断言里存的是引用编号，"
                                  "复用会让历史评测数据全部对不上"))

    # ---- 近重复 ----
    key = re.sub(r"\W", "", d.text)[:60]
    for eid, etext in existing_texts.items():
        if key and key == re.sub(r"\W", "", etext)[:60]:
            d.issues.append(Issue("warn", "NEARDUP",
                                  f"与已有切片 {eid} 高度相似，确认不是重复录入"))
            break


def next_id(existing: list[dict]) -> int:
    mx = 0
    for c in existing:
        m = re.match(r"KB-(\d+)", c.get("id", ""))
        if m:
            mx = max(mx, int(m.group(1)))
    return mx + 1


# ============================================================
# 主流程
# ============================================================

def load_existing() -> list[dict]:
    p = Path(config.KB_PATH)
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def run(paths: list[Path], apply: bool) -> int:
    kps = {k["id"] for k in
           json.loads(Path(config.KP_PATH).read_text(encoding="utf-8"))["points"]}
    existing = load_existing()
    used = {c["id"] for c in existing}
    texts = {c["id"]: c.get("text", "") for c in existing}

    drafts: list[Draft] = []
    for p in paths:
        if not p.exists():
            print(f"找不到文件：{p}")
            return 2
        drafts.extend(parse_markdown(p.read_text(encoding="utf-8"), p.name))

    if not drafts:
        print("没有解析出任何切片。检查文件里是否用二级标题（##）分条。")
        return 2

    seq = next_id(existing)
    for d in drafts:
        validate(d, kps, used, texts)
        if not d.id and not d.blocked:
            d.id = f"KB-{seq:03d}"
            seq += 1
        if d.id:
            used.add(d.id)

    ok = [d for d in drafts if not d.blocked]
    bad = [d for d in drafts if d.blocked]
    warned = [d for d in ok if d.issues]

    print(f"解析 {len(drafts)} 条：可入库 {len(ok)} 条，拒收 {len(bad)} 条，"
          f"其中带提醒 {len(warned)} 条")
    print(f"已核实 {sum(1 for d in ok if d.verified)} 条 / "
          f"未核实 {sum(1 for d in ok if not d.verified)} 条")

    if bad:
        print("\n── 拒收（必须修改后重报）──")
        for d in bad:
            print(f"  「{d.title[:26]}」")
            for i in sorted(d.issues, key=lambda x: SEVERITY_ORDER[x.severity]):
                if i.severity == "error":
                    print(f"      {i.code}  {i.detail}")

    if warned:
        print("\n── 提醒（可入库，但建议处理）──")
        for d in warned[:10]:
            ws = [i for i in d.issues if i.severity == "warn"]
            if ws:
                print(f"  {d.id} 「{d.title[:22]}」")
                for i in ws:
                    print(f"      {i.code}  {i.detail}")

    if not apply:
        print("\n未写入。确认无误后加 --apply 入库。")
        return 1 if bad else 0

    if bad:
        print("\n存在拒收条目，整批不入库。修好再来 —— "
              "半批入库会让编号和核实率都对不上账。")
        return 2

    with open(config.KB_PATH, "a", encoding="utf-8") as fh:
        for d in ok:
            rec = {"id": d.id, "kp": d.kp, "title": d.title,
                   "source": d.source, "text": d.text, "verified": d.verified}
            if d.source_note:
                rec["source_note"] = d.source_note
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    total = len(existing) + len(ok)
    ver = sum(1 for c in existing if c.get("verified")) + sum(1 for d in ok if d.verified)
    print(f"\n已入库 {len(ok)} 条。知识库现有 {total} 条，"
          f"已核实 {ver} 条（{ver / total:.0%}）")
    if ver / total < 0.8:
        print("核实率仍低于八成，评测数字暂不能作为答辩举证。")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--apply", action="store_true", help="校验通过后写入知识库")
    args = ap.parse_args()
    sys.exit(run(args.files, args.apply))


if __name__ == "__main__":
    main()
