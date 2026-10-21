"""Fault-injection benchmark for DockerEnvironment reliability.

The benchmark deliberately triggers command timeouts and noisy submissions. It compares the
historical behavior with the hardened behavior without making model calls, then writes the raw
trial records and summary metrics to JSON.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from minisweagent.environments.docker import DockerEnvironment
from minisweagent.exceptions import Submitted


def count_orphans(env: DockerEnvironment, marker: str) -> int:
    result = subprocess.run(
        [
            env.config.executable,
            "exec",
            env.container_id,
            "bash",
            "-lc",
            f"pgrep -af {marker!r} | grep -v pgrep | wc -l",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    )
    return int(result.stdout.strip())


def kill_marker_processes(env: DockerEnvironment, marker: str) -> None:
    subprocess.run(
        [
            env.config.executable,
            "exec",
            env.container_id,
            "bash",
            "-lc",
            f"pkill -KILL -f {marker!r} >/dev/null 2>&1 || true",
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )


def run_timeout_trials(image: str, trials: int, hardened: bool) -> list[dict]:
    records: list[dict] = []
    env = DockerEnvironment(
        image=image,
        timeout=1,
        termination_grace_period=0.1,
        terminate_process_group_on_timeout=hardened,
    )
    try:
        for index in range(trials):
            marker = f"msa_fault_{'fixed' if hardened else 'baseline'}_{index}"
            command = (
                "python3 -c 'import signal,time; "
                "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                f"time.sleep(60)' {marker} & wait"
            )
            started = time.perf_counter()
            result = env.execute({"command": command})
            elapsed_ms = (time.perf_counter() - started) * 1000
            time.sleep(0.1)
            orphan_count = count_orphans(env, marker)
            records.append(
                {
                    "variant": "hardened" if hardened else "baseline",
                    "trial": index,
                    "returncode": result["returncode"],
                    "exception_type": result.get("extra", {}).get("exception_type"),
                    "orphan_processes": orphan_count,
                    "elapsed_ms": elapsed_ms,
                }
            )
            if orphan_count:
                kill_marker_processes(env, marker)
    finally:
        env.cleanup()
    return records


def legacy_marker_detected(output: str, returncode: int) -> bool:
    lines = output.lstrip().splitlines()
    return bool(lines and lines[0].strip() == "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" and returncode == 0)


def hardened_marker_detected(output: str, returncode: int) -> bool:
    env = object.__new__(DockerEnvironment)
    try:
        env._check_finished({"output": output, "returncode": returncode})
    except Submitted:
        return True
    return False


def run_marker_trials(trials: int) -> list[dict]:
    records = []
    for leading_lines in range(trials):
        prefix = "".join(f"startup noise {line}\n" for line in range(leading_lines))
        output = f"{prefix}COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\npatch\n"
        records.append(
            {
                "leading_lines": leading_lines,
                "baseline_detected": legacy_marker_detected(output, 0),
                "hardened_detected": hardened_marker_detected(output, 0),
            }
        )
    return records


def percentile_95(values: list[float]) -> float:
    if len(values) < 2:
        return values[0] if values else 0.0
    return statistics.quantiles(values, n=20, method="inclusive")[18]


def summarize(timeout_records: list[dict], marker_records: list[dict]) -> dict:
    summary: dict[str, object] = {}
    for variant in ("baseline", "hardened"):
        rows = [row for row in timeout_records if row["variant"] == variant]
        summary[f"{variant}_timeout_trials"] = len(rows)
        summary[f"{variant}_orphan_trials"] = sum(row["orphan_processes"] > 0 for row in rows)
        summary[f"{variant}_orphan_processes"] = sum(row["orphan_processes"] for row in rows)
        summary[f"{variant}_p95_elapsed_ms"] = round(percentile_95([row["elapsed_ms"] for row in rows]), 3)
    summary["baseline_submission_detection_rate"] = sum(
        row["baseline_detected"] for row in marker_records
    ) / len(marker_records)
    summary["hardened_submission_detection_rate"] = sum(
        row["hardened_detected"] for row in marker_records
    ) / len(marker_records)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="python:3.11")
    parser.add_argument("--trials", type=int, default=20)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    timeout_records = [
        *run_timeout_trials(args.image, args.trials, hardened=False),
        *run_timeout_trials(args.image, args.trials, hardened=True),
    ]
    marker_records = run_marker_trials(args.trials)
    payload = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "image": args.image,
        "trials_per_variant": args.trials,
        "summary": summarize(timeout_records, marker_records),
        "timeout_trials": timeout_records,
        "submission_marker_trials": marker_records,
    }
    output = args.output or Path("results") / f"docker-reliability-{int(time.time())}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(payload["summary"], indent=2))
    print(f"wrote {output}")


if __name__ == "__main__":
    main()
