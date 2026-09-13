#!/usr/bin/env python3
"""g15ctl tray applet.

A GTK3 + AyatanaAppIndicator tray icon showing live CPU/GPU temperature and
fan RPM, with a menu to switch thermal mode, toggle G-Mode and set a fan
boost -- the everyday parts of Alienware Command Center.

Runs as your normal user. Because changing thermal state needs root, the
applet shells out through pkexec (falling back to sudo in a terminal), so you
get a graphical password prompt instead of a silent failure.

Dependencies, all present on a stock Ubuntu desktop:
    python3-gi  gir1.2-gtk-3.0  gir1.2-ayatanaappindicator3-0.1
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

_INDICATOR = None
for _name, _version in (("AyatanaAppIndicator3", "0.1"), ("AppIndicator3", "0.1")):
    try:
        gi.require_version(_name, _version)
        _INDICATOR = __import__("gi.repository", fromlist=[_name])
        _INDICATOR = getattr(_INDICATOR, _name)
        break
    except (ValueError, ImportError, AttributeError):
        continue

# Allow running straight from a source checkout as well as from /usr/lib.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

try:
    from g15ctl import backends, constants as C, controller as ctl
except ImportError:
    sys.exit("g15ctl package not found. Install g15ctl first.")

REFRESH_SECONDS = 3
MODES = ["low-power", "quiet", "balanced", "balanced-performance", "g-mode"]
FAN_PRESETS = [("Auto (firmware)", "auto"), ("40%", 40), ("60%", 60),
               ("80%", 80), ("100%", 100)]


def elevate(args: list[str]) -> tuple[bool, str]:
    """Run `g15ctl <args>` with privileges. Returns (ok, message)."""
    binary = shutil.which("g15ctl") or shutil.which("g15ctl.py")
    if binary is None:
        # Running from a checkout: re-invoke the module with the same python.
        base = [sys.executable, "-m", "g15ctl"]
        cwd = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    else:
        base = [binary]
        cwd = None

    if os.geteuid() == 0:
        launcher = []
    elif shutil.which("pkexec"):
        launcher = ["pkexec"]
    elif shutil.which("sudo"):
        launcher = ["sudo", "-n"]
    else:
        return False, "no way to gain privileges (install pkexec)"

    env = dict(os.environ)
    if cwd:
        env["PYTHONPATH"] = cwd + os.pathsep + env.get("PYTHONPATH", "")
    try:
        result = subprocess.run(
            launcher + base + args,
            capture_output=True, text=True, timeout=30, cwd=cwd, env=env,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        return False, detail[-1] if detail else "exit status %d" % result.returncode
    return True, (result.stdout or "").strip()


class Tray:
    def __init__(self):
        try:
            self.controller = ctl.Controller.open()
        except Exception as e:
            self._fatal("Cannot talk to the thermal interface:\n%s" % e)
            raise SystemExit(1)

        self.items: dict[str, Gtk.MenuItem] = {}
        self.menu = Gtk.Menu()
        self._build_menu()

        if _INDICATOR is not None:
            self.indicator = _INDICATOR.Indicator.new(
                "g15ctl", "sensors-temperature-symbolic",
                _INDICATOR.IndicatorCategory.HARDWARE,
            )
            self.indicator.set_status(_INDICATOR.IndicatorStatus.ACTIVE)
            self.indicator.set_menu(self.menu)
            self.indicator.set_title("g15ctl")
        else:
            # No AppIndicator typelib: fall back to the legacy status icon so
            # the applet still works on minimal desktops.
            self.indicator = None
            self.status_icon = Gtk.StatusIcon()
            self.status_icon.set_from_icon_name("sensors-temperature-symbolic")
            self.status_icon.connect(
                "popup-menu",
                lambda icon, button, t: self.menu.popup(None, None, None, None, button, t),
            )

        self.refresh()
        GLib.timeout_add_seconds(REFRESH_SECONDS, self.refresh)

    # -- menu ---------------------------------------------------------------

    def _build_menu(self) -> None:
        self.items["summary"] = Gtk.MenuItem(label="reading sensors...")
        self.items["summary"].set_sensitive(False)
        self.menu.append(self.items["summary"])
        self.items["fans"] = Gtk.MenuItem(label="")
        self.items["fans"].set_sensitive(False)
        self.menu.append(self.items["fans"])
        self.menu.append(Gtk.SeparatorMenuItem())

        available = self.controller.modes
        group: list[Gtk.RadioMenuItem] = []
        for mode in MODES:
            if mode not in available:
                continue
            label = "G-Mode (Game Shift)" if mode == "g-mode" else mode.replace("-", " ").title()
            item = Gtk.RadioMenuItem(label=label)
            if group:
                item.join_group(group[0])
            group.append(item)
            item.connect("toggled", self._on_mode, mode)
            self.menu.append(item)
            self.items["mode:" + mode] = item
        self.menu.append(Gtk.SeparatorMenuItem())

        if self.controller.backend.supports(backends.CAP_FAN_BOOST):
            fan_root = Gtk.MenuItem(label="Fan speed")
            submenu = Gtk.Menu()
            for label, value in FAN_PRESETS:
                item = Gtk.MenuItem(label=label)
                item.connect("activate", self._on_fan, value)
                submenu.append(item)
            fan_root.set_submenu(submenu)
            self.menu.append(fan_root)
        else:
            item = Gtk.MenuItem(label="Fan speed (needs acpi_call)")
            item.set_sensitive(False)
            self.menu.append(item)

        monitor = Gtk.MenuItem(label="Open live monitor")
        monitor.connect("activate", self._on_monitor)
        self.menu.append(monitor)

        self.menu.append(Gtk.SeparatorMenuItem())
        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", lambda _: Gtk.main_quit())
        self.menu.append(quit_item)
        self.menu.show_all()

    # -- actions ------------------------------------------------------------

    def _on_mode(self, item: Gtk.RadioMenuItem, mode: str) -> None:
        # RadioMenuItem fires for both the deselected and selected item, and
        # also when we sync the UI in refresh(); only act on a real click.
        if not item.get_active() or getattr(self, "_syncing", False):
            return
        ok, message = elevate(["mode", mode])
        if not ok:
            self._error("Could not set mode %s" % mode, message)
        self.refresh()

    def _on_fan(self, _item, value) -> None:
        ok, message = elevate(["fan", str(value)])
        if not ok:
            self._error("Could not set fan speed", message)
        self.refresh()

    def _on_monitor(self, _item) -> None:
        for terminal, flag in (("gnome-terminal", "--"), ("konsole", "-e"),
                               ("xfce4-terminal", "-x"), ("xterm", "-e")):
            if shutil.which(terminal):
                binary = shutil.which("g15ctl") or "g15ctl"
                subprocess.Popen([terminal, flag, binary, "monitor"])
                return
        self._error("No terminal emulator found",
                    "Run `g15ctl monitor` yourself.")

    # -- refresh ------------------------------------------------------------

    def refresh(self) -> bool:
        try:
            data = self.controller.status()
        except Exception as e:
            self.items["summary"].set_label("sensor error: %s" % e)
            return True

        temps = data.get("temps", {})
        cpu = temps.get("CPU Package") or temps.get("CPU")
        gpu = temps.get("GPU")
        mode = data.get("mode") or "?"
        if data.get("gmode"):
            mode = "G-Mode"

        parts = []
        if cpu is not None:
            parts.append("%.0f\u00b0C" % cpu)
        if gpu is not None:
            parts.append("GPU %.0f\u00b0C" % gpu)
        title = "  ".join(parts) or "g15ctl"

        if self.indicator is not None:
            self.indicator.set_label(title, "100\u00b0C GPU 100\u00b0C")
        else:
            self.status_icon.set_tooltip_text("%s | %s" % (title, mode))

        self.items["summary"].set_label("Mode: %s  (%s)" % (mode, data.get("backend")))
        self.items["fans"].set_label("  ".join(
            "%s %d rpm%s" % (f["label"], f["rpm"],
                             " (%d%%)" % f["percent"] if f["percent"] is not None else "")
            for f in data.get("fans", [])
        ) or "no fan data")

        # Reflect the real hardware state without re-triggering _on_mode.
        self._syncing = True
        try:
            key = "mode:" + (data.get("mode") or "")
            item = self.items.get(key)
            if item is not None and not item.get_active():
                item.set_active(True)
        finally:
            self._syncing = False
        return True

    # -- dialogs ------------------------------------------------------------

    def _error(self, primary: str, secondary: str = "") -> None:
        dialog = Gtk.MessageDialog(
            transient_for=None, flags=0, message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.OK, text=primary,
        )
        if secondary:
            dialog.format_secondary_text(secondary)
        dialog.run()
        dialog.destroy()

    def _fatal(self, message: str) -> None:
        self._error("g15ctl", message)


def main() -> int:
    Tray()
    try:
        Gtk.main()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
