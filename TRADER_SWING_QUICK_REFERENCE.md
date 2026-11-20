# Trader-Swing Quick Reference Cheat Sheet
## Daily Operations & Verification Commands (Phase 9 & 9.1)

```
========================================================================================================
ENVIRONMENT : FORWARD_EVALUATION_ONLY (READ-ONLY) &bull; REFERENCE CAPITAL: $5,000.00 USD
WORKING DIR : <workspace-root> (e.g. C:\trading) &bull; SHELL: Windows PowerShell
SAFETY RULE : ZERO BROKER APIS &bull; ZERO REAL CAPITAL &bull; NEVER OPEN ROOT paper_journal.json
========================================================================================================
```

---

## 1. Environment & Initialization

```powershell
# 1. Always navigate to the workspace root:
Set-Location -Path "<workspace-root>"

# 2. Activate virtual environment (if configured):
.\.venv\Scripts\Activate.ps1

# 3. Verify Python version (requires Python 3.11+):
python --version
```

---

## 2. Core CLI Commands

### A. Run Daily Forward Evaluation (After 4:15 PM Eastern)
```powershell
# 1. Acquire completed daily session data (after 4:15 PM Eastern):
python scripts/acquire_current_daily.py

# 2. Standard forward evaluation run for today's completed session:
python forward_evaluation_cli.py --mode DAILY

# 3. Run controlled deterministic fixture test (Offline diagnostic dry run — NOT performance evidence):
python forward_evaluation_cli.py --mode FIXTURE

# 4. Run research observation workflow on current daily partition (Read-only display):
python .agents/skills/trader-swing/observation_workflow.py --current --no-record
```

### B. Consolidated Performance Report (Weekly / Monthly Review)
```powershell
# Generates win rate, Wilson 95% CI, net P&L, drawdown, and N>=30 sample status:
python forward_evaluation_cli.py --report
```

### C. Launch Research Terminal (Interactive Dashboard)
```powershell
# Starts local Streamlit dashboard (Navigate to Screen 5: 🔭 Forward Paper Evaluation):
streamlit run dashboard/app.py

# If port 8501 is occupied, launch on alternative port:
streamlit run dashboard/app.py --server.port 8502
```

### D. Data Quality & Partition Verification
```powershell
# Verify current daily partition health & manifest integrity:
# Note: 753 is the historical 2021–2023 partition threshold, while current_daily uses 276 sessions.
python scripts/verify_historical_partitions.py --partition .planning/data/current_daily --expected-sessions 276

# Check observation audit trail:
Get-Content -Path ".planning/data/forward_evaluation/audit/audit_events.jsonl" -Tail 20
```

---

## 3. Daily 8-Step Workflow Checklist

| Step | Action | Exact Command / Check | Expected Outcome |
| :---: | :--- | :--- | :--- |
| **1** | **Check Clock** | Wait until $\ge$ 4:15 PM Eastern | No forming candles (`CandleStatus.FORMING`) |
| **2** | **Acquire Daily Data** | `python scripts/acquire_current_daily.py` | `CURRENT_DAILY_MANIFEST.json` updated with status `PASSED` |
| **3** | **Run Evaluation** | `python forward_evaluation_cli.py --mode DAILY` | Genuine forward session evaluated, summary printed |
| **4** | **Inspect Regime** | Review macro regime policy | If `STRONG_BEARISH`: 100% longs blocked |
| **5** | **Check Sizing** | Review sized shares and stop/target | Risk $\le \$25.00$, max 4 positions |
| **6** | **Open Dashboard** | `streamlit run dashboard/app.py` | Screen 5 renders without error |
| **7** | **Verify Storage** | Check `.planning/data/forward_evaluation/` | `observations/` and `summaries/` updated |
| **8** | **Confirm Safety** | Inspect root directory | `paper_journal.json` strictly untouched |

---

## 4. Weekly & Monthly Review Checklist

### Weekly Review (Every Friday Evening):
- [ ] Run `python forward_evaluation_cli.py --report`
- [ ] Confirm all 5 exchange sessions were observed.
- [ ] Inspect candidate attrition funnel (screened $\to$ passed $\to$ sized).
- [ ] Review any open vs. closed hypothetical positions.
- [ ] Compare hypothetical equity trajectory against SPY benchmark.
- [ ] Check `audit_events.jsonl` for upstream data exclusions.

### Monthly Strategic Audit (Every 4 Weeks):
- [ ] Run `python forward_evaluation_cli.py --report`
- [ ] **Sample Size Gate**: Is closed trades $N \ge 30$?
  - If $N < 30$: Record verdict as **INCONCLUSIVE (SAMPLE INSUFFICIENT)**.
  - If $N \ge 30$: Inspect Wilson score 95% confidence interval lower bound.
- [ ] Review performance across each macro regime (`BULLISH` vs. `BEARISH`).
- [ ] Audit fee amortization ($1.00 entry + $1.00 exit per trade).
- [ ] Confirm zero broker orders, zero real money, zero strategy code mutations.

---

## 5. Fast Failure Recovery Matrix

| Error Message / Symptom | Immediate Diagnostic | Fast Safe Recovery |
| :--- | :--- | :--- |
| `FATAL RETROSPECTIVE REPLAY` | Historical session $\le$ 2026-11-16. | Forward evaluation requires fresh sessions after 2026-11-16. |
| `FATAL SESSION DATE MISMATCH` | Local current_daily data stale or missing. | Run `python scripts/acquire_current_daily.py` after 4:15 PM ET. |
| `FATAL INCOMPLETE SESSION` | Run attempted during regular market hours. | Wait until 4:15 PM ET for regular session close. |
| `FATAL SAFETY VIOLATION: Prohibited argument '--live'` | Live/broker flag detected. | Remove prohibited argument. Only offline modes (`--mode DAILY` or `FIXTURE`) permitted. |
| `DuplicateObservationError` | Exact session/ticker already logged. | Pass `force_idempotent_overwrite=True` or use a new session date. |
| `CandleStatus.FORMING` error | Run attempted during market hours. | Wait until 4:15 PM ET for completed bars. |
| `GEOMETRY_INVALID` / `High < Low` | Upstream data provider corruption. | Exclude ticker from universe; never synthesize false candle prices. |
| `Streamlit: Port 8501 is already in use` | Stale background server process. | Run on alternative port: `streamlit run dashboard/app.py --server.port 8502`. |
| `Prohibited storage path: 'paper_journal.json'` | Path boundary guard triggered. | Set `--storage-dir` to an isolated research folder under `.planning/data/`. |

---

## 6. Clean Shutdown

```powershell
# 1. Stop Dashboard:
Press [Ctrl + C] in the running PowerShell terminal

# 2. Clear Session Keys (if any were set):
Remove-Item env:APCA_API_KEY_ID -ErrorAction SilentlyContinue
Remove-Item env:APCA_API_SECRET_KEY -ErrorAction SilentlyContinue
```
