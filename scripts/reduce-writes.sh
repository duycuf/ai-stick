#!/bin/sh
# Cut the constant writing to the eMMC. On these sticks the storage is a small, old eMMC and the
# system journal alone rewrote about 2 MB of it every idle minute (2-3 GB a day).
#
#   - the journal lives in RAM (lost at reboot, still readable live with journalctl)
#   - kernel audit records are no longer copied into it (they were a third of all lines)
#   - one line per boot is kept on disk in /var/log/boots.log, so power problems still leave a trace
#   - the root filesystem flushes every 60 s instead of every 5 s and stops updating access times
#
# Run:   sudo sh reduce-writes.sh
# Undo:  sudo rm /etc/systemd/journald.conf.d/10-low-write.conf /etc/systemd/system/boot-log.service
#        then remove the "/" line this script added to /etc/fstab, and reboot
set -e
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }

written() { awk '$3=="mmcblk0"{print $10*512/1024}' /proc/diskstats; }
measure() { a=$(written); sleep 30; b=$(written); echo "$1: $(( (${b%.*} - ${a%.*}) * 2 )) KB written per minute"; }
measure "before"

install -d /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/10-low-write.conf <<'EOF'
# Keep the journal in RAM: the eMMC is small and old, and the journal was its busiest writer.
[Journal]
Storage=volatile
RuntimeMaxUse=24M
Audit=no
EOF
systemctl restart systemd-journald
# The old on-disk journal is not needed any more; keep the last day in case something just happened.
journalctl --vacuum-time=1d >/dev/null 2>&1 || true

cat > /etc/systemd/system/boot-log.service <<'EOF'
[Unit]
Description=Record each boot in /var/log/boots.log (one line, survives the RAM-only journal)
After=time-sync.target
Wants=time-sync.target

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'v=$(cat /sys/bus/iio/devices/iio:device0/in_voltage0_input 2>/dev/null); echo "$(date "+%%F %%T %%z") boot usb_uV=$v kernel=$(uname -r)" >> /var/log/boots.log; tail -n 500 /var/log/boots.log > /var/log/boots.log.tmp && mv /var/log/boots.log.tmp /var/log/boots.log'

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable boot-log.service >/dev/null 2>&1
systemctl start boot-log.service

# Root filesystem: flush less often, do not record access times. The kernel mounts / from its
# command line, so add an fstab line for systemd to re-apply the options at every boot.
ROOT=$(sed -n 's/.*root=\(PARTUUID=[^ ]*\).*/\1/p' /proc/cmdline)
if [ -n "$ROOT" ] && ! grep -q '[[:space:]]/[[:space:]]' /etc/fstab 2>/dev/null; then
    printf '%s\t/\text4\tdefaults,noatime,commit=60\t0 1\n' "$ROOT" >> /etc/fstab
fi
mount -o remount,noatime,commit=60 /

measure "after"
echo "journal: $(journalctl --disk-usage 2>/dev/null | tail -1)"
echo "root mount: $(grep ' / ' /proc/mounts | cut -d' ' -f4)"
tail -n 2 /var/log/boots.log
