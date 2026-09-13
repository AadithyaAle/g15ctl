"""Background service: state restore, thermal watchdog, optional fan curve.

The daemon has three jobs, in order of importance:

1. **Restore** the last-used mode at boot and after resume, so the machine
   behaves the way you left it (this is what AWCC does on Windows).
2. **Protect** the hardware. If we are holding a manual fan boost and
   temperatures climb anyway, the watchdog escalates to full boost and then
   hands control back to the firmware entirely. A user-space fan controller
   must never be the reason a laptop overheats.
3. **Follow a fan curve**, if one is enabled.

It is deliberately conservative: if anything goes wrong it releases control
rather than holding a possibly-wrong fan speed.
"""

from __future__ import annotations

import logging
import signal
import threading
import time

from . import backends, constants as C, controller as ctl, sensors
from .curve import CurveController, CurveError

log = logging.getLogger(__name__)


class Daemon:
    def __init__(self, controller: ctl.Controller):
        self.controller = controller
        self.config = controller.config
        self._stop = threading.Event()
        self._hot_samples = 0
        self._watchdog_engaged = False
        self._curve: CurveController | None = None
        self._last_applied: int | None = None

        curve_cfg = self.config.get("curve", {})
        if curve_cfg.get("enabled"):
            if not controller.backend.supports(backends.CAP_FAN_BOOST):
                log.warning(
                    "fan curve is enabled in config but backend %s cannot set fan "
                    "speed; running in monitor-only mode", controller.backend.name
                )
            else:
                try:
                    self._curve = CurveController(
                        curve_cfg.get("points", []),
                        curve_cfg.get("hysteresis_c", 3.0),
                    )
                    log.info("fan curve active: %s", self._curve.points)
                except CurveError as e:
                    log.error("invalid fan curve, ignoring it: %s", e)

    # -- lifecycle ----------------------------------------------------------

    def install_signal_handlers(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, self._on_signal)

    def _on_signal(self, signum, _frame) -> None:
        log.info("received %s, shutting down", signal.Signals(signum).name)
        self._stop.set()

    def stop(self) -> None:
        self._stop.set()

    # -- main loop ----------------------------------------------------------

    def run(self) -> int:
        interval = float(self.config.get("curve", {}).get("interval_s", 3.0))
        interval = max(1.0, min(60.0, interval))

        log.info("g15ctl %s starting (backend=%s, caps=%s)",
                 C.APP_VERSION, self.controller.backend.name,
                 ",".join(sorted(self.controller.backend.caps)))

        try:
            self.controller.restore()
        except Exception as e:
            log.error("state restore failed: %s", e)

        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:
                # A transient ACPI failure must not kill the service, but we
                # must not keep asserting a fan speed we cannot verify either.
                log.error("tick failed (%s); releasing fan control", e)
                self._release()
            self._stop.wait(interval)

        self._shutdown()
        return 0

    def _shutdown(self) -> None:
        if self.config.get("reset_on_shutdown", True):
            # This is what keeps a dual-booted Windows + AWCC clean: G-Mode and
            # fan boost live in volatile EC state that survives a warm reboot.
            log.info("resetting fan boost and G-Mode before exit")
            try:
                self.controller.backend.reset()
            except Exception as e:
                log.error("reset on shutdown failed: %s", e)
        log.info("g15ctl stopped")

    # -- one iteration ------------------------------------------------------

    def tick(self) -> None:
        snap = sensors.snapshot()
        hottest = snap.hottest
        if hottest is None:
            log.debug("no usable temperature sensor this tick")
            return
        label, temp = hottest

        if self._run_watchdog(label, temp):
            return
        self._run_curve(snap, label, temp)

    # -- watchdog -----------------------------------------------------------

    def _run_watchdog(self, label: str, temp: float) -> bool:
        """Return True if the watchdog took over this tick."""
        wd = self.config.get("watchdog", {})
        if not wd.get("enabled", True):
            return False
        if not self.controller.backend.supports(backends.CAP_FAN_BOOST):
            return False

        critical = float(wd.get("critical_c", C.WATCHDOG_TEMP_C))
        force = float(wd.get("force_boost_c", C.WATCHDOG_BOOST_TEMP_C))

        if temp < force:
            if self._watchdog_engaged and temp < force - 5:
                log.info("temperatures normal again (%s %.0f C); watchdog standing down",
                         label, temp)
                self._watchdog_engaged = False
                if self._curve:
                    self._curve.reset()
            self._hot_samples = 0
            return False

        self._hot_samples += 1
        if self._hot_samples < C.WATCHDOG_SAMPLES:
            log.debug("%s at %.0f C (%d/%d samples over %.0f C)",
                      label, temp, self._hot_samples, C.WATCHDOG_SAMPLES, force)
            return False

        self._watchdog_engaged = True
        if temp >= critical:
            # Past this point we stop trusting ourselves. Give the firmware
            # full authority; its curve is thermally validated and ours is not.
            log.warning("%s reached %.0f C (critical %.0f C); releasing control "
                        "to firmware", label, temp, critical)
            self._release()
            return True

        log.warning("%s at %.0f C (>= %.0f C); forcing fans to maximum",
                    label, temp, force)
        self._apply(100)
        return True

    # -- curve --------------------------------------------------------------

    def _run_curve(self, snap: sensors.Snapshot, label: str, temp: float) -> None:
        if self._curve is None:
            return
        sensor = self.config.get("curve", {}).get("sensor", "hottest")
        if sensor != "hottest":
            value = snap.temps.get(sensor)
            if value is None:
                log.warning("curve sensor %r not found; using hottest (%s)", sensor, label)
            else:
                temp, label = value, sensor
        percent = self._curve.update(temp)
        self._apply(percent, label, temp)

    # -- actuation ----------------------------------------------------------

    def _apply(self, percent: float, label: str | None = None,
               temp: float | None = None) -> None:
        boost = ctl.percent_to_boost(percent)
        if boost == self._last_applied:
            return
        self.controller.backend.set_fan_boost(boost)
        self._last_applied = boost
        if label is not None and temp is not None:
            log.info("fan -> %d%% (boost %d/255) for %s at %.0f C",
                     round(percent), boost, label, temp)
        else:
            log.info("fan -> %d%% (boost %d/255)", round(percent), boost)

    def _release(self) -> None:
        if self._last_applied == 0:
            return
        try:
            self.controller.backend.set_fan_boost(0)
            self._last_applied = 0
            if self._curve:
                self._curve.reset()
        except Exception as e:
            log.error("could not release fan control: %s", e)


def run(preferred_backend: str | None = None) -> int:
    controller = ctl.Controller.open(preferred_backend)
    daemon = Daemon(controller)
    daemon.install_signal_handlers()
    return daemon.run()


def run_restore(preferred_backend: str | None = None) -> int:
    """One-shot: reapply saved state. Used by the boot/resume units."""
    controller = ctl.Controller.open(preferred_backend)
    controller.restore()
    return 0


def run_reset(preferred_backend: str | None = None) -> int:
    """One-shot: hand control back to the firmware.

    Run before shutdown/reboot and before suspend so that neither Windows nor
    the firmware inherits a fan boost or G-Mode that we set.
    """
    controller = ctl.Controller.open(preferred_backend)
    controller.reset()
    return 0
