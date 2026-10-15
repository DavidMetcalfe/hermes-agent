# ============================================================================
# Hermes Offline Installation Module
# ============================================================================
# Extracted from scripts/install.ps1 -- bounded module for offline installation
# logic (bundle creation, bundle-based clone, verification, behavioral witnesses).
#
# This module is dot-sourced by install.ps1, so it must PARSE on Windows
# PowerShell 5.1 (the shell `irm | iex` lands in and the one the desktop
# bootstrap spawns). Constructs that only parse under PowerShell 7 --
# `ConvertFrom-Json -AsHashtable`, for example -- break the whole installer at
# load time, not at the call site. See
# scripts/tests/test-install-ps1-offline-module.ps1.
# ============================================================================

# Precede all module-level code with a strict mode guard so syntax errors are
# caught at load time rather than at invocation.
Set-StrictMode -Version Latest

# --- Behavioral witness helpers (BLOCKER 1 evidence) ---

function New-BehavioralWitness {
    param(
        [string]$Name,
        [string]$Path,
        [string]$Expected,
        [string]$Actual = "",
        [string]$Type = "assert"
    )
    $witness = @{
        witness_name = $Name
        type         = $Type
        path         = $Path
        expected     = $Expected
        actual       = $Actual
        timestamp    = (Get-Date -Format "o")
    }
    $witnessPath = Join-Path (Split-Path $Path -Parent) "behavioral-witness.json"
    $witnessDir = Split-Path $witnessPath -Parent
    if (-not (Test-Path $witnessDir)) {
        New-Item -ItemType Directory -Path $witnessDir -Force | Out-Null
    }
    $existing = @()
    if (Test-Path $witnessPath) {
        # NOTE: no -AsHashtable. It is PowerShell 6+, and this module is loaded
        # by install.ps1 under Windows PowerShell 5.1.
        try {
            $parsed = Get-Content $witnessPath -Raw | ConvertFrom-Json
            if ($null -ne $parsed) { $existing = @($parsed) }
        } catch { $existing = @() }
    }
    $existing = @($existing) + $witness
    $existing | ConvertTo-Json -Depth 5 | Set-Content -Path $witnessPath -Encoding UTF8
}

function Get-BehavioralWitness {
    param([string]$Name, [string]$Path = ".")
    $witnessPath = Join-Path (Split-Path $Path -Parent) "behavioral-witness.json"
    if (-not (Test-Path $witnessPath)) { return $null }
    try {
        $all = Get-Content $witnessPath -Raw | ConvertFrom-Json
        return ($all | Where-Object { $_.witness_name -eq $Name } | Select-Object -First 1)
    } catch { return $null }
}

# --- Bundle contract helpers ---

# Resolve a requested pin (Commit > Tag > Branch) to a single commit OID.
#
# This matters for identity checks. `git rev-parse refs/tags/<annotated-tag>`
# returns the TAG OBJECT oid, which is not the commit the working tree gets
# checked out at; `<tag>^{commit}` peels to the commit. Comparing a raw tag oid
# against HEAD therefore rejects a correct checkout.
function Resolve-OfflineCommitOid {
    param(
        [Parameter(Mandatory = $true)][string]$RepoPath,
        [string]$Commit = "",
        [string]$Tag = "",
        [string]$Branch = "main"
    )
    # `git -C ""` does NOT mean "current directory" -- it fails. A caller that
    # has already Push-Location'd into the repository passes "" to mean "here".
    if (-not $RepoPath) { $RepoPath = "." }
    if ($Commit) {
        $resolved = (git -C $RepoPath rev-parse --verify "${Commit}^{commit}" 2>$null | Select-Object -First 1)
        if ($LASTEXITCODE -ne 0 -or -not $resolved) {
            $resolved = (git -C $RepoPath rev-parse --verify $Commit 2>$null | Select-Object -First 1)
        }
        if ($resolved) { return $resolved.ToString().Trim() }
        return ""
    }
    if ($Tag) {
        $peeled = (git -C $RepoPath rev-parse --verify "refs/tags/${Tag}^{commit}" 2>$null | Select-Object -First 1)
        if ($peeled) { return $peeled.ToString().Trim() }
        return ""
    }
    $head = (git -C $RepoPath rev-parse --verify "refs/heads/$Branch^{commit}" 2>$null | Select-Object -First 1)
    if ($head) { return $head.ToString().Trim() }
    return ""
}

# Builds a git bundle from the remote repository honoring Commit > Tag > Branch.
#
# Contract:
#   * the bundle must carry a COMPLETE object graph -- a shallow fetch produces
#     a bundle that no empty consumer can clone (`remote did not send all
#     necessary objects`);
#   * verification must run with a repository in scope, because plain
#     `git bundle verify` from a non-repository cwd exits 1;
#   * a consumer clone only checks out a valid branch when the exported bare
#     repo's HEAD points at a ref the bundle actually advertises.
function Invoke-OfflineBundleCreate {
    param(
        [string]$TargetDir,
        [string]$Branch = "main",
        [string]$Commit = "",
        [string]$Tag = ""
    )
    $bundleName = "hermes-agent.bundle"
    $bundlePath = Join-Path $TargetDir $bundleName

    # Determine which ref to fetch, using the exact-commit precedence.
    $fetchRef = ""
    if ($Commit) {
        $fetchRef = $Commit
    } elseif ($Tag) {
        $fetchRef = "refs/tags/$Tag"
    } else {
        $fetchRef = "refs/heads/$Branch"
    }

    # Create a temporary bare repository to fetch from the network, then bundle it.
    # [System.IO.Path]::GetTempPath() covers $env:TEMP on Windows and $TMPDIR on
    # POSIX, so this module stays runnable off-Windows for its tests.
    $tempRepo = Join-Path ([System.IO.Path]::GetTempPath()) ("hermes-offline-bundle-repo-" + (Get-Random))
    try {
        if (Test-Path $tempRepo) { Remove-Item -Recurse -Force $tempRepo }
        git init --bare $tempRepo 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to initialize temporary bundle repository at $tempRepo"
        }
        $repoUrl = "https://github.com/NousResearch/hermes-agent.git"

        # Full fetch (no --depth). A shallow fetch leaves the bundle without the
        # parent objects a fresh clone needs to materialize the commit.
        git -C $tempRepo fetch --no-tags $repoUrl "+${fetchRef}:${fetchRef}" 2>$null
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to fetch $fetchRef for bundle creation (exit $LASTEXITCODE)"
        }

        # Make the advertised HEAD resolvable so `git clone <bundle>` checks out.
        #
        # The advertised HEAD must name a COMMIT, never a tag ref: pointing
        # HEAD at an annotated tag advertises the tag OBJECT
        # (`git bundle list-heads` shows it as HEAD), and a consumer clone then
        # dies with "trying to write non-commit object ... to branch 'HEAD'".
        if ($Commit) {
            git -C $tempRepo update-ref refs/heads/offline-pin $Commit 2>$null
            git -C $tempRepo symbolic-ref HEAD refs/heads/offline-pin 2>$null | Out-Null
        } else {
            # Branch and tag modes both advertise a commit on a scratch branch
            # so the clone materializes the requested pin.
            $pinOid = Resolve-OfflineCommitOid -RepoPath $tempRepo -Commit "" -Tag $Tag -Branch $Branch
            if (-not $pinOid) {
                throw "Could not resolve requested pin (tag '$Tag' / branch '$Branch') to a commit oid"
            }
            git -C $tempRepo update-ref refs/heads/offline-pin $pinOid 2>$null
            git -C $tempRepo symbolic-ref HEAD refs/heads/offline-pin 2>$null | Out-Null
        }

        # Bundle all fetched refs so the consumer can clone / fetch from it.
        git -C $tempRepo bundle create $bundlePath --all 2>$null
        if (-not (Test-Path $bundlePath)) {
            throw "Bundle creation did not produce $bundlePath"
        }

        # Verify bundle integrity (fail-closed witness). `git bundle verify`
        # requires a repository in scope, so run it via -C rather than relying
        # on the caller's (possibly non-repository) working directory.
        git -C $tempRepo bundle verify $bundlePath 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Bundle verification failed for $bundlePath"
        }

        # Prove an EMPTY consumer can actually materialize the pin before this
        # artifact is published. Header verification alone does not establish
        # object closure.
        $verifyRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("hermes-offline-consumer-" + (Get-Random))
        try {
            git clone --quiet $bundlePath $verifyRoot 2>$null
            if ($LASTEXITCODE -ne 0) {
                throw "Offline bundle cannot be cloned by an empty consumer (exit $LASTEXITCODE)"
            }
            $expectedOid = Resolve-OfflineCommitOid -RepoPath $tempRepo -Commit $Commit -Tag $Tag -Branch $Branch
            $clonedOid = (git -C $verifyRoot rev-parse --verify HEAD 2>$null | Select-Object -First 1)
            if (-not $clonedOid -or ($expectedOid -and $clonedOid.ToString().Trim() -ne $expectedOid)) {
                throw "Offline bundle clone HEAD ($clonedOid) does not match requested pin ($expectedOid)"
            }
        } finally {
            if (Test-Path $verifyRoot) { Remove-Item -Recurse -Force $verifyRoot -ErrorAction SilentlyContinue }
        }

        # Behavioral witness: bundle created with the correct contract.
        # Only $bundlePath may reach the success stream -- the caller assigns the
        # result to $bundlePath, and emitting the witness hashtable too would
        # make that an array whose [string] cast is garbage.
        $bundleHash = (Get-FileHash $bundlePath -Algorithm SHA256).Hash
        New-BehavioralWitness -Name "bundle-created" -Path $bundlePath `
            -Expected "$bundleName;hash=$bundleHash;ref=$fetchRef" | Out-Null
        return $bundlePath
    } finally {
        if (Test-Path $tempRepo) { Remove-Item -Recurse -Force $tempRepo -ErrorAction SilentlyContinue }
    }
}

# Resolve the local ref a branch checkout should start from.
#
# Cloning from a bundle does NOT create `origin/<branch>` tracking refs, so
# hardcoding `origin/$Branch` fails for the exact transport this module exists to
# support. Try the tracking ref, then the local branch the bundle checked out,
# then FETCH_HEAD (populated by the init+fetch fallback).
function Resolve-OfflineBranchStartPoint {
    param(
        [Parameter(Mandatory = $true)][string]$RepoPath,
        [Parameter(Mandatory = $true)][string]$Branch
    )
    # `git -C ""` does NOT mean "current directory" -- it fails. A caller that
    # has already Push-Location'd into the repository passes "" to mean "here".
    if (-not $RepoPath) { $RepoPath = "." }
    foreach ($candidate in @("refs/remotes/origin/$Branch", "refs/heads/$Branch")) {
        git -C $RepoPath rev-parse --verify --quiet $candidate 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { return $candidate }
    }
    git -C $RepoPath rev-parse --verify --quiet FETCH_HEAD 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { return "FETCH_HEAD" }
    return ""
}

# Clones from a bundle using the exact-commit contract precedence.
function Install-RepositoryFromBundle {
    param(
        [string]$BundlePath,
        [string]$InstallDir,
        [string]$Branch = "main",
        [string]$Commit = "",
        [string]$Tag = ""
    )
    if (-not (Test-Path $BundlePath)) {
        throw "Offline repository bundle not found: $BundlePath"
    }
    if (Test-Path $InstallDir) {
        $backupDir = "$InstallDir.broken-" + (Get-Date -Format "yyyyMMdd-HHmmss")
        Move-Item -LiteralPath $InstallDir -Destination $backupDir -ErrorAction Stop
    }
    # Clone directly from bundle file when possible; else init + remote + fetch.
    $bundleCloneSuccess = $false
    try {
        git clone --quiet $BundlePath $InstallDir 2>$null
        if ($LASTEXITCODE -eq 0) {
            $bundleCloneSuccess = $true
        }
    } catch { }
    if (-not $bundleCloneSuccess) {
        # Fallback: init and fetch from bundle.
        git init $InstallDir 2>$null | Out-Null
        Push-Location $InstallDir
        git remote add origin $BundlePath 2>$null
        # Fetch every advertised branch/tag ref. A bundle is a transport, not a
        # remote, so a single-ref fetch keyed on the pin is brittle; --all keeps
        # the fallback equivalent to the direct clone.
        git fetch --quiet origin "+refs/heads/*:refs/remotes/origin/*" "+refs/tags/*:refs/tags/*" 2>$null
        if ($LASTEXITCODE -ne 0) {
            Pop-Location
            throw "git fetch from bundle failed (exit $LASTEXITCODE)"
        }
        git config windows.appendAtomically false 2>$null
        git config core.autocrlf false 2>$null
        # `remote add` fails when origin already exists; set-url is idempotent.
        git remote set-url origin https://github.com/NousResearch/hermes-agent.git 2>$null
        if ($LASTEXITCODE -ne 0) {
            git remote add origin https://github.com/NousResearch/hermes-agent.git 2>$null
        }
        Pop-Location
    }

    # Check out the requested pin on BOTH clone paths. The direct-clone path
    # lands on whatever the bundle advertised as HEAD, which is not necessarily
    # the requested pin (and a clone from a bundle whose advertised HEAD was
    # unresolvable exits 0 with no checkout at all).
    Push-Location $InstallDir
    try {
        if ($Commit) {
            git checkout --quiet --detach $Commit 2>$null
        } elseif ($Tag) {
            git checkout --quiet --detach "refs/tags/$Tag" 2>$null
        } else {
            $startPoint = Resolve-OfflineBranchStartPoint -RepoPath $InstallDir -Branch $Branch
            if (-not $startPoint) {
                throw "Bundle does not advertise branch '$Branch' or any fetchable commit"
            }
            git checkout --quiet -B $Branch $startPoint 2>$null
        }
        if ($LASTEXITCODE -ne 0) {
            throw "Checked out $InstallDir but could not check out the requested pin (commit '$Commit' / tag '$Tag' / branch '$Branch')"
        }
    } finally {
        Pop-Location
    }

    # Verify HEAD materialization matches the requested pin, and normalize the
    # request to a commit OID so annotated tags compare like-for-like.
    Push-Location $InstallDir
    try {
        $headSha = (git rev-parse HEAD 2>$null | Select-Object -First 1)
        if ($headSha) { $headSha = $headSha.ToString().Trim() }
        $expectedSha = ""
        if ($Commit) {
            $expectedSha = (git rev-parse --verify "${Commit}^{commit}" 2>$null | Select-Object -First 1)
        } elseif ($Tag) {
            $expectedSha = (git rev-parse --verify "refs/tags/${Tag}^{commit}" 2>$null | Select-Object -First 1)
        } else {
            $expectedSha = (git rev-parse --verify "refs/heads/$Branch^{commit}" 2>$null | Select-Object -First 1)
        }
        if ($expectedSha) { $expectedSha = $expectedSha.ToString().Trim() }
        $match = ($headSha -and $expectedSha -and ($headSha -eq $expectedSha))
        $witnessType = "mismatch"
        if ($match) { $witnessType = "assert" }
        New-BehavioralWitness -Name "repo-head-verify" -Path $InstallDir `
            -Expected $expectedSha -Actual $headSha -Type $witnessType | Out-Null
        if (-not $match) {
            throw "Repository HEAD ($headSha) does not match requested pin ($expectedSha)"
        }
    } finally {
        Pop-Location
    }
    return $true
}

# Verify that an offline asset's SHA-256 matches manifest expectations.
# Fail-closed: returns $false when no expected hash is present (BLOCKER 3).
function Test-OfflineAssetHash {
    param(
        [string]$FilePath,
        [string]$ExpectedHash
    )
    if (-not $ExpectedHash) {
        # BLOCKER 3: fail closed -- no hash means verification fails.
        return $false
    }
    if (-not (Test-Path $FilePath)) { return $false }
    $actual = (Get-FileHash -Path $FilePath -Algorithm SHA256).Hash.ToLower()
    return $actual -eq $ExpectedHash.ToLower()
}
