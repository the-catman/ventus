#!/usr/bin/python3 -sE
"""ventus-bench - a small CPU benchmark for comparing fan modes / power limits / profiles.

Usage:  ventus-bench [label]        run (about 2-3 min) and add the result to the comparison table
        ventus-bench --list         just show the table of earlier runs
        ventus-bench --clear        forget earlier runs

Workload: `openssl speed` SHA-512 (plain integer crunching, like compiling or fuzzing).
  1. single thread, 10 s
  2. all cores, 9 x 10 s: the first slice shows the short burst (PL2), slices 5-9 the
     sustained speed (after the ~32 s burst window), which is what the power limit controls.
Readings (package watts, temps, fans) come from ventusd if it's running; no root needed.
"""

import csv
import os
import re
import resource
import subprocess
import sys
import threading
import time
from datetime import datetime

for d in ("/opt/ventus", os.path.dirname(os.path.abspath(__file__))):
    if os.path.exists(os.path.join(d, "common.py")):
        sys.path.insert(0, d)
        break
import common as C  # noqa: E402

RESULTS = os.environ.get("VENTUS_BENCH_RESULTS",
                         os.path.expanduser("~/.local/share/ventus/bench-results.csv"))
FIELDS = ["when", "label", "mode", "limit_w", "profile", "busy_pct", "others_pct", "single_mbs",
          "burst_mbs", "sustained_mbs", "sustained_w", "max_c", "fan_rpm", "p_ghz", "e_ghz"]
SLICES, SLICE_S = 9, 10
SUSTAINED_FROM = 4          # slices 5-9 (index 4..8): after 40 s
SINGLE_S = 10
if os.environ.get("VENTUS_BENCH_QUICK"):   # for testing the script itself only
    SLICES, SLICE_S, SUSTAINED_FROM, SINGLE_S = 2, 2, 1, 2
NCPU = os.cpu_count()


def status():
    try:
        s = C.request({"cmd": "status"}, timeout=0.3)
        return s if s.get("ok") and not s.get("starting") else None
    except (OSError, ValueError):
        return None


def coretemp():
    import glob
    for d in glob.glob("/sys/class/hwmon/hwmon*"):
        try:
            if open(d + "/name").read().strip() == "coretemp":
                return int(open(d + "/temp1_input").read()) / 1000
        except OSError:
            pass
    return None


def children_cpu():
    """CPU seconds used by our finished child processes (openssl and its workers)."""
    r = resource.getrusage(resource.RUSAGE_CHILDREN)
    return r.ru_utime + r.ru_stime


def cpu_jiffies():
    v = [int(x) for x in open("/proc/stat").readline().split()[1:]]
    return v[3] + v[4], sum(v)          # idle+iowait, total


def timed_openssl(seconds, threads):
    """Run openssl; return (MB/s, how much other programs interfered, in %).
    All cores: share of our workers' CPU time other programs took.
    One thread: how busy the rest of the CPU was (a busy hyperthread sibling slows us down
    without taking any of our CPU time, so time accounting alone would miss it)."""
    c0, (i0, t0) = children_cpu(), cpu_jiffies()
    score = openssl(seconds, multi=threads if threads > 1 else None)
    c1, (i1, t1) = children_cpu(), cpu_jiffies()
    if threads > 1:
        others = 100 * (1 - (c1 - c0) / (threads * seconds))
    else:
        busy_cpus = NCPU * (1 - (i1 - i0) / max(1, t1 - t0))
        others = 100 * (busy_cpus - 1) / (NCPU - 1)
    return score, max(0.0, min(100.0, others))


def cpu_busy(seconds=2.0):
    def snap():
        v = [int(x) for x in open("/proc/stat").readline().split()[1:]]
        return v[3] + v[4], sum(v)
    i1, t1 = snap()
    time.sleep(seconds)
    i2, t2 = snap()
    return 100 * (1 - (i2 - i1) / max(1, t2 - t1))


class Sampler(threading.Thread):
    """Collects ventusd readings once a second while a phase runs."""

    def __init__(self):
        super().__init__(daemon=True)
        self.samples, self.stop_ev = [], threading.Event()

    def run(self):
        while not self.stop_ev.wait(1.0):
            s = status() or {"cpu_temp": coretemp()}
            self.samples.append(s)

    def stop(self):
        self.stop_ev.set()
        self.join()
        return self.samples


def avg(samples, key, skip=0):
    v = [s.get(key) for s in samples[skip:] if s.get(key) is not None]
    return sum(v) / len(v) if v else None


def peak(samples, key):
    v = [s.get(key) for s in samples if s.get(key) is not None]
    return max(v) if v else None


def openssl(seconds, multi=None):
    cmd = ["openssl", "speed", "-seconds", str(seconds), "-bytes", "16384", "-evp", "sha512"]
    if multi:
        cmd[2:2] = ["-multi", str(multi)]
    out = subprocess.run(cmd, capture_output=True, text=True).stdout
    m = re.findall(r"^sha512\s+([\d.]+)k\s*$", out, re.M)
    return float(m[-1]) * 1000 / 1e6 if m else None     # MB/s


def f(v, nd=0, unit=""):
    return "–" if v is None or v == "" else f"{float(v):.{nd}f}{unit}"


def warn(r):
    try:
        return " !" if float(r.get("others_pct") or 0) > 10 else ""
    except ValueError:
        return ""


def show_table():
    try:
        rows = list(csv.DictReader(open(RESULTS)))
    except OSError:
        rows = []
    if not rows:
        print("No earlier runs yet.")
        return
    print(f"\n{'when':<11} {'label':<22} {'single':>7} {'burst':>7} {'sustain':>8} {'W':>4} "
          f"{'max°C':>5} {'fan':>5} {'others':>6}")
    print(f"{'':<11} {'':<22} {'MB/s':>7} {'MB/s':>7} {'MB/s':>8}")
    best = max((float(r["sustained_mbs"] or 0) for r in rows), default=0)
    for r in rows:
        mark = " ◀ best" if best and float(r["sustained_mbs"] or 0) == best and len(rows) > 1 else ""
        print(f"{r['when'][5:16]:<11} {r['label'][:22]:<22} {f(r['single_mbs']):>7} {f(r['burst_mbs']):>7} "
              f"{f(r['sustained_mbs']):>8} {f(r['sustained_w']):>4} {f(r['max_c']):>5} "
              f"{f(r['fan_rpm']):>5} {f(r.get('others_pct'), 0, '%'):>6}{warn(r)}{mark}")
    print("\nsustain = all cores after 40 s (what the power limit controls); "
          "others = CPU time other programs took during the run (over 10% = not a fair comparison)")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    opts = {a for a in sys.argv[1:] if a.startswith("--")}
    if "--list" in opts:
        show_table()
        return
    if "--clear" in opts:
        if os.path.exists(RESULTS):
            os.unlink(RESULTS)
        print("Cleared.")
        return
    if "--help" in opts or "-h" in sys.argv:
        print(__doc__)
        return

    st = status()
    if st:
        label_auto = f"{st['mode']} {st['power_limit_w']}W {st.get('profile') or ''}".strip()
    else:
        label_auto = "no ventusd"
        print("Note: ventusd isn't running - no watts or fan readings, temperatures only.")
    label = " ".join(args) or label_auto
    print(f"Ventus bench - '{label}'")
    if st:
        print(f"  fan mode {st['mode']}, CPU power limit {st['power_limit_w']} W, "
              f"profile {st.get('profile')}, {'on AC' if st.get('on_ac') else 'ON BATTERY'}")

    busy = cpu_busy()
    if busy > 10:
        print(f"  ! other programs are using {busy:.0f}% of the CPU (fuzzers?) - "
              "results will be lower and noisier. Stop them for a fair comparison.")

    print(f"  starting at {f(coretemp(), 0)}°C")
    print("  1/2 single thread, 10 s ...", end=" ", flush=True)
    single, single_others = timed_openssl(SINGLE_S, 1)
    print(f"{f(single)} MB/s" + (f"   ! other programs kept {single_others:.0f}% of the other cores busy" if single_others > 10 else ""))
    time.sleep(3)

    print(f"  2/2 all {NCPU} threads, {SLICES} x {SLICE_S} s:")
    slice_scores, slice_samples, slice_others = [], [], []
    for i in range(SLICES):
        smp = Sampler()
        smp.start()
        score, others = timed_openssl(SLICE_S, NCPU)
        slice_others.append(others)
        samples = smp.stop()
        slice_scores.append(score)
        slice_samples.append(samples)
        w, t = avg(samples, "package_w", skip=1), peak(samples, "cpu_temp")
        rpm, p = avg(samples, "fan1_rpm"), avg(samples, "p_mhz")
        phase = "burst" if i == 0 else ("sustained" if i >= SUSTAINED_FROM else "")
        print(f"     {i * SLICE_S:>3}-{(i + 1) * SLICE_S:<3}s  {f(score):>6} MB/s  {f(w, 0, ' W'):>5}  "
              f"max {f(t, 0, '°C'):>5}  fan {f(rpm):>4} rpm  P {f(p and p / 1000, 2)} GHz  "
              f"others {others:>2.0f}%{' !' if others > 10 else '  '}  {phase}")

    sus = [s for s in slice_scores[SUSTAINED_FROM:] if s]
    sus_samples = [x for ss in slice_samples[SUSTAINED_FROM:] for x in ss]
    all_samples = [x for ss in slice_samples for x in ss]
    row = {
        "when": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "label": label,
        "mode": st["mode"] if st else "",
        "limit_w": st["power_limit_w"] if st else "",
        "profile": (st or {}).get("profile") or "",
        "busy_pct": round(busy),
        "others_pct": round(sum(slice_others) / len(slice_others)) if slice_others else "",
        "single_mbs": round(single) if single else "",
        "burst_mbs": round(slice_scores[0]) if slice_scores[0] else "",
        "sustained_mbs": round(sum(sus) / len(sus)) if sus else "",
        "sustained_w": round(w) if (w := avg(sus_samples, "package_w")) else "",
        "max_c": round(t) if (t := peak(all_samples, "cpu_temp")) else "",
        "fan_rpm": round(r) if (r := avg(sus_samples, "fan1_rpm")) else "",
        "p_ghz": round(p / 1000, 2) if (p := avg(sus_samples, "p_mhz")) else "",
        "e_ghz": round(e / 1000, 2) if (e := avg(sus_samples, "e_mhz")) else "",
    }
    os.makedirs(os.path.dirname(RESULTS), exist_ok=True)
    if os.path.exists(RESULTS):
        with open(RESULTS) as fh:
            old_rows = list(csv.DictReader(fh))
            header_ok = old_rows == [] or set(FIELDS) <= set(old_rows[0].keys())
        if not header_ok:
            with open(RESULTS, "w", newline="") as fh:
                wr = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
                wr.writeheader()
                wr.writerows(old_rows)
    new = not os.path.exists(RESULTS)
    with open(RESULTS, "a", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=FIELDS)
        if new:
            wr.writeheader()
        wr.writerow(row)

    print(f"\n  RESULT  single {f(row['single_mbs'])} MB/s · burst {f(row['burst_mbs'])} MB/s · "
          f"sustained {f(row['sustained_mbs'])} MB/s at {f(row['sustained_w'], 0, ' W')} · "
          f"max {f(row['max_c'], 0, '°C')}")
    if row["others_pct"] != "" and row["others_pct"] > 10:
        print(f"\n  ! Other programs took ~{row['others_pct']}% of the CPU during this run, so it isn't "
              "comparable with clean runs. Stop them (fuzzers?) and run again.")
    show_table()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(130)
