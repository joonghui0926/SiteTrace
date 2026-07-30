[CmdletBinding()]
param(
    [ValidateSet("Start", "Status", "Stop")]
    [string]$Action = "Start",
    [string]$ProfileName = "sitetrace-workshop",
    [string]$Region = "us-east-1",
    [string]$BucketName = "workshop-sitetrace-755666155522",
    [string]$ProductionOrigin = "https://sitetrace-safety.danajoonghui.chatgpt.site",
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$pythonExecutable = Join-Path $repositoryRoot "backend\.venv\Scripts\python.exe"
$logDirectory = Join-Path $env:TEMP "sitetrace-runtime"
$stdoutPath = Join-Path $logDirectory "backend.out.log"
$stderrPath = Join-Path $logDirectory "backend.err.log"

function Get-SiteTraceListener {
    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $listener) {
        return $null
    }

    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)" -ErrorAction SilentlyContinue
    if (-not $process) {
        return $null
    }

    [pscustomobject]@{
        Listener = $listener
        Process = $process
    }
}

function Stop-SiteTraceBackend {
    $runtime = Get-SiteTraceListener
    if (-not $runtime) {
        return
    }

    $expectedBackend = Join-Path $repositoryRoot "backend"
    if ($runtime.Process.CommandLine -notlike "*$expectedBackend*" -and
        $runtime.Process.CommandLine -notlike "*--app-dir backend*") {
        throw "Port $Port is owned by an unrelated process. Refusing to stop it."
    }

    $parentId = $runtime.Process.ParentProcessId
    Stop-Process -Id $runtime.Process.ProcessId -Force -ErrorAction SilentlyContinue

    $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$parentId" -ErrorAction SilentlyContinue
    if ($parent -and
        ($parent.CommandLine -like "*$expectedBackend*" -or $parent.CommandLine -like "*--app-dir backend*")) {
        Stop-Process -Id $parentId -Force -ErrorAction SilentlyContinue
    }
}

if ($Action -eq "Stop") {
    Stop-SiteTraceBackend
    Write-Output '{"status":"stopped","service":"SiteTrace"}'
    exit 0
}

if ($Action -eq "Status") {
    $runtime = Get-SiteTraceListener
    if (-not $runtime) {
        Write-Output '{"status":"offline","service":"SiteTrace"}'
        exit 0
    }

    Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 10 |
        ConvertTo-Json -Depth 8
    exit 0
}

if (-not (Test-Path -LiteralPath $pythonExecutable)) {
    throw "Backend virtual environment not found at $pythonExecutable"
}

Stop-SiteTraceBackend
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null

$env:AWS_PROFILE = $ProfileName
$env:AWS_DEFAULT_REGION = $Region
$env:AWS_REGION = $Region
$env:SITETRACE_S3_BUCKET = $BucketName
$env:SITETRACE_SESSION_BUCKET = $BucketName
$env:APP_ENVIRONMENT = "workshop"
$env:CORS_ORIGINS = "$ProductionOrigin,http://localhost:3000,http://127.0.0.1:3000"

$process = Start-Process `
    -FilePath $pythonExecutable `
    -ArgumentList @(
        "-m",
        "uvicorn",
        "app.main:app",
        "--app-dir",
        "backend",
        "--host",
        "127.0.0.1",
        "--port",
        "$Port"
    ) `
    -WorkingDirectory $repositoryRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdoutPath `
    -RedirectStandardError $stderrPath `
    -PassThru

$ready = $false
for ($attempt = 0; $attempt -lt 30; $attempt += 1) {
    Start-Sleep -Milliseconds 500
    try {
        $ping = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/ping" -TimeoutSec 3
        $ready = $ping.status -eq "Healthy"
        break
    }
    catch {
        if ($process.HasExited) {
            break
        }
    }
}

if (-not $ready) {
    if (Test-Path -LiteralPath $stderrPath) {
        Get-Content -LiteralPath $stderrPath -Tail 40
    }
    throw "SiteTrace backend did not become ready."
}

$health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 30

[pscustomobject]@{
    status = $health.status
    service = $health.service
    mode = $health.mode
    process_id = $process.Id
    port = $Port
    aws = $health.sponsors.aws
    cors_origin = $ProductionOrigin
} | ConvertTo-Json -Depth 8
