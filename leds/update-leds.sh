#!/bin/sh
# Update only the LED program. Run: sudo sh ~/leds-update/update-leds.sh
set -e
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
install -m 0644 "$(dirname "$0")/stick-leds.py" /usr/local/lib/stick-leds/stick-leds.py
systemctl restart stick-leds
sleep 14
echo "stick-leds: $(systemctl is-active stick-leds)"
for l in /sys/class/leds/red:power /sys/class/leds/green:wlan /sys/class/leds/blue:wan; do
    echo "$(basename $l): trigger=$(grep -o '\[[a-z0-9_-]*\]' $l/trigger) brightness=$(cat $l/brightness)"
done
echo "All good = blue steady, green off (flickers while the assistant answers), red off."
