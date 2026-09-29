"""Count special-token look-alikes in message text as ordinary text.

tiktoken's `Encoding.encode` raises `ValueError` by default when the input
contains the literal text of a special token such as `<|endoftext|>` or
`<|fim_prefix|>`. Honcho only ever encodes user content to count tokens or to
chunk it for embedding, so that text is never meant as a control token. Left
alone, a transcript that merely mentions one of these strings fails with HTTP
500 on every attempt to store it.

Patching the class once covers every encoder, including module-level ones
created before this import, without touching the upstream call sites. Callers
that pass `disallowed_special` explicitly keep their own value.
"""

import tiktoken

_encode = tiktoken.Encoding.encode

if not getattr(_encode, "_honcho_plain_text", False):

    def encode(self: tiktoken.Encoding, text: str, **kwargs: object) -> list[int]:
        kwargs.setdefault("disallowed_special", ())
        return _encode(self, text, **kwargs)  # pyright: ignore[reportArgumentType]

    encode._honcho_plain_text = True  # pyright: ignore[reportFunctionMemberAccess]
    tiktoken.Encoding.encode = encode  # pyright: ignore[reportAttributeAccessIssue]
