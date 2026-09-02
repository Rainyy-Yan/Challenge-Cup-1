from __future__ import annotations

from contextlib import redirect_stderr
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.qykw.domain import CommandMode, CommandName, CommandRequest


HEAD = "a" * 40
BASE = "b" * 40
CONTROLLER_SHA = "c" * 40
IMAGE_DIGEST = "sha256:" + "d" * 64


def run_binding(run_id: str = "QY-PR53-A1B2") -> dict[str, object]:
    return {
        "run_id": run_id,
        "idempotency_key": "owner/repo:53:comment:77",
        "repository_id": 8,
        "repository": "owner/repo",
        "pr_number": 53,
        "event_name": "issue_comment",
        "event_action": "created",
        "source_repository": "owner/repo",
        "source_head_sha": HEAD,
        "target_base_sha": BASE,
        "target_base_ref": "main",
        "actor_login": "owner",
        "trigger_comment_id": 77,
        "trigger_comment_kind": "issue",
        "command": {"name": "修复", "argument": "repair parser", "mode": "change"},
    }


class FakeServices:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def authorize_change(self, runtime: object) -> dict[str, object]:
        self.calls.append(("authorize-change", runtime))
        return {
            "run": run_binding(),
            "payload": {"status": "accepted", "data": {"request": {}}},
        }

    def prepare_change(self, artifact: dict[str, object], runtime: object) -> dict[str, object]:
        self.calls.append(("prepare-change", runtime))
        return {"status": "prepared", "data": {"request": {}, "manifest": {}}}

    def verify_change(self, artifact: dict[str, object], runtime: object) -> dict[str, object]:
        self.calls.append(("verify-change", runtime))
        return {
            "status": "verified",
            "data": {"request": {}, "manifest": {}, "attestation": {}},
        }

    def publish_change(self, artifact: dict[str, object], runtime: object) -> dict[str, object]:
        self.calls.append(("publish-change", runtime))
        return {"status": "completed", "data": {"publication": {}}}

    def record_change_result(self, artifact: dict[str, object], runtime: object) -> dict[str, object]:
        self.calls.append(("record-change-result", runtime))
        return {"status": "completed", "data": {"outcome": {}}}


class TestChangePhaseRouting(unittest.TestCase):
    def runtime(self, phase: str):
        from tools.qykw.change_phases import TrustedPhaseRuntime

        return TrustedPhaseRuntime(
            phase=phase,
            workflow_run_id=44,
            controller_sha=CONTROLLER_SHA,
            verification_profile="backend",
            image_digest=IMAGE_DIGEST if phase in {"verify-change", "publish-change"} else None,
            runner_temp=None,
        )

    def controller(self, phase: str, services: FakeServices | None = None):
        from tools.qykw.change_phases import ChangePhaseController

        return ChangePhaseController(
            phase,
            self.environment(phase),
            services=services or FakeServices(),
            runtime=self.runtime(phase),
        )

    @staticmethod
    def environment(phase: str) -> dict[str, str]:
        common = {
            "GITHUB_RUN_ID": "44",
            "GITHUB_SHA": CONTROLLER_SHA,
            "QYKW_VERIFICATION_PROFILE": "backend",
        }
        if phase in {"verify-change", "publish-change"}:
            common["QYKW_VERIFICATION_IMAGE_DIGEST"] = IMAGE_DIGEST
        credentials = {
            "authorize-change": {"QYKW_REVIEW_TOKEN": "review-token"},
            "prepare-change": {"QYKW_INFERENCE_API_KEY": "inference-key", "GITHUB_TOKEN": "read-token"},
            "verify-change": {"GITHUB_TOKEN": "read-token"},
            "publish-change": {"QYKW_PUBLISH_TOKEN": "publish-token", "RUNNER_TEMP": "C:\\runner\\temp"},
            "record-change-result": {"QYKW_REVIEW_TOKEN": "review-token"},
        }
        return {**common, **credentials[phase]}

    def artifact(self, phase: str, predecessor: dict[str, object] | None = None) -> dict[str, object]:
        from tools.qykw.__main__ import _change_artifact

        if predecessor is None and phase != "authorize-change":
            preceding = {
                "prepare-change": "authorize-change",
                "verify-change": "prepare-change",
                "publish-change": "verify-change",
                "record-change-result": "publish-change",
            }
            predecessor = self.artifact(preceding[phase])

        payloads = {
            "authorize-change": {"status": "accepted", "data": {"request": {}}},
            "prepare-change": {"status": "prepared", "data": {"request": {}, "manifest": {}}},
            "verify-change": {
                "status": "verified",
                "data": {"request": {}, "manifest": {}, "attestation": {}},
            },
            "publish-change": {"status": "completed", "data": {"publication": {}}},
            "record-change-result": {"status": "completed", "data": {"outcome": {}}},
        }
        runtime = self.runtime(phase)
        return _change_artifact(
            phase,
            run_binding(),
            payloads[phase],
            workflow_run_id=runtime.workflow_run_id,
            controller_sha=runtime.controller_sha,
            verification_profile=runtime.verification_profile,
            predecessor=predecessor,
        )

    def test_only_five_fixed_change_literals_map_to_five_unique_handlers(self) -> None:
        from tools.qykw.__main__ import _CHANGE_CLI_PHASES, _CHANGE_HANDLER_NAMES

        expected = {
            "authorize-change": "authorize_change",
            "prepare-change": "prepare_change",
            "verify-change": "verify_change",
            "publish-change": "publish_change",
            "record-change-result": "record_change_result",
        }
        self.assertEqual(_CHANGE_CLI_PHASES, frozenset(expected))
        self.assertEqual(_CHANGE_HANDLER_NAMES, expected)
        self.assertEqual(len(set(_CHANGE_HANDLER_NAMES.values())), 5)

    def test_each_controller_method_invokes_only_its_matching_service(self) -> None:
        from tools.qykw.__main__ import _CHANGE_HANDLER_NAMES

        services = FakeServices()
        authorize = self.controller("authorize-change", services)
        root = authorize.authorize_change()
        self.assertEqual(root["payload"]["status"], "accepted")

        previous = self.artifact("authorize-change")
        stages = (
            ("prepare-change", "prepare_change"),
            ("verify-change", "verify_change"),
            ("publish-change", "publish_change"),
            ("record-change-result", "record_change_result"),
        )
        for phase, method_name in stages:
            with self.subTest(phase=phase):
                controller = self.controller(phase, services)
                getattr(controller, method_name)(previous)
        self.assertEqual([name for name, _ in services.calls], list(_CHANGE_HANDLER_NAMES))

    def test_change_cli_rejects_alias_case_and_sixth_phase(self) -> None:
        from tools.qykw.__main__ import main

        for phase in ("Authorize-Change", "authorize_change", "change", "delete-change"):
            with self.subTest(phase=phase):
                self.assertEqual(main(["--phase", phase]), 2)


class TestChangeArtifactBoundary(TestChangePhaseRouting):
    def test_artifact_binds_schema_run_runtime_predecessor_file_purpose_and_digest(self) -> None:
        from tools.qykw.__main__ import _change_artifact, _validate_artifact

        authorize = self.artifact("authorize-change")
        prepare = _change_artifact(
            "prepare-change",
            run_binding(),
            {"status": "prepared", "data": {"request": {}, "manifest": {}}},
            workflow_run_id=44,
            controller_sha=CONTROLLER_SHA,
            verification_profile="backend",
            predecessor=authorize,
        )
        self.assertEqual(
            set(prepare),
            {
                "schema_version",
                "phase",
                "workflow_run_id",
                "run",
                "context_digest",
                "runtime",
                "predecessor",
                "file",
                "payload",
                "digest",
            },
        )
        self.assertEqual(prepare["predecessor"], {"phase": "authorize-change", "digest": authorize["digest"]})
        self.assertEqual(prepare["file"], {"name": "prepare-change.json", "purpose": "prepared-change-manifest"})
        _validate_artifact(prepare, expected_phase="prepare-change")

    def test_extra_missing_old_version_and_digest_tampering_fail_before_handler(self) -> None:
        from tools.qykw.__main__ import _run_phase

        original = self.artifact("authorize-change")
        mutations = (
            {**original, "extra": True},
            {key: value for key, value in original.items() if key != "runtime"},
            {**original, "schema_version": 0},
            {**original, "workflow_run_id": 45},
            {**original, "context_digest": "0" * 64},
            {**original, "digest": "0" * 64},
            {**original, "file": {"name": "wrong.json", "purpose": "authorized-change-request"}},
        )
        for artifact in mutations:
            with self.subTest(keys=tuple(artifact)):
                services = FakeServices()
                with self.assertRaises(ValueError):
                    _run_phase("prepare-change", artifact, self.controller("prepare-change", services), None)
                self.assertEqual(services.calls, [])

    def test_cross_phase_direct_publish_and_cross_run_runtime_are_rejected(self) -> None:
        from tools.qykw.__main__ import _run_phase

        authorize = self.artifact("authorize-change")
        with self.assertRaisesRegex(ValueError, "artifact_phase_mismatch"):
            _run_phase("publish-change", authorize, self.controller("publish-change"), None)

        other_run = self.artifact("authorize-change")
        other_run["workflow_run_id"] = 99
        with self.assertRaises(ValueError):
            _run_phase("prepare-change", other_run, self.controller("prepare-change"), None)

    def test_non_utf8_symlink_wrong_name_and_phase_limits_are_rejected(self) -> None:
        from tools.qykw import __main__ as entry

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            invalid = root / "authorize-change.json"
            invalid.write_bytes(b"\xff")
            with self.assertRaisesRegex(ValueError, "invalid_artifact_json"):
                entry._read_artifact(invalid)

            valid = root / "authorize-change.json"
            entry._write_artifact(valid, self.artifact("authorize-change"))
            wrong = root / "wrong.json"
            wrong.write_bytes(valid.read_bytes())
            with self.assertRaisesRegex(ValueError, "artifact_file_mismatch"):
                entry._read_artifact(wrong)

            link = root / "link.json"
            try:
                link.symlink_to(valid)
            except OSError:
                pass
            else:
                with self.assertRaisesRegex(ValueError, "unsafe_artifact_path"):
                    entry._read_artifact(link)

            with patch.dict(entry._CHANGE_ARTIFACT_LIMITS, {"authorize-change": 1}, clear=False):
                with self.assertRaisesRegex(ValueError, "artifact_too_large"):
                    entry._read_artifact(valid)

    def test_duplicate_json_keys_and_nonfinite_numbers_are_rejected(self) -> None:
        from tools.qykw.__main__ import _read_artifact

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            duplicate = root / "authorize-change.json"
            duplicate.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
            nonfinite = root / "prepare-change.json"
            nonfinite.write_text('{"value":NaN}', encoding="utf-8")
            for path in (duplicate, nonfinite):
                with self.subTest(path=path.name), self.assertRaisesRegex(ValueError, "invalid_artifact_json"):
                    _read_artifact(path)

    def test_change_cli_builds_digest_chain_instead_of_accepting_handler_envelopes(self) -> None:
        from tools.qykw.__main__ import _run_phase

        services = FakeServices()
        previous = self.artifact("authorize-change")
        result = _run_phase("prepare-change", previous, self.controller("prepare-change", services), None)
        self.assertEqual(result["phase"], "prepare-change")
        self.assertEqual(result["run"], previous["run"])
        self.assertEqual(result["predecessor"], {"phase": "authorize-change", "digest": previous["digest"]})
        self.assertNotEqual(result["digest"], previous["digest"])

    def test_change_cli_root_and_successor_use_fixed_file_names(self) -> None:
        from tools.qykw.__main__ import main

        services = FakeServices()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authorize_path = root / "authorize-change.json"
            prepare_path = root / "prepare-change.json"
            self.assertEqual(
                main(
                    ["--phase", "authorize-change", "--output", str(authorize_path)],
                    controller=self.controller("authorize-change", services),
                ),
                0,
            )
            self.assertEqual(
                main(
                    [
                        "--phase",
                        "prepare-change",
                        "--artifact",
                        str(authorize_path),
                        "--output",
                        str(prepare_path),
                    ],
                    controller=self.controller("prepare-change", services),
                ),
                0,
            )
            prepared = json.loads(prepare_path.read_text(encoding="utf-8"))
        self.assertEqual(prepared["phase"], "prepare-change")
        self.assertEqual([name for name, _ in services.calls], ["authorize-change", "prepare-change"])

    def test_skipped_root_artifact_has_no_run_or_context_digest(self) -> None:
        from tools.qykw.__main__ import _change_artifact

        artifact = _change_artifact(
            "authorize-change",
            None,
            {"status": "skipped", "data": {"reason": "not_a_change_command"}},
            workflow_run_id=44,
            controller_sha=CONTROLLER_SHA,
            verification_profile="backend",
            predecessor=None,
        )
        self.assertIsNone(artifact["run"])
        self.assertIsNone(artifact["context_digest"])

    def test_nested_payload_is_json_only_bounded_and_strictly_shaped(self) -> None:
        from tools.qykw.__main__ import _change_artifact

        authorize = self.artifact("authorize-change")
        valid = {
            "status": "prepared",
            "data": {"request": {"items": [None, True, 1, "text"]}, "manifest": {}},
        }
        _change_artifact(
            "prepare-change",
            run_binding(),
            valid,
            workflow_run_id=44,
            controller_sha=CONTROLLER_SHA,
            verification_profile="backend",
            predecessor=authorize,
        )
        invalid_payloads = (
            {"status": "prepared", "data": {"request": {}, "manifest": {}, "runtime": {}}},
            {"status": "wrong", "data": {"request": {}, "manifest": {}}},
            {"status": "prepared", "data": {"request": 1.5, "manifest": {}}},
            {"status": "skipped", "data": {"reason": "", "extra": True}},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaisesRegex(ValueError, "invalid_phase_payload"):
                _change_artifact(
                    "prepare-change",
                    run_binding(),
                    payload,
                    workflow_run_id=44,
                    controller_sha=CONTROLLER_SHA,
                    verification_profile="backend",
                    predecessor=authorize,
                )

    def test_every_envelope_rejection_branch_fails_before_dispatch(self) -> None:
        from tools.qykw import __main__ as entry

        authorize = self.artifact("authorize-change")
        prepare = self.artifact("prepare-change")
        cases = []
        for artifact, mutation, message in (
            (authorize, {"workflow_run_id": 0}, "invalid_workflow_run_id"),
            (authorize, {"runtime": {}}, "invalid_artifact_runtime"),
            (authorize, {"predecessor": {"phase": "authorize-change", "digest": "0" * 64}}, "invalid_artifact_predecessor"),
            (prepare, {"predecessor": None}, "invalid_artifact_predecessor"),
            (prepare, {"payload": None}, "invalid_phase_payload"),
        ):
            cases.append(({**artifact, **mutation}, message))
        for artifact, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                entry._validate_artifact(artifact)
        with self.assertRaisesRegex(ValueError, "artifact_phase_mismatch"):
            entry._validate_artifact(authorize, expected_phase="prepare-change")
        with self.assertRaisesRegex(ValueError, "invalid_change_phase"):
            entry._change_artifact(
                "unknown",
                run_binding(),
                {},
                workflow_run_id=44,
                controller_sha=CONTROLLER_SHA,
                verification_profile="backend",
                predecessor=None,
            )
        with self.assertRaisesRegex(ValueError, "artifact_phase_mismatch"):
            entry._change_artifact(
                "verify-change",
                run_binding(),
                {"status": "verified", "data": {"request": {}, "manifest": {}, "attestation": {}}},
                workflow_run_id=44,
                controller_sha=CONTROLLER_SHA,
                verification_profile="backend",
                predecessor=authorize,
            )

        nested: object = "leaf"
        for _ in range(34):
            nested = {"nested": nested}
        with self.assertRaisesRegex(ValueError, "invalid_phase_payload"):
            entry._change_artifact(
                "prepare-change",
                run_binding(),
                {"status": "prepared", "data": {"request": nested, "manifest": {}}},
                workflow_run_id=44,
                controller_sha=CONTROLLER_SHA,
                verification_profile="backend",
                predecessor=authorize,
            )
        with self.assertRaisesRegex(ValueError, "invalid_artifact_schema"):
            entry._change_artifact(
                "prepare-change",
                run_binding(),
                {"status": "prepared", "data": {"request": {"bad": {1}}, "manifest": {}}},
                workflow_run_id=44,
                controller_sha=CONTROLLER_SHA,
                verification_profile="backend",
                predecessor=authorize,
            )


class TestCredentialAndRuntimeIsolation(TestChangePhaseRouting):
    def test_missing_required_phase_credentials_fail_closed(self) -> None:
        from tools.qykw.change_phases import ChangePhaseController

        for phase, required in (
            ("authorize-change", "QYKW_REVIEW_TOKEN"),
            ("prepare-change", "QYKW_INFERENCE_API_KEY"),
            ("publish-change", "QYKW_PUBLISH_TOKEN"),
            ("record-change-result", "QYKW_REVIEW_TOKEN"),
        ):
            environment = self.environment(phase)
            del environment[required]
            with self.subTest(phase=phase), self.assertRaisesRegex(ValueError, "phase_credentials_unavailable"):
                ChangePhaseController(phase, environment, services=FakeServices(), runtime=self.runtime(phase))

    def test_every_foreign_qykw_variable_is_rejected_not_filtered(self) -> None:
        from tools.qykw.change_phases import ChangePhaseController

        for phase in (
            "authorize-change",
            "prepare-change",
            "verify-change",
            "publish-change",
            "record-change-result",
        ):
            environment = {**self.environment(phase), "QYKW_FOREIGN_TOKEN": "must-reject"}
            with self.subTest(phase=phase), self.assertRaisesRegex(ValueError, "unexpected_qykw_environment"):
                ChangePhaseController(phase, environment, services=FakeServices(), runtime=self.runtime(phase))

    def test_inference_review_and_publish_credentials_never_coexist(self) -> None:
        from tools.qykw.change_phases import ChangePhaseController

        pairs = (
            ("prepare-change", "QYKW_REVIEW_TOKEN"),
            ("prepare-change", "QYKW_PUBLISH_TOKEN"),
            ("authorize-change", "QYKW_INFERENCE_API_KEY"),
            ("publish-change", "QYKW_INFERENCE_API_KEY"),
            ("record-change-result", "QYKW_INFERENCE_API_KEY"),
        )
        for phase, foreign in pairs:
            with self.subTest(phase=phase, foreign=foreign), self.assertRaisesRegex(ValueError, "unexpected_qykw_environment"):
                ChangePhaseController(
                    phase,
                    {**self.environment(phase), foreign: "foreign-secret"},
                    services=FakeServices(),
                    runtime=self.runtime(phase),
                )

    def test_runtime_profile_image_and_workflow_run_come_from_trusted_metadata(self) -> None:
        services = FakeServices()
        controller = self.controller("verify-change", services)
        forged = self.artifact("prepare-change")
        forged["payload"]["data"]["runtime"] = {  # type: ignore[index]
            "workflow_run_id": 999,
            "verification_profile": "full",
            "image_digest": "sha256:" + "f" * 64,
        }
        from tools.qykw.__main__ import _change_artifact

        with self.assertRaisesRegex(ValueError, "invalid_phase_payload"):
            _change_artifact(
                "prepare-change",
                forged["run"],
                forged["payload"],
                workflow_run_id=44,
                controller_sha=CONTROLLER_SHA,
                verification_profile="backend",
                predecessor=self.artifact("authorize-change"),
            )
        controller.verify_change(self.artifact("prepare-change"))
        runtime = services.calls[-1][1]
        self.assertEqual(runtime.workflow_run_id, 44)
        self.assertEqual(runtime.verification_profile, "backend")
        self.assertEqual(runtime.image_digest, IMAGE_DIGEST)

    def test_publish_runtime_exposes_runner_temp_only_as_a_journal_factory_input(self) -> None:
        from tools.qykw.change_phases import build_change_controller

        with tempfile.TemporaryDirectory() as directory:
            environment = self.environment("publish-change")
            environment["RUNNER_TEMP"] = directory
            controller = build_change_controller(
                "publish-change", environment=environment, services=FakeServices()
            )
        self.assertEqual(controller.runtime.runner_temp, Path(directory).resolve())
        self.assertNotIn("runner_temp", json.dumps(self.artifact("publish-change")))

    def test_runtime_rejects_invalid_controller_owned_metadata(self) -> None:
        from tools.qykw.change_phases import TrustedPhaseRuntime

        mutations = (
            ("unknown", 44, CONTROLLER_SHA, "backend", None, None),
            ("authorize-change", 0, CONTROLLER_SHA, "backend", None, None),
            ("authorize-change", 44, "bad", "backend", None, None),
            ("authorize-change", 44, CONTROLLER_SHA, "unknown", None, None),
            ("authorize-change", 44, CONTROLLER_SHA, "backend", IMAGE_DIGEST, None),
            ("authorize-change", 44, CONTROLLER_SHA, "backend", None, Path("relative")),
            ("verify-change", 44, CONTROLLER_SHA, "backend", None, None),
            ("verify-change", 44, CONTROLLER_SHA, "backend", "sha256:bad", None),
        )
        for values in mutations:
            with self.subTest(values=values), self.assertRaises(ValueError):
                TrustedPhaseRuntime(*values)

    def test_controller_rejects_mismatched_runtime_and_ambiguous_factory(self) -> None:
        from tools.qykw.change_phases import ChangePhaseController

        with self.assertRaisesRegex(ValueError, "invalid_change_phase"):
            ChangePhaseController("unknown", {}, services=FakeServices(), runtime=self.runtime("authorize-change"))
        with self.assertRaisesRegex(ValueError, "phase_runtime_mismatch"):
            ChangePhaseController(
                "prepare-change",
                self.environment("prepare-change"),
                services=FakeServices(),
                runtime=self.runtime("authorize-change"),
            )
        with self.assertRaisesRegex(ValueError, "ambiguous_change_phase_dependencies"):
            ChangePhaseController(
                "authorize-change",
                self.environment("authorize-change"),
                services=FakeServices(),
                runtime=self.runtime("authorize-change"),
                factory=lambda *_: FakeServices(),
            )

    def test_factory_gets_only_read_only_narrow_environment(self) -> None:
        from types import MappingProxyType

        from tools.qykw.change_phases import build_change_controller

        captured: list[object] = []

        def factory(phase: str, environment: object, runtime: object) -> FakeServices:
            captured.extend((phase, environment, runtime))
            return FakeServices()

        controller = build_change_controller(
            "prepare-change",
            environment={**self.environment("prepare-change"), "UNRELATED_SECRET": "hidden"},
            runtime=self.runtime("prepare-change"),
            factory=factory,
        )
        self.assertIsInstance(captured[1], MappingProxyType)
        self.assertNotIn("UNRELATED_SECRET", captured[1])
        with self.assertRaises(TypeError):
            captured[1]["NEW"] = "blocked"  # type: ignore[index]
        controller.prepare_change(self.artifact("authorize-change"))

    def test_default_services_fail_closed_and_wrong_method_cannot_cross_dispatch(self) -> None:
        from tools.qykw.change_phases import ChangePhaseController

        controllers = {
            phase: ChangePhaseController(
                phase,
                self.environment(phase),
                runtime=self.runtime(phase),
            )
            for phase in (
                "authorize-change",
                "prepare-change",
                "verify-change",
                "publish-change",
                "record-change-result",
            )
        }
        calls = (
            lambda: controllers["authorize-change"].authorize_change(),
            lambda: controllers["prepare-change"].prepare_change(self.artifact("authorize-change")),
            lambda: controllers["verify-change"].verify_change(self.artifact("prepare-change")),
            lambda: controllers["publish-change"].publish_change(self.artifact("verify-change")),
            lambda: controllers["record-change-result"].record_change_result(self.artifact("publish-change")),
        )
        for call in calls:
            with self.assertRaisesRegex(ValueError, "change_phase_dependencies_unavailable"):
                call()
        with self.assertRaisesRegex(ValueError, "change_handler_phase_mismatch"):
            controllers["authorize-change"].prepare_change(self.artifact("authorize-change"))

    def test_environment_runtime_rejects_invalid_run_id_and_journal_root(self) -> None:
        from tools.qykw.change_phases import build_change_controller

        invalid_run = self.environment("authorize-change")
        invalid_run["GITHUB_RUN_ID"] = "not-an-int"
        with self.assertRaisesRegex(ValueError, "invalid_workflow_run_id"):
            build_change_controller("authorize-change", environment=invalid_run, services=FakeServices())
        invalid_journal = self.environment("publish-change")
        invalid_journal["RUNNER_TEMP"] = "missing-runner-temp"
        with self.assertRaisesRegex(ValueError, "invalid_publication_journal_root"):
            build_change_controller("publish-change", environment=invalid_journal, services=FakeServices())


class TestReviewChangeIsolation(unittest.TestCase):
    def test_review_root_and_publish_noop_on_change_before_any_service(self) -> None:
        from tools.qykw.phases import ProductionPhaseController, _run_from_artifact, _run_payload

        from tests.test_qykw_runner import event

        change_event = replace(
            event(CommandName.FIX),
            command=CommandRequest(CommandName.FIX, "repair", CommandMode.CHANGE),
        )
        self.assertIs(change_event.command.mode, CommandMode.CHANGE)
        controller = ProductionPhaseController("authorize", {})
        controller._event = lambda: (change_event, "")  # type: ignore[method-assign]
        controller._review_services = lambda: (_ for _ in ()).throw(AssertionError("side effect"))  # type: ignore[method-assign]
        result = controller.root()
        self.assertEqual(result["payload"], {"status": "skipped", "reason": "review_lane_noop"})

        run = _run_from_artifact({"run": run_binding()})
        self.assertIsNotNone(run)
        artifact = {
            "version": 1,
            "phase": "analyze",
            "run": _run_payload(run),
            "payload": {"kind": "none", "status": "canceled"},
        }
        publish = ProductionPhaseController("publish", {})
        publish._review_services = lambda: (_ for _ in ()).throw(AssertionError("side effect"))  # type: ignore[method-assign]
        published = publish.publish(artifact)
        self.assertEqual(published["payload"]["status"], "review_lane_noop")

    def test_review_analyze_and_failure_noop_on_change_before_any_service(self) -> None:
        from tools.qykw.phases import ProductionPhaseController, _run_from_artifact, _run_payload

        run = _run_from_artifact({"run": run_binding()})
        self.assertIsNotNone(run)
        artifact = {
            "version": 1,
            "phase": "authorize",
            "run": _run_payload(run),
            "payload": {"authorization": "accepted"},
        }

        analyze = ProductionPhaseController("analyze", {})
        analyze._read_services = lambda: (_ for _ in ()).throw(AssertionError("read service called"))  # type: ignore[method-assign]
        analyzed = analyze.analyze(artifact)
        self.assertEqual(analyzed["payload"], {"status": "skipped", "reason": "review_lane_noop"})

        failure = ProductionPhaseController("record-failure", {})
        failure._review_services = lambda: (_ for _ in ()).throw(AssertionError("review service called"))  # type: ignore[method-assign]
        recorded = failure.record_failure(artifact, "provider_failed")
        self.assertEqual(recorded["payload"], {"status": "skipped", "reason": "review_lane_noop"})

    def test_cli_unknown_exceptions_never_become_public_error_codes(self) -> None:
        from tools.qykw import __main__ as entry

        class FailingController:
            def __init__(self, error: Exception) -> None:
                self.error = error

            def root(self) -> dict[str, object]:
                raise self.error

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "authorize.json"
            for error in (
                ValueError("SENTINELTOKEN"),
                RuntimeError("SENTINELTOKEN"),
                OSError("SENTINELTOKEN"),
            ):
                with self.subTest(error=type(error).__name__):
                    stderr = io.StringIO()
                    with redirect_stderr(stderr):
                        result = entry.main(
                            ["--phase", "authorize", "--output", str(output)],
                            controller=FailingController(error),
                        )
                    self.assertEqual(result, 2)
                    self.assertEqual(stderr.getvalue(), "::error::phase_failed\n")
                    self.assertNotIn("SENTINELTOKEN", stderr.getvalue())

            stderr = io.StringIO()
            with redirect_stderr(stderr):
                result = entry.main(
                    ["--phase", "authorize", "--output", str(output)],
                    controller=FailingController(ValueError("artifact_required")),
                )
            self.assertEqual(result, 2)
            self.assertEqual(stderr.getvalue(), "::error::artifact_required\n")


if __name__ == "__main__":
    unittest.main()
