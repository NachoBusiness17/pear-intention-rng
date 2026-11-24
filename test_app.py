"""Tests for app.py and Vercel serverless api/qrng.py."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import uvicorn

REPO_DIR = Path(__file__).resolve().parent
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

import app as pear_app  # noqa: E402
from api import qrng as vercel_qrng  # noqa: E402


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class TestMathAndEntropy(unittest.TestCase):
    def test_bytes_to_bits_exact_and_msb_first(self) -> None:
        raw = bytes([0b10100001, 0b11110000])
        self.assertEqual(pear_app.bytes_to_bits(raw, 8), "10100001")
        self.assertEqual(pear_app.bytes_to_bits(raw, 12), "101000011111")
        with self.assertRaises(ValueError):
            pear_app.bytes_to_bits(raw, 17)

    def test_two_sided_p_value(self) -> None:
        self.assertAlmostEqual(pear_app.two_sided_p_value(0.0), 1.0, places=9)
        self.assertAlmostEqual(pear_app.two_sided_p_value(1.959963984540054), 0.05, places=6)
        self.assertEqual(
            pear_app.two_sided_p_value(2.5),
            pear_app.two_sided_p_value(-2.5),
        )

    def test_entropy_pool_anu_legacy_and_buffering(self) -> None:
        calls: list[str] = []

        def fake_fetcher(url: str, headers: dict[str, str], timeout: float) -> dict:
            calls.append(url)
            return {"type": "uint8", "length": 64, "data": [170] * 64, "success": True}

        pool = pear_app.EntropyPool(chunk_size=64, fetcher=fake_fetcher)
        b1, src1, fb1 = pool.draw_bytes(25, entropy_mode="anu_legacy")
        b2, src2, fb2 = pool.draw_bytes(25, entropy_mode="anu_legacy")
        self.assertEqual(len(calls), 1)
        self.assertEqual(b1, bytes([170] * 25))
        self.assertEqual(b2, bytes([170] * 25))
        self.assertEqual(src1, "anu_legacy")
        self.assertEqual(src2, "anu_legacy")
        self.assertEqual(fb1, "")
        self.assertEqual(fb2, "")

    def test_entropy_pool_anu_v2_header_and_no_secret_leak(self) -> None:
        seen_headers: list[dict[str, str]] = []

        def fake_fetcher(url: str, headers: dict[str, str], timeout: float) -> dict:
            seen_headers.append(headers)
            return {"success": True, "type": "uint8", "length": "64", "data": [85] * 64}

        old_key = os.environ.get("ANU_QRNG_API_KEY")
        os.environ["ANU_QRNG_API_KEY"] = "secret-test-key-xyz"
        try:
            pool = pear_app.EntropyPool(chunk_size=64, fetcher=fake_fetcher)
            raw, src, fb = pool.draw_bytes(25, entropy_mode="auto")
            self.assertEqual(src, "anu_v2")
            self.assertEqual(fb, "")
            self.assertEqual(raw, bytes([85] * 25))
            self.assertEqual(seen_headers[0].get("x-api-key"), "secret-test-key-xyz")
            probe = pool.probe("anu_v2")
            self.assertTrue(probe["anu_api_key_configured"])
            self.assertNotIn("secret-test-key-xyz", json.dumps(probe))
        finally:
            if old_key is None:
                os.environ.pop("ANU_QRNG_API_KEY", None)
            else:
                os.environ["ANU_QRNG_API_KEY"] = old_key

    def test_entropy_pool_fallback_provenance_on_failure(self) -> None:
        calls = 0

        def failing_fetcher(url: str, headers: dict[str, str], timeout: float) -> dict:
            nonlocal calls
            calls += 1
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)

        pool = pear_app.EntropyPool(chunk_size=64, cooldown_seconds=30.0, fetcher=failing_fetcher)
        raw, src, reason = pool.draw_bytes(25, entropy_mode="anu_legacy")
        self.assertEqual(len(raw), 25)
        self.assertEqual(src, "secrets_fallback")
        self.assertIn("anu_legacy_http_429", reason)
        raw2, src2, reason2 = pool.draw_bytes(25, entropy_mode="anu_legacy")
        self.assertEqual(len(raw2), 25)
        self.assertEqual(src2, "secrets_fallback")
        self.assertEqual(calls, 1)
        self.assertIn("anu_legacy_http_429", reason2)


class TestVercelServerlessQrng(unittest.TestCase):
    def test_serverless_anu_and_fallback(self) -> None:
        def ok_fetcher(url: str, headers: dict[str, str], timeout: float) -> dict:
            return {"success": True, "data": [42] * 25}

        res = vercel_qrng.draw_qrng_payload(25, mode="anu_legacy", fetcher=ok_fetcher)
        self.assertEqual(res["source"], "anu_legacy")
        self.assertEqual(res["count"], 25)
        self.assertEqual(len(res["bytes"]), 25)
        self.assertEqual(
            hashlib.sha256(bytes(res["bytes"])).hexdigest(),
            res["sha256"],
        )

        def fail_fetcher(url: str, headers: dict[str, str], timeout: float) -> dict:
            raise urllib.error.URLError("offline")

        fb = vercel_qrng.draw_qrng_payload(25, mode="anu_legacy", fetcher=fail_fetcher)
        self.assertEqual(fb["source"], "secrets_fallback")
        self.assertIn("offline", fb["fallback_reason"])
        self.assertEqual(len(fb["bytes"]), 25)


class TestStorageAndStatistics(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.storage = pear_app.SessionStorage(self.db_path)

    def tearDown(self) -> None:
        self.storage.close()
        self.tmp.cleanup()

    def test_deterministic_200_bit_cumulative_stats_and_checksums(self) -> None:
        pattern_bytes = [0xFF] * 64

        def fake_fetcher(url: str, headers: dict[str, str], timeout: float) -> dict:
            return {"success": True, "data": pattern_bytes}

        pool = pear_app.EntropyPool(chunk_size=64, fetcher=fake_fetcher)
        session = self.storage.create_session(
            {
                "session_id": "sess-det-1",
                "created_at": pear_app.utc_now_iso(),
                "mode": "intention",
                "condition": "HIGH",
                "intention_text": "Deterministic test",
                "high_label": "Delay",
                "low_label": "Proceed",
                "bits_per_trial": 200,
                "planned_trials": 3,
                "entropy_mode": "anu_legacy",
                "status": "active",
            }
        )
        t1 = self.storage.append_trials(session["session_id"], pool, count=1)[0]
        self.assertEqual(t1["trial_sum"], 200)
        self.assertEqual(t1["trial_deviation"], 100.0)
        self.assertEqual(t1["cumulative_deviation"], 100.0)
        self.assertAlmostEqual(t1["z_score"], 100.0 / math.sqrt(50.0), places=9)
        self.assertEqual(
            hashlib.sha256(bytes.fromhex(t1["raw_hex"])).hexdigest(),
            t1["bytes_sha256"],
        )

        salmon = self.storage.create_session(
            {
                "session_id": "sess-salmon",
                "created_at": pear_app.utc_now_iso(),
                "mode": "dead_salmon",
                "condition": "BASELINE",
                "intention_text": "Dead salmon",
                "high_label": "Delay",
                "low_label": "Proceed",
                "bits_per_trial": 200,
                "planned_trials": 10,
                "entropy_mode": "local_secrets",
                "status": "active",
            }
        )
        local_pool = pear_app.EntropyPool()
        trials = self.storage.append_trials(
            salmon["session_id"], local_pool, count=5, condition_override="HIGH"
        )
        self.assertTrue(all(t["condition"] == "BASELINE" for t in trials))
        stopped = self.storage.stop_session(salmon["session_id"])
        self.assertEqual(stopped["early_stop"], 1)

    def test_dead_salmon_gate_refuses_noise(self) -> None:
        local_pool = pear_app.EntropyPool()
        session = self.storage.create_session(
            {
                "session_id": "sess-gate",
                "created_at": pear_app.utc_now_iso(),
                "mode": "intention",
                "condition": "HIGH",
                "intention_text": "Gate test",
                "high_label": "Delay",
                "low_label": "Proceed",
                "bits_per_trial": 200,
                "planned_trials": 20,
                "entropy_mode": "local_secrets",
                "status": "active",
            }
        )
        trials = self.storage.append_trials(session["session_id"], local_pool, count=20)
        summary = pear_app.compute_session_summary(session, trials)
        if abs(summary["z_score"]) < 1.96:
            self.assertEqual(summary["statistical_gate"]["verdict"], "NULL_NOISE_CONSISTENT")
        else:
            self.assertEqual(summary["statistical_gate"]["verdict"], "STATISTICALLY_NOTABLE_EXPLORATORY")
        self.assertIn("NONE", summary["exploratory_binary_lean"]["authority"])


class TestLiveUvicornHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.tmp.name) / "live.db"

        def failing_fetcher(url: str, headers: dict[str, str], timeout: float) -> dict:
            raise urllib.error.URLError("offline-test")

        cls.pool = pear_app.EntropyPool(chunk_size=64, fetcher=failing_fetcher)
        cls.app = pear_app.create_app(db_path=cls.db_path, entropy_pool=cls.pool)
        cls.port = _free_port()
        config = uvicorn.Config(cls.app, host="127.0.0.1", port=cls.port, log_level="error")
        cls.server = uvicorn.Server(config)
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()

        deadline = time.time() + 10.0
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{cls.port}/api/health", timeout=1.0) as r:
                    if r.status == 200:
                        break
            except Exception:
                time.sleep(0.05)
        else:
            raise RuntimeError("Live uvicorn test server failed to start")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.should_exit = True
        cls.thread.join(timeout=5.0)
        cls.tmp.cleanup()

    def _req(self, path: str, method: str = "GET", body: dict | None = None) -> tuple[int, str, dict[str, str]]:
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            return resp.status, resp.read().decode("utf-8"), dict(resp.headers)

    def test_full_http_lifecycle_and_exports(self) -> None:
        status, html, _ = self._req("/")
        self.assertEqual(status, 200)
        self.assertIn("PEAR Intention RNG", html)

        status, sess_raw, _ = self._req(
            "/api/sessions",
            method="POST",
            body={
                "mode": "intention",
                "condition": "HIGH",
                "intention_text": "Iowa lead-azide test session",
                "high_label": "Delay / Safer Path (HIGH)",
                "low_label": "Proceed (LOW)",
                "bits_per_trial": 200,
                "planned_trials": 5,
                "entropy_mode": "auto",
            },
        )
        self.assertEqual(status, 200)
        sid = json.loads(sess_raw)["session"]["session_id"]

        status, after_raw, _ = self._req(
            f"/api/sessions/{sid}/trial",
            method="POST",
            body={"count": 5},
        )
        self.assertEqual(status, 200)
        after = json.loads(after_raw)
        self.assertEqual(after["summary"]["trial_count"], 5)
        self.assertEqual(after["summary"]["preregistration_audit"]["status"], "COMPLETE_AT_TARGET")
        self.assertEqual(after["trials"][0]["entropy_source"], "secrets_fallback")

        status, stopped_raw, _ = self._req(f"/api/sessions/{sid}/stop", method="POST")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(stopped_raw)["session"]["status"], "stopped")

        status, csv_text, _ = self._req(f"/api/sessions/{sid}/export.csv?include_bits=true")
        self.assertEqual(status, 200)
        rows = list(csv.DictReader(io.StringIO(csv_text)))
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0]["raw_bits"].count("1"), int(rows[0]["trial_sum"]))

        status, bits_text, _ = self._req(f"/api/sessions/{sid}/export.bits.txt")
        self.assertEqual(status, 200)
        self.assertIn("trial=5", bits_text)


if __name__ == "__main__":
    unittest.main()
