"""Docling assembly and per-document conversion inside the owned parser process."""

import subprocess
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

from docling.backend.docx.drawingml.utils import (
    _build_soffice_command,
    _isolated_libreoffice_profile,
)
from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
from docling.datamodel.backend_options import MsExcelBackendOptions, MsWordBackendOptions
from docling.datamodel.base_models import (
    ConversionStatus,
    DocumentStream,
    FailureCategory,
    InputFormat,
)
from docling.datamodel.pipeline_options import (
    ConvertPipelineOptions,
    PdfPipelineOptions,
    RapidOcrOptions,
)
from docling.document_converter import (
    DocumentConverter,
    ExcelFormatOption,
    PdfFormatOption,
    WordFormatOption,
)
from pydantic import ValidationError

from intelligent_reimbursement_assitant.contracts.models import ArtifactRef
from intelligent_reimbursement_assitant.errors.codes import ErrorCode
from intelligent_reimbursement_assitant.errors.contracts import ExecutionError
from intelligent_reimbursement_assitant.parser.adapters.docling.excel import ExcelBackend
from intelligent_reimbursement_assitant.parser.adapters.docling.mapping import build_document
from intelligent_reimbursement_assitant.parser.adapters.docling.pdf import recover_pdf_structure
from intelligent_reimbursement_assitant.parser.adapters.docling.word import WordBackend
from intelligent_reimbursement_assitant.parser.domain.config import ParserSettings
from intelligent_reimbursement_assitant.parser.domain.models import ParsedDocument
from intelligent_reimbursement_assitant.scanner.domain.models import DOC, DOCX, PDF, XLSX


def build_converter(settings: ParserSettings) -> DocumentConverter:
    if settings.pdf_backend == "docling" and not settings.artifacts_path.is_dir():
        raise ExecutionError(
            ErrorCode.PARSER_CONFIGURATION_ERROR,
            "PERMANENT",
            "Local Docling models directory is missing",
        )
    common = dict(
        enable_remote_services=False,
        allow_external_plugins=False,
        accelerator_options=AcceleratorOptions(
            device=AcceleratorDevice.CPU, num_threads=settings.threads
        ),
        document_timeout=settings.timeout_seconds,
    )
    pdf = PdfPipelineOptions.model_validate(
        {
            **common,
            "artifacts_path": settings.artifacts_path,
            "do_ocr": True,
            "do_table_structure": True,
            "ocr_options": RapidOcrOptions(lang=["ch"], backend="onnxruntime"),
            "ocr_batch_size": 1,
            "layout_batch_size": 1,
            "table_batch_size": 1,
            "generate_page_images": False,
            "generate_picture_images": False,
            "generate_table_images": False,
        }
    )
    formats = [InputFormat.DOCX, InputFormat.XLSX]
    options = {
        InputFormat.DOCX: WordFormatOption(
            backend=WordBackend,
            pipeline_options=ConvertPipelineOptions.model_validate(common),
            backend_options=MsWordBackendOptions.model_validate(
                {"enable_local_fetch": False, "enable_remote_fetch": False}
            ),
        ),
        InputFormat.XLSX: ExcelFormatOption(
            backend=ExcelBackend,
            pipeline_options=ConvertPipelineOptions.model_validate(common),
            backend_options=MsExcelBackendOptions.model_validate(
                {"enable_local_fetch": False, "enable_remote_fetch": False}
            ),
        ),
    }
    if settings.pdf_backend == "docling":
        formats.insert(0, InputFormat.PDF)
        options[InputFormat.PDF] = PdfFormatOption(pipeline_options=pdf)
    return DocumentConverter(allowed_formats=formats, format_options=options)


def convert_document(
    raw: bytes,
    document_id: UUID,
    ref: ArtifactRef,
    settings: ParserSettings,
    converter: DocumentConverter,
) -> ParsedDocument:
    if ref.media_type == DOC:
        if settings.libreoffice_path is None or not settings.libreoffice_path.is_file():
            raise ExecutionError(
                ErrorCode.PARSER_CONFIGURATION_ERROR,
                "PERMANENT",
                "DOC requires a configured LibreOffice executable",
            )
        # The process entry routes tempfile defaults into the current document directory.
        with TemporaryDirectory() as working:
            path = Path(working) / "source.doc"
            path.write_bytes(raw)
            with _isolated_libreoffice_profile() as profile:
                command = _build_soffice_command(
                    str(settings.libreoffice_path),
                    profile,
                    target_format="docx",
                    outdir=working,
                    input_path=str(path),
                )
                completed = subprocess.run(
                    command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
            converted = path.with_suffix(".docx")
            if completed.returncode or not converted.is_file():
                raise ExecutionError(
                    ErrorCode.DOCUMENT_PARSE_FAILED,
                    "PERMANENT",
                    "LibreOffice could not convert this DOC original",
                )
            raw_input = converted.read_bytes()
        extension = ".docx"
    else:
        raw_input = raw
        extension = {PDF: ".pdf", DOCX: ".docx", XLSX: ".xlsx"}[ref.media_type]
    with BytesIO(raw_input) as stream:
        result = converter.convert(
            DocumentStream(name="source" + extension, stream=stream), raises_on_error=False
        )
        try:
            if result.has_timeout_errors():
                raise ExecutionError(
                    ErrorCode.DOCUMENT_PARSE_TIMEOUT,
                    "TASK_RETRYABLE",
                    "Docling conversion timed out",
                )
            if result.status == ConversionStatus.FAILURE:
                if result.has_errors(FailureCategory.UNKNOWN) or result.has_errors(
                    FailureCategory.INTERNAL
                ):
                    raise ExecutionError(
                        ErrorCode.EXECUTION_INTERNAL_ERROR,
                        "OUTCOME_UNKNOWN",
                        "Parser backend failed unexpectedly",
                    )
                raise ExecutionError(
                    ErrorCode.DOCUMENT_PARSE_FAILED, "PERMANENT", "Docling rejected the original"
                )
            try:
                if ref.media_type == PDF:
                    recover_pdf_structure(raw, result.document)
                return build_document(result, document_id, ref, raw)
            except ValidationError as exc:
                raise ExecutionError(
                    ErrorCode.PARSE_RESULT_INVALID,
                    "PERMANENT",
                    "Parser result structure is invalid",
                ) from exc
        finally:
            # PDF pipelines already unload their document/page backends; SimplePipeline does not.
            backend = result.input._backend
            if isinstance(backend, (WordBackend, ExcelBackend)):
                backend.unload()
