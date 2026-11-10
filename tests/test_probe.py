import ssl

import httpx

from assistant.probe import ProbeResult, probe_url


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _probe(handler, *, tls_days=lambda host: None, resolve=lambda host: ("203.0.113.7",)):
    with _client(handler) as client:
        return probe_url(
            "https://app.example.com/health", client=client, tls_days=tls_days, resolve=resolve
        )


def test_probe_502():
    def handler(request):
        return httpx.Response(
            502, headers={"Server": "Caddy", "X-Other": "no", "Retry-After": "30"}
        )

    result = _probe(handler, tls_days=lambda host: 9)

    assert result.status_code == 502
    assert result.error is None
    assert result.latency_ms is not None
    assert result.headers == {"server": "Caddy", "retry-after": "30"}
    assert result.tls_days_left == 9
    assert result.addresses == ("203.0.113.7",)


def test_probe_connection_refused():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    result = _probe(handler)

    assert result.error == "refused"
    assert result.status_code is None


def test_probe_tls_error():
    def handler(request):
        raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate has expired")

    assert _probe(handler).error == "tls"


def test_probe_tls_error_from_cause():
    def handler(request):
        error = httpx.ConnectError("connect failed")
        error.__cause__ = ssl.SSLError("bad cert")
        raise error

    assert _probe(handler).error == "tls"


def test_probe_timeout():
    def handler(request):
        raise httpx.ReadTimeout("too slow")

    assert _probe(handler).error == "timeout"


def test_probe_dns_failure():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200)

    result = _probe(handler, resolve=lambda host: ())

    assert result.error == "dns"
    assert calls == []
    assert result.tls_days_left is None


def test_probe_tls_days_failure_is_none():
    def boom(host):
        raise OSError("no route")

    result = _probe(lambda request: httpx.Response(200), tls_days=boom)

    assert result.status_code == 200
    assert result.tls_days_left is None


def test_fact_texts_hide_addresses():
    result = ProbeResult(
        status_code=502,
        error=None,
        latency_ms=120,
        headers={"server": "Caddy"},
        tls_days_left=9,
        addresses=("203.0.113.7", "203.0.113.8"),
    )

    texts = result.fact_texts()

    assert "HTTP 502 nach 120 ms" in texts
    assert "Zertifikat noch 9 Tage gültig" in texts
    assert "DNS: 2 Adressen" in texts
    assert "Header server: Caddy" in texts
    assert not any("203.0.113" in text for text in texts)


def test_fact_texts_for_errors():
    def result(error, addresses=("203.0.113.7",)):
        return ProbeResult(None, error, None, {}, None, addresses).fact_texts()

    assert "Verbindung abgelehnt" in result("refused")
    assert "Zeitüberschreitung nach 10 s" in result("timeout")
    assert "TLS-Fehler beim Verbindungsaufbau" in result("tls")
    assert result("dns", ()) == ["DNS: keine Adresse"]
    assert "DNS: 1 Adresse" in result("refused")
