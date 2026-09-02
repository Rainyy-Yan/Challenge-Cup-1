"""Static security contract for the authorized qykw change workflow."""

from __future__ import annotations

from pathlib import Path
import inspect
import re
import unittest

import yaml

from tools.qykw.sandbox import DockerSandboxExecutor


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
CHANGE = WORKFLOWS / "qykw-change.yml"
REVIEW = WORKFLOWS / "qykw-review.yml"
ACTION_SHA = re.compile(r"^[A-Za-z0-9_./-]+@[0-9a-f]{40}$")
SHARED_GROUP = (
    "qykw-${{ github.repository_id }}-pr-${{ "
    "github.event.pull_request.number || github.event.issue.number || inputs.pr_number }}"
)


def load_workflow(path: Path) -> dict[str, object]:
    value = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(value, dict)
    return value


def jobs(path: Path) -> dict[str, dict[str, object]]:
    value = load_workflow(path).get("jobs")
    assert isinstance(value, dict)
    assert all(isinstance(job, dict) for job in value.values())
    return value


def job_source(name: str) -> str:
    source = CHANGE.read_text(encoding="utf-8")
    match = re.search(
        rf"^  {re.escape(name)}:\n(?P<body>[\s\S]*?)(?=^  [a-z_]+:\n|\Z)",
        source,
        re.MULTILINE,
    )
    assert match is not None
    return match.group(0)


def steps_using(job: dict[str, object], action: str) -> list[dict[str, object]]:
    steps = job.get("steps")
    assert isinstance(steps, list)
    return [step for step in steps if isinstance(step, dict) and str(step.get("uses", "")).startswith(action + "@")]


def named_step(job: dict[str, object], name: str) -> dict[str, object]:
    steps = job.get("steps")
    assert isinstance(steps, list)
    matches = [step for step in steps if isinstance(step, dict) and step.get("name") == name]
    assert len(matches) == 1
    return matches[0]


class TestQykwChangeWorkflow(unittest.TestCase):
    def test_change_listens_only_to_created_and_edited_pr_comments(self) -> None:
        triggers = load_workflow(CHANGE)["on"]
        self.assertEqual(triggers, {
            "issue_comment": {"types": ["created", "edited"]},
            "pull_request_review_comment": {"types": ["created", "edited"]},
        })
        authorize_if = " ".join(str(jobs(CHANGE)["authorize"]["if"]).split())
        self.assertIn("github.event.issue.pull_request", authorize_if)
        self.assertIn("github.event.comment.body", authorize_if)
        self.assertIn("@qykw", authorize_if)
        self.assertIn("修复", authorize_if)
        self.assertIn("实现", authorize_if)

    def test_change_has_the_fixed_five_job_phase_graph(self) -> None:
        workflow_jobs = jobs(CHANGE)
        self.assertEqual(list(workflow_jobs), [
            "authorize", "prepare", "verify", "publish", "record_result",
        ])
        self.assertNotIn("needs", workflow_jobs["authorize"])
        self.assertEqual(workflow_jobs["prepare"]["needs"], "authorize")
        self.assertEqual(workflow_jobs["verify"]["needs"], "prepare")
        self.assertEqual(workflow_jobs["publish"]["needs"], "verify")
        self.assertEqual(
            workflow_jobs["record_result"]["needs"],
            ["authorize", "prepare", "verify", "publish"],
        )
        self.assertEqual(
            workflow_jobs["record_result"]["if"],
            "${{ always() && needs.authorize.result == 'success' }}",
        )
        for job, phase in {
            "authorize": "authorize-change",
            "prepare": "prepare-change",
            "verify": "verify-change",
            "publish": "publish-change",
            "record_result": "record-change-result",
        }.items():
            block = job_source(job)
            self.assertEqual(block.count(f"python -m tools.qykw --phase {phase}"), 1)
            self.assertEqual(len(re.findall(r"python -m tools\.qykw --phase [a-z-]+", block)), 1)

    def test_permissions_are_explicit_and_minimal(self) -> None:
        workflow = load_workflow(CHANGE)
        self.assertEqual(workflow["permissions"], {"contents": "none"})
        workflow_jobs = jobs(CHANGE)
        self.assertEqual(workflow_jobs["authorize"]["permissions"], {"contents": "read"})
        self.assertEqual(workflow_jobs["prepare"]["permissions"], {
            "contents": "read", "pull-requests": "read", "issues": "read",
        })
        self.assertEqual(workflow_jobs["verify"]["permissions"], {
            "contents": "read", "issues": "read",
        })
        self.assertEqual(workflow_jobs["publish"]["permissions"], {"contents": "read"})
        self.assertEqual(workflow_jobs["record_result"]["permissions"], {"contents": "read"})

    def test_each_job_has_only_its_allowed_credentials(self) -> None:
        expected = {
            "authorize": {"QYKW_REVIEW_TOKEN"},
            "prepare": {"QYKW_INFERENCE_API_KEY"},
            "verify": set(),
            "publish": {"QYKW_PUBLISH_TOKEN"},
            "record_result": {"QYKW_REVIEW_TOKEN"},
        }
        for name, allowed in expected.items():
            block = job_source(name)
            found = set(re.findall(r"QYKW_[A-Z0-9_]+", block))
            found -= {
                "QYKW_CONFIG_PATH", "QYKW_VERIFICATION_PROFILE",
                "QYKW_VERIFICATION_IMAGE_REF",
                "QYKW_INFERENCE_BASE_URL", "QYKW_INFERENCE_MODEL",
                "QYKW_INFERENCE_ALLOWED_HOSTS", "QYKW_INFERENCE_CONTEXT_WINDOW",
                "QYKW_INFERENCE_MAX_OUTPUT_TOKENS", "QYKW_INFERENCE_TIMEOUT_SECONDS",
                "QYKW_AUTHORIZE_JOB_RESULT", "QYKW_PREPARE_JOB_RESULT",
                "QYKW_VERIFY_JOB_RESULT", "QYKW_PUBLISH_JOB_RESULT",
            }
            with self.subTest(job=name):
                self.assertEqual(found, allowed)
        self.assertIn("GITHUB_TOKEN: ${{ github.token }}", job_source("prepare"))
        self.assertIn("GITHUB_TOKEN: ${{ github.token }}", job_source("verify"))
        for name, step_name in (
            ("verify", "Verify in the fixed isolated sandbox"),
            ("publish", "Publish verified change"),
        ):
            environment = named_step(jobs(CHANGE)[name], step_name)["env"]
            self.assertEqual(
                environment["QYKW_VERIFICATION_IMAGE_REF"],
                "${{ vars.QYKW_VERIFICATION_IMAGE_REF }}",
            )
            self.assertNotIn("QYKW_VERIFICATION_IMAGE_DIGEST", environment)

    def test_controller_and_candidate_checkouts_are_credentialless_and_fixed(self) -> None:
        workflow_jobs = jobs(CHANGE)
        for name, job in workflow_jobs.items():
            controller = [
                step for step in steps_using(job, "actions/checkout")
                if step.get("with", {}).get("path") == "controller"
            ]
            with self.subTest(job=name):
                self.assertEqual(len(controller), 1)
                self.assertEqual(controller[0]["with"]["ref"], "${{ github.event.repository.default_branch }}")
                self.assertEqual(controller[0]["with"]["persist-credentials"], "false")
        candidate = [
            step for step in steps_using(workflow_jobs["verify"], "actions/checkout")
            if step.get("with", {}).get("path") == "candidate-source"
        ]
        self.assertEqual(len(candidate), 1)
        self.assertEqual(candidate[0]["with"]["repository"], "${{ needs.prepare.outputs.source_repository }}")
        self.assertEqual(candidate[0]["with"]["ref"], "${{ needs.prepare.outputs.source_head_sha }}")
        self.assertEqual(candidate[0]["with"]["persist-credentials"], "false")
        for name in ("authorize", "prepare", "publish", "record_result"):
            self.assertNotIn("candidate-source", job_source(name))

    def test_both_comment_events_freeze_the_actual_controller_checkout(self) -> None:
        workflow = load_workflow(CHANGE)
        self.assertEqual(set(workflow["on"]), {
            "issue_comment", "pull_request_review_comment",
        })
        workflow_jobs = jobs(CHANGE)
        freeze = str(named_step(workflow_jobs["authorize"], "Freeze trusted controller revision")["run"])
        self.assertIn('controller_sha="$(git rev-parse HEAD)"', freeze)
        self.assertNotIn("$GITHUB_SHA", freeze)

        phase_steps = {
            "authorize": ("Authorize exact change request", "steps.freeze_controller.outputs.sha"),
            "prepare": ("Prepare bounded manifest", "needs.authorize.outputs.controller_sha"),
            "verify": ("Verify in the fixed isolated sandbox", "needs.prepare.outputs.controller_sha"),
            "publish": ("Publish verified change", "needs.verify.outputs.controller_sha"),
            "record_result": ("Record bounded result", "needs.authorize.outputs.controller_sha"),
        }
        for job_name, (step_name, sha_source) in phase_steps.items():
            step = named_step(workflow_jobs[job_name], step_name)
            environment = step.get("env")
            self.assertIsInstance(environment, dict)
            with self.subTest(job=job_name):
                self.assertEqual(environment["TRUSTED_CONTROLLER_SHA"], "${{ " + sha_source + " }}")
                self.assertIn('GITHUB_SHA="$TRUSTED_CONTROLLER_SHA" python -m tools.qykw', str(step["run"]))

    def test_record_result_receives_all_trusted_job_outcome_enums(self) -> None:
        record = named_step(jobs(CHANGE)["record_result"], "Record bounded result")
        environment = record.get("env")
        self.assertIsInstance(environment, dict)
        self.assertEqual(
            {
                key: value
                for key, value in environment.items()
                if key.startswith("QYKW_") and key.endswith("_JOB_RESULT")
            },
            {
                "QYKW_AUTHORIZE_JOB_RESULT": "${{ needs.authorize.result }}",
                "QYKW_PREPARE_JOB_RESULT": "${{ needs.prepare.result }}",
                "QYKW_VERIFY_JOB_RESULT": "${{ needs.verify.result }}",
                "QYKW_PUBLISH_JOB_RESULT": "${{ needs.publish.result }}",
            },
        )
        self.assertNotIn("toJSON(needs)", str(record))

    def test_record_result_skips_without_an_authorized_run_but_survives_downstream_results(self) -> None:
        condition = str(jobs(CHANGE)["record_result"]["if"])
        self.assertEqual(
            condition,
            "${{ always() && needs.authorize.result == 'success' }}",
        )
        for authorize_result, expected in (
            ("success", True),
            ("failure", False),
            ("cancelled", False),
            ("skipped", False),
        ):
            with self.subTest(authorize_result=authorize_result):
                self.assertEqual(authorize_result == "success", expected)
        for downstream in ("prepare", "verify", "publish"):
            with self.subTest(downstream=downstream):
                self.assertNotIn(f"needs.{downstream}.result == 'success'", condition)

    def test_verify_is_networkless_and_mounts_no_sensitive_host_path(self) -> None:
        block = job_source("verify")
        self.assertIn("path: candidate-source", block)
        executable = "\n".join(
            line for line in block.splitlines() if not line.lstrip().startswith("#")
        )
        self.assertIn("python -m tools.qykw --phase verify-change", executable)
        self.assertNotIn("--network none", executable)
        sandbox_start = inspect.getsource(DockerSandboxExecutor._ensure_started)
        self.assertRegex(sandbox_start, r'"--network",\s+"none"')
        self.assertEqual(sandbox_start.count('"--mount"'), 1)
        for forbidden in (
            "-v controller", "-v ${{ runner.temp }}", "GITHUB_OUTPUT:/",
            "/var/run/docker.sock", "${{ runner.temp }}/qykw:/", "$HOME:/",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, block)
                self.assertNotIn(forbidden, sandbox_start)
        self.assertNotIn("secrets.", block)
        self.assertNotIn("docker run", block.casefold())

    def test_publish_never_runs_or_fetches_candidate_code(self) -> None:
        block = job_source("publish").casefold()
        for forbidden in (
            "candidate-source", "candidate-workspace", "provider", "docker",
            "unittest", "pytest", "npm test", "source_head_sha }}",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, block)
        self.assertIn("--phase publish-change", block)

    def test_actions_and_artifacts_are_immutable_and_short_lived(self) -> None:
        workflow = load_workflow(CHANGE)
        upload_count = 0
        for name, job in jobs(CHANGE).items():
            steps = job["steps"]
            assert isinstance(steps, list)
            for step in steps:
                assert isinstance(step, dict)
                if "uses" in step:
                    self.assertRegex(step["uses"], ACTION_SHA, name)
                if str(step.get("uses", "")).startswith("actions/upload-artifact@"):
                    upload_count += 1
                    self.assertEqual(step["with"]["retention-days"], "1")
                    self.assertIn("github.run_id", step["with"]["name"])
                    self.assertNotIn("*", str(step["with"]["path"]))
        self.assertGreaterEqual(upload_count, 4)
        self.assertNotIn("secrets.", str(workflow.get("env", {})))

    def test_review_and_change_share_a_lossless_pr_queue(self) -> None:
        change = load_workflow(CHANGE)["concurrency"]
        review = load_workflow(REVIEW)["concurrency"]
        expected = {"group": SHARED_GROUP, "cancel-in-progress": "false", "queue": "max"}
        self.assertEqual(change, expected)
        self.assertEqual(review, expected)

    def test_review_keeps_first_review_events_and_routes_change_to_controller_noop(self) -> None:
        review = load_workflow(REVIEW)
        self.assertEqual(review["on"]["pull_request_target"]["types"], [
            "opened", "ready_for_review", "reopened",
        ])
        self.assertNotIn("synchronize", str(review["on"]))
        authorize = jobs(REVIEW)["authorize"]
        self.assertIn("change mode exits before side effects", str(authorize.get("name", "")))
        self.assertIn("github.event.comment.body", str(authorize["if"]))

    def test_workflow_has_no_broad_write_or_external_service_surface(self) -> None:
        source = CHANGE.read_text(encoding="utf-8").casefold()
        for forbidden in (
            "pull_request_target", "workflow_dispatch", "workflow_run", "push:",
            "synchronize", "contents: write", "pull-requests: write", "issues: write",
            "merge", "approve", "delete", "update-ref", "update_ref", "force-push",
            "webhook", "http://", "https://", "curl ", "wget ",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
