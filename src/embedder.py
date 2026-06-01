"""1024-d embeddings via Perplexity's `pplx-embed-v1-0.6b`.

Perplexity returns int8-quantized vectors as base64 (the API rejects a "float"
encoding format, so this is the lightest wire format available). We decode to
float32 by dividing by 127. pgvector's cosine distance is scale-invariant, so
the int8 quantization shows up only as a small precision loss — fine for
prototype-scale dedup; the cluster threshold is calibrated empirically per
corpus anyway (DESIGN.md §8).

Pre-prod: per DESIGN.md the production embedder is `voyage-3` (also 1024-d).
Swap is small — change `EMBEDDING_MODEL`, point the client at Voyage's API,
and add `VOYAGE_API_KEY`. Schema column doesn't move.
"""

import base64
import os
import struct
from typing import Iterable

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .config import EMBEDDING_MODEL


PERPLEXITY_EMBEDDINGS_URL = "https://api.perplexity.ai/v1/embeddings"


_client: httpx.Client | None = None


def _http() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(
            headers={
                "Authorization": f"Bearer {os.environ['PERPLEXITY_API_KEY']}",
                "Content-Type": "application/json",
            },
            timeout=60,
        )
    return _client


def _decode(b64: str) -> list[float]:
    raw = base64.b64decode(b64)
    # 'b' = signed char (int8). One byte per dimension → 1024 dims.
    return [x / 127.0 for x in struct.unpack(f"{len(raw)}b", raw)]


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, max=10),
    retry=retry_if_exception_type((httpx.RequestError, httpx.HTTPStatusError)),
    reraise=True,
)
def embed(texts: list[str]) -> list[list[float]]:
    """One request, batched. Empty input → empty output."""
    if not texts:
        return []
    resp = _http().post(
        PERPLEXITY_EMBEDDINGS_URL,
        json={
            "model": EMBEDDING_MODEL,
            "input": texts,
            "encoding_format": "base64_int8",
        },
    )
    resp.raise_for_status()
    body = resp.json()
    return [_decode(item["embedding"]) for item in body["data"]]


def embed_batched(texts: Iterable[str], batch_size: int = 128) -> list[list[float]]:
    out: list[list[float]] = []
    buf: list[str] = []
    for t in texts:
        buf.append(t)
        if len(buf) >= batch_size:
            out.extend(embed(buf))
            buf = []
    if buf:
        out.extend(embed(buf))
    return out
