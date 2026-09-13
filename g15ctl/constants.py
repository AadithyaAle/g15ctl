"""Hardware facts for the Dell G-series AWCC/WMAX thermal interface.

Every constant in this module was read out of this machine's own firmware by
tools/probe2.sh (ACPI table decompilation) and tools/probe3.py (live resource
enumeration). Nothing here is copied from another laptop model, because the
WMAX namespace path and the set of supported thermal profiles genuinely differ
between G15 revisions -- e.g. the widely circulated `\\_SB.AMW3.WMAX` path does
not exist on the 5530 and silently returns AE_NOT_FOUND.

Reference (Dell G15 5530, BIOS 1.34.0):
    ACPI path : \\_SB.AMWW.WMAX  (Device \\_SB_.AMWW, PNP0C14:0b, in SSDT2)
    signature : WMAX(instance, method_id, buffer{op, arg1, arg2, arg3})
    fans      : 0x32 CPU, 0x33 GPU/Video, max 4800 RPM
    sensors   : 0x01 CPU, 0x06 GPU
    profiles  : 0xA0 balanced, 0xA1 balanced-performance, 0xA3 quiet,
                0xA5 low-power  (no 0xA2 cool, no 0xA4 performance)
    G-Mode    : method 0x25, currently OFF
"""

from __future__ import annotations

APP_NAME = "g15ctl"
APP_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# ACPI transport
# ---------------------------------------------------------------------------

ACPI_CALL_PROC = "/proc/acpi/call"

#: Candidate WMAX namespace paths, most likely first. The 5530 uses AMWW; other
#: G15/G16 revisions use AMW3 or AMW1. We probe rather than assume, so the tool
#: works across models instead of failing silently on the wrong path.
WMAX_PATH_CANDIDATES = (
    r"\_SB.AMWW.WMAX",
    r"\_SB.AMW3.WMAX",
    r"\_SB.AMW1.WMAX",
    r"\_SB.AMW2.WMAX",
    r"\_SB.AMW.WMAX",
    r"\_SB.PCI0.AMW1.WMAX",
)

#: The AWCC WMI GUID. Its presence proves the firmware implements the
#: interface, independent of which ACPI path exposes it.
AWCC_WMI_GUID = "A70591CE-A997-11DA-B012-B622A1EF5492"

# ---------------------------------------------------------------------------
# WMAX method IDs (from the decompiled `Method (WMAX, 3, Serialized)`)
# ---------------------------------------------------------------------------

M_FAN_SENSORS = 0x13     # AX10/AX11 - fan<->sensor topology
M_THERMAL_INFO = 0x14    # AX12..AX22 - all read-only information
M_THERMAL_CONTROL = 0x15  # AX23/AX24 - set profile / set fan boost
M_GAME_SHIFT = 0x25      # AX27/AX28 - set / get G-Mode

# Operations for M_THERMAL_INFO (buffer byte 0)
OP_SYS_DESCRIPTION = 0x02   # AX13() -> packed [fans, sensors, ?, profiles]
OP_RESOURCE_ID = 0x03       # AX14(index) -> resource id, walks a table
OP_GET_TEMP = 0x04          # AX15(sensor_id) -> degrees C
OP_GET_RPM = 0x05           # AX16(fan_id) -> RPM
OP_GET_FAN_MIN_RPM = 0x08   # AX18(fan_id)
OP_GET_FAN_MAX_RPM = 0x09   # AX19(fan_id)
OP_GET_BASE_PROFILE = 0x0A  # AX20() -> firmware default profile
OP_GET_CURRENT_PROFILE = 0x0B  # AX21() -> active profile id
OP_GET_FAN_BOOST = 0x0C     # AX22(fan_id) -> 0..255

# Operations for M_THERMAL_CONTROL (buffer byte 0)
OP_ACTIVATE_PROFILE = 0x01  # AX23(profile_id)
OP_SET_FAN_BOOST = 0x02     # AX24(fan_id, boost 0..255)

# Operations for M_GAME_SHIFT (buffer byte 0)
OP_SET_GAME_SHIFT = 0x01    # AX27(0|1)
OP_GET_GAME_SHIFT = 0x02    # AX28() -> 0|1

#: Firmware sentinels meaning "unsupported operation".
WMAX_ERRORS = (0xFFFFFFFE, 0xFFFFFFFF)

# ---------------------------------------------------------------------------
# Thermal profiles
# ---------------------------------------------------------------------------

# AWCC exposes two profile tables. The "USTT" table (0xA0-0xA5) is what modern
# G-series use; the legacy table (0x96-0x99) appears on older Alienware units.
PROFILE_IDS = {
    0x96: "legacy-quiet",
    0x97: "legacy-balanced",
    0x98: "legacy-balanced-performance",
    0x99: "legacy-performance",
    0xA0: "balanced",
    0xA1: "balanced-performance",
    0xA2: "cool",
    0xA3: "quiet",
    0xA4: "performance",
    0xA5: "low-power",
}

#: G-Mode is not a profile in the enumeration table; the firmware reports it as
#: the *current* profile (0xAB) once method 0x25 has switched it on.
PROFILE_GMODE = 0xAB
PROFILE_IDS_WITH_GMODE = {**PROFILE_IDS, PROFILE_GMODE: "g-mode"}

#: Canonical mode names accepted on the command line. `g-mode` is handled
#: separately because it is toggled through method 0x25, not 0x15.
MODE_GMODE = "g-mode"

#: Friendly aliases so users can type what AWCC or powerprofilesctl call things.
MODE_ALIASES = {
    "bal": "balanced",
    "balance": "balanced",
    "b": "balanced",
    "q": "quiet",
    "silent": "quiet",
    "perf": "balanced-performance",
    "p": "balanced-performance",
    "performance-mode": "balanced-performance",
    "battery": "low-power",
    "powersave": "low-power",
    "power-saver": "low-power",
    "lowpower": "low-power",
    "eco": "low-power",
    "g": MODE_GMODE,
    "gmode": MODE_GMODE,
    "game": MODE_GMODE,
    "gameshift": MODE_GMODE,
    "game-shift": MODE_GMODE,
    "turbo": MODE_GMODE,
}

#: How the in-tree alienware-wmi driver's platform_profile names map onto our
#: canonical names. With force_gmode=1 that driver exposes G-Mode as its
#: `performance` choice, so the rename is meaningful rather than cosmetic.
#:
#: This mapping is specific to alienware-wmi. Other platform_profile drivers
#: (notably dell-pc) use `performance` to mean an ordinary performance profile,
#: so they must not inherit it -- see backends._PlatformProfileBackend.
AWCC_NATIVE_PROFILE_MAP = {
    "performance": MODE_GMODE,
}

# ---------------------------------------------------------------------------
# Fans
# ---------------------------------------------------------------------------

#: Fallback fan IDs if runtime enumeration fails. Verified on the 5530 against
#: dell_ddv: 0x32 -> "CPU Fan", 0x33 -> "Video Fan".
DEFAULT_FAN_IDS = (0x32, 0x33)
FAN_LABELS = {0x32: "CPU", 0x33: "GPU"}

#: Fallback temperature sensor IDs (0x01 CPU, 0x06 GPU on the 5530).
DEFAULT_TEMP_IDS = (0x01, 0x06)
TEMP_LABELS = {0x01: "CPU", 0x06: "GPU"}

#: Boost is a 0-255 byte. Per the kernel's AWCC documentation the effect is
#: approximately pwm = pwm_base + (boost / 255) * (pwm_max - pwm_base), i.e. it
#: raises the floor of the firmware curve rather than replacing it.
BOOST_MIN = 0
BOOST_MAX = 255

#: Fallback max RPM if the firmware does not report one (0x14/0x09 -> 4800).
DEFAULT_FAN_MAX_RPM = 4800

# ---------------------------------------------------------------------------
# Safety limits
# ---------------------------------------------------------------------------

#: If any monitored sensor exceeds this while we hold a manual fan boost, the
#: watchdog releases control back to the firmware. Raptor Lake HX parts run hot
#: by design and throttle near 100 C, so we intervene before the silicon does.
WATCHDOG_TEMP_C = 95

#: Sustained temperature above which the watchdog forces maximum boost instead
#: of whatever the user asked for.
WATCHDOG_BOOST_TEMP_C = 88

#: Consecutive samples above threshold before acting, to ignore brief spikes.
WATCHDOG_SAMPLES = 3

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

CONFIG_PATH = "/etc/g15ctl/config.json"
STATE_PATH = "/var/lib/g15ctl/state.json"
LOG_DIR = "/var/log/g15ctl"
LOG_PATH = "/var/log/g15ctl/g15ctl.log"
RUNTIME_DIR = "/run/g15ctl"

HWMON_ROOT = "/sys/class/hwmon"
PLATFORM_PROFILE_CLASS = "/sys/class/platform-profile"
PLATFORM_PROFILE_LEGACY = "/sys/firmware/acpi/platform_profile"
PLATFORM_PROFILE_LEGACY_CHOICES = "/sys/firmware/acpi/platform_profile_choices"
