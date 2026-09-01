"""Deterministic authorization for qykw commands."""

from __future__ import annotations

from tools.qykw.config import QykwConfig
from tools.qykw.domain import (
    Actor,
    AuthorizationDecision,
    CommandName,
    CommandRequest,
    RepositoryPermission,
)


_CHANGE_COMMANDS = frozenset({CommandName.FIX, CommandName.IMPLEMENT})
_WRITE_PERMISSIONS = frozenset(
    {
        RepositoryPermission.WRITE,
        RepositoryPermission.MAINTAIN,
        RepositoryPermission.ADMIN,
    }
)


def authorize_command(
    command: CommandRequest,
    actor: Actor,
    config: QykwConfig,
    *,
    run_trigger_actor: str | None = None,
) -> AuthorizationDecision:
    """Authorize a command using only parsed data and trusted configuration."""

    if actor.permission is RepositoryPermission.NONE:
        return AuthorizationDecision(False, "repository_member_required")
    if command.name not in config.commands.enabled_commands:
        return AuthorizationDecision(False, "command_disabled")

    if command.name in _CHANGE_COMMANDS:
        if not _is_configured_writer(actor.login, config):
            return AuthorizationDecision(False, "change_actor_not_allowed")
        if actor.permission not in _WRITE_PERMISSIONS:
            return AuthorizationDecision(False, "change_permission_denied")
        return AuthorizationDecision(False, "capability_disabled")

    if command.name is CommandName.STOP:
        if _same_login(actor.login, run_trigger_actor) or _is_configured_writer(
            actor.login, config
        ):
            return AuthorizationDecision(True, "allowed")
        return AuthorizationDecision(False, "stop_actor_not_allowed")

    return AuthorizationDecision(True, "allowed")


def _is_configured_writer(login: str, config: QykwConfig) -> bool:
    return any(_same_login(login, writer) for writer in config.authorization.code_writers)


def _same_login(left: str, right: str | None) -> bool:
    return right is not None and left.casefold() == right.casefold()
