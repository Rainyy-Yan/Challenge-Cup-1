"""Tests for deterministic qykw event trigger decisions."""

from __future__ import annotations

import unittest

from tools.qykw.config import parse_qykw_config
from tools.qykw.domain import (
    CommandMode,
    CommandName,
    CommandRequest,
    CommentKind,
    PullRef,
    RunContext,
    RunRecord,
    RunStage,
    RunStatus,
)
from tools.qykw.triggers import (
    build_run_context,
    decide_trigger,
    make_run_id,
    normalize_event,
)


def config():
    """Return the default strict configuration fixture."""

    return parse_qykw_config({"version": 1})


def pull_payload(
    action: str,
    *,
    draft: bool,
    repository_id: int = 7,
    pr_number: int = 23,
    head_sha: str = "head-abc123",
) -> dict[str, object]:
    """Return the relevant pull-request webhook facts."""

    return {
        "action": action,
        "sender": {"login": "contributor"},
        "repository": {"id": repository_id, "full_name": "owner/repository"},
        "pull_request": {
            "number": pr_number,
            "draft": draft,
            "head": {"sha": head_sha},
        },
    }


def pull_event(action: str, *, draft: bool, **kwargs: object):
    """Normalize a pull-request fixture into an event context."""

    event = normalize_event(
        "pull_request",
        pull_payload(action, draft=draft, **kwargs),
        repository_id=7,
        repository="owner/repository",
    )
    assert event is not None
    return event


def trusted_pull(
    *,
    number: int = 23,
    state: str = "open",
    source_repository: str = "fork/repository",
    source_head_sha: str = "trusted-pr-head",
    target_repository: str = "owner/repository",
    target_base_sha: str = "trusted-base",
    target_base_ref: str = "main",
) -> PullRef:
    """Return a complete pull reference from the trusted gateway."""

    return PullRef(
        number=number,
        state=state,
        draft=False,
        source_repository=source_repository,
        source_head_sha=source_head_sha,
        target_repository=target_repository,
        target_base_sha=target_base_sha,
        target_base_ref=target_base_ref,
    )


def run_for(event) -> RunRecord:
    """Return a record that represents an existing idempotent run."""

    context = RunContext(
        run_id=make_run_id(event.pr_number, event.idempotency_key),
        idempotency_key=event.idempotency_key,
        repository_id=event.repository_id,
        repository=event.repository,
        pr_number=event.pr_number,
        event_name=event.event_name,
        event_action=event.action,
        source_repository="fork/repository",
        source_head_sha="trusted-pr-head",
        target_base_sha="trusted-base",
        target_base_ref="main",
        command=event.command,
        trigger_actor=event.actor_login,
        trigger_comment_id=event.trigger_comment_id,
        trigger_comment_kind=event.trigger_comment_kind,
    )
    return RunRecord(
        context=context,
        stage=RunStage.COMPLETED,
        status=RunStatus.COMPLETED,
        prompt_version="test",
        summary_comment_id=None,
        initial_review=True,
        coverage=None,
        warning_codes=(),
        error_code=None,
        created_at="2026-09-02T00:00:00Z",
        updated_at="2026-09-02T00:00:00Z",
    )


class TestAutomaticInitialReviewTriggers(unittest.TestCase):
    def test_opened_non_draft_runs_once(self) -> None:
        event = pull_event("opened", draft=False, head_sha="abc123")

        first = decide_trigger(
            event, existing_run=None, initial_review_completed=False, config=config()
        )
        replay = decide_trigger(
            event,
            existing_run=run_for(event),
            initial_review_completed=False,
            config=config(),
        )

        self.assertTrue(first.run)
        self.assertEqual(first.reason, "initial_opened")
        self.assertFalse(replay.run)
        self.assertEqual(replay.reason, "duplicate")

    def test_opened_draft_never_auto_reviews(self) -> None:
        decision = decide_trigger(
            pull_event("opened", draft=True),
            existing_run=None,
            initial_review_completed=False,
            config=config(),
        )

        self.assertFalse(decision.run)
        self.assertEqual(decision.reason, "draft_opened")

    def test_first_ready_for_review_runs_and_replay_does_not(self) -> None:
        event = pull_event("ready_for_review", draft=False, head_sha="ready-head")

        first = decide_trigger(
            event, existing_run=None, initial_review_completed=False, config=config()
        )
        replay = decide_trigger(
            event,
            existing_run=run_for(event),
            initial_review_completed=False,
            config=config(),
        )

        self.assertTrue(first.run)
        self.assertEqual(first.reason, "initial_ready_for_review")
        self.assertFalse(replay.run)
        self.assertEqual(replay.reason, "duplicate")

    def test_ready_for_review_after_success_never_runs_again(self) -> None:
        decision = decide_trigger(
            pull_event("ready_for_review", draft=False),
            existing_run=None,
            initial_review_completed=True,
            config=config(),
        )

        self.assertFalse(decision.run)
        self.assertEqual(decision.reason, "initial_review_completed")

    def test_synchronize_never_auto_reviews(self) -> None:
        event = pull_event("synchronize", draft=False, head_sha="def456")

        decision = decide_trigger(
            event, existing_run=None, initial_review_completed=False, config=config()
        )

        self.assertFalse(decision.run)
        self.assertEqual(decision.reason, "automatic_event_ignored")

    def test_reopened_runs_only_when_initial_review_never_succeeded(self) -> None:
        event = pull_event("reopened", draft=False)
        missing_initial = decide_trigger(
            event, existing_run=None, initial_review_completed=False, config=config()
        )
        completed_initial = decide_trigger(
            event, existing_run=None, initial_review_completed=True, config=config()
        )

        self.assertTrue(missing_initial.run)
        self.assertEqual(missing_initial.reason, "initial_reopened")
        self.assertFalse(completed_initial.run)
        self.assertEqual(completed_initial.reason, "initial_review_completed")


class TestCommentAndManualTriggers(unittest.TestCase):
    def test_ordinary_issue_comment_is_not_a_pull_request_event(self) -> None:
        event = normalize_event(
            "issue_comment",
            {
                "action": "created",
                "sender": {"login": "member"},
                "issue": {"number": 31},
                "comment": {"id": 101, "body": "@qykw 复审"},
            },
            repository_id=7,
            repository="owner/repository",
        )

        self.assertIsNone(event)

    def test_comment_rereview_is_deterministic_and_idempotent(self) -> None:
        event = normalize_event(
            "issue_comment",
            {
                "action": "created",
                "sender": {"login": "member"},
                "issue": {"number": 23, "pull_request": {"url": "trusted-later"}},
                "comment": {"id": 101, "body": "@qykw 复审"},
                "GITHUB_SHA": "default-branch-sha",
            },
            repository_id=7,
            repository="owner/repository",
        )
        assert event is not None

        first = decide_trigger(
            event, existing_run=None, initial_review_completed=True, config=config()
        )
        replay = decide_trigger(
            event,
            existing_run=run_for(event),
            initial_review_completed=True,
            config=config(),
        )

        self.assertEqual(event.source_head_hint, None)
        self.assertEqual(event.idempotency_key, "comment:7:issue:101")
        self.assertTrue(first.run)
        self.assertEqual(first.reason, "explicit_rereview")
        self.assertFalse(replay.run)

    def test_review_comment_does_not_take_actions_sha_as_source_hint(self) -> None:
        event = normalize_event(
            "pull_request_review_comment",
            {
                "action": "created",
                "sender": {"login": "member"},
                "pull_request": {"number": 23, "head": {"sha": "untrusted"}},
                "comment": {"id": 202, "body": "@qykw 审查 全部"},
                "GITHUB_SHA": "default-branch-sha",
            },
            repository_id=7,
            repository="owner/repository",
        )
        assert event is not None

        self.assertEqual(event.source_head_hint, None)
        self.assertEqual(event.idempotency_key, "comment:7:review:202")

    def test_manual_event_key_uses_workflow_run_id(self) -> None:
        event = normalize_event(
            "workflow_dispatch",
            {
                "inputs": {"pr_number": "23", "command": "审查"},
                "sender": {"login": "maintainer"},
            },
            repository_id=7,
            repository="owner/repository",
            workflow_run_id=812,
        )
        assert event is not None

        decision = decide_trigger(
            event, existing_run=None, initial_review_completed=True, config=config()
        )

        self.assertEqual(event.idempotency_key, "manual:7:812")
        self.assertTrue(decision.run)
        self.assertEqual(decision.reason, "manual")

    def test_manual_event_rejects_non_ascii_decimal_pr_numbers(self) -> None:
        invalid_numbers = ("²", "１２", "٢٣", "+23", " 23", "23 ", "", "-23", "0")

        for pr_number in invalid_numbers:
            with self.subTest(pr_number=pr_number):
                self.assertIsNone(
                    normalize_event(
                        "workflow_dispatch",
                        {
                            "inputs": {"pr_number": pr_number, "command": "审查"},
                            "sender": {"login": "maintainer"},
                        },
                        repository_id=7,
                        repository="owner/repository",
                        workflow_run_id=812,
                    )
                )


class TestTrustedPullBinding(unittest.TestCase):
    def test_build_run_context_uses_trusted_pull_ref_and_preserves_event(self) -> None:
        event = normalize_event(
            "issue_comment",
            {
                "action": "edited",
                "sender": {"login": "member"},
                "issue": {"number": 23, "pull_request": {"url": "trusted-later"}},
                "comment": {"id": 101, "body": "@qykw 复审"},
                "GITHUB_SHA": "default-branch-sha",
            },
            repository_id=7,
            repository="owner/repository",
        )
        assert event is not None

        context = build_run_context(event, trusted_pull())

        assert context is not None
        self.assertEqual(context.source_repository, "fork/repository")
        self.assertEqual(context.source_head_sha, "trusted-pr-head")
        self.assertEqual(context.target_base_sha, "trusted-base")
        self.assertEqual(context.target_base_ref, "main")
        self.assertEqual(context.event_name, "issue_comment")
        self.assertEqual(context.event_action, "edited")

    def test_invalid_trusted_pull_refs_do_not_create_context(self) -> None:
        event = pull_event("opened", draft=False)
        cases = (
            trusted_pull(state="closed"),
            trusted_pull(state="merged"),
            trusted_pull(number=24),
            trusted_pull(target_repository="other/repository"),
            trusted_pull(source_repository=""),
            trusted_pull(source_head_sha=""),
            trusted_pull(target_base_sha=""),
            trusted_pull(target_base_ref=""),
        )

        for pull in cases:
            with self.subTest(pull=pull):
                self.assertIsNone(build_run_context(event, pull))

    def test_missing_trusted_pull_ref_does_not_create_context(self) -> None:
        self.assertIsNone(build_run_context(pull_event("opened", draft=False), None))

    def test_run_id_is_stable_and_scoped_by_pull_and_idempotency_key(self) -> None:
        first = make_run_id(23, "comment:7:issue:101")
        sample = [make_run_id(23, f"comment:7:issue:{index}") for index in range(512)]

        self.assertEqual(first, make_run_id(23, "comment:7:issue:101"))
        self.assertNotEqual(first, make_run_id(24, "comment:7:issue:101"))
        self.assertNotEqual(first, make_run_id(23, "comment:7:issue:102"))
        self.assertRegex(first, r"^QY-PR23-[0-9A-F]{32}$")
        self.assertEqual(len(sample), len(set(sample)))


if __name__ == "__main__":
    unittest.main()
