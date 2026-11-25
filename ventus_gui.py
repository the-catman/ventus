#!/usr/bin/python3 -sE
"""Ventus - GUI for ventusd: live temperatures/power, fan mode, fan curve editor, CPU power limit."""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402

import gi  # noqa: E402
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gsk, Gtk, Pango  # noqa: E402


def rgba(r, g, b, a=1.0):
    c = Gdk.RGBA()
    c.red, c.green, c.blue, c.alpha = r, g, b, a
    return c


def pt(x, y):
    return Graphene.Point().init(x, y)


def poly(points, close_to=None):
    b = Gsk.PathBuilder.new()
    b.move_to(*points[0])
    for p in points[1:]:
        b.line_to(*p)
    if close_to:
        b.close()
    return b.to_path()


def circle(x, y, r):
    b = Gsk.PathBuilder.new()
    b.add_circle(pt(x, y), r)
    return b.to_path()


def stroke(snap, path, color, width, dash=None):
    st = Gsk.Stroke.new(width)
    if dash:
        st.set_dash(dash)
    snap.append_stroke(path, st, color)


def fill(snap, path, color):
    snap.append_fill(path, Gsk.FillRule.WINDING, color)

PROFILES = [("power-saver", "Power Saver"), ("balanced", "Balanced"), ("performance", "Performance")]
PLATFORM_TO_PPD = {"low-power": "power-saver", "balanced": "balanced", "performance": "performance"}
MODE_TEXT = {
    "auto": "HP's own fan control. Quietest, but in this mode HP holds the CPU to about 45 W.",
    "curve": "Ventus sets the fans from the curve below, and the CPU gets its full power limit.",
    "max": "Fans at full speed. Loud, but the coolest option for long heavy loads.",
}
CSS = """
.ventus-tile { padding: 14px 16px; }
.ventus-tile .value { font-size: 26pt; font-weight: 700; }
.ventus-curve { padding: 6px; }
"""


def fmt(v, unit="", nd=0):
    return "–" if v is None else f"{v:.{nd}f}{unit}"


class StatTile(Gtk.Box):
    def __init__(self, caption):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.add_css_class("card")
        self.add_css_class("ventus-tile")
        cap = Gtk.Label(label=caption, xalign=0)
        cap.add_css_class("caption-heading")
        cap.add_css_class("dim-label")
        self.value = Gtk.Label(label="–", xalign=0)
        self.value.add_css_class("value")
        self.value.add_css_class("numeric")
        self.sub = Gtk.Label(label="", xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.sub.add_css_class("caption")
        self.sub.add_css_class("dim-label")
        for w in (cap, self.value, self.sub):
            self.append(w)

    def set(self, value, sub=""):
        self.value.set_label(value)
        self.sub.set_label(sub)


class CurveEditor(Gtk.Widget):
    """Fan curve graph: temperature (x) -> fan % (y), with draggable points and a live marker."""
    T0, T1 = C.CURVE_T_MIN, C.CURVE_T_MAX
    ML, MR, MT, MB = 40, 14, 14, 28
    HIT = 18

    def __init__(self, on_changed):
        super().__init__(hexpand=True)
        self.set_size_request(-1, 230)
        self.points = [list(p) for p in C.DEFAULT_CURVE]
        self.on_changed = on_changed
        self.live_temp = None
        self.live_pct = None
        self.active = True
        self.drag_idx = self.hover_idx = None
        self._drag_start = None
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-update", self._drag_update)
        drag.connect("drag-end", self._drag_end)
        self.add_controller(drag)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._motion)
        motion.connect("leave", lambda *_: self._set_hover(None))
        self.add_controller(motion)

    # geometry
    def _plot(self):
        w, h = self.get_width(), self.get_height()
        return self.ML, self.MT, max(1, w - self.ML - self.MR), max(1, h - self.MT - self.MB)

    def to_px(self, t, p):
        x0, y0, pw, ph = self._plot()
        return x0 + (t - self.T0) / (self.T1 - self.T0) * pw, y0 + (1 - p / 100) * ph

    def from_px(self, x, y):
        x0, y0, pw, ph = self._plot()
        return self.T0 + (x - x0) / pw * (self.T1 - self.T0), (1 - (y - y0) / ph) * 100

    def _nearest(self, x, y):
        best, bd = None, self.HIT
        for i, (t, p) in enumerate(self.points):
            px, py = self.to_px(t, p)
            d = ((px - x) ** 2 + (py - y) ** 2) ** 0.5
            if d <= bd:
                best, bd = i, d
        return best

    # interaction
    def _set_hover(self, idx):
        if idx != self.hover_idx:
            self.hover_idx = idx
            self.set_cursor_from_name("grab" if idx is not None else None)
            self.queue_draw()

    def _motion(self, _ctl, x, y):
        if self.drag_idx is None:
            self._set_hover(self._nearest(x, y))

    def _drag_begin(self, gesture, x, y):
        self.drag_idx = self._nearest(x, y)
        if self.drag_idx is None:
            gesture.set_state(Gtk.EventSequenceState.DENIED)
            return
        self._drag_start = self.to_px(*self.points[self.drag_idx])
        self.set_cursor_from_name("grabbing")

    def _drag_update(self, _g, dx, dy):
        i = self.drag_idx
        if i is None:
            return
        t, p = self.from_px(self._drag_start[0] + dx, self._drag_start[1] + dy)
        pts = self.points
        t_lo = pts[i - 1][0] + 1 if i > 0 else self.T0
        t_hi = pts[i + 1][0] - 1 if i < len(pts) - 1 else self.T1
        p_lo = pts[i - 1][1] if i > 0 else 0
        p_hi = pts[i + 1][1] if i < len(pts) - 1 else 100
        pts[i] = [int(round(min(max(t, t_lo), t_hi))), int(round(min(max(p, p_lo), p_hi)))]
        self.queue_draw()

    def _drag_end(self, *_):
        if self.drag_idx is not None:
            self.drag_idx = None
            self.set_cursor_from_name("grab")
            self.queue_draw()
            self.on_changed([list(p) for p in self.points])

    def set_points(self, pts):
        if self.drag_idx is None and pts != self.points:
            self.points = [list(p) for p in pts]
            self.queue_draw()

    def set_live(self, temp, pct, active):
        self.live_temp, self.live_pct, self.active = temp, pct, active
        self.queue_draw()

    # drawing (GTK 4 snapshot / GSK - no cairo needed)
    def _text(self, snap, s, x, y, color, align=0.0, valign=0.0, bold=False):
        layout = self.create_pango_layout(s)
        layout.set_font_description(Pango.FontDescription.from_string(("Bold " if bold else "") + "9"))
        _, log = layout.get_pixel_extents()
        snap.save()
        snap.translate(pt(round(x - log.width * align), round(y - log.height * valign)))
        snap.append_layout(layout, color)
        snap.restore()

    def do_snapshot(self, snap):
        fg = self.get_color()
        f = (fg.red, fg.green, fg.blue)
        acc = Adw.StyleManager.get_default().get_accent_color_rgba()
        a = (acc.red, acc.green, acc.blue)
        dim = 0.45 if not self.active else 1.0
        x0, y0, pw, ph = self._plot()

        # forced-full zone
        fx, _ = self.to_px(C.FORCE_FULL_TEMP, 0)
        snap.append_color(rgba(0.88, 0.2, 0.2, 0.07), Graphene.Rect().init(fx, y0, x0 + pw - fx, ph))

        # grid + labels
        for t in range(self.T0, self.T1 + 1, 10):
            x, _ = self.to_px(t, 0)
            stroke(snap, poly([(x, y0), (x, y0 + ph)]), rgba(*f, 0.08), 1)
            self._text(snap, f"{t}°", x, y0 + ph + 6, rgba(*f, 0.55), align=0.5)
        for p in (0, 25, 50, 75, 100):
            _, y = self.to_px(self.T0, p)
            stroke(snap, poly([(x0, y), (x0 + pw, y)]), rgba(*f, 0.08), 1)
            self._text(snap, f"{p}%", x0 - 6, y, rgba(*f, 0.55), align=1.0, valign=0.5)

        # curve: flat before the first and after the last point
        pts = [self.to_px(t, p) for t, p in self.points]
        line = [(x0, pts[0][1])] + pts + [(x0 + pw, pts[-1][1])]
        fill(snap, poly([(x0, y0 + ph)] + line + [(x0 + pw, y0 + ph)], close_to=True),
             rgba(*a, 0.16 * dim))
        stroke(snap, poly(line), rgba(*a, dim), 2.5)

        # live temperature marker
        if self.live_temp is not None:
            t = min(max(self.live_temp, self.T0), self.T1)
            x, _ = self.to_px(t, 0)
            stroke(snap, poly([(x, y0), (x, y0 + ph)]), rgba(*f, 0.5), 1.2, dash=[4, 4])
            self._text(snap, f"now {self.live_temp:.0f}°", x + 5, y0 + 2, rgba(*f, 0.75), bold=True)
            if self.live_pct is not None and self.active:
                _, y = self.to_px(t, self.live_pct)
                fill(snap, circle(x, y, 4), rgba(*f, 0.9))

        # points
        for i, (x, y) in enumerate(pts):
            big = i in (self.drag_idx, self.hover_idx)
            fill(snap, circle(x, y, 8 if big else 6), rgba(*a, dim))
            fill(snap, circle(x, y, 3 if big else 2.2), rgba(1, 1, 1, 0.9 * dim))
        idx = self.drag_idx if self.drag_idx is not None else self.hover_idx
        if idx is not None:
            t, p = self.points[idx]
            x, y = pts[idx]
            right = x < x0 + pw * 0.7
            self._text(snap, f"{t}°C → {p}%", x + (12 if right else -12), y - 10, rgba(*f, 0.95),
                       align=0.0 if right else 1.0, valign=1.0, bold=True)


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Ventus", default_width=580, default_height=900)
        # What each control currently shows. GTK can deliver a control's "changed" notification
        # late (after we set it from code), so handlers ignore any value equal to what's shown;
        # only a real user change differs from it.
        self._shown = {"mode": None, "power": None, "profile": None}
        # A user change just sent: keep showing it until the service reports it (or 4 s pass).
        self._pending = {}
        self._updating = False
        self._power_timer = None
        self._status = None

        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        menu = Gio.Menu()
        menu.append("About Ventus", "app.about")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                                       tooltip_text="Menu"))
        view.add_top_bar(header)
        self.banner = Adw.Banner()
        view.add_top_bar(self.banner)
        page = Adw.PreferencesPage()
        view.set_content(page)
        self.toasts.set_child(view)
        self.set_content(self.toasts)

        # -- live readings
        now = Adw.PreferencesGroup(title="Now")
        grid = Gtk.Grid(column_spacing=12, row_spacing=12, column_homogeneous=True)
        self.t_cpu, self.t_gpu = StatTile("CPU"), StatTile("GPU")
        self.t_pow, self.t_fan = StatTile("CPU POWER"), StatTile("CPU FAN")
        grid.attach(self.t_cpu, 0, 0, 1, 1)
        grid.attach(self.t_gpu, 1, 0, 1, 1)
        grid.attach(self.t_pow, 0, 1, 1, 1)
        grid.attach(self.t_fan, 1, 1, 1, 1)
        now.add(grid)
        page.add(now)

        # -- fans
        self.fans = Adw.PreferencesGroup(title="Fans", description=MODE_TEXT["auto"])
        self.mode_group = Adw.ToggleGroup(homogeneous=True, hexpand=True, margin_bottom=10)
        for name, label in (("auto", "HP Auto"), ("curve", "Curve"), ("max", "Max")):
            self.mode_group.add(Adw.Toggle(name=name, label=label))
        self.mode_group.connect("notify::active-name", self._on_mode)
        self.fans.add(self.mode_group)
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        card.add_css_class("card")
        card.add_css_class("ventus-curve")
        self.curve = CurveEditor(self._on_curve)
        card.append(self.curve)
        foot = Gtk.Box(spacing=8, margin_start=10, margin_end=4, margin_bottom=4)
        hint = Gtk.Label(label="Drag the points to shape the curve. Fans always run at 100% "
                               "above 95 °C.", xalign=0, wrap=True, hexpand=True)
        hint.add_css_class("caption")
        hint.add_css_class("dim-label")
        reset = Gtk.Button(label="Reset", valign=Gtk.Align.CENTER)
        reset.add_css_class("flat")
        reset.connect("clicked", lambda *_: self._on_curve([list(p) for p in C.DEFAULT_CURVE]))
        foot.append(hint)
        foot.append(reset)
        card.append(foot)
        self.fans.add(card)
        page.add(self.fans)

        # -- CPU power
        power = Adw.PreferencesGroup(title="CPU power")
        self.power_row = Adw.SpinRow.new_with_range(C.POWER_MIN_W, C.POWER_MAX_W, 5)
        self.power_row.set_title("Sustained power limit (W)")
        self.power_row.set_subtitle("HP default is 55 W. Higher is faster and hotter. "
                                    "Not used in HP Auto mode.")
        self.power_row.connect("notify::value", self._on_power)
        power.add(self.power_row)
        self.limit_row = Adw.ActionRow(title="Limits in effect (sustained / burst)")
        self.limit_row.add_css_class("property")
        power.add(self.limit_row)
        self.profile_row = Adw.ComboRow(title="Power profile",
                                        model=Gtk.StringList.new([p[1] for p in PROFILES]))
        self.profile_row.connect("notify::selected", self._on_profile)
        power.add(self.profile_row)
        page.add(power)

        self.refresh()
        GLib.timeout_add(1000, self.refresh)

    # -- talking to ventusd
    def _send(self, **kw):
        try:
            r = C.request({"cmd": "set", **kw})
        except (OSError, ValueError) as e:
            r = {"ok": False, "error": str(e)}
        if not r.get("ok"):
            self.toasts.add_toast(Adw.Toast(title=f"Not applied: {r.get('error')}"))
        return r.get("ok", False)

    def refresh(self):
        try:
            s = C.request({"cmd": "status"}, timeout=0.3)
        except (OSError, ValueError):
            s = None
        if not s or not s.get("ok") or s.get("starting"):
            self.banner.set_title("Can't reach the Ventus service. Is ventusd running? "
                                  "(systemctl status ventusd)" if not s else "Ventus is starting…")
            self.banner.set_revealed(True)
            for w in (self.mode_group, self.curve, self.power_row):
                w.set_sensitive(False)
            return True
        self._status = s
        for w in (self.mode_group, self.curve, self.power_row):
            w.set_sensitive(True)
        self.mode_group.set_sensitive(s["fan_control"])
        self.banner.set_title(s.get("message") or "")
        self.banner.set_revealed(bool(s.get("message")))

        eff = s["effective_mode"]
        mode = self._settle("mode", s["mode"])
        power = self._settle("power", s["power_limit_w"])
        ppd = self._settle("profile", PLATFORM_TO_PPD.get(s.get("profile")))
        names = [p[0] for p in PROFILES]
        self._updating = True
        try:
            if mode != self._shown["mode"] or self.mode_group.get_active_name() != mode:
                self._shown["mode"] = mode
                self.mode_group.set_active_name(mode)
            self.fans.set_description(MODE_TEXT[mode])
            self.curve.set_points(s["curve"])
            if self._power_timer is None and power != self._shown["power"]:
                self._shown["power"] = power
                self.power_row.set_value(power)
            if ppd in names and ppd != self._shown["profile"]:
                self._shown["profile"] = ppd
                self.profile_row.set_selected(names.index(ppd))
        finally:
            self._updating = False

        self.curve.set_live(s.get("control_temp") if eff == "curve" else s.get("cpu_temp"),
                            s.get("fan_target_pct"), eff == "curve")

        # tiles
        self.t_cpu.set(fmt(s["cpu_temp"], "°"),
                       f"{fmt((s['p_mhz'] or 0) / 1000, '', 1)} / {fmt((s['e_mhz'] or 0) / 1000, '', 1)} GHz  P / E"
                       if s.get("p_mhz") else "")
        if s.get("gpu_temp") is not None:
            self.t_gpu.set(fmt(s["gpu_temp"], "°"), "awake")
        else:
            self.t_gpu.set("Off" if not s.get("gpu_active") else "–",
                           "asleep" if not s.get("gpu_active") else "idle")
        lim = s.get("limits") or {}
        sustained = [v for v in (lim.get("msr_pl1_w"), lim.get("fw_pl1_w")) if v]
        if eff == "auto" and s.get("hp_takeover_s"):
            m, sec = divmod(s["hp_takeover_s"], 60)
            cap = f"HP takes over in {m}:{sec:02d}"
        elif eff == "auto":
            cap = "HP caps it at ≈45 W"
        else:
            cap = f"limit {min(sustained)} W" if sustained else ""
        self.t_pow.set(fmt(s.get("package_w"), " W"), cap)
        f1, f2 = s.get("fan1_rpm"), s.get("fan2_rpm")
        pct = s.get("fan_target_pct")
        self.t_fan.set(fmt(f1, " rpm"), f"GPU fan {fmt(f2)} rpm" + (f" · {pct:.0f}%" if pct is not None else "")
                       + ("" if s.get("on_ac", True) else " · on battery"))
        self.limit_row.set_subtitle(
            f"CPU {fmt(lim.get('msr_pl1_w'), ' W')} / {fmt(lim.get('msr_pl2_w'), ' W')}  ·  "
            f"firmware {fmt(lim.get('fw_pl1_w'), ' W')} / {fmt(lim.get('fw_pl2_w'), ' W')}  ·  "
            "the lower one applies")
        return True

    def _settle(self, key, reported):
        """The value to show: the user's pending choice until the service reports it."""
        pend = self._pending.get(key)
        if pend and pend[0] != reported and time.monotonic() < pend[1]:
            return pend[0]
        self._pending.pop(key, None)
        return reported

    def _user_choice(self, key, value):
        """True if this notification is a real user change (not one we caused from code)."""
        if self._updating or value == self._shown[key]:
            return False
        self._shown[key] = value
        self._pending[key] = (value, time.monotonic() + 4)
        return True

    def _refresh_once(self):
        self.refresh()
        return False

    # -- user actions
    def _on_mode(self, group, _pspec):
        name = group.get_active_name()
        if not self._user_choice("mode", name):
            return
        if self._send(fan_mode=name):
            self.fans.set_description(MODE_TEXT[name])
            if name == "auto":
                self.toasts.add_toast(Adw.Toast(
                    title="Fans stay at ~4000 rpm or faster for 2 minutes until HP takes over.",
                    timeout=6))
        else:
            self._pending.pop("mode", None)
        GLib.idle_add(self._refresh_once)

    def _on_curve(self, pts):
        if self._send(curve=pts):
            self.curve.set_points(pts)
        GLib.idle_add(self._refresh_once)

    def _on_power(self, row, _pspec):
        if not self._user_choice("power", int(row.get_value())):
            return
        if self._power_timer:
            GLib.source_remove(self._power_timer)
        self._power_timer = GLib.timeout_add(700, self._apply_power)

    def _apply_power(self):
        self._power_timer = None
        if not self._send(power_limit_w=self._shown["power"]):
            self._pending.pop("power", None)
        self.refresh()
        return False

    def _on_profile(self, row, _pspec):
        name = PROFILES[row.get_selected()][0]
        if not self._user_choice("profile", name):
            return
        try:
            Gio.Subprocess.new(["powerprofilesctl", "set", name], Gio.SubprocessFlags.NONE)
        except GLib.Error as e:
            self._pending.pop("profile", None)
            self.toasts.add_toast(Adw.Toast(title=f"Could not set profile: {e.message}"))

    def screenshot(self, path):
        content = self.get_content()
        w, h = content.get_width(), content.get_height()
        snap = Gtk.Snapshot()
        Gtk.WidgetPaintable.new(content).snapshot(snap, w, h)
        tex = self.get_renderer().render_texture(snap.to_node(), None)
        tex.save_to_png(path)


class App(Adw.Application):
    def __init__(self, shot=None):
        super().__init__(application_id=C.APP_ID if not shot else None,
                         flags=Gio.ApplicationFlags.NON_UNIQUE if shot else Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.shot = shot
        about = Gio.SimpleAction.new("about", None)
        about.connect("activate", self._about)
        self.add_action(about)

    def do_activate(self):
        prov = Gtk.CssProvider()
        prov.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), prov,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        win = self.props.active_window or Window(self)
        win.present()
        if self.shot:
            def take():
                win.screenshot(self.shot)
                self.quit()
                return False
            GLib.timeout_add(2500, take)

    def _about(self, *_):
        Adw.AboutDialog(application_name="Ventus", application_icon=C.APP_ID, version=C.VERSION,
                        comments="Fan curve and CPU power control for the HP OMEN 16 (8CC0).",
                        license_type=Gtk.License.GPL_2_0).present(self.props.active_window)


if __name__ == "__main__":
    shot = None
    if len(sys.argv) == 3 and sys.argv[1] == "--screenshot":
        shot = sys.argv[2]
    sys.exit(App(shot).run([sys.argv[0]]))
