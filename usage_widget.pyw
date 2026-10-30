"""Small always-on-top widget showing Claude and Codex usage limits.

Reads the logins Claude Code (~/.claude/.credentials.json) and Codex
(~/.codex/auth.json) already store, so there's nothing to set up.
Drag to move, click a section for the breakdown, x to close.
Opening it again while it's running closes it (open/close toggle).
"""

import json
import math
import socket
import ssl
import sys
import threading
import tkinter as tk
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import usage_details

HOME = Path.home()
CLAUDE_CREDS = HOME / ".claude" / ".credentials.json"
CODEX_AUTH = HOME / ".codex" / "auth.json"
APP_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
POSITION_FILE = APP_DIR / "position.json"
REFRESH_MS = 2 * 60 * 1000
TOGGLE_PORT = 47821  # a second launch tells the running widget to close

BG = "#1e1e1e"
TEXT = "#d4d4d4"
SUBTLE = "#8a8a8a"
TRACK = "#3a3a3a"
GREY_BAR = "#9a9a9a"
ORANGE_BAR = "#d97757"
RED_BAR = "#e5534b"
GREEN = "#7cc28a"

WIDTH = 240
DETAIL_WIDTH = 320
BAR_HEIGHT = 6

# The work network inspects HTTPS, and Python 3.13's strict check rejects its
# certificates, so keep normal verification but drop the strict flag.
SSL_CTX = ssl.create_default_context()
SSL_CTX.verify_flags &= ~ssl.VERIFY_X509_STRICT

WINDOWS = {"Session (5hr)": timedelta(hours=5), "Weekly (7 day)": timedelta(days=7)}


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
    for key, label in (("five_hour", "Session (5hr)"), ("seven_day", "Weekly (7 day)")):
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
    for key, label in (("primary_window", "Session (5hr)"), ("secondary_window", "Weekly (7 day)")):
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
        return ORANGE_BAR
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
        self.root = tk.Tk()
        self.root.title("AI Usage")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.95)
        self.root.configure(bg=BG)

        self.canvas = tk.Canvas(self.root, width=WIDTH, height=60, bg=BG, highlightthickness=1,
                                highlightbackground="#ffffff", highlightcolor="#ffffff", bd=0)
        self.canvas.pack()

        self.results = {"Claude": None, "Codex": None}
        self.sections = {}  # name -> (top y, bottom y), for click hit-testing
        self.updated = None
        self.detail = None
        self.detail_name = None

        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="Refresh now", command=self.refresh_now)
        self.menu.add_separator()
        self.menu.add_command(label="Close", command=self.close)

        self.canvas.bind("<ButtonPress-1>", self.press)
        self.canvas.bind("<B1-Motion>", self.drag)
        self.canvas.bind("<ButtonRelease-1>", self.release)
        self.canvas.bind("<Button-3>", lambda e: self.menu.tk_popup(e.x_root, e.y_root))

        self.listen_for_toggle()
        self.place_window()
        self.draw()
        self.refresh()

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
        self.drag_offset = (event.x, event.y)
        self.press_at = (event.x_root, event.y_root)

    def drag(self, event):
        x = self.root.winfo_pointerx() - self.drag_offset[0]
        y = self.root.winfo_pointery() - self.drag_offset[1]
        self.root.geometry(f"+{x}+{y}")
        if self.detail:
            self.position_detail()

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
        threading.Thread(target=self.fetch_all, daemon=True).start()
        self.root.after(REFRESH_MS, self.refresh)

    def refresh_now(self):
        threading.Thread(target=self.fetch_all, daemon=True).start()

    def fetch_all(self):
        results = {}
        for name, fetch in (("Claude", fetch_claude), ("Codex", fetch_codex)):
            try:
                results[name] = fetch()
            except Exception as exc:  # show the problem in the widget instead of crashing
                results[name] = str(exc)[:60]
        self.root.after(0, self.show, results)

    def show(self, results):
        self.results = results
        self.updated = datetime.now()
        self.draw()

    # --- drawing ----------------------------------------------------------
    def draw(self):
        c = self.canvas
        c.delete("all")
        pad = 12
        y = 10
        c.create_text(WIDTH - 11, 9, text="✕", anchor="ne", fill=SUBTLE, font=("Segoe UI", 9))
        for name in ("Claude", "Codex"):
            top = y - 6
            LOGOS[name](c, pad, y, 18)
            c.create_text(pad + 26, y + 9, text=name, anchor="w", fill=TEXT, font=("Segoe UI Semibold", 10))
            arrow = "▾" if self.detail_name == name else "›"
            c.create_text(pad + 26 + 52, y + 9, text=arrow, anchor="w", fill=SUBTLE, font=("Segoe UI", 10))
            y += 26
            rows = self.results.get(name)
            if rows is None:
                c.create_text(pad, y, text="Loading...", anchor="nw", fill=SUBTLE, font=("Segoe UI", 8))
                y += 20
            elif isinstance(rows, str):
                c.create_text(pad, y, text=rows, anchor="nw", fill=RED_BAR, font=("Segoe UI", 8),
                              width=WIDTH - 2 * pad)
                y += 30
            else:
                for label, pct, resets, _ in rows:
                    c.create_text(pad, y, text=label, anchor="nw", fill=TEXT, font=("Segoe UI", 9))
                    c.create_text(WIDTH - pad, y, text=f"{pct:.0f}%", anchor="ne", fill=TEXT, font=("Segoe UI", 9))
                    y += 19
                    c.create_rectangle(pad, y, WIDTH - pad, y + BAR_HEIGHT, fill=TRACK, outline="")
                    fill_to = pad + (WIDTH - 2 * pad) * min(pct, 100) / 100
                    if pct > 0:
                        c.create_rectangle(pad, y, fill_to, y + BAR_HEIGHT, fill=bar_colour(pct), outline="")
                    y += BAR_HEIGHT + 4
                    c.create_text(pad, y, text=reset_text(resets), anchor="nw", fill=SUBTLE, font=("Segoe UI", 8))
                    y += 20
            y += 6
            self.sections[name] = (top, y - 2)
        if self.updated:
            c.create_text(WIDTH - pad, y, text=f"Updated {self.updated:%H:%M}", anchor="ne", fill="#5a5a5a",
                          font=("Segoe UI", 7))
            y += 12
        c.configure(height=y + 4)

    # --- detail panel -------------------------------------------------------
    def toggle_detail(self, name):
        if self.detail:
            self.detail.destroy()
            self.detail = None
            if self.detail_name == name:
                self.detail_name = None
                self.draw()
                return
        rows = self.results.get(name)
        if not isinstance(rows, list):
            return
        self.detail_name = name
        self.draw()

        self.detail = tk.Toplevel(self.root)
        self.detail.overrideredirect(True)
        self.detail.attributes("-topmost", True)
        self.detail.attributes("-alpha", 0.97)
        self.body = tk.Frame(self.detail, bg=BG, highlightthickness=1, highlightbackground="#ffffff")
        self.body.pack(fill="both", expand=True)
        self.body.bind("<Button-1>", lambda e: self.toggle_detail(name))
        self.add_label(f"{name} - working it out...", SUBTLE, 9)
        self.position_detail()

        other = self.results.get("Codex" if name == "Claude" else "Claude")
        other = other if isinstance(other, list) else []

        def work():
            try:
                info = usage_details.build(name, rows, other)
            except Exception as exc:
                info = {"error": str(exc)}
            self.root.after(0, self.fill_detail, name, info)

        threading.Thread(target=work, daemon=True).start()

    def position_detail(self):
        self.detail.update_idletasks()
        x = self.root.winfo_x() - DETAIL_WIDTH - 6
        if x < 0:
            x = self.root.winfo_x() + self.root.winfo_width() + 6
        self.detail.geometry(f"{DETAIL_WIDTH}x{self.detail.winfo_reqheight()}+{x}+{self.root.winfo_y()}")

    def add_label(self, text, colour=TEXT, size=8, bold=False, right=None, pady=(0, 0)):
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
        self.add_label(f"{name} - this week", TEXT, 10, bold=True, pady=(10, 4))
        if "error" in info:
            self.add_label(info["error"], RED_BAR)
        else:
            for label, text in info["pace"]:
                colour = ORANGE_BAR if "run out" in text or "reached" in text else GREEN
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
                                   "towards the limit too but aren't shown.", "#6a6a6a", 7, pady=(4, 0))
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
