"""Local clamd INSTREAM protocol and controlled diagnostic storage."""

import ipaddress
import json
import logging
import os
import re
import socket
import struct
import time
from pathlib import Path
from uuid import uuid4

from intelligent_reimbursement_assitant.errors.codes import ErrorCode
from intelligent_reimbursement_assitant.scanner.domain.errors import UploadFailure
from intelligent_reimbursement_assitant.scanner.domain.models import (
    QuarantinedUpload,
    ScanReport,
    ScanVerdict,
    UploadPolicy,
)
from intelligent_reimbursement_assitant.scanner.ports.artifact_store import ArtifactStorePort

logger = logging.getLogger(__name__)
_RESPONSE_LIMIT = 4096


def validate_clamd_configuration(path: Path, policy: UploadPolicy) -> None:
    """Check the same configuration mounted into the locally managed daemon."""
    with path.open("rb") as source:
        content = source.read(65537)
    if len(content) > 65536:
        raise ValueError("clamd configuration exceeds 64 KiB")
    options: dict[str, str] = {}
    for line in content.decode("utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        name, _, value = line.partition(" ")
        if name in options:
            raise ValueError(f"duplicate clamd option: {name}")
        options[name] = value.strip()
    for name in (
        "ScanPDF",
        "ScanOLE2",
        "ScanArchive",
        "AlertEncryptedDoc",
        "AlertEncryptedArchive",
        "AlertOLE2Macros",
        "AlertExceedsMax",
    ):
        if options.get(name, "").lower() != "yes":
            raise ValueError(f"clamd must enable {name}")

    def number(name: str) -> int:
        match = re.fullmatch(r"([0-9]+)([KMG]?)", options.get(name, ""), re.IGNORECASE)
        if match is None:
            raise ValueError(f"clamd must explicitly configure {name}")
        result = int(match[1]) * {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3}[match[2].upper()]
        if result <= 0:
            raise ValueError(f"clamd limit {name} must be finite and positive")
        return result

    if number("StreamMaxLength") < policy.max_file_bytes:
        raise ValueError("StreamMaxLength must cover the upload limit")
    if number("MaxFileSize") < max(policy.max_file_bytes, policy.max_container_member_bytes):
        raise ValueError("MaxFileSize must cover the upload and member limits")
    if number("MaxScanSize") < policy.max_container_expanded_bytes + policy.max_file_bytes:
        raise ValueError("MaxScanSize must cover expanded bytes plus the original input")
    if number("MaxFiles") <= policy.max_container_entries:
        raise ValueError("MaxFiles needs capacity for the input plus container entries")
    if number("MaxRecursion") != policy.max_container_depth:
        raise ValueError("MaxRecursion must match the container depth policy")
    for name, maximum in (
        ("MaxScanTime", policy.scan_timeout_seconds * 1000),
        ("ReadTimeout", policy.scan_timeout_seconds),
        ("CommandReadTimeout", policy.scan_timeout_seconds),
    ):
        if number(name) > maximum:
            raise ValueError(f"clamd {name} must fit the overall scan deadline")
    number("MaxThreads")
    number("MaxQueue")


def normalize_scan_response(response: bytes, version: str) -> ScanReport:
    """An exact clean record is the only response that can produce CLEAN."""
    if response == b"stream: OK\0":
        return ScanReport(verdict=ScanVerdict.CLEAN, reason_codes=[], scanner_version=version)
    if not response.endswith(b"\0") or response.count(b"\0") != 1:
        return ScanReport(
            verdict=ScanVerdict.INCOMPLETE,
            reason_codes=["INVALID_SCANNER_RESPONSE"],
            scanner_version=version,
        )
    text = response[:-1].decode("utf-8", "replace")
    if "INSTREAM size limit exceeded" in text and text.endswith(" ERROR"):
        reasons = ["SCAN_STREAM_TOO_LARGE"]
        verdict = ScanVerdict.INCOMPLETE
    elif re.fullmatch(r"stream: [^\r\n\x00]+ FOUND", text):
        signature = text[len("stream: ") : -len(" FOUND")]
        if signature.startswith("Heuristics.Limits.Exceeded"):
            verdict, reasons = ScanVerdict.INCOMPLETE, ["SCAN_RESOURCE_LIMIT"]
        elif signature == "Heuristics.Encrypted" or signature.startswith("Heuristics.Encrypted."):
            verdict, reasons = ScanVerdict.REJECTED, ["ENCRYPTED_FILE"]
        elif signature == "Heuristics.OLE2.ContainsMacros" or signature.startswith(
            "Heuristics.OLE2.ContainsMacros."
        ):
            verdict, reasons = ScanVerdict.REJECTED, ["MACROS_DETECTED"]
        else:
            verdict, reasons = ScanVerdict.REJECTED, ["MALWARE_DETECTED"]
    elif re.fullmatch(r"(?:stream: )?[^\r\n\x00]+ ERROR", text):
        verdict, reasons = ScanVerdict.INCOMPLETE, ["SCANNER_ERROR"]
    else:
        verdict, reasons = ScanVerdict.INCOMPLETE, ["INVALID_SCANNER_RESPONSE"]
    return ScanReport(verdict=verdict, reason_codes=reasons, scanner_version=version)


class _InvalidReply(Exception):
    def __init__(self, message: str, response: bytes):
        self.response = response[:_RESPONSE_LIMIT]
        super().__init__(message)


class ClamAVScanner:
    def __init__(
        self,
        store: ArtifactStorePort,
        *,
        config_file: Path,
        policy: UploadPolicy,
        diagnostic_dir: Path,
        unix_socket: Path | None = None,
        tcp_host: str = "127.0.0.1",
        tcp_port: int = 3310,
    ):
        validate_clamd_configuration(config_file, policy)
        if unix_socket is None and not ipaddress.ip_address(tcp_host).is_loopback:
            raise ValueError("clamd TCP must use a loopback IP address")
        if not 0 < tcp_port < 65536:
            raise ValueError("invalid clamd TCP port")
        diagnostic_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if diagnostic_dir.is_symlink() or diagnostic_dir.stat().st_mode & 0o077:
            raise ValueError("scanner diagnostics must use a private directory")
        self.store = store
        self.unix_socket = unix_socket
        self.tcp_host = tcp_host
        self.tcp_port = tcp_port
        self.diagnostic_dir = diagnostic_dir

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("scan deadline exhausted")
        return remaining

    def _connect(self, deadline: float) -> socket.socket:
        if self.unix_socket is not None:
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            address = str(self.unix_socket)
        else:
            family = socket.AF_INET6 if ":" in self.tcp_host else socket.AF_INET
            connection = socket.socket(family, socket.SOCK_STREAM)
            address = (self.tcp_host, self.tcp_port)
        try:
            connection.settimeout(self._remaining(deadline))
            connection.connect(address)
        except BaseException:
            connection.close()
            raise
        return connection

    def _send(self, connection: socket.socket, content: bytes, deadline: float) -> None:
        connection.settimeout(self._remaining(deadline))
        connection.sendall(content)

    def _reply(self, connection: socket.socket, deadline: float) -> bytes:
        chunks: list[bytes] = []
        size = 0
        while True:
            connection.settimeout(self._remaining(deadline))
            block = connection.recv(min(1024, _RESPONSE_LIMIT + 1 - size))
            if not block:
                break
            size += len(block)
            if size > _RESPONSE_LIMIT:
                raise _InvalidReply("response limit exceeded", b"".join(chunks) + block)
            chunks.append(block)
        response = b"".join(chunks)
        if not response.endswith(b"\0") or response.count(b"\0") != 1:
            raise _InvalidReply("incomplete or multiple records", response)
        return response

    def _version(self, deadline: float) -> tuple[str, str]:
        try:
            with self._connect(deadline) as connection:
                self._send(connection, b"zVERSION\0", deadline)
                reply = self._reply(connection, deadline)
            text = reply[:-1].decode("ascii")
            if text.startswith("ClamAV ") and all(32 <= ord(char) < 127 for char in text):
                return text, repr(reply)
            return "unknown", repr(reply)
        except (OSError, TimeoutError, _InvalidReply, UnicodeDecodeError) as exc:
            return "unknown", type(exc).__name__

    def _diagnostic(self, ref: str, record: dict[str, object], report: ScanReport | None) -> None:
        try:
            directory = os.open(self.diagnostic_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                fd = os.open(
                    f"{ref}.json",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory,
                )
            finally:
                os.close(directory)
            with os.fdopen(fd, "w", encoding="utf-8") as target:
                json.dump(record, target, ensure_ascii=True)
        except OSError as exc:
            logger.error("scan_diagnostic_write_failed stage=D02 detail_ref=%s", ref)
            if report is not None and report.verdict == ScanVerdict.CLEAN:
                raise UploadFailure(
                    ErrorCode.UPLOAD_IO_ERROR,
                    "无法保存扫描诊断。",
                    stage="D02",
                    retryable=True,
                    detail_ref=ref,
                ) from exc

    def scan(
        self, upload: QuarantinedUpload, *, deadline: float, detail_ref: str | None = None
    ) -> ScanReport:
        ref = detail_ref or uuid4().hex
        if re.fullmatch(r"[0-9a-f]{32}", ref) is None:
            raise ValueError("detail_ref must be a managed diagnostic key")
        started = time.monotonic()
        version, version_diagnostic = self._version(min(deadline, started + 1))
        raw = ""
        connected = False
        try:
            with (
                self.store.open_read(upload.temp_key) as source,
                self._connect(deadline) as connection,
            ):
                connected = True
                self._send(connection, b"zINSTREAM\0", deadline)
                while True:
                    try:
                        block = source.read(1024**2)
                    except OSError as exc:
                        raise UploadFailure(
                            ErrorCode.UPLOAD_IO_ERROR,
                            "无法读取待扫描文件。",
                            stage="D02",
                            retryable=True,
                        ) from exc
                    if not block:
                        break
                    self._send(connection, struct.pack("!I", len(block)) + block, deadline)
                self._send(connection, b"\0\0\0\0", deadline)
                response = self._reply(connection, deadline)
                raw = repr(response)
                report = normalize_scan_response(response, version)
        except UploadFailure as exc:
            self._diagnostic(
                ref,
                {
                    "stage": "D02",
                    "temp_key": upload.temp_key,
                    "detail_ref": ref,
                    "error_code": exc.error.code,
                    "scanner_version": version,
                    "version_response": version_diagnostic[:4096],
                    "scan_response": raw[:4096],
                },
                None,
            )
            raise UploadFailure(
                exc.error.code,
                exc.error.user_message,
                stage="D02",
                retryable=exc.error.retryable,
                detail_ref=ref,
            ) from exc
        except TimeoutError:
            raw = "TimeoutError"
            report = ScanReport(
                verdict=ScanVerdict.INCOMPLETE,
                reason_codes=["SCAN_TIMEOUT"],
                scanner_version=version,
            )
        except _InvalidReply as exc:
            raw = f"{exc}: {exc.response!r}"
            report = ScanReport(
                verdict=ScanVerdict.INCOMPLETE,
                reason_codes=["INVALID_SCANNER_RESPONSE"],
                scanner_version=version,
            )
        except OSError as exc:
            raw = type(exc).__name__
            report = ScanReport(
                verdict=ScanVerdict.INCOMPLETE,
                reason_codes=["SCANNER_ERROR" if connected else "SCANNER_UNAVAILABLE"],
                scanner_version=version,
            )
        self._diagnostic(
            ref,
            {
                "stage": "D02",
                "temp_key": upload.temp_key,
                "detail_ref": ref,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "verdict": report.verdict,
                "reason_codes": report.reason_codes,
                "scanner_version": version,
                "version_response": version_diagnostic[:4096],
                "scan_response": raw[:4096],
            },
            report,
        )
        logger.info(
            "scan_completed stage=D02 detail_ref=%s temp_key=%s verdict=%s elapsed_ms=%d",
            ref,
            upload.temp_key,
            report.verdict,
            int((time.monotonic() - started) * 1000),
        )
        return report
