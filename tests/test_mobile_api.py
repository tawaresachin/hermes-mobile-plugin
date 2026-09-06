"""Tests for hermes-mobile-plugin API endpoints."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from hermes_mobile_plugin.mobile_api import router


@pytest.fixture
def client():
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_health(client: TestClient):
    response = client.get("/hermes-mobile/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "version" in data


def test_pair_verify_invalid_token(client: TestClient):
    response = client.post("/hermes-mobile/pair/verify", json={
        "pairing_token": "invalid-token",
        "device_name": "Test Device"
    })
    assert response.status_code == 400


def test_generate_pairing_qr(client: TestClient):
    response = client.post("/hermes-mobile/pair/generate")
    assert response.status_code == 200
    data = response.json()
    assert "pairing_token" in data
    assert "pairing_url" in data
    assert data["expires_in"] == 300
