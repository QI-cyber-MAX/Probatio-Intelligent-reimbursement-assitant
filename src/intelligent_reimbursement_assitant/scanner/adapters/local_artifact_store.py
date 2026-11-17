"""Private quarantine storage and on-demand durable original promotion."""

import errno
import hashlib
import logging
import os
import re
import stat
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import BinaryIO, Literal
from uuid import uuid4

from intelligent_reimbursement_assitant.contracts.models import ArtifactRef
from intelligent_reimbursement_assitant.errors.codes import ErrorCode
from intelligent_reimbursement_assitant.scanner.domain.errors import UploadFailure
from intelligent_reimbursement_assitant.scanner.domain.models import QuarantinedUpload

logger = logging.getLogger(__name__)
_KEY = re.compile(r"[0-9a-f]{32}\.upload\Z")


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    # Rapid chmod/write operations can leave both timestamps unchanged.
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode


class LocalArtifactStore:
    def __init__(
        self,
        quarantine_dir: Path,
        *,
        originals_dir: Path | None = None,
        parsed_dir: Path | None = None,
        chunk_bytes: int = 1024**2,
    ):
        if type(chunk_bytes) is not int or chunk_bytes <= 0:
            raise ValueError("chunk_bytes must be positive")
        quarantine_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if quarantine_dir.is_symlink() or not quarantine_dir.is_dir():
            raise ValueError("quarantine_dir must be a directory, not a symbolic link")
        if quarantine_dir.stat().st_mode & 0o077:
            raise ValueError("quarantine_dir must be private (mode 0700)")
        self.quarantine_dir = quarantine_dir.absolute()
        self.originals_dir = (originals_dir or quarantine_dir.parent / "originals").absolute()
        self.parsed_dir = (parsed_dir or quarantine_dir.parent / "parsed").absolute()
        self.chunks_dir = (quarantine_dir.parent / "chunks").absolute()
        if self.originals_dir == self.quarantine_dir:
            raise ValueError("quarantine and originals must be separate directories")
        self.wiki_dir = (quarantine_dir.parent / "wiki").absolute()
        self.chunk_bytes = chunk_bytes
        self._identities: dict[str, tuple[int, int, int, int, int, int]] = {}
        self._reception_started = False

    @contextmanager
    def _artifact_directory(self, path: Path) -> Iterator[int]:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if os.fstat(fd).st_mode & 0o077:
                raise PermissionError("artifact directory must be private")
            yield fd
        finally:
            os.close(fd)

    @contextmanager
    def open_artifact(self, ref: ArtifactRef) -> Iterator[BinaryIO]:
        prefix, _, name = ref.key.partition("/")
        if prefix == "originals" and _KEY.fullmatch(name):
            directory = self.originals_dir
        elif prefix == "parsed" and re.fullmatch(r"[0-9a-f]{64}\.json", name):
            directory = self.parsed_dir
        elif prefix == "chunks" and re.fullmatch(r"[0-9a-f]{64}\.json", name):
            directory = self.chunks_dir
        elif prefix == "wiki" and re.fullmatch(r"[0-9a-f]{64}\.json", name):
            directory = self.wiki_dir
        else:
            raise ValueError("invalid managed artifact key")
        with self._artifact_directory(directory) as directory_fd:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        with os.fdopen(fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError("artifact must be a regular file")
            yield source

    def write_artifact(
        self,
        content: bytes,
        *,
        media_type: str,
        artifact_kind: Literal["PARSED", "CHUNKED", "WIKI"],
    ) -> ArtifactRef:
        """Publish immutable JSON bytes before recording a PG reference.

        A published file survives IO/commit uncertainty. Do not delete content-addressed
        files online: another invocation may be about to adopt the same bytes.
        """
        if media_type != "application/json":
            raise ValueError("only JSON artifacts are supported")
        prefix, path = {
            "PARSED": ("parsed", self.parsed_dir),
            "CHUNKED": ("chunks", self.chunks_dir),
            "WIKI": ("wiki", self.wiki_dir),
        }[artifact_kind]
        digest = hashlib.sha256(content).hexdigest()
        name = f"{digest}.json"
        # Sync newly created directory ancestors, just as original promotion does.
        parents_to_sync = [path.parent]
        for parent in path.parents:
            if parent.exists():
                break
            parents_to_sync.append(parent.parent)
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = f".{uuid4().hex}.tmp"
        with self._artifact_directory(path) as directory:

            def confirm_content() -> None:
                existing_fd = os.open(
                    name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
                )
                with os.fdopen(existing_fd, "rb") as existing:
                    if not stat.S_ISREG(os.fstat(existing.fileno()).st_mode):
                        raise ValueError("existing artifact is not a regular file")
                    if existing.read() != content:
                        raise ValueError("existing artifact does not match its content address")
                    os.fsync(existing.fileno())

            def sync_entries() -> None:
                os.fsync(directory)
                for parent in parents_to_sync:
                    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    try:
                        os.fsync(parent_fd)
                    finally:
                        os.close(parent_fd)

            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            try:
                with os.fdopen(fd, "wb") as target:
                    target.write(content)
                    target.flush()
                    os.fchmod(target.fileno(), 0o400)
                    os.fsync(target.fileno())
                try:
                    os.link(
                        temporary,
                        name,
                        src_dir_fd=directory,
                        dst_dir_fd=directory,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    # This is a cross-process store boundary, not a repeated model check.
                    confirm_content()
                sync_entries()
            except OSError as failure:
                if failure.errno != errno.EIO:
                    raise
                # An IO error may follow a successful link/fsync. Reconcile this exact
                # content address before reporting an unknown write; never delete it.
                try:
                    confirm_content()
                    sync_entries()
                except (OSError, ValueError) as uncertainty:
                    raise failure from uncertainty
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except OSError:
                    logger.error("artifact_temporary_cleanup_failed name=%s", temporary)
        return ArtifactRef(
            key=f"{prefix}/{name}", sha256=digest, media_type=media_type, size_bytes=len(content)
        )

    @contextmanager
    def _directory(self) -> Iterator[int]:
        fd = os.open(self.quarantine_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            yield fd
        finally:
            os.close(fd)

    @staticmethod
    def _validate_key(temp_key: str) -> None:
        if _KEY.fullmatch(temp_key) is None:
            raise UploadFailure(ErrorCode.FILE_IDENTITY_MISMATCH, "隔离文件引用无效。")

    def stage_stream(
        self, stream: BinaryIO, *, max_bytes: int, deadline: float
    ) -> tuple[str, int, str]:
        self._reception_started = True
        temp_key = f"{uuid4().hex}.upload"
        created = False
        try:
            stream.seek(0)
            with self._directory() as directory:
                fd = os.open(
                    temp_key,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory,
                )
                created = True
                with os.fdopen(fd, "wb") as target:
                    total = 0
                    digest = hashlib.sha256()
                    while True:
                        if time.monotonic() >= deadline:
                            raise UploadFailure(
                                ErrorCode.UPLOAD_TIMEOUT,
                                "上传接收超时，请重试。",
                                retryable=True,
                            )
                        block = stream.read(self.chunk_bytes)
                        if time.monotonic() >= deadline:
                            raise UploadFailure(
                                ErrorCode.UPLOAD_TIMEOUT,
                                "上传接收超时，请重试。",
                                retryable=True,
                            )
                        if not block:
                            break
                        total += len(block)
                        if total > max_bytes:
                            raise UploadFailure(ErrorCode.UPLOAD_TOO_LARGE, "文件超过大小上限。")
                        target.write(block)
                        digest.update(block)
                    if total == 0:
                        raise UploadFailure(ErrorCode.INVALID_UPLOAD, "不能上传空文件。")
                    target.flush()
                    os.fchmod(target.fileno(), 0o400)
                    self._identities[temp_key] = _identity(os.fstat(target.fileno()))
            logger.info("quarantine_staged temp_key=%s size_bytes=%d", temp_key, total)
            return temp_key, total, digest.hexdigest()
        except BaseException as exc:
            if created:
                self.discard_after_failure(temp_key)
            if isinstance(exc, OSError):
                raise UploadFailure(
                    ErrorCode.UPLOAD_IO_ERROR, "文件接收或存储失败，请重试。", retryable=True
                ) from exc
            raise

    @contextmanager
    def open_read(self, temp_key: str) -> Iterator[BinaryIO]:
        self._validate_key(temp_key)
        try:
            with self._directory() as directory:
                fd = os.open(temp_key, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
            source = os.fdopen(fd, "rb")
        except OSError as exc:
            raise UploadFailure(
                ErrorCode.UPLOAD_IO_ERROR, "无法读取隔离文件。", retryable=True
            ) from exc
        with source:
            try:
                before = os.fstat(source.fileno())
            except OSError as exc:
                raise UploadFailure(
                    ErrorCode.UPLOAD_IO_ERROR, "无法检查隔离文件。", retryable=True
                ) from exc
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise UploadFailure(ErrorCode.FILE_IDENTITY_MISMATCH, "隔离文件身份异常。")
            expected = self._identities.get(temp_key)
            # A fresh store in the format-check subprocess has no staging snapshot.
            if expected is None and stat.S_IMODE(before.st_mode) != 0o400:
                raise UploadFailure(ErrorCode.FILE_IDENTITY_MISMATCH, "隔离文件权限异常。")
            if expected is not None and _identity(before) != expected:
                raise UploadFailure(ErrorCode.FILE_IDENTITY_MISMATCH, "隔离文件已发生变化。")
            # The context body may perform socket IO. Its failures belong to the
            # consumer and must not be relabelled as storage errors.
            yield source
            try:
                if _identity(os.fstat(source.fileno())) != _identity(before):
                    raise UploadFailure(ErrorCode.FILE_IDENTITY_MISMATCH, "隔离文件已发生变化。")
                with self._directory() as directory:
                    current = os.stat(temp_key, dir_fd=directory, follow_symlinks=False)
                if _identity(current) != _identity(before):
                    raise UploadFailure(ErrorCode.FILE_IDENTITY_MISMATCH, "隔离文件已被替换。")
            except OSError as exc:
                raise UploadFailure(
                    ErrorCode.UPLOAD_IO_ERROR, "无法复核隔离文件。", retryable=True
                ) from exc

    def discard(self, temp_key: str) -> None:
        self._validate_key(temp_key)
        with self._directory() as directory:
            try:
                os.unlink(temp_key, dir_fd=directory)
            except FileNotFoundError:
                pass
        self._identities.pop(temp_key, None)

    def discard_after_failure(self, temp_key: str) -> None:
        """Preserve the primary error and make failed cleanup observable."""
        try:
            self.discard(temp_key)
        except OSError:
            logger.error("quarantine_cleanup_failed temp_key=%s", temp_key)

    @contextmanager
    def _originals(self) -> Iterator[int]:
        fd = os.open(self.originals_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if os.fstat(fd).st_mode & 0o077:
                raise UploadFailure(ErrorCode.FILE_IDENTITY_MISMATCH, "原件目录必须为私有目录。")
            yield fd
        finally:
            os.close(fd)

    def promote(self, upload: QuarantinedUpload) -> ArtifactRef:
        """Link the scanned inode without overwrite; persist before PG can reference it."""
        self._validate_key(upload.temp_key)
        linked = False
        try:
            # Newly created directory entries must also survive before PG references them.
            parents_to_sync = [self.originals_dir.parent]
            for parent in self.originals_dir.parents:
                if parent.exists():
                    break
                parents_to_sync.append(parent.parent)
            self.originals_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            with self._directory() as quarantine, self._originals() as originals:
                # Managed paths can change at the filesystem boundary; never link a symlink.
                info = os.stat(upload.temp_key, dir_fd=quarantine, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or _identity(info) != self._identities.get(
                    upload.temp_key
                ):
                    raise UploadFailure(ErrorCode.FILE_IDENTITY_MISMATCH, "隔离文件已发生变化。")
                os.link(
                    upload.temp_key,
                    upload.temp_key,
                    src_dir_fd=quarantine,
                    dst_dir_fd=originals,
                    follow_symlinks=False,
                )
                linked = True
                fd = os.open(upload.temp_key, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=originals)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.fsync(originals)
                for parent in parents_to_sync:
                    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    try:
                        os.fsync(parent_fd)
                    finally:
                        os.close(parent_fd)
            return ArtifactRef(
                key=f"originals/{upload.temp_key}",
                sha256=upload.sha256,
                media_type=upload.detected_type,
                size_bytes=upload.size_bytes,
            )
        except BaseException as exc:
            if linked:
                try:
                    self.discard_artifact(f"originals/{upload.temp_key}")
                except OSError:
                    logger.error("original_cleanup_failed key=originals/%s", upload.temp_key)
            if isinstance(exc, OSError):
                raise UploadFailure(
                    ErrorCode.UPLOAD_IO_ERROR, "原件固化失败，请重试。", retryable=True
                ) from exc
            raise

    def discard_artifact(self, artifact_key: str) -> None:
        prefix, separator, name = artifact_key.partition("/")
        if prefix != "originals" or not separator or _KEY.fullmatch(name) is None:
            raise UploadFailure(ErrorCode.FILE_IDENTITY_MISMATCH, "原件引用无效。")
        with ExitStack() as stack:
            try:
                directory = stack.enter_context(self._originals())
            except FileNotFoundError:
                return
            try:
                os.unlink(name, dir_fd=directory)
            except FileNotFoundError:
                return
            os.fsync(directory)

    def cleanup_unreferenced_originals(
        self, *, cutoff_timestamp: float, referenced_keys: set[str]
    ) -> list[str]:
        """Only during a quiescent maintenance window, after a successful PG snapshot."""
        if cutoff_timestamp >= time.time():
            raise ValueError("expiry cutoff must be in the past")
        removed = []
        with ExitStack() as stack:
            try:
                directory = stack.enter_context(self._originals())
            except FileNotFoundError:
                return removed
            for name in os.listdir(directory):
                key = f"originals/{name}"
                if _KEY.fullmatch(name) is None or key in referenced_keys:
                    continue
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff_timestamp:
                    os.unlink(name, dir_fd=directory)
                    removed.append(key)
                    logger.info("unreferenced_original_removed key=%s", key)
            os.fsync(directory)
        return removed

    def cleanup_expired_before_startup(self, *, cutoff_timestamp: float) -> list[str]:
        """Single-process startup only: remove expired managed regular files.

        Invoke before accepting requests. Never sweep during reception, or in a
        second API process sharing the quarantine. The caller defines expiry.
        """
        if self._reception_started:
            raise RuntimeError("quarantine cleanup must precede upload reception")
        if cutoff_timestamp >= time.time():
            raise ValueError("expiry cutoff must be in the past")
        removed: list[str] = []
        with self._directory() as directory:
            for name in os.listdir(directory):
                if _KEY.fullmatch(name) is None:
                    continue
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_mtime >= cutoff_timestamp:
                    continue
                os.unlink(name, dir_fd=directory)
                removed.append(name)
                logger.info("quarantine_expired_removed temp_key=%s", name)
        return removed
