"""Load the deployed BGE-M3 tokenizer JSON, including its special-token wrapper."""

from collections.abc import Callable
from pathlib import Path

from tokenizers import Tokenizer


def load_token_counter(tokenizer_dir: Path) -> Callable[[str], int]:
    # from_file only reads this local file; no hub access or alternative model.
    tokenizer = Tokenizer.from_file(str(tokenizer_dir / "tokenizer.json"))
    tokenizer.no_truncation()
    tokenizer.no_padding()

    def count_tokens(text: str) -> int:
        return len(tokenizer.encode(text, add_special_tokens=True).ids)

    return count_tokens
