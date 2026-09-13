#!/usr/bin/env bash
#
# Build the release tarball that install.sh downloads.
#
#   bash tools/make-release.sh
#
# Produces ./g15ctl.tar.gz containing exactly what the installer needs. The
# archive has a single top-level directory because install.sh extracts with
# --strip-components=1.
#
# To publish (needs the `gh` CLI, or upload both files by hand):
#
#   gh release create v1.0.0 g15ctl.tar.gz install.sh \
#       --title "g15ctl 1.0.0" --notes-file RELEASE_NOTES.md
#
# Users then install with:
#
#   curl -fsSL https://github.com/AadithyaAle/g15ctl/releases/latest/download/install.sh | sudo bash

set -euo pipefail

cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." || exit 1

VERSION=$(python3 -c 'import re;print(re.search(r"APP_VERSION = \"([^\"]+)\"", open("g15ctl/constants.py").read()).group(1))')
NAME="g15ctl-$VERSION"
OUT="g15ctl.tar.gz"

echo "==> Building $NAME"

# Refuse to ship a build that does not pass its own tests.
echo "==> Running unit tests"
python3 tests/test_g15ctl.py >/dev/null 2>&1 \
    || { echo "FATAL: unit tests fail; refusing to build a release" >&2; exit 1; }
echo "    tests pass"

STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
DEST="$STAGE/$NAME"
mkdir -p "$DEST"

# Only what the installer reads. Probe reports and git metadata are excluded.
cp -r g15ctl "$DEST/"
cp -r gui packaging "$DEST/"
cp install.sh README.md LICENSE log.md "$DEST/"
mkdir -p "$DEST/tools" "$DEST/tests"
cp tools/*.sh tools/*.py "$DEST/tools/"
cp tests/*.py "$DEST/tests/"

find "$DEST" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
find "$DEST" -name '*.pyc' -delete 2>/dev/null || true

# Deterministic archive: sorted, fixed owner, fixed mtime, so rebuilding the
# same source produces the same bytes.
tar --sort=name --owner=0 --group=0 --numeric-owner \
    --mtime="@$(git log -1 --format=%ct 2>/dev/null || echo 0)" \
    -czf "$OUT" -C "$STAGE" "$NAME"

echo "==> Wrote $OUT ($(du -h "$OUT" | cut -f1)), sha256:"
sha256sum "$OUT"
echo
echo "Verifying the archive is installable..."
VERIFY=$(mktemp -d)
tar -xzf "$OUT" -C "$VERIFY" --strip-components=1
for required in g15ctl/cli.py install.sh packaging/g15ctl.service packaging/g15ctl.1; do
    [[ -f "$VERIFY/$required" ]] || { echo "FATAL: missing $required" >&2; exit 1; }
done
rm -rf "$VERIFY"
echo "    archive layout OK"
echo
echo "Next:"
echo "  gh release create v$VERSION $OUT install.sh --title \"g15ctl $VERSION\" --generate-notes"
