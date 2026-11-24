# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "fastapi>=0.115.0",
#   "uvicorn>=0.30.0",
# ]
# ///
"""
PEAR-Style Consciousness / Intention RNG Experiment (FastAPI + SQLite + Single-Page UI)

Implements a tripolar (HIGH / LOW / BASELINE) PEAR REG-inspired experiment with:
- Primary entropy: ANU QRNG API (v2 if ANU_QRNG_API_KEY is set, or legacy jsonI.php)
- Cryptographic fallback: Python `secrets` module (explicitly tagged per trial)
- Buffered byte pool with per-byte provenance and SHA-256 checksums
- Experiment modes: intention, continuous, calibration, dead_salmon (negative control)
- Pre-registered trial target tracking & null-gated binary directional summary
- SQLite persistence + in-memory active state
- Exports: CSV, JSON, and raw bitstream (.txt)
- Single-file dark-mode HTML + Chart.js UI (with offline Canvas fallback)

DISCLAIMER:
This software is an exploratory consciousness/statistics experiment inspired by
the Princeton Engineering Anomalies Research (PEAR) laboratory protocol.
It is NOT scientific proof of anomalous cognition and has NO legal, safety,
or operational decision-making authority.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import secrets
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

try:
    from fastapi import FastAPI, HTTPException, Query
    from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
    from pydantic import BaseModel, Field
except ModuleNotFoundError as exc:
    missing = exc.name or "fastapi / pydantic"
    msg = (
        f"\n[pear-rng] Missing Python dependency: {missing}\n"
        "Required runtime dependencies: fastapi, uvicorn (see requirements.txt / pyproject.toml).\n\n"
        "Quick ways to run:\n"
        "  1) ./run.sh  (macOS/Linux) or run.bat (Windows) — auto-creates .venv and installs deps\n"
        "  2) uv run app.py\n"
        "  3) pip install -r requirements.txt && python3 app.py\n"
        "  4) Zero-dependency fallback: open public/index.html directly in any web browser\n"
    )
    if __name__ == "__main__":
        print(msg, file=sys.stderr)
        raise SystemExit(1) from exc
    raise ModuleNotFoundError(msg, name=exc.name) from exc

DEFAULT_DB_PATH = Path(__file__).resolve().with_name("pear_rng.db")
ANU_LEGACY_URL = "https://qrng.anu.edu.au/API/jsonI.php?length={length}&type=uint8"
ANU_V2_URL = "https://api.quantumnumbers.anu.edu.au/?length={length}&type=uint8"

DISCLAIMER_TEXT = (
    "EXPLORATORY RESEARCH INSTRUMENT ONLY — Inspired by PEAR Lab REG protocols. "
    "Output is statistical noise monitoring, NOT scientific proof and NOT an authority "
    "for legal, public-safety, or energetic-material decisions."
)

ConditionType = Literal["HIGH", "LOW", "BASELINE"]
ModeType = Literal["intention", "continuous", "calibration", "dead_salmon"]
EntropyModeType = Literal["auto", "local_secrets", "anu_legacy", "anu_v2"]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def two_sided_p_value(z: float) -> float:
    """Two-sided normal p-value using stdlib math.erfc."""
    return math.erfc(abs(z) / math.sqrt(2.0))


def bytes_to_bits(raw: bytes, bit_count: int) -> str:
    """Extract exact MSB-first bits as a '0'/'1' string without modulo bias."""
    bits: list[str] = []
    for byte in raw:
        for shift in range(7, -1, -1):
            bits.append("1" if (byte >> shift) & 1 else "0")
            if len(bits) == bit_count:
                return "".join(bits)
    raise ValueError(f"Insufficient bytes ({len(raw)}) for {bit_count} bits")


class EntropyPool:
    """Thread-safe buffered byte pool with explicit per-byte provenance tracking."""

    def __init__(
        self,
        chunk_size: int = 1024,
        cooldown_seconds: float = 60.0,
        http_timeout: float = 5.0,
        fetcher: Callable[[str, dict[str, str], float], dict[str, Any]] | None = None,
    ) -> None:
        self.chunk_size = max(64, min(chunk_size, 1024))
        self.cooldown_seconds = cooldown_seconds
        self.http_timeout = http_timeout
        self._fetcher = fetcher or self._default_http_json
        self._lock = threading.Lock()
        self._buffers: dict[str, deque[tuple[int, str, str]]] = {
            "anu_v2": deque(),
            "anu_legacy": deque(),
        }
        self._cooldown_until: dict[str, float] = {
            "anu_v2": 0.0,
            "anu_legacy": 0.0,
        }
        self._last_error: dict[str, str] = {}

    @staticmethod
    def _default_http_json(url: str, headers: dict[str, str], timeout: float) -> dict[str, Any]:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "PEAR-RNG-FalsifierExperiment/1.0", **headers},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    @staticmethod
    def api_key_configured() -> bool:
        return bool((os.environ.get("ANU_QRNG_API_KEY") or "").strip())

    def _fetch_anu_chunk(self, kind: Literal["anu_v2", "anu_legacy"]) -> tuple[list[int], str | None]:
        now = time.monotonic()
        if now < self._cooldown_until.get(kind, 0.0):
            return [], self._last_error.get(kind) or f"{kind}_cooldown"

        try:
            if kind == "anu_v2":
                api_key = (os.environ.get("ANU_QRNG_API_KEY") or "").strip()
                if not api_key:
                    return [], "anu_v2_missing_api_key"
                url = ANU_V2_URL.format(length=self.chunk_size)
                payload = self._fetcher(url, {"x-api-key": api_key}, self.http_timeout)
            else:
                url = ANU_LEGACY_URL.format(length=self.chunk_size)
                payload = self._fetcher(url, {}, self.http_timeout)

            if not isinstance(payload, dict) or not payload.get("success", False):
                raise ValueError(f"{kind}_unsuccessful_payload")
            data = payload.get("data")
            if not isinstance(data, list) or not data:
                raise ValueError(f"{kind}_empty_data")
            cleaned = [int(v) for v in data]
            if any(v < 0 or v > 255 for v in cleaned):
                raise ValueError(f"{kind}_out_of_range_uint8")
            self._last_error.pop(kind, None)
            self._cooldown_until[kind] = 0.0
            return cleaned, None
        except urllib.error.HTTPError as exc:
            reason = f"{kind}_http_{exc.code}"
        except urllib.error.URLError as exc:
            reason = f"{kind}_url_error:{exc.reason}"
        except Exception as exc:
            reason = f"{kind}_error:{type(exc).__name__}"

        self._last_error[kind] = reason
        self._cooldown_until[kind] = now + self.cooldown_seconds
        return [], reason

    def draw_bytes(
        self,
        n_bytes: int,
        entropy_mode: EntropyModeType = "auto",
    ) -> tuple[bytes, str, str]:
        """
        Return (raw_bytes, entropy_source, fallback_reason).
        Every byte tracks its own origin so outages cannot masquerade as QRNG.
        """
        if n_bytes <= 0:
            raise ValueError("n_bytes must be positive")

        if entropy_mode == "local_secrets":
            return secrets.token_bytes(n_bytes), "secrets_local", ""

        with self._lock:
            target_order: list[Literal["anu_v2", "anu_legacy"]]
            if entropy_mode == "anu_v2":
                target_order = ["anu_v2"]
            elif entropy_mode == "anu_legacy":
                target_order = ["anu_legacy"]
            else:
                target_order = ["anu_v2", "anu_legacy"] if self.api_key_configured() else ["anu_legacy"]

            collected: list[tuple[int, str, str]] = []
            reasons: list[str] = []

            for kind in target_order:
                buf = self._buffers[kind]
                while len(collected) < n_bytes:
                    if not buf:
                        chunk, err = self._fetch_anu_chunk(kind)
                        if chunk:
                            for val in chunk:
                                buf.append((val, kind, ""))
                        else:
                            if err:
                                reasons.append(err)
                            break
                    if buf:
                        collected.append(buf.popleft())
                if len(collected) == n_bytes:
                    break

            if len(collected) < n_bytes:
                needed = n_bytes - len(collected)
                fb_bytes = secrets.token_bytes(needed)
                fb_reason = ";".join(dict.fromkeys(reasons)) or "anu_unavailable"
                for val in fb_bytes:
                    collected.append((val, "secrets_fallback", fb_reason))

            raw = bytes(item[0] for item in collected)
            sources = list(dict.fromkeys(item[1] for item in collected))
            source_label = sources[0] if len(sources) == 1 else "mixed:" + "+".join(sources)
            reason_list = [item[2] for item in collected if item[2]]
            fallback_reason = ";".join(dict.fromkeys(reason_list))
            return raw, source_label, fallback_reason

    def probe(self, entropy_mode: EntropyModeType = "auto") -> dict[str, Any]:
        t0 = time.monotonic()
        raw, source, reason = self.draw_bytes(8, entropy_mode=entropy_mode)
        latency_ms = round((time.monotonic() - t0) * 1000.0, 2)
        return {
            "requested_mode": entropy_mode,
            "actual_source": source,
            "fallback_reason": reason,
            "sample_sha256": hashlib.sha256(raw).hexdigest(),
            "latency_ms": latency_ms,
            "anu_api_key_configured": self.api_key_configured(),
        }


class SessionStorage:
    """Thread-safe SQLite persistence for sessions and trials."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA foreign_keys=ON;")
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    ended_at TEXT,
                    mode TEXT NOT NULL,
                    condition TEXT NOT NULL,
                    intention_text TEXT NOT NULL,
                    high_label TEXT NOT NULL,
                    low_label TEXT NOT NULL,
                    bits_per_trial INTEGER NOT NULL,
                    planned_trials INTEGER NOT NULL,
                    entropy_mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    early_stop INTEGER NOT NULL DEFAULT 0,
                    notes TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS trials (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    trial_index INTEGER NOT NULL,
                    timestamp TEXT NOT NULL,
                    condition TEXT NOT NULL,
                    bits_per_trial INTEGER NOT NULL,
                    trial_sum INTEGER NOT NULL,
                    expected_mean REAL NOT NULL,
                    trial_deviation REAL NOT NULL,
                    cumulative_deviation REAL NOT NULL,
                    z_score REAL NOT NULL,
                    p_value_two_sided REAL NOT NULL,
                    entropy_source TEXT NOT NULL,
                    fallback_reason TEXT NOT NULL,
                    raw_hex TEXT NOT NULL,
                    raw_bits TEXT NOT NULL,
                    bytes_sha256 TEXT NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE,
                    UNIQUE (session_id, trial_index)
                );
                """
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def create_session(self, session: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO sessions (
                    session_id, created_at, ended_at, mode, condition,
                    intention_text, high_label, low_label, bits_per_trial,
                    planned_trials, entropy_mode, status, early_stop, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session["session_id"],
                    session["created_at"],
                    session.get("ended_at"),
                    session["mode"],
                    session["condition"],
                    session["intention_text"],
                    session["high_label"],
                    session["low_label"],
                    session["bits_per_trial"],
                    session["planned_trials"],
                    session["entropy_mode"],
                    session["status"],
                    1 if session.get("early_stop") else 0,
                    session.get("notes", ""),
                ),
            )
            self._conn.commit()
        return self.get_session(session["session_id"])  # type: ignore[return-value]

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if not row:
                return None
            return dict(row)

    def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT s.*,
                       COUNT(t.id) AS trial_count,
                       COALESCE(MAX(t.trial_index), 0) AS last_index
                FROM sessions s
                LEFT JOIN trials t ON t.session_id = s.session_id
                GROUP BY s.session_id
                ORDER BY s.created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                latest = self._conn.execute(
                    """
                    SELECT cumulative_deviation, z_score, p_value_two_sided
                    FROM trials
                    WHERE session_id = ?
                    ORDER BY trial_index DESC
                    LIMIT 1
                    """,
                    (d["session_id"],),
                ).fetchone()
                d["cumulative_deviation"] = latest["cumulative_deviation"] if latest else 0.0
                d["z_score"] = latest["z_score"] if latest else 0.0
                d["p_value_two_sided"] = latest["p_value_two_sided"] if latest else 1.0
                out.append(d)
            return out

    def get_trials(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM trials WHERE session_id = ? ORDER BY trial_index ASC",
                (session_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def append_trials(
        self,
        session_id: str,
        pool: EntropyPool,
        count: int = 1,
        condition_override: ConditionType | None = None,
    ) -> list[dict[str, Any]]:
        """Atomically draw entropy, compute running PEAR stats, and persist trials."""
        with self._lock:
            s_row = self._conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if not s_row:
                raise KeyError(session_id)
            session = dict(s_row)
            if session["status"] != "active":
                raise ValueError("Session is stopped; start a new session to collect trials.")

            last = self._conn.execute(
                """
                SELECT trial_index, cumulative_deviation
                FROM trials
                WHERE session_id = ?
                ORDER BY trial_index DESC
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            next_idx = (last["trial_index"] + 1) if last else 1
            cum_dev = float(last["cumulative_deviation"]) if last else 0.0

            bits_per_trial = int(session["bits_per_trial"])
            n_bytes = (bits_per_trial + 7) // 8
            expected_mean = bits_per_trial / 2.0
            trial_var = bits_per_trial / 4.0

            cond: ConditionType
            if session["mode"] in ("calibration", "dead_salmon"):
                cond = "BASELINE"
            else:
                cond = condition_override or session["condition"]  # type: ignore[assignment]

            created: list[dict[str, Any]] = []
            for offset in range(count):
                idx = next_idx + offset
                raw, source, fb_reason = pool.draw_bytes(
                    n_bytes,
                    entropy_mode=session["entropy_mode"],  # type: ignore[arg-type]
                )
                bit_str = bytes_to_bits(raw, bits_per_trial)
                trial_sum = bit_str.count("1")
                trial_dev = trial_sum - expected_mean
                cum_dev += trial_dev
                z_score = cum_dev / math.sqrt(idx * trial_var)
                p_val = two_sided_p_value(z_score)
                ts = utc_now_iso()
                raw_hex = raw.hex()
                sha = hashlib.sha256(raw).hexdigest()

                self._conn.execute(
                    """
                    INSERT INTO trials (
                        session_id, trial_index, timestamp, condition,
                        bits_per_trial, trial_sum, expected_mean,
                        trial_deviation, cumulative_deviation, z_score,
                        p_value_two_sided, entropy_source, fallback_reason,
                        raw_hex, raw_bits, bytes_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        idx,
                        ts,
                        cond,
                        bits_per_trial,
                        trial_sum,
                        expected_mean,
                        trial_dev,
                        cum_dev,
                        z_score,
                        p_val,
                        source,
                        fb_reason,
                        raw_hex,
                        bit_str,
                        sha,
                    ),
                )
                created.append(
                    {
                        "session_id": session_id,
                        "trial_index": idx,
                        "timestamp": ts,
                        "condition": cond,
                        "bits_per_trial": bits_per_trial,
                        "trial_sum": trial_sum,
                        "expected_mean": expected_mean,
                        "trial_deviation": trial_dev,
                        "cumulative_deviation": cum_dev,
                        "z_score": z_score,
                        "p_value_two_sided": p_val,
                        "entropy_source": source,
                        "fallback_reason": fb_reason,
                        "raw_hex": raw_hex,
                        "raw_bits": bit_str,
                        "bytes_sha256": sha,
                    }
                )
            self._conn.commit()
            return created

    def stop_session(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if not row:
                return None
            session = dict(row)
            if session["status"] == "stopped":
                return session
            count_row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM trials WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            n_trials = int(count_row["n"]) if count_row else 0
            planned = int(session["planned_trials"])
            early_stop = 1 if (planned > 0 and n_trials != planned) else 0
            ended = utc_now_iso()
            self._conn.execute(
                """
                UPDATE sessions
                SET status = 'stopped', ended_at = ?, early_stop = ?
                WHERE session_id = ?
                """,
                (ended, early_stop, session_id),
            )
            self._conn.commit()
        return self.get_session(session_id)


def compute_session_summary(session: dict[str, Any], trials: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute honest PEAR summary, pre-registration audit, and null-gated readout."""
    n_trials = len(trials)
    bits_per_trial = int(session["bits_per_trial"])
    expected_mean = bits_per_trial / 2.0
    planned = int(session["planned_trials"])

    if n_trials == 0:
        cum_dev = 0.0
        z_score = 0.0
        p_val = 1.0
        mean_shift = 0.0
        above_mean = 0
        below_mean = 0
        equal_mean = 0
        envelope_95 = 0.0
    else:
        last = trials[-1]
        cum_dev = float(last["cumulative_deviation"])
        z_score = float(last["z_score"])
        p_val = float(last["p_value_two_sided"])
        mean_shift = cum_dev / n_trials
        above_mean = sum(1 for t in trials if t["trial_sum"] > expected_mean)
        below_mean = sum(1 for t in trials if t["trial_sum"] < expected_mean)
        equal_mean = sum(1 for t in trials if t["trial_sum"] == expected_mean)
        envelope_95 = 1.96 * math.sqrt(n_trials * (bits_per_trial / 4.0))

    source_counts: dict[str, int] = {}
    for t in trials:
        src = t["entropy_source"]
        source_counts[src] = source_counts.get(src, 0) + 1

    # Pre-registration / optional stopping audit
    if planned == 0:
        prereg_status = "UNFIXED_OPEN_ENDED"
        prereg_note = "No pre-registered trial count (N); stopping on chart excursions risks optional-stopping bias."
    elif n_trials < planned:
        prereg_status = "INCOMPLETE_OR_EARLY_STOP" if session["status"] == "stopped" else "IN_PROGRESS"
        prereg_note = f"{n_trials}/{planned} pre-registered trials completed."
    elif n_trials == planned:
        prereg_status = "COMPLETE_AT_TARGET"
        prereg_note = f"Exact pre-registered target ({planned} trials) reached."
    else:
        prereg_status = "EXCEEDED_TARGET"
        prereg_note = f"Ran {n_trials} trials beyond pre-registered target {planned}."

    # Thesis 54 (Dead-Salmon gate): never treat noise around 0 as decisive signal
    if n_trials == 0:
        gate_verdict = "NO_DATA"
        gate_reason = "No trials recorded yet."
    elif session["mode"] in ("calibration", "dead_salmon") or session["condition"] == "BASELINE":
        if abs(z_score) >= 1.96:
            gate_verdict = "CONTROL_EXCURSION"
            gate_reason = (
                f"Negative-control / baseline run crossed |z| >= 1.96 (z={z_score:+.3f}, p={p_val:.4f}); "
                "demonstrates expected random-walk false-positive excursions under zero intention."
            )
        else:
            gate_verdict = "CONTROL_NULL"
            gate_reason = f"Baseline / control stream consistent with random null (z={z_score:+.3f}, p={p_val:.4f})."
    elif abs(z_score) >= 1.96:
        gate_verdict = "STATISTICALLY_NOTABLE_EXPLORATORY"
        gate_reason = (
            f"|z| >= 1.96 (z={z_score:+.3f}, two-sided p={p_val:.4f}). "
            "Exploratory statistical excursion only — not causal proof or operational authority."
        )
    else:
        gate_verdict = "NULL_NOISE_CONSISTENT"
        gate_reason = (
            f"|z| < 1.96 (z={z_score:+.3f}, two-sided p={p_val:.4f}). "
            "Indistinguishable from baseline 50/50 random noise."
        )

    # Nice-to-have binary directional lean (explicitly marked non-authoritative)
    high_label = session["high_label"]
    low_label = session["low_label"]
    if n_trials == 0 or z_score == 0.0:
        lean_direction = "NEUTRAL"
        lean_label = "No directional lean (z = 0.000)"
    elif z_score > 0:
        lean_direction = "HIGH_POLE"
        lean_label = f"Raw z-direction leans toward HIGH pole: {high_label}"
    else:
        lean_direction = "LOW_POLE"
        lean_label = f"Raw z-direction leans toward LOW pole: {low_label}"

    if above_mean > below_mean:
        majority_lean = f"Trial majority ({above_mean}/{n_trials} > {expected_mean:g}) leans HIGH: {high_label}"
    elif below_mean > above_mean:
        majority_lean = f"Trial majority ({below_mean}/{n_trials} < {expected_mean:g}) leans LOW: {low_label}"
    else:
        majority_lean = f"Trial majority tied ({above_mean} above / {below_mean} below)"

    return {
        "trial_count": n_trials,
        "bits_per_trial": bits_per_trial,
        "expected_mean": expected_mean,
        "cumulative_deviation": round(cum_dev, 6),
        "mean_shift_per_trial": round(mean_shift, 6),
        "z_score": round(z_score, 6),
        "p_value_two_sided": round(p_val, 6),
        "envelope_95_bits": round(envelope_95, 4),
        "trials_above_mean": above_mean,
        "trials_below_mean": below_mean,
        "trials_equal_mean": equal_mean,
        "entropy_source_breakdown": source_counts,
        "preregistration_audit": {
            "status": prereg_status,
            "planned_trials": planned,
            "note": prereg_note,
        },
        "statistical_gate": {
            "verdict": gate_verdict,
            "threshold_z": 1.96,
            "reason": gate_reason,
        },
        "exploratory_binary_lean": {
            "direction": lean_direction,
            "z_lean_summary": lean_label,
            "majority_summary": majority_lean,
            "authority": "NONE — exploratory readout only; never authorizes real-world action",
        },
    }


class CreateSessionRequest(BaseModel):
    mode: ModeType = "intention"
    condition: ConditionType = "HIGH"
    intention_text: str = Field(default="", max_length=1000)
    high_label: str = Field(default="Delay / Safer Path (HIGH)", min_length=1, max_length=120)
    low_label: str = Field(default="Proceed (LOW)", min_length=1, max_length=120)
    bits_per_trial: int = Field(default=200, ge=8, le=10000)
    planned_trials: int = Field(default=50, ge=0, le=100000)
    entropy_mode: EntropyModeType = "auto"
    notes: str = Field(default="", max_length=1000)


class RunTrialsRequest(BaseModel):
    count: int = Field(default=1, ge=1, le=500)
    condition_override: ConditionType | None = None


HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>PEAR-Style Intention RNG Experiment</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
  <style>
    :root {
      --bg: #0b0f14;
      --panel: #131a22;
      --panel-2: #1a232e;
      --border: #263241;
      --text: #e6edf3;
      --muted: #8b9bb0;
      --accent: #38bdf8;
      --emerald: #34d399;
      --amber: #fbbf24;
      --rose: #fb7185;
      --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      --sans: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: var(--sans);
      background: var(--bg);
      color: var(--text);
      line-height: 1.45;
    }
    .disclaimer {
      background: rgba(251, 191, 36, 0.12);
      border-bottom: 1px solid rgba(251, 191, 36, 0.4);
      color: #fde68a;
      padding: 10px 20px;
      font-size: 0.88rem;
      display: flex;
      gap: 10px;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
    }
    .disclaimer strong { color: #fbbf24; }
    .container {
      max-width: 1360px;
      margin: 0 auto;
      padding: 20px;
      display: grid;
      grid-template-columns: 370px 1fr;
      gap: 20px;
    }
    @media (max-width: 980px) {
      .container { grid-template-columns: 1fr; }
    }
    .card {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 16px;
    }
    h1 {
      margin: 0 0 6px;
      font-size: 1.25rem;
      letter-spacing: -0.01em;
    }
    h2 {
      margin: 0 0 12px;
      font-size: 0.95rem;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: var(--muted);
    }
    .sub { color: var(--muted); font-size: 0.84rem; margin-bottom: 14px; }
    label {
      display: block;
      font-size: 0.8rem;
      color: var(--muted);
      margin: 10px 0 4px;
    }
    input, select, textarea {
      width: 100%;
      background: var(--panel-2);
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 8px 10px;
      font-size: 0.88rem;
      font-family: inherit;
    }
    textarea { resize: vertical; min-height: 62px; }
    .row2 { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .btn-row { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
    button {
      background: var(--panel-2);
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 8px 12px;
      font-size: 0.85rem;
      cursor: pointer;
      font-weight: 600;
    }
    button:hover:not(:disabled) { border-color: var(--accent); }
    button:disabled { opacity: 0.45; cursor: not-allowed; }
    button.primary { background: #0284c7; border-color: #38bdf8; color: #fff; }
    button.danger { background: #881337; border-color: #fb7185; color: #fff; }
    button.emerald { background: #065f46; border-color: #34d399; color: #fff; }
    button.small { padding: 5px 9px; font-size: 0.78rem; }
    .kpis {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 16px;
    }
    @media (max-width: 760px) { .kpis { grid-template-columns: repeat(2, 1fr); } }
    .kpi {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 12px 14px;
    }
    .kpi .k-label { font-size: 0.75rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }
    .kpi .k-val { font-family: var(--mono); font-size: 1.35rem; font-weight: 700; margin-top: 4px; }
    .kpi .k-sub { font-size: 0.76rem; color: var(--muted); margin-top: 2px; font-family: var(--mono); }
    .chart-wrap {
      position: relative;
      height: 330px;
      width: 100%;
    }
    canvas { width: 100%; height: 100%; }
    .pill {
      display: inline-block;
      padding: 2px 8px;
      border-radius: 999px;
      font-size: 0.75rem;
      font-family: var(--mono);
      border: 1px solid var(--border);
      background: var(--panel-2);
    }
    .pill.null { color: var(--muted); }
    .pill.notable { color: var(--amber); border-color: rgba(251, 191, 36, 0.5); }
    .pill.live { color: var(--emerald); border-color: rgba(52, 211, 153, 0.5); }
    .pill.warn { color: var(--rose); border-color: rgba(251, 113, 133, 0.5); }
    .gate-box {
      margin-top: 14px;
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
    }
    @media (max-width: 760px) { .gate-box { grid-template-columns: 1fr; } }
    .subcard {
      background: var(--panel-2);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px;
      font-size: 0.84rem;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-family: var(--mono);
      font-size: 0.78rem;
    }
    th, td {
      text-align: left;
      padding: 6px 8px;
      border-bottom: 1px solid var(--border);
    }
    th { color: var(--muted); font-weight: 600; }
    .table-scroll { max-height: 240px; overflow-y: auto; }
    .stack { display: flex; flex-direction: column; gap: 16px; }
    .mono { font-family: var(--mono); }
    .muted { color: var(--muted); }
    .status-msg { font-size: 0.8rem; color: var(--accent); margin-top: 8px; min-height: 1.2em; }
  </style>
</head>
<body>
  <div class="disclaimer">
    <div>
      <strong>EXPLORATORY EXPERIMENT ONLY:</strong>
      Inspired by PEAR Lab 200-bit REG protocols. Fluctuations in random bitstreams are
      <strong>not</strong> scientific proof and carry <strong>zero operational, legal, or safety authority</strong>.
    </div>
    <span id="entropyHealthBadge" class="pill">entropy: checking…</span>
  </div>

  <div class="container">
    <div class="stack">
      <div class="card">
        <h1>PEAR Intention RNG</h1>
        <div class="sub">Tripolar protocol (HIGH / LOW / BASELINE) + Dead-Salmon control &amp; per-byte provenance</div>

        <div class="btn-row" style="margin-top:0; margin-bottom:8px;">
          <button type="button" class="small" id="btnPresetIowa">Load Iowa Detonation Preset</button>
          <button type="button" class="small" id="btnPresetSalmon">Load Dead-Salmon Control</button>
        </div>

        <label for="modeSelect">Experiment Mode</label>
        <select id="modeSelect">
          <option value="intention">Intention Session (manual / batch)</option>
          <option value="continuous">Continuous Monitoring (timed loop)</option>
          <option value="calibration">Calibration (forced BASELINE)</option>
          <option value="dead_salmon">Dead-Salmon Negative Control (Thesis 54)</option>
        </select>

        <div class="row2">
          <div>
            <label for="conditionSelect">Condition (Tripolar)</label>
            <select id="conditionSelect">
              <option value="HIGH">HIGH (+z target)</option>
              <option value="LOW">LOW (-z opposite)</option>
              <option value="BASELINE">BASELINE (no intention)</option>
            </select>
          </div>
          <div>
            <label for="entropySelect">Entropy Source</label>
            <select id="entropySelect">
              <option value="auto">Auto (ANU QRNG -> secrets)</option>
              <option value="local_secrets">Local CSPRNG (secrets)</option>
              <option value="anu_legacy">ANU Legacy (+ fallback)</option>
              <option value="anu_v2">ANU v2 API Key (+ fallback)</option>
            </select>
          </div>
        </div>

        <label for="intentionInput">Declared Intention</label>
        <textarea id="intentionInput" placeholder="Enter intention (or leave blank for baseline/calibration)"></textarea>

        <div class="row2">
          <div>
            <label for="highLabelInput">HIGH (+z) Pole Label</label>
            <input id="highLabelInput" value="Delay / Safer Path (HIGH)" />
          </div>
          <div>
            <label for="lowLabelInput">LOW (-z) Pole Label</label>
            <input id="lowLabelInput" value="Proceed (LOW)" />
          </div>
        </div>

        <div class="row2">
          <div>
            <label for="bitsInput">Bits / Trial (PEAR = 200)</label>
            <input id="bitsInput" type="number" min="8" max="10000" value="200" />
          </div>
          <div>
            <label for="plannedInput">Pre-registered N (0 = open)</label>
            <input id="plannedInput" type="number" min="0" max="100000" value="50" />
          </div>
        </div>

        <label for="intervalInput">Continuous Interval (ms)</label>
        <input id="intervalInput" type="number" min="150" max="10000" value="1000" />

        <div class="btn-row">
          <button type="button" class="primary" id="btnStart">Start Session</button>
          <button type="button" class="danger" id="btnStop" disabled>Stop Session</button>
        </div>

        <div class="btn-row">
          <button type="button" class="emerald" id="btnStep1" disabled>+1 Trial</button>
          <button type="button" id="btnStep10" disabled>+10 Trials</button>
          <button type="button" id="btnStep50" disabled>+50 Trials</button>
          <button type="button" id="btnToggleAuto" disabled>Start Loop</button>
        </div>

        <div class="btn-row">
          <button type="button" class="small" id="btnExportCsv" disabled>Export CSV</button>
          <button type="button" class="small" id="btnExportBits" disabled>Export Raw Bits</button>
          <button type="button" class="small" id="btnExportJson" disabled>Export JSON</button>
        </div>

        <div id="statusMsg" class="status-msg"></div>
      </div>

      <div class="card">
        <h2>Recent Sessions (SQLite)</h2>
        <div class="table-scroll">
          <table>
            <thead>
              <tr><th>ID</th><th>Mode / Cond</th><th>N</th><th>Z</th><th>Status</th></tr>
            </thead>
            <tbody id="sessionsBody"></tbody>
          </table>
        </div>
      </div>
    </div>

    <div class="stack">
      <div class="kpis">
        <div class="kpi">
          <div class="k-label">Trials</div>
          <div class="k-val" id="kpiTrials">0</div>
          <div class="k-sub" id="kpiPlanned">target: -</div>
        </div>
        <div class="kpi">
          <div class="k-label">Cumulative Dev</div>
          <div class="k-val" id="kpiCumDev">0.0</div>
          <div class="k-sub" id="kpiMeanShift">shift/trial: 0.000</div>
        </div>
        <div class="kpi">
          <div class="k-label">Running Z-Score</div>
          <div class="k-val" id="kpiZ">0.000</div>
          <div class="k-sub" id="kpiP">p (2-sided): 1.0000</div>
        </div>
        <div class="kpi">
          <div class="k-label">Entropy Provenance</div>
          <div class="k-val" id="kpiSource" style="font-size:1.0rem;">-</div>
          <div class="k-sub" id="kpiSessionMeta">no active session</div>
        </div>
      </div>

      <div class="card">
        <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px; margin-bottom:8px;">
          <h2 style="margin:0;">Cumulative Deviation vs. 95% Null Envelope (±1.96σ)</h2>
          <span id="preregBadge" class="pill null">no session</span>
        </div>
        <div class="chart-wrap">
          <canvas id="devChart"></canvas>
        </div>

        <div class="gate-box">
          <div class="subcard">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
              <strong>Statistical Gate (Thesis 54)</strong>
              <span id="gatePill" class="pill null">NO_DATA</span>
            </div>
            <div id="gateText" class="muted">Start a session and run trials to inspect deviation against random null bounds.</div>
          </div>
          <div class="subcard">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
              <strong>Exploratory Directional Lean</strong>
              <span id="leanPill" class="pill null">NEUTRAL</span>
            </div>
            <div id="leanText" class="muted">No directional lean yet.</div>
            <div id="majorityText" class="muted" style="margin-top:4px; font-size:0.78rem;"></div>
          </div>
        </div>
      </div>

      <div class="card">
        <h2>Trial Log (latest first)</h2>
        <div class="table-scroll">
          <table>
            <thead>
              <tr>
                <th>#</th>
                <th>Cond</th>
                <th>Sum</th>
                <th>Dev</th>
                <th>CumDev</th>
                <th>Z</th>
                <th>Entropy Source</th>
                <th>SHA-256 (12)</th>
              </tr>
            </thead>
            <tbody id="trialsBody"></tbody>
          </table>
        </div>
      </div>
    </div>
  </div>

<script>
  const state = {
    sessionId: null,
    session: null,
    trials: [],
    summary: null,
    loopTimer: null,
    chart: null,
  };

  const el = (id) => document.getElementById(id);

  function setStatus(msg) {
    el("statusMsg").textContent = msg || "";
  }

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
      ...opts,
    });
    const text = await res.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch (_) {}
    if (!res.ok) {
      throw new Error((data && data.detail) ? data.detail : `HTTP ${res.status}`);
    }
    return data;
  }

  function syncModeLock() {
    const m = el("modeSelect").value;
    const cond = el("conditionSelect");
    if (m === "calibration" || m === "dead_salmon") {
      cond.value = "BASELINE";
      cond.disabled = true;
    } else {
      cond.disabled = false;
    }
  }

  function renderChart(trials, bitsPerTrial) {
    const labels = [0, ...trials.map(t => t.trial_index)];
    const cumVals = [0, ...trials.map(t => t.cumulative_deviation)];
    const envPos = labels.map(n => 1.96 * Math.sqrt(n * (bitsPerTrial / 4.0)));
    const envNeg = envPos.map(v => -v);
    const zeroLine = labels.map(() => 0);

    const canvas = el("devChart");
    if (typeof window.Chart !== "undefined") {
      if (state.chart) {
        state.chart.data.labels = labels;
        state.chart.data.datasets[0].data = cumVals;
        state.chart.data.datasets[1].data = envPos;
        state.chart.data.datasets[2].data = envNeg;
        state.chart.data.datasets[3].data = zeroLine;
        state.chart.update("none");
        return;
      }
      state.chart = new Chart(canvas.getContext("2d"), {
        type: "line",
        data: {
          labels,
          datasets: [
            {
              label: "Cumulative Deviation (bits)",
              data: cumVals,
              borderColor: "#38bdf8",
              backgroundColor: "rgba(56, 189, 248, 0.12)",
              borderWidth: 2,
              pointRadius: trials.length > 80 ? 0 : 2.5,
              tension: 0.08,
            },
            {
              label: "+1.96σ Null Envelope",
              data: envPos,
              borderColor: "rgba(251, 191, 36, 0.65)",
              borderDash: [5, 5],
              borderWidth: 1.4,
              pointRadius: 0,
              fill: false,
            },
            {
              label: "-1.96σ Null Envelope",
              data: envNeg,
              borderColor: "rgba(251, 191, 36, 0.65)",
              borderDash: [5, 5],
              borderWidth: 1.4,
              pointRadius: 0,
              fill: false,
            },
            {
              label: "Expected Mean (0)",
              data: zeroLine,
              borderColor: "rgba(139, 155, 176, 0.35)",
              borderDash: [2, 4],
              borderWidth: 1,
              pointRadius: 0,
            }
          ]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          animation: false,
          plugins: {
            legend: { labels: { color: "#cbd5e1", boxWidth: 16 } }
          },
          scales: {
            x: {
              title: { display: true, text: "Trial #", color: "#8b9bb0" },
              ticks: { color: "#8b9bb0" },
              grid: { color: "rgba(38, 50, 65, 0.55)" }
            },
            y: {
              title: { display: true, text: "Cumulative Bit Deviation", color: "#8b9bb0" },
              ticks: { color: "#8b9bb0" },
              grid: { color: "rgba(38, 50, 65, 0.55)" }
            }
          }
        }
      });
      return;
    }

    // Offline Canvas fallback if Chart.js CDN is unreachable
    const rect = canvas.parentElement.getBoundingClientRect();
    canvas.width = Math.max(400, Math.floor(rect.width));
    canvas.height = 320;
    const ctx = canvas.getContext("2d");
    const w = canvas.width, h = canvas.height;
    ctx.fillStyle = "#131a22";
    ctx.fillRect(0, 0, w, h);

    const allY = [...cumVals, ...envPos, ...envNeg, 10, -10];
    const minY = Math.min(...allY);
    const maxY = Math.max(...allY);
    const padL = 52, padR = 16, padT = 18, padB = 28;
    const pw = w - padL - padR;
    const ph = h - padT - padB;
    const xAt = (i) => padL + (labels.length <= 1 ? 0 : (i / (labels.length - 1)) * pw);
    const yAt = (v) => padT + ((maxY - v) / (maxY - minY || 1)) * ph;

    // Zero line
    ctx.strokeStyle = "rgba(139,155,176,0.4)";
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(padL, yAt(0));
    ctx.lineTo(w - padR, yAt(0));
    ctx.stroke();

    // Envelopes
    ctx.strokeStyle = "rgba(251,191,36,0.7)";
    [envPos, envNeg].forEach((arr) => {
      ctx.beginPath();
      arr.forEach((v, i) => i === 0 ? ctx.moveTo(xAt(i), yAt(v)) : ctx.lineTo(xAt(i), yAt(v)));
      ctx.stroke();
    });

    // Cumulative line
    ctx.setLineDash([]);
    ctx.strokeStyle = "#38bdf8";
    ctx.lineWidth = 2;
    ctx.beginPath();
    cumVals.forEach((v, i) => i === 0 ? ctx.moveTo(xAt(i), yAt(v)) : ctx.lineTo(xAt(i), yAt(v)));
    ctx.stroke();

    ctx.fillStyle = "#8b9bb0";
    ctx.font = "11px monospace";
    ctx.fillText(maxY.toFixed(1), 6, padT + 8);
    ctx.fillText("0.0", 6, yAt(0) + 4);
    ctx.fillText(minY.toFixed(1), 6, h - padB);
    ctx.fillText(`Trials: ${trials.length} (offline canvas fallback)`, padL, h - 8);
  }

  function renderSession(payload) {
    state.session = payload.session;
    state.sessionId = payload.session.session_id;
    state.trials = payload.trials || [];
    state.summary = payload.summary;

    const s = state.session;
    const sum = state.summary;
    const active = s.status === "active";

    el("btnStop").disabled = !active;
    el("btnStep1").disabled = !active;
    el("btnStep10").disabled = !active;
    el("btnStep50").disabled = !active;
    el("btnToggleAuto").disabled = !active;
    if (!active && state.loopTimer) {
      clearInterval(state.loopTimer);
      state.loopTimer = null;
      el("btnToggleAuto").textContent = "Start Loop";
    }
    el("btnExportCsv").disabled = false;
    el("btnExportBits").disabled = false;
    el("btnExportJson").disabled = false;

    el("kpiTrials").textContent = String(sum.trial_count);
    el("kpiPlanned").textContent = sum.preregistration_audit.planned_trials > 0
      ? `target: ${sum.preregistration_audit.planned_trials}`
      : "target: unfixed";
    el("kpiCumDev").textContent = (sum.cumulative_deviation >= 0 ? "+" : "") + sum.cumulative_deviation.toFixed(1);
    el("kpiMeanShift").textContent = `shift/trial: ${(sum.mean_shift_per_trial >= 0 ? "+" : "")}${sum.mean_shift_per_trial.toFixed(3)}`;
    el("kpiZ").textContent = (sum.z_score >= 0 ? "+" : "") + sum.z_score.toFixed(3);
    el("kpiP").textContent = `p (2-sided): ${sum.p_value_two_sided.toFixed(4)}`;

    const sources = Object.entries(sum.entropy_source_breakdown);
    el("kpiSource").textContent = sources.length ? sources.map(([k, v]) => `${k}:${v}`).join(", ") : s.entropy_mode;
    el("kpiSessionMeta").textContent = `${s.mode} / ${s.condition} / ${s.status}`;

    const pr = sum.preregistration_audit;
    const prBadge = el("preregBadge");
    prBadge.textContent = `${pr.status}: ${pr.note}`;
    prBadge.className = "pill " + (pr.status === "COMPLETE_AT_TARGET" ? "live" : (pr.status.includes("EARLY") || pr.status === "UNFIXED_OPEN_ENDED" ? "warn" : "null"));

    const gate = sum.statistical_gate;
    const gPill = el("gatePill");
    gPill.textContent = gate.verdict;
    gPill.className = "pill " + (gate.verdict.includes("NOTABLE") || gate.verdict === "CONTROL_EXCURSION" ? "notable" : "null");
    el("gateText").textContent = gate.reason;

    const lean = sum.exploratory_binary_lean;
    const lPill = el("leanPill");
    lPill.textContent = lean.direction;
    lPill.className = "pill " + (lean.direction === "NEUTRAL" ? "null" : "notable");
    el("leanText").textContent = lean.z_lean_summary;
    el("majorityText").textContent = `${lean.majority_summary} · ${lean.authority}`;

    renderChart(state.trials, s.bits_per_trial);

    // Trials table (latest first, capped at 200 rows rendered for snappy DOM)
    const tbody = el("trialsBody");
    tbody.replaceChildren();
    const recent = [...state.trials].reverse().slice(0, 200);
    for (const t of recent) {
      const tr = document.createElement("tr");
      const cells = [
        String(t.trial_index),
        t.condition,
        `${t.trial_sum}/${t.bits_per_trial}`,
        (t.trial_deviation >= 0 ? "+" : "") + t.trial_deviation.toFixed(1),
        (t.cumulative_deviation >= 0 ? "+" : "") + t.cumulative_deviation.toFixed(1),
        (t.z_score >= 0 ? "+" : "") + t.z_score.toFixed(3),
        t.fallback_reason ? `${t.entropy_source} (${t.fallback_reason})` : t.entropy_source,
        (t.bytes_sha256 || "").slice(0, 12),
      ];
      for (const c of cells) {
        const td = document.createElement("td");
        td.textContent = c;
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }
  }

  async function refreshSessionList() {
    const data = await api("/api/sessions");
    const body = el("sessionsBody");
    body.replaceChildren();
    for (const s of data.sessions || []) {
      const tr = document.createElement("tr");
      tr.style.cursor = "pointer";
      tr.addEventListener("click", () => loadSession(s.session_id));
      const vals = [
        s.session_id.slice(0, 8),
        `${s.mode}/${s.condition}`,
        String(s.trial_count),
        (s.z_score >= 0 ? "+" : "") + Number(s.z_score).toFixed(2),
        s.status,
      ];
      for (const v of vals) {
        const td = document.createElement("td");
        td.textContent = v;
        tr.appendChild(td);
      }
      body.appendChild(tr);
    }
  }

  async function loadSession(sessionId) {
    const payload = await api(`/api/sessions/${encodeURIComponent(sessionId)}`);
    renderSession(payload);
    setStatus(`Loaded session ${sessionId.slice(0, 8)}`);
  }

  async function startSession() {
    try {
      if (state.loopTimer) {
        clearInterval(state.loopTimer);
        state.loopTimer = null;
        el("btnToggleAuto").textContent = "Start Loop";
      }
      const body = {
        mode: el("modeSelect").value,
        condition: el("conditionSelect").value,
        intention_text: el("intentionInput").value.trim(),
        high_label: el("highLabelInput").value.trim() || "Delay / Safer Path (HIGH)",
        low_label: el("lowLabelInput").value.trim() || "Proceed (LOW)",
        bits_per_trial: Number(el("bitsInput").value) || 200,
        planned_trials: Number(el("plannedInput").value) || 0,
        entropy_mode: el("entropySelect").value,
      };
      const payload = await api("/api/sessions", { method: "POST", body: JSON.stringify(body) });
      renderSession(payload);
      await refreshSessionList();
      setStatus(`Started session ${payload.session.session_id.slice(0, 8)}`);
      if (body.mode === "continuous") {
        toggleLoop();
      }
    } catch (err) {
      setStatus(`Error: ${err.message}`);
    }
  }

  async function runTrials(count) {
    if (!state.sessionId) return;
    try {
      const payload = await api(`/api/sessions/${encodeURIComponent(state.sessionId)}/trial`, {
        method: "POST",
        body: JSON.stringify({ count }),
      });
      renderSession(payload);
      await refreshSessionList();
      const target = state.session.planned_trials;
      if (state.loopTimer && target > 0 && state.summary.trial_count >= target) {
        clearInterval(state.loopTimer);
        state.loopTimer = null;
        el("btnToggleAuto").textContent = "Start Loop";
        setStatus(`Pre-registered target (${target} trials) reached; loop paused.`);
      }
    } catch (err) {
      if (state.loopTimer) {
        clearInterval(state.loopTimer);
        state.loopTimer = null;
        el("btnToggleAuto").textContent = "Start Loop";
      }
      setStatus(`Error: ${err.message}`);
    }
  }

  async function stopSession() {
    if (!state.sessionId) return;
    try {
      const payload = await api(`/api/sessions/${encodeURIComponent(state.sessionId)}/stop`, {
        method: "POST",
      });
      renderSession(payload);
      await refreshSessionList();
      setStatus(`Stopped session ${state.sessionId.slice(0, 8)}`);
    } catch (err) {
      setStatus(`Error: ${err.message}`);
    }
  }

  function toggleLoop() {
    if (!state.sessionId) return;
    if (state.loopTimer) {
      clearInterval(state.loopTimer);
      state.loopTimer = null;
      el("btnToggleAuto").textContent = "Start Loop";
      setStatus("Continuous loop paused.");
      return;
    }
    const ms = Math.max(150, Number(el("intervalInput").value) || 1000);
    el("btnToggleAuto").textContent = "Pause Loop";
    runTrials(1);
    state.loopTimer = setInterval(() => runTrials(1), ms);
    setStatus(`Running 1 trial every ${ms}ms…`);
  }

  async function checkHealth() {
    try {
      const h = await api("/api/health");
      const badge = el("entropyHealthBadge");
      badge.textContent = `ANU key: ${h.anu_api_key_configured ? "configured (v2)" : "none (legacy+secrets)"}`;
      badge.className = "pill live";
    } catch (err) {
      el("entropyHealthBadge").textContent = "backend offline";
      el("entropyHealthBadge").className = "pill warn";
    }
  }

  el("modeSelect").addEventListener("change", syncModeLock);
  el("btnPresetIowa").addEventListener("click", () => {
    el("modeSelect").value = "intention";
    syncModeLock();
    el("conditionSelect").value = "HIGH";
    el("intentionInput").value = "Exploratory PEAR run: safer path / delay / no open detonation (Iowa lead-azide context)";
    el("highLabelInput").value = "Delay / Safer Path (HIGH)";
    el("lowLabelInput").value = "Proceed (LOW)";
    el("plannedInput").value = "50";
    setStatus("Loaded Iowa detonation exploratory preset.");
  });
  el("btnPresetSalmon").addEventListener("click", () => {
    el("modeSelect").value = "dead_salmon";
    syncModeLock();
    el("intentionInput").value = "Dead-Salmon negative control: zero intention (Thesis 54 baseline check)";
    el("plannedInput").value = "50";
    setStatus("Loaded Dead-Salmon negative control preset.");
  });
  el("btnStart").addEventListener("click", startSession);
  el("btnStop").addEventListener("click", stopSession);
  el("btnStep1").addEventListener("click", () => runTrials(1));
  el("btnStep10").addEventListener("click", () => runTrials(10));
  el("btnStep50").addEventListener("click", () => runTrials(50));
  el("btnToggleAuto").addEventListener("click", toggleLoop);
  el("btnExportCsv").addEventListener("click", () => {
    if (state.sessionId) window.location.href = `/api/sessions/${encodeURIComponent(state.sessionId)}/export.csv`;
  });
  el("btnExportBits").addEventListener("click", () => {
    if (state.sessionId) window.location.href = `/api/sessions/${encodeURIComponent(state.sessionId)}/export.bits.txt`;
  });
  el("btnExportJson").addEventListener("click", () => {
    if (state.sessionId) window.location.href = `/api/sessions/${encodeURIComponent(state.sessionId)}/export.json`;
  });

  (async () => {
    syncModeLock();
    renderChart([], 200);
    await checkHealth();
    await refreshSessionList();
  })();
</script>
</body>
</html>
"""


def create_app(
    db_path: str | Path | None = None,
    entropy_pool: EntropyPool | None = None,
) -> FastAPI:
    resolved_db = Path(db_path or os.environ.get("PEAR_RNG_DB") or DEFAULT_DB_PATH)
    pool = entropy_pool or EntropyPool()
    storage = SessionStorage(resolved_db)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            yield
        finally:
            storage.close()

    application = FastAPI(
        title="PEAR-Style Consciousness / Intention RNG Experiment",
        version="1.0.0",
        lifespan=lifespan,
    )
    application.state.storage = storage
    application.state.pool = pool

    def _bundle(session_id: str) -> dict[str, Any]:
        session = storage.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        trials = storage.get_trials(session_id)
        summary = compute_session_summary(session, trials)
        return {
            "disclaimer": DISCLAIMER_TEXT,
            "session": session,
            "trials": trials,
            "summary": summary,
        }

    @application.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse(content=HTML_PAGE)

    @application.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "disclaimer": DISCLAIMER_TEXT,
            "anu_api_key_configured": pool.api_key_configured(),
            "db_path": str(resolved_db),
        }

    @application.get("/api/entropy/probe")
    def probe_entropy(entropy_mode: EntropyModeType = "auto") -> dict[str, Any]:
        return pool.probe(entropy_mode=entropy_mode)

    @application.get("/api/sessions")
    def list_sessions(limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
        return {"sessions": storage.list_sessions(limit=limit)}

    @application.post("/api/sessions")
    def create_session(req: CreateSessionRequest) -> dict[str, Any]:
        effective_condition: ConditionType = (
            "BASELINE" if req.mode in ("calibration", "dead_salmon") else req.condition
        )
        session = {
            "session_id": str(uuid.uuid4()),
            "created_at": utc_now_iso(),
            "ended_at": None,
            "mode": req.mode,
            "condition": effective_condition,
            "intention_text": req.intention_text.strip(),
            "high_label": req.high_label.strip(),
            "low_label": req.low_label.strip(),
            "bits_per_trial": req.bits_per_trial,
            "planned_trials": req.planned_trials,
            "entropy_mode": req.entropy_mode,
            "status": "active",
            "early_stop": False,
            "notes": req.notes.strip(),
        }
        storage.create_session(session)
        return _bundle(session["session_id"])

    @application.get("/api/sessions/{session_id}")
    def get_session(session_id: str) -> dict[str, Any]:
        return _bundle(session_id)

    @application.post("/api/sessions/{session_id}/trial")
    def run_trials(session_id: str, req: RunTrialsRequest) -> dict[str, Any]:
        try:
            storage.append_trials(
                session_id,
                pool=pool,
                count=req.count,
                condition_override=req.condition_override,
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="Session not found")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return _bundle(session_id)

    @application.post("/api/sessions/{session_id}/stop")
    def stop_session(session_id: str) -> dict[str, Any]:
        stopped = storage.stop_session(session_id)
        if not stopped:
            raise HTTPException(status_code=404, detail="Session not found")
        return _bundle(session_id)

    @application.get("/api/sessions/{session_id}/export.csv")
    def export_csv(
        session_id: str,
        include_bits: bool = Query(default=False),
    ) -> Response:
        bundle = _bundle(session_id)
        session = bundle["session"]
        trials = bundle["trials"]

        buf = io.StringIO()
        writer = csv.writer(buf)
        headers = [
            "session_id",
            "mode",
            "trial_index",
            "timestamp",
            "intention_text",
            "condition",
            "bits_per_trial",
            "trial_sum",
            "expected_mean",
            "trial_deviation",
            "cumulative_deviation",
            "z_score",
            "p_value_two_sided",
            "entropy_source",
            "fallback_reason",
            "bytes_sha256",
        ]
        if include_bits:
            headers.extend(["raw_hex", "raw_bits"])
        writer.writerow(headers)

        clean_intention = (session["intention_text"] or "").replace("\r", " ").replace("\n", " ")
        for t in trials:
            row = [
                session["session_id"],
                session["mode"],
                t["trial_index"],
                t["timestamp"],
                clean_intention,
                t["condition"],
                t["bits_per_trial"],
                t["trial_sum"],
                t["expected_mean"],
                t["trial_deviation"],
                t["cumulative_deviation"],
                t["z_score"],
                t["p_value_two_sided"],
                t["entropy_source"],
                t["fallback_reason"],
                t["bytes_sha256"],
            ]
            if include_bits:
                row.extend([t["raw_hex"], t["raw_bits"]])
            writer.writerow(row)

        filename = f"pear_session_{session_id[:8]}.csv"
        return Response(
            content=buf.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @application.get("/api/sessions/{session_id}/export.json")
    def export_json(session_id: str) -> JSONResponse:
        bundle = _bundle(session_id)
        filename = f"pear_session_{session_id[:8]}.json"
        return JSONResponse(
            content=bundle,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @application.get("/api/sessions/{session_id}/export.bits.txt")
    def export_bits(session_id: str) -> PlainTextResponse:
        bundle = _bundle(session_id)
        session = bundle["session"]
        trials = bundle["trials"]
        lines = [
            f"# {DISCLAIMER_TEXT}",
            f"# session_id={session['session_id']} mode={session['mode']} condition={session['condition']}",
            f"# bits_per_trial={session['bits_per_trial']} planned_trials={session['planned_trials']}",
            f"# intention={session['intention_text'].replace(chr(10), ' ')}",
            "",
        ]
        for t in trials:
            lines.append(
                f"trial={t['trial_index']} ts={t['timestamp']} cond={t['condition']} "
                f"sum={t['trial_sum']}/{t['bits_per_trial']} z={t['z_score']:+.6f} "
                f"src={t['entropy_source']} sha256={t['bytes_sha256']}"
            )
            lines.append(t["raw_bits"])
        filename = f"pear_bits_{session_id[:8]}.txt"
        return PlainTextResponse(
            content="\n".join(lines) + "\n",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    return application


app = create_app()


def main() -> None:
    try:
        import uvicorn
    except ModuleNotFoundError as exc:
        print(
            "\n[pear-rng] Missing Python dependency: uvicorn\n"
            "Install dependencies via './run.sh' (or 'run.bat'), 'uv run app.py', "
            "or 'pip install -r requirements.txt'.\n"
            "Zero-dependency browser option: open public/index.html directly.\n",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc

    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "127.0.0.1")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
