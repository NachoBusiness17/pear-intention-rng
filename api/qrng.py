"""
Stateless Vercel Python serverless relay for ANU QRNG with CSPRNG fallback.

Returns random uint8 bytes along with explicit provenance (`anu_v2`, `anu_legacy`,
or `secrets_fallback`) and SHA-256 checksum so browser clients can avoid CORS
while keeping `ANU_QRNG_API_KEY` server-side.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler
from typing import Any, Callable

ANU_LEGACY_URL = "https://qrng.anu.edu.au/API/jsonI.php?length={length}&type=uint8"
ANU_V2_URL = "https://api.quantumnumbers.anu.edu.au/?length={length}&type=uint8"


def _default_fetcher(url: str, headers: dict[str, str], timeout: float) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "pear-intention-rng-vercel/1.0", **headers},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def draw_qrng_payload(
    n_bytes: int,
    mode: str = "auto",
    fetcher: Callable[[str, dict[str, str], float], dict[str, Any]] | None = None,
    timeout: float = 4.0,
) -> dict[str, Any]:
    count = max(1, min(int(n_bytes), 1024))
    fetch = fetcher or _default_fetcher
    api_key = (os.environ.get("ANU_QRNG_API_KEY") or "").strip()

    if mode == "secrets":
        raw = secrets.token_bytes(count)
        return {
            "ok": True,
            "count": count,
            "bytes": list(raw),
            "hex": raw.hex(),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "source": "secrets_local",
            "fallback_reason": "",
            "anu_api_key_configured": bool(api_key),
        }

    order: list[str]
    if mode == "anu_v2":
        order = ["anu_v2"]
    elif mode == "anu_legacy":
        order = ["anu_legacy"]
    else:
        order = ["anu_v2", "anu_legacy"] if api_key else ["anu_legacy"]

    reasons: list[str] = []
    for kind in order:
        try:
            if kind == "anu_v2":
                if not api_key:
                    reasons.append("anu_v2_missing_api_key")
                    continue
                payload = fetch(
                    ANU_V2_URL.format(length=count),
                    {"x-api-key": api_key},
                    timeout,
                )
            else:
                payload = fetch(ANU_LEGACY_URL.format(length=count), {}, timeout)

            if not isinstance(payload, dict) or not payload.get("success", False):
                raise ValueError(f"{kind}_unsuccessful_payload")
            data = payload.get("data")
            if not isinstance(data, list) or len(data) < count:
                raise ValueError(f"{kind}_insufficient_data")
            cleaned = [int(v) for v in data[:count]]
            if any(v < 0 or v > 255 for v in cleaned):
                raise ValueError(f"{kind}_out_of_range_uint8")
            raw = bytes(cleaned)
            return {
                "ok": True,
                "count": count,
                "bytes": cleaned,
                "hex": raw.hex(),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "source": kind,
                "fallback_reason": "",
                "anu_api_key_configured": bool(api_key),
            }
        except urllib.error.HTTPError as exc:
            reasons.append(f"{kind}_http_{exc.code}")
        except urllib.error.URLError as exc:
            reasons.append(f"{kind}_url_error:{exc.reason}")
        except Exception as exc:
            reasons.append(f"{kind}_error:{type(exc).__name__}")

    raw = secrets.token_bytes(count)
    return {
        "ok": True,
        "count": count,
        "bytes": list(raw),
        "hex": raw.hex(),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "source": "secrets_fallback",
        "fallback_reason": ";".join(dict.fromkeys(reasons)) or "anu_unavailable",
        "anu_api_key_configured": bool(api_key),
    }


class handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        try:
            n_bytes = int((params.get("bytes") or ["25"])[0])
        except ValueError:
            n_bytes = 25
        mode = (params.get("mode") or ["auto"])[0]
        body = json.dumps(draw_qrng_payload(n_bytes=n_bytes, mode=mode)).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
