#!/usr/bin/env bash
#
# g15ctl installer -- fan and thermal control for Dell G-series laptops.
#
#   curl -fsSL https://example.com/install.sh | sudo bash
#
# Also works from a source checkout:
#
#   sudo ./install.sh
#
# The script is idempotent: running it again upgrades in place. `--uninstall`
# removes everything and restores firmware control.

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# A release *asset* URL (a direct file), not a repository page. Override at run
# time with G15CTL_TARBALL_URL=... if you need to test a different build.
TARBALL_URL="${G15CTL_TARBALL_URL:-https://github.com/AadithyaAle/g15ctl/releases/latest/download/g15ctl.tar.gz}"

VERSION="1.0.0"
PREFIX="/usr"
LIBDIR="$PREFIX/lib/g15ctl"
BINDIR="$PREFIX/bin"
MANDIR="$PREFIX/share/man/man1"
DESKTOPDIR="$PREFIX/share/applications"
SYSTEMDDIR="/etc/systemd/system"
CONFDIR="/etc/g15ctl"

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; BOLD=$'\033[1m'; OFF=$'\033[0m'
if [[ ! -t 1 ]]; then RED=""; GREEN=""; YELLOW=""; BOLD=""; OFF=""; fi

info() { printf '%s==>%s %s\n' "$GREEN" "$OFF" "$*"; }
warn() { printf '%swarn:%s %s\n' "$YELLOW" "$OFF" "$*" >&2; }
die()  { printf '%serror:%s %s\n' "$RED" "$OFF" "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

[[ $EUID -eq 0 ]] || die "must run as root. Use: curl -fsSL <url> | sudo bash"

check_hardware() {
    local vendor product
    vendor=$(cat /sys/class/dmi/id/sys_vendor 2>/dev/null || echo unknown)
    product=$(cat /sys/class/dmi/id/product_name 2>/dev/null || echo unknown)
    info "Detected: $vendor $product"

    if [[ "$vendor" != *Dell* ]]; then
        warn "This is not a Dell system. g15ctl will almost certainly not work."
        [[ "${G15CTL_FORCE:-0}" == "1" ]] || die "refusing to install (set G15CTL_FORCE=1 to override)"
    fi

    # The AWCC WMI GUID is the real capability test: it proves the firmware
    # implements the interface, regardless of model name.
    if [[ ! -e /sys/bus/wmi/devices/A70591CE-A997-11DA-B012-B622A1EF5492 ]]; then
        warn "AWCC WMI device (A70591CE-...) not found."
        warn "Thermal profiles may still work through the generic dell-pc driver,"
        warn "but G-Mode and manual fan control will not be available."
        [[ "${G15CTL_FORCE:-0}" == "1" ]] || die "refusing to install (set G15CTL_FORCE=1 to override)"
    else
        info "AWCC firmware interface present."
    fi
}

install_deps() {
    local missing=()
    command -v python3 >/dev/null || missing+=(python3)
    # acpi_call gives us per-fan boost; without it we can still do profiles.
    if ! modinfo acpi_call >/dev/null 2>&1; then missing+=(acpi-call-dkms); fi
    # dell_wmi_ddv provides unprivileged fan RPM + temperatures.
    if ! modinfo dell_wmi_ddv >/dev/null 2>&1; then
        warn "dell_wmi_ddv not available in this kernel; sensor detail will be reduced"
    fi

    if [[ ${#missing[@]} -eq 0 ]]; then
        info "Dependencies already satisfied."
        return
    fi

    info "Installing: ${missing[*]}"
    if command -v apt-get >/dev/null; then
        DEBIAN_FRONTEND=noninteractive apt-get update -qq || warn "apt update failed; continuing"
        DEBIAN_FRONTEND=noninteractive apt-get install -y "${missing[@]}" \
            || warn "could not install ${missing[*]}; some features will be unavailable"
    elif command -v dnf >/dev/null; then
        dnf install -y python3 acpi_call-kmod || warn "could not install dependencies"
    elif command -v pacman >/dev/null; then
        pacman -Sy --noconfirm python acpi_call || warn "could not install dependencies"
    else
        warn "unknown package manager; install python3 and acpi_call yourself"
    fi
}

# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

SRC=""
TMPDIR_CREATED=""

#: Path to this script, or empty when we were piped in from curl.
SELF=""
if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
    SELF="$(readlink -f "${BASH_SOURCE[0]}")"
fi

locate_source() {
    # When piped from curl there is no script on disk, so BASH_SOURCE is not a
    # usable path and we must not infer a checkout from it.
    if [[ -n "$SELF" ]]; then
        local here
        here="$(cd "$(dirname "$SELF")" && pwd)"
        if [[ -d "$here/g15ctl" && -f "$here/g15ctl/cli.py" ]]; then
            SRC="$here"
            info "Installing from local checkout: $SRC"
            return
        fi
    fi

    case "$TARBALL_URL" in
        *CHANGEME*)
            die "This installer has no download URL configured yet.
Either run it from a source checkout, or edit TARBALL_URL at the top of
install.sh to point at your release asset."
            ;;
    esac

    command -v curl >/dev/null || command -v wget >/dev/null \
        || die "need curl or wget to download g15ctl"
    TMPDIR_CREATED=$(mktemp -d)
    SRC="$TMPDIR_CREATED/src"
    mkdir -p "$SRC"
    info "Downloading $TARBALL_URL"
    if command -v curl >/dev/null; then
        curl -fsSL "$TARBALL_URL" -o "$TMPDIR_CREATED/g15ctl.tar.gz" \
            || die "download failed"
    else
        wget -qO "$TMPDIR_CREATED/g15ctl.tar.gz" "$TARBALL_URL" \
            || die "download failed"
    fi
    tar -xzf "$TMPDIR_CREATED/g15ctl.tar.gz" -C "$SRC" --strip-components=1 \
        || die "could not extract archive"
    [[ -f "$SRC/g15ctl/cli.py" ]] || die "archive does not look like g15ctl"
}

cleanup() { [[ -n "$TMPDIR_CREATED" ]] && rm -rf "$TMPDIR_CREATED"; }
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------

install_files() {
    info "Installing Python package to $LIBDIR"
    rm -rf "$LIBDIR/g15ctl"
    install -d "$LIBDIR"
    cp -r "$SRC/g15ctl" "$LIBDIR/g15ctl"
    # Strip caches so we never ship stale bytecode.
    find "$LIBDIR/g15ctl" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
    chmod -R a+rX "$LIBDIR"

    info "Installing launcher to $BINDIR/g15ctl"
    install -d "$BINDIR"
    cat > "$BINDIR/g15ctl" <<EOF
#!/bin/sh
# g15ctl $VERSION launcher
exec /usr/bin/env PYTHONPATH="$LIBDIR\${PYTHONPATH:+:\$PYTHONPATH}" \\
    python3 -m g15ctl "\$@"
EOF
    chmod 0755 "$BINDIR/g15ctl"

    if [[ -f "$SRC/gui/g15ctl-gui.py" ]]; then
        info "Installing tray applet"
        install -m 0755 "$SRC/gui/g15ctl-gui.py" "$LIBDIR/g15ctl-gui.py"
        cat > "$BINDIR/g15ctl-gui" <<EOF
#!/bin/sh
# g15ctl $VERSION tray applet launcher
exec /usr/bin/env PYTHONPATH="$LIBDIR\${PYTHONPATH:+:\$PYTHONPATH}" \\
    python3 "$LIBDIR/g15ctl-gui.py" "\$@"
EOF
        chmod 0755 "$BINDIR/g15ctl-gui"
        install -d "$DESKTOPDIR"
        install -m 0644 "$SRC/packaging/g15ctl-gui.desktop" "$DESKTOPDIR/" 2>/dev/null || true
    fi

    if [[ -f "$SRC/packaging/g15ctl.1" ]]; then
        install -d "$MANDIR"
        install -m 0644 "$SRC/packaging/g15ctl.1" "$MANDIR/g15ctl.1"
        command -v mandb >/dev/null && mandb -q 2>/dev/null || true
    fi

    # Keep a copy of this installer so `g15ctl-uninstall` works even when the
    # original checkout or download is long gone. When we were piped from curl
    # there is no script file on disk, so take the copy from the payload.
    if [[ -f "$SRC/install.sh" ]]; then
        install -m 0755 "$SRC/install.sh" "$LIBDIR/install.sh"
    elif [[ -n "$SELF" && -f "$SELF" ]]; then
        install -m 0755 "$SELF" "$LIBDIR/install.sh"
    else
        warn "could not save a copy of the installer; g15ctl-uninstall unavailable"
        return
    fi
    cat > "$BINDIR/g15ctl-uninstall" <<EOF
#!/bin/sh
# Removes g15ctl and restores firmware thermal control.
exec "$LIBDIR/install.sh" --uninstall "\$@"
EOF
    chmod 0755 "$BINDIR/g15ctl-uninstall"

    install -d "$CONFDIR" /var/lib/g15ctl /var/log/g15ctl
}

configure_modules() {
    info "Configuring kernel modules"
    install -m 0644 "$SRC/packaging/modules-load-g15ctl.conf" /etc/modules-load.d/g15ctl.conf

    # modprobe refuses to load a module if given an unknown parameter, so the
    # options line must match what this kernel's driver actually supports.
    local params="force_platform_profile=1 force_gmode=1"
    if modinfo alienware_wmi 2>/dev/null | grep -q 'parm:.*force_hwmon'; then
        params="$params force_hwmon=1"
        info "Kernel supports force_hwmon: native manual fan control available."
    else
        info "Kernel lacks force_hwmon (needs 6.15+): fan speed will use acpi_call."
    fi

    if modinfo alienware_wmi >/dev/null 2>&1; then
        {
            sed '/^options /d' "$SRC/packaging/modprobe-g15ctl.conf"
            echo "options alienware_wmi $params"
        } > /etc/modprobe.d/g15ctl.conf
        chmod 0644 /etc/modprobe.d/g15ctl.conf

        # Apply now so the user does not have to reboot.
        modprobe -r alienware_wmi 2>/dev/null || true
        if modprobe alienware_wmi $params 2>/dev/null; then
            info "Loaded alienware_wmi with $params"
        else
            warn "could not load alienware_wmi with $params; profiles may be limited"
            modprobe alienware_wmi 2>/dev/null || true
        fi
    else
        warn "alienware_wmi not present in this kernel; skipping native backend"
    fi

    modprobe acpi_call 2>/dev/null \
        && info "Loaded acpi_call (manual fan control enabled)" \
        || warn "could not load acpi_call; manual fan control will be unavailable"
    modprobe dell_wmi_ddv 2>/dev/null || true
}

install_services() {
    info "Installing systemd units"
    for unit in g15ctl.service g15ctl-reset.service g15ctl-resume.service; do
        install -m 0644 "$SRC/packaging/$unit" "$SYSTEMDDIR/$unit"
    done
    systemctl daemon-reload

    # g15ctl-reset guarantees a clean EC for a reboot into Windows, and
    # g15ctl-resume reapplies the mode after suspend. Both are cheap oneshots.
    systemctl enable g15ctl-reset.service >/dev/null 2>&1 || warn "could not enable g15ctl-reset"
    systemctl enable g15ctl-resume.service >/dev/null 2>&1 || warn "could not enable g15ctl-resume"
    systemctl start g15ctl-reset.service >/dev/null 2>&1 || true

    # The main daemon is only needed for the fan curve and the watchdog, so
    # enable it but leave the curve itself off by default.
    systemctl enable g15ctl.service >/dev/null 2>&1 || warn "could not enable g15ctl.service"
    systemctl restart g15ctl.service >/dev/null 2>&1 || warn "could not start g15ctl.service"
}

# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------

do_uninstall() {
    info "Restoring firmware thermal control"
    "$BINDIR/g15ctl" reset 2>/dev/null || true

    info "Stopping and removing services"
    for unit in g15ctl.service g15ctl-reset.service g15ctl-resume.service; do
        systemctl disable --now "$unit" >/dev/null 2>&1 || true
        rm -f "$SYSTEMDDIR/$unit"
    done
    systemctl daemon-reload 2>/dev/null || true

    info "Removing files"
    rm -rf "$LIBDIR"
    rm -f "$BINDIR/g15ctl" "$BINDIR/g15ctl-gui"
    rm -f "$MANDIR/g15ctl.1" "$DESKTOPDIR/g15ctl-gui.desktop"
    rm -f /etc/modprobe.d/g15ctl.conf /etc/modules-load.d/g15ctl.conf
    rm -rf /var/lib/g15ctl

    # Leave logs and config behind; they are cheap and useful after a problem.
    info "Kept $CONFDIR and /var/log/g15ctl (delete manually if you want them gone)"
    info "Uninstalled. Your fans are back under firmware control."
    echo "A reboot fully reverts the alienware_wmi module parameters."
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if [[ "${1:-}" == "--uninstall" || "${1:-}" == "uninstall" ]]; then
    do_uninstall
    exit 0
fi

printf '%s\n' "${BOLD}g15ctl $VERSION installer${OFF}"
echo

check_hardware
locate_source
install_deps
install_files
configure_modules
install_services

echo
info "Installed."
echo
"$BINDIR/g15ctl" doctor || true
echo
cat <<'EOF'
Try it:
  g15ctl status              show temperatures, fans and mode
  g15ctl monitor             live dashboard
  sudo g15ctl mode quiet     switch thermal profile
  sudo g15ctl fan 70         pin the fans to at least 70%
  sudo g15ctl fan auto       hand the fans back to the firmware
  sudo g15ctl gmode toggle   G-Mode on/off
  g15ctl logs                service logs
  man g15ctl                 full documentation

Dual boot: fan boost and G-Mode are cleared automatically on shutdown, so
Windows and Alienware Command Center always start from the firmware default.

Uninstall:  sudo g15ctl-uninstall   (or: sudo bash install.sh --uninstall)
EOF
