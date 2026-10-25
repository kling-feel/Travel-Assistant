"""Sample memory and CPU of the isolated 2 GiB Compose project only."""

import json
import os
from pathlib import Path
import re
import subprocess
import time


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data/generated/p34b-2g-metrics.jsonl"
STOP = ROOT / "data/generated/p34b-2g-monitor.stop"
DOCKER = os.environ.get("TRAVEL_DOCKER_CLI", "docker")
PROJECT = "travel-p34b-2g"


def command(*arguments):
    return subprocess.run([DOCKER, *arguments], capture_output=True, text=True,
                          check=True, timeout=20).stdout


def mib(raw):
    match = re.match(r"\s*([\d.]+)(B|KiB|MiB|GiB|TiB)\b", raw)
    if not match:
        raise ValueError(f"Unrecognized memory unit: {raw[:32]}")
    return float(match.group(1)) * {"B": 1 / 1048576, "KiB": 1 / 1024,
                                    "MiB": 1, "GiB": 1024, "TiB": 1048576}[match.group(2)]


def main():
    if STOP.exists():
        raise RuntimeError("Capacity monitor stop marker exists")
    deadline = time.monotonic() + 1200
    with OUTPUT.open("a" if OUTPUT.exists() else "x", encoding="utf-8") as output:
        while not STOP.exists() and time.monotonic() < deadline:
            names = command("ps", "--filter", f"label=com.docker.compose.project={PROJECT}",
                            "--format", "{{.Names}}").splitlines()
            sample = {"time": time.time(), "containers": {}, "total_mib": 0.0}
            if names:
                try:
                    rows = command("stats", "--no-stream", "--format", "{{json .}}", *names)
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                    # A one-shot setup/admin container can disappear between ps and stats.
                    time.sleep(1)
                    continue
                for row in rows.splitlines():
                    item = json.loads(row)
                    usage = mib(item["MemUsage"].split("/", 1)[0])
                    sample["containers"][item["Name"]] = {
                        "memory_mib": round(usage, 2),
                        "cpu_percent": item["CPUPerc"],
                    }
                    sample["total_mib"] += usage
            sample["total_mib"] = round(sample["total_mib"], 2)
            output.write(json.dumps(sample, sort_keys=True) + "\n")
            output.flush()
            time.sleep(1)
    print("Isolated 2 GiB resource sampling complete")


if __name__ == "__main__":
    main()
