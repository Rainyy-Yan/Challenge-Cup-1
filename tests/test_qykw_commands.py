"""Tests for deterministic qykw mention and command parsing."""

import unittest

from tools.qykw.commands import CommandRouter, parse_command
from tools.qykw.domain import CommandMode, CommandName, CommandRequest, CommandRoute


class TestCommandParsing(unittest.TestCase):
    def test_exact_first_effective_mention_triggers(self) -> None:
        command = parse_command("@qykw 审查 安全")

        self.assertEqual(
            command,
            CommandRequest(CommandName.REVIEW, "安全", CommandMode.READ_ONLY),
        )

    def test_quotes_code_comments_and_similar_logins_do_not_trigger(self) -> None:
        for body in (
            "> @qykw 审查",
            "`@qykw 审查`",
            "<!-- @qykw 审查 -->",
            "```text\n@qykw 审查\n```",
            "@qykw-old 审查",
            "@qykw_other 审查",
            "@qy\u200bkw 审查",
            "＠qykw 审查",
            "mail@qykw.example",
        ):
            with self.subTest(body=body):
                self.assertIsNone(parse_command(body))

    def test_ambiguous_write_request_stays_read_only(self) -> None:
        command = parse_command("@qykw 帮我改一下")

        self.assertEqual(
            command,
            CommandRequest(CommandName.ANALYZE, "帮我改一下", CommandMode.READ_ONLY),
        )

    def test_only_first_effective_paragraph_can_trigger(self) -> None:
        self.assertIsNone(parse_command("普通正文\n\n@qykw 审查"))

    def test_ignored_content_does_not_consume_first_effective_paragraph(self) -> None:
        command = parse_command("<!-- note -->\n> @qykw 审查\n\n@qykw 计划 测试")

        self.assertEqual(
            command,
            CommandRequest(CommandName.PLAN, "测试", CommandMode.READ_ONLY),
        )

    def test_commands_and_routes_are_fixed_and_complete(self) -> None:
        expected = {
            CommandName.HELP: ("帮助", "", CommandMode.READ_ONLY, CommandRoute.DETERMINISTIC),
            CommandName.ANALYZE: ("分析", "问题", CommandMode.READ_ONLY, CommandRoute.ADVISORY),
            CommandName.PLAN: ("计划", "需求", CommandMode.READ_ONLY, CommandRoute.ADVISORY),
            CommandName.REVIEW: ("审查", "安全", CommandMode.READ_ONLY, CommandRoute.REVIEW),
            CommandName.REREVIEW: ("复审", "测试", CommandMode.READ_ONLY, CommandRoute.REVIEW),
            CommandName.STATUS: ("状态", "", CommandMode.READ_ONLY, CommandRoute.DETERMINISTIC),
            CommandName.SUMMARY: ("总结", "", CommandMode.READ_ONLY, CommandRoute.DETERMINISTIC),
            CommandName.FIX: ("修复", "QY-01", CommandMode.CHANGE, CommandRoute.CHANGE),
            CommandName.IMPLEMENT: ("实现", "需求", CommandMode.CHANGE, CommandRoute.CHANGE),
            CommandName.STOP: ("停止", "", CommandMode.READ_ONLY, CommandRoute.DETERMINISTIC),
        }

        self.assertEqual(set(CommandName), set(CommandRouter.ROUTES))
        router = CommandRouter()
        for name, (keyword, argument, mode, route) in expected.items():
            with self.subTest(name=name):
                suffix = f" {argument}" if argument else ""
                self.assertEqual(
                    parse_command(f"@QYKW {keyword}{suffix}"),
                    CommandRequest(name, argument, mode),
                )
                self.assertEqual(router.resolve(CommandRequest(name, argument, mode)), route)

    def test_first_command_after_mention_wins(self) -> None:
        command = parse_command("@qykw 分析 修复 QY-01")

        self.assertEqual(
            command,
            CommandRequest(CommandName.ANALYZE, "修复 QY-01", CommandMode.READ_ONLY),
        )


if __name__ == "__main__":
    unittest.main()
