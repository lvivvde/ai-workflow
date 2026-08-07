"""Start the real HTTP server, probe /health, and always clean it up."""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

from app.config import get_settings


STARTUP_TIMEOUT_SECONDS = 10


def wait_for_health(host: str, port: int) -> dict[str, object]:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    url = f"http://{host}:{port}/health"
    last_error: Exception | None = None

    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status != 200:
                    raise RuntimeError(f"health returned HTTP {response.status}")
                return json.loads(response.read().decode("utf-8"))
        except (OSError, RuntimeError, urllib.error.URLError) as error:
            last_error = error
            time.sleep(0.1)

    raise RuntimeError(f"server did not become healthy: {last_error}")


def main() -> int:
    settings = get_settings()
    command = [
        sys.executable,
        "-m",
        "app.server",
    ]
    server = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )

    try:
        health = wait_for_health(settings.memory_host, settings.memory_port)
        print(json.dumps(health, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as error:
        if server.poll() is not None:
            stdout, stderr = server.communicate(timeout=2)
            print(f"server exited with code {server.returncode}", file=sys.stderr)
            if stdout:
                print(stdout, file=sys.stderr)
            if stderr:
                print(stderr, file=sys.stderr)
        print(str(error), file=sys.stderr)
        return 1
    finally:
        if server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
