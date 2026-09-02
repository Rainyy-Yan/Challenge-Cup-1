"""Production adapters for the five authorized-change workflow phases."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import MappingProxyType
from typing import Callable, Mapping

from tools.qykw.change import (
    ChangeKind,
    ChangePublication,
    ChangeRequest,
    CommandResult,
    FileDigest,
    FilePatch,
    PatchManifest,
    PublicationRequest,
    PublicationStage,
    TextEdit,
    VerificationAttestation,
    WriteKind,
    WriteState,
    canonical_manifest_bytes,
    compute_manifest_digest,
    prepare_change,
)
from tools.qykw.change_github import (
    HttpChangeGitHubGateway,
    HttpTrustedSourceTreeProvider,
)
from tools.qykw.change_phases import (
    CHANGE_PHASES,
    ChangePhaseServices,
    TrustedPhaseRuntime,
)
from tools.qykw.change_publish import (
    PublicationJournalEntry,
    TrustedPublicationRuntime,
    publish_verified_change,
)
from tools.qykw.config import load_qykw_config
from tools.qykw.domain import (
    Actor,
    CommandMode,
    CommandName,
    CommentKind,
    EventContext,
    RunContext,
    RunRecord,
    RunStage,
    RunStatus,
    ReactionResult,
    TriggerRef,
)
from tools.qykw.github import HttpGitHubGateway
from tools.qykw.patches import materialize_workspace
from tools.qykw.policy import DeterministicChangePolicy, authorize_command
from tools.qykw.provider import ResponsesInferenceProvider
from tools.qykw.sandbox import DockerSandboxExecutor
from tools.qykw.state import GitHubCommentStateStore
from tools.qykw.triggers import build_run_context, make_run_id, normalize_event
from tools.qykw.verification import (
    VerificationRuntimeMetadata,
    get_verification_profile,
    verify_change,
)


_PROMPT_VERSION = "qykw-v1"
_MAX_JOURNAL_BYTES = 1024 * 1024
_OUTPUT_LIMIT_BYTES = 1024 * 1024
class ProductionChangeServicesFactory:
    """Construct only the adapter graph owned by the requested workflow phase."""

    def __call__(
        self,
        phase: str,
        environment: Mapping[str, str],
        runtime: TrustedPhaseRuntime,
    ) -> ChangePhaseServices:
        if phase not in CHANGE_PHASES or runtime.phase != phase:
            raise ValueError("invalid_change_phase")
        builder = _PHASE_BUILDERS.get(phase)
        if builder is None:
            raise ValueError("invalid_change_phase")
        narrow = MappingProxyType(dict(environment))
        return builder(narrow, runtime)


def build_production_change_factory() -> ProductionChangeServicesFactory:
    return ProductionChangeServicesFactory()


class _AuthorizeServices:
    def __init__(self, environment: Mapping[str, str]) -> None:
        self._environment = environment

    def authorize_change(self, runtime: TrustedPhaseRuntime) -> dict[str, object]:
        event = _event_context(self._environment)
        if event is None:
            return _root_skip("not_a_change_comment")
        if event.command.name not in {CommandName.FIX, CommandName.IMPLEMENT}:
            return _root_skip("change_command_required")
        if event.command.mode is not CommandMode.CHANGE:
            return _root_skip("change_mode_required")
        gateway = _review_gateway(self._environment)
        state = GitHubCommentStateStore(gateway, repository=event.repository)
        config = _config(self._environment)
        permission = gateway.get_actor_permission(event.actor_login)
        decision = authorize_command(event.command, Actor(event.actor_login, permission), config)
        if not decision.allowed:
            return _root_skip(decision.reason)
        existing = state.find_by_idempotency_key(event.pr_number, event.idempotency_key)
        if existing is not None:
            return _authorized_result(_recover_existing(existing, event), runtime)
        run = build_run_context(event, gateway.get_pull_ref(event.pr_number))
        if run is None:
            return _root_skip("stale_pull_ref")
        now = _now()
        record = RunRecord(
            run,
            RunStage.ACCEPTED,
            RunStatus.ACTIVE,
            _PROMPT_VERSION,
            None,
            False,
            None,
            (),
            None,
            now,
            now,
        )
        gateway.assert_bot_identity("qykw")
        if not state.create(record):
            existing = state.find_by_idempotency_key(event.pr_number, event.idempotency_key)
            if existing is None:
                raise ValueError("state_claim_unconfirmed")
            return _authorized_result(_recover_existing(existing, event), runtime)
        if event.trigger_comment_id is not None and event.trigger_comment_kind is not None:
            warning_code: str | None = None
            try:
                reaction = gateway.try_add_reaction(
                    TriggerRef(
                        "issue_comment"
                        if event.trigger_comment_kind is CommentKind.ISSUE
                        else "review_comment",
                        event.trigger_comment_id,
                    ),
                    "laugh",
                )
                if type(reaction) is not ReactionResult or reaction.warning_code not in {
                    None,
                    "reaction_failed",
                }:
                    warning_code = "reaction_failed"
                else:
                    warning_code = reaction.warning_code
            except Exception:
                warning_code = "reaction_failed"
            if warning_code is not None:
                stored = state.get(run.pr_number, run.run_id)
                if stored is not None and stored.context == run:
                    state.save(
                        replace(
                            stored,
                            warning_codes=tuple(
                                dict.fromkeys((*stored.warning_codes, warning_code))
                            ),
                            updated_at=_now(),
                        )
                    )
        return _authorized_result(run, runtime)


class _PrepareServices:
    def __init__(self, environment: Mapping[str, str]) -> None:
        self._environment = environment

    def prepare_change(
        self, artifact: dict[str, object], runtime: TrustedPhaseRuntime
    ) -> dict[str, object]:
        request = _request_from_artifact(artifact, self._environment, runtime)
        gateway = _read_gateway(self._environment)
        state = GitHubCommentStateStore(gateway, repository=request.target_repository)
        record = state.get(request.context.pr_number, request.context.run_id)
        if record is None or record.context != request.context:
            raise ValueError("change_state_unavailable")
        snapshot = gateway.get_pull_snapshot(request.context.pr_number, run=request.context)
        tree_provider = HttpTrustedSourceTreeProvider(
            api_url=_api_url(self._environment),
            repository=request.source_repository,
            source_head_sha=request.source_head_sha,
            token=_required(self._environment, "GITHUB_TOKEN"),
        )
        policy = DeterministicChangePolicy(_config(self._environment), tree_provider=tree_provider)
        provider = ResponsesInferenceProvider.from_env(self._environment)
        manifest = prepare_change(request, snapshot, provider, policy, state)
        return {
            "status": "prepared",
            "data": {
                "request": _request_payload(request),
                "manifest": _manifest_payload(manifest),
            },
        }


class _VerifyServices:
    def __init__(
        self,
        environment: Mapping[str, str],
        source_root: Path | None = None,
    ) -> None:
        self._environment = environment
        self._source_root = source_root or (Path.cwd().parent / "candidate-source")

    def verify_change(
        self, artifact: dict[str, object], runtime: TrustedPhaseRuntime
    ) -> dict[str, object]:
        request = _request_from_artifact(artifact, self._environment, runtime)
        manifest = _manifest_from_artifact(artifact)
        if runtime.image_ref is None or runtime.image_digest is None:
            raise ValueError("verification_image_digest_unavailable")
        state = GitHubCommentStateStore(
            _read_gateway(self._environment), repository=request.target_repository
        )
        config = _config(self._environment)
        with tempfile.TemporaryDirectory(prefix="qykw-verify-") as directory:
            workspace = _materialize_for_verification(
                request,
                self._source_root,
                Path(directory) / "workspace",
                self._environment,
            )
            executor = DockerSandboxExecutor(workspace.root, runtime.image_ref)
            verification_runtime = VerificationRuntimeMetadata(
                runtime.workflow_run_id,
                runtime.image_digest,
                config.review.run_timeout_seconds,
                _OUTPUT_LIMIT_BYTES,
            )
            attestation = verify_change(
                request,
                manifest,
                workspace,
                executor,
                state,
                runtime=verification_runtime,
            )
        status = "canceled" if attestation.canceled else (
            "verified" if attestation.success else "failed"
        )
        return {
            "status": status,
            "data": {
                "request": _request_payload(request),
                "manifest": _manifest_payload(manifest),
                "attestation": _attestation_payload(attestation),
            },
        }


class _PublishServices:
    def __init__(self, environment: Mapping[str, str], runtime: TrustedPhaseRuntime) -> None:
        self._environment = environment
        if runtime.runner_temp is None:
            raise ValueError("invalid_publication_journal_root")
        self._journal = _FilePublicationJournal(
            runtime.runner_temp / "qykw" / "publication-journal-v1.jsonl"
        )

    def publish_change(
        self, artifact: dict[str, object], runtime: TrustedPhaseRuntime
    ) -> dict[str, object]:
        request = _request_from_artifact(artifact, self._environment, runtime)
        manifest = _manifest_from_artifact(artifact)
        attestation = _attestation_from_artifact(artifact)
        if runtime.image_digest is None:
            raise ValueError("verification_image_digest_unavailable")
        token = _required(self._environment, "QYKW_PUBLISH_TOKEN")
        gateway = HttpChangeGitHubGateway(
            _api_url(self._environment), request.target_repository, token
        )
        state = GitHubCommentStateStore(
            HttpGitHubGateway(
                _api_url(self._environment),
                request.target_repository,
                token,
                "",
            ),
            repository=request.target_repository,
        )
        tree_provider = HttpTrustedSourceTreeProvider(
            api_url=_api_url(self._environment),
            repository=request.source_repository,
            source_head_sha=request.source_head_sha,
            token=token,
        )
        policy = DeterministicChangePolicy(_config(self._environment), tree_provider=tree_provider)
        publication_request = PublicationRequest(
            request,
            manifest,
            attestation,
            f"qykw/{request.context.run_id.lower()}-{request.kind.value}",
            f"qykw: {request.kind.value} {request.context.run_id.lower()}",
            "Authorized qykw change. Review is required before merge.",
        )
        publication = publish_verified_change(
            publication_request,
            gateway,
            state,
            runtime=TrustedPublicationRuntime(
                runtime.workflow_run_id,
                runtime.image_digest,
                get_verification_profile(runtime.verification_profile),
            ),
            journal=self._journal,
            policy=policy,
        )
        status = _publication_status(publication)
        return {
            "status": status,
            "data": {"publication": _publication_payload(publication)},
        }


class _RecordServices:
    def __init__(self, environment: Mapping[str, str]) -> None:
        self._environment = environment

    def record_change_result(
        self, artifact: dict[str, object], runtime: TrustedPhaseRuntime
    ) -> dict[str, object]:
        context = _run_context(artifact)
        job_results = runtime.job_results
        if job_results is None:
            raise ValueError("invalid_job_results")
        ordered = (
            ("authorize-change", job_results.authorize),
            ("prepare-change", job_results.prepare),
            ("verify-change", job_results.verify),
            ("publish-change", job_results.publish),
        )
        failed_phase: str | None = None
        status = "completed"
        for phase, result in ordered:
            if result == "success":
                continue
            failed_phase = phase
            status = "canceled" if result == "cancelled" else "failed"
            break
        if status == "completed" and artifact.get("phase") == "publish-change":
            published_status = _mapping(
                artifact.get("payload"), "invalid_phase_payload"
            ).get("status")
            if published_status in {"completed", "partial", "failed", "canceled"}:
                status = published_status
        outcome = {"status": status, "failed_phase": failed_phase}
        _record_terminal(context, status, failed_phase, self._environment)
        return {"status": status, "data": {"outcome": outcome}}


def _build_authorize(
    environment: Mapping[str, str], runtime: TrustedPhaseRuntime
) -> ChangePhaseServices:
    del runtime
    _required(environment, "QYKW_REVIEW_TOKEN")
    return _AuthorizeServices(environment)


def _build_prepare(
    environment: Mapping[str, str], runtime: TrustedPhaseRuntime
) -> ChangePhaseServices:
    del runtime
    _required(environment, "GITHUB_TOKEN")
    _required(environment, "QYKW_INFERENCE_API_KEY")
    return _PrepareServices(environment)


def _build_verify(
    environment: Mapping[str, str], runtime: TrustedPhaseRuntime
) -> ChangePhaseServices:
    if runtime.image_ref is None:
        raise ValueError("verification_image_digest_unavailable")
    _required(environment, "GITHUB_TOKEN")
    return _VerifyServices(environment)


def _build_publish(
    environment: Mapping[str, str], runtime: TrustedPhaseRuntime
) -> ChangePhaseServices:
    _required(environment, "QYKW_PUBLISH_TOKEN")
    return _PublishServices(environment, runtime)


def _build_record(
    environment: Mapping[str, str], runtime: TrustedPhaseRuntime
) -> ChangePhaseServices:
    del runtime
    _required(environment, "QYKW_REVIEW_TOKEN")
    return _RecordServices(environment)


_PHASE_BUILDERS: dict[
    str, Callable[[Mapping[str, str], TrustedPhaseRuntime], ChangePhaseServices]
] = {
    "authorize-change": _build_authorize,
    "prepare-change": _build_prepare,
    "verify-change": _build_verify,
    "publish-change": _build_publish,
    "record-change-result": _build_record,
}


def _materialize_for_verification(
    request: ChangeRequest,
    source_root: Path,
    destination: Path,
    environment: Mapping[str, str],
):
    provider = HttpTrustedSourceTreeProvider(
        api_url=_api_url(environment),
        repository=request.source_repository,
        source_head_sha=request.source_head_sha,
        token=_required(environment, "GITHUB_TOKEN"),
    )
    index = provider.get_complete_tree(request.source_repository, request.source_head_sha)
    digests = tuple(
        sorted(
            (
                FileDigest(blob.path, blob.mode, hashlib.sha256(blob.content).hexdigest())
                for blob in index.blobs
            ),
            key=lambda item: item.path,
        )
    )
    return materialize_workspace(
        source_root,
        source_head_sha=request.source_head_sha,
        tracked_files=digests,
        destination=destination,
    )


def _event_context(environment: Mapping[str, str]) -> EventContext | None:
    path = Path(_required(environment, "GITHUB_EVENT_PATH"))
    raw = json.loads(path.read_text(encoding="utf-8"))
    if type(raw) is not dict:
        raise ValueError("invalid_event")
    return normalize_event(
        _required(environment, "GITHUB_EVENT_NAME"),
        raw,
        repository_id=int(_required(environment, "GITHUB_REPOSITORY_ID")),
        repository=_required(environment, "GITHUB_REPOSITORY"),
        workflow_run_id=int(_required(environment, "GITHUB_RUN_ID")),
    )


def _review_gateway(environment: Mapping[str, str]) -> HttpGitHubGateway:
    token = _required(environment, "QYKW_REVIEW_TOKEN")
    return HttpGitHubGateway(
        _api_url(environment),
        _required(environment, "GITHUB_REPOSITORY"),
        token,
        token,
    )


def _read_gateway(environment: Mapping[str, str]) -> HttpGitHubGateway:
    return HttpGitHubGateway(
        _api_url(environment),
        _required(environment, "GITHUB_REPOSITORY"),
        _required(environment, "GITHUB_TOKEN"),
        "",
    )


def _config(environment: Mapping[str, str]):
    return load_qykw_config(Path(environment.get("QYKW_CONFIG_PATH", ".github/qykw.toml")))


def _api_url(environment: Mapping[str, str]) -> str:
    return environment.get("GITHUB_API_URL", "https://api.github.com")


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name)
    if not value:
        raise ValueError("phase_credentials_unavailable")
    return value


def _root_skip(reason: str) -> dict[str, object]:
    return {"run": None, "payload": {"status": "skipped", "data": {"reason": reason}}}


def _recover_existing(record: object, event: EventContext) -> RunContext:
    if type(record) is not RunRecord:
        raise ValueError("invalid_existing_change_run")
    context = record.context
    if (
        context.repository_id != event.repository_id
        or context.repository != event.repository
        or context.pr_number != event.pr_number
        or context.event_name != event.event_name
        or context.event_action != event.action
        or context.command != event.command
        or context.trigger_actor != event.actor_login
        or context.trigger_comment_id != event.trigger_comment_id
        or context.trigger_comment_kind != event.trigger_comment_kind
        or context.idempotency_key != event.idempotency_key
        or context.run_id != make_run_id(event.pr_number, event.idempotency_key)
        or record.prompt_version != _PROMPT_VERSION
    ):
        raise ValueError("invalid_existing_change_run")
    return context


def _authorized_result(
    run: RunContext, runtime: TrustedPhaseRuntime
) -> dict[str, object]:
    request = ChangeRequest(
        context=run,
        kind=(ChangeKind.FIX if run.command.name is CommandName.FIX else ChangeKind.IMPLEMENT),
        instruction=run.command.argument,
        source_repository=run.source_repository,
        target_repository=run.repository,
        source_head_sha=run.source_head_sha,
        target_base_sha=run.target_base_sha,
        target_base_ref=run.target_base_ref,
        verification_profile=runtime.verification_profile,
    )
    return {
        "run": _run_payload(run),
        "payload": {
            "status": "accepted",
            "data": {"request": _request_payload(request)},
        },
    }


def _run_context(artifact: Mapping[str, object]) -> RunContext:
    from tools.qykw.phases import _run_from_artifact

    run = _run_from_artifact(dict(artifact))
    if run is None:
        raise ValueError("invalid_run_binding")
    return run


def _request_from_artifact(
    artifact: Mapping[str, object],
    environment: Mapping[str, str] | None = None,
    runtime: TrustedPhaseRuntime | None = None,
) -> ChangeRequest:
    context = _run_context(artifact)
    data = _artifact_data(artifact)
    value = _mapping(data.get("request"), "invalid_change_request")
    expected = {
        "kind",
        "instruction",
        "source_repository",
        "target_repository",
        "source_head_sha",
        "target_base_sha",
        "target_base_ref",
        "verification_profile",
    }
    if set(value) != expected:
        raise ValueError("invalid_change_request")
    try:
        request = ChangeRequest(
            context,
            ChangeKind(_string(value["kind"])),
            _string(value["instruction"], allow_empty=False),
            _string(value["source_repository"]),
            _string(value["target_repository"]),
            _string(value["source_head_sha"]),
            _string(value["target_base_sha"]),
            _string(value["target_base_ref"]),
            _string(value["verification_profile"]),
        )
    except (KeyError, TypeError, ValueError):
        raise ValueError("invalid_change_request") from None
    if _request_payload(request) != dict(value):
        raise ValueError("invalid_change_request")
    expected_kind = {
        CommandName.FIX: ChangeKind.FIX,
        CommandName.IMPLEMENT: ChangeKind.IMPLEMENT,
    }.get(context.command.name)
    if (
        context.command.mode is not CommandMode.CHANGE
        or request.kind is not expected_kind
        or request.instruction != context.command.argument
        or request.source_repository != context.source_repository
        or request.target_repository != context.repository
        or request.source_head_sha != context.source_head_sha
        or request.target_base_sha != context.target_base_sha
        or request.target_base_ref != context.target_base_ref
    ):
        raise ValueError("invalid_change_request")
    if environment is not None:
        _validate_context_environment(context, environment)
        if request.target_repository != _required(environment, "GITHUB_REPOSITORY"):
            raise ValueError("invalid_change_request")
    if runtime is not None:
        if (
            request.verification_profile != runtime.verification_profile
            or artifact.get("workflow_run_id") != runtime.workflow_run_id
            or artifact.get("runtime")
            != {
                "controller_sha": runtime.controller_sha,
                "verification_profile": runtime.verification_profile,
            }
        ):
            raise ValueError("invalid_change_request")
    return request


def _validate_context_environment(
    context: RunContext, environment: Mapping[str, str]
) -> None:
    event = _event_context(environment)
    if event is None or (
        context.run_id != make_run_id(event.pr_number, event.idempotency_key)
        or context.idempotency_key != event.idempotency_key
        or context.repository_id != event.repository_id
        or context.repository != event.repository
        or context.pr_number != event.pr_number
        or context.event_name != event.event_name
        or context.event_action != event.action
        or context.command != event.command
        or context.trigger_actor != event.actor_login
        or context.trigger_comment_id != event.trigger_comment_id
        or context.trigger_comment_kind != event.trigger_comment_kind
    ):
        raise ValueError("invalid_change_request")


def _manifest_from_artifact(artifact: Mapping[str, object]) -> PatchManifest:
    data = _artifact_data(artifact)
    value = _mapping(data.get("manifest"), "invalid_patch_manifest")
    expected = {
        "schema_version", "run_id", "source_repository", "target_repository",
        "source_pr_number", "source_head_sha", "target_base_sha", "target_base_ref",
        "verification_profile", "files", "digest",
    }
    if set(value) != expected or type(value.get("files")) is not list:
        raise ValueError("invalid_patch_manifest")
    files: list[FilePatch] = []
    try:
        for raw in value["files"]:
            patch = _mapping(raw, "invalid_patch_manifest")
            if set(patch) != {"path", "base_sha256", "create", "edits"} or type(patch["edits"]) is not list:
                raise ValueError
            edits = []
            for raw_edit in patch["edits"]:
                edit = _mapping(raw_edit, "invalid_patch_manifest")
                if set(edit) != {"before", "after"}:
                    raise ValueError
                edits.append(TextEdit(_string(edit["before"], allow_empty=True), _string(edit["after"], allow_empty=True)))
            base = patch["base_sha256"]
            if base is not None and type(base) is not str:
                raise ValueError
            files.append(FilePatch(_string(patch["path"]), base, _bool(patch["create"]), tuple(edits)))
        manifest = PatchManifest(
            _int(value["schema_version"]), _string(value["run_id"]),
            _string(value["source_repository"]), _string(value["target_repository"]),
            _int(value["source_pr_number"]), _string(value["source_head_sha"]),
            _string(value["target_base_sha"]), _string(value["target_base_ref"]),
            _string(value["verification_profile"]), tuple(files), _string(value["digest"]),
        )
    except (KeyError, TypeError, ValueError):
        raise ValueError("invalid_patch_manifest") from None
    if manifest.digest != compute_manifest_digest(manifest):
        raise ValueError("invalid_patch_manifest")
    return manifest


def _attestation_from_artifact(artifact: Mapping[str, object]) -> VerificationAttestation:
    data = _artifact_data(artifact)
    value = _mapping(data.get("attestation"), "invalid_verification_attestation")
    expected = {
        "schema_version", "workflow_run_id", "run_id", "source_repository",
        "source_head_sha", "target_repository", "target_base_sha", "target_base_ref",
        "manifest_digest", "profile", "image_digest", "output_tree_digest",
        "workspace_tree_digest", "output_files", "success", "canceled", "results",
    }
    if set(value) != expected or type(value["output_files"]) is not list or type(value["results"]) is not list:
        raise ValueError("invalid_verification_attestation")
    try:
        output_files = tuple(
            FileDigest(_string(item["path"]), _string(item["mode"]), _string(item["sha256"]))
            for raw in value["output_files"]
            for item in [_exact_mapping(raw, {"path", "mode", "sha256"}, "invalid_verification_attestation")]
        )
        results = tuple(
            CommandResult(
                _string(item["name"]), _string(item["argv_digest"]),
                None if item["exit_code"] is None else _int(item["exit_code"]),
                _bool(item["timed_out"]), _int(item["duration_ms"]),
                _string(item["output_digest"]), _string(item["output_excerpt"], allow_empty=True),
            )
            for raw in value["results"]
            for item in [_exact_mapping(raw, {"name", "argv_digest", "exit_code", "timed_out", "duration_ms", "output_digest", "output_excerpt"}, "invalid_verification_attestation")]
        )
        return VerificationAttestation(
            _int(value["schema_version"]), _int(value["workflow_run_id"]),
            _string(value["run_id"]), _string(value["source_repository"]),
            _string(value["source_head_sha"]), _string(value["target_repository"]),
            _string(value["target_base_sha"]), _string(value["target_base_ref"]),
            _string(value["manifest_digest"]), _string(value["profile"]),
            _string(value["image_digest"]), _string(value["output_tree_digest"]),
            _string(value["workspace_tree_digest"]), output_files,
            _bool(value["success"]), _bool(value["canceled"]), results,
        )
    except (KeyError, TypeError, ValueError):
        raise ValueError("invalid_verification_attestation") from None


def _artifact_data(artifact: Mapping[str, object]) -> Mapping[str, object]:
    payload = _mapping(artifact.get("payload"), "invalid_phase_payload")
    return _mapping(payload.get("data"), "invalid_phase_payload")


def _request_payload(request: ChangeRequest) -> dict[str, object]:
    return {
        "kind": request.kind.value,
        "instruction": request.instruction,
        "source_repository": request.source_repository,
        "target_repository": request.target_repository,
        "source_head_sha": request.source_head_sha,
        "target_base_sha": request.target_base_sha,
        "target_base_ref": request.target_base_ref,
        "verification_profile": request.verification_profile,
    }


def _manifest_payload(manifest: PatchManifest) -> dict[str, object]:
    return json.loads(canonical_manifest_bytes(manifest, include_digest=True))


def _attestation_payload(value: VerificationAttestation) -> dict[str, object]:
    return {
        "schema_version": value.schema_version,
        "workflow_run_id": value.workflow_run_id,
        "run_id": value.run_id,
        "source_repository": value.source_repository,
        "source_head_sha": value.source_head_sha,
        "target_repository": value.target_repository,
        "target_base_sha": value.target_base_sha,
        "target_base_ref": value.target_base_ref,
        "manifest_digest": value.manifest_digest,
        "profile": value.profile,
        "image_digest": value.image_digest,
        "output_tree_digest": value.output_tree_digest,
        "workspace_tree_digest": value.workspace_tree_digest,
        "output_files": [vars(item) for item in value.output_files],
        "success": value.success,
        "canceled": value.canceled,
        "results": [vars(item) for item in value.results],
    }


def _publication_status(value: ChangePublication) -> str:
    if value.error_code is None and value.stage is PublicationStage.COMPLETED:
        return "completed"
    if value.error_code is not None and "cancel" in value.error_code:
        return "canceled"
    return "partial" if value.partial else "failed"


def _publication_payload(value: ChangePublication) -> dict[str, object]:
    return {
        "stage": value.stage.value,
        "branch_name": value.branch_name,
        "branch_state": value.branch_state.value,
        "pull_state": value.pull_state.value,
        "commit_sha": value.commit_sha,
        "pull_number": value.pull_number,
        "receipts": [
            {
                "kind": receipt.kind.value,
                "target": receipt.target,
                "object_id": receipt.object_id,
                "state": receipt.state.value,
            }
            for receipt in value.receipts
        ],
        "partial": value.partial,
        "error_code": value.error_code,
    }


def _record_terminal(
    context: RunContext,
    status: str,
    failed_phase: str | None,
    environment: Mapping[str, str],
) -> None:
    gateway = _review_gateway(environment)
    state = GitHubCommentStateStore(gateway, repository=context.repository)
    record = state.get(context.pr_number, context.run_id)
    if record is None or record.context != context:
        raise ValueError("change_state_unavailable")
    statuses = {
        "completed": RunStatus.COMPLETED,
        "partial": RunStatus.PARTIAL,
        "failed": RunStatus.FAILED,
        "canceled": RunStatus.CANCELED,
    }
    terminal = statuses[status]
    state.save(
        replace(
            record,
            stage=RunStage.COMPLETED,
            status=terminal,
            error_code=None if status == "completed" else f"{failed_phase or 'change'}_failed",
            updated_at=_now(),
        )
    )


class _FilePublicationJournal:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self, run_id: str) -> tuple[PublicationJournalEntry, ...]:
        if not self._path.exists():
            return ()
        if self._path.is_symlink() or not self._path.is_file() or self._path.stat().st_size > _MAX_JOURNAL_BYTES:
            raise ValueError("invalid_publication_journal")
        records = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            raw = json.loads(line, object_pairs_hook=_unique_object)
            entry = _journal_entry(raw)
            if entry.run_id == run_id:
                records.append(entry)
        return tuple(records)

    def append_synced(self, entry: PublicationJournalEntry) -> None:
        if type(entry) is not PublicationJournalEntry:
            raise ValueError("invalid_publication_journal_entry")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._path.exists() and self._path.is_symlink():
            raise ValueError("invalid_publication_journal")
        data = (json.dumps(_journal_payload(entry), sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        descriptor = os.open(self._path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            written = 0
            while written < len(data):
                written += os.write(descriptor, data[written:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _journal_payload(entry: PublicationJournalEntry) -> dict[str, object]:
    return {
        "schema_version": entry.schema_version, "run_id": entry.run_id,
        "operation_id": entry.operation_id, "stage": entry.stage.value,
        "kind": entry.kind.value, "target": entry.target,
        "object_id": entry.object_id, "state": entry.state.value,
        "repository": entry.repository, "source_head_sha": entry.source_head_sha,
        "target_base_sha": entry.target_base_sha, "manifest_digest": entry.manifest_digest,
        "workflow_run_id": entry.workflow_run_id,
    }


def _journal_entry(value: object) -> PublicationJournalEntry:
    raw = _exact_mapping(value, set(_journal_payload(PublicationJournalEntry(
        1, "x", "x", PublicationStage.PREFLIGHT, WriteKind.BLOB, "x", None,
        WriteState.NOT_CREATED, "a/b", "a" * 40, "b" * 40, "c" * 64, 1,
    ))), "invalid_publication_journal")
    return PublicationJournalEntry(
        _int(raw["schema_version"]), _string(raw["run_id"]),
        _string(raw["operation_id"]), PublicationStage(_string(raw["stage"])),
        WriteKind(_string(raw["kind"])), _string(raw["target"]),
        None if raw["object_id"] is None else _string(raw["object_id"]),
        WriteState(_string(raw["state"])), _string(raw["repository"]),
        _string(raw["source_head_sha"]), _string(raw["target_base_sha"]),
        _string(raw["manifest_digest"]), _int(raw["workflow_run_id"]),
    )


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("invalid_publication_journal")
        result[key] = value
    return result


def _mapping(value: object, code: str) -> Mapping[str, object]:
    if type(value) is not dict:
        raise ValueError(code)
    return value


def _exact_mapping(value: object, keys: set[str], code: str) -> Mapping[str, object]:
    result = _mapping(value, code)
    if set(result) != keys:
        raise ValueError(code)
    return result


def _string(value: object, *, allow_empty: bool = False) -> str:
    if type(value) is not str or (not allow_empty and not value):
        raise ValueError("invalid_string")
    return value


def _int(value: object) -> int:
    if type(value) is not int:
        raise ValueError("invalid_integer")
    return value


def _bool(value: object) -> bool:
    if type(value) is not bool:
        raise ValueError("invalid_boolean")
    return value


def _run_payload(run: RunContext) -> dict[str, object]:
    from tools.qykw.phases import _run_payload as serialize

    return serialize(run)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
