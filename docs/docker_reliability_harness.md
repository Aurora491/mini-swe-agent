# Docker execution reliability harness

This fork extends mini-swe-agent's Docker environment with a reproducible fault-injection harness.
It is based on upstream issue [#1007](https://github.com/SWE-agent/mini-swe-agent/issues/1007);
mini-swe-agent and its core agent loop remain upstream work.

## Problem

The original Docker execution path had three harness-level failure modes:

1. timing out the host-side `docker exec` client did not stop its container-side shell and descendants;
2. cleanup launched an unawaited POSIX shell and could run twice from explicit cleanup plus `__del__`;
3. a valid submission marker was ignored when startup or validation output appeared before it.

These failures can turn a correct agent action into leaked resources or a false-negative task result.

## Design

### Process-group lifecycle

For the default `bash -lc` interpreter, each command starts in a new session and records its process-group
ID in a command-specific file. If the host call times out, a second bounded `docker exec` sends `TERM`
and then `KILL` to that group. Custom interpreters keep their previous behavior rather than being wrapped
with unsafe shell assumptions.

### Idempotent cleanup

`cleanup()` clears the container ID before cleanup starts, performs an argument-list `docker stop`, and
falls back to `docker rm -f`. It no longer uses `shell=True`, `/dev/null`, or an unawaited background job.

### Submission detection

The completion marker is accepted on any complete output line when the command exits successfully.
Only output after the marker becomes the submitted patch.

## Fault-injection benchmark

`scripts/benchmark_docker_reliability.py` runs equal baseline and hardened timeout trials in fresh
containers. Each command launches a `SIGTERM`-ignoring child, forces a host timeout, and counts matching
processes that remain alive. It also varies the number of lines before a valid submission marker.

```bash
python scripts/benchmark_docker_reliability.py \
  --trials 30 \
  --output results/docker-reliability-30.json
```

The JSON report contains every trial, the image, wall-clock time, orphan counts, marker decisions, and
P95 elapsed time. No language-model calls are used in this deterministic layer of the benchmark.

### Local result (2026-10-21)

Environment: Docker 29.7.2, `python:3.11` image, 30 timeout trials per variant and 30 marker cases.

| Metric | Historical behavior | Hardened behavior |
| --- | ---: | ---: |
| Timeout trials leaving orphan processes | 30 / 30 | 0 / 30 |
| Total matching processes left alive | 60 | 0 |
| Noisy submission markers detected | 1 / 30 (3.3%) | 30 / 30 (100%) |
| Timeout path P95 | 1007.1 ms | 1209.7 ms |

The added TERM/KILL cleanup removed all observed orphan processes at a P95 cost of 202.6 ms on this
machine. Raw records are in `results/docker-reliability-30.json`.

## Verification

```bash
pytest tests/environments/test_docker.py -q
ruff check \
  src/minisweagent/environments/docker.py \
  tests/environments/test_docker.py \
  scripts/benchmark_docker_reliability.py
```

The test suite includes focused mock tests and a real Docker integration test that verifies no marked
process remains after a timed-out command.
