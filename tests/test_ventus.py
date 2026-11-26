"""Tests for Ventus. No root, no real hardware: the daemon runs against a fake sysfs tree.
Run:  /usr/bin/python3 -m unittest discover -s tests -v      (from the ventus directory)
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.dirname(HERE)
sys.path.insert(0, SRC)
import common as C  # noqa: E402


class CurveMath(unittest.TestCase):
    def test_interpolation_and_ends(self):
        curve = [[40, 20], [60, 40], [80, 100]]
        self.assertEqual(C.curve_percent(curve, 30), 20)
        self.assertEqual(C.curve_percent(curve, 50), 30)
        self.assertEqual(C.curve_percent(curve, 70), 70)
        self.assertEqual(C.curve_percent(curve, 99), 100)

    def test_validation(self):
        self.assertEqual(C.validate_curve([[40.4, 20.6], [60, 40]]), [[40, 21], [60, 40]])
        for bad in ([[40, 20]], [[60, 20], [40, 30]], [[40, 50], [60, 40]], [[40, 120], [60, 130]],
                    [[10, 20], [60, 40]], "x", [[40, True], [60, 40]], [[40, 20, 1], [60, 40]]):
            with self.assertRaises(ValueError, msg=bad):
                C.validate_curve(bad)
        self.assertEqual(C.validate_power(75), 75)
        for bad in (30, 200, "75", True):
            with self.assertRaises(ValueError):
                C.validate_power(bad)

    def test_default_curve_valid(self):
        C.validate_curve(C.DEFAULT_CURVE)


def write(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(str(value))


def read(path):
    with open(path) as f:
        return f.read().strip()


class FakeLaptop:
    """Builds a fake sysfs tree shaped like the OMEN's."""

    def __init__(self, base, allowed_uids):
        self.base = base
        r = self.root = os.path.join(base, "root")
        self.hp = f"{r}/sys/devices/platform/hp-wmi/hwmon/hwmon5"
        self.core = f"{r}/sys/class/hwmon/hwmon7"
        self.rapl = f"{r}/sys/class/powercap/intel-rapl:0"
        self.mmio = f"{r}/sys/class/powercap/intel-rapl-mmio:0"
        for k, v in dict(name="hp", pwm1_enable=2, pwm1=140, fan1_input=3200, fan2_input=3500).items():
            write(f"{self.hp}/{k}", v)
        write(f"{self.core}/name", "coretemp")
        self.temp(65)
        write(f"{self.rapl}/energy_uj", 1_000_000)
        write(f"{self.rapl}/max_energy_range_uj", 262143328850)
        write(f"{self.rapl}/constraint_0_power_limit_uw", 130_000_000)
        write(f"{self.rapl}/constraint_1_power_limit_uw", 130_000_000)
        write(f"{self.mmio}/constraint_0_power_limit_uw", 55_000_000)
        write(f"{self.mmio}/constraint_1_power_limit_uw", 130_000_000)
        write(f"{r}/sys/devices/cpu_core/cpus", "0-15")
        write(f"{r}/sys/devices/cpu_atom/cpus", "16-27")
        for c in range(28):
            write(f"{r}/sys/devices/system/cpu/cpu{c}/cpufreq/scaling_cur_freq", 2_000_000)
        gpu = f"{r}/sys/bus/pci/devices/0000:01:00.0"
        write(f"{gpu}/vendor", "0x10de")
        write(f"{gpu}/class", "0x030000")
        write(f"{gpu}/power/runtime_status", "active")
        write(f"{r}/sys/firmware/acpi/platform_profile", "performance")
        write(f"{r}/sys/class/power_supply/ACAD/online", "1")
        self.smi = os.path.join(base, "nvidia-smi")
        write(self.smi, "#!/bin/sh\necho '48, 0'\n")
        os.chmod(self.smi, 0o755)
        self.sock = os.path.join(base, "run", "ventusd.sock")
        self.config = os.path.join(base, "etc", "config.json")
        write(self.config, json.dumps({"allowed_uids": allowed_uids}))

    def temp(self, c):
        write(f"{self.core}/temp1_input", int(c * 1000))

    def env(self):
        return dict(os.environ, VENTUS_SYSFS_ROOT=self.root, VENTUS_SOCKET=self.sock,
                    VENTUS_CONFIG=self.config, VENTUS_NVIDIA_SMI=self.smi, VENTUS_CLAMP_WAIT="1",
                    PYTHONDONTWRITEBYTECODE="1")


class DaemonOnFakeLaptop(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="ventus-test-")
        self.fake = FakeLaptop(self.base, [os.getuid()])
        self.log = open(os.path.join(self.base, "daemon.log"), "w")
        self.proc = subprocess.Popen([sys.executable, "-sE", os.path.join(SRC, "ventusd.py")],
                                     env=self.fake.env(), stdout=self.log, stderr=subprocess.STDOUT)
        self.wait(lambda: self.status() and not self.status().get("starting"), 8, "daemon start")

    def tearDown(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        self.log.close()
        if os.environ.get("VENTUS_KEEP"):
            print("kept", self.base)
        else:
            shutil.rmtree(self.base)

    def status(self):
        try:
            return C.request({"cmd": "status"}, path=self.fake.sock)
        except OSError:
            return None

    def set(self, **kw):
        return C.request({"cmd": "set", **kw}, path=self.fake.sock)

    def wait(self, cond, timeout, what):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return
            time.sleep(0.2)
        with open(os.path.join(self.base, "daemon.log")) as f:
            log = f.read()
        self.fail(f"timed out waiting for {what}\n--- daemon log ---\n{log}")

    def enable(self):
        return read(f"{self.fake.hp}/pwm1_enable")

    def test_full_flow(self):
        f = self.fake
        s = self.status()
        self.assertEqual(s["mode"], "auto")
        self.assertEqual(self.enable(), "2")
        self.assertEqual(s["gpu_temp"], 48.0)
        self.assertEqual(s["p_mhz"], 2000)
        self.assertEqual(read(f"{f.mmio}/constraint_0_power_limit_uw"), "55000000")

        # curve mode: manual fans, pwm from the curve (65 C -> 50 % -> 128)
        self.assertTrue(self.set(fan_mode="curve")["ok"])
        self.wait(lambda: self.enable() == "1", 4, "manual mode")
        self.wait(lambda: read(f"{f.hp}/pwm1") == "128", 4, "pwm 128")
        with open(f.config) as fh:
            self.assertEqual(json.load(fh)["fan_mode"], "curve")

        # hotter: fans ramp up (at most +10 %/s), towards 85 C -> 85 %
        f.temp(85)
        self.wait(lambda: int(read(f"{f.hp}/pwm1")) > 160, 6, "ramp up")

        # power limit
        self.assertTrue(self.set(power_limit_w=75)["ok"])
        self.wait(lambda: read(f"{f.mmio}/constraint_0_power_limit_uw") == "75000000", 4, "PL1 75 W")

        # bad input rejected, nothing changed
        r = self.set(curve=[[50, 80], [60, 10]])
        self.assertFalse(r["ok"])
        self.assertIn("must not drop", r["error"])
        self.assertFalse(self.set(power_limit_w=500)["ok"])
        self.assertFalse(self.set(fan_mode="turbo")["ok"])

        # fans reading 0 RPM while hot: Ventus leaves the chosen mode alone
        write(f"{f.hp}/fan1_input", 0)
        write(f"{f.hp}/fan2_input", 0)
        time.sleep(4)
        self.assertEqual(self.enable(), "1")
        self.assertEqual(self.status()["effective_mode"], "curve")
        write(f"{f.hp}/fan1_input", 4000)
        write(f"{f.hp}/fan2_input", 4300)

        # CPU temperature unreadable in curve mode -> max
        os.unlink(f"{f.core}/temp1_input")
        self.wait(lambda: self.enable() == "0", 4, "max on sensor failure")
        f.temp(60)
        self.wait(lambda: self.enable() == "1", 4, "curve again")

        # someone else flips the mode (e.g. driver reload resets to auto) -> re-asserted
        write(f"{f.hp}/pwm1_enable", 2)
        self.wait(lambda: self.enable() == "1", 4, "re-assert manual")

        # stop: fans back to auto, HP's power limit restored, socket gone
        self.proc.send_signal(signal.SIGTERM)
        self.proc.wait(5)
        self.assertEqual(self.enable(), "2")
        self.assertEqual(read(f"{f.mmio}/constraint_0_power_limit_uw"), "55000000")
        self.assertFalse(os.path.exists(f.sock))

    def test_handover_to_hp_auto(self):
        f = self.fake
        pwm = lambda: int(read(f"{f.hp}/pwm1"))  # noqa: E731
        self.assertIsNone(self.status()["hp_takeover_s"])

        # curve running slowly (65 C -> pwm 128): hand over at the floor of 176
        self.assertTrue(self.set(fan_mode="curve")["ok"])
        self.wait(lambda: pwm() == 128, 4, "pwm 128")
        self.assertTrue(self.set(fan_mode="auto")["ok"])
        self.wait(lambda: self.enable() == "2", 4, "auto")
        self.assertEqual(pwm(), 176)
        self.wait(lambda: self.status()["hp_takeover_s"] is not None, 3, "countdown")
        self.assertTrue(115 <= self.status()["hp_takeover_s"] <= 125)

        # curve running fast: keep its speed
        f.temp(85)
        self.assertTrue(self.set(fan_mode="curve")["ok"])
        self.wait(lambda: self.enable() == "1" and pwm() >= 200, 8, "fast curve")
        self.assertIsNone(self.status()["hp_takeover_s"])
        fast = pwm()
        self.assertTrue(self.set(fan_mode="auto")["ok"])
        self.wait(lambda: self.enable() == "2", 4, "auto")
        self.assertGreaterEqual(pwm(), fast)

        # from max: the curve's speed at the current temperature (85 C -> 85 %), not full speed
        self.assertTrue(self.set(fan_mode="max")["ok"])
        self.wait(lambda: self.enable() == "0", 4, "max")
        self.assertTrue(self.set(fan_mode="auto")["ok"])
        self.wait(lambda: self.enable() == "2", 4, "auto")
        self.assertEqual(pwm(), 217)

        # from max while cool: the 176 floor
        f.temp(40)
        self.assertTrue(self.set(fan_mode="max")["ok"])
        self.wait(lambda: self.enable() == "0", 4, "max")
        self.assertTrue(self.set(fan_mode="auto")["ok"])
        self.wait(lambda: self.enable() == "2", 4, "auto")
        self.assertEqual(pwm(), 176)

    def test_battery_limit_put_back_after_power_cut(self):
        f = self.fake
        ac = f"{f.root}/sys/class/power_supply/ACAD/online"
        profile = f"{f.root}/sys/firmware/acpi/platform_profile"

        def msr():
            return (int(read(f"{f.rapl}/constraint_0_power_limit_uw")) // 1_000_000,
                    int(read(f"{f.rapl}/constraint_1_power_limit_uw")) // 1_000_000)

        def firmware_sets(pl1, pl2):
            write(f"{f.rapl}/constraint_0_power_limit_uw", pl1 * 1_000_000)
            write(f"{f.rapl}/constraint_1_power_limit_uw", pl2 * 1_000_000)

        def restores():
            with open(os.path.join(self.base, "daemon.log")) as fh:
                return fh.read().count("left over from running on battery")

        # power cut: the firmware's battery limit is left alone while on battery
        write(ac, 0)
        firmware_sets(24, 30)
        time.sleep(3)
        self.assertEqual(msr(), (24, 30))
        # charger back: the Performance profile's own 130/130 W comes back
        write(ac, 1)
        self.wait(lambda: msr() == (130, 130), 6, "130/130 W after the charger is back")
        self.assertEqual(restores(), 1)
        # Balanced: restored to Balanced's 55/130, never more
        write(profile, "balanced")
        firmware_sets(55, 130)
        time.sleep(1.5)
        firmware_sets(24, 30)
        self.wait(lambda: msr() == (55, 130), 8, "55/130 W in balanced")
        # a limit above the profile's value (thermald writes PL1 200 W at boot) is not touched
        write(profile, "performance")
        firmware_sets(200, 130)
        time.sleep(3)
        self.assertEqual(msr(), (200, 130))
        self.assertEqual(restores(), 2)

        # the firmware keeps lowering it again: 3 tries, then stop and say so in the GUI
        write(ac, 0)
        time.sleep(1.5)
        write(ac, 1)
        end = time.time() + 16
        while time.time() < end:
            if msr() != (24, 30):
                firmware_sets(24, 30)
            time.sleep(0.1)
        self.assertEqual(restores(), 5)
        self.assertIn("stuck at 24/30 W", self.status()["message"])

    def test_gpu_asleep_not_polled(self):
        write(f"{self.fake.root}/sys/bus/pci/devices/0000:01:00.0/power/runtime_status", "suspended")
        write(self.fake.smi, "#!/bin/sh\ntouch \"$0.called\"\necho '48, 0'\n")
        time.sleep(3)
        s = self.status()
        self.assertIsNone(s["gpu_temp"])
        self.assertFalse(s["gpu_active"])
        if os.path.exists(self.fake.smi + ".called"):
            os.unlink(self.fake.smi + ".called")
        time.sleep(3)
        self.assertFalse(os.path.exists(self.fake.smi + ".called"), "nvidia-smi ran while GPU asleep")


class Permissions(unittest.TestCase):
    def test_other_users_can_read_but_not_change(self):
        base = tempfile.mkdtemp(prefix="ventus-test-")
        try:
            fake = FakeLaptop(base, [])
            os.environ.update(fake.env())
            import importlib
            import ventusd
            importlib.reload(C)          # pick up the fake paths from the environment
            importlib.reload(ventusd)
            self.assertTrue(C.CONFIG_PATH.startswith(base))
            d = ventusd.Daemon()
            self.assertFalse(d.handle({"cmd": "set", "fan_mode": "max"}, uid=12345)["ok"])
            self.assertEqual(d.cfg["fan_mode"], "auto")
            self.assertTrue(d.handle({"cmd": "status"}, uid=12345)["ok"])
            self.assertTrue(d.handle({"cmd": "set", "fan_mode": "max"}, uid=0)["ok"])
            self.assertFalse(d.handle({"cmd": "nope"}, uid=0)["ok"])
        finally:
            for k in ("VENTUS_SYSFS_ROOT", "VENTUS_SOCKET", "VENTUS_CONFIG", "VENTUS_NVIDIA_SMI"):
                os.environ.pop(k, None)
            import importlib
            importlib.reload(C)
            shutil.rmtree(base)


if __name__ == "__main__":
    unittest.main()
