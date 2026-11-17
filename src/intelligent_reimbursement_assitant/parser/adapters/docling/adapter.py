"""Async parser port owning one persistent, serial conversion process tree."""

import asyncio
import errno
import json
import logging
import os
import signal
import sys
import time
from collections.abc import Awaitable, Callable
from importlib.metadata import version
from pathlib import Path
from shutil import rmtree
from tempfile import mkdtemp
from typing import cast
from uuid import UUID

import psutil

from intelligent_reimbursement_assitant.contracts.models import ArtifactRef
from intelligent_reimbursement_assitant.errors.codes import ErrorCode
from intelligent_reimbursement_assitant.errors.contracts import ExecutionError
from intelligent_reimbursement_assitant.parser.adapters.docling.protocol import frame, read_response
from intelligent_reimbursement_assitant.parser.domain.config import ParserSettings
from intelligent_reimbursement_assitant.parser.domain.models import ParsedDocument
from intelligent_reimbursement_assitant.scanner.ports.artifact_store import ArtifactStorePort

logger = logging.getLogger(__name__)


def _alive(process: psutil.Process) -> bool:
    try:
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _collect_tree(process: asyncio.subprocess.Process, owned: set[psutil.Process]) -> None:
    try:
        owned.add(psutil.Process(process.pid))
    except psutil.NoSuchProcess:
        pass
    for parent in tuple(owned):
        try:
            owned.update(parent.children(recursive=True))
        except psutil.NoSuchProcess:
            pass
    # The group can outlive its leader; detached Office descendants are tracked above.
    for pid in psutil.pids():
        try:
            if os.getpgid(pid) == process.pid:
                owned.add(psutil.Process(pid))
        except ProcessLookupError, psutil.NoSuchProcess:
            pass


def _signal_group(process: asyncio.subprocess.Process, signum: signal.Signals) -> None:
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        pass


async def _wait_stopped(owned: set[psutil.Process], deadline: float) -> list[psutil.Process]:
    # Avoid pidfd_open, which some Docker seccomp profiles reject.
    while live := [child for child in owned if _alive(child)]:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return live
        await asyncio.sleep(min(0.05, remaining))
    return []


async def _stop_tree(
    process: asyncio.subprocess.Process, owned: set[psutil.Process], grace: float
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + grace
    _collect_tree(process, owned)
    _signal_group(process, signal.SIGTERM)
    for child in owned:
        try:
            if _alive(child):
                child.terminate()
        except psutil.NoSuchProcess:
            pass
    await _wait_stopped(owned, deadline)
    _collect_tree(process, owned)
    _signal_group(process, signal.SIGKILL)
    for child in owned:
        try:
            if _alive(child):
                child.kill()
        except psutil.NoSuchProcess:
            pass
    try:
        await asyncio.wait_for(process.wait(), grace)
    except TimeoutError as exc:
        raise ExecutionError(
            ErrorCode.EXECUTION_INTERNAL_ERROR,
            "OUTCOME_UNKNOWN",
            "Parser resource stopping could not be confirmed",
        ) from exc
    if await _wait_stopped(owned, loop.time() + grace):
        raise ExecutionError(
            ErrorCode.EXECUTION_INTERNAL_ERROR,
            "OUTCOME_UNKNOWN",
            "Parser resource stopping could not be confirmed",
        )


class DoclingParserAdapter:
    def __init__(self, store: ArtifactStorePort, settings: ParserSettings):
        self.store = store
        self.settings = settings
        self._lock = asyncio.Lock()
        self._exited = asyncio.Event()
        self._process: asyncio.subprocess.Process | None = None
        self._directory: Path | None = None
        self._owned: set[psutil.Process] = set()
        self._observer: asyncio.Task[None] | None = None
        self._draining: asyncio.Task[None] | None = None
        self._peak_bytes = 0
        self._usable = False
        self._closed = False
        logger.info(
            "parser_configured docling=%s core=%s rapidocr=%s cpu_threads=%d",
            version("docling"),
            version("docling-core"),
            version("rapidocr"),
            settings.threads,
        )

    async def _observe_tree(self, process: asyncio.subprocess.Process) -> None:
        try:
            root = psutil.Process(process.pid)
            self._owned.add(root)
            while process.returncode is None:
                self._owned.update(root.children(recursive=True))
                rss = 0
                for child in tuple(self._owned):
                    try:
                        if _alive(child):
                            rss += child.memory_info().rss
                        else:
                            self._owned.discard(child)
                    except psutil.NoSuchProcess:
                        self._owned.discard(child)
                self._peak_bytes = max(self._peak_bytes, rss)
                await asyncio.sleep(0.05)
        except psutil.NoSuchProcess:
            pass
        finally:
            self._exited.set()

    async def _receive(self, process: asyncio.subprocess.Process) -> bytes:
        received = asyncio.create_task(read_response(cast(asyncio.StreamReader, process.stdout)))
        exited = asyncio.create_task(self._exited.wait())
        try:
            await asyncio.wait({received, exited}, return_when=asyncio.FIRST_COMPLETED)
            if received.done():
                return received.result()
            raise EOFError("Parser process exited before completing its response")
        finally:
            for task in (received, exited):
                if not task.done():
                    task.cancel()
            await asyncio.gather(received, exited, return_exceptions=True)

    async def _discard(self) -> None:
        graceful = self._closed and self._usable
        self._usable = False
        if self._process is not None:
            process = self._process
            reader = cast(asyncio.StreamReader, process.stdout)
            writer = cast(asyncio.StreamWriter, process.stdin)

            # A paused stdout transport can otherwise prevent Process.wait from finishing.
            async def drain() -> None:
                while await reader.read(65536):
                    pass

            if self._draining is None:
                self._draining = asyncio.create_task(drain())
            writer.close()
            if graceful:
                try:
                    await asyncio.wait_for(process.wait(), self.settings.terminate_seconds)
                except TimeoutError:
                    pass
            await _stop_tree(process, self._owned, self.settings.terminate_seconds)
            await self._draining
            try:
                await writer.wait_closed()
            except BrokenPipeError, ConnectionResetError:
                pass
            if self._observer is not None:
                await self._observer
            self._process = None
            self._observer = None
            self._draining = None
            self._owned.clear()
        if self._directory is not None:
            rmtree(self._directory)
            self._directory = None

    async def _stop_owned(self) -> None:
        # Keep cleanup owned even if cancellation arrives while waiting for termination.
        stopping = asyncio.create_task(self._discard())
        cancelled = False
        while not stopping.done():
            try:
                await asyncio.shield(stopping)
            except asyncio.CancelledError:
                cancelled = True
        stopping.result()
        if cancelled:
            raise asyncio.CancelledError

    async def aclose(self) -> None:
        async with self._lock:
            self._closed = True
            await self._stop_owned()

    async def _start(self) -> asyncio.subprocess.Process:
        if self._process is not None:
            if self._usable and self._process.returncode is None:
                return self._process
        if self._process is not None or self._directory is not None:
            await self._stop_owned()
        self.settings.temporary_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._directory = Path(mkdtemp(dir=self.settings.temporary_directory))
        started = time.monotonic()
        self._exited.clear()
        launching = asyncio.create_task(
            asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "intelligent_reimbursement_assitant.parser.entrypoints.worker",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=None,
                start_new_session=True,
                cwd=self._directory,
            )
        )
        try:
            process = await asyncio.shield(launching)
            self._process = process
        except asyncio.CancelledError:
            self._process = await launching
            self._observer = asyncio.create_task(self._observe_tree(self._process))
            raise
        self._observer = asyncio.create_task(self._observe_tree(process))
        writer = cast(asyncio.StreamWriter, process.stdin)
        writer.write(frame(self.settings.model_dump_json().encode()))
        await writer.drain()
        response = json.loads(await self._receive(process))
        if "error" in response:
            raise ExecutionError(**response["error"])
        if response != {"ready": True}:
            raise ValueError("Invalid parser initialization response")
        self._usable = True
        logger.info(
            "parser_process_ready pid=%d initialization_seconds=%.3f",
            process.pid,
            time.monotonic() - started,
        )
        return process

    async def _read_original(self, ref: ArtifactRef) -> bytes:
        def read() -> bytes:
            with self.store.open_artifact(ref) as source:
                return source.read()

        reading = asyncio.create_task(asyncio.to_thread(read))
        try:
            return await asyncio.shield(reading)
        except asyncio.CancelledError:
            await reading
            raise
        except (OSError, ValueError) as exc:
            temporary = isinstance(exc, OSError) and exc.errno in {
                errno.EAGAIN,
                errno.EBUSY,
                errno.EINTR,
                errno.ETIMEDOUT,
                errno.EIO,
            }
            raise ExecutionError(
                ErrorCode.PARSING_ARTIFACT_ERROR,
                "LOCAL_RETRYABLE" if temporary else "PERMANENT",
                "Parser original could not be read",
            ) from exc

    async def parse(
        self,
        document_id: UUID,
        source_ref: ArtifactRef,
        *,
        check_execution: Callable[[], Awaitable[bool]],
    ) -> ParsedDocument:
        async with self._lock:
            if not await check_execution():
                raise asyncio.CancelledError
            if self._closed:
                raise RuntimeError("Parser adapter is closed")
            started = time.monotonic()
            self._peak_bytes = 0
            try:
                raw = await self._read_original(source_ref)
                async with asyncio.timeout(self.settings.timeout_seconds):
                    process = await self._start()
                    writer = cast(asyncio.StreamWriter, process.stdin)
                    writer.write(
                        frame(
                            json.dumps(
                                {
                                    "document_id": str(document_id),
                                    "source_ref": source_ref.model_dump(mode="json"),
                                }
                            ).encode()
                        )
                    )
                    writer.write(raw)
                    await writer.drain()
                    payload = await self._receive(process)
                try:
                    result = json.loads(payload)
                    if "error" in result:
                        raise ExecutionError(**result["error"])
                    document = ParsedDocument.model_validate(result["document"])
                    if (
                        document.document_id != document_id
                        or document.source_hash != source_ref.sha256
                    ):
                        raise ValueError("Parser response belongs to another original")
                except (ValueError, KeyError, TypeError) as exc:
                    self._usable = False
                    raise ExecutionError(
                        ErrorCode.PARSE_RESULT_INVALID,
                        "PERMANENT",
                        "Parser result structure is invalid",
                    ) from exc
                logger.info(
                    "parse_completed document_id=%s source_hash=%s pid=%d coverage=%s elapsed_seconds=%.3f sampled_process_tree_peak_bytes=%d",
                    document_id,
                    source_ref.sha256,
                    process.pid,
                    document.parse_report.coverage_status,
                    time.monotonic() - started,
                    self._peak_bytes,
                )
                return document
            except TimeoutError as exc:
                await self._stop_owned()
                raise ExecutionError(
                    ErrorCode.DOCUMENT_PARSE_TIMEOUT,
                    "TASK_RETRYABLE",
                    "Single parsing call timed out",
                ) from exc
            except asyncio.CancelledError:
                await self._stop_owned()
                raise
            except ExecutionError as exc:
                if not self._usable or exc.code not in {
                    ErrorCode.DOCUMENT_PARSE_FAILED,
                    ErrorCode.PARSE_RESULT_INVALID,
                    ErrorCode.PARSING_ARTIFACT_ERROR,
                }:
                    await self._stop_owned()
                raise
            except (OSError, EOFError, asyncio.IncompleteReadError) as exc:
                process = self._process
                await self._stop_owned()
                logger.error(
                    "parser_process_failed document_id=%s source_hash=%s exit_code=%s elapsed_seconds=%.3f sampled_process_tree_peak_bytes=%d",
                    document_id,
                    source_ref.sha256,
                    process.returncode if process else None,
                    time.monotonic() - started,
                    self._peak_bytes,
                )
                raise ExecutionError(
                    ErrorCode.EXECUTION_INTERNAL_ERROR,
                    "OUTCOME_UNKNOWN",
                    f"Parser process exited without a result (exit={process.returncode if process else None})",
                ) from exc
            except BaseException:
                await self._stop_owned()
                raise
