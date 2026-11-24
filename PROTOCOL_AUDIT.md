# Protocol Audit: PEAR-Style Intention RNG Handoff

- **Source artifact:** `@LilithDatura` X post ([status/2125180506774151448](https://x.com/LilithDatura/status/2125180506774151448), `2026-11-24`)
- **Evaluated against:**
  - [`NachoBusiness17/falsifier-index`](https://github.com/NachoBusiness17/falsifier-index): [`INDEX.md`](https://github.com/NachoBusiness17/falsifier-index/blob/main/INDEX.md), [`AUDIT_PROTOCOL.md`](https://github.com/NachoBusiness17/falsifier-index/blob/main/AUDIT_PROTOCOL.md), [`hostile_corpus_2.md`](https://github.com/NachoBusiness17/falsifier-index/blob/main/evidence/hostile_corpus_2.md), [`EVIDENCE.md`](https://github.com/NachoBusiness17/falsifier-index/blob/main/evidence/EVIDENCE.md)
  - [`NachoBusiness17/mycelial-republic`](https://github.com/NachoBusiness17/mycelial-republic): [`CONSTITUTION.md`](https://github.com/NachoBusiness17/mycelial-republic/blob/master/docs/CONSTITUTION.md) (v0.1.1), [`AGENCY_SHAPE.md`](https://github.com/NachoBusiness17/mycelial-republic/blob/master/docs/AGENCY_SHAPE.md) (`agency-shape-life-ops-20260731`), [`SELFTEST.md`](https://github.com/NachoBusiness17/mycelial-republic/blob/master/docs/SELFTEST.md)
- **Implementation under audit:** [`app.py`](app.py), [`api/qrng.py`](api/qrng.py), [`public/index.html`](public/index.html)

---

## 1. Verdict Summary

As an **exploratory statistics / consciousness-research instrument**, the handoff specification is sound once tightened with explicit entropy provenance, pre-registered trial bounds, and a null-gated readout.

As a **decision mechanism** ("Turn the Iowa lead-azide detonation decision ... into a live PEAR-style intention experiment" with a binary `Proceed / Delay` recommendation), the unmodified handoff protocol **FAILS** both rule sets:

1. **Thesis 54 failure (Dead-Salmon Control):** Mapping any final z-score direction (`z > 0` vs `z <= 0`) to `Proceed / Delay` fires on pure noise ~99% of the time. Measured on `app.py` across 100 zero-intention `dead_salmon` sessions (`1,000,000` bits from Python `secrets`, 50 trials/session): **99/100 runs produced a non-neutral binary lean** (`HIGH=52`, `LOW=47`), and **33/100 runs crossed `|z| >= 1.96` at some intermediate peek** (`max |z| = 3.3941`), compared to **4/100** at fixed pre-registered `N = 50`.
2. **Constitutional Capture C7 / C11 & L3 violation:** Delegating or framing an **irreversible energetic-material / public-safety act** (lead-azide detonation) via an oracle/RNG readout violates `AGENCY_SHAPE.md` Design Goal 4 (`Irreversible = L3`), `CONSTITUTION.md` §11.5, and Thesis 47 ("rehearsal never settles").

---

## 2. Evaluation Against `falsifier-index` Rules

### 2.1 `INDEX.md` & `hostile_corpus_2.md` (Falsifiability & Evasion Classes)

| Rule / Class | Protocol Vulnerability in Raw Handoff | Status in Implementation |
|---|---|---|
| **Thesis 54 (Dead-Salmon Control)** — *"a gate that cannot fail is decorative by definition"* | A naive binary decision readout (`z > 0 -> Proceed`, `z <= 0 -> Delay`) never outputs "null / indeterminate" and fires on 99% of dead-salmon runs. | **Mitigated:** Added `dead_salmon` mode (forced `BASELINE`), `±1.96σ` null envelope on chart, and explicit `statistical_gate` (`NULL_NOISE_CONSISTENT` when `\|z\| < 1.96`). |
| **Hostile Class A (Croesus / Optional Stopping)** — *"satisfied by every possible outcome"* | Open-ended `Start / Stop` with a live cumulative chart invites peeking and stopping at local extrema (measured 33% false excursion rate across 50-trial walks). | **Mitigated:** Added `planned_trials` pre-registration field and `early_stop` / `UNFIXED_OPEN_ENDED` audit flags on session summary and exports. |
| **Hostile Class B (Sabbatai Zevi / Absorbed Falsification)** | Post-hoc re-interpretation of negative deviations ("psi-missing") or flipping which pole (`HIGH` vs `LOW`) meant "Delay" vs "Proceed" makes both arms confirm. | **Mitigated:** Pole labels (`high_label`, `low_label`) and `condition` (`HIGH`/`LOW`/`BASELINE`) are bound at session creation before trial 1. |
| **Thesis 50 & 51 (Trust Bytes / Banked Claims)** | Silent fallback from ANU QRNG to `secrets` would let a local PRNG session masquerade as "quantum" without receipts. | **Mitigated:** Every trial records exact `entropy_source` (`anu_v2`, `anu_legacy`, `secrets_fallback`, `secrets_local`, `browser_csprng`), `fallback_reason`, `raw_hex`, `raw_bits`, and `bytes_sha256`. |
| **Thesis 53 (Prophecy Without a Falsifier)** | Framing the Iowa detonation decision as an RNG-mediated experiment with no stated kill condition for the intention hypothesis. | **Recorded debt:** An intention run only tests null departure on that bitstream; it tests nothing about physical detonation safety. |

### 2.2 `AUDIT_PROTOCOL.md` (Four Possessions Audit)

| Possession | Standalone (`public/index.html` / `local_secrets`) | Remote ANU QRNG (`anu_legacy` / `anu_v2`) | Notes |
|---|---|---|---|
| **1. Keys / Entropy Origin** | **PASS (Clean):** Holder's OS CSPRNG (`crypto.getRandomValues` / `secrets`), offline, zero third-party permission. | **PARTIAL (Permissioned):** External HTTP dependency; `anu_legacy` rate-limited/intermittent; `anu_v2` gated by provider-issued `x-api-key`. | Every trial tags exact byte provenance so remote failure degrades openly. |
| **2. Memory** | **PASS (Clean):** Browser `localStorage` or local SQLite (`pear_rng.db`) + unmediated CSV, JSON, and raw bitstream exports. | **PASS (Clean):** Remote receives only byte-length queries, never session state or intention text. | Holder keeps full machine-readable history. |
| **3. Weights / Scoring** | **PASS (Clean):** Exact closed-form binomial + `erfc` scoring; zero opaque weights. | **PASS (Clean):** Same deterministic scoring. | Fully inspectable source. |
| **4. Settlement / Authority** | **FAIL as Decision Authority:** Zero physical, legal, or chain-verifiable authority over real-world detonation. | **FAIL as Decision Authority:** Same. | Valid only as an exploratory statistical notebook. |

### 2.3 Empirical Dead-Salmon Receipt (`app.py`, `2026-11-24`)

Executed directly against `SessionStorage`, `EntropyPool`, and `compute_session_summary` in [`app.py`](app.py) (`100` independent `dead_salmon` sessions × `50` trials × `200` bits = `1,000,000` bits from `secrets`):

- **Naive directional lean (`z != 0`):** fired on `99 / 100` control runs (`HIGH_POLE = 52`, `LOW_POLE = 47`, `NEUTRAL = 1`).
- **Pre-registered fixed-`N` gate (`|z| >= 1.96` at `N = 50`):** fired on `4 / 100` control runs (`4%`, matching theoretical $\alpha = 0.05$).
- **Optional-stopping / live-peek rate (`|z| >= 1.96` at any trial `1..50`):** `33 / 100` control runs (`33%`), with `max |z| = 3.3941`.

---

## 3. Evaluation Against `mycelial-republic` Rules

| Constitutional / Agency Rule | Assessment | Design Enforcement |
|---|---|---|
| **`CONSTITUTION.md` §11.5 & `AGENCY_SHAPE.md` Goal 4 (`Irreversible = L3`)** | Detonation / energetic-material handling is an irreversible physical act. Delegating or nudging it via RNG output is **Capture C7** (*model/oracle-as-truth*) and **C11** (*autonomy without L3 human seal*). | Top-level UI banner, API JSON `disclaimer`, and `exploratory_binary_lean.authority = "NONE"` explicitly refuse decision authority. |
| **`CONSTITUTION.md` §7 (Data Tiers T0–T3)** | `ANU_QRNG_API_KEY` is **T0** (secret). `intention_text` can be **T1** (private operator text). | `ANU_QRNG_API_KEY` stays in server environment variables, never enters SQLite/exports/HTML; `intention_text` stays local and is never sent to external APIs. |
| **`CONSTITUTION.md` §10 (`Mining law`)** | External X post is public T2 spark (`https://x.com/LilithDatura/status/2125180506774151448`, `2026-11-24`). | Source attributed with URL + date. |
| **`CONSTITUTION.md` §6 (`C8` Anti-greenwashing) & `SELFTEST.md` (`Performativity`)** | Slapping a tiny disclaimer below a big `PROCEED` / `DELAY` button is performative ("disclaimer sludge" masking capture). | The statistical gate (`NULL_NOISE_CONSISTENT` vs `STATISTICALLY_NOTABLE_EXPLORATORY`) sits co-equal beside the directional lean and defaults to rejecting noise below `|z| = 1.96`. |

---

## 4. Field Alignment (`Adopt / Adapt / Refuse`)

- **Adopt:** Tripolar condition separation (`HIGH` / `LOW` / `BASELINE`), `200`-bit PEAR trial structure, local-first storage, raw bitstream export, and calibration baseline mode.
- **Adapt:** Require explicit byte-level entropy provenance (`anu_v2` vs `anu_legacy` vs `secrets_fallback` vs `browser_csprng`), `±1.96σ` null envelopes on the chart, pre-registered `planned_trials`, and a `dead_salmon` control mode.
- **Refuse:** Any framing that treats RNG directional lean as decision authority over Iowa lead-azide detonation or substitutes uncalibrated z-score peeks for physical safety/governance accountability.

---

## 5. Follow-Up Field Check: GCP 2.0 / HeartMath Institute vs. Sovereign Local RNG

Evaluating the GCP 2.0 comparison (`11 Dec 2025` `~9.9σ` at `~17:49–21:13 UTC`; `1 Mar 2026` `~8.6σ` at `~04:13–05:40 UTC`; plus retrospective "calibration glitch" reclassifications):

1. **Post-hoc event matching (Hostile Class A / B & Thesis 53):** `1 Mar 2026` (`~8.6σ`) maps to Khamenei's confirmed death, whereas the larger `11 Dec 2025` (`~9.9σ`) peak lacks a singular shock and gets matched to six concurrent news items, while inconvenient excursions are labeled calibration glitches. Without pre-registered event windows, Wikipedia's Current Events feed guarantees plausible hits on most days.
2. **Third-party mediation (`AUDIT_PROTOCOL.md` Possessions 1–4):** An institute-hosted network owns calibration filters and exclusion rules behind a closed pipeline. Running locally with raw SHA-256 bitstream receipts + `dead_salmon` controls keeps data in the operator's hands while preventing the same post-hoc storytelling locally.
