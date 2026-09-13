#!/usr/bin/env bash
# g15ctl hardware probe, stage 2.
#
# Stage 1 proved the WMAX method is NOT in the DSDT, so it lives in an SSDT.
# This script dumps ALL ACPI tables, finds the real WMAX method, and dumps its
# body so we can read the authoritative method/profile IDs for this firmware.
#
# It then tests whether the in-tree alienware-wmi driver can bind to the AWCC
# device using its force_* module parameters.
#
# Still read-only with respect to thermal state: it evaluates no ACPI control
# methods. Loading/unloading alienware_wmi is fully reversible.
#
# Usage: sudo bash tools/probe2.sh 2>&1 | tee probe2-report.txt

set -uo pipefail

OUT_DIR="${OUT_DIR:-/tmp/g15ctl-probe2}"
TBL_DIR="$OUT_DIR/tables"

if [[ $EUID -ne 0 ]]; then
    echo "FATAL: must run as root (sudo bash tools/probe2.sh)" >&2
    exit 1
fi

rm -rf "$OUT_DIR"
mkdir -p "$TBL_DIR"

hr() { printf '\n===== %s =====\n' "$1"; }

hr "DUMP ALL ACPI TABLES"
cd "$TBL_DIR" || exit 1
acpidump -b >/dev/null 2>&1
echo "tables dumped: $(ls -1 ./*.dat 2>/dev/null | wc -l)"
ls -la ./*.dat 2>/dev/null | awk '{printf "  %-20s %s bytes\n", $9, $5}'

hr "DECOMPILE AND HUNT FOR WMAX"
found_files=()
for t in ./*.dat; do
    [[ -f "$t" ]] || continue
    # Decompile each table independently. Warnings are expected and harmless;
    # we only need readable ASL to locate the method.
    iasl -d "$t" >/dev/null 2>&1
    dsl="${t%.dat}.dsl"
    [[ -f "$dsl" ]] || continue
    if awk '/Method *\(WMAX/{found=1} END{exit !found}' "$dsl"; then
        echo "  *** WMAX method found in: $dsl"
        found_files+=("$dsl")
    fi
done

if [[ ${#found_files[@]} -eq 0 ]]; then
    echo "  WMAX not found by method name. Searching for the AWCC object id / GUID bytes..."
    # AWCC GUID A70591CE-A997-11DA-B012-B622A1EF5492 appears in _WDG as raw
    # little-endian bytes: CE 91 05 A7 97 A9 DA 11 B0 12 B6 22 A1 EF 54 92
    for dsl in ./*.dsl; do
        [[ -f "$dsl" ]] || continue
        if awk '/0xCE, 0x91, 0x05, 0xA7/{found=1} END{exit !found}' "$dsl"; then
            echo "  *** AWCC GUID bytes found in: $dsl"
            found_files+=("$dsl")
        fi
    done
fi

hr "WMAX METHOD BODY"
if [[ ${#found_files[@]} -gt 0 ]]; then
    for dsl in "${found_files[@]}"; do
        echo "--- from $dsl ---"
        awk '/Device \(AMW|Name \(_WDG|Method \(WMAX/{print NR": "$0}' "$dsl"
        echo
        # Extract the WMAX method with brace matching so we capture the whole body.
        python3 - "$dsl" <<'PY' > "$OUT_DIR/wmax-method.txt"
import re, sys
src = open(sys.argv[1], errors="replace").read()
m = re.search(r'^([ \t]*)Method \(WMAX.*$', src, re.M)
if not m:
    sys.exit("WMAX method not found in " + sys.argv[1])
start = m.start()
depth = 0
i = src.index('{', start)
j = i
while j < len(src):
    if src[j] == '{':
        depth += 1
    elif src[j] == '}':
        depth -= 1
        if depth == 0:
            break
    j += 1
print(src[start:j+1])
PY
        if [[ -s "$OUT_DIR/wmax-method.txt" ]]; then
            echo "WMAX body: $(wc -l < "$OUT_DIR/wmax-method.txt") lines -> $OUT_DIR/wmax-method.txt"
            echo
            echo "-- the method-ID switch cases (these are the authoritative IDs) --"
            awk '/Case \(|If \(\(Arg|Local0 = Arg|Method \(WMAX/{print "  "$0}' \
                "$OUT_DIR/wmax-method.txt" | head -60
            echo
            echo "-- first 60 lines --"
            head -60 "$OUT_DIR/wmax-method.txt"
        fi
        break
    done
else
    echo "  WMAX method could not be located in any ACPI table."
    echo "  The AWCC interface may be implemented purely in the EC."
fi

hr "FULL ACPI NAMESPACE PATH OF THE AWCC DEVICE"
# Definitive path: walk the live ACPI namespace instead of guessing scopes.
if [[ -d /sys/bus/acpi/devices ]]; then
    for d in /sys/bus/acpi/devices/*/; do
        p=$(cat "$d/path" 2>/dev/null)
        case "$p" in *AMW*|*AWCC*) echo "  $(basename "$d") -> $p";; esac
    done
fi
echo "-- grep decompiled tables for the enclosing Scope/Device of WMAX --"
if [[ -s "$OUT_DIR/wmax-method.txt" ]] && [[ ${#found_files[@]} -gt 0 ]]; then
    python3 - "${found_files[0]}" <<'PY'
import re, sys
lines = open(sys.argv[1], errors="replace").read().splitlines()
idx = next((i for i, l in enumerate(lines) if re.search(r'Method \(WMAX', l)), None)
if idx is None:
    sys.exit(0)
# Walk backwards collecting the enclosing Scope(...) / Device(...) declarations
# by tracking indentation, which gives us the full ACPI path.
stack, want = [], None
for i in range(idx, -1, -1):
    l = lines[i]
    ind = len(l) - len(l.lstrip())
    m = re.match(r'\s*(Scope|Device) \(([^)]+)\)', l)
    if m and (want is None or ind < want):
        stack.append(m.group(2).strip())
        want = ind
        if m.group(1) == 'Scope' and stack[-1].startswith('\\'):
            break
parts = [p for p in reversed(stack)]
path = ''
for p in parts:
    if p.startswith('\\'):
        path = p
    else:
        path = path.rstrip('.') + '.' + p
print("  ACPI PATH -> %s.WMAX" % path.replace('\\_SB_', '\\_SB').rstrip('.'))
print("  enclosing scopes:", parts)
PY
fi

hr "TEST: NATIVE alienware-wmi WITH FORCE PARAMETERS"
echo "before: platform-profile devices ="
for d in /sys/class/platform-profile/*/; do
    [[ -d "$d" ]] && echo "  $(basename "$d") name=$(cat "$d/name" 2>/dev/null) choices=[$(cat "$d/choices" 2>/dev/null)]"
done
echo "before: hwmon alienware =" \
    "$(grep -l alienware_wmi /sys/class/hwmon/hwmon*/name 2>/dev/null || echo none)"

dmesg -C 2>/dev/null || true
modprobe -r alienware_wmi 2>/dev/null && echo "unloaded alienware_wmi" || echo "could not unload alienware_wmi"

# force_hwmon only exists on kernel >= 6.15 (split alienware-wmi-wmax driver).
PARAMS="force_platform_profile=1 force_gmode=1"
if modinfo alienware_wmi 2>/dev/null | grep -q 'parm:.*force_hwmon'; then
    PARAMS="$PARAMS force_hwmon=1"
fi
echo "loading: modprobe alienware_wmi $PARAMS"
# shellcheck disable=SC2086
modprobe alienware_wmi $PARAMS && echo "  load OK" || echo "  load FAILED"
sleep 1

echo
echo "after: platform-profile devices ="
for d in /sys/class/platform-profile/*/; do
    [[ -d "$d" ]] && echo "  $(basename "$d") name=$(cat "$d/name" 2>/dev/null) choices=[$(cat "$d/choices" 2>/dev/null)] current=$(cat "$d/profile" 2>/dev/null)"
done

echo
echo "after: AWCC hwmon device ="
awcc_hwmon=$(grep -l alienware_wmi /sys/class/hwmon/hwmon*/name 2>/dev/null | head -1)
if [[ -n "$awcc_hwmon" ]]; then
    h=$(dirname "$awcc_hwmon")
    echo "  $h"
    for f in "$h"/*; do
        [[ -f "$f" ]] || continue
        case "$(basename "$f")" in
            fan*|pwm*|temp*) echo "    $(basename "$f") = $(cat "$f" 2>&1)";;
        esac
    done
else
    echo "  none (expected on kernel 6.14: no force_hwmon support)"
fi

echo
echo "after: WMAX wmi device binding ="
d=/sys/bus/wmi/devices/A70591CE-A997-11DA-B012-B622A1EF5492
if [[ -L "$d/driver" ]]; then
    echo "  BOUND to $(basename "$(readlink -f "$d/driver")")"
else
    echo "  still unbound"
fi

echo
echo "-- kernel messages from the load attempt --"
dmesg | grep -iE 'alienware|awcc|platform_profile|wmax' | tail -25 || echo "  (none)"

hr "DONE"
echo "Artifacts: $OUT_DIR"
echo "  tables/          - all ACPI tables, raw + decompiled"
echo "  wmax-method.txt  - the WMAX method body"
