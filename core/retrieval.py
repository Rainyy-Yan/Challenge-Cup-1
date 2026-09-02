"""知识库检索。

刻意不用 jieba / faiss / sentence-transformers，纯标准库实现 BM25，
中文用字符二元组切分。理由：
  1. 骨架要能在任何机器上 `python3 -m` 直接跑起来，评委现场部署不出岔子；
  2. 二元组 BM25 在几百到几千切片的规模上召回率够用，瓶颈不在这里；
  3. 后期要换向量检索，只需替换 Retriever.search，接口不变。

真要升级，建议做 BM25 + 向量的混合召回，再加一层重排，别直接扔掉 BM25，
术语和型号这类词向量检索反而容易漏。
"""

import dataclasses
import json
import math
import re
from pathlib import Path

from .schema import Chunk

_ASCII = re.compile(r"[a-zA-Z0-9]+")
_CJK = re.compile(r"[\u4e00-\u9fff]")


def tokenize(text: str) -> list[str]:
    """中文切二元组，英文数字按词切，保留 SRVO-001 这类型号的完整形态。"""
    text = text.lower()
    tokens = [m.group(0) for m in _ASCII.finditer(text)]
    cjk = [ch for ch in text if _CJK.match(ch)]
    for i in range(len(cjk) - 1):
        tokens.append(cjk[i] + cjk[i + 1])
    tokens.extend(cjk)
    return tokens


class Retriever:
    K1 = 1.5
    B = 0.75

    def __init__(self, chunks: list[Chunk]):
        self.chunks = chunks
        self._docs: list[list[str]] = []
        self._tf: list[dict[str, int]] = []
        self._df: dict[str, int] = {}
        self._char_df: dict[str, int] = {}
        for c in chunks:
            toks = tokenize(f"{c.title} {c.text}")
            self._docs.append(toks)
            tf: dict[str, int] = {}
            for t in toks:
                tf[t] = tf.get(t, 0) + 1
            self._tf.append(tf)
            for t in tf:
                self._df[t] = self._df.get(t, 0) + 1
            for ch in {x for x in f"{c.title} {c.text}" if _CJK.match(x)}:
                self._char_df[ch] = self._char_df.get(ch, 0) + 1
        self._avg_len = sum(len(d) for d in self._docs) / max(1, len(self._docs))
        self._n = len(chunks)

    @classmethod
    def from_jsonl(cls, path: str | Path, *, demo_only: bool = False) -> "Retriever":
        chunks = []
        known = {f.name for f in dataclasses.fields(Chunk)}
        with open(path, encoding="utf-8") as fh:
            for line_number, line in enumerate(fh, start=1):
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                if not isinstance(rec, dict):
                    raise ValueError(
                        f"知识库第 {line_number} 行不是对象，无法读取切片"
                    )
                # 先按原始记录过滤，避免已排除的失效来源因缺字段或结构错误
                # 影响正式 Demo 的加载。
                if demo_only and not rec.get("demo_eligible", True):
                    continue
                # 只取 Chunk 认识的字段。
                #
                # 端到端跑出来的教训：摄入工具给切片加了 origin 字段，
                # Chunk 不认，于是**入库之后整个系统起不来** ——
                # 检索、诊断、生成全部无法初始化。
                # 单元测试没覆盖到，因为它们从不真正入库再读回。
                #
                # 知识库是多个工具共同写入的，字段会随工具演进而增加。
                # 读取端必须对未知字段容错，否则任何一个工具加字段
                # 都会把整个系统打死。
                try:
                    chunk = Chunk(**{k: v for k, v in rec.items() if k in known})
                except TypeError as exc:
                    chunk_id = rec.get("id", "<missing id>")
                    raise ValueError(
                        f"知识库切片 {chunk_id!r}（第 {line_number} 行）字段无效"
                    ) from exc
                chunks.append(chunk)
        return cls(chunks)

    def _idf(self, term: str) -> float:
        df = self._df.get(term, 0)
        return math.log(1 + (self._n - df + 0.5) / (df + 0.5))

    def search(self, query: str, top_k: int = 4, kp: str | None = None) -> list[tuple[Chunk, float]]:
        """kp 不为空时只在该知识点范围内检索，相当于一层硬过滤。

        知识点过滤是这套方案压幻觉的第一道闸：生成某个知识点的资源时，
        只让模型看到该知识点的切片，跨知识点的串味在检索阶段就被挡住。
        """
        q = tokenize(query)
        scored: list[tuple[Chunk, float]] = []
        for i, chunk in enumerate(self.chunks):
            if kp and chunk.kp != kp:
                continue
            tf = self._tf[i]
            dl = len(self._docs[i])
            score = 0.0
            for term in q:
                f = tf.get(term, 0)
                if not f:
                    continue
                denom = f + self.K1 * (1 - self.B + self.B * dl / self._avg_len)
                score += self._idf(term) * f * (self.K1 + 1) / denom
            if score > 0:
                scored.append((chunk, score))
        scored.sort(key=lambda x: -x[1])
        return scored[:top_k]

    def distinctive_terms(self, max_df: int = 3, char_ratio: float = 0.35) -> set[str]:
        """只在少数切片里出现的中文二元组，且两个字本身都不是高频虚字。

        用途见 AuditAgent：断言里出现了知识库的特征术语，却不在它所引的切片里，
        基本可以断定是张冠李戴。「超程」「润滑」这种词就是典型。

        char_ratio 这一层是红队测试逼出来的。原来只看二元组本身的 df，
        结果「点和」这种跨短语边界拼出来的组合也被当成术语，把
        「依次示教原点、X轴正方向上一点和XY平面内一点」这条真断言误伤了。
        现在要求两个字的单字 df 都低于全库的 char_ratio，「和」「的」「在」
        这类到处都是的字自然被排除，不用手写停用词表。
        """
        if not hasattr(self, "_distinct"):
            cap = max(1, int(self._n * char_ratio))
            self._distinct = {
                t for t, df in self._df.items()
                if df <= max_df and len(t) == 2
                and _CJK.match(t[0]) and _CJK.match(t[1])
                and self._char_df.get(t[0], 0) <= cap
                and self._char_df.get(t[1], 0) <= cap
            }
        return self._distinct

    def distinctive_in(self, text: str) -> set[str]:
        return set(tokenize(text)) & self.distinctive_terms()

    def by_kp(self, kp: str) -> list[Chunk]:
        return [c for c in self.chunks if c.kp == kp]

    def verified_ratio(self) -> float:
        """已核实切片占比。低于 0.8 时任何幻觉率数字都只能算自检，不算效果。"""
        if not self.chunks:
            return 0.0
        return sum(1 for c in self.chunks if c.verified) / len(self.chunks)

    def is_verified(self, chunk_id: str) -> bool:
        c = self.byId.get(chunk_id) if hasattr(self, "byId") else self.get(chunk_id)
        return bool(c and c.verified)

    def get(self, chunk_id: str) -> Chunk | None:
        for c in self.chunks:
            if c.id == chunk_id:
                return c
        return None


def jaccard_like(a: str, b: str) -> float:
    """两段文本的二元组 Jaccard。用于判断两个干扰项是不是在说同一件事。

    和 overlap_ratio 的区别：overlap_ratio 是有方向的（断言被切片覆盖了多少），
    Jaccard 是对称的。比较两个平等的选项时必须用对称度量，
    否则短的那个总是"被覆盖"，会把长短不一的两个不同选项误判成重复。
    """
    ta, tb = set(tokenize(a)), set(tokenize(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def overlap_ratio(claim: str, chunk_text: str) -> float:
    """断言与切片的二元组覆盖率，审核 Agent 的离线兜底判据。

    注意这只是兜底：接了真模型以后主判据是模型的蕴含判断，
    但这条规则始终并行运行，用来接住模型自己判错的情况。
    """
    a = set(tokenize(claim))
    b = set(tokenize(chunk_text))
    if not a:
        return 0.0
    return len(a & b) / len(a)


_NUM = re.compile(r"\d+(?:\.\d+)?")
_CN_NUM = re.compile(r"[零〇一二两三四五六七八九十百千万亿]{1,12}")
_CN_DIGIT = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNIT = {"十": 10, "百": 100, "千": 1000, "万": 10000, "亿": 100000000}


def cn_to_int(s: str) -> int | None:
    """中文数字转阿拉伯数字。支持到亿级，够工业文本用。

    这个函数是红队测试逼出来的。原来的数值核验只认阿拉伯数字，
    「减速机更换周期为运行两万小时或五年」这条把「一万小时或三年」改成
    「两万小时或五年」，全篇没有一个阿拉伯数字，直接从数值检查底下溜过去了。
    中文技术文档里汉字数字极其常见，这个漏洞不补，H3 类幻觉能漏掉一半。
    """
    if not s:
        return None
    total, section, digit = 0, 0, None
    for ch in s:
        if ch in _CN_DIGIT:
            digit = _CN_DIGIT[ch]
        elif ch in _CN_UNIT:
            unit = _CN_UNIT[ch]
            if unit >= 10000:
                section = (section + (digit or 0)) * unit
                total += section
                section, digit = 0, None
            else:
                section += (digit if digit is not None else 1) * unit
                digit = None
        else:
            return None
    return total + section + (digit or 0)


def numbers_in(text: str) -> set[str]:
    """抽出文中所有数值，中文数字归一化为阿拉伯数字后一并返回。

    归一化是必须的：切片写「一万小时」、断言写「10000小时」应当视为同一个数，
    反过来断言写「两万」而切片是「一万」必须能判出冲突。
    """
    out = {m.group(0) for m in _NUM.finditer(text)}
    for m in _CN_NUM.finditer(text):
        v = cn_to_int(m.group(0))
        if v is not None and v > 0:
            out.add(str(v))
    return out
