import time

from evals.meter import DockerMeter, NullMeter

MIB = 1024 * 1024


def test_null_meter_reports_nothing():
    meter = NullMeter()

    meter.start()

    assert meter.stop() == (None, None)


def test_docker_meter_reports_cpu_delta_and_memory_peak():
    cpu = iter([1_000_000, 3_500_000])
    memory = iter([100, 300, 200])
    memory_reads = []

    def read(name: str) -> str:
        if name == "cpu.stat":
            return f"usage_usec {next(cpu)}\nuser_usec 1\nsystem_usec 2\n"
        memory_reads.append(name)
        return f"{next(memory, 200) * MIB}\n"

    meter = DockerMeter("ollama", read=read, interval=0.001)
    meter.start()
    deadline = time.monotonic() + 5
    while len(memory_reads) < 3 and time.monotonic() < deadline:
        time.sleep(0.001)

    assert meter.stop() == (2.5, 300.0)


def test_docker_meter_gives_up_quietly_when_the_container_is_unreadable():
    def read(name: str) -> str:
        raise OSError("no such container")

    meter = DockerMeter("ollama", read=read, interval=0.001)
    meter.start()

    assert meter.stop() == (None, None)
