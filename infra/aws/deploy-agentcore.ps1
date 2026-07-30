<#
.SYNOPSIS
Builds and deploys the temporary SiteTrace AgentCore runtime.

.DESCRIPTION
The default action is Plan and performs no AWS or Docker mutation. Deploy uses
the temporary AWS CLI profile "sitetrace-workshop" in us-east-1, pushes a
linux/arm64 image to ECR, and creates or updates one HTTP AgentCore Runtime.

Provider credentials are never read by this script. The runtime receives only
Secrets Manager secret IDs; backend/agentcore_entrypoint.py retrieves the
secret values with the runtime execution role.

.EXAMPLE
.\infra\aws\deploy-agentcore.ps1

.EXAMPLE
.\infra\aws\deploy-agentcore.ps1 -Action Deploy `
  -RoleArn arn:aws:iam::123456789012:role/SiteTraceAgentCoreExecutionRole `
  -DataBucket sitetrace-workshop-123456789012

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
    [ValidateSet("Plan", "Deploy", "Cleanup")]
    [string]$Action = "Plan",

    [ValidatePattern("^[A-Za-z0-9_-]+$")]
    [string]$Profile = "sitetrace-workshop",

    [ValidatePattern("^[a-z]{2}-[a-z]+-[0-9]$")]
    [string]$Region = "us-east-1",

    [ValidatePattern("^[a-z0-9]+(?:[._/-][a-z0-9]+)*$")]
    [string]$EcrRepository = "sitetrace-agent",

    [ValidatePattern("^[A-Za-z][A-Za-z0-9_]{0,47}$")]
    [string]$RuntimeName = "SiteTraceWorkshop",

    [ValidatePattern("^[A-Za-z][A-Za-z0-9_]{0,47}$")]
    [string]$EndpointName = "Workshop",

    [string]$RoleArn,
    [string]$DataBucket,
    [string]$ImageTag,

    [string]$OpenAISecretId = "sitetrace/openai",
    [string]$TwelveLabsSecretId = "sitetrace/twelvelabs",
    [string]$Neo4jSecretId = "sitetrace/neo4j",

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

function Write-Plan {
    Write-Host "SiteTrace AgentCore temporary deployment plan"
    Write-Host "  AWS profile/region : $Profile / $Region"
    Write-Host "  ECR repository     : $EcrRepository"
    Write-Host "  Runtime/endpoint   : $RuntimeName / $EndpointName"
    Write-Host "  Container          : linux/arm64, port 8080, HTTP"
    Write-Host "  Secret references  : OpenAI, TwelveLabs, Neo4j (values are never printed)"
    if ($DataBucket) {
        Write-Host "  Existing data bucket: $DataBucket"
    }
    else {
        Write-Host "  Existing data bucket: not configured"
    }
    Write-Host ""
    Write-Host "Plan is read-only. Use -Action Deploy with -RoleArn to apply it."
    Write-Host "Use -WhatIf with Deploy or Cleanup to inspect mutations."
}

function Assert-Command {
    param([Parameter(Mandatory = $true)][string]$Name)
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
    $output = & aws @allArguments 2>&1
    $exitCode = $LASTEXITCODE
    $text = ($output | Out-String).Trim()
    if ($exitCode -ne 0 -and -not $AllowFailure) {
        throw "AWS CLI failed (exit $exitCode): $text"
    }
    if ($exitCode -ne 0) {
        return $null
    }
    return $text
}

function Save-TemporaryJson {
    param(
        [Parameter(Mandatory = $true)]$Value,
        [Parameter(Mandatory = $true)]
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

function Assert-DeploymentInputs {
    Assert-Command "aws"
    Assert-Command "docker"

    if (-not (Test-Path -LiteralPath $dockerfile -PathType Leaf)) {
        throw "Backend Dockerfile was not found at '$dockerfile'."
    }
    if (-not $RoleArn) {
        throw "-RoleArn is required for -Action Deploy."
    }
    if ($RoleArn -notmatch "^arn:aws(?:-[^:]+)?:iam::[0-9]{12}:role/.+$") {
        throw "-RoleArn is not a valid IAM role ARN."
    }
    if ($DataBucket -and $DataBucket -notmatch "^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$") {
        throw "-DataBucket is not a valid S3 bucket name."
    }

    [void](Invoke-AwsText -Arguments @(
        "sts", "get-caller-identity",
        "--query", "Account",
        "--output", "text"
    ))
    foreach ($secretId in @(
        $OpenAISecretId,
        $TwelveLabsSecretId,
        $Neo4jSecretId
    )) {
        if ($secretId) {
            [void](Invoke-AwsText -Arguments @(
                "secretsmanager", "describe-secret",
                "--secret-id", $secretId,
                "--output", "json"
            ))
        }
    }
    if ($DataBucket) {
        [void](Invoke-AwsText -Arguments @(
            "s3api", "head-bucket",
            "--bucket", $DataBucket
        ))
    }
}

function Deploy-AgentCore {
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
            $loginPassword = & aws ecr get-login-password `
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
            Write-Host "  Endpoint   : $($endpoint.endpointName)"
            Write-Host ""
            Write-Host "Cleanup after the workshop:"
            Write-Host "  .\infra\aws\deploy-agentcore.ps1 -Action Cleanup -ConfirmCleanup"
            Write-Host "The S3 data bucket and Secrets Manager secrets are deliberately retained."
        }
    }
    finally {
        foreach ($path in $temporaryFiles) {
            if (Test-Path -LiteralPath $path) {
                Remove-Item -LiteralPath $path -Force
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
            Start-Sleep -Seconds 5
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
    "Deploy" { Deploy-AgentCore }
    "Cleanup" { Remove-AgentCoreDeployment }
}
