"""Health, ping, and uptime monitoring tests."""

from unittest.mock import patch
import pytest


async def test_root(client):
    resp = await client.get("/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert "version" in body["data"]


async def test_health_get(client):
    """Confirm GET /health returns status 200 and Cache-Control: no-store header."""
    resp = await client.get("/health")
    assert resp.status_code == 200
    cache_control = resp.headers.get("cache-control", "")
    assert "no-store" in cache_control
    assert "no-cache" in cache_control
    body = resp.json()
    assert body["status"] in {"ok", "degraded"}
    assert body["database"] in {"connected", "disconnected"}
    assert body["data"]["status"] in {"ok", "degraded"}


async def test_health_head(client):
    """Confirm HEAD /health explicitly returns status 200 and Cache-Control: no-store header."""
    resp = await client.head("/health")
    assert resp.status_code == 200
    cache_control = resp.headers.get("cache-control", "")
    assert "no-store" in cache_control
    assert len(resp.text) == 0


async def test_ping_get(client):
    """Confirm GET /ping returns status 200, ping: pong, and Cache-Control: no-store."""
    resp = await client.get("/ping")
    assert resp.status_code == 200
    cache_control = resp.headers.get("cache-control", "")
    assert "no-store" in cache_control
    body = resp.json()
    assert body["ping"] == "pong"
    assert body["status"] == "ok"


async def test_ping_head(client):
    """Confirm HEAD /ping returns status 200 and Cache-Control: no-store."""
    resp = await client.head("/ping")
    assert resp.status_code == 200
    cache_control = resp.headers.get("cache-control", "")
    assert "no-store" in cache_control
    assert len(resp.text) == 0


async def test_health_and_ping_api_v1_aliases(client):
    """Confirm /api/v1/health and /api/v1/ping aliases work for both GET and HEAD with 200."""
    # /api/v1/health
    r_h_get = await client.get("/api/v1/health")
    assert r_h_get.status_code == 200
    assert "no-store" in r_h_get.headers.get("cache-control", "")

    r_h_head = await client.head("/api/v1/health")
    assert r_h_head.status_code == 200
    assert "no-store" in r_h_head.headers.get("cache-control", "")

    # /api/v1/ping
    r_p_get = await client.get("/api/v1/ping")
    assert r_p_get.status_code == 200
    assert r_p_get.json()["ping"] == "pong"
    assert "no-store" in r_p_get.headers.get("cache-control", "")

    r_p_head = await client.head("/api/v1/ping")
    assert r_p_head.status_code == 200
    assert "no-store" in r_p_head.headers.get("cache-control", "")


async def test_health_resilience_when_db_down(client):
    """Always-200 resilience rule: even when database ping fails, /health must return HTTP 200 with degraded status."""
    with patch("app.main.ping", return_value=False):
        resp = await client.get("/health")
        assert resp.status_code == 200
        cache_control = resp.headers.get("cache-control", "")
        assert "no-store" in cache_control
        body = resp.json()
        assert body["status"] == "degraded"
        assert body["database"] == "disconnected"
        assert body["data"]["status"] == "degraded"
        assert body["data"]["database"] == "disconnected"


async def test_health_resilience_when_db_raises(client):
    """Always-200 resilience rule: even when database raises uncaught exception/timeout, /health must return HTTP 200."""
    with patch("app.main.ping", side_effect=RuntimeError("Connection timeout to Mongo")):
        resp = await client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "degraded"
        assert body["database"] == "disconnected"
