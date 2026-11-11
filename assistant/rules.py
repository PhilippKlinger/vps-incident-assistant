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
            "The monitor has no URL, so no probe was run.",
        )
    if probe.error == "dns":
        return RuleResult(
            Category.dns, Severity.critical, "DNS resolution fails for the host name."
        )
    if probe.error == "tls":
        return RuleResult(
            Category.tls,
            Severity.critical,
            "The TLS handshake fails; the certificate is probably invalid.",
        )
    if probe.tls_days_left is not None and probe.tls_days_left < TLS_WARN_DAYS:
        return RuleResult(
            Category.tls,
            Severity.high,
            f"The certificate expires in {probe.tls_days_left} days.",
        )
    if probe.error == "refused":
        return RuleResult(
            Category.proxy_down,
            Severity.critical,
            "The connection is refused; the proxy is not accepting requests.",
        )
    if probe.error == "timeout":
        return RuleResult(
            Category.timeout, Severity.high, "The URL does not respond within the timeout."
        )
    code = probe.status_code
    if code in (502, 503, 504):
        return RuleResult(
            Category.upstream_502,
            Severity.high,
            f"The URL returns HTTP {code}; the proxy cannot reach the upstream application.",
        )
    if code is not None and code >= 500:
        return RuleResult(
            Category.app_error,
            Severity.high,
            f"The application returns HTTP {code}.",
        )
    if event.status == "down" and code == 200 and (event.url or "").endswith("/api/health/"):
        return RuleResult(
            Category.health_degraded,
            Severity.high,
            "The health check returns HTTP 200 but reports a degraded state.",
        )
    return RuleResult(Category.unknown, Severity.high, "The probe found no clear cause.")
