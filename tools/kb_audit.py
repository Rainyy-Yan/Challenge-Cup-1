"""知识库交叉校验：查库内矛盾与结构问题。

    python3 -m tools.kb_audit                 # 只报告
    python3 -m tools.kb_audit --quarantine    # 把问题条目移进隔离区

与其他两个检查工具的分工：

  tools/kb_import    入库门禁，查**单条**切片的格式与溯源纪律
  agents/factcheck   对外核实，查切片与**外部资料**是否一致
  tools/kb_audit     本模块，查切片**之间**是否自相矛盾

第三件事必须单独做，因为前两者都看不见它。
单条格式完全合规、出处也真实的两条切片，可以互相打架 ——
同一个报警码在 A 页写"急停"、在 B 页写"过载"，
两条都过了入库门禁，外部核实也可能各自找到支持的来源。
只有把它们放在一起比才能发现。

这类矛盾的危害是隐蔽的：检索时两条都会被召回，
生成的断言无论采信哪一条都能通过审核（因为确实"有依据"），
于是幻觉率照样是零，而输出的内容有一半是错的。
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import config
from core.retrieval import Retriever, jaccard_like, numbers_in, overlap_ratio

# 主题键：抽出切片讨论的"对象"。同一对象的不同说法才值得比。
_SUBJECT = re.compile(r"[A-Z]{2,}[-–]?\d{2,}|[A-Z]\d[-–]?\d*")

DUP_SIM = 0.82          # 近重复阈值
CONFLICT_SIM = 0.45     # 说法相近的下限，用于识别"数值被改"型冲突
DEFN_SIM = 0.45         # 低于此值视为说法迥异，用于识别"含义不同"型冲突


@dataclass
class Finding:
    kind: str
    severity: str
    ids: list[str]
    detail: str


@dataclass
class AuditReport:
    findings: list[Finding] = field(default_factory=list)
    kp_coverage: dict = field(default_factory=dict)
    stats: dict = field(default_factory=dict)

    def by_kind(self) -> dict:
        c: dict[str, int] = {}
        for f in self.findings:
            c[f.kind] = c.get(f.kind, 0) + 1
        return c


def subjects_of(text: str) -> set[str]:
    """抽出切片里的型号、代码一类的"讨论对象"。"""
    return {m.group(0).replace("–", "-").upper() for m in _SUBJECT.finditer(text)}


def _definition_of(text: str, subj: str) -> str:
    """抽出切片中对某个对象下定义的那一句，供人工对照。

    找包含该对象的第一个分句。工业文档里定义句的形态相当固定
    （「SRVO-005 含义为机器人超程」），不需要复杂的句法分析。
    """
    # 只在句号级别切分，不在分号处切。
    #
    # 表格摄入产生的切片形如
    #   「报警代码为SRVO-001；含义为操作面板急停被按下；处理方法为…」
    # 按分号切会只取到「报警代码为SRVO-001」这半截，
    # 拿它去和正文切片的完整定义句比，必然判成"含义不同"—— 纯误报。
    # 定义的语义单元是整句，分号只是句内并列。
    for part in re.split(r"[。！？\n]", text):
        if subj.lower() in part.lower().replace("–", "-"):
            p = part.strip()
            # 半截话（只有对象没有谓述）不构成定义，不参与比对
            return p if len(p) >= len(subj) + 6 else ""
    return ""


def audit(chunks, kps: list[dict]) -> AuditReport:
    rep = AuditReport()
    n = len(chunks)

    # ---- 一、按讨论对象分组，查数值与含义冲突 ----
    #
    # 这是最有价值的一项。同一个报警码、同一个型号，
    # 两条切片给出不同的数字或不同的含义，必有一条是错的。
    by_subject: dict[str, list] = defaultdict(list)
    for c in chunks:
        for s in subjects_of(f"{c.title} {c.text}"):
            by_subject[s].append(c)

    # 相似度在这里**不能**当作冲突的前提条件。
    #
    # 第一版写的是"相似度够高才比对"，结果漏掉了最重要的一类矛盾：
    # KB-017 说 SRVO-005 是机器人超程，注入的 KB-900 说它是伺服放大器过载，
    # 两条讲的是同一个报警码，相似度却只有 0.175 ——
    # **恰恰因为它们说的完全不同**。拿相似度做门槛，逻辑正好反了：
    # 越是严重的矛盾，两条切片越不像。
    #
    # 正确的判据是：讨论对象相同（同一个报警码、同一个型号）就该比，
    # 比的是它们给出的**结论**是否一致。相似度只用来区分冲突的类型 ——
    # 高相似度加数值不同是"数值被改"，低相似度是"含义不同"，
    # 后者往往更严重。
    for subj, group in sorted(by_subject.items()):
        if len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                sim = jaccard_like(a.text, b.text)
                na, nb = numbers_in(a.text), numbers_in(b.text)

                if sim >= CONFLICT_SIM and na and nb and na != nb:
                    rep.findings.append(Finding(
                        "NUMCONFLICT", "error", [a.id, b.id],
                        f"同一对象 {subj} 的两条切片说法相近但数值不一致，"
                        f"差异值 {sorted(na ^ nb)}。必有一条是错的，须回查原文"))
                    continue

                if sim < DEFN_SIM:
                    # 同一对象、说法差异极大 —— 多半是含义定义冲突。
                    # 用各自的定义句做对照，方便人一眼看出分歧在哪。
                    da, db = _definition_of(a.text, subj), _definition_of(b.text, subj)
                    if da and db and jaccard_like(da, db) < 0.4:
                        rep.findings.append(Finding(
                            "DEFCONFLICT", "error", [a.id, b.id],
                            f"同一对象 {subj} 被赋予了不同含义："
                            f"「{da[:26]}」与「{db[:26]}」。"
                            "两条都能通过审核，检索时都会被召回，必须裁定后删掉错的一条"))

    # ---- 二、近重复 ----
    seen_pairs = set()
    for i in range(n):
        for j in range(i + 1, n):
            a, b = chunks[i], chunks[j]
            if (a.id, b.id) in seen_pairs:
                continue
            sim = jaccard_like(a.text, b.text)
            if sim >= DUP_SIM:
                seen_pairs.add((a.id, b.id))
                same_src = a.source.split("｜")[0] == b.source.split("｜")[0]
                rep.findings.append(Finding(
                    "NEARDUP", "warn", [a.id, b.id],
                    f"相似度 {sim:.2f}"
                    + ("，同一来源，多半是重复摄入" if same_src
                       else "，来自不同来源，可能是转载或互相抄录")))

    # ---- 三、知识点覆盖 ----
    kp_ids = [k["id"] for k in kps]
    kp_name = {k["id"]: k["name"] for k in kps}
    cnt = Counter(c.kp for c in chunks)
    for kid in kp_ids:
        rep.kp_coverage[kid] = cnt.get(kid, 0)
        if cnt.get(kid, 0) == 0:
            rep.findings.append(Finding(
                "EMPTYKP", "error", [kid],
                f"知识点「{kp_name[kid]}」没有任何切片。"
                "覆盖率指标会在这里直接掉下去，且该知识点无法生成资源"))
        elif cnt[kid] < 3:
            rep.findings.append(Finding(
                "THINKP", "warn", [kid],
                f"知识点「{kp_name[kid]}」只有 {cnt[kid]} 条切片，"
                "双专家的宽窄检索会拿到几乎相同的上下文，交叉验证失去意义"))

    # 孤儿：切片挂到了不存在的知识点
    for c in chunks:
        if c.kp not in kp_ids:
            rep.findings.append(Finding(
                "ORPHAN", "error", [c.id],
                f"挂在不存在的知识点 {c.kp} 上，检索永远召回不到"))

    # ---- 四、溯源结构 ----
    for c in chunks:
        if not c.verified and "待核实" not in c.source and "｜sha:" not in c.source:
            rep.findings.append(Finding(
                "FAKESOURCE", "error", [c.id],
                f"未核实但出处看起来是真的：{c.source[:34]}。"
                "既非占位标记，也不是摄入产生的可回溯出处"))

    # ---- 五、内容自足性 ----
    #
    # 切片里出现"如上表""见前文""该步骤"这类指代，说明它依赖被切掉的上下文。
    # 这种切片单独拿出来看是残缺的，模型引用它必然要靠脑补补全。
    dangling = re.compile(r"如上|如下表|见前文|见上文|上述步骤|该步骤|前述|下表所示|上图")
    for c in chunks:
        m = dangling.search(c.text)
        if m:
            rep.findings.append(Finding(
                "DANGLING", "warn", [c.id],
                f"含跨切片指代「{m.group(0)}」，脱离原文上下文后语义不完整"))

    ver = sum(1 for c in chunks if c.verified)
    sourced = sum(1 for c in chunks if "｜sha:" in c.source)
    rep.stats = {
        "chunks": n, "verified": ver, "sourced": sourced,
        "verified_ratio": round(ver / n, 3) if n else 0,
        "kp_total": len(kp_ids),
        "kp_empty": sum(1 for k in kp_ids if cnt.get(k, 0) == 0),
        "errors": sum(1 for f in rep.findings if f.severity == "error"),
        "warns": sum(1 for f in rep.findings if f.severity == "warn"),
    }
    return rep


KIND_CN = {
    "NUMCONFLICT": "同一对象数值冲突",
    "DEFCONFLICT": "同一对象含义冲突",
    "NEARDUP": "近重复",
    "EMPTYKP": "知识点无切片",
    "THINKP": "知识点切片过少",
    "ORPHAN": "挂在不存在的知识点",
    "FAKESOURCE": "出处可疑",
    "DANGLING": "含跨切片指代",
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quarantine", action="store_true",
                    help="把 error 级条目移进隔离文件（不删除原文件内容）")
    ap.add_argument("--out", default="data/audit")
    args = ap.parse_args()

    R = Retriever.from_jsonl(config.KB_PATH)
    kps = json.loads(Path(config.KP_PATH).read_text(encoding="utf-8"))["points"]
    rep = audit(R.chunks, kps)
    st = rep.stats

    print(f"知识库 {st['chunks']} 条：已核实 {st['verified']} 条"
          f"（{st['verified_ratio']:.0%}），有可回溯出处 {st['sourced']} 条")
    print(f"知识点 {st['kp_total']} 个，其中 {st['kp_empty']} 个没有切片")
    print(f"发现问题：错误 {st['errors']} 项，提醒 {st['warns']} 项")

    if rep.findings:
        print()
        for kind, n in sorted(rep.by_kind().items(), key=lambda x: -x[1]):
            print(f"  {n:>4}  {KIND_CN.get(kind, kind)}")

    errs = [f for f in rep.findings if f.severity == "error"]
    if errs:
        print("\n── 错误（须处理）──")
        for f in errs[:15]:
            print(f"  [{KIND_CN.get(f.kind, f.kind)}] {'、'.join(f.ids)}")
            print(f"      {f.detail}")

    warns = [f for f in rep.findings if f.severity == "warn"]
    if warns:
        print("\n── 提醒 ──")
        for f in warns[:10]:
            print(f"  [{KIND_CN.get(f.kind, f.kind)}] {'、'.join(f.ids)}　{f.detail}")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "audit.json").write_text(json.dumps({
        "stats": st, "kp_coverage": rep.kp_coverage,
        "findings": [{"kind": f.kind, "severity": f.severity,
                      "ids": f.ids, "detail": f.detail} for f in rep.findings],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n明细已写入 {out}/audit.json")

    if args.quarantine:
        # 冲突类问题涉及两条切片，不能两条都隔离。
        #
        # 实测第一版把 KB-004 与 KB-017 也一起移走了 ——
        # 这两条是**已人工核实**的，反倒被注入的错误数据带下水。
        # 已核实的一方是当前唯一可信的版本，隔离它等于让错误数据
        # 把正确数据挤出知识库，这是最糟糕的一种失败。
        #
        # 规则：冲突双方中，已核实的留下，未核实的隔离；
        # 双方都未核实时两条都留下，仅报告 —— 机器无从判断谁对，
        # 此时该做的是叫人来裁，不是随便挑一条丢掉。
        verified_ids = {c.id for c in R.chunks if c.verified}
        bad_ids: set[str] = set()
        for f in errs:
            ids = [i for i in f.ids if i.startswith("KB-")]
            if f.kind in ("NUMCONFLICT", "DEFCONFLICT") and len(ids) == 2:
                unver = [i for i in ids if i not in verified_ids]
                if len(unver) == 1:
                    bad_ids.add(unver[0])
                # 双方都未核实或都已核实 → 不自动隔离，留给人裁
                continue
            bad_ids.update(ids)

        undecided = [f for f in errs
                     if f.kind in ("NUMCONFLICT", "DEFCONFLICT")
                     and len({i for i in f.ids if i.startswith("KB-")} - verified_ids) != 1]
        if undecided:
            print(f"\n{len(undecided)} 组冲突双方核实状态相同，机器无从裁定，未自动隔离：")
            for f in undecided[:6]:
                print(f"    {'、'.join(f.ids)}　{f.detail[:56]}")
            print("  请人工回查原文后裁定，再手动处理。")
        if not bad_ids:
            print("没有需要隔离的切片。")
            return
        p = Path(config.KB_PATH)
        lines = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()
                 if l.strip()]
        keep = [c for c in lines if c["id"] not in bad_ids]
        moved = [c for c in lines if c["id"] in bad_ids]
        qp = out / "quarantine.jsonl"
        with open(qp, "a", encoding="utf-8") as fh:
            for c in moved:
                c["quarantine_reason"] = [f.detail for f in errs if c["id"] in f.ids]
                fh.write(json.dumps(c, ensure_ascii=False) + "\n")
        p.write_text("\n".join(json.dumps(c, ensure_ascii=False) for c in keep) + "\n",
                     encoding="utf-8")
        print(f"\n已隔离 {len(moved)} 条到 {qp}，知识库剩 {len(keep)} 条。")
        print("**内容没有删除**，人工处理后可以改好再摄入回来。")
    else:
        print("未改动知识库。加 --quarantine 可把错误条目移进隔离区。")


if __name__ == "__main__":
    main()
