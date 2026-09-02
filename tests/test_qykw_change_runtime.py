from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import copy
import tempfile
import unittest
from unittest.mock import Mock, patch

from tools.qykw.change_phases import TrustedJobResults, TrustedPhaseRuntime
from tools.qykw.domain import (
    AuthorizationDecision,
    CommandMode,
    CommandName,
    CommandRequest,
    CommentKind,
    EventContext,
    ReactionResult,
    RunContext,
    RunRecord,
    RunStage,
    RunStatus,
)
from tools.qykw.triggers import make_run_id


CONTROLLER_SHA = "c" * 40
IMAGE_DIGEST = "sha256:" + "d" * 64
IMAGE_REF = "ghcr.io/owner/qykw-verify@" + IMAGE_DIGEST


def run_payload() -> dict[str, object]:
    return {
        "run_id": make_run_id(7, "issue_comment:77"),
        "idempotency_key": "issue_comment:77",
        "repository_id": 8,
        "repository": "owner/repo",
        "pr_number": 7,
        "event_name": "issue_comment",
        "event_action": "created",
        "source_repository": "owner/repo",
        "source_head_sha": "a" * 40,
        "target_base_sha": "b" * 40,
        "target_base_ref": "main",
        "actor_login": "owner",
        "trigger_comment_id": 77,
        "trigger_comment_kind": "issue",
        "command": {"name": "修复", "argument": "repair", "mode": "change"},
    }


def request_payload() -> dict[str, object]:
    return {
        "kind": "fix",
        "instruction": "repair",
        "source_repository": "owner/repo",
        "target_repository": "owner/repo",
        "source_head_sha": "a" * 40,
        "target_base_sha": "b" * 40,
        "target_base_ref": "main",
        "verification_profile": "full",
    }


def event_context() -> EventContext:
    return EventContext(
        8,
        "owner/repo",
        7,
        "issue_comment",
        "created",
        "owner",
        None,
        "issue_comment:77",
        CommandRequest(CommandName.FIX, "repair", CommandMode.CHANGE),
        77,
        CommentKind.ISSUE,
    )


def run_context() -> RunContext:
    event = event_context()
    return RunContext(
        make_run_id(event.pr_number, event.idempotency_key),
        event.idempotency_key,
        event.repository_id,
        event.repository,
        event.pr_number,
        event.event_name,
        event.action,
        "owner/repo",
        "a" * 40,
        "b" * 40,
        "main",
        event.command,
        event.actor_login,
        event.trigger_comment_id,
        event.trigger_comment_kind,
    )


def run_record() -> RunRecord:
    return RunRecord(
        run_context(),
        RunStage.ACCEPTED,
        RunStatus.ACTIVE,
        "qykw-v1",
        None,
        False,
        None,
        (),
        None,
        "2026-09-02T00:00:00Z",
        "2026-09-02T00:00:00Z",
    )


def published_artifact() -> dict[str, object]:
    from tools.qykw.__main__ import _change_artifact

    payloads = {
        "authorize-change": {"status": "accepted", "data": {"request": {}}},
        "prepare-change": {
            "status": "prepared",
            "data": {"request": {}, "manifest": {}},
        },
        "verify-change": {
            "status": "verified",
            "data": {"request": {}, "manifest": {}, "attestation": {}},
        },
        "publish-change": {"status": "completed", "data": {"publication": {}}},
    }
    predecessor = None
    for phase in payloads:
        predecessor = _change_artifact(
            phase,
            run_payload(),
            payloads[phase],
            workflow_run_id=44,
            controller_sha=CONTROLLER_SHA,
            verification_profile="full",
            predecessor=predecessor,
        )
    assert predecessor is not None
    return predecessor


class TestProductionChangeFactory(unittest.TestCase):
    def runtime(self, phase: str, *, runner_temp: Path | None = None) -> TrustedPhaseRuntime:
        return TrustedPhaseRuntime(
            phase,
            44,
            CONTROLLER_SHA,
            "full",
            IMAGE_REF if phase in {"verify-change", "publish-change"} else None,
            runner_temp,
            TrustedJobResults("success", "success", "success", "success")
            if phase == "record-change-result"
            else None,
        )

    def test_factory_constructs_only_requested_phase_dependencies(self) -> None:
        from tools.qykw import change_runtime

        environment = {"GITHUB_REPOSITORY": "owner/repo"}
        for phase in (
            "authorize-change",
            "prepare-change",
            "verify-change",
            "publish-change",
            "record-change-result",
        ):
            builders = {name: Mock(return_value=object()) for name in change_runtime._PHASE_BUILDERS}
            with patch.dict(change_runtime._PHASE_BUILDERS, builders, clear=True):
                result = change_runtime.ProductionChangeServicesFactory()(
                    phase, environment, self.runtime(phase)
                )
            self.assertIs(result, builders[phase].return_value)
            self.assertEqual(
                [name for name, builder in builders.items() if builder.called],
                [phase],
            )

    def test_verify_builds_real_executor_from_full_ref_and_workspace(self) -> None:
        from tools.qykw import change_runtime

        workspace = Mock(root=Path("/tmp/candidate"))
        executor = object()
        service = change_runtime._VerifyServices(
            environment={"GITHUB_REPOSITORY": "owner/repo"},
            source_root=Path("/fixed/candidate-source"),
        )
        artifact = {
            "run": {"run_id": "QY-PR7-A1B2"},
            "payload": {"data": {"request": {}, "manifest": {}}},
        }
        with (
            patch.object(
                change_runtime,
                "_request_from_artifact",
                return_value=SimpleNamespace(target_repository="owner/repo"),
            ),
            patch.object(change_runtime, "_manifest_from_artifact", return_value=object()),
            patch.object(change_runtime, "_read_gateway", return_value=object()),
            patch.object(
                change_runtime,
                "_config",
                return_value=SimpleNamespace(review=SimpleNamespace(run_timeout_seconds=900)),
            ),
            patch.object(change_runtime, "_materialize_for_verification", return_value=workspace),
            patch.object(change_runtime, "_request_payload", return_value={}),
            patch.object(change_runtime, "_manifest_payload", return_value={}),
            patch.object(change_runtime, "DockerSandboxExecutor", return_value=executor) as docker,
            patch.object(
                change_runtime, "VerificationRuntimeMetadata", return_value=object()
            ) as metadata,
            patch.object(
                change_runtime,
                "verify_change",
                return_value=SimpleNamespace(canceled=False, success=True),
            ) as verify,
            patch.object(change_runtime, "_attestation_payload", return_value={"ok": True}),
        ):
            result = service.verify_change(artifact, self.runtime("verify-change"))

        docker.assert_called_once_with(workspace.root, IMAGE_REF)
        metadata.assert_called_once_with(44, IMAGE_DIGEST, 900, 1024 * 1024)
        self.assertIs(verify.call_args.args[3], executor)
        self.assertEqual(result["status"], "verified")

    def test_prepare_rejects_an_invalid_artifact_with_a_fixed_domain_error(self) -> None:
        from tools.qykw import change_runtime

        service = change_runtime._PrepareServices(
            environment={"GITHUB_REPOSITORY": "owner/repo"}
        )

        with self.assertRaisesRegex(ValueError, "invalid_run_binding"):
            service.prepare_change({}, self.runtime("prepare-change"))

    def test_record_terminal_status_comes_only_from_job_results(self) -> None:
        from tools.qykw import change_runtime

        service = change_runtime._RecordServices(environment={})
        artifact = {
            "run": {"run_id": "QY-PR7-A1B2"},
            "payload": {"status": "completed", "data": {"outcome": {"status": "completed"}}},
        }
        runtime = TrustedPhaseRuntime(
            "record-change-result",
            44,
            CONTROLLER_SHA,
            "full",
            None,
            None,
            TrustedJobResults("success", "success", "failure", "success"),
        )
        context = object()
        with (
            patch.object(change_runtime, "_run_context", return_value=context),
            patch.object(change_runtime, "_validate_context_environment"),
            patch.object(change_runtime, "_record_terminal") as record,
        ):
            result = service.record_change_result(artifact, runtime)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["data"]["outcome"]["failed_phase"], "verify-change")
        record.assert_called_once_with(context, "failed", "verify-change", {})

    def test_record_rejects_each_rehashed_run_identity_override(self) -> None:
        from tools.qykw import change_runtime
        from tools.qykw.__main__ import _validate_run
        from tools.qykw.change_phases import (
            _artifact_digest,
            _context_digest,
            validate_change_artifact,
        )

        runtime = self.runtime("record-change-result")
        service = change_runtime._RecordServices(
            environment={"GITHUB_REPOSITORY": "owner/repo"}
        )
        mutations = {
            "run_id": "QY-PR7-TAMPERED",
            "idempotency_key": "issue_comment:88",
            "repository": "other/repo",
            "repository_id": 9,
            "pr_number": 8,
            "event_name": "pull_request_review_comment",
            "event_action": "edited",
            "actor_login": "attacker",
            "trigger_comment_id": 88,
            "trigger_comment_kind": "review",
            "command": {"name": "实现", "argument": "repair", "mode": "change"},
        }
        for field, value in mutations.items():
            forged = copy.deepcopy(published_artifact())
            forged["run"][field] = value
            forged["context_digest"] = _context_digest(forged["run"], _validate_run)
            forged["digest"] = _artifact_digest(forged)
            validate_change_artifact(
                forged,
                expected_phase="publish-change",
                validate_run=_validate_run,
            )
            with (
                self.subTest(run_field=field),
                patch.object(change_runtime, "_event_context", return_value=event_context()),
                patch.object(change_runtime, "_record_terminal"),
                self.assertRaisesRegex(ValueError, "invalid_change_request"),
            ):
                service.record_change_result(forged, runtime)

    def test_request_codec_rejects_repository_override_from_artifact(self) -> None:
        from tools.qykw import change_runtime

        request = request_payload()
        artifact = {
            "run": run_payload(),
            "payload": {"data": {"request": request}},
        }
        parsed = change_runtime._request_from_artifact(artifact)
        self.assertEqual(parsed.target_repository, "owner/repo")
        request["target_repository"] = "attacker/repo"
        with self.assertRaisesRegex(ValueError, "invalid_change_request"):
            change_runtime._request_from_artifact(artifact)

    def test_request_run_and_runtime_bindings_reject_each_tampered_field(self) -> None:
        from tools.qykw import change_runtime

        runtime = self.runtime("verify-change")
        artifact = {
            "workflow_run_id": 44,
            "runtime": {
                "controller_sha": CONTROLLER_SHA,
                "verification_profile": "full",
            },
            "run": run_payload(),
            "payload": {"data": {"request": request_payload()}},
        }
        environment = {
            "GITHUB_REPOSITORY": "owner/repo",
            "GITHUB_REPOSITORY_ID": "8",
        }
        with patch.object(change_runtime, "_event_context", return_value=event_context()):
            change_runtime._request_from_artifact(artifact, environment, runtime)
            request_mutations = {
                "kind": "implement",
                "instruction": "other",
                "source_repository": "other/repo",
                "target_repository": "other/repo",
                "source_head_sha": "f" * 40,
                "target_base_sha": "e" * 40,
                "target_base_ref": "other",
                "verification_profile": "backend",
            }
            for field, value in request_mutations.items():
                tampered = copy.deepcopy(artifact)
                tampered["payload"]["data"]["request"][field] = value
                with self.subTest(request_field=field), self.assertRaisesRegex(
                    ValueError, "invalid_change_request"
                ):
                    change_runtime._request_from_artifact(tampered, environment, runtime)

            run_mutations = {
                "run_id": "QY-PR7-TAMPERED",
                "idempotency_key": "issue_comment:88",
                "repository_id": 9,
                "repository": "other/repo",
                "pr_number": 8,
                "event_name": "pull_request_review_comment",
                "event_action": "edited",
                "actor_login": "attacker",
                "trigger_comment_id": 88,
                "trigger_comment_kind": "review",
                "command": {"name": "实现", "argument": "repair", "mode": "change"},
            }
            for field, value in run_mutations.items():
                tampered = copy.deepcopy(artifact)
                tampered["run"][field] = value
                with self.subTest(run_field=field), self.assertRaisesRegex(
                    ValueError, "invalid_change_request"
                ):
                    change_runtime._request_from_artifact(tampered, environment, runtime)

            for field, value in (
                ("workflow_run_id", 45),
                ("controller_sha", "e" * 40),
                ("verification_profile", "backend"),
            ):
                tampered = copy.deepcopy(artifact)
                if field == "workflow_run_id":
                    tampered[field] = value
                else:
                    tampered["runtime"][field] = value
                with self.subTest(runtime_field=field), self.assertRaisesRegex(
                    ValueError, "invalid_change_request"
                ):
                    change_runtime._request_from_artifact(tampered, environment, runtime)

    def test_authorize_replay_and_create_race_return_the_fixed_existing_run(self) -> None:
        from tools.qykw import change_runtime

        results: list[dict[str, object]] = []
        for finds in ((run_record(),), (None, run_record())):
            state = Mock()
            state.find_by_idempotency_key.side_effect = finds
            state.create.return_value = False
            gateway = Mock(unsafe=True)
            gateway.get_actor_permission.return_value = object()
            gateway.get_pull_ref.return_value = object()
            service = change_runtime._AuthorizeServices({})
            with (
                patch.object(change_runtime, "_event_context", return_value=event_context()),
                patch.object(change_runtime, "_review_gateway", return_value=gateway),
                patch.object(change_runtime, "GitHubCommentStateStore", return_value=state),
                patch.object(change_runtime, "_config", return_value=object()),
                patch.object(
                    change_runtime,
                    "authorize_command",
                    return_value=AuthorizationDecision(True, "allowed"),
                ),
                patch.object(change_runtime, "build_run_context", return_value=run_context()),
            ):
                result = service.authorize_change(self.runtime("authorize-change"))

            results.append(result)
            self.assertEqual(result["run"], run_payload())
            self.assertEqual(result["payload"]["status"], "accepted")
            self.assertEqual(result["payload"]["data"]["request"], request_payload())
        self.assertEqual(results[0], results[1])

    def test_reaction_result_warning_and_exception_are_persisted(self) -> None:
        from tools.qykw import change_runtime

        for reaction in (ReactionResult("reaction_failed"), RuntimeError("private")):
            record = run_record()
            state = Mock()
            state.find_by_idempotency_key.return_value = None
            state.create.return_value = True
            state.get.return_value = record
            gateway = Mock(unsafe=True)
            gateway.get_actor_permission.return_value = object()
            gateway.get_pull_ref.return_value = object()
            if isinstance(reaction, Exception):
                gateway.try_add_reaction.side_effect = reaction
            else:
                gateway.try_add_reaction.return_value = reaction
            with (
                patch.object(change_runtime, "_event_context", return_value=event_context()),
                patch.object(change_runtime, "_review_gateway", return_value=gateway),
                patch.object(change_runtime, "GitHubCommentStateStore", return_value=state),
                patch.object(change_runtime, "_config", return_value=object()),
                patch.object(
                    change_runtime,
                    "authorize_command",
                    return_value=AuthorizationDecision(True, "allowed"),
                ),
                patch.object(change_runtime, "build_run_context", return_value=record.context),
            ):
                change_runtime._AuthorizeServices({}).authorize_change(
                    self.runtime("authorize-change")
                )
            self.assertEqual(state.save.call_args.args[0].warning_codes, ("reaction_failed",))

    def test_file_journal_roundtrips_and_rejects_unknown_fields(self) -> None:
        from tools.qykw import change_runtime
        from tools.qykw.change import PublicationStage, WriteKind, WriteState
        from tools.qykw.change_publish import PublicationJournalEntry

        entry = PublicationJournalEntry(
            1,
            "QY-PR7-A1B2",
            "blob:a.py",
            PublicationStage.BLOBS,
            WriteKind.BLOB,
            "a.py",
            "a" * 40,
            WriteState.CREATED,
            "owner/repo",
            "b" * 40,
            "c" * 40,
            "d" * 64,
            44,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "journal.jsonl")
            journal = change_runtime._FilePublicationJournal(path)
            journal.append_synced(entry)
            self.assertEqual(journal.load(entry.run_id), (entry,))
            path.write_text(
                path.read_text(encoding="utf-8").rstrip()[:-1] + ',"extra":1}\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "invalid_publication_journal"):
                journal.load(entry.run_id)


if __name__ == "__main__":
    unittest.main()
