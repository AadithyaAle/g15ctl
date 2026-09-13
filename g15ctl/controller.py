"""High-level control API shared by the CLI, TUI, daemon and tray applet.

This is the only layer that combines hardware access with policy, so that
behaviour cannot drift between the four front-ends.
"""

from __future__ import annotations

import logging
import os

from . import backends, constants as C, sensors, state

log = logging.getLogger(__name__)

#: How the fan is currently being driven.
FAN_AUTO = "auto"
FAN_MANUAL = "manual"


class ControlError(RuntimeError):
    pass


def percent_to_boost(percent: float) -> int:
    """Map a user-facing 0-100% onto the firmware's 0-255 boost byte."""
    percent = max(0.0, min(100.0, float(percent)))
    return round(percent * C.BOOST_MAX / 100.0)


def boost_to_percent(boost: int) -> int:
    return round(max(0, min(C.BOOST_MAX, int(boost))) * 100.0 / C.BOOST_MAX)


def normalise_mode(mode: str) -> str:
    """Resolve aliases and casing to a canonical mode name."""
    key = mode.strip().lower().replace("_", "-")
    return C.MODE_ALIASES.get(key, key)


class Controller:
    def __init__(self, backend: backends.Backend, config: dict | None = None):
        self.backend = backend
        self.config = config if config is not None else state.load_config()

    @classmethod
    def open(cls, preferred: str | None = None, config: dict | None = None) -> "Controller":
        return cls(backends.detect(preferred), config)

    # -- introspection ------------------------------------------------------

    @property
    def modes(self) -> list[str]:
        return self.backend.list_modes()

    def require_root(self, action: str) -> None:
        if os.geteuid() != 0:
            raise ControlError(
                "%s requires root privileges. Re-run with: sudo %s ..."
                % (action, C.APP_NAME)
            )

    # -- modes --------------------------------------------------------------

    def get_mode(self) -> str | None:
        return self.backend.get_mode()

    def set_mode(self, mode: str, persist: bool = True) -> str:
        self.require_root("changing the thermal mode")
        canonical = normalise_mode(mode)
        available = self.backend.list_modes()
        if canonical not in available:
            raise ControlError(
                "unsupported mode %r.\nThis firmware supports: %s"
                % (mode, ", ".join(available))
            )
        # Selecting a firmware profile means the firmware owns the fans again,
        # so drop any manual boost we were holding. Otherwise a stale boost
        # would silently persist into the new profile.
        if canonical != C.MODE_GMODE and self.backend.supports(backends.CAP_FAN_BOOST):
            current = self.backend.get_fan_boost()
            if any(v for v in current.values()):
                log.info("clearing manual fan boost before switching profile")
                self.backend.set_fan_boost(0)

        self.backend.set_mode(canonical)
        applied = self.backend.get_mode() or canonical
        log.info("thermal mode set to %s (backend=%s)", applied, self.backend.name)
        if persist:
            state.save_state(mode=canonical, fan=FAN_AUTO,
                             gmode=(canonical == C.MODE_GMODE))
        return applied

    # -- G-Mode -------------------------------------------------------------

    def get_gmode(self) -> bool | None:
        return self.backend.get_gmode()

    def set_gmode(self, enabled: bool, persist: bool = True) -> None:
        self.require_root("toggling G-Mode")
        if not self.backend.supports(backends.CAP_GMODE):
            raise ControlError(
                "G-Mode is not available through the %s backend" % self.backend.name
            )
        self.backend.set_gmode(enabled)
        log.info("G-Mode %s", "enabled" if enabled else "disabled")
        if persist:
            state.save_state(gmode=enabled)

    def toggle_gmode(self) -> bool:
        current = bool(self.get_gmode())
        self.set_gmode(not current)
        return not current

    # -- fans ---------------------------------------------------------------

    def fan_mode(self) -> str:
        if not self.backend.supports(backends.CAP_FAN_BOOST):
            return FAN_AUTO
        boosts = self.backend.get_fan_boost()
        return FAN_MANUAL if any(v for v in boosts.values()) else FAN_AUTO

    def set_fan(self, percent: float | str, persist: bool = True) -> str:
        """Set a manual fan boost, or release control back to the firmware.

        Pass "auto" to release. Note this is a *boost* and not an absolute PWM:
        per the kernel's AWCC documentation the firmware applies roughly
        ``pwm = base + (boost/255) * (max - base)``, so it raises the floor of
        the firmware curve. The fan will still spin faster than requested if
        the firmware decides it needs to.
        """
        self.require_root("changing fan speed")
        if not self.backend.supports(backends.CAP_FAN_BOOST):
            raise ControlError(
                "manual fan control is unavailable through the %s backend.\n"
                "Install acpi-call-dkms for full control, or use kernel >= 6.15 "
                "with alienware_wmi force_hwmon=1." % self.backend.name
            )
        if isinstance(percent, str) and percent.strip().lower() in (FAN_AUTO, "off", "firmware"):
            self.backend.set_fan_boost(0)
            log.info("fan control released to firmware")
            if persist:
                state.save_state(fan=FAN_AUTO)
            return FAN_AUTO

        try:
            value = float(str(percent).rstrip("%"))
        except ValueError:
            raise ControlError("fan speed must be 0-100 or 'auto', got %r" % percent) from None
        if not 0 <= value <= 100:
            raise ControlError("fan speed must be between 0 and 100, got %g" % value)

        self.backend.set_fan_boost(percent_to_boost(value))
        log.info("fan boost set to %g%% (%d/255)", value, percent_to_boost(value))
        if persist:
            state.save_state(fan=value)
        return "%g%%" % value

    # -- safety -------------------------------------------------------------

    def reset(self) -> None:
        """Return the machine to firmware-managed thermals.

        Everything we touch is volatile EC state, but G-Mode and a fan boost
        can survive a warm reboot, so this runs before shutdown to guarantee
        a dual-booted Windows/AWCC starts from a clean slate.
        """
        log.info("resetting thermal state to firmware defaults")
        self.backend.reset()
        default = normalise_mode(self.config.get("default_mode", "balanced"))
        if default in self.backend.list_modes():
            try:
                self.backend.set_mode(default)
            except backends.BackendError as e:
                log.warning("could not restore default profile: %s", e)
        state.save_state(mode=default, fan=FAN_AUTO, gmode=False)

    def restore(self) -> None:
        """Reapply the last-used mode. Called at boot and after resume."""
        saved = state.load_state()
        if self.config.get("restore_last_mode", True) and saved.get("mode"):
            target = saved["mode"]
        else:
            target = self.config.get("default_mode", "balanced")
        target = normalise_mode(target)
        if target not in self.backend.list_modes():
            log.warning("saved mode %r unsupported; falling back to balanced", target)
            target = "balanced"
        try:
            self.backend.set_mode(target)
            log.info("restored thermal mode %s", target)
        except backends.BackendError as e:
            log.error("could not restore mode %s: %s", target, e)
            return
        # Reapply a manual fan boost only if it was explicitly set. We do not
        # restore it blindly after resume, because holding a boost across a
        # suspend cycle has no benefit and risks a surprise.
        fan = saved.get("fan")
        if isinstance(fan, (int, float)) and fan > 0 \
                and self.backend.supports(backends.CAP_FAN_BOOST):
            try:
                self.backend.set_fan_boost(percent_to_boost(fan))
                log.info("restored manual fan boost %g%%", fan)
            except backends.BackendError as e:
                log.warning("could not restore fan boost: %s", e)

    # -- reporting ----------------------------------------------------------

    def status(self) -> dict:
        """One complete picture of the machine, for every front-end."""
        max_rpms = self.backend.fan_max_rpm()
        boosts = self.backend.get_fan_boost() if os.geteuid() == 0 else {}
        snap = sensors.snapshot(max_rpms, boosts)

        fans = []
        for index, fan in enumerate(snap.fans):
            fan_id = self.backend.fan_ids[index] if index < len(self.backend.fan_ids) else None
            fans.append({
                "label": fan.label,
                "rpm": fan.rpm,
                "max_rpm": fan.max_rpm,
                "percent": fan.percent,
                "boost": fan.boost,
                "boost_percent": None if fan.boost is None else boost_to_percent(fan.boost),
                "id": None if fan_id is None else "%#x" % fan_id,
            })

        battery = snap.battery
        return {
            "backend": self.backend.name,
            "capabilities": sorted(self.backend.caps),
            "mode": self.get_mode(),
            "available_modes": self.backend.list_modes(),
            "gmode": self.get_gmode(),
            "fan_control": self.fan_mode(),
            "fans": fans,
            "temps": {k: round(v, 1) for k, v in sorted(snap.temps.items())},
            "hottest": snap.hottest,
            "battery": {
                "percent": battery.percent,
                "status": battery.status,
                "cycles": battery.cycles,
                "temp_c": battery.temp_c,
                "health_percent": battery.health_percent,
            },
            "on_ac": snap.on_ac,
            "cpu": {"governor": snap.cpu_governor, "mhz": snap.cpu_mhz},
        }
