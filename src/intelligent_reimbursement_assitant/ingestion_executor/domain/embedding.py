from intelligent_reimbursement_assitant.document_ingestion_service.domain.models import (
    Chunk,
    IndexFilterMetadata,
    IndexRecord,
    IndexRecordBatch,
)


def assemble_index_records(
    chunks: list[Chunk], vectors: list[list[float]], filter_fields: IndexFilterMetadata
) -> IndexRecordBatch:
    # The embedding adapter owns response validation; no second vector validation here.
    return [
        IndexRecord(chunk=chunk, dense_vector=vector, filter_fields=filter_fields)
        for chunk, vector in zip(chunks, vectors, strict=False)
    ]
