"""Strict parsing for the non-sensitive qykw TOML configuration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
import tomllib

from tools.qykw.domain import CommandName


class ConfigError(ValueError):
    """Raised when qykw configuration is unsafe or invalid."""


@dataclass(frozen=True)
class AuthorizationConfig:
    code_writers: tuple[str, ...]


@dataclass(frozen=True)
class ReviewConfig:
    auto_initial: bool
    auto_on_synchronize: bool
    max_findings: int
    run_timeout_seconds: int


@dataclass(frozen=True)
class ContextConfig:
    safety_reserve_ratio: float
    max_chunk_ratio: float


@dataclass(frozen=True)
class CommandsConfig:
    enabled_commands: tuple[CommandName, ...]


@dataclass(frozen=True)
class VerificationConfig:
    required_checks: tuple[str, ...]
    profiles: tuple[str, ...]


@dataclass(frozen=True)
class QykwConfig:
    version: int
    language: str
    authorization: AuthorizationConfig
    review: ReviewConfig
    context: ContextConfig
    commands: CommandsConfig
    verification: VerificationConfig


_DEFAULT_CODE_WRITERS = ("xyh202131",)
_DEFAULT_COMMANDS = tuple(CommandName)
_DEFAULT_PROFILES = ("backend", "frontend", "full")
_TOP_LEVEL_KEYS = {
    "version",
    "language",
    "authorization",
    "review",
    "context",
    "commands",
    "verification",
}
_GROUP_KEYS = {
    "authorization": {"code_writers"},
    "review": {
        "auto_initial",
        "auto_on_synchronize",
        "max_findings",
        "run_timeout_seconds",
    },
    "context": {"safety_reserve_ratio", "max_chunk_ratio"},
    "commands": {"enabled"},
    "verification": {"required_checks", "profiles"},
}


def parse_qykw_config(data: Mapping[str, object]) -> QykwConfig:
    """Parse a non-sensitive, version-one configuration mapping."""

    _reject_unknown_keys(data, _TOP_LEVEL_KEYS, "configuration")
    version = _positive_int(data.get("version", 1), "version")
    if version != 1:
        raise ConfigError("version must be 1")

    language = _nonempty_string(data.get("language", "zh-CN"), "language")
    authorization = _parse_authorization(_group(data, "authorization"))
    review = _parse_review(_group(data, "review"))
    context = _parse_context(_group(data, "context"))
    commands = _parse_commands(_group(data, "commands"))
    verification = _parse_verification(_group(data, "verification"))
    return QykwConfig(
        version=version,
        language=language,
        authorization=authorization,
        review=review,
        context=context,
        commands=commands,
        verification=verification,
    )


def load_qykw_config(path: Path) -> QykwConfig:
    """Load and parse a qykw TOML file from a trusted path."""

    with path.open("rb") as config_file:
        data = tomllib.load(config_file)
    return parse_qykw_config(data)


def _parse_authorization(data: Mapping[str, object]) -> AuthorizationConfig:
    _reject_unknown_keys(data, _GROUP_KEYS["authorization"], "authorization")
    writers = _string_tuple(
        data.get("code_writers", _DEFAULT_CODE_WRITERS), "authorization.code_writers"
    )
    if not writers:
        raise ConfigError("authorization.code_writers must not be empty")
    return AuthorizationConfig(code_writers=writers)


def _parse_review(data: Mapping[str, object]) -> ReviewConfig:
    _reject_unknown_keys(data, _GROUP_KEYS["review"], "review")
    return ReviewConfig(
        auto_initial=_bool(data.get("auto_initial", True), "review.auto_initial"),
        auto_on_synchronize=_bool(
            data.get("auto_on_synchronize", False), "review.auto_on_synchronize"
        ),
        max_findings=_positive_int(data.get("max_findings", 20), "review.max_findings"),
        run_timeout_seconds=_positive_int(
            data.get("run_timeout_seconds", 900), "review.run_timeout_seconds"
        ),
    )


def _parse_context(data: Mapping[str, object]) -> ContextConfig:
    _reject_unknown_keys(data, _GROUP_KEYS["context"], "context")
    return ContextConfig(
        safety_reserve_ratio=_ratio(
            data.get("safety_reserve_ratio", 0.20), "context.safety_reserve_ratio"
        ),
        max_chunk_ratio=_ratio(
            data.get("max_chunk_ratio", 0.25), "context.max_chunk_ratio"
        ),
    )


def _parse_commands(data: Mapping[str, object]) -> CommandsConfig:
    _reject_unknown_keys(data, _GROUP_KEYS["commands"], "commands")
    names = _string_tuple(
        data.get("enabled", tuple(command.value for command in _DEFAULT_COMMANDS)),
        "commands.enabled",
    )
    try:
        commands = tuple(CommandName(name) for name in names)
    except ValueError as error:
        raise ConfigError("commands.enabled contains an unsupported command") from error
    if len(commands) != len(set(commands)):
        raise ConfigError("commands.enabled must not contain duplicates")
    return CommandsConfig(enabled_commands=commands)


def _parse_verification(data: Mapping[str, object]) -> VerificationConfig:
    _reject_unknown_keys(data, _GROUP_KEYS["verification"], "verification")
    return VerificationConfig(
        required_checks=_string_tuple(
            data.get("required_checks", ()), "verification.required_checks"
        ),
        profiles=_string_tuple(
            data.get("profiles", _DEFAULT_PROFILES), "verification.profiles"
        ),
    )


def _group(data: Mapping[str, object], name: str) -> Mapping[str, object]:
    value = data.get(name, {})
    if not isinstance(value, Mapping):
        raise ConfigError(f"{name} must be a table")
    return value


def _reject_unknown_keys(
    data: Mapping[str, object], allowed: set[str], location: str
) -> None:
    for key in data:
        if not isinstance(key, str) or key not in allowed:
            if isinstance(key, str) and any(
                marker in key.lower()
                for marker in ("secret", "token", "key", "password", "credential")
            ):
                raise ConfigError("secret-bearing fields are not allowed")
            raise ConfigError(f"unknown field in {location}")


def _nonempty_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{field} must be a non-empty string")
    return value


def _string_tuple(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ConfigError(f"{field} must be an array of strings")
    strings = tuple(_nonempty_string(item, field) for item in value)
    if len(strings) != len(set(strings)):
        raise ConfigError(f"{field} must not contain duplicates")
    return strings


def _bool(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise ConfigError(f"{field} must be a boolean")
    return value


def _positive_int(value: object, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise ConfigError(f"{field} must be a positive integer")
    return value


def _ratio(value: object, field: str) -> float:
    if type(value) not in (int, float) or not 0 < value < 1:
        raise ConfigError(f"{field} must be between 0 and 1")
    return float(value)
