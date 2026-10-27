# Derived from paper-io-core bench/sanitize_iocore.ps1 (itself from paper-one-port bench/sanitize_oneport.ps1
# at commit 1f27ea8), same author (Apache-2.0).
# Changed: wakeloop's names, cache options and paths; no flag line and no link order; the code paths decide what
# must be clean (bench\code_paths.txt), and a timed record also wants the whole tree clean; ctest runs serially and also
# writes a JUnit file and the list of registered tests; the campaign smoke; every *SAN_OPTIONS is set; a toolchain file
# joins the kept logs; LAB_NINJA, LAB_CMAKE and LAB_PYTHON of the lab job are honoured.
#
# The sanitizer record of wakeloop on W (hypotheses-v2.md, section 11 item 7; the Papers repo's rule D5: on W, ASan
# with MSVC). The W counterpart of bench/sanitize_wakeloop.sh, same record (bench/wakeloop_record.py, --host-tag W).
# The gate matches the compiler, so the records and the measured builds on W must use one MSVC.
#
#   powershell -ExecutionPolicy Bypass -File bench\sanitize_wakeloop.ps1 -Records DIR [-DryRun]
#              [-Work DIR] [-RecordsLogs DIR] [-LabBin DIR] [-RepoUrl URL] [-VcVars vcvarsall.bat]
#
# From this checkout, whose whole tree must be clean (git status --porcelain empty, ignored build folders apart; a
# dry run only needs the code paths of bench\code_paths.txt clean): in a vcvars x64 shell, configures the whole CMake
# project with MSVC and Ninja (Release, WAKELOOP_SANITIZER=address), builds it, hashes what it compiled
# (bench\build_inputs.py --host W), lists the registered tests (ctest -N), runs the whole CTest suite serially
# (ctest -V and a JUnit file) and then the campaign smoke (bench\campaign_smoke.py: one reduced real invocation of
# every distinct code shape of the campaign, pilot and H5 runs, with exit codes and report counts kept and no
# measurement; under -DryRun it only lists the shapes and runs no tool), all with
# ASAN_OPTIONS=strict_string_checks=1:symbolize=1 (MSVC's runtime offers no detect_leaks, and
# detect_stack_use_after_return needs a compile option that CMakeLists.txt does not pass: both are declared gaps in
# bench\coverage.json) and the other *SAN_OPTIONS empty, all five set by the driver and written into the record. It keeps
# the logs under RecordsLogs\<name>\ with their SHA256SUMS, packed into <name>.tar.gz with its sha256 (tar.exe of
# Windows; made in a staging folder and moved into place whole, so a failed pack leaves nothing that blocks a rerun),
# and writes Records\wakeloop2-<commit>-W-asan-<time stamp>.json (bench\wakeloop_record.py, which never replaces a file). Green only if
# the build and every registered test passed, every backend has its required tests, every smoke run exited 0, and no log
# holds a sanitizer report.
#
# A timed record needs LAB_JOB (the lab runner sets it) and the laptop's host name (gate_lib.LAB_HOSTS). The paths
# the driver writes into its .cmd file must be ASCII; anything else is refused.
#
# Defaults: Work %USERPROFILE%\lab\p1\records-work (the build tree is Work\asan[-dryrun], made afresh), RecordsLogs
# %USERPROFILE%\lab\records-logs, LabBin the Papers repo's lab\bin beside this checkout (or a sibling Papers clone, as
# the lab job's), RepoUrl the checkout's origin (else "unknown"). Under a lab job, LAB_NINJA's folder is put first on
# PATH, cmake and ctest are LAB_CMAKE's folder's, and python is LAB_PYTHON.
#
# -VcVars: the vcvarsall.bat whose instance builds the record, by default the lab's (Visual Studio 18 Community, the
# laptop's compiler, lab\laptop\versions.md), not the instance that vswhere -latest returns. Before it configures, the
# driver checks that cl resolves under that instance, and the record holds the build's CMAKE_CXX_COMPILER.
#
# -DryRun: a test of this driver, never citable: the name ends in -dryrun, the record is marked (the gate refuses it),
# Records may not be the Papers repo's lab\sanitizer-records, and the logs go under Work\records-logs.
#
# It changes nothing outside Work, RecordsLogs and Records.
param(
    [Parameter(Mandatory = $true)][string]$Records,
    [switch]$DryRun,
    [string]$Work = (Join-Path $env:USERPROFILE "lab\p1\records-work"),
    [string]$RecordsLogs = "",
    [string]$LabBin = "",
    [string]$RepoUrl = "",
    [string]$VcVars = "C:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvarsall.bat"
)
$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonExe = "python"
if ($env:LAB_PYTHON) { $pythonExe = $env:LAB_PYTHON }
# cl.exe reads CL and _CL_ as extra options: a record built with them is not a plain Release build's twin.
if ($env:CL -or $env:_CL_) { throw "CL or _CL_ is set in the environment; cl.exe would read it as extra compiler options" }
if (-not $DryRun) {
    # A timed record is made by a lab job on the laptop, from a clean checkout.
    if (-not $env:LAB_JOB) { throw "refusing a record: LAB_JOB is not set (the lab runner sets it on the laptop); use -DryRun to test the driver" }
    & $pythonExe -c "import sys; sys.path.insert(0, sys.argv[1]); import gate_lib; sys.exit(0 if gate_lib.is_lab_host('W', sys.argv[2]) else 1)" $PSScriptRoot $env:COMPUTERNAME
    if ($LASTEXITCODE -ne 0) { throw "the host $env:COMPUTERNAME is not the laptop's Windows; a record is made on the laptop" }
    if (git -C $repo status --porcelain) { throw "$repo has changed or untracked files; a record names a clean checkout" }
}
New-Item -ItemType Directory -Force $Records | Out-Null
$Records = (Resolve-Path $Records).Path
if ($DryRun -and ($Records -match '[\\/]lab[\\/]sanitizer-records([\\/]|$)')) { throw "a dry run never writes into lab\sanitizer-records" }
# The code paths count even for a dry run: the records driver and the gate are not code (wakeprobe's BENCH_DIRTY reads the same paths).
$codePaths = @(Get-Content (Join-Path $PSScriptRoot "code_paths.txt") | Where-Object { $_.Trim() -and -not $_.StartsWith("#") })
if (git -C $repo status --porcelain --untracked-files=all -- @codePaths) { throw "the code paths of $repo have changes; a record must name committed code" }
$commit = (git -C $repo rev-parse HEAD).Trim()
$fingerprint = (& $pythonExe -c "import sys; sys.path.insert(0, sys.argv[1]); import build_inputs; print(build_inputs.code_fingerprint())" $PSScriptRoot)
if ($LASTEXITCODE -ne 0 -or -not $fingerprint) { throw "the code fingerprint cannot be read" }
$fingerprint = "$fingerprint".Trim()
if (-not $RepoUrl) {
    $RepoUrl = "unknown"
    # Under Stop, a native command's standard error aborts the script even when its exit code is only a "no": say Continue here.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { $origin = git -C $repo remote get-url origin 2>&1; $originRc = $LASTEXITCODE } finally { $ErrorActionPreference = $previous }
    if ($originRc -eq 0 -and $origin) { $RepoUrl = "$origin".Trim() }
}
$suffix = ""
if ($DryRun) { $suffix = "-dryrun" }
# The name is unique (a UTC time stamp): a second record of the same commit is never blocked by the name of a first that
# was red, and the path is checked here, before any build (wakeloop_record.py never replaces a file either).
$stamp = [DateTime]::UtcNow.ToString("yyyyMMddTHHmmss", [Globalization.CultureInfo]::InvariantCulture).ToLower()
$name = "wakeloop2-$($commit.Substring(0, 9))-W-asan-$stamp$suffix"
if (Test-Path (Join-Path $Records "$name.json")) { throw "$Records\$name.json exists; a record is never replaced" }
$workDir = Join-Path $Work "asan$suffix"
if (-not $RecordsLogs) {
    if ($DryRun) { $RecordsLogs = Join-Path $Work "records-logs" } else { $RecordsLogs = Join-Path $env:USERPROFILE "lab\records-logs" }
}
$logDir = Join-Path $RecordsLogs $name
$archive = Join-Path $RecordsLogs "$name.tar.gz"
if ((Test-Path $logDir) -or (Test-Path $archive)) { throw "$logDir or $archive exists; record logs are never overwritten" }
if (-not $LabBin) {
    foreach ($cand in @((Join-Path $repo "..\..\lab\bin"), (Join-Path $repo "..\Papers\lab\bin"))) {
        if (Test-Path (Join-Path $cand "inputs_hash.py")) { $LabBin = (Resolve-Path $cand).Path; break }
    }
}
$inputsHash = Join-Path $LabBin "inputs_hash.py"
if (-not $LabBin -or -not (Test-Path $inputsHash)) { throw "no inputs_hash.py (give -LabBin, the Papers repo's lab\bin)" }

# The lab job's tools, when there is one: ninja's folder first on PATH, cmake and ctest from LAB_CMAKE's folder.
$pathLines = @()
if ($env:LAB_NINJA) { $pathLines += "set `"PATH=$(Split-Path -Parent $env:LAB_NINJA);%PATH%`"" }
$cmake = "cmake"
$ctest = "ctest"
if ($env:LAB_CMAKE) {
    $cmake = "`"$env:LAB_CMAKE`""
    $ctest = "`"$(Join-Path (Split-Path -Parent $env:LAB_CMAKE) 'ctest.exe')`""
}
$python = "python"
if ($env:LAB_PYTHON) { $python = "`"$env:LAB_PYTHON`"" }

$vcvars = $VcVars
if (-not (Test-Path $vcvars)) { throw "vcvarsall.bat not found: $vcvars (give -VcVars)" }
# The .cmd file below is written as ASCII; a path with another character would be mangled in it.
foreach ($p in @($repo, $Work, $Records, $RecordsLogs, $vcvars, $LabBin, $inputsHash, $env:LAB_NINJA, $env:LAB_CMAKE, $env:LAB_PYTHON)) {
    if ($p -and ($p -match '[^\x00-\x7F]')) { throw "the path '$p' has a character that is not ASCII; the .cmd file this driver writes would mangle it" }
}
# cl must resolve under the instance of $vcvars (...\<instance>\VC\Auxiliary\Build\vcvarsall.bat), before anything is built.
$instance = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $vcvars)))
$clOut = & cmd.exe /c "`"$vcvars`" x64 >nul 2>&1 && where cl"
$clPath = ("$($clOut | Select-Object -First 1)").Trim()
if (-not $clPath -or -not $clPath.StartsWith($instance, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "cl resolves to '$clPath' under $vcvars, not under the instance $instance"
}

if (Test-Path $workDir) { Remove-Item -Recurse -Force $workDir }
New-Item -ItemType Directory -Force $workDir | Out-Null
$build = Join-Path $workDir "build"
# Every *SAN_OPTIONS is set: ASan's to the value below, the others to nothing (set "X=" removes the variable), so that
# nothing the shell had reaches a run. The record names all of them.
$asanOptions = "strict_string_checks=1:symbolize=1"
$optionsText = "ASAN_OPTIONS=$asanOptions UBSAN_OPTIONS= LSAN_OPTIONS= TSAN_OPTIONS= MSAN_OPTIONS="
$cmakeArgs = "-G Ninja -DCMAKE_CXX_COMPILER=cl -DCMAKE_BUILD_TYPE=Release -DWAKELOOP_SANITIZER=address"
$buildLog = Join-Path $workDir "build.log"
$ctestLog = Join-Path $workDir "ctest.log"
$ctestXml = Join-Path $workDir "ctest.xml"
$smokeJson = Join-Path $workDir "smoke.json"
$smokeLog = Join-Path $workDir "smoke.log"
$smokeList = ""
if ($DryRun) { $smokeList = "--list" }
$testsTxt = Join-Path $workDir "tests.txt"
$toolchain = Join-Path $workDir "toolchain.txt"
$bat = Join-Path $workDir "sanitize.cmd"
@(
    "@echo off"
    "call `"$vcvars`" x64 >nul || (echo 1 > `"$workDir\build.rc`" & exit /b 1)"
) + $pathLines + @(
    "(ver & cl 2>&1 & $cmake --version & ninja --version & $python --version & git --version & echo inputs_hash.py $inputsHash & echo sanitizer options: $optionsText) > `"$toolchain`" 2>&1"
    "$cmake -S `"$repo`" -B `"$build`" $cmakeArgs > `"$buildLog`" 2>&1 || (echo 2 > `"$workDir\build.rc`" & exit /b 2)"
    "ninja -C `"$build`" >> `"$buildLog`" 2>&1 || (echo 3 > `"$workDir\build.rc`" & exit /b 3)"
    "echo 0 > `"$workDir\build.rc`""
    "$python `"$repo\bench\build_inputs.py`" --build `"$build`" --host W --out `"$workDir\build.inputs.json`" --inputs-hash `"$inputsHash`" >> `"$buildLog`" 2>&1"
    "echo %ERRORLEVEL% > `"$workDir\inputs.rc`""
    "$ctest --test-dir `"$build`" -N > `"$testsTxt`" 2>&1"
    "set `"ASAN_OPTIONS=$asanOptions`""
    "set `"UBSAN_OPTIONS=`""
    "set `"LSAN_OPTIONS=`""
    "set `"TSAN_OPTIONS=`""
    "set `"MSAN_OPTIONS=`""
    "cd /d `"$build`""
    "$ctest -V --timeout 900 --output-junit `"$ctestXml`" > `"$ctestLog`" 2>&1"
    "echo %ERRORLEVEL% > `"$workDir\ctest.rc`""
    "$python `"$repo\bench\campaign_smoke.py`" --build-dir `"$build`" --system windows --out `"$smokeJson`" $smokeList > `"$smokeLog`" 2>&1"
    "echo %ERRORLEVEL% > `"$workDir\smoke.rc`""
    "exit /b 0"
) | Set-Content -Encoding ascii $bat
$start = Get-Date
& cmd.exe /c $bat
$seconds = [int]((Get-Date) - $start).TotalSeconds
function Read-Rc([string]$Name) {
    $path = Join-Path $workDir $Name
    if (Test-Path $path) { return [int](Get-Content $path | Select-Object -First 1).Trim() }
    return 1
}
$buildRc = Read-Rc "build.rc"
$inputsRc = Read-Rc "inputs.rc"
$ctestRc = Read-Rc "ctest.rc"
$smokeRc = Read-Rc "smoke.rc"

# The logs, kept as bench/keep_record_logs.sh keeps them on L: made in a staging folder, moved into place whole.
$archiveSha = ""
$keptArchive = ""
$stage = Join-Path $RecordsLogs ".partial-$PID"
try {
    $stageLog = Join-Path $stage $name
    New-Item -ItemType Directory -Force $stageLog | Out-Null
    foreach ($f in @($buildLog, $ctestLog, $ctestXml, $testsTxt, $toolchain, $smokeJson, $smokeLog, (Join-Path $workDir "build.inputs.json"))) {
        if (Test-Path $f) { Copy-Item $f $stageLog }
    }
    $sums = Get-ChildItem -File $stageLog | Sort-Object Name | ForEach-Object {
        "$((Get-FileHash -Algorithm SHA256 $_.FullName).Hash.ToLower())  ./$($_.Name)"
    }
    $sums | Set-Content -Encoding ascii (Join-Path $stageLog "SHA256SUMS")
    # Windows' own tar.exe by its path: a Git for Windows tar earlier on PATH reads "C:" as a remote host.
    & (Join-Path $env:SystemRoot "System32\tar.exe") -czf "$archive.partial" -C $stage $name
    if ($LASTEXITCODE -ne 0) { throw "tar failed for $archive" }
    $archiveSha = (Get-FileHash -Algorithm SHA256 "$archive.partial").Hash.ToLower()
    Move-Item $stageLog $logDir
    Move-Item "$archive.partial" $archive
    "$archiveSha  $name.tar.gz" | Set-Content -Encoding ascii "$archive.sha256"
    $keptArchive = $archive
} catch {
    [Console]::Error.WriteLine("sanitize_wakeloop.ps1: the logs could not be packed ($_); the record will be red")
    $archiveSha = ""
} finally {
    if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
    if (Test-Path "$archive.partial") { Remove-Item -Force "$archive.partial" }
}

$recArgs = @("$repo\bench\wakeloop_record.py", "--work", $workDir, "--record", (Join-Path $Records "$name.json"),
    "--repo", $RepoUrl, "--commit", $commit, "--repo-head", $commit, "--fingerprint-start", $fingerprint, "--sanitizer", "asan",
    "--cmake-args=$cmakeArgs", "--options=$optionsText", "--host", $env:COMPUTERNAME, "--host-tag", "W",
    "--build-exit", "$buildRc", "--inputs-exit", "$inputsRc", "--ctest-exit", "$ctestRc", "--smoke-exit", "$smokeRc",
    "--seconds", "$seconds", "--ctest-jobs", "1", "--logs-archive", $(if ($keptArchive) { $keptArchive } else { "-" }),
    "--logs-sha256", $(if ($archiveSha) { $archiveSha } else { "-" }))
if ($DryRun) { $recArgs += "--dry-run" }
& $pythonExe @recArgs
exit $LASTEXITCODE
