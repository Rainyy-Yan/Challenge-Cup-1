"""Tests for qykw command authorization policy."""

import unittest

from tools.qykw.config import parse_qykw_config
from tools.qykw.domain import (
    Actor,
    CommandMode,
    CommandName,
    CommandRequest,
    RepositoryPermission,
)
from tools.qykw.policy import authorize_command


def config_with_writers(*writers: str, enabled: tuple[str, ...] | None = None):
    """Return a strict configuration fixture with the requested writers."""

    data: dict[str, object] = {
        "version": 1,
        "authorization": {"code_writers": list(writers)},
    }
    if enabled is not None:
        data["commands"] = {"enabled": list(enabled)}
    return parse_qykw_config(data)


class TestAuthorization(unittest.TestCase):
    def test_change_requires_configured_writer(self) -> None:
        decision = authorize_command(
            CommandRequest(CommandName.FIX, "QY-01", CommandMode.CHANGE),
            Actor("member", RepositoryPermission.WRITE),
            config_with_writers("xyh202131"),
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "change_actor_not_allowed")

    def test_change_requires_write_or_higher_permission(self) -> None:
        command = CommandRequest(CommandName.IMPLEMENT, "需求", CommandMode.CHANGE)
        config = config_with_writers("xyh202131")

        for permission in (
            RepositoryPermission.READ,
            RepositoryPermission.TRIAGE,
        ):
            with self.subTest(permission=permission):
                decision = authorize_command(
                    command,
                    Actor("xyh202131", permission),
                    config,
                )
                self.assertEqual(decision.reason, "change_permission_denied")

        for permission in (
            RepositoryPermission.WRITE,
            RepositoryPermission.MAINTAIN,
            RepositoryPermission.ADMIN,
        ):
            with self.subTest(permission=permission):
                decision = authorize_command(
                    command,
                    Actor("xyh202131", permission),
                    config,
                )
                self.assertTrue(decision.allowed)
                self.assertEqual(decision.reason, "allowed")

    def test_command_name_not_request_mode_controls_change_policy(self) -> None:
        decision = authorize_command(
            CommandRequest(CommandName.FIX, "QY-01", CommandMode.READ_ONLY),
            Actor("member", RepositoryPermission.WRITE),
            config_with_writers("xyh202131"),
        )

        self.assertEqual(decision.reason, "change_actor_not_allowed")

    def test_read_only_commands_require_repository_membership(self) -> None:
        command = CommandRequest(CommandName.REVIEW, "安全", CommandMode.READ_ONLY)
        config = config_with_writers("xyh202131")

        self.assertEqual(
            authorize_command(
                command,
                Actor("outsider", RepositoryPermission.NONE),
                config,
            ).reason,
            "repository_member_required",
        )
        for permission in (
            RepositoryPermission.READ,
            RepositoryPermission.TRIAGE,
            RepositoryPermission.WRITE,
            RepositoryPermission.MAINTAIN,
            RepositoryPermission.ADMIN,
        ):
            with self.subTest(permission=permission):
                decision = authorize_command(
                    command,
                    Actor("member", permission),
                    config,
                )
                self.assertEqual(decision.reason, "allowed")
                self.assertTrue(decision.allowed)

    def test_stop_allows_only_trigger_actor_or_configured_writer(self) -> None:
        command = CommandRequest(CommandName.STOP, "", CommandMode.READ_ONLY)
        config = config_with_writers("xyh202131")

        self.assertEqual(
            authorize_command(
                command,
                Actor("trigger", RepositoryPermission.READ),
                config,
                run_trigger_actor="trigger",
            ).reason,
            "allowed",
        )
        self.assertEqual(
            authorize_command(
                command,
                Actor("xyh202131", RepositoryPermission.READ),
                config,
                run_trigger_actor="trigger",
            ).reason,
            "allowed",
        )
        self.assertEqual(
            authorize_command(
                command,
                Actor("member", RepositoryPermission.WRITE),
                config,
                run_trigger_actor="trigger",
            ).reason,
            "stop_actor_not_allowed",
        )

    def test_disabled_commands_are_rejected_before_routing(self) -> None:
        decision = authorize_command(
            CommandRequest(CommandName.REVIEW, "", CommandMode.READ_ONLY),
            Actor("member", RepositoryPermission.READ),
            config_with_writers("xyh202131", enabled=("帮助",)),
        )

        self.assertEqual(decision.reason, "command_disabled")


if __name__ == "__main__":
    unittest.main()
