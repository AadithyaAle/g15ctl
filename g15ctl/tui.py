"""Live terminal dashboard.

Uses curses from the standard library, so there is no dependency to install.
Read-only by default; the interactive mode keys only work when running as root,
and the footer says so rather than failing silently when you press one.
"""

from __future__ import annotations

import curses
import time

from . import backends, constants as C, controller as ctl

_REFRESH_MIN = 0.25

#: Single-key mode switches, shown in the footer.
_KEYS = [
    ("1", "low-power"),
    ("2", "quiet"),
    ("3", "balanced"),
    ("4", "balanced-performance"),
    ("5", C.MODE_GMODE),
]


class Dashboard:
    def __init__(self, controller: ctl.Controller, interval: float):
        self.controller = controller
        self.interval = max(_REFRESH_MIN, interval)
        self.message = ""
        self.message_until = 0.0
        self.history: list[float] = []

    # -- drawing ------------------------------------------------------------

    def _colour(self, temp: float) -> int:
        if temp >= 90:
            return curses.color_pair(3) | curses.A_BOLD
        if temp >= 80:
            return curses.color_pair(2)
        if temp >= 70:
            return curses.color_pair(4)
        return curses.color_pair(1)

    def _bar(self, percent: float | None, width: int) -> str:
        if percent is None:
            return " " * width
        filled = int(round(max(0.0, min(100.0, percent)) / 100.0 * width))
        return "\u2588" * filled + "\u2591" * (width - filled)

    def draw(self, stdscr, data: dict) -> None:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        row = 0

        def put(text: str, attr: int = 0, indent: int = 0) -> None:
            nonlocal row
            if row >= height - 1:
                return
            try:
                stdscr.addnstr(row, indent, text, max(0, width - indent - 1), attr)
            except curses.error:
                pass
            row += 1

        mode = (data.get("mode") or "unknown").upper()
        if data.get("gmode"):
            mode = "G-MODE"
        header = " %s %s   mode: %s   backend: %s " % (
            C.APP_NAME, C.APP_VERSION, mode, data.get("backend"))
        put(header.ljust(width - 1), curses.color_pair(5) | curses.A_BOLD)
        put("")

        # Fans
        put("FANS  (control: %s)" % data.get("fan_control"), curses.A_BOLD)
        meter = max(10, min(30, width - 40))
        for fan in data.get("fans", []):
            percent = fan.get("percent")
            line = "  %-4s %5d rpm  %s %s" % (
                fan.get("label", "?"), fan.get("rpm", 0),
                self._bar(percent, meter),
                "%3d%%" % percent if percent is not None else " n/a",
            )
            if fan.get("boost_percent"):
                line += "  boost %d%%" % fan["boost_percent"]
            put(line)
        put("")

        # Temperatures
        put("TEMPERATURES", curses.A_BOLD)
        temps = data.get("temps", {})
        priority = ["CPU", "CPU Package", "GPU"]
        ordered = [k for k in priority if k in temps]
        ordered += [k for k in sorted(temps) if k not in ordered]
        for key in ordered:
            value = temps[key]
            put("  %-13s %5.1f C  %s" % (key, value, self._bar(min(value, 100), meter)),
                self._colour(value))
        put("")

        # Sparkline of the hottest sensor, so a trend is visible at a glance.
        if self.history:
            put("HOTTEST SENSOR TREND", curses.A_BOLD)
            put("  " + self._sparkline(width - 6))
            put("")

        battery = data.get("battery", {})
        if battery.get("percent") is not None:
            health = battery.get("health_percent")
            put("BATTERY", curses.A_BOLD)
            put("  %d%% %s   health %s   %s cycles   %s" % (
                battery["percent"], battery.get("status") or "",
                "%d%%" % health if health is not None else "n/a",
                battery.get("cycles") if battery.get("cycles") is not None else "?",
                "%.1f C" % battery["temp_c"] if battery.get("temp_c") else "",
            ))
            put("")

        cpu = data.get("cpu", {})
        put("  %s | governor %s | %s" % (
            "AC" if data.get("on_ac") else "Battery",
            cpu.get("governor") or "?",
            "%.0f MHz avg" % cpu["mhz"] if cpu.get("mhz") else "",
        ), curses.A_DIM)

        # Footer
        footer_row = height - 1
        if self.message and time.monotonic() < self.message_until:
            footer = " " + self.message
            attr = curses.color_pair(2) | curses.A_BOLD
        else:
            available = data.get("available_modes", [])
            keys = " ".join(
                "%s:%s" % (k, m.replace("balanced-performance", "perf"))
                for k, m in _KEYS if m in available
            )
            footer = " q:quit  a:auto-fan  f:+10%%  F:-10%%  %s " % keys
            attr = curses.A_REVERSE
        try:
            stdscr.addnstr(footer_row, 0, footer.ljust(width - 1), width - 1, attr)
        except curses.error:
            pass
        stdscr.refresh()

    def _sparkline(self, width: int) -> str:
        glyphs = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"
        data = self.history[-width:]
        if not data:
            return ""
        low, high = min(data), max(data)
        span = max(1.0, high - low)
        out = [glyphs[min(len(glyphs) - 1, int((v - low) / span * (len(glyphs) - 1)))]
               for v in data]
        return "%s  %.0f-%.0f C" % ("".join(out), low, high)

    # -- interaction --------------------------------------------------------

    def notify(self, text: str, seconds: float = 3.0) -> None:
        self.message = text
        self.message_until = time.monotonic() + seconds

    def handle_key(self, key: int, data: dict) -> bool:
        """Return False to quit."""
        if key in (ord("q"), ord("Q"), 27):
            return False

        try:
            if key in (ord("a"), ord("A")):
                self.controller.set_fan("auto")
                self.notify("fan control released to firmware")
            elif key == ord("f"):
                self._nudge_fan(+10, data)
            elif key == ord("F"):
                self._nudge_fan(-10, data)
            else:
                for k, mode in _KEYS:
                    if key == ord(k):
                        if mode not in data.get("available_modes", []):
                            self.notify("%s not supported by this firmware" % mode)
                            break
                        self.controller.set_mode(mode)
                        self.notify("mode -> %s" % mode)
                        break
        except (ctl.ControlError, backends.BackendError) as e:
            self.notify(str(e).splitlines()[0])
        return True

    def _nudge_fan(self, delta: int, data: dict) -> None:
        fans = data.get("fans") or []
        current = 0
        for fan in fans:
            if fan.get("boost_percent"):
                current = max(current, fan["boost_percent"])
        target = max(0, min(100, current + delta))
        self.controller.set_fan(target)
        self.notify("fan boost -> %d%%" % target)

    # -- loop ---------------------------------------------------------------

    def loop(self, stdscr) -> int:
        curses.curs_set(0)
        stdscr.nodelay(True)
        curses.use_default_colors()
        for index, colour in enumerate(
            (curses.COLOR_GREEN, curses.COLOR_YELLOW, curses.COLOR_RED,
             curses.COLOR_CYAN, curses.COLOR_BLUE), start=1
        ):
            curses.init_pair(index, colour, -1)

        data = self.controller.status()
        last = 0.0
        while True:
            now = time.monotonic()
            if now - last >= self.interval:
                data = self.controller.status()
                hottest = data.get("hottest")
                if hottest:
                    self.history.append(hottest[1])
                    del self.history[:-240]
                last = now
                self.draw(stdscr, data)

            key = stdscr.getch()
            if key != -1:
                if not self.handle_key(key, data):
                    return 0
                data = self.controller.status()
                self.draw(stdscr, data)
            time.sleep(0.05)


def run(controller: ctl.Controller, interval: float = 1.5) -> int:
    dashboard = Dashboard(controller, interval)
    try:
        return curses.wrapper(dashboard.loop)
    except KeyboardInterrupt:
        return 130
