# Trader-Swing Phase 8.2 — Paper Broker Simulation, Cash Reservation, Concurrency & Accounting Integrity Report

**Project:** Trader-Swing Automated Paper Execution Readiness  
**Phase:** 8.2 (Integrated Paper Broker Simulation, Cash Reservation, Concurrency & Accounting Integrity)  
**Execution Date:** 2026-11-20  
**Execution Mode:** Accelerated Implementation Mode (Complete Subsystem Delivered)  
**Environment Tag:** `SIMULATION_ONLY` (Strict Local Offline Simulation — Zero Broker Network Calls — Zero Broker Credentials)  
**Overall Status:** **PASSED & VERIFIED** (17/17 Dedicated Unit & Security Tests Pass — 130/130 Core Regression Tests Pass)  
**Recommendation:** **CONDITIONAL GO (READY FOR FUTURE AUTOMATED STAGING PHASES — STRICT AUTHORIZATION REQUIRED BEFORE PROCEEDING)**  

---

> [!CAUTION]
> **CRITICAL PROFITABILITY & STATISTICAL EDGE DISCLOSURE:**  
> The successful implementation and verification of the Phase 8.2 paper broker simulation layer establishes operational reliability, concurrency safety, deterministic accounting integrity, and risk containment. However, **technical simulation readiness does NOT constitute evidence of trading profitability or statistical edge in live equity markets.** The Trader-Swing strategy has **NOT demonstrated a statistically significant trading edge** in out-of-sample forward testing. The historical backtests (2025–2026 Sharpe ratio ~0.94, maximum drawdown ~11.8%) and forward observation represent exploratory research baselines only. Operational correctness prevents catastrophic financial loss due to software bugs or over-allocation, but provides zero guarantee of financial returns.

---

## 1. Executive Summary

Phase 8.2 delivers the complete isolated paper broker simulation subsystem for Trader-Swing in Accelerated Implementation Mode. This phase resolves the final execution-readiness gaps identified in the Phase 7.3.7 Stage C Audit:

1. **GAP-01 (Duplicate-Order Prevention & Fill Replay Protection):** Resolved via deterministic order and fill identification algorithms (`compute_order_id`, `compute_fill_identity`), persistent idempotency registries, and strict fill replay rejection (`DuplicateFillError`).
2. **GAP-02 (Pre-Submission Cash Reservation & Concurrent Over-Allocation):** Resolved via the dedicated `CashReservationEngine` which locks required buying power `(LimitPrice * Shares) + EstimatedFee` before order submission and guarantees that available cash never breaches the 20% minimum cash reserve invariant against concurrent or sequential pending orders.
3. **GAP-04 (Complete Order Lifecycle & Simulated Broker Integration):** Resolved via `PaperBrokerSimulator` (`ENVIRONMENT = "SIMULATION_ONLY"`), integrating Phase 8.0 order lifecycle state machines with Phase 8.1 3-gate human authorization, bracket order emulation, partial fill tracking, and cross-process file locking.

### Key Metrics:
- **Environment Safety:** 100% offline local simulation. Prohibits any live brokerage endpoints, Alpaca credentials, or automated network requests.
- **Dedicated Test Suite:** 17/17 tests passing in [test_swing_paper_broker_simulation.py](file:///C:/Users/mmano/OneDrive/Desktop/trading/tests/test_swing_paper_broker_simulation.py).
- **Core Regression Suites:** 130/130 tests passing across order lifecycle, human authorization, risk, journal, accounting, and paper trading suites.
- **Protected State Integrity:** Zero modifications to root `paper_journal.json`, root `portfolio.json`, root `broker.json`, or external `C:\Users\mmano\Documents\TradingPaperState`. All 4 protected baseline SHA-256 hashes remain 100% identical.

---

## 2. Component Inventory & Architecture

Phase 8.2 introduces four core modules within [.agents/skills/trader-swing/](file:///C:/Users/mmano/OneDrive/Desktop/trading/.agents/skills/trader-swing/):

```
.agents/skills/trader-swing/
├── paper_broker_simulator.py  # Local simulated broker adapter (ENVIRONMENT = "SIMULATION_ONLY")
├── cash_reservation.py        # Pre-submission cash reservation engine & available cash tracking
├── paper_lock.py              # Cross-process OS file mutex (.broker.lock) & thread locking
└── paper_storage.py           # Updated with trading_paper_reservations.json persistence & recovery
```

```mermaid
flowchart TD
    subgraph OrderLifecycle["Phase 8.0 & 8.1 Gates"]
        A["Order Proposed (PROPOSED)"] --> B["Gate 1: Risk Validation (RISK_VALIDATED)"]
        B --> C["Gate 2: Explicit Human Approval (HUMAN_APPROVED)"]
        C --> D["Gate 3: Platform Permission Token (AUTHORIZED)"]
        D --> E["PaperOrder State: APPROVED"]
    end

    subgraph Phase82["Phase 8.2 Simulated Broker & Accounting"]
        E --> F["Submit Order Request"]
        F --> G["Acquire File Mutex (.broker.lock)"]
        G --> H["Verify Authorization Record & HMAC Token"]
        H --> I["CashReservationEngine: Reserve Cash"]
        I -->|Available Cash < 20% Reserve| J["REJECT SUBMISSION (InsufficientAvailableCashError)"]
        I -->|Invariant Satisfied| K["State: SUBMITTED (trading_paper_reservations.json)"]
        K --> L{"Execution Event"}
        L -->|Simulated Fill| M["Deterministic Fill ID Check (Replay Protection)"]
        M --> N["Convert Proportional Reservation"]
        N --> O["AccountingLedger.buy() -> Double-Entry Lot"]
        O --> P["State: PARTIALLY_FILLED or FILLED"]
        L -->|Cancel / Expire / Fail| Q["State: CANCELLED / EXPIRED / FAILED"]
        Q --> R["Immediate Release of Reserved Cash"]
        P --> S["Commit Manifest Protocol (commit_manifest.json)"]
        R --> S
        S --> T["Release File Mutex"]
    end
```

### Component Breakdown:

1. **`PaperBrokerSimulator` ([paper_broker_simulator.py](file:///C:/Users/mmano/OneDrive/Desktop/trading/.agents/skills/trader-swing/paper_broker_simulator.py)):**
   - Explicitly tagged with `ENVIRONMENT = "SIMULATION_ONLY"`.
   - Places a permanent `SIMULATION_MARKER.json` in the state directory.
   - Refuses order submission unless the order state is `APPROVED` and the authorization record is in state `AUTHORIZED` with a valid, unexpired HMAC-SHA256 approval token.
   - Executes deterministic fills, supports partial executions, logs structured audit events to `audit_events.jsonl`, and emulates bracket order exits.

2. **`CashReservationEngine` ([cash_reservation.py](file:///C:/Users/mmano/OneDrive/Desktop/trading/.agents/skills/trader-swing/cash_reservation.py)):**
   - Manages pre-submission cash locks using the typed `CashReservationRecord` model.
   - Computes required cash reservation: $\text{Required Amount} = (\text{LimitPrice} \times \text{Shares}) + \text{EstimatedFee}$.
   - Enforces the available cash invariant: $\text{Available Cash} = \text{Ledger Cash} - \sum \text{Active Reservations}$.
   - Prevents submission if post-reservation available cash violates the 20% minimum cash reserve requirement.
   - Implements proportional reservation conversion for partial fills and immediate release upon order cancellation, expiration, or rejection.
   - Houses `reconcile_reservations()` to detect and resolve orphaned reservations during crash recovery.

3. **`PaperFileLock` ([paper_lock.py](file:///C:/Users/mmano/OneDrive/Desktop/trading/.agents/skills/trader-swing/paper_lock.py)):**
   - Provides OS-level cross-process mutual exclusion (`msvcrt` on Windows, `fcntl` on POSIX).
   - Combines OS locking with process-level `threading.RLock` to serialize concurrent threads and processes accessing `.broker.lock`.
   - Enforces fail-closed timeout behavior (`PaperLockTimeoutError`) to guarantee deadlock prevention.

4. **Transactional Storage Integration ([paper_storage.py](file:///C:/Users/mmano/OneDrive/Desktop/trading/.agents/skills/trader-swing/paper_storage.py)):**
   - Manages `trading_paper_reservations.json` alongside journal, portfolio, orders, and authorizations.
   - Incorporates staged reservations into the two-phase commit manifest protocol (`commit_manifest.json` status `COMMITTED`).
   - Automatically recovers and applies committed reservation updates following simulated process crashes.

---

## 3. Order Lifecycle & State Machine Transitions

Phase 8.2 extends the order state machine across the complete simulation lifecycle:

| From State | Allowed Target States | Triggering Event / Action | Invariant / Validation Rules |
| :--- | :--- | :--- | :--- |
| **`PROPOSED`** | `PENDING_APPROVAL`, `REJECTED`, `CANCELLED` | Deterministic Risk Validation (Gate 1) | Rejects if risk limits exceeded. |
| **`PENDING_APPROVAL`** | `APPROVED`, `REJECTED`, `CANCELLED`, `EXPIRED` | Human Confirmation (Gate 2) & Token Seal (Gate 3) | Affirmative `"YES"` required; HMAC token issued. |
| **`APPROVED`** | `SUBMITTED`, `CANCELLED`, `EXPIRED` | Simulated Broker `submit_order()` | Must possess valid `AUTHORIZED` record; Cash reserved. |
| **`SUBMITTED`** | `PARTIALLY_FILLED`, `FILLED`, `CANCELLED`, `EXPIRED`, `FAILED` | Simulated Execution Fill, Cancellation, Expiration | Deterministic fill ID; proportional cash conversion. |
| **`PARTIALLY_FILLED`** | `PARTIALLY_FILLED`, `FILLED`, `CANCELLED`, `EXPIRED`, `FAILED` | Subsequent Simulated Fill, Cancellation | Decrements `shares_remaining`; converts reservation. |
| **`FILLED`** | *(Terminal — Irreversible)* | Final fill (`shares_remaining == 0`) | Reservation status `CONVERTED_FULL`; Tax lot active. |
| **`CANCELLED`** | *(Terminal — Irreversible)* | User or manual cancellation | Unlocks and releases remaining reserved cash. |
| **`EXPIRED`** | *(Terminal — Irreversible)* | Session close expiration | Unlocks and releases remaining reserved cash. |
| **`FAILED`** | *(Terminal — Irreversible)* | Broker rejection or exchange fault | Unlocks and releases remaining reserved cash. |

---

## 4. Pre-Submission Cash Reservation Engine (GAP-02)

### A. The Double-Spending Vulnerability in Unreserved Systems
In basic paper-trading architectures, cash is only deducted when a trade execution fills (`AccountingLedger.buy()`). If multiple orders are submitted concurrently or sequentially before fills arrive, each order sees the full unreserved ledger cash balance, allowing orders to collectively exceed available capital and violate the 20% minimum cash reserve requirement.

### B. Mathematical Invariants Enforced by Phase 8.2
1. **Required Reservation per Buy Order:**
   $$\text{Reserved Amount} = (\text{LimitPrice} \times \text{Shares}) + \text{EstimatedFee}$$
2. **Effective Available Cash:**
   $$\text{Available Cash} = \text{Ledger Cash} - \sum_{r \in \text{ActiveReservations}} r.\text{total\_reserved\_usd}$$
3. **The 20% Minimum Equity Reserve Guard:**
   $$\text{Available Cash} - \text{Reserved Amount} \ge (\text{Ledger Total Equity} \times 0.20)$$

If the guard condition is violated by even one cent, `InsufficientAvailableCashError` is immediately raised, order submission is rejected, and zero reservations or order transitions occur.

### C. Reservation Lifecycle Transitions
- **`ACTIVE`:** Created upon successful order submission. Cash is locked.
- **`CONVERTED_PARTIAL`:** Upon a partial fill of $N$ shares, proportional reserved principal $(N \times \text{Price})$ is converted into an actual ledger cash deduction. Remaining reserved shares continue locking unexecuted capital.
- **`CONVERTED_FULL`:** Upon final execution fill, all remaining reserved funds are cleared and converted into ledger equity.
- **`RELEASED`:** If the order is cancelled, expired, or rejected, all remaining reserved funds are instantly unlocked and restored to available cash.

---

## 5. Concurrency, Locking & Replay Protection (GAP-01)

### A. Cross-Process Mutex Locking (`PaperFileLock`)
All order submissions, fill executions, cancellations, and reconciliations are wrapped in `with self.lock:`.
- **Platform Independence:** Implements Windows `msvcrt.locking(fileno, LK_NBLCK, 1)` and POSIX `fcntl.flock(fileno, LOCK_EX | LOCK_NB)`.
- **Thread Re-entrancy:** An internal `threading.RLock` coordinates threads within the same process.
- **Fail-Closed Timeout:** Contending callers poll up to `lock_timeout` (default: 10.0s). If the lock cannot be acquired within the timeout, `PaperLockTimeoutError` is raised, preventing process hangs or race conditions.

### B. Deterministic Fill Identity & Replay Protection
Fills generated by the simulated broker are assigned a deterministic cryptographic identity:
$$\text{Fill ID} = \text{FILL-SIM-}\{\text{OrderID}[-8:]\}\text{-}\{\text{FillIndex}:02d\}\text{-}\{\text{SHA256}(\text{OrderID}, \text{Shares}, \text{Price}, \text{Timestamp})[:8]\}$$

- **Replay Protection Guard:** If `process_simulated_fill()` is called with a fill ID or parameters that match an already applied fill, `DuplicateFillError` is raised.
- **Quantity Guard:** A fill requesting more shares than `order.shares_remaining` is rejected with `InvalidExecutionStateError`.

---

## 6. Bracket Order Simulation & Accounting Integrity

The simulated broker provides full bracket order emulation for long-only swing trading:

1. **Entry Bracket Structure:** When submitting a bracket order, the order records the entry limit price, protective stop-loss, and target take-profit.
2. **Lot Tracking:** Upon execution, tax lots are created in `AccountingLedger` and synchronized with portfolio metadata in `trading_paper_portfolio.json`.
3. **Simulated Exit Execution:** `simulate_bracket_exit()` executes exit orders against open lots:
   - Evaluates exit reason (`STOP_LOSS` or `TAKE_PROFIT`).
   - Sells shares on `AccountingLedger` with FIFO lot allocation and proportional fee amortization.
   - Records the exit in `trading_paper_orders.json` and updates portfolio metadata.
   - Logs `SIM_BRACKET_EXIT_EXECUTED` to `audit_events.jsonl`.
   - Preserves the fundamental double-entry equity invariant:
     $$\text{Cash} + \text{Dividends Receivable} + \text{Gross Market Value} \equiv \text{Initial Capital} + \text{Realized P&L} + \text{Dividends} + \text{Unrealized P&L}$$

---

## 7. Crash Recovery, Reconciliation & Orphan Detection

The simulated broker incorporates automated crash recovery and reconciliation:

1. **Commit Manifest Recovery:** On startup or reconciliation, `PaperStorageManager.recover_interrupted_transaction()` scans for `commit_manifest.json`. If a prior process crashed after setting status `COMMITTED`, all staged state files (including reservations) are atomically applied. If it crashed prior to commit, uncommitted staging directories are purged.
2. **Orphan Reservation Reconciliation:** `CashReservationEngine.reconcile_reservations()` audits all active reservations against the orders registry:
   - If an order was terminated (`CANCELLED`, `EXPIRED`, `FAILED`) during an unexpected crash while its reservation remained `ACTIVE`, the reservation is automatically reconciled to `RELEASED`.
   - If an order was fully filled (`FILLED`), the reservation is reconciled to `CONVERTED_FULL`.
   - Reconciles total reserved cash with ledger cash to ensure available cash is 100% accurate.

---

## 8. Verification & Test Matrix

### A. Dedicated Phase 8.2 Test Suite
Executed via: `python -m unittest tests/test_swing_paper_broker_simulation.py`  
Result: **17 passed, 0 failed, 0 errors (6.039s)**

| Test Case | Description | Result |
| :--- | :--- | :---: |
| `test_01_environment_tag_and_simulation_marker` | Verifies `SIMULATION_ONLY` tag and persistent marker file | **PASS** |
| `test_02_rejection_of_live_environment` | Verifies simulator fails closed if environment tag is altered | **PASS** |
| `test_03_submission_fails_without_authorization` | Unapproved/unauthorized orders rejected at submission | **PASS** |
| `test_04_successful_order_submission_with_cash_reservation` | Approved order transitions to `SUBMITTED` and reserves cash | **PASS** |
| `test_05_concurrent_overallocation_prevention_against_20_percent_reserve` | Prevents multiple orders from breaching the 20% cash reserve | **PASS** |
| `test_06_cancellation_releases_reservation` | Order cancellation immediately unlocks reserved cash | **PASS** |
| `test_07_expiration_releases_reservation` | Order expiration immediately unlocks reserved cash | **PASS** |
| `test_08_rejection_failure_releases_reservation` | Broker rejection marks order `FAILED` and releases reservation | **PASS** |
| `test_09_full_execution_fill_flow` | Full fill executes buy, converts reservation, and updates to `FILLED` | **PASS** |
| `test_10_partial_fills_and_final_completion` | Sequential partial fills decrement remaining shares accurately | **PASS** |
| `test_11_fill_replay_protection` | Rejects duplicate fill identity or replaying identical fill | **PASS** |
| `test_12_fill_rejected_on_terminal_order` | Prevents fill execution against cancelled or terminal orders | **PASS** |
| `test_13_file_lock_mutual_exclusion_and_timeout` | Verifies file mutex serializes access and times out cleanly | **PASS** |
| `test_14_orphan_reservation_reconciliation` | Detects and cleans up orphaned reservations during reconciliation | **PASS** |
| `test_15_bracket_order_stop_loss_exit` | Emulates stop-loss exit, realizes loss, and preserves equity invariant | **PASS** |
| `test_16_bracket_order_take_profit_exit` | Emulates take-profit exit, realizes gain, and preserves equity invariant | **PASS** |
| `test_17_protected_files_and_external_paper_state_unmodified` | Confirms root state and external paper state remain untouched | **PASS** |

### B. Core Regression Suite Execution
Executed via: `python -m unittest tests/test_swing_order_lifecycle.py tests/test_swing_human_authorization.py tests/test_swing_paper_broker_simulation.py tests/test_swing_paper_61.py tests/test_swing_risk.py tests/test_swing_accounting.py tests/test_swing_journal.py`  
Result: **130 passed, 0 failed, 0 errors (18.920s)**

---

## 9. Protected State & File Integrity Verification

To guarantee that Phase 8.2 implementation remained strictly isolated, all protected files and external directories were verified before and after execution:

| Target File / Directory | Baseline SHA-256 / Timestamp | Post-Verification Status | Result |
| :--- | :--- | :--- | :---: |
| `.planning/data/observations/observation_log.jsonl` | `A8F5603652062A8D1E25DEC309CA046293D0733E02DAD5FA177C3F1727E75E34` | Exact match | **VERIFIED** |
| `.planning/data/current_daily/CURRENT_DAILY_MANIFEST.json` | `C31A6AE4DE4F52FB55288D766C980D4BFB26D039281443E5A7005CBFABF35B51` | Exact match | **VERIFIED** |
| `.planning/data/current_daily/SPY_daily.csv` | `03F55CEA4EDCBB985723688E60224E812E67A2C0DCB32FE99728671F79835626` | Exact match | **VERIFIED** |
| `paper_journal.json` (Root) | `04116B7BDA4326880FF4D56899B170CA7AA9C69719E804148FCC5B684735656B` | Exact match | **VERIFIED** |
| `C:\Users\mmano\Documents\TradingPaperState` | Last write: `07-11-2026 01:28:29` | Unmodified | **VERIFIED** |

---

## 10. Audit Gaps Resolution Status

With Phase 8.2 complete, all readiness gaps identified in the Stage C Audit are fully resolved:

- [x] **GAP-01: Order Lifecycle Models & Duplicate Prevention** &rarr; Resolved in Phase 8.0 & Phase 8.2 (`order_models.py`, `order_idempotency.py`, `paper_broker_simulator.py`).
- [x] **GAP-02: Cash Reservation Engine & Concurrency** &rarr; Resolved in Phase 8.2 (`cash_reservation.py`, `paper_lock.py`).
- [x] **GAP-03: Multi-Gate Human Authorization Subsystem** &rarr; Resolved in Phase 8.1 (`authorization_models.py`, `authorization_service.py`, `authorization_token.py`).
- [x] **GAP-04: Isolated Broker Simulator & Bracket Order Emulation** &rarr; Resolved in Phase 8.2 (`paper_broker_simulator.py`).

---

## 11. Final Assessment & Stop Condition

### Assessment:
**CONDITIONAL GO**. The foundational simulation and accounting infrastructure is complete, thoroughly tested, and mathematically verified. All frozen strategy parameters ($5,000 equity, 0.50% max risk/trade, 1.25x gap-risk buffer, 2.0% aggregate buffered risk ceiling, max 4 positions, 25% single-ticker allocation cap, 80% max invested capital, 20% minimum cash reserve) remain strictly enforced.

### Stop Condition:
In accordance with user instructions, **Phase 8.2 execution is now complete.** No further phases (e.g. Phase 8.3 or automated paper execution) will be initiated without explicit user authorization.
