from collections.abc import Callable
from pathlib import Path

from tokenizers import Tokenizer


def _load_tokenizer(tokenizer_dir: Path) -> Tokenizer:
    tokenizer = Tokenizer.from_file(str(tokenizer_dir / "tokenizer.json"))
    tokenizer.no_truncation()
    tokenizer.no_padding()
    return tokenizer


def load_token_counter(tokenizer_dir: Path) -> Callable[[str], int]:
    tokenizer = _load_tokenizer(tokenizer_dir)

    def count_tokens(text: str) -> int:
        return len(tokenizer.encode(text, add_special_tokens=False).ids)

    return count_tokens
