#!/usr/bin/env python3
"""Determine what G-Mode actually requires on this firmware.

Reported symptom: `g15ctl mode g-mode` reports success and the G-Mode flag
reads back ON, but the fans do not audibly change.

Hypothesis: G-Mode needs BOTH of
    0x15/0x01 with profile 0xAB   (activate the G-Mode thermal profile)
    0x25/0x01 with 1              (set the Game Shift flag)
and g15ctl currently only issues the second. Note 0xAB is NOT in the
firmware's enumerated profile table (0x14/0x03 returns only A0/A1/A3/A5), so
it has to be tried explicitly rather than discovered.

This script measures fan RPM under each combination, with proper settling
time, so the answer is based on measurement rather than on a flag readback.

Fan RPM is read from dell_ddv, an interface independent of the WMAX calls we
make, so the numbers are trustworthy.

Restores the starting state on exit, including on Ctrl-C.

Usage: sudo python3 tools/gmode-test.py 2>&1 | tee gmode-test-report.txt
"""

from __future__ import annotations

import glob
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from g15ctl import constants as C  # noqa: E402
from g15ctl.acpi import Wmax  # noqa: E402

SETTLE = 24.0        # seconds to let the fans stabilise
SAMPLE_WINDOW = 8.0  # average over the final part of the settle period

PROFILE_BALANCED = 0xA0
PROFILE_GMODE = 0xAB


def ddv_path() -> str | None:
    for h in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        try:
            if open(os.path.join(h, "name")).read().strip() == "dell_ddv":
                return h
        except OSError:
            continue
    return None


DDV = ddv_path()


def read_rpm() -> tuple[int, int]:
    if DDV is None:
        return (0, 0)
    out = []
    for n in (1, 2):
        try:
            out.append(int(open(os.path.join(DDV, "fan%d_input" % n)).read()))
        except OSError:
            out.append(0)
    return tuple(out)  # type: ignore[return-value]


def read_temp() -> float:
    if DDV is None:
        return 0.0
    try:
        return int(open(os.path.join(DDV, "temp1_input")).read()) / 1000.0
    except OSError:
        return 0.0


class Rig:
    def __init__(self):
        self.w = Wmax.detect()
        print("WMAX path: %s" % self.w.path)
        self.orig_profile = self.w.query(C.M_THERMAL_INFO, C.OP_GET_CURRENT_PROFILE)
        self.orig_gs = self.w.query(C.M_GAME_SHIFT, C.OP_GET_GAME_SHIFT)
        print("baseline: profile=%#x game_shift=%s"
              % (self.orig_profile or 0, self.orig_gs))

    # -- primitives ---------------------------------------------------------

    def set_profile(self, pid: int) -> str:
        r = self.w.call(C.M_THERMAL_CONTROL, C.OP_ACTIVATE_PROFILE, pid)
        return "rejected(%#x)" % r if r in C.WMAX_ERRORS else "accepted"

    def set_gs(self, on: bool) -> str:
        r = self.w.call(C.M_GAME_SHIFT, C.OP_SET_GAME_SHIFT, 1 if on else 0)
        return "rejected(%#x)" % r if r in C.WMAX_ERRORS else "accepted"

    def flags(self) -> str:
        p = self.w.query(C.M_THERMAL_INFO, C.OP_GET_CURRENT_PROFILE)
        g = self.w.query(C.M_GAME_SHIFT, C.OP_GET_GAME_SHIFT)
        return "profile=%#x game_shift=%s" % (p or 0, g)

    def boosts(self) -> str:
        out = []
        for fan in (0x32, 0x33):
            out.append(str(self.w.query(C.M_THERMAL_INFO, C.OP_GET_FAN_BOOST, fan)))
        return "/".join(out)

    # -- measurement --------------------------------------------------------

    def measure(self, label: str) -> tuple[float, float]:
        """Settle, then average fan RPM over the final window."""
        print("\n--- %s" % label)
        print("    %s  boost=%s" % (self.flags(), self.boosts()))
        sys.stdout.write("    settling %.0fs: " % SETTLE)
        sys.stdout.flush()
        samples: list[tuple[int, int]] = []
        start = time.monotonic()
        while True:
            elapsed = time.monotonic() - start
            if elapsed >= SETTLE:
                break
            if elapsed >= SETTLE - SAMPLE_WINDOW:
                samples.append(read_rpm())
            time.sleep(1.0)
            sys.stdout.write(".")
            sys.stdout.flush()
        print()
        cpu = statistics.mean(s[0] for s in samples) if samples else 0.0
        gpu = statistics.mean(s[1] for s in samples) if samples else 0.0
        print("    RESULT  cpu=%.0f rpm  gpu=%.0f rpm  (cpu temp %.0f C)"
              % (cpu, gpu, read_temp()))
        return cpu, gpu

    def restore(self) -> None:
        print("\n=== restoring baseline ===")
        self.set_gs(bool(self.orig_gs))
        if self.orig_profile and self.orig_profile not in C.WMAX_ERRORS:
            pid = self.orig_profile
            if pid == PROFILE_GMODE:
                pid = PROFILE_BALANCED
            self.set_profile(pid)
        else:
            self.set_profile(PROFILE_BALANCED)
        for fan in (0x32, 0x33):
            self.w.call(C.M_THERMAL_CONTROL, C.OP_SET_FAN_BOOST, fan, 0)
        print("    %s  boost=%s" % (self.flags(), self.boosts()))


def main() -> int:
    if os.geteuid() != 0:
        sys.exit("must run as root: sudo python3 tools/gmode-test.py")
    if DDV is None:
        sys.exit("dell_ddv hwmon not found; cannot measure fan RPM")

    print("=" * 70)
    print("G-MODE EFFECT TEST -- Dell G15")
    print("Keep the machine IDLE and do not run anything heavy during this.")
    print("Total runtime about 2.5 minutes.")
    print("=" * 70)

    rig = Rig()
    results = {}
    try:
        # A: baseline, both off.
        rig.set_profile(PROFILE_BALANCED)
        rig.set_gs(False)
        results["A balanced, gs=0"] = rig.measure(
            "A: profile 0xA0 (balanced), game_shift=0   [BASELINE]")

        # B: only the Game Shift flag, which is all g15ctl does today.
        print("\n    setting game_shift=1 -> %s" % rig.set_gs(True))
        results["B balanced, gs=1"] = rig.measure(
            "B: profile 0xA0, game_shift=1   [what g15ctl does now]")

        # C: only the 0xAB profile.
        print("\n    setting game_shift=0 -> %s" % rig.set_gs(False))
        print("    setting profile 0xAB -> %s" % rig.set_profile(PROFILE_GMODE))
        results["C 0xAB, gs=0"] = rig.measure(
            "C: profile 0xAB, game_shift=0   [profile only]")

        # D: both, which is what the prior-art script did.
        print("\n    setting game_shift=1 -> %s" % rig.set_gs(True))
        print("    re-asserting profile 0xAB -> %s" % rig.set_profile(PROFILE_GMODE))
        results["D 0xAB, gs=1"] = rig.measure(
            "D: profile 0xAB, game_shift=1   [prior-art behaviour]")

        # E: for reference, the highest enumerated profile.
        rig.set_gs(False)
        print("\n    setting profile 0xA1 -> %s" % rig.set_profile(0xA1))
        results["E 0xA1 balanced-perf"] = rig.measure(
            "E: profile 0xA1 (balanced-performance)   [reference]")
    finally:
        rig.restore()

    print("\n" + "=" * 70)
    print("SUMMARY (idle fan RPM, cpu/gpu)")
    print("=" * 70)
    base = results.get("A balanced, gs=0", (0, 0))
    for label, (cpu, gpu) in results.items():
        delta = cpu - base[0]
        print("  %-26s cpu=%6.0f gpu=%6.0f   delta vs baseline %+6.0f"
              % (label, cpu, gpu, delta))

    print("\nINTERPRETATION")
    threshold = 150  # rpm; larger than sampling noise
    b = results.get("B balanced, gs=1", (0, 0))[0] - base[0]
    c = results.get("C 0xAB, gs=0", (0, 0))[0] - base[0]
    d = results.get("D 0xAB, gs=1", (0, 0))[0] - base[0]
    if d > threshold and b <= threshold:
        print("  -> G-Mode REQUIRES profile 0xAB. The 0x25 flag alone does")
        print("     nothing. g15ctl must issue 0x15/0x01 with 0xAB as well.")
    elif c > threshold and b <= threshold:
        print("  -> profile 0xAB alone drives the fans; the 0x25 flag is")
        print("     cosmetic. g15ctl must issue 0x15/0x01 with 0xAB.")
    elif b > threshold:
        print("  -> the 0x25 flag alone does raise fan speed, so g15ctl's")
        print("     current implementation is correct and the symptom is")
        print("     something else.")
    else:
        print("  -> NO combination changed idle fan speed measurably.")
        print("     G-Mode likely only raises CPU/GPU power limits, and its")
        print("     fan effect appears only under load. Re-run this while a")
        print("     CPU stress load is active to confirm.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
