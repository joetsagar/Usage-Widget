"""Small always-on-top widget showing Claude and Codex usage limits.

Reads the logins Claude Code (~/.claude/.credentials.json) and Codex
(~/.codex/auth.json) already store, so there's nothing to set up.
Drag to move, click a section for the breakdown, x to close.
Pops up a warning when a recent chat did something IT security would worry about.
Opening it again while it's running closes it (open/close toggle).
"""

import ctypes
import ctypes.wintypes
import json
import math
import socket
import ssl
import sys
import threading
import tkinter as tk
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import kilo_usage
import security_check
import usage_details

HOME = Path.home()
CLAUDE_CREDS = HOME / ".claude" / ".credentials.json"
CODEX_AUTH = HOME / ".codex" / "auth.json"
APP_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
POSITION_FILE = APP_DIR / "position.json"
SEEN_FILE = APP_DIR / "security_seen.json"  # warnings already shown, so each pops up once
REFRESH_MS = 2 * 60 * 1000
TOGGLE_PORT = 47821  # a second launch tells the running widget to close

BG = "#1e1e1e"
TEXT = "#d4d4d4"
SUBTLE = "#8a8a8a"
TRACK = "#3a3a3a"
GREY_BAR = "#9a9a9a"
ORANGE_BAR = "#d97757"
RED_BAR = "#f07068"
WARNING = "#e2b340"
BORDER = "#454545"
GREEN = "#7cc28a"

WIDTH = 280
DETAIL_WIDTH = 320
BAR_HEIGHT = 6

# The work network inspects HTTPS, and Python 3.13's strict check rejects its
# certificates, so keep normal verification but drop the strict flag.
SSL_CTX = ssl.create_default_context()
SSL_CTX.verify_flags &= ~ssl.VERIFY_X509_STRICT

WINDOWS = {"Session (5 hours)": timedelta(hours=5), "Weekly (7 days)": timedelta(days=7)}


def enable_dpi_awareness():
    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()


def system_alpha(value):
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return value if winreg.QueryValueEx(key, "EnableTransparency")[0] else 1.0
    except (ImportError, OSError):
        return 1.0


def round_corners(window):
    if sys.platform == "win32":
        try:
            window.update_idletasks()
            user32 = ctypes.windll.user32
            user32.GetParent.restype = ctypes.wintypes.HWND
            hwnd = user32.GetParent(ctypes.wintypes.HWND(window.winfo_id()))
            preference = ctypes.c_int(2)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                ctypes.wintypes.HWND(hwnd), 33, ctypes.byref(preference), ctypes.sizeof(preference))
        except (AttributeError, OSError):
            pass


def elapsed_fraction(resets, window, now=None):
    if resets is None or window.total_seconds() <= 0:
        return None
    now = now or datetime.now(timezone.utc)
    return max(0.0, min(1.0, (now - (resets - window)).total_seconds() / window.total_seconds()))


def friendly_error(name, exc):
    login = "open Claude Code" if name == "Claude" else "run codex login"
    if name == "Kilo Code":
        return "Can't read Kilo usage – retrying in 2 min"
    if isinstance(exc, FileNotFoundError):
        return f"{name} login missing – {login}"
    if isinstance(exc, urllib.error.HTTPError) and exc.code in (401, 403):
        return f"{name} login needs attention – {login}"
    if isinstance(exc, RuntimeError) and "expired" in str(exc).lower():
        return f"{name} login expired – {login}"
    return f"Can't reach {name} – retrying in 2 min"


def get_json(url, headers):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=20, context=SSL_CTX) as resp:
        return json.loads(resp.read().decode())


def fetch_claude():
    creds = json.loads(CLAUDE_CREDS.read_text())["claudeAiOauth"]
    if creds.get("expiresAt", 0) / 1000 < datetime.now().timestamp():
        raise RuntimeError("Login expired - open Claude Code")
    data = get_json(
        "https://api.anthropic.com/api/oauth/usage",
        {
            "Authorization": "Bearer " + creds["accessToken"],
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "claude-code/2.0",
        },
    )
    rows = []
    for key, label in (("five_hour", "Session (5 hours)"), ("seven_day", "Weekly (7 days)")):
        window = data.get(key)
        if window:
            resets = datetime.fromisoformat(window["resets_at"]) if window.get("resets_at") else None
            rows.append((label, window.get("utilization") or 0, resets, WINDOWS[label]))
    return rows


def fetch_codex():
    tokens = json.loads(CODEX_AUTH.read_text())["tokens"]
    data = get_json(
        "https://chatgpt.com/backend-api/wham/usage",
        {
            "Authorization": "Bearer " + tokens["access_token"],
            "ChatGPT-Account-Id": tokens.get("account_id", ""),
            "User-Agent": "codex-cli",
        },
    )
    limits = data.get("rate_limit") or {}
    rows = []
    for key, label in (("primary_window", "Session (5 hours)"), ("secondary_window", "Weekly (7 days)")):
        window = limits.get(key)
        if window:
            resets = datetime.fromtimestamp(window["reset_at"], timezone.utc) if window.get("reset_at") else None
            rows.append((label, window.get("used_percent") or 0, resets, WINDOWS[label]))
    return rows


def reset_text(when):
    if when is None:
        return ""
    local = when.astimezone()
    clock = local.strftime("%I:%M %p").lstrip("0")
    if local.date() == datetime.now().date():
        return f"Resets at {clock}"
    return f"Resets {local.strftime('%a, %b')} {local.day} at {clock}"


def bar_colour(pct):
    if pct >= 90:
        return RED_BAR
    if pct >= 75:
        return WARNING
    return GREY_BAR


def draw_claude_logo(canvas, x, y, size):
    """Orange starburst, like the Claude mark."""
    cx, cy, r = x + size / 2, y + size / 2, size / 2
    for i in range(12):
        angle = math.radians(i * 30 + 8)
        length = r if i % 2 == 0 else r * 0.75
        canvas.create_line(
            cx + math.cos(angle) * r * 0.15, cy + math.sin(angle) * r * 0.15,
            cx + math.cos(angle) * length, cy + math.sin(angle) * length,
            fill=ORANGE_BAR, width=2.2, capstyle=tk.ROUND,
        )


def draw_codex_logo(canvas, x, y, size):
    """White rounded tile with a >_ prompt, like the Codex CLI icon."""
    canvas.create_oval(x, y, x + size, y + size, fill="#f2f2f2", outline="")
    m = size * 0.28
    canvas.create_line(x + m, y + m, x + size * 0.48, y + size / 2, x + m, y + size - m,
                       fill="#111111", width=2, capstyle=tk.ROUND, joinstyle=tk.ROUND)
    canvas.create_line(x + size * 0.55, y + size - m, x + size - m * 0.9, y + size - m,
                       fill="#111111", width=2, capstyle=tk.ROUND)


LOGOS = {"Claude": draw_claude_logo, "Codex": draw_codex_logo}


def monitor_area(x, y):
    """(left, top, right, bottom) of the work area of the screen containing x, y."""
    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", ctypes.c_ulong)]

    try:
        user32 = ctypes.windll.user32
        user32.MonitorFromPoint.restype = ctypes.c_void_p
        handle = user32.MonitorFromPoint(ctypes.wintypes.POINT(x, y), 2)  # 2 = nearest screen
        info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
        user32.GetMonitorInfoW(ctypes.c_void_p(handle), ctypes.byref(info))
        r = info.rcWork
        return r.left, r.top, r.right, r.bottom
    except (AttributeError, OSError):
        return 0, 0, 10000, 10000


def place_beside(root, width, height):
    """x, y for a panel next to the widget, kept on the widget's own screen."""
    rx, ry, rw, rh = root.winfo_x(), root.winfo_y(), root.winfo_width(), root.winfo_height()
    left, top, right, bottom = monitor_area(rx + rw // 2, ry + rh // 2)
    x = rx - width - 6
    if x < left:
        x = rx + rw + 6
    if x + width > right:
        x = max(right - width, left)
    y = min(max(ry, top), max(bottom - height, top))
    return x, y


def close_running_copy():
    """If the widget is already open, tell it to close and return True."""
    try:
        with socket.create_connection(("127.0.0.1", TOGGLE_PORT), timeout=1) as s:
            s.sendall(b"close")
        return True
    except OSError:
        return False


class UsageWidget:
    def __init__(self):
        if sys.platform == "win32":
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("UsageWidget.AIUsage")
        enable_dpi_awareness()
        self.root = tk.Tk()
        self.scale = self.root.winfo_fpixels("1i") / 96
        global WIDTH, DETAIL_WIDTH, BAR_HEIGHT
        WIDTH = round(280 * self.scale)
        DETAIL_WIDTH = round(360 * self.scale)
        BAR_HEIGHT = round(6 * self.scale)
        icon_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
        self.root.iconbitmap(str(icon_dir / "usage_widget.ico"))
        self.root.title("AI Usage")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", system_alpha(0.95))
        self.root.configure(bg=BG)

        self.canvas = tk.Canvas(self.root, width=WIDTH, height=60, bg=BG, highlightthickness=1,
                                highlightbackground=BORDER, highlightcolor=BORDER, takefocus=True, bd=0)
        self.canvas.pack()

        self.results = {"Claude": None, "Codex": None, "Kilo Code": None}
        self.errors = {}
        self.last_good = {}
        self.fetching = False
        self.tooltip = None
        self.selected = "Claude"
        self.sections = {}  # name -> (top y, bottom y), for click hit-testing
        self.updated = None
        self.detail = None
        self.detail_name = None
        self.alert = None

        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="Refresh now", command=self.refresh_now)
        self.menu.add_command(label="Security check (7 days)", command=self.security_week)
        self.menu.add_separator()
        self.menu.add_command(label="Close", command=self.close)

        self.canvas.bind("<ButtonPress-1>", self.press)
        self.canvas.bind("<B1-Motion>", self.drag)
        self.canvas.bind("<ButtonRelease-1>", self.release)
        self.canvas.bind("<Button-3>", lambda e: self.menu.tk_popup(e.x_root, e.y_root))

        self.root.bind("<Escape>", self.escape)
        self.root.bind("<F5>", lambda e: self.refresh_now())
        self.root.bind("<Return>", lambda e: self.toggle_detail(self.selected))
        self.root.bind("1", lambda e: self.toggle_detail("Claude"))
        self.root.bind("2", lambda e: self.toggle_detail("Codex"))
        self.root.bind("<Tab>", self.select_next)
        self.root.bind("<Shift-F10>", self.keyboard_menu)
        self.canvas.bind("<Motion>", self.hover)
        self.canvas.bind("<Leave>", lambda e: self.hide_tooltip())
        self.canvas.bind("<FocusIn>", lambda e: self.draw())
        self.canvas.bind("<FocusOut>", lambda e: self.draw())
        self.root.after_idle(lambda: round_corners(self.root))
        self.listen_for_toggle()
        self.place_window()
        self.draw()
        self.refresh()

    def escape(self, event=None):
        if self.alert:
            self.close_alert()
        elif self.detail:
            self.toggle_detail(self.detail_name)
        else:
            self.close()
        return "break"

    def select_next(self, event=None):
        self.selected = "Codex" if self.selected == "Claude" else "Claude"
        self.draw()
        return "break"

    def keyboard_menu(self, event=None):
        self.menu.tk_popup(self.root.winfo_rootx() + 12, self.root.winfo_rooty() + 12)
        return "break"

    def hide_tooltip(self):
        if self.tooltip:
            self.tooltip.destroy()
            self.tooltip = None

    def hover(self, event):
        clickable = any(top <= event.y < bottom for top, bottom in self.sections.values())
        self.canvas.configure(cursor="hand2" if clickable else "")
        if not self.tooltip:
            self.tooltip = tk.Toplevel(self.root)
            self.tooltip.overrideredirect(True)
            self.tooltip.attributes("-topmost", True)
            tk.Label(self.tooltip, text="Click Claude/Codex for pace and tips. Right-click for menu.\n"
                     "F5: refresh · 1/2: details · Tab/Enter: select/open · Esc: close",
                     bg=BG, fg=TEXT, font=("Segoe UI", 8), padx=8, pady=6).pack()
            self.tooltip.geometry(f"+{self.root.winfo_x()}+{self.root.winfo_y() + self.root.winfo_height() + 6}")

    # --- open/close toggle ------------------------------------------------
    def listen_for_toggle(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", TOGGLE_PORT))
        server.listen(1)

        def wait():
            while True:
                conn, _ = server.accept()
                conn.close()
                self.root.after(0, self.close)

        threading.Thread(target=wait, daemon=True).start()

    def close(self):
        self.hide_tooltip()
        self.root.destroy()

    # --- window position and clicks ----------------------------------------
    def place_window(self):
        try:
            pos = json.loads(POSITION_FILE.read_text())
            x, y = pos["x"], pos["y"]
        except (OSError, ValueError, KeyError):
            x, y = self.root.winfo_screenwidth() - WIDTH - 20, 20
        self.root.geometry(f"+{x}+{y}")

    def press(self, event):
        self.canvas.focus_set()
        self.hide_tooltip()
        self.drag_offset = (event.x, event.y)
        self.press_at = (event.x_root, event.y_root)

    def drag(self, event):
        x = self.root.winfo_pointerx() - self.drag_offset[0]
        y = self.root.winfo_pointery() - self.drag_offset[1]
        self.hide_tooltip()
        self.root.geometry(f"+{x}+{y}")
        if self.detail:
            self.position_detail()
        if self.alert:
            self.position_alert()

    def release(self, event):
        moved = abs(event.x_root - self.press_at[0]) + abs(event.y_root - self.press_at[1])
        if moved > 4:
            try:
                POSITION_FILE.write_text(json.dumps({"x": self.root.winfo_x(), "y": self.root.winfo_y()}))
            except OSError:
                pass
            return
        if event.x >= WIDTH - 22 and event.y <= 22:
            self.close()
            return
        for name, (top, bottom) in self.sections.items():
            if top <= event.y < bottom:
                self.toggle_detail(name)

    # --- data -------------------------------------------------------------
    def refresh(self):
        self.refresh_now()
        self.root.after(REFRESH_MS, self.refresh)

    def refresh_now(self):
        if self.fetching:
            return
        self.fetching = True
        threading.Thread(target=self.fetch_all, daemon=True).start()

    def fetch_all(self):
        results = {}
        for name, fetch in (("Claude", fetch_claude), ("Codex", fetch_codex), ("Kilo Code", lambda: kilo_usage.fetch(get_json))):
            try:
                results[name] = fetch()
            except Exception as exc:  # show the problem in the widget instead of crashing
                results[name] = friendly_error(name, exc)
        try:
            findings = security_check.recent(1)
        except Exception:
            findings = []
        self.root.after(0, self.show, results, findings)

    def show(self, results, findings):
        self.fetching = False
        self.errors = {name: data for name, data in results.items() if isinstance(data, str)}
        for name, data in results.items():
            if not isinstance(data, str):
                self.last_good[name] = data
            self.results[name] = self.last_good.get(name, data)
        if len(self.errors) < len(results):
            self.updated = datetime.now()
        self.draw()
        try:
            seen = set(json.loads(SEEN_FILE.read_text()))
        except (OSError, ValueError):
            seen = set()
        new = [f for f in findings if f["key"] not in seen]
        if new:
            self.show_alert(new, "New security warning")
            try:
                SEEN_FILE.write_text(json.dumps(sorted(seen | {f["key"] for f in findings})))
            except OSError:
                pass

    # --- security warnings ----------------------------------------------------
    def security_week(self):
        def work():
            try:
                findings = security_check.recent(7)
            except Exception as exc:
                findings = str(exc)
            self.root.after(0, self.show_alert, findings, "Security check · last 7 days")

        threading.Thread(target=work, daemon=True).start()

    def show_alert(self, findings, heading):
        if self.alert:
            self.alert.destroy()
        self.alert = tk.Toplevel(self.root)
        self.alert.overrideredirect(True)
        self.alert.attributes("-topmost", True)
        self.alert.attributes("-alpha", system_alpha(0.97))
        frame = tk.Frame(self.alert, bg=BG, highlightthickness=2, highlightbackground=RED_BAR)
        frame.pack(fill="both", expand=True)
        widgets = [frame]

        def label(text, colour=TEXT, size=10, bold=False, pady=(0, 0)):
            w = tk.Label(frame, text=text, fg=colour, bg=BG, font=("Segoe UI Semibold" if bold else "Segoe UI", size),
                         anchor="w", justify="left", wraplength=DETAIL_WIDTH - 28)
            w.pack(fill="x", padx=12, pady=pady)
            widgets.append(w)

        label("⚠ " + heading, RED_BAR, 11, bold=True, pady=(10, 6))
        if isinstance(findings, str):
            label(findings, RED_BAR)
        elif not findings:
            label("Nothing risky found.", GREEN)
        else:
            for f in findings[:6]:
                label(f["risk"], TEXT, 9, bold=True)
                label(f"{f['chat']} - {f['when'].astimezone():%a %H:%M}", SUBTLE)
                label(f["detail"], SUBTLE, 8, pady=(0, 6))
            if len(findings) > 6:
                label(f"...and {len(findings) - 6} more. Right-click > Security check to see the week.", SUBTLE)
        label("Click or press Esc to dismiss", SUBTLE, 8, pady=(2, 8))
        for w in widgets:
            w.bind("<Button-1>", lambda e: self.close_alert())

        round_corners(self.alert)
        self.position_alert()

    def position_alert(self):
        self.alert.update_idletasks()
        height = self.alert.winfo_reqheight()
        x, y = place_beside(self.root, DETAIL_WIDTH, height)
        if self.detail:
            dx, dy = self.detail.winfo_x(), self.detail.winfo_y()
            dw, dh = self.detail.winfo_width(), self.detail.winfo_height()
            left, top, right, bottom = monitor_area(self.root.winfo_x(), self.root.winfo_y())
            if x < dx + dw and x + DETAIL_WIDTH > dx and y < dy + dh and y + height > dy:
                candidates = [(dx, dy + dh + 6), (dx, dy - height - 6),
                              (self.root.winfo_x() + self.root.winfo_width() + 6, y),
                              (self.root.winfo_x() - DETAIL_WIDTH - 6, y)]
                for ax, ay in candidates:
                    if (left <= ax and ax + DETAIL_WIDTH <= right and top <= ay and ay + height <= bottom
                            and not (ax < dx + dw and ax + DETAIL_WIDTH > dx and ay < dy + dh and ay + height > dy)):
                        x, y = ax, ay
                        break
                else:
                    # On small screens show one panel at a time instead of hiding content.
                    self.detail.destroy()
                    self.detail = None
                    self.detail_name = None
                    self.draw()
        self.alert.geometry(f"{DETAIL_WIDTH}x{height}+{x}+{y}")

    def close_alert(self):
        if self.alert:
            self.alert.destroy()
            self.alert = None

    # --- drawing ----------------------------------------------------------
    def draw(self):
        c = self.canvas
        c.delete("all")
        self.sections.clear()
        scale = self.scale
        # Canvas coordinates use logical pixels; fonts already scale through Tk.
        width = WIDTH / scale
        bar_height = BAR_HEIGHT / scale
        pad = 12
        y = 12
        c.create_text(width - 11, 9, text="✕", anchor="ne", fill=SUBTLE, font=("Segoe UI", 10))
        for name in ("Claude", "Codex", "Kilo Code"):
            top = y - 6
            stale = name in self.errors
            row_colour = SUBTLE if stale else TEXT
            if name == "Kilo Code":
                c.create_rectangle(pad, y, pad + 18, y + 18, fill="#f8e45c", outline="")
                c.create_text(pad + 9, y + 9, text="K", fill=BG, font=("Segoe UI", 10, "bold"))
            else:
                LOGOS[name](c, pad, y, 18)
            c.create_text(pad + 26, y + 9, text=name, anchor="w", fill=row_colour, font=("Segoe UI Semibold", 11))
            arrow = "▾" if self.detail_name == name else "›"
            if name != "Kilo Code":
                c.create_text(pad + 26 + 62, y + 9, text=arrow, anchor="w", fill=SUBTLE, font=("Segoe UI", 10))
            y += 26
            rows = self.results.get(name)
            if rows is None:
                c.create_text(pad, y, text=f"Checking {name}…", anchor="nw", fill=SUBTLE, font=("Segoe UI", 8))
                y += 20
            elif isinstance(rows, str):
                c.create_text(pad, y, text=rows, anchor="nw", fill=RED_BAR, font=("Segoe UI", 8),
                              width=width - 2 * pad)
                y = c.bbox(c.find_all()[-1])[3] + 8
            elif isinstance(rows, dict):
                for label, value in kilo_usage.summary(rows):
                    c.create_text(pad, y, text=label, anchor="nw", fill=row_colour, font=("Segoe UI", 10))
                    c.create_text(width - pad, y, text=value, anchor="ne", fill=row_colour, font=("Segoe UI", 10))
                    y += 21
            else:
                for label, pct, resets, window in rows:
                    c.create_text(pad, y, text=label, anchor="nw", fill=row_colour, font=("Segoe UI", 10))
                    c.create_text(width - pad, y, text=f"{pct:.0f}%", anchor="ne", fill=row_colour, font=("Segoe UI Semibold", 11))
                    y += 23
                    c.create_rectangle(pad, y, width - pad, y + bar_height, fill=TRACK, outline="")
                    fill_to = pad + (width - 2 * pad) * min(pct, 100) / 100
                    if pct > 0:
                        c.create_rectangle(pad, y, fill_to, y + bar_height, fill=SUBTLE if stale else bar_colour(pct), outline="")
                    fraction = elapsed_fraction(resets, window)
                    if fraction is not None:
                        tick = pad + (width - 2 * pad) * fraction
                        c.create_line(tick, y - 2, tick, y + bar_height + 2, fill=TEXT, width=2)
                    y += bar_height + 4
                    c.create_text(pad, y, text=reset_text(resets), anchor="nw", fill=SUBTLE, font=("Segoe UI", 8))
                    y += 20
            y += 6
            if stale and not isinstance(rows, str):
                item = c.create_text(pad, y, text="Stale · " + self.errors[name], anchor="nw",
                                    fill=WARNING, font=("Segoe UI", 8), width=width - 2 * pad)
                y = c.bbox(item)[3] + 8
            if name != "Kilo Code":
                self.sections[name] = (top * scale, (y - 2) * scale)
        if self.updated:
            c.create_text(width - pad, y, text=f"Updated {self.updated:%H:%M}", anchor="ne", fill=SUBTLE,
                          font=("Segoe UI", 8))
            y += 12
        c.create_text(pad, y, text="| time elapsed · click a provider for tips", anchor="nw",
                      fill=SUBTLE, font=("Segoe UI", 8))
        y += 20
        if c.focus_get() == c:
            top, bottom = self.sections.get(self.selected, (0, 0))
            c.create_rectangle(4, top / scale, width - 4, bottom / scale, outline=SUBTLE, dash=(2, 2))
        c.scale("all", 0, 0, scale, scale)
        c.configure(height=round((y + 4) * scale))

    # --- detail panel -------------------------------------------------------
    def toggle_detail(self, name):
        if name == "Kilo Code":
            return
        if self.detail:
            self.detail.destroy()
            self.detail = None
            if self.detail_name == name:
                self.detail_name = None
                self.draw()
                return
        rows = self.results.get(name)
        if not isinstance(rows, (list, dict)):
            return
        self.detail_name = name
        self.draw()

        self.detail = tk.Toplevel(self.root)
        self.detail.overrideredirect(True)
        self.detail.attributes("-topmost", True)
        self.detail.attributes("-alpha", system_alpha(0.97))
        self.body = tk.Frame(self.detail, bg=BG, highlightthickness=1, highlightbackground=BORDER)
        self.body.pack(fill="both", expand=True)
        self.body.bind("<Button-1>", lambda e: self.toggle_detail(name))
        self.add_label(f"{name} · checking usage…", SUBTLE, 9)
        self.position_detail()

        other = self.results.get("Codex" if name == "Claude" else "Claude")
        other = other if isinstance(other, list) else []

        def work():
            try:
                info = rows if name == "Kilo Code" else usage_details.build(name, rows, other)
            except Exception as exc:
                info = {"error": str(exc)}
            self.root.after(0, self.fill_detail, name, info)

        threading.Thread(target=work, daemon=True).start()

    def position_detail(self):
        self.detail.update_idletasks()
        height = self.detail.winfo_reqheight()
        x, y = place_beside(self.root, DETAIL_WIDTH, height)
        self.detail.geometry(f"{DETAIL_WIDTH}x{height}+{x}+{y}")
        round_corners(self.detail)
        if self.alert:
            self.position_alert()

    def add_label(self, text, colour=TEXT, size=10, bold=False, right=None, pady=(0, 0)):
        row = tk.Frame(self.body, bg=BG)
        row.pack(fill="x", padx=12, pady=pady)
        font = ("Segoe UI Semibold" if bold else "Segoe UI", size)
        left = tk.Label(row, text=text, fg=colour, bg=BG, font=font, anchor="w", justify="left",
                        wraplength=DETAIL_WIDTH - (80 if right else 28))
        left.pack(side="left", fill="x", expand=True)
        widgets = [row, left]
        if right:
            r = tk.Label(row, text=right, fg=colour, bg=BG, font=font)
            r.pack(side="right")
            widgets.append(r)
        for w in widgets:  # click anywhere on the panel to close it
            w.bind("<Button-1>", lambda e: self.toggle_detail(self.detail_name))

    def fill_detail(self, name, info):
        if not self.detail or self.detail_name != name:
            return
        for child in self.body.winfo_children():
            child.destroy()
        self.add_label(f"{name} · this week", TEXT, 11, bold=True, pady=(10, 4))
        if "error" in info:
            self.add_label(info["error"], RED_BAR)
        else:
            for label, text in info["pace"]:
                colour = WARNING if "run out" in text or "reached" in text else GREEN
                self.add_label(f"{label}: {text}", colour)

            detail = info["detail"]
            if detail:
                unit = detail["unit"]
                self.add_label(f"Biggest chats ({unit})", SUBTLE, 8, bold=True, pady=(8, 2))
                for chat in detail["chats"][:6]:
                    self.add_label(chat["title"], TEXT, right=f"{chat['share']:.0f}%")
                if name == "Codex" and detail.get("efforts"):
                    self.add_label("By reasoning effort", SUBTLE, 8, bold=True, pady=(8, 2))
                    for effort, pct in detail["efforts"].items():
                        self.add_label(effort.title(), TEXT, right=f"{pct:.0f}%")
                else:
                    self.add_label("By model", SUBTLE, 8, bold=True, pady=(8, 2))
                    for model, pct in list(detail["models"].items())[:4]:
                        self.add_label(model, TEXT, right=f"{pct:.0f}%")
                if name == "Claude":
                    self.add_label("Estimated from Claude Code logs on this PC. claude.ai chats count "
                                   "towards the limit too but aren't shown.", SUBTLE, 8, pady=(4, 0))
            else:
                self.add_label("No local usage logged this week.", SUBTLE, pady=(8, 0))

            self.add_label("What to change", SUBTLE, 8, bold=True, pady=(8, 2))
            for tip in info["tips"]:
                self.add_label("• " + tip, TEXT, pady=(0, 3))
        tk.Frame(self.body, bg=BG, height=8).pack()
        self.position_detail()


if __name__ == "__main__":
    if close_running_copy():
        sys.exit()
    UsageWidget().root.mainloop()
