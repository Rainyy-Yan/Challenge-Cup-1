"""Safe materialization and deterministic replay of qykw text patches."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import weakref

from tools.qykw.change import (
    AppliedPatch,
    FileDigest,
    PatchManifest,
    PreparedWorkspace,
    compute_manifest_digest,
)


_REGULAR_MODE = "100644"
_HEX_DIGITS = frozenset("0123456789abcdef")
_TREE_DIGEST_DOMAIN = b"qykw-workspace-tree-v1\0"
_MAX_FILE_BYTES = 256 * 1024
_TRUSTED_WORKSPACES: dict[
    int,
    tuple[
        weakref.ReferenceType[PreparedWorkspace],
        Path,
        str,
        tuple[FileDigest, ...],
    ],
] = {}


def materialize_workspace(
    source_root: Path,
    *,
    source_head_sha: str,
    tracked_files: tuple[FileDigest, ...],
    destination: Path,
) -> PreparedWorkspace:
    """Copy one complete trusted UTF-8 tree into a newly created workspace."""

    _validate_source_head(source_head_sha)
    source = _require_real_directory(source_root, "invalid_source_root")
    destination_path = _require_new_destination(destination)
    trusted_files = _validate_file_digests(tracked_files)
    actual_paths = _scan_tree_paths(source, symlink_error="source_symlink_forbidden")
    expected_paths = tuple(item.path for item in trusted_files)
    if actual_paths != tuple(sorted(expected_paths)):
        raise ValueError("source_file_list_mismatch")

    source_bytes: dict[str, bytes] = {}
    for item in trusted_files:
        path = _safe_member(
            source, item.path, must_exist=True, symlink_error="source_symlink_forbidden"
        )
        content = _read_stable_bytes(
            path,
            root=source,
            symlink_error="source_symlink_forbidden",
            changed_error="source_file_changed",
        )
        _validate_utf8_text(content, "source")
        if not _same_digest(content, item.sha256):
            raise ValueError("source_digest_mismatch")
        source_bytes[item.path] = content

    destination_path.mkdir(mode=0o700)
    destination_root = destination_path.resolve(strict=True)
    for item in trusted_files:
        target = _safe_member(
            destination_root,
            item.path,
            must_exist=False,
            symlink_error="workspace_symlink_forbidden",
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_create(target, source_bytes[item.path], destination_root)

    copied = _digest_tree(
        destination_root,
        expected_paths,
        symlink_error="workspace_symlink_forbidden",
    )
    if copied != trusted_files:
        raise ValueError("workspace_copy_mismatch")
    workspace = PreparedWorkspace(destination_root, source_head_sha, trusted_files)
    workspace_id = id(workspace)

    def discard(_reference: object, *, key: int = workspace_id) -> None:
        _TRUSTED_WORKSPACES.pop(key, None)

    _TRUSTED_WORKSPACES[workspace_id] = (
        weakref.ref(workspace, discard),
        destination_root,
        source_head_sha,
        trusted_files,
    )
    return workspace


def apply_patch_manifest(
    manifest: PatchManifest, workspace: PreparedWorkspace
) -> AppliedPatch:
    """Replay a validated text manifest against one controller-owned workspace."""

    root, source_files = _require_trusted_workspace(workspace)
    _validate_manifest_binding(manifest, workspace)
    patches = _validate_manifest_paths(manifest)
    source_by_path = {item.path: item for item in source_files}
    source_paths = tuple(sorted(source_by_path))
    if _scan_tree_paths(root, symlink_error="workspace_symlink_forbidden") != source_paths:
        raise ValueError("workspace_file_list_changed")

    originals = {
        item.path: _read_and_match(root, item, "workspace_file_changed")
        for item in source_files
    }
    outputs: dict[str, bytes] = {}
    for patch in patches:
        if patch.create:
            if patch.path in source_by_path or _member_exists(root, patch.path):
                raise ValueError("create_path_exists")
            if patch.base_sha256 is not None:
                raise ValueError("new_file_base_digest_forbidden")
            if len(patch.edits) != 1 or patch.edits[0].before != "":
                raise ValueError("invalid_new_file_edit")
            output = _encode_output(patch.edits[0].after)
        else:
            baseline = source_by_path.get(patch.path)
            if baseline is None:
                raise ValueError("unknown_source_path")
            if patch.base_sha256 != baseline.sha256:
                raise ValueError("base_digest_mismatch")
            content = originals[patch.path].decode("utf-8")
            for edit in patch.edits:
                if not edit.before or edit.before == edit.after:
                    raise ValueError("invalid_text_edit")
                if content.count(edit.before) != 1:
                    raise ValueError("ambiguous_edit")
                content = content.replace(edit.before, edit.after, 1)
            output = _encode_output(content)
        outputs[patch.path] = output

    # Close the broad validation-to-write window before the first mutation.
    for item in source_files:
        _read_and_match(root, item, "workspace_file_changed")

    output_digests: list[FileDigest] = []
    for patch in patches:
        target = _safe_member(
            root,
            patch.path,
            must_exist=not patch.create,
            symlink_error="workspace_symlink_forbidden",
        )
        if patch.create:
            if target.exists() or target.is_symlink():
                raise ValueError("create_path_exists")
            target.parent.mkdir(parents=True, exist_ok=True)
            _atomic_create(target, outputs[patch.path], root)
        else:
            baseline = source_by_path[patch.path]
            _read_and_match(root, baseline, "workspace_file_changed")
            _atomic_replace(
                target,
                outputs[patch.path],
                root,
                expected=originals[patch.path],
            )
        written = _read_stable_bytes(
            target,
            root=root,
            symlink_error="workspace_symlink_forbidden",
            changed_error="workspace_file_changed",
        )
        if written != outputs[patch.path]:
            raise ValueError("workspace_write_mismatch")
        output_digests.append(digest_file(patch.path, written))

    final_paths = tuple(sorted(set(source_paths) | set(outputs)))
    final_files = _digest_tree(
        root,
        final_paths,
        symlink_error="workspace_symlink_forbidden",
    )
    if _scan_tree_paths(root, symlink_error="workspace_symlink_forbidden") != final_paths:
        raise ValueError("workspace_file_list_changed")
    final_by_path = {item.path: item for item in final_files}
    for path, baseline in source_by_path.items():
        if path not in outputs and final_by_path.get(path) != baseline:
            raise ValueError("workspace_file_changed")
    changed = tuple(sorted(output_digests, key=lambda item: item.path))
    return AppliedPatch(
        files=changed,
        output_tree_digest=compute_workspace_tree_digest(changed),
        workspace_tree_digest=compute_workspace_tree_digest(final_files),
    )


def compute_workspace_tree_digest(files: tuple[FileDigest, ...]) -> str:
    """Hash a canonical, complete path/mode/content-digest listing."""

    normalized = _validate_file_digests(files)
    payload = [
        {"mode": item.mode, "path": item.path, "sha256": item.sha256}
        for item in normalized
    ]
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(_TREE_DIGEST_DOMAIN + encoded).hexdigest()


def digest_file(path: str, content: bytes) -> FileDigest:
    """Build the only file mode accepted by the change channel."""

    return FileDigest(path, _REGULAR_MODE, hashlib.sha256(content).hexdigest())


def _validate_manifest_binding(
    manifest: PatchManifest, workspace: PreparedWorkspace
) -> None:
    if type(manifest) is not PatchManifest or manifest.schema_version != 1:
        raise ValueError("unsupported_manifest_schema")
    if manifest.source_head_sha != workspace.source_head_sha:
        raise ValueError("source_head_mismatch")
    if manifest.digest != compute_manifest_digest(manifest):
        raise ValueError("manifest_digest_mismatch")
    from tools.qykw.verification import get_verification_profile

    get_verification_profile(manifest.verification_profile)


def _validate_manifest_paths(manifest: PatchManifest) -> tuple[FilePatch, ...]:
    # Use the same deterministic path policy that admitted the manifest.  The
    # import remains local to avoid coupling module initialization order.
    from tools.qykw.policy import _normalize_change_path

    if not manifest.files:
        raise ValueError("empty_patch_manifest")
    seen: set[str] = set()
    for patch in manifest.files:
        normalized = _normalize_change_path(patch.path)
        key = _collision_key(normalized)
        if key in seen:
            raise ValueError("duplicate_patch_path")
        seen.add(key)
        if not patch.edits:
            raise ValueError("invalid_edit_count")
    return manifest.files


def _require_trusted_workspace(
    workspace: PreparedWorkspace,
) -> tuple[Path, tuple[FileDigest, ...]]:
    if type(workspace) is not PreparedWorkspace:
        raise ValueError("untrusted_workspace")
    record = _TRUSTED_WORKSPACES.get(id(workspace))
    if record is None or record[0]() is not workspace:
        raise ValueError("untrusted_workspace")
    _, root, head, files = record
    if (
        workspace.root != root
        or workspace.source_head_sha != head
        or workspace.source_files != files
        or root.is_symlink()
        or root.resolve(strict=True) != root
    ):
        raise ValueError("untrusted_workspace")
    return root, files


def _validate_file_digests(files: tuple[FileDigest, ...]) -> tuple[FileDigest, ...]:
    if type(files) is not tuple:
        raise TypeError("tracked_files_must_be_tuple")
    seen: set[str] = set()
    normalized: list[FileDigest] = []
    for item in files:
        if type(item) is not FileDigest:
            raise TypeError("invalid_file_digest")
        path = _normalize_path(item.path)
        key = _collision_key(path)
        if key in seen:
            error = "duplicate_file_path" if any(old.path == path for old in normalized) else "path_collision"
            raise ValueError(error)
        seen.add(key)
        if item.mode != _REGULAR_MODE:
            raise ValueError("unsupported_file_mode")
        if (
            type(item.sha256) is not str
            or len(item.sha256) != 64
            or any(character not in _HEX_DIGITS for character in item.sha256)
        ):
            raise ValueError("invalid_file_digest")
        normalized.append(FileDigest(path, item.mode, item.sha256))
    return tuple(sorted(normalized, key=lambda item: item.path))


def _normalize_path(value: object) -> str:
    from tools.qykw.policy import _normalize_repository_path

    normalized = _normalize_repository_path(value)  # type: ignore[arg-type]
    parts = normalized.split("/")
    if any(part.casefold() == ".git" for part in parts):
        raise ValueError("git_metadata_forbidden")
    return normalized


def _collision_key(path: str) -> str:
    return path.casefold()


def _require_real_directory(value: Path, error: str) -> Path:
    value = Path(value)
    try:
        if value.is_symlink() or not stat.S_ISDIR(value.lstat().st_mode):
            raise ValueError(error)
        resolved = value.resolve(strict=True)
    except (FileNotFoundError, OSError):
        raise ValueError(error) from None
    return resolved


def _require_new_destination(value: Path) -> Path:
    destination = Path(value).absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("destination_must_not_exist")
    try:
        parent = destination.parent.resolve(strict=True)
    except (FileNotFoundError, OSError):
        raise ValueError("invalid_destination_parent") from None
    if destination.parent != parent or destination.name in {"", ".", ".."}:
        raise ValueError("unsafe_destination_parent")
    return destination


def _scan_tree_paths(root: Path, *, symlink_error: str) -> tuple[str, ...]:
    paths: list[str] = []
    for current, directory_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current)
        if current_path == root and ".git" in directory_names:
            directory_names.remove(".git")
        if current_path == root and ".git" in file_names:
            file_names.remove(".git")
        for directory_name in directory_names:
            directory = current_path / directory_name
            if directory.is_symlink():
                raise ValueError(symlink_error)
            if not stat.S_ISDIR(directory.lstat().st_mode):
                raise ValueError("non_regular_source_entry")
        for file_name in file_names:
            file_path = current_path / file_name
            if file_path.is_symlink():
                raise ValueError(symlink_error)
            if not stat.S_ISREG(file_path.lstat().st_mode):
                raise ValueError("non_regular_source_entry")
            relative = file_path.relative_to(root).as_posix()
            paths.append(_normalize_path(relative))
    return tuple(sorted(paths))


def _safe_member(
    root: Path,
    relative: str,
    *,
    must_exist: bool,
    symlink_error: str,
) -> Path:
    parts = _normalize_path(relative).split("/")
    current = root
    for index, part in enumerate(parts):
        current = current / part
        exists = current.exists() or current.is_symlink()
        if exists:
            try:
                metadata = current.lstat()
            except OSError:
                raise ValueError("workspace_path_unavailable") from None
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError(symlink_error)
            if index < len(parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
                raise ValueError("workspace_parent_not_directory")
        elif must_exist:
            raise ValueError("workspace_path_missing")
    candidate = current
    existing_parent = candidate if candidate.exists() else candidate.parent
    while not existing_parent.exists() and existing_parent != root:
        existing_parent = existing_parent.parent
    try:
        resolved_parent = existing_parent.resolve(strict=True)
        resolved_parent.relative_to(root)
    except (FileNotFoundError, OSError, ValueError):
        raise ValueError("workspace_path_escape") from None
    return candidate


def _read_stable_bytes(
    path: Path,
    *,
    root: Path,
    symlink_error: str,
    changed_error: str,
) -> bytes:
    relative = path.relative_to(root).as_posix()
    checked = _safe_member(
        root, relative, must_exist=True, symlink_error=symlink_error
    )
    try:
        before = checked.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("non_regular_source_entry")
        with checked.open("rb") as handle:
            opened_before = os.fstat(handle.fileno())
            content = handle.read(_MAX_FILE_BYTES + 1)
            opened_after = os.fstat(handle.fileno())
        after = checked.lstat()
    except (FileNotFoundError, OSError):
        raise ValueError(changed_error) from None
    if len(content) > _MAX_FILE_BYTES:
        raise ValueError("source_file_too_large")
    if not _same_file(before, opened_before, opened_after, after):
        raise ValueError(changed_error)
    return content


def _same_file(*stats: os.stat_result) -> bool:
    first = stats[0]
    identity = (first.st_dev, first.st_ino, first.st_mode, first.st_size, first.st_mtime_ns)
    return all(
        (item.st_dev, item.st_ino, item.st_mode, item.st_size, item.st_mtime_ns)
        == identity
        for item in stats[1:]
    )


def _validate_utf8_text(content: bytes, label: str) -> None:
    if b"\x00" in content:
        raise ValueError(f"binary_{label}_file")
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"non_utf8_{label}_file") from None


def _same_digest(content: bytes, expected: str) -> bool:
    return hashlib.sha256(content).hexdigest() == expected


def _validate_source_head(value: object) -> None:
    if (
        type(value) is not str
        or len(value) not in {40, 64}
        or any(character not in _HEX_DIGITS for character in value)
    ):
        raise ValueError("invalid_source_head_sha")


def _encode_output(content: str) -> bytes:
    if type(content) is not str or not content:
        raise ValueError("empty_file_forbidden")
    try:
        encoded = content.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("non_utf8_text") from None
    if b"\x00" in encoded:
        raise ValueError("binary_content_forbidden")
    if len(encoded) > _MAX_FILE_BYTES:
        raise ValueError("change_file_too_large")
    return encoded


def _read_and_match(root: Path, item: FileDigest, error: str) -> bytes:
    path = _safe_member(
        root, item.path, must_exist=True, symlink_error="workspace_symlink_forbidden"
    )
    content = _read_stable_bytes(
        path,
        root=root,
        symlink_error="workspace_symlink_forbidden",
        changed_error=error,
    )
    _validate_utf8_text(content, "workspace")
    if not _same_digest(content, item.sha256):
        raise ValueError(error)
    return content


def _member_exists(root: Path, relative: str) -> bool:
    path = _safe_member(
        root,
        relative,
        must_exist=False,
        symlink_error="workspace_symlink_forbidden",
    )
    return path.exists() or path.is_symlink()


def _atomic_create(path: Path, content: bytes, root: Path) -> None:
    _atomic_write(path, content, root, replace=False, expected=b"")


def _atomic_replace(
    path: Path, content: bytes, root: Path, *, expected: bytes
) -> None:
    _atomic_write(path, content, root, replace=True, expected=expected)


def _atomic_write(
    path: Path,
    content: bytes,
    root: Path,
    *,
    replace: bool,
    expected: bytes,
) -> None:
    _safe_member(
        root,
        path.relative_to(root).as_posix(),
        must_exist=replace,
        symlink_error="workspace_symlink_forbidden",
    )
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=".qykw-", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            temporary.resolve(strict=True).relative_to(root)
            path.parent.resolve(strict=True).relative_to(root)
        except (FileNotFoundError, OSError, ValueError):
            raise ValueError("workspace_path_escape") from None
        if not replace and (path.exists() or path.is_symlink()):
            raise ValueError("create_path_exists")
        if replace:
            current = _read_stable_bytes(
                path,
                root=root,
                symlink_error="workspace_symlink_forbidden",
                changed_error="workspace_file_changed",
            )
            if current != expected:
                raise ValueError("workspace_file_changed")
            os.replace(temporary, path)
        else:
            # Hard-linking is an atomic no-overwrite publication on supported hosts.
            try:
                os.link(temporary, path)
            except FileExistsError:
                raise ValueError("create_path_exists") from None
            temporary.unlink()
    finally:
        if temporary.exists():
            temporary.unlink()


def _digest_tree(
    root: Path, paths: tuple[str, ...], *, symlink_error: str
) -> tuple[FileDigest, ...]:
    result: list[FileDigest] = []
    for relative in paths:
        path = _safe_member(
            root, relative, must_exist=True, symlink_error=symlink_error
        )
        content = _read_stable_bytes(
            path,
            root=root,
            symlink_error=symlink_error,
            changed_error="workspace_file_changed",
        )
        result.append(digest_file(relative, content))
    return tuple(sorted(result, key=lambda item: item.path))
