from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tools.qykw.change_phases import TrustedJobResults, TrustedPhaseRuntime


CONTROLLER_SHA = "c" * 40
IMAGE_DIGEST = "sha256:" + "d" * 64
IMAGE_REF = "ghcr.io/owner/qykw-verify@" + IMAGE_DIGEST


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
            patch.object(change_runtime, "_record_terminal") as record,
        ):
            result = service.record_change_result(artifact, runtime)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["data"]["outcome"]["failed_phase"], "verify-change")
        record.assert_called_once_with(context, "failed", "verify-change", {})


if __name__ == "__main__":
    unittest.main()
