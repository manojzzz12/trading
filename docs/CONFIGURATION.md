# Trader-Swing Configuration Guide

This guide details all configuration options, environment variables, security keys, and directory paths for Trader-Swing.

---

## 1. Environment Variables Reference

Trader-Swing reads configuration from environment variables or a local `.env` file in the project root.

| Variable | Required? | Default | Description |
| :--- | :---: | :--- | :--- |
| `TRADER_AUTH_SECRET` | **Yes (for orders)** | *None* | Cryptographically secure secret key (minimum 16 bytes) used to sign HMAC-SHA256 order approval tokens. |
| `TRADING_PAPER_STATE_DIR` | No | Platform default | Directory where paper trading state (`trading_paper_journal.json`) is stored. |
| `TRADING_PAPER_SIMULATION_DIR` | No | Platform default | Directory where automated pilot simulation state is isolated. |
| `ALPACA_API_BASE_URL` | No | `https://paper-api.alpaca.markets` | Alpaca API endpoint. Always defaults to Paper Trading. |
| `ALPACA_API_KEY_ID` | No | *None* | Optional Alpaca API key for market data streaming or paper trading. |
| `ALPACA_API_SECRET_KEY` | No | *None* | Optional Alpaca API secret key. |
| `SEC_USER_AGENT` | No | *None* | User-agent header for SEC EDGAR requests (e.g., `MyApp Admin<user@example.com>`). |

---

## 2. Setting Up `TRADER_AUTH_SECRET`

Trader-Swing employs a fail-closed cryptographic gate: automated paper orders cannot be submitted or simulated without a valid HMAC signature.

### Generating a Secure Secret
Run this in PowerShell:
```powershell
.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_hex(16))"
```

### Adding to `.env`
Add the output to your local `.env` file:
```env
TRADER_AUTH_SECRET=4f8b2c9e7a1d3f5b8c6e2a4d9f1b7e3a
```

### Fail-Closed Validation Rules
The system will immediately reject configuration with `SecretKeyUnavailableError` if:
- `TRADER_AUTH_SECRET` is unset, empty, or whitespace only.
- The secret is shorter than 16 bytes.
- The secret matches known insecure placeholders (e.g., `changeme`, `your_secret_here`, `default_secret`, `password12345678`).

---

## 3. Storage & State Directory Resolution

Trader-Swing ensures that personal paper journals and simulation runs remain isolated per Windows user profile and never collide with repository source control.

### Implemented Directory Resolution:
- **Paper State Directory**:
  1. `TRADING_PAPER_STATE_DIR` environment variable (if configured).
  2. Default: `Path.home() / "Documents" / "TradingPaperState"` (`C:\Users\<username>\Documents\TradingPaperState` on Windows, `~/Documents/TradingPaperState` on Linux/macOS).
- **Simulation State Directory**:
  1. `TRADING_PAPER_SIMULATION_DIR` environment variable (if configured).
  2. Default: `Path.home() / "Documents" / "TradingPaperSimulation"` (`C:\Users\<username>\Documents\TradingPaperSimulation` on Windows, `~/Documents/TradingPaperSimulation` on Linux/macOS).

### Path Safety Guards:
The storage manager enforces path safety:
- Storage paths targeting repository-root files (e.g. `paper_journal.json`, `portfolio.json`, `broker.json`) are blocked (`PaperStorageSafetyError`).
- Storage paths targeting repo-internal observations directories (`.planning/data/observations`) are blocked.

---

## 4. Risk & Trading Rules Configuration

Risk rules for the modular skills are configured in JSON templates (e.g., `trader-budget/config.example.json`):

```json
{
  "paper": true,
  "max_loss_per_trade_usd": 50.00,
  "max_daily_loss_usd": 150.00,
  "max_trades_per_day": 3,
  "min_reward_risk": 1.5,
  "require_stop": true,
  "no_entries_after": "15:30",
  "flat_by": "15:55",
  "allow_short": false,
  "allow_margin": false,
  "allow_options": false
}
```

### Key Parameter Guarantees:
- `"paper": true`: Hardcoded safety default. Real-money live brokerage orders are disabled.
- `"require_stop": true`: Every position must define an explicit stop-loss before entry.
- `"allow_short": false`: Long-only equity swing trading. Synthetic shorting is blocked.
- `"allow_margin": false`: Leverage and margin borrowing are disabled.
