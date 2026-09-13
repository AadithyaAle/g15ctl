#!/usr/bin/env python3
"""g15ctl hardware probe, stage 3 -- enumerate the AWCC interface.

Stage 2 established the authoritative facts for this machine:
  * ACPI path      : \\_SB.AMWW.WMAX
  * Call convention: WMAX(0, method_id, buffer{op, arg1, arg2, arg3})

This stage reads the firmware's own resource tables so we hardcode nothing:
which fan IDs exist, which temperature sensor IDs exist, each fan's min/max
RPM, the current thermal profile, and which profile IDs are supported.

READ-ONLY. Only informational methods are called:
  0x13 (get fan/sensor counts), 0x14 (thermal information), 0x25/0x02 (get
  G-Mode status). The control method 0x15 and the G-Mode *set* op 0x25/0x01
  are never invoked.

Usage: sudo python3 tools/probe3.py | tee probe3-report.txt
"""

import os
import subprocess
import sys

ACPI_CALL = "/proc/acpi/call"
WMAX = r"\_SB.AMWW.WMAX"

# Method IDs, read straight out of the decompiled WMAX method.
M_FAN_SENSORS = 0x13
M_THERMAL_INFO = 0x14
M_GAME_SHIFT = 0x25

# Operations for M_THERMAL_INFO (buffer byte 0).
OP_SYS_DESCRIPTION = 0x02
OP_RESOURCE_ID = 0x03
OP_GET_TEMP = 0x04
OP_GET_RPM = 0x05
OP_GET_FAN_MIN_RPM = 0x08
OP_GET_FAN_MAX_RPM = 0x09
OP_GET_CURRENT_PROFILE = 0x0B
OP_GET_FAN_BOOST = 0x0C

# Profile IDs used by AWCC firmware (superset; we detect which are real).
KNOWN_PROFILES = {
    0x96: "legacy-quiet",
    0x97: "legacy-balanced",
    0x98: "legacy-balanced-performance",
    0x99: "legacy-performance",
    0xA0: "ustt-balanced",
    0xA1: "ustt-balanced-performance",
    0xA2: "ustt-cool",
    0xA3: "ustt-quiet",
    0xA4: "ustt-performance",
    0xA5: "ustt-low-power",
    0xAB: "gmode",
}


def die(msg):
    print("FATAL: " + msg, file=sys.stderr)
    sys.exit(1)


def ensure_ready():
    if os.geteuid() != 0:
        die("must run as root: sudo python3 tools/probe3.py")
    if not os.path.exists(ACPI_CALL):
        subprocess.run(["modprobe", "acpi_call"], check=False)
    if not os.path.exists(ACPI_CALL):
        die("/proc/acpi/call unavailable; install acpi-call-dkms")


def wmax(method, op, a1=0, a2=0, a3=0):
    """Evaluate WMAX(0, method, {op, a1, a2, a3}); return int or error string."""
    cmd = "%s 0 %#x {%#x, %#x, %#x, %#x}" % (WMAX, method, op, a1, a2, a3)
    try:
        with open(ACPI_CALL, "w") as f:
            f.write(cmd)
        with open(ACPI_CALL, "rb") as f:
            raw = f.read().split(b"\0")[0].decode(errors="replace").strip()
    except OSError as e:
        return "OSError: %s" % e
    if not raw or raw.startswith("Error") or raw.startswith("not called"):
        return raw or "(empty)"
    try:
        return int(raw, 16) if raw.startswith("0x") else int(raw)
    except ValueError:
        return raw


def ok(v):
    # 0xFFFFFFFE / 0xFFFFFFFF are the firmware's "unsupported" sentinels.
    return isinstance(v, int) and v not in (0xFFFFFFFE, 0xFFFFFFFF)


def show(v):
    return "%#x (%d)" % (v, v) if isinstance(v, int) else str(v)


def section(title):
    print("\n===== %s =====" % title)


def main():
    ensure_ready()

    section("IDENTITY")
    for f, label in (
        ("/sys/class/dmi/id/product_name", "product"),
        ("/sys/class/dmi/id/bios_version", "bios"),
    ):
        try:
            print("  %-10s: %s" % (label, open(f).read().strip()))
        except OSError:
            pass
    print("  %-10s: %s" % ("kernel", os.uname().release))
    print("  %-10s: %s" % ("acpi path", WMAX))

    section("SANITY CHECK")
    cur = wmax(M_THERMAL_INFO, OP_GET_CURRENT_PROFILE)
    print("  current thermal profile (0x14/0x0b) = %s -> %s"
          % (show(cur), KNOWN_PROFILES.get(cur, "unknown") if isinstance(cur, int) else "n/a"))
    if not ok(cur):
        die("WMAX is not responding at %s. Aborting before deeper probing." % WMAX)
    print("  WMAX responds correctly.")

    section("SYSTEM DESCRIPTION (0x14/0x02)")
    desc = wmax(M_THERMAL_INFO, OP_SYS_DESCRIPTION)
    print("  raw = %s" % show(desc))
    if isinstance(desc, int):
        b = desc.to_bytes(4, "little")
        print("  bytes = %s" % " ".join("%#04x" % x for x in b))
        print("  interpretation: fans=%d sensors=%d thermals=%d unknown=%d"
              % (b[0], b[1], b[2], b[3]))

    section("FAN / SENSOR COUNTS (0x13)")
    for op in (0x01, 0x02):
        print("  0x13/%#04x -> %s" % (op, show(wmax(M_FAN_SENSORS, op))))

    section("RESOURCE ID ENUMERATION (0x14/0x03)")
    resources = []
    for idx in range(32):
        v = wmax(M_THERMAL_INFO, OP_RESOURCE_ID, idx)
        if not ok(v):
            print("  index %-2d -> %s  [end of table]" % (idx, show(v)))
            break
        kind = KNOWN_PROFILES.get(v)
        label = "THERMAL PROFILE: %s" % kind if kind else ""
        print("  index %-2d -> %s  %s" % (idx, show(v), label))
        resources.append(v)

    # Classify: profile IDs are >= 0x90, fans are typically 0x31+, sensors low.
    profiles = [r for r in resources if r in KNOWN_PROFILES]
    others = [r for r in resources if r not in KNOWN_PROFILES]
    print("\n  --> thermal profile IDs : %s" % [hex(p) for p in profiles])
    print("  --> other resource IDs  : %s" % [hex(o) for o in others])

    section("TEMPERATURE SENSORS (0x14/0x04)")
    temps = []
    for rid in sorted(set(others + list(range(0x01, 0x0A)))):
        v = wmax(M_THERMAL_INFO, OP_GET_TEMP, rid)
        if ok(v) and 0 < v < 150:
            print("  sensor id %#04x -> %d C" % (rid, v))
            temps.append(rid)
    if not temps:
        print("  (none responded in a plausible range)")

    section("FANS (0x14/0x05 rpm, 0x08 min, 0x09 max, 0x0c boost)")
    fans = []
    candidates = sorted(set(others + [0x31, 0x32, 0x33, 0x34]))
    for rid in candidates:
        rpm = wmax(M_THERMAL_INFO, OP_GET_RPM, rid)
        if not (ok(rpm) and 0 <= rpm < 12000):
            continue
        mn = wmax(M_THERMAL_INFO, OP_GET_FAN_MIN_RPM, rid)
        mx = wmax(M_THERMAL_INFO, OP_GET_FAN_MAX_RPM, rid)
        boost = wmax(M_THERMAL_INFO, OP_GET_FAN_BOOST, rid)
        print("  fan id %#04x -> rpm=%-6s min=%-14s max=%-14s boost=%s"
              % (rid, rpm, show(mn), show(mx), show(boost)))
        fans.append(rid)
    if not fans:
        print("  (no fan IDs responded)")
    print("\n  --> fan IDs: %s" % [hex(f) for f in fans])

    section("MISC INFORMATIONAL OPS (0x14)")
    for op in (0x01, 0x07, 0x0A):
        print("  0x14/%#04x -> %s" % (op, show(wmax(M_THERMAL_INFO, op))))

    section("G-MODE STATUS (0x25/0x02, read-only)")
    gs = wmax(M_GAME_SHIFT, 0x02)
    print("  game shift status = %s" % show(gs))
    if isinstance(gs, int):
        print("  interpretation: %s" % ("ON" if gs == 1 else "OFF" if gs == 0 else "unknown"))

    section("CROSS-CHECK AGAINST dell_ddv HWMON")
    import glob
    for h in glob.glob("/sys/class/hwmon/hwmon*"):
        try:
            if open(os.path.join(h, "name")).read().strip() != "dell_ddv":
                continue
        except OSError:
            continue
        for fan in sorted(glob.glob(os.path.join(h, "fan*_input"))):
            lbl = fan.replace("_input", "_label")
            try:
                name = open(lbl).read().strip()
            except OSError:
                name = "?"
            print("  %-10s = %s rpm" % (name, open(fan).read().strip()))

    section("SUMMARY FOR g15ctl")
    print("  acpi_path      = %s" % WMAX)
    print("  profile_ids    = %s" % {hex(p): KNOWN_PROFILES[p] for p in profiles})
    print("  fan_ids        = %s" % [hex(f) for f in fans])
    print("  temp_ids       = %s" % [hex(t) for t in temps])
    print("\nDone. No control methods were invoked.")


if __name__ == "__main__":
    main()
