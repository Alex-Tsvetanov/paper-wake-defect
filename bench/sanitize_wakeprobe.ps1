# Sanitizer record for wakeloop on Windows (lab host W): AddressSanitizer, MSVC or clang-cl.
#
# Usage: sanitize_wakeprobe.ps1 [-RecordsDir DIR] [-Compiler cl|clang-cl]
#
# Builds the fixed and defect arms of the whole repository (the loop, its tests and wakeprobe)
# with Compiler and /fsanitize=address, runs each arm's tests with the full output of every test
# kept, then every design of run_matrix.ps1 at reduced size (11 posts, 5 gap-sweep steps), all
# from a vcvars shell so the ASan runtime DLL resolves, and writes
# RecordsDir\wakeloop-<code commit>-W-asan.json (-W-asan-clangcl.json for clang-cl, the record
# that gates the measured W builds) with sanitizer_record.py. <code commit> is the last commit
# that changed a path in bench\code_paths.txt; those paths must have no local changes. Neither
# MSVC nor LLVM ships LeakSanitizer for this target.
#
# Logs: every log and output of the run is kept in %USERPROFILE%\lab\records-logs\<record>\ and
# packed into <record>.tar.gz beside it, whose sha256 goes into the record. A record directory
# that already exists is never overwritten. Build trees go to -Work (default
# %USERPROFILE%\lab\records-build\<record>).
#
# clang-cl: CMake links with lld-link directly, so clang-cl's driver never adds the ASan
# runtime; LDFLAGS names it (lld-link finds LLVM's copy before the MSVC one of the same name).
# LLVM's runtime directory goes first on PATH for the runs, because the vcvars PATH holds
# MSVC's clang_rt.asan_dynamic-x86_64.dll, which a clang-cl binary cannot load.
param(
    [string]$RecordsDir = "",
    [string]$Work = "",
    [string]$Compiler = "cl"
)
$ErrorActionPreference = "Stop"

$here = $PSScriptRoot
$repo = Split-Path $here -Parent
if (-not $RecordsDir) { $RecordsDir = Join-Path $repo "..\..\lab\sanitizer-records" }
New-Item -ItemType Directory -Force $RecordsDir | Out-Null
$RecordsDir = (Resolve-Path $RecordsDir).Path
$codePaths = @(Get-Content (Join-Path $here "code_paths.txt") | Where-Object { $_ })

$code = (git -C $repo log -1 --format=%H -- @codePaths).Trim()
if (git -C $repo status --porcelain --untracked-files=all -- @codePaths) {
    throw "the code paths have local changes; a record must name committed code"
}

$cmakeArgs = "-DWAKELOOP_SANITIZER=address"
$asan = "detect_stack_use_after_return=1:strict_string_checks=1:symbolize=1"
$ubsan = "print_stacktrace=1:halt_on_error=1"
$vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
$vcvars = Join-Path (& $vswhere -latest -products * -property installationPath) "VC\Auxiliary\Build\vcvarsall.bat"

$suffix = ""
$sanitizer = "msvc-asan"
$ldflags = ""
$runtimeDir = ""
if ($Compiler -match '^clang-cl') {
    $suffix = "-clangcl"
    $sanitizer = "clang-cl-asan"
    $runtimeDir = Join-Path ((& $Compiler /clang:-print-resource-dir) | Select-Object -First 1).Trim() "lib\windows"
    if (-not (Test-Path (Join-Path $runtimeDir "clang_rt.asan_dynamic-x86_64.dll"))) {
        throw "no clang_rt.asan_dynamic-x86_64.dll in $runtimeDir"
    }
    $ldflags = "clang_rt.asan_dynamic-x86_64.lib /include:__asan_seh_interceptor " +
               "/wholearchive:clang_rt.asan_dynamic_runtime_thunk-x86_64.lib"
} elseif ($Compiler -ne "cl") {
    throw "unsupported compiler '$Compiler' (cl or clang-cl)"
}

$record = "wakeloop-" + $code.Substring(0, 9) + "-W-asan$suffix"
$logsRoot = Join-Path $env:USERPROFILE "lab\records-logs"
$logs = Join-Path $logsRoot $record
if (-not $Work) { $Work = Join-Path $env:USERPROFILE "lab\records-build\$record" }
if ((Test-Path $logs) -or (Test-Path "$logs.tar.gz")) { throw "$logs exists; record logs are never overwritten" }
New-Item -ItemType Directory -Force $logs | Out-Null
if (Test-Path $Work) { Remove-Item -Recurse -Force $Work }
New-Item -ItemType Directory -Force $Work | Out-Null

$bat = Join-Path $Work "run.cmd"
$toolLines = @()
if ($ldflags) { $toolLines += "set `"LDFLAGS=$ldflags`"" }
if ($runtimeDir) { $toolLines += "set `"PATH=$runtimeDir;%PATH%`"" }
@(
    "@echo off"
    "call `"$vcvars`" x64 >nul || exit /b 1"
) + $toolLines + @(
    "set `"ASAN_OPTIONS=$asan`""
    "set `"UBSAN_OPTIONS=$ubsan`""
    "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$here\run_matrix.ps1`" -Mode all -OutDir `"$logs`" -BuildRoot `"$Work\build`" -Compiler $Compiler -CmakeArgs `"$cmakeArgs`" -BuildTests -RunCtest -Posts 11 -SweepPosts 11 -SweepSteps 5 -Warmup 2 > `"$logs\run_matrix.log`" 2>&1"
) | Set-Content -Encoding ascii $bat
$start = Get-Date
& cmd.exe /c $bat
$rc = $LASTEXITCODE
$seconds = [int]((Get-Date) - $start).TotalSeconds
Get-Content (Join-Path $logs "run_matrix.log") -Tail 12

$tar = "$logs.tar.gz"
& tar.exe -czf $tar -C $logsRoot $record
if ($LASTEXITCODE -ne 0) { throw "tar of $logs failed" }
$sha = (Get-FileHash -Algorithm SHA256 $tar).Hash.ToLower()
[System.IO.File]::WriteAllText("$tar.sha256", "$sha  $record.tar.gz`n", (New-Object System.Text.UTF8Encoding($false)))

& python (Join-Path $here "sanitizer_record.py") --out-dir $logs --record (Join-Path $RecordsDir "$record.json") `
    --repo (git -C $repo remote get-url origin).Trim() --commit $code `
    --repo-head (git -C $repo rev-parse HEAD).Trim() `
    --sanitizer $sanitizer "--cmake-args=-DCMAKE_CXX_COMPILER=$Compiler $cmakeArgs" `
    "--ldflags=$ldflags" "--runtime-path=$runtimeDir" `
    "--options=ASAN_OPTIONS=$asan;UBSAN_OPTIONS=$ubsan" --matrix-exit $rc --seconds $seconds --host $env:COMPUTERNAME `
    --logs-archive $tar --logs-sha256 $sha
exit $LASTEXITCODE
