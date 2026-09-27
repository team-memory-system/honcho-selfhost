import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from src.config import EmbeddingModelConfig, settings
from src.embedding_client import (  # pyright: ignore[reportPrivateUsage]
    QUERY_CALL_PURPOSES,
    EmbeddingClient,
    _EmbeddingClient,
)
from src.telemetry.events import EmbeddingCallPurpose
from src.utils.types import embedding_call_purpose

# Purposes whose embedding input is stored content. Kept here rather than in
# src/ because production code only needs to know what a query looks like —
# this set exists so a new upstream purpose trips
# test_query_purposes_partition_upstream_taxonomy instead of being silently
# treated as content.
CONTENT_CALL_PURPOSES = frozenset(
    {
        "create_observations",
        "message_create",
        "vector_sync",
        "summary",
    }
)


class FakeOpenAIEmbeddingsAPI:
    def __init__(self, embedding: list[float]) -> None:
        self.embedding: list[float] = embedding
        self.calls: list[dict[str, Any]] = []

    async def create(
        self,
        *,
        model: str,
        input: str | list[str],
        **kwargs: Any,
    ) -> SimpleNamespace:
        call: dict[str, Any] = {"model": model, "input": input}
        call.update(kwargs)
        self.calls.append(call)
        if isinstance(input, list):
            data = [SimpleNamespace(embedding=self.embedding) for _ in input]
        else:
            data = [SimpleNamespace(embedding=self.embedding)]
        return SimpleNamespace(data=data)


class RecordingInnerClient:
    """Stands in for the transport-level client so the tests see exactly the
    text `EmbeddingClient` hands down."""

    def __init__(self) -> None:
        self.embedded: list[str] = []
        self.batched: list[list[str]] = []

    async def embed(self, query: str) -> list[float]:
        self.embedded.append(query)
        return [0.1, 0.2]

    async def simple_batch_embed(self, texts: list[str]) -> list[list[float]]:
        self.batched.append(list(texts))
        return [[0.1] for _ in texts]


@pytest.fixture
def recording_client(monkeypatch: pytest.MonkeyPatch) -> RecordingInnerClient:
    inner = RecordingInnerClient()
    monkeypatch.setattr(settings.EMBEDDING, "QUERY_INSTRUCTION", "find memories")
    monkeypatch.setattr(EmbeddingClient, "_get_client", lambda _self: inner)
    return inner


@pytest.mark.asyncio
async def test_query_purpose_applies_configured_retrieval_instruction(
    recording_client: RecordingInnerClient,
) -> None:
    with embedding_call_purpose("search_memory"):
        result = await EmbeddingClient().embed("coffee preferences")

    assert result == [0.1, 0.2]
    assert recording_client.embedded == [
        "Instruct: find memories\nQuery: coffee preferences"
    ]


@pytest.mark.asyncio
async def test_document_purpose_embeds_content_verbatim(
    recording_client: RecordingInnerClient,
) -> None:
    with embedding_call_purpose("message_create"):
        await EmbeddingClient().embed("the user rides a Trek Madone")

    assert recording_client.embedded == ["the user rides a Trek Madone"]


@pytest.mark.asyncio
async def test_absent_purpose_embeds_content_verbatim(
    recording_client: RecordingInnerClient,
) -> None:
    """No purpose in scope means no instruction — upstream behaviour. Keeps a
    new content path from picking up the prefix just because it wasn't tagged.
    """
    await EmbeddingClient().embed("untagged text")

    assert recording_client.embedded == ["untagged text"]


@pytest.mark.asyncio
async def test_batch_embed_applies_instruction_to_each_query(
    recording_client: RecordingInnerClient,
) -> None:
    with embedding_call_purpose("preference_extraction"):
        result = await EmbeddingClient().simple_batch_embed(["one", "two"])

    assert result == [[0.1], [0.1]]
    assert recording_client.batched == [
        ["Instruct: find memories\nQuery: one", "Instruct: find memories\nQuery: two"]
    ]


@pytest.mark.asyncio
async def test_unset_instruction_embeds_content_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inner = RecordingInnerClient()
    monkeypatch.setattr(settings.EMBEDDING, "QUERY_INSTRUCTION", None)
    monkeypatch.setattr(EmbeddingClient, "_get_client", lambda _self: inner)

    with embedding_call_purpose("search_memory"):
        await EmbeddingClient().embed("coffee preferences")

    assert inner.embedded == ["coffee preferences"]


#: The only `EmbeddingClient` methods whose text is a caller's retrieval query.
#: Everything else the class forwards is stored content, and must reach the
#: provider untouched — attaching the instruction to documents cancels the effect
#: out, and undoing that means re-embedding the whole corpus.
QUERY_EMBEDDING_METHODS = frozenset({"embed", "simple_batch_embed"})

#: Methods that hand stored content to the provider. Listed so that adding the
#: prefix to one of them fails here.
CONTENT_EMBEDDING_METHODS = frozenset({"batch_embed", "prepare_chunks"})


def _embedding_client_methods() -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """Every method of `EmbeddingClient`, read from the file itself."""
    source = Path("src/embedding_client.py").read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "EmbeddingClient":
            return {
                item.name: item
                for item in node.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
    raise AssertionError("EmbeddingClient is no longer a class in src/embedding_client.py")


def _takes_caller_text(method: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True when the method receives something from the caller.

    The property accessors also reach the inner client, but they pass nothing in,
    so there is no text for the instruction to attach to.
    """
    arguments = method.args
    names = [argument.arg for argument in [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]]
    return [name for name in names if name not in {"self", "cls"}] != []


def test_exactly_the_query_paths_route_through_prepare() -> None:
    """Guard the wiring, not just the taxonomy.

    `test_query_purposes_partition_upstream_taxonomy` checks that every upstream
    purpose is classified. It says nothing about whether `_prepare` is still
    called: dropping it from `embed` during an upstream merge would leave every
    test green while retrieval quietly loses the instruction. So assert the call
    sites by name, in both directions — a query path that stops wrapping fails
    here, and so does a content path that starts.
    """
    methods = _embedding_client_methods()
    forwarding = {
        name: ast.unparse(method)
        for name, method in methods.items()
        if not name.startswith("_") and _takes_caller_text(method)
        and "_get_client()" in ast.unparse(method)
    }
    wrapping = {name for name, body in forwarding.items() if "_prepare(" in body}

    assert wrapping == QUERY_EMBEDDING_METHODS, {
        "stopped wrapping": sorted(QUERY_EMBEDDING_METHODS - wrapping),
        "started wrapping": sorted(wrapping - QUERY_EMBEDDING_METHODS),
    }

    unclassified = set(forwarding) - QUERY_EMBEDDING_METHODS - CONTENT_EMBEDDING_METHODS
    assert not unclassified, (
        f"EmbeddingClient forwards to the provider through new methods {sorted(unclassified)}; "
        "add each to QUERY_EMBEDDING_METHODS or CONTENT_EMBEDDING_METHODS in this file"
    )

    assert "_prepare" in methods, "the instruction helper itself was removed"


@pytest.mark.asyncio
async def test_every_query_method_carries_the_instruction(
    recording_client: RecordingInnerClient,
) -> None:
    """The behavioural half of the same guard: each named query method really does
    prefix, so the static check above cannot pass on a `_prepare` that no longer
    prefixes anything."""
    client = EmbeddingClient()
    with embedding_call_purpose("search_memory"):
        await client.embed("한 건")
        await client.simple_batch_embed(["여러 건"])

    assert recording_client.embedded == ["Instruct: find memories\nQuery: 한 건"]
    assert recording_client.batched == [["Instruct: find memories\nQuery: 여러 건"]]
    assert QUERY_EMBEDDING_METHODS == {"embed", "simple_batch_embed"}, (
        "this test covers each method in QUERY_EMBEDDING_METHODS; extend it when that grows"
    )


def test_query_purposes_partition_upstream_taxonomy() -> None:
    """Guard on the one thing this design depends on: that every upstream
    embedding purpose is knowingly classified as query or content. If upstream
    renames a purpose or adds one, this fails at merge time instead of quietly
    dropping the instruction from a retrieval path.
    """
    upstream = {purpose.value for purpose in EmbeddingCallPurpose}

    assert upstream >= QUERY_CALL_PURPOSES, sorted(QUERY_CALL_PURPOSES - upstream)
    assert not QUERY_CALL_PURPOSES & CONTENT_CALL_PURPOSES

    unclassified = upstream - QUERY_CALL_PURPOSES - CONTENT_CALL_PURPOSES
    assert not unclassified, (
        f"upstream added embedding purposes {sorted(unclassified)}; classify each "
        "as query (src/embedding_client.py QUERY_CALL_PURPOSES) or content "
        "(CONTENT_CALL_PURPOSES in this file)"
    )


@pytest.mark.asyncio
async def test_openai_embedding_client_uses_configured_model_and_dimensions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_embeddings = FakeOpenAIEmbeddingsAPI([0.1] * 8)

    class FakeOpenAIClient:
        def __init__(self, *, api_key: str | None, base_url: str | None) -> None:
            self.api_key: str | None = api_key
            self.base_url: str | None = base_url
            self.embeddings: FakeOpenAIEmbeddingsAPI = fake_embeddings

    monkeypatch.setattr("src.embedding_client.AsyncOpenAI", FakeOpenAIClient)

    client = _EmbeddingClient(
        EmbeddingModelConfig(
            transport="openai",
            model="text-embedding-3-small",
            api_key="test-key",
            base_url="http://localhost:8000/v1",
        ),
        vector_dimensions=8,
        max_input_tokens=8192,
        max_tokens_per_request=300_000,
        send_dimensions=False,
    )

    embedding = await client.embed("hello world")

    assert embedding == [0.1] * 8
    assert fake_embeddings.calls == [
        {"model": "text-embedding-3-small", "input": ["hello world"]}
    ]


@pytest.mark.asyncio
async def test_openai_embedding_client_rejects_dimension_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_embeddings = FakeOpenAIEmbeddingsAPI([0.1] * 7)

    class FakeOpenAIClient:
        def __init__(self, *, api_key: str | None, base_url: str | None) -> None:
            self.embeddings: FakeOpenAIEmbeddingsAPI = fake_embeddings

    monkeypatch.setattr("src.embedding_client.AsyncOpenAI", FakeOpenAIClient)

    client = _EmbeddingClient(
        EmbeddingModelConfig(
            transport="openai",
            model="text-embedding-3-small",
            api_key="test-key",
        ),
        vector_dimensions=8,
        max_input_tokens=8192,
        max_tokens_per_request=300_000,
        send_dimensions=False,
    )

    with pytest.raises(ValueError, match="Embedding dimension mismatch"):
        await client.embed("hello world")


@pytest.mark.asyncio
async def test_gemini_embedding_client_uses_output_dimensionality(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    class FakeGeminiModels:
        async def embed_content(
            self,
            *,
            model: str,
            contents: str | list[str],
            config: dict[str, Any],
        ) -> SimpleNamespace:
            calls.append(
                {
                    "model": model,
                    "contents": contents,
                    "config": config,
                }
            )
            return SimpleNamespace(
                embeddings=[SimpleNamespace(values=[0.2] * 12)],
            )

    class FakeGeminiClient:
        def __init__(self, *, api_key: str | None, http_options: Any) -> None:
            self.api_key: str | None = api_key
            self.http_options: Any = http_options
            self.aio: Any = SimpleNamespace(models=FakeGeminiModels())

    monkeypatch.setattr("src.embedding_client.genai.Client", FakeGeminiClient)

    client = _EmbeddingClient(
        EmbeddingModelConfig(
            transport="gemini",
            model="gemini-embedding-001",
            api_key="gemini-key",
            base_url="https://gemini-proxy.example/v1beta",
        ),
        vector_dimensions=12,
        max_input_tokens=4096,
        max_tokens_per_request=300_000,
        send_dimensions=False,
    )

    embedding = await client.embed("hello world")

    assert embedding == [0.2] * 12
    assert calls == [
        {
            "model": "gemini-embedding-001",
            "contents": "hello world",
            "config": {"output_dimensionality": 12},
        }
    ]


def _build_openai_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    embedding: list[float],
    model: str,
    send_dimensions: bool,
    vector_dimensions: int,
) -> tuple[_EmbeddingClient, FakeOpenAIEmbeddingsAPI]:
    fake_embeddings = FakeOpenAIEmbeddingsAPI(embedding)

    class FakeOpenAIClient:
        def __init__(self, *, api_key: str | None, base_url: str | None) -> None:
            self.api_key: str | None = api_key
            self.base_url: str | None = base_url
            self.embeddings: FakeOpenAIEmbeddingsAPI = fake_embeddings

    monkeypatch.setattr("src.embedding_client.AsyncOpenAI", FakeOpenAIClient)

    client = _EmbeddingClient(
        EmbeddingModelConfig(
            transport="openai",
            model=model,
            api_key="test-key",
        ),
        vector_dimensions=vector_dimensions,
        max_input_tokens=8192,
        max_tokens_per_request=300_000,
        send_dimensions=send_dimensions,
    )
    return client, fake_embeddings


@pytest.mark.asyncio
async def test_openai_embed_forwards_dimensions_when_send_dimensions_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, fake = _build_openai_client(
        monkeypatch,
        embedding=[0.1] * 768,
        model="text-embedding-3-small",
        send_dimensions=True,
        vector_dimensions=768,
    )

    await client.embed("hello")

    assert fake.calls == [
        {
            "model": "text-embedding-3-small",
            "input": ["hello"],
            "dimensions": 768,
        }
    ]


@pytest.mark.asyncio
async def test_openai_embed_omits_dimensions_when_send_dimensions_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, fake = _build_openai_client(
        monkeypatch,
        embedding=[0.1] * 1536,
        model="text-embedding-3-small",
        send_dimensions=False,
        vector_dimensions=1536,
    )

    await client.embed("hello")

    assert fake.calls == [{"model": "text-embedding-3-small", "input": ["hello"]}]


@pytest.mark.asyncio
async def test_openai_simple_batch_embed_forwards_dimensions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, fake = _build_openai_client(
        monkeypatch,
        embedding=[0.1] * 768,
        model="text-embedding-3-small",
        send_dimensions=True,
        vector_dimensions=768,
    )

    await client.simple_batch_embed(["a", "b"])

    assert len(fake.calls) == 1
    assert fake.calls[0]["dimensions"] == 768
    assert fake.calls[0]["input"] == ["a", "b"]


@pytest.mark.asyncio
async def test_openai_batch_embed_forwards_dimensions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, fake = _build_openai_client(
        monkeypatch,
        embedding=[0.1] * 768,
        model="text-embedding-3-small",
        send_dimensions=True,
        vector_dimensions=768,
    )

    await client.batch_embed({"a": "hello", "b": "world"})

    assert len(fake.calls) == 1
    assert fake.calls[0]["dimensions"] == 768


def _build_embedding_settings(
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    """Construct a fresh EmbeddingSettings from the given env, isolated from os.environ."""
    from src.config import EmbeddingSettings

    for key in (
        "EMBEDDING_VECTOR_DIMENSIONS",
        "EMBEDDING_MODEL_CONFIG__MODEL",
        "EMBEDDING_MODEL_CONFIG__TRANSPORT",
        "EMBEDDING_MODEL_CONFIG__DIMENSIONS_MODE",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return EmbeddingSettings()


def test_resolve_send_dimensions_auto_default_dim_returns_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _build_embedding_settings({}, monkeypatch)
    assert s.resolve_send_dimensions() is False


def test_resolve_send_dimensions_auto_explicit_dim_returns_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _build_embedding_settings({"EMBEDDING_VECTOR_DIMENSIONS": "768"}, monkeypatch)
    assert s.resolve_send_dimensions() is True


def test_resolve_send_dimensions_auto_ada_002_returns_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _build_embedding_settings(
        {
            "EMBEDDING_VECTOR_DIMENSIONS": "1536",
            "EMBEDDING_MODEL_CONFIG__MODEL": "text-embedding-ada-002",
        },
        monkeypatch,
    )
    assert s.resolve_send_dimensions() is False


def test_resolve_send_dimensions_always_returns_true_regardless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _build_embedding_settings(
        {"EMBEDDING_MODEL_CONFIG__DIMENSIONS_MODE": "always"},
        monkeypatch,
    )
    assert s.resolve_send_dimensions() is True


def test_resolve_send_dimensions_always_overrides_ada_rejecting_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _build_embedding_settings(
        {
            "EMBEDDING_MODEL_CONFIG__DIMENSIONS_MODE": "always",
            "EMBEDDING_MODEL_CONFIG__MODEL": "text-embedding-ada-002",
        },
        monkeypatch,
    )
    assert s.resolve_send_dimensions() is True


def test_resolve_send_dimensions_never_returns_false_regardless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _build_embedding_settings(
        {
            "EMBEDDING_MODEL_CONFIG__DIMENSIONS_MODE": "never",
            "EMBEDDING_VECTOR_DIMENSIONS": "768",
        },
        monkeypatch,
    )
    assert s.resolve_send_dimensions() is False


@pytest.mark.asyncio
async def test_simple_batch_embed_respects_token_budget_per_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """simple_batch_embed must split inputs across requests so per-request token cap holds."""
    fake_embeddings = FakeOpenAIEmbeddingsAPI([0.5] * 4)

    class FakeOpenAIClient:
        def __init__(self, *, api_key: str | None, base_url: str | None) -> None:
            self.embeddings: FakeOpenAIEmbeddingsAPI = fake_embeddings

    monkeypatch.setattr("src.embedding_client.AsyncOpenAI", FakeOpenAIClient)

    # max_input_tokens=100 per single input; max_tokens_per_request=120 total,
    # so two ~80-token inputs must end up in *separate* requests.
    client = _EmbeddingClient(
        EmbeddingModelConfig(
            transport="openai",
            model="text-embedding-3-small",
            api_key="test-key",
            base_url=None,
        ),
        vector_dimensions=4,
        max_input_tokens=100,
        max_tokens_per_request=120,
        send_dimensions=False,
    )

    # "word " * 80 produces ~80 tokens with cl100k_base/the model encoding.
    long_a = ("alpha " * 80).strip()
    long_b = ("beta " * 80).strip()

    out = await client.simple_batch_embed([long_a, long_b])
    assert len(out) == 2
    # Per-request token cap forces two separate requests.
    assert len(fake_embeddings.calls) == 2


@pytest.mark.asyncio
async def test_simple_batch_embed_rejects_oversized_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inputs that exceed max_embedding_tokens must raise ValueError immediately."""
    fake_embeddings = FakeOpenAIEmbeddingsAPI([0.1] * 4)

    class FakeOpenAIClient:
        def __init__(self, *, api_key: str | None, base_url: str | None) -> None:
            self.embeddings: FakeOpenAIEmbeddingsAPI = fake_embeddings

    monkeypatch.setattr("src.embedding_client.AsyncOpenAI", FakeOpenAIClient)

    client = _EmbeddingClient(
        EmbeddingModelConfig(
            transport="openai",
            model="text-embedding-3-small",
            api_key="test-key",
            base_url=None,
        ),
        vector_dimensions=4,
        max_input_tokens=10,
        max_tokens_per_request=1000,
        send_dimensions=False,
    )

    too_long = ("word " * 50).strip()
    with pytest.raises(ValueError, match="maximum token limit"):
        await client.simple_batch_embed([too_long])


def test_prepare_chunks_returns_ordered_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """prepare_chunks must split oversized inputs using the same rules as batch_embed."""
    fake_embeddings = FakeOpenAIEmbeddingsAPI([0.1] * 4)

    class FakeOpenAIClient:
        def __init__(self, *, api_key: str | None, base_url: str | None) -> None:
            self.embeddings: FakeOpenAIEmbeddingsAPI = fake_embeddings

    monkeypatch.setattr("src.embedding_client.AsyncOpenAI", FakeOpenAIClient)

    client = _EmbeddingClient(
        EmbeddingModelConfig(
            transport="openai",
            model="text-embedding-3-small",
            api_key="test-key",
            base_url=None,
        ),
        vector_dimensions=4,
        max_input_tokens=10,
        max_tokens_per_request=1000,
        send_dimensions=False,
    )

    short_text = "hello"
    long_text = ("word " * 50).strip()

    out = client.prepare_chunks({"short": short_text, "long": long_text})

    assert out["short"] == [short_text]
    assert len(out["long"]) > 1
    # Order preserved
    assert isinstance(out["long"][0], str)
