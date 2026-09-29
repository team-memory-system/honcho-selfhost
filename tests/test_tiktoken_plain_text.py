import pytest
import tiktoken

import src.tiktoken_plain_text  # noqa: F401

TEXT = "tokenizer notes: <|fim_prefix|> def f(): <|fim_suffix|> <|endoftext|>"


@pytest.mark.parametrize("name", ["cl100k_base", "o200k_base"])
def test_special_token_text_encodes_as_plain_text(name: str):
    encoding = tiktoken.get_encoding(name)
    tokens = encoding.encode(TEXT)
    assert encoding.decode(tokens) == TEXT
    assert not set(tokens) & {
        encoding.encode_single_token(t) for t in encoding.special_tokens_set
    }


def test_explicit_disallowed_special_is_kept():
    with pytest.raises(ValueError):
        tiktoken.get_encoding("cl100k_base").encode(TEXT, disallowed_special="all")
