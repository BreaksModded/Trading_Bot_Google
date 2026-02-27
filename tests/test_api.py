"""
Integration tests for the FastAPI API.

Tests JWT authentication, all major routes, and error handling.
Uses FastAPI's TestClient for real HTTP request simulation.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from api.app import app
from api.dependencies import app_state


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def setup_app_state(tmp_path, settings):
    """Set up minimal app state for testing."""
    from data.database import Database

    db = Database(tmp_path / "test_api.db")
    app_state.settings = settings
    app_state.db = db
    app_state.exchange = None
    app_state.strategy = None
    app_state.risk_manager = None
    yield
    app_state.settings = None
    app_state.db = None


@pytest.fixture
def client() -> TestClient:
    """Create a FastAPI test client."""
    return TestClient(app)


@pytest.fixture
def auth_headers(client: TestClient) -> dict[str, str]:
    """Get JWT authorization headers via login."""
    resp = client.post("/api/auth/login", json={
        "username": "admin",
        "password": "changeme",
    })
    assert resp.status_code == 200
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


# ── Auth Tests ────────────────────────────────────────────────────────


class TestAuth:
    """Tests for JWT authentication."""

    def test_login_success(self, client: TestClient):
        """Should return JWT token with valid credentials."""
        resp = client.post("/api/auth/login", json={
            "username": "admin",
            "password": "changeme",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"
        assert data["expires_in"] > 0

    def test_login_wrong_password(self, client: TestClient):
        """Should return 401 with wrong password."""
        resp = client.post("/api/auth/login", json={
            "username": "admin",
            "password": "wrong_password",
        })
        assert resp.status_code == 401

    def test_login_wrong_username(self, client: TestClient):
        """Should return 401 with wrong username."""
        resp = client.post("/api/auth/login", json={
            "username": "hacker",
            "password": "changeme",
        })
        assert resp.status_code == 401

    def test_protected_route_without_token(self, client: TestClient):
        """Should return 401 when accessing protected route without token."""
        resp = client.get("/api/auth/me")
        assert resp.status_code == 401

    def test_protected_route_with_token(self, client: TestClient, auth_headers: dict):
        """Should return user info with valid token."""
        resp = client.get("/api/auth/me", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["username"] == "admin"

    def test_invalid_token(self, client: TestClient):
        """Should return 401 with invalid token."""
        resp = client.get("/api/auth/me", headers={
            "Authorization": "Bearer invalid_token_here",
        })
        assert resp.status_code == 401


# ── Dashboard Routes Tests ────────────────────────────────────────────


class TestDashboardRoutes:
    """Tests for dashboard overview endpoints."""

    def test_get_status(self, client: TestClient, auth_headers: dict):
        """Should return bot status."""
        resp = client.get("/api/dashboard/status", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "status" in data
        assert "pnl" in data

    def test_bot_pause(self, client: TestClient, auth_headers: dict):
        """Should accept pause command."""
        resp = client.post("/api/dashboard/bot/pause", headers=auth_headers)
        assert resp.status_code == 200

    def test_bot_emergency(self, client: TestClient, auth_headers: dict):
        """Should accept emergency stop command."""
        resp = client.post("/api/dashboard/bot/emergency", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "orders_cancelled" in data


# ── Trading Routes Tests ──────────────────────────────────────────────


class TestTradingRoutes:
    """Tests for trading data endpoints."""

    def test_get_trades_empty(self, client: TestClient, auth_headers: dict):
        """Should return empty trades list from empty DB."""
        resp = client.get("/api/trading/trades?limit=10", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["trades"] == []
        assert data["total"] == 0

    def test_get_grid_empty(self, client: TestClient, auth_headers: dict):
        """Should return inactive grid state."""
        resp = client.get("/api/trading/grid", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["active"] is False

    def test_get_market_no_exchange(self, client: TestClient, auth_headers: dict):
        """Should handle missing exchange gracefully."""
        resp = client.get("/api/trading/market", headers=auth_headers)
        assert resp.status_code == 200
        assert "symbol" in resp.json()


# ── Performance Routes Tests ──────────────────────────────────────────


class TestPerformanceRoutes:
    """Tests for performance data endpoints."""

    def test_get_metrics(self, client: TestClient, auth_headers: dict):
        """Should return performance metrics."""
        resp = client.get("/api/performance/metrics?period=all", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "total_trades" in data

    def test_get_equity(self, client: TestClient, auth_headers: dict):
        """Should return equity curve data."""
        resp = client.get("/api/performance/equity?days=30", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "points" in data

    def test_get_daily_pnl(self, client: TestClient, auth_headers: dict):
        """Should return daily PnL data."""
        resp = client.get("/api/performance/daily-pnl?days=7", headers=auth_headers)
        assert resp.status_code == 200
        assert "data" in resp.json()


# ── Config Routes Tests ───────────────────────────────────────────────


class TestConfigRoutes:
    """Tests for configuration endpoints."""

    def test_get_config(self, client: TestClient, auth_headers: dict):
        """Should return current configuration."""
        resp = client.get("/api/config/current", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "num_levels" in data
        assert "min_spacing_pct" in data

    def test_update_config(self, client: TestClient, auth_headers: dict):
        """Should update configuration."""
        resp = client.put("/api/config/update", headers=auth_headers, json={
            "num_levels": 7,
            "order_size_usdt": 30.0,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["changes"]["num_levels"] == 7

    def test_preview_grid(self, client: TestClient, auth_headers: dict):
        """Should preview grid levels."""
        resp = client.post("/api/config/preview-grid", headers=auth_headers, json={
            "num_levels": 5,
            "min_spacing_pct": 0.01,
            "order_size_usdt": 25.0,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "buy_levels" in data
        assert "sell_levels" in data
        assert len(data["buy_levels"]) == 5


# ── Logs Routes Tests ─────────────────────────────────────────────────


class TestLogRoutes:
    """Tests for event log endpoints."""

    def test_get_events_empty(self, client: TestClient, auth_headers: dict):
        """Should return empty events list."""
        resp = client.get("/api/logs/events?hours=24", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["events"] == []

    def test_get_circuit_breakers(self, client: TestClient, auth_headers: dict):
        """Should return circuit breaker history."""
        resp = client.get("/api/logs/circuit-breakers", headers=auth_headers)
        assert resp.status_code == 200
        assert "events" in resp.json()

    def test_get_risk_status_no_manager(self, client: TestClient, auth_headers: dict):
        """Should handle missing risk manager gracefully."""
        resp = client.get("/api/logs/risk-status", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "error" in data  # Expected because risk_manager is None


# ── Backtest Routes Tests ─────────────────────────────────────────────


class TestBacktestRoutes:
    """Tests for backtesting endpoints."""

    def test_backtest_status_initial(self, client: TestClient, auth_headers: dict):
        """Should return initial backtest status."""
        resp = client.get("/api/backtest/status", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["running"] is False

    def test_backtest_results_empty(self, client: TestClient, auth_headers: dict):
        """Should return no results when nothing has run."""
        resp = client.get("/api/backtest/results", headers=auth_headers)
        assert resp.status_code == 200
