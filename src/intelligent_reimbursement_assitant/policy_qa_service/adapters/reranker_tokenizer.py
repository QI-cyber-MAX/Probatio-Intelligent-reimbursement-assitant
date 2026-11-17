"""Count complete query/document pairs with the locally prepared BAAI tokenizer."""

from collections.abc import Callable
from pathlib import Path

from tokenizers import Tokenizer


def load_pair_token_counter(directory: Path) -> Callable[[str, str], int]:
    tokenizer = Tokenizer.from_file(str(directory / "tokenizer.json"))
    tokenizer.no_truncation()
    tokenizer.no_padding()
    return lambda question, document: len(
        tokenizer.encode(question, document, add_special_tokens=True).ids
    )
