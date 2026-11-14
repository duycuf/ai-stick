"""Everything the "Assistant" tab shows: power, network, services, model router, today's log, LEDs.

All of it is read-only and every block is optional: a stick without the assistant installed
just gets fewer cards. Paths and names can be changed in /etc/openstick-admin/assistant.json.
"""
import collections
import csv
import datetime as dt
import glob
import json
import os
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request

import wificlient

DEFAULTS = {
    "services": ["zeroclaw", "stick-router", "stick-leds", "qwen-tunnel", "tailscaled", "NetworkManager"],
    "router": "http://127.0.0.1:20128",
    "router_config": "/etc/stick-router/config.json",
    "coach_dir": "/var/lib/zeroclaw/coach",
    "timers_glob": "/etc/systemd/system/coach-*.timer",
    "leds_state": "/run/stick-leds.json",
    "low_voltage": 4.5,
}
IIO = "/sys/bus/iio/devices"
SAMPLE_SECONDS = 5
WINDOW_SECONDS = 3600


def _read(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return default


def _run(args, timeout=5):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _tcp_ok(host, port, timeout=2):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class Assistant:
    def __init__(self, start=True):
        self.cfg = dict(DEFAULTS)
        try:
            with open("/etc/openstick-admin/assistant.json", encoding="utf-8") as f:
                self.cfg.update(json.load(f))
        except (OSError, ValueError):
            pass
        self.adc = self._find_adc()
        self.volts = collections.deque(maxlen=WINDOW_SECONDS // SAMPLE_SECONDS)
        self._cache = {}
        if start and self.adc.get("USBIN"):
            threading.Thread(target=self._sample_loop, name="volts", daemon=True).start()

    # ---------- power ----------
    def _find_adc(self):
        """Map PMIC ADC channel labels (USBIN, VSYS, DIE_TEMP) to their sysfs input files."""
        found = {}
        for label_file in glob.glob(f"{IIO}/iio:device*/in_*_label"):
            label = _read(label_file)
            if label:
                found[label] = label_file.replace("_label", "_input")
        return found

    def _volt(self, label):
        v = _read(self.adc.get(label, ""))
        try:
            return round(int(v) / 1e6, 3)
        except (TypeError, ValueError):
            return None

    def _sample_loop(self):
        while True:
            v = self._volt("USBIN")
            if v:
                self.volts.append((time.time(), v))
            time.sleep(SAMPLE_SECONDS)

    def power(self):
        now_v = self._volt("USBIN")
        window = [v for _, v in self.volts] or ([now_v] if now_v else [])
        die = _read(self.adc.get("DIE_TEMP", ""))
        cpu = max([int(_read(z, "0") or 0) for z in glob.glob("/sys/class/thermal/thermal_zone*/temp")] or [0]) / 1000
        up = float((_read("/proc/uptime", "0") or "0").split()[0])
        return {
            "usb_v": now_v,
            "usb_v_min": min(window) if window else None,
            "usb_v_avg": round(sum(window) / len(window), 3) if window else None,
            "window_minutes": round(len(self.volts) * SAMPLE_SECONDS / 60) if self.volts else 0,
            "sys_v": self._volt("VSYS"),
            "pmic_temp_c": round(int(die) / 1000, 1) if die and die.lstrip("-").isdigit() else None,
            "cpu_temp_c": round(cpu, 1) if cpu else None,
            "uptime_s": int(up),
            "low_voltage": bool(window and min(window) < self.cfg["low_voltage"]),
            "low_voltage_limit": self.cfg["low_voltage"],
        }

    # ---------- small cache for the slower lookups ----------
    def _cached(self, key, seconds, fn):
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < seconds:
            return hit[1]
        val = fn()
        self._cache[key] = (time.time(), val)
        return val

    # ---------- network ----------
    def _tailscale(self):
        out = _run(["tailscale", "status", "--json", "--peers=false"])
        try:
            d = json.loads(out)
        except ValueError:
            return None
        me = d.get("Self") or {}
        return {"state": d.get("BackendState"), "ip": (me.get("TailscaleIPs") or [None])[0], "name": me.get("HostName")}

    def _model_link(self):
        try:
            with open(self.cfg["router_config"], encoding="utf-8") as f:
                url = urllib.parse.urlparse(json.load(f)["providers"][0]["base_url"])
        except (OSError, ValueError, KeyError, IndexError):
            return None
        port = url.port or (443 if url.scheme == "https" else 80)
        return {"address": f"{url.hostname}:{port}", "ok": _tcp_ok(url.hostname, port)}

    def network(self):
        return {
            "wifi": wificlient.uplink(),
            "internet": self._cached("inet", 15, lambda: _tcp_ok("1.1.1.1", 443) or _tcp_ok("8.8.8.8", 53)),
            "tailscale": self._cached("ts", 20, self._tailscale),
            "model_link": self._cached("link", 15, self._model_link),
        }

    # ---------- services ----------
    def services(self):
        out = []
        for name in self.cfg["services"]:
            kv = dict(line.partition("=")[::2] for line in _run(
                ["systemctl", "show", name, "-p", "LoadState,ActiveState,SubState,NRestarts,ActiveEnterTimestamp"]).splitlines())
            if kv.get("LoadState") != "loaded":
                continue                                   # not installed on this stick
            out.append({"name": name, "active": kv.get("ActiveState") == "active", "state": kv.get("SubState"),
                        "restarts": int(kv.get("NRestarts") or 0), "since": kv.get("ActiveEnterTimestamp") or None})
        return out

    # ---------- model router ----------
    def model(self):
        try:
            with urllib.request.urlopen(self.cfg["router"] + "/stats", timeout=2) as r:
                d = json.load(r)
        except Exception:                                  # router not installed or down
            return None
        targets = [dict(v, name=k) for k, v in (d.get("targets") or {}).items()]
        return {"version": d.get("version"), "uptime_s": d.get("uptime_seconds"), "in_flight": d.get("in_flight", 0),
                "cooling_down": d.get("cooling_down_seconds") or {}, "targets": targets}

    # ---------- the coach's log for today ----------
    def _jsonl(self, path, date):
        rows = []
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if date is None or r.get("date") == date:
                        rows.append(r)
        except OSError:
            pass
        return rows

    def _timers(self):
        out = []
        for path in sorted(glob.glob(self.cfg["timers_glob"])):
            unit = os.path.basename(path)
            nxt = _run(["systemctl", "show", unit, "-p", "NextElapseUSecRealtime", "--value"]).strip()
            if nxt:
                out.append({"name": unit.replace(".timer", ""), "next": nxt})
        return out

    def coach(self):
        base = self.cfg["coach_dir"]
        if not os.path.isdir(base):
            return None
        today = dt.date.today().isoformat()
        row = {}
        try:
            with open(f"{base}/data/daily.csv", encoding="utf-8", newline="") as f:
                for r in csv.DictReader(f):
                    if r.get("date") == today:
                        row = r
        except OSError:
            pass
        try:
            with open(f"{base}/references/targets.json", encoding="utf-8") as f:
                goal = json.load(f).get("daily", {})
        except (OSError, ValueError):
            goal = {}
        meals = self._jsonl(f"{base}/data/meals.jsonl", today)
        workouts = self._jsonl(f"{base}/data/workouts.jsonl", today)

        def num(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        notebook = [f"{r.get('date')}: {r.get('text')}" for r in self._jsonl(f"{base}/data/brain.jsonl", None)][-6:]
        return {
            "date": today, "weight_kg": num(row.get("weight_kg")),
            "kcal": round(sum(m.get("kcal") or 0 for m in meals)), "kcal_goal": goal.get("kcal"),
            "protein_g": round(sum(m.get("protein_g") or 0 for m in meals)), "protein_goal": goal.get("protein_g"),
            "meals": len(meals), "trained": row.get("trained") or None, "creatine": bool(row.get("creatine")),
            "workouts": [{"exercise": w.get("label") or w.get("exercise"), "sets": len(w.get("sets") or []),
                          "top_kg": max([s.get("kg") or 0 for s in w.get("sets") or [{}]]),
                          "reps": (w.get("sets") or [{}])[0].get("reps")} for w in workouts],
            "reminders": self._cached("timers", 60, self._timers),
            "notebook": list(reversed(notebook)),
        }

    # ---------- LEDs ----------
    def leds(self):
        raw = _read(self.cfg["leds_state"])
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            return None

    def overview(self):
        return {"power": self.power(), "network": self.network(), "services": self.services(),
                "model": self.model(), "coach": self.coach(), "leds": self.leds(), "now": int(time.time())}
