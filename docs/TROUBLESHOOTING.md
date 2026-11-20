# Trader-Swing Troubleshooting Guide

This guide addresses common setup, environment, and operational questions when running Trader-Swing on Windows.

---

## 1. PowerShell Script Execution Disabled

### Symptom:
```
File C:\...\setup.ps1 cannot be loaded because running scripts is disabled on this system.
+ CategoryInfo          : SecurityError: (:) [], PSSecurityException
+ FullyQualifiedErrorId : UnauthorizedAccess
```

### Cause:
Windows PowerShell restricts script execution by default (`Restricted` execution policy).

### Solution:
Bypass the restriction for the current terminal session:
```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```
Or execute the script directly with bypass:
```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
powershell -ExecutionPolicy Bypass -File .\run.ps1 dashboard
```

---

## 2. Missing Virtual Environment (`.venv`)

### Symptom:
```
[ERROR] Virtual environment not found at .venv
Please run the setup script first:
    .\setup.ps1
```

### Cause:
The `.venv` folder has not been created or the repository was freshly cloned.

### Solution:
Run the automated setup script to build the virtual environment:
```powershell
.\setup.ps1
```

---

## 3. SecretKeyUnavailableError (`TRADER_AUTH_SECRET`)

### Symptom:
```
SecretKeyUnavailableError: TRADER_AUTH_SECRET uses an insecure default/placeholder secret.
-- OR --
SecretKeyUnavailableError: TRADER_AUTH_SECRET is too short (< 16 bytes).
```

### Cause:
Trader-Swing enforces fail-closed authorization. If `TRADER_AUTH_SECRET` is unset, too short, or set to an insecure placeholder (such as `changeme` or `your_secret_here`), order signing and simulation will fail closed.

### Solution:
Generate a secure random 32-character hexadecimal secret:
```powershell
.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_hex(16))"
```
Paste this value into your `.env` file:
```env
TRADER_AUTH_SECRET=your_generated_32_character_hex_value
```

---

## 4. Port 8501 Already in Use (Dashboard)

### Symptom:
```
AxiosError: Request failed with status code 403 / Port 8501 is already in use
```

### Cause:
Another Streamlit instance, background process, or local service is bound to port 8501.

### Solution:
Run the dashboard on an alternate port:
```powershell
.\run.ps1 dashboard --server.port 8502
```
Or identify and close the existing process in PowerShell:
```powershell
Get-NetTCPConnection -LocalPort 8501 -ErrorAction SilentlyContinue | Select-Object OwningProcess
Stop-Process -Id <OwningProcessId>
```

---

## 5. Python Version Incompatibility

### Symptom:
```
ERROR: Could not find a version that satisfies the requirement pandas==3.1.1
```

### Cause:
Trader-Swing requires Python >= 3.11, <= 3.13. Older Python versions (such as 3.8 or 3.9) do not support the modern pinned versions of pandas and numpy.

### Solution:
1. Install Python 3.12 or 3.13 from [python.org](https://www.python.org/downloads/).
2. When installing on Windows, check the box: **"Add Python to PATH"**.
3. Verify the installed version:
   ```powershell
   py --version
   # or
   python --version
   ```
4. Re-run `.\setup.ps1`.

---

## 6. Dashboard Does Not Open in Browser Automatically

### Symptom:
The terminal displays `You can now view your Streamlit app in your browser`, but no browser window opens.

### Solution:
Open your web browser (Chrome, Edge, or Firefox) and manually navigate to:
```
http://localhost:8501
```

---

## 7. SEC EDGAR 403 Forbidden Errors (Analyst / News)

### Symptom:
SEC requests return HTTP 403 Forbidden or empty filing data.

### Cause:
The SEC EDGAR API requires a descriptive `User-Agent` header containing the user's name and email address. Generic headers are rejected by SEC firewalls.

### Solution:
Configure `SEC_USER_AGENT` in your `.env` file:
```env
SEC_USER_AGENT=ResearchUser Admin<user@example.com>
```
