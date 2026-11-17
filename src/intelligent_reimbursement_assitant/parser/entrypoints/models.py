"""Explicit deployment-time model download; never invoked by parsing jobs."""

import argparse
import logging
from pathlib import Path

from docling.datamodel.pipeline_options import LayoutObjectDetectionOptions
from docling.models.utils.hf_model_download import download_hf_model
from docling.utils.model_downloader import download_models


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare CPU layout/table/Chinese-English OCR models"
    )
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    # Fetch the selected CPU torch layout, not unused ONNX/VLM/image-caption engines.
    model = LayoutObjectDetectionOptions().model_spec
    download_hf_model(
        repo_id=model.repo_id,
        revision=model.revision,
        local_dir=args.directory / model.repo_id.replace("/", "--"),
    )
    download_models(
        output_dir=args.directory,
        with_layout=False,
        with_code_formula=False,
        with_picture_classifier=False,
        rapidocr_models=["onnxruntime:ch"],
    )


if __name__ == "__main__":
    main()
