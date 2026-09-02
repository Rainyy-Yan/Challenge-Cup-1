from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools.qykw.config import parse_qykw_config
from tools.qykw.domain import (
    Actor, CommandMode, CommandName, CommandRequest, CoverageReport, EventContext,
    CommentKind, IssueComment, PullRef, PullSnapshot, ReviewResult, RunRecord, RunStage, RunStatus,
)
from tools.qykw.runner import QykwRunner
from tools.qykw.state import GitHubCommentStateStore


HEAD = "a" * 40
BASE = "b" * 40


def event(command: CommandName = CommandName.REVIEW, *, key: str = "event-1") -> EventContext:
    return EventContext(8, "owner/repo", 53, "issue_comment", "created", "alice", None,
                        key, CommandRequest(command, "", CommandMode.READ_ONLY), 77, CommentKind.ISSUE)


def config() -> object:
    return parse_qykw_config({"authorization": {"code_writers": ["owner"]}})


def complete_run() -> dict[str, object]:
    return {
        "run_id": "QY-PR53-A", "idempotency_key": "comment:8:issue:77",
        "repository_id": 8, "repository": "owner/repo", "pr_number": 53,
        "event_name": "issue_comment", "event_action": "created",
        "source_repository": "source/repo", "source_head_sha": HEAD,
        "target_base_sha": BASE, "target_base_ref": "main", "actor_login": "alice",
        "trigger_comment_id": 77, "trigger_comment_kind": "issue",
        "command": {"name": "审查", "argument": "", "mode": "read_only"},
    }


def artifact_for(phase: str, run: object, payload: object) -> dict[str, object]:
    return {"version": 1, "phase": phase, "run": run, "payload": payload}


class FakeGateway:
    def __init__(self, *, heads: tuple[str, ...] = (HEAD,), reaction_fails: bool = False,
                 permission: object | None = None, permissions: tuple[object, ...] | None = None) -> None:
        self.heads = list(heads)
        self.reaction_fails = reaction_fails
        self.permission = permission
        self.permissions = list(permissions or ())
        self.reaction_calls: list[int] = []
        self.write_calls: list[str] = []
        self.comments: list[str] = []
        self.issue_comments: list[IssueComment] = []
        self.next_comment_id = 1

    def get_actor_permission(self, _: str):
        from tools.qykw.domain import RepositoryPermission
        if self.permissions:
            return self.permissions.pop(0) if len(self.permissions) > 1 else self.permissions[0]
        return self.permission or RepositoryPermission.WRITE

    def get_pull_ref(self, _: int) -> PullRef:
        head = self.heads.pop(0) if len(self.heads) > 1 else self.heads[0]
        return PullRef(53, "open", False, "source/repo", head, "owner/repo", BASE, "main")

    def assert_bot_identity(self, expected_login: str = "qykw") -> object:
        self.write_calls.append("identity")
        return object()

    def try_add_reaction(self, trigger: object, content: str = "laugh") -> object:
        self.reaction_calls.append(trigger.node_id)
        if self.reaction_fails:
            raise RuntimeError("reaction unavailable")
        return object()

    def get_pull_snapshot(self, _: int, *, run: object) -> PullSnapshot:
        self.write_calls.append("snapshot")
        return PullSnapshot(53, "open", False, "source/repo", run.source_head_sha,
                            "owner/repo", BASE, "main", "title", "body", (), (), (), ())

    def create_issue_comment(self, _: int, body: str) -> int:
        self.write_calls.append("create_comment")
        self.comments.append(body)
        comment_id = self.next_comment_id
        self.next_comment_id += 1
        self.issue_comments.append(IssueComment(comment_id, "qykw", body, "2026-09-02T00:00:00Z"))
        return comment_id

    def list_issue_comments(self, _: int) -> tuple[IssueComment, ...]:
        return tuple(self.issue_comments)

    def update_issue_comment(self, comment_id: int, body: str) -> None:
        for index, comment in enumerate(self.issue_comments):
            if comment.comment_id == comment_id:
                self.issue_comments[index] = IssueComment(comment_id, "qykw", body, "2026-09-02T00:00:00Z")
                return
        raise AssertionError("unknown comment")


class FakeState:
    def __init__(self) -> None:
        self.records: dict[str, object] = {}
        self.canceled: set[str] = set()
        self.cancels: list[tuple[str, int]] = []

    def find_by_idempotency_key(self, _: int, key: str):
        return next((record for record in self.records.values()
                     if record.context.idempotency_key == key), None)

    def has_successful_initial_review(self, _: int) -> bool:
        return False

    def create(self, record: object) -> bool:
        self.records[record.context.run_id] = record
        return True

    def save(self, record: object) -> None:
        self.records[record.context.run_id] = record

    def get(self, _: int, run_id: str):
        return self.records.get(run_id)

    def find_latest(self, _: int):
        return next(reversed(self.records.values()), None)

    def find_latest_active(self, _: int):
        return next((record for record in reversed(tuple(self.records.values()))
                     if record.status is RunStatus.ACTIVE), None)

    def is_cancel_requested(self, _: int, run_id: str) -> bool:
        return run_id in self.canceled

    def request_cancel(self, _: int, run_id: str, *, stop_comment_id: int, actor_login: str) -> object:
        self.cancels.append((run_id, stop_comment_id))
        self.canceled.add(run_id)
        return object()


class FakeEngine:
    def __init__(self, state: FakeState, *, cancel_before_publish: bool = False) -> None:
        self.state = state
        self.cancel_before_publish = cancel_before_publish
        self.calls = 0

    def review(self, run: object, snapshot: object, plan: object) -> ReviewResult:
        del snapshot, plan
        self.calls += 1
        if self.cancel_before_publish:
            self.state.canceled.add(run.run_id)
        return ReviewResult("ok", (), CoverageReport(0, 0, 0, 0, (), True), (), ())


class FakeAdvisory:
    def __init__(self) -> None:
        self.calls = 0

    def handle(self, run: object, plan: object | None, record: object = None) -> object:
        del run, plan, record
        self.calls += 1
        return type("Advice", (), {"title": "<b>title</b>", "body": "@team https://evil.test clean", "evidence": (), "limitations": ()})()


class FakePublisher:
    def __init__(self, gateway: FakeGateway) -> None:
        self.gateway = gateway
        self.calls: list[str] = []

    def publish_review(self, run: object, result: object):
        del run, result
        self.calls.append("publish")
        self.gateway.write_calls.append("create_review")
        from tools.qykw.domain import PublishResult
        return PublishResult(RunStatus.COMPLETED, 1, "ok", 2, (), ())

    def publish_status(self, record: object) -> None:
        del record
        self.calls.append("status")


class TestQykwRunner(unittest.TestCase):
    def build(self, *, heads: tuple[str, ...] = (HEAD,), reaction_fails: bool = False,
              cancel_before_publish: bool = False, advisory: object | None = None) -> tuple[QykwRunner, FakeGateway, FakeState, FakePublisher]:
        gateway = FakeGateway(heads=heads, reaction_fails=reaction_fails)
        state = FakeState()
        publisher = FakePublisher(gateway)
        runner = QykwRunner(config(), gateway, state, advisory,
                            FakeEngine(state, cancel_before_publish=cancel_before_publish), publisher,
                            context_builder=lambda snapshot, run: object(),
                            now=lambda: "2026-09-02T00:00:00Z")
        return runner, gateway, state, publisher

    def test_head_drift_prevents_inline_publish(self) -> None:
        runner, gateway, _, publisher = self.build(heads=(HEAD, "c" * 40))
        outcome = runner.handle(event())
        self.assertEqual(outcome.status, RunStatus.STALE)
        self.assertNotIn("create_review", gateway.write_calls)
        self.assertEqual(publisher.calls, [])

    def test_reaction_failure_does_not_stop_review(self) -> None:
        runner, gateway, _, publisher = self.build(reaction_fails=True)
        outcome = runner.handle(event())
        self.assertEqual(outcome.status, RunStatus.COMPLETED)
        self.assertEqual(gateway.reaction_calls, [77])
        self.assertEqual(publisher.calls, ["publish"])

    def test_reaction_happens_only_after_auth_and_idempotency(self) -> None:
        runner, gateway, state, _ = self.build()
        denied = replace(event(key="denied"), actor_login="")
        replay = event(key="replay")
        runner.handle(replay)
        gateway.reaction_calls.clear()
        for item in (denied, replay):
            runner.handle(item)
        self.assertEqual(gateway.reaction_calls, [])

    def test_review_persists_the_required_stage_order(self) -> None:
        runner, _, state, _ = self.build()
        outcome = runner.handle(event())
        self.assertEqual(outcome.stage, RunStage.COMPLETED)
        record = state.get(53, outcome.run_id)
        self.assertEqual(record.status, RunStatus.COMPLETED)
        self.assertEqual(record.stage, RunStage.COMPLETED)

    def test_cancellation_after_analysis_skips_publish(self) -> None:
        runner, gateway, state, publisher = self.build(cancel_before_publish=True)
        outcome = runner.handle(event())
        self.assertEqual(outcome.status, RunStatus.CANCELED)
        self.assertEqual(publisher.calls, [])
        self.assertNotIn("create_review", gateway.write_calls)
        self.assertEqual(state.get(53, outcome.run_id).stage, RunStage.COMPLETED)

    def test_permission_drift_prevents_publish_without_marking_head_stale(self) -> None:
        from tools.qykw.domain import RepositoryPermission
        runner, gateway, _, publisher = self.build()
        gateway.permissions = [RepositoryPermission.WRITE, RepositoryPermission.NONE]
        outcome = runner.handle(event())
        self.assertEqual(outcome.status, RunStatus.FAILED)
        self.assertEqual(outcome.error_code, "authorization_drift")
        self.assertEqual(publisher.calls, [])

    def test_local_state_is_versioned_and_excludes_command_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            runner, _, _, _ = self.build()
            runner = replace_runner_state_path(runner, state_path)
            runner.handle(replace(event(), command=CommandRequest(CommandName.REVIEW, "private comment body", CommandMode.READ_ONLY)))
            payload = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["version"], 1)
        self.assertNotIn("private comment body", json.dumps(payload))
        self.assertEqual(set(payload), {"version", "run_id", "repository_id", "pr_number", "head_sha", "base_sha", "stage", "status", "error_code"})

    def test_cli_rejects_unknown_phase_without_reading_artifacts(self) -> None:
        from tools.qykw.__main__ import main
        self.assertEqual(main(["--phase", "model-selected-phase"]), 2)

    def test_root_authorize_noop_uses_trusted_event_without_an_input_artifact(self) -> None:
        from tools.qykw.__main__ import main

        with tempfile.TemporaryDirectory() as directory:
            event_path = Path(directory) / "event.json"
            output_path = Path(directory) / "authorize.json"
            event_path.write_text(json.dumps({"action": "created", "repository": {"id": 8, "full_name": "owner/repo"},
                                              "issue": {"number": 53}, "comment": {"id": 77, "body": "ordinary issue"},
                                              "sender": {"login": "alice"}}), encoding="utf-8")
            environment = {"GITHUB_EVENT_PATH": str(event_path), "GITHUB_REPOSITORY_ID": "8",
                           "GITHUB_REPOSITORY": "owner/repo", "GITHUB_EVENT_NAME": "issue_comment", "GITHUB_RUN_ID": "44"}
            with patch.dict(os.environ, environment, clear=True):
                result = main(["--phase", "authorize", "--artifact", str(Path(directory) / "missing-request.json"),
                               "--output", str(output_path)])
            payload = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual(result, 0)
        self.assertEqual(payload, {"version": 1, "phase": "authorize", "run": None,
                                   "payload": {"status": "skipped", "reason": "not_a_pull_request"}})

    def test_real_module_entrypoint_roots_authorize_without_a_controller_or_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            event_path = Path(directory) / "event.json"
            output_path = Path(directory) / "authorize.json"
            event_path.write_text(json.dumps({"action": "created", "repository": {"id": 8, "full_name": "owner/repo"},
                                              "issue": {"number": 53}, "comment": {"id": 77, "body": "ordinary issue"},
                                              "sender": {"login": "alice"}}), encoding="utf-8")
            environment = {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_PATH": str(event_path),
                           "GITHUB_REPOSITORY_ID": "8", "GITHUB_REPOSITORY": "owner/repo",
                           "GITHUB_EVENT_NAME": "issue_comment", "GITHUB_RUN_ID": "44"}
            process = subprocess.run([sys.executable, "-m", "tools.qykw", "--phase", "authorize", "--artifact", str(Path(directory) / "missing-request.json"),
                                      "--output", str(output_path)],
                                     cwd=Path(__file__).resolve().parents[1], env={**os.environ, **environment},
                                     text=True, capture_output=True, check=False)
            payload = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertNotIn("phase_controller_required", process.stderr)
        self.assertEqual(payload["payload"]["status"], "skipped")

    def test_analyze_phase_has_only_read_operations_and_never_saves_state(self) -> None:
        from tools.qykw.phases import ProductionPhaseController, _run_from_artifact

        class TrackingState(FakeState):
            def __init__(self) -> None:
                super().__init__()
                self.save_calls = 0

            def save(self, record: object) -> None:
                self.save_calls += 1
                super().save(record)

        binding = complete_run()
        binding["command"] = {"name": "帮助", "argument": "", "mode": "read_only"}
        artifact = artifact_for("authorize", binding, {"authorization": "accepted"})
        run = _run_from_artifact(artifact)
        self.assertIsNotNone(run)
        gateway = FakeGateway()
        state = TrackingState()
        state.records[run.run_id] = RunRecord(run, RunStage.ACCEPTED, RunStatus.ACTIVE, "qykw-v1", None,
                                               False, None, (), None, "2026-09-02T00:00:00Z", "2026-09-02T00:00:00Z")
        controller = ProductionPhaseController("analyze", {})
        controller._read_services = lambda: (gateway, state, config())  # type: ignore[method-assign]
        result = controller.analyze(artifact)
        self.assertEqual(result["phase"], "analyze")
        self.assertEqual(state.save_calls, 0)
        self.assertNotIn("create_comment", gateway.write_calls)

    def test_root_authorize_creates_the_fixed_run_before_reacting(self) -> None:
        from tools.qykw.phases import ProductionPhaseController

        with tempfile.TemporaryDirectory() as directory:
            event_path = Path(directory) / "event.json"
            event_path.write_text(json.dumps({"action": "created", "repository": {"id": 8, "full_name": "owner/repo"},
                                              "issue": {"number": 53, "pull_request": {}},
                                              "comment": {"id": 321, "body": "@qykw 审查"}, "sender": {"login": "alice"}}), encoding="utf-8")
            controller = ProductionPhaseController("authorize", {"GITHUB_EVENT_PATH": str(event_path), "GITHUB_REPOSITORY_ID": "8",
                                                                    "GITHUB_REPOSITORY": "owner/repo", "GITHUB_EVENT_NAME": "issue_comment", "GITHUB_RUN_ID": "44"})
            gateway = FakeGateway()
            state = GitHubCommentStateStore(gateway, repository="owner/repo")
            controller._review_services = lambda: (gateway, state, config())  # type: ignore[method-assign]
            result = controller.root()
        self.assertEqual(result["payload"], {"authorization": "accepted"})
        self.assertEqual(state.get(53, result["run"]["run_id"]).context.trigger_comment_id, 321)  # type: ignore[union-attr,index]
        self.assertEqual(gateway.reaction_calls, [321])

    def test_comment_authorize_artifacts_roundtrip_real_state_for_issue_and_review_comments(self) -> None:
        from tools.qykw.phases import ProductionPhaseController, _run_from_artifact

        cases = (
            ("issue_comment", 321, "issue", {"issue": {"number": 53, "pull_request": {}}}),
            ("pull_request_review_comment", 909, "review", {"pull_request": {"number": 53}}),
        )
        for event_name, comment_id, comment_kind, pull_data in cases:
            with self.subTest(event_name=event_name, comment_id=comment_id), tempfile.TemporaryDirectory() as directory:
                event_path = Path(directory) / "event.json"
                event_path.write_text(json.dumps({"action": "created", "repository": {"id": 8, "full_name": "owner/repo"},
                                                  **pull_data, "comment": {"id": comment_id, "body": "@qykw 帮助"},
                                                  "sender": {"login": "alice"}}), encoding="utf-8")
                environment = {"GITHUB_EVENT_PATH": str(event_path), "GITHUB_REPOSITORY_ID": "8", "GITHUB_REPOSITORY": "owner/repo",
                               "GITHUB_EVENT_NAME": event_name, "GITHUB_RUN_ID": "44"}
                gateway = FakeGateway()
                state = GitHubCommentStateStore(gateway, repository="owner/repo")
                authorize = ProductionPhaseController("authorize", environment)
                authorize._review_services = lambda: (gateway, state, config())  # type: ignore[method-assign]
                artifact = authorize.root()
                run = _run_from_artifact(artifact)
                analyze = ProductionPhaseController("analyze", {})
                analyze._read_services = lambda: (gateway, state, config())  # type: ignore[method-assign]
                analyzed = analyze.analyze(artifact)
            stored = state.get(53, run.run_id)  # type: ignore[union-attr]
            self.assertEqual(run, stored.context)  # type: ignore[union-attr]
            self.assertEqual(run.trigger_comment_id, comment_id)  # type: ignore[union-attr]
            self.assertEqual(run.trigger_comment_kind.value, comment_kind)  # type: ignore[union-attr]
            self.assertNotEqual(analyzed["payload"], {"status": "skipped", "reason": "state_unavailable"})

    def test_automatic_and_manual_authorize_roundtrip_with_null_comment_binding(self) -> None:
        from tools.qykw.phases import ProductionPhaseController, _run_from_artifact

        cases = (
            ("pull_request_target", {"action": "opened", "number": 53, "repository": {"id": 8, "full_name": "owner/repo"},
                                       "pull_request": {"number": 53, "draft": False, "head": {"sha": HEAD}}, "sender": {"login": "alice"}}),
            ("workflow_dispatch", {"action": "requested", "repository": {"id": 8, "full_name": "owner/repo"},
                                    "inputs": {"pr_number": "53", "command": "审查"}, "sender": {"login": "alice"}}),
        )
        for event_name, payload in cases:
            with self.subTest(event_name=event_name), tempfile.TemporaryDirectory() as directory:
                event_path = Path(directory) / "event.json"
                event_path.write_text(json.dumps(payload), encoding="utf-8")
                gateway = FakeGateway()
                state = GitHubCommentStateStore(gateway, repository="owner/repo")
                controller = ProductionPhaseController("authorize", {"GITHUB_EVENT_PATH": str(event_path), "GITHUB_REPOSITORY_ID": "8",
                                                                        "GITHUB_REPOSITORY": "owner/repo", "GITHUB_EVENT_NAME": event_name, "GITHUB_RUN_ID": "44"})
                controller._review_services = lambda: (gateway, state, config())  # type: ignore[method-assign]
                artifact = controller.root()
                run = _run_from_artifact(artifact)
            self.assertIsNotNone(run, artifact)
            self.assertEqual(run, state.get(53, run.run_id).context)  # type: ignore[union-attr]
            self.assertIsNone(run.trigger_comment_id)  # type: ignore[union-attr]
            self.assertIsNone(run.trigger_comment_kind)  # type: ignore[union-attr]

    def test_no_comment_run_binding_roundtrips_as_paired_nulls(self) -> None:
        from tools.qykw.__main__ import main

        class Controller:
            def authorize(self, artifact: dict[str, object]) -> dict[str, object]:
                return artifact_for("authorize", artifact["run"], {"authorization": "accepted"})

        run = complete_run()
        run["trigger_comment_id"] = None
        run["trigger_comment_kind"] = None
        with tempfile.TemporaryDirectory() as directory:
            request, output = Path(directory) / "request.json", Path(directory) / "output.json"
            request.write_text(json.dumps(artifact_for("request", run, {"command": "审查"})), encoding="utf-8")
            self.assertEqual(main(["--phase", "authorize", "--artifact", str(request), "--output", str(output)], controller=Controller()), 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertIsNone(payload["run"]["trigger_comment_id"])
        self.assertIsNone(payload["run"]["trigger_comment_kind"])

    def test_cli_rejects_tampered_comment_trigger_binding_before_controller(self) -> None:
        from tools.qykw.__main__ import main

        class Controller:
            called = False

            def authorize(self, artifact: dict[str, object]) -> dict[str, object]:
                self.called = True
                return artifact_for("authorize", artifact["run"], {"authorization": "accepted"})

        for comment_id, kind in ((77, None), (None, "issue"), (True, "issue"), (77, "invalid")):
            with self.subTest(comment_id=comment_id, kind=kind), tempfile.TemporaryDirectory() as directory:
                run = complete_run()
                run["trigger_comment_id"], run["trigger_comment_kind"] = comment_id, kind
                request, output = Path(directory) / "request.json", Path(directory) / "output.json"
                request.write_text(json.dumps(artifact_for("request", run, {"command": "审查"})), encoding="utf-8")
                controller = Controller()
                self.assertEqual(main(["--phase", "authorize", "--artifact", str(request), "--output", str(output)], controller=controller), 2)
                self.assertFalse(controller.called)

    def test_root_authorize_normalizes_pull_request_target_as_pull_request(self) -> None:
        from tools.qykw.phases import ProductionPhaseController

        with tempfile.TemporaryDirectory() as directory:
            event_path = Path(directory) / "event.json"
            event_path.write_text(json.dumps({"action": "opened", "number": 53,
                                              "repository": {"id": 8, "full_name": "owner/repo"},
                                              "pull_request": {"number": 53, "draft": False, "head": {"sha": HEAD}},
                                              "sender": {"login": "alice"}}), encoding="utf-8")
            controller = ProductionPhaseController("authorize", {"GITHUB_EVENT_PATH": str(event_path), "GITHUB_REPOSITORY_ID": "8",
                                                                    "GITHUB_REPOSITORY": "owner/repo", "GITHUB_EVENT_NAME": "pull_request_target", "GITHUB_RUN_ID": "44"})
            gateway, state = FakeGateway(), FakeState()
            controller._review_services = lambda: (gateway, state, config())  # type: ignore[method-assign]
            result = controller.root()
        self.assertEqual(result["payload"], {"authorization": "accepted"})
        self.assertEqual(result["run"]["event_name"], "pull_request")

    def test_cli_accepts_a_run_bound_explicit_skip_outcome(self) -> None:
        from tools.qykw.__main__ import main

        class Controller:
            def authorize(self, artifact: dict[str, object]) -> dict[str, object]:
                return artifact_for("authorize", artifact["run"], {"status": "skipped", "reason": "duplicate"})

        with tempfile.TemporaryDirectory() as directory:
            request = Path(directory) / "request.json"
            output = Path(directory) / "output.json"
            request.write_text(json.dumps(artifact_for("request", complete_run(), {"command": "审查"})), encoding="utf-8")
            result = main(["--phase", "authorize", "--artifact", str(request), "--output", str(output)], controller=Controller())
            payload = json.loads(output.read_text(encoding="utf-8")) if output.exists() else None
        self.assertEqual(result, 0)
        self.assertEqual(payload["payload"], {"status": "skipped", "reason": "duplicate"})

    def test_analyze_post_snapshot_head_drift_is_stale_not_canceled(self) -> None:
        from tools.qykw.phases import ProductionPhaseController, _run_from_artifact

        binding = complete_run()
        binding["command"] = {"name": "帮助", "argument": "", "mode": "read_only"}
        artifact = artifact_for("authorize", binding, {"authorization": "accepted"})
        run = _run_from_artifact(artifact)
        self.assertIsNotNone(run)
        gateway, state = FakeGateway(heads=(HEAD, "c" * 40)), FakeState()
        state.records[run.run_id] = RunRecord(run, RunStage.ACCEPTED, RunStatus.ACTIVE, "qykw-v1", None,
                                               False, None, (), None, "2026-09-02T00:00:00Z", "2026-09-02T00:00:00Z")
        controller = ProductionPhaseController("analyze", {})
        controller._read_services = lambda: (gateway, state, config())  # type: ignore[method-assign]
        result = controller.analyze(artifact)
        self.assertEqual(result["payload"], {"kind": "none", "status": "stale"})
        self.assertEqual(state.canceled, set())

    def test_phase_controllers_filter_environment_to_their_credential_boundary(self) -> None:
        from tools.qykw.phases import ProductionPhaseController

        environment = {"GITHUB_EVENT_PATH": "event.json", "GITHUB_REPOSITORY": "owner/repo", "GITHUB_TOKEN": "read-token",
                       "QYKW_REVIEW_TOKEN": "review-token", "QYKW_INFERENCE_API_KEY": "inference-key",
                       "QYKW_INFERENCE_BASE_URL": "https://provider.test", "UNRELATED_SECRET": "must-not-survive"}
        analyze = ProductionPhaseController("analyze", environment)
        publish = ProductionPhaseController("publish", environment)
        self.assertIn("GITHUB_TOKEN", analyze.environment)
        self.assertIn("QYKW_INFERENCE_API_KEY", analyze.environment)
        self.assertNotIn("QYKW_REVIEW_TOKEN", analyze.environment)
        self.assertNotIn("UNRELATED_SECRET", analyze.environment)
        self.assertIn("QYKW_REVIEW_TOKEN", publish.environment)
        self.assertNotIn("GITHUB_TOKEN", publish.environment)
        self.assertNotIn("QYKW_INFERENCE_API_KEY", publish.environment)
        self.assertNotIn("UNRELATED_SECRET", publish.environment)

    def test_analyze_provider_receives_only_the_filtered_phase_environment(self) -> None:
        from tools.qykw.domain import InferenceResponse, InferenceUsage, ProviderCapabilities
        from tools.qykw.phases import ProductionPhaseController, _run_from_artifact

        class Provider:
            def capabilities(self) -> ProviderCapabilities:
                return ProviderCapabilities(20_000, 2_000, True, frozenset({"maximum"}))

            def complete(self, request: object) -> InferenceResponse:
                del request
                return InferenceResponse(None, {"advisory": {"title": "safe", "body": "safe", "evidence": [], "limitations": []}}, InferenceUsage(None, None))

        binding = complete_run()
        binding["command"] = {"name": "分析", "argument": "", "mode": "read_only"}
        artifact = artifact_for("authorize", binding, {"authorization": "accepted"})
        run = _run_from_artifact(artifact)
        self.assertIsNotNone(run)
        state, gateway = FakeState(), FakeGateway()
        state.records[run.run_id] = RunRecord(run, RunStage.ACCEPTED, RunStatus.ACTIVE, "qykw-v1", None,
                                               False, None, (), None, "2026-09-02T00:00:00Z", "2026-09-02T00:00:00Z")
        controller = ProductionPhaseController("analyze", {"GITHUB_TOKEN": "read", "QYKW_REVIEW_TOKEN": "review",
                                                              "QYKW_INFERENCE_API_KEY": "inference", "UNRELATED_SECRET": "hidden"})
        controller._read_services = lambda: (gateway, state, config())  # type: ignore[method-assign]
        with patch("tools.qykw.phases.ResponsesInferenceProvider.from_env", return_value=Provider()) as factory:
            result = controller.analyze(artifact)
        self.assertEqual(result["payload"]["kind"], "advisory")
        factory.assert_called_once_with(controller.environment)
        supplied = factory.call_args.args[0]
        self.assertNotIn("QYKW_REVIEW_TOKEN", supplied)
        self.assertNotIn("UNRELATED_SECRET", supplied)

    def test_authorize_change_commands_preserve_precise_policy_without_side_effects(self) -> None:
        from tools.qykw.phases import ProductionPhaseController

        controller = ProductionPhaseController("authorize", {})
        gateway, state = FakeGateway(), FakeState()
        controller._review_services = lambda: (gateway, state, config())  # type: ignore[method-assign]
        allowed = controller._authorize_event(replace(event(CommandName.FIX), actor_login="owner"))
        denied = controller._authorize_event(event(CommandName.IMPLEMENT))
        self.assertEqual(allowed["payload"], {"status": "skipped", "reason": "review_lane_noop"})
        self.assertEqual(denied["payload"], {"status": "skipped", "reason": "review_lane_noop"})
        self.assertEqual(gateway.reaction_calls, [])
        self.assertEqual(state.records, {})

    def test_control_reacts_only_after_successful_new_cancel_marker(self) -> None:
        from tools.qykw.phases import ProductionPhaseController, _run_from_artifact

        run = _run_from_artifact(artifact_for("authorize", complete_run(), {"authorization": "accepted"}))
        self.assertIsNotNone(run)
        gateway, state = FakeGateway(), FakeState()
        state.records[run.run_id] = RunRecord(run, RunStage.ANALYZING, RunStatus.ACTIVE, "qykw-v1", None,
                                               False, None, (), None, "2026-09-02T00:00:00Z", "2026-09-02T00:00:00Z")
        controller = ProductionPhaseController("control", {})
        controller._review_services = lambda: (gateway, state, config())  # type: ignore[method-assign]
        stop = replace(event(CommandName.STOP), actor_login="alice")
        first = controller._control_event(stop)
        second = controller._control_event(stop)
        self.assertEqual(first["payload"], {"stop_comment_id": 77})
        self.assertEqual(second["payload"], {"status": "skipped", "reason": "duplicate"})
        self.assertEqual(state.cancels, [(run.run_id, 77)])
        self.assertEqual(gateway.reaction_calls, [77])

    def test_control_cancel_failure_has_zero_reaction(self) -> None:
        from tools.qykw.phases import ProductionPhaseController, _run_from_artifact

        class FailingCancelState(FakeState):
            def request_cancel(self, *args: object, **kwargs: object) -> object:
                raise RuntimeError("cancel write failed")

        run = _run_from_artifact(artifact_for("authorize", complete_run(), {"authorization": "accepted"}))
        self.assertIsNotNone(run)
        gateway, state = FakeGateway(), FailingCancelState()
        state.records[run.run_id] = RunRecord(run, RunStage.ANALYZING, RunStatus.ACTIVE, "qykw-v1", None,
                                               False, None, (), None, "2026-09-02T00:00:00Z", "2026-09-02T00:00:00Z")
        controller = ProductionPhaseController("control", {})
        controller._review_services = lambda: (gateway, state, config())  # type: ignore[method-assign]
        result = controller._control_event(replace(event(CommandName.STOP), actor_login="alice"))
        self.assertEqual(result["payload"]["status"], "skipped")
        self.assertEqual(gateway.reaction_calls, [])

    def test_cli_phase_chain_preserves_the_complete_immutable_run_binding(self) -> None:
        from tools.qykw.__main__ import main

        class Controller:
            def authorize(self, artifact: dict[str, object]) -> dict[str, object]:
                return artifact_for("authorize", artifact["run"], {"authorization": "accepted"})

            def analyze(self, artifact: dict[str, object]) -> dict[str, object]:
                return artifact_for("analyze", artifact["run"], {"analysis": {"result_ref": "sha256:abc"}})

            def publish(self, artifact: dict[str, object]) -> dict[str, object]:
                return artifact_for("publish", artifact["run"], {"published": True})

        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            request = directory_path / "request.json"
            authorize = directory_path / "authorize.json"
            analyze = directory_path / "analyze.json"
            publish = directory_path / "publish.json"
            request.write_text(json.dumps(artifact_for("request", complete_run(), {"command": "审查"})), encoding="utf-8")
            controller = Controller()
            self.assertEqual(main(["--phase", "authorize", "--artifact", str(request), "--output", str(authorize)], controller=controller), 0)
            self.assertEqual(main(["--phase", "analyze", "--artifact", str(authorize), "--output", str(analyze)], controller=controller), 0)
            self.assertEqual(main(["--phase", "publish", "--artifact", str(analyze), "--output", str(publish)], controller=controller), 0)
            final = json.loads(publish.read_text(encoding="utf-8"))
        self.assertEqual(final["run"], complete_run())
        self.assertEqual(final["payload"], {"published": True})

    def test_cli_rejects_wrong_predecessor_and_controller_run_override(self) -> None:
        from tools.qykw.__main__ import main

        class Controller:
            def publish(self, artifact: dict[str, object]) -> dict[str, object]:
                changed = dict(artifact["run"])
                changed["source_head_sha"] = "c" * 40
                return artifact_for("publish", changed, {"published": True})

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.json"
            target = Path(directory) / "target.json"
            source.write_text(json.dumps(artifact_for("analyze", complete_run(), {"analysis": {"result_ref": "sha256:abc"}})), encoding="utf-8")
            self.assertEqual(main(["--phase", "publish", "--artifact", str(source), "--output", str(target)], controller=Controller()), 2)
            self.assertFalse(target.exists())

    def test_cli_records_a_sanitized_failure_only_from_authorized_predecessor(self) -> None:
        from tools.qykw.__main__ import main

        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "input.json"
            output_path = Path(directory) / "output.json"
            input_path.write_text(json.dumps(artifact_for("analyze", complete_run(), {"analysis": {"result_ref": "sha256:abc"}})), encoding="utf-8")
            result = main(["--phase", "record-failure", "--artifact", str(input_path),
                           "--output", str(output_path), "--error-code", "provider_failed"])
            payload = json.loads(output_path.read_text(encoding="utf-8")) if output_path.exists() else {}
        self.assertEqual(result, 0)
        self.assertEqual(payload["payload"], {"error_code": "provider_failed"})

    def test_production_failure_normalizes_only_review_analysis_failures(self) -> None:
        from tools.qykw.phases import ProductionPhaseController, _run_from_artifact

        cases = (
            (CommandName.REVIEW, "analyze_failed", "review_failed"),
            (CommandName.REREVIEW, "analyze_failed", "review_failed"),
            (CommandName.ANALYZE, "analyze_failed", "analyze_failed"),
            (CommandName.PLAN, "analyze_failed", "analyze_failed"),
            (CommandName.REVIEW, "publish_failed", "publish_failed"),
            (CommandName.REVIEW, "provider_failed", "provider_failed"),
        )
        for command, input_code, expected_code in cases:
            with self.subTest(command=command, input_code=input_code):
                binding = complete_run()
                binding["command"] = {"name": command.value, "argument": "", "mode": "read_only"}
                artifact = artifact_for("authorize", binding, {"authorization": "accepted"})
                run = _run_from_artifact(artifact)
                self.assertIsNotNone(run)
                gateway, state = FakeGateway(), FakeState()
                state.records[run.run_id] = RunRecord(run, RunStage.ANALYZING, RunStatus.ACTIVE, "qykw-v1", None,
                                                       False, None, (), None, "2026-09-02T00:00:00Z", "2026-09-02T00:00:00Z")
                controller = ProductionPhaseController("record-failure", {})
                controller._review_services = lambda: (gateway, state, config())  # type: ignore[method-assign]
                result = controller.record_failure(artifact, input_code)
                stored = state.get(53, run.run_id)
                self.assertEqual(result["run"], binding)
                self.assertEqual(result["payload"], {"error_code": expected_code})
                self.assertEqual(stored.stage, RunStage.COMPLETED)  # type: ignore[union-attr]
                self.assertEqual(stored.status, RunStatus.FAILED)  # type: ignore[union-attr]
                self.assertEqual(stored.error_code, expected_code)  # type: ignore[union-attr]
                self.assertNotIn("create_comment", gateway.write_calls)

    def test_control_phase_refuses_any_command_except_stop(self) -> None:
        from tools.qykw.__main__ import main

        class Controller:
            called = False

            def control(self, artifact: dict[str, object]) -> dict[str, object]:
                self.called = True
                return artifact

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "control.json"
            target = Path(directory) / "result.json"
            source.write_text(json.dumps(artifact_for("control", complete_run(), {"stop_comment_id": 77})), encoding="utf-8")
            controller = Controller()
            result = main(["--phase", "control", "--artifact", str(source), "--output", str(target)], controller=controller)
        self.assertEqual(result, 2)
        self.assertFalse(controller.called)

    def test_cancel_between_publication_recheck_and_publisher_prevents_write(self) -> None:
        runner, gateway, state, publisher = self.build()
        original = gateway.get_pull_ref
        calls = 0

        def cancel_after_recheck(pr_number: int) -> PullRef:
            nonlocal calls
            calls += 1
            pull = original(pr_number)
            if calls == 2:
                state.canceled.add(next(iter(state.records)))
            return pull

        gateway.get_pull_ref = cancel_after_recheck  # type: ignore[method-assign]
        outcome = runner.handle(event())
        self.assertEqual(outcome.status, RunStatus.CANCELED)
        self.assertEqual(publisher.calls, [])

    def test_cancel_between_advisory_recheck_and_comment_prevents_write(self) -> None:
        advisory = FakeAdvisory()
        runner, gateway, state, _ = self.build(advisory=advisory)
        original = gateway.get_pull_ref
        calls = 0

        def cancel_after_recheck(pr_number: int) -> PullRef:
            nonlocal calls
            calls += 1
            pull = original(pr_number)
            if calls == 2:
                state.canceled.add(next(iter(state.records)))
            return pull

        gateway.get_pull_ref = cancel_after_recheck  # type: ignore[method-assign]
        outcome = runner.handle(event(CommandName.ANALYZE))
        self.assertEqual(outcome.status, RunStatus.CANCELED)
        self.assertEqual(advisory.calls, 1)

    def test_advisory_comment_uses_shared_safe_public_renderer(self) -> None:
        advisory = FakeAdvisory()
        runner, gateway, _, _ = self.build(advisory=advisory)
        outcome = runner.handle(event(CommandName.ANALYZE))
        self.assertEqual(outcome.status, RunStatus.COMPLETED)
        body = gateway.comments[0]
        for forbidden in ("<b>", "https:", "@team"):
            self.assertNotIn(forbidden, body.lower())
        self.assertIn("clean", body)

    def test_deterministic_permission_drift_blocks_its_public_comment(self) -> None:
        from tools.qykw.domain import RepositoryPermission

        advisory = FakeAdvisory()
        runner, gateway, _, _ = self.build(advisory=advisory)
        gateway.permissions = [RepositoryPermission.WRITE, RepositoryPermission.NONE]
        outcome = runner.handle(event(CommandName.HELP))
        self.assertEqual(outcome.status, RunStatus.FAILED)
        self.assertEqual(outcome.error_code, "authorization_drift")
        self.assertEqual(gateway.comments, [])


def replace_runner_state_path(runner: QykwRunner, state_path: Path) -> QykwRunner:
    return QykwRunner(runner.config, runner.gateway, runner.state, runner.advisory,
                      runner.engine, runner.publisher, context_builder=runner.context_builder,
                      now=runner.now, state_path=state_path)


if __name__ == "__main__":
    unittest.main()
