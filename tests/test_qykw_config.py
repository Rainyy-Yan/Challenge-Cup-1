"""Tests for qykw configuration parsing."""

import unittest

from tools.qykw.config import ConfigError, parse_qykw_config


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
