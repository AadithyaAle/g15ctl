# Reverse-engineering AWCC fan control on a Dell G15 5530 under Linux

**Build log and technical report for `g15ctl` v1.0.0**
Date: 2026-09-13 · Platform: Dell G15 5530, BIOS 1.34.0, Ubuntu 25.10, kernel 6.14.0-35-generic

---

## Abstract

Dell's Alienware Command Center (AWCC) controls thermal profiles, G-Mode and
fan speed on G-series laptops through a firmware WMI interface named `WMAX`.
No equivalent existed on Linux for this model. This log documents how the
interface was located and characterised **on this specific machine**, the
resulting tool, and the measured outcomes.

The central finding is that the publicly circulated approach for Dell G15 fan
control — ACPI calls to `\_SB.AMW3.WMAX` — **does not work on the 5530 and
fails silently**. The correct path is `\_SB.AMWW.WMAX`, declared in *SSDT2
rather than the DSDT*. Once corrected, full AWCC parity was achieved: four
thermal profiles, G-Mode, and per-fan boost verified to drive the fans from
1490 to 4716 RPM.

Result: **63/63 unit tests and 17/17 hardware checks pass.**

---

## 1. System under test

| Property | Value |
|---|---|
| Model | Dell G15 5530 (board 0NMHYY) |
| BIOS | 1.34.0, 2026-05-26 |
| OS | Ubuntu 25.10 (questing), kernel 6.14.0-35-generic |
| Secure Boot | Disabled (so DKMS modules load unsigned) |
| Fans | 2 — CPU, GPU/Video |
| Battery | 82 % health (3,947,000 / 4,815,000 µAh), 281 cycles |

---

## 2. Method

Control interfaces were not assumed; they were measured, in four read-only
stages. Every stage is reproducible from `tools/`.

| Stage | Tool | Purpose |
|---|---|---|
| 1 | `tools/probe.sh` | Inventory hwmon, WMI, modules; decompile DSDT |
| 2 | `tools/probe2.sh` | Dump **all** ACPI tables; locate `WMAX`; test native driver |
| 3 | `tools/probe3.py` | Enumerate the firmware's own resource tables |
| 4 | `tools/verify.sh` | End-to-end control tests with measured readback |

Stages 1–3 invoke only informational methods. Stage 4 performs writes but
records and restores the initial state via a `trap … EXIT INT TERM`.

A deliberate principle throughout: **verify writes through an independent
interface.** Fan boost is written via `WMAX` but read back via the
`dell_wmi_ddv` hwmon driver, so agreement between them is genuine evidence
rather than a value echoed back from the same register.

---

## 3. Findings

### 3.1 Four candidate interfaces existed; two were dead ends

| Interface | Status | Evidence |
|---|---|---|
| `dell_smm_hwmon` (i8k) | **Dead** | Loads, but exposes *zero* sysfs attributes — no `fan*`, no `pwm*`. Legacy SMM fan control is absent on this generation. |
| `acpi_call` → `\_SB.AMW3.WMAX` | **Dead** | Every call returned `AE_NOT_FOUND`. This path does not exist in BIOS 1.34.0. |
| `dell-pc` platform_profile | Works, limited | `cool quiet balanced performance`; no G-Mode, no fan control. |
| AWCC `WMAX` | **Works, full** | See below. |

The second row is the important one. The prior-art script cloned into this
repository (`Mohit-Pala/Dell_G15_Fan_Cli`) hardcodes `\_SB.AMW3.WMAX`, and the
ArchWiki snippets it derives from do the same. `acpi_call` reports failure only
to `dmesg`, so **such a script exits 0 and appears to work while changing
nothing.** This was the single most valuable result of probing before coding.

### 3.2 The AWCC device is in an SSDT, not the DSDT

Stage 1 decompiled the DSDT (653,360 bytes → 136,635 lines) and found **zero**
occurrences of `WMAX` or the AWCC GUID. The DSDT declares five unrelated WMI
devices (`AMW0`, `AMW2`, `AMW4`, `AMW5`, `AMWV`), which is precisely the trap
that makes a DSDT-only search produce a plausible but wrong answer.

Stage 2 dumped all **43** ACPI tables. `WMAX` was found in `ssdt2` (8,127
bytes):

```
ssdt2.dsl:78    Device (AMWW)
ssdt2.dsl:82        Name (_WDG, Buffer (0x28) ...)
ssdt2.dsl:867       Method (WMAX, 3, Serialized)
```

Confirmed against the live namespace: `PNP0C14:0b → \_SB_.AMWW`, and the WMI
device `A70591CE-A997-11DA-B012-B622A1EF5492` reports `object_id=AX`, which is
how ACPI-WMI names the method `WM` + `AX` = `WMAX`.

> **Authoritative path: `\_SB.AMWW.WMAX`**
> Signature: `WMAX(instance, method_id, buffer{op, arg1, arg2, arg3})`

### 3.3 Interface map, read from the decompiled method

The method dispatches on 18 IDs. The thermally relevant ones, with the
firmware subroutine each delegates to:

| Method | Op | Call | Meaning |
|---|---|---|---|
| `0x14` | `0x02` | `AX13()` | system descriptor |
| `0x14` | `0x03` | `AX14(i)` | enumerate resource ID at index |
| `0x14` | `0x04` / `0x05` | `AX15` / `AX16` | temperature / fan RPM |
| `0x14` | `0x08` / `0x09` | `AX18` / `AX19` | fan min / max RPM |
| `0x14` | `0x0A` / `0x0B` | `AX20` / `AX21` | default / current profile |
| `0x14` | `0x0C` | `AX22(fan)` | current fan boost |
| **`0x15`** | **`0x01`** | **`AX23(profile)`** | **set thermal profile** |
| **`0x15`** | **`0x02`** | **`AX24(fan, boost)`** | **set per-fan boost (0–255)** |
| **`0x25`** | **`0x01`** / `0x02` | **`AX27`** / `AX28` | **set / get G-Mode** |

This numbering matches the Linux kernel's `alienware-wmi-wmax` driver exactly,
which independently corroborates the decoding.

### 3.4 The firmware describes its own topology

Rather than hardcode IDs, stage 3 asked the firmware. Descriptor `0x14/0x02`
returned `0x04000202` = bytes `02 02 00 04` → **2 fans, 2 sensors, 4
profiles**, which the enumeration then confirmed exactly:

| Index | ID | Identified as |
|---|---|---|
| 0 | `0x32` | CPU fan — 1091 RPM, max **4800** |
| 1 | `0x33` | GPU fan — 1240 RPM, max **4800** |
| 2–3 | `0x101`, `0x106` | sensor aliases of `0x01` / `0x06` |
| 4 | `0xA0` | profile: balanced |
| 5 | `0xA1` | profile: balanced-performance |
| 6 | `0xA5` | profile: low-power |
| 7 | `0xA3` | profile: quiet |
| 8 | — | `AE_AML_PACKAGE_LIMIT` → end of table |

Sensors: `0x01` = CPU (65 °C), `0x06` = GPU (53 °C). Fan RPM agreed with
`dell_ddv` to the digit (1091/1240), confirming the ID→fan mapping.

**Notably absent: `0xA2` (cool) and `0xA4` (performance).** They exist in the
AWCC profile table but not in this firmware. A tool that assumed the full
table would offer two modes that do nothing. `g15ctl` therefore enumerates at
runtime and offers only what the firmware reports.

### 3.5 The in-tree kernel driver can be forced to work

Unexpectedly, kernel 6.14's `alienware-wmi` already carries
`force_platform_profile` and `force_gmode` (but **not** `force_hwmon`, added in
6.15). Loading with them succeeded:

```
platform-profile-1  name=alienware-wmi
  choices=[low-power quiet balanced balanced-performance performance]
```

Four choices map to the four enumerated profiles; the fifth, `performance`, is
G-Mode surfaced by `force_gmode=1`. This exactly reproduces AWCC's G15 lineup.
The load logs `Setting dangerous option force_platform_profile - tainting
kernel` — expected for an `unsafe` module parameter, with no functional
consequence.

This yields a genuinely useful fallback: **profiles and G-Mode without any
out-of-tree module.** Manual fan speed still requires `acpi_call` until kernel
6.15+ provides `force_hwmon`.

---

## 4. Implementation

```
g15ctl/
  constants.py   hardware facts, all derived from this machine's firmware
  acpi.py        /proc/acpi/call transport, flock-serialised
  backends.py    3 backends behind one capability-gated interface
  sensors.py     unprivileged hwmon collection
  controller.py  policy: validation, persistence, safety
  curve.py       fan curve interpolation + hysteresis
  daemon.py      restore, watchdog, curve
  cli.py tui.py  CLI and curses dashboard
gui/             GTK3 tray applet
```

Three backends, auto-selected most-capable-first:

| Backend | Profiles | G-Mode | Manual fan | Requires |
|---|:-:|:-:|:-:|---|
| `awcc-acpi` | ✓ | ✓ | **✓** | `acpi_call` + root |
| `awcc-native` | ✓ | ✓ | kernel ≥ 6.15 | in-tree driver |
| `dell-pc` | ✓ | ✗ | ✗ | always present |

Design decisions worth recording:

- **Capability gating, not silent no-ops.** A backend raises `BackendError`
  for what it cannot do. Pretending to set a fan speed is worse than refusing.
- **Runtime enumeration over hardcoding**, so the tool ports to other G-series
  models by probing rather than patching.
- **Monitoring needs no root.** Sensors come from `dell_wmi_ddv` hwmon, so
  `status`, `monitor` and the tray applet run as a normal user; only writes
  need privileges.
- **`flock` around `/proc/acpi/call`.** It is a single global write-then-read
  file; without serialisation the daemon, CLI and GUI would read each other's
  results.

### 4.1 Fan boost is a floor, not an absolute

The firmware accepts no absolute PWM. Its boost byte raises the floor of the
firmware's own curve, per kernel documentation:

```
pwm ≈ pwm_base + (boost / 255) × (pwm_max − pwm_base)
```

So `fan 50` means "at least ~half speed"; the firmware may still go faster.
This is a safety property, not a limitation: **a boost can never make the
machine run cooler-than-safe, and never disables firmware thermal protection.**

---

## 5. Results

### 5.1 Thermal profiles (`tools/verify.sh` test 1–2)

All four enumerated profiles applied and read back correctly. Idle effect:

| Profile | CPU fan | GPU fan |
|---|---|---|
| quiet | 1028 | 1185 |
| balanced-performance | 1808 | 2132 |

### 5.2 Manual fan boost (test 3) — the headline result

| Command | Boost byte | CPU fan | GPU fan |
|---|---|---|---|
| `fan auto` | 0 | 1490 | 1793 |
| `fan 40` | 102 | 2730 | 2992 |
| `fan 70` | 178 | 3942 | 4149 |
| `fan 100` | 255 | **4716** | **4773** |

Boost bytes are exactly `round(pct × 255/100)`. RPM is monotonic in boost, and
100 % reaches 4716/4773 RPM against the firmware's declared 4800 maximum —
i.e. **98–99 % of rated speed**. This is real proportional control, not on/off.

### 5.3 G-Mode (test 4) — initially wrong, see §6.1

The first version of this test checked only that the G-Mode *flag* read back
as `ON`, and it passed. That was a **false pass**: G-Mode was not actually
engaged. Measuring idle CPU fan RPM with `tools/gmode-test.py` exposed it:

| Thermal profile | Game Shift flag | CPU fan | Δ vs baseline |
|---|:-:|---|---|
| `0xA0` balanced | 0 | 1042 | — (baseline) |
| `0xA0` balanced | **1** | **1024** | **−19 — no effect** |
| `0xAB` | 0 | 4373 | +3330 |
| **`0xAB`** | **1** | **4991** | **+3949 — true G-Mode** |
| `0xA1` balanced-performance | 0 | 2700 | +1657 |

**G-Mode is two pieces of state, not one.** The Game Shift flag (`0x25`) is
inert on its own; the `0xAB` thermal profile (`0x15/0x01`) does the work, and
the flag adds a further step on top. Setting only the flag — which the first
implementation did — reported success while changing nothing.

Note `0xAB` is deliberately **absent** from the firmware's enumerated profile
table (§3.4), yet `0x15/0x01` accepts it. It cannot be discovered by
enumeration and must be requested explicitly.

Also of note: G-Mode reaches 4991/5000 RPM, *above* the 4800 RPM that
`0x14/0x09` reports as the maximum. The reported maximum is therefore a
nominal figure, not a hard ceiling.

### 5.4 Safety (test 5)

`reset` cleared boost → 0, G-Mode → off, profile → balanced.

### 5.5 Independent cross-check (test 6)

| Fan | via `WMAX` | via `dell_ddv` | Δ |
|---|---|---|---|
| CPU (`0x32`) | 1842 | 1895 | 53 |
| GPU (`0x33`) | 1977 | 2031 | 54 |

Two independent kernel paths agree within ~3 %, the residual being sample
timing. The interface is decoded correctly.

### 5.6 Test totals

```
Unit tests      66/66 pass   (no hardware required)
Hardware checks 19/19 pass
```

---

## 6. Bugs found and fixed

Recorded because each was caught by a check rather than by inspection.

### 6.1 The worst one: G-Mode did nothing

This bug deserves its own section because of *how* it survived.

**Symptom.** `g15ctl mode g-mode` printed success and the flag read back `ON`,
but the fans did not change. Reported by the user, not by any test.

**Why the tests missed it.** Two compounding mistakes, both mine:

1. The hardware test asserted on a **flag readback** rather than on a physical
   effect. `0x25/0x02` returning 1 only proves the flag was stored.
2. When the test initially *failed* (the profile read `0xA0`, not `0xAB`), the
   fix applied was to make `get_mode()` trust the flag first — so the test
   went green while the underlying behaviour stayed broken. **A failing test
   was silenced instead of diagnosed.**

**Root cause.** G-Mode requires `0x15/0x01` with profile `0xAB` *and*
`0x25/0x01`. Only the latter was implemented. Quantified in §5.3: the flag
alone moves the fans by −19 RPM, i.e. not at all.

**Fix.** `set_gmode(True)` now activates `0xAB` then sets the flag;
`set_gmode(False)` clears the flag *and* restores the base profile from
`0x14/0x0A`, since clearing only the flag would leave the fans pinned.

**Test changes.** `verify.sh` test 4 now settles to a clean idle baseline and
requires a **>1000 RPM rise**, checks the raw profile really is `0xAB`, and
checks the fans come back down afterwards. The unit-test fake now models the
measured RPM per `(profile, flag)` pair, so assertions are on effect rather
than on flags. Both new tests were confirmed to fail against the old
implementation (`AssertionError: 1024 not greater than 2042`).

**Lesson.** For hardware control, assert on the physical consequence. A write
that the firmware accepts and stores is not the same as a write that does
something.

### 6.2 The rest

| # | Bug | Found by | Fix |
|---|---|---|---|
| 1 | **G-Mode inert** — see §6.1. | User report; `tools/gmode-test.py` | Activate profile `0xAB` as well as the flag. |
| 2 | **`dell-pc` capability overstated.** A shared name map aliased `performance` → `g-mode`, making the fallback backend claim G-Mode it does not have. | Reviewing `mode` output | Per-backend `profile_map`; the alias now applies only to `alienware-wmi`. |
| 3 | **`force_hwmon` would break module loading.** `modprobe` *refuses* a module given an unknown parameter, so shipping it in `modprobe.d` would stop `alienware_wmi` loading at all on kernel 6.14. | Reasoning about 6.14 vs 6.15 | `install.sh` greps `modinfo` and writes only supported parameters. |
| 4 | **systemd sandbox blocked `modprobe`.** `SystemCallFilter=@system-service` excludes `@module` and the capability set omitted `CAP_SYS_MODULE`, so the daemon's fallback module load would fail with EPERM. | `systemd-analyze verify` review | Added `@module` and `CAP_SYS_MODULE`. |
| 5 | **`install.sh` broken under `curl \| bash`.** `BASH_SOURCE[0]` is not a real path, so checkout detection and the uninstaller copy misbehaved. | Reviewing the pipe path | Explicit `SELF` detection; payload copy used when piped. |
| 6 | `dict(PROFILE_IDS, **{int: str})` → `TypeError: keywords must be strings`. | First import | Dict literal unpacking. |
| 7 | **TUI rendered as solid blocks.** The dashboard used U+2588/U+2591 for meters; many terminal fonts draw U+2591 (light shade) as a *full* cell, so filled and empty were identical and every bar became one rectangle. | User screenshot | Meters now use reverse-video spaces, which are font-independent; the trend line uses an ASCII ramp. CLI bars are plain ASCII. |
| 8 | **Log records corrupted the TUI.** `logs.setup()` attached a stderr handler even for `monitor`, so each fan change wrote over the curses display and scrolled it. | User screenshot | `monitor` gets a `NullHandler` for the console (file/journal logging unaffected), plus `scrollok(False)` and `KEY_RESIZE` handling. |
| 9 | **`keyprobe.py` false positive.** It counted *any* input event, so Alt+Tab and touchpad contact (`KEY_TAB`, `KEY_LEFTALT`, `BTN_TOUCH`) were reported as "the G key is visible to Linux". | Inspecting the reported key codes | Pointer devices skipped, modifiers/Tab/`BTN_*` filtered, `KEY_F10` required in the control step, and only codes unique to step 2 are reported. |

---

## 7. Dual-boot: keeping Windows AWCC unaffected

This was an explicit requirement and shaped the design.

**Mechanism.** Everything `g15ctl` writes is *volatile EC state* reached via
ACPI. Nothing touches BIOS NVRAM, and no `dell_wmi_sysman` firmware attributes
are written. A power cycle clears it all regardless.

**The real risk** is narrower than "settings persisting": "volatile" means lost
on power removal, **not** on a warm reboot. Leaving G-Mode asserted and
rebooting straight into Windows could hand AWCC an EC that disagrees with its
own idea of state.

**Mitigation.** `g15ctl-reset.service` is a `RemainAfterExit=yes` oneshot whose
`ExecStop` runs `g15ctl reset`. systemd stops enabled units on shutdown *and*
reboot, so boost → 0, G-Mode → off and profile → balanced are guaranteed before
power-off. Verified as test 5. `g15ctl.service` additionally carries
`ExecStopPost=-/usr/bin/g15ctl reset` in case the daemon dies uncleanly.

Uninstalling calls `g15ctl reset` first, and a reboot fully reverts the
`alienware_wmi` module parameters.

---

## 8. Safety design

- **Watchdog.** Monitors the hottest *coolable* sensor (charger, battery and
  SODIMM excluded, since fan speed does not govern them). Above **88 °C** it
  forces fans to maximum regardless of the requested speed; above **95 °C** it
  releases control to the firmware entirely — the firmware's curve is
  thermally validated and a user-space one is not. Requires 3 consecutive hot
  samples so transient spikes are ignored.
- **Fail-safe on error.** Any exception in the daemon loop releases fan
  control rather than holding an unverifiable speed.
- **Curve validation** rejects decreasing percentages, duplicate temperatures
  and out-of-range values instead of clamping, so a config typo is loud.
- **Hysteresis** (default 3 °C, downward only) prevents oscillation while
  still responding immediately to rising temperature.
- **Profile switches clear manual boost**, so a boost cannot silently persist
  into a profile chosen later.

---

## 9. Limitations

1. **Manual fan control needs `acpi_call`** on kernel < 6.15 — an out-of-tree
   DKMS module that permits arbitrary ACPI evaluation. It is used narrowly
   here, but it is a broad privilege. On kernel 6.15+ the in-tree
   `force_hwmon=1` path removes this need; the backend picks it up
   automatically.
2. **Kernel taint** when the native backend is enabled via forced parameters.
   Cosmetic, but it will appear in bug reports.
3. **`cool` and `performance` profiles are unavailable** on this firmware.
   Hardware limitation, not a software one.
4. **Boost is a floor, not a ceiling** — fans cannot be forced *below* the
   firmware curve. This is by design and cannot be changed safely.
5. **Single model verified.** The code is written to enumerate rather than
   assume, and carries several candidate ACPI paths, but only the 5530 /
   BIOS 1.34.0 has been measured.
6. **`thermald` is active** and manages Intel RAPL power limits. It does not
   contend for fan or profile state, so they coexist; `doctor` reports it.

---

## 10. Reproduction

```bash
python3 tests/test_g15ctl.py      # 63 unit tests, any machine
sudo bash tools/probe2.sh         # locate WMAX on a new model
sudo python3 tools/probe3.py      # enumerate its resource tables
sudo bash tools/verify.sh         # 17 hardware checks, self-restoring
```

Artifacts retained in the repository: `probe-report.txt`,
`probe2-report.txt`, `probe3-report.txt`, `verify-report.txt`.

---

## 11. Key notes

1. **The published `AMW3` path is wrong for the 5530 and fails silently.** The
   correct path is `\_SB.AMWW.WMAX`, in **SSDT2, not the DSDT**. Any guide that
   greps only the DSDT will find five decoy `AMWx` devices and conclude wrongly.
2. **Probe before coding.** Three of the four candidate interfaces were dead or
   limited. Writing code first would have produced a tool that appeared to work.
3. **Ask the firmware, don't hardcode.** `0x14/0x03` enumerates fans, sensors
   and profiles. This is what revealed that `cool` and `performance` don't
   exist here, and is what makes the tool portable.
4. **Verify through a second interface.** Writing via `WMAX` and reading via
   `dell_ddv` (Δ ≈ 53 RPM) is what turns "the call returned 0" into evidence.
5. **G-Mode is two writes, not one:** thermal profile `0xAB` via `0x15/0x01`
   **and** the Game Shift flag via `0x25/0x01`. The flag alone is inert
   (−19 RPM). `0xAB` is not in the firmware's enumerated profile table, so it
   cannot be discovered — it must be requested explicitly.
6. **Assert on physical effect, not on flag readback.** A stored flag is not a
   working feature. Checking `0x25/0x02 == 1` let a completely non-functional
   G-Mode pass as working, and "fixing" the resulting test failure by changing
   the *detection* logic hid the bug rather than solving it (§6.1).
7. **Manual fan control is genuinely proportional:** 1490 → 4716 RPM across
   boost 0 → 255, monotonic, at 98 % of rated maximum.
8. **Kernel 6.14 already has `force_platform_profile`/`force_gmode`**, giving
   profiles and G-Mode with no out-of-tree module. Upgrading to **6.17**
   (already in Ubuntu 25.10's repos) would add `force_hwmon` and make
   `acpi_call` unnecessary — the recommended next step.
9. **Dual-boot safety is a shutdown-ordering problem,** not a persistence
   problem: EC state survives warm reboots, so it is explicitly cleared before
   power-off.

---

## 12. Recommended next step

Upgrade to kernel 6.17 (`sudo apt install linux-generic`, already at
6.17.0-41 in `questing-updates`). `g15ctl` will then detect `force_hwmon`,
expose `fan[1-2]_boost` natively, and drop the `acpi_call` dependency
entirely — no code changes required. Also consider submitting `Dell G15 5530`
to `awcc_dmi_table` in `drivers/platform/x86/dell/alienware-wmi-wmax.c` so the
forced parameters, and the kernel taint, become unnecessary.
