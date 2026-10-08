# Publication runs (tier T3) on the Windows lab host W: every design at full size.
#
# Usage: run_publication.ps1 [-CheckOnly] [-Seed N] [-RecordsDir DIR] [-RunName NAME]
#
# Refuses to start unless
#   - the code paths (bench\code_paths.txt) have no local changes,
#   - lab/sanitizer-records covers the builds about to be made (check_records.py): a green
#     clang-cl wakeloop record for the code commit (wakeloop-<code>-W-asan-clangcl.json,
#     sanitize_wakeprobe.ps1 -Compiler clang-cl) that names, per arm, the compiler, the compiled
#     first-party inputs and the configuration, and
#   - the host is quiet: CPU idle at least 95% averaged over 10 s. The top 5 processes by
#     CPU are recorded either way.
# Then run_matrix.ps1 builds both arms and stops before any cell runs unless both builds
# identify their compiler as the record does and each compiled exactly its arm's inputs in its
# arm's configuration (gate.json in the run directory, checked by inputs_gate.py; inputs.json
# records what the builds compiled). -CheckOnly checks the records only: a build's compiled
# inputs exist only after it is built. Then all designs run with the cell order shuffled by
# Seed, written to results\raw\<RunName>\ (default <date>-W-publication). The Papers mono-repo's
# lab\bin\inputs_hash.py is found from here unless $env:INPUTS_HASH_PY names it.
param(
    [switch]$CheckOnly,
    [int]$Seed = 20260926,
    [string]$RecordsDir = "",
    [string]$RunName = ""
)
$ErrorActionPreference = "Stop"

$here = $PSScriptRoot
$repo = Split-Path $here -Parent
if (-not $RecordsDir) { $RecordsDir = Join-Path $repo "..\..\lab\sanitizer-records" }
if (-not $RunName) { $RunName = (Get-Date -Format "yyyy-MM-dd") + "-W-publication" }
$out = Join-Path $repo ("results\raw\" + $RunName)
$codePaths = @(Get-Content (Join-Path $here "code_paths.txt") | Where-Object { $_ })

function Refuse([string]$Why) { [Console]::Error.WriteLine("run_publication: $Why"); exit 1 }

$code = (git -C $repo log -1 --format=%H -- @codePaths).Trim()
if (git -C $repo status --porcelain --untracked-files=all -- @codePaths) { Refuse "the code paths have local changes" }

$prevPref = $ErrorActionPreference; $ErrorActionPreference = "Continue"
$gateLines = & python (Join-Path $here "check_records.py") --records $RecordsDir --code $code --host W 2>&1
$gateRc = $LASTEXITCODE; $ErrorActionPreference = $prevPref
$gateText = ($gateLines | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] }) -join "`n"
$gateLines | Where-Object { $_ -is [System.Management.Automation.ErrorRecord] } | ForEach-Object { Write-Host "$_" }
if ($gateRc -ne 0) { Refuse "the sanitizer records do not cover this build (see above)" }
$recordCompiler = ($gateText | ConvertFrom-Json).compiler

# Quiet check. WMI's formatted counters rather than Get-Counter, whose counter paths are
# localized on a non-English Windows.
$idle = @()
foreach ($i in 1..10) {
    Start-Sleep -Seconds 1
    $idle += [double](Get-CimInstance Win32_PerfFormattedData_PerfOS_Processor -Filter "Name='_Total'").PercentIdleTime
}
$avgIdle = ($idle | Measure-Object -Average).Average
$top = Get-CimInstance Win32_PerfFormattedData_PerfProc_Process |
    Where-Object { $_.Name -notin @("_Total", "Idle") } |
    Sort-Object PercentProcessorTime -Descending | Select-Object -First 5 |
    ForEach-Object { [ordered]@{ name = $_.Name; pid = $_.IDProcess; cpu_percent = $_.PercentProcessorTime } }
$quiet = [ordered]@{ date = (Get-Date -Format o); idle_percent_samples = $idle; idle_percent_mean = $avgIdle
                     threshold = 95; top_processes = @($top) } | ConvertTo-Json -Depth 4
Write-Host $quiet
if ($avgIdle -lt 95) { Refuse ("host is not quiet: mean CPU idle {0:N1}% over 10 s" -f $avgIdle) }

Write-Host "code $code, seed $Seed -> $out"
if ($CheckOnly) { Write-Host "CheckOnly: nothing run (the compiled inputs are checked after the builds of a real run)"; exit 0 }
if (Test-Path $out) { Refuse "$out exists; publication output is never overwritten" }
New-Item -ItemType Directory -Force $out | Out-Null
$utf8 = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText((Join-Path $out "quiet.json"), $quiet + "`n", $utf8)
[System.IO.File]::WriteAllText((Join-Path $out "gate.json"), $gateText + "`n", $utf8)
& (Join-Path $here "run_matrix.ps1") -Mode all -OutDir $out -Seed "$Seed" `
    -BuildRoot (Join-Path $here "build-publication") -RequireCompiler $recordCompiler -RequireInputs (Join-Path $out "gate.json")
exit $LASTEXITCODE
