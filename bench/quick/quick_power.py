"""The default power settings of run R1 (quick-plan.md): set for one condition block, then set back.

The lab sets its own power settings for every job before the run starts (lab/laptop/README.md): on Arch
every governor at performance, boost off and each CPU's scaling_min_freq at its maximum; on Windows the
lab plan b1bcd431. Both record what the host had before. The "power" condition puts those recorded
values back for its block and restores the lab's values after it:

- Arch: per CPU the governor from LAB_POWER_RECORD's "before" (labpower.sh), boost from the same record,
  and scaling_min_freq from IOCORE_CLOCK_RECORD's "before" (clockfloor.sh), written with `sudo -n tee` as
  labpower.sh writes them. labpower.sh and clockfloor.sh set the host back again at the job's end, so a
  runner that dies mid-block leaves nothing changed.
- Windows: the plan saved in C:/lab/power-restore.json (labjob.py saves the plan that was active before
  the job), made active with powercfg /setactive; labjob.py restores it at the job's end anyway.

Each value is read back; a mismatch raises, after every value written is set back. Nothing persistent
changes. Tests pass a fake sysfs root, an empty sudo and a fake powercfg.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

GUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


class PowerError(RuntimeError):
    pass


class LinuxPower:
    def __init__(self, root: str = "/sys/devices/system/cpu", sudo: tuple[str, ...] = ("sudo", "-n"),
                 power_record: str | None = None, clock_record: str | None = None):
        self.root = Path(root)
        self.sudo = tuple(sudo)
        self.power_record = power_record if power_record is not None else os.environ.get("LAB_POWER_RECORD", "")
        self.clock_record = clock_record if clock_record is not None else os.environ.get("IOCORE_CLOCK_RECORD", "")

    def targets(self) -> dict[str, str]:
        """Path to the value the host had before the lab's settings, in the order they are written."""
        if not self.power_record or not Path(self.power_record).is_file():
            raise PowerError("no LAB_POWER_RECORD file: the host's own governors and boost are unknown")
        if not self.clock_record or not Path(self.clock_record).is_file():
            raise PowerError("no IOCORE_CLOCK_RECORD file: the host's own scaling_min_freq is unknown")
        before = json.loads(Path(self.power_record).read_text(encoding="utf-8")).get("before") or {}
        cbefore = json.loads(Path(self.clock_record).read_text(encoding="utf-8")).get("before") or {}
        govs = before.get("governor_by_cpu") or {}
        cpus = cbefore.get("cpus") or {}
        if not govs or not cpus:
            raise PowerError("the lab records hold no per-CPU before values")
        out: dict[str, str] = {}
        boost = self.root / "cpufreq" / "boost"
        if before.get("boost") is not None and boost.exists():
            out[str(boost)] = str(before["boost"])  # first: boost moves scaling_max_freq
        for c in sorted(govs, key=int):
            if govs[c]:
                out[str(self.root / f"cpu{c}" / "cpufreq" / "scaling_governor")] = str(govs[c])
        for c in sorted(cpus, key=int):
            v = (cpus[c] or {}).get("scaling_min_freq")
            if v is not None:
                out[str(self.root / f"cpu{c}" / "cpufreq" / "scaling_min_freq")] = str(v)
        return out

    def read(self, path: str) -> str:
        return Path(path).read_text(encoding="utf-8").strip()

    def write(self, path: str, value: str) -> None:
        if self.sudo:
            subprocess.run([*self.sudo, "tee", path], input=value + "\n", text=True, stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE, check=False, timeout=30)
        else:
            Path(path).write_text(value + "\n", encoding="utf-8")

    def precheck(self) -> dict:
        """Before anything is timed: the records parse and sudo writes (cpu0's governor, its own value)."""
        t = self.targets()
        gov0 = str(self.root / "cpu0" / "cpufreq" / "scaling_governor")
        cur = self.read(gov0)
        self.write(gov0, cur)
        if self.read(gov0) != cur:
            raise PowerError(f"{gov0} did not keep its value after a write")
        return {"targets": t}

    def apply(self) -> dict:
        targets = self.targets()
        snap = {p: self.read(p) for p in targets}
        try:
            self._set(targets)
        except PowerError:
            self._set_back(snap)
            raise
        return {"snapshot": snap, "set": {p: self.read(p) for p in targets}}

    def restore(self, applied: dict) -> dict:
        snap = applied["snapshot"]
        self._set_back(snap)
        return {"restored": {p: self.read(p) for p in snap}}

    def _set(self, values: dict[str, str]) -> None:
        bad = []
        for p, v in values.items():
            if self.read(p) != v:
                self.write(p, v)
            if self.read(p) != v:
                bad.append(f"{p} reads {self.read(p)}, not {v}")
        if bad:
            raise PowerError("; ".join(bad))

    def _set_back(self, snap: dict[str, str]) -> None:
        self._set(dict(reversed(list(snap.items()))))


def run_powercfg(*args: str) -> str:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    p = subprocess.run(["powercfg", *args], capture_output=True, text=True, timeout=60, creationflags=flags)
    if p.returncode != 0:
        raise PowerError(f"powercfg {' '.join(args)}: exit {p.returncode}: {(p.stderr or p.stdout).strip()[-300:]}")
    return p.stdout


class WindowsPower:
    def __init__(self, restore_file: str = "C:/lab/power-restore.json", powercfg=run_powercfg):
        self.restore_file = Path(os.environ.get("QUICK_POWER_RESTORE", restore_file))
        self.powercfg = powercfg

    def active(self) -> str:
        m = GUID_RE.search(self.powercfg("/getactivescheme"))
        if not m:
            raise PowerError("no GUID in powercfg /getactivescheme")
        return m.group(0).lower()

    def target(self) -> str:
        if not self.restore_file.is_file():
            raise PowerError(f"no {self.restore_file}: the plan active before the job is unknown")
        plan = str(json.loads(self.restore_file.read_text(encoding="utf-8")).get("plan", "")).lower()
        if not GUID_RE.fullmatch(plan):
            raise PowerError(f"{self.restore_file} holds no plan GUID")
        return plan

    def precheck(self) -> dict:
        return {"target": self.target(), "active": self.active()}

    def apply(self) -> dict:
        target = self.target()
        before = self.active()
        self.powercfg("/setactive", target)
        now = self.active()
        if now != target:
            self.powercfg("/setactive", before)
            raise PowerError(f"active plan is {now}, not {target}")
        return {"snapshot": before, "set": now}

    def restore(self, applied: dict) -> dict:
        before = applied["snapshot"]
        self.powercfg("/setactive", before)
        now = self.active()
        if now != before:
            raise PowerError(f"active plan is {now} after the restore, not {before}")
        return {"restored": now}


def for_os(os_name: str):
    return LinuxPower() if os_name == "linux" else WindowsPower()
