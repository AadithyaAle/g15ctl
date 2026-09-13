#!/usr/bin/env python3
"""Watch every input device at once and report any key event.

Purpose: determine whether the Dell G15's G-Mode key (F9) reaches Linux as an
input event at all, or whether the EC consumes it internally.

Watching a single device is not conclusive -- the key could surface on the
main AT keyboard, on "Dell WMI hotkeys", or on "Intel HID events". This
watches all of them simultaneously.

It also asks you to press a control key first. If the control key registers
and F9 does not, that is real evidence the EC swallows F9, rather than the
probe being broken.

Read-only. Requires root to open /dev/input/event*.

Usage: sudo python3 tools/keyprobe.py
"""

from __future__ import annotations

import glob
import os
import select
import struct
import sys
import time

# struct input_event on x86_64:
#   struct timeval { __s64 tv_sec; __s64 tv_usec; }  = 16 bytes
#   __u16 type; __u16 code; __s32 value              =  8 bytes
EVENT_FORMAT = "llHHi"
EVENT_SIZE = struct.calcsize(EVENT_FORMAT)

EV_SYN, EV_KEY, EV_MSC, EV_SW = 0x00, 0x01, 0x04, 0x05
TYPE_NAMES = {EV_KEY: "EV_KEY", EV_MSC: "EV_MSC", EV_SW: "EV_SW"}

# Common codes we might plausibly see from a vendor hotkey.
KEY_NAMES = {
    67: "KEY_F9", 68: "KEY_F10", 87: "KEY_F11", 88: "KEY_F12",
    148: "KEY_PROG1", 149: "KEY_PROG2", 202: "KEY_PROG3", 203: "KEY_PROG4",
    212: "KEY_CAMERA", 224: "KEY_BRIGHTNESSDOWN", 225: "KEY_BRIGHTNESSUP",
    372: "KEY_FN_F9", 0x1d0: "KEY_FN", 431: "KEY_GAMES",
    120: "KEY_SCALE", 425: "KEY_TOUCHPAD_TOGGLE",
}
VALUE_NAMES = {0: "release", 1: "press", 2: "repeat"}


def device_name(path: str) -> str:
    node = os.path.basename(path)
    try:
        with open("/sys/class/input/%s/device/name" % node) as f:
            return f.read().strip()
    except OSError:
        return node


#: Devices that only generate noise for this test.
SKIP_DEVICE_WORDS = ("mouse", "touchpad", "trackpoint", "hdmi", "headphone",
                     "pcm=", "avrcp", "lid switch")

#: Codes to ignore. Window switching (Alt+Tab), modifiers and pointer buttons
#: would otherwise be mistaken for the hotkey -- which is exactly what made an
#: earlier run of this script report a false positive.
NOISE_CODES = {
    15,   # KEY_TAB
    28, 96,   # KEY_ENTER, KEY_KPENTER
    29, 97,   # ctrl
    42, 54,   # shift
    56, 100,  # alt
    125, 126,  # meta / super
    103, 105, 106, 108,  # arrows
    1,    # KEY_ESC
}


def is_noise(code: int) -> bool:
    # Anything >= 0x100 is a BTN_* (pointer/gamepad), never a keyboard hotkey.
    return code in NOISE_CODES or code >= 0x100


def open_all() -> dict:
    devices = {}
    for path in sorted(glob.glob("/dev/input/event*"),
                       key=lambda p: int(p.rsplit("event", 1)[1])):
        name = device_name(path)
        if any(word in name.lower() for word in SKIP_DEVICE_WORDS):
            continue
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            continue  # busy or permission denied; skip quietly
        devices[fd] = (path, name)
    return devices


def watch(devices: dict, seconds: float, label: str) -> list:
    print("\n>>> %s -- listening for %.0f seconds..." % (label, seconds))
    sys.stdout.flush()
    seen = []
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        ready, _, _ = select.select(list(devices), [], [], min(0.5, remaining))
        for fd in ready:
            try:
                data = os.read(fd, EVENT_SIZE * 64)
            except OSError:
                continue
            for offset in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
                _s, _us, etype, code, value = struct.unpack_from(
                    EVENT_FORMAT, data, offset
                )
                if etype in (EV_SYN, EV_MSC):
                    continue
                if etype == EV_KEY and is_noise(code):
                    continue
                path, name = devices[fd]
                print("    %-22s %-7s code=%-4d %-22s %s" % (
                    name[:22], TYPE_NAMES.get(etype, "0x%02x" % etype), code,
                    KEY_NAMES.get(code, ""), VALUE_NAMES.get(value, value),
                ))
                sys.stdout.flush()
                seen.append((name, etype, code, value))
    return seen


def main() -> int:
    if os.geteuid() != 0:
        sys.exit("must run as root: sudo python3 tools/keyprobe.py")

    devices = open_all()
    print("Watching %d input devices:" % len(devices))
    for path, name in devices.values():
        print("  %-18s %s" % (os.path.basename(path), name))

    print("\n" + "=" * 68)
    print("IMPORTANT: do not Alt+Tab, click, or use the touchpad during the")
    print("test. Only press the key you are asked for. Pointer devices and")
    print("modifier keys are filtered out, but stay still anyway.")
    print("=" * 68)

    print("\nSTEP 1 (control): press and release  F10  about five times.")
    print("This proves the probe can see your keyboard. Do NOT press F9 yet.")
    control = watch(devices, 8, "waiting for F10")
    control_keys = {c for _n, t, c, _v in control if t == EV_KEY}

    print("\nSTEP 2 (the real test): press and release the  G / F9  key five times.")
    print("Try it plain, and if nothing appears, try Fn+F9 as well.")
    target = watch(devices, 12, "waiting for G / F9")
    target_keys = {c for _n, t, c, _v in target if t == EV_KEY}

    print("\n" + "=" * 68)
    print("RESULT")
    print("=" * 68)
    if 68 not in control_keys:
        print("  INCONCLUSIVE: KEY_F10 (code 68) was never seen, so the probe")
        print("  could not observe your keyboard and step 2 proves nothing.")
        print("  Saw these codes instead: %s" % (sorted(control_keys) or "none"))
        print("  Re-run and press F10 with this terminal focused.")
        return 2

    print("  control: KEY_F10 seen -> probe is working")
    # Only codes that appeared in step 2 and NOT in step 1 can be the hotkey.
    candidates = sorted(target_keys - control_keys)
    print("  step 2 new key codes: %s" % (candidates or "none"))
    print()

    if candidates:
        print("  CONCLUSION: the G key IS visible to Linux.")
        for code in candidates:
            names = sorted({n for n, t, c, _v in target
                            if t == EV_KEY and c == code})
            print("    code=%-4d %-16s on %s"
                  % (code, KEY_NAMES.get(code, "(unnamed)"), ", ".join(names)))
        print("  -> g15ctl can intercept it directly.")
        return 0

    print("  CONCLUSION: the G / F9 key emits NO input event.")
    print()
    print("  The embedded controller consumes the key internally, so NO")
    print("  userspace program can intercept it -- not g15ctl, not GNOME.")
    print()
    print("  Next question: does the EC toggle G-Mode by itself? Run:")
    print("      sudo g15ctl gmode status     # note the value")
    print("      <press the G / F9 key>")
    print("      sudo g15ctl gmode status     # did it flip?")
    print()
    print("  If it flips, the hardware key already works and g15ctl only needs")
    print("  to watch for the change. If not, bind a desktop shortcut to")
    print("  'pkexec g15ctl gmode toggle' instead.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
