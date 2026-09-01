"""核实 Agent。

这组测试守的是一条方法论底线，比任何功能都重要：

**模型的判断本身永远不能成为核实依据。核实依据只能是外部证据。**

知识库本来就是大模型写出来的。再让大模型来判它对不对，等于让同一个来源
自己给自己背书。结果是把"未核实"洗成"已核实"，标签变了、可信度没变，
错误从此戴上一顶合规的帽子，比裸奔更难发现。

下面每一条测试都是在防这件事。
"""

import unittest

import config
from agents.factcheck import (MACHINE_WRITABLE, ST_DISPUTED, ST_MACHINE,
                              ST_REFUTED, ST_VERIFIED, Evidence,
                              FactCheckAgent, NullSearch, SearchBackend,
                              domain_of)
from core.llm import MockLLM
from core.retrieval import Retriever


class _Src(SearchBackend):
    def __init__(self, docs):
        self.docs = docs

    def search(self, query, top_k=5):
        return self.docs


class _Judge(MockLLM):
    """按资料文本里的标记词返回判定，便于构造场景。"""

    def _task_factcheck_judge(self, ctx):
        import json
        t = (ctx.get("doc") or {}).get("text", "")
        if "REFUTE" in t:
            return json.dumps({"verdict": "refute", "reason": "矛盾", "quote": "x"})
        if "SUPPORT" in t:
            return json.dumps({"verdict": "support", "reason": "一致", "quote": "y"})
        return json.dumps({"verdict": "unknown", "reason": "未涉及"})


class TestNeverSelfCertifies(unittest.TestCase):
    """核心：没有外部证据就不能标为已核实。"""

    @classmethod
    def setUpClass(cls):
        cls.R = Retriever.from_jsonl(config.KB_PATH)
        cls.chunk = cls.R.get("KB-022")

    def test_no_search_backend_never_marks_checked(self):
        r = FactCheckAgent(MockLLM(), NullSearch()).check(self.chunk)
        self.assertEqual(r.status, ST_DISPUTED)
        self.assertIn("不回落到模型记忆", r.note)

    def test_empty_results_never_marks_checked(self):
        r = FactCheckAgent(MockLLM(), _Src([])).check(self.chunk)
        self.assertNotEqual(r.status, ST_MACHINE)

    def test_machine_can_never_write_verified(self):
        """机器可写状态集合里绝不能出现 verified。

        一旦机器能写这个值，整条溯源链就退化成"模型说它查过了"，
        与不核实无异。
        """
        self.assertNotIn(ST_VERIFIED, MACHINE_WRITABLE)

    def test_apply_results_rejects_verified_status(self):
        from agents.factcheck import CheckResult, apply_results
        bad = CheckResult(chunk_id="KB-001", status=ST_VERIFIED)
        with self.assertRaises(AssertionError):
            apply_results(config.KB_PATH, [bad])


class TestMultiSourceRule(unittest.TestCase):
    """单一来源不足以定案 —— 这不是理论顾虑，是真实踩过的坑。"""

    @classmethod
    def setUpClass(cls):
        cls.chunk = Retriever.from_jsonl(config.KB_PATH).get("KB-015")

    def _run(self, docs):
        return FactCheckAgent(_Judge(), _Src(docs)).check(self.chunk)

    def test_single_source_is_disputed(self):
        r = self._run([{"url": "https://a.com/1", "text": "SUPPORT"}])
        self.assertEqual(r.status, ST_DISPUTED)

    def test_two_independent_sources_pass(self):
        r = self._run([{"url": "https://a.com/1", "text": "SUPPORT"},
                       {"url": "https://b.org/2", "text": "SUPPORT"}])
        self.assertEqual(r.status, ST_MACHINE)
        self.assertEqual(len(r.support_domains), 2)

    def test_same_domain_pages_are_not_independent(self):
        """同一域名下的多个页面不构成多来源。"""
        r = self._run([{"url": "https://a.com/p1", "text": "SUPPORT"},
                       {"url": "https://a.com/p2", "text": "SUPPORT"},
                       {"url": "https://a.com/p3", "text": "SUPPORT"}])
        self.assertEqual(r.status, ST_DISPUTED)
        self.assertEqual(r.support_domains, ["a.com"])

    def test_www_prefix_is_same_domain(self):
        r = self._run([{"url": "https://a.com/p1", "text": "SUPPORT"},
                       {"url": "https://www.a.com/p2", "text": "SUPPORT"}])
        self.assertEqual(r.status, ST_DISPUTED)


class TestConflictWins(unittest.TestCase):
    """矛盾证据压倒多数支持。

    对应真实案例：某服务商网站把 SRVO-001 说成"伺服放大器过载"，
    与多份官方资料矛盾。系统必须顶出来，而不是按多数放行。
    """

    def test_one_refutation_overrides_three_supports(self):
        chunk = Retriever.from_jsonl(config.KB_PATH).get("KB-015")
        r = FactCheckAgent(_Judge(), _Src([
            {"url": "https://a.com/1", "text": "SUPPORT"},
            {"url": "https://b.org/2", "text": "SUPPORT"},
            {"url": "https://c.net/3", "text": "SUPPORT"},
            {"url": "https://bad.io/x", "text": "REFUTE"},
        ])).check(chunk)
        self.assertEqual(r.status, ST_REFUTED)
        self.assertEqual(r.refute_domains, ["bad.io"])
        self.assertEqual(len(r.support_domains), 3)

    def test_evidence_list_is_preserved_for_human_review(self):
        """证据必须完整留给人看，包括被推翻的那条。"""
        chunk = Retriever.from_jsonl(config.KB_PATH).get("KB-015")
        r = FactCheckAgent(_Judge(), _Src([
            {"url": "https://a.com/1", "text": "SUPPORT"},
            {"url": "https://bad.io/x", "text": "REFUTE"},
        ])).check(chunk)
        self.assertEqual(len(r.evidence), 2)
        self.assertTrue(any(e.verdict == "refute" for e in r.evidence))


class TestMisc(unittest.TestCase):

    def test_domain_extraction(self):
        self.assertEqual(domain_of("https://www.Example.COM/a/b"), "example.com")
        self.assertEqual(domain_of("http://a.b.cn/x"), "a.b.cn")
        self.assertEqual(domain_of("not a url"), "")

    def test_quote_is_truncated(self):
        """证据摘录要短，只留定位用的片段，不整段搬运他人内容。"""
        e = Evidence(url="https://a.com", domain="a.com", verdict="support",
                     quote="很长的引文" * 40)
        self.assertLessEqual(len(e.quote), 61)

    def test_queries_fall_back_without_model(self):
        """模型不可用时仍要能生成检索词，否则整个核实流程停摆。"""
        chunk = Retriever.from_jsonl(config.KB_PATH).get("KB-015")

        class Dead(MockLLM):
            def run(self, *a, **k):
                raise RuntimeError("模型不可用")

        qs = FactCheckAgent(Dead(), NullSearch()).make_queries(chunk)
        self.assertTrue(qs and qs[0].strip())
