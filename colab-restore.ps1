# Colab CLI Windows Native — One-Click Restore
# ==============================================
# Usage: .\colab-restore.ps1
# Installs the Windows-compatible fork, configures SSL certs, and verifies.
#
# Source: C:\Users\woodh\Documents\colab-cli-windows\
# PR:     https://github.com/googlecolab/google-colab-cli/pull/70

param(
    [switch]$SkipInstall,
    [switch]$SkipSSL,
    [switch]$SkipVerify,
    [switch]$SkipADC
)

$ErrorActionPreference = "Stop"
$certBundle = "C:\anaconda3\Lib\site-packages\certifi\cacert.pem"

Write-Host "=== Colab CLI Windows Restore ===" -ForegroundColor Cyan

# ── 1. Install ──────────────────────────────────────────
if (-not $SkipInstall) {
    Write-Host "[1/4] Installing colab CLI (Windows fork)..." -ForegroundColor Yellow
    pip install git+https://github.com/woodhaha/google-colab-cli.git@windows-support --quiet 2>&1 | Out-Null
    Write-Host "       Installed: $(colab version 2>&1)" -ForegroundColor Green
}

# ── 2. ADC Auth ──────────────────────────────────────────
if (-not $SkipADC) {
    Write-Host "[2/4] Checking ADC auth..." -ForegroundColor Yellow
    $adcFile = "$env:APPDATA\gcloud\application_default_credentials.json"
    if (Test-Path $adcFile) {
        Write-Host "       ADC file exists: $adcFile" -ForegroundColor Green
    } else {
        Write-Host "       No ADC credentials found. Run:" -ForegroundColor Red
        Write-Host '       & "C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd" auth application-default login --scopes=openid,https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/userinfo.email,https://www.googleapis.com/auth/colaboratory' -ForegroundColor White
    }
}

# ── 3. SSL cert env vars ─────────────────────────────────
if (-not $SkipSSL) {
    Write-Host "[3/4] Configuring SSL cert env vars..." -ForegroundColor Yellow
    $profilePath = $PROFILE.CurrentUserCurrentHost
    $profileDir = Split-Path $profilePath -Parent
    if (-not (Test-Path $profileDir)) { New-Item -ItemType Directory -Force $profileDir | Out-Null }
    if (-not (Test-Path $profilePath)) { New-Item -ItemType File -Force $profilePath | Out-Null }

    $lines = @(
        '$env:SSL_CERT_FILE = "C:\anaconda3\Lib\site-packages\certifi\cacert.pem"',
        '$env:REQUESTS_CA_BUNDLE = "C:\anaconda3\Lib\site-packages\certifi\cacert.pem"'
    )
    $existing = Get-Content $profilePath -Raw -ErrorAction SilentlyContinue
    foreach ($line in $lines) {
        if ($existing -notmatch [regex]::Escape($line)) {
            Add-Content $profilePath $line
            Write-Host "       Added to profile: $line" -ForegroundColor Green
        } else {
            Write-Host "       Already in profile: $line" -ForegroundColor Gray
        }
    }

    # Also set for current session
    $env:SSL_CERT_FILE = $certBundle
    $env:REQUESTS_CA_BUNDLE = $certBundle
}

# ── 4. Verify ────────────────────────────────────────────
if (-not $SkipVerify) {
    Write-Host "[4/4] Verifying..." -ForegroundColor Yellow
    $result = colab --auth=adc sessions 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host "       $result" -ForegroundColor Green
        Write-Host ""
        Write-Host "=== Colab CLI Ready ===" -ForegroundColor Green
    } else {
        Write-Host "       $result" -ForegroundColor Red
        Write-Host ""
        Write-Host "=== Auth needed — see [2/4] above ===" -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "Quick commands:" -ForegroundColor Cyan
Write-Host "  colab --auth=adc new -s <name> --gpu T4" -ForegroundColor White
Write-Host "  colab --auth=adc exec -s <name> -f script.py" -ForegroundColor White
Write-Host "  colab --auth=adc upload -s <name> local.file /content/" -ForegroundColor White
Write-Host "  colab --auth=adc stop -s <name>" -ForegroundColor White
