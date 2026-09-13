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

    def _meter(self, stdscr, row: int, col: int, width: int,
               percent: float | None, attr: int = 0) -> int:
        """Draw a bracketed meter using reverse video rather than glyphs.

        Shade characters like U+2591 are unreliable: many terminal fonts render
        them as a full cell, making the filled and empty halves of a bar
        indistinguishable (the bar becomes one solid block). Reverse-video
        spaces always render correctly regardless of font.

        Returns the column just past the meter.
        """
        try:
            stdscr.addch(row, col, "[")
            stdscr.addch(row, col + width + 1, "]")
        except curses.error:
            pass
        if percent is None:
            return col + width + 2
        filled = int(round(max(0.0, min(100.0, percent)) / 100.0 * width))
        for i in range(width):
            try:
                stdscr.addch(row, col + 1 + i, " ",
                             (attr | curses.A_REVERSE) if i < filled else attr)
            except curses.error:
                pass
        return col + width + 2

    #: ASCII ramp for the trend line. Block glyphs (U+2581..U+2588) hit the
    #: same font problem as the shade characters above.
    _RAMP = " .:-=+*#"

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
        for fan in data.get("fans", []):
            percent = fan.get("percent")
            prefix = "  %-4s %5d rpm  " % (fan.get("label", "?"), fan.get("rpm", 0))
            meter = max(8, min(28, width - len(prefix) - 16))
            if row >= height - 1:
                break
            try:
                stdscr.addnstr(row, 0, prefix, max(0, width - 1))
            except curses.error:
                pass
            end = self._meter(stdscr, row, len(prefix), meter, percent)
            suffix = " %3d%%" % percent if percent is not None else "  n/a"
            if fan.get("boost_percent"):
                suffix += " boost %d%%" % fan["boost_percent"]
            try:
                stdscr.addnstr(row, end, suffix, max(0, width - end - 1))
            except curses.error:
                pass
            row += 1
        put("")

        # Temperatures
        put("TEMPERATURES", curses.A_BOLD)
        temps = data.get("temps", {})
        priority = ["CPU", "CPU Package", "GPU"]
        ordered = [k for k in priority if k in temps]
        ordered += [k for k in sorted(temps) if k not in ordered]
        for key in ordered:
            if row >= height - 1:
                break
            value = temps[key]
            colour = self._colour(value)
            prefix = "  %-13s %5.1f C  " % (key, value)
            meter = max(8, min(28, width - len(prefix) - 4))
            try:
                stdscr.addnstr(row, 0, prefix, max(0, width - 1), colour)
            except curses.error:
                pass
            # Scale against 100 C so the bars are comparable to each other.
            self._meter(stdscr, row, len(prefix), meter, min(value, 100.0), colour)
            row += 1
        put("")

        # Trend of the hottest sensor, so a direction is visible at a glance.
        if len(self.history) > 1:
            put("HOTTEST SENSOR TREND", curses.A_BOLD)
            put("  " + self._sparkline(max(10, width - 22)))
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
        data = self.history[-width:]
        if not data:
            return ""
        low, high = min(data), max(data)
        span = max(1.0, high - low)
        ramp = self._RAMP
        out = [ramp[min(len(ramp) - 1, int((v - low) / span * (len(ramp) - 1)))]
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
        if key == curses.KEY_RESIZE:
            # Drop the old geometry and repaint from scratch.
            curses.update_lines_cols()
            return True

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
        try:
            curses.curs_set(0)
        except curses.error:
            pass  # some terminals cannot hide the cursor
        stdscr.nodelay(True)
        # Never let a write past the last column scroll the screen; that is what
        # shifts the footer and smears the layout.
        stdscr.scrollok(False)
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
