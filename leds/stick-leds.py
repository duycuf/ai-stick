#!/usr/bin/env python3
"""Status LEDs for the AI Stick: three lights, three questions.

A light that sits still means "fine". Anything that blinks wants your attention
(green is the exception: it flickers briefly while a message is being answered).

  blue    Is the network up?
          steady      WiFi joined and internet reachable
          slow blink  WiFi joined but no internet
          off         no WiFi
  green   Is the assistant working on something?
          fast blink  answering a message right now (the router reports requests in flight)
          off         idle
  red     Is anything wrong?  Red is only ever about trouble.
          off         no fault
          slow blink  warning: the model server cannot be reached (the stick itself is fine)
          steady      fault on the stick: a watched service is down. Also while booting
          fast blink  urgent: too hot, or the disk is nearly full

All good = blue steady, green off, red off.

Every blink is done by this program, five steps a second, because the postmarketOS kernel on
these sticks has no "timer" LED trigger. At start every LED lights for two seconds.
Boards with fewer LEDs simply show fewer of the three answers.

The kernel names the LEDs green:wlan and blue:wan, but on some boards the colours are soldered
the other way round. /etc/stick-leds.json maps each role to a sysfs name, for example
{"green": "blue:wan", "blue": "green:wlan"} when they are swapped.

Runs as root (LEDs live in sysfs). Settings come from /etc/stick-leds.json; every key is optional.
"""
import json
import os
import socket
import subprocess
import time
import urllib.parse
import urllib.request

LEDS = "/sys/class/leds"
DEFAULTS = {
    "green": "green:wlan", "blue": "blue:wan", "red": "red:power",
    "wifi_if": "wlan0",
    "internet_probe": ["1.1.1.1:443", "8.8.8.8:53"],
    "services": ["zeroclaw", "stick-router", "tailscaled"],
    "router_health": "http://127.0.0.1:20128/health",
    "router_config": "/etc/stick-router/config.json",   # the model server address is read from here
    "model_probe": "",                                   # or set "host:port" directly
    "max_temp_c": 70,
    "min_free_mb": 300,
    "check_seconds": 10,
}
STEP = 0.2            # seconds per loop step; "fast" toggles every step, "slow" every five steps


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open("/etc/stick-leds.json", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def write(path, value):
    try:
        with open(path, "w") as f:
            f.write(str(value))
        return True
    except OSError:
        return False


class Led:
    """One LED. `pattern` is 'off', 'on', 'slow' or 'fast'; show(step) writes the level for this step."""

    def __init__(self, name):
        self.dir = f"{LEDS}/{name}"
        self.pattern = "off"
        self.level = None
        if os.path.isdir(self.dir):
            write(f"{self.dir}/trigger", "none")         # we drive brightness ourselves

    def show(self, step):
        want = {"off": 0, "on": 1, "slow": (step // 5) % 2, "fast": step % 2}[self.pattern]
        if want != self.level and os.path.isdir(self.dir):
            write(f"{self.dir}/brightness", want)
            self.level = want


def tcp_ok(hostport, timeout=3):
    host, _, port = hostport.rpartition(":")
    try:
        with socket.create_connection((host.strip("[]"), int(port)), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def wifi_up(iface):
    try:
        with open(f"/sys/class/net/{iface}/operstate") as f:
            return f.read().strip() == "up"
    except OSError:
        return False


def services_ok(names):
    if not names:
        return True
    p = subprocess.run(["systemctl", "is-active", *names], capture_output=True, text=True)
    lines = p.stdout.splitlines()
    return len(lines) == len(names) and all(line.strip() == "active" for line in lines)


def hottest_c():
    best = 0
    try:
        for zone in os.listdir("/sys/class/thermal"):
            try:
                with open(f"/sys/class/thermal/{zone}/temp") as f:
                    best = max(best, int(f.read().strip()) / 1000)
            except (OSError, ValueError):
                pass
    except OSError:
        pass
    return best


def free_mb(path="/"):
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize / 1048576


def model_address(cfg):
    if cfg["model_probe"]:
        return cfg["model_probe"]
    try:
        with open(cfg["router_config"], encoding="utf-8") as f:
            url = urllib.parse.urlparse(json.load(f)["providers"][0]["base_url"])
        return f"{url.hostname}:{url.port or (443 if url.scheme == 'https' else 80)}"
    except (OSError, ValueError, KeyError, IndexError):
        return ""


def in_flight(url):
    try:
        with urllib.request.urlopen(url, timeout=1.0) as r:
            return int(json.load(r).get("in_flight", 0))
    except Exception:                                    # router down or old version: treat as idle
        return 0


def check(cfg):
    wifi = wifi_up(cfg["wifi_if"])
    addr = model_address(cfg)
    return {
        "wifi": wifi,
        "internet": wifi and any(tcp_ok(p) for p in cfg["internet_probe"]),
        "services": services_ok(cfg["services"]),
        "model": tcp_ok(addr) if addr else True,
        "urgent": hottest_c() > cfg["max_temp_c"] or free_mb() < cfg["min_free_mb"],
    }


def main():
    cfg = load_config()
    green, blue, red = Led(cfg["green"]), Led(cfg["blue"]), Led(cfg["red"])
    for led in (green, blue, red):                       # lamp test: which LEDs does this board really have?
        led.pattern = "on"
        led.show(0)
    time.sleep(2)
    green.pattern, blue.pattern, red.pattern = "off", "off", "on"    # red stays on until the first full check
    health, busy, step = None, False, 0
    next_check = 0.0
    while True:
        if time.monotonic() >= next_check:               # the slow checks; LEDs hold their pattern meanwhile
            health = check(cfg)
            next_check = time.monotonic() + cfg["check_seconds"]
        if step % 5 == 0:                                # once a second: is a message being answered?
            busy = in_flight(cfg["router_health"]) > 0
        blue.pattern = "off" if not health["wifi"] else "slow" if not health["internet"] else "on"
        green.pattern = "fast" if busy else "off"
        red.pattern = ("fast" if health["urgent"] else "on" if not health["services"]
                       else "slow" if not health["model"] else "off")
        for led in (green, blue, red):
            led.show(step)
        step += 1
        time.sleep(STEP)


if __name__ == "__main__":
    main()
