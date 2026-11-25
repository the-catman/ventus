"""Drives the real Ventus window against a (fake) ventusd: picks fan modes like a user would and
checks they stick - no bounce back to the previous mode. Opens a window for ~10 s.
Run with VENTUS_* env pointing at a fake daemon (see tests/test_ventus.py FakeLaptop)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import common as C  # noqa: E402
import ventus_gui as G  # noqa: E402
from gi.repository import Gio, GLib  # noqa: E402

results = []


def daemon_mode():
    return C.request({"cmd": "status"})["mode"]


class DriveApp(G.App):
    def do_activate(self):
        G.App.do_activate(self)
        win = self.props.active_window
        sent = []
        orig_send = win._send

        def spy(**kw):           # record everything the GUI sends
            sent.append(kw)
            return orig_send(**kw)
        win._send = spy

        steps = [("curve", 1500), ("max", 4500), ("auto", 7500), ("curve", 10500)]
        for name, at in steps:
            GLib.timeout_add(at, lambda n=name: (win.mode_group.set_active_name(n), False)[1])
            GLib.timeout_add(at + 2500, lambda n=name: (check(win, n, sent), False)[1])
        GLib.timeout_add(13500, lambda: (self.quit(), False)[1])


def check(win, want, sent):
    got_d, got_ui = daemon_mode(), win.mode_group.get_active_name()
    modes_sent = [k["fan_mode"] for k in sent if "fan_mode" in k]
    ok = got_d == want and got_ui == want and modes_sent[-1:] == [want]
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}: picked {want}: daemon={got_d} ui={got_ui} sent so far={modes_sent}")


if __name__ == "__main__":
    app = DriveApp(shot=None)
    app.set_application_id(None)          # never hand off to a running Ventus
    app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)
    app.run([sys.argv[0]])
    n_sent_ok = len(results) == 4 and all(results)
    print("ALL PASS" if n_sent_ok else "FAILED")
    sys.exit(0 if n_sent_ok else 1)
