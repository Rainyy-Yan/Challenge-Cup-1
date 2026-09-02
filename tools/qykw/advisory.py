"""Deterministic qykw command replies and strict read-only advice parsing."""

from __future__ import annotations

from collections.abc import Mapping

from tools.qykw.domain import AdvisoryResult, CommandName, ContextPlan, RepositoryFile, RunContext, RunRecord, RunStage, RunStatus
from tools.qykw.prompts import build_analysis_request, build_plan_request
from tools.qykw.provider import InferenceProvider, validate_provider_capabilities

_MAX_TITLE = 160
_MAX_BODY = 6_000
_MAX_ITEMS = 20
_MAX_ITEM = 1_000
_UNAVAILABLE = AdvisoryResult("qykw", "请求状态不可用。", (), ("缺少有效的运行记录。",))
_INFERENCE_FAILURE = AdvisoryResult("qykw", "暂时无法生成只读建议。", (), ("本次结构化结果不可用。",))


class AdvisoryService:
    """Routes deterministic commands locally and advisory commands to strict schemas."""

    def __init__(self, provider: InferenceProvider) -> None:
        self.provider = provider

    def handle(
        self,
        run: RunContext,
        plan: ContextPlan | None,
        record: RunRecord | None = None,
        *,
        trusted_rules: tuple[RepositoryFile, ...] = (),
    ) -> AdvisoryResult:
        if run.command.name is CommandName.HELP:
            return self.help(run)
        if run.command.name is CommandName.STATUS:
            return self.status(run, record) if _valid_record(run, record) else _UNAVAILABLE
        if run.command.name is CommandName.SUMMARY:
            return self.summary(run, record) if _valid_record(run, record) else _UNAVAILABLE
        if run.command.name is CommandName.STOP:
            return self.stop(run, record) if _valid_record(run, record) else _UNAVAILABLE
        if plan is None or not _valid_plan(run, plan):
            return _UNAVAILABLE
        if run.command.name is CommandName.ANALYZE:
            return self.analyze(run, plan, trusted_rules)
        if run.command.name is CommandName.PLAN:
            return self.plan(run, plan, trusted_rules)
        return _UNAVAILABLE

    def help(self, run: RunContext) -> AdvisoryResult:
        del run
        return AdvisoryResult("qykw 帮助", "可使用：分析、计划、审查、复审、状态、总结、停止。", (), ("帮助内容为确定性说明。",))

    def analyze(self, run: RunContext, plan: ContextPlan, trusted_rules: tuple[RepositoryFile, ...] = ()) -> AdvisoryResult:
        return self._complete(build_analysis_request(run, plan, trusted_rules))

    def plan(self, run: RunContext, plan: ContextPlan, trusted_rules: tuple[RepositoryFile, ...] = ()) -> AdvisoryResult:
        return self._complete(build_plan_request(run, plan, trusted_rules))

    def status(self, run: RunContext, record: RunRecord) -> AdvisoryResult:
        del run
        return AdvisoryResult("qykw 状态", f"运行状态：{record.status.value}；阶段：{record.stage.value}。", (), _record_limitations(record))

    def summary(self, run: RunContext, record: RunRecord) -> AdvisoryResult:
        del run
        coverage = record.coverage
        evidence = () if coverage is None else (f"覆盖：{coverage.reviewed_files}/{coverage.total_files} 个文件，{coverage.reviewed_hunks}/{coverage.total_hunks} 个变更块。",)
        return AdvisoryResult("qykw 总结", f"运行状态：{record.status.value}。", evidence, _record_limitations(record))

    def stop(self, run: RunContext, record: RunRecord) -> AdvisoryResult:
        del run, record
        return AdvisoryResult("qykw 停止", "已登记软停止请求；当前阶段将在安全检查点结束。", (), ("正在传输的请求不能被瞬时中断。",))

    def _complete(self, request: object) -> AdvisoryResult:
        try:
            validate_provider_capabilities(self.provider, request)  # type: ignore[arg-type]
            response = self.provider.complete(request)  # type: ignore[arg-type]
            parsed = parse_advisory_response(response.value)
            return parsed if parsed is not None else _INFERENCE_FAILURE
        except Exception:
            return _INFERENCE_FAILURE


def parse_advisory_response(value: object) -> AdvisoryResult | None:
    """Accept only the exact public advisory response shape; never coerce values."""
    if not isinstance(value, Mapping) or set(value) != {"advisory"}:
        return None
    advisory = value.get("advisory")
    if not isinstance(advisory, Mapping) or set(advisory) != {"title", "body", "evidence", "limitations"}:
        return None
    title = _bounded_string(advisory.get("title"), _MAX_TITLE)
    body = _bounded_string(advisory.get("body"), _MAX_BODY)
    evidence = _string_list(advisory.get("evidence"))
    limitations = _string_list(advisory.get("limitations"))
    if title is None or body is None or evidence is None or limitations is None:
        return None
    return AdvisoryResult(title, body, evidence, limitations)


def _bounded_string(value: object, maximum: int) -> str | None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        return None
    return value


def _string_list(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, list) or len(value) > _MAX_ITEMS:
        return None
    parsed = tuple(_bounded_string(item, _MAX_ITEM) for item in value)
    return parsed if all(item is not None for item in parsed) else None


def _valid_plan(run: RunContext, plan: ContextPlan) -> bool:
    return (plan.run_id == run.run_id and plan.repository == run.repository and plan.pr_number == run.pr_number
            and plan.source_head_sha == run.source_head_sha)


def _valid_record(run: RunContext, record: RunRecord | None) -> bool:
    if not isinstance(record, RunRecord) or record.context != run:
        return False
    return isinstance(record.stage, RunStage) and isinstance(record.status, RunStatus)


def _record_limitations(record: RunRecord) -> tuple[str, ...]:
    values = list(record.warning_codes)
    if record.error_code is not None:
        values.append("本次运行存在已记录错误。")
    return tuple(values)
