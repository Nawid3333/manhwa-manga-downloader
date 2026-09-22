"""Shared pytest fixtures for the test suite.

Every test here is hermetic: HTTP calls go through `httpx.MockTransport`
(the `mock_client` fixture) instead of the network -- the repo's own CI
comment notes these sites "cannot be reached reliably from a runner", so
nothing in this suite talks to a live site.
"""

from __future__ import annotations

import asyncio
import io
import sys
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip real backoff delays so retry-heavy tests run fast."""

    async def _instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant)


@pytest.fixture
def mock_client() -> Callable[..., httpx.AsyncClient]:
    """Factory for an AsyncClient wired to a MockTransport, not the network."""

    def _make(handler: Handler, **kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    return _make


@pytest.fixture(scope="session")
def jpeg_bytes() -> bytes:
    """A small but genuinely decodable JPEG.

    src/common.py now verifies downloads with a real Image.load(), not just
    a magic-byte sniff, so tests that want a download to be accepted need a
    real image -- a bare magic-byte prefix is exactly the case that check is
    meant to reject. Noise (not a solid fill) keeps the encoded size safely
    past _MIN_IMAGE_BYTES.
    """
    buf = io.BytesIO()
    Image.effect_noise((48, 48), 40).convert("RGB").save(buf, format="JPEG", quality=80)
    return buf.getvalue()
