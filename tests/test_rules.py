from datetime import UTC, datetime

from assistant.models import KumaEvent
from assistant.probe import ProbeResult
from assistant.rules import Category, Severity, classify


def _event(url="https://videoflix.example/", status="down"):
    return KumaEvent(
        monitor_name="Videoflix · API",
        url=url,
        status=status,
        msg="boom",
        time=datetime(2026, 11, 10, 12, 0, tzinfo=UTC),
    )


def _probe(status=None, error=None, days=60, addresses=("1.2.3.4",)):
    return ProbeResult(status, error, 20 if status else None, {}, days, addresses)


def test_classify_dns():
    result = classify(_event(), _probe(error="dns", addresses=()))
    assert (result.category, result.severity) == (Category.dns, Severity.critical)


def test_classify_tls_error():
    result = classify(_event(), _probe(error="tls"))
    assert (result.category, result.severity) == (Category.tls, Severity.critical)


def test_classify_tls_days():
    result = classify(_event(), _probe(status=200, days=13))
    assert (result.category, result.severity) == (Category.tls, Severity.high)
    assert classify(_event(), _probe(status=502, days=14)).category == Category.upstream_502


def test_classify_tls_days_reason_uses_singular_for_one_day():
    assert classify(_event(), _probe(status=200, days=1)).reason == (
        "The certificate expires in 1 day."
    )
    assert classify(_event(), _probe(status=200, days=13)).reason == (
        "The certificate expires in 13 days."
    )


def test_classify_proxy_down():
    result = classify(_event(), _probe(error="refused"))
    assert (result.category, result.severity) == (Category.proxy_down, Severity.critical)


def test_classify_timeout():
    result = classify(_event(), _probe(error="timeout"))
    assert (result.category, result.severity) == (Category.timeout, Severity.high)


def test_classify_upstream_502():
    for code in (502, 503, 504):
        result = classify(_event(), _probe(status=code))
        assert (result.category, result.severity) == (Category.upstream_502, Severity.high)
    assert "502" in classify(_event(), _probe(status=502)).reason


def test_classify_app_error():
    result = classify(_event(), _probe(status=500))
    assert (result.category, result.severity) == (Category.app_error, Severity.high)


def test_classify_health_degraded():
    event = _event(url="https://videoflix.example/api/health/")
    result = classify(event, _probe(status=200))
    assert (result.category, result.severity) == (Category.health_degraded, Severity.high)
    up = _event(url="https://videoflix.example/api/health/", status="up")
    assert classify(up, _probe(status=200)).category == Category.unknown


def test_classify_unknown():
    result = classify(_event(), _probe(status=200))
    assert (result.category, result.severity) == (Category.unknown, Severity.high)


def test_classify_without_probe_is_unknown():
    result = classify(_event(url=None), None)
    assert (result.category, result.severity) == (Category.unknown, Severity.high)
    assert result.reason.endswith(".")
