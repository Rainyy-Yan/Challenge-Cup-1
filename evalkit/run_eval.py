"""批量评测。

    python3 -m evalkit.run_eval --n 50 --out evalkit/report

算榜题评分标准里的三项指标，并把每一条判定明细落盘，答辩被问到就翻明细。

三项指标的口径（规则写在 config.py，改动早于本次评测的提交时间）：

  幻觉率
      对最终输出的每一条断言做独立复核：在全库范围内找是否存在支撑它的切片。
      注意这里是全库检索，和审核 Agent 只看引用切片的判据不同，属于独立的第二次核验。
      结论必须配人工抽样标注一起报，见 README 的说明，自动数只是筛子。

  适配准确率
      资源难度落在 [学习者该知识点水平, 水平+2] 视为适配。
      系统内部已按同一规则封顶，所以这个数会很高。它证明的是实现与规则一致，
      规则本身合不合理要靠人工评分那一栏，别把两件事混着说。

  覆盖率
      学习者的盲区知识点中，有多少个真的产出了带有效引用的资源。
      知识库缺切片的知识点会在这里掉下来，这一项是有真信号的。
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

import config
from agents.generate import is_adapted, learner_level
from core.retrieval import overlap_ratio
from orchestrator import Orchestrator, load_profile

BASE_PROFILES = ["P-A", "P-B", "P-C"]


def make_cases(n: int, seed: int = 20260905) -> list[dict]:
    """由 3 个基础画像扰动出 n 组用例。

    扰动只翻转前测作答，不改背景描述，这样每组用例仍然是一个自洽的学习者。
    随机种子固定，用例集可复现，提交材料里附种子即可。
    """
    rng = random.Random(seed)
    bases = [load_profile(p) for p in BASE_PROFILES]
    cases = [dict(p, case_id=p["id"]) for p in bases]
    i = 0
    while len(cases) < n:
        i += 1
        base = bases[i % len(bases)]
        prof = json.loads(json.dumps(base))
        keys = list(prof["responses"].keys())
        for k in rng.sample(keys, rng.randint(2, 6)):
            prof["responses"][k] = rng.randint(0, 3)
        prof["background"] = dict(prof["background"])
        prof["background"]["hands_on_hours"] = max(
            0, prof["background"]["hands_on_hours"] + rng.choice([-20, 0, 20, 60]))
        prof["id"] = f"{base['id']}-{i:03d}"
        prof["case_id"] = prof["id"]
        cases.append(prof)
    return cases[:n]


def verify_independently(claim, retriever) -> tuple[bool, str, float]:
    """全库复核一条断言。与审核 Agent 的判据独立。"""
    best, best_score = None, 0.0
    for chunk in retriever.chunks:
        r = overlap_ratio(claim.text, chunk.text)
        if r > best_score:
            best, best_score = chunk, r
    ok = best is not None and best_score >= config.EVIDENCE_MIN
    return ok, (best.id if best else ""), round(best_score, 3)


def evaluate(n: int, audit: bool = True, debate: bool | None = None) -> dict:
    """audit=False 是消融对照组：关掉审核 Agent，其余不变。

    这一组是技术创新那部分最有力的证据。只报"我们幻觉率 0"没人信，
    报"关掉审核是 X%，开着是 0%"才是能站住的因果论证。答辩必备。
    """
    orch_probe = Orchestrator()
    retriever = orch_probe.retriever

    rows, claim_rows = [], []
    for case in make_cases(n):
        orch = Orchestrator(llm=orch_probe.llm, retriever=retriever)
        if not audit:
            orch.auditor.review = lambda claims: (list(claims), [])
        diag = orch.diagnoser.run(case)
        session = orch.run(case, max_kp=len(diag.gaps) or 1)

        # 幻觉：逐条独立复核
        hallucinated = 0
        total_claims = 0
        for res in session.resources:
            for c in res.claims:
                total_claims += 1
                ok, src, score = verify_independently(c, retriever)
                if not ok:
                    hallucinated += 1
                claim_rows.append({
                    "case": case["case_id"], "kp": res.kp, "kind": res.kind,
                    "claim": c.text, "cited": c.source_id,
                    "best_match": src, "score": score, "supported": ok,
                    "consensus": c.consensus or "n/a",
                    "proposed_by": c.proposed_by,
                    "kb_verified": retriever.is_verified(c.source_id) if c.source_id else False,
                })

        # 适配：资源难度是否落在窗口内
        adapted = 0
        checked = 0
        for res in session.resources:
            # 只统计首轮资源。降维补救故意低于窗口、进阶挑战故意高于窗口，
            # 把它们算进去会得到一个假性偏低的适配率，那是在惩罚正确的行为。
            if res.variant != "primary":
                continue
            m = session.diagnosis.by_kp(res.kp)
            lvl = learner_level(m)
            kp_level = orch.kp_index[res.kp]["level"]
            checked += 1
            if is_adapted(res.difficulty, lvl, kp_level,
                          strong=(m is not None and m.status == "strong")):
                adapted += 1

        # 覆盖：盲区知识点是否产出了带有效引用的资源
        gaps = set(session.diagnosis.gaps)
        covered = {r.kp for r in session.resources if r.claims and r.sources()}
        hit = len(gaps & covered)

        dbt = {"agreed_n": 0, "arbitrated_n": 0, "dropped_n": 0, "single_n": 0}
        for d in session.debates:
            for k in dbt:
                dbt[k] += d["stats"].get(k, 0)

        rows.append({
            "case": case["case_id"], "debate": dbt,
            "gaps": len(gaps), "covered": hit,
            "claims": total_claims, "hallucinated": hallucinated,
            "resources": checked, "adapted": adapted,
            "intercepted": session.metrics["claims_dropped"],
        })

    tot_claims = sum(r["claims"] for r in rows)
    tot_hall = sum(r["hallucinated"] for r in rows)
    tot_res = sum(r["resources"] for r in rows)
    tot_adapt = sum(r["adapted"] for r in rows)
    tot_gap = sum(r["gaps"] for r in rows)
    tot_cov = sum(r["covered"] for r in rows)

    # 按共识等级分层统计幻觉率。这是辩论机制有没有信息增益的直接证据：
    # 如果双方印证的断言和单方断言错误率一样，说明第二位专家白跑了。
    strata: dict[str, dict] = {}
    for r in claim_rows:
        k = r["consensus"]
        st = strata.setdefault(k, {"n": 0, "bad": 0})
        st["n"] += 1
        if not r["supported"]:
            st["bad"] += 1
    consensus_table = {
        k: {"claims": v["n"], "unsupported": v["bad"],
            "rate": round(v["bad"] / v["n"], 4) if v["n"] else 0.0}
        for k, v in sorted(strata.items())
    }

    debate_stats = {"agreed": 0, "arbitrated": 0, "dropped": 0, "single": 0}
    for r in rows:
        for k in debate_stats:
            debate_stats[k] += r.get("debate", {}).get(k + "_n", 0)

    # 按知识库溯源可信度分层。这一层比其余所有指标都重要：
    # 落在未核实切片上的断言，即使"零幻觉"也只说明它忠实复述了一段
    # 我们自己都没查证过的内容。
    ver = [r for r in claim_rows if r["kb_verified"]]
    unver = [r for r in claim_rows if not r["kb_verified"]]
    grounding = {
        "verified_claims": len(ver),
        "unverified_claims": len(unver),
        "verified_share": round(len(ver) / len(claim_rows), 4) if claim_rows else 0.0,
        "kb_verified_ratio": round(retriever.verified_ratio(), 4),
        "hallucination_on_verified": (
            round(sum(1 for r in ver if not r["supported"]) / len(ver), 4) if ver else None),
        "hallucination_on_unverified": (
            round(sum(1 for r in unver if not r["supported"]) / len(unver), 4) if unver else None),
    }

    summary = {
        "cases": len(rows),
        "grounding": grounding,
        "by_consensus": consensus_table,
        "debate_totals": debate_stats,
        "hallucination_rate": round(tot_hall / tot_claims, 4) if tot_claims else 0.0,
        "adapt_accuracy": round(tot_adapt / tot_res, 4) if tot_res else 0.0,
        "coverage": round(tot_cov / tot_gap, 4) if tot_gap else 0.0,
        "claims_total": tot_claims,
        "claims_intercepted": sum(r["intercepted"] for r in rows),
        "targets": {
            "hallucination_rate": f"<{config.TARGET_HALLUCINATION}",
            "adapt_accuracy": f">={config.TARGET_ADAPT}",
            "coverage": f">={config.TARGET_COVERAGE}",
        },
    }
    summary["pass"] = {
        "hallucination_rate": summary["hallucination_rate"] < config.TARGET_HALLUCINATION,
        "adapt_accuracy": summary["adapt_accuracy"] >= config.TARGET_ADAPT,
        "coverage": summary["coverage"] >= config.TARGET_COVERAGE,
    }
    return {"summary": summary, "cases": rows, "claims": claim_rows}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--out", default="evalkit/report")
    ap.add_argument("--no-audit", action="store_true",
                    help="消融对照：关闭审核 Agent")
    ap.add_argument("--no-debate", action="store_true",
                    help="消融对照：关闭交叉验证与辩论，退化为单专家生成")
    args = ap.parse_args()

    if args.no_debate:
        config.DEBATE_ENABLED = False
    result = evaluate(args.n, audit=not args.no_audit)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(
        json.dumps(result["summary"], ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "cases.json").write_text(
        json.dumps(result["cases"], ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "claims.json").write_text(
        json.dumps(result["claims"], ensure_ascii=False, indent=2), encoding="utf-8")

    s = result["summary"]
    mode = []
    if args.no_audit:
        mode.append("审核关闭")
    if args.no_debate:
        mode.append("辩论关闭")
    print("模式：" + ("完整流程" if not mode else "消融对照（" + "、".join(mode) + "）"))
    print(f"用例数 {s['cases']}    断言总数 {s['claims_total']}    "
          f"审核拦截 {s['claims_intercepted']} 条")
    for key, label in (("hallucination_rate", "幻觉率"),
                       ("adapt_accuracy", "适配规则一致性"),
                       ("coverage", "核心知识点覆盖率")):
        mark = "达标" if s["pass"][key] else "未达标"
        print(f"{label:<24} {s[key]:.2%}    目标 {s['targets'][key]}    {mark}")

    g = s["grounding"]
    print()
    print("── 知识库溯源可信度（比上面三项都重要）──")
    print(f"  知识库已核实切片占比      {g['kb_verified_ratio']:.0%}")
    print(f"  落在已核实切片上的断言    {g['verified_claims']} 条"
          f"（占 {g['verified_share']:.0%}）")
    print(f"  落在未核实切片上的断言    {g['unverified_claims']} 条")
    if g["kb_verified_ratio"] < 0.8:
        print()
        print("  警告：知识库中大部分切片尚未核实来源。")
        print("  上面的幻觉率只能说明「生成内容与知识库一致」，")
        print("  **不能说明内容正确** —— 知识库本身错了，审核闸只会把错误认证为正确。")
        print("  在完成知识库核实之前，这些数字属于系统自检，不是效果证明，")
        print("  不得作为答辩举证使用。")

    if s["adapt_accuracy"] >= 0.999:
        print()
        print("  注意：适配一致性达到 100%，这是结构性结果，不是效果指标。")
        print("  系统按规则计算资源难度，本项又用同一条规则去校验，因此必然一致；")
        print("  它证明的是实现与预登记规则相符，证明不了规则本身合理。")
        print("  对外只能表述为「规则一致性」，绝不能说成「适配准确率达到 100%」。")
        print("  真正的适配效果必须由人工评分给出，做法见 README 第五节。")
    if s["by_consensus"] and not args.no_debate:
        print(f"\n{'共识等级':<14}{'断言数':>8}{'无依据':>8}{'占比':>9}")
        print("-" * 40)
        label = {"both": "双方印证", "single": "单方提出",
                 "arbitrated": "仲裁采纳", "n/a": "未经辩论"}
        for k, v in s["by_consensus"].items():
            print(f"{label.get(k, k):<14}{v['claims']:>8}{v['unsupported']:>8}"
                  f"{v['rate']:>8.2%}")
        d = s["debate_totals"]
        print(f"\n辩论过程：印证 {d['agreed']} 条，仲裁 {d['arbitrated']} 条，"
              f"存疑弃用 {d['dropped']} 条，单方 {d['single']} 条")
    print(f"\n明细已写入 {out}/")


if __name__ == "__main__":
    main()
