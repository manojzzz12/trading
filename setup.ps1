<#
.SYNOPSIS
    Automated environment setup script for Trader-Swing on Windows.

.DESCRIPTION
    Verifies Python installation, creates a virtual environment (.venv),
    installs project dependencies from requirements.txt, and sets up
    a local .env configuration template.

.NOTES
    Run from PowerShell in the project root directory:
        powershell -ExecutionPolicy Bypass -File .\setup.ps1
#>

[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "         Trader-Swing Windows Setup & Installation         " -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host ""

# 1. Verify Python Installation
Write-Host "[1/4] Checking Python installation..." -ForegroundColor Yellow
$PythonCmd = $null
if (Get-Command "python" -ErrorAction SilentlyContinue) {
    $PythonCmd = "python"
} elseif (Get-Command "py" -ErrorAction SilentlyContinue) {
    $PythonCmd = "py"
} else {
    Write-Error "Python was not found in your PATH. Please install Python 3.11, 3.12, or 3.13 from https://www.python.org/downloads/ and check 'Add Python to PATH'."
    exit 1
}

$PyVersion = & $PythonCmd --version 2>&1
Write-Host "      Found: $PyVersion" -ForegroundColor Green

# 2. Virtual Environment Setup
Write-Host "[2/4] Setting up Python virtual environment (.venv)..." -ForegroundColor Yellow
$VenvDir = Join-Path $PSScriptRoot ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$VenvPip = Join-Path $VenvDir "Scripts\pip.exe"

if (-not (Test-Path $VenvPython)) {
    Write-Host "      Creating fresh virtual environment at .venv..." -ForegroundColor Gray
    & $PythonCmd -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Failed to create virtual environment."
        exit $LASTEXITCODE
    }
    Write-Host "      Virtual environment created successfully." -ForegroundColor Green
} else {
    Write-Host "      Existing virtual environment detected at .venv." -ForegroundColor Green
}

# 3. Dependency Installation
Write-Host "[3/4] Installing dependencies from requirements.txt..." -ForegroundColor Yellow
$ReqFile = Join-Path $PSScriptRoot "requirements.txt"
if (Test-Path $ReqFile) {
    & $VenvPython -m pip install --quiet --disable-pip-version-check -r $ReqFile
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Dependency installation encountered an issue. Ensure you have an active internet connection or pre-cached wheels."
    } else {
        Write-Host "      Dependencies installed and verified." -ForegroundColor Green
    }
} else {
    Write-Warning "requirements.txt not found in project root."
}

# 4. Configuration Template (.env) Check
Write-Host "[4/4] Verifying local configuration (.env)..." -ForegroundColor Yellow
$EnvFile = Join-Path $PSScriptRoot ".env"
$EnvExample = Join-Path $PSScriptRoot ".env.example"

if (-not (Test-Path $EnvFile)) {
    if (Test-Path $EnvExample) {
        Write-Host "      Generating initial .env from .env.example..." -ForegroundColor Gray
        Copy-Item -Path $EnvExample -Destination $EnvFile
        
        # Generate a random 32-char hex secret for TRADER_AUTH_SECRET
        $GeneratedSecret = & $VenvPython -c "import secrets; print(secrets.token_hex(16))" 2>$null
        if ($GeneratedSecret -and $GeneratedSecret.Length -ge 32) {
            $EnvContent = Get-Content $EnvFile -Raw
            $EnvContent = $EnvContent -replace "TRADER_AUTH_SECRET=", "TRADER_AUTH_SECRET=$GeneratedSecret"
            Set-Content -Path $EnvFile -Value $EnvContent -Encoding utf8
            Write-Host "      Created local .env with a secure random TRADER_AUTH_SECRET." -ForegroundColor Green
        } else {
            Write-Host "      Created local .env template. Please edit .env and configure TRADER_AUTH_SECRET." -ForegroundColor Yellow
        }
    } else {
        Write-Warning ".env.example not found. Please create .env manually."
    }
} else {
    Write-Host "      Local .env already exists. Preserving existing settings." -ForegroundColor Green
}

Write-Host ""
Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "                  Setup Complete!                         " -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "Next steps:" -ForegroundColor White
Write-Host "  1. Launch Research Dashboard:    .\run.ps1 dashboard" -ForegroundColor White
Write-Host "  2. Run Forward Evaluation CLI:   .\run.ps1 eval" -ForegroundColor White
Write-Host "  3. Run Automated Test Suite:     .\run.ps1 test" -ForegroundColor White
Write-Host ""
exit 0
