# =============================================================================
# client.py - the STASH desktop app (the part the user sees and clicks)
#
# What it does
#   Log in, then upload, view, download, rename and delete note files that
#   are stored on the servers (server.py).
#
# How it talks to the servers
#   It sends one short text command (like "READ:notes.txt") over the network
#   and waits for one reply.
#
# If a server crashes
#   There are two servers: Primary and Backup. If the Primary stops
#   answering, the app shows "Server Crash Detected" and keeps working by
#   using the Backup. No action is needed from the user.
#
# How this file is organised
#   1. Settings       2. Network code      3. Small widgets
#   4. The app: login screen, main screen, file actions
#
# Keys: Ctrl+U upload, Ctrl+F search, F5 refresh, F2 rename, Del delete
# Needs only Python. Pillow is optional (JPG previews, sharper logo).
# =============================================================================

import base64
import io
import os
import queue
import socket
import struct
import sys
import threading
import time
import tkinter as tk
import traceback
from tkinter import filedialog, font as tkfont, ttk

# Pillow is optional. Without it the app works, but no JPG previews.
try:
  from PIL import Image, ImageTk  # optional
except Exception:
  Image = ImageTk = None

# The two servers: (name, address, port).
NODES = [("Primary", "127.0.0.1", 8000), ("Backup", "127.0.0.1", 8001)]
# Allowed file types, and the biggest file size (50 MB).
ALLOWED = {".txt", ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".png", ".jpg",
           ".jpeg", ".xls", ".xlsx"}
MAX_SIZE = 50 * 1024 * 1024

# Colours used by the whole app.
BG, SURF, SIDE = "#f6f4fb", "#ffffff", "#1a1433"
BORDER, TEXT, MUTED = "#e6e1f1", "#1c1632", "#6f6888"
ACCENT, ACCENT_D, SOFT = "#6045a9", "#4b3590", "#efeafa"
OK, WARN, DANGER = "#16a34a", "#d98a06", "#dc3c3c"
SIDE_TEXT, SIDE_MUTED, SIDE_HOVER = "#ece8f8", "#a198c2", "#271f4a"

# File groups: (name, sidebar label, colour, extensions).
GROUPS = [
    ("Text", "Text notes", "#64748b", (".txt",)),
    ("PDF", "PDFs", "#e5484d", (".pdf",)),
    ("Document", "Documents", "#2f6fed", (".doc", ".docx")),
    ("Presentation", "Slides", "#f08a24", (".ppt", ".pptx")),
    ("Image", "Images", "#9b5de5", (".png", ".jpg", ".jpeg")),
    ("Spreadsheet", "Spreadsheets", "#1f9d55", (".xls", ".xlsx")),
]
# Short badge text shown for each file type.
CODES = {".txt": "TXT", ".pdf": "PDF", ".doc": "DOC", ".docx": "DOC",
         ".ppt": "PPT", ".pptx": "PPT",
         ".png": "PNG", ".jpg": "JPG", ".jpeg": "JPG", ".xls": "XLS",
         ".xlsx": "XLS"}


# Returns (badge text, group name, colour) for a file name.
def type_info(name):
  ext = os.path.splitext(name)[1].lower()
  for group, _, color, exts in GROUPS:
    if ext in exts:
      return CODES.get(ext, "FILE"), group, color
  return "FILE", "Other", "#64748b"


# ---------- Network code ----------
# Send a message: 4 bytes for its length, then the message itself.
def send_msg(sock, data: bytes):
  sock.sendall(struct.pack(">I", len(data)) + data)


# Read exactly n bytes (the network may deliver them in small pieces).
def recv_exact(sock, n: int) -> bytes:
  buf = bytearray()
  while len(buf) < n:
    chunk = sock.recv(n - len(buf))
    # Empty means the other side closed the connection.
    if not chunk:
      raise ConnectionError("Socket closed before full payload arrived")
    buf.extend(chunk)
  return bytes(buf)


# Read one whole message: its length first, then its content.
def recv_msg(sock) -> bytes:
  n = struct.unpack(">I", recv_exact(sock, 4))[0]
  return recv_exact(sock, n) if n else b""


# Connect to one server, send a message, return the reply, then close.
def exchange(host, port, payload, timeout=4.0):
  s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
  # Do not wait forever.
  s.settimeout(timeout)
  try:
    s.connect((host, port))
    send_msg(s, payload)
    return recv_msg(s)
  finally:
    s.close()


# The two servers. Sends requests and switches server when one crashes.
# Safe to use from several threads (it uses a lock).
class Cluster:
  # active: 0 = Primary, 1 = Backup.
  def __init__(self):
    self.active = 0
    self.lock = threading.Lock()

  # Send a request. Try the active server first, then the other one.
  # Returns (reply, events). The events tell the screen what happened:
  #   ('crash', i)    server i did not answer
  #   ('failover', i) the other server answered, so we switched to it
  #   ('down', None)  both servers failed (reply is None)
  def request(self, payload):
    """Returns (reply_or_None, events).
    Events: ('crash',i) ('failover',i) ('down',None)"""
    with self.lock:
      # Remember where we started; another thread may switch servers.
      start = self.active
    events = []
    # First the active server, then the other one.
    for attempt, idx in enumerate((start, 1 - start)):
      _, host, port = NODES[idx]
      try:
        reply = exchange(host, port, payload)
      except Exception:
        if attempt == 0:
          events.append(("crash", idx))
        continue
      # The other server worked, so use it from now on.
      if attempt == 1:
        with self.lock:
          if self.active == start:
            self.active = idx
        events.append(("failover", idx))
      return reply, events
    # Both servers failed.
    events.append(("down", None))
    return None, events

  # How fast one server answers, in ms. None means it is offline.
  def probe(self, idx):
    t = time.time()
    try:
      exchange(NODES[idx][1], NODES[idx][2], b"LIST", timeout=1.5)
      return max(1, int((time.time() - t) * 1000))
    except Exception:
      return None


# Load the STASH logo for a square of `size` pixels.
# Needs logo.png (and logo_84.png, logo_32.png if Pillow is missing)
# in the same folder. Returns None if not found; a plain "N" is drawn instead.
def load_logo(size):
  s = px(size)
  here = os.path.dirname(os.path.abspath(__file__))
  try:
    if Image:
      im = Image.open(os.path.join(here, "logo.png")).convert("RGBA")
      resample = (Image.Resampling.LANCZOS if hasattr(Image, "Resampling")
                  else Image.LANCZOS)
      return ImageTk.PhotoImage(im.resize((s, s), resample))
    name = "logo_84.png" if size > 40 else "logo_32.png"
    return tk.PhotoImage(file=os.path.join(here, name))
  except Exception:
    return None


# ---------- Small widgets ----------
# Screen scale: 1.0 normally, bigger on high-resolution screens.
UI_SCALE = 1.0


# Make a size bigger on high-resolution screens.
def px(n):
  return int(round(n * UI_SCALE))


# Font name. Replaced by the best font the computer has.
FAMILY = "TkDefaultFont"


# Make a font: (name, size) or (name, size, bold).
def F(size=10, bold=False):
  return (FAMILY, size, "bold") if bold else (FAMILY, size)


# Mix colour fg into colour bg by amount a (0 to 1) for soft colours.
def tint(fg, bg, a):
  f = [int(fg[i:i + 2], 16) for i in (1, 3, 5)]
  b = [int(bg[i:i + 2], 16) for i in (1, 3, 5)]
  return "#%02x%02x%02x" % tuple(
      int(b[i] + (f[i] - b[i]) * a) for i in range(3))


# Draw a rectangle with rounded corners.
def rrect(cv, x1, y1, x2, y2, r, **kw):
  pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2, x2 - r,
         y2,
         x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
  return cv.create_polygon(pts, smooth=True, **kw)


# Small rounded badge with the file type text (like PDF).
def chip(parent, code, color, bg, size=36):
  s = px(size)
  cv = tk.Canvas(parent, width=s, height=s, bg=bg, highlightthickness=0, bd=0)
  rrect(cv, 1, 1, s - 1, s - 1, px(9), fill=tint(color, SURF, 0.14), outline="")
  cv.create_text(s / 2, s / 2, text=code, fill=color,
                 font=F(max(7, int(size / 4.6)), True))
  return cv


# Flat button. Built from a Label so colours look the same everywhere.
# kind: primary, soft, ghost, danger, side.
class Btn(tk.Label):
  KINDS = {
      "primary": (ACCENT, "#ffffff", ACCENT_D),
      "soft": (SOFT, ACCENT, "#e2d9f6"),
      "ghost": (None, TEXT, "#eee9f8"),
      "danger": ("#fdeeee", DANGER, "#fbdcdc"),
      "side": (SIDE_HOVER, SIDE_TEXT, "#3a2f68"),
  }

  # Set the colours and the click action.
  def __init__(self, parent, text, command, kind="soft", bg=None, wide=False,
               **kw):
    base, fg, hover = self.KINDS[kind]
    self.base = base or bg or parent.cget("bg")
    self.fg, self.hover, self.command, self.enabled = fg, hover, command, True
    super().__init__(parent, text=text, bg=self.base, fg=fg, font=F(10, True),
                     cursor="hand2",
                     padx=px(14), pady=px(7), **kw)
    self.bind("<Enter>", lambda e: self.enabled and self.config(bg=self.hover))
    self.bind("<Leave>", lambda e: self.config(bg=self.base))
    self.bind("<ButtonRelease-1>", self._click)

  # Count it as a click only if the mouse is released over the button.
  def _click(self, e):
    if (self.enabled and 0 <= e.x <= self.winfo_width()
        and 0 <= e.y <= self.winfo_height()):
      self.command()

  # Grey the button out (False) or turn it on again (True).
  def set_enabled(self, on):
    self.enabled = on
    self.config(fg=self.fg if on else "#b3abc9",
                cursor="hand2" if on else "arrow", bg=self.base)


# Text box with a label. The border turns purple when you click inside.
# Use show="•" for a password box with a Show/Hide link.
class Field(tk.Frame):
  def __init__(self, parent, label, show=None, bg=SURF):
    super().__init__(parent, bg=bg)
    tk.Label(self, text=label, bg=bg, fg=MUTED,
             font=F(9, True)).pack(anchor="w")
    self.box = tk.Frame(self, bg=BORDER, padx=1, pady=1)
    self.box.pack(fill="x", pady=(px(5), 0))
    inner = tk.Frame(self.box, bg="#ffffff")
    inner.pack(fill="x")
    self.hidden = show is not None
    self.e = tk.Entry(inner, relief="flat", bd=0, show=show or "", font=F(11),
                      bg="#ffffff", fg=TEXT,
                      insertbackground=TEXT, highlightthickness=0)
    self.e.pack(side="left", fill="x", expand=True, padx=px(10), pady=px(9))
    if show is not None:
      eye = tk.Label(inner, text="Show", bg="#ffffff", fg=MUTED, font=F(9),
                     cursor="hand2")
      eye.pack(side="right", padx=px(10))
      eye.bind("<Button-1>", lambda e: self._toggle(eye))
    self.e.bind("<FocusIn>", lambda e: self.box.config(bg=ACCENT))
    self.e.bind("<FocusOut>", lambda e: self.box.config(bg=BORDER))

  # Switch the password between dots and readable text.
  def _toggle(self, eye):
    self.hidden = not self.hidden
    self.e.config(show="•" if self.hidden else "")
    eye.config(text="Show" if self.hidden else "Hide")

  # The typed text, without spaces at the start or end.
  def get(self):
    return self.e.get().strip()


# Pop-up window (rename, delete, replace, users). Esc closes it.
class Modal(tk.Toplevel):
  def __init__(self, app, title, width=420):
    super().__init__(app.root)
    self.withdraw()
    self.app = app
    self.title(title)
    self.configure(bg=SURF)
    self.resizable(False, False)
    self.transient(app.root)
    self.width = px(width)
    self.body = tk.Frame(self, bg=SURF, padx=px(26), pady=px(24))
    self.body.pack(fill="both", expand=True)
    self.bind("<Escape>", lambda e: self.destroy())

  # Add a title and small grey text.
  def heading(self, text, sub=None):
    tk.Label(self.body, text=text, bg=SURF, fg=TEXT, font=F(14, True),
             anchor="w").pack(fill="x")
    if sub:
      tk.Label(self.body, text=sub, bg=SURF, fg=MUTED, font=F(10), anchor="w",
               justify="left",
               wraplength=self.width - px(60)).pack(fill="x", pady=(px(6), 0))

  # Put the buttons in one row at the bottom right.
  def buttons(self, *btns):
    row = tk.Frame(self.body, bg=SURF)
    row.pack(fill="x", side="bottom", pady=(px(22), 0))
    for b in reversed(btns):
      b.pack(in_=row, side="right", padx=(px(8), 0))
      b.lift()

  # Centre the pop-up over the main window and show it.
  def show(self):
    self.update_idletasks()
    r = self.app.root
    w = max(self.width, self.winfo_reqwidth())
    h = self.winfo_reqheight()
    x = r.winfo_rootx() + (r.winfo_width() - w) // 2
    y = max(0, r.winfo_rooty() + (r.winfo_height() - h) // 3)
    self.geometry(f"{w}x{h}+{x}+{y}")
    self.deiconify()
    self.lift()
    try:
      self.wait_visibility()
      self.grab_set()
    except tk.TclError:
      pass
    self.focus_set()


# ---------- The app ----------
# Each screen (login, main) is drawn by a show_... method.
# Rule: only the main thread may change widgets. Slow work (network) runs in
# a background thread and sends its result back through a queue.
class NotesDriveApp:
  # Set up the window, fonts, state and shortcuts, then show the login screen.
  def __init__(self, root):
    global UI_SCALE, FAMILY
    self.root = root
    root.title("STASH")
    UI_SCALE = max(1.0, root.winfo_fpixels("1i") / 96.0)
    avail = set(tkfont.families())
    FAMILY = next(
        (f for f in ("Segoe UI", "Inter", "SF Pro Text", "Helvetica Neue",
                     "Ubuntu", "DejaVu Sans") if f in avail),
        "TkDefaultFont")
    root.geometry(f"{px(1180)}x{px(720)}")
    root.minsize(px(980), px(600))
    root.configure(bg=BG)
    # Colours for the scrollbar and the loading bar.
    st = ttk.Style()
    st.theme_use("clam")
    st.configure("Thin.Horizontal.TProgressbar", troughcolor=BORDER,
                 background=ACCENT, thickness=3,
                 borderwidth=0, lightcolor=ACCENT, darkcolor=ACCENT)
    st.configure("Vertical.TScrollbar", troughcolor=SURF, background="#cfc7e3",
                 bordercolor=SURF,
                 arrowcolor=SURF, lightcolor="#cfc7e3", darkcolor="#cfc7e3",
                 relief="flat")

    # State: user, server status, file list, current filter and sort.
    self.cluster = Cluster()
    self.user = self.role = None
    self.q = queue.Queue()
    self.status = [None, None]          # latency in ms, None = offline
    self.probed = False
    self.auto_id = 0
    self.node_rows = []
    self.files, self.owner_of = [], {}
    self.sig = None
    self.selected = None
    self.filter, self.sort, self.sort_rev = "all", "name", False
    self.busy_count, self.toasts = 0, []
    self.banner_frame, self.banner_kind = None, None
    self.preview_token = 0
    self.screen = "auth"
    self.last_down_toast = 0
    self.rows = {}
    self.prefill_user = ""

    # Keyboard shortcuts (only on the main screen).
    root.bind("<Control-u>", lambda e: self.screen == "dash" and self.upload())
    root.bind("<Control-f>", lambda e: self.screen == "dash"
              and self.search_entry.focus_set())
    root.bind("<F5>", lambda e: self.screen == "dash" and self.refresh())
    root.bind("<F2>", lambda e: self.screen == "dash" and self.rename())
    root.bind("<Delete>", lambda e: self.screen == "dash"
              and not isinstance(root.focus_get(), tk.Entry)
              and self.delete())

    self.pump()
    threading.Thread(target=self.monitor, daemon=True).start()
    self.show_auth("login")

  # Runs every 40 ms on the main thread. Does the screen updates that the
  # background threads left in the queue.
  def pump(self):
    try:
      while True:
        try:
          # Take one waiting job from the queue and run it.
          self.q.get_nowait()()
        except queue.Empty:
          break
        except Exception:
          traceback.print_exc()
    finally:
      self.root.after(40, self.pump)

  # Send a request without freezing the window.
  # When the reply arrives, done(reply) is called. reply is None if both
  # servers failed.
  def call(self, payload, done):
    self.set_busy(1)

    def work():
      reply, events = self.cluster.request(payload)
      # Back on the main thread: update the screen, then call done.
      self.q.put(lambda: (self.set_busy(-1), self.on_events(events),
                          done(reply)))

    # Run the slow network call in the background.
    threading.Thread(target=work, daemon=True).start()

  # Background thread: every 3 seconds, check how both servers are doing.
  def monitor(self):
    while True:
      for i in (0, 1):
        self.status[i] = self.cluster.probe(i)
      self.probed = True
      self.q.put(self.on_status)
      time.sleep(3)

  # Show the thin loading bar while a request is running.
  def set_busy(self, d):
    self.busy_count = max(0, self.busy_count + d)
    bar = getattr(self, "progress", None)
    try:
      if bar and bar.winfo_exists():
        if self.busy_count and not bar.winfo_ismapped():
          bar.pack(fill="x", side="bottom")
          bar.start(12)
        elif not self.busy_count and bar.winfo_ismapped():
          bar.stop()
          bar.pack_forget()
    except tk.TclError:
      pass

  # Remove everything on screen so a new screen can be drawn.
  def clear(self):
    self.root.unbind("<MouseWheel>")
    for w in self.root.winfo_children():
      if not getattr(w, "keep", False):
        w.destroy()
    self.node_rows, self.rows, self.banner_frame, self.toasts = [], {}, None, []
    self.progress = None

  # ---------- Messages to the user ----------
  # Small message in the bottom right corner. kind: ok, err, warn, info.
  def toast(self, text, kind="ok", ms=3800):
    color = {"ok": OK, "err": DANGER, "warn": WARN, "info": ACCENT}[kind]
    t = tk.Frame(self.root, bg=SURF, highlightbackground=BORDER,
                 highlightthickness=1)
    tk.Frame(t, bg=color, width=px(4)).pack(side="left", fill="y")
    tk.Label(t, text=text, bg=SURF, fg=TEXT, font=F(10), wraplength=px(300),
             justify="left",
             padx=px(14), pady=px(11)).pack(side="left")
    self.toasts.append(t)
    self.layout_toasts()
    self.root.after(ms, lambda: self.drop_toast(t))

  # Remove a toast and move the others down.
  def drop_toast(self, t):
    if t in self.toasts:
      self.toasts.remove(t)
    try:
      t.destroy()
    except tk.TclError:
      pass
    self.layout_toasts()

  # Stack the toasts up from the bottom right corner.
  def layout_toasts(self):
    y = px(46)
    for t in reversed(self.toasts):
      try:
        t.place(relx=1.0, rely=1.0, x=-px(20), y=-y, anchor="se")
        t.lift()
        t.update_idletasks()
        y += t.winfo_reqheight() + px(10)
      except tk.TclError:
        pass

  # Coloured bar above the file list (crash, failover, Primary is back).
  # kind: danger, warn or back. Can have one button.
  def banner(self, kind, title, text, action=None):
    if self.screen != "dash" or not hasattr(self, "banner_host"):
      return
    self.clear_banner()
    color = {"danger": DANGER, "warn": WARN,
             "ok": OK}[kind.replace("back", "ok")]
    f = tk.Frame(self.banner_host, bg=tint(color, SURF, 0.10),
                 highlightbackground=tint(color, SURF, 0.35),
                 highlightthickness=1)
    f.pack(fill="x", padx=px(28), pady=(0, px(12)))
    tk.Frame(f, bg=color, width=px(5)).pack(side="left", fill="y")
    box = tk.Frame(f, bg=f.cget("bg"))
    box.pack(side="left", fill="x", expand=True, padx=px(14), pady=px(10))
    tk.Label(box, text=title, bg=f.cget("bg"), fg=color, font=F(10, True),
             anchor="w").pack(fill="x")
    tk.Label(box, text=text, bg=f.cget("bg"), fg=TEXT, font=F(10), anchor="w",
             justify="left",
             wraplength=px(620)).pack(fill="x")
    if action:
      Btn(f, action[0], action[1], "primary").pack(side="right", padx=px(12))
    self.banner_frame, self.banner_kind = f, kind

  # Remove the message bar.
  def clear_banner(self):
    if self.banner_frame:
      try:
        self.banner_frame.destroy()
      except tk.TclError:
        pass
    self.banner_frame, self.banner_kind = None, None

  # Show the user what happened with the servers:
  #   crash    -> red "Server Crash Detected" bar
  #   failover -> yellow bar: now using the other server
  #   down     -> red bar: both servers are unreachable
  def on_events(self, events):
    for ev, idx in events:
      if ev == "crash":
        n, h, p = NODES[idx]
        self.banner("danger", "Server Crash Detected",
                    f"The {n} server ({h}:{p}) has crashed or is "
                    f"unreachable. Switching to {NODES[1 - idx][0]}…")
      elif ev == "failover":
        n = NODES[idx][0]
        self.banner("warn", f"Failover complete - now using the {n} server",
                    "Your session continues with no action needed. The "
                    "other server re-syncs from this one "
                    "when it is restarted.")
        self.toast(f"Switched to the {n} server", "warn")
        self.refresh_nodes()
      elif ev == "down":
        self.banner("danger", "Both servers are unreachable",
                    "Start server.py and the client will reconnect.")
        if time.time() - self.last_down_toast > 10:
          self.last_down_toast = time.time()
          self.toast("Both Primary and Backup servers are unreachable.", "err")

  # Runs after every server check. Updates the status panel, and:
  #  - active server died -> switch to the other one now
  #  - on Backup and Primary is back -> offer to switch back
  #  - on a healthy Primary -> remove old warnings
  def on_status(self):
    self.refresh_nodes()
    if self.screen != "dash":
      return
    a = self.cluster.active
    if self.status[a] is None and self.status[1 - a] is not None:
      self.refresh(silent=True)
    elif a != 0 and self.status[0] is not None and self.banner_kind != "back":
      self.banner("back", "The Primary server is back online",
                  "You are still on the Backup. Switch back whenever you like.",
                  ("Switch to Primary", self.switch_primary))
    elif a == 0 and self.status[0] is not None and self.banner_kind in ("warn",
        "back", "danger"):
      self.clear_banner()

  # Go back to the Primary after it has recovered.
  def switch_primary(self):
    self.cluster.active = 0
    self.clear_banner()
    self.refresh_nodes()
    self.toast("Now using the Primary server", "ok")
    self.refresh(silent=True)

  # Draw one row per server (dot, name, speed). Used on login and sidebar.
  def make_node_rows(self, parent, bg, fg, muted):
    for i, (name, host, port) in enumerate(NODES):
      row = tk.Frame(parent, bg=bg)
      row.pack(fill="x", pady=px(3))
      dot = tk.Label(row, text="●", bg=bg, fg=MUTED, font=F(10))
      dot.pack(side="left")
      col = tk.Frame(row, bg=bg)
      col.pack(side="left", padx=px(8))
      title = tk.Label(col, text=name, bg=bg, fg=fg, font=F(10, True),
                       anchor="w")
      title.pack(anchor="w")
      right = tk.Label(row, text="", bg=bg, fg=muted, font=F(9))
      right.pack(side="right")
      self.node_rows.append((dot, right, title, bg, fg))
    self.refresh_nodes()

  # Update the rows: green = online, red = offline, plus speed and ACTIVE.
  def refresh_nodes(self):
    for i, (dot, right, title, bg, fg) in enumerate(self.node_rows):
      ms = self.status[i]
      try:
        dot.config(fg=OK if ms else (DANGER if self.probed else MUTED))
        active = self.cluster.active == i
        title.config(text=NODES[i][0] + ("  ACTIVE" if active else ""))
        right.config(
            text=(f"{ms} ms" if ms
                  else ("offline" if self.probed else "checking…")),
            fg=SIDE_MUTED if bg == SIDE else MUTED)
      except tk.TclError:
        pass

  # ---------- Login and register ----------
  # Draw the login screen (mode="login") or the register screen.
  def show_auth(self, mode):
    self.clear()
    self.screen = "auth"
    self.root.configure(bg=SURF)
    # Left: dark panel with server status. Right: the form.
    left = tk.Frame(self.root, bg=SIDE)
    left.place(relx=0, rely=0, relwidth=0.42, relheight=1)
    inner = tk.Frame(left, bg=SIDE)
    inner.place(relx=0.5, rely=0.5, anchor="center", relwidth=0.78)
    img = load_logo(84)
    if img:
      logo = tk.Label(inner, image=img, bg=SIDE, bd=0)
      logo.image = img        # keep a reference, or Tk drops the picture
    else:                     # fallback: the built-in "N" badge
      logo = tk.Canvas(inner, width=px(84), height=px(84), bg=SIDE,
                       highlightthickness=0)
      rrect(logo, 1, 1, px(83), px(83), px(24), fill=ACCENT, outline="")
      logo.create_text(px(42), px(43), text="N", fill="white",
                       font=F(38, True))
    logo.pack(anchor="w")
    tk.Label(inner, text="STASH", bg=SIDE, fg="white", font=F(40, True),
             justify="left", anchor="w").pack(anchor="w",
                                              pady=(px(24), px(4)))
    tk.Label(inner, text="Always Synced", bg=SIDE, fg=SIDE_MUTED,
             font=F(14), justify="left", anchor="w").pack(anchor="w")
    card = tk.Frame(inner, bg=SIDE_HOVER, padx=px(18), pady=px(12))
    card.pack(fill="x", pady=(px(34), 0))
    self.make_node_rows(card, SIDE_HOVER, SIDE_TEXT, SIDE_MUTED)

    right = tk.Frame(self.root, bg=SURF)
    right.place(relx=0.42, rely=0, relwidth=0.58, relheight=1)
    form = tk.Frame(right, bg=SURF)
    form.place(relx=0.5, rely=0.5, anchor="center", width=px(380))
    # One screen for both forms: True = sign in, False = register.
    login = mode == "login"
    tk.Label(form, text="Welcome back" if login else "Create your account",
             bg=SURF, fg=TEXT,
             font=F(22, True), anchor="w").pack(fill="x")
    tk.Label(form,
             text=("Sign in to your notes." if login
                   else "Pick a username and password "
                        "to start sharing notes."),
             bg=SURF, fg=MUTED, font=F(10), anchor="w").pack(
                 fill="x", pady=(px(4), px(24)))
    self.f_user = Field(form, "Username")
    self.f_user.pack(fill="x", pady=(0, px(14)))
    self.f_pass = Field(form, "Password", show="•")
    self.f_pass.pack(fill="x")
    self.f_conf = None
    if not login:
      self.f_conf = Field(form, "Confirm password", show="•")
      self.f_conf.pack(fill="x", pady=(px(14), 0))
    self.f_user.e.insert(0, self.prefill_user)
    self.submit_btn = Btn(form, "Sign in" if login else "Create account",
                          self.do_login if login else self.do_register,
                          "primary", anchor="center")
    self.submit_btn.config(pady=px(10), font=F(11, True))
    self.submit_btn.pack(fill="x", pady=(px(22), px(14)))
    alt = tk.Label(form,
                   text=("New here? Create an account" if login
                         else "Already registered? Sign in"),
                   bg=SURF, fg=ACCENT, font=F(10, True), cursor="hand2")
    alt.pack()
    alt.bind("<Button-1>",
             lambda e: self.show_auth("register" if login else "login"))
    for f in (self.f_user, self.f_pass, self.f_conf):
      if f:
        f.e.bind("<Return>", lambda e: self.submit_btn.enabled
                 and self.submit_btn.command())
    (self.f_pass if self.prefill_user else self.f_user).e.focus_set()

  # Check the fields, send LOGIN, open the main screen if it works.
  def do_login(self):
    u, p = self.f_user.get(), self.f_pass.get()
    if not u or not p:
      return self.toast("Enter your username and password.", "warn")
    if ":" in u:
      return self.toast("Usernames cannot contain ':'.", "warn")
    # Turn the button off while waiting so it is not sent twice.
    self.submit_btn.config(text="Signing in…")
    self.submit_btn.set_enabled(False)

    # Handle the server's answer.
    def done(res):
      if res and res.startswith(b"SUCCESS"):
        self.user, self.role = u, res.decode().split(":", 1)[1]
        self.prefill_user = u
        self.show_dashboard()
      else:
        try:
          self.submit_btn.config(text="Sign in")
          self.submit_btn.set_enabled(True)
        except tk.TclError:
          pass
        if res is not None:
          self.toast(res.decode().replace("ERROR: ", "") or "Login failed",
                     "err")

    # Command format: LOGIN:username:password
    self.call(f"LOGIN:{u}:{p}".encode(), done)

  # Check the fields, then send REGISTER.
  def do_register(self):
    u, p, c = self.f_user.get(), self.f_pass.get(), self.f_conf.get()
    if not u or not p:
      return self.toast("Fill in every field.", "warn")
    if ":" in u:
      return self.toast("Usernames cannot contain ':'.", "warn")
    if len(p) < 4:
      return self.toast("Use a password of at least 4 characters.", "warn")
    if p != c:
      return self.toast("The two passwords don't match.", "warn")

    # Handle the server's answer.
    def done(res):
      if res and b"SUCCESS" in res:
        self.prefill_user = u
        self.show_auth("login")
        self.toast("Account created. Sign in to continue.", "ok")
      elif res is not None:
        self.toast(res.decode().replace("ERROR: ", ""), "err")

    self.call(f"REGISTER:{u}:{p}".encode(), done)

  # Sign out and go back to the login screen.
  def logout(self):
    self.user = self.role = None
    self.files, self.owner_of, self.sig, self.selected = [], {}, None, None
    self.show_auth("login")

  # ---------- Main screen ----------
  # Draw it: sidebar (left), file list (middle), details (right).
  def show_dashboard(self):
    self.clear()
    self.screen = "dash"
    self.root.configure(bg=BG)

    side = tk.Frame(self.root, bg=SIDE, width=px(250))
    side.pack(side="left", fill="y")
    # Keep the sidebar at a fixed width.
    side.pack_propagate(False)
    top = tk.Frame(side, bg=SIDE)
    top.pack(fill="x", padx=px(18), pady=(px(22), px(14)))
    img = load_logo(32)
    if img:
      logo = tk.Label(top, image=img, bg=SIDE, bd=0)
      logo.image = img        # keep a reference, or Tk drops the picture
    else:                     # fallback: the built-in "N" badge
      logo = tk.Canvas(top, width=px(32), height=px(32), bg=SIDE,
                       highlightthickness=0)
      rrect(logo, 1, 1, px(31), px(31), px(9), fill=ACCENT, outline="")
      logo.create_text(px(16), px(16), text="N", fill="white",
                       font=F(13, True))
    logo.pack(side="left")
    tk.Label(top, text="STASH", bg=SIDE, fg="white",
             font=F(14, True)).pack(side="left", padx=px(10))
    up = Btn(side, "+  Upload notes", self.upload, "primary", anchor="center")
    up.config(pady=px(10))
    up.pack(fill="x", padx=px(18), pady=(0, px(16)))

    self.nav = {}
    items = [("all", "All files", MUTED), ("mine", "My uploads", MUTED)]
    for key, label, color in items:
      self.nav_item(side, key, label, SIDE_MUTED)
    tk.Label(side, text="Types", bg=SIDE, fg=SIDE_MUTED, font=F(9, True),
             anchor="w").pack(
        fill="x", padx=px(22), pady=(px(16), px(4)))
    for group, label, color, _ in GROUPS:
      self.nav_item(side, group, label, color)
    if self.role == "admin":
      tk.Label(side, text="Admin", bg=SIDE, fg=SIDE_MUTED, font=F(9, True),
               anchor="w").pack(
          fill="x", padx=px(22), pady=(px(16), px(4)))
      self.nav_item(side, "_users", "Manage users", SIDE_MUTED, count=False,
                    command=self.manage_users)

    foot = tk.Frame(side, bg=SIDE)
    foot.pack(side="bottom", fill="x", padx=px(14), pady=px(14))
    me = tk.Frame(foot, bg=SIDE)
    me.pack(fill="x", pady=(px(12), 0))
    av = tk.Canvas(me, width=px(34), height=px(34), bg=SIDE,
                   highlightthickness=0)
    av.create_oval(1, 1, px(33), px(33), fill=ACCENT, outline="")
    av.create_text(px(17), px(17), text=self.user[:1].upper(), fill="white",
                   font=F(12, True))
    av.pack(side="left")
    who = tk.Frame(me, bg=SIDE)
    who.pack(side="left", padx=px(10))
    tk.Label(who, text=self.user, bg=SIDE, fg="white", font=F(10, True),
             anchor="w").pack(anchor="w")
    tk.Label(who, text=self.role.capitalize(), bg=SIDE, fg=SIDE_MUTED,
             font=F(9), anchor="w").pack(anchor="w")
    out = Btn(me, "Sign out", self.logout, "side")
    out.config(font=F(9, True), padx=px(10), pady=px(5))
    out.pack(side="right")
    cl = tk.Frame(foot, bg=SIDE_HOVER, padx=px(12), pady=px(8))
    cl.pack(fill="x", side="top")
    self.make_node_rows(cl, SIDE_HOVER, SIDE_TEXT, SIDE_MUTED)

    # Details panel (right side).
    self.det = tk.Frame(self.root, bg=SURF, width=px(340),
                        highlightbackground=BORDER, highlightthickness=1)
    self.det.pack(side="right", fill="y")
    self.det.pack_propagate(False)

    # Middle column: title, search, message bar and file list.
    main = tk.Frame(self.root, bg=BG)
    main.pack(side="left", fill="both", expand=True)
    head = tk.Frame(main, bg=BG)
    head.pack(fill="x", padx=px(28), pady=(px(24), px(14)))
    tt = tk.Frame(head, bg=BG)
    tt.pack(side="left")
    self.title_lbl = tk.Label(tt, text="All files", bg=BG, fg=TEXT,
                              font=F(20, True), anchor="w")
    self.title_lbl.pack(anchor="w")
    self.sub_lbl = tk.Label(tt, text="", bg=BG, fg=MUTED, font=F(10),
                            anchor="w")
    self.sub_lbl.pack(anchor="w")
    sbox = tk.Frame(head, bg=BORDER, padx=1, pady=1)
    sbox.pack(side="right")
    sin = tk.Frame(sbox, bg="#ffffff")
    sin.pack()
    # The list is redrawn on every key typed in the search box.
    self.search_var = tk.StringVar()
    self.search_entry = tk.Entry(sin, textvariable=self.search_var, width=26,
                                 relief="flat", bd=0,
                                 font=F(10), bg="#ffffff", fg=TEXT,
                                 insertbackground=TEXT, highlightthickness=0)
    self.search_entry.pack(padx=px(12), pady=px(8))
    ph = tk.Label(sin, text="Search files   Ctrl+F", bg="#ffffff", fg="#a39cb8",
                  font=F(10))
    ph.place(x=px(12), rely=0.5, anchor="w")
    ph.bind("<Button-1>", lambda e: self.search_entry.focus_set())
    self.search_var.trace_add(
        "write",
        lambda *a: (ph.place_forget() if self.search_var.get() else
                    ph.place(x=px(12), rely=0.5, anchor="w"),
                    self.render_list()))
    self.search_entry.bind("<FocusIn>", lambda e: sbox.config(bg=ACCENT))
    self.search_entry.bind("<FocusOut>", lambda e: sbox.config(bg=BORDER))
    self.search_entry.bind(
        "<Escape>",
        lambda e: (self.search_var.set(""), self.root.focus_set()))

    self.banner_host = tk.Frame(main, bg=BG)
    self.banner_host.pack(fill="x")

    card = tk.Frame(main, bg=SURF, highlightbackground=BORDER,
                    highlightthickness=1)
    card.pack(fill="both", expand=True, padx=px(28), pady=(0, px(14)))
    self.hdr = tk.Frame(card, bg=SURF)
    self.hdr.pack(fill="x")
    self.col_lbls = {}
    spec = [("name", "Name", 2, "w"), ("owner", "Owner", 3, "w"),
            ("type", "Type", 4, "w"), ("acc", "Access", 5, "w")]
    for key, text, col, anc in spec:
      l = tk.Label(self.hdr, text=text, bg=SURF, fg=MUTED, font=F(9, True),
                   anchor=anc, cursor="hand2" if key != "acc" else "arrow")
      l.grid(row=0, column=col, sticky="ew", pady=px(10), padx=(0, px(8)))
      if key != "acc":
        l.bind("<Button-1>", lambda e, k=key: self.set_sort(k))
      self.col_lbls[key] = l
    self.grid_cols(self.hdr)
    tk.Frame(card, bg=BORDER, height=1).pack(fill="x")

    body = tk.Frame(card, bg=SURF)
    body.pack(fill="both", expand=True)
    # Scrolling list: only a canvas can scroll, so the rows are put
    # in a frame that sits inside the canvas.
    self.cv = tk.Canvas(body, bg=SURF, highlightthickness=0, bd=0)
    sb = ttk.Scrollbar(body, orient="vertical", command=self.cv.yview)
    self.cv.configure(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y")
    self.cv.pack(side="left", fill="both", expand=True)
    self.list_inner = tk.Frame(self.cv, bg=SURF)
    win = self.cv.create_window(0, 0, window=self.list_inner, anchor="nw")
    self.cv.bind("<Configure>",
                 lambda e: self.cv.itemconfig(win, width=e.width))
    self.list_inner.bind(
        "<Configure>",
        lambda e: self.cv.configure(scrollregion=self.cv.bbox("all")))
    # Mouse wheel scrolls the list (Linux uses Button-4 and Button-5).
    self.root.bind(
        "<MouseWheel>",
        lambda e: self.cv.yview_scroll(-1 if e.delta > 0 else 1, "units"))
    self.root.bind("<Button-4>", lambda e: self.cv.yview_scroll(-1, "units"))
    self.root.bind("<Button-5>", lambda e: self.cv.yview_scroll(1, "units"))

    self.progress = ttk.Progressbar(main, style="Thin.Horizontal.TProgressbar",
                                    mode="indeterminate")
    self.set_sort_arrows()
    self.show_details()
    self.render_list(loading=True)
    self.refresh()
    self.auto_id += 1
    self.auto_refresh(self.auto_id)

  # Same column widths for the header and every row, so they line up.
  def grid_cols(self, f):
    f.grid_columnconfigure(0, minsize=px(4))
    f.grid_columnconfigure(1, minsize=px(60))
    f.grid_columnconfigure(2, weight=1)
    f.grid_columnconfigure(3, minsize=px(120))
    f.grid_columnconfigure(4, minsize=px(110))
    f.grid_columnconfigure(5, minsize=px(100))

  # One clickable sidebar entry (a filter) with a file count.
  def nav_item(self, parent, key, label, color, count=True, command=None):
    row = tk.Frame(parent, bg=SIDE, cursor="hand2")
    row.pack(fill="x", padx=px(10), pady=1)
    dot = tk.Label(
        row,
        text=("●" if key not in ("all", "mine")
              else ("▤" if key == "all" else "☺")),
        bg=SIDE, fg=color, font=F(9))
    dot.pack(side="left", padx=(px(10), px(8)), pady=px(7))
    name = tk.Label(row, text=label, bg=SIDE, fg=SIDE_TEXT, font=F(10),
                    anchor="w")
    name.pack(side="left")
    cnt = tk.Label(row, text="", bg=SIDE, fg=SIDE_MUTED, font=F(9))
    if count:
      cnt.pack(side="right", padx=px(12))
    widgets = (row, dot, name, cnt)

    def paint(c):
      for w in widgets:
        w.config(bg=c)

    for w in widgets:
      w.bind("<Enter>", lambda e: paint(
          "#3a2f68" if self.filter == key else SIDE_HOVER))
      w.bind("<Leave>", lambda e: self.restyle_nav())
      w.bind("<Button-1>",
             lambda e: command() if command else self.set_filter(key))
    self.nav[key] = (widgets, label)

  # Highlight the selected sidebar entry.
  def restyle_nav(self):
    for key, (widgets, _) in self.nav.items():
      sel = key == self.filter
      for w in widgets:
        try:
          w.config(bg="#3a2f68" if sel else SIDE)
        except tk.TclError:
          pass
      try:
        widgets[2].config(font=F(10, sel), fg="white" if sel else SIDE_TEXT)
      except tk.TclError:
        pass

  # Choose what the list shows (all, mine, or one file type).
  def set_filter(self, key):
    self.filter = key
    self.render_list()

  # Click a column title to sort. Click again to reverse.
  def set_sort(self, key):
    self.sort_rev = (not self.sort_rev) if self.sort == key else False
    self.sort = key
    self.set_sort_arrows()
    self.render_list()

  # Show the arrow on the column used for sorting.
  def set_sort_arrows(self):
    names = {"name": "Name", "owner": "Owner", "type": "Type"}
    for k, l in self.col_lbls.items():
      if k in names:
        arrow = (("  ▼" if self.sort_rev else "  ▲")
                 if k == self.sort else "")
        l.config(text=names[k] + arrow, fg=TEXT if k == self.sort else MUTED)

  # Same rule as the server: admins can change any file, others only their own.
  def can_edit(self, owner):
    return self.role == "admin" or owner == self.user

  # Load the file list from the server. Redraw only if it changed.
  # after: a function to run when done (optional).
  def refresh(self, silent=False, after=None):
    def done(res):
      if res is None or self.screen != "dash":
        return
      files = []
      for line in res.decode("utf-8", "replace").splitlines():
        if line.strip():
          name, _, owner = line.partition(" | uploaded by: ")
          files.append((name.strip(), owner.strip() or "unknown"))
      # If the list is the same as last time, skip the redraw.
      sig = tuple(sorted(files))
      if sig != self.sig:
        self.sig = sig
        self.files, self.owner_of = files, dict(files)
        if self.selected not in self.owner_of:
          self.selected = None
          self.show_details()
        self.render_list()
      if after:
        after()
      if not silent:
        self.toast("File list is up to date", "info", 1500)

    self.call(b"LIST", done)

  # Reload the list every 8 seconds so changes from others show up.
  def auto_refresh(self, chain):
    if self.screen != "dash" or chain != self.auto_id:
      return
    if not self.busy_count:
      self.refresh(silent=True)
    self.root.after(8000, lambda: self.auto_refresh(chain))

  # The files to show now: filtered, searched and sorted.
  def visible(self):
    q = self.search_var.get().strip().lower()
    out = []
    for name, owner in self.files:
      group = type_info(name)[1]
      if self.filter == "mine" and owner != self.user:
        continue
      if self.filter not in ("all", "mine") and group != self.filter:
        continue
      if q and q not in name.lower() and q not in owner.lower():
        continue
      out.append((name, owner))
    # What to sort by, depending on the clicked column.
    keyf = {"name": lambda f: f[0].lower(),
            "owner": lambda f: (f[1].lower(), f[0].lower()),
            "type": lambda f: (type_info(f[0])[1], f[0].lower())}[self.sort]
    return sorted(out, key=keyf, reverse=self.sort_rev)

  # Redraw the file rows, the counts and the title.
  def render_list(self, loading=False):
    if self.screen != "dash":
      return
    for w in self.list_inner.winfo_children():
      w.destroy()
    self.rows = {}
    for key, (widgets, label) in self.nav.items():
      if key == "_users":
        continue
      n = (len(self.files) if key == "all"
           else sum(1 for f in self.files if f[1] == self.user)
           if key == "mine"
           else sum(1 for f in self.files if type_info(f[0])[1] == key))
      widgets[3].config(text=str(n) if n else "")
    self.restyle_nav()
    label = self.nav[self.filter][1] if self.filter in self.nav else "All files"
    self.title_lbl.config(text=label)
    vis = self.visible()
    self.sub_lbl.config(
        text="Loading…" if loading
        else f"{len(vis)} file{'s' if len(vis) != 1 else ''}"
        + (f" matching “{self.search_var.get().strip()}”"
           if self.search_var.get().strip() else ""))
    if not vis:
      box = tk.Frame(self.list_inner, bg=SURF)
      box.pack(fill="x", pady=px(70))
      msg = ("Loading your notes…" if loading else
             ("No matches." if (self.files and (self.search_var.get()
                                                or self.filter != "all"))
              else "No notes here yet."))
      tk.Label(box, text=msg, bg=SURF, fg=TEXT, font=F(14, True)).pack()
      if not loading:
        tk.Label(box, text="Upload a file and it appears on both servers.",
                 bg=SURF, fg=MUTED,
                 font=F(10)).pack(pady=(px(4), px(14)))
        if not self.files:
          Btn(box, "+  Upload notes", self.upload, "primary").pack()
      return
    for name, owner in vis:
      self.build_row(name, owner)

  # Draw one file row.
  # Click = select, double click = download, right click = menu.
  def build_row(self, name, owner):
    code, group, color = type_info(name)
    row = tk.Frame(self.list_inner, bg=SURF, cursor="hand2")
    row.pack(fill="x")
    self.grid_cols(row)
    bar = tk.Frame(row, bg=SURF, width=px(4))
    bar.grid(row=0, column=0, sticky="ns")
    ch = chip(row, code, color, SURF)
    ch.grid(row=0, column=1, pady=px(9))
    nm = tk.Label(row, text=name, bg=SURF, fg=TEXT, font=F(10, True),
                  anchor="w")
    nm.grid(row=0, column=2, sticky="ew", padx=(0, px(8)))
    ow = tk.Label(row, text=owner, bg=SURF,
                  fg=TEXT if owner == self.user else MUTED, font=F(10),
                  anchor="w")
    ow.grid(row=0, column=3, sticky="ew", padx=(0, px(8)))
    ty = tk.Label(row, text=group, bg=SURF, fg=MUTED, font=F(10), anchor="w")
    ty.grid(row=0, column=4, sticky="ew")
    editable = self.can_edit(owner)
    bd = tk.Label(row, text="Can edit" if editable else "View only", bg=SURF,
                  fg=ACCENT if editable else MUTED,
                  font=F(9, True))
    bd.grid(row=0, column=5, sticky="w")
    sep = tk.Frame(self.list_inner, bg="#efebf7", height=1)
    sep.pack(fill="x")
    # A row is several widgets; handle them together.
    parts = (row, bar, ch, nm, ow, ty, bd)
    self.rows[name] = parts

    def paint(c, selected=False):
      for w in parts:
        if w is bar:
          w.config(bg=ACCENT if selected else c)
        else:
          w.config(bg=c)

    for w in parts:
      w.bind("<Enter>", lambda e: name != self.selected and paint("#f8f5fd"))
      w.bind("<Leave>", lambda e: name != self.selected and paint(SURF))
      w.bind("<Button-1>", lambda e: self.select(name))
      w.bind("<Double-Button-1>", lambda e: self.download())
      w.bind("<Button-3>", lambda e: (self.select(name), self.context_menu(e)))
      w.bind("<Button-2>", lambda e: (self.select(name), self.context_menu(e)))
    if name == self.selected:
      paint(SOFT, True)

  # Select a file and show its details.
  def select(self, name):
    old = self.selected
    self.selected = name
    for n, sel in ((old, False), (name, True)):
      parts = self.rows.get(n)
      if parts:
        for w in parts:
          try:
            w.config(bg=(ACCENT if sel else SURF) if w is parts[1]
                     else (SOFT if sel else SURF))
          except tk.TclError:
            pass
    self.show_details()

  # Right-click menu.
  def context_menu(self, e):
    m = tk.Menu(self.root, tearoff=0, font=F(10))
    owner = self.owner_of.get(self.selected, "")
    m.add_command(label="Download", command=self.download)
    m.add_command(label="Rename", command=self.rename,
                  state="normal" if self.can_edit(owner) else "disabled")
    m.add_separator()
    m.add_command(label="Delete", command=self.delete,
                  state="normal" if self.can_edit(owner) else "disabled")
    m.tk_popup(e.x_root, e.y_root)

  # Fill the right panel: file info, buttons and a preview.
  # Only text and image files are downloaded for the preview.
  def show_details(self):
    for w in self.det.winfo_children():
      w.destroy()
    # Changes for every new file, so a late preview of an old file is ignored.
    self.preview_token += 1
    name = self.selected
    pad = px(22)
    if not name or name not in self.owner_of:
      e = tk.Frame(self.det, bg=SURF)
      e.place(relx=0.5, rely=0.45, anchor="center")
      tk.Label(e, text="Select a file", bg=SURF, fg=TEXT,
               font=F(13, True)).pack()
      tk.Label(e, text="Its preview and actions\nshow up here.", bg=SURF,
               fg=MUTED, font=F(10)).pack(pady=(px(4), 0))
      return
    owner = self.owner_of[name]
    code, group, color = type_info(name)
    ext = os.path.splitext(name)[1].lower()
    top = tk.Frame(self.det, bg=SURF)
    top.pack(fill="x", padx=pad, pady=(pad, px(12)))
    chip(top, code, color, SURF, 44).pack(side="left", anchor="n")
    tk.Label(top, text=name, bg=SURF, fg=TEXT, font=F(12, True),
             wraplength=px(235), justify="left",
             anchor="w").pack(side="left", padx=px(12), anchor="n")
    meta = tk.Frame(self.det, bg=SURF)
    meta.pack(fill="x", padx=pad)
    editable = self.can_edit(owner)
    rows = [("Owner", owner), ("Type", group),
            ("Access", "You can rename and delete" if editable else
                                              f"View only - owned by {owner}")]
    for k, v in rows:
      r = tk.Frame(meta, bg=SURF)
      r.pack(fill="x", pady=px(2))
      tk.Label(r, text=k, bg=SURF, fg=MUTED, font=F(9), width=7,
               anchor="w").pack(side="left")
      tk.Label(r, text=v, bg=SURF, fg=TEXT, font=F(10), anchor="w",
               justify="left",
               wraplength=px(210)).pack(side="left")
    self.size_row = tk.Label(meta, text="", bg=SURF, fg=MUTED, font=F(9),
                             anchor="w")
    self.size_row.pack(fill="x", pady=(px(2), 0))
    acts = tk.Frame(self.det, bg=SURF)
    acts.pack(fill="x", padx=pad, pady=px(16))
    Btn(acts, "Download", self.download, "primary").pack(side="left")
    rn = Btn(acts, "Rename", self.rename, "soft")
    rn.pack(side="left", padx=px(8))
    dl = Btn(acts, "Delete", self.delete, "danger")
    dl.pack(side="left")
    rn.set_enabled(editable)
    dl.set_enabled(editable)
    self.pv = tk.Frame(self.det, bg="#f3f0fa")
    self.pv.pack(fill="both", expand=True, padx=pad, pady=(0, pad))
    if ext in (".txt", ".png") or (ext in (".jpg", ".jpeg") and Image):
      tk.Label(self.pv, text="Loading preview…", bg="#f3f0fa", fg=MUTED,
               font=F(10)).pack(expand=True)
      token = self.preview_token

      def done(res):
        if token == self.preview_token and res is not None:
          self.render_preview(name, ext, res)

      self.call(f"READ:{name}".encode(), done)
    else:
      note = ("Install Pillow (pip install pillow) to preview JPG images."
              "\nDownload to open it."
              if ext in (".jpg", ".jpeg")
              else f"No in-app preview for {group.lower()} files."
                   "\nUse Download to open it.")
      tk.Label(self.pv, text=note, bg="#f3f0fa", fg=MUTED, font=F(10),
               justify="center").pack(expand=True)

  # Show the preview: text in a box, or an image shrunk to fit.
  def render_preview(self, name, ext, data):
    for w in self.pv.winfo_children():
      w.destroy()
    # The server sends "ERROR..." when something went wrong.
    if data.startswith(b"ERROR"):
      tk.Label(self.pv, text="Could not load this file.", bg="#f3f0fa",
               fg=DANGER, font=F(10)).pack(expand=True)
      return
    kb = len(data) / 1024
    self.size_row.config(
        text=(f"Size   {kb / 1024:.1f} MB" if kb > 1024
              else f"Size   {kb:.1f} KB"))
    if ext == ".txt":
      t = tk.Text(self.pv, wrap="word", relief="flat", bd=0, bg="#f3f0fa",
                  fg=TEXT, font=F(10),
                  padx=px(12), pady=px(10), highlightthickness=0)
      t.pack(fill="both", expand=True)
      try:
        text = data.decode("utf-8")
      except UnicodeDecodeError:
        text = data.decode("latin-1")
      t.insert("1.0", text[:20000]
               + ("\n\n… preview truncated" if len(text) > 20000 else ""))
      t.config(state="disabled")
      return
    try:
      w, h = px(290), px(300)
      if Image:
        im = Image.open(io.BytesIO(data))
        im.thumbnail((w, h))
        ph = ImageTk.PhotoImage(im)
      else:
        ph = tk.PhotoImage(data=base64.b64encode(data))
        f = max(1, -(-ph.width() // w), -(-ph.height() // h))
        ph = ph.subsample(f)
      l = tk.Label(self.pv, image=ph, bg="#f3f0fa")
      # Keep a reference, or the picture disappears.
      l.image = ph
      l.pack(expand=True)
    except Exception:
      tk.Label(self.pv, text="Preview unavailable.\nUse Download to open it.",
               bg="#f3f0fa", fg=MUTED,
               font=F(10)).pack(expand=True)

  # ---------- File actions ----------
  # Upload files. Check each file first, then send them one by one.
  def upload(self):
    paths = filedialog.askopenfilenames(
        title="Select note files (max 50 MB each)",
        filetypes=[("Allowed files",
                    "*.txt *.pdf *.doc *.docx *.ppt *.pptx "
                    "*.png *.jpg *.jpeg *.xls *.xlsx")])
    if not paths:
      return
    # Keep only the files that pass the checks.
    queue_ = []
    for p in paths:
      name = os.path.basename(p)
      if os.path.splitext(name)[1].lower() not in ALLOWED:
        self.toast(f"'{name}': file type not allowed.", "err")
      elif ":" in name:
        self.toast(f"'{name}': remove ':' from the file name first.", "err")
      elif os.path.getsize(p) > MAX_SIZE:
        self.toast(f"'{name}' is over the 50 MB limit.", "err")
      elif name in self.owner_of and not self.can_edit(self.owner_of[name]):
        self.toast(f"'{name}' already exists and belongs to "
                   f"{self.owner_of[name]}. Rename yours first.", "err")
      else:
        queue_.append((p, name))
    if not queue_:
      return
    # Ask before replacing files that already exist.
    exists = [n for _, n in queue_ if n in self.owner_of]
    if exists:
      m = Modal(self, "Replace files?")
      m.heading("Replace existing files?",
                "These already exist and will be overwritten:\n"
                + "\n".join(exists[:5]))
      m.buttons(Btn(m.body, "Cancel", m.destroy, "ghost"),
                Btn(m.body, "Replace",
                    lambda: (m.destroy(), self.upload_next(queue_, 0, 0)),
                    "primary"))
      m.show()
    else:
      self.upload_next(queue_, 0, 0)

  # Upload the next file in the list, then go on until all are done.
  def upload_next(self, items, ok, bad):
    if not items:
      self.toast(f"Uploaded {ok} file{'s' if ok != 1 else ''}"
                 + (f", {bad} failed" if bad else ""),
                 "ok" if not bad else "warn")
      return self.refresh(silent=True)
    path, name = items[0]
    try:
      with open(path, "rb") as f:
        # Command format: WRITE:filename:username:file-bytes
        payload = (b"WRITE:" + name.encode() + b":" + self.user.encode()
                   + b":" + f.read())
    except OSError as e:
      self.toast(f"Could not read '{name}': {e}", "err")
      return self.upload_next(items[1:], ok, bad + 1)

    def done(res):
      good = bool(res and b"SUCCESS" in res)
      if res is not None and not good:
        self.toast(res.decode().replace("ERROR: ", ""), "err")
      self.upload_next(items[1:], ok + good, bad + (not good))

    self.call(payload, done)

  # Save the selected file on the computer.
  def download(self):
    name = self.selected
    if not name:
      return self.toast("Select a file first.", "warn")
    dest = filedialog.asksaveasfilename(initialfile=name, title="Save file as")
    if not dest:
      return

    def done(res):
      if res is None:
        return
      if res.startswith(b"ERROR"):
        return self.toast("File not found on the server.", "err")
      try:
        with open(dest, "wb") as f:
          f.write(res)
        self.toast(f"Saved '{os.path.basename(dest)}'", "ok")
      except OSError as e:
        self.toast(f"Could not save file: {e}", "err")

    self.call(f"READ:{name}".encode(), done)

  # Rename the selected file (owner or admin only).
  def rename(self):
    name = self.selected
    if not name or not self.can_edit(self.owner_of.get(name, "")):
      return
    m = Modal(self, "Rename file")
    m.heading("Rename file",
              f"Give “{name}” a new name. Keep a valid extension.")
    fld = Field(m.body, "New name")
    fld.pack(fill="x", pady=(px(16), 0))
    fld.e.insert(0, name)
    stem = os.path.splitext(name)[0]
    fld.e.select_range(0, len(stem))

    def go():
      new = fld.get()
      if not new or new == name:
        return m.destroy()
      if ":" in new:
        return self.toast("File names cannot contain ':'.", "warn")
      if os.path.splitext(new)[1].lower() not in ALLOWED:
        return self.toast("That extension is not allowed.", "warn")
      if new in self.owner_of:
        return self.toast(f"'{new}' already exists.", "warn")
      m.destroy()

      def done(res):
        if res and b"SUCCESS" in res:
          self.selected = new
          self.toast("File renamed", "ok")
          self.sig = None
          self.refresh(silent=True, after=self.show_details)
        elif res is not None:
          self.toast(res.decode().replace("ERROR: ", ""), "err")

      # Command format: RENAME:oldname:newname:username
      self.call(f"RENAME:{name}:{new}:{self.user}".encode(), done)

    fld.e.bind("<Return>", lambda e: go())
    m.buttons(Btn(m.body, "Cancel", m.destroy, "ghost"),
              Btn(m.body, "Rename", go, "primary"))
    m.show()
    fld.e.focus_set()

  # Delete the selected file after asking to confirm.
  def delete(self):
    name = self.selected
    if not name or not self.can_edit(self.owner_of.get(name, "")):
      return
    m = Modal(self, "Delete file")
    m.heading("Delete this file?",
              f"“{name}” will be removed from both servers. "
              "This can't be undone.")

    def go():
      m.destroy()

      def done(res):
        if res and b"SUCCESS" in res:
          self.selected = None
          self.toast("File deleted", "ok")
          self.sig = None
          self.refresh(silent=True)
        elif res is not None:
          self.toast(res.decode().replace("ERROR: ", ""), "err")

      # Command format: DELETE:filename:username
      self.call(f"DELETE:{name}:{self.user}".encode(), done)

    cancel_btn = Btn(m.body, "Cancel", m.destroy, "ghost")
    confirm_btn = Btn(m.body, "Delete", go, "danger")
    m.buttons(cancel_btn, confirm_btn)
    m.show()

  # Admin only: list the users and let the admin delete them.
  def manage_users(self):
    m = Modal(self, "Manage users", 420)
    m.heading("User accounts",
              "Remove accounts you no longer need. "
              "The primary admin can't be deleted.")

    # Cap the visible user list height so the modal never clips off-screen,
    # and only display the scrollbar when the list exceeds that height.
    max_h = px(280)
    list_host = tk.Frame(m.body, bg=SURF, height=px(80))
    list_host.pack(fill="both", expand=True, pady=(px(14), 0))
    list_host.pack_propagate(False)

    cv = tk.Canvas(list_host, bg=SURF, highlightthickness=0, bd=0, height=px(80))
    sb = ttk.Scrollbar(list_host, orient="vertical", command=cv.yview)
    cv.configure(yscrollcommand=sb.set)
    cv.pack(side="left", fill="both", expand=True)

    box = tk.Frame(cv, bg=SURF)
    win_id = cv.create_window(0, 0, window=box, anchor="nw")
    cv.bind("<Configure>", lambda e: cv.itemconfig(win_id, width=e.width))

    def sync_scroll(_e=None):
      if not cv.winfo_exists() or not box.winfo_exists():
        return
      cv.update_idletasks()
      req_h = box.winfo_reqheight()
      vis_h = min(max_h, max(px(40), req_h))
      list_host.config(height=vis_h)
      cv.config(height=vis_h)
      cv.configure(scrollregion=(0, 0, box.winfo_reqwidth(), req_h))
      if req_h > max_h:
        if not sb.winfo_ismapped():
          sb.pack(side="right", fill="y")
      else:
        if sb.winfo_ismapped():
          sb.pack_forget()
          cv.yview_moveto(0)
      # Recenter and resize the modal cleanly
      m.update_idletasks()
      r = self.root
      w = max(m.width, m.winfo_reqwidth())
      h = m.winfo_reqheight()
      x = r.winfo_rootx() + (r.winfo_width() - w) // 2
      y = max(0, r.winfo_rooty() + (r.winfo_height() - h) // 3)
      m.geometry(f"{w}x{h}+{x}+{y}")

    # Mouse wheel scrolls the user list only when the scrollbar is active.
    def on_wheel(e):
      if not cv.winfo_exists() or not sb.winfo_ismapped():
        return
      if getattr(e, "num", None) == 4:
        cv.yview_scroll(-1, "units")
      elif getattr(e, "num", None) == 5:
        cv.yview_scroll(1, "units")
      else:
        cv.yview_scroll(-1 if e.delta > 0 else 1, "units")

    m.bind("<MouseWheel>", on_wheel)
    m.bind("<Button-4>", on_wheel)
    m.bind("<Button-5>", on_wheel)
    box.bind("<Configure>", sync_scroll)

    def load():
      def done(res):
        if res is None or not box.winfo_exists():
          return
        for w in box.winfo_children():
          w.destroy()
        if res.startswith(b"ERROR"):
          tk.Label(box, text=res.decode().replace("ERROR: ", ""),
                   bg=SURF, fg=DANGER).pack(anchor="w")
          sync_scroll()
          return
        for u in res.decode().splitlines():
          if not u.strip():
            continue
          r = tk.Frame(box, bg=SURF)
          r.pack(fill="x", pady=px(4), padx=(0, px(4)))
          av = tk.Canvas(r, width=px(30), height=px(30), bg=SURF,
                         highlightthickness=0)
          av.create_oval(1, 1, px(29), px(29),
                         fill=SOFT if u != "admin" else ACCENT, outline="")
          av.create_text(px(15), px(15), text=u[:1].upper(),
                         fill=ACCENT if u != "admin" else "white",
                         font=F(10, True))
          av.pack(side="left")
          tk.Label(r, text=u, bg=SURF, fg=TEXT,
                   font=F(10, True)).pack(side="left", padx=px(10))
          if u == "admin":
            tk.Label(r, text="Primary admin", bg=SURF, fg=MUTED,
                     font=F(9)).pack(side="right")
          else:
            Btn(r, "Delete", lambda u=u: confirm(u),
                "danger").pack(side="right")
        sync_scroll()

      self.call(f"GET_USERS:{self.user}".encode(), done)

    def confirm(u):
      def done(res):
        if res and b"SUCCESS" in res:
          self.toast(f"Deleted user '{u}'", "ok")
          load()
        elif res is not None:
          self.toast(res.decode().replace("ERROR: ", ""), "err")

      self.call(f"DELETE_USER:{u}:{self.user}".encode(), done)

    load()
    m.buttons(Btn(m.body, "Done", m.destroy, "primary"))
    m.show()


# Save an error to client_error.log (next to client.py) and print it,
# so a problem is never hidden.
def report_error(exc_type, exc, tb):
  text = "".join(traceback.format_exception(exc_type, exc, tb))
  print(text, file=sys.stderr)
  try:
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "client_error.log"), "a") as f:
      f.write(text + "\n")
  except OSError:
    pass


# Start the app.
if __name__ == "__main__":
  if sys.platform == "win32":
    try:  # crisp text on high-DPI Windows screens
      # Ask Windows for sharp text on high-resolution screens.
      import ctypes
      ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
      pass
  try:
    root = tk.Tk()
    root.report_callback_exception = report_error  # errors inside buttons
    NotesDriveApp(root)
    root.mainloop()
  except Exception:
    # The app could not start: show why instead of closing silently.
    report_error(*sys.exc_info())
    try:
      input("Press Enter to close...")
    except Exception:
      pass