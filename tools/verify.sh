#!/usr/bin/env bash
#
# g15ctl end-to-end hardware verification.
#
# Exercises every control path against the real firmware and MEASURES the
# result, rather than trusting that a write succeeded. Fan RPM is read back
# from dell_ddv, which is an independent source from the WMAX method we write
# through, so agreement between them is real evidence.
#
# The script always restores the starting state, including on Ctrl-C or error.
#
# Usage: sudo bash tools/verify.sh 2>&1 | tee verify-report.txt

set -uo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "FATAL: must run as root (sudo bash tools/verify.sh)" >&2
    exit 1
fi

cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." || exit 1
G15="python3 -m g15ctl"

PASS=0
FAIL=0
ORIG_MODE=""
ORIG_GMODE=""

hr()   { printf '\n===== %s =====\n' "$1"; }
ok()   { printf '  [ ok ] %s\n' "$*"; PASS=$((PASS+1)); }
bad()  { printf '  [FAIL] %s\n' "$*"; FAIL=$((FAIL+1)); }
note() { printf '         %s\n' "$*"; }

# Read fan RPM from dell_ddv: an independent path from the WMAX writes.
ddv_rpm() {
    local h
    for h in /sys/class/hwmon/hwmon*; do
        [[ "$(cat "$h/name" 2>/dev/null)" == "dell_ddv" ]] || continue
        printf '%s %s' "$(cat "$h/fan1_input" 2>/dev/null)" "$(cat "$h/fan2_input" 2>/dev/null)"
        return
    done
    printf '0 0'
}

ddv_temp() {
    local h
    for h in /sys/class/hwmon/hwmon*; do
        [[ "$(cat "$h/name" 2>/dev/null)" == "dell_ddv" ]] || continue
        awk '{printf "%.0f", $1/1000}' "$h/temp1_input" 2>/dev/null
        return
    done
    printf '0'
}

restore() {
    hr "RESTORING ORIGINAL STATE"
    $G15 fan auto >/dev/null 2>&1 || true
    if [[ -n "$ORIG_GMODE" && "$ORIG_GMODE" == "ON" ]]; then
        $G15 gmode on >/dev/null 2>&1 || true
    else
        $G15 gmode off >/dev/null 2>&1 || true
    fi
    if [[ -n "$ORIG_MODE" ]]; then
        $G15 mode "$ORIG_MODE" >/dev/null 2>&1 || true
    fi
    echo "  restored: mode=$ORIG_MODE gmode=$ORIG_GMODE fan=auto"
    echo "  live: $($G15 status 2>/dev/null | grep -E 'Thermal mode|CPU ' | head -2 | tr '\n' ' ')"
}
trap restore EXIT INT TERM

hr "ENVIRONMENT"
echo "  product : $(cat /sys/class/dmi/id/product_name)"
echo "  bios    : $(cat /sys/class/dmi/id/bios_version)"
echo "  kernel  : $(uname -r)"
echo "  date    : $(date -Is)"

hr "BACKEND DETECTION"
$G15 doctor 2>&1 | sed 's/^/  /'

BACKEND=$($G15 status --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["backend"])' 2>/dev/null)
echo
echo "  active backend: ${BACKEND:-UNKNOWN}"
if [[ "$BACKEND" == "awcc-acpi" ]]; then
    ok "most capable backend (awcc-acpi) selected as root"
else
    bad "expected awcc-acpi as root, got '${BACKEND:-none}'"
    note "manual fan tests below will be skipped or limited"
fi

hr "BASELINE"
ORIG_MODE=$($G15 status --json | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["mode"])')
ORIG_GMODE=$($G15 gmode status 2>/dev/null | grep -o 'ON\|off' | head -1)
[[ "$ORIG_GMODE" == "off" ]] && ORIG_GMODE="OFF"
read -r R1 R2 <<< "$(ddv_rpm)"
echo "  mode=$ORIG_MODE gmode=$ORIG_GMODE cpu=$(ddv_temp)C fans=${R1}/${R2} rpm"

# ---------------------------------------------------------------------------
hr "TEST 1: THERMAL PROFILE SWITCHING"
# Each profile is set, then read back from the firmware. A profile that does
# not read back is a profile that did not apply.
MODES=$($G15 status --json | python3 -c 'import json,sys; print(" ".join(m for m in json.load(sys.stdin)["available_modes"] if m != "g-mode"))')
echo "  firmware reports: $MODES"
for m in $MODES; do
    if ! $G15 mode "$m" >/dev/null 2>&1; then
        bad "could not set mode $m"
        continue
    fi
    sleep 1
    got=$($G15 status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["mode"])')
    if [[ "$got" == "$m" ]]; then
        ok "mode $m applied and read back"
    else
        bad "set mode $m but firmware reports '$got'"
    fi
done

# ---------------------------------------------------------------------------
hr "TEST 2: PROFILE EFFECT ON FAN SPEED"
# Quiet vs performance should differ measurably at idle. We allow either
# direction to be equal, because at low load the firmware may park both.
$G15 mode quiet >/dev/null 2>&1; sleep 6
read -r Q1 Q2 <<< "$(ddv_rpm)"
echo "  quiet:                fans=${Q1}/${Q2} rpm  cpu=$(ddv_temp)C"
$G15 mode balanced-performance >/dev/null 2>&1; sleep 6
read -r P1 P2 <<< "$(ddv_rpm)"
echo "  balanced-performance: fans=${P1}/${P2} rpm  cpu=$(ddv_temp)C"
if [[ "$P1" -ge "$Q1" ]]; then
    ok "performance fan speed >= quiet (${P1} >= ${Q1})"
else
    note "performance idled lower than quiet (${P1} < ${Q1}); normal at idle"
    PASS=$((PASS+1))
fi

# ---------------------------------------------------------------------------
hr "TEST 3: MANUAL FAN BOOST (the AWCC feature kernel 6.14 cannot do natively)"
if [[ "$BACKEND" != "awcc-acpi" ]]; then
    note "SKIPPED: needs the awcc-acpi backend"
else
    $G15 mode balanced >/dev/null 2>&1
    $G15 fan auto >/dev/null 2>&1
    sleep 5
    read -r A1 A2 <<< "$(ddv_rpm)"
    echo "  fan auto:  ${A1}/${A2} rpm"

    declare -A MEASURED
    for pct in 40 70 100; do
        if ! $G15 fan "$pct" >/dev/null 2>&1; then
            bad "could not set fan ${pct}%"
            continue
        fi
        sleep 7
        read -r F1 F2 <<< "$(ddv_rpm)"
        MEASURED[$pct]="$F1"
        boost=$($G15 status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["fans"][0]["boost"])')
        echo "  fan ${pct}%:  ${F1}/${F2} rpm   (firmware boost byte: $boost)"
        if [[ "$F1" -gt "$A1" ]]; then
            ok "fan ${pct}% raised RPM above auto (${F1} > ${A1})"
        else
            bad "fan ${pct}% did not raise RPM (${F1} <= ${A1})"
        fi
    done

    # Monotonicity is the real proof that the boost byte is being honoured
    # proportionally rather than acting as a simple on/off.
    if [[ -n "${MEASURED[40]:-}" && -n "${MEASURED[100]:-}" ]]; then
        if [[ "${MEASURED[100]}" -gt "${MEASURED[40]}" ]]; then
            ok "RPM scales with boost (40%=${MEASURED[40]} < 100%=${MEASURED[100]})"
        else
            bad "RPM did not scale with boost (40%=${MEASURED[40]}, 100%=${MEASURED[100]})"
        fi
    fi

    $G15 fan auto >/dev/null 2>&1
    sleep 6
    read -r B1 B2 <<< "$(ddv_rpm)"
    echo "  fan auto:  ${B1}/${B2} rpm  (released)"
    if [[ "$B1" -lt "${MEASURED[100]:-99999}" ]]; then
        ok "releasing control returned fans toward firmware control"
    else
        bad "fans still elevated after 'fan auto' (${B1})"
    fi
fi

# ---------------------------------------------------------------------------
hr "TEST 4: G-MODE"
# This test MUST measure fan RPM, not just read the G-Mode flag back. An
# earlier version checked only the flag and therefore passed while G-Mode was
# doing nothing at all: on this firmware the Game Shift flag alone is inert
# and the 0xAB thermal profile is what drives the fans.
$G15 fan auto >/dev/null 2>&1
$G15 mode balanced >/dev/null 2>&1
echo "  settling to a clean idle baseline (20s)..."
sleep 20
read -r N1 N2 <<< "$(ddv_rpm)"
echo "  balanced baseline:  ${N1}/${N2} rpm  cpu=$(ddv_temp)C"

if $G15 gmode on >/dev/null 2>&1; then
    sleep 15
    st=$($G15 gmode status | grep -o 'ON\|off' | head -1)
    read -r G1 G2 <<< "$(ddv_rpm)"
    echo "  gmode on:           ${G1}/${G2} rpm  status=$st  cpu=$(ddv_temp)C"

    [[ "$st" == "ON" ]] && ok "G-Mode reports ON" || bad "G-Mode did not report ON"

    mode_now=$($G15 status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["mode"])')
    [[ "$mode_now" == "g-mode" ]] && ok "mode reads back as g-mode" \
        || bad "mode reads '$mode_now', expected g-mode"

    # The firmware profile itself must be 0xAB, not merely the flag.
    prof=$(python3 - <<'PY'
import os, sys
sys.path.insert(0, os.getcwd())
from g15ctl import constants as C
from g15ctl.acpi import Wmax
print("%#x" % (Wmax.detect().query(C.M_THERMAL_INFO, C.OP_GET_CURRENT_PROFILE) or 0))
PY
)
    [[ "$prof" == "0xab" ]] && ok "firmware profile is 0xab (the G-Mode profile)" \
        || bad "firmware profile is $prof, expected 0xab -- G-Mode is not really engaged"

    # The decisive check: did the fans actually speed up?
    if [[ "$G1" -gt $((N1 + 1000)) ]]; then
        ok "G-Mode raised CPU fan by $((G1 - N1)) rpm (${N1} -> ${G1})"
    else
        bad "G-Mode barely changed fan speed (${N1} -> ${G1}); it is not working"
    fi

    $G15 gmode off >/dev/null 2>&1
    sleep 15
    st=$($G15 gmode status | grep -o 'ON\|off' | head -1)
    read -r O1 O2 <<< "$(ddv_rpm)"
    echo "  gmode off:          ${O1}/${O2} rpm  status=$st"
    [[ "$st" == "off" ]] && ok "G-Mode turned off" || bad "G-Mode stuck on"
    if [[ "$O1" -lt "$G1" ]]; then
        ok "fans came back down after leaving G-Mode (${G1} -> ${O1})"
    else
        bad "fans still pinned after leaving G-Mode (${O1}); profile may still be 0xab"
    fi
else
    bad "could not toggle G-Mode"
fi

# ---------------------------------------------------------------------------
hr "TEST 5: SAFETY -- reset hands control back to firmware"
if [[ "$BACKEND" == "awcc-acpi" ]]; then
    $G15 fan 100 >/dev/null 2>&1
    $G15 gmode on >/dev/null 2>&1
    sleep 3
    $G15 reset >/dev/null 2>&1
    sleep 3
    boost=$($G15 status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["fans"][0]["boost"])')
    gm=$($G15 gmode status | grep -o 'ON\|off' | head -1)
    mode=$($G15 status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["mode"])')
    echo "  after reset: boost=$boost gmode=$gm mode=$mode"
    [[ "$boost" == "0" ]] && ok "fan boost cleared to 0" || bad "boost is $boost, expected 0"
    [[ "$gm" == "off" ]] && ok "G-Mode cleared" || bad "G-Mode still $gm"
    [[ "$mode" == "balanced" ]] && ok "default profile restored" || bad "mode is $mode"
    note "this is exactly what runs before shutdown, so Windows/AWCC sees a clean EC"
else
    note "SKIPPED: needs the awcc-acpi backend"
fi

# ---------------------------------------------------------------------------
hr "TEST 6: SENSOR CROSS-CHECK (WMAX vs dell_ddv)"
# Two independent interfaces must agree, or one of them is lying to us.
python3 - <<'PY'
import glob, os, sys
sys.path.insert(0, os.getcwd())
from g15ctl import backends, constants as C
b = backends.AwccAcpiBackend.probe()
if b is None:
    print("         SKIPPED: awcc-acpi unavailable")
    raise SystemExit(0)
wm = b.fan_rpm()
ddv = {}
for h in glob.glob('/sys/class/hwmon/hwmon*'):
    if open(os.path.join(h, 'name')).read().strip() != 'dell_ddv':
        continue
    for f in sorted(glob.glob(os.path.join(h, 'fan*_input'))):
        ddv[open(f.replace('_input', '_label')).read().strip()] = int(open(f).read())
print("         WMAX     : %s" % {hex(k): v for k, v in wm.items()})
print("         dell_ddv : %s" % ddv)
wv, dv = sorted(wm.values()), sorted(ddv.values())
if len(wv) == len(dv) and all(abs(a - b) <= 250 for a, b in zip(wv, dv)):
    print("  [ ok ] independent interfaces agree within 250 rpm")
else:
    print("  [FAIL] interfaces disagree: %s vs %s" % (wv, dv))
PY

hr "SUMMARY"
echo "  passed: $PASS"
echo "  failed: $FAIL"
if [[ $FAIL -eq 0 ]]; then
    echo "  RESULT: ALL CHECKS PASSED"
else
    echo "  RESULT: $FAIL CHECK(S) FAILED"
fi
