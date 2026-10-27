# H5 on Windows: the 2026-09 designs, run again with v2's wakeprobe (backend iocp).
#
# Usage: run_h5.ps1 -BuildDir DIR -OutDir DIR [-Seed N] [-NoPin] [-DryRun] [-ExpectCells N]
#   -BuildDir DIR    an existing v2 Release build directory; this script never builds
#   -OutDir DIR      plan.txt and, per cell, <cell>.jsonl, <cell>.out and <cell>.err go here
#   -Seed N          shuffle the cell order with this seed (default 20261026)
#   -NoPin           do not pass --worker-cpus 14 --poster-cpu 2 to wakeprobe (pinned by default)
#   -DryRun          write plan.txt and print every command line a real run executes; run nothing
#   -ExpectCells N   exit 1 unless the plan has exactly N cells (the CTest entry uses this)
# A run that is not a dry run needs the environment variable LAB_JOB (the lab runner sets it on
# the laptop); without it the script exits 2. It also exits 2, before it writes anything, when the
# output directory already holds a .jsonl file.
#
# The designs of bench/run_matrix.ps1. One wakeprobe process per cell, always --workers 1
# --protocol single:
#   gap-sweep    defect arm, B in {1, 10} ms, phase targets 0.05 to 2.95 B_eff in 30 steps of
#                0.1 B_eff (B_eff measured by each run's calibration posts)
#   bound-sweep  fixed arm, gap 2 ms, B in {1, 10, 100} ms and 0 (blocking)
#   matrix       both arms, B in {1, 100} ms, gap 1.7 B
# Every design runs at timer period {system default, 1 ms} (timeBeginPeriod).
# The arms are variants of one binary (--variant): fixed is plain_wake, defect is absent_wake.
# Every cell is labelled with its arm (fixed or defect), as in 2026-09. Cells: 2 x (60 + 4 + 4) = 136.
#
# plan.txt lists the cells in run order and is written before any cell runs. It also records the
# seed, the placement and the sha256 of wakeprobe. One log line per cell gives its exit code.
# A failing cell is run once more, into <cell>-rerun.jsonl; the first run's files stay. Every run
# has its own stdout file (.out) and stderr file (.err). A cell whose rerun also fails is logged as
# incomplete and the run goes on. At the end the script prints "incomplete cells: N" and exits 1
# when N > 0, else 0 (hypotheses-v2.md, section 8).
param(
    [string]$BuildDir = "",
    [string]$OutDir = "",
    [int]$Seed = 20261026,
    [switch]$NoPin,
    [switch]$DryRun,
    [int]$ExpectCells = -1
)
$ErrorActionPreference = "Stop"

# The sizes of 2026-09 (run_matrix.ps1 defaults). They are fixed here on purpose.
$Posts = 51
$SweepPosts = 21
$Warmup = 5
$SweepWarmup = 3
$Calibrate = 11

function Fail([string]$Message) {
    [Console]::Error.WriteLine("run_h5.ps1: $Message")
    exit 2
}

# Windows PowerShell's Set-Content -Encoding utf8 writes a BOM; these files are read by tools.
function Write-NoBom([string]$Path) {
    [System.IO.File]::WriteAllLines($Path, [string[]]@($input), (New-Object System.Text.UTF8Encoding($false)))
}

# A script without [CmdletBinding()] collects parameters it does not know in $args, so a typo or the
# old -Pin would be ignored without a word. Refuse them, as run_h5.sh does.
if ($args.Count -gt 0) { Fail "unknown argument '$($args[0])'" }
if (-not $BuildDir -or -not $OutDir) {
    Fail "usage: run_h5.ps1 -BuildDir DIR -OutDir DIR [-Seed N] [-NoPin] [-DryRun] [-ExpectCells N]"
}

# SHA-256 as lower-case hex. Get-FileHash is a function of the Utility module, which Windows
# PowerShell cannot always load (for example when PowerShell 7's module path is inherited); the
# .NET call gives the same hash.
function Get-Sha256([string]$Path) {
    if (Get-Command Get-FileHash -ErrorAction SilentlyContinue) {
        return (Get-FileHash -Algorithm SHA256 $Path).Hash.ToLower()
    }
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $stream = [System.IO.File]::OpenRead($Path)
    try { return ([BitConverter]::ToString($sha.ComputeHash($stream)) -replace '-', '').ToLower() }
    finally { $stream.Dispose(); $sha.Dispose() }
}

# The lab gate: a timed run is started by the lab runner, which sets LAB_JOB on the laptop.
if (-not $DryRun -and -not $env:LAB_JOB) {
    Fail "refusing a timed run: LAB_JOB is not set (the lab runner sets it on the laptop); use -DryRun to check the plan"
}

if (-not (Test-Path -PathType Container $BuildDir)) { Fail "no build directory at $BuildDir" }
$BuildDir = (Resolve-Path $BuildDir).Path
$wakeprobe = Join-Path $BuildDir "bench\wakeprobe.exe"
if (-not (Test-Path -PathType Leaf $wakeprobe)) { Fail "no wakeprobe at $wakeprobe; build it first (this script never builds)" }

# Only a Release build is ever timed. A dry run only warns. No CMakeCache.txt counts as unknown.
$buildType = "unknown"
$cache = Join-Path $BuildDir "CMakeCache.txt"
if (Test-Path -PathType Leaf $cache) {
    $line = Select-String -Path $cache -Pattern '^CMAKE_BUILD_TYPE:[A-Z]*=(.*)$' | Select-Object -First 1
    if ($line) { $buildType = $line.Matches[0].Groups[1].Value.Trim() }
}
if ($buildType -ne "Release") {
    if ($DryRun) { [Console]::Error.WriteLine("run_h5.ps1: warning: $BuildDir is built as '$buildType', not Release") }
    else { Fail "$BuildDir is built as '$buildType', not Release; nothing measured" }
}

# Nothing is overwritten: a directory that holds records is not reused.
if (Test-Path -PathType Container $OutDir) {
    $old = Get-ChildItem -Path $OutDir -Recurse -Filter *.jsonl -File -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($old) { Fail "the output directory $OutDir already holds records (for example $($old.FullName)); use an empty one" }
}
New-Item -ItemType Directory -Force $OutDir | Out-Null
$OutDir = (Resolve-Path $OutDir).Path

# The plan: one entry per cell.
function Timer-Name([int]$TimerMs) { if ($TimerMs -eq 0) { "default" } else { "${TimerMs}ms" } }
$cells = New-Object System.Collections.Generic.List[object]
$seen = @{}
function Add-Cell($Cell) {
    if ($seen.ContainsKey($Cell.File)) { Fail "two cells would write $($Cell.File)" }
    $seen[$Cell.File] = $true
    $cells.Add($Cell)
}
function Variant-Of([string]$Arm) { if ($Arm -eq "fixed") { "plain_wake" } else { "absent_wake" } }
$inv = [Globalization.CultureInfo]::InvariantCulture
foreach ($t in @(0, 1)) {
    $tn = Timer-Name $t
    foreach ($bound in @(1000, 10000)) {
        foreach ($i in 1..30) {
            $frac = (0.05 + 0.1 * ($i - 1)).ToString("0.00", $inv)
            Add-Cell @{ Design = "gap-sweep"; Arm = "defect"; Variant = (Variant-Of "defect"); Bound = $bound; Gap = ""; Frac = $frac
                        Posts = $SweepPosts; Warmup = $SweepWarmup; Timer = $t
                        File = "gap-sweep-iocp-${bound}us-f$frac-defect-timer$tn.jsonl" }
        }
    }
    foreach ($bound in @(0, 1000, 10000, 100000)) {
        Add-Cell @{ Design = "bound-sweep"; Arm = "fixed"; Variant = (Variant-Of "fixed"); Bound = $bound; Gap = "2000"; Frac = ""
                    Posts = $Posts; Warmup = $Warmup; Timer = $t
                    File = "bound-sweep-iocp-${bound}us-g2000us-fixed-timer$tn.jsonl" }
    }
    foreach ($bound in @(1000, 100000)) {
        $gap = [long]($bound * 17 / 10)
        foreach ($arm in @("fixed", "defect")) {
            Add-Cell @{ Design = "matrix"; Arm = $arm; Variant = (Variant-Of $arm); Bound = $bound; Gap = "$gap"; Frac = ""
                        Posts = $Posts; Warmup = $Warmup; Timer = $t
                        File = "matrix-iocp-${bound}us-g${gap}us-$arm-timer$tn.jsonl" }
        }
    }
}
$total = $cells.Count

# Fisher-Yates with System.Random, so a seed gives the same order on every host (as run_matrix.ps1).
$rng = New-Object System.Random $Seed
for ($i = $cells.Count - 1; $i -gt 0; $i--) {
    $j = $rng.Next($i + 1)
    $tmp = $cells[$i]; $cells[$i] = $cells[$j]; $cells[$j] = $tmp
}
if ($cells.Count -ne $total) { Fail "the shuffle changed the number of cells ($($cells.Count) of $total)" }

function Count-Of([string]$Design) { @($cells | Where-Object { $_.Design -eq $Design }).Count }
$counts = "gap-sweep $(Count-Of 'gap-sweep'), bound-sweep $(Count-Of 'bound-sweep'), matrix $(Count-Of 'matrix')"
$placement = if ($NoPin) { "off (-NoPin)" } else { "worker-cpus 14, poster-cpu 2" }
$wakeprobeSha = Get-Sha256 $wakeprobe
$header = @(
    "# seed: $Seed"
    "# build: $BuildDir"
    "# wakeprobe sha256: $wakeprobeSha"
    "# placement: $placement"
    "# cells: $total ($counts)"
    "# fields: design arm variant bound_us gap_us phase_frac timer_ms posts warmup file"
)
($header + ($cells | ForEach-Object {
    "$($_.Design) $($_.Arm) $($_.Variant) $($_.Bound) gap=$($_.Gap) frac=$($_.Frac) timer=$($_.Timer) $($_.Posts) $($_.Warmup) $($_.File)"
})) | Write-NoBom (Join-Path $OutDir "plan.txt")
Write-Host "plan: $total cells ($counts), seed $Seed, written to $(Join-Path $OutDir 'plan.txt')"
if ($ExpectCells -ge 0 -and $total -ne $ExpectCells) {
    [Console]::Error.WriteLine("run_h5.ps1: the plan has $total cells, expected $ExpectCells")
    exit 1
}

# The string that cmd.exe /c runs for one run of a cell, so that the process's stdout and stderr
# land in files of their own without PowerShell turning stderr into error records. The suffix is
# empty for the first run and -rerun for the second.
function Cell-Line($c, [string]$Suffix) {
    $stem = $c.File -replace '\.jsonl$', ''
    $spacing = if ($c.Frac) { @("--phase-frac", $c.Frac) } else { @("--gap-us", $c.Gap) }
    $argv = @("--design", $c.Design, "--label", $c.Arm, "--backend", "iocp", "--variant", $c.Variant,
              "--workers", 1, "--protocol", "single", "--wait-us", $c.Bound) + $spacing +
            @("--posts", $c.Posts, "--warmup", $c.Warmup, "--calibrate", $Calibrate, "--timer-period-ms", $c.Timer,
              "--out", ('"' + (Join-Path $OutDir ($stem + $Suffix + ".jsonl")) + '"'))
    if (-not $NoPin) { $argv += @("--worker-cpus", 14, "--poster-cpu", 2) }
    $stdoutFile = Join-Path $OutDir ($stem + $Suffix + ".out")
    $stderrFile = Join-Path $OutDir ($stem + $Suffix + ".err")
    return ('"' + $wakeprobe + '" ' + ($argv -join ' ') + ' > "' + $stdoutFile + '" 2> "' + $stderrFile + '"')
}
function Run-Cell($c, [string]$Suffix) {
    $line = Cell-Line $c $Suffix
    & cmd.exe /c $line | Out-Null
    return $LASTEXITCODE
}

if ($DryRun) {
    $n = 0
    foreach ($c in $cells) {
        $n++
        Write-Host ("${n}: cmd.exe /c " + (Cell-Line $c ""))
    }
    Write-Host "dry run: $total cells"
    exit 0
}

Write-Host "running $total cells"
$n = 0
$incomplete = 0
foreach ($c in $cells) {
    $n++
    $stem = $c.File -replace '\.jsonl$', ''
    $rc = Run-Cell $c ""
    Write-Host ("{0} cell {1}/{2} exit {3} {4}" -f (Get-Date -Format o), $n, $total, $rc, $c.File)
    if ($rc -ne 0) {
        $rc2 = Run-Cell $c "-rerun"
        Write-Host ("{0} cell {1}/{2} rerun exit {3} {4}-rerun.jsonl" -f (Get-Date -Format o), $n, $total, $rc2, $stem)
        if ($rc2 -ne 0) {
            Write-Host ("{0} cell {1}/{2} INCOMPLETE {3} (see {4}.err and {4}-rerun.err)" -f (Get-Date -Format o), $n, $total, $c.File, $stem)
            $incomplete++
        }
    }
}
Write-Host "results in $OutDir"
Write-Host "incomplete cells: $incomplete"
if ($incomplete -gt 0) { exit 1 }
exit 0
