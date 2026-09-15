"""Command-line entry points for the local Grok Remote Bridge."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="grok-remote-bridge",
        description="Run and inspect the local Grok Remote Bridge.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the Bridge HTTP/WebSocket server")
    serve.add_argument("--host", default=os.environ.get("GROK_BRIDGE_HOST", "127.0.0.1"))
    serve.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("GROK_BRIDGE_PORT", "8899")),
    )

    sub.add_parser("pair-token", help="mint a one-time iOS pairing token")

    doctor = sub.add_parser("doctor", help="check local prerequisites and Bridge health")
    doctor.add_argument("--json", action="store_true", dest="as_json")
    doctor.add_argument(
        "--url",
        default=os.environ.get("GROK_BRIDGE_URL", "http://127.0.0.1:8899"),
        help="Bridge base URL to probe",
    )

    status = sub.add_parser("status", help="show local Bridge health")
    status.add_argument(
        "--url",
        default=os.environ.get("GROK_BRIDGE_URL", "http://127.0.0.1:8899"),
        help="Bridge base URL to probe",
    )
    status.add_argument("--json", action="store_true", dest="as_json")
    return parser


def _health(url: str) -> dict[str, object]:
    endpoint = url.rstrip("/") + "/health"
    request = urllib.request.Request(endpoint, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("health response is not an object")
            return {"reachable": True, "url": endpoint, "health": payload}
    except (OSError, ValueError, urllib.error.URLError) as error:
        return {"reachable": False, "url": endpoint, "error": str(error)}


def _doctor(url: str) -> dict[str, object]:
    from . import config

    grok = Path(config.GROK_BIN)
    bridge_dir = Path(config.BRIDGE_DIR)
    checks: dict[str, object] = {
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "grok": {
            "path": str(grok),
            "exists": grok.exists(),
            "executable": grok.is_file() and os.access(grok, os.X_OK),
        },
        "bridge_dir": {
            "path": str(bridge_dir),
            "exists": bridge_dir.is_dir(),
            "writable": bridge_dir.is_dir() and os.access(bridge_dir, os.W_OK),
        },
        "uv": shutil.which("uv") is not None,
        "cloudflared": shutil.which("cloudflared") is not None,
        "qrencode": shutil.which("qrencode") is not None,
        "sandbox_required": config.SANDBOX_REQUIRED,
        "sandbox_profile": str(config.SANDBOX_PATH),
        "database": str(config.DB_PATH),
    }
    checks["health"] = _health(url)
    return checks


def _print_result(result: dict[str, object], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return

    if "health" in result and len(result) == 1:
        result = result["health"]  # type: ignore[assignment]
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    if args.command == "serve":
        import uvicorn

        uvicorn.run("grok_bridge.app:app", host=args.host, port=args.port)
        return 0

    if args.command == "pair-token":
        from .db import Store

        print(Store().create_pairing_token())
        return 0

    if args.command == "status":
        result = _health(args.url)
        _print_result(result, as_json=args.as_json)
        return 0 if result.get("reachable") else 1

    if args.command == "doctor":
        result = _doctor(args.url)
        _print_result(result, as_json=args.as_json)
        health = result.get("health") or {}
        grok = result.get("grok") or {}
        healthy = (
            isinstance(health, dict)
            and health.get("reachable") is True
            and isinstance(grok, dict)
            and grok.get("exists") is True
        )
        return 0 if healthy else 1

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
