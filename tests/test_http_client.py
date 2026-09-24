import platform
import sys
from pathlib import Path

import distro
import pytest
import requests
from curl_cffi import Session as CurlSession

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from http_utils.http_client import HttpClient  # noqa: E402
from utils.utils import is_termux  # noqa: E402


def test_plain_linux_with_no_distro_like_uses_curl_cffi(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(distro, "like", lambda: "")
    monkeypatch.delenv("TERMUX_VERSION", raising=False)
    monkeypatch.delenv("PREFIX", raising=False)
    monkeypatch.delenv("TIKTOK_HTTP_BACKEND", raising=False)

    assert is_termux() is False
    client = HttpClient()
    try:
        assert isinstance(client.req, CurlSession)
    finally:
        client.req.close()
        client.req_stream.close()


def test_termux_prefix_is_detected(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setenv("PREFIX", "/data/data/com.termux/files/usr")
    monkeypatch.delenv("TERMUX_VERSION", raising=False)

    assert is_termux() is True


@pytest.mark.parametrize(
    ("backend", "detected_termux", "expected_type"),
    [
        ("curl_cffi", True, CurlSession),
        ("requests", False, requests.Session),
    ],
)
def test_http_backend_can_be_forced(
    monkeypatch, backend, detected_termux, expected_type
):
    monkeypatch.setenv("TIKTOK_HTTP_BACKEND", backend)
    monkeypatch.setattr(
        "http_utils.http_client.is_termux", lambda: detected_termux
    )

    client = HttpClient()
    try:
        assert isinstance(client.req, expected_type)
        assert isinstance(client.req_stream, requests.Session)
    finally:
        client.req.close()
        if client.req_stream is not client.req:
            client.req_stream.close()


def test_invalid_http_backend_is_rejected(monkeypatch):
    monkeypatch.setenv("TIKTOK_HTTP_BACKEND", "unknown")

    with pytest.raises(ValueError, match="TIKTOK_HTTP_BACKEND"):
        HttpClient()
