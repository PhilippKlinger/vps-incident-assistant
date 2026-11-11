import socket
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlparse

import httpx

TIMEOUT_SECONDS = 10
_KEPT_HEADERS = ("server", "content-type", "retry-after", "x-request-id")

ProbeError = Literal["refused", "timeout", "tls", "dns", "other"]


@dataclass(frozen=True)
class ProbeResult:
    status_code: int | None
    error: ProbeError | None
    latency_ms: int | None
    headers: dict[str, str]
    tls_days_left: int | None
    addresses: tuple[str, ...]

    def fact_texts(self) -> list[str]:
        texts = []
        if self.status_code is not None:
            texts.append(f"HTTP {self.status_code} in {self.latency_ms} ms")
        elif self.error == "refused":
            texts.append("Connection refused")
        elif self.error == "timeout":
            texts.append(f"Timed out after {TIMEOUT_SECONDS} s")
        elif self.error == "tls":
            texts.append("TLS handshake failed")
        elif self.error == "other":
            texts.append("Request failed")
        # Only the count goes into the text, never the addresses themselves.
        if not self.addresses:
            texts.append("DNS: no address")
        elif len(self.addresses) == 1:
            texts.append("DNS: 1 address")
        else:
            texts.append(f"DNS: {len(self.addresses)} addresses")
        if self.tls_days_left is not None:
            unit = "day" if self.tls_days_left == 1 else "days"
            texts.append(f"Certificate expires in {self.tls_days_left} {unit}")
        texts.extend(f"Header {name}: {value}" for name, value in self.headers.items())
        return texts


def probe_url(
    url: str,
    *,
    client: httpx.Client,
    tls_days: Callable[[str], int | None],
    resolve: Callable[[str], tuple[str, ...]],
) -> ProbeResult:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    addresses = resolve(host)
    if not addresses:
        return ProbeResult(None, "dns", None, {}, None, ())

    tls_days_left = _safe_tls_days(tls_days, host) if parsed.scheme == "https" else None
    started = time.monotonic()
    try:
        response = client.get(url, timeout=TIMEOUT_SECONDS, follow_redirects=False)
    except httpx.TimeoutException:
        return ProbeResult(None, "timeout", None, {}, tls_days_left, addresses)
    except httpx.ConnectError as exc:
        error = "tls" if _is_tls_error(exc) else "refused"
        return ProbeResult(None, error, None, {}, tls_days_left, addresses)
    except httpx.HTTPError:
        return ProbeResult(None, "other", None, {}, tls_days_left, addresses)

    latency_ms = round((time.monotonic() - started) * 1000)
    headers = {name: response.headers[name] for name in _KEPT_HEADERS if name in response.headers}
    return ProbeResult(response.status_code, None, latency_ms, headers, tls_days_left, addresses)


def _safe_tls_days(tls_days: Callable[[str], int | None], host: str) -> int | None:
    try:
        return tls_days(host)
    except Exception:
        return None


def _is_tls_error(exc: httpx.ConnectError) -> bool:
    if isinstance(exc.__cause__, ssl.SSLError):
        return True
    message = str(exc).lower()
    return "ssl" in message or "certificate" in message


def default_tls_days(host: str) -> int | None:
    try:
        context = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=5) as sock:
            with context.wrap_socket(sock, server_hostname=host) as tls:
                not_after = tls.getpeercert()["notAfter"]
        expires = datetime.fromtimestamp(ssl.cert_time_to_seconds(not_after), UTC)
        return (expires - datetime.now(UTC)).days
    except Exception:
        return None


def default_resolve(host: str) -> tuple[str, ...]:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return ()
    return tuple(dict.fromkeys(info[4][0] for info in infos))
