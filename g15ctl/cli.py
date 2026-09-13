"""Command-line interface."""

from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys

from . import backends, constants as C, controller as ctl, logs, sensors, state
from .acpi import AcpiError

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(code: str, text: str) -> str:
    return "\033[%sm%s\033[0m" % (code, text) if _COLOR else text


def bold(t): return _c("1", t)
def dim(t): return _c("2", t)
def red(t): return _c("31", t)
def green(t): return _c("32", t)
def yellow(t): return _c("33", t)
def cyan(t): return _c("36", t)


def temp_colour(value: float) -> str:
    text = "%.0f C" % value
    if value >= 90:
        return red(text)
    if value >= 80:
        return yellow(text)
    if value >= 70:
        return cyan(text)
    return green(text)


def bar(percent: float | None, width: int = 22) -> str:
    """A unicode meter. Falls back to ASCII when the locale cannot encode it."""
    if percent is None:
        return dim("  n/a".ljust(width + 2))
    filled = int(round(max(0.0, min(100.0, percent)) / 100.0 * width))
    glyphs = ("\u2588", "\u2591")
    try:
        "".join(glyphs).encode(sys.stdout.encoding or "utf-8")
    except (UnicodeEncodeError, LookupError):
        glyphs = ("#", "-")
    return "[%s%s]" % (glyphs[0] * filled, glyphs[1] * (width - filled))


def die(message: str, code: int = 1) -> None:
    print("%s %s" % (red("error:"), message), file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def render_status(data: dict) -> str:
    out = []
    mode = data.get("mode") or "unknown"
    gmode = data.get("gmode")
    fan_control = data.get("fan_control")

    label = bold(mode.upper())
    if gmode:
        label = bold(red("G-MODE"))
    out.append("%s  %s   %s %s" % (
        bold("Thermal mode:"), label,
        dim("backend"), dim(data.get("backend", "?")),
    ))

    power = "AC" if data.get("on_ac") else "Battery"
    cpu = data.get("cpu", {})
    bits = [power]
    if cpu.get("governor"):
        bits.append("governor %s" % cpu["governor"])
    if cpu.get("mhz"):
        bits.append("%.0f MHz avg" % cpu["mhz"])
    out.append("%s  %s" % (bold("Power:       "), dim(" | ".join(bits))))

    out.append("")
    out.append(bold("Fans") + dim("  (control: %s)" % fan_control))
    for fan in data.get("fans", []):
        percent = fan.get("percent")
        boost = fan.get("boost_percent")
        suffix = ""
        if boost:
            suffix = dim("  boost %d%%" % boost)
        out.append("  %-5s %5d rpm  %s %s%s" % (
            fan.get("label", "?"), fan.get("rpm", 0), bar(percent),
            ("%3d%%" % percent) if percent is not None else " n/a", suffix,
        ))
        if fan.get("max_rpm"):
            out[-1] += dim("  of %d" % fan["max_rpm"])

    out.append("")
    out.append(bold("Temperatures"))
    temps = data.get("temps", {})
    # Put the sensors that matter first, then the rest alphabetically.
    priority = ["CPU", "CPU Package", "GPU"]
    ordered = [k for k in priority if k in temps]
    ordered += [k for k in sorted(temps) if k not in ordered]
    for i in range(0, len(ordered), 3):
        row = []
        for key in ordered[i:i + 3]:
            row.append("%-12s %s" % (key, temp_colour(temps[key])))
        out.append("  " + "   ".join(row))

    hottest = data.get("hottest")
    if hottest:
        out.append("  " + dim("hottest: %s at %.0f C" % (hottest[0], hottest[1])))

    battery = data.get("battery", {})
    if battery.get("percent") is not None:
        health = battery.get("health_percent")
        health_text = ""
        if health is not None:
            colour = green if health >= 80 else yellow if health >= 60 else red
            health_text = "  health %s" % colour("%d%%" % health)
        out.append("")
        out.append("%s %s%%  %s%s%s" % (
            bold("Battery:"), battery["percent"],
            dim(battery.get("status") or ""), health_text,
            dim("  %s cycles" % battery["cycles"]) if battery.get("cycles") else "",
        ))

    out.append("")
    out.append(dim("modes: %s" % ", ".join(data.get("available_modes", []))))
    return "\n".join(out)


def cmd_status(args) -> int:
    controller = _open(args)
    data = controller.status()
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True, default=str))
        return 0
    print(render_status(data))
    return 0


def cmd_monitor(args) -> int:
    from . import tui
    return tui.run(_open(args), interval=args.interval)


# ---------------------------------------------------------------------------
# control
# ---------------------------------------------------------------------------


def cmd_mode(args) -> int:
    controller = _open(args)
    if not args.mode:
        current = controller.get_mode() or "unknown"
        print("current: %s" % bold(current))
        for mode in controller.modes:
            marker = green(" *") if mode == current else "  "
            print("%s %s" % (marker, mode))
        return 0
    try:
        applied = controller.set_mode(args.mode)
    except (ctl.ControlError, backends.BackendError) as e:
        die(str(e))
    print("thermal mode: %s" % bold(applied))
    return 0


def cmd_fan(args) -> int:
    controller = _open(args)
    if args.speed is None:
        data = controller.status()
        print("fan control: %s" % bold(data["fan_control"]))
        for fan in data["fans"]:
            print("  %-5s %5d rpm  %s%%  boost %s" % (
                fan["label"], fan["rpm"],
                fan["percent"] if fan["percent"] is not None else "n/a",
                fan["boost"] if fan["boost"] is not None else "n/a",
            ))
        return 0
    try:
        result = controller.set_fan(args.speed)
    except (ctl.ControlError, backends.BackendError) as e:
        die(str(e))
    print("fan: %s" % bold(result))
    if result != ctl.FAN_AUTO:
        print(dim("note: this is a boost applied on top of the firmware curve, "
                  "not an absolute speed."))
        print(dim("      the watchdog will override it above %d C." % C.WATCHDOG_BOOST_TEMP_C))
    return 0


def cmd_gmode(args) -> int:
    controller = _open(args)
    if args.action in (None, "status"):
        value = controller.get_gmode()
        print("G-Mode: %s" % (bold(red("ON")) if value else bold("off")
                              if value is not None else dim("unsupported")))
        return 0
    try:
        if args.action == "toggle":
            new = controller.toggle_gmode()
        else:
            new = args.action == "on"
            controller.set_gmode(new)
    except (ctl.ControlError, backends.BackendError) as e:
        die(str(e))
    print("G-Mode: %s" % (bold(red("ON")) if new else bold("off")))
    return 0


def cmd_reset(args) -> int:
    controller = _open(args)
    try:
        controller.reset()
    except (ctl.ControlError, backends.BackendError) as e:
        die(str(e))
    print("thermal state handed back to firmware")
    return 0


def cmd_restore(args) -> int:
    controller = _open(args)
    controller.restore()
    return 0


# ---------------------------------------------------------------------------
# curve
# ---------------------------------------------------------------------------


def cmd_curve(args) -> int:
    config = state.load_config()
    curve = config.setdefault("curve", {})

    if args.action == "show":
        print("enabled:    %s" % curve.get("enabled"))
        print("sensor:     %s" % curve.get("sensor"))
        print("hysteresis: %s C" % curve.get("hysteresis_c"))
        print("interval:   %s s" % curve.get("interval_s"))
        print("points:")
        for temp, percent in curve.get("points", []):
            print("  %3d C -> %3d%%  %s" % (temp, percent, bar(percent)))
        return 0

    if os.geteuid() != 0:
        die("editing the fan curve requires root (sudo %s curve ...)" % C.APP_NAME)

    if args.action in ("enable", "disable"):
        curve["enabled"] = args.action == "enable"
    elif args.action == "set":
        points = []
        for token in args.points:
            if ":" not in token:
                die("expected TEMP:PERCENT pairs, e.g. 60:20 75:50, got %r" % token)
            temp, _, percent = token.partition(":")
            try:
                points.append([float(temp), float(percent)])
            except ValueError:
                die("non-numeric curve point %r" % token)
        from .curve import CurveError, validate
        try:
            validate(points)
        except CurveError as e:
            die(str(e))
        curve["points"] = points
        curve["enabled"] = True

    state.save_config(config)
    print("curve updated. restart the service to apply:")
    print("  sudo systemctl restart %s" % C.APP_NAME)
    return 0


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def _check(label: str, ok: bool | None, detail: str = "") -> None:
    if ok is True:
        mark = green("  ok  ")
    elif ok is False:
        mark = red(" fail ")
    else:
        mark = yellow(" warn ")
    print("[%s] %-34s %s" % (mark, label, dim(detail)))


def cmd_doctor(args) -> int:
    print(bold("g15ctl %s diagnostics" % C.APP_VERSION))
    print()

    product = sensors._read("/sys/class/dmi/id/product_name") or "unknown"
    vendor = sensors._read("/sys/class/dmi/id/sys_vendor") or "unknown"
    bios = sensors._read("/sys/class/dmi/id/bios_version") or "unknown"
    kernel = os.uname().release
    _check("Dell G-series laptop", "Dell" in vendor, "%s / %s" % (vendor, product))
    _check("BIOS version", True, bios)
    _check("Kernel", True, kernel)
    # Not being root is perfectly valid for monitoring, so this is a warning
    # rather than a failure.
    _check("Running as root", True if os.geteuid() == 0 else None,
           "yes" if os.geteuid() == 0
           else "no: monitoring works, but changing anything needs sudo")

    print()
    print(bold("Firmware interfaces"))
    guid_path = "/sys/bus/wmi/devices/%s" % C.AWCC_WMI_GUID
    _check("AWCC WMI device present", os.path.exists(guid_path), C.AWCC_WMI_GUID)

    ddv = sensors.hwmon_by_name("dell_ddv")
    _check("dell_wmi_ddv sensors", ddv is not None,
           ddv or "modprobe dell_wmi_ddv for fan RPM and temperatures")

    acpi_loaded = os.path.exists(C.ACPI_CALL_PROC)
    _check("acpi_call module", acpi_loaded,
           C.ACPI_CALL_PROC if acpi_loaded
           else "apt install acpi-call-dkms  (needed for manual fan control)")

    native = any(
        sensors._read(os.path.join(d, "name")) == "alienware-wmi"
        for d in glob.glob(os.path.join(C.PLATFORM_PROFILE_CLASS, "*"))
    )
    _check("alienware-wmi platform profile", native or None,
           "load with force_platform_profile=1 force_gmode=1" if not native
           else "native AWCC profiles available")

    print()
    print(bold("Backends"))
    available = backends.detect_all()
    if not available:
        _check("any usable backend", False, "no thermal interface detected")
    for backend in available:
        caps = ", ".join(sorted(backend.caps))
        _check(backend.name, True, caps)
    if os.geteuid() != 0:
        print(dim("  (run as root to test the awcc-acpi backend)"))

    print()
    print(bold("Conflicts"))
    for unit in ("power-profiles-daemon", "tuned"):
        active = _unit_active(unit)
        _check("%s inactive" % unit, not active,
               "may fight over platform_profile" if active else "not running")
    thermald = _unit_active("thermald")
    _check("thermald", None if thermald else True,
           "active: manages Intel RAPL power limits, not fans. Coexists fine."
           if thermald else "not running")

    print()
    print(bold("Selected backend"))
    try:
        controller = ctl.Controller.open(args.backend)
    except (backends.BackendError, AcpiError) as e:
        _check("controller", False, str(e).splitlines()[0])
        return 1
    _check(controller.backend.name, True,
           "modes: %s" % ", ".join(controller.modes))
    for cap, description in (
        (backends.CAP_PROFILES, "thermal profile switching"),
        (backends.CAP_GMODE, "G-Mode toggle"),
        (backends.CAP_FAN_BOOST, "manual fan speed"),
        (backends.CAP_FAN_LIMITS, "fan min/max RPM reporting"),
    ):
        _check(description, controller.backend.supports(cap))
    return 0


def _unit_active(unit: str) -> bool:
    if not shutil.which("systemctl"):
        return False
    result = subprocess.run(
        ["systemctl", "is-active", "--quiet", unit],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


# ---------------------------------------------------------------------------
# service plumbing
# ---------------------------------------------------------------------------


def cmd_daemon(args) -> int:
    from . import daemon
    return daemon.run(args.backend)


def cmd_logs(args) -> int:
    return logs.read_logs(lines=args.lines, follow=args.follow)


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------


def _open(args) -> ctl.Controller:
    try:
        return ctl.Controller.open(args.backend)
    except (backends.BackendError, AcpiError) as e:
        die("%s\n\nRun `%s doctor` to diagnose." % (e, C.APP_NAME))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=C.APP_NAME,
        description="Alienware Command Center style fan and thermal control "
                    "for Dell G-series laptops on Linux.",
        epilog="Monitoring works as a normal user; changing anything needs sudo.",
    )
    parser.add_argument("--version", action="version",
                        version="%s %s" % (C.APP_NAME, C.APP_VERSION))
    parser.add_argument("--backend", choices=sorted(backends.BACKENDS_BY_NAME),
                        help="force a specific control backend")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("-q", "--quiet", action="store_true", help="warnings only")

    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("status", help="show temperatures, fans and mode")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("monitor", help="live dashboard")
    p.add_argument("-i", "--interval", type=float, default=1.5,
                   help="refresh seconds (default: 1.5)")
    p.set_defaults(func=cmd_monitor)

    p = sub.add_parser("mode", help="get or set the thermal mode")
    p.add_argument("mode", nargs="?",
                   help="quiet, balanced, balanced-performance, low-power, g-mode")
    p.set_defaults(func=cmd_mode)

    p = sub.add_parser("fan", help="set a manual fan boost, or 'auto'")
    p.add_argument("speed", nargs="?", help="0-100 percent, or 'auto'")
    p.set_defaults(func=cmd_fan)

    p = sub.add_parser("gmode", help="G-Mode (Game Shift)")
    p.add_argument("action", nargs="?", choices=["on", "off", "toggle", "status"])
    p.set_defaults(func=cmd_gmode)

    p = sub.add_parser("curve", help="inspect or edit the automatic fan curve")
    p.add_argument("action", nargs="?", default="show",
                   choices=["show", "set", "enable", "disable"])
    p.add_argument("points", nargs="*", metavar="TEMP:PERCENT",
                   help="e.g. 50:0 65:25 75:50 85:100")
    p.set_defaults(func=cmd_curve)

    p = sub.add_parser("reset", help="hand thermal control back to the firmware")
    p.set_defaults(func=cmd_reset)

    p = sub.add_parser("restore", help="reapply the saved mode (used at boot)")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("doctor", help="diagnose hardware and backend support")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("logs", help="show service logs")
    p.add_argument("-n", "--lines", type=int, default=50)
    p.add_argument("-f", "--follow", action="store_true")
    p.set_defaults(func=cmd_logs)

    p = sub.add_parser("daemon", help="run the background service (systemd)")
    p.set_defaults(func=cmd_daemon)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        # Bare `g15ctl` is most useful as a status readout. Set the defaults
        # `status` would have had rather than re-parsing, which would discard
        # the global flags already given.
        args.command = "status"
        args.func = cmd_status
        args.json = False

    # `monitor` draws with curses and owns the terminal, so log records must
    # never go to stderr there; they still go to the log file when we are root.
    interactive_fullscreen = args.command == "monitor"
    logs.setup(
        verbose=args.verbose,
        quiet=args.quiet,
        to_file=args.command in ("daemon", "restore", "reset", "monitor"),
        console=not interactive_fullscreen,
    )
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        # e.g. `g15ctl status | head`
        try:
            sys.stdout.close()
        finally:
            return 0


if __name__ == "__main__":
    sys.exit(main())
