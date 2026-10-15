# ============================================================================
# Offline install module -- executable behavior tests
# ============================================================================
# Run from a PowerShell prompt:
#
#   pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/tests/test-install-ps1-offline-module.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/tests/test-install-ps1-offline-module.ps1
#
# This replaces the previous "behavioral witness" script, which recorded a
# made-up `expected` value written by the test author instead of exercising the
# module. A witness whose expectations are authored by the code under test
# proves nothing. Every check below either invokes the real module function and
# asserts an observed outcome, or fails.
#
# The bundle tests run against a LOCAL fixture repository, so this is
# network-free and runs on Windows PowerShell 5.1 and PowerShell 7 alike.
# ============================================================================

$ErrorActionPreference = "Stop"

# This test lives in scripts/tests/, so step back to the repository root the
# same way the sibling installer tests do.
$repoRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path))
$scriptsDir = Join-Path $repoRoot "scripts"

$script:Failures = 0
$script:Checks = 0

function Assert-True {
    param([bool]$Condition, [string]$Label)
    $script:Checks++
    if ($Condition) {
        Write-Host "  ok   $Label"
    } else {
        Write-Host "  FAIL $Label" -ForegroundColor Red
        $script:Failures++
    }
}

function Assert-Equal {
    param($Expected, $Actual, [string]$Label)
    $script:Checks++
    if ($Expected -eq $Actual) {
        Write-Host "  ok   $Label"
    } else {
        Write-Host "  FAIL $Label" -ForegroundColor Red
        Write-Host "         expected: $Expected"
        Write-Host "         actual:   $Actual"
        $script:Failures++
    }
}

# --- Parse gate -------------------------------------------------------------
# install.ps1 dot-sources this module. A construct that only parses under
# PowerShell 7 (or a bad variable interpolation) breaks the WHOLE installer at
# load time, so parse first and dot-source in a fresh scope.
Write-Host "parse gate"
foreach ($file in @("install-offline.ps1", "install.ps1")) {
    $path = Join-Path $scriptsDir $file
    $errors = $null
    $tokens = $null
    [System.Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$errors) | Out-Null
    Assert-Equal 0 $errors.Count "$file parses with zero errors"
    foreach ($parseError in $errors) {
        Write-Host "         L$($parseError.Extent.StartLineNumber): $($parseError.Message)" -ForegroundColor Red
    }
}

# --- Module load gate -------------------------------------------------------
Write-Host "module load gate"
. (Join-Path $scriptsDir "install-offline.ps1")
Assert-True ([bool](Get-Command Invoke-OfflineBundleCreate -ErrorAction SilentlyContinue)) "Invoke-OfflineBundleCreate is available after dot-sourcing"
Assert-True ([bool](Get-Command Install-RepositoryFromBundle -ErrorAction SilentlyContinue)) "Install-RepositoryFromBundle is available after dot-sourcing"

# --- Local fixture repository ----------------------------------------------
$work = Join-Path ([System.IO.Path]::GetTempPath()) ("hermes-offline-tests-" + (Get-Random))
$src = Join-Path $work "src"
$media = Join-Path $work "media"
New-Item -ItemType Directory -Path $src -Force | Out-Null
New-Item -ItemType Directory -Path $media -Force | Out-Null

function Invoke-Git {
    param([string]$Repo, [string[]]$GitArgs)
    $output = & git -C $Repo @GitArgs 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "git $($GitArgs -join ' ') failed in ${Repo}: $output"
    }
    return $output
}

& git init -q -b main $src 2>&1 | Out-Null
& git -C $src config user.email "test@example.invalid" 2>&1 | Out-Null
& git -C $src config user.name "Offline Test" 2>&1 | Out-Null
Set-Content -Path (Join-Path $src "f.txt") -Value "one"
& git -C $src add . 2>&1 | Out-Null
& git -C $src commit -qm "c1" 2>&1 | Out-Null
& git -C $src checkout -qb feature 2>&1 | Out-Null
Set-Content -Path (Join-Path $src "f.txt") -Value "two"
& git -C $src commit -qam "c2" 2>&1 | Out-Null
& git -C $src tag -a v1.0 -m "annotated" 2>&1 | Out-Null
$featureOid = (Invoke-Git -Repo $src -GitArgs @("rev-parse", "feature")).Trim()
$peeledOid = (Invoke-Git -Repo $src -GitArgs @("rev-parse", "v1.0^{commit}")).Trim()
$rawTagOid = (Invoke-Git -Repo $src -GitArgs @("rev-parse", "v1.0")).Trim()

# Invoke-OfflineBundleCreate targets the public remote. Redirect that URL to the
# local fixture so the REAL function runs with no network. This is a git client
# setting, not a change to the code under test.
$fixtureUrl = "file://" + ($src -replace '\\', '/')
& git config --global "url.$fixtureUrl.insteadOf" "https://github.com/NousResearch/hermes-agent.git" 2>&1 | Out-Null

try {
    # --- Identity normalization ---------------------------------------------
    Write-Host "pin identity"
    Assert-Equal $featureOid (Resolve-OfflineCommitOid -RepoPath $src -Branch feature) "branch pin resolves to the branch commit"
    Assert-Equal $peeledOid (Resolve-OfflineCommitOid -RepoPath $src -Tag v1.0) "annotated tag pin resolves to the PEELED commit"
    Assert-True ($peeledOid -ne $rawTagOid) "annotated tag object oid differs from its commit oid (peeling matters)"

    # --- Bundle producer / consumer -----------------------------------------
    Write-Host "bundle contract (branch pin)"
    $bundle = Invoke-OfflineBundleCreate -TargetDir $media -Branch feature
    Assert-Equal "String" $bundle.GetType().Name "producer returns a single path string, not an array"
    Assert-True (Test-Path $bundle) "bundle file exists"

    $consumer = Join-Path $work "consumer-branch"
    $ok = Install-RepositoryFromBundle -BundlePath $bundle -InstallDir $consumer -Branch feature
    Assert-True ($ok -eq $true) "consumer reports success"
    $consumerHead = (Invoke-Git -Repo $consumer -GitArgs @("rev-parse", "HEAD")).Trim()
    Assert-Equal $featureOid $consumerHead "consumer HEAD materializes the requested branch pin"

    Write-Host "bundle contract (annotated tag pin)"
    # Separate media dir: the producer writes the fixed name hermes-agent.bundle,
    # so a tag run into the same directory would overwrite the branch bundle.
    $tagMedia = Join-Path $work "media-tag"
    New-Item -ItemType Directory -Path $tagMedia -Force | Out-Null
    $tagBundle = Invoke-OfflineBundleCreate -TargetDir $tagMedia -Tag v1.0
    $tagConsumer = Join-Path $work "consumer-tag"
    $okTag = Install-RepositoryFromBundle -BundlePath $tagBundle -InstallDir $tagConsumer -Tag v1.0
    Assert-True ($okTag -eq $true) "tag consumer reports success"
    $tagHead = (Invoke-Git -Repo $tagConsumer -GitArgs @("rev-parse", "HEAD")).Trim()
    Assert-Equal $peeledOid $tagHead "consumer HEAD matches the peeled tag commit (not the tag object)"

    # --- Existing checkout preservation -------------------------------------
    Write-Host "existing checkout preservation"
    $sentinel = Join-Path $consumer "SENTINEL.txt"
    Set-Content -Path $sentinel -Value "keep me"
    Install-RepositoryFromBundle -BundlePath $bundle -InstallDir $consumer -Branch feature | Out-Null
    $parked = @(Get-ChildItem $work -Directory -Filter "consumer-branch.broken-*" -ErrorAction SilentlyContinue)
    Assert-True ($parked.Count -ge 1) "prior checkout is parked, not destroyed"
    $parkedSentinel = $false
    foreach ($dir in $parked) {
        if (Test-Path (Join-Path $dir.FullName "SENTINEL.txt")) { $parkedSentinel = $true }
    }
    Assert-True $parkedSentinel "parked checkout retains its contents"

    # --- Invalid media ------------------------------------------------------
    Write-Host "invalid media"
    $missing = Join-Path $work "does-not-exist.bundle"
    $threw = $false
    try {
        Install-RepositoryFromBundle -BundlePath $missing -InstallDir (Join-Path $work "consumer-missing") | Out-Null
    } catch {
        $threw = $true
    }
    Assert-True $threw "a missing bundle fails closed instead of producing an empty checkout"

    # --- Hash helper --------------------------------------------------------
    Write-Host "asset hash contract"
    Assert-True ((Test-OfflineAssetHash -FilePath (Join-Path $src "f.txt") -ExpectedHash "") -eq $false) "absent expected hash fails closed"
    $realHash = (Get-FileHash (Join-Path $src "f.txt") -Algorithm SHA256).Hash
    Assert-True ((Test-OfflineAssetHash -FilePath (Join-Path $src "f.txt") -ExpectedHash $realHash) -eq $true) "matching hash passes"
    Assert-True ((Test-OfflineAssetHash -FilePath (Join-Path $src "f.txt") -ExpectedHash ("0" * 64)) -eq $false) "mismatching hash fails"
} finally {
    & git config --global --remove-section "url.$fixtureUrl" 2>&1 | Out-Null
    if (Test-Path $work) { Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue }
}

Write-Host ""
if ($script:Failures -gt 0) {
    Write-Host "$($script:Failures) of $($script:Checks) checks FAILED" -ForegroundColor Red
    exit 1
}
Write-Host "All $($script:Checks) offline module checks passed."
exit 0
