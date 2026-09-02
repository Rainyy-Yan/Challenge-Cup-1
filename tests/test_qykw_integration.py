"""No-network acceptance tests for the complete phase-one qykw review core."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
import json
from pathlib import Path
from threading import Event, Thread
import tempfile
import unittest
from unittest.mock import patch

from tools.qykw.advisory import AdvisoryService
from tools.qykw.commands import CommandRouter
from tools.qykw.config import parse_qykw_config
from tools.qykw.context import build_context_plan
from tools.qykw.domain import (
    AuthenticatedUser, ChangedFile, ChangedLine, CommandMode, CommandName,
    CommandRequest, CommandRoute, CommentKind, ContextChunk, ContextPlan,
    CoverageReport, DiffSide, EventContext, FileManifest, InferenceRequest,
    InferenceResponse, InferenceUsage, InlineComment, IssueComment, PullRef,
    PullSnapshot, ReactionResult, RepositoryPermission, ReviewComment,
    RunContext, RunRecord, RunStage, RunStatus,
)
from tools.qykw.github import HttpGitHubGateway
from tools.qykw.publish import ReviewPublisher
from tools.qykw.review import ReviewEngine
from tools.qykw.runner import QykwRunner
from tools.qykw.state import GitHubCommentStateStore
from tools.qykw.triggers import build_run_context, normalize_event


REPOSITORY_ID = 7
REPOSITORY = "owner/repo"
SOURCE_REPOSITORY = "source/repo"
PR_NUMBER = 53
HEAD = "a" * 40
MOVED_HEAD = "c" * 40
BASE = "b" * 40
NOW = "2026-09-02T00:00:00Z"


def qykw_config() -> object:
    return parse_qykw_config({"version": 1, "authorization": {"code_writers": ["owner"]}})


def changed_file() -> ChangedFile:
    return ChangedFile(
        "src/a.py", None, "modified", BASE, HEAD, "100644", "100644",
        "old\n", "new\n", "@@ -1 +1 @@\n-old\n+new\n",
        False, False, 1, 1,
    )


def pull_snapshot() -> PullSnapshot:
    return PullSnapshot(
        PR_NUMBER, "open", False, SOURCE_REPOSITORY, HEAD, REPOSITORY, BASE,
        "main", "Title", "Body", (changed_file(),), (), (), (),
    )


def pull_event(action: str, *, draft: bool = False) -> EventContext:
    event = normalize_event(
        "pull_request",
        {
            "action": action,
            "number": PR_NUMBER,
            "repository": {"id": REPOSITORY_ID, "full_name": REPOSITORY},
            "pull_request": {
                "number": PR_NUMBER,
                "draft": draft,
                "head": {"sha": HEAD},
            },
            "sender": {"login": "alice"},
        },
        repository_id=REPOSITORY_ID,
        repository=REPOSITORY,
    )
    assert event is not None
    return event


def comment_event(command: CommandName, *, comment_id: int, actor: str = "alice") -> EventContext:
    body = f"@qykw {command.value} 验证权限边界\n并检查明确失败路径"
    event = normalize_event(
        "issue_comment",
        {
            "action": "created",
            "repository": {"id": REPOSITORY_ID, "full_name": REPOSITORY},
            "issue": {"number": PR_NUMBER, "pull_request": {}},
            "comment": {"id": comment_id, "body": body},
            "sender": {"login": actor},
        },
        repository_id=REPOSITORY_ID,
        repository=REPOSITORY,
    )
    assert event is not None
    return event


def fixed_single_chunk_plan(snapshot: PullSnapshot, run: RunContext) -> ContextPlan:
    """Supply one literal acceptance input while keeping all qykw services real."""

    prefix = (
        f"P run={run.run_id} rid={run.repository_id} repo={run.repository} pr={run.pr_number} "
        f"bs={run.target_base_sha} br={run.target_base_ref} hs={run.source_head_sha} "
    )
    chunk = ContextChunk(
        f"{run.run_id}|chunk=1",
        ("src/a.py",),
        prefix + "path=src/a.py prev=- side=RIGHT old=- new=1\n+new\n",
        64,
    )
    return ContextPlan(
        snapshot.target_repository,
        snapshot.number,
        snapshot.source_head_sha,
        run.run_id,
        FileManifest(("src/a.py",), ("src/a.py",)),
        (chunk,),
        CoverageReport(1, 1, 1, 1, (), True),
        frozenset({ChangedLine("src/a.py", 1, DiffSide.RIGHT)}),
        1_000,
        20_000,
    )


def candidate_value() -> dict[str, object]:
    return {
        "path": "src/a.py",
        "line": 1,
        "side": "RIGHT",
        "severity": "P2",
        "failure_path": "empty input reaches the changed branch",
        "impact": "the local request returns the wrong value",
        "evidence": "the added line replaces the guarded value",
        "suggestion": "restore the guard before returning",
        "verification": "run the empty input regression test",
    }


class BoundaryProvider:
    """Deterministic provider boundary; qykw builds and validates every request."""

    def __init__(self, *, block_review: bool = False, fail_capabilities: bool = False) -> None:
        self.requests: list[InferenceRequest] = []
        self.block_review = block_review
        self.fail_capabilities = fail_capabilities
        self.started = Event()
        self.release = Event()

    @property
    def stages(self) -> list[str]:
        return [request.schema_name.rsplit("-", 1)[-1] for request in self.requests]

    @property
    def run_count(self) -> int:
        return len({request.run_id for request in self.requests})

    def capabilities(self):
        if self.fail_capabilities:
            raise RuntimeError("provider unavailable")
        from tools.qykw.domain import ProviderCapabilities
        return ProviderCapabilities(100_000, 20_000, True, frozenset({"maximum"}))

    def complete(self, request: InferenceRequest) -> InferenceResponse:
        self.requests.append(request)
        kind = request.schema_name.rsplit("-", 1)[-1]
        if kind == "review" and self.block_review:
            self.started.set()
            if not self.release.wait(2):
                raise RuntimeError("bounded provider wait expired")
        if kind == "review":
            context = request.payload["untrusted"]["context"]  # type: ignore[index]
            value = {"candidates": [dict(candidate_value(), source_chunk_id=context["chunk_id"])]}
        elif kind == "validation":
            value = {
                "conclusion": "建议修改后合并",
                "findings": [candidate_value()],
                "validation_notes": ["已检查反例"],
                "limitations": [],
            }
        elif kind in {"analysis", "plan"}:
            value = {
                "advisory": {
                    "title": "只读结果",
                    "body": "已完成有边界的只读处理。",
                    "evidence": ["固定 Head"],
                    "limitations": ["未执行 PR 代码"],
                }
            }
        else:
            raise AssertionError(f"unexpected provider stage: {kind}")
        return InferenceResponse("request-1", value, InferenceUsage(10, 5))


class BoundaryGateway:
    """In-memory network boundary for real state, runner, review and publisher components."""

    repository = REPOSITORY

    def __init__(self, *, heads: tuple[str, ...] = (HEAD,), reaction_fails: bool = False) -> None:
        self.heads = heads
        self.ref_calls = 0
        self.reaction_fails = reaction_fails
        self.issue_comments: list[IssueComment] = []
        self.review_comments: list[ReviewComment] = []
        self.reactions: list[int] = []
        self.write_order: list[str] = []
        self._next_comment = 1_000
        self._next_review = 2_000

    def get_pull_ref(self, pr_number: int) -> PullRef:
        index = min(self.ref_calls, len(self.heads) - 1)
        self.ref_calls += 1
        return PullRef(pr_number, "open", False, SOURCE_REPOSITORY, self.heads[index], REPOSITORY, BASE, "main")

    def get_pull_snapshot(self, pr_number: int, *, run: RunContext) -> PullSnapshot:
        self.assertEqualBinding(pr_number, run)
        return pull_snapshot()

    def assertEqualBinding(self, pr_number: int, run: RunContext) -> None:
        if pr_number != PR_NUMBER or run.pr_number != PR_NUMBER or run.source_head_sha != HEAD:
            raise RuntimeError("snapshot binding mismatch")

    def get_head_sha(self, pr_number: int) -> str:
        return self.heads[-1]

    def get_actor_permission(self, login: str) -> RepositoryPermission:
        return RepositoryPermission.WRITE if login else RepositoryPermission.NONE

    def get_authenticated_user(self) -> AuthenticatedUser:
        return AuthenticatedUser("qykw", 1)

    def assert_bot_identity(self, expected_login: str = "qykw") -> AuthenticatedUser:
        if expected_login != "qykw":
            raise RuntimeError("identity mismatch")
        return self.get_authenticated_user()

    def try_add_reaction(self, trigger: object, content: str = "laugh") -> ReactionResult:
        if self.reaction_fails:
            raise RuntimeError("reaction unavailable")
        node_id = getattr(trigger, "node_id")
        self.reactions.append(node_id)
        self.write_order.append("reaction")
        return ReactionResult(None)

    def list_issue_comments(self, pr_number: int) -> tuple[IssueComment, ...]:
        return tuple(self.issue_comments)

    def list_review_comments(self, pr_number: int) -> tuple[ReviewComment, ...]:
        return tuple(self.review_comments)

    def list_changed_files(self, pr_number: int) -> tuple[ChangedFile, ...]:
        return (changed_file(),)

    def list_check_runs(self, head_sha: str) -> tuple[object, ...]:
        return ()

    def get_file_for_run(self, run: RunContext, path: str, side: DiffSide):
        return None

    def get_default_branch_rules(self) -> tuple[object, ...]:
        return ()

    def create_issue_comment(self, pr_number: int, body: str) -> int:
        comment_id = self._next_comment
        self._next_comment += 1
        self.issue_comments.append(IssueComment(comment_id, "qykw", body, NOW))
        if "qykw-cancel:v1" in body:
            self.write_order.append("cancel")
        elif "qykw-state:v1" in body:
            self.write_order.append("state")
        else:
            self.write_order.append("comment")
        return comment_id

    def update_issue_comment(self, comment_id: int, body: str) -> None:
        for index, comment in enumerate(self.issue_comments):
            if comment.comment_id == comment_id:
                self.issue_comments[index] = replace(comment, body=body)
                self.write_order.append("summary" if body.startswith("## qykw") else "state")
                return
        raise RuntimeError("comment not found")

    def create_review(self, pr_number: int, *, head_sha: str, body: str,
                      comments: tuple[InlineComment, ...]) -> int:
        if head_sha != HEAD:
            raise RuntimeError("review head mismatch")
        review_id = self._next_review
        self._next_review += 1
        for item in comments:
            self.review_comments.append(ReviewComment(
                review_id + len(self.review_comments), "qykw", item.body, NOW,
                item.path, item.line, item.side,
            ))
        self.write_order.append("inline")
        return review_id


class ReviewSystem:
    def __init__(self, *, heads: tuple[str, ...] = (HEAD,), reaction_fails: bool = False,
                 block_review: bool = False, fail_capabilities: bool = False) -> None:
        self.gateway = BoundaryGateway(heads=heads, reaction_fails=reaction_fails)
        self.provider = BoundaryProvider(block_review=block_review, fail_capabilities=fail_capabilities)
        self.state = GitHubCommentStateStore(self.gateway, now=lambda: NOW, repository=REPOSITORY)
        self.publisher = ReviewPublisher(self.gateway, self.state, max_findings=20)
        self.runner = QykwRunner(
            qykw_config(), self.gateway, self.state, AdvisoryService(self.provider),
            ReviewEngine(self.provider, max_findings=20), self.publisher,
            context_builder=fixed_single_chunk_plan, now=lambda: NOW,
        )


class TestQykwIntegration(unittest.TestCase):
    def test_initial_review_lifecycle_uses_real_services_and_fixed_stage_order(self) -> None:
        system = ReviewSystem()
        outcome = system.runner.handle(pull_event("opened"))
        self.assertEqual(outcome.status, RunStatus.COMPLETED)
        self.assertEqual(system.provider.stages, ["review", "validation"])
        public_order = [item for item in system.gateway.write_order if item in {"summary", "inline"}]
        self.assertEqual(public_order, ["summary", "inline"])
        self.assertEqual(len(system.gateway.review_comments), 1)

    def test_replayed_event_has_no_second_inference_or_publication(self) -> None:
        system = ReviewSystem()
        event = pull_event("opened")
        first = system.runner.handle(event)
        writes = tuple(system.gateway.write_order)
        second = system.runner.handle(event)
        self.assertEqual((first.status, second.status), (RunStatus.COMPLETED, RunStatus.COMPLETED))
        self.assertEqual(system.provider.run_count, 1)
        self.assertEqual(system.provider.stages, ["review", "validation"])
        self.assertEqual(tuple(system.gateway.write_order), writes)

    def test_initial_draft_ready_and_later_push_matrix_is_bounded(self) -> None:
        system = ReviewSystem()
        draft = system.runner.handle(pull_event("opened", draft=True))
        ready = system.runner.handle(pull_event("ready_for_review"))
        push = system.runner.handle(pull_event("synchronize"))
        self.assertEqual(draft.error_code, "draft_opened")
        self.assertEqual(ready.status, RunStatus.COMPLETED)
        self.assertEqual(push.error_code, "automatic_event_ignored")
        self.assertEqual(system.provider.stages, ["review", "validation"])

    def test_every_command_uses_the_frozen_route_provider_count_and_side_effects(self) -> None:
        cases = (
            (CommandName.HELP, CommandRoute.DETERMINISTIC, 0, False),
            (CommandName.ANALYZE, CommandRoute.ADVISORY, 1, False),
            (CommandName.PLAN, CommandRoute.ADVISORY, 1, False),
            (CommandName.REVIEW, CommandRoute.REVIEW, 2, True),
            (CommandName.REREVIEW, CommandRoute.REVIEW, 2, True),
            (CommandName.STATUS, CommandRoute.DETERMINISTIC, 0, False),
            (CommandName.SUMMARY, CommandRoute.DETERMINISTIC, 0, False),
            (CommandName.FIX, CommandRoute.CHANGE, 0, False),
            (CommandName.IMPLEMENT, CommandRoute.CHANGE, 0, False),
            (CommandName.STOP, CommandRoute.DETERMINISTIC, 0, False),
        )
        for index, (command, route, provider_calls, inline) in enumerate(cases, start=1):
            with self.subTest(command=command.value):
                system = ReviewSystem()
                event = comment_event(command, comment_id=100 + index,
                                      actor="owner" if command in {CommandName.FIX, CommandName.IMPLEMENT} else "alice")
                self.assertEqual(CommandRouter().resolve(event.command), route)
                if command is CommandName.STOP:
                    review = comment_event(CommandName.REVIEW, comment_id=900)
                    run = build_run_context(review, system.gateway.get_pull_ref(PR_NUMBER))
                    assert run is not None
                    active = RunRecord(run, RunStage.ANALYZING, RunStatus.ACTIVE, "qykw-v1", None,
                                       False, None, (), None, NOW, NOW)
                    self.assertTrue(system.state.create(active))
                outcome = system.runner.handle(event)
                self.assertEqual(len(system.provider.requests), provider_calls)
                self.assertEqual(bool(system.gateway.review_comments), inline)
                if command in {CommandName.FIX, CommandName.IMPLEMENT}:
                    self.assertEqual(outcome.error_code, "change_lane_noop")
                    self.assertEqual(system.gateway.write_order, [])
                elif command is CommandName.STOP:
                    self.assertIsNone(outcome.error_code)
                    self.assertIn("cancel", system.gateway.write_order)
                    self.assertEqual(system.gateway.reactions, [event.trigger_comment_id])
                else:
                    self.assertEqual(outcome.status, RunStatus.COMPLETED)
                    self.assertEqual(system.gateway.reactions, [event.trigger_comment_id])

    def test_multiline_comment_and_reaction_failure_continue_safely(self) -> None:
        system = ReviewSystem(reaction_fails=True)
        outcome = system.runner.handle(comment_event(CommandName.ANALYZE, comment_id=301))
        record = system.state.find_latest(PR_NUMBER)
        self.assertEqual(outcome.status, RunStatus.COMPLETED)
        self.assertEqual(system.provider.stages, ["analysis"])
        self.assertIsNotNone(record)
        self.assertIn("reaction_failed", record.warning_codes if record else ())

    def test_head_drift_prevents_summary_and_inline_publication(self) -> None:
        system = ReviewSystem(heads=(HEAD, MOVED_HEAD))
        outcome = system.runner.handle(pull_event("opened"))
        self.assertEqual(outcome.status, RunStatus.STALE)
        self.assertNotIn("summary", system.gateway.write_order)
        self.assertNotIn("inline", system.gateway.write_order)


class TestPhaseArtifactIntegration(unittest.TestCase):
    """Exercise the real credential-separated phase and artifact boundaries."""

    def active_phase_system(self, command: CommandName = CommandName.REVIEW):
        system = ReviewSystem()
        event = comment_event(command, comment_id=6_000 + list(CommandName).index(command))
        run = build_run_context(event, system.gateway.get_pull_ref(PR_NUMBER))
        assert run is not None
        record = RunRecord(run, RunStage.ACCEPTED, RunStatus.ACTIVE, "qykw-v1", None,
                           False, None, (), None, NOW, NOW)
        self.assertTrue(system.state.create(record))
        return system, run

    def test_phase_analyze_and_publish_review_round_trip(self) -> None:
        from tools.qykw.phases import ProductionPhaseController, _review_from_payload, _run_payload

        system, run = self.active_phase_system()
        authorize = {"version": 1, "phase": "authorize", "run": _run_payload(run),
                     "payload": {"authorization": "accepted"}}
        analyze = ProductionPhaseController("analyze", {})
        analyze._read_services = lambda: (system.gateway, system.state, qykw_config())  # type: ignore[method-assign]
        with patch("tools.qykw.phases.ResponsesInferenceProvider.from_env", return_value=system.provider):
            analysis = analyze.analyze(authorize)
        self.assertEqual(analysis["payload"]["kind"], "review")  # type: ignore[index]
        self.assertIsNotNone(_review_from_payload(analysis["payload"]["review"]))  # type: ignore[index]

        publish = ProductionPhaseController("publish", {})
        publish._review_services = lambda: (system.gateway, system.state, qykw_config())  # type: ignore[method-assign]
        result = publish.publish(analysis)
        self.assertEqual(result["payload"], {"published": True, "status": "completed"})
        self.assertIn("summary", system.gateway.write_order)
        self.assertIn("inline", system.gateway.write_order)

    def test_phase_analyze_and_publish_every_advisory_handler(self) -> None:
        from tools.qykw.phases import ProductionPhaseController, _run_payload

        for command in (CommandName.HELP, CommandName.STATUS, CommandName.SUMMARY,
                        CommandName.ANALYZE, CommandName.PLAN):
            with self.subTest(command=command.value):
                system, run = self.active_phase_system(command)
                authorize = {"version": 1, "phase": "authorize", "run": _run_payload(run),
                             "payload": {"authorization": "accepted"}}
                analyze = ProductionPhaseController("analyze", {})
                analyze._read_services = lambda: (system.gateway, system.state, qykw_config())  # type: ignore[method-assign]
                with patch("tools.qykw.phases.ResponsesInferenceProvider.from_env", return_value=system.provider):
                    analysis = analyze.analyze(authorize)
                self.assertEqual(analysis["payload"]["kind"], "advisory")  # type: ignore[index]
                publish = ProductionPhaseController("publish", {})
                publish._review_services = lambda: (system.gateway, system.state, qykw_config())  # type: ignore[method-assign]
                self.assertEqual(publish.publish(analysis)["payload"]["published"], True)  # type: ignore[index]

    def test_phase_terminal_and_precondition_failures_are_typed(self) -> None:
        from tools.qykw.phases import ProductionPhaseController, _run_payload

        controller = ProductionPhaseController("analyze", {})
        self.assertEqual(controller.analyze({"run": None})["payload"]["reason"], "upstream_skipped")  # type: ignore[index]
        system, run = self.active_phase_system()
        artifact = {"version": 1, "phase": "authorize", "run": _run_payload(run),
                    "payload": {"authorization": "accepted"}}

        missing = ProductionPhaseController("analyze", {})
        other_system = ReviewSystem()
        missing._read_services = lambda: (other_system.gateway, other_system.state, qykw_config())  # type: ignore[method-assign]
        self.assertEqual(missing.analyze(artifact)["payload"]["reason"], "state_unavailable")  # type: ignore[index]

        system.state.request_cancel(PR_NUMBER, run.run_id, stop_comment_id=9_001, actor_login="alice")
        canceled = ProductionPhaseController("analyze", {})
        canceled._read_services = lambda: (system.gateway, system.state, qykw_config())  # type: ignore[method-assign]
        self.assertEqual(canceled.analyze(artifact)["payload"], {"kind": "none", "status": "canceled"})

        for payload, expected in (({"kind": "none", "status": "stale"}, "stale"),
                                  ({"kind": "none", "status": "canceled"}, "canceled")):
            with self.subTest(payload=payload):
                publish = ProductionPhaseController("publish", {})
                result = publish.publish({"version": 1, "phase": "analyze", "run": _run_payload(run), "payload": payload})
                self.assertEqual(result["payload"]["status"], expected)  # type: ignore[index]

    def test_phase_record_failure_and_controller_construction(self) -> None:
        from tools.qykw.phases import ProductionPhaseController, build_production_controller, _run_payload

        system, run = self.active_phase_system()
        artifact = {"version": 1, "phase": "analyze", "run": _run_payload(run),
                    "payload": {"kind": "none", "status": "stale"}}
        controller = ProductionPhaseController("record-failure", {})
        controller._review_services = lambda: (system.gateway, system.state, qykw_config())  # type: ignore[method-assign]
        result = controller.record_failure(artifact, "provider_failed")
        self.assertEqual(result["payload"], {"error_code": "provider_failed"})
        self.assertEqual(system.state.get(PR_NUMBER, run.run_id).status, RunStatus.FAILED)  # type: ignore[union-attr]
        self.assertEqual(ProductionPhaseController("record-failure", {}).record_failure({"run": None}, "x")["payload"]["reason"], "upstream_skipped")  # type: ignore[index]
        self.assertEqual(build_production_controller("authorize").phase, "authorize")
        with self.assertRaisesRegex(ValueError, "invalid_phase"):
            build_production_controller("untrusted")

    def test_phase_payload_parsers_fail_closed_for_malformed_values(self) -> None:
        from tools.qykw import phases

        system, run = self.active_phase_system()
        run_payload = phases._run_payload(run)
        self.assertIsNone(phases._run_from_artifact({"run": None}))
        self.assertIsNone(phases._run_from_artifact({"run": []}))
        self.assertIsNone(phases._run_from_artifact({"run": {}}))
        self.assertEqual(phases._run_from_artifact({"run": run_payload}), run)
        self.assertTrue(phases._matches_run(system.gateway.get_pull_ref(PR_NUMBER), run))
        self.assertFalse(phases._matches_run(replace(system.gateway.get_pull_ref(PR_NUMBER), state="closed"), run))

        advisory = {"title": "title", "body": "body", "evidence": ["e"], "limitations": ["l"]}
        self.assertEqual(phases._advisory_from_payload(advisory), advisory)
        for value in (None, {}, {**advisory, "extra": 1}, {**advisory, "title": 1}, {**advisory, "evidence": [1]}):
            self.assertIsNone(phases._advisory_from_payload(value))
        rendered = phases._render_advisory(advisory)
        self.assertIn("证据", rendered)
        self.assertIn("限制", rendered)
        self.assertEqual(phases._bounded("abcdef", 3), "abc")
        self.assertEqual(phases._bounded(None, 3), "信息不可用")

        finding = candidate_value() | {"fingerprint": "fingerprint"}
        finding["severity"] = "P2"
        self.assertIsNotNone(phases._finding_from_payload(finding))
        for value in (None, {}, {**finding, "extra": 1}, {**finding, "line": 0},
                      {**finding, "path": ""}, {**finding, "side": "MIDDLE"}):
            self.assertIsNone(phases._finding_from_payload(value))

        review = {
            "conclusion": "done", "findings": [finding],
            "coverage": {"total_files": 1, "reviewed_files": 1, "total_hunks": 1,
                         "reviewed_hunks": 1, "omissions": [], "explains_every_file": True},
            "validation_notes": ["checked"], "limitations": [],
        }
        parsed = phases._review_from_payload(review)
        self.assertIsNotNone(parsed)
        if parsed is not None:
            self.assertEqual(phases._review_payload(parsed)["conclusion"], "done")
        for value in (None, {}, {**review, "extra": 1}, {**review, "coverage": {}},
                      {**review, "conclusion": 1}, {**review, "findings": [{}]},
                      {**review, "validation_notes": [1]}):
            self.assertIsNone(phases._review_from_payload(value))

    def test_phase_environment_event_and_service_guards(self) -> None:
        from tools.qykw.phases import ProductionPhaseController

        controller = ProductionPhaseController("authorize", {})
        self.assertEqual(controller._event(), (None, "event_unavailable"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "event.json"
            for content, reason in (("not-json", "event_unavailable"), ("[]", "invalid_event")):
                path.write_text(content, encoding="utf-8")
                environment = {"GITHUB_EVENT_PATH": str(path), "GITHUB_REPOSITORY_ID": "7",
                               "GITHUB_REPOSITORY": REPOSITORY, "GITHUB_EVENT_NAME": "issue_comment",
                               "GITHUB_RUN_ID": "1"}
                self.assertEqual(ProductionPhaseController("authorize", environment)._event()[1], reason)
        with self.assertRaisesRegex(ValueError, "review_credentials_unavailable"):
            ProductionPhaseController("authorize", {})._review_services()
        with self.assertRaisesRegex(ValueError, "read_credentials_unavailable"):
            ProductionPhaseController("analyze", {})._read_services()

    def test_artifact_boundary_accepts_all_valid_shapes_and_rejects_mutations(self) -> None:
        from tools.qykw import __main__ as entry
        from tools.qykw.phases import _run_payload

        system, run = self.active_phase_system()
        binding = _run_payload(run)
        valid = (
            entry._artifact("request", binding, {"command": "审查"}),
            entry._artifact("control", binding, {"stop_comment_id": 7}),
            entry._artifact("authorize", binding, {"authorization": "accepted"}),
            entry._artifact("analyze", binding, {"analysis": {"result_ref": "sha256:value"}}),
            entry._artifact("analyze", binding, {"kind": "none", "status": "stale"}),
            entry._artifact("publish", binding, {"published": True}),
            entry._artifact("publish", binding, {"published": False, "status": "failed"}),
            entry._artifact("record-failure", binding, {"error_code": "provider_failed"}),
        )
        for artifact in valid:
            entry._validate_artifact(artifact)
        skipped = {"version": 1, "phase": "authorize", "run": None,
                   "payload": {"status": "skipped", "reason": "no_event"}}
        entry._validate_artifact(skipped)

        invalid = (
            None, {}, {**valid[0], "extra": 1}, {**valid[0], "version": 2},
            {**valid[0], "phase": "unknown"}, {**valid[0], "run": None},
            {**valid[0], "payload": {}},
        )
        for artifact in invalid:
            with self.assertRaises(ValueError):
                entry._validate_artifact(artifact)

        bad_runs = (
            [], {}, {**binding, "extra": 1}, {**binding, "repository_id": True},
            {**binding, "run_id": ""}, {**binding, "command": None},
            {**binding, "command": {"name": "审查", "argument": "", "mode": "read_only", "extra": 1}},
        )
        for bad in bad_runs:
            with self.assertRaisesRegex(ValueError, "invalid_run_binding"):
                entry._validate_run(bad)

    def test_artifact_structured_analysis_validation_covers_numeric_and_text_edges(self) -> None:
        from tools.qykw import __main__ as entry

        finding = candidate_value() | {"fingerprint": "fingerprint"}
        review = {
            "kind": "review", "status": "completed",
            "review": {
                "conclusion": "done", "findings": [finding],
                "coverage": {"total_files": 1, "reviewed_files": 1, "total_hunks": 1,
                             "reviewed_hunks": 1, "omissions": [], "explains_every_file": True},
                "validation_notes": [], "limitations": [],
            },
        }
        advisory = {"kind": "advisory", "status": "completed",
                    "advisory": {"title": "t", "body": "b", "evidence": [], "limitations": []}}
        self.assertTrue(entry._valid_structured_analysis(review))
        self.assertTrue(entry._valid_structured_analysis(advisory))
        self.assertTrue(entry._valid_structured_analysis({"kind": "none", "status": "canceled"}))
        self.assertTrue(entry._valid_finding(finding))
        self.assertTrue(entry._valid_text_list([]))
        self.assertTrue(entry._text("", 1, allow_empty=True))
        self.assertTrue(entry._error_code("provider_failed"))

        mutations = (
            {}, {**review, "status": "failed"},
            {**review, "review": None},
            {**review, "review": {**review["review"], "findings": [None]}},  # type: ignore[index]
            {**review, "review": {**review["review"], "coverage": {}}},  # type: ignore[index]
            {**review, "review": {**review["review"], "coverage": {**review["review"]["coverage"], "reviewed_files": 2}}},  # type: ignore[index]
            {**review, "review": {**review["review"], "validation_notes": [1]}},  # type: ignore[index]
        )
        for value in mutations:
            self.assertFalse(entry._valid_structured_analysis(value))
        for value in (None, {}, {**finding, "line": True}, {**finding, "line": 0},
                      {**finding, "side": "MIDDLE"}, {**finding, "path": ""}):
            self.assertFalse(entry._valid_finding(value))
        self.assertFalse(entry._valid_text_list([1]))
        self.assertFalse(entry._error_code("not-an-identifier"))

    def test_artifact_file_io_and_phase_dispatch_are_bounded(self) -> None:
        from tools.qykw import __main__ as entry
        from tools.qykw.phases import _run_payload

        _, run = self.active_phase_system()
        binding = _run_payload(run)
        request = entry._artifact("request", binding, {"command": "审查"})

        class Controller:
            def authorize(self, artifact: dict[str, object]) -> dict[str, object]:
                return entry._artifact("authorize", artifact["run"], {"authorization": "accepted"})

            def record_failure(self, artifact: dict[str, object], error: str) -> dict[str, object]:
                return entry._artifact("record-failure", artifact["run"], {"error_code": error})

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "request.json"
            target = root / "result.json"
            entry._write_artifact(source, request)
            self.assertEqual(entry._read_artifact(source), request)
            self.assertEqual(entry.main(["--phase", "authorize", "--artifact", str(source),
                                         "--output", str(target)], controller=Controller()), 0)
            self.assertTrue(target.is_file())

            bad = root / "bad.json"
            bad.write_bytes(b"\xff")
            with self.assertRaisesRegex(ValueError, "invalid_artifact_json"):
                entry._read_artifact(bad)
            missing = root / "missing.json"
            with self.assertRaisesRegex(ValueError, "unsafe_artifact_path"):
                entry._read_artifact(missing)
            with patch.object(entry, "_MAX_ARTIFACT_BYTES", 1):
                with self.assertRaisesRegex(ValueError, "artifact_too_large"):
                    entry._read_artifact(source)

        authorize = entry._run_phase("authorize", request, Controller(), None)
        self.assertEqual(authorize["phase"], "authorize")
        self.assertEqual(entry._run_phase("record-failure", authorize, None, "provider_failed")["phase"], "record-failure")
        self.assertEqual(entry._run_phase("record-failure", authorize, Controller(), "provider_failed")["phase"], "record-failure")
        for phase, artifact, controller, code in (
            ("analyze", request, Controller(), None),
            ("authorize", request, None, None),
            ("record-failure", authorize, None, "bad-code"),
        ):
            with self.subTest(phase=phase):
                with self.assertRaises(ValueError):
                    entry._run_phase(phase, artifact, controller, code)
        self.assertEqual(entry._safe_code(ValueError("safe_code")), "phase_failed")
        self.assertEqual(entry._safe_code(ValueError("unsafe code!")), "phase_failed")
        from tools.qykw.provider import ProviderError, ProviderErrorCode
        for code in ProviderErrorCode:
            with self.subTest(provider_error=code.value):
                self.assertEqual(
                    entry._safe_code(ProviderError(code)),
                    f"inference_{code.value}",
                )
        from tools.qykw.config import ConfigError
        from tools.qykw.context import ContextError
        from tools.qykw.domain import InferenceError, InferenceErrorCode, InferenceFailure
        from tools.qykw.github import GitHubError
        from tools.qykw.prompts import PromptError
        for error, code in (
            (InferenceError(InferenceFailure(InferenceErrorCode.CAPABILITY_UNSUPPORTED, False, False)), "inference_capability_unsupported"),
            (ConfigError("sensitive config detail"), "analysis_config_failed"),
            (ContextError("sensitive context detail"), "analysis_context_failed"),
            (GitHubError("sensitive github detail"), "analysis_github_failed"),
            (PromptError("sensitive prompt detail"), "analysis_prompt_failed"),
        ):
            with self.subTest(phase_error=type(error).__name__):
                self.assertEqual(entry._safe_code(error), code)

    def test_provider_capability_failure_records_a_failed_terminal_run(self) -> None:
        system = ReviewSystem(fail_capabilities=True)
        outcome = system.runner.handle(pull_event("opened"))
        record = system.state.find_latest(PR_NUMBER)
        self.assertEqual((outcome.status, outcome.error_code), (RunStatus.FAILED, "review_failed"))
        self.assertEqual(record.status if record else None, RunStatus.FAILED)
        self.assertEqual(system.provider.requests, [])
        self.assertNotIn("summary", system.gateway.write_order)

    def test_real_context_planner_covers_the_network_snapshot_without_silent_omission(self) -> None:
        run = build_run_context(pull_event("opened"), BoundaryGateway().get_pull_ref(PR_NUMBER))
        assert run is not None
        plan = build_context_plan(
            pull_snapshot(), run_id=run.run_id, repository_id=run.repository_id,
            repository_limit=100_000, backend_context_window=100_000,
            output_reserve=20_000, safety_reserve_ratio=0.20, max_chunk_ratio=0.25,
        )
        self.assertEqual(plan.manifest.paths, ("src/a.py",))
        self.assertEqual(plan.coverage.reviewed_files, 1)
        self.assertEqual(plan.coverage.reviewed_hunks, 1)
        self.assertTrue(plan.coverage.explains_every_file)

    def test_comment_pagination_reaches_the_second_network_page(self) -> None:
        api = "https://api.github.test/repos/owner/repo"
        first = [{"id": value, "user": {"login": "user"}, "body": "early", "updated_at": NOW}
                 for value in range(1, 101)]
        second = [{"id": 101, "user": {"login": "qykw"}, "body": "late", "updated_at": NOW}]
        routes = {
            f"GET {api}/issues/{PR_NUMBER}/comments?per_page=100": (
                200,
                {"content-type": "application/json", "link": f'<{api}/issues/{PR_NUMBER}/comments?per_page=100&page=2>; rel="next"'},
                first,
            ),
            f"GET {api}/issues/{PR_NUMBER}/comments?per_page=100&page=2": (
                200, {"content-type": "application/json"}, second,
            ),
        }
        calls: list[str] = []

        def transport(method: str, url: str, headers: Mapping[str, str], body: bytes | None):
            del headers, body
            calls.append(url)
            status, response_headers, payload = routes[f"{method} {url}"]
            return status, response_headers, json.dumps(payload).encode("utf-8")

        gateway = HttpGitHubGateway(
            api_url="https://api.github.test", repository=REPOSITORY,
            read_token="read-boundary", review_token=None, transport=transport,
        )
        comments = gateway.list_issue_comments(PR_NUMBER)
        self.assertEqual((len(comments), comments[-1].comment_id), (101, 101))
        self.assertEqual(len(calls), 2)

    def test_independent_stop_marker_survives_a_late_state_save(self) -> None:
        system = ReviewSystem(block_review=True)
        result: list[object] = []
        worker = Thread(target=lambda: result.append(system.runner.handle(pull_event("opened"))), daemon=True)
        worker.start()
        self.assertTrue(system.provider.started.wait(2), "review provider did not reach its bounded checkpoint")
        stale_record = system.state.find_latest_active(PR_NUMBER)
        self.assertIsNotNone(stale_record)

        stop = comment_event(CommandName.STOP, comment_id=777)
        stop_outcome = system.runner.handle(stop)
        self.assertEqual(stop_outcome.status, RunStatus.ACTIVE)
        assert stale_record is not None
        system.state.save(stale_record)
        self.assertTrue(system.state.is_cancel_requested(PR_NUMBER, stale_record.context.run_id))

        system.provider.release.set()
        worker.join(3)
        self.assertFalse(worker.is_alive(), "review worker exceeded the bounded test deadline")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].status, RunStatus.CANCELED)
        self.assertNotIn("summary", system.gateway.write_order)
        self.assertNotIn("inline", system.gateway.write_order)


class TestProductionBranchCoverage(unittest.TestCase):
    """Close defensive production branches with bounded, behavior-first probes."""

    def test_provider_defensive_parsers_and_transport_classification(self) -> None:
        import ipaddress
        import threading
        from tools.qykw import provider as subject

        valid_schema = {
            "type": "object", "additionalProperties": False,
            "required": ["name", "items", "count"],
            "properties": {
                "name": {"type": "string", "minLength": 1, "enum": ["ok"]},
                "items": {"type": "array", "items": {"type": "integer", "minimum": 0}},
                "count": {"type": "integer", "minimum": 0},
            },
        }
        subject._validate_schema_contract(valid_schema)
        subject._validate_schema({"name": "ok", "items": [0, 1], "count": 2}, valid_schema)
        invalid_contracts = (
            None, {"type": "string"},
            {"type": "object", "additionalProperties": True, "required": [], "properties": {}},
            {"type": "object", "additionalProperties": False, "required": ["x", "x"], "properties": {"x": {"type": "string"}}},
            {"type": "array", "items": "bad"},
            {"type": "array", "items": {"type": "string"}, "extra": True},
            {"type": "string", "minLength": True}, {"type": "string", "enum": []},
            {"type": "integer", "minimum": True}, {"type": "boolean"},
        )
        for schema in invalid_contracts:
            with self.subTest(contract=schema), self.assertRaises(ValueError):
                subject._validate_schema_contract(schema)  # type: ignore[arg-type]
        invalid_values = (
            ([], valid_schema), ({"name": "ok", "items": [], "count": 0, "extra": 1}, valid_schema),
            ({"name": "ok", "items": []}, valid_schema), ({"name": "bad", "items": [], "count": 0}, valid_schema),
            ({"name": "ok", "items": [True], "count": 0}, valid_schema), ({"name": "ok", "items": [], "count": -1}, valid_schema),
            ("x", {"type": "array", "items": {"type": "string"}}), ("x", {"type": "unknown"}),
        )
        for value, schema in invalid_values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                subject._validate_schema(value, schema)

        self.assertEqual(subject._bounded_int("2", 1, 3), 2)
        for value in (None, "", " 2", "四", "0", "4"):
            with self.subTest(integer=value), self.assertRaises(ValueError):
                subject._bounded_int(value, 1, 3)
        self.assertEqual(subject._canonical_host("api.example.com."), "api.example.com")
        for host in (None, "", " API.example.com", "API.example.com", "a..com", "é.example", "-.-"):
            with self.subTest(host=host), self.assertRaises((TypeError, ValueError, UnicodeError)):
                subject._canonical_host(host)  # type: ignore[arg-type]

        self.assertEqual(subject._validate_endpoint("https://api.example.com/v1", ("api.example.com",)).port, 443)
        endpoint_errors = (
            ("http://api.example.com/v1", subject.ProviderErrorCode.ENDPOINT_INVALID),
            ("https://127.0.0.1/v1", subject.ProviderErrorCode.ENDPOINT_BLOCKED),
            ("https://other.example.com/v1", subject.ProviderErrorCode.ENDPOINT_NOT_ALLOWED),
            ("https://api.example.com:444/v1", subject.ProviderErrorCode.ENDPOINT_INVALID),
        )
        for url, code in endpoint_errors:
            with self.subTest(url=url), self.assertRaises(subject.ProviderError) as caught:
                subject._validate_endpoint(url, ("api.example.com",))
            self.assertEqual(caught.exception.code, code)

        self.assertEqual(subject._retry_after({"Retry-After": "0.5"}), 0.5)
        for headers in ({}, {"retry-after": "bad"}, {"retry-after": "0"}, {"retry-after": "61"}):
            self.assertIsNone(subject._retry_after(headers))
        for kind, expected, retry in (
            (subject.TransportFailureKind.DNS, subject.ProviderErrorCode.DNS_ERROR, True),
            (subject.TransportFailureKind.TLS_HANDSHAKE, subject.ProviderErrorCode.TLS_ERROR, True),
            (subject.TransportFailureKind.CERTIFICATE, subject.ProviderErrorCode.TLS_ERROR, False),
            (subject.TransportFailureKind.READ_TIMEOUT, subject.ProviderErrorCode.READ_TIMEOUT, False),
            (subject.TransportFailureKind.RESPONSE_INTERRUPTED, subject.ProviderErrorCode.RESPONSE_INTERRUPTED, False),
            (subject.TransportFailureKind.CONNECTION, subject.ProviderErrorCode.CONNECTION_ERROR, True),
        ):
            self.assertEqual(subject._transport_failure(subject.TransportFailure(kind, pre_send=True)), (expected, retry))
        self.assertIsNone(subject._transport_failure(RuntimeError()))
        with self.assertRaises(subject.ProviderError):
            subject._transport_failure(subject.ProviderError(subject.ProviderErrorCode.INVALID_CONFIG))

        slots = threading.BoundedSemaphore(1)
        self.assertEqual(subject._resolve_public("api.example.com", 443, 1, lambda *_: ("8.8.8.8",), slots), "8.8.8.8")
        for resolver, exception in (
            (lambda *_: (), subject.TransportFailure), (lambda *_: ("not-an-ip",), subject.TransportFailure),
            (lambda *_: ("127.0.0.1",), subject.ProviderError),
            (lambda *_: (_ for _ in ()).throw(OSError()), subject.TransportFailure),
        ):
            with self.subTest(resolver=resolver), self.assertRaises(exception):
                subject._resolve_public("api.example.com", 443, 1, resolver, threading.BoundedSemaphore(1))
        locked = threading.BoundedSemaphore(1)
        self.assertTrue(locked.acquire(blocking=False))
        with self.assertRaises(subject.TransportFailure):
            subject._resolve_with_deadline("api.example.com", 443, 0.01, lambda *_: (), locked)
        self.assertFalse(subject._is_acceptable_global_address(ipaddress.ip_address("127.0.0.1")))
        self.assertTrue(subject._is_acceptable_global_address(ipaddress.ip_address("8.8.8.8")))

    def test_provider_response_parser_rejects_each_untrusted_shape(self) -> None:
        from tools.qykw import provider as subject

        schema = {"type": "object", "additionalProperties": False, "required": ["value"],
                  "properties": {"value": {"type": "integer", "minimum": 0}}}

        def response(document: object, content_type: str = "application/json"):
            return subject.TransportResponse(200, {"content-type": content_type}, json.dumps(document).encode())

        valid = {
            "id": "req-1",
            "object": "response",
            "created_at": 1_764_000_000,
            "model": "configured-model",
            "status": "completed",
            "output": [],
            "output_text": json.dumps({"value": 1}),
            "usage": {
                "input_tokens": 2,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": 3,
                "output_tokens_details": {"reasoning_tokens": 1},
                "total_tokens": 5,
            },
        }
        parsed = subject._parse_response(response(valid), schema, 10, 20)
        self.assertEqual((parsed.request_id, parsed.value), ("req-1", {"value": 1}))
        invalid_documents = (
            (valid, "text/plain"), ([], "application/json"),
            ({key: value for key, value in valid.items() if key != "status"}, "application/json"),
            ({**valid, "id": ""}, "application/json"), ({**valid, "id": "unsafe/id"}, "application/json"),
            ({**valid, "object": "chat.completion"}, "application/json"),
            ({**valid, "status": "incomplete"}, "application/json"),
            ({**valid, "output": {}}, "application/json"), ({**valid, "output_text": []}, "application/json"),
            ({**valid, "usage": []}, "application/json"),
            ({**valid, "usage": {**valid["usage"], "input_tokens": True, "output_tokens": 1}}, "application/json"),
            ({**valid, "usage": {**valid["usage"], "input_tokens": 21, "output_tokens": 1}}, "application/json"),
            ({**valid, "usage": {**valid["usage"], "input_tokens": 2, "output_tokens": 11}}, "application/json"),
            ({**valid, "usage": {**valid["usage"], "input_tokens": 18, "output_tokens": 3}}, "application/json"),
            ({**valid, "output_text": json.dumps({"value": -1})}, "application/json"),
        )
        for document, content_type in invalid_documents:
            with self.subTest(document=document), self.assertRaises((TypeError, ValueError)):
                subject._parse_response(response(document, content_type), schema, 10, 20)
        oversize = subject.TransportResponse(200, {"content-type": "application/json"}, b"x" * (subject._MAX_RESPONSE_BODY_BYTES + 1))
        with self.assertRaises(ValueError):
            subject._parse_response(oversize, schema, 10, 20)

    def test_state_codecs_fail_closed_for_every_mutable_boundary(self) -> None:
        import threading
        from tools.qykw import state as subject

        system = ReviewSystem()
        run = build_run_context(comment_event(CommandName.REVIEW, comment_id=8_001), system.gateway.get_pull_ref(PR_NUMBER))
        assert run is not None
        record = RunRecord(run, RunStage.ACCEPTED, RunStatus.ACTIVE, "qykw-v1", None,
                           False, CoverageReport(1, 1, 1, 1, (), True), (), None, NOW, NOW)
        payload = subject._record_payload(record)
        self.assertEqual(subject._record_from_payload(payload), record)
        self.assertEqual(subject._parse_json('{"ok":1}'), {"ok": 1})
        self.assertIsNone(subject._parse_json("[]"))
        self.assertIsNone(subject._parse_json("{"))
        with patch.object(subject, "_JSON_LIMIT", 1):
            self.assertIsNone(subject._parse_json('{"ok":1}'))

        invalid_payloads: list[dict[str, object]] = []
        for key, value in (
            ("version", 2), ("kind", "other"), ("summary_comment_id", 0),
            ("initial_review", 1), ("prompt_version", 1), ("error_code", 1),
            ("stage", "unknown"), ("status", "unknown"), ("warning_codes", "bad"),
        ):
            changed = dict(payload)
            changed[key] = value
            invalid_payloads.append(changed)
        missing = dict(payload)
        missing.pop("kind")
        invalid_payloads.append(missing)
        for changed in invalid_payloads:
            with self.subTest(payload=changed):
                self.assertIsNone(subject._record_from_payload(changed))

        context = dict(payload["context"])  # type: ignore[arg-type]
        invalid_contexts = []
        short = dict(context); short.pop("event_action"); invalid_contexts.append(short)
        bad_command = dict(context); bad_command["command"] = {"name": "review"}; invalid_contexts.append(bad_command)
        bad_trigger = dict(context); bad_trigger["trigger_comment_id"] = 0; invalid_contexts.append(bad_trigger)
        bad_kind = dict(context); bad_kind["trigger_comment_kind"] = 2; invalid_contexts.append(bad_kind)
        bad_repository_id = dict(context); bad_repository_id["repository_id"] = True; invalid_contexts.append(bad_repository_id)
        bad_string = dict(context); bad_string["repository"] = ""; invalid_contexts.append(bad_string)
        for changed in invalid_contexts:
            with self.subTest(context=changed), self.assertRaises(ValueError):
                subject._context_from_payload(changed)

        valid_coverage = payload["coverage"]
        self.assertIsNone(subject._coverage_from_payload(None))
        self.assertEqual(subject._coverage_from_payload(valid_coverage), record.coverage)
        for value in ([], {"total_files": 1}, {**valid_coverage, "explains_every_file": 1},
                      {**valid_coverage, "total_files": -1}, {**valid_coverage, "omissions": "bad"}):  # type: ignore[arg-type]
            with self.subTest(coverage=value), self.assertRaises(ValueError):
                subject._coverage_from_payload(value)

        for value, helper, arguments in (
            (None, subject._mapping, (None,)),
            (None, subject._string, (None,)),
            (None, subject._positive, (True,)),
            (None, subject._nonnegative, (-1,)),
            (None, subject._strings, ("bad",)),
        ):
            del value
            with self.assertRaises(ValueError):
                if helper is subject._strings:
                    helper(*arguments, maximum=2)
                else:
                    helper(*arguments)
        self.assertEqual(subject._string("", allow_empty=True), "")
        self.assertEqual(subject._strings(["a", "b"], maximum=2), ("a", "b"))
        for bad_id in (None, "", "has space", "x" * 513):
            with self.assertRaises(ValueError):
                subject._valid_id(bad_id, "id")

        comment = IssueComment(90, "qykw", "no marker", NOW)
        self.assertIsNone(subject._parse_state_comment(comment))
        self.assertIsNone(subject._parse_cancel_comment(comment))
        self.assertIsNone(subject._parse_state_comment(replace(comment, author_login="other")))
        self.assertIsNone(subject._parse_cancel_comment(replace(comment, body="x" * (subject._MARKER_LIMIT + 1))))
        malformed_state = replace(comment, body="<!-- qykw-state:v1 {bad} -->")
        malformed_cancel = replace(comment, body="<!-- qykw-cancel:v1 {\"version\":1} -->")
        self.assertIsNone(subject._parse_state_comment(malformed_state))
        self.assertIsNone(subject._parse_cancel_comment(malformed_cancel))

        valid_cancel = subject.render_cancel_marker(PR_NUMBER, run.run_id, 91, "alice", NOW)
        cancel = subject._parse_cancel_comment(replace(comment, body=valid_cancel))
        self.assertEqual(cancel.stop_comment_id if cancel else None, 91)
        wrong_cancel = valid_cancel.replace('"kind":"cancel"', '"kind":"other"')
        self.assertIsNone(subject._parse_cancel_comment(replace(comment, body=wrong_cancel)))
        for args in ((0, run.run_id, 91, "alice", NOW), (PR_NUMBER, run.run_id, 0, "alice", NOW),
                     (PR_NUMBER, run.run_id, 91, "alice", "bad")):
            with self.assertRaises(ValueError):
                subject._cancel_payload(*args)

        self.assertIsNone(subject._best([]))
        self.assertIsNone(subject._best_stored([]))
        with subject._create_critical_section((REPOSITORY, PR_NUMBER)):
            pass
        key = (REPOSITORY, PR_NUMBER + 1)
        subject._CREATE_LOCKS[key] = (threading.Lock(), 1)
        with subject._create_critical_section(key):
            pass
        self.assertEqual(subject._CREATE_LOCKS.pop(key)[1], 1)

    def test_github_gateway_validates_all_local_untrusted_boundaries(self) -> None:
        from tools.qykw import github as subject

        gateway = subject.HttpGitHubGateway(
            "https://api.github.test", REPOSITORY, "read", "review",
            transport=lambda *_: (200, {"content-type": "application/json"}, b"{}"),
        )
        self.assertNotIn("read", repr(gateway))
        self.assertEqual(gateway._repo_path("pulls/1"), "https://api.github.test/repos/owner/repo/pulls/1")
        self.assertEqual(gateway._validate_url("https://api.github.test/user"), "https://api.github.test/user")
        self.assertEqual(subject._safe_path("src/a.py"), "src/a.py")
        self.assertEqual(subject._diff_side("RIGHT"), DiffSide.RIGHT)
        self.assertEqual(subject._diff_side(DiffSide.LEFT), DiffSide.LEFT)
        self.assertIsNone(subject._optional_string(None, "bad"))
        self.assertEqual(subject._next_link('<https://api.github.test/a>; rel="next"'), "https://api.github.test/a")
        self.assertIsNone(subject._next_link('<https://api.github.test/a>; rel="last"'))

        invalid_calls = (
            (subject._validate_origin, ("http://api.github.test",)),
            (subject._validate_repository, ("bad",)),
            (subject._validate_ref, ("bad ref",)),
            (subject._validate_ref, ("bad..ref",)),
            (subject._validate_git_sha, ("abc",)),
            (subject._validate_login, ("bad_login",)),
            (subject._safe_path, ("../secret",)),
            (subject._safe_path, ("a\\b",)),
            (subject._mapping, ([], "bad")),
            (subject._list_payload, ({}, None)),
            (subject._list_payload, ({"items": []}, "missing")),
            (subject._string, (None, "bad")),
            (subject._bool, (1, "bad")),
            (subject._positive_int, (True, "bad")),
            (subject._nonnegative_int, (-1, "bad")),
            (subject._repository_name, ({}, "bad")),
            (subject._diff_side, ("MIDDLE",)),
            (subject._write_body, ("",)),
            (subject._next_link, (5,)),
            (subject._next_link, ("malformed",)),
            (subject._next_link, ('<https://a>; rel="next", <https://b>; rel="next"',)),
        )
        for function, arguments in invalid_calls:
            with self.subTest(function=function.__name__, arguments=arguments), self.assertRaises(subject.GitHubError):
                function(*arguments)

        for url in (
            "http://api.github.test/repos/owner/repo/pulls/1",
            "https://other.test/repos/owner/repo/pulls/1",
            "https://api.github.test/repos/other/repo/pulls/1",
            "https://api.github.test/repos/source/repo/git/refs/not-a-sha",
        ):
            with self.subTest(url=url), self.assertRaises(subject.GitHubError):
                gateway._validate_url(url)
        with self.assertRaises(subject.GitHubError):
            gateway._repo_path("/absolute")

        changed = {
            "filename": "src/a.py", "status": "modified", "sha": HEAD,
            "patch": "@@ -1 +1 @@\n-old\n+new", "additions": 1, "deletions": 1,
        }
        self.assertEqual(gateway._changed_file(changed).path, "src/a.py")
        with self.assertRaises(subject.GitHubError):
            gateway._changed_file({**changed, "status": "unknown"})

        ref = PullRef(PR_NUMBER, "open", False, SOURCE_REPOSITORY, HEAD, REPOSITORY, BASE, "main")
        run = build_run_context(pull_event("opened"), ref)
        assert run is not None
        gateway._assert_run_matches(run, ref)
        with self.assertRaises(subject.GitHubError):
            gateway._assert_run_matches(replace(run, source_head_sha=MOVED_HEAD), ref)

        pull_payload = {
            "number": PR_NUMBER, "state": "open", "merged": False, "draft": False,
            "head": {"sha": HEAD, "ref": "feature", "repo": {"full_name": SOURCE_REPOSITORY}},
            "base": {"sha": BASE, "ref": "main", "repo": {"full_name": REPOSITORY}},
        }
        self.assertEqual(gateway._parse_pull_ref(pull_payload, PR_NUMBER), ref)
        for changed_pull, code in (
            ({**pull_payload, "number": 99}, "pull_number_mismatch"),
            ({**pull_payload, "state": "closed"}, "pull_not_open"),
            ({**pull_payload, "base": {**pull_payload["base"], "repo": {"full_name": "other/repo"}}}, "pull_repository_mismatch"),  # type: ignore[index]
        ):
            with self.subTest(code=code), self.assertRaises(subject.GitHubError) as caught:
                gateway._parse_pull_ref(changed_pull, PR_NUMBER)
            self.assertEqual(caught.exception.code, code)

        tree_payloads = (
            ({"tree": "bad"}, "invalid_tree"),
            ({"tree": [{"type": "blob", "path": "a", "sha": HEAD, "mode": "100644"},
                       {"type": "blob", "path": "a", "sha": BASE, "mode": "100644"}]}, "invalid_tree"),
        )
        for tree, code in tree_payloads:
            with patch.object(subject.HttpGitHubGateway, "_get_commit_tree_sha", return_value=HEAD), \
                 patch.object(subject.HttpGitHubGateway, "_read_json", return_value=tree), \
                 self.assertRaises(subject.GitHubError) as caught:
                gateway._get_tree(REPOSITORY, HEAD, "head")
            self.assertEqual(caught.exception.code, code)
        with patch.object(subject.HttpGitHubGateway, "_get_commit_tree_sha", return_value=HEAD), \
             patch.object(subject.HttpGitHubGateway, "_read_json", return_value={"tree": [{"type": "tree"}], "truncated": True}):
            tree, omissions = gateway._get_tree(REPOSITORY, HEAD, "head")
        self.assertEqual((tree, omissions), ({}, ["head_tree_truncated"]))

        encoded = __import__("base64").b64encode("内容".encode()).decode()
        valid_file = {"type": "file", "content": encoded, "encoding": "base64", "sha": HEAD}
        with patch.object(subject.HttpGitHubGateway, "_read_json", return_value=valid_file):
            self.assertEqual(gateway._get_file_at_ref("src/a.py", HEAD, repository=REPOSITORY, purpose="head").content, "内容")  # type: ignore[union-attr]
        for file_payload in ({"type": "dir"}, {**valid_file, "encoding": "plain"}, {**valid_file, "content": "%%%"}):
            with patch.object(subject.HttpGitHubGateway, "_read_json", return_value=file_payload), self.assertRaises(subject.GitHubError):
                gateway._get_file_at_ref("src/a.py", HEAD, repository=REPOSITORY, purpose="head")
        with patch.object(subject.HttpGitHubGateway, "_read_json", return_value=None):
            self.assertIsNone(gateway._get_file_at_ref("src/a.py", HEAD, repository=REPOSITORY, purpose="head"))

    def test_github_http_request_contract_fails_closed(self) -> None:
        from tools.qykw import github as subject

        def make(transport):
            return subject.HttpGitHubGateway("https://api.github.test", REPOSITORY, "read", "review", transport=transport)

        url = "https://api.github.test/repos/owner/repo/pulls/1"
        ok = make(lambda *_: (200, {"X": "Y"}, b'{"ok":true}'))
        self.assertEqual(ok._request("GET", url, token="read", allow_not_found=False)[0], {"ok": True})
        self.assertIsNone(make(lambda *_: (404, {}, b"{}" ))._request("GET", url, token="read", allow_not_found=True)[0])
        cases = (
            (lambda *_: (500, {}, b"{}"), "github_http_error"),
            (lambda *_: (200, {}, b"bad"), "invalid_response"),
            (lambda *_: ("200", {}, b"{}"), "invalid_response"),
            (lambda *_: (_ for _ in ()).throw(RuntimeError()), "transport_failed"),
        )
        for transport, code in cases:
            with self.subTest(code=code), self.assertRaises(subject.GitHubError) as caught:
                make(transport)._request("GET", url, token="read", allow_not_found=False)
            self.assertEqual(caught.exception.code, code)
        with self.assertRaises(subject.GitHubError):
            ok._request("DELETE", url, token="read", allow_not_found=False)
        with patch.object(subject, "_MAX_RESPONSE_BYTES", 1), self.assertRaises(subject.GitHubError):
            ok._request("POST", url, token="review", body=b"xx", allow_not_found=False)

    def test_phase_artifact_decoders_reject_every_invalid_shape(self) -> None:
        from tools.qykw import phases as subject
        from tools.qykw.domain import Finding, ReviewResult, Severity

        system = ReviewSystem()
        run = build_run_context(comment_event(CommandName.REVIEW, comment_id=8_100), system.gateway.get_pull_ref(PR_NUMBER))
        assert run is not None
        run_payload = subject._run_payload(run)
        self.assertEqual(subject._run_from_artifact({"run": run_payload}), run)
        invalid_runs: list[object] = (
            None, [], {**run_payload, "command": None},
            {**run_payload, "trigger_comment_id": None},
            {**run_payload, "trigger_comment_id": 0},
            {**run_payload, "command": {"name": "unknown", "argument": "", "mode": "read_only"}},
            {key: value for key, value in run_payload.items() if key != "repository"},
        )
        for value in invalid_runs:
            with self.subTest(run=value):
                self.assertIsNone(subject._run_from_artifact({"run": value}))

        advice = {"title": "标题", "body": "正文", "evidence": ["证据"], "limitations": []}
        self.assertEqual(subject._advisory_from_payload(advice), advice)
        for value in (None, {}, {**advice, "title": 1}, {**advice, "evidence": "bad"},
                      {**advice, "limitations": ["x" * (subject._MAX_TEXT + 1)]}):
            with self.subTest(advice=value):
                self.assertIsNone(subject._advisory_from_payload(value))
        rendered = subject._render_advisory(advice)
        self.assertIn("证据：", rendered)
        self.assertEqual(subject._bounded("abcdef", 3), "abc")
        self.assertEqual(subject._bounded("", 3), "信息不可用")

        finding = Finding(
            "src/a.py", 1, DiffSide.RIGHT, Severity.P2,
            "path", "impact", "evidence", "suggestion", "verification", "fingerprint",
        )
        finding_payload = subject._finding_payload(finding)
        self.assertEqual(subject._finding_from_payload(finding_payload), finding)
        for value in (None, {}, {**finding_payload, "line": 0}, {**finding_payload, "path": ""},
                      {**finding_payload, "side": "MIDDLE"}, {**finding_payload, "severity": "PX"}):
            with self.subTest(finding=value):
                self.assertIsNone(subject._finding_from_payload(value))

        review = ReviewResult("ok", (finding,), CoverageReport(1, 1, 1, 1, (), True), ("note",), ())
        review_payload = subject._review_payload(review)
        self.assertEqual(subject._review_from_payload(review_payload), review)
        for value in (
            None, {}, {**review_payload, "coverage": None},
            {**review_payload, "conclusion": "x" * 501},
            {**review_payload, "findings": [{}]},
            {**review_payload, "validation_notes": [1]},
            {**review_payload, "coverage": {**review_payload["coverage"], "total_files": "bad"}},  # type: ignore[index]
        ):
            with self.subTest(review=value):
                self.assertIsNone(subject._review_from_payload(value))

        self.assertTrue(subject._matches_run(system.gateway.get_pull_ref(PR_NUMBER), run))
        self.assertFalse(subject._matches_run(system.gateway.get_pull_ref(PR_NUMBER), replace(run, source_head_sha=MOVED_HEAD)))
        with self.assertRaises(ValueError):
            subject.build_production_controller("unknown")

    def test_phase_controller_early_failures_are_bounded_artifacts(self) -> None:
        from tools.qykw import phases as subject

        system = ReviewSystem()
        run = build_run_context(comment_event(CommandName.REVIEW, comment_id=8_200), system.gateway.get_pull_ref(PR_NUMBER))
        assert run is not None
        artifact = {"version": 1, "phase": "authorize", "run": subject._run_payload(run),
                    "payload": {"authorization": "accepted"}}
        analyze = subject.ProductionPhaseController("analyze", {})
        self.assertEqual(analyze.analyze({"run": None})["payload"]["reason"], "upstream_skipped")  # type: ignore[index]
        analyze._read_services = lambda: (system.gateway, system.state, qykw_config())  # type: ignore[method-assign]
        self.assertEqual(analyze.analyze(artifact)["payload"]["reason"], "state_unavailable")  # type: ignore[index]

        record = RunRecord(run, RunStage.ACCEPTED, RunStatus.ACTIVE, "qykw-v1", None, False, None, (), None, NOW, NOW)
        self.assertTrue(system.state.create(record))
        system.state.request_cancel(PR_NUMBER, run.run_id, stop_comment_id=8_201, actor_login="alice")
        self.assertEqual(analyze.analyze(artifact)["payload"], {"kind": "none", "status": "canceled"})

        stale = ReviewSystem(heads=(MOVED_HEAD,))
        stale_run = replace(run, source_head_sha=MOVED_HEAD)
        stale_record = replace(record, context=stale_run)
        self.assertTrue(stale.state.create(stale_record))
        stale.gateway.heads = (HEAD,)
        stale_artifact = {**artifact, "run": subject._run_payload(stale_run)}
        analyze._read_services = lambda: (stale.gateway, stale.state, qykw_config())  # type: ignore[method-assign]
        self.assertEqual(analyze.analyze(stale_artifact)["payload"], {"kind": "none", "status": "stale"})

        publish = subject.ProductionPhaseController("publish", {})
        self.assertEqual(publish.publish({"run": None})["payload"]["reason"], "upstream_skipped")  # type: ignore[index]
        self.assertEqual(publish.publish({**artifact, "payload": {"status": "failed"}})["payload"]["status"], "failed")  # type: ignore[index]
        publish._review_services = lambda: (system.gateway, system.state, qykw_config())  # type: ignore[method-assign]
        unknown = replace(run, run_id="unknown")
        unknown_artifact = {**artifact, "run": subject._run_payload(unknown),
                            "payload": {"status": "completed", "kind": "advisory", "advisory": {}}}
        self.assertEqual(publish.publish(unknown_artifact)["payload"]["status"], "state_unavailable")  # type: ignore[index]
        invalid_analysis = {**artifact, "payload": {"status": "completed", "kind": "review", "review": {}}}
        self.assertEqual(publish.publish(invalid_analysis)["payload"]["status"], "publication_blocked")  # canceled real run

        failure = subject.ProductionPhaseController("record-failure", {})
        self.assertEqual(failure.record_failure({"run": None}, "failed")["payload"]["reason"], "upstream_skipped")  # type: ignore[index]
        failure._review_services = lambda: (system.gateway, system.state, qykw_config())  # type: ignore[method-assign]
        self.assertEqual(failure.record_failure(unknown_artifact, "failed")["payload"], {"error_code": "failed"})

    def test_runner_rejects_each_preclaim_failure_without_publication(self) -> None:
        from tools.qykw import runner as subject

        system = ReviewSystem()
        invalid = system.runner.handle(object())  # type: ignore[arg-type]
        self.assertEqual(invalid.error_code, "invalid_event")
        no_actor = system.runner.handle(replace(pull_event("opened"), actor_login=""))
        self.assertEqual(no_actor.error_code, "authorization_unavailable")

        for method, error in (
            ("get_actor_permission", "authorization_unavailable"),
            ("get_pull_ref", "pull_ref_unavailable"),
            ("assert_bot_identity", "bot_identity_unavailable"),
        ):
            candidate = ReviewSystem()
            with patch.object(candidate.gateway, method, side_effect=RuntimeError("boundary")):
                outcome = candidate.runner.handle(pull_event("opened"))
            self.assertEqual(outcome.error_code, error)
            self.assertNotIn("summary", candidate.gateway.write_order)

        candidate = ReviewSystem()
        with patch.object(candidate.state, "create", return_value=False), \
             patch.object(candidate.state, "find_by_idempotency_key", return_value=None):
            outcome = candidate.runner.handle(pull_event("opened"))
        self.assertEqual(outcome.error_code, "state_claim_failed")

        stop = ReviewSystem()
        self.assertEqual(stop.runner.handle(comment_event(CommandName.STOP, comment_id=9_001)).error_code, "no_active_run")
        run = build_run_context(comment_event(CommandName.REVIEW, comment_id=9_002), stop.gateway.get_pull_ref(PR_NUMBER))
        assert run is not None
        record = RunRecord(run, RunStage.ANALYZING, RunStatus.ACTIVE, "qykw-v1", None, False, None, (), None, NOW, NOW)
        self.assertTrue(stop.state.create(record))
        no_actor_stop = replace(comment_event(CommandName.STOP, comment_id=9_003), actor_login="")
        self.assertEqual(stop.runner.handle(no_actor_stop).error_code, "authorization_unavailable")
        missing_comment = replace(comment_event(CommandName.STOP, comment_id=9_004), trigger_comment_id=None)
        self.assertEqual(stop.runner.handle(missing_comment).error_code, "missing_stop_comment")
        with patch.object(stop.gateway, "assert_bot_identity", side_effect=RuntimeError("boundary")):
            self.assertEqual(stop.runner.handle(comment_event(CommandName.STOP, comment_id=9_005)).error_code, "cancel_record_failed")

        self.assertTrue(subject.QykwRunner._valid_event(pull_event("opened")))
        self.assertFalse(subject.QykwRunner._valid_event(replace(pull_event("opened"), pr_number=0)))
        review_comment = replace(comment_event(CommandName.REVIEW, comment_id=9_006), trigger_comment_kind=CommentKind.REVIEW)
        self.assertEqual(subject.QykwRunner._trigger_ref(review_comment).kind, "review_comment")
        body = subject.QykwRunner._safe_advisory_body(type("Advice", (), {
            "title": "title", "body": "body", "evidence": ("e",), "limitations": ["ignored"],
        })())
        self.assertIn("证据：", body)
        self.assertNotIn("限制：", body)

    def test_runner_terminal_failure_paths_remain_sanitized(self) -> None:
        systems: list[tuple[ReviewSystem, object, str]] = []
        collect = ReviewSystem()
        systems.append((collect, pull_event("opened"), "stale_pull_ref"))
        review = ReviewSystem()
        systems.append((review, pull_event("opened"), "review_failed"))
        publish = ReviewSystem()
        systems.append((publish, pull_event("opened"), "publish_failed"))
        advisory = ReviewSystem()
        systems.append((advisory, comment_event(CommandName.ANALYZE, comment_id=9_100), "advisory_failed"))
        deterministic = ReviewSystem()
        systems.append((deterministic, comment_event(CommandName.HELP, comment_id=9_101), "deterministic_response_failed"))

        patches = (
            patch.object(collect.gateway, "get_pull_snapshot", side_effect=RuntimeError("secret")),
            patch.object(review.runner.engine, "review", side_effect=RuntimeError("secret")),
            patch.object(publish.runner.publisher, "publish_review", side_effect=RuntimeError("secret")),
            patch.object(advisory.runner.advisory, "handle", side_effect=RuntimeError("secret")),
            patch.object(deterministic.runner.advisory, "handle", side_effect=RuntimeError("secret")),
        )
        for (system, event, expected), boundary in zip(systems, patches):
            with self.subTest(expected=expected), boundary:
                outcome = system.runner.handle(event)  # type: ignore[arg-type]
            self.assertEqual(outcome.error_code, expected)
            self.assertNotIn("secret", repr(outcome))

        drift = ReviewSystem()
        with patch.object(drift.gateway, "get_actor_permission", side_effect=[RepositoryPermission.WRITE, RuntimeError("drift")]):
            outcome = drift.runner.handle(pull_event("opened"))
        self.assertEqual(outcome.error_code, "authorization_drift")

    def test_runner_private_transitions_and_local_state_are_bounded(self) -> None:
        from tools.qykw import runner as subject

        system = ReviewSystem()
        run = build_run_context(comment_event(CommandName.REVIEW, comment_id=9_200), system.gateway.get_pull_ref(PR_NUMBER))
        assert run is not None
        record = RunRecord(run, RunStage.ACCEPTED, RunStatus.ACTIVE, "qykw-v1", None, False, None, (), None, NOW, NOW)
        self.assertTrue(system.state.create(record))
        with self.assertRaisesRegex(ValueError, "invalid_stage_transition"):
            system.runner._transition(record, RunStage.PUBLISHING)
        self.assertEqual(system.runner._publication_failure(record, "authorization_drift").status, RunStatus.FAILED)

        no_provider = subject.QykwRunner(qykw_config(), system.gateway, system.state, None, object(), object())
        with self.assertRaisesRegex(ValueError, "context_builder_required"):
            no_provider._build_context(pull_snapshot(), run)

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "nested" / "state.json"
            writer = subject.QykwRunner(qykw_config(), system.gateway, system.state, None, object(), object(), state_path=target)
            writer._write_local_state(record)
            stored = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(stored["run_id"], run.run_id)
            with patch("tools.qykw.runner.os.chmod", side_effect=OSError("unsupported")):
                writer._write_local_state(record)
            with patch.object(subject, "json") as fake_json:
                fake_json.dumps.return_value = "x" * 4097
                with self.assertRaisesRegex(ValueError, "local_state_too_large"):
                    writer._write_local_state(record)
        self.assertRegex(subject._utc_now(), r"Z$")

    def test_context_helpers_cover_rejection_and_truncation_boundaries(self) -> None:
        from tools.qykw import context as subject
        from tools.qykw.domain import DiffHunk, RepositoryFile

        with self.assertRaises(subject.ContextError):
            subject.estimate_tokens(1)  # type: ignore[arg-type]
        self.assertEqual(subject.parse_hunks(replace(changed_file(), patch=None)), ())
        self.assertEqual(subject.parse_hunks(replace(changed_file(), patch="")), ())
        for file in (
            object(), replace(changed_file(), patch=1),
            replace(changed_file(), patch="@@ -1 +1 @@\n\\ No newline at end of file\n"),
            replace(changed_file(), patch="@@ -1 +1 @@\n?bad\n"),
        ):
            with self.subTest(file=file), self.assertRaises(subject.ContextError):
                subject.parse_hunks(file)  # type: ignore[arg-type]

        for arguments in (
            (True, 100, 0, .1, .5), (100, 0, 0, .1, .5), (100, 100, -1, .1, .5),
            (100, 100, 0, 1.0, .5), (100, 100, 0, .1, 0), (1, 1, 1, 0, .5),
        ):
            with self.subTest(arguments=arguments), self.assertRaises(subject.ContextError):
                subject._effective_budget(*arguments)

        for snapshot in (
            object(), replace(pull_snapshot(), number=0),
            replace(pull_snapshot(), changed_files=(object(),)),
        ):
            with self.subTest(snapshot=snapshot), self.assertRaises(subject.ContextError):
                subject._validate_snapshot(snapshot)  # type: ignore[arg-type]
        for path in ("", "/abs", "a\\b", "a/../b", "C:/b", "a\x7fb"):
            with self.subTest(path=path), self.assertRaises(subject.ContextError):
                subject._validate_relative_path(path)
        for run_id, repository_id in (("bad run", 1), ("ok", True), ("ok", 0)):
            with self.assertRaises(subject.ContextError):
                subject._validate_run_identity(run_id, repository_id)

        class BadOmissions:
            omissions = ["not", "tuple"]
        with self.assertRaises(subject.ContextError):
            subject._snapshot_omissions(BadOmissions())  # type: ignore[arg-type]

        base = changed_file()
        reasons = (
            (replace(base, binary=True), "binary"),
            (replace(base, generated=True), "generated"),
            (replace(base, path=".env"), "sensitive"),
            (base, "unreadable"),
            (replace(base, base_content=None), "unreadable"),
        )
        for file, expected in reasons:
            omissions = (f"head_content_missing:{file.path}",) if file is base else ()
            self.assertEqual(subject._file_skip_reason(file, omissions), expected)
        with patch.object(subject, "_OVERSIZED_CHARACTERS", 1):
            self.assertEqual(subject._file_skip_reason(base, ()), "oversized")
        self.assertIsNone(subject._file_skip_reason(base, ()))

        content_only = replace(base, patch="", base_mode="100644", head_mode="100644")
        records = subject._content_records(content_only)
        self.assertGreaterEqual(len(records), 2)
        self.assertEqual(subject._file_context_records(content_only, ()), records)
        malformed_hunk = DiffHunk(base.path, None, "bad", (), "bad")
        with self.assertRaises(subject.ContextError):
            subject._diff_records(base, malformed_hunk, 0)
        parsed_hunk = subject.parse_hunks(base)[0]
        broken_hunk = replace(parsed_hunk, text=parsed_hunk.header + "\n?bad\n")
        with self.assertRaises(subject.ContextError):
            subject._diff_records(base, broken_hunk, 0)

        record = subject._triage_record(base)
        chunks: list[ContextChunk] = []
        self.assertEqual(subject._allocate_record(
            record, plan_identity="p", provenance_identity="q", chunk_index=1, used=1,
            effective_budget=1, max_chunk_tokens=1, chunks=chunks,
        ), (False, False, 1, 1))
        self.assertEqual(subject._prefix_within_token_budget("abc", 0), "")
        self.assertEqual(subject._prefix_within_token_budget("éa", 2), "é")
        self.assertEqual(subject._line_range(None, 1), "-")
        self.assertEqual(subject._line_range(1, 2), "1-2")
        self.assertEqual(subject._run_id(pull_snapshot()), f"{REPOSITORY}:{PR_NUMBER}:{HEAD}")

        self.assertEqual(subject._contiguous_ranges(set()), ())
        self.assertEqual(subject._contiguous_ranges({1, 2, 4}), ((1, 2), (4, 4)))
        summaries = subject._truncation_summaries(
            ("a",), {("a", 0): {0, 1}, ("a", 1): {0, 1}},
            {("a", 0): {0}}, {("a", 0): {1}, ("a", 1): {0, 1}},
        )
        self.assertTrue(any("partial" in item for item in summaries))
        self.assertTrue(any("unallocated" in item for item in summaries))
        references = subject._ordered_references(
            (RepositoryFile("z", "main", HEAD, "x", "rule"),),
            (RepositoryFile("a", "main", BASE, "y", "related"),),
        )
        self.assertEqual(tuple(item.path for item in references), ("a", "z"))
        self.assertIn("REFERENCE", subject._reference_context_record(references[0]).text)

    def test_context_plan_rejects_duplicate_changed_paths(self) -> None:
        from tools.qykw import context as subject

        snapshot = replace(pull_snapshot(), changed_files=(changed_file(), changed_file()))
        with self.assertRaisesRegex(subject.ContextError, "duplicate_changed_path"):
            subject.build_context_plan(
                snapshot, run_id="run", repository_id=1, repository_limit=100_000,
                backend_context_window=100_000, output_reserve=1_000,
                safety_reserve_ratio=.1, max_chunk_ratio=.5,
            )

    def test_publisher_helpers_fail_closed_and_bound_public_output(self) -> None:
        from tools.qykw import publish as subject
        from tools.qykw.domain import Finding, ReviewResult, Severity

        for maximum in (0, 21):
            with self.assertRaises(ValueError):
                subject.ReviewPublisher(BoundaryGateway(), max_findings=maximum)
        run = build_run_context(comment_event(CommandName.REVIEW, comment_id=9_300), BoundaryGateway().get_pull_ref(PR_NUMBER))
        assert run is not None
        finding = Finding("src/a.py", 1, DiffSide.RIGHT, Severity.P2, "path", "impact", "evidence",
                          "suggestion", "verification", "fingerprint")
        marker = subject.render_fingerprint_marker(run, finding)
        comment = ReviewComment(1, "qykw", marker, NOW, finding.path, finding.line, finding.side)
        self.assertEqual(subject._parse_fingerprint(comment), subject._fingerprint_key(run, finding))
        for body in ("plain", "<!-- qykw-fingerprint:v1 {bad} -->",
                     marker.replace('"kind":"fingerprint"', '"kind":"other"'),
                     marker.replace('"line":1', '"line":0'), marker.replace('"side":"RIGHT"', '"side":"MIDDLE"')):
            self.assertIsNone(subject._parse_fingerprint(replace(comment, body=body)))
        self.assertEqual(subject._existing_fingerprints((replace(comment, author_login="other"), comment), run),
                         {subject._fingerprint_key(run, finding)})

        invalid_findings = (
            object(), replace(finding, line=0), replace(finding, line=True),
            replace(finding, fingerprint=""),
        )
        limited = subject._limited_sorted((finding, finding, *invalid_findings), 20)  # type: ignore[arg-type]
        self.assertEqual(limited, (finding,))
        with self.assertRaises(ValueError):
            subject._inline(run, replace(finding, path="../bad"))

        invalid_coverage = type("BadCoverage", (), {
            "total_files": 0, "reviewed_files": 1, "total_hunks": 0, "reviewed_hunks": 0,
            "explains_every_file": True, "omissions": (),
        })()
        self.assertFalse(subject._valid_coverage(invalid_coverage))
        result = ReviewResult("ok", (), invalid_coverage, (), ())  # type: ignore[arg-type]
        self.assertIn("覆盖信息不可用", subject._summary_body(result))
        lines: list[str] = []
        subject._append_reasons(lines, "x", "bad")
        subject._append_reasons(lines, "x", ())
        subject._append_reasons(lines, "x", tuple(str(i) for i in range(101)))
        self.assertTrue(any("100+" in line for line in lines))
        self.assertEqual(subject._safe_text(None), "信息不可用")
        self.assertEqual(subject._safe_text(""), "信息不可用")
        self.assertFalse(subject._write_allowed(lambda: (_ for _ in ()).throw(RuntimeError())))
        self.assertTrue(subject._write_allowed(None))
        self.assertEqual(subject._display_path("../bad"), "未定位文件")
        for path in (None, "", "/abs", "a\\b", "a/../b", "x" * 1025, "a\x00b"):
            self.assertIsNone(subject._api_path(path))

        system = ReviewSystem(reaction_fails=True)
        publisher = system.publisher
        acknowledge_id = publisher.acknowledge(run)
        self.assertGreater(acknowledge_id, 0)
        no_state = subject.ReviewPublisher(system.gateway)
        no_state.publish_status(RunRecord(run, RunStage.ACCEPTED, RunStatus.ACTIVE, "v", None, False, None, (), None, NOW, NOW))
        self.assertIsNone(no_state._state_comment_id(run))

    def test_trigger_normalization_rejects_malformed_events_and_covers_manual_matrix(self) -> None:
        from tools.qykw import triggers as subject

        base_repository = {"id": REPOSITORY_ID, "full_name": REPOSITORY}
        self.assertIsNone(subject.normalize_event("unknown", {"repository": base_repository},
                                                   repository_id=REPOSITORY_ID, repository=REPOSITORY))
        for repository_id, repository, payload in (
            (0, REPOSITORY, {}), (REPOSITORY_ID, "", {}),
            (REPOSITORY_ID, REPOSITORY, {"repository": {"id": 99, "full_name": REPOSITORY}}),
            (REPOSITORY_ID, REPOSITORY, {"repository": {"id": REPOSITORY_ID, "full_name": "other/repo"}}),
        ):
            self.assertIsNone(subject.normalize_event("pull_request", payload,
                                                       repository_id=repository_id, repository=repository))
        malformed_pulls = (
            {"repository": base_repository},
            {"repository": base_repository, "pull_request": {}},
            {"repository": base_repository, "number": PR_NUMBER,
             "pull_request": {"draft": False, "head": {}}},
        )
        for payload in malformed_pulls:
            self.assertIsNone(subject.normalize_event("pull_request", payload,
                                                       repository_id=REPOSITORY_ID, repository=REPOSITORY))

        issue_payload = {"repository": base_repository, "issue": {"number": PR_NUMBER},
                         "comment": {"id": 1, "body": "@qykw review"}}
        self.assertIsNone(subject.normalize_event("issue_comment", issue_payload,
                                                   repository_id=REPOSITORY_ID, repository=REPOSITORY))
        self.assertIsNone(subject.normalize_event("pull_request_review_comment", {"repository": base_repository},
                                                   repository_id=REPOSITORY_ID, repository=REPOSITORY))

        manual = {"repository": base_repository, "inputs": {"pr_number": str(PR_NUMBER), "command": "修复"},
                  "sender": {"login": "owner"}}
        event = subject.normalize_event("workflow_dispatch", manual, repository_id=REPOSITORY_ID,
                                        repository=REPOSITORY, workflow_run_id=123)
        self.assertEqual(event.command.mode if event else None, CommandMode.CHANGE)
        for payload, workflow_id in (({}, 123), ({"inputs": {}}, 123), (manual, None),
                                     ({"inputs": {"pr_number": "0", "command": "review"}}, 123),
                                     ({"inputs": {"pr_number": "1", "command": "bad"}}, 123)):
            self.assertIsNone(subject.normalize_event("workflow_dispatch", {"repository": base_repository, **payload},
                                                       repository_id=REPOSITORY_ID, repository=REPOSITORY,
                                                       workflow_run_id=workflow_id))

        automatic = pull_event("opened")
        for action, completed, expected in (
            ("opened", True, "initial_review_completed"),
            ("ready_for_review", True, "initial_review_completed"),
            ("reopened", False, "initial_reopened"),
            ("reopened", True, "initial_review_completed"),
            ("closed", False, "automatic_event_ignored"),
        ):
            event = replace(automatic, action=action)
            self.assertEqual(subject.decide_trigger(event, existing_run=None,
                initial_review_completed=completed, config=qykw_config()).reason, expected)
        self.assertEqual(subject.decide_trigger(replace(automatic, event_name="unknown"), existing_run=None,
            initial_review_completed=False, config=qykw_config()).reason, "unsupported_event")
        self.assertIsNone(subject.build_run_context(automatic, None))
        self.assertIsNone(subject.build_run_context(automatic, replace(BoundaryGateway().get_pull_ref(PR_NUMBER), state="closed")))
        self.assertEqual(subject._positive_int("123"), 123)
        for value in (True, "", "-1", "1" * 19, "abc"):
            self.assertIsNone(subject._positive_int(value))
        self.assertIsNone(subject._command_name(1))
        self.assertIsNone(subject._command_name("bad"))

    def test_cli_artifact_validator_exercises_every_fail_closed_shape(self) -> None:
        from tools.qykw import __main__ as subject
        from tools.qykw.phases import _run_payload

        run = build_run_context(comment_event(CommandName.REVIEW, comment_id=9_400), BoundaryGateway().get_pull_ref(PR_NUMBER))
        assert run is not None
        run_payload = _run_payload(run)
        authorize = subject._artifact("authorize", run_payload, {"authorization": "accepted"})
        for payload in (
            None, {}, {**authorize, "phase": "unknown"}, {**authorize, "version": 2},
            {**authorize, "run": None}, {**authorize, "payload": "bad"},
            {**authorize, "run": {"bad": 1}},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                subject._validate_artifact(payload)

        invalid_runs = []
        for key, value in (
            ("run_id", ""), ("repository_id", True), ("pr_number", 0),
            ("trigger_comment_id", 0), ("trigger_comment_kind", "bad"),
            ("command", None),
        ):
            changed = dict(run_payload); changed[key] = value; invalid_runs.append(changed)
        unpaired = dict(run_payload); unpaired["trigger_comment_kind"] = None; invalid_runs.append(unpaired)
        bad_command = dict(run_payload); bad_command["command"] = {"name": "审查", "argument": ""}; invalid_runs.append(bad_command)
        bad_command_text = dict(run_payload); bad_command_text["command"] = {"name": "", "argument": "", "mode": "read_only"}; invalid_runs.append(bad_command_text)
        for value in invalid_runs:
            with self.assertRaises(ValueError):
                subject._validate_run(value)

        valid_payloads = (
            ("request", {"command": "审查"}),
            ("control", {"stop_comment_id": 1}),
            ("authorize", {"authorization": "accepted"}),
            ("analyze", {"analysis": {"result_ref": "artifact"}}),
            ("analyze", {"kind": "none", "status": "stale"}),
            ("publish", {"published": True}),
            ("publish", {"published": False, "status": "failed"}),
            ("record-failure", {"error_code": "failed"}),
        )
        for phase, payload in valid_payloads:
            subject._validate_payload(phase, payload)
        for phase, payload in (("request", {}), ("control", {"stop_comment_id": 0}),
                               ("authorize", {}), ("analyze", {}), ("publish", {}),
                               ("record-failure", {"error_code": "bad-code"})):
            with self.assertRaises(ValueError):
                subject._validate_payload(phase, payload)
        for value in (None, {}, {"status": "bad", "reason": "x"}, {"status": "skipped", "reason": ""}):
            with self.assertRaises(ValueError):
                subject._validate_skipped_payload(value)

        finding = candidate_value() | {"fingerprint": "fp"}
        valid_review = {
            "conclusion": "ok", "findings": [finding],
            "coverage": {"total_files": 1, "reviewed_files": 1, "total_hunks": 1,
                         "reviewed_hunks": 1, "omissions": [], "explains_every_file": True},
            "validation_notes": [], "limitations": [],
        }
        self.assertTrue(subject._valid_structured_analysis({"kind": "review", "status": "completed", "review": valid_review}))
        for review in (
            {}, {**valid_review, "findings": "bad"}, {**valid_review, "findings": [{}]},
            {**valid_review, "coverage": {}},
            {**valid_review, "coverage": {**valid_review["coverage"], "reviewed_files": 2}},  # type: ignore[index]
            {**valid_review, "validation_notes": [1]},
        ):
            self.assertFalse(subject._valid_structured_analysis({"kind": "review", "status": "completed", "review": review}))
        self.assertFalse(subject._valid_finding({}))
        self.assertFalse(subject._valid_finding({**finding, "line": 0}))
        self.assertFalse(subject._valid_finding({**finding, "side": "MIDDLE"}))
        self.assertFalse(subject._valid_finding({**finding, "path": ""}))

        class BadController:
            def record_failure(self, artifact, code):
                del artifact, code
                return "bad"
        with self.assertRaisesRegex(ValueError, "invalid_phase_result"):
            subject._run_phase("record-failure", authorize, BadController(), "failed")
        class Missing:
            pass
        with self.assertRaisesRegex(ValueError, "phase_not_available"):
            subject._run_phase("analyze", authorize, Missing(), None)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "result.json"
            self.assertEqual(subject.main(["--phase", "authorize", "--output", str(output)], controller=Missing()), 2)
            self.assertEqual(subject.main(["--phase", "analyze", "--output", str(output)], controller=Missing()), 2)
            with self.assertRaisesRegex(ValueError, "unsafe_output_path"):
                subject._write_artifact(root / "missing" / "result.json", authorize)
            with patch.object(subject, "_MAX_ARTIFACT_BYTES", 1), self.assertRaisesRegex(ValueError, "artifact_too_large"):
                subject._write_artifact(output, authorize)

    def test_phase_authorize_control_and_environment_fail_closed(self) -> None:
        from tools.qykw import phases as subject

        controller = subject.ProductionPhaseController("authorize", {})
        self.assertEqual(controller.authorize({})["payload"]["reason"], "event_unavailable")  # type: ignore[index]
        control = subject.ProductionPhaseController("control", {})
        self.assertEqual(control.control({})["payload"]["reason"], "event_unavailable")  # type: ignore[index]
        self.assertEqual(controller._authorize_event(object())["payload"]["reason"], "invalid_event")  # type: ignore[index]
        self.assertEqual(controller._authorize_event(comment_event(CommandName.STOP, comment_id=9_500))["payload"]["reason"], "review_lane_noop")  # type: ignore[index]
        self.assertEqual(control._control_event(object())["payload"]["reason"], "review_lane_noop")  # type: ignore[index]
        self.assertEqual(control._control_event(comment_event(CommandName.REVIEW, comment_id=9_501))["payload"]["reason"], "review_lane_noop")  # type: ignore[index]

        system = ReviewSystem()
        controller._review_services = lambda: (system.gateway, system.state, qykw_config())  # type: ignore[method-assign]
        first = controller._authorize_event(comment_event(CommandName.REVIEW, comment_id=9_502))
        self.assertEqual(first["payload"], {"authorization": "accepted"})
        duplicate = controller._authorize_event(comment_event(CommandName.REVIEW, comment_id=9_502))
        self.assertEqual(duplicate["payload"]["reason"], "duplicate")  # type: ignore[index]
        denied = controller._authorize_event(comment_event(CommandName.FIX, comment_id=9_503, actor="owner"))
        self.assertEqual(denied["payload"]["reason"], "review_lane_noop")  # type: ignore[index]

        stale = ReviewSystem(heads=(MOVED_HEAD,))
        stale.gateway.heads = ()
        controller._review_services = lambda: (stale.gateway, stale.state, qykw_config())  # type: ignore[method-assign]
        with patch.object(stale.gateway, "get_pull_ref", return_value=None):
            self.assertEqual(controller._authorize_event(comment_event(CommandName.REVIEW, comment_id=9_504))["payload"]["reason"], "stale_pull_ref")  # type: ignore[index]
        with patch.object(stale.gateway, "get_actor_permission", side_effect=RuntimeError("boundary")):
            self.assertEqual(controller._authorize_event(comment_event(CommandName.REVIEW, comment_id=9_505))["payload"]["reason"], "authorization_unavailable")  # type: ignore[index]

        active_system = ReviewSystem()
        active_event = comment_event(CommandName.REVIEW, comment_id=9_506)
        active_run = build_run_context(active_event, active_system.gateway.get_pull_ref(PR_NUMBER))
        assert active_run is not None
        active_record = RunRecord(active_run, RunStage.ANALYZING, RunStatus.ACTIVE, "qykw-v1", None, False, None, (), None, NOW, NOW)
        self.assertTrue(active_system.state.create(active_record))
        control._review_services = lambda: (active_system.gateway, active_system.state, qykw_config())  # type: ignore[method-assign]
        self.assertEqual(control._control_event(replace(comment_event(CommandName.STOP, comment_id=9_507), trigger_comment_id=None))["payload"]["reason"], "no_active_run")  # type: ignore[index]
        stopped = control._control_event(comment_event(CommandName.STOP, comment_id=9_508))
        self.assertEqual(stopped["payload"], {"stop_comment_id": 9_508})
        self.assertEqual(control._control_event(comment_event(CommandName.STOP, comment_id=9_509))["payload"]["reason"], "duplicate")  # type: ignore[index]

        for phase in ("authorize", "analyze"):
            candidate = subject.ProductionPhaseController(phase, {})
            method = candidate._read_services if phase == "analyze" else candidate._review_services
            with self.assertRaisesRegex(ValueError, "credentials_unavailable"):
                method()
        self.assertFalse(subject.ProductionPhaseController._publication_allowed(
            active_system.gateway, active_system.state, qykw_config(), replace(active_run, source_head_sha=MOVED_HEAD)))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            invalid = root / "invalid.json"; invalid.write_text("{", encoding="utf-8")
            invalid_list = root / "list.json"; invalid_list.write_text("[]", encoding="utf-8")
            for path, reason in ((invalid, "event_unavailable"), (invalid_list, "invalid_event")):
                event_controller = subject.ProductionPhaseController("authorize", {
                    "GITHUB_EVENT_PATH": str(path), "GITHUB_REPOSITORY_ID": str(REPOSITORY_ID),
                    "GITHUB_REPOSITORY": REPOSITORY, "GITHUB_EVENT_NAME": "pull_request", "GITHUB_RUN_ID": "1",
                })
                self.assertEqual(event_controller._event()[1], reason)

    def test_review_parsers_and_local_support_policy_fail_closed(self) -> None:
        from tools.qykw import review as subject
        from tools.qykw.domain import FindingCandidate, Severity

        for maximum in (True, -1, 101):
            with self.assertRaises(ValueError):
                subject.ReviewEngine(BoundaryProvider(), max_findings=maximum)
        valid = candidate_value()
        parsed = subject.parse_candidates({"candidates": [valid]})
        self.assertEqual(len(parsed), 1)
        for value in (None, {}, {"candidates": "bad"}, {"candidates": [{}]},
                      {"candidates": [{**valid, "line": True}]},
                      {"candidates": [{**valid, "side": "MIDDLE"}]}):
            self.assertEqual(subject.parse_candidates(value), ())

        for value in (
            None, {}, {"conclusion": "ok", "findings": [], "validation_notes": [], "limitations": [], "extra": 1},
            {"conclusion": "ok", "findings": "bad", "validation_notes": [], "limitations": []},
            {"conclusion": "", "findings": [], "validation_notes": [], "limitations": []},
            {"conclusion": "ok", "findings": [{}], "validation_notes": [], "limitations": []},
            {"conclusion": "ok", "findings": [], "validation_notes": [1], "limitations": []},
        ):
            self.assertIsNone(subject.parse_validation_response(value))

        candidate = parsed[0]
        changed = frozenset({ChangedLine(candidate.path, candidate.line, candidate.side)})
        self.assertTrue(subject._supported(candidate, changed))
        for unsupported in (
            replace(candidate, path="other.py"),
            replace(candidate, failure_path="might be risky"),
            replace(candidate, severity=Severity.P1, failure_path="minor local issue"),
            replace(candidate, severity=Severity.P0, failure_path="small issue", verification="test it"),
        ):
            self.assertFalse(subject._supported(unsupported, changed))
        p1 = replace(candidate, severity=Severity.P1, failure_path="production authorization fails")
        self.assertTrue(subject._supported(p1, changed))
        p0 = replace(candidate, severity=Severity.P0,
                     failure_path="delete causes irrecoverable data loss everywhere",
                     verification="run five word destructive path regression")
        self.assertTrue(subject._supported(p0, changed))
        self.assertEqual(subject.validate_findings((candidate,), commentable_lines=changed, max_findings=True), ())
        finding = subject.validate_findings((candidate, candidate), commentable_lines=changed, max_findings=1)
        self.assertEqual(len(finding), 1)
        self.assertEqual(subject._normalized("  A   B "), "a b")

        system = ReviewSystem()
        run = build_run_context(pull_event("opened"), system.gateway.get_pull_ref(PR_NUMBER))
        assert run is not None
        plan = fixed_single_chunk_plan(pull_snapshot(), run)
        subject._validate_plan_chunks(run, plan)
        with self.assertRaises(ValueError):
            subject._validate_plan_chunks(run, replace(plan, chunks=(plan.chunks[0], plan.chunks[0])))
        with self.assertRaises(ValueError):
            subject._validate_plan_chunks(run, replace(plan, chunks=(replace(plan.chunks[0], paths=()),)))

    def test_phase_publication_checkpoints_and_secondary_failures_are_explicit(self) -> None:
        from tools.qykw import phases as subject
        from tools.qykw.domain import PublishResult, ReviewResult

        def active(comment_id: int):
            system = ReviewSystem()
            run = build_run_context(comment_event(CommandName.REVIEW, comment_id=comment_id), system.gateway.get_pull_ref(PR_NUMBER))
            assert run is not None
            record = RunRecord(run, RunStage.ACCEPTED, RunStatus.ACTIVE, "qykw-v1", None, False, None, (), None, NOW, NOW)
            self.assertTrue(system.state.create(record))
            artifact = {"version": 1, "phase": "analyze", "run": subject._run_payload(run),
                        "payload": {"status": "completed", "kind": "review", "review": {}}}
            controller = subject.ProductionPhaseController("publish", {})
            controller._review_services = lambda: (system.gateway, system.state, qykw_config())  # type: ignore[method-assign]
            return system, run, artifact, controller

        system, _, artifact, controller = active(9_600)
        with patch.object(system.gateway, "assert_bot_identity", side_effect=RuntimeError("boundary")):
            self.assertEqual(controller.publish(artifact)["payload"]["status"], "bot_identity_unavailable")  # type: ignore[index]

        _, _, artifact, controller = active(9_601)
        with patch.object(controller, "_advance_for_publish", return_value=None):
            self.assertEqual(controller.publish(artifact)["payload"]["status"], "publication_blocked")  # type: ignore[index]

        _, _, artifact, controller = active(9_602)
        self.assertEqual(controller.publish(artifact)["payload"]["status"], "invalid_analysis")  # type: ignore[index]

        system, run, _, controller = active(9_603)
        advice_artifact = {"version": 1, "phase": "analyze", "run": subject._run_payload(run),
                           "payload": {"status": "completed", "kind": "advisory", "advisory": {}}}
        self.assertEqual(controller.publish(advice_artifact)["payload"]["status"], "publication_blocked")  # type: ignore[index]

        system, run, artifact, controller = active(9_604)
        review = ReviewResult("ok", (), CoverageReport(1, 1, 1, 1, (), True), (), ())
        artifact["payload"] = {"status": "completed", "kind": "review", "review": subject._review_payload(review)}
        with patch("tools.qykw.phases.ReviewPublisher.publish_review",
                   return_value=PublishResult(RunStatus.FAILED, 0, "", None, (), ("failed",))):
            self.assertEqual(controller.publish(artifact)["payload"]["status"], "publish_failed")  # type: ignore[index]

        system, run, artifact, controller = active(9_605)
        artifact["payload"] = {"status": "completed", "kind": "review", "review": subject._review_payload(review)}
        with patch.object(controller, "_publication_allowed", side_effect=[True, True, True, True, False]):
            self.assertEqual(controller.publish(artifact)["payload"]["status"], "publication_blocked")  # type: ignore[index]

        system, run, artifact, controller = active(9_606)
        failure = subject.ProductionPhaseController("record-failure", {})
        failure._review_services = lambda: (system.gateway, system.state, qykw_config())  # type: ignore[method-assign]
        with patch.object(system.gateway, "assert_bot_identity", side_effect=RuntimeError("boundary")):
            self.assertEqual(failure.record_failure(artifact, "failed")["payload"]["error_code"], "bot_identity_unavailable")  # type: ignore[index]

        duplicate_system = ReviewSystem(reaction_fails=True)
        authorize = subject.ProductionPhaseController("authorize", {})
        authorize._review_services = lambda: (duplicate_system.gateway, duplicate_system.state, qykw_config())  # type: ignore[method-assign]
        with patch.object(duplicate_system.state, "create", return_value=False):
            self.assertEqual(authorize._authorize_event(comment_event(CommandName.REVIEW, comment_id=9_607))["payload"]["reason"], "duplicate")  # type: ignore[index]
        accepted = authorize._authorize_event(comment_event(CommandName.REVIEW, comment_id=9_608))
        self.assertEqual(accepted["payload"], {"authorization": "accepted"})

        stop_system = ReviewSystem(reaction_fails=True)
        start = comment_event(CommandName.REVIEW, comment_id=9_609)
        stop_run = build_run_context(start, stop_system.gateway.get_pull_ref(PR_NUMBER))
        assert stop_run is not None
        self.assertTrue(stop_system.state.create(RunRecord(stop_run, RunStage.ANALYZING, RunStatus.ACTIVE,
            "qykw-v1", None, False, None, (), None, NOW, NOW)))
        control = subject.ProductionPhaseController("control", {})
        control._review_services = lambda: (stop_system.gateway, stop_system.state, qykw_config())  # type: ignore[method-assign]
        with patch.object(stop_system.gateway, "get_actor_permission", return_value=RepositoryPermission.READ):
            denied = control._control_event(comment_event(CommandName.STOP, comment_id=9_610, actor="other"))
        self.assertFalse(denied["payload"].get("stop_comment_id"))  # type: ignore[index]
        stopped = control._control_event(comment_event(CommandName.STOP, comment_id=9_611))
        self.assertEqual(stopped["payload"], {"stop_comment_id": 9_611})

        read_gateway = subject._ReadGateway(stop_system.gateway)  # type: ignore[arg-type]
        self.assertEqual(read_gateway.get_pull_ref(PR_NUMBER).number, PR_NUMBER)
        self.assertEqual(read_gateway.list_issue_comments(PR_NUMBER), stop_system.gateway.list_issue_comments(PR_NUMBER))

    def test_state_store_rejects_unknown_mutation_and_invalid_record_shapes(self) -> None:
        from tools.qykw import state as subject

        system = ReviewSystem()
        run = build_run_context(comment_event(CommandName.REVIEW, comment_id=9_700), system.gateway.get_pull_ref(PR_NUMBER))
        assert run is not None
        record = RunRecord(run, RunStage.ACCEPTED, RunStatus.ACTIVE, "qykw-v1", None, False, None, (), None, NOW, NOW)
        with self.assertRaisesRegex(ValueError, "unknown_run"):
            system.state.save(record)
        with self.assertRaisesRegex(ValueError, "invalid_stop_comment_id"):
            system.state.request_cancel(PR_NUMBER, run.run_id, stop_comment_id=0, actor_login="alice")
        with self.assertRaisesRegex(ValueError, "unknown_run"):
            system.state.request_cancel(PR_NUMBER, run.run_id, stop_comment_id=1, actor_login="alice")
        with self.assertRaisesRegex(ValueError, "invalid_pr_number"):
            system.state.find_latest(0)
        with self.assertRaisesRegex(ValueError, "repository_mismatch"):
            system.state.create(replace(record, context=replace(run, repository="other/repo")))
        with self.assertRaisesRegex(ValueError, "invalid_timestamp"):
            system.state._checked_time("bad")

        claim = ReviewSystem()
        with patch.object(claim.state, "find_by_idempotency_key", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "state_claim_unconfirmed"):
                claim.state.create(record)

        invalid_records = (
            object(), replace(record, context=object()),
            replace(record, context=replace(run, pr_number=0)),
            replace(record, stage="accepted"), replace(record, initial_review=1),
            replace(record, coverage=object()), replace(record, error_code=1),
            replace(record, created_at="bad"), replace(record, updated_at="bad"),
        )
        for value in invalid_records:
            with self.subTest(record=value), self.assertRaises(ValueError):
                subject._validate_record(value)  # type: ignore[arg-type]

        legacy_payload = subject._record_payload(record)
        legacy_payload.pop("version"); legacy_payload.pop("kind")
        legacy = IssueComment(71, "qykw", f"<!-- qykw-state {json.dumps(legacy_payload, separators=(',', ':'))} -->", NOW)
        self.assertIsNotNone(subject._parse_state_comment(legacy))
        bad_cancel = IssueComment(72, "qykw",
            '<!-- qykw-cancel:v1 {"version":1,"kind":"cancel","pr_number":0,"target_run_id":"r","stop_comment_id":1,"actor_login":"a","created_at":"2026-09-02T00:00:00Z"} -->', NOW)
        self.assertIsNone(subject._parse_cancel_comment(bad_cancel))
        with patch.object(subject, "_JSON_LIMIT", 1), self.assertRaisesRegex(ValueError, "state_payload_too_large"):
            subject.render_state_marker(record)


if __name__ == "__main__":
    unittest.main()
