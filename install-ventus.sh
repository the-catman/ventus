#!/usr/bin/env bash
# Install / update / remove Ventus (fan curve + CPU power limit for the HP OMEN 16, board 8CC0).
#
#   Install or update:  sudo bash install-ventus.sh
#   Uninstall:          sudo bash install-ventus.sh --uninstall
#
# Installs: /opt/ventus (program), /usr/local/bin/ventus (GUI launcher), ventusd.service (root
# service), /etc/ventus/config.json (settings), app menu entry + icon.
# Needs the patched hp-wmi driver (install-hpwmi-dkms.sh) for fan control.

set -e
[ "$(id -u)" = 0 ] || { echo "Please run with: sudo bash $0"; exit 1; }
SRC=$(cd "$(dirname "$0")" && pwd)
APP=io.ventus.Ventus
SHARE=/usr/local/share

if [ "$1" = "--uninstall" ]; then
  # stopping the service puts the fans back to HP auto and restores HP's power limit
  systemctl disable --now ventusd.service 2>/dev/null || true
  rm -f /etc/systemd/system/ventusd.service
  systemctl daemon-reload
  rm -rf /opt/ventus /etc/ventus /usr/local/bin/ventus /usr/local/bin/ventus-bench
  rm -f $SHARE/applications/$APP.desktop $SHARE/icons/hicolor/scalable/apps/$APP.svg
  gtk-update-icon-cache -q -t -f $SHARE/icons/hicolor 2>/dev/null || true
  update-desktop-database -q $SHARE/applications 2>/dev/null || true
  echo "Ventus removed. Fans are on HP auto."
  exit 0
fi

[ "$(cat /sys/class/dmi/id/board_name 2>/dev/null)" = 8CC0 ] || \
  echo "Warning: this isn't board 8CC0 - Ventus was only tested on the HP OMEN 16-ae0xxx."
ls /sys/devices/platform/hp-wmi/hwmon/hwmon*/pwm1_enable >/dev/null 2>&1 || \
  echo "Warning: no hp-wmi fan control found - install the patched driver (install-hpwmi-dkms.sh)."

# program
install -d -m 755 /opt/ventus
install -m 644 "$SRC/common.py" "$SRC/ventusd.py" "$SRC/ventus_gui.py" "$SRC/bench.py" /opt/ventus/
rm -rf /opt/ventus/__pycache__
cat > /usr/local/bin/ventus <<'EOF'
#!/bin/sh
exec /usr/bin/python3 -sE /opt/ventus/ventus_gui.py "$@"
EOF
chmod 755 /usr/local/bin/ventus
cat > /usr/local/bin/ventus-bench <<'EOF'
#!/bin/sh
exec /usr/bin/python3 -sE /opt/ventus/bench.py "$@"
EOF
chmod 755 /usr/local/bin/ventus-bench

# settings: allow the user who ran sudo to change them from the GUI
install -d -m 755 /etc/ventus
/usr/bin/python3 -sE - "${SUDO_UID:-}" <<'EOF'
import json, sys
path = "/etc/ventus/config.json"
try:
    cfg = json.load(open(path))
except (OSError, ValueError):
    cfg = {}
uids = [u for u in cfg.get("allowed_uids", []) if isinstance(u, int)]
if sys.argv[1].isdigit() and int(sys.argv[1]) not in uids:
    uids.append(int(sys.argv[1]))
cfg["allowed_uids"] = uids
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
print(f"  allowed to change settings: root + uids {uids}")
EOF
chmod 644 /etc/ventus/config.json

# service
install -m 644 "$SRC/data/ventusd.service" /etc/systemd/system/ventusd.service
systemctl daemon-reload
systemctl enable ventusd.service >/dev/null 2>&1
systemctl restart ventusd.service

# app menu entry + icon
install -D -m 644 "$SRC/data/$APP.desktop" $SHARE/applications/$APP.desktop
install -D -m 644 "$SRC/data/$APP.svg" $SHARE/icons/hicolor/scalable/apps/$APP.svg
gtk-update-icon-cache -q -t -f $SHARE/icons/hicolor 2>/dev/null || true
update-desktop-database -q $SHARE/applications 2>/dev/null || true

sleep 3
echo
echo "== Ventus installed =="
systemctl --no-pager --lines=0 status ventusd.service | sed -n '1,3p'
journalctl -u ventusd -b --no-pager -o cat | tail -4 | sed 's/^/  /'
echo
echo "Open it from the app menu (Ventus), or run: ventus      Benchmark: ventus-bench [label]"
echo "Uninstall: sudo bash $0 --uninstall"
