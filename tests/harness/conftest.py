"""
Spawn the @archastro/channel-harness subprocess for the Python side.

The subprocess is the same service TS tests spawn — a compiled Node CLI
from the @archastro/channel-harness npm package — so both languages
drive the same `ContractServer` and exercise the same wire contract.

This conftest:

  1. Locates the channel-harness bin under node_modules (installed by
     `npm ci` at the repo root).
  2. Spawns it pointed at the LiveDoc fixture spec.
  3. Parses the first JSON line on stdout to discover the ephemeral URLs.
  4. Exposes those URLs via a session fixture + environment variables.
  5. Kills the subprocess on session teardown.

Keeping the lifecycle at session scope matches the TS `globalSetup` story —
one service instance, scenarios/observations reset between tests through
``HarnessServiceClient.reset()``.
"""

from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest


def _repo_root() -> Path:
    p = Path(__file__).resolve().parent
    while p != p.parent:
        if (p / ".git").exists():
            return p
        p = p.parent
    raise RuntimeError("Could not find repo root from tests/harness/conftest.py")


REPO_ROOT = _repo_root()
HARNESS_BIN = Path(
    os.environ.get(
        "ARCHASTRO_HARNESS_BIN",
        REPO_ROOT / "node_modules" / "@archastro" / "channel-harness" / "dist" / "bin.js",
    )
)
SPEC_PATH = Path(__file__).resolve().parent / "fixtures" / "channel-harness-spec.json"


@pytest.fixture(scope="session")
def harness_service() -> Iterator[dict[str, str]]:
    """Session-scoped: the running service's ``wsUrl`` and ``controlUrl``."""
    if not HARNESS_BIN.exists():
        raise RuntimeError(
            f"channel-harness bin not found at {HARNESS_BIN}. "
            f"Run `npm ci` at the repo root to install @archastro/channel-harness, "
            f"or set ARCHASTRO_HARNESS_BIN to point at an alternate checkout."
        )

    proc = subprocess.Popen(
        ["node", str(HARNESS_BIN), str(SPEC_PATH)],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        urls = _read_first_json_line(proc, timeout=15.0)
    except Exception:
        _terminate(proc)
        raise

    os.environ["ARCHASTRO_HARNESS_WS_URL"] = urls["wsUrl"]
    os.environ["ARCHASTRO_HARNESS_CONTROL_URL"] = urls["controlUrl"]

    try:
        yield urls
    finally:
        _terminate(proc)
        os.environ.pop("ARCHASTRO_HARNESS_WS_URL", None)
        os.environ.pop("ARCHASTRO_HARNESS_CONTROL_URL", None)


def _read_first_json_line(proc: subprocess.Popen[str], *, timeout: float) -> dict[str, str]:
    assert proc.stdout is not None
    # Use `selectors` to bound each wait to a fraction of the deadline so a
    # subprocess that starts but stalls before printing can't hang pytest.
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    buf = ""
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    f"timed out after {timeout}s waiting for harness service to emit URLs"
                )
            if proc.poll() is not None:
                err = proc.stderr.read() if proc.stderr else ""
                raise RuntimeError(
                    f"harness service exited with code {proc.returncode} before "
                    f"reporting URLs\nstderr: {err}"
                )
            events = selector.select(timeout=min(remaining, 0.25))
            if not events:
                continue
            chunk = proc.stdout.readline()
            if not chunk:
                time.sleep(0.05)
                continue
            buf += chunk
            if "\n" not in buf:
                continue
            line, _, buf = buf.partition("\n")
            parsed = json.loads(line.strip())
            if "wsUrl" in parsed and "controlUrl" in parsed:
                return parsed  # type: ignore[return-value]
    finally:
        selector.close()


def _terminate(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)
