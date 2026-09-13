"""Thermal control backends, in order of capability.

Three mechanisms can drive thermal state on a Dell G15. They are not
equivalent, so the tool detects what is present rather than assuming:

AwccAcpi (preferred)
    Calls the firmware's WMAX method directly through acpi_call. This is the
    only backend that can do everything AWCC does on Windows: select thermal
    profiles, toggle G-Mode, and set a per-fan boost. It also reports each
    fan's true maximum RPM, which is what makes a real percentage readout
    possible.

NativeProfile
    The in-tree alienware-wmi driver, loaded with force_platform_profile=1.
    Exposes the AWCC profiles through the standard platform_profile class, and
    G-Mode as the `performance` choice when force_gmode=1. On kernels before
    6.15 it cannot do manual fan control; from 6.15 onward, force_hwmon=1 adds
    `fanN_boost` attributes, which this backend will use automatically.

DellPc (fallback)
    The generic dell-pc SMBIOS thermal driver present on every modern Dell.
    Profiles only (cool/quiet/balanced/performance), no G-Mode, no fan control.
    Always available, so the tool degrades rather than failing.
"""

from __future__ import annotations

import glob
import logging
import os
from abc import ABC, abstractmethod

from . import constants as C
from . import sensors
from .acpi import AcpiError, Wmax

log = logging.getLogger(__name__)

CAP_PROFILES = "profiles"
CAP_GMODE = "gmode"
CAP_FAN_BOOST = "fan_boost"
CAP_FAN_LIMITS = "fan_limits"


class BackendError(RuntimeError):
    pass


class Backend(ABC):
    name = "abstract"
    caps: frozenset[str] = frozenset()

    # -- discovery ----------------------------------------------------------

    @classmethod
    @abstractmethod
    def probe(cls) -> "Backend | None":
        """Return a ready instance, or None if this mechanism is unavailable."""

    def supports(self, cap: str) -> bool:
        return cap in self.caps

    # -- thermal profiles ---------------------------------------------------

    @abstractmethod
    def list_modes(self) -> list[str]:
        """Canonical mode names this backend can select."""

    @abstractmethod
    def get_mode(self) -> str | None:
        ...

    @abstractmethod
    def set_mode(self, mode: str) -> None:
        ...

    # -- G-Mode -------------------------------------------------------------

    def get_gmode(self) -> bool | None:
        return None

    def set_gmode(self, enabled: bool) -> None:
        raise BackendError("%s cannot control G-Mode" % self.name)

    # -- fans ---------------------------------------------------------------

    @property
    def fan_ids(self) -> tuple[int, ...]:
        return ()

    def get_fan_boost(self) -> dict[int, int]:
        return {}

    def set_fan_boost(self, boost: int, fan_id: int | None = None) -> None:
        raise BackendError("%s cannot control fan speed" % self.name)

    def fan_max_rpm(self) -> dict[int, int]:
        return {}

    # -- safety -------------------------------------------------------------

    def reset(self) -> None:
        """Hand thermal control back to the firmware.

        Called on shutdown, on resume, and by the watchdog. Must be safe to
        call repeatedly and must leave no state that a reboot into Windows
        would inherit.
        """
        if self.supports(CAP_FAN_BOOST):
            try:
                self.set_fan_boost(0)
            except BackendError:
                pass
        if self.supports(CAP_GMODE):
            try:
                self.set_gmode(False)
            except BackendError:
                pass


# ---------------------------------------------------------------------------
# AWCC over acpi_call
# ---------------------------------------------------------------------------


class AwccAcpiBackend(Backend):
    name = "awcc-acpi"
    caps = frozenset({CAP_PROFILES, CAP_GMODE, CAP_FAN_BOOST, CAP_FAN_LIMITS})

    def __init__(self, wmax: Wmax):
        self.wmax = wmax
        self._fan_ids: tuple[int, ...] = ()
        self._temp_ids: tuple[int, ...] = ()
        self._profile_ids: dict[str, int] = {}
        self._max_rpm: dict[int, int] = {}
        self._enumerate()

    @classmethod
    def probe(cls) -> "AwccAcpiBackend | None":
        if os.geteuid() != 0:
            return None
        try:
            return cls(Wmax.detect())
        except AcpiError as e:
            log.debug("AWCC/acpi_call backend unavailable: %s", e)
            return None

    # -- firmware self-description -----------------------------------------

    def _enumerate(self) -> None:
        """Read the firmware's resource table instead of hardcoding IDs.

        0x14/0x03 walks a table of resource IDs; entries that match a known
        profile ID are thermal profiles, and the rest are fans or sensors,
        which we tell apart by asking for an RPM.
        """
        resources: list[int] = []
        for index in range(32):
            value = self.wmax.query(C.M_THERMAL_INFO, C.OP_RESOURCE_ID, index)
            if value is None:
                break
            resources.append(value)

        profiles: dict[str, int] = {}
        fans: list[int] = []
        temps: list[int] = []
        for rid in resources:
            if rid in C.PROFILE_IDS:
                profiles[C.PROFILE_IDS[rid]] = rid
                continue
            rpm = self.wmax.query(C.M_THERMAL_INFO, C.OP_GET_RPM, rid & 0xFF)
            if rpm is not None and 0 <= rpm <= 12000:
                fans.append(rid & 0xFF)
            else:
                temp = self.wmax.query(C.M_THERMAL_INFO, C.OP_GET_TEMP, rid & 0xFF)
                if temp is not None and 0 < temp < 150:
                    temps.append(rid & 0xFF)

        self._profile_ids = profiles or {
            C.PROFILE_IDS[i]: i for i in (0xA0, 0xA1, 0xA3, 0xA5)
        }
        # Deduplicate while preserving order; resource IDs can appear both as
        # 0x32 and as 0x101-style aliases that mask down to the same sensor.
        self._fan_ids = tuple(dict.fromkeys(fans)) or C.DEFAULT_FAN_IDS
        self._temp_ids = tuple(dict.fromkeys(temps)) or C.DEFAULT_TEMP_IDS

        for fan in self._fan_ids:
            value = self.wmax.query(C.M_THERMAL_INFO, C.OP_GET_FAN_MAX_RPM, fan)
            self._max_rpm[fan] = value or C.DEFAULT_FAN_MAX_RPM

        log.debug("AWCC enumerated: profiles=%s fans=%s temps=%s max_rpm=%s",
                  self._profile_ids, self._fan_ids, self._temp_ids, self._max_rpm)

    # -- profiles -----------------------------------------------------------

    def list_modes(self) -> list[str]:
        return sorted(self._profile_ids) + [C.MODE_GMODE]

    def get_mode(self) -> str | None:
        # G-Mode must be checked through its own method, not inferred from the
        # profile. Measured on a G15 5530 (BIOS 1.34.0): with Game Shift ON,
        # 0x25/0x02 returns 1 but 0x14/0x0B still reports the underlying
        # profile (0xA0), so G-Mode is an overlay rather than a profile value.
        # Some firmwares do report 0xAB, so we still honour that below.
        if self.get_gmode():
            return C.MODE_GMODE
        value = self.wmax.query(C.M_THERMAL_INFO, C.OP_GET_CURRENT_PROFILE)
        if value is None:
            return None
        if value == C.PROFILE_GMODE:
            return C.MODE_GMODE
        return C.PROFILE_IDS.get(value, "unknown(%#x)" % value)

    def set_mode(self, mode: str) -> None:
        if mode == C.MODE_GMODE:
            self.set_gmode(True)
            return
        if mode not in self._profile_ids:
            raise BackendError(
                "profile %r is not supported by this firmware (have: %s)"
                % (mode, ", ".join(self.list_modes()))
            )
        # Leaving G-Mode implicitly when picking another profile matches AWCC.
        if self.get_gmode():
            self.set_gmode(False)
        result = self.wmax.call(
            C.M_THERMAL_CONTROL, C.OP_ACTIVATE_PROFILE, self._profile_ids[mode]
        )
        if result in C.WMAX_ERRORS:
            raise BackendError("firmware rejected profile %s (%#x)" % (mode, result))

    # -- G-Mode -------------------------------------------------------------

    def get_gmode(self) -> bool | None:
        value = self.wmax.query(C.M_GAME_SHIFT, C.OP_GET_GAME_SHIFT)
        return None if value is None else bool(value)

    def set_gmode(self, enabled: bool) -> None:
        result = self.wmax.call(C.M_GAME_SHIFT, C.OP_SET_GAME_SHIFT, 1 if enabled else 0)
        if result in C.WMAX_ERRORS:
            raise BackendError("firmware rejected G-Mode toggle (%#x)" % result)

    # -- fans ---------------------------------------------------------------

    @property
    def fan_ids(self) -> tuple[int, ...]:
        return self._fan_ids

    def get_fan_boost(self) -> dict[int, int]:
        out = {}
        for fan in self._fan_ids:
            value = self.wmax.query(C.M_THERMAL_INFO, C.OP_GET_FAN_BOOST, fan)
            if value is not None:
                out[fan] = value
        return out

    def set_fan_boost(self, boost: int, fan_id: int | None = None) -> None:
        boost = max(C.BOOST_MIN, min(C.BOOST_MAX, int(boost)))
        targets = self._fan_ids if fan_id is None else (fan_id,)
        for fan in targets:
            result = self.wmax.call(
                C.M_THERMAL_CONTROL, C.OP_SET_FAN_BOOST, fan, boost
            )
            if result in C.WMAX_ERRORS:
                raise BackendError(
                    "firmware rejected boost %d on fan %#x (%#x)" % (boost, fan, result)
                )

    def fan_max_rpm(self) -> dict[int, int]:
        return dict(self._max_rpm)

    def fan_rpm(self) -> dict[int, int]:
        out = {}
        for fan in self._fan_ids:
            value = self.wmax.query(C.M_THERMAL_INFO, C.OP_GET_RPM, fan)
            if value is not None:
                out[fan] = value
        return out


# ---------------------------------------------------------------------------
# platform_profile backends
# ---------------------------------------------------------------------------


class _PlatformProfileBackend(Backend):
    """Shared logic for sysfs platform_profile drivers."""

    driver_name = ""
    caps = frozenset({CAP_PROFILES})

    #: Driver-specific renaming of platform_profile choices. Empty means the
    #: sysfs names are already canonical.
    profile_map: dict[str, str] = {}

    def __init__(self, path: str):
        self.path = path
        self._boost_attrs: dict[int, str] = {}

    @classmethod
    def _find(cls, driver_name: str) -> str | None:
        for d in sorted(glob.glob(os.path.join(C.PLATFORM_PROFILE_CLASS, "*"))):
            if sensors._read(os.path.join(d, "name")) == driver_name:
                return d
        return None

    def _choices(self) -> list[str]:
        raw = sensors._read(os.path.join(self.path, "choices")) or ""
        return raw.split()

    def list_modes(self) -> list[str]:
        return [self.profile_map.get(c, c) for c in self._choices()]

    def get_mode(self) -> str | None:
        raw = sensors._read(os.path.join(self.path, "profile"))
        if raw is None:
            return None
        return self.profile_map.get(raw, raw)

    def set_mode(self, mode: str) -> None:
        reverse = {v: k for k, v in self.profile_map.items()}
        target = reverse.get(mode, mode)
        if target not in self._choices():
            raise BackendError(
                "profile %r unsupported by %s (have: %s)"
                % (mode, self.driver_name, ", ".join(self.list_modes()))
            )
        try:
            with open(os.path.join(self.path, "profile"), "w") as f:
                f.write(target)
        except OSError as e:
            raise BackendError("could not set profile: %s" % e) from e


class NativeProfileBackend(_PlatformProfileBackend):
    """The in-tree alienware-wmi AWCC driver."""

    name = "awcc-native"
    driver_name = "alienware-wmi"
    profile_map = C.AWCC_NATIVE_PROFILE_MAP

    def __init__(self, path: str):
        super().__init__(path)
        caps = {CAP_PROFILES}
        # G-Mode is surfaced as the `performance` choice by force_gmode=1.
        if "performance" in self._choices():
            caps.add(CAP_GMODE)
        # Kernel >= 6.15 adds fanN_boost when loaded with force_hwmon=1.
        hwmon = sensors.hwmon_by_name("alienware_wmi")
        if hwmon:
            for attr in sorted(glob.glob(os.path.join(hwmon, "fan*_boost"))):
                index = os.path.basename(attr).removeprefix("fan").split("_")[0]
                try:
                    self._boost_attrs[int(index)] = attr
                except ValueError:
                    continue
            if self._boost_attrs:
                caps |= {CAP_FAN_BOOST}
        self.caps = frozenset(caps)

    @classmethod
    def probe(cls) -> "NativeProfileBackend | None":
        path = cls._find(cls.driver_name)
        return cls(path) if path else None

    def get_gmode(self) -> bool | None:
        if CAP_GMODE not in self.caps:
            return None
        return self.get_mode() == C.MODE_GMODE

    def set_gmode(self, enabled: bool) -> None:
        if CAP_GMODE not in self.caps:
            raise BackendError("G-Mode unavailable; load alienware_wmi with force_gmode=1")
        self.set_mode(C.MODE_GMODE if enabled else "balanced")

    @property
    def fan_ids(self) -> tuple[int, ...]:
        return tuple(sorted(self._boost_attrs))

    def get_fan_boost(self) -> dict[int, int]:
        out = {}
        for index, attr in self._boost_attrs.items():
            value = sensors._read_int(attr)
            if value is not None:
                out[index] = value
        return out

    def set_fan_boost(self, boost: int, fan_id: int | None = None) -> None:
        if not self._boost_attrs:
            raise BackendError(
                "this kernel's alienware-wmi cannot set fan speed; "
                "kernel >= 6.15 with force_hwmon=1 is required"
            )
        boost = max(C.BOOST_MIN, min(C.BOOST_MAX, int(boost)))
        targets = self._boost_attrs if fan_id is None else {fan_id: self._boost_attrs[fan_id]}
        for attr in targets.values():
            try:
                with open(attr, "w") as f:
                    f.write(str(boost))
            except OSError as e:
                raise BackendError("could not write %s: %s" % (attr, e)) from e


class DellPcBackend(_PlatformProfileBackend):
    """Generic Dell SMBIOS thermal driver. Profiles only."""

    name = "dell-pc"
    driver_name = "dell-pc"

    @classmethod
    def probe(cls) -> "DellPcBackend | None":
        path = cls._find(cls.driver_name)
        if path:
            return cls(path)
        # Older kernels expose only the legacy firmware-wide node.
        if os.path.exists(C.PLATFORM_PROFILE_LEGACY):
            return _LegacyAcpiProfileBackend("/sys/firmware/acpi")
        return None


class _LegacyAcpiProfileBackend(_PlatformProfileBackend):
    name = "acpi-legacy"
    driver_name = "acpi"

    def _choices(self) -> list[str]:
        raw = sensors._read(C.PLATFORM_PROFILE_LEGACY_CHOICES) or ""
        return raw.split()

    def get_mode(self) -> str | None:
        raw = sensors._read(C.PLATFORM_PROFILE_LEGACY)
        return None if raw is None else self.profile_map.get(raw, raw)

    def set_mode(self, mode: str) -> None:
        reverse = {v: k for k, v in self.profile_map.items()}
        target = reverse.get(mode, mode)
        if target not in self._choices():
            raise BackendError("profile %r unsupported (have: %s)"
                               % (mode, ", ".join(self._choices())))
        try:
            with open(C.PLATFORM_PROFILE_LEGACY, "w") as f:
                f.write(target)
        except OSError as e:
            raise BackendError("could not set profile: %s" % e) from e


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

#: Most capable first.
BACKEND_ORDER = (AwccAcpiBackend, NativeProfileBackend, DellPcBackend)

BACKENDS_BY_NAME = {
    "awcc-acpi": AwccAcpiBackend,
    "awcc-native": NativeProfileBackend,
    "dell-pc": DellPcBackend,
}


def detect(preferred: str | None = None) -> Backend:
    """Pick the most capable backend that actually works on this machine."""
    if preferred:
        cls = BACKENDS_BY_NAME.get(preferred)
        if cls is None:
            raise BackendError(
                "unknown backend %r (choose from: %s)"
                % (preferred, ", ".join(BACKENDS_BY_NAME))
            )
        backend = cls.probe()
        if backend is None:
            raise BackendError("backend %r is not available on this system" % preferred)
        return backend

    for cls in BACKEND_ORDER:
        backend = cls.probe()
        if backend is not None:
            log.debug("selected backend %s", backend.name)
            return backend
    raise BackendError(
        "no thermal control interface found. This tool targets Dell G-series "
        "laptops; run `g15ctl doctor` for details."
    )


def detect_all() -> list[Backend]:
    """Every available backend, for diagnostics."""
    found = []
    for cls in BACKEND_ORDER:
        try:
            backend = cls.probe()
        except Exception as e:  # diagnostics must never crash
            log.debug("probe of %s raised: %s", cls.__name__, e)
            continue
        if backend is not None:
            found.append(backend)
    return found
