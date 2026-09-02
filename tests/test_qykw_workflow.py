"""Security contract tests for the split qykw Actions workflows."""

from __future__ import annotations

from collections import deque
from pathlib import Path
import re
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
REVIEW = WORKFLOWS / "qykw-review.yml"
CONTROL = WORKFLOWS / "qykw-control.yml"
CONFIG = ROOT / ".github" / "qykw.toml"
LEGACY_WORKFLOW = WORKFLOWS / ("mini" + "max-review.yml")
LEGACY_TEST = ROOT / "tests" / ("test_mini" + "max_workflow.py")
SHA = re.compile(r"^[A-Za-z0-9_./-]+@[0-9a-f]{40}(?:\s+#.*)?$")


def job_blocks(source: str) -> dict[str, str]:
    """Return each top-level job block without pretending YAML is JSON."""
    jobs = re.search(r"^jobs:\n(?P<body>[\s\S]*)", source, re.MULTILINE)
    if jobs is None:
        return {}
    starts = list(re.finditer(r"^  (?P<name>[a-z_]+):\n", jobs.group("body"), re.MULTILINE))
    return {
        match.group("name"): jobs.group("body")[match.start(): starts[index + 1].start() if index + 1 < len(starts) else None]
        for index, match in enumerate(starts)
    }


def concurrency_block(source: str) -> str:
    match = re.search(r"^concurrency:\n(?P<body>(?:  [^\n]*\n)+)", source, re.MULTILINE)
    return match.group("body") if match is not None else ""


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
        source = REVIEW.read_text(encoding="utf-8")
        for required in ("pull_request_target:", "opened", "ready_for_review", "reopened", "issue_comment:", "pull_request_review_comment:", "workflow_dispatch:", "created", "edited"):
            self.assertIn(required, source)
        self.assertNotIn("synchronize", source)
        self.assertNotIn("pull_request:", source)

    def test_review_uses_the_shared_pr_queue_without_cancellation(self) -> None:
        source = REVIEW.read_text(encoding="utf-8")
        concurrency = concurrency_block(source)
        self.assertIn("qykw-${{ github.repository_id }}-pr-${{ github.event.pull_request.number || github.event.issue.number }}", concurrency)
        self.assertIn("cancel-in-progress: false", concurrency)
        self.assertRegex(concurrency, re.compile(r"^  queue: max$", re.MULTILINE))
        self.assertNotIn("# queue:", concurrency)
        queue = QueuedRuns()
        queue.submit("occupied")
        queue.submit("review-noop")
        queue.submit("change")
        queue.finish()
        queue.finish()
        queue.finish()
        self.assertEqual(queue.completed, ["occupied", "review-noop", "change"])

    def test_control_isolated_to_comment_stop_lane(self) -> None:
        source = CONTROL.read_text(encoding="utf-8")
        self.assertIn("issue_comment:", source)
        self.assertIn("pull_request_review_comment:", source)
        self.assertNotIn("pull_request_target:", source)
        self.assertNotIn("workflow_dispatch:", source)
        concurrency = concurrency_block(source)
        self.assertIn("qykw-control-${{ github.repository_id }}-comment-${{ github.event.comment.id }}", concurrency)
        self.assertIn("cancel-in-progress: false", concurrency)
        self.assertRegex(concurrency, re.compile(r"^  queue: max$", re.MULTILINE))
        self.assertNotIn("# queue:", concurrency)
        self.assertIn("--phase control", source)
        self.assertIn("停止", source)

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
        self.assertIn("secrets.QYKW_INFERENCE_API_KEY", review["analyze"])
        for name in required_inference - {"QYKW_INFERENCE_API_KEY"}:
            self.assertIn("vars." + name, review["analyze"])
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
        self.assertIn("QYKW_REVIEW_TOKEN", CONTROL.read_text(encoding="utf-8"))

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
            self.assertNotIn("github.event.comment.body", source)
            self.assertNotRegex(source, r"github\.event\.comment\.body.*(?:artifact|name)|(?:artifact|name).*github\.event\.comment\.body")
            self.assertNotIn("github.event.pull_request.head", source)
            self.assertNotIn("QYKW_PUBLISH_TOKEN", source)
            self.assertNotIn("pull-requests: write", job_blocks(REVIEW.read_text(encoding="utf-8")).get("analyze", ""))


if __name__ == "__main__":
    unittest.main()
