# g15ctl

Alienware Command Center style fan and thermal control for Dell G-series
laptops on Linux.

Developed and verified on a **Dell G15 5530 (BIOS 1.34.0, Ubuntu 25.10,
kernel 6.14)** by decompiling the machine's own ACPI tables, rather than
copying method IDs from other models — which is why it works where the
commonly circulated scripts silently do nothing.

```
Thermal mode:  BALANCED   backend awcc-acpi
Power:         AC | governor powersave | 2128 MHz avg

Fans  (control: auto)
  CPU    1245 rpm  [██████░░░░░░░░░░░░░░░░]  26%  of 4800
  GPU    1467 rpm  [███████░░░░░░░░░░░░░░░]  31%  of 4800

Temperatures
  CPU          65 C   CPU Package  80 C   GPU          53 C
  Charger      51 C   NVMe 1       53 C   NVMe 2       34 C
  SODIMM       53 C   SODIMM 2     53 C   Wi-Fi        60 C
  hottest: CPU Package at 80 C

Battery: 75%  Charging  health 82%  281 cycles
```

## Install

```bash
curl -fsSL https://github.com/AadithyaAle/g15ctl/releases/latest/download/install.sh | sudo bash
```

Or from a checkout:

```bash
sudo ./install.sh
```

Uninstall with `sudo g15ctl-uninstall`.

## Usage

Monitoring works as a normal user. Changing anything needs `sudo`.

```bash
g15ctl status              # temperatures, fans, mode, battery health
g15ctl monitor             # live dashboard with a temperature sparkline
g15ctl status --json       # machine-readable, for scripts and bars

sudo g15ctl mode quiet     # switch thermal profile
sudo g15ctl mode           # list what your firmware supports
sudo g15ctl fan 70         # pin fans to at least 70%
sudo g15ctl fan auto       # hand the fans back to the firmware
sudo g15ctl gmode toggle   # G-Mode (Game Shift)
sudo g15ctl reset          # full handback to firmware

g15ctl doctor              # diagnose hardware, backends and conflicts
g15ctl logs -f             # follow service logs
man g15ctl                 # full documentation
```

A tray applet is installed as `g15ctl-gui` ("G15 Fan Control" in your
launcher).

### Thermal modes

The 5530's firmware reports four profiles, plus G-Mode:

| Mode | Firmware ID | Notes |
|---|---|---|
| `low-power` | `0xA5` | lowest power and noise |
| `quiet` | `0xA3` | prioritises silence |
| `balanced` | `0xA0` | firmware default |
| `balanced-performance` | `0xA1` | AWCC's "Performance" |
| `g-mode` | `0xAB` | maximum fans and power, via method `0x25` |

`cool` (`0xA2`) and `performance` (`0xA4`) exist in the AWCC profile table but
are **not implemented on this model**, so `g15ctl` does not offer them. Run
`g15ctl mode` to see what your own firmware reports.

Aliases are accepted: `perf`, `silent`, `turbo`, `powersave`, `b`, `q`, `g`.

### Fan boost semantics

The firmware does not take an absolute PWM. It takes a *boost* byte (0–255)
that raises the floor of its own fan curve:

```
pwm ≈ pwm_base + (boost / 255) × (pwm_max − pwm_base)
```

So `g15ctl fan 50` means "at least about half speed" — the firmware will still
spin faster if it wants to. A boost can therefore never make the machine run
hotter than leaving it on automatic.

### Automatic fan curve

Off by default. The background service applies it when enabled:

```bash
sudo g15ctl curve set 50:0 65:25 75:50 85:100
sudo g15ctl curve enable
sudo systemctl restart g15ctl
g15ctl curve show
```

Points are `TEMP:PERCENT`, linearly interpolated, with a hysteresis band so the
fan does not oscillate on a boundary.

## Dual boot with Windows

Fan boost and G-Mode live in **volatile EC state**, but "volatile" means lost
on power removal, not on a warm reboot. `g15ctl` therefore clears them before
shutdown via `g15ctl-reset.service`, so rebooting into Windows always hands
Alienware Command Center the firmware default.

Nothing is ever written to BIOS NVRAM, and no `dell_wmi_sysman` firmware
attributes are touched. Everything is runtime EC state that a power cycle
clears anyway.

## Safety

- A watchdog monitors the hottest relevant sensor. Above **88 °C** it forces
  fans to maximum regardless of what you asked for; above **95 °C** it releases
  control to the firmware entirely, on the grounds that the firmware's curve is
  thermally validated and a user-space one is not.
- The daemon releases fan control if any ACPI call fails, rather than holding a
  speed it cannot verify.
- A fan boost only ever raises the floor of the firmware curve, so the
  firmware's own thermal protection always remains in effect.
- The tool refuses profiles the firmware did not report as supported, instead
  of sending arbitrary bytes to the EC.

## How it works

Three backends, detected automatically, most capable first:

| Backend | Profiles | G-Mode | Manual fan | Requires |
|---|:-:|:-:|:-:|---|
| `awcc-acpi` | yes | yes | **yes** | `acpi_call` + root |
| `awcc-native` | yes | yes | kernel ≥ 6.15 | in-tree `alienware-wmi` |
| `dell-pc` | yes | no | no | always present |

`awcc-acpi` calls the firmware's `WMAX` ACPI method directly:

```
\_SB.AMWW.WMAX(0, method_id, buffer{op, arg1, arg2, arg3})
```

| Method | Op | Purpose |
|---|---|---|
| `0x14` | `0x02`/`0x03` | system description / enumerate resource IDs |
| `0x14` | `0x04`/`0x05` | read temperature / fan RPM |
| `0x14` | `0x08`/`0x09` | fan min / max RPM |
| `0x14` | `0x0B`/`0x0C` | current profile / current fan boost |
| `0x15` | `0x01`/`0x02` | **set profile / set per-fan boost** |
| `0x25` | `0x01`/`0x02` | **set / get G-Mode** |

Fan and sensor IDs are *enumerated from the firmware at runtime* (`0x14`/`0x03`)
rather than hardcoded, so the tool adapts to other G-series models. On the
5530 this yields fans `0x32` (CPU) and `0x33` (GPU), both 4800 RPM max, and
sensors `0x01` (CPU) and `0x06` (GPU).

Sensor *reading* uses `dell_wmi_ddv` hwmon, which needs no privileges — that is
why the dashboard runs as a normal user.

### Why not `\_SB.AMW3.WMAX`?

Most Dell G15 guides and scripts hardcode `\_SB.AMW3.WMAX` or
`\_SB.AMWW.WMAX` in the DSDT. On the 5530 the AWCC device is `\_SB.AMWW`,
declared in **SSDT2, not the DSDT**. Calling the wrong path returns
`AE_NOT_FOUND` and the script appears to work while doing nothing at all.

`tools/probe2.sh` finds the correct path on any machine by dumping every ACPI
table and locating the `WMAX` method.

## Requirements

- Dell G-series laptop with the AWCC WMI device
  (`A70591CE-A997-11DA-B012-B622A1EF5492`)
- Python 3.9+ (standard library only)
- `acpi-call-dkms` for manual fan control
- Secure Boot disabled, or the DKMS module enrolled via MOK
- Tray applet: `python3-gi`, `gir1.2-gtk-3.0`,
  `gir1.2-ayatanaappindicator3-0.1` (preinstalled on stock Ubuntu desktop)

## Development

```bash
python3 tests/test_g15ctl.py        # 61 unit tests, no hardware needed
sudo bash tools/verify.sh           # end-to-end hardware verification
sudo bash tools/probe2.sh           # find the WMAX path on a new model
sudo python3 tools/probe3.py        # enumerate the AWCC resource tables
```

`log.md` documents the full investigation and measured results.

## Porting to another G-series model

1. `sudo bash tools/probe2.sh` — finds your `WMAX` ACPI path.
2. `sudo python3 tools/probe3.py` — lists your fan IDs, sensor IDs and
   supported profiles.
3. If the path differs, add it to `WMAX_PATH_CANDIDATES` in
   `g15ctl/constants.py`. Everything else is discovered at runtime.

Consider also submitting your model to the kernel's `awcc_dmi_table` in
`drivers/platform/x86/dell/alienware-wmi-wmax.c` so the in-tree driver works
without forced module parameters.

## Credits

The AWCC interface is documented by Kurt Borja's kernel driver
(`Documentation/admin-guide/laptops/alienware-wmi.rst`), which is the reference
for the method/operation numbering and the fan boost formula used here.

## License

MIT — see [LICENSE](LICENSE).

Not affiliated with or endorsed by Dell Technologies or Alienware. This is an
independent implementation written against the machine's own ACPI tables and
the public kernel documentation.
