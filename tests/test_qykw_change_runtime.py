from __future__ import annotations

from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import copy
import json
import tempfile
import unittest
from unittest.mock import Mock, patch

from tools.qykw.change import (
    ChangeKind,
    ChangePublication,
    ChangeRequest,
    CommandResult,
    FileDigest,
    FilePatch,
    PatchManifest,
    PublicationStage,
    TextEdit,
    VerificationAttestation,
    WriteKind,
    WriteReceipt,
    WriteState,
    compute_manifest_digest,
)
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


def change_request() -> ChangeRequest:
    context = run_context()
    return ChangeRequest(
        context,
        ChangeKind.FIX,
        "repair",
        context.source_repository,
        context.repository,
        context.source_head_sha,
        context.target_base_sha,
        context.target_base_ref,
        "full",
    )


def patch_manifest() -> PatchManifest:
    request = change_request()
    provisional = PatchManifest(
        1,
        request.context.run_id,
        request.source_repository,
        request.target_repository,
        request.context.pr_number,
        request.source_head_sha,
        request.target_base_sha,
        request.target_base_ref,
        request.verification_profile,
        (FilePatch("src/a.py", "0" * 64, False, (TextEdit("old", "new"),)),),
        "",
    )
    return replace(provisional, digest=compute_manifest_digest(provisional))


def verification_attestation() -> VerificationAttestation:
    request = change_request()
    manifest = patch_manifest()
    return VerificationAttestation(
        1,
        44,
        request.context.run_id,
        request.source_repository,
        request.source_head_sha,
        request.target_repository,
        request.target_base_sha,
        request.target_base_ref,
        manifest.digest,
        "full",
        IMAGE_DIGEST,
        "e" * 64,
        "f" * 64,
        (FileDigest("src/a.py", "100644", "1" * 64),),
        True,
        False,
        (CommandResult("tests", "2" * 64, 0, False, 12, "3" * 64, "ok"),),
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

        for reaction in (
            ReactionResult("reaction_failed"),
            RuntimeError("private"),
            object(),
        ):
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

    def test_factory_builders_enforce_phase_local_credentials(self) -> None:
        from tools.qykw import change_runtime

        self.assertIsInstance(
            change_runtime.build_production_change_factory(),
            change_runtime.ProductionChangeServicesFactory,
        )
        with self.assertRaisesRegex(ValueError, "invalid_change_phase"):
            change_runtime.ProductionChangeServicesFactory()(
                "unknown", {}, self.runtime("authorize-change")
            )
        with patch.dict(change_runtime._PHASE_BUILDERS, {}, clear=True), self.assertRaisesRegex(
            ValueError, "invalid_change_phase"
        ):
            change_runtime.ProductionChangeServicesFactory()(
                "authorize-change", {}, self.runtime("authorize-change")
            )

        cases = (
            (
                change_runtime._build_authorize,
                {"QYKW_REVIEW_TOKEN": "review"},
                self.runtime("authorize-change"),
                change_runtime._AuthorizeServices,
                ("QYKW_REVIEW_TOKEN",),
            ),
            (
                change_runtime._build_prepare,
                {"GITHUB_TOKEN": "read", "QYKW_INFERENCE_API_KEY": "inference"},
                self.runtime("prepare-change"),
                change_runtime._PrepareServices,
                ("GITHUB_TOKEN", "QYKW_INFERENCE_API_KEY"),
            ),
            (
                change_runtime._build_verify,
                {"GITHUB_TOKEN": "read"},
                self.runtime("verify-change"),
                change_runtime._VerifyServices,
                ("GITHUB_TOKEN",),
            ),
            (
                change_runtime._build_publish,
                {"QYKW_PUBLISH_TOKEN": "publish"},
                self.runtime(
                    "publish-change",
                    runner_temp=Path(tempfile.gettempdir()).resolve(),
                ),
                change_runtime._PublishServices,
                ("QYKW_PUBLISH_TOKEN",),
            ),
            (
                change_runtime._build_record,
                {"QYKW_REVIEW_TOKEN": "review"},
                self.runtime("record-change-result"),
                change_runtime._RecordServices,
                ("QYKW_REVIEW_TOKEN",),
            ),
        )
        for builder, environment, runtime, expected_type, required_credentials in cases:
            with self.subTest(builder=builder.__name__):
                self.assertIsInstance(builder(environment, runtime), expected_type)
                for credential in required_credentials:
                    missing = dict(environment)
                    missing.pop(credential)
                    with (
                        self.subTest(missing=credential),
                        self.assertRaisesRegex(
                            ValueError, "phase_credentials_unavailable"
                        ),
                    ):
                        builder(missing, runtime)
        with self.assertRaisesRegex(ValueError, "verification_image_digest_unavailable"):
            change_runtime._build_verify(
                {"GITHUB_TOKEN": "read"}, SimpleNamespace(image_ref=None)
            )
        with self.assertRaisesRegex(ValueError, "invalid_publication_journal_root"):
            change_runtime._PublishServices(
                {"QYKW_PUBLISH_TOKEN": "publish"}, SimpleNamespace(runner_temp=None)
            )

    def test_authorize_fail_closed_skip_paths_and_reaction_validation(self) -> None:
        from tools.qykw import change_runtime

        runtime = self.runtime("authorize-change")
        service = change_runtime._AuthorizeServices({})
        review = replace(
            event_context(),
            command=CommandRequest(CommandName.REVIEW, "", CommandMode.READ_ONLY),
        )
        wrong_mode = replace(
            event_context(),
            command=CommandRequest(CommandName.FIX, "repair", CommandMode.READ_ONLY),
        )
        for event, reason in (
            (None, "not_a_change_comment"),
            (review, "change_command_required"),
            (wrong_mode, "change_mode_required"),
        ):
            with patch.object(change_runtime, "_event_context", return_value=event):
                self.assertEqual(
                    service.authorize_change(runtime)["payload"]["data"]["reason"], reason
                )

        gateway = Mock(unsafe=True)
        gateway.get_actor_permission.return_value = object()
        state = Mock()
        state.find_by_idempotency_key.return_value = None
        common = (
            patch.object(change_runtime, "_event_context", return_value=event_context()),
            patch.object(change_runtime, "_review_gateway", return_value=gateway),
            patch.object(change_runtime, "GitHubCommentStateStore", return_value=state),
            patch.object(change_runtime, "_config", return_value=object()),
        )
        with ExitStack() as stack:
            for manager in common:
                stack.enter_context(manager)
            stack.enter_context(patch.object(
                change_runtime,
                "authorize_command",
                return_value=AuthorizationDecision(False, "permission_denied"),
            ))
            self.assertEqual(
                service.authorize_change(runtime)["payload"]["data"]["reason"],
                "permission_denied",
            )
        with ExitStack() as stack:
            for manager in common:
                stack.enter_context(manager)
            stack.enter_context(patch.object(
                change_runtime,
                "authorize_command",
                return_value=AuthorizationDecision(True, "allowed"),
            ))
            stack.enter_context(
                patch.object(change_runtime, "build_run_context", return_value=None)
            )
            self.assertEqual(
                service.authorize_change(runtime)["payload"]["data"]["reason"],
                "stale_pull_ref",
            )
        state.create.return_value = False
        state.find_by_idempotency_key.side_effect = (None, None)
        with ExitStack() as stack:
            for manager in common:
                stack.enter_context(manager)
            stack.enter_context(patch.object(
                change_runtime,
                "authorize_command",
                return_value=AuthorizationDecision(True, "allowed"),
            ))
            stack.enter_context(
                patch.object(
                    change_runtime, "build_run_context", return_value=run_context()
                )
            )
            with self.assertRaisesRegex(ValueError, "state_claim_unconfirmed"):
                service.authorize_change(runtime)

    def test_prepare_constructs_the_trusted_provider_and_returns_manifest(self) -> None:
        from tools.qykw import change_runtime

        request = change_request()
        manifest = patch_manifest()
        record = run_record()
        state = Mock()
        state.get.return_value = record
        gateway = Mock()
        snapshot = object()
        gateway.get_pull_snapshot.return_value = snapshot
        provider = object()
        policy = object()
        inference = object()
        environment = {
            "GITHUB_TOKEN": "read-token",
            "GITHUB_REPOSITORY": "owner/repo",
        }
        service = change_runtime._PrepareServices(environment)
        with (
            patch.object(change_runtime, "_request_from_artifact", return_value=request),
            patch.object(change_runtime, "_read_gateway", return_value=gateway),
            patch.object(change_runtime, "GitHubCommentStateStore", return_value=state),
            patch.object(
                change_runtime, "HttpTrustedSourceTreeProvider", return_value=provider
            ) as provider_type,
            patch.object(change_runtime, "_config", return_value=object()),
            patch.object(change_runtime, "DeterministicChangePolicy", return_value=policy),
            patch.object(
                change_runtime.ResponsesInferenceProvider,
                "from_env",
                return_value=inference,
            ),
            patch.object(change_runtime, "prepare_change", return_value=manifest) as prepare,
        ):
            result = service.prepare_change({}, self.runtime("prepare-change"))
        provider_type.assert_called_once_with(
            api_url="https://api.github.com",
            repository="owner/repo",
            source_head_sha="a" * 40,
            token="read-token",
        )
        prepare.assert_called_once_with(request, snapshot, inference, policy, state)
        self.assertEqual(result["status"], "prepared")

        state.get.return_value = None
        with (
            patch.object(change_runtime, "_request_from_artifact", return_value=request),
            patch.object(change_runtime, "_read_gateway", return_value=gateway),
            patch.object(change_runtime, "GitHubCommentStateStore", return_value=state),
            self.assertRaisesRegex(ValueError, "change_state_unavailable"),
        ):
            service.prepare_change({}, self.runtime("prepare-change"))

    def test_verify_reports_canceled_failed_and_missing_image(self) -> None:
        from tools.qykw import change_runtime

        service = change_runtime._VerifyServices({"GITHUB_REPOSITORY": "owner/repo"})
        artifact = {"payload": {"data": {}}}
        common = (
            patch.object(change_runtime, "_request_from_artifact", return_value=change_request()),
            patch.object(change_runtime, "_manifest_from_artifact", return_value=patch_manifest()),
        )
        with ExitStack() as stack:
            for manager in common:
                stack.enter_context(manager)
            with self.assertRaisesRegex(ValueError, "verification_image_digest_unavailable"):
                service.verify_change(
                    artifact,
                    SimpleNamespace(image_ref=None, image_digest=None),
                )
        for canceled, success, status in (
            (True, False, "canceled"),
            (False, False, "failed"),
        ):
            with ExitStack() as stack:
                for manager in common:
                    stack.enter_context(manager)
                stack.enter_context(patch.object(change_runtime, "_read_gateway", return_value=object()))
                stack.enter_context(patch.object(change_runtime, "GitHubCommentStateStore", return_value=object()))
                stack.enter_context(patch.object(
                    change_runtime,
                    "_config",
                    return_value=SimpleNamespace(
                        review=SimpleNamespace(run_timeout_seconds=1)
                    ),
                ))
                stack.enter_context(patch.object(
                    change_runtime,
                    "_materialize_for_verification",
                    return_value=SimpleNamespace(root=Path("C:/workspace")),
                ))
                stack.enter_context(patch.object(change_runtime, "DockerSandboxExecutor", return_value=object()))
                stack.enter_context(patch.object(
                    change_runtime, "VerificationRuntimeMetadata", return_value=object()
                ))
                stack.enter_context(patch.object(
                    change_runtime,
                    "verify_change",
                    return_value=SimpleNamespace(canceled=canceled, success=success),
                ))
                stack.enter_context(patch.object(change_runtime, "_request_payload", return_value={}))
                stack.enter_context(patch.object(change_runtime, "_manifest_payload", return_value={}))
                stack.enter_context(patch.object(change_runtime, "_attestation_payload", return_value={}))
                self.assertEqual(
                    service.verify_change(artifact, self.runtime("verify-change"))["status"],
                    status,
                )

    def test_publish_builds_repo_bound_runtime_and_serializes_result(self) -> None:
        from tools.qykw import change_runtime

        request = change_request()
        manifest = patch_manifest()
        attestation = verification_attestation()
        publication = ChangePublication(
            PublicationStage.COMPLETED,
            "qykw/test",
            WriteState.CREATED,
            WriteState.CREATED,
            "4" * 40,
            9,
            (WriteReceipt(WriteKind.PULL, "9", "9", WriteState.CREATED),),
            False,
            None,
        )
        environment = {
            "QYKW_PUBLISH_TOKEN": "publish-token",
            "GITHUB_REPOSITORY": "owner/repo",
            "GITHUB_API_URL": "https://github.example/api",
        }
        runtime = self.runtime(
            "publish-change", runner_temp=Path(tempfile.gettempdir()).resolve()
        )
        service = change_runtime._PublishServices(environment, runtime)
        target_gateway = object()
        state_gateway = object()
        state = object()
        source_provider = object()
        with (
            patch.object(change_runtime, "_request_from_artifact", return_value=request),
            patch.object(change_runtime, "_manifest_from_artifact", return_value=manifest),
            patch.object(
                change_runtime, "_attestation_from_artifact", return_value=attestation
            ),
            patch.object(
                change_runtime, "HttpChangeGitHubGateway", return_value=target_gateway
            ) as target_gateway_type,
            patch.object(
                change_runtime, "HttpGitHubGateway", return_value=state_gateway
            ) as state_gateway_type,
            patch.object(
                change_runtime, "GitHubCommentStateStore", return_value=state
            ) as state_type,
            patch.object(
                change_runtime,
                "HttpTrustedSourceTreeProvider",
                return_value=source_provider,
            ) as source_provider_type,
            patch.object(change_runtime, "_config", return_value=object()),
            patch.object(change_runtime, "DeterministicChangePolicy", return_value=object()),
            patch.object(
                change_runtime, "publish_verified_change", return_value=publication
            ) as publish,
        ):
            result = service.publish_change({}, runtime)
        publication_request = publish.call_args.args[0]
        target_gateway_type.assert_called_once_with(
            "https://github.example/api", "owner/repo", "publish-token"
        )
        state_gateway_type.assert_called_once_with(
            "https://github.example/api", "owner/repo", "publish-token", ""
        )
        state_type.assert_called_once_with(state_gateway, repository="owner/repo")
        source_provider_type.assert_called_once_with(
            api_url="https://github.example/api",
            repository="owner/repo",
            source_head_sha="a" * 40,
            token="publish-token",
        )
        self.assertEqual(publication_request.change, request)
        self.assertEqual(publication_request.branch_name, f"qykw/{request.context.run_id.lower()}-fix")
        self.assertIs(publish.call_args.args[1], target_gateway)
        self.assertIs(publish.call_args.args[2], state)
        self.assertEqual(publish.call_args.kwargs["runtime"].image_digest, IMAGE_DIGEST)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["data"]["publication"]["pull_number"], 9)

    def test_record_maps_trusted_results_and_published_status(self) -> None:
        from tools.qykw import change_runtime

        service = change_runtime._RecordServices({})
        base = {"phase": "publish-change", "payload": {"status": "partial"}}
        cases = (
            (TrustedJobResults("success", "success", "success", "success"), "partial", None),
            (TrustedJobResults("success", "cancelled", "skipped", "skipped"), "canceled", "prepare-change"),
            (TrustedJobResults("failure", "skipped", "skipped", "skipped"), "failed", "authorize-change"),
        )
        for job_results, expected, failed_phase in cases:
            runtime = SimpleNamespace(job_results=job_results)
            with (
                patch.object(change_runtime, "_run_context", return_value=run_context()),
                patch.object(change_runtime, "_validate_context_environment"),
                patch.object(change_runtime, "_record_terminal"),
            ):
                result = service.record_change_result(base, runtime)
            self.assertEqual(result["status"], expected)
            self.assertEqual(result["data"]["outcome"]["failed_phase"], failed_phase)
        with (
            patch.object(change_runtime, "_run_context", return_value=run_context()),
            patch.object(change_runtime, "_validate_context_environment"),
            self.assertRaisesRegex(ValueError, "invalid_job_results"),
        ):
            service.record_change_result({}, SimpleNamespace(job_results=None))

    def test_remaining_runtime_boundaries_fail_closed(self) -> None:
        from tools.qykw import change_runtime

        with self.assertRaisesRegex(ValueError, "invalid_existing_change_run"):
            change_runtime._recover_existing(object(), event_context())
        with self.assertRaisesRegex(ValueError, "invalid_existing_change_run"):
            change_runtime._recover_existing(
                replace(run_record(), prompt_version="other"), event_context()
            )

        artifact = {
            "workflow_run_id": 44,
            "runtime": {
                "controller_sha": CONTROLLER_SHA,
                "verification_profile": "full",
            },
            "run": run_payload(),
            "payload": {"data": {"request": request_payload()}},
        }
        malformed = copy.deepcopy(artifact)
        malformed["payload"]["data"]["request"].pop("kind")
        with self.assertRaisesRegex(ValueError, "invalid_change_request"):
            change_runtime._request_from_artifact(malformed)
        malformed = copy.deepcopy(artifact)
        malformed["payload"]["data"]["request"]["kind"] = 7
        with self.assertRaisesRegex(ValueError, "invalid_change_request"):
            change_runtime._request_from_artifact(malformed)
        with (
            patch.object(change_runtime, "_validate_context_environment"),
            self.assertRaisesRegex(ValueError, "invalid_change_request"),
        ):
            change_runtime._request_from_artifact(
                artifact,
                {"GITHUB_REPOSITORY": "other/repo"},
                self.runtime("verify-change"),
            )

        publish_runtime = self.runtime(
            "publish-change", runner_temp=Path(tempfile.gettempdir()).resolve()
        )
        service = change_runtime._PublishServices({}, publish_runtime)
        with (
            patch.object(
                change_runtime, "_request_from_artifact", return_value=change_request()
            ),
            patch.object(
                change_runtime, "_manifest_from_artifact", return_value=patch_manifest()
            ),
            patch.object(
                change_runtime,
                "_attestation_from_artifact",
                return_value=verification_attestation(),
            ),
            self.assertRaisesRegex(ValueError, "verification_image_digest_unavailable"),
        ):
            service.publish_change({}, SimpleNamespace(image_digest=None))

    def test_materializer_hashes_the_complete_tree_before_copying(self) -> None:
        from tools.qykw import change_runtime

        provider = Mock()
        provider.get_complete_tree.return_value = SimpleNamespace(
            blobs=(
                SimpleNamespace(path="z.py", mode="100755", content=b"z"),
                SimpleNamespace(path="a.py", mode="100644", content=b"a"),
            )
        )
        workspace = object()
        with (
            patch.object(change_runtime, "HttpTrustedSourceTreeProvider", return_value=provider),
            patch.object(change_runtime, "materialize_workspace", return_value=workspace) as materialize,
        ):
            result = change_runtime._materialize_for_verification(
                change_request(),
                Path("C:/source"),
                Path("C:/destination"),
                {"GITHUB_TOKEN": "read"},
            )
        self.assertIs(result, workspace)
        tracked = materialize.call_args.kwargs["tracked_files"]
        self.assertEqual([item.path for item in tracked], ["a.py", "z.py"])
        self.assertEqual(tracked[0].sha256, "ca978112ca1bbdcafac231b39a23dc4da786eff8147c4e72b9807785afee48bb")

    def test_event_gateway_and_configuration_helpers_use_trusted_environment(self) -> None:
        from tools.qykw import change_runtime

        with tempfile.TemporaryDirectory() as directory:
            event_path = Path(directory, "event.json")
            event_path.write_text('{"issue":{}}', encoding="utf-8")
            environment = {
                "GITHUB_EVENT_PATH": str(event_path),
                "GITHUB_EVENT_NAME": "issue_comment",
                "GITHUB_REPOSITORY_ID": "8",
                "GITHUB_REPOSITORY": "owner/repo",
                "GITHUB_RUN_ID": "44",
                "QYKW_REVIEW_TOKEN": "review",
                "GITHUB_TOKEN": "read",
                "GITHUB_API_URL": "https://github.example/api",
                "QYKW_CONFIG_PATH": "config.toml",
            }
            with patch.object(
                change_runtime, "normalize_event", return_value=event_context()
            ) as normalize:
                self.assertEqual(change_runtime._event_context(environment), event_context())
            self.assertEqual(normalize.call_args.kwargs["workflow_run_id"], 44)
            with patch.object(change_runtime, "HttpGitHubGateway", return_value=object()) as gateway:
                change_runtime._review_gateway(environment)
                self.assertEqual(gateway.call_args.args[-2:], ("review", "review"))
                change_runtime._read_gateway(environment)
                self.assertEqual(gateway.call_args.args[-2:], ("read", ""))
            with patch.object(change_runtime, "load_qykw_config", return_value=object()) as load:
                change_runtime._config(environment)
                load.assert_called_once_with(Path("config.toml"))
            self.assertEqual(
                change_runtime._api_url({}), "https://api.github.com"
            )
            event_path.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid_event"):
                change_runtime._event_context(environment)

    def test_manifest_and_attestation_codecs_roundtrip_and_reject_malformed_data(self) -> None:
        from tools.qykw import change_runtime

        manifest = patch_manifest()
        manifest_artifact = {
            "payload": {"data": {"manifest": change_runtime._manifest_payload(manifest)}}
        }
        self.assertEqual(change_runtime._manifest_from_artifact(manifest_artifact), manifest)
        for mutate in (
            lambda value: value.update(extra=True),
            lambda value: value.__setitem__("files", "bad"),
            lambda value: value["files"][0].update(extra=True),
            lambda value: value["files"][0]["edits"][0].update(extra=True),
            lambda value: value["files"][0].__setitem__("base_sha256", 7),
            lambda value: value.__setitem__("digest", "0" * 64),
        ):
            malformed = copy.deepcopy(manifest_artifact)
            mutate(malformed["payload"]["data"]["manifest"])
            with self.assertRaisesRegex(ValueError, "invalid_patch_manifest"):
                change_runtime._manifest_from_artifact(malformed)

        attestation = verification_attestation()
        attestation_artifact = {
            "payload": {
                "data": {"attestation": change_runtime._attestation_payload(attestation)}
            }
        }
        self.assertEqual(
            change_runtime._attestation_from_artifact(attestation_artifact), attestation
        )
        for mutate in (
            lambda value: value.update(extra=True),
            lambda value: value.__setitem__("output_files", "bad"),
            lambda value: value["output_files"][0].update(extra=True),
            lambda value: value["results"][0].__setitem__("timed_out", 0),
            lambda value: value["results"][0].__setitem__("exit_code", "bad"),
        ):
            malformed = copy.deepcopy(attestation_artifact)
            mutate(malformed["payload"]["data"]["attestation"])
            with self.assertRaisesRegex(ValueError, "invalid_verification_attestation"):
                change_runtime._attestation_from_artifact(malformed)

    def test_publication_status_and_terminal_recording_cover_each_outcome(self) -> None:
        from tools.qykw import change_runtime

        publications = (
            (PublicationStage.COMPLETED, False, None, "completed"),
            (PublicationStage.PULL, True, "cancel_requested", "canceled"),
            (PublicationStage.PULL, True, "write_failed", "partial"),
            (PublicationStage.PREFLIGHT, False, "preflight_failed", "failed"),
        )
        for stage, partial, error, expected in publications:
            value = ChangePublication(
                stage,
                "branch",
                WriteState.NOT_CREATED,
                WriteState.NOT_CREATED,
                None,
                None,
                (),
                partial,
                error,
            )
            self.assertEqual(change_runtime._publication_status(value), expected)

        state = Mock()
        record = run_record()
        state.get.return_value = record
        with (
            patch.object(change_runtime, "_review_gateway", return_value=object()),
            patch.object(change_runtime, "GitHubCommentStateStore", return_value=state),
        ):
            for status, expected in (
                ("completed", RunStatus.COMPLETED),
                ("partial", RunStatus.PARTIAL),
                ("failed", RunStatus.FAILED),
                ("canceled", RunStatus.CANCELED),
            ):
                change_runtime._record_terminal(
                    record.context, status, None, {}
                )
                saved = state.save.call_args.args[0]
                self.assertEqual(saved.status, expected)
                self.assertEqual(
                    saved.error_code,
                    None if status == "completed" else "change_failed",
                )
            state.get.return_value = None
            with self.assertRaisesRegex(ValueError, "change_state_unavailable"):
                change_runtime._record_terminal(record.context, "failed", "verify-change", {})

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

    def test_file_journal_rejects_unsafe_storage_and_malformed_scalars(self) -> None:
        from tools.qykw import change_runtime

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "journal.jsonl")
            journal = change_runtime._FilePublicationJournal(path)
            self.assertEqual(journal.load("QY-PR7-A1B2"), ())
            with self.assertRaisesRegex(ValueError, "invalid_publication_journal_entry"):
                journal.append_synced(object())
            path.mkdir()
            with self.assertRaisesRegex(ValueError, "invalid_publication_journal"):
                journal.load("QY-PR7-A1B2")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "journal.jsonl")
            path.write_text('{"a":1,"a":2}\n', encoding="utf-8")
            journal = change_runtime._FilePublicationJournal(path)
            with self.assertRaisesRegex(ValueError, "invalid_publication_journal"):
                journal.load("QY-PR7-A1B2")

        with self.assertRaisesRegex(ValueError, "invalid_mapping"):
            change_runtime._mapping([], "invalid_mapping")
        with self.assertRaisesRegex(ValueError, "invalid_string"):
            change_runtime._string("")


if __name__ == "__main__":
    unittest.main()
