"""Offline parser bootstrap and serial request loop; no task-state or PG ownership."""

import gc
import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from shutil import rmtree
from uuid import UUID

from intelligent_reimbursement_assitant.contracts.models import ArtifactRef
from intelligent_reimbursement_assitant.errors.codes import ErrorCode
from intelligent_reimbursement_assitant.errors.contracts import ExecutionError
from intelligent_reimbursement_assitant.errors.execution import exception_diagnostic
from intelligent_reimbursement_assitant.parser.adapters.docling.protocol import (
    read_exactly,
    read_frame,
    write_frame,
)
from intelligent_reimbursement_assitant.parser.domain.config import ParserSettings

logger = logging.getLogger(__name__)


def _error_response(exc: Exception) -> tuple[bytes, bool]:
    if isinstance(exc, ExecutionError):
        error = exc
    elif isinstance(exc, (ImportError, FileNotFoundError)):
        error = ExecutionError(
            ErrorCode.PARSER_CONFIGURATION_ERROR, "PERMANENT", "Parser configuration is unavailable"
        )
    elif isinstance(exc, MemoryError):
        error = ExecutionError(
            ErrorCode.DOCUMENT_PARSE_RESOURCE_LIMIT, "PERMANENT", "Parser resource limit exceeded"
        )
    else:
        error = ExecutionError(
            ErrorCode.EXECUTION_INTERNAL_ERROR,
            "OUTCOME_UNKNOWN",
            "Unexpected parser execution failure",
        )
    logger.error(
        "parser_failed code=%s category=%s %s",
        error.code,
        error.category,
        exception_diagnostic(exc),
    )
    payload = json.dumps(
        {
            "error": {
                "code": error.code,
                "category": error.category,
                "message": error.message,
            }
        }
    ).encode()
    return payload, error.code not in {
        ErrorCode.DOCUMENT_PARSE_FAILED,
        ErrorCode.PARSE_RESULT_INVALID,
    }


def main() -> None:
    # Reserve the original pipe, then redirect fd 1 as well as Python output: native
    # libraries may write directly to stdout. Only framed responses use the reserved fd.
    with os.fdopen(os.dup(sys.stdout.fileno()), "wb") as output:
        os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
        sys.stdout = sys.stderr
        working = Path.cwd()
        tempfile.tempdir = str(working)
        try:
            configuration = read_frame(sys.stdin.buffer)
            if configuration is None:
                return
            settings = ParserSettings.model_validate_json(configuration)
            os.environ.update(
                HF_HUB_OFFLINE="1",
                TRANSFORMERS_OFFLINE="1",
                OMP_NUM_THREADS=str(settings.threads),
                MKL_NUM_THREADS=str(settings.threads),
            )
            # Delay SDK imports until the offline and thread environment is established.
            from intelligent_reimbursement_assitant.parser.adapters.docling.convert import (
                build_converter,
                convert_document,
            )

            converter = build_converter(settings)
            for input_format in converter.allowed_formats:
                # Initialization failures must not be flattened by raises_on_error=False.
                converter.initialize_pipeline(input_format)
        except Exception as exc:
            payload, _ = _error_response(exc)
            write_frame(output, payload)
            return
        write_frame(output, b'{"ready":true}')

        def convert(payload: bytes) -> bytes:
            request = json.loads(payload)
            document_id = UUID(request["document_id"])
            ref = ArtifactRef.model_validate(request["source_ref"])
            directory = tempfile.mkdtemp(prefix="document-", dir=working)
            safe_to_clean = False
            try:
                tempfile.tempdir = directory
                os.chdir(directory)
                raw = read_exactly(sys.stdin.buffer, ref.size_bytes)
                document = convert_document(raw, document_id, ref, settings, converter)
                response = json.dumps(
                    {"document": document.model_dump(mode="json")}, ensure_ascii=False
                ).encode()
                safe_to_clean = True
                return response
            except ExecutionError as exc:
                safe_to_clean = exc.code in {
                    ErrorCode.DOCUMENT_PARSE_FAILED,
                    ErrorCode.PARSE_RESULT_INVALID,
                }
                raise
            finally:
                tempfile.tempdir = str(working)
                os.chdir(working)
                # SDK timeouts can leave model calls running. Their files belong to
                # the parent until the complete process tree is confirmed stopped.
                if safe_to_clean:
                    try:
                        rmtree(directory)
                    except OSError as exc:
                        raise ExecutionError(
                            ErrorCode.EXECUTION_INTERNAL_ERROR,
                            "OUTCOME_UNKNOWN",
                            "Parser temporary resources could not be released",
                        ) from exc

        while (request := read_frame(sys.stdin.buffer)) is not None:
            retire = False
            try:
                response = convert(request)
            except Exception as exc:
                response, retire = _error_response(exc)
            del request
            # Office structures and exception tracebacks can form cycles; retain only models.
            gc.collect()
            write_frame(output, response)
            del response
            if retire:
                return


if __name__ == "__main__":
    main()
