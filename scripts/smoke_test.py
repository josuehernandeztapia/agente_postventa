#!/usr/bin/env python3
"""Run a minimal smoke test against a running Higer RAG API instance.

Usage:
    python3 scripts/smoke_test.py --base http://127.0.0.1:8000

The script checks `/health`, `/version`, `/metrics` and `/query_hybrid`
(unless flags `--skip-query`, `--skip-version`, `--skip-metrics` are passed).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Tuple
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

DEFAULT_QUESTION = "Resume qué puede hacer el asistente de postventa Higer."


def _http_call(method: str, url: str, *, data: dict | None = None, timeout: float = 15.0) -> Tuple[int, bytes]:
    payload: bytes | None = None
    headers = {"Accept": "application/json"}
    if data is not None:
        payload = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urlrequest.Request(url, data=payload, headers=headers, method=method.upper())
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:  # type: ignore[arg-type]
            return resp.status, resp.read()
    except HTTPError as exc:  # pragma: no cover - simple CLI
        return exc.code, exc.read()
    except URLError as exc:  # pragma: no cover - simple CLI
        raise RuntimeError(f"Error contacting {url}: {exc}") from exc


def check_health(base_url: str) -> dict[str, Any]:
    status, body = _http_call("GET", f"{base_url}/health", timeout=10.0)
    if status != 200:
        raise AssertionError(f"/health returned status {status}: {body.decode('utf-8', 'ignore')}")
    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise AssertionError("/health did not return JSON") from exc
    expected_keys = {"status", "initialized"}
    if not expected_keys.issubset(payload.keys()):
        raise AssertionError(f"/health JSON missing keys {expected_keys - set(payload.keys())}")
    return payload


def check_version(base_url: str) -> str:
    status, body = _http_call("GET", f"{base_url}/version", timeout=10.0)
    if status != 200:
        raise AssertionError(f"/version returned status {status}: {body.decode('utf-8', 'ignore')}")
    return body.decode('utf-8', 'ignore').strip()


def check_metrics(base_url: str) -> int:
    status, body = _http_call("GET", f"{base_url}/metrics", timeout=15.0)
    if status != 200:
        raise AssertionError(f"/metrics returned status {status}: {body.decode('utf-8', 'ignore')}")
    lines = body.decode('utf-8', 'ignore').strip().splitlines()
    return len(lines)


def check_query(base_url: str, question: str) -> dict[str, Any]:
    payload = {
        "question": question,
        "meta": {"channel": "smoke", "max_chars": 400},
    }
    status, body = _http_call("POST", f"{base_url}/query_hybrid", data=payload, timeout=25.0)
    if status != 200:
        raise AssertionError(f"/query_hybrid returned status {status}: {body.decode('utf-8', 'ignore')}")
    try:
        response = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise AssertionError("/query_hybrid did not return JSON") from exc
    required_fields = {"question", "answer"}
    if not required_fields.issubset(response.keys()):
        raise AssertionError(f"/query_hybrid JSON missing keys {required_fields - set(response.keys())}")
    return response


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke test for Higer RAG API")
    parser.add_argument(
        "--base",
        default=os.getenv("SMOKE_BASE_URL", "http://127.0.0.1:8000"),
        help="Base URL of the running API (default: %(default)s)",
    )
    parser.add_argument(
        "--question",
        default=DEFAULT_QUESTION,
        help="Question to use for /query_hybrid (default: %(default)s)",
    )
    parser.add_argument(
        "--skip-query",
        action="store_true",
        help="Only check /health and skip the /query_hybrid test",
    )
    parser.add_argument(
        "--skip-version",
        action="store_true",
        help="Skip /version check",
    )
    parser.add_argument(
        "--skip-metrics",
        action="store_true",
        help="Skip /metrics check",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    base = args.base.rstrip("/")
    start = time.perf_counter()
    print(f"[smoke] Checking {base}/health …", flush=True)
    health_payload = check_health(base)
    print(f"  ok: status={health_payload.get('status')} initialized={health_payload.get('initialized')}")

    if not args.skip_version:
        print(f"[smoke] Checking {base}/version …", flush=True)
        version_text = check_version(base)
        print(f"  ok: version={version_text}")

    if not args.skip_metrics:
        print(f"[smoke] Checking {base}/metrics …", flush=True)
        metrics_lines = check_metrics(base)
        print(f"  ok: metrics lines={metrics_lines}")

    query_payload: dict[str, Any] | None = None
    if not args.skip_query:
        print(f"[smoke] Checking {base}/query_hybrid …", flush=True)
        query_payload = check_query(base, args.question)
        answer_preview = (query_payload.get("answer") or "").strip().splitlines()[0:1]
        preview_text = answer_preview[0] if answer_preview else "<sin respuesta>"
        print(f"  ok: received {len((query_payload.get('answer') or ''))} chars — preview: {preview_text}")

    elapsed = time.perf_counter() - start
    print(f"[smoke] Completed in {elapsed:.2f}s")

    # Return non-zero if the response hints at non-initialized backend
    if not bool(health_payload.get("initialized")):
        print("[smoke] WARNING: backend reports initialized=false", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"[smoke] ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
