<#
.SYNOPSIS
Deploys the SiteTrace web app to a temporary AWS Amplify Hosting app.

.DESCRIPTION
The default Plan action is read-only. Deploy creates or updates a Git-connected
Amplify WEB_COMPUTE app and starts a build of the selected branch. The build
uses amplify.yml and the standard Next.js output.

When a new Amplify app must be connected to GitHub, supply a token through the
SITETRACE_GITHUB_ACCESS_TOKEN/GITHUB_ACCESS_TOKEN process environment or use
-GitHubTokenSecretId. The token is never printed and is not committed.

.EXAMPLE
.\infra\aws\deploy-frontend.ps1

.EXAMPLE
$env:SITETRACE_GITHUB_ACCESS_TOKEN = "<temporary-token>"
.\infra\aws\deploy-frontend.ps1 -Action Deploy `
  -ApiUrl https://api.example.invalid

.EXAMPLE
.\infra\aws\deploy-frontend.ps1 -Action Deploy `
  -ApiUrl https://api.example.invalid `
  -GitHubTokenSecretId sitetrace/github

.EXAMPLE
.\infra\aws\deploy-frontend.ps1 -Action Cleanup -ConfirmCleanup
#>

[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [ValidateSet("Plan", "Deploy", "Cleanup")]
    [string]$Action = "Plan",

    [ValidatePattern("^[A-Za-z0-9_-]+$")]
    [string]$Profile = "sitetrace-workshop",

    [ValidatePattern("^[a-z]{2}-[a-z]+-[0-9]$")]
    [string]$Region = "us-east-1",

    [ValidatePattern("^[A-Za-z0-9-]{1,255}$")]
    [string]$AppName = "sitetrace-workshop",

    [string]$BranchName,

    [string]$AppId,
    [string]$RepositoryUrl = "https://github.com/joonghui0926/SiteTrace",
    [string]$ApiUrl = $env:NEXT_PUBLIC_SITETRACE_API_URL,
    [string]$GitHubTokenSecretId,

    [switch]$DryRun,
    [switch]$ConfirmCleanup,
    [switch]$WaitForBuild
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$env:AWS_PAGER = ""

$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$buildSpecPath = Join-Path $repositoryRoot "amplify.yml"
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

if (-not $BranchName) {
    $git = Get-Command "git" -ErrorAction SilentlyContinue
    if ($git) {
        $detectedBranch = & git -C $repositoryRoot branch --show-current 2>$null
        if ($LASTEXITCODE -eq 0 -and $detectedBranch) {
            $BranchName = ($detectedBranch | Select-Object -First 1).Trim()
        }
    }
    if (-not $BranchName) {
        $BranchName = "main"
    }
}
if ($BranchName -notmatch "^[A-Za-z0-9._/-]{1,255}$") {
    throw "-BranchName contains unsupported characters."
}

function Write-Plan {
    Write-Host "SiteTrace Amplify temporary deployment plan"
    Write-Host "  AWS CLI            : $(
        if ($script:AwsExecutable) { $script:AwsExecutable } else { "not found" }
    )"
    Write-Host "  AWS profile/region : $Profile / $Region"
    Write-Host "  App/branch         : $AppName / $BranchName"
    Write-Host "  Repository         : $RepositoryUrl"
    Write-Host "  Platform           : Amplify WEB_COMPUTE (Next.js)"
    if ($ApiUrl) {
        Write-Host "  SiteTrace API URL  : configured"
    }
    else {
        Write-Host "  SiteTrace API URL  : required for deployment"
    }
    Write-Host ""
    Write-Host "Plan is read-only. Use -Action Deploy to apply it."
    Write-Host "Use -WhatIf with Deploy or Cleanup to inspect mutations."
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

function Get-AmplifyApp {
    if ($AppId) {
        $raw = Invoke-AwsText -Arguments @(
            "amplify", "get-app",
            "--app-id", $AppId,
            "--output", "json"
        )
        return ($raw | ConvertFrom-Json).app
    }

    $raw = Invoke-AwsText -Arguments @(
        "amplify", "list-apps",
        "--max-items", "100",
        "--output", "json"
    )
    $matches = @(
        (($raw | ConvertFrom-Json).apps) |
            Where-Object { $_.name -eq $AppName }
    )
    if ($matches.Count -gt 1) {
        throw "More than one Amplify app is named '$AppName'; pass -AppId."
    }
    if ($matches.Count -eq 1) {
        return $matches[0]
    }
    return $null
}

function Get-GitHubToken {
    $token = $env:SITETRACE_GITHUB_ACCESS_TOKEN
    if (-not $token) {
        $token = $env:GITHUB_ACCESS_TOKEN
    }
    if ($token) {
        return $token.Trim()
    }
    if (-not $GitHubTokenSecretId) {
        return $null
    }

    $secret = Invoke-AwsText -Arguments @(
        "secretsmanager", "get-secret-value",
        "--secret-id", $GitHubTokenSecretId,
        "--query", "SecretString",
        "--output", "text"
    )
    try {
        $json = $secret | ConvertFrom-Json
        foreach ($name in @(
            "GITHUB_ACCESS_TOKEN",
            "github_access_token",
            "access_token",
            "token",
            "value"
        )) {
            if ($json.PSObject.Properties.Name -contains $name) {
                return [string]$json.$name
            }
        }
    }
    catch {
        return $secret.Trim()
    }
    throw "The GitHub token secret is JSON but contains no recognized token key."
}

function Get-AmplifyBranch {
    param([Parameter(Mandatory = $true)][string]$ResolvedAppId)

    $raw = Invoke-AwsText -Arguments @(
        "amplify", "get-branch",
        "--app-id", $ResolvedAppId,
        "--branch-name", $BranchName,
        "--output", "json"
    ) -AllowFailure
    if (-not $raw) {
        return $null
    }
    return ($raw | ConvertFrom-Json).branch
}

function Wait-AmplifyBuild {
    param(
        [Parameter(Mandatory = $true)][string]$ResolvedAppId,
        [Parameter(Mandatory = $true)][string]$JobId
    )

    for ($attempt = 1; $attempt -le 120; $attempt += 1) {
        $raw = Invoke-AwsText -Arguments @(
            "amplify", "get-job",
            "--app-id", $ResolvedAppId,
            "--branch-name", $BranchName,
            "--job-id", $JobId,
            "--output", "json"
        )
        $job = ($raw | ConvertFrom-Json).job
        $status = [string]$job.summary.status
        if ($status -eq "SUCCEED") {
            return $job
        }
        if ($status -in @("FAILED", "CANCELLED")) {
            throw "Amplify build $JobId ended with status $status."
        }
        Start-Sleep -Seconds 5
    }
    throw "Timed out waiting for Amplify build $JobId."
}

function Assert-DeploymentInputs {
    Assert-Command "aws"
    if (-not (Test-Path -LiteralPath $buildSpecPath -PathType Leaf)) {
        throw "Amplify build specification not found at '$buildSpecPath'."
    }
    if (-not $ApiUrl) {
        throw "-ApiUrl (or NEXT_PUBLIC_SITETRACE_API_URL) is required."
    }
    $parsedApiUrl = $null
    if (
        -not [Uri]::TryCreate(
            $ApiUrl,
            [UriKind]::Absolute,
            [ref]$parsedApiUrl
        ) -or
        $parsedApiUrl.Scheme -notin @("http", "https")
    ) {
        throw "-ApiUrl must be an absolute HTTP(S) URL."
    }
    if ($RepositoryUrl -notmatch "^https://github\.com/[^/]+/[^/]+/?$") {
        throw "-RepositoryUrl must be an HTTPS GitHub repository URL."
    }
    [void](Invoke-AwsText -Arguments @(
        "sts", "get-caller-identity",
        "--query", "Account",
        "--output", "text"
    ))
}

function Deploy-AmplifyFrontend {
    Assert-DeploymentInputs
    $temporaryFiles = [System.Collections.Generic.List[string]]::new()
    $githubToken = $null

    try {
        $buildSpec = [System.IO.File]::ReadAllText($buildSpecPath)
        $environmentVariables = [ordered]@{
            NEXT_PUBLIC_SITETRACE_API_URL = $ApiUrl.TrimEnd("/")
            NEXT_PUBLIC_SITE_URL = "https://$BranchName.pending.amplifyapp.com"
            NODE_VERSION = "22"
        }

        $app = Get-AmplifyApp
        if (-not $app) {
            $githubToken = Get-GitHubToken
            if (-not $githubToken) {
                throw (
                    "Creating a Git-connected Amplify app requires " +
                    "SITETRACE_GITHUB_ACCESS_TOKEN, GITHUB_ACCESS_TOKEN, " +
                    "or -GitHubTokenSecretId."
                )
            }
            $createRequest = [ordered]@{
                name = $AppName
                description = "Temporary SiteTrace workshop web application"
                repository = $RepositoryUrl.TrimEnd("/")
                accessToken = $githubToken
                platform = "WEB_COMPUTE"
                enableBranchAutoBuild = $true
                enableBranchAutoDeletion = $true
                buildSpec = $buildSpec
                environmentVariables = $environmentVariables
                tags = @{
                    Project = "SiteTrace"
                    Lifecycle = "WorkshopTemporary"
                }
            }
            $requestPath = Save-TemporaryJson `
                -Value $createRequest `
                -TrackedFiles $temporaryFiles
            if ($PSCmdlet.ShouldProcess(
                $AppName,
                "Create Git-connected Amplify WEB_COMPUTE app"
            )) {
                $raw = Invoke-AwsText -Arguments @(
                    "amplify", "create-app",
                    "--cli-input-json", "file://$requestPath",
                    "--output", "json"
                )
                $app = ($raw | ConvertFrom-Json).app
            }
        }
        else {
            if (
                $app.repository -and
                $app.repository.TrimEnd("/") -ne $RepositoryUrl.TrimEnd("/")
            ) {
                throw (
                    "Amplify app '$($app.appId)' is connected to a different " +
                    "repository. Pass a different -AppId or -AppName."
                )
            }
            $updateRequest = [ordered]@{
                appId = $app.appId
                name = $app.name
                description = "Temporary SiteTrace workshop web application"
                platform = "WEB_COMPUTE"
                enableBranchAutoBuild = $true
                enableBranchAutoDeletion = $true
                buildSpec = $buildSpec
                environmentVariables = $environmentVariables
            }
            $requestPath = Save-TemporaryJson `
                -Value $updateRequest `
                -TrackedFiles $temporaryFiles
            if ($PSCmdlet.ShouldProcess(
                $app.appId,
                "Update Amplify build and public environment"
            )) {
                $raw = Invoke-AwsText -Arguments @(
                    "amplify", "update-app",
                    "--cli-input-json", "file://$requestPath",
                    "--output", "json"
                )
                $app = ($raw | ConvertFrom-Json).app
            }
        }

        if (-not $app -or -not $app.appId) {
            Write-Host "WhatIf complete; no Amplify app was changed."
            return
        }

        # Correct the canonical site URL now that the AWS-generated domain is
        # known. It is public build configuration, not a credential.
        $environmentVariables.NEXT_PUBLIC_SITE_URL = (
            "https://$BranchName.$($app.defaultDomain)"
        )
        $branch = Get-AmplifyBranch -ResolvedAppId $app.appId
        if ($branch) {
            $branchRequest = [ordered]@{
                appId = $app.appId
                branchName = $BranchName
                description = "Temporary SiteTrace workshop branch"
                stage = "BETA"
                framework = "Next.js - SSR"
                enableAutoBuild = $true
                enableSkewProtection = $true
                environmentVariables = $environmentVariables
                buildSpec = $buildSpec
            }
            $requestPath = Save-TemporaryJson `
                -Value $branchRequest `
                -TrackedFiles $temporaryFiles
            if ($PSCmdlet.ShouldProcess(
                "$($app.appId)/$BranchName",
                "Update Amplify branch"
            )) {
                [void](Invoke-AwsText -Arguments @(
                    "amplify", "update-branch",
                    "--cli-input-json", "file://$requestPath",
                    "--output", "json"
                ))
            }
        }
        else {
            $branchRequest = [ordered]@{
                appId = $app.appId
                branchName = $BranchName
                description = "Temporary SiteTrace workshop branch"
                stage = "BETA"
                framework = "Next.js - SSR"
                enableAutoBuild = $true
                enableSkewProtection = $true
                environmentVariables = $environmentVariables
                buildSpec = $buildSpec
                tags = @{
                    Project = "SiteTrace"
                    Lifecycle = "WorkshopTemporary"
                }
            }
            $requestPath = Save-TemporaryJson `
                -Value $branchRequest `
                -TrackedFiles $temporaryFiles
            if ($PSCmdlet.ShouldProcess(
                "$($app.appId)/$BranchName",
                "Create Amplify branch"
            )) {
                [void](Invoke-AwsText -Arguments @(
                    "amplify", "create-branch",
                    "--cli-input-json", "file://$requestPath",
                    "--output", "json"
                ))
            }
        }

        $job = $null
        if ($PSCmdlet.ShouldProcess(
            "$($app.appId)/$BranchName",
            "Start Amplify RELEASE build from latest pushed commit"
        )) {
            $raw = Invoke-AwsText -Arguments @(
                "amplify", "start-job",
                "--app-id", $app.appId,
                "--branch-name", $BranchName,
                "--job-type", "RELEASE",
                "--job-reason", "SiteTrace workshop deployment",
                "--output", "json"
            )
            $job = ($raw | ConvertFrom-Json).jobSummary
        }

        $siteUrl = "https://$BranchName.$($app.defaultDomain)"
        Write-Host "Amplify deployment started."
        Write-Host "  App ID : $($app.appId)"
        Write-Host "  URL    : $siteUrl"
        if ($job) {
            Write-Host "  Job ID : $($job.jobId)"
            if ($WaitForBuild) {
                [void](Wait-AmplifyBuild `
                    -ResolvedAppId $app.appId `
                    -JobId $job.jobId)
                Write-Host "Amplify build succeeded."
            }
        }
        Write-Host ""
        Write-Host "Cleanup after the workshop:"
        Write-Host "  .\infra\aws\deploy-frontend.ps1 -Action Cleanup -ConfirmCleanup"
        Write-Host "GitHub source and AgentCore resources are not removed by this script."
    }
    finally {
        $githubToken = $null
        foreach ($path in $temporaryFiles) {
            if (Test-Path -LiteralPath $path) {
                Remove-Item -LiteralPath $path -Force
            }
        }
    }
}

function Remove-AmplifyFrontend {
    Assert-Command "aws"
    [void](Invoke-AwsText -Arguments @(
        "sts", "get-caller-identity",
        "--query", "Account",
        "--output", "text"
    ))

    if (-not $ConfirmCleanup -and -not $WhatIfPreference) {
        throw "Cleanup requires -ConfirmCleanup (or use -WhatIf)."
    }

    $app = Get-AmplifyApp
    if (-not $app) {
        Write-Host "No Amplify app named '$AppName' was found."
        return
    }
    if ($PSCmdlet.ShouldProcess($app.appId, "Delete Amplify app and branches")) {
        [void](Invoke-AwsText -Arguments @(
            "amplify", "delete-app",
            "--app-id", $app.appId,
            "--output", "json"
        ))
    }
    Write-Host "Amplify cleanup submitted for '$($app.appId)'."
    Write-Host "GitHub source and AgentCore resources were not changed."
}

if ($DryRun -or $Action -eq "Plan") {
    Write-Plan
    return
}

switch ($Action) {
    "Deploy" { Deploy-AmplifyFrontend }
    "Cleanup" { Remove-AmplifyFrontend }
}
