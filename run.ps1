<#
.SYNOPSIS
    Convenience runner script for Trader-Swing commands and services.

.DESCRIPTION
    Provides simple, one-line commands to run the Streamlit research dashboard,
    the forward evaluation CLI, the test suite, or automated batch runners.

.EXAMPLE
    .\run.ps1 dashboard
    .\run.ps1 eval
    .\run.ps1 summary
    .\run.ps1 test
    .\run.ps1 scan
#>

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Command = "help",

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$RemainingArgs
)

$ErrorActionPreference = "Stop"

$VenvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Write-Host "[ERROR] Virtual environment not found at .venv" -ForegroundColor Red
    Write-Host "Please run the setup script first:" -ForegroundColor Yellow
    Write-Host "    .\setup.ps1" -ForegroundColor White
    exit 1
}

# Load environment variables from local .env if present
$EnvFile = Join-Path $PSScriptRoot ".env"
if (Test-Path $EnvFile) {
    Get-Content $EnvFile | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
            $key, $val = $line.Split("=", 2)
            $key = $key.Trim()
            $val = $val.Trim()
            if ($key -and -not [System.Environment]::GetEnvironmentVariable($key)) {
                [System.Environment]::SetEnvironmentVariable($key, $val, "Process")
            }
        }
    }
}

switch ($Command.ToLowerInvariant()) {
    { $_ -in "dashboard", "dash", "ui", "web" } {
        Write-Host "Starting Trader-Swing Research Dashboard on http://localhost:8501..." -ForegroundColor Cyan
        & $VenvPython -m streamlit run (Join-Path $PSScriptRoot "dashboard\app.py") @RemainingArgs
        exit $LASTEXITCODE
    }

    { $_ -in "eval", "evaluation", "report" } {
        Write-Host "Running Forward Evaluation Performance Report..." -ForegroundColor Cyan
        & $VenvPython (Join-Path $PSScriptRoot "forward_evaluation_cli.py") --report @RemainingArgs
        exit $LASTEXITCODE
    }

    { $_ -in "summary", "obs" } {
        Write-Host "Displaying Historical Forward Evaluation Summary..." -ForegroundColor Cyan
        & $VenvPython (Join-Path $PSScriptRoot "forward_evaluation_cli.py") --summary @RemainingArgs
        exit $LASTEXITCODE
    }

    { $_ -in "batch", "daily-run" } {
        Write-Host "Running Automated Daily Forward Evaluation Batch..." -ForegroundColor Cyan
        & $VenvPython (Join-Path $PSScriptRoot "run_forward_evaluation.py") @RemainingArgs
        exit $LASTEXITCODE
    }

    { $_ -in "test", "tests", "pytest" } {
        Write-Host "Executing Trader-Swing Automated Test Suite..." -ForegroundColor Cyan
        & $VenvPython -m pytest (Join-Path $PSScriptRoot "tests") -q @RemainingArgs
        exit $LASTEXITCODE
    }

    { $_ -in "scan", "scanner" } {
        Write-Host "Running Chart Scanner (Pre-Market Setups)..." -ForegroundColor Cyan
        & $VenvPython (Join-Path $PSScriptRoot "trader-scanner\scan.py") daily @RemainingArgs
        exit $LASTEXITCODE
    }

    default {
        Write-Host "==========================================================" -ForegroundColor Cyan
        Write-Host "               Trader-Swing Command Runner                " -ForegroundColor Cyan
        Write-Host "==========================================================" -ForegroundColor Cyan
        Write-Host ""
        Write-Host "Usage: .\run.ps1 <command> [options]" -ForegroundColor White
        Write-Host ""
        Write-Host "Available Commands:" -ForegroundColor Yellow
        Write-Host "  dashboard      Launch the Streamlit research dashboard (web UI)" -ForegroundColor White
        Write-Host "  eval           Run the forward paper evaluation performance report" -ForegroundColor White
        Write-Host "  summary        Display summary of recorded forward observations" -ForegroundColor White
        Write-Host "  batch          Execute automated daily forward evaluation batch" -ForegroundColor White
        Write-Host "  test           Execute the full pytest verification test suite" -ForegroundColor White
        Write-Host "  scan           Scan market for daily pre-market swing setups" -ForegroundColor White
        Write-Host "  help           Show this help menu" -ForegroundColor White
        Write-Host ""
        Write-Host "Examples:" -ForegroundColor Gray
        Write-Host "  .\run.ps1 dashboard" -ForegroundColor Gray
        Write-Host "  .\run.ps1 eval --session-date 2026-11-20" -ForegroundColor Gray
        Write-Host "  .\run.ps1 test" -ForegroundColor Gray
        Write-Host ""
        exit 0
    }
}
