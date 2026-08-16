#requires -Version 5
<#
.SYNOPSIS
    Build the OpenFOV standalone distribution with Nuitka.

.DESCRIPTION
    Produces `dist/openfov.dist/` -- a self-contained directory with
    OpenFOV.exe, PySide6, MediaPipe, OpenCV, and the bundled resources
    (icon, model, NPClient binaries). Inno Setup then wraps this into a
    single installer.

    Run after `npclient-vendor/build.ps1` has produced the native
    binaries (NPClient64.dll + TrackIR.exe in resources/bin/). The
    Nuitka --include-data-dir flag pulls those into the bundle.

.PARAMETER OutDir
    Where to write the standalone folder. Defaults to dist/.

.PARAMETER Version
    Version string baked into the exe metadata. Default 0.2.2.0.

.NOTES
    On CI (windows-latest), Python 3.12 + Nuitka 2.7+ should already be
    installed via `pip install -r requirements-dev.txt`. Locally:

        python -m pip install -r requirements-dev.txt
        pwsh build/nuitka_build.ps1
#>

[CmdletBinding()]
param(
    [string]$OutDir = "",
    [string]$Version = "0.2.2.0",
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# Derive the script dir robustly. $PSScriptRoot is the canonical way,
# but it's been empirically observed empty when this script is invoked
# through certain non-pwsh wrappers (e.g. `powershell -File ...` from
# inside a bash shell). Fall back to $MyInvocation, then to PWD.
$scriptDir = $PSScriptRoot
if (-not $scriptDir) {
    $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
}
if (-not $scriptDir) {
    $scriptDir = (Get-Location).Path
}

if (-not $OutDir) {
    $OutDir = Join-Path $scriptDir "..\dist"
}

$root = Resolve-Path (Join-Path $scriptDir "..")
$src  = Join-Path $root "src"
# Point Nuitka at the *package directory*, not __main__.py. This is the
# correct way to compile a `python -m openfov`-style app: Nuitka treats
# the result as a package and preserves "import openfov.X" at runtime.
# Passing __main__.py directly compiles it as a standalone script --
# `openfov` then doesn't exist as an importable namespace and every
# internal `from openfov.X import Y` blows up with ModuleNotFoundError.
# (Verified empirically in build attempt 7 and noted by Nuitka itself
# at the start of every build as a WARNING.)
$entry = Join-Path $src "openfov"
$resources = Join-Path $root "resources"
$icon = Join-Path $resources "icons\openfov.ico"

if (-not (Test-Path $entry))      { throw "Missing entry: $entry" }
if (-not (Test-Path $resources))  { throw "Missing resources: $resources" }
if (-not (Test-Path $icon))       { throw "Missing icon: $icon" }

# Sanity check: native binaries should already be built. Don't fail hard
# - the build script may be testing the Python side - but warn loudly.
$bin = Join-Path $resources "bin"
if (-not (Test-Path (Join-Path $bin "NPClient64.dll"))) {
    Write-Warning "NPClient64.dll missing from $bin. Run npclient-vendor/build.ps1 first."
}
if (-not (Test-Path (Join-Path $bin "TrackIR.exe"))) {
    Write-Warning "TrackIR.exe missing from $bin. Run npclient-vendor/build.ps1 first."
}

# Locate Python. We don't use the null-conditional `?.Source` syntax
# because Windows PowerShell 5.1 (the default on most Windows systems)
# can't parse it -- the whole script would fail to load even if we
# never hit this branch. Plain `if`-then is 5.1-compatible.
if (-not $PythonExe) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $PythonExe = $cmd.Source }
}
if (-not $PythonExe) {
    $cmd = Get-Command py -ErrorAction SilentlyContinue
    if ($cmd) { $PythonExe = $cmd.Source }
}
if (-not $PythonExe) {
    throw "Python not found on PATH. Pass -PythonExe or install Python."
}

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$OutDir = (Resolve-Path $OutDir).Path

# Redirect Nuitka's cache out of the Windows Store / WindowsApps
# virtualized AppData. When this script is run from a sandboxed
# terminal (e.g. Claude Code's bundled bash), the default cache lands
# under %LOCALAPPDATA%\Packages\<app>\LocalCache\Local\Nuitka\Nuitka,
# which has file-virtualization quirks: shell tools can see files
# that the C compiler then claims don't exist. Pinning the cache to
# a normal non-sandboxed path (D:\nuitka-cache by default) avoids
# this entire class of "file is there but compile fails to find it"
# headaches.
if (-not $env:NUITKA_CACHE_DIR) {
    $env:NUITKA_CACHE_DIR = "D:\nuitka-cache"
    Write-Host "Using NUITKA_CACHE_DIR=$($env:NUITKA_CACHE_DIR) (avoids sandbox virtualization)."
}
if (-not (Test-Path $env:NUITKA_CACHE_DIR)) {
    New-Item -ItemType Directory -Force -Path $env:NUITKA_CACHE_DIR | Out-Null
}

Write-Host "Building OpenFOV $Version with Nuitka..."
Write-Host "  Python: $PythonExe"
Write-Host "  Source: $entry"
Write-Host "  Output: $OutDir"
Write-Host ""

# MediaPipe's native runtime (libmediapipe.dll, ~27 MB) is loaded via
# dlopen at runtime, so Nuitka's dependency scanner never sees it and
# --include-package-data skips DLLs. Bundle it explicitly. We resolve the
# path from the active interpreter (differs per machine / CI) rather than
# hardcoding. Without this the standalone imports mediapipe fine but
# throws "Could not find module libmediapipe.dll" the instant it creates
# the FaceLandmarker -- a runtime failure a successful compile won't catch.
$mpDir = (& $PythonExe -c "import mediapipe, os; print(os.path.dirname(mediapipe.__file__))").Trim()
$mpDll = Join-Path $mpDir "tasks\c\libmediapipe.dll"
if (-not (Test-Path $mpDll)) {
    throw "libmediapipe.dll not found at '$mpDll' - mediapipe layout changed; the build would be broken."
}
Write-Host "  MediaPipe DLL: $mpDll"
Write-Host ""

# Nuitka args. Standalone (not onefile) - faster startup, better AV
# reputation, cleaner Inno Setup integration.
#
# IMPORTANT: --nofollow-import-to excludes transitive imports we don't
# actually use. MediaPipe drags in jax + jaxlib + tensorflow indirectly,
# and Nuitka tries to compile them all to C -- JAX alone added 30+ min
# of build time and ~100 MB to the bundle, all dead weight. We use only
# MediaPipe's bundled TFLite path for FaceLandmarker, never JAX or TF.
$args = @(
    "-m", "nuitka",
    "--standalone",
    "--assume-yes-for-downloads",
    # Compile in package mode -- paired with $entry pointing at the
    # openfov/ directory above. Without this flag the __main__.py
    # block guarded by `if __name__ == "__main__":` doesn't fire.
    "--python-flag=-m",
    "--enable-plugin=pyside6",
    "--include-package=mediapipe",
    "--include-package=cv2",
    "--include-package-data=mediapipe",
    # Exclude heavyweight ML libs MediaPipe drags in via transitive
    # imports but the FaceLandmarker codepath never actually uses.
    # JAX alone added 30+ min of build time and ~100 MB of dead weight.
    #
    # Do NOT exclude matplotlib / scipy / pandas here -- MediaPipe's
    # drawing_utils + python.solutions imports those at module-load
    # time, even when we don't call them. Excluding them makes the
    # `import mediapipe` line at module top fail at runtime with
    # ImportError. Better to bloat the bundle a little than ship a
    # broken .exe.
    "--nofollow-import-to=jax",
    "--nofollow-import-to=jaxlib",
    "--nofollow-import-to=tensorflow",
    "--nofollow-import-to=tensorflow_hub",
    "--nofollow-import-to=tf_keras",
    "--nofollow-import-to=torch",
    "--nofollow-import-to=IPython",
    "--include-data-files=$resources\models\face_landmarker.task=face_landmarker.task",
    "--include-data-files=$mpDll=mediapipe/tasks/c/libmediapipe.dll",
    # NPClient64.dll + TrackIR.exe are binaries; Nuitka's --include-data-dir
    # silently skips .dll/.exe, so include them explicitly or the installed
    # app can't deliver tracking to iRacing (registry points at a missing
    # DLL). Destination matches bundled_bin_dir() -> <exe>/resources/bin.
    "--include-data-files=$resources\bin\NPClient64.dll=resources/bin/NPClient64.dll",
    "--include-data-files=$resources\bin\TrackIR.exe=resources/bin/TrackIR.exe",
    "--include-data-dir=$resources=resources",
    "--windows-console-mode=disable",
    "--windows-icon-from-ico=$icon",
    "--company-name=OpenFOV Project",
    "--product-name=OpenFOV",
    "--file-version=$Version",
    "--product-version=$Version",
    "--file-description=OpenFOV head tracker",
    "--copyright=MIT (c) 2026 OpenFOV Contributors",
    "--output-dir=$OutDir",
    "--output-filename=OpenFOV.exe",
    $entry
)

Push-Location $root
try {
    & $PythonExe @args
    if ($LASTEXITCODE -ne 0) { throw "Nuitka build failed (exit $LASTEXITCODE)" }
} finally {
    Pop-Location
}

$distFolder = Join-Path $OutDir "__main__.dist"
$renamed = Join-Path $OutDir "openfov.dist"
if (Test-Path $distFolder) {
    if (Test-Path $renamed) { Remove-Item -Recurse -Force $renamed }
    Rename-Item -Path $distFolder -NewName "openfov.dist"
}

Write-Host ""
Write-Host "Build complete. Standalone folder:" -ForegroundColor Green
Write-Host "  $renamed"
$exe = Join-Path $renamed "OpenFOV.exe"
if (Test-Path $exe) {
    $size = (Get-Item $exe).Length
    Write-Host ("  OpenFOV.exe = {0:N0} bytes" -f $size)
}
$total = (Get-ChildItem -Recurse -File $renamed | Measure-Object -Property Length -Sum).Sum
Write-Host ("  Total bundle size = {0:N1} MB" -f ($total / 1MB))
