"""Unprivileged sensor collection from sysfs.

Everything here is readable as a normal user, which matters: the monitoring
side of the tool (`g15ctl status`, `g15ctl monitor`, the tray applet) must not
require root. Only the *control* paths need privileges.

On the Dell G15 5530 the useful sources are:
  dell_ddv   fan1/fan2 RPM plus 7 labelled temperatures, straight from the EC
  coretemp   per-core and package temperatures
  nvme       SSD composite temperatures
  iwlwifi    Wi-Fi module temperature
  BAT0       charge, cycle count and true capacity health
"""

from __future__ import annotations

import glob
import os
from dataclasses import dataclass, field

from . import constants as C


def _read(path: str) -> str | None:
    try:
        with open(path) as f:
            return f.read().strip()
    except (OSError, UnicodeDecodeError):
        return None


def _read_int(path: str) -> int | None:
    raw = _read(path)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def hwmon_by_name(name: str) -> str | None:
    """Return the hwmonN directory whose `name` file matches, or None.

    hwmon numbering is not stable across boots, so we always resolve by name
    instead of hardcoding e.g. hwmon6.
    """
    for path in sorted(glob.glob(os.path.join(C.HWMON_ROOT, "hwmon*"))):
        if _read(os.path.join(path, "name")) == name:
            return path
    return None


def all_hwmon_names() -> dict[str, str]:
    out = {}
    for path in sorted(glob.glob(os.path.join(C.HWMON_ROOT, "hwmon*"))):
        name = _read(os.path.join(path, "name"))
        if name:
            out.setdefault(name, path)
    return out


@dataclass
class Fan:
    label: str
    rpm: int
    max_rpm: int | None = None
    boost: int | None = None

    @property
    def percent(self) -> int | None:
        """Fan speed as a percentage of the firmware-reported maximum."""
        if not self.max_rpm:
            return None
        return max(0, min(100, round(self.rpm * 100 / self.max_rpm)))


@dataclass
class Battery:
    percent: int | None = None
    status: str | None = None
    cycles: int | None = None
    temp_c: float | None = None
    charge_full: int | None = None
    charge_design: int | None = None

    @property
    def health_percent(self) -> int | None:
        """Remaining capacity vs factory design capacity."""
        if not self.charge_full or not self.charge_design:
            return None
        return round(self.charge_full * 100 / self.charge_design)


@dataclass
class Snapshot:
    temps: dict[str, float] = field(default_factory=dict)
    fans: list[Fan] = field(default_factory=list)
    battery: Battery = field(default_factory=Battery)
    on_ac: bool | None = None
    cpu_governor: str | None = None
    cpu_mhz: float | None = None

    # -- convenience accessors used by the UI and the fan curve -------------

    @property
    def cpu_temp(self) -> float | None:
        for key in ("CPU", "Package id 0", "CPU Package"):
            if key in self.temps:
                return self.temps[key]
        return None

    @property
    def gpu_temp(self) -> float | None:
        for key in ("GPU", "Video"):
            if key in self.temps:
                return self.temps[key]
        return None

    @property
    def hottest(self) -> tuple[str, float] | None:
        """The hottest sensor that reflects a thermal zone we can cool.

        Excludes the charger and battery, which run warm for reasons unrelated
        to fan speed and would otherwise skew a fan curve.
        """
        ignored = ("Charger", "Battery", "SODIMM")
        candidates = {
            k: v for k, v in self.temps.items()
            if not any(k.startswith(p) for p in ignored)
        }
        if not candidates:
            return None
        key = max(candidates, key=candidates.get)
        return key, candidates[key]


#: dell_ddv uses the EC's own wording; normalise it to match the fan labels so
#: the UI reads consistently ("GPU" next to the GPU fan, not "Video").
_LABEL_ALIASES = {"Video": "GPU"}


def _collect_hwmon_temps(path: str, out: dict[str, float], prefix: str = "") -> None:
    for tin in sorted(glob.glob(os.path.join(path, "temp*_input"))):
        raw = _read_int(tin)
        if raw is None:
            continue
        label = _read(tin.replace("_input", "_label"))
        if not label:
            label = os.path.basename(tin).split("_")[0]
        # dell_ddv reports two sensors it cannot name; skip rather than show
        # "Unknown" twice in the UI.
        if label == "Unknown":
            continue
        label = _LABEL_ALIASES.get(label, label)
        key = ("%s %s" % (prefix, label)).strip()
        out[key] = raw / 1000.0


def read_temps() -> dict[str, float]:
    """Collect temperatures from every interesting hwmon device."""
    temps: dict[str, float] = {}
    available = all_hwmon_names()

    # dell_ddv is authoritative for CPU/GPU/SODIMM/Charger: it is the EC's own
    # view, the same numbers AWCC shows on Windows.
    if "dell_ddv" in available:
        _collect_hwmon_temps(available["dell_ddv"], temps)

    if "coretemp" in available:
        pkg = temps_from_label(available["coretemp"], "Package id 0")
        if pkg is not None:
            temps.setdefault("CPU Package", pkg)

    nvme_index = 0
    for path in sorted(glob.glob(os.path.join(C.HWMON_ROOT, "hwmon*"))):
        if _read(os.path.join(path, "name")) != "nvme":
            continue
        nvme_index += 1
        value = temps_from_label(path, "Composite")
        if value is not None:
            temps["NVMe %d" % nvme_index] = value

    if "iwlwifi_1" in available:
        value = _read_int(os.path.join(available["iwlwifi_1"], "temp1_input"))
        if value is not None:
            temps["Wi-Fi"] = value / 1000.0

    return temps


def temps_from_label(hwmon_path: str, label: str) -> float | None:
    for lbl in sorted(glob.glob(os.path.join(hwmon_path, "temp*_label"))):
        if _read(lbl) == label:
            value = _read_int(lbl.replace("_label", "_input"))
            return None if value is None else value / 1000.0
    return None


def read_fans(max_rpms: dict[int, int] | None = None,
              boosts: dict[int, int] | None = None) -> list[Fan]:
    """Read fan RPM from dell_ddv, enriched with AWCC max/boost when known.

    dell_ddv gives us RPM without root, so the dashboard works unprivileged.
    max_rpms/boosts come from the AWCC backend when it is available.
    """
    fans: list[Fan] = []
    path = hwmon_by_name("dell_ddv")
    if path is None:
        return fans
    ids = list(C.DEFAULT_FAN_IDS)
    for idx, fin in enumerate(sorted(glob.glob(os.path.join(path, "fan*_input")))):
        rpm = _read_int(fin)
        if rpm is None:
            continue
        label = _read(fin.replace("_input", "_label")) or os.path.basename(fin)
        label = label.replace(" Fan", "").replace("Video", "GPU")
        fan_id = ids[idx] if idx < len(ids) else None
        fans.append(Fan(
            label=label,
            rpm=rpm,
            max_rpm=(max_rpms or {}).get(fan_id, C.DEFAULT_FAN_MAX_RPM),
            boost=(boosts or {}).get(fan_id),
        ))
    return fans


def read_battery() -> Battery:
    base = "/sys/class/power_supply/BAT0"
    if not os.path.isdir(base):
        return Battery()
    temp = _read_int(os.path.join(base, "temp"))
    return Battery(
        percent=_read_int(os.path.join(base, "capacity")),
        status=_read(os.path.join(base, "status")),
        cycles=_read_int(os.path.join(base, "cycle_count")),
        temp_c=None if temp is None else temp / 10.0,
        charge_full=_read_int(os.path.join(base, "charge_full")),
        charge_design=_read_int(os.path.join(base, "charge_full_design")),
    )


def read_on_ac() -> bool | None:
    for name in ("AC", "ADP1", "ACAD"):
        value = _read_int("/sys/class/power_supply/%s/online" % name)
        if value is not None:
            return bool(value)
    return None


def read_cpu() -> tuple[str | None, float | None]:
    governor = _read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
    freqs = []
    for path in glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq"):
        value = _read_int(path)
        if value:
            freqs.append(value)
    if not freqs:
        # Fall back to /proc/cpuinfo on systems without cpufreq sysfs.
        raw = _read("/proc/cpuinfo") or ""
        for line in raw.splitlines():
            if line.lower().startswith("cpu mhz"):
                try:
                    freqs.append(int(float(line.split(":")[1]) * 1000))
                except (ValueError, IndexError):
                    pass
    avg = sum(freqs) / len(freqs) / 1000.0 if freqs else None
    return governor, avg


def snapshot(max_rpms: dict[int, int] | None = None,
             boosts: dict[int, int] | None = None) -> Snapshot:
    """Take one complete, unprivileged reading of the machine."""
    governor, mhz = read_cpu()
    return Snapshot(
        temps=read_temps(),
        fans=read_fans(max_rpms, boosts),
        battery=read_battery(),
        on_ac=read_on_ac(),
        cpu_governor=governor,
        cpu_mhz=mhz,
    )
