#!/usr/bin/python3 -sE
"""ventusd - fan curve and CPU power-limit service for the HP OMEN 16 (board 8CC0).

Why it exists: in fan AUTO mode HP's EC drops into a fallback state that caps the CPU at ~45 W.
In fan MANUAL mode the hp-wmi driver keeps the EC in its "user-defined" state (keep-alive every
90 s) and the CPU gets its full limit, but then nothing adjusts the fans. ventusd runs the fans in
manual mode from a temperature curve, and sets the firmware (MMIO) sustained CPU power limit.

After a power cut HP's firmware leaves the CPU's own (MSR) power limit at its battery value
(24/30 W) even once the charger is back; ventusd puts the power profile's value back.

HP's EC holds the last fan speed for ~2 min after the fans go back to auto (while the CPU keeps its
full limit), so ventusd hands over at no less than ~4000 rpm.

Fail-safes: any exit puts the fans back to HP auto (also done by systemd ExecStopPost);
unreadable CPU temperature -> fans max; fans reading 0 RPM while hot -> fans max for 60 s;
control temperature >= 95 C for 3 s -> 100 %.

Talks to the GUI over a Unix socket (JSON lines). Anyone may read status; changes are allowed
for root and the uids in config "allowed_uids" (checked with SO_PEERCRED).
"""

import glob
import json
import os
import signal
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402

ROOT = os.environ.get("VENTUS_SYSFS_ROOT", "")          # prefix, for tests against a fake tree
NVIDIA_SMI = os.environ.get("VENTUS_NVIDIA_SMI", "/usr/bin/nvidia-smi")
RUN_DIR = os.path.dirname(C.SOCKET_PATH)
ORIG_PL1_FILE = os.path.join(RUN_DIR, "firmware_pl1_uw")  # HP's value, remembered until reboot
TICK = 1.0
ENABLE_FOR_MODE = {"auto": 2, "curve": 1, "max": 0}       # hwmon pwm1_enable values

# Handing the fans back to HP: the driver's "auto" command does nothing in the firmware, so the EC
# keeps the last fan speed until its user-defined state times out, ~2 min after the last fan
# command (123 s measured). The CPU keeps its full power limit until then. So never hand over
# below this speed: pwm 176 = 4000/4300 rpm on 8CC0, about what HP auto runs under load.
HANDOVER_PWM = 176
HP_TAKEOVER_S = 125

# CPU (MSR) PL1/PL2 in W that HP's firmware sets for each power profile when on AC (measured on
# 8CC0). On battery it lowers them to 24/30 W and doesn't raise them again when the charger comes
# back. ventusd restores these values, never anything higher.
PROFILE_MSR_W = {"performance": (130, 130), "balanced": (55, 130), "low-power": (55, 130)}
# Seconds on AC before restoring (the firmware applies its battery limit ~3 s after the switch).
# The limit must also stay low for half of this, with at least 3x this between tries.
CLAMP_WAIT = float(os.environ.get("VENTUS_CLAMP_WAIT", "10"))
CLAMP_TRIES = 3                                           # then stop fighting, and say so


def log(msg):
    print(msg, flush=True)


def rd(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def rd_int(path):
    v = rd(path)
    try:
        return int(v) if v is not None else None
    except ValueError:
        return None


def wr(path, value):
    with open(path, "w") as f:
        f.write(str(value))


def parse_cpulist(s):
    cpus = []
    for part in (s or "").split(","):
        if "-" in part:
            a, b = part.split("-")
            cpus.extend(range(int(a), int(b) + 1))
        elif part.strip():
            cpus.append(int(part))
    return cpus


def sd_notify(msg):
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(msg.encode())
    except OSError:
        pass


# ---------------------------------------------------------------- config

def load_config():
    cfg = json.loads(json.dumps(C.DEFAULT_CONFIG))
    try:
        with open(C.CONFIG_PATH) as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return cfg
    if raw.get("fan_mode") in C.FAN_MODES:
        cfg["fan_mode"] = raw["fan_mode"]
    try:
        cfg["curve"] = C.validate_curve(raw.get("curve"))
    except ValueError:
        pass
    try:
        cfg["power_limit_w"] = C.validate_power(raw.get("power_limit_w"))
    except ValueError:
        pass
    uids = raw.get("allowed_uids", [])
    if isinstance(uids, list):
        cfg["allowed_uids"] = [u for u in uids if isinstance(u, int) and not isinstance(u, bool)]
    return cfg


def save_config(cfg):
    d = os.path.dirname(C.CONFIG_PATH)
    os.makedirs(d, mode=0o755, exist_ok=True)
    tmp = C.CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, C.CONFIG_PATH)


# ---------------------------------------------------------------- hardware

class Hardware:
    def __init__(self):
        self.rapl = ROOT + "/sys/class/powercap/intel-rapl:0"
        self.mmio = ROOT + "/sys/class/powercap/intel-rapl-mmio:0"
        self.hp = self.coretemp = self.gpu = None
        self.p_cpus, self.e_cpus = [], []
        self._energy = None
        self._gpu_temp, self._gpu_time, self._gpu_idle_polls = None, 0.0, 0
        self.resolve()

    def resolve(self):
        self.hp = None
        for d in sorted(glob.glob(ROOT + "/sys/devices/platform/hp-wmi/hwmon/hwmon*")):
            if rd(d + "/name") == "hp" and os.path.exists(d + "/pwm1_enable"):
                self.hp = d
                break
        self.coretemp = None
        for d in sorted(glob.glob(ROOT + "/sys/class/hwmon/hwmon*")):
            if rd(d + "/name") == "coretemp":
                self.coretemp = d
                break
        self.gpu = None
        for d in sorted(glob.glob(ROOT + "/sys/bus/pci/devices/*")):
            if rd(d + "/vendor") == "0x10de" and (rd(d + "/class") or "").startswith("0x03"):
                self.gpu = d
                break
        self.p_cpus = parse_cpulist(rd(ROOT + "/sys/devices/cpu_core/cpus"))
        self.e_cpus = parse_cpulist(rd(ROOT + "/sys/devices/cpu_atom/cpus"))
        if not self.p_cpus:
            n = len(glob.glob(ROOT + "/sys/devices/system/cpu/cpu[0-9]*"))
            self.p_cpus = list(range(n))

    def cpu_temp(self):
        v = rd_int(self.coretemp + "/temp1_input") if self.coretemp else None
        return v / 1000 if v is not None else None

    def gpu_active(self):
        return bool(self.gpu) and rd(self.gpu + "/power/runtime_status") == "active"

    def gpu_temp(self):
        """GPU temperature via nvidia-smi - only while the GPU is already awake (never wakes it),
        every 3 s while it's busy, and only every 30 s once it's idle, so our polling doesn't
        keep an idle GPU from powering down."""
        if not self.gpu_active():
            self._gpu_temp, self._gpu_idle_polls = None, 0
            return None
        now = time.monotonic()
        interval = 30 if self._gpu_idle_polls >= 3 else 3
        if now - self._gpu_time >= interval:
            self._gpu_time = now
            try:
                out = subprocess.run([NVIDIA_SMI, "--query-gpu=temperature.gpu,utilization.gpu",
                                      "--format=csv,noheader,nounits"],
                                     capture_output=True, text=True, timeout=2)
                temp, util = (float(x) for x in out.stdout.split(",")[:2])
                self._gpu_temp = temp
                self._gpu_idle_polls = self._gpu_idle_polls + 1 if util < 1 else 0
            except (OSError, subprocess.SubprocessError, ValueError):
                self._gpu_temp = None
        return self._gpu_temp

    def fans(self):
        if not self.hp:
            return None, None
        return rd_int(self.hp + "/fan1_input"), rd_int(self.hp + "/fan2_input")

    def pwm_enable(self):
        return rd_int(self.hp + "/pwm1_enable") if self.hp else None

    def set_pwm_enable(self, v):
        wr(self.hp + "/pwm1_enable", v)

    def set_pwm(self, v):
        wr(self.hp + "/pwm1", v)

    def package_watts(self):
        e = rd_int(self.rapl + "/energy_uj")
        now = time.monotonic()
        prev, self._energy = self._energy, (e, now) if e is not None else None
        if e is None or prev is None or now <= prev[1]:
            return None
        de = e - prev[0]
        if de < 0:
            de += rd_int(self.rapl + "/max_energy_range_uj") or 0
        return de / 1e6 / (now - prev[1])

    def mhz(self, cpus):
        vals = [rd_int(f"{ROOT}/sys/devices/system/cpu/cpu{c}/cpufreq/scaling_cur_freq") for c in cpus]
        vals = [v for v in vals if v]
        return round(sum(vals) / len(vals) / 1000) if vals else None

    def limits(self):
        def w(p):
            v = rd_int(p)
            return round(v / 1e6) if v is not None else None
        return {"msr_pl1_w": w(self.rapl + "/constraint_0_power_limit_uw"),
                "msr_pl2_w": w(self.rapl + "/constraint_1_power_limit_uw"),
                "fw_pl1_w": w(self.mmio + "/constraint_0_power_limit_uw"),
                "fw_pl2_w": w(self.mmio + "/constraint_1_power_limit_uw")}

    def set_msr_limits_w(self, pl1, pl2):
        wr(self.rapl + "/constraint_1_power_limit_uw", pl2 * 1_000_000)
        wr(self.rapl + "/constraint_0_power_limit_uw", pl1 * 1_000_000)

    def profile(self):
        return rd(ROOT + "/sys/firmware/acpi/platform_profile")

    def on_ac(self):
        return rd(ROOT + "/sys/class/power_supply/ACAD/online") == "1"

    def fw_pl1_uw(self):
        return rd_int(self.mmio + "/constraint_0_power_limit_uw")

    def set_fw_pl1_uw(self, uw):
        wr(self.mmio + "/constraint_0_power_limit_uw", uw)


# ---------------------------------------------------------------- fan curve logic

class FanLogic:
    """Smoothed curve follower: quick to speed up, slow to slow down."""

    def __init__(self):
        self.ema = None
        self.pct = None
        self.hot_ticks = 0

    def reset(self):
        self.pct = None

    def step(self, curve, temp):
        self.ema = temp if self.ema is None else self.ema + 0.35 * (temp - self.ema)
        self.hot_ticks = self.hot_ticks + 1 if temp >= C.FORCE_FULL_TEMP else 0
        if self.hot_ticks >= 3:
            self.pct = 100.0
            return self.pct
        up = C.curve_percent(curve, self.ema)
        down = C.curve_percent(curve, self.ema + 2)       # 2 C hysteresis before slowing down
        if self.pct is None:
            self.pct = up
        elif up > self.pct:
            self.pct = min(up, self.pct + 10)              # up to +10 %/s
        elif down < self.pct:
            self.pct = max(down, self.pct - 2)             # down at most -2 %/s
        return self.pct


# ---------------------------------------------------------------- daemon

class Daemon:
    def __init__(self):
        self.lock = threading.Lock()
        self.cfg = load_config()
        self.hw = Hardware()
        self.fan = FanLogic()
        self.status = {"ok": True, "starting": True}
        self.running = True
        self.dirty = True                 # (re)apply the fan mode on the next tick
        self.emergency_until = 0.0
        self.zero_ticks = 0
        self.last_pwm = None
        self.last_pwm_write = 0.0
        self.handover_at = None           # when the fans were last handed back to HP auto
        self.last_tick = None
        self.last_resolve = 0.0
        self.orig_pl1 = self._remember_firmware_pl1()
        self.ac_since = None              # when the charger was (last) seen plugged in
        self.clamp_since = None           # when the CPU limit was first seen below the profile's
        self.clamp_profile = None
        self.clamp_tries = 0
        self.last_restore = -1e9

    def _remember_firmware_pl1(self):
        v = rd_int(ORIG_PL1_FILE)
        if v is None:
            v = self.hw.fw_pl1_uw()
            if v is not None:
                try:
                    os.makedirs(RUN_DIR, exist_ok=True)
                    wr(ORIG_PL1_FILE, v)
                except OSError:
                    pass
        return v

    # -- one control cycle
    def tick(self):
        now = time.monotonic()
        boot = time.clock_gettime(time.CLOCK_BOOTTIME)   # keeps counting through suspend
        if self.last_tick is not None and boot - self.last_tick > 5 * TICK:
            log("resumed from suspend (or stalled) - re-applying fan mode")
            self.dirty = True
        self.last_tick = boot
        if (not self.hw.hp or not self.hw.coretemp) and now - self.last_resolve > 5:
            self.last_resolve = now
            self.hw.resolve()

        with self.lock:
            cfg = json.loads(json.dumps(self.cfg))

        cpu = self.hw.cpu_temp()
        gpu = self.hw.gpu_temp()
        f1, f2 = self.hw.fans()
        control = max(t for t in (cpu, gpu) if t is not None) if (cpu or gpu) else None
        msg = ""

        # fans reading 0 RPM while hot is never right (seen on 8CC0 after a bad auto reset)
        hot = cpu is not None and cpu >= 80
        if self.hw.hp and f1 == 0 and f2 == 0 and hot:
            self.zero_ticks += 1
        else:
            self.zero_ticks = 0
        if self.zero_ticks >= 3 and now >= self.emergency_until:
            log(f"fans at 0 RPM at {cpu:.0f} C - forcing max for 60 s")
            self.emergency_until = now + 60
            self.dirty = True

        mode = cfg["fan_mode"]
        if now < self.emergency_until:
            mode = "max"
            msg = "Fans were stopped while hot - running them at full speed for a minute."
        elif cpu is None and mode == "curve":
            mode = "max"
            msg = "CPU temperature unreadable - fans at full speed."

        target_pct = None
        if self.hw.hp:
            try:
                want = ENABLE_FOR_MODE[mode]
                cur = self.hw.pwm_enable()
                if self.dirty or cur != want:
                    if mode == "auto":
                        try:
                            if self.hand_fans_to_hp(cur):
                                self.handover_at = now
                        except OSError as e:          # still hand over, just without the speed
                            log(f"could not set the handover fan speed: {e}")
                    self.hw.set_pwm_enable(want)
                    log(f"fan mode -> {mode}")
                    self.dirty = False
                    self.fan.reset()
                    self.last_pwm = None
                if mode == "curve":
                    target_pct = self.fan.step(cfg["curve"], control)
                    pwm = max(0, min(255, round(target_pct * 255 / 100)))
                    if self.last_pwm is None or abs(pwm - self.last_pwm) >= 4 \
                            or now - self.last_pwm_write >= 60:
                        self.hw.set_pwm(pwm)
                        self.last_pwm, self.last_pwm_write = pwm, now
            except OSError as e:
                msg = f"Fan control error: {e}"
                log(msg)
                self.hw.hp = None          # re-resolve (driver reloaded?)
        else:
            msg = msg or "Fan control unavailable (patched hp-wmi driver not loaded)."

        # firmware sustained CPU power limit
        want_uw = cfg["power_limit_w"] * 1_000_000
        cur_uw = self.hw.fw_pl1_uw()
        if cur_uw is not None and cur_uw != want_uw:
            try:
                self.hw.set_fw_pl1_uw(want_uw)
                log(f"CPU sustained power limit {cur_uw // 1_000_000} -> {cfg['power_limit_w']} W")
            except OSError as e:
                msg = msg or f"Could not set the CPU power limit: {e}"

        # CPU (MSR) limit left at the battery value after a power cut
        limits, profile, on_ac = self.hw.limits(), self.hw.profile(), self.hw.on_ac()
        msg = msg or self.undo_battery_limit(now, on_ac, profile, limits)

        status = {
            "ok": True, "version": C.VERSION,
            "fan_control": bool(self.hw.hp),
            "mode": cfg["fan_mode"], "effective_mode": mode,
            "emergency": now < self.emergency_until,
            "curve": cfg["curve"], "power_limit_w": cfg["power_limit_w"],
            "cpu_temp": cpu, "gpu_temp": gpu, "gpu_active": self.hw.gpu_active(),
            "control_temp": round(self.fan.ema, 1) if (mode == "curve" and self.fan.ema is not None) else control,
            "fan1_rpm": f1, "fan2_rpm": f2,
            "fan_target_pct": round(target_pct, 1) if target_pct is not None else None,
            "hp_takeover_s": self.takeover_left(now, mode),
            "package_w": round(w, 1) if (w := self.hw.package_watts()) is not None else None,
            "p_mhz": self.hw.mhz(self.hw.p_cpus), "e_mhz": self.hw.mhz(self.hw.e_cpus),
            "limits": limits, "profile": profile, "on_ac": on_ac,
            "message": msg,
        }
        with self.lock:
            self.status = status

    def undo_battery_limit(self, now, on_ac, profile, limits):
        """On battery HP's firmware lowers the CPU (MSR) limit to 24/30 W, and it doesn't raise it
        again when the charger comes back (every power cut leaves the CPU at 24 W until reboot).
        Once on AC, write back the value the firmware itself sets for the current profile.
        Returns a message for the GUI, or ""."""
        if not on_ac:
            self.ac_since = self.clamp_since = None
            self.clamp_tries = 0
            return ""
        if self.ac_since is None:
            self.ac_since = now
        want = PROFILE_MSR_W.get(profile)
        have = (limits.get("msr_pl1_w"), limits.get("msr_pl2_w"))
        if not want or None in have or (have[0] >= want[0] and have[1] >= want[1]):
            self.clamp_since = None
            if now - self.last_restore > 30 * CLAMP_WAIT:  # the last fix held: start counting anew
                self.clamp_tries = 0
            return ""
        if self.clamp_since is None or profile != self.clamp_profile:
            self.clamp_since, self.clamp_profile = now, profile
        if now - self.ac_since < CLAMP_WAIT or now - self.clamp_since < CLAMP_WAIT / 2:
            return ""
        if self.clamp_tries >= CLAMP_TRIES:
            return (f"The CPU power limit is stuck at {have[0]}/{have[1]} W and the firmware keeps "
                    "lowering it again. Switching the power profile or rebooting may help.")
        if now - self.last_restore < 3 * CLAMP_WAIT:
            return ""
        try:
            self.hw.set_msr_limits_w(*want)
        except OSError as e:
            return f"Could not restore the CPU power limit: {e}"
        self.clamp_tries += 1
        self.last_restore, self.clamp_since = now, None
        log(f"CPU limit {have[0]}/{have[1]} W left over from running on battery -> restored the "
            f"{profile} profile's {want[0]}/{want[1]} W (try {self.clamp_tries})")
        return ""

    def hand_fans_to_hp(self, cur):
        """Before switching to HP auto from curve or max: set the speed the EC will hold for the
        next ~2 minutes (see HANDOVER_PWM) - the curve's speed if faster, full speed from max.
        This also restarts the EC's timer, so we know when HP takes over. True if done."""
        if cur not in (ENABLE_FOR_MODE["curve"], ENABLE_FOR_MODE["max"]):
            return False                   # already auto (or unknown): the EC isn't holding a speed
        pwm = 255 if cur == ENABLE_FOR_MODE["max"] else max(self.last_pwm or 0, HANDOVER_PWM)
        if cur != ENABLE_FOR_MODE["curve"]:
            self.hw.set_pwm_enable(ENABLE_FOR_MODE["curve"])   # pwm1 is only writable in manual
        self.hw.set_pwm(pwm)
        log(f"handing the fans to HP auto at pwm {pwm} (held ~2 min until HP takes over)")
        return True

    def takeover_left(self, now, mode):
        """Seconds until HP's EC takes the fans back (and caps the CPU again), or None."""
        if mode != "auto" or self.handover_at is None:
            return None
        left = HP_TAKEOVER_S - (now - self.handover_at)
        return round(left) if left > 0 else None

    def cleanup(self):
        log("stopping - fans back to HP auto, firmware power limit restored")
        if self.hw.hp:
            try:
                self.hand_fans_to_hp(self.hw.pwm_enable())
            except OSError as e:
                log(f"could not set the handover fan speed: {e}")
            try:
                self.hw.set_pwm_enable(ENABLE_FOR_MODE["auto"])
            except OSError as e:
                log(f"could not restore fan auto: {e}")
        if self.orig_pl1 is not None:
            try:
                self.hw.set_fw_pl1_uw(self.orig_pl1)
            except OSError as e:
                log(f"could not restore power limit: {e}")

    # -- requests from the GUI
    def handle(self, req, uid):
        cmd = req.get("cmd")
        if cmd == "status":
            with self.lock:
                return dict(self.status)
        if cmd == "set":
            if uid != 0 and uid not in self.cfg["allowed_uids"]:
                return {"ok": False, "error": "not allowed - your user isn't in allowed_uids"}
            try:
                new = {}
                if "fan_mode" in req:
                    if req["fan_mode"] not in C.FAN_MODES:
                        raise ValueError("unknown fan mode")
                    new["fan_mode"] = req["fan_mode"]
                if "curve" in req:
                    new["curve"] = C.validate_curve(req["curve"])
                if "power_limit_w" in req:
                    new["power_limit_w"] = C.validate_power(req["power_limit_w"])
            except ValueError as e:
                return {"ok": False, "error": str(e)}
            with self.lock:
                if "fan_mode" in new:          # an explicit choice also ends an emergency
                    self.dirty = True          # (it re-triggers if the fans really are stopped)
                    self.emergency_until = 0.0
                    self.zero_ticks = 0
                self.cfg.update(new)
                # reflect the change in status right away, so clients never read a stale mode
                self.status.update({k: v for k, v in new.items() if k in self.status})
                try:
                    save_config(self.cfg)
                except OSError as e:
                    log(f"could not save config: {e}")
            log(f"settings changed by uid {uid}: {new}")
            return {"ok": True}
        return {"ok": False, "error": "unknown command"}

    def run(self):
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "running", False))
        signal.signal(signal.SIGINT, lambda *_: setattr(self, "running", False))
        server = start_server(self)
        log(f"ventusd {C.VERSION} started; hp hwmon: {self.hw.hp}; mode {self.cfg['fan_mode']}, "
            f"{self.cfg['power_limit_w']} W")
        sd_notify("READY=1")
        try:
            while self.running:
                t0 = time.monotonic()
                try:
                    self.tick()
                except Exception as e:  # never let one bad cycle kill fan control
                    log(f"tick error: {e!r}")
                sd_notify("WATCHDOG=1")
                time.sleep(max(0.05, TICK - (time.monotonic() - t0)))
        finally:
            server.shutdown()
            server.server_close()
            try:
                os.unlink(C.SOCKET_PATH)
            except OSError:
                pass
            self.cleanup()


def start_server(daemon):
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            try:
                creds = self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                                struct.calcsize("3i"))
                _, uid, _ = struct.unpack("3i", creds)
                line = self.rfile.readline(65536)
                req = json.loads(line.decode())
                reply = daemon.handle(req if isinstance(req, dict) else {}, uid)
            except (ValueError, OSError) as e:
                reply = {"ok": False, "error": f"bad request: {e}"}
            try:
                self.wfile.write(json.dumps(reply).encode() + b"\n")
            except OSError:
                pass

    class Server(socketserver.ThreadingUnixStreamServer):
        daemon_threads = True

    os.makedirs(RUN_DIR, mode=0o755, exist_ok=True)
    try:
        os.unlink(C.SOCKET_PATH)
    except FileNotFoundError:
        pass
    server = Server(C.SOCKET_PATH, Handler)
    os.chmod(C.SOCKET_PATH, 0o666)   # access control is per request, via SO_PEERCRED
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":
    Daemon().run()
