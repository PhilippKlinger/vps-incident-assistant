from dataclasses import dataclass
from enum import StrEnum

from assistant.models import KumaEvent
from assistant.probe import ProbeResult

TLS_WARN_DAYS = 14


class Category(StrEnum):
    tls = "tls"
    dns = "dns"
    proxy_down = "proxy_down"
    upstream_502 = "upstream_502"
    app_error = "app_error"
    timeout = "timeout"
    health_degraded = "health_degraded"
    unknown = "unknown"


class Severity(StrEnum):
    critical = "critical"
    high = "high"


@dataclass(frozen=True)
class RuleResult:
    category: Category
    severity: Severity
    reason: str


def classify(event: KumaEvent, probe: ProbeResult | None) -> RuleResult:
    """First matching row wins; the category never comes from the LLM."""
    if probe is None:
        return RuleResult(
            Category.unknown,
            Severity.high,
            "Der Monitor hat keine Adresse, deshalb wurde keine Prüfung durchgeführt.",
        )
    if probe.error == "dns":
        return RuleResult(
            Category.dns, Severity.critical, "Der Domainname lässt sich nicht auflösen."
        )
    if probe.error == "tls":
        return RuleResult(
            Category.tls,
            Severity.critical,
            "Der TLS-Verbindungsaufbau schlägt fehl, das Zertifikat ist vermutlich ungültig.",
        )
    if probe.tls_days_left is not None and probe.tls_days_left < TLS_WARN_DAYS:
        return RuleResult(
            Category.tls,
            Severity.high,
            f"Das Zertifikat läuft in {probe.tls_days_left} Tagen ab.",
        )
    if probe.error == "refused":
        return RuleResult(
            Category.proxy_down,
            Severity.critical,
            "Die Verbindung wird abgelehnt, der Proxy nimmt keine Anfragen an.",
        )
    if probe.error == "timeout":
        return RuleResult(
            Category.timeout, Severity.high, "Die Adresse antwortet nicht innerhalb der Frist."
        )
    code = probe.status_code
    if code in (502, 503, 504):
        return RuleResult(
            Category.upstream_502,
            Severity.high,
            f"Die Adresse antwortet mit HTTP {code}, der Proxy erreicht die Anwendung nicht.",
        )
    if code is not None and code >= 500:
        return RuleResult(
            Category.app_error,
            Severity.high,
            f"Die Anwendung antwortet mit HTTP {code}.",
        )
    if event.status == "down" and code == 200 and (event.url or "").endswith("/api/health/"):
        return RuleResult(
            Category.health_degraded,
            Severity.high,
            "Der Health-Check antwortet mit HTTP 200, meldet aber einen Alarm.",
        )
    return RuleResult(
        Category.unknown, Severity.high, "Die Prüfung ergibt keine eindeutige Ursache."
    )
