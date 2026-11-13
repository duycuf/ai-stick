"""Real device backend: mmcli / nmcli / hostapd / sysfs.

Principle: no shell, subprocess runs with argument lists only, all input is validated.
Validation failures raise ValueError (server.py turns them into 400).
"""
import json
import os
import re
import subprocess
import threading
import time

import netctl
import sms
import systools

LTE_CONN = "lte"                 # NetworkManager connection name
HOSTAPD_CONF = "/etc/hostapd/hostapd.conf"
LEASES = "/var/lib/misc/dnsmasq.leases"
WLAN_IF = "wlan0"
LAN_BRIDGE = "br0"
THERMAL_POLL = 30               # thermal monitor interval (seconds)
THERMAL_WARN_DEFAULT = 80       # default warning temperature (°C); alerts before the 95°C critical point
THERMAL_CRIT = 95               # hardware protection shutdown temperature (°C)
THERMAL_HYST = 6                # recovery alert hysteresis (margin to clear the warning, °C)

_apn_lock = threading.Lock()
_wifi_lock = threading.Lock()


def run(args, timeout=15, check=True):
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError(f"{args[0]} failed: {(p.stderr or p.stdout).strip()[:300]}")
    return p.stdout


def mmcli_json(*args, timeout=15):
    try:
        return json.loads(run(["mmcli", "-J", *args], timeout=timeout))
    except (RuntimeError, json.JSONDecodeError, subprocess.TimeoutExpired):
        return {}


def _v(x):
    """Turn empty mmcli values such as '--' into None."""
    return None if x in (None, "", "--") else x


def _read(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def _tg_send(token, chat_id, text):
    """Telegram sendMessage over the device's normal uplink. Returns (ok, error)."""
    try:
        out = run(["curl", "-s", "-m", "15", f"https://api.telegram.org/bot{token}/sendMessage",
                   "-d", f"chat_id={chat_id}", "--data-urlencode", f"text={text}",
                   "-w", "\n%{http_code}"], timeout=25, check=False)
        code = out.rsplit("\n", 1)[-1].strip()
        if code == "200":
            return True, None
        return False, f"HTTP {code or 'connection failed'}: {out[:160]}"
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:160]


def _tg_send_photo(token, chat_id, path):
    try:
        run(["curl", "-s", "-m", "60", f"https://api.telegram.org/bot{token}/sendPhoto",
             "-F", f"chat_id={chat_id}", "-F", f"photo=@{path}",
             "-o", "/dev/null"], timeout=70, check=False)
    except Exception:  # noqa: BLE001 - a failed photo upload must not block the text alert
        pass


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


class Backend:
    def __init__(self):
        # Messages: inbound collector (background thread) + sqlite storage
        self.msgs = sms.Messages()
        self.settings = netctl.Settings("/var/lib/openstick-admin/settings.json")
        self.msgs.on_inbound = self._notify_telegram      # forward to Telegram on receive (if configured)
        threading.Thread(target=self._thermal_loop, name="thermal", daemon=True).start()
        self.net = netctl.NetCtl(self.settings)            # data usage, device blocking (nft inet osa)
        self.sys = systools.SysTools(self.settings, wan_connected=self._wan_up)
        # detailed signal values (RSRP etc.) only refresh once a polling rate is set
        try:
            run(["mmcli", "-m", "any", "--signal-setup=10"], timeout=10, check=False)
        except Exception:  # noqa: BLE001
            pass

    # ================= Dashboard =================
    def status(self):
        m = mmcli_json("-m", "any").get("modem", {})
        g, t = m.get("generic", {}), m.get("3gpp", {})
        sig = mmcli_json("-m", "any", "--signal-get").get("modem", {}).get("signal", {})
        lte = sig.get("lte", {})
        bearer = self._active_bearer(g.get("bearers") or [])
        return {
            "modem": {
                "state": _v(g.get("state")),
                "operator": _v(t.get("operator-name")),
                "registration": _v(t.get("registration-state")),
                "access_tech": g.get("access-technologies") or [],
                "quality": _num((g.get("signal-quality") or {}).get("value")),
                "rsrp": _num(_v(lte.get("rsrp"))), "rsrq": _num(_v(lte.get("rsrq"))),
                "snr": _num(_v(lte.get("snr"))), "rssi": _num(_v(lte.get("rssi"))),
                "sim": self._sim_state(g),
            },
            "wan": {
                "connected": bool(bearer and bearer["connected"]),
                "ipv4": bearer and bearer["ipv4"], "apn": bearer and bearer["apn"],
                # wwan0 (bam-dmux) sysfs counters stay 0, so use ModemManager bearer stats (for this connection)
                "rx_bytes": bearer["rx_bytes"] if bearer else 0,
                "tx_bytes": bearer["tx_bytes"] if bearer else 0,
                "connected_sec": bearer["duration"] if bearer else None,
                # for live speed: bridge counters on the client side (br0 tx = bytes sent to clients = download)
                "lan_down_bytes": int(_read(f"/sys/class/net/{LAN_BRIDGE}/statistics/tx_bytes", "0") or 0),
                "lan_up_bytes": int(_read(f"/sys/class/net/{LAN_BRIDGE}/statistics/rx_bytes", "0") or 0),
                "ts": time.time(),
            },
            "clients": len(self.clients()["clients"]),
            "unread": self.msgs.unread(),
            "storage": self.msgs.storage(),
            "usage": self._usage_summary(),
            "system": self._system(),
        }

    def _sim_state(self, g):
        if not _v(g.get("sim")):
            return "None"
        lock = _v(g.get("unlock-required"))
        if lock and lock not in ("sim-pin2", "none"):
            return f"Locked ({lock})"
        return "OK"

    def _active_bearer(self, paths):
        for p in paths:
            b = mmcli_json("-b", p).get("bearer", {})
            st, pr, v4 = b.get("status", {}), b.get("properties", {}), b.get("ipv4-config", {})
            info = {
                "connected": st.get("connected") == "yes", "interface": _v(st.get("interface")),
                "apn": _v(pr.get("apn")), "ip_type": _v(pr.get("ip-type")),
                "roaming": _v(pr.get("roaming")),
                "ipv4": _v(v4.get("address")), "gateway": _v(v4.get("gateway")),
                "dns": [d for d in (v4.get("dns") or []) if _v(d)],
                "rx_bytes": int(_num(_v(b.get("stats", {}).get("bytes-rx"))) or 0),
                "tx_bytes": int(_num(_v(b.get("stats", {}).get("bytes-tx"))) or 0),
                "duration": int(_num(_v(b.get("stats", {}).get("duration"))) or 0),
            }
            if info["connected"]:
                return info
        return None

    def _temps(self):
        """Per-zone temperatures in °C: {zone type: value}."""
        out = {}
        base = "/sys/class/thermal"
        if os.path.isdir(base):
            for z in sorted(os.listdir(base)):
                t = _num(_read(f"{base}/{z}/temp"))
                if t:
                    out[_read(f"{base}/{z}/type") or z] = round(t / 1000, 1)
        return out

    def _system(self):
        mem = {}
        for line in (_read("/proc/meminfo", "") or "").splitlines():
            k, _, v = line.partition(":")
            mem[k] = int(v.split()[0]) if v.split() else 0
        temps = self._temps()
        return {
            "uptime": int(float((_read("/proc/uptime", "0") or "0").split()[0])),
            "load": (_read("/proc/loadavg", "0 0 0") or "0").split()[:3],
            "mem_total_kb": mem.get("MemTotal"), "mem_avail_kb": mem.get("MemAvailable"),
            "temp_c": max(temps.values()) if temps else None,
        }

    # ================= Connected devices =================
    def clients(self):
        leases = {}
        for line in (_read(LEASES, "") or "").splitlines():
            parts = line.split()
            if len(parts) >= 4:
                leases[parts[1].lower()] = {"ip": parts[2], "name": None if parts[3] == "*" else parts[3]}
        # WiFi clients: iw station dump (works without hostapd ctrl_interface)
        wifi, cur = {}, None
        try:
            out = run(["iw", "dev", WLAN_IF, "station", "dump"], timeout=5, check=False)
        except Exception:  # noqa: BLE001
            out = ""
        for line in out.splitlines():
            m = re.match(r"station ([0-9a-f:]{17})", line.strip().lower())
            if m:
                cur = m.group(1)
                wifi[cur] = {}
            elif cur and ":" in line:
                k, _, v = line.strip().partition(":")
                if k == "signal":
                    wifi[cur]["signal"] = v.split()[0] if v.split() else None
                elif k == "connected time":
                    wifi[cur]["connected_time"] = v.split()[0] if v.split() else None
        # tell the connection path from the bridge port (wlan0 = WiFi, usb* = USB)
        port = {}
        try:
            for f in json.loads(run(["bridge", "-j", "fdb", "show", "br", LAN_BRIDGE], timeout=5) or "[]"):
                if f.get("mac") and "permanent" not in (f.get("state") or "") and f.get("ifname"):
                    port[f["mac"].lower()] = f["ifname"]
        except Exception:  # noqa: BLE001
            pass
        neigh = {}
        try:
            for n in json.loads(run(["ip", "-j", "neigh", "show", "dev", LAN_BRIDGE], timeout=5) or "[]"):
                if n.get("lladdr") and ":" not in n.get("dst", ":"):     # IPv4 only
                    neigh[n["lladdr"].lower()] = {"ip": n["dst"], "state": (n.get("state") or [""])[0]}
        except Exception:  # noqa: BLE001
            pass

        blocked = {b["mac"] for b in self.net.blocked()}
        result = []
        for mac in sorted(set(leases) | set(wifi) | {m for m, n in neigh.items() if n["state"] in ("REACHABLE", "DELAY", "PROBE")}):
            l, n, w = leases.get(mac, {}), neigh.get(mac, {}), wifi.get(mac)
            online = w is not None or n.get("state") in ("REACHABLE", "DELAY", "PROBE", "STALE")
            if not online:
                continue
            dev = port.get(mac, "")
            via = "WiFi" if (w is not None or dev == WLAN_IF) else "USB" if dev.startswith("usb") else "-"
            result.append({
                "mac": mac, "ip": l.get("ip") or n.get("ip"), "name": l.get("name"),
                "via": via, "blocked": mac in blocked,
                "connected_sec": int(w["connected_time"]) if w and w.get("connected_time", "").isdigit() else None,
                "signal_dbm": _num(w.get("signal")) if w else None,
            })
        online = {c["mac"] for c in result}
        offline_blocked = [b for b in self.net.blocked() if b["mac"] not in online]
        return {"clients": result, "blocked_offline": offline_blocked}

    # ================= WiFi =================
    WIFI_KEYS = ("ssid", "wpa_passphrase", "channel", "ignore_broadcast_ssid", "max_num_sta", "ap_isolate", "wpa_key_mgmt")

    def _hostapd_lines(self):
        with open(HOSTAPD_CONF) as f:
            return f.read().splitlines()

    def wifi_get(self):
        cfg = {}
        for line in self._hostapd_lines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, _, v = line.partition("=")
                cfg[k.strip()] = v.strip()
        return {
            "ssid": cfg.get("ssid", ""),
            "has_password": bool(cfg.get("wpa_passphrase")),
            "security": "WPA2/WPA3" if "SAE" in cfg.get("wpa_key_mgmt", "") else "WPA2",
            "channel": int(cfg.get("channel", "6") or 6),
            "hidden": cfg.get("ignore_broadcast_ssid", "0") != "0",
            # hostapd default 2007 = no limit (on save, 0 is also written as 2007)
            "max_clients": (lambda n: None if n == 0 or n >= 2007 else n)(int(cfg.get("max_num_sta", "0") or 0)),
            "isolate": cfg.get("ap_isolate", "0") == "1",
            "running": run(["systemctl", "is-active", "hostapd"], check=False).strip() == "active",
        }

    def wifi_set(self, d):
        changes = {}
        if "ssid" in d:
            ssid = str(d["ssid"])
            if not (1 <= len(ssid.encode()) <= 32) or any(c in ssid for c in "\n\r"):
                raise ValueError("WiFi name must be 1-32 bytes, no line breaks")
            changes["ssid"] = ssid
        if d.get("password"):
            pw = str(d["password"])
            if not (8 <= len(pw) <= 63) or not all(32 <= ord(c) < 127 for c in pw):
                raise ValueError("WiFi password must be 8-63 letters/digits/symbols")
            changes["wpa_passphrase"] = pw
        if "channel" in d:
            ch = int(d["channel"])
            if not 1 <= ch <= 13:
                raise ValueError("Channel must be 1-13")
            changes["channel"] = str(ch)
        if "hidden" in d:
            changes["ignore_broadcast_ssid"] = "1" if d["hidden"] else "0"
        if "isolate" in d:
            changes["ap_isolate"] = "1" if d["isolate"] else "0"
        if "max_clients" in d:
            mc = int(d["max_clients"] or 0)
            if not 0 <= mc <= 32:
                raise ValueError("Max clients must be 0 (no limit) to 32")
            changes["max_num_sta"] = str(mc or 2007)
        if "security" in d:
            if d["security"] not in ("WPA2", "WPA2/WPA3"):
                raise ValueError("Invalid security mode")
            changes["wpa_key_mgmt"] = "WPA-PSK SAE" if d["security"] == "WPA2/WPA3" else "WPA-PSK"
            changes["ieee80211w"] = "1" if d["security"] == "WPA2/WPA3" else "0"
        if not changes:
            raise ValueError("No changes to save")

        with _wifi_lock:
            lines = self._hostapd_lines()
            backup = HOSTAPD_CONF + ".bak"
            if not os.path.exists(backup):
                with open(backup, "w") as f:
                    f.write("\n".join(lines) + "\n")
            seen, out = set(), []
            for line in lines:
                k = line.partition("=")[0].strip()
                if "=" in line and not line.lstrip().startswith("#") and k in changes:
                    out.append(f"{k}={changes[k]}")
                    seen.add(k)
                else:
                    out.append(line)
            out += [f"{k}={v}" for k, v in changes.items() if k not in seen]
            tmp = HOSTAPD_CONF + ".tmp"
            with open(tmp, "w") as f:
                f.write("\n".join(out) + "\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, HOSTAPD_CONF)
            run(["systemctl", "restart", "hostapd"], timeout=30)
        return {"ok": True, "applied": sorted(k for k in changes if k != "wpa_passphrase") + (["password"] if "wpa_passphrase" in changes else [])}

    # ================= Internet (WAN) / APN =================
    def _nm_get(self):
        out = run(["nmcli", "-t", "-f",
                   "connection.autoconnect,gsm.apn,gsm.username,gsm.auto-config,gsm.home-only,ipv4.method,ipv6.method",
                   "c", "show", LTE_CONN])
        cfg = {}
        for line in out.splitlines():
            k, _, v = line.partition(":")
            cfg[k] = v
        return cfg

    def wan_get(self):
        cfg = self._nm_get()
        g = mmcli_json("-m", "any").get("modem", {}).get("generic", {})
        bearer = self._active_bearer(g.get("bearers") or [])
        v4, v6 = cfg.get("ipv4.method") != "disabled", cfg.get("ipv6.method") not in ("disabled", "ignore")
        return {
            "data_enabled": cfg.get("connection.autoconnect") == "yes",
            "roaming_allowed": cfg.get("gsm.home-only") != "yes",
            "apn_mode": "auto" if cfg.get("gsm.auto-config") == "yes" else "manual",
            "apn": cfg.get("gsm.apn") or "", "username": cfg.get("gsm.username") or "",
            "ip_type": "ipv4v6" if v4 and v6 else ("ipv6" if v6 else "ipv4"),
            "bearer": bearer,
        }

    def wan_set_data(self, d):
        changes = []
        if "roaming_allowed" in d:
            changes += ["gsm.home-only", "no" if d["roaming_allowed"] else "yes"]
        if "data_enabled" in d:
            changes += ["connection.autoconnect", "yes" if d["data_enabled"] else "no"]
        if not changes:
            raise ValueError("No changes to save")
        run(["nmcli", "c", "modify", LTE_CONN, *changes])
        if "data_enabled" in d:
            if d["data_enabled"]:
                run(["nmcli", "c", "up", LTE_CONN], timeout=90)
            else:
                run(["nmcli", "c", "down", LTE_CONN], timeout=30, check=False)
        return {"ok": True}

    def wan_set_apn(self, d):
        mode = d.get("apn_mode")
        if mode not in ("auto", "manual"):
            raise ValueError("Invalid APN mode")
        ip_type = d.get("ip_type", "ipv4v6")
        if ip_type not in ("ipv4", "ipv6", "ipv4v6"):
            raise ValueError("Invalid IP type")
        apn, user, pw = str(d.get("apn", "")), str(d.get("username", "")), str(d.get("password", ""))
        if mode == "manual" and not re.fullmatch(r"[A-Za-z0-9.\-_]{1,63}", apn):
            raise ValueError("APN must be 1-63 letters/digits/.-_")
        for x in (user, pw):
            if len(x) > 64 or any(c in x for c in "\n\r"):
                raise ValueError("Invalid username/password format")

        with _apn_lock:
            prev = self._nm_get()
            new = ["gsm.auto-config", "yes" if mode == "auto" else "no",
                   "gsm.apn", apn if mode == "manual" else "",
                   "gsm.username", user if mode == "manual" else ""]
            if mode == "manual" and pw:
                new += ["gsm.password", pw, "gsm.password-flags", "0"]
            new += ["ipv4.method", "auto" if ip_type != "ipv6" else "disabled",
                    "ipv6.method", "auto" if ip_type != "ipv4" else "disabled"]
            run(["nmcli", "c", "modify", LTE_CONN, *new])
            ok = self._reconnect_and_check()
            if ok:
                return {"ok": True, "rolled_back": False, "bearer": ok}
            # failed -> roll back to the previous settings (the password is still stored in NM)
            run(["nmcli", "c", "modify", LTE_CONN,
                 "gsm.auto-config", prev.get("gsm.auto-config", "yes"),
                 "gsm.apn", prev.get("gsm.apn", ""), "gsm.username", prev.get("gsm.username", ""),
                 "ipv4.method", prev.get("ipv4.method", "auto"), "ipv6.method", prev.get("ipv6.method", "auto")])
            again = self._reconnect_and_check()
            return {"ok": False, "rolled_back": True, "restored_connected": bool(again),
                    "error": "Could not connect with the new APN, reverted to the previous settings"}

    def _reconnect_and_check(self):
        run(["nmcli", "c", "down", LTE_CONN], timeout=30, check=False)
        r = subprocess.run(["nmcli", "c", "up", LTE_CONN], capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            return None
        for _ in range(10):
            g = mmcli_json("-m", "any").get("modem", {}).get("generic", {})
            b = self._active_bearer(g.get("bearers") or [])
            if b and b["ipv4"]:
                return b
            time.sleep(2)
        return None

    # ================= Device =================
    def device(self):
        m = mmcli_json("-m", "any").get("modem", {})
        g, t = m.get("generic", {}), m.get("3gpp", {})
        sim = {}
        if _v(g.get("sim")):
            sim = mmcli_json("-i", g["sim"]).get("sim", {}).get("properties", {})
        osr = {}
        for line in (_read("/etc/os-release", "") or "").splitlines():
            k, _, v = line.partition("=")
            osr[k] = v.strip('"')
        return {
            "model": (_read("/proc/device-tree/model", "") or "").rstrip("\x00"),
            "hostname": _read("/etc/hostname"),
            "os": osr.get("PRETTY_NAME"),
            "kernel": os.uname().release,
            "imei": _v(t.get("imei")),
            "modem_firmware": ((_v(g.get("revision")) or "").split() or [None])[0],
            "iccid": _v(sim.get("iccid")), "imsi": _v(sim.get("imsi")),
            "sim_operator": _v(sim.get("operator-name")) or _v(t.get("operator-name")),
            "sim_operator_code": _v(sim.get("operator-code")),
            "lan_ip": "192.168.100.1",
        }

    def reboot(self):
        threading.Timer(1.5, lambda: subprocess.run(["systemctl", "reboot"])).start()
        return {"ok": True}

    # ================= Messages (SMS/MMS) =================
    def messages_list(self, q):
        return self.msgs.list(q.get("limit", 50), q.get("before"))

    def messages_send(self, d):
        return self.msgs.send(d.get("number", ""), d.get("text", ""))

    def messages_send_mms(self, d):
        return self.msgs.send_mms(d.get("number", ""), d.get("text", ""), d.get("subject"),
                                  sms.decode_attachments(d.get("attachments")))

    def messages_delete(self, d):
        if d.get("older_than_days") is not None:
            return self.msgs.delete(older_than_days=d["older_than_days"])
        ids = d.get("ids") or []
        if not isinstance(ids, list):
            raise ValueError("Select messages to delete")
        return self.msgs.delete(ids)

    def messages_read(self, d):
        return self.msgs.mark_read(d.get("ids"))

    def messages_retry(self, d):
        return self.msgs.retry(d.get("id"))

    def messages_media(self, msg_id, part):
        return self.msgs.media(msg_id, part)

    # ================= Telegram inbound alerts =================
    def _tg_target(self):
        """Bot token and chat ID. Used for alerts (thermal etc.) whether inbound forwarding is on or off."""
        t = self.settings.get("telegram") or {}
        if t.get("token") and t.get("chat_id"):
            return t["token"], t["chat_id"]
        return None

    def telegram_get(self):
        t = self.settings.get("telegram") or {}
        return {"enabled": bool(t.get("enabled")), "chat_id": t.get("chat_id", ""),
                "has_token": bool(t.get("token")), "send_media": t.get("send_media", True)}

    def telegram_set(self, d):
        t = dict(self.settings.get("telegram") or {})
        if "chat_id" in d:
            cid = str(d.get("chat_id") or "").strip()
            if cid and not re.fullmatch(r"-?\d{1,20}|@[A-Za-z]\w{3,}", cid):
                raise ValueError("Invalid chat ID format (a number, or a channel name starting with @)")
            t["chat_id"] = cid
        tok = d.get("token")
        if tok:                                 # empty value keeps the existing token
            tok = str(tok).strip()
            if not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{30,}", tok):
                raise ValueError("Invalid bot token format (e.g. 123456789:ABCdef...)")
            t["token"] = tok
        if "send_media" in d:
            t["send_media"] = bool(d["send_media"])
        if d.get("enabled"):
            if not t.get("token"):
                raise ValueError("Enter the bot token")
            if not t.get("chat_id"):
                raise ValueError("Enter the chat ID")
        t["enabled"] = bool(d.get("enabled"))
        self.settings.update(telegram=t)
        return self.telegram_get()

    def telegram_test(self, d):
        t = self.settings.get("telegram") or {}
        if not t.get("token") or not t.get("chat_id"):
            raise ValueError("Save the bot token and chat ID first")
        ok, err = _tg_send(t["token"], t["chat_id"], "✅ OpenStick connection test.")
        if not ok:
            raise ValueError(f"Send failed: {err}")
        return {"ok": True}

    def _notify_telegram(self, msg):
        """Forward an inbound message to Telegram, on a separate thread so the collector thread is not blocked."""
        t = self.settings.get("telegram") or {}
        if not t.get("enabled") or not t.get("token") or not t.get("chat_id"):
            return
        threading.Thread(target=self._do_notify, args=(dict(t), msg), daemon=True).start()

    # ================= Thermal alerts =================
    def thermal_get(self):
        c = self.settings.get("thermal") or {}
        temps = self._temps()
        return {"enabled": bool(c.get("enabled")), "warn_c": c.get("warn_c", THERMAL_WARN_DEFAULT),
                "current_c": max(temps.values()) if temps else None, "zones": temps,
                "crit_c": THERMAL_CRIT, "has_telegram": self._tg_target() is not None}

    def thermal_set(self, d):
        c = dict(self.settings.get("thermal") or {})
        if "warn_c" in d:
            w = _num(d.get("warn_c"))
            if w is None or not 60 <= w <= 90:
                raise ValueError("Warning temperature must be 60-90°C")
            c["warn_c"] = int(w)
        if d.get("enabled") and not self._tg_target():
            raise ValueError("Save the Telegram bot token and chat ID first")
        c["enabled"] = bool(d.get("enabled"))
        self.settings.update(thermal=c)
        return self.thermal_get()

    def _thermal_loop(self):
        alerted = False
        while True:
            time.sleep(THERMAL_POLL)
            try:
                c = self.settings.get("thermal") or {}
                tg = self._tg_target()
                if not c.get("enabled") or not tg:
                    alerted = False
                    continue
                temps = self._temps()
                if not temps:
                    continue
                hot = c.get("warn_c", THERMAL_WARN_DEFAULT)
                zone, mx = max(temps.items(), key=lambda kv: kv[1])
                if not alerted and mx >= hot:
                    alerted = True
                    _tg_send(tg[0], tg[1],
                             f"⚠️ Overheating warning: {mx:.0f}°C ({zone})\n"
                             f"The device powers off automatically at the critical {THERMAL_CRIT}°C. "
                             f"Check the ventilation.")
                elif alerted and mx <= hot - THERMAL_HYST:
                    alerted = False
                    _tg_send(tg[0], tg[1], f"✅ Temperature back to normal: {mx:.0f}°C")
            except Exception as e:  # noqa: BLE001 - the monitor must never die
                print(f"thermal monitor: {e}", flush=True)

    def _do_notify(self, t, msg):
        kind = "MMS" if msg.get("kind") == "mms" else "SMS"
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(msg.get("ts") or time.time()))
        num = (msg.get("number") or "(no number)").replace("+82", "0")
        lines = [f"\U0001F4E9 Message received ({kind})", f"From: {num}", when]
        if msg.get("subject"):
            lines.append(f"Subject: {msg['subject']}")
        if msg.get("text"):
            lines += ["", msg["text"]]
        parts = msg.get("parts") or []
        imgs = [p for p in parts if str(p.get("type", "")).startswith("image/")]
        other = [p for p in parts if p not in imgs]
        if other:
            lines.append("")
            lines += [f"📎 {p.get('name') or p.get('type')}" for p in other]
        _tg_send(t["token"], t["chat_id"], "\n".join(lines))
        if t.get("send_media", True):
            for p in imgs:
                path = os.path.join(self.msgs.media_dir, p["file"])
                if os.path.exists(path):
                    _tg_send_photo(t["token"], t["chat_id"], path)

    # ================= Data usage / device blocking =================
    def _wan_up(self):
        try:
            out = json.loads(run(["ip", "-j", "-4", "addr", "show", "dev", "wwan0"], timeout=5, check=False) or "[]")
            return any(i.get("addr_info") for i in out)
        except Exception:  # noqa: BLE001
            return False

    def _usage_summary(self):
        u = self.net.usage()
        return {k: u[k] for k in ("today", "period", "cap_bytes", "percent", "warn_pct", "blocked_now")}

    def usage_get(self):
        return self.net.usage()

    def usage_set(self, d):
        return self.net.set_usage(d)

    def usage_reset(self):
        return self.net.reset_usage()

    def client_block(self, d, requester_ip):
        return self.net.block(d.get("mac"), d.get("name"), netctl.mac_of_ip(requester_ip))

    def client_unblock(self, d):
        return self.net.unblock(d.get("mac"))

    # ================= System tools =================
    def leds_set(self, d):
        return self.sys.set_leds(d.get("mode"))

    def system_get(self):
        return {"leds": self.settings.get("leds", "on"), "time": self.sys.time_info(), **self.sys.services()}

    def timezone_set(self, d):
        return self.sys.set_timezone(d.get("timezone"))

    def logs(self, q):
        return self.sys.logs(q.get("unit", "admin"), q.get("lines", 200))

    def service_restart(self, d):
        return self.sys.restart(d.get("name"))

    def dns_get(self):
        return self.sys.dns_get()

    def dns_set(self, d):
        carrier = []
        if d.get("preset") == "carrier":
            g = mmcli_json("-m", "any").get("modem", {}).get("generic", {})
            b = self._active_bearer(g.get("bearers") or [])
            carrier = (b or {}).get("dns") or []
        return self.sys.dns_set(d.get("preset"), carrier)

    def diag(self, d):
        kind = d.get("kind")
        if kind == "ping":
            return self.sys.ping(d.get("host"))
        if kind == "lookup":
            return self.sys.lookup(d.get("host"))
        if kind == "traceroute":
            return self.sys.traceroute(d.get("host"))
        if kind == "speedtest":
            return self.sys.speedtest(d.get("mb", 10))
        raise ValueError("Unknown diagnostic type")
