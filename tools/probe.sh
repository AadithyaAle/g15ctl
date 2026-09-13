#!/usr/bin/env bash
# g15ctl hardware probe -- READ ONLY.
#
# This script gathers ground truth about the AWCC/WMAX thermal interface on this
# machine. It deliberately only ever calls the AWCC *information* method (0x14).
# It never calls the thermal *control* method (0x15) nor Game Shift (0x25), so it
# cannot change your fan or power state.
#
# Usage: sudo bash tools/probe.sh 2>&1 | tee probe-report.txt

set -uo pipefail

OUT_DIR="${OUT_DIR:-/tmp/g15ctl-probe}"
mkdir -p "$OUT_DIR"

if [[ $EUID -ne 0 ]]; then
    echo "FATAL: must run as root (sudo bash tools/probe.sh)" >&2
    exit 1
fi

hr() { printf '\n===== %s =====\n' "$1"; }

hr "SYSTEM"
echo "date         : $(date -Is)"
echo "kernel       : $(uname -r)"
echo "product      : $(cat /sys/class/dmi/id/product_name 2>/dev/null)"
echo "vendor       : $(cat /sys/class/dmi/id/sys_vendor 2>/dev/null)"
echo "bios         : $(cat /sys/class/dmi/id/bios_version 2>/dev/null)"
echo "bios date    : $(cat /sys/class/dmi/id/bios_date 2>/dev/null)"
echo "board        : $(cat /sys/class/dmi/id/board_name 2>/dev/null)"
echo "distro       : $(. /etc/os-release && echo "$PRETTY_NAME")"
echo "secureboot   : $(mokutil --sb-state 2>/dev/null || echo 'mokutil not installed')"

hr "LOADED DELL/WMI/ALIENWARE MODULES"
lsmod | grep -iE 'dell|i8k|acpi_call|alienware|^wmi|platform_profile' || echo "(none)"

hr "ALIENWARE-WMI MODULE CAPABILITIES"
# The split alienware-wmi-wmax driver (kernel >= 6.15) is what gives us native
# G-Mode + manual fan control. The old monolithic alienware-wmi has no params.
for m in alienware_wmi alienware_wmi_wmax alienware_wmi_legacy; do
    f=$(modinfo -n "$m" 2>/dev/null)
    if [[ -n "$f" ]]; then
        echo "--- $m ($f)"
        modinfo "$m" 2>/dev/null | grep -E '^parm:' || echo "    no module parameters (old monolithic driver)"
    else
        echo "--- $m: not present"
    fi
done

hr "PLATFORM PROFILE"
for d in /sys/class/platform-profile/*/; do
    [[ -d "$d" ]] || continue
    echo "$(basename "$d"): name=$(cat "$d/name" 2>/dev/null)"
    echo "    choices: $(cat "$d/choices" 2>/dev/null)"
    echo "    current: $(cat "$d/profile" 2>/dev/null)"
done
echo "legacy acpi node: $(cat /sys/firmware/acpi/platform_profile 2>/dev/null) [choices: $(cat /sys/firmware/acpi/platform_profile_choices 2>/dev/null)]"

hr "HWMON INVENTORY"
for h in /sys/class/hwmon/hwmon*; do
    name=$(cat "$h/name" 2>/dev/null)
    echo "--- $h ($name)"
    for f in "$h"/fan*_input "$h"/fan*_boost "$h"/fan*_label "$h"/pwm* "$h"/temp*_input "$h"/temp*_label; do
        [[ -f "$f" ]] || continue
        echo "      $(basename "$f") = $(cat "$f" 2>&1)"
    done
done

hr "WMI DEVICES (AWCC = A70591CE-A997-11DA-B012-B622A1EF5492)"
for d in /sys/bus/wmi/devices/*/; do
    guid=$(basename "$d")
    drv="(unbound)"
    [[ -L "$d/driver" ]] && drv=$(basename "$(readlink -f "$d/driver")")
    printf '  %-45s obj=%-6s inst=%-3s driver=%s\n' \
        "$guid" "$(cat "$d/object_id" 2>/dev/null)" \
        "$(cat "$d/instance_count" 2>/dev/null)" "$drv"
done

hr "DSDT: LOCATE WMAX METHOD"
# Ground truth for the ACPI path. On G-series this is \_SB.AMW3.WMAX or
# \_SB.AMWW.WMAX depending on firmware revision.
cd "$OUT_DIR" || exit 1
rm -f dsdt.dat dsdt.dsl ./*.dat 2>/dev/null
if command -v acpidump >/dev/null && command -v iasl >/dev/null; then
    acpidump -b -n DSDT -o "$OUT_DIR/dsdt.dat" >/dev/null 2>&1 \
        || cat /sys/firmware/acpi/tables/DSDT > "$OUT_DIR/dsdt.dat"
    iasl -d "$OUT_DIR/dsdt.dat" >/dev/null 2>&1
    if [[ -f "$OUT_DIR/dsdt.dsl" ]]; then
        echo "decompiled: $OUT_DIR/dsdt.dsl ($(wc -l < "$OUT_DIR/dsdt.dsl") lines)"
        echo
        echo "-- scopes/devices declaring a WMAX method --"
        grep -nE 'Method *\(WMAX' "$OUT_DIR/dsdt.dsl"
        echo
        echo "-- candidate AWCC device names --"
        grep -nE 'Device *\((AMW3|AMWW|AMW[0-9A-Z]|AWCC)' "$OUT_DIR/dsdt.dsl"
        echo
        echo "-- _WDG / WMI GUID bindings mentioning AX (WMAX) --"
        grep -n 'A70591CE' "$OUT_DIR/dsdt.dsl" | head
        echo
        # Extract the WMAX method body so we can read the real switch cases.
        awk '/Method *\(WMAX/{f=1; d=0}
             f{print NR": "$0; d+=gsub(/{/,"{"); d-=gsub(/}/,"}"); if(d<=0 && NR>1 && /}/){exit}}' \
            "$OUT_DIR/dsdt.dsl" > "$OUT_DIR/wmax-method.txt"
        echo "-- WMAX method body written to $OUT_DIR/wmax-method.txt ($(wc -l < "$OUT_DIR/wmax-method.txt") lines) --"
        head -80 "$OUT_DIR/wmax-method.txt"
    else
        echo "iasl decompile failed"
    fi
else
    echo "acpidump/iasl missing; install with: apt install acpica-tools"
fi

hr "AWCC READ-ONLY QUERIES VIA acpi_call"
ACPI_PATH=""
if [[ -f "$OUT_DIR/dsdt.dsl" ]]; then
    # Derive the real path rather than assuming it.
    for cand in AMW3 AMWW AMW1 AMW2; do
        if grep -qE "Device *\($cand\)" "$OUT_DIR/dsdt.dsl"; then
            ACPI_PATH="\\_SB.$cand.WMAX"
            break
        fi
    done
fi
[[ -z "$ACPI_PATH" ]] && ACPI_PATH="\\_SB.AMW3.WMAX"
echo "using ACPI path: $ACPI_PATH"

if ! modprobe acpi_call 2>/dev/null; then
    echo "WARN: could not load acpi_call (install acpi-call-dkms). Skipping ACPI queries."
elif [[ ! -e /proc/acpi/call ]]; then
    echo "WARN: /proc/acpi/call missing after modprobe. Skipping ACPI queries."
else
    echo "acpi_call loaded."
    # $1=method id, $2..$5 = buffer bytes. Method 0x14 only (informational).
    aw() {
        local mid="$1" b0="$2" b1="${3:-0x00}" b2="${4:-0x00}" b3="${5:-0x00}"
        echo "$ACPI_PATH 0 $mid {$b0, $b1, $b2, $b3}" > /proc/acpi/call 2>/dev/null
        tr -d '\0' < /proc/acpi/call
    }

    echo
    echo "-- 0x14/0x02 system description (fans, sensors, profile count) --"
    printf '  %s\n' "$(aw 0x14 0x02)"

    echo
    echo "-- 0x14/0x0b CURRENT thermal profile (0xab => G-Mode active) --"
    printf '  %s\n' "$(aw 0x14 0x0b)"

    echo
    echo "-- 0x14/0x03 resource id enumeration (fan + sensor ids) --"
    for i in $(seq 0 15); do
        printf '  index %-2s -> %s\n' "$i" "$(aw 0x14 0x03 "$i")"
    done

    echo
    echo "-- per-id detail: 0x04=temp 0x05=rpm 0x08=min_rpm 0x09=max_rpm 0x0c=boost --"
    for id in 0x01 0x02 0x03 0x04 0x05 0x06 0x32 0x33; do
        printf '  id %-5s temp=%-12s rpm=%-12s min=%-10s max=%-10s boost=%s\n' \
            "$id" "$(aw 0x14 0x04 "$id")" "$(aw 0x14 0x05 "$id")" \
            "$(aw 0x14 0x08 "$id")" "$(aw 0x14 0x09 "$id")" "$(aw 0x14 0x0c "$id")"
    done

    echo
    echo "-- probing which thermal profile IDs the firmware knows (0x14/0x03 style check) --"
    # Query each candidate profile id for a max-rpm/description response. A
    # supported profile returns a sane value; unsupported returns 0 or error.
    for p in 0x96 0x97 0x98 0x99 0xa0 0xa1 0xa2 0xa3 0xa4 0xa5 0xab; do
        printf '  profile %-5s -> %s\n' "$p" "$(aw 0x14 0x03 "$p")"
    done

    echo
    echo "(acpi_call left loaded; 'sudo modprobe -r acpi_call' to unload)"
fi

hr "DELL SMBIOS / DDV EXTRAS"
echo "dell_smm attributes: $(ls /sys/class/hwmon/hwmon*/ 2>/dev/null | grep -c pwm) pwm nodes found"
ls /sys/bus/platform/devices/ 2>/dev/null | grep -iE 'dell|alienware' || echo "(no dell platform devices)"
echo
echo "-- dell-wmi-ddv battery extras --"
for f in /sys/class/power_supply/BAT0/*; do
    [[ -f "$f" ]] || continue
    case "$(basename "$f")" in
        capacity|health|cycle_count|charge_full|charge_full_design|manufacture_date|temp)
            echo "  $(basename "$f") = $(cat "$f" 2>&1)";;
    esac
done

hr "RECENT KERNEL MESSAGES (dell/alienware/acpi)"
dmesg | grep -iE 'dell|alienware|acpi_call|i8k|smm|thermal' | tail -40

hr "DONE"
echo "Full artifacts in: $OUT_DIR"
echo "  dsdt.dsl         - decompiled ACPI tables"
echo "  wmax-method.txt  - the WMAX method body (authoritative method IDs)"
