# deploy.ps1 - Deploy G2C AI Sync to Google Apps Script
#
# First time (with API key):
#   .\deploy.ps1 -GeminiApiKey "AIza..."
#
# Update code only (no key change):
#   .\deploy.ps1

param(
    [string]$GeminiApiKey = ""
)

$ScriptId = "1RlXvEwwmxKNMdTxaddwPMvCWvvaooKkvzYTGx0yA5HgYblwlE_4L7p8D"

function Get-AccessToken {
    $clasprc = Get-Content "$env:USERPROFILE\.clasprc.json" | ConvertFrom-Json
    $creds = $clasprc.tokens.default

    # Refresh if needed
    $body = @{
        client_id     = $creds.client_id
        client_secret = $creds.client_secret
        refresh_token = $creds.refresh_token
        grant_type    = "refresh_token"
    }
    $resp = Invoke-RestMethod -Uri "https://oauth2.googleapis.com/token" -Method Post -Body $body
    return $resp.access_token
}

function Invoke-GasFunction {
    param([string]$FunctionName, [array]$Params = @())
    $token = Get-AccessToken
    $body = @{ function = $FunctionName; parameters = $Params } | ConvertTo-Json
    $resp = Invoke-RestMethod `
        -Uri "https://script.googleapis.com/v1/scripts/$ScriptId`:run" `
        -Method Post `
        -Headers @{ Authorization = "Bearer $token" } `
        -ContentType "application/json" `
        -Body $body
    if ($resp.error) {
        Write-Host "ERROR: $($resp.error.message)" -ForegroundColor Red
        return $false
    }
    return $true
}

# Push code
Write-Host "Pushing code..." -ForegroundColor Cyan
Set-Location "$PSScriptRoot\gas"
clasp push --force
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: clasp push failed" -ForegroundColor Red; exit 1 }
clasp deploy | Out-Null

# Set API key
if ($GeminiApiKey -ne "") {
    Write-Host "Setting GEMINI_API_KEY..." -ForegroundColor Cyan
    $ok = Invoke-GasFunction -FunctionName "setProperties" -Params @($GeminiApiKey)
    if (-not $ok) { exit 1 }
    Write-Host "API key set." -ForegroundColor Green
}

# Setup trigger
Write-Host "Setting hourly trigger..." -ForegroundColor Cyan
$ok = Invoke-GasFunction -FunctionName "setupTrigger"
if (-not $ok) { exit 1 }

Write-Host "Done." -ForegroundColor Green
