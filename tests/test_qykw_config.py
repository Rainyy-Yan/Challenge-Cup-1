"""Tests for qykw configuration parsing and shared domain contracts."""

from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tools.qykw import domain
from tools.qykw.config import ConfigError, load_qykw_config, parse_qykw_config
from tools.qykw.domain import CommandName


class TestQykwConfig(unittest.TestCase):
    def test_rejects_unknown_and_secret_fields(self) -> None:
        with self.assertRaises(ConfigError):
            parse_qykw_config({"version": 1, "api_key": "forbidden"})

    def test_accepts_confirmed_defaults(self) -> None:
        config = parse_qykw_config({"version": 1})
        self.assertTrue(config.review.auto_initial)
        self.assertFalse(config.review.auto_on_synchronize)
        self.assertEqual(config.language, "zh-CN")

    def test_rejects_unknown_nested_fields(self) -> None:
        with self.assertRaises(ConfigError):
            parse_qykw_config({"version": 1, "review": {"timeout": 10}})

    def test_rejects_empty_code_writer_list(self) -> None:
        with self.assertRaises(ConfigError):
            parse_qykw_config(
                {"version": 1, "authorization": {"code_writers": []}}
            )

    def test_rejects_invalid_context_ratios(self) -> None:
        with self.assertRaises(ConfigError):
            parse_qykw_config(
                {"version": 1, "context": {"safety_reserve_ratio": 1.0}}
            )

    def test_rejects_unknown_command(self) -> None:
        with self.assertRaises(ConfigError):
            parse_qykw_config(
                {"version": 1, "commands": {"enabled": ["不存在"]}}
            )

    def test_rejects_non_v1_versions(self) -> None:
        for version in (0, 2):
            with self.subTest(version=version):
                with self.assertRaises(ConfigError):
                    parse_qykw_config({"version": version})

    def test_rejects_context_ratio_boundaries(self) -> None:
        cases = (
            ("safety_reserve_ratio", 0.0),
            ("max_chunk_ratio", 0.0),
            ("max_chunk_ratio", 1.0),
        )
        for field, value in cases:
            with self.subTest(field=field, value=value):
                with self.assertRaises(ConfigError):
                    parse_qykw_config({"version": 1, "context": {field: value}})

    def test_accepts_explicit_enabled_commands(self) -> None:
        config = parse_qykw_config(
            {"version": 1, "commands": {"enabled": ["分析", "复审"]}}
        )
        self.assertEqual(
            config.commands.enabled_commands,
            (CommandName.ANALYZE, CommandName.REREVIEW),
        )

    def test_loads_toml_configuration(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "qykw.toml"
            path.write_text(
                """version = 1
language = "zh-CN"

[authorization]
code_writers = ["xyh202131"]

[commands]
enabled = ["帮助", "状态"]
""",
                encoding="utf-8",
            )
            config = load_qykw_config(path)

        self.assertEqual(config.authorization.code_writers, ("xyh202131",))
        self.assertEqual(
            config.commands.enabled_commands,
            (CommandName.HELP, CommandName.STATUS),
        )


class TestSharedDomainContracts(unittest.TestCase):
    def test_shared_enums_match_the_domain_contract(self) -> None:
        expected = {
            "RepositoryPermission": ("none", "read", "triage", "write", "maintain", "admin"),
            "Severity": ("P0", "P1", "P2"),
            "DiffSide": ("LEFT", "RIGHT"),
            "ContextChunkKind": ("DIFF", "TRIAGE", "REFERENCE"),
            "CommandName": ("帮助", "分析", "计划", "审查", "复审", "状态", "总结", "修复", "实现", "停止"),
            "CommandMode": ("read_only", "change"),
            "RunStage": ("accepted", "acknowledged", "collecting", "analyzing", "validating", "testing", "publishing", "completed"),
            "RunStatus": ("active", "completed", "partial", "failed", "canceled", "stale"),
            "CommentKind": ("issue", "review"),
            "InferenceErrorCode": ("capability_unsupported", "invalid_config", "dns_error", "tls_error", "connection_error", "read_timeout", "rate_limited", "response_interrupted", "invalid_response", "deadline_exceeded"),
            "CommandRoute": ("deterministic", "advisory", "review", "change"),
        }
        actual = {
            name: tuple(member.value for member in value)
            for name, value in vars(domain).items()
            if isinstance(value, type) and issubclass(value, Enum) and value is not Enum
        }
        self.assertEqual(actual, expected)

    def test_shared_dataclasses_match_the_frozen_domain_contract(self) -> None:
        expected_fields = {
            "Actor": ("login", "permission"),
            "AuthenticatedUser": ("login", "database_id"),
            "CommandRequest": ("name", "argument", "mode"),
            "EventContext": ("repository_id", "repository", "pr_number", "event_name", "action", "actor_login", "source_head_hint", "idempotency_key", "command", "trigger_comment_id", "trigger_comment_kind"),
            "RunContext": ("run_id", "idempotency_key", "repository_id", "repository", "pr_number", "event_name", "event_action", "source_repository", "source_head_sha", "target_base_sha", "target_base_ref", "command", "trigger_actor", "trigger_comment_id", "trigger_comment_kind"),
            "PullRef": ("number", "state", "draft", "source_repository", "source_head_sha", "target_repository", "target_base_sha", "target_base_ref"),
            "ChangedFile": ("path", "previous_path", "status", "base_sha", "head_sha", "base_mode", "head_mode", "base_content", "head_content", "patch", "binary", "generated", "additions", "deletions"),
            "RepositoryFile": ("path", "ref", "sha", "content", "purpose"),
            "CheckRun": ("name", "status", "conclusion"),
            "PullSnapshot": ("number", "state", "draft", "source_repository", "source_head_sha", "target_repository", "target_base_sha", "target_base_ref", "title", "body", "changed_files", "trusted_rules", "related_files", "checks"),
            "FindingCandidate": ("path", "line", "side", "severity", "failure_path", "impact", "evidence", "suggestion", "verification"),
            "Finding": ("path", "line", "side", "severity", "failure_path", "impact", "evidence", "suggestion", "verification", "fingerprint"),
            "CoverageReport": ("total_files", "reviewed_files", "total_hunks", "reviewed_hunks", "omissions", "explains_every_file"),
            "ReviewResult": ("conclusion", "findings", "coverage", "validation_notes", "limitations"),
            "RunRecord": ("context", "stage", "status", "prompt_version", "summary_comment_id", "initial_review", "coverage", "warning_codes", "error_code", "created_at", "updated_at"),
            "CancelRecord": ("pr_number", "target_run_id", "stop_comment_id", "actor_login", "created_at"),
            "RunOutcome": ("run_id", "status", "stage", "error_code"),
            "ProviderCapabilities": ("context_window", "max_output_tokens", "structured_output", "supported_reasoning_profiles"),
            "InferenceUsage": ("input_tokens", "output_tokens"),
            "InferenceRequest": ("run_id", "stage", "prompt_version", "reasoning_profile", "deadline_seconds", "max_output_tokens", "idempotency_key", "schema_name", "schema", "payload"),
            "InferenceResponse": ("request_id", "value", "usage"),
            "InferenceFailure": ("code", "retryable", "request_may_have_been_accepted"),
            "AuthorizationDecision": ("allowed", "reason"),
            "TriggerDecision": ("run", "reason", "idempotency_key"),
            "TriggerRef": ("kind", "node_id"),
            "ReactionResult": ("warning_code",),
            "IssueComment": ("comment_id", "author_login", "body", "updated_at"),
            "ReviewComment": ("comment_id", "author_login", "body", "updated_at", "path", "line", "side"),
            "InlineComment": ("path", "line", "side", "body", "fingerprint"),
            "ChangedLine": ("path", "line", "side"),
            "DiffHunk": ("path", "previous_path", "header", "changed_lines", "text"),
            "FileManifest": ("paths", "risk_order"),
            "ContextChunk": ("chunk_id", "paths", "text", "estimated_tokens", "kind"),
            "ContextPlan": ("repository", "pr_number", "source_head_sha", "run_id", "manifest", "chunks", "coverage", "commentable_lines", "max_chunk_tokens", "effective_input_budget_tokens"),
            "AdvisoryResult": ("title", "body", "evidence", "limitations"),
            "PublishResult": ("status", "summary_comment_id", "summary_body", "review_id", "published_fingerprints", "warning_codes"),
        }
        actual = {
            name: tuple(field.name for field in fields(value))
            for name, value in vars(domain).items()
            if isinstance(value, type) and is_dataclass(value)
        }
        self.assertEqual(actual, expected_fields)
        for name in expected_fields:
            self.assertTrue(domain.__dict__[name].__dataclass_params__.frozen)
