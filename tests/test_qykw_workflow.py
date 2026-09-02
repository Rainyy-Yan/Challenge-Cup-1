"""Security contract tests for the split qykw Actions workflows."""

from __future__ import annotations

from collections import deque
from copy import deepcopy
from pathlib import Path
import re
import tomllib
import unittest
import yaml

from tools.qykw.domain import CommandName
from tools.qykw.triggers import normalize_event


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
REVIEW = WORKFLOWS / "qykw-review.yml"
CONTROL = WORKFLOWS / "qykw-control.yml"
CI = WORKFLOWS / "ci.yml"
CONFIG = ROOT / ".github" / "qykw.toml"
LEGACY_WORKFLOW = WORKFLOWS / ("mini" + "max-review.yml")
LEGACY_TEST = ROOT / "tests" / ("test_mini" + "max_workflow.py")
SHA = re.compile(r"^[A-Za-z0-9_./-]+@[0-9a-f]{40}(?:\s+#.*)?$")


def job_blocks(source: str) -> dict[str, str]:
    """Return each top-level job block without pretending YAML is JSON."""
    jobs = re.search(r"^jobs:\n(?P<body>[\s\S]*)", source, re.MULTILINE)
    if jobs is None:
        return {}
    starts = list(re.finditer(r"^  (?P<name>[a-z_-]+):\n", jobs.group("body"), re.MULTILINE))
    return {
        match.group("name"): jobs.group("body")[match.start(): starts[index + 1].start() if index + 1 < len(starts) else None]
        for index, match in enumerate(starts)
    }


def concurrency_block(source: str) -> str:
    match = re.search(r"^concurrency:\n(?P<body>(?:  [^\n]*\n)+)", source, re.MULTILINE)
    return match.group("body") if match is not None else ""


def workflow_mapping(path: Path) -> dict[str, object]:
    """Parse the active YAML structure without treating comments as configuration."""
    value = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(value, dict)
    return value


def workflow_jobs(path: Path) -> dict[str, dict[str, object]]:
    jobs = workflow_mapping(path).get("jobs")
    assert isinstance(jobs, dict)
    assert all(isinstance(job, dict) for job in jobs.values())
    return jobs


def compact_expression(value: object) -> str:
    return " ".join(str(value).split())


def comment_body_paths(value: object, path: tuple[object, ...] = ()) -> set[tuple[object, ...]]:
    if isinstance(value, dict):
        return set().union(*(comment_body_paths(item, path + (key,)) for key, item in value.items()))
    if isinstance(value, list):
        return set().union(*(comment_body_paths(item, path + (index,)) for index, item in enumerate(value)))
    return {path} if isinstance(value, str) and "github.event.comment.body" in value else set()


def assert_comment_body_is_only_in_job_if(workflow: dict[str, object], job: str) -> None:
    expected = {("jobs", job, "if")}
    actual = comment_body_paths(workflow)
    if actual != expected:
        raise AssertionError(f"comment body paths {actual!r} do not equal {expected!r}")


class QueuedRuns:
    """Small deterministic model of the required no-loss concurrency contract."""

    def __init__(self) -> None:
        self.active: str | None = None
        self.pending: deque[str] = deque()
        self.completed: list[str] = []

    def submit(self, run: str) -> None:
        if self.active is None:
            self.active = run
        else:
            self.pending.append(run)

    def finish(self) -> None:
        self.completed.append(self.active or "")
        self.active = self.pending.popleft() if self.pending else None


class TestQykwWorkflow(unittest.TestCase):
    def test_ci_has_an_isolated_no_secret_qykw_coverage_gate(self) -> None:
        jobs = workflow_jobs(CI)
        self.assertIn("qykw-coverage", jobs)
        job = jobs["qykw-coverage"]
        self.assertEqual(job["runs-on"], "ubuntu-latest")
        self.assertEqual(job["permissions"], {"contents": "read"})
        self.assertNotIn("secrets.", str(job))

        source = job_blocks(CI.read_text(encoding="utf-8"))["qykw-coverage"]
        self.assertIn('python-version: "3.11"', source)
        self.assertIn("python -m pip install --disable-pip-version-check -r requirements-dev.txt", source)
        self.assertIn('python -m coverage run --branch --source=tools.qykw -m unittest discover -s tests -p "test_qykw*.py" -v', source)
        self.assertIn("python -m coverage json -o qykw-coverage.json", source)
        self.assertIn("python tools/check_qykw_coverage.py qykw-coverage.json --line 95 --branch 90", source)

    def test_migration_replaces_the_legacy_workflow_and_test(self) -> None:
        self.assertTrue(REVIEW.is_file())
        self.assertTrue(CONTROL.is_file())
        self.assertFalse(LEGACY_WORKFLOW.exists())
        self.assertFalse(LEGACY_TEST.exists())

    def test_default_configuration_matches_the_confirmed_phase_one_policy(self) -> None:
        config = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
        self.assertEqual(config, {
            "version": 1,
            "language": "zh-CN",
            "authorization": {"code_writers": ["xyh202131"]},
            "review": {"auto_initial": True, "auto_on_synchronize": False, "max_findings": 20, "run_timeout_seconds": 900},
            "context": {"safety_reserve_ratio": 0.20, "max_chunk_ratio": 0.25},
            "commands": {"enabled": ["帮助", "分析", "计划", "审查", "复审", "状态", "总结", "修复", "实现", "停止"]},
            "verification": {"required_checks": [], "profiles": ["backend", "frontend", "full"]},
        })

    def test_review_subscribes_only_to_initial_pr_events_and_allowed_comment_events(self) -> None:
        triggers = workflow_mapping(REVIEW)["on"]
        self.assertEqual(set(triggers), {
            "pull_request_target", "issue_comment", "pull_request_review_comment", "workflow_dispatch",
        })
        self.assertEqual(triggers["pull_request_target"]["types"], ["opened", "ready_for_review", "reopened"])
        self.assertEqual(triggers["issue_comment"]["types"], ["created", "edited"])
        self.assertEqual(triggers["pull_request_review_comment"]["types"], ["created", "edited"])

    def test_manual_inputs_are_active_bounded_read_only_choices_and_share_the_pr_queue(self) -> None:
        workflow = workflow_mapping(REVIEW)
        dispatch = workflow["on"]["workflow_dispatch"]
        self.assertIsInstance(dispatch, dict)
        inputs = dispatch["inputs"]
        self.assertEqual(inputs["pr_number"], {
            "description": "Pull request number (1 to 999999999999999999)",
            "required": "true",
            "type": "number",
        })
        self.assertEqual(inputs["command"], {
            "description": "Read-only qykw command",
            "required": "true",
            "type": "choice",
            "options": ["帮助", "分析", "计划", "审查", "复审", "状态", "总结"],
        })
        concurrency = concurrency_block(REVIEW.read_text(encoding="utf-8"))
        self.assertIn("github.event.pull_request.number || github.event.issue.number || inputs.pr_number", concurrency)
        self.assertNotIn("修复", REVIEW.read_text(encoding="utf-8"))
        self.assertNotIn("实现", REVIEW.read_text(encoding="utf-8"))

    def test_manual_workflow_input_normalizes_to_the_selected_pr_and_read_only_command(self) -> None:
        event = normalize_event(
            "workflow_dispatch",
            {"inputs": {"pr_number": "23", "command": "复审"}, "sender": {"login": "maintainer"}},
            repository_id=7,
            repository="owner/repository",
            workflow_run_id=812,
        )
        self.assertIsNotNone(event)
        self.assertEqual(event.pr_number if event else None, 23)
        self.assertEqual(event.command.name if event else None, CommandName.REREVIEW)
        self.assertEqual(event.command.mode.value if event else None, "read_only")

    def test_review_uses_the_shared_pr_queue_without_cancellation(self) -> None:
        concurrency = workflow_mapping(REVIEW)["concurrency"]
        self.assertEqual(concurrency, {
            "group": "qykw-${{ github.repository_id }}-pr-${{ github.event.pull_request.number || github.event.issue.number || inputs.pr_number }}",
            "cancel-in-progress": "false",
            "queue": "max",
        })
        queue = QueuedRuns()
        queue.submit("occupied")
        queue.submit("review-noop")
        queue.submit("change")
        queue.finish()
        queue.finish()
        queue.finish()
        self.assertEqual(queue.completed, ["occupied", "review-noop", "change"])

    def test_control_isolated_to_comment_stop_lane(self) -> None:
        workflow = workflow_mapping(CONTROL)
        self.assertEqual(set(workflow["on"]), {"issue_comment", "pull_request_review_comment"})
        self.assertEqual(workflow["on"]["issue_comment"]["types"], ["created", "edited"])
        self.assertEqual(workflow["on"]["pull_request_review_comment"]["types"], ["created", "edited"])
        self.assertEqual(workflow["concurrency"], {
            "group": "qykw-control-${{ github.repository_id }}-comment-${{ github.event.comment.id }}",
            "cancel-in-progress": "false",
            "queue": "max",
        })
        self.assertEqual(compact_expression(workflow_jobs(CONTROL)["control"]["if"]), "(github.event_name == 'issue_comment' && github.event.issue.pull_request && contains(github.event.comment.body, '@qykw') && contains(github.event.comment.body, '停止')) || (github.event_name == 'pull_request_review_comment' && contains(github.event.comment.body, '@qykw') && contains(github.event.comment.body, '停止'))")

    def test_active_comment_prefilters_are_exact_and_comment_body_is_not_executable_data(self) -> None:
        review = workflow_mapping(REVIEW)
        control = workflow_mapping(CONTROL)
        self.assertEqual(compact_expression(workflow_jobs(REVIEW)["authorize"]["if"]), "github.event_name == 'pull_request_target' || github.event_name == 'workflow_dispatch' || (github.event_name == 'issue_comment' && github.event.issue.pull_request && contains(github.event.comment.body, '@qykw')) || (github.event_name == 'pull_request_review_comment' && contains(github.event.comment.body, '@qykw'))")
        self.assertEqual(compact_expression(workflow_jobs(CONTROL)["control"]["if"]), "(github.event_name == 'issue_comment' && github.event.issue.pull_request && contains(github.event.comment.body, '@qykw') && contains(github.event.comment.body, '停止')) || (github.event_name == 'pull_request_review_comment' && contains(github.event.comment.body, '@qykw') && contains(github.event.comment.body, '停止'))")
        assert_comment_body_is_only_in_job_if(review, "authorize")
        assert_comment_body_is_only_in_job_if(control, "control")

    def test_comment_body_path_guard_rejects_unconditional_filters_and_step_payloads(self) -> None:
        review = workflow_mapping(REVIEW)
        unconditional = deepcopy(review)
        unconditional["jobs"]["authorize"]["if"] = "true"
        with self.assertRaises(AssertionError):
            assert_comment_body_is_only_in_job_if(unconditional, "authorize")
        executable_payload = deepcopy(review)
        executable_payload["jobs"]["authorize"]["steps"][2]["run"] += " ${{ github.event.comment.body }}"
        with self.assertRaises(AssertionError):
            assert_comment_body_is_only_in_job_if(executable_payload, "authorize")

    def test_root_phases_create_outputs_without_impossible_input_artifacts(self) -> None:
        review = job_blocks(REVIEW.read_text(encoding="utf-8"))
        control = job_blocks(CONTROL.read_text(encoding="utf-8"))
        for job, phase in ((review["authorize"], "authorize"), (control["control"], "control")):
            with self.subTest(phase=phase):
                self.assertIn('mkdir -p "$RUNNER_TEMP/qykw"', job)
                command = re.search(r"python -m tools\.qykw --phase " + phase + r"[^\n]+", job)
                self.assertIsNotNone(command)
                self.assertNotIn("--artifact", command.group(0) if command else "")
                self.assertIn("--output", command.group(0) if command else "")
                self.assertNotIn("QYKW_INPUT_ARTIFACT", job)

    def test_every_action_is_commit_pinned_and_every_python_job_uses_trusted_controller(self) -> None:
        for workflow in (REVIEW, CONTROL):
            source = workflow.read_text(encoding="utf-8")
            jobs = job_blocks(source)
            self.assertTrue(jobs)
            self.assertIn("contents: none", source)
            for line in (line.strip() for line in source.splitlines() if "uses:" in line):
                self.assertRegex(line.partition("uses: ")[2], SHA)
            for name, job in jobs.items():
                with self.subTest(workflow=workflow.name, job=name):
                    self.assertIn("timeout-minutes: 15", job)
                    self.assertIn("permissions:", job)
                    self.assertIn("ref: ${{ github.event.repository.default_branch }}", job)
                    self.assertIn("path: controller", job)
                    self.assertIn("persist-credentials: false", job)
                    self.assertIn("working-directory: controller", job)
                    self.assertIn("python -m tools.qykw --phase", job)

    def test_job_credentials_and_phase_graph_are_strictly_separated(self) -> None:
        review = job_blocks(REVIEW.read_text(encoding="utf-8"))
        self.assertEqual(set(review), {"authorize", "analyze", "publish", "record_failure"})
        self.assertIn("QYKW_REVIEW_TOKEN", review["authorize"])
        self.assertIn("QYKW_REVIEW_TOKEN", review["publish"])
        self.assertIn("QYKW_REVIEW_TOKEN", review["record_failure"])
        self.assertNotIn("QYKW_INFERENCE_", review["authorize"] + review["publish"] + review["record_failure"])
        self.assertIn("QYKW_INFERENCE_", review["analyze"])
        self.assertIn("GITHUB_TOKEN", review["analyze"])
        self.assertNotIn("QYKW_REVIEW_TOKEN", review["analyze"])
        required_inference = {
            "QYKW_INFERENCE_API_KEY", "QYKW_INFERENCE_BASE_URL", "QYKW_INFERENCE_MODEL",
            "QYKW_INFERENCE_ALLOWED_HOSTS", "QYKW_INFERENCE_CONTEXT_WINDOW",
            "QYKW_INFERENCE_MAX_OUTPUT_TOKENS", "QYKW_INFERENCE_TIMEOUT_SECONDS",
        }
        self.assertTrue(required_inference.issubset(set(re.findall(r"QYKW_INFERENCE_[A-Z_]+", review["analyze"]))))
        self.assertIn("secrets.MINIMAX_API_KEY", review["analyze"])
        self.assertNotIn("secrets.QYKW_INFERENCE_API_KEY", review["analyze"])
        for name in required_inference - {"QYKW_INFERENCE_API_KEY"}:
            self.assertIn("vars." + name, review["analyze"])
        expected_secret_sources = {
            "authorize": {"QYKW_TOKEN"},
            "analyze": {"MINIMAX_API_KEY"},
            "publish": {"QYKW_TOKEN"},
            "record_failure": {"QYKW_TOKEN"},
        }
        for name, expected in expected_secret_sources.items():
            with self.subTest(secret_sources=name):
                self.assertEqual(
                    set(re.findall(r"secrets\.([A-Z0-9_]+)", review[name])),
                    expected,
                )
        self.assertIn("--phase authorize", review["authorize"])
        self.assertIn("--phase analyze", review["analyze"])
        self.assertIn("--phase publish", review["publish"])
        self.assertIn("--phase record-failure", review["record_failure"])
        self.assertIn("needs: authorize", review["analyze"])
        self.assertIn("needs: analyze", review["publish"])
        self.assertIn("needs: [authorize, analyze, publish]", review["record_failure"])
        self.assertIn("needs.authorize.result == 'success'", review["record_failure"])
        self.assertIn("needs.analyze.result == 'failure'", review["record_failure"])
        self.assertIn("needs.publish.result == 'failure'", review["record_failure"])
        self.assertNotIn("needs.authorize.result == 'failure'", review["record_failure"])
        self.assertNotIn("--phase publish", review["record_failure"])
        self.assertNotIn("QYKW_INFERENCE_", CONTROL.read_text(encoding="utf-8"))
        control = job_blocks(CONTROL.read_text(encoding="utf-8"))["control"]
        self.assertIn("QYKW_REVIEW_TOKEN", control)
        self.assertEqual(
            set(re.findall(r"secrets\.([A-Z0-9_]+)", control)),
            {"QYKW_TOKEN"},
        )

    def test_job_permissions_are_complete_read_only_and_do_not_gain_unneeded_scopes(self) -> None:
        review = workflow_jobs(REVIEW)
        self.assertEqual(review["authorize"]["permissions"], {"contents": "read"})
        self.assertEqual(review["analyze"]["permissions"], {
            "contents": "read", "pull-requests": "read", "checks": "read",
        })
        self.assertEqual(review["publish"]["permissions"], {"contents": "read"})
        self.assertEqual(review["record_failure"]["permissions"], {"contents": "read"})
        self.assertEqual(workflow_jobs(CONTROL)["control"]["permissions"], {"contents": "read"})

    def test_artifact_handoffs_use_real_output_names_and_failure_uses_available_predecessors(self) -> None:
        review = job_blocks(REVIEW.read_text(encoding="utf-8"))
        self.assertIn('name: qykw-${{ github.run_id }}-authorize-v1', review["authorize"])
        self.assertIn('name: qykw-${{ github.run_id }}-authorize-v1', review["analyze"])
        self.assertIn('path: ${{ runner.temp }}/qykw', review["analyze"])
        self.assertIn('name: qykw-${{ github.run_id }}-analyze-v1', review["analyze"])
        self.assertIn('name: qykw-${{ github.run_id }}-analyze-v1', review["publish"])
        self.assertIn('path: ${{ runner.temp }}/qykw', review["publish"])
        self.assertIn("needs.analyze.outputs.analysis_ready == 'true'", review["publish"])
        self.assertIn("Download authorize artifact for failure", review["record_failure"])
        self.assertIn("Download analysis artifact for failure", review["record_failure"])
        self.assertIn("--artifact \"$QYKW_INPUT_ARTIFACT\"", review["record_failure"])
        self.assertNotIn("${{ runner.temp }}/qykw/request.json", REVIEW.read_text(encoding="utf-8"))
        self.assertNotIn("${{ runner.temp }}/qykw/control.json", CONTROL.read_text(encoding="utf-8"))

    def test_artifacts_are_short_lived_versioned_and_safe(self) -> None:
        for workflow in (REVIEW, CONTROL):
            source = workflow.read_text(encoding="utf-8")
            self.assertIn("retention-days: 1", source)
            self.assertIn("github.run_id", source)
            self.assertNotRegex(source, r"(?:run|path|name):[^\n]*github\.event\.comment\.body")
            self.assertNotIn("github.event.pull_request.head", source)
            self.assertNotIn("QYKW_PUBLISH_TOKEN", source)
            self.assertNotIn("pull-requests: write", job_blocks(REVIEW.read_text(encoding="utf-8")).get("analyze", ""))


if __name__ == "__main__":
    unittest.main()
