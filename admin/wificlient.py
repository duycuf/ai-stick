"""WiFi client mode: join an existing WiFi network instead of broadcasting one.

The WCN36xx radio cannot be an access point and a client at the same time, and
scanning while hostapd owns wlan0 crashes its firmware. So there are two modes:

  ap      hostapd runs, wlan0 is hidden from NetworkManager (image default)
  client  hostapd is stopped, NetworkManager owns wlan0

The mode is the existence of NM_CLIENT_CONF, so it survives a reboot: a systemd
drop-in makes hostapd refuse to start while that file exists. The USB
network (192.168.100.1) works in both modes and is the way back in.
"""
import os
import re
import subprocess
import threading
import time

WLAN_IF = "wlan0"
CONN = "wifi-uplink"
CONN_FILE = f"/etc/NetworkManager/system-connections/{CONN}.nmconnection"
# Sorts after 99-custom.conf, whose unmanaged-devices list it replaces.
NM_CLIENT_CONF = "/etc/NetworkManager/conf.d/99-wifi-client.conf"
HOSTAPD_DROPIN = "/etc/systemd/system/hostapd.service.d/client-mode.conf"
JOIN_TIMEOUT = 45

_lock = threading.Lock()


def _run(args, timeout=20, check=True):
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError(f"{args[0]} failed: {(p.stderr or p.stdout).strip()[:300]}")
    return p.stdout


def mode():
    return "client" if os.path.exists(NM_CLIENT_CONF) else "ap"


# ---------- scan ----------
def _unescape_ssid(s):
    """iw prints bytes outside printable ASCII as \\xNN and a backslash as \\\\."""
    out, i = bytearray(), 0
    while i < len(s):
        if s[i] == "\\" and s[i + 1:i + 2] == "x" and re.fullmatch(r"[0-9a-fA-F]{2}", s[i + 2:i + 4] or ""):
            out.append(int(s[i + 2:i + 4], 16))
            i += 4
        elif s[i] == "\\" and s[i + 1:i + 2] == "\\":
            out.append(0x5C)
            i += 2
        else:
            out += s[i].encode()
            i += 1
    return out.decode("utf-8", "replace")


def parse_scan(text):
    """`iw dev wlan0 scan` output -> networks, strongest first, one entry per SSID."""
    nets, cur = [], None
    for line in text.splitlines():
        if line.startswith("BSS "):
            cur = {"ssid": "", "signal_dbm": None, "channel": None, "rsn": False, "wpa": False,
                   "wep": False, "suites": ""}
            nets.append(cur)
            continue
        if cur is None:
            continue
        s = line.strip()
        if s.startswith("SSID: "):
            cur["ssid"] = _unescape_ssid(s[6:])
        elif s.startswith("signal: "):
            try:
                cur["signal_dbm"] = round(float(s.split()[1]))
            except (ValueError, IndexError):
                pass
        elif s.startswith("DS Parameter set: channel "):
            cur["channel"] = int(s.rsplit(" ", 1)[1])
        elif s.startswith("RSN:"):
            cur["rsn"] = True
        elif s.startswith("WPA:"):
            cur["wpa"] = True
        elif s.startswith("capability:") and "Privacy" in s:
            cur["wep"] = True
        elif s.startswith("* Authentication suites: "):
            cur["suites"] += " " + s.split(": ", 1)[1]
    best = {}
    for n in nets:
        if not n["ssid"] or "\x00" in n["ssid"]:
            continue                      # hidden networks
        suites = n["suites"].split()
        if n["rsn"] or n["wpa"]:
            if "PSK" in suites:
                sec = "WPA2/WPA3" if "SAE" in suites else "WPA2" if n["rsn"] else "WPA"
            elif "SAE" in suites:
                sec = "WPA3"
            else:
                sec = "Enterprise"
        else:
            sec = "WEP" if n["wep"] else "Open"
        item = {"ssid": n["ssid"], "signal_dbm": n["signal_dbm"], "channel": n["channel"], "security": sec,
                "supported": sec in ("WPA", "WPA2", "WPA2/WPA3", "WPA3", "Open")}
        old = best.get(n["ssid"])
        if old is None or (item["signal_dbm"] or -999) > (old["signal_dbm"] or -999):
            best[n["ssid"]] = item
    return sorted(best.values(), key=lambda n: -(n["signal_dbm"] or -999))


def _iw_scan():
    last = ""
    for _ in range(6):
        p = subprocess.run(["iw", "dev", WLAN_IF, "scan"], capture_output=True, text=True, timeout=30)
        if p.returncode == 0:
            return p.stdout
        last = (p.stderr or p.stdout).strip()
        time.sleep(2)                     # "Device or resource busy": a scan is already running
    raise RuntimeError(f"WiFi scan failed: {last[:200]}")


def scan():
    """List nearby networks. In AP mode the access point goes off for about ten seconds."""
    with _lock:
        if mode() == "client":
            return {"networks": parse_scan(_iw_scan()), "mode": "client"}
        _run(["systemctl", "stop", "hostapd"], timeout=30, check=False)
        try:
            _run(["ip", "link", "set", WLAN_IF, "nomaster"], check=False)
            _run(["ip", "link", "set", WLAN_IF, "down"], check=False)
            _run(["iw", "dev", WLAN_IF, "set", "type", "managed"])
            _run(["ip", "link", "set", WLAN_IF, "up"])
            nets = parse_scan(_iw_scan())
        finally:
            _run(["ip", "link", "set", WLAN_IF, "down"], check=False)
            _run(["systemctl", "start", "hostapd"], timeout=30, check=False)
        return {"networks": nets, "mode": "ap"}


# ---------- mode switch ----------
def _keyfile_escape(v):
    v = v.replace("\\", "\\\\")
    if v.startswith(" "):
        v = "\\s" + v[1:]
    return v


def _write_connection(ssid, password, security):
    ssid_bytes = ";".join(str(b) for b in ssid.encode()) + ";"
    lines = ["[connection]", f"id={CONN}", "type=wifi", f"interface-name={WLAN_IF}",
             "autoconnect=true", "autoconnect-retries=0", "",
             "[wifi]", "mode=infrastructure", f"ssid={ssid_bytes}", "powersave=3", ""]
    if password:
        lines += ["[wifi-security]", f"key-mgmt={'sae' if security == 'WPA3' else 'wpa-psk'}",
                  f"psk={_keyfile_escape(password)}", ""]
    lines += ["[ipv4]", "method=auto", "", "[ipv6]", "method=auto", ""]
    tmp = CONN_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines))
    os.replace(tmp, CONN_FILE)


def _nm_state():
    for line in _run(["nmcli", "-t", "-f", "DEVICE,STATE", "device"], check=False).splitlines():
        dev, _, state = line.partition(":")
        if dev == WLAN_IF:
            return state
    return ""


def _ensure_dropin():
    """Images built before client mode existed do not have the drop-in yet."""
    if os.path.exists(HOSTAPD_DROPIN):
        return
    os.makedirs(os.path.dirname(HOSTAPD_DROPIN), exist_ok=True)
    with open(HOSTAPD_DROPIN, "w") as f:
        f.write("# hostapd stays off while the stick is a WiFi client (see the admin page).\n"
                f"[Unit]\nConditionPathExists=!{NM_CLIENT_CONF}\n")
    _run(["systemctl", "daemon-reload"], timeout=60, check=False)


def _to_client():
    _ensure_dropin()
    _run(["systemctl", "stop", "hostapd"], timeout=30, check=False)
    _run(["ip", "link", "set", WLAN_IF, "nomaster"], check=False)
    tmp = NM_CLIENT_CONF + ".tmp"
    with open(tmp, "w") as f:
        f.write("# Written by the admin page: WiFi client mode. Delete to go back to access point mode.\n"
                "[keyfile]\nunmanaged-devices=interface-name:lo\n")
    os.replace(tmp, NM_CLIENT_CONF)
    _run(["nmcli", "general", "reload", "conf"], check=False)
    _run(["nmcli", "device", "set", WLAN_IF, "managed", "yes"], check=False)
    for _ in range(20):
        if _nm_state() not in ("", "unmanaged", "unavailable"):
            return
        time.sleep(1)
    raise RuntimeError("NetworkManager did not take over the WiFi radio")


def _to_ap():
    _run(["nmcli", "connection", "down", CONN], check=False)
    try:
        os.remove(NM_CLIENT_CONF)
    except FileNotFoundError:
        pass
    _run(["nmcli", "general", "reload", "conf"], check=False)
    _run(["nmcli", "device", "set", WLAN_IF, "managed", "no"], check=False)
    time.sleep(1)
    _run(["systemctl", "restart", "hostapd"], timeout=30, check=False)


def join(d):
    ssid = str(d.get("ssid", ""))
    password = str(d.get("password") or "")
    security = str(d.get("security") or ("WPA2" if password else "Open"))
    if not (1 <= len(ssid.encode()) <= 32) or any(c in ssid for c in "\n\r\x00"):
        raise ValueError("WiFi name must be 1-32 bytes, no line breaks")
    if security not in ("WPA", "WPA2", "WPA2/WPA3", "WPA3", "Open"):
        raise ValueError("This kind of network is not supported")
    if security == "Open":
        password = ""
    elif not (8 <= len(password) <= 63) or not all(32 <= ord(c) < 127 for c in password):
        raise ValueError("WiFi password must be 8-63 letters/digits/symbols")

    with _lock:
        was = mode()
        old_conn = None
        if os.path.exists(CONN_FILE):
            with open(CONN_FILE) as f:
                old_conn = f.read()
        try:
            _write_connection(ssid, password, security)
            if was == "ap":
                _to_client()
            _run(["nmcli", "connection", "reload"])
            _run(["nmcli", "--wait", str(JOIN_TIMEOUT), "connection", "up", CONN], timeout=JOIN_TIMEOUT + 15)
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            # Put everything back the way it was, so a typo never strands the device.
            _run(["nmcli", "connection", "down", CONN], check=False)
            if old_conn is None:
                try:
                    os.remove(CONN_FILE)
                except FileNotFoundError:
                    pass
            else:
                fd = os.open(CONN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write(old_conn)
            _run(["nmcli", "connection", "reload"], check=False)
            if was == "ap":
                _to_ap()
            else:
                _run(["nmcli", "connection", "up", CONN], timeout=60, check=False)
            msg = str(e)
            hint = "wrong password?" if "Secrets" in msg or "802-11" in msg or "secrets" in msg else "out of range?"
            raise ValueError(f"Could not join \"{ssid}\" ({hint}). The previous WiFi setup is back.") from e
    return status()


def leave():
    """Forget the joined network and broadcast the stick's own WiFi again."""
    with _lock:
        _to_ap()
        try:
            os.remove(CONN_FILE)
        except FileNotFoundError:
            pass
        _run(["nmcli", "connection", "reload"], check=False)
    return status()


# ---------- status ----------
def uplink():
    """Cheap WiFi uplink summary for the dashboard, polled every few seconds. None in access point mode."""
    if mode() != "client":
        return None
    out = {"connected": False, "ssid": None, "signal_dbm": None, "ip": None, "rx_bytes": 0, "tx_bytes": 0}
    link = _run(["iw", "dev", WLAN_IF, "link"], check=False)
    m = re.search(r"^\s*SSID: (.*)$", link, re.M)
    if m:
        out["ssid"] = _unescape_ssid(m.group(1))
        s = re.search(r"signal: (-?\d+)", link)
        out["signal_dbm"] = int(s.group(1)) if s else None
    m = re.search(r"inet (\S+?)/", _run(["ip", "-4", "-o", "addr", "show", "dev", WLAN_IF], check=False))
    out["ip"] = m.group(1) if m else None
    out["connected"] = bool(out["ssid"] and out["ip"])
    for key, name in (("rx_bytes", "rx_bytes"), ("tx_bytes", "tx_bytes")):
        try:
            with open(f"/sys/class/net/{WLAN_IF}/statistics/{name}") as f:
                out[key] = int(f.read().strip() or 0)
        except (OSError, ValueError):
            pass
    return out


def status():
    st = {"mode": mode(), "connected": False, "ssid": None, "ip": None, "signal_dbm": None,
          "gateway": None, "saved_ssid": None}
    if st["mode"] != "client":
        return st
    out = _run(["nmcli", "-g", "802-11-wireless.ssid", "connection", "show", CONN], check=False).strip()
    st["saved_ssid"] = out or None
    link = _run(["iw", "dev", WLAN_IF, "link"], check=False)
    m = re.search(r"^\s*SSID: (.*)$", link, re.M)
    if m:
        st["ssid"] = _unescape_ssid(m.group(1))
        s = re.search(r"signal: (-?\d+)", link)
        st["signal_dbm"] = int(s.group(1)) if s else None
    ip = _run(["ip", "-4", "-o", "addr", "show", "dev", WLAN_IF], check=False)
    m = re.search(r"inet (\S+?)/", ip)
    st["ip"] = m.group(1) if m else None
    gw = _run(["ip", "-4", "route", "show", "default", "dev", WLAN_IF], check=False)
    m = re.search(r"via (\S+)", gw)
    st["gateway"] = m.group(1) if m else None
    st["connected"] = bool(st["ssid"] and st["ip"])
    return st
