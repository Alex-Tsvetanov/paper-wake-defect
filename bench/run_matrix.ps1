# Windows wake-defect runs (lab host W), backend iocp.
#
# Usage: run_matrix.ps1 [-Mode all|gap-sweep|bound-sweep|matrix]   (a, b, c are accepted too)
#
# Builds the repository twice, Release with clang-cl by default: `fixed` and `defect`
# (WAKELOOP_DEFECT=ON, the wake seeded out). Designs, one wakeprobe process per cell:
#   (a) gap-sweep    defect, B in {1, 10} ms, phase targets 0.05 to 2.95 B_eff in 30 steps
#                    of 0.1 B_eff (B_eff measured by each run's calibration posts), so the
#                    sawtooth is traversed even where B_eff is the 15.6 ms timer tick
#   (b) bound-sweep  fixed, gap 2 ms, B in {1, 10, 100} ms and 0 (blocking)
#   (c) matrix       both arms, B in {1, 100} ms, gap 1.7B
# Every design runs at timer period {system default, 1 ms} (timeBeginPeriod).
# The cells to run are written to plan.txt in the order they ran, and inputs.json records what
# each build compiled (see -RequireInputs).
#
# Needs Visual Studio (or Build Tools) for the Windows SDK, LLVM clang-cl, CMake, Ninja.
param(
    [string]$Mode = "all",
    [string]$OutDir = "",        # default: results\raw\<date>-W
    [string]$BuildRoot = "",     # default: bench\build
    [string]$Compiler = "clang-cl",
    [string]$RequireCompiler = "",  # if set, both builds must identify as this (CMake's line)
    [string]$RequireInputs = "",    # if set, a gate.json from check_records.py: each build must
                                    # have compiled exactly its arm's inputs and configuration
    [string]$InputsHashPy = "",     # the Papers mono-repo's lab\bin\inputs_hash.py (default:
                                    # $env:INPUTS_HASH_PY, else found from here)
    [string]$CmakeArgs = "",     # extra configure arguments for both builds (e.g. a sanitizer)
    [switch]$BuildTests,         # build every target, the loop's tests included
    [switch]$RunCtest,           # run each build's tests before any cell, full output kept
    [int]$Posts = 51,            # measured posts, matrix and bound sweep
    [int]$SweepPosts = 21,       # measured posts per gap-sweep step
    [int]$SweepSteps = 30,       # gap-sweep steps out of the 30, evenly spaced
    [int]$Warmup = 5,            # warmup posts, matrix and bound sweep; the sweep uses 3
    [int]$Calibrate = 11,
    [string]$Seed = ""           # shuffle the cell order with this seed (default: design order)
)
$ErrorActionPreference = "Stop"

switch ($Mode) {
    "a" { $Mode = "gap-sweep" }
    "b" { $Mode = "bound-sweep" }
    "c" { $Mode = "matrix" }
    { $_ -in @("all", "gap-sweep", "bound-sweep", "matrix") } { }
    default { throw "unknown mode '$Mode'" }
}

# Windows PowerShell's Set-Content -Encoding utf8 writes a BOM; these files are read by tools.
function Write-NoBom([string]$Path) {
    [System.IO.File]::WriteAllLines($Path, [string[]]@($input), (New-Object System.Text.UTF8Encoding($false)))
}

$here = $PSScriptRoot
$repo = Split-Path $here -Parent
if (-not $OutDir) { $OutDir = Join-Path $repo ("results\raw\" + (Get-Date -Format "yyyy-MM-dd") + "-W") }
if (-not $BuildRoot) { $BuildRoot = Join-Path $here "build" }
New-Item -ItemType Directory -Force $OutDir | Out-Null
$OutDir = (Resolve-Path $OutDir).Path
$codePaths = @(Get-Content (Join-Path $here "code_paths.txt") | Where-Object { $_ })

$vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
$vsroot = & $vswhere -latest -products * -property installationPath
$vcvars = Join-Path $vsroot "VC\Auxiliary\Build\vcvarsall.bat"
if (-not (Test-Path $vcvars)) { throw "vcvarsall.bat not found under $vsroot" }

# Host state. There is no pin.sh on Windows; record what would change the numbers.
$cpu = (Get-CimInstance Win32_Processor).Name.Trim()
$os = (Get-CimInstance Win32_OperatingSystem)
$plan = (powercfg /getactivescheme) -join " "
$codeCommit = (git -C $repo log -1 --format=%H -- @codePaths)
@(
    "date: " + (Get-Date -Format o)
    "mode: " + $Mode
    "seed: " + $(if ($Seed) { $Seed } else { "none" })
    "sizes: posts=$Posts sweep_posts=$SweepPosts sweep_steps=$SweepSteps warmup=$Warmup"
    "compiler: $Compiler"
    "cmake_args: $CmakeArgs"
    "host: " + $env:COMPUTERNAME
    "cpu: " + $cpu
    "os: " + $os.Caption + " " + $os.Version
    "power_plan: " + $plan
    "clang-cl: " + ((& clang-cl --version) | Select-Object -First 1)
    "repo_head: " + (git -C $repo rev-parse HEAD)
    "code_commit: " + $codeCommit
) | Write-NoBom (Join-Path $OutDir "env.txt")

# Fresh build directories, so the compiler and the injected commit cannot be stale.
foreach ($arm in @("fixed", "defect")) {
    $defect = if ($arm -eq "defect") { "ON" } else { "OFF" }
    $dir = Join-Path $BuildRoot $arm
    if (Test-Path $dir) { Remove-Item -Recurse -Force $dir }
    $log = Join-Path $OutDir "build-$arm.log"
    Write-Host "building $arm (WAKELOOP_DEFECT=$defect)"
    $target = if ($BuildTests) { "" } else { "--target wakeprobe" }
    # A .cmd file, so vcvarsall's environment reaches cmake and no quoting passes through
    # cmd /c (which strips the outer quotes of a line that holds several).
    New-Item -ItemType Directory -Force $BuildRoot | Out-Null
    $bat = Join-Path $BuildRoot "build-$arm.cmd"
    @(
        "@echo off"
        "call `"$vcvars`" x64 >nul || exit /b 1"
        "cmake -S `"$repo`" -B `"$dir`" -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_CXX_COMPILER=$Compiler -DWAKELOOP_DEFECT=$defect $CmakeArgs > `"$log`" 2>&1 || exit /b 2"
        "cmake --build `"$dir`" $target >> `"$log`" 2>&1 || exit /b 3"
    ) | Set-Content -Encoding ascii $bat
    & cmd.exe /c $bat
    if ($LASTEXITCODE -ne 0) { throw "build of $arm failed ($LASTEXITCODE), see $log" }
}
Get-FileHash -Algorithm SHA256 (Join-Path $BuildRoot "fixed\bench\wakeprobe.exe"), (Join-Path $BuildRoot "defect\bench\wakeprobe.exe") |
    ForEach-Object { $_.Hash.ToLower() + "  " + $_.Path } |
    Write-NoBom (Join-Path $OutDir "binaries.sha256")

# The gate: the compiler CMake identified for each build must be the one the sanitizer
# records name, checked on the builds about to be measured and before any cell runs.
if ($RequireCompiler) {
    foreach ($arm in @("fixed", "defect")) {
        $id = (Select-String -Path (Join-Path $OutDir "build-$arm.log") -Pattern '^-- The CXX compiler identification is (.+)$' |
            Select-Object -First 1 | ForEach-Object { $_.Matches[0].Groups[1].Value.Trim() })
        if ($id -ne $RequireCompiler) {
            throw "build $arm identified its compiler as '$id', the sanitizer records name '$RequireCompiler'; nothing measured"
        }
    }
    Write-Host "compiler matches the sanitizer records: $RequireCompiler"
}

# What each build compiled, and that the loop's sources are among it. With -RequireInputs this is
# also the gate.
if (-not $InputsHashPy) { $InputsHashPy = $(if ($env:INPUTS_HASH_PY) { $env:INPUTS_HASH_PY } else { Join-Path $repo "..\..\lab\bin\inputs_hash.py" }) }
$gateArgs = @((Join-Path $here "inputs_gate.py"), "--inputs-hash", $InputsHashPy, "--build", "fixed=$(Join-Path $BuildRoot 'fixed')",
              "--build", "defect=$(Join-Path $BuildRoot 'defect')", "--out", (Join-Path $OutDir "inputs.json"))
if ($RequireInputs) { $gateArgs += @("--expect", $RequireInputs) }
$prevPref = $ErrorActionPreference; $ErrorActionPreference = "Continue"
$gateMsg = (& python @gateArgs 2>&1 | Out-String)
$gateRc = $LASTEXITCODE; $ErrorActionPreference = $prevPref
Write-Host $gateMsg.Trim()
if ($gateRc -ne 0) { throw "the builds failed the inputs gate; nothing measured" }

# The loop's tests, with the full output of every test kept (the record scans all of it). They
# run in this script's environment, as the cells do: sanitize_wakeprobe.ps1 starts it from a
# vcvars shell with the right ASan runtime first on PATH, which a second vcvars call would undo.
if ($RunCtest) {
    foreach ($arm in @("fixed", "defect")) {
        $dir = Join-Path $BuildRoot $arm
        $bat = Join-Path $BuildRoot "ctest-$arm.cmd"
        @(
            "@echo off"
            "ctest --test-dir `"$dir`" -V --output-junit `"$(Join-Path $OutDir "ctest-$arm.xml")`" > `"$(Join-Path $OutDir "ctest-$arm.log")`" 2>&1"
        ) | Set-Content -Encoding ascii $bat
        & cmd.exe /c $bat
        $passed = (Select-String -Path (Join-Path $OutDir "ctest-$arm.log") -Pattern "tests passed").Line
        Write-Host "ctest ${arm}: exit $LASTEXITCODE; $passed"
    }
}

# The plan: one entry per cell.
function Timer-Name([int]$TimerMs) { if ($TimerMs -eq 0) { "default" } else { "${TimerMs}ms" } }
$cells = New-Object System.Collections.Generic.List[object]
$timers = @(0, 1)
foreach ($t in $timers) {
    $tn = Timer-Name $t
    if ($Mode -in @("all", "gap-sweep")) {
        foreach ($bound in @(1000, 10000)) {
            foreach ($i in 1..$SweepSteps) {
                $step = [int][math]::Ceiling($i * 30 / $SweepSteps)
                $frac = (0.05 + 0.1 * ($step - 1)).ToString("0.00", [Globalization.CultureInfo]::InvariantCulture)
                $cells.Add(@{ Design = "gap-sweep"; Arm = "defect"; Bound = $bound; Gap = ""; Frac = $frac; Posts = $SweepPosts; Warmup = 3; Timer = $t
                               File = "iocp-${bound}us-f$frac-defect-timer$tn.jsonl" })
            }
        }
    }
    if ($Mode -in @("all", "bound-sweep")) {
        foreach ($bound in @(0, 1000, 10000, 100000)) {
            $cells.Add(@{ Design = "bound-sweep"; Arm = "fixed"; Bound = $bound; Gap = "2000"; Frac = ""; Posts = $Posts; Warmup = $Warmup; Timer = $t
                           File = "iocp-${bound}us-g2000us-fixed-timer$tn.jsonl" })
        }
    }
    if ($Mode -in @("all", "matrix")) {
        foreach ($bound in @(1000, 100000)) {
            $gap = [long]($bound * 17 / 10)
            foreach ($arm in @("fixed", "defect")) {
                $cells.Add(@{ Design = "matrix"; Arm = $arm; Bound = $bound; Gap = "$gap"; Frac = ""; Posts = $Posts; Warmup = $Warmup; Timer = $t
                               File = "iocp-${bound}us-g${gap}us-$arm-timer$tn.jsonl" })
            }
        }
    }
}
if ($Seed) {
    # Fisher-Yates with System.Random, so a seed gives the same order on every host.
    $rng = New-Object System.Random ([int]$Seed)
    for ($i = $cells.Count - 1; $i -gt 0; $i--) {
        $j = $rng.Next($i + 1)
        $tmp = $cells[$i]; $cells[$i] = $cells[$j]; $cells[$j] = $tmp
    }
}
@("# seed: " + $(if ($Seed) { $Seed } else { "none" })) + ($cells | ForEach-Object { "$($_.Design) $($_.Arm) $($_.Bound) gap=$($_.Gap) frac=$($_.Frac) timer=$($_.Timer) $($_.File)" }) |
    Write-NoBom (Join-Path $OutDir "plan.txt")

# A failed cell is logged to failures.txt and the run goes on; the exit status reports it.
$failed = 0
$summaries = Join-Path $OutDir "summaries.log"
Write-Host "running $($cells.Count) cells"
foreach ($c in $cells) {
    $dir = Join-Path $OutDir $c.Design
    New-Item -ItemType Directory -Force $dir | Out-Null
    $exe = Join-Path $BuildRoot "$($c.Arm)\bench\wakeprobe.exe"
    $spacing = if ($c.Frac) { @("--phase-frac", $c.Frac) } else { @("--gap-us", $c.Gap) }
    # cmd /c so the process's stderr (sanitizer reports) lands in the log without
    # PowerShell turning it into error records.
    $argv = @("--design", $c.Design, "--label", $c.Arm, "--backend", "iocp", "--wait-us", $c.Bound) + $spacing +
            @("--posts", $c.Posts, "--warmup", $c.Warmup, "--calibrate", $Calibrate, "--timer-period-ms", $c.Timer,
              "--out", (Join-Path $dir $c.File))
    & cmd.exe /c "`"$exe`" $($argv -join ' ') >> `"$summaries`" 2>&1"
    if ($LASTEXITCODE -ne 0) {
        "exit ${LASTEXITCODE}: $($c.Design)/$($c.File)" | Add-Content -Encoding ascii (Join-Path $OutDir "failures.txt")
        Write-Warning "exit ${LASTEXITCODE}: $($c.Design)/$($c.File)"
        $failed++
    }
}
Write-Host "results in $OutDir ($failed failed cells)"
if ($failed -ne 0) { exit 1 }
