#!/bin/sh
# Install the status LEDs and the router that reports "busy". Run: sudo sh ~/leds-update/install-leds.sh
set -e
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
D=$(cd "$(dirname "$0")" && pwd)
echo "== router 0.2.0 (reports requests in flight)"
install -m 0755 "$D/stick-router" /usr/local/bin/stick-router.new
mv /usr/local/bin/stick-router.new /usr/local/bin/stick-router
systemctl restart stick-router
echo "== admin page lets go of the LEDs"
[ -f /etc/stick-leds.json ] || echo '{}' > /etc/stick-leds.json
install -m 0644 "$D/systools.py" /opt/openstick-admin/systools.py
systemctl restart openstick-admin
echo "== status LED service"
install -d /usr/local/lib/stick-leds
install -m 0644 "$D/stick-leds.py" /usr/local/lib/stick-leds/stick-leds.py
install -m 0644 "$D/stick-leds.service" /etc/systemd/system/stick-leds.service
systemctl daemon-reload
systemctl enable --now stick-leds >/dev/null 2>&1
systemctl restart stick-leds
sleep 14
echo "stick-router: $(systemctl is-active stick-router) $(stick-router -version)"
echo "stick-leds:   $(systemctl is-active stick-leds)"
curl -s -m 5 http://127.0.0.1:20128/health
for l in /sys/class/leds/red:power /sys/class/leds/green:wlan /sys/class/leds/blue:wan; do
    echo "$(basename $l): trigger=$(grep -o '\[[a-z0-9_-]*\]' $l/trigger) brightness=$(cat $l/brightness)"
done
echo "Expected when all is well: green on, red off."
