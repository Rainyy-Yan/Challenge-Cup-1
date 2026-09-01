from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from tools.qykw.config import parse_qykw_config
from tools.qykw.domain import (
    Actor, CommandMode, CommandName, CommandRequest, CoverageReport, EventContext,
    CommentKind, PullRef, PullSnapshot, ReviewResult, RunStage, RunStatus,
)
from tools.qykw.runner import QykwRunner


HEAD = "a" * 40
BASE = "b" * 40


def event(command: CommandName = CommandName.REVIEW, *, key: str = "event-1") -> EventContext:
    return EventContext(8, "owner/repo", 53, "issue_comment", "created", "alice", None,
                        key, CommandRequest(command, "", CommandMode.READ_ONLY), 77, CommentKind.ISSUE)


def config() -> object:
    return parse_qykw_config({"authorization": {"code_writers": ["owner"]}})


class FakeGateway:
    def __init__(self, *, heads: tuple[str, ...] = (HEAD,), reaction_fails: bool = False,
                 permission: object | None = None, permissions: tuple[object, ...] | None = None) -> None:
        self.heads = list(heads)
        self.reaction_fails = reaction_fails
        self.permission = permission
        self.permissions = list(permissions or ())
        self.reaction_calls: list[int] = []
        self.write_calls: list[str] = []

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
              cancel_before_publish: bool = False) -> tuple[QykwRunner, FakeGateway, FakeState, FakePublisher]:
        gateway = FakeGateway(heads=heads, reaction_fails=reaction_fails)
        state = FakeState()
        publisher = FakePublisher(gateway)
        runner = QykwRunner(config(), gateway, state, None,
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

    def test_cli_rejects_cross_run_artifact_before_phase_controller(self) -> None:
        from tools.qykw.__main__ import main

        class Controller:
            called = False

            def authorize(self, artifact: object) -> object:
                self.called = True
                return artifact

        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "input.json"
            output_path = Path(directory) / "output.json"
            input_path.write_text(json.dumps({
                "version": 1, "kind": "qykw-run", "phase": "control",
                "run": {"run_id": "QY-PR53-A", "idempotency_key": "key", "repository_id": 8,
                        "repository": "owner/repo", "pr_number": 53, "head_sha": HEAD,
                        "base_sha": BASE, "base_ref": "main"},
            }), encoding="utf-8")
            controller = Controller()
            result = main(["--phase", "authorize", "--artifact", str(input_path),
                           "--output", str(output_path)], controller=controller)
        self.assertEqual(result, 2)
        self.assertFalse(controller.called)

    def test_cli_records_a_sanitized_failure_artifact(self) -> None:
        from tools.qykw.__main__ import main

        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "input.json"
            output_path = Path(directory) / "output.json"
            input_path.write_text(json.dumps({
                "version": 1, "kind": "qykw-run", "phase": "analyze",
                "run": {"run_id": "QY-PR53-A", "idempotency_key": "key", "repository_id": 8,
                        "repository": "owner/repo", "pr_number": 53, "head_sha": HEAD,
                        "base_sha": BASE, "base_ref": "main"},
            }), encoding="utf-8")
            result = main(["--phase", "record-failure", "--artifact", str(input_path),
                           "--output", str(output_path), "--error-code", "provider_failed"])
            payload = json.loads(output_path.read_text(encoding="utf-8")) if output_path.exists() else {}
        self.assertEqual(result, 0)
        self.assertEqual(payload["failure"], {"code": "provider_failed"})


def replace_runner_state_path(runner: QykwRunner, state_path: Path) -> QykwRunner:
    return QykwRunner(runner.config, runner.gateway, runner.state, runner.advisory,
                      runner.engine, runner.publisher, context_builder=runner.context_builder,
                      now=runner.now, state_path=state_path)


if __name__ == "__main__":
    unittest.main()
