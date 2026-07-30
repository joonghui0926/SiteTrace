<#
.SYNOPSIS
Builds and deploys the temporary SiteTrace AgentCore runtime.

.DESCRIPTION
The default action is Plan and performs no AWS or Docker mutation. Deploy uses
the temporary AWS CLI profile "sitetrace-workshop" in us-east-1. CodeZip is the
Workshop-compatible default because the participant role can manage prefixed
S3, IAM, and AgentCore resources but cannot create or push ECR repositories.
Container mode remains available for accounts with ECR permissions.

By default, the runtime receives only Secrets Manager secret IDs and
backend/agentcore_entrypoint.py retrieves values with the execution role.
The explicit -UseLocalEnvironmentSecrets workshop fallback reads an allowlist
from .env, never prints values, and should not be used for production.

.EXAMPLE
.\infra\aws\deploy-agentcore.ps1

.EXAMPLE
.\infra\aws\deploy-agentcore.ps1 -Action Deploy `
  -DeploymentMode CodeZip -UseLocalEnvironmentSecrets

.EXAMPLE
.\infra\aws\deploy-agentcore.ps1 -Action ValidatePackage

.EXAMPLE
.\infra\aws\deploy-agentcore.ps1 -Action Deploy -RoleArn <role-arn> -WhatIf

.EXAMPLE
.\infra\aws\deploy-agentcore.ps1 -Action Cleanup -ConfirmCleanup `
  -RemoveImageRepository

.NOTES
Cleanup intentionally leaves the evidence bucket and Secrets Manager secrets
untouched. Remove those separately only after exporting anything required.
#>

[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [ValidateSet("Plan", "ValidatePackage", "Deploy", "Cleanup")]
    [string]$Action = "Plan",

    [ValidatePattern("^[A-Za-z0-9_-]+$")]
    [string]$Profile = "sitetrace-workshop",

    [ValidatePattern("^[a-z]{2}-[a-z]+-[0-9]$")]
    [string]$Region = "us-east-1",

    [ValidateSet("CodeZip", "Container")]
    [string]$DeploymentMode = "CodeZip",

    [ValidatePattern("^[a-z0-9]+(?:[._/-][a-z0-9]+)*$")]
    [string]$EcrRepository = "sitetrace-agent",

    [ValidatePattern("^[A-Za-z][A-Za-z0-9_]{0,47}$")]
    [string]$RuntimeName = "SiteTraceWorkshop",

    [ValidatePattern("^[A-Za-z][A-Za-z0-9_]{0,47}$")]
    [string]$EndpointName = "Workshop",

    [string]$RoleArn = $env:SITETRACE_AGENTCORE_ROLE_ARN,
    [string]$DataBucket = $env:SITETRACE_S3_BUCKET,
    [string]$ImageTag,
    [ValidatePattern("^workshop-[A-Za-z0-9+=,.@_-]{1,55}$")]
    [string]$RuntimeRoleName = "workshop-sitetrace-agentcore-runtime",

    [string]$OpenAISecretId = "sitetrace/openai",
    [string]$TwelveLabsSecretId = "sitetrace/twelvelabs",
    [string]$Neo4jSecretId = "sitetrace/neo4j",

    [switch]$UseLocalEnvironmentSecrets,
    [string]$EnvironmentFile,
    [switch]$DryRun,
    [switch]$ConfirmCleanup,
    [switch]$RemoveImageRepository
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$env:AWS_PAGER = ""

$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$backendDirectory = Join-Path $repositoryRoot "backend"
$dockerfile = Join-Path $backendDirectory "Dockerfile"
if (-not $EnvironmentFile) {
    $EnvironmentFile = Join-Path $repositoryRoot ".env"
}
$awsCommand = Get-Command "aws" -ErrorAction SilentlyContinue
if ($awsCommand) {
    $script:AwsExecutable = $awsCommand.Source
}
else {
    $standardAwsPaths = @(
        "C:\Program Files\Amazon\AWSCLIV2\aws.exe",
        "C:\Program Files (x86)\Amazon\AWSCLIV2\aws.exe"
    )
    $script:AwsExecutable = $standardAwsPaths |
        Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } |
        Select-Object -First 1
}

function Write-Plan {
    Write-Host "SiteTrace AgentCore temporary deployment plan"
    Write-Host "  AWS CLI            : $(
        if ($script:AwsExecutable) { $script:AwsExecutable } else { "not found" }
    )"
    Write-Host "  AWS profile/region : $Profile / $Region"
    Write-Host "  Deployment mode    : $DeploymentMode"
    if ($DeploymentMode -eq "Container") {
        Write-Host "  ECR repository     : $EcrRepository"
    }
    else {
        Write-Host "  Artifact storage   : $(
            if ($DataBucket) {
                $DataBucket
            }
            else {
                "workshop-sitetrace-<account-id> (created on deploy)"
            }
        )"
        Write-Host "  Runtime role       : $(
            if ($RoleArn) { $RoleArn } else { $RuntimeRoleName }
        )"
    }
    Write-Host "  Runtime/endpoint   : $RuntimeName / $EndpointName"
    Write-Host "  Runtime contract   : linux/arm64, port 8080, HTTP"
    Write-Host "  Secret references  : OpenAI, TwelveLabs, Neo4j (values are never printed)"
    if ($UseLocalEnvironmentSecrets) {
        Write-Warning (
            "Local provider secrets will be copied into the temporary " +
            "AgentCore environment. Prefer Secrets Manager outside this " +
            "isolated Workshop account."
        )
    }
    if ($DataBucket) {
        Write-Host "  Existing data bucket: $DataBucket"
    }
    else {
        Write-Host "  Existing data bucket: not configured"
    }
    Write-Host ""
    Write-Host "Plan is read-only. Use -Action Deploy to apply it."
    Write-Host "Use -WhatIf with Deploy or Cleanup to inspect mutations."
    Write-Host ""
    Write-Host "Browser REST plan:"
    Write-Host "  Lambda + HTTP API can reuse the generated ARM64 ZIP via"
    Write-Host "  agentcore_entrypoint.lambda_handler. The Workshop role currently lacks"
    Write-Host "  API Gateway and Lambda Function URL management permissions, so this"
    Write-Host "  public HTTPS facade cannot be created until those actions are granted."
}

function Assert-Command {
    param([Parameter(Mandatory = $true)][string]$Name)
    if ($Name -eq "aws" -and $script:AwsExecutable) {
        return
    }
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command '$Name' was not found on PATH."
    }
}

function Invoke-AwsText {
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [switch]$AllowFailure
    )

    $allArguments = @($Arguments) + @(
        "--profile", $Profile,
        "--region", $Region,
        "--no-cli-pager"
    )
    if (-not $script:AwsExecutable) {
        throw (
            "AWS CLI was not found on PATH or under " +
            "'C:\Program Files\Amazon\AWSCLIV2'."
        )
    }
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $output = & $script:AwsExecutable @allArguments 2>&1
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    $text = ($output | Out-String).Trim()
    if ($exitCode -ne 0 -and -not $AllowFailure) {
        throw "AWS CLI failed (exit $exitCode): $text"
    }
    if ($exitCode -ne 0) {
        return $null
    }
    return $text
}

function Test-AwsCommand {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    $allArguments = @($Arguments) + @(
        "--profile", $Profile,
        "--region", $Region,
        "--no-cli-pager"
    )
    if (-not $script:AwsExecutable) {
        throw (
            "AWS CLI was not found on PATH or under " +
            "'C:\Program Files\Amazon\AWSCLIV2'."
        )
    }
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & $script:AwsExecutable @allArguments *> $null
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    return $exitCode -eq 0
}

function Save-TemporaryJson {
    param(
        [Parameter(Mandatory = $true)]$Value,
        [Parameter(Mandatory = $true)]
        [AllowEmptyCollection()]
        [System.Collections.Generic.List[string]]$TrackedFiles
    )

    $path = [System.IO.Path]::GetTempFileName()
    [System.IO.File]::WriteAllText(
        $path,
        ($Value | ConvertTo-Json -Depth 20 -Compress),
        [System.Text.UTF8Encoding]::new($false)
    )
    $TrackedFiles.Add($path)
    return $path
}

function Get-Runtime {
    $raw = Invoke-AwsText -Arguments @(
        "bedrock-agentcore-control", "list-agent-runtimes",
        "--max-items", "100",
        "--output", "json"
    )
    $matches = @(
        (($raw | ConvertFrom-Json).agentRuntimes) |
            Where-Object { $_.agentRuntimeName -eq $RuntimeName }
    )
    if ($matches.Count -gt 1) {
        throw "More than one AgentCore Runtime is named '$RuntimeName'."
    }
    if ($matches.Count -eq 1) {
        return $matches[0]
    }
    return $null
}

function Wait-RuntimeReady {
    param([Parameter(Mandatory = $true)][string]$RuntimeId)

    for ($attempt = 1; $attempt -le 60; $attempt += 1) {
        $raw = Invoke-AwsText -Arguments @(
            "bedrock-agentcore-control", "get-agent-runtime",
            "--agent-runtime-id", $RuntimeId,
            "--output", "json"
        )
        $runtime = $raw | ConvertFrom-Json
        if ($runtime.status -eq "READY") {
            return $runtime
        }
        if ($runtime.status -in @("CREATE_FAILED", "UPDATE_FAILED")) {
            throw "AgentCore Runtime failed: $($runtime.failureReason)"
        }
        Start-Sleep -Seconds 5
    }
    throw "Timed out waiting for AgentCore Runtime '$RuntimeId'."
}

function Get-Endpoint {
    param([Parameter(Mandatory = $true)][string]$RuntimeId)

    $raw = Invoke-AwsText -Arguments @(
        "bedrock-agentcore-control", "list-agent-runtime-endpoints",
        "--agent-runtime-id", $RuntimeId,
        "--max-items", "100",
        "--output", "json"
    )
    $matches = @(
        (($raw | ConvertFrom-Json).runtimeEndpoints) |
            Where-Object { $_.name -eq $EndpointName }
    )
    if ($matches.Count -gt 1) {
        throw "More than one endpoint is named '$EndpointName'."
    }
    if ($matches.Count -eq 1) {
        return $matches[0]
    }
    return $null
}

function Wait-EndpointReady {
    param([Parameter(Mandatory = $true)][string]$RuntimeId)

    for ($attempt = 1; $attempt -le 60; $attempt += 1) {
        $raw = Invoke-AwsText -Arguments @(
            "bedrock-agentcore-control", "get-agent-runtime-endpoint",
            "--agent-runtime-id", $RuntimeId,
            "--endpoint-name", $EndpointName,
            "--output", "json"
        )
        $endpoint = $raw | ConvertFrom-Json
        if ($endpoint.status -eq "READY") {
            return $endpoint
        }
        if ($endpoint.status -in @("CREATE_FAILED", "UPDATE_FAILED")) {
            throw "AgentCore endpoint failed: $($endpoint.failureReason)"
        }
        Start-Sleep -Seconds 5
    }
    throw "Timed out waiting for AgentCore endpoint '$EndpointName'."
}

function Wait-EndpointDeleted {
    param([Parameter(Mandatory = $true)][string]$RuntimeId)

    for ($attempt = 1; $attempt -le 60; $attempt += 1) {
        $raw = Invoke-AwsText -Arguments @(
            "bedrock-agentcore-control", "get-agent-runtime-endpoint",
            "--agent-runtime-id", $RuntimeId,
            "--endpoint-name", $EndpointName,
            "--output", "json"
        ) -AllowFailure
        if (-not $raw) {
            return
        }
        Start-Sleep -Seconds 5
    }
    throw "Timed out waiting for AgentCore endpoint '$EndpointName' deletion."
}

function Get-PythonExecutable {
    $virtualEnvironmentPython = Join-Path (
        Join-Path $backendDirectory ".venv"
    ) "Scripts\python.exe"
    if (Test-Path -LiteralPath $virtualEnvironmentPython -PathType Leaf) {
        return $virtualEnvironmentPython
    }
    $python = Get-Command "python" -ErrorAction SilentlyContinue
    if (-not $python) {
        throw "Python 3.11 or newer is required to build the CodeZip package."
    }
    return $python.Source
}

function Get-RuntimeEnvironmentVariables {
    param([Parameter(Mandatory = $true)][string]$ResolvedBucket)

    $values = [ordered]@{
        AWS_REGION = $Region
        APP_ENVIRONMENT = "workshop"
        SITETRACE_S3_BUCKET = $ResolvedBucket
        SITETRACE_SESSION_BUCKET = $ResolvedBucket
        SITETRACE_SESSION_PREFIX = "sitetrace/sessions/"
        SITETRACE_OPENAI_SECRET_ID = $OpenAISecretId
        SITETRACE_TWELVELABS_SECRET_ID = $TwelveLabsSecretId
        SITETRACE_NEO4J_SECRET_ID = $Neo4jSecretId
        UPLOAD_DIRECTORY = "/tmp/sitetrace/uploads"
        CASE_DIRECTORY = "/tmp/sitetrace/cases"
        REPORT_DIRECTORY = "/tmp/sitetrace/reports"
        SITETRACE_SESSION_DIRECTORY = "/tmp/sitetrace/sessions"
    }

    if (-not $UseLocalEnvironmentSecrets) {
        return ,$values
    }
    if (-not (Test-Path -LiteralPath $EnvironmentFile -PathType Leaf)) {
        throw "Local environment file was not found at '$EnvironmentFile'."
    }

    $allowedKeys = @(
        "OPENAI_API_KEY",
        "OPENAI_NORMALIZATION_MODEL",
        "OPENAI_REASONING_MODEL",
        "OPENAI_ESCALATION_MODEL",
        "TWELVE_LABS_API_KEY",
        "TWELVE_LABS_KNOWLEDGE_STORE_ID",
        "NEO4J_URI",
        "NEO4J_USERNAME",
        "NEO4J_PASSWORD",
        "NEO4J_DATABASE"
    )
    $loadedKeys = [System.Collections.Generic.HashSet[string]]::new()
    foreach ($line in Get-Content -LiteralPath $EnvironmentFile) {
        if ($line -notmatch "^\s*([A-Z0-9_]+)\s*=(.*)$") {
            continue
        }
        $name = $matches[1]
        if ($name -notin $allowedKeys) {
            continue
        }
        $value = $matches[2].Trim()
        if (
            $value.Length -ge 2 -and
            (
                ($value.StartsWith('"') -and $value.EndsWith('"')) -or
                ($value.StartsWith("'") -and $value.EndsWith("'"))
            )
        ) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        if ($value) {
            $values[$name] = $value
            [void]$loadedKeys.Add($name)
        }
    }
    foreach ($requiredKey in @(
        "OPENAI_API_KEY",
        "TWELVE_LABS_API_KEY",
        "NEO4J_URI",
        "NEO4J_USERNAME",
        "NEO4J_PASSWORD"
    )) {
        if (-not $loadedKeys.Contains($requiredKey)) {
            throw (
                "UseLocalEnvironmentSecrets requires '$requiredKey' in " +
                "the selected environment file."
            )
        }
    }
    return ,$values
}

function New-CodeZipPackage {
    param(
        [Parameter(Mandatory = $true)][string]$PythonExecutable,
        [Parameter(Mandatory = $true)]
        [AllowEmptyCollection()]
        [System.Collections.Generic.List[string]]$TrackedDirectories
    )

    $temporaryRoot = Join-Path (
        [System.IO.Path]::GetTempPath()
    ) "sitetrace-agentcore-$([Guid]::NewGuid().ToString('N'))"
    $packageRoot = Join-Path $temporaryRoot "package"
    $zipPath = Join-Path $temporaryRoot "sitetrace-agentcore.zip"
    [void](New-Item -ItemType Directory -Path $packageRoot -Force)
    $TrackedDirectories.Add($temporaryRoot)

    $pyprojectPath = Join-Path $backendDirectory "pyproject.toml"
    $requirementsPath = Join-Path $temporaryRoot "requirements.txt"
    $dependencies = & $PythonExecutable -c (
        "import pathlib,sys,tomllib;" +
        "d=tomllib.loads(pathlib.Path(sys.argv[1]).read_text('utf-8'));" +
        "print('\n'.join(d['project']['dependencies']))"
    ) $pyprojectPath
    if ($LASTEXITCODE -ne 0) {
        throw "Could not read backend dependencies from pyproject.toml."
    }
    @($dependencies) + @("mangum>=0.19,<1") |
        Set-Content -LiteralPath $requirementsPath -Encoding utf8

    # pip's --platform controls wheel selection but evaluates environment
    # markers against the build host. Override only the marker environment so
    # Windows does not try to add pywin32 to a Linux ARM64 package.
    $pipShim = (
        "import sys;" +
        "from pip._vendor.packaging import markers;" +
        "_default=markers.default_environment;" +
        "markers.default_environment=lambda:dict(" +
        "_default(),sys_platform='linux',platform_system='Linux'," +
        "os_name='posix',python_version='3.13'," +
        "python_full_version='3.13.0'," +
        "platform_python_implementation='CPython'," +
        "implementation_name='cpython',implementation_version='3.13.0');" +
        "from pip._internal.cli.main import main;" +
        "raise SystemExit(main(sys.argv[1:]))"
    )
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $pipOutput = & $PythonExecutable -c $pipShim install `
            --disable-pip-version-check `
            --no-compile `
            --target $packageRoot `
            --platform manylinux2014_aarch64 `
            --implementation cp `
            --python-version 3.13 `
            --only-binary=:all: `
            --requirement $requirementsPath 2>&1
        $pipExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($pipExitCode -ne 0) {
        $pipSummary = ($pipOutput | Select-Object -Last 8 | Out-String).Trim()
        if ($pipSummary) {
            Write-Warning $pipSummary
        }
        throw "Unable to resolve Linux ARM64 dependencies for CodeZip."
    }

    Copy-Item -LiteralPath (
        Join-Path $backendDirectory "agentcore_entrypoint.py"
    ) -Destination $packageRoot
    Copy-Item -LiteralPath (
        Join-Path $backendDirectory "app"
    ) -Destination $packageRoot -Recurse
    Copy-Item -LiteralPath (
        Join-Path $backendDirectory "skills"
    ) -Destination $packageRoot -Recurse
    Copy-Item -LiteralPath (
        Join-Path $backendDirectory "cypher"
    ) -Destination $packageRoot -Recurse

    Get-ChildItem -LiteralPath $packageRoot -Recurse -Directory |
        Where-Object { $_.Name -eq "__pycache__" } |
        Remove-Item -Recurse -Force
    Get-ChildItem -LiteralPath $packageRoot -Recurse -File -Filter "*.pyc" |
        Remove-Item -Force

    $uncompressedBytes = (
        Get-ChildItem -LiteralPath $packageRoot -Recurse -File |
            Measure-Object -Property Length -Sum
    ).Sum
    if ($uncompressedBytes -gt 250MB) {
        throw (
            "AgentCore CodeZip package is larger than the 250 MB " +
            "uncompressed service limit."
        )
    }
    Compress-Archive -Path (Join-Path $packageRoot "*") `
        -DestinationPath $zipPath `
        -CompressionLevel Optimal

    return [pscustomobject]@{
        Path = $zipPath
        UncompressedBytes = $uncompressedBytes
        CompressedBytes = (Get-Item -LiteralPath $zipPath).Length
    }
}

function Remove-TrackedTemporaryDirectories {
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyCollection()]
        [System.Collections.Generic.List[string]]$TrackedDirectories
    )

    $temporaryBase = [System.IO.Path]::GetFullPath(
        [System.IO.Path]::GetTempPath()
    ).TrimEnd("\", "/")
    foreach ($path in $TrackedDirectories) {
        $fullPath = [System.IO.Path]::GetFullPath($path)
        $leafName = Split-Path -Leaf $fullPath
        $isUnderTemporaryBase = $fullPath.StartsWith(
            "$temporaryBase\",
            [System.StringComparison]::OrdinalIgnoreCase
        )
        if (
            -not $isUnderTemporaryBase -or
            $leafName -notlike "sitetrace-agentcore-*"
        ) {
            throw "Refusing to remove unexpected temporary path '$fullPath'."
        }
        if (Test-Path -LiteralPath $fullPath -PathType Container) {
            Remove-Item -LiteralPath $fullPath -Recurse -Force `
                -WhatIf:$false -Confirm:$false
        }
    }
}

function Test-CodeZipPackage {
    $temporaryDirectories = [System.Collections.Generic.List[string]]::new()
    try {
        $pythonExecutable = Get-PythonExecutable
        Write-Host "Validating Linux ARM64 Python 3.13 CodeZip package..."
        $package = New-CodeZipPackage `
            -PythonExecutable $pythonExecutable `
            -TrackedDirectories $temporaryDirectories
        Write-Host "CodeZip package validation passed."
        Write-Host "  Uncompressed bytes : $($package.UncompressedBytes)"
        Write-Host "  ZIP bytes          : $($package.CompressedBytes)"
    }
    finally {
        Remove-TrackedTemporaryDirectories `
            -TrackedDirectories $temporaryDirectories
    }
}

function Ensure-CodeZipResources {
    param(
        [Parameter(Mandatory = $true)][string]$AccountId,
        [Parameter(Mandatory = $true)][string]$ResolvedBucket,
        [Parameter(Mandatory = $true)]
        [AllowEmptyCollection()]
        [System.Collections.Generic.List[string]]$TrackedFiles
    )

    $bucketExists = Test-AwsCommand -Arguments @(
        "s3api", "head-bucket",
        "--bucket", $ResolvedBucket
    )
    if (-not $bucketExists) {
        if ($DataBucket) {
            throw "The requested S3 bucket is unavailable: '$ResolvedBucket'."
        }
        if ($PSCmdlet.ShouldProcess(
            $ResolvedBucket,
            "Create private temporary workshop S3 bucket"
        )) {
            $createBucketArguments = @(
                "s3api", "create-bucket",
                "--bucket", $ResolvedBucket
            )
            if ($Region -ne "us-east-1") {
                $createBucketArguments += @(
                    "--create-bucket-configuration",
                    "LocationConstraint=$Region"
                )
            }
            $createBucketArguments += @(
                "--output", "json"
            )
            [void](Invoke-AwsText -Arguments $createBucketArguments)
            $lifecycle = @{
                Rules = @(
                    @{
                        ID = "ExpireDeploymentPackages"
                        Status = "Enabled"
                        Filter = @{ Prefix = "sitetrace/deployments/" }
                        Expiration = @{ Days = 2 }
                    }
                )
            }
            $lifecyclePath = Save-TemporaryJson `
                -Value $lifecycle `
                -TrackedFiles $TrackedFiles
            [void](Invoke-AwsText -Arguments @(
                "s3api", "put-bucket-lifecycle-configuration",
                "--bucket", $ResolvedBucket,
                "--lifecycle-configuration", "file://$lifecyclePath"
            ))
        }
    }

    if ($RoleArn) {
        return $RoleArn
    }
    $roleRaw = Invoke-AwsText -Arguments @(
        "iam", "get-role",
        "--role-name", $RuntimeRoleName,
        "--output", "json"
    ) -AllowFailure
    if ($roleRaw) {
        return ($roleRaw | ConvertFrom-Json).Role.Arn
    }

    $trustPolicy = @{
        Version = "2012-10-17"
        Statement = @(
            @{
                Sid = "AgentCoreAssumeRole"
                Effect = "Allow"
                Principal = @{
                    Service = "bedrock-agentcore.amazonaws.com"
                }
                Action = "sts:AssumeRole"
                Condition = @{
                    StringEquals = @{
                        "aws:SourceAccount" = $AccountId
                    }
                    ArnLike = @{
                        "aws:SourceArn" = (
                            "arn:aws:bedrock-agentcore:${Region}:" +
                            "${AccountId}:*"
                        )
                    }
                }
            }
        )
    }
    $runtimePolicy = @{
        Version = "2012-10-17"
        Statement = @(
            @{
                Sid = "SiteTraceData"
                Effect = "Allow"
                Action = @(
                    "s3:ListBucket",
                    "s3:GetObject",
                    "s3:PutObject",
                    "s3:DeleteObject"
                )
                Resource = @(
                    "arn:aws:s3:::$ResolvedBucket",
                    "arn:aws:s3:::$ResolvedBucket/*"
                )
            },
            @{
                Sid = "SiteTraceProviderSecrets"
                Effect = "Allow"
                Action = @("secretsmanager:GetSecretValue")
                Resource = @(
                    "arn:aws:secretsmanager:${Region}:${AccountId}:secret:$OpenAISecretId*",
                    "arn:aws:secretsmanager:${Region}:${AccountId}:secret:$TwelveLabsSecretId*",
                    "arn:aws:secretsmanager:${Region}:${AccountId}:secret:$Neo4jSecretId*"
                )
            },
            @{
                Sid = "AgentCoreObservability"
                Effect = "Allow"
                Action = @(
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "logs:DescribeLogStreams",
                    "xray:PutTraceSegments",
                    "xray:PutTelemetryRecords",
                    "xray:GetSamplingRules",
                    "xray:GetSamplingTargets",
                    "cloudwatch:PutMetricData"
                )
                Resource = "*"
            }
        )
    }
    $trustPath = Save-TemporaryJson `
        -Value $trustPolicy `
        -TrackedFiles $TrackedFiles
    $policyPath = Save-TemporaryJson `
        -Value $runtimePolicy `
        -TrackedFiles $TrackedFiles

    if ($PSCmdlet.ShouldProcess(
        $RuntimeRoleName,
        "Create least-privilege AgentCore execution role"
    )) {
        $createdRoleRaw = Invoke-AwsText -Arguments @(
            "iam", "create-role",
            "--role-name", $RuntimeRoleName,
            "--assume-role-policy-document", "file://$trustPath",
            "--description", "Temporary SiteTrace AgentCore runtime role",
            "--tags",
            "Key=Project,Value=SiteTrace",
            "Key=Lifecycle,Value=WorkshopTemporary",
            "--output", "json"
        )
        [void](Invoke-AwsText -Arguments @(
            "iam", "put-role-policy",
            "--role-name", $RuntimeRoleName,
            "--policy-name", "SiteTraceAgentCoreRuntime",
            "--policy-document", "file://$policyPath"
        ))
        Start-Sleep -Seconds 10
        return ($createdRoleRaw | ConvertFrom-Json).Role.Arn
    }
    return $null
}

function Assert-DeploymentInputs {
    Assert-Command "aws"
    if (
        $RoleArn -and
        $RoleArn -notmatch "^arn:aws(?:-[^:]+)?:iam::[0-9]{12}:role/.+$"
    ) {
        throw "-RoleArn is not a valid IAM role ARN."
    }
    if ($DataBucket -and $DataBucket -notmatch "^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$") {
        throw "-DataBucket is not a valid S3 bucket name."
    }
    if ($DeploymentMode -eq "Container") {
        Assert-Command "docker"
        if (-not (Test-Path -LiteralPath $dockerfile -PathType Leaf)) {
            throw "Backend Dockerfile was not found at '$dockerfile'."
        }
        if (-not $RoleArn) {
            throw "-RoleArn is required for Container deployment mode."
        }
    }
    else {
        [void](Get-PythonExecutable)
        if (-not (Test-Path -LiteralPath (
            Join-Path $backendDirectory "pyproject.toml"
        ) -PathType Leaf)) {
            throw "backend/pyproject.toml is required for CodeZip."
        }
        if ($UseLocalEnvironmentSecrets) {
            [void](Get-RuntimeEnvironmentVariables `
                -ResolvedBucket "workshop-validation-placeholder")
        }
    }

    [void](Invoke-AwsText -Arguments @(
        "sts", "get-caller-identity",
        "--query", "Account",
        "--output", "text"
    ))
    if ($DataBucket) {
        [void](Invoke-AwsText -Arguments @(
            "s3api", "head-bucket",
            "--bucket", $DataBucket
        ))
    }
}

function Deploy-AgentCoreCodeZip {
    Assert-DeploymentInputs
    $temporaryFiles = [System.Collections.Generic.List[string]]::new()
    $temporaryDirectories = [System.Collections.Generic.List[string]]::new()

    try {
        $accountId = Invoke-AwsText -Arguments @(
            "sts", "get-caller-identity",
            "--query", "Account",
            "--output", "text"
        )
        $resolvedBucket = $DataBucket
        if (-not $resolvedBucket) {
            $resolvedBucket = "workshop-sitetrace-$accountId"
        }

        $resolvedRoleArn = Ensure-CodeZipResources `
            -AccountId $accountId `
            -ResolvedBucket $resolvedBucket `
            -TrackedFiles $temporaryFiles

        if ($WhatIfPreference) {
            Write-Host "WhatIf complete; no package or AWS resource was changed."
            return
        }
        if (-not $resolvedRoleArn) {
            throw "The AgentCore execution role could not be resolved."
        }

        $environmentVariables = Get-RuntimeEnvironmentVariables `
            -ResolvedBucket $resolvedBucket
        $pythonExecutable = Get-PythonExecutable
        Write-Host "Building Linux ARM64 Python 3.13 CodeZip package..."
        $package = New-CodeZipPackage `
            -PythonExecutable $pythonExecutable `
            -TrackedDirectories $temporaryDirectories
        $artifactKey = (
            "sitetrace/deployments/{0}/sitetrace-agentcore.zip" -f
            [DateTime]::UtcNow.ToString("yyyyMMddHHmmss")
        )

        if ($PSCmdlet.ShouldProcess(
            "s3://$resolvedBucket/$artifactKey",
            "Upload AgentCore CodeZip package"
        )) {
            [void](Invoke-AwsText -Arguments @(
                "s3api", "put-object",
                "--bucket", $resolvedBucket,
                "--key", $artifactKey,
                "--body", $package.Path,
                "--content-type", "application/zip",
                "--output", "json"
            ))
        }

        $runtimeConfiguration = [ordered]@{
            agentRuntimeArtifact = @{
                codeConfiguration = @{
                    code = @{
                        s3 = @{
                            bucket = $resolvedBucket
                            prefix = $artifactKey
                        }
                    }
                    runtime = "PYTHON_3_13"
                    entryPoint = @("agentcore_entrypoint.py")
                }
            }
            roleArn = $resolvedRoleArn
            networkConfiguration = @{ networkMode = "PUBLIC" }
            protocolConfiguration = @{ serverProtocol = "HTTP" }
            lifecycleConfiguration = @{
                idleRuntimeSessionTimeout = 900
                maxLifetime = 28800
            }
            environmentVariables = $environmentVariables
            description = (
                "Temporary SiteTrace workshop evidence investigation runtime"
            )
        }

        $runtime = Get-Runtime
        if ($runtime) {
            $updateRequest = [ordered]@{
                agentRuntimeId = $runtime.agentRuntimeId
                clientToken = ([Guid]::NewGuid().ToString())
            } + $runtimeConfiguration
            $requestPath = Save-TemporaryJson `
                -Value $updateRequest `
                -TrackedFiles $temporaryFiles
            if ($PSCmdlet.ShouldProcess(
                $runtime.agentRuntimeId,
                "Update AgentCore Runtime from CodeZip"
            )) {
                $raw = Invoke-AwsText -Arguments @(
                    "bedrock-agentcore-control", "update-agent-runtime",
                    "--cli-input-json", "file://$requestPath",
                    "--output", "json"
                )
                $runtime = $raw | ConvertFrom-Json
            }
        }
        else {
            $createRequest = [ordered]@{
                agentRuntimeName = $RuntimeName
                clientToken = ([Guid]::NewGuid().ToString())
                tags = @{
                    Project = "SiteTrace"
                    Lifecycle = "WorkshopTemporary"
                }
            } + $runtimeConfiguration
            $requestPath = Save-TemporaryJson `
                -Value $createRequest `
                -TrackedFiles $temporaryFiles
            if ($PSCmdlet.ShouldProcess(
                $RuntimeName,
                "Create AgentCore Runtime from CodeZip"
            )) {
                $raw = Invoke-AwsText -Arguments @(
                    "bedrock-agentcore-control", "create-agent-runtime",
                    "--cli-input-json", "file://$requestPath",
                    "--output", "json"
                )
                $runtime = $raw | ConvertFrom-Json
            }
        }

        if (-not $runtime -or -not $runtime.agentRuntimeId) {
            throw "AgentCore did not return a runtime identifier."
        }

        $runtime = Wait-RuntimeReady -RuntimeId $runtime.agentRuntimeId
        $endpoint = Get-Endpoint -RuntimeId $runtime.agentRuntimeId
        if ($endpoint) {
            if ($PSCmdlet.ShouldProcess(
                "$($runtime.agentRuntimeId)/$EndpointName",
                "Point endpoint at runtime version $($runtime.agentRuntimeVersion)"
            )) {
                [void](Invoke-AwsText -Arguments @(
                    "bedrock-agentcore-control",
                    "update-agent-runtime-endpoint",
                    "--agent-runtime-id", $runtime.agentRuntimeId,
                    "--endpoint-name", $EndpointName,
                    "--agent-runtime-version", $runtime.agentRuntimeVersion,
                    "--client-token", ([Guid]::NewGuid().ToString()),
                    "--description", "Temporary SiteTrace workshop endpoint",
                    "--output", "json"
                ))
            }
        }
        else {
            if ($PSCmdlet.ShouldProcess(
                "$($runtime.agentRuntimeId)/$EndpointName",
                "Create endpoint for runtime version $($runtime.agentRuntimeVersion)"
            )) {
                [void](Invoke-AwsText -Arguments @(
                    "bedrock-agentcore-control",
                    "create-agent-runtime-endpoint",
                    "--agent-runtime-id", $runtime.agentRuntimeId,
                    "--name", $EndpointName,
                    "--agent-runtime-version", $runtime.agentRuntimeVersion,
                    "--client-token", ([Guid]::NewGuid().ToString()),
                    "--description", "Temporary SiteTrace workshop endpoint",
                    "--tags",
                    "Project=SiteTrace,Lifecycle=WorkshopTemporary",
                    "--output", "json"
                ))
            }
        }

        $endpoint = Wait-EndpointReady -RuntimeId $runtime.agentRuntimeId
        Write-Host "AgentCore CodeZip deployment is ready."
        Write-Host "  Runtime ID : $($runtime.agentRuntimeId)"
        Write-Host "  Version    : $($runtime.agentRuntimeVersion)"
        Write-Host "  Endpoint   : $($endpoint.name)"
        Write-Host "  Artifact   : s3://$resolvedBucket/$artifactKey"
        Write-Host "  ZIP bytes  : $($package.CompressedBytes)"
        Write-Host ""
        Write-Host "Cleanup after the workshop:"
        Write-Host (
            "  .\infra\aws\deploy-agentcore.ps1 " +
            "-Action Cleanup -ConfirmCleanup"
        )
        Write-Host (
            "The S3 bucket and provider credentials are deliberately retained."
        )
    }
    finally {
        foreach ($path in $temporaryFiles) {
            if (Test-Path -LiteralPath $path -PathType Leaf) {
                Remove-Item -LiteralPath $path -Force `
                    -WhatIf:$false -Confirm:$false
            }
        }
        Remove-TrackedTemporaryDirectories `
            -TrackedDirectories $temporaryDirectories
    }
}

function Deploy-AgentCore {
    if ($DeploymentMode -eq "CodeZip") {
        Deploy-AgentCoreCodeZip
        return
    }

    Assert-DeploymentInputs
    $temporaryFiles = [System.Collections.Generic.List[string]]::new()

    try {
        $accountId = Invoke-AwsText -Arguments @(
            "sts", "get-caller-identity",
            "--query", "Account",
            "--output", "text"
        )
        $ecrHost = "$accountId.dkr.ecr.$Region.amazonaws.com"

        $repository = Invoke-AwsText -Arguments @(
            "ecr", "describe-repositories",
            "--repository-names", $EcrRepository,
            "--output", "json"
        ) -AllowFailure
        if (-not $repository) {
            if ($PSCmdlet.ShouldProcess(
                "$EcrRepository in $Region",
                "Create encrypted ECR repository"
            )) {
                [void](Invoke-AwsText -Arguments @(
                    "ecr", "create-repository",
                    "--repository-name", $EcrRepository,
                    "--image-tag-mutability", "IMMUTABLE",
                    "--image-scanning-configuration", "scanOnPush=true",
                    "--encryption-configuration", "encryptionType=AES256",
                    "--tags",
                    "Key=Project,Value=SiteTrace",
                    "Key=Lifecycle,Value=WorkshopTemporary",
                    "--output", "json"
                ))
            }
        }

        if (-not $ImageTag) {
            $ImageTag = [DateTime]::UtcNow.ToString("yyyyMMddHHmmss")
        }
        if ($ImageTag -notmatch "^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$") {
            throw "-ImageTag is not a valid container tag."
        }
        $imageUri = "$ecrHost/${EcrRepository}:$ImageTag"

        if ($PSCmdlet.ShouldProcess($imageUri, "Build and push ARM64 image")) {
            $loginPassword = & $script:AwsExecutable ecr get-login-password `
                --profile $Profile `
                --region $Region `
                --no-cli-pager
            if ($LASTEXITCODE -ne 0 -or -not $loginPassword) {
                throw "Unable to obtain the temporary ECR login token."
            }
            try {
                $loginPassword | & docker login `
                    --username AWS `
                    --password-stdin $ecrHost
                if ($LASTEXITCODE -ne 0) {
                    throw "Docker could not authenticate to ECR."
                }
            }
            finally {
                $loginPassword = $null
            }

            & docker buildx build `
                --platform linux/arm64 `
                --file $dockerfile `
                --tag $imageUri `
                --push `
                $backendDirectory
            if ($LASTEXITCODE -ne 0) {
                throw "The linux/arm64 container build or push failed."
            }
        }

        $environmentVariables = [ordered]@{
            AWS_REGION = $Region
            APP_ENVIRONMENT = "workshop"
            SITETRACE_SESSION_PREFIX = "sitetrace/sessions/"
            SITETRACE_OPENAI_SECRET_ID = $OpenAISecretId
            SITETRACE_TWELVELABS_SECRET_ID = $TwelveLabsSecretId
            SITETRACE_NEO4J_SECRET_ID = $Neo4jSecretId
        }
        if ($DataBucket) {
            $environmentVariables.SITETRACE_S3_BUCKET = $DataBucket
            $environmentVariables.SITETRACE_SESSION_BUCKET = $DataBucket
        }

        $runtimeConfiguration = [ordered]@{
            agentRuntimeArtifact = @{
                containerConfiguration = @{ containerUri = $imageUri }
            }
            roleArn = $RoleArn
            networkConfiguration = @{ networkMode = "PUBLIC" }
            protocolConfiguration = @{ serverProtocol = "HTTP" }
            lifecycleConfiguration = @{
                idleRuntimeSessionTimeout = 900
                maxLifetime = 28800
            }
            environmentVariables = $environmentVariables
            description = "Temporary SiteTrace workshop evidence investigation runtime"
        }

        $runtime = Get-Runtime
        if ($runtime) {
            $updateRequest = [ordered]@{
                agentRuntimeId = $runtime.agentRuntimeId
            } + $runtimeConfiguration
            $requestPath = Save-TemporaryJson `
                -Value $updateRequest `
                -TrackedFiles $temporaryFiles
            if ($PSCmdlet.ShouldProcess(
                $runtime.agentRuntimeId,
                "Update AgentCore Runtime to $imageUri"
            )) {
                $raw = Invoke-AwsText -Arguments @(
                    "bedrock-agentcore-control", "update-agent-runtime",
                    "--cli-input-json", "file://$requestPath",
                    "--output", "json"
                )
                $runtime = $raw | ConvertFrom-Json
            }
        }
        else {
            $createRequest = [ordered]@{
                agentRuntimeName = $RuntimeName
                clientToken = ([Guid]::NewGuid().ToString())
                tags = @{
                    Project = "SiteTrace"
                    Lifecycle = "WorkshopTemporary"
                }
            } + $runtimeConfiguration
            $requestPath = Save-TemporaryJson `
                -Value $createRequest `
                -TrackedFiles $temporaryFiles
            if ($PSCmdlet.ShouldProcess(
                $RuntimeName,
                "Create AgentCore Runtime for $imageUri"
            )) {
                $raw = Invoke-AwsText -Arguments @(
                    "bedrock-agentcore-control", "create-agent-runtime",
                    "--cli-input-json", "file://$requestPath",
                    "--output", "json"
                )
                $runtime = $raw | ConvertFrom-Json
            }
        }

        if (-not $runtime -or -not $runtime.agentRuntimeId) {
            Write-Host "WhatIf complete; no runtime was changed."
            return
        }

        $runtime = Wait-RuntimeReady -RuntimeId $runtime.agentRuntimeId
        $endpoint = Get-Endpoint -RuntimeId $runtime.agentRuntimeId
        if ($endpoint) {
            if ($PSCmdlet.ShouldProcess(
                "$($runtime.agentRuntimeId)/$EndpointName",
                "Point endpoint at runtime version $($runtime.agentRuntimeVersion)"
            )) {
                [void](Invoke-AwsText -Arguments @(
                    "bedrock-agentcore-control",
                    "update-agent-runtime-endpoint",
                    "--agent-runtime-id", $runtime.agentRuntimeId,
                    "--endpoint-name", $EndpointName,
                    "--agent-runtime-version", $runtime.agentRuntimeVersion,
                    "--client-token", ([Guid]::NewGuid().ToString()),
                    "--description", "Temporary SiteTrace workshop endpoint",
                    "--output", "json"
                ))
            }
        }
        else {
            if ($PSCmdlet.ShouldProcess(
                "$($runtime.agentRuntimeId)/$EndpointName",
                "Create endpoint for runtime version $($runtime.agentRuntimeVersion)"
            )) {
                [void](Invoke-AwsText -Arguments @(
                    "bedrock-agentcore-control",
                    "create-agent-runtime-endpoint",
                    "--agent-runtime-id", $runtime.agentRuntimeId,
                    "--name", $EndpointName,
                    "--agent-runtime-version", $runtime.agentRuntimeVersion,
                    "--client-token", ([Guid]::NewGuid().ToString()),
                    "--description", "Temporary SiteTrace workshop endpoint",
                    "--tags",
                    "Project=SiteTrace,Lifecycle=WorkshopTemporary",
                    "--output", "json"
                ))
            }
        }

        if (-not $WhatIfPreference) {
            $endpoint = Wait-EndpointReady -RuntimeId $runtime.agentRuntimeId
            Write-Host "AgentCore deployment is ready."
            Write-Host "  Runtime ID : $($runtime.agentRuntimeId)"
            Write-Host "  Version    : $($runtime.agentRuntimeVersion)"
            Write-Host "  Endpoint   : $($endpoint.name)"
            Write-Host ""
            Write-Host "Cleanup after the workshop:"
            Write-Host "  .\infra\aws\deploy-agentcore.ps1 -Action Cleanup -ConfirmCleanup"
            Write-Host "The S3 data bucket and Secrets Manager secrets are deliberately retained."
        }
    }
    finally {
        foreach ($path in $temporaryFiles) {
            if (Test-Path -LiteralPath $path) {
                Remove-Item -LiteralPath $path -Force `
                    -WhatIf:$false -Confirm:$false
            }
        }
    }
}

function Remove-AgentCoreDeployment {
    Assert-Command "aws"
    [void](Invoke-AwsText -Arguments @(
        "sts", "get-caller-identity",
        "--query", "Account",
        "--output", "text"
    ))

    if (-not $ConfirmCleanup -and -not $WhatIfPreference) {
        throw "Cleanup requires -ConfirmCleanup (or use -WhatIf)."
    }

    $runtime = Get-Runtime
    if ($runtime) {
        $endpoint = Get-Endpoint -RuntimeId $runtime.agentRuntimeId
        if ($endpoint -and $PSCmdlet.ShouldProcess(
            "$($runtime.agentRuntimeId)/$EndpointName",
            "Delete AgentCore endpoint"
        )) {
            [void](Invoke-AwsText -Arguments @(
                "bedrock-agentcore-control",
                "delete-agent-runtime-endpoint",
                "--agent-runtime-id", $runtime.agentRuntimeId,
                "--endpoint-name", $EndpointName,
                "--client-token", ([Guid]::NewGuid().ToString()),
                "--output", "json"
            ))
            Wait-EndpointDeleted -RuntimeId $runtime.agentRuntimeId
        }

        if ($PSCmdlet.ShouldProcess(
            $runtime.agentRuntimeId,
            "Delete AgentCore Runtime"
        )) {
            [void](Invoke-AwsText -Arguments @(
                "bedrock-agentcore-control", "delete-agent-runtime",
                "--agent-runtime-id", $runtime.agentRuntimeId,
                "--client-token", ([Guid]::NewGuid().ToString()),
                "--output", "json"
            ))
        }
    }
    else {
        Write-Host "No AgentCore Runtime named '$RuntimeName' was found."
    }

    if ($RemoveImageRepository -and $PSCmdlet.ShouldProcess(
        $EcrRepository,
        "Delete ECR repository and all images"
    )) {
        [void](Invoke-AwsText -Arguments @(
            "ecr", "delete-repository",
            "--repository-name", $EcrRepository,
            "--force",
            "--output", "json"
        ))
    }

    Write-Host "AgentCore cleanup submitted."
    Write-Host "S3 data and Secrets Manager secrets were not deleted."
}

if ($DryRun -or $Action -eq "Plan") {
    Write-Plan
    return
}

switch ($Action) {
    "ValidatePackage" { Test-CodeZipPackage }
    "Deploy" { Deploy-AgentCore }
    "Cleanup" { Remove-AgentCoreDeployment }
}
