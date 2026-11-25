"""Shared bits for the Ventus daemon (ventusd) and GUI: paths, protocol, fan-curve math."""

import json
import os
import socket

VERSION = "0.1"
APP_ID = "io.ventus.Ventus"

# Overridable for testing against a fake sysfs tree without root.
SOCKET_PATH = os.environ.get("VENTUS_SOCKET", "/run/ventus/ventusd.sock")
CONFIG_PATH = os.environ.get("VENTUS_CONFIG", "/etc/ventus/config.json")

FAN_MODES = ("auto", "curve", "max")

# Curve limits. Temperatures are the control temperature (max of CPU and GPU), in C.
CURVE_T_MIN, CURVE_T_MAX = 30, 100
CURVE_MIN_POINTS, CURVE_MAX_POINTS = 2, 8
# Fans run at 100% whenever the control temperature stays at/above this, whatever the curve says.
FORCE_FULL_TEMP = 95

# CPU sustained power limit (firmware/MMIO PL1) the user may pick, in W.
POWER_MIN_W, POWER_MAX_W = 45, 90

DEFAULT_CURVE = [[40, 20], [55, 35], [65, 50], [75, 65], [85, 85], [92, 100]]
DEFAULT_CONFIG = {
    "fan_mode": "auto",       # start with HP's own behaviour until the user picks
    "curve": DEFAULT_CURVE,
    "power_limit_w": 55,      # HP's firmware default on this laptop
    "allowed_uids": [],       # besides root; filled in by the installer
}


def curve_percent(curve, temp):
    """Fan percent for a temperature: linear between points, flat beyond the ends."""
    if temp <= curve[0][0]:
        return float(curve[0][1])
    for (t0, p0), (t1, p1) in zip(curve, curve[1:]):
        if temp <= t1:
            return p0 + (p1 - p0) * (temp - t0) / (t1 - t0)
    return float(curve[-1][1])


def validate_curve(curve):
    """Return a cleaned copy of the curve as [[int temp, int pct], ...] or raise ValueError."""
    if not isinstance(curve, list) or not CURVE_MIN_POINTS <= len(curve) <= CURVE_MAX_POINTS:
        raise ValueError(f"curve needs {CURVE_MIN_POINTS}-{CURVE_MAX_POINTS} points")
    out = []
    for pt in curve:
        if not isinstance(pt, (list, tuple)) or len(pt) != 2:
            raise ValueError("each point must be [temp, percent]")
        t, p = pt
        if isinstance(t, bool) or isinstance(p, bool) or not isinstance(t, (int, float)) \
                or not isinstance(p, (int, float)):
            raise ValueError("temp and percent must be numbers")
        t, p = int(round(t)), int(round(p))
        if not CURVE_T_MIN <= t <= CURVE_T_MAX:
            raise ValueError(f"temperatures must be {CURVE_T_MIN}-{CURVE_T_MAX} C")
        if not 0 <= p <= 100:
            raise ValueError("percent must be 0-100")
        out.append([t, p])
    for (t0, p0), (t1, p1) in zip(out, out[1:]):
        if t1 <= t0:
            raise ValueError("temperatures must increase from point to point")
        if p1 < p0:
            raise ValueError("fan speed must not drop as temperature rises")
    return out


def validate_power(watts):
    if isinstance(watts, bool) or not isinstance(watts, (int, float)):
        raise ValueError("power limit must be a number")
    w = int(round(watts))
    if not POWER_MIN_W <= w <= POWER_MAX_W:
        raise ValueError(f"power limit must be {POWER_MIN_W}-{POWER_MAX_W} W")
    return w


def request(obj, timeout=0.5, path=None):
    """Send one JSON request to ventusd and return the decoded reply (raises OSError on failure)."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(path or SOCKET_PATH)
        s.sendall(json.dumps(obj).encode() + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf.decode())
