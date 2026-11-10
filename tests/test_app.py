from pathlib import Path

from fastapi.testclient import TestClient

from assistant.app import create_app
from tests.test_pipeline import Setup

FIXTURES = Path(__file__).parent / "fixtures"
SECRET = {"X-Webhook-Secret": "s"}


def _client(s: Setup) -> TestClient:
    return TestClient(create_app(s.deps))


def _body(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_webhook_rejects_missing_secret():
    response = _client(Setup()).post("/webhook/kuma", content=_body("kuma_down.json"))
    assert response.status_code == 401
    assert response.json() == {"detail": "unauthorized"}


def test_webhook_rejects_wrong_secret():
    s = Setup()
    response = _client(s).post(
        "/webhook/kuma", content=_body("kuma_down.json"), headers={"X-Webhook-Secret": "nope"}
    )
    assert response.status_code == 401
    assert s.store.requests == []


def test_webhook_ignores_test_message():
    s = Setup()
    response = _client(s).post("/webhook/kuma", content=_body("kuma_test.json"), headers=SECRET)
    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}
    assert s.store.requests == []


def test_webhook_ignores_invalid_json():
    response = _client(Setup()).post("/webhook/kuma", content=b"{nope", headers=SECRET)
    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}


def test_webhook_ignores_non_object():
    response = _client(Setup()).post("/webhook/kuma", content=b"[1]", headers=SECRET)
    assert response.json() == {"status": "ignored"}


def test_webhook_accepts_down():
    s = Setup()
    response = _client(s).post("/webhook/kuma", content=_body("kuma_down.json"), headers=SECRET)
    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}
    # TestClient runs the background task before returning.
    assert len(s.store.calls("POST", "/issues")) == 1


def test_healthz_and_no_docs():
    client = _client(Setup())
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/docs").status_code == 404
