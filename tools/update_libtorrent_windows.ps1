# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

param(
    [string]$Base = "C:\Users\admin\libtorrent-build",
    [string]$Python = "C:\Users\admin\AppData\Local\Programs\Python\Python314\python.exe"
)

$ErrorActionPreference = 'Stop'

$repo = Join-Path $Base "libtorrent"
$build = Join-Path $Base "build"
$boostRoot = Join-Path $Base "boost_1_90_0"
$boostStageLib = Join-Path $boostRoot "stage\lib"
$vcpkgRoot = Join-Path $Base "vcpkg"
$vcpkgTriplet = "x64-windows"
$vcpkgExe = Join-Path $vcpkgRoot "vcpkg.exe"
$opensslRoot = Join-Path $vcpkgRoot ("installed\" + $vcpkgTriplet)
$opensslBin = Join-Path $opensslRoot "bin"
$opensslDlls = @("libssl-3-x64.dll", "libcrypto-3-x64.dll")
$site = & $Python -c "import sysconfig; print(sysconfig.get_path('platlib'))"
if ($LASTEXITCODE -ne 0) { throw "Python 3.14 site-packages lookup failed" }
$git = "C:\Program Files\Git\cmd\git.exe"
$cmake = "C:\Program Files\CMake\bin\cmake.exe"
$installedFile = Join-Path $Base "installed_version.txt"
$logDir = Join-Path $Base "logs"
$wheelDir = Join-Path $Base "wheels"
$makeWheel = Join-Path $Base "package_libtorrent_wheel.py"
$testVenv = Join-Path $Base "wheel-test"

New-Item -ItemType Directory -Force -Path $logDir, $wheelDir | Out-Null
$log = Join-Path $logDir ("update-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".log")
Start-Transcript -Path $log -Append

function Assert-NativeSuccess([string]$Action) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Action failed with exit code $LASTEXITCODE"
    }
}

function Ensure-Repo {
    if (!(Test-Path $repo)) {
        & $git clone --recurse-submodules https://github.com/arvidn/libtorrent.git $repo
        Assert-NativeSuccess "git clone"
    }
    & $git -C $repo fetch --tags --force
    Assert-NativeSuccess "git fetch"
    & $git -C $repo submodule update --init --recursive
    Assert-NativeSuccess "git submodule update"
}

function Get-LatestStableTag {
    $tags = & $git -C $repo tag --list "v*" --sort=-version:refname
    Assert-NativeSuccess "git tag listing"
    $tag = $tags | Where-Object { $_ -match '^v\d+\.\d+\.\d+$' } | Select-Object -First 1
    if (-not $tag) { throw "No stable libtorrent release tag was found" }
    return $tag
}

function Ensure-Boost {
    if (!(Test-Path "$boostRoot\bootstrap.bat")) {
        throw "Boost not found at $boostRoot. Re-run the initial setup."
    }
    Push-Location $boostRoot
    try {
        if (!(Test-Path "$boostRoot\b2.exe")) {
            & "$boostRoot\bootstrap.bat" | Out-Host
            Assert-NativeSuccess "Boost bootstrap"
        }
        if (!(Get-ChildItem -Path $boostStageLib -Filter "boost_python314*.dll" -ErrorAction SilentlyContinue)) {
            & "$boostRoot\b2.exe" --user-config=C:\Users\admin\user-config.jam `
                --with-python --with-system --with-chrono --with-random `
                --with-date_time --with-atomic --with-filesystem `
                --with-serialization --with-thread --with-headers `
                variant=release threading=multi address-model=64 link=shared `
                runtime-link=shared python=3.14 | Out-Host
            Assert-NativeSuccess "Boost.Python build"
        }
    }
    finally { Pop-Location }
}

function Ensure-OpenSSL {
    if (!(Test-Path "$opensslRoot\include\openssl\ssl.h")) {
        throw "OpenSSL headers not found under $opensslRoot"
    }
    foreach ($dll in $opensslDlls) {
        if (!(Test-Path (Join-Path $opensslBin $dll))) {
            throw "OpenSSL DLL not found: $dll"
        }
    }
}

function Ensure-VcpkgUpdated {
    if (!(Test-Path $vcpkgExe)) {
        & (Join-Path $vcpkgRoot "bootstrap-vcpkg.bat") -disableMetrics | Out-Host
        Assert-NativeSuccess "vcpkg bootstrap"
    }
    if (Test-Path (Join-Path $vcpkgRoot ".git")) {
        & $git -C $vcpkgRoot pull --rebase | Out-Host
        Assert-NativeSuccess "vcpkg git pull"
        # The ports tree can adopt a newer tool-data schema. Refresh the
        # executable after pulling so it can parse the checkout it belongs to.
        & (Join-Path $vcpkgRoot "bootstrap-vcpkg.bat") | Out-Host
        Assert-NativeSuccess "vcpkg refresh"
    }
    & $vcpkgExe update | Out-Host
    Assert-NativeSuccess "vcpkg update"
    & $vcpkgExe upgrade --no-dry-run --disable-metrics | Out-Host
    Assert-NativeSuccess "vcpkg upgrade"
    & $vcpkgExe install ("openssl:" + $vcpkgTriplet) --disable-metrics | Out-Host
    Assert-NativeSuccess "vcpkg OpenSSL install"
}

function Get-OpenSSLVersion {
    $line = (& $vcpkgExe list ("openssl:" + $vcpkgTriplet) | Select-Object -First 1)
    if (-not $line) { return "" }
    return ($line -split "\s+")[1]
}

function Configure-Build {
    New-Item -ItemType Directory -Force -Path $build | Out-Null
    & $cmake -S $repo -B $build --fresh -Wno-author -G "Visual Studio 17 2022" -A x64 `
        -Dpython-bindings=ON `
        -Dpython-install-system-dir=OFF `
        -DPython3_EXECUTABLE="$($Python -replace '\\', '/')" `
        -DBoost_ROOT="$($boostRoot -replace '\\', '/')" `
        -DBoost_NO_SYSTEM_PATHS=ON `
        -DOPENSSL_ROOT_DIR="$opensslRoot" `
        -DOPENSSL_USE_STATIC_LIBS=OFF `
        -Dboost-python-module-name=python314 | Out-Host
    Assert-NativeSuccess "libtorrent CMake configuration"
}

function Build-Install {
    & $cmake --build $build --config Release -- /m | Out-Host
    Assert-NativeSuccess "libtorrent build"
    $extension = Get-ChildItem -Path "$build\bindings\python\Release" -Filter "libtorrent.cp314-*.pyd" |
        Select-Object -First 1
    $rasterbar = Join-Path $build "Release\torrent-rasterbar.dll"
    if (-not $extension) { throw "The build produced no CPython 3.14 extension" }
    if (!(Test-Path $rasterbar)) { throw "The build produced no torrent-rasterbar.dll" }
    Copy-Item -LiteralPath $extension.FullName -Destination $site -Force
    Copy-Item -LiteralPath $rasterbar -Destination $site -Force
    Copy-Item -Path "$boostStageLib\boost_*.dll" -Destination $site -Force
    foreach ($dll in $opensslDlls) {
        Copy-Item -Path (Join-Path $opensslBin $dll) -Destination $site -Force
    }
    & $Python -c "import libtorrent; print(libtorrent.__version__, libtorrent.__file__)"
    Assert-NativeSuccess "installed libtorrent import"
}

function Build-Wheel {
    if (!(Test-Path $makeWheel)) { throw "Wheel packager not found: $makeWheel" }
    & $Python -c "import delvewheel" 2>$null
    if ($LASTEXITCODE -ne 0) {
        & $Python -m pip install --disable-pip-version-check --quiet delvewheel
        Assert-NativeSuccess "delvewheel install"
    }
    $extension = Get-ChildItem -Path $site -Filter "libtorrent.cp314-*.pyd" |
        Select-Object -First 1
    if (-not $extension) { throw "No CPython 3.14 libtorrent extension found in $site" }
    & $Python $makeWheel `
        --extension $extension.FullName `
        --repo $repo `
        --outdir $wheelDir `
        --stamp (Get-Date -Format "yyyyMMdd") `
        --add-path $boostStageLib `
        --add-path $opensslBin `
        --add-path (Join-Path $build "Release") | Out-Host
    Assert-NativeSuccess "libtorrent wheel packaging"
}

function Test-LatestWheel([string]$Version) {
    $wheel = Get-ChildItem -Path $wheelDir -Filter "libtorrent-$Version+*-cp314-cp314-win_amd64.whl" |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $wheel) { throw "No CPython 3.14 wheel found for libtorrent $Version" }
    & $Python -m venv --clear $testVenv
    Assert-NativeSuccess "wheel-test virtual environment creation"
    $testPython = Join-Path $testVenv "Scripts\python.exe"
    & $testPython -m pip install --disable-pip-version-check $wheel.FullName | Out-Host
    Assert-NativeSuccess "wheel-test installation"
    & $testPython -c "import libtorrent; print(libtorrent.__version__, libtorrent.__file__)" | Out-Host
    Assert-NativeSuccess "wheel-test import"
}

try {
    if (!(Test-Path $Python)) { throw "Python 3.14 not found at $Python" }
    Ensure-Repo
    Ensure-VcpkgUpdated
    $opensslVersion = Get-OpenSSLVersion
    $latestTag = Get-LatestStableTag
    $latestCommit = & $git -C $repo rev-list -n 1 $latestTag
    Assert-NativeSuccess "git release commit lookup"
    $latestLabel = "$latestTag@$latestCommit|openssl=$opensslVersion"
    $version = $latestTag.TrimStart('v')
    $current = if (Test-Path $installedFile) { (Get-Content $installedFile -First 1).Trim() } else { "" }
    $wheelExists = Get-ChildItem -Path $wheelDir -Filter "libtorrent-$version+*-cp314-cp314-win_amd64.whl" -ErrorAction SilentlyContinue
    if ($current -eq $latestLabel -and $wheelExists) {
        try {
            Test-LatestWheel $version
            Write-Host "Already up to date: $latestLabel"
            exit 0
        }
        catch {
            Write-Host "Existing wheel validation failed; rebuilding: $_"
        }
    }

    & $git -C $repo checkout --force $latestTag
    Assert-NativeSuccess "git checkout"
    & $git -C $repo submodule update --init --recursive
    Assert-NativeSuccess "git submodule update"
    Ensure-Boost
    Ensure-OpenSSL
    Configure-Build
    Build-Install
    Build-Wheel
    Test-LatestWheel $version
    Set-Content -Path $installedFile -Value $latestLabel -Encoding ASCII
    Write-Host "Updated to $latestLabel"
}
finally {
    try { Stop-Transcript | Out-Null } catch { }
}
