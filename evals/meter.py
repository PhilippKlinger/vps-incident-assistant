import subprocess
import threading
from collections.abc import Callable

_CGROUP = "/sys/fs/cgroup"
_MIB = 1024 * 1024
_USEC = 1_000_000


class NullMeter:
    def start(self) -> None:
        pass

    def stop(self) -> tuple[float | None, float | None]:
        return None, None


def _docker_read(container: str) -> Callable[[str], str]:
    def read(name: str) -> str:
        result = subprocess.run(
            ["docker", "exec", container, "cat", f"{_CGROUP}/{name}"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        return result.stdout

    return read


class DockerMeter:
    """CPU seconds and RAM peak of the Ollama container, read from its cgroup."""

    def __init__(
        self,
        container: str,
        read: Callable[[str], str] | None = None,
        interval: float = 0.5,
    ):
        self._read = read or _docker_read(container)
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cpu_start: int | None = None
        self._peak: int | None = None
        self._failed = False

    def _cpu_usec(self) -> int:
        for line in self._read("cpu.stat").splitlines():
            key, _, value = line.partition(" ")
            if key == "usage_usec":
                return int(value)
        raise ValueError("usage_usec missing in cpu.stat")

    def _sample_memory(self) -> None:
        current = int(self._read("memory.current").strip())
        self._peak = current if self._peak is None else max(self._peak, current)

    def _loop(self) -> None:
        # Sampling runs beside the request, so a failed read must not raise into it.
        while not self._stop.wait(self._interval):
            try:
                self._sample_memory()
            except (OSError, ValueError, subprocess.SubprocessError):
                self._failed = True

    def start(self) -> None:
        self._stop.clear()
        self._peak = None
        self._failed = False
        try:
            self._cpu_start = self._cpu_usec()
            self._sample_memory()
        except (OSError, ValueError, subprocess.SubprocessError):
            self._failed = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> tuple[float | None, float | None]:
        self._stop.set()
        if self._thread:
            self._thread.join()
        if self._failed or self._cpu_start is None:
            return None, None
        try:
            self._sample_memory()
            cpu = (self._cpu_usec() - self._cpu_start) / _USEC
        except (OSError, ValueError, subprocess.SubprocessError):
            return None, None
        if self._failed or self._peak is None:
            return None, None
        return round(cpu, 1), round(self._peak / _MIB, 1)
