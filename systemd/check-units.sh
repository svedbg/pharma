#!/usr/bin/env bash
# Do the installed systemd user units still match this checkout?
#
#   systemd/check-units.sh
#
# The units are *copied* into ~/.config/systemd/user/, so editing one here
# changes nothing until it is copied again and daemon-reload has run. A unit a
# release behind then fails in ways that look like the code rather than the
# install -- the launchd side has the same hazard and the same --check.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

# On a machine with no systemd there are no units to be stale, so "not
# installed" is the correct state rather than a finding -- the mirror of what
# launchd/install-launchd.sh --check does about Darwin.
command -v systemctl >/dev/null 2>&1 && MISSING_IS_DRIFT=1 || MISSING_IS_DRIFT=0

DRIFT=0
for src in "$ROOT"/pharma-*.{service,timer}; do
    [[ -e "$src" ]] || continue
    name="$(basename "$src")"
    if [[ ! -f "$DEST/$name" ]]; then
        echo "not installed: $name"
        DRIFT=$(( DRIFT | MISSING_IS_DRIFT ))
    elif diff -q "$src" "$DEST/$name" >/dev/null 2>&1; then
        echo "current:       $name"
    else
        echo "STALE:         $name -- installed unit differs from this checkout"
        diff -u "$DEST/$name" "$src" | sed -n '3,40p'
        DRIFT=1
    fi
done

if [[ $DRIFT -ne 0 ]]; then
    cat >&2 <<EOF

to bring them up to date:
  cp $ROOT/pharma-*.{service,timer} $DEST/
  systemctl --user daemon-reload
EOF
fi
exit $DRIFT
