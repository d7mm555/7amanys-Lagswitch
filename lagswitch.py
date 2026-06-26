#!/usr/bin/env python3
"""7amany's Lagswitch.

A cross-platform (macOS + Windows) desktop app that cuts the computer off the
internet while a chosen global key is pressed, holds the cut for a chosen
duration (0.5-10 seconds), then automatically reconnects.

The cut is done at the firewall layer so the physical link stays associated and
both the cut and the reconnect are near-instant -- this keeps the short timer
accurate:
  * macOS  -> pfctl (needs a one-time passwordless-sudo rule; see README)
  * Windows -> Windows Defender Firewall via netsh (the packaged .exe
               auto-elevates through UAC, so no setup is required)

The trigger key is captured globally with pynput so it fires even while another
app (a game) is focused. On macOS that needs Accessibility + Input Monitoring
permission; on Windows it works out of the box.

Run:  python3 lagswitch.py   (macOS)   /   double-click Lagswitch.exe (Windows)
"""

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import font as tkfont

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

if IS_WIN:
    import ctypes
    import winsound
    # Stops a black cmd window from flashing on every netsh call.
    CREATE_NO_WINDOW = 0x08000000
else:
    CREATE_NO_WINDOW = 0

try:
    from pynput import keyboard
except ImportError:
    sys.stderr.write(
        "Missing dependency 'pynput'.\n"
        "Install it with:  pip3 install -r requirements.txt\n"
    )
    sys.exit(1)


# --- Theme -----------------------------------------------------------------
BG = "#0a0a0d"           # window background
PANEL = "#15091f"         # dark badge background
ACCENT = "#a020f0"        # purple accent
ACCENT_HI = "#bb00ff"     # brighter purple for hover / active
GLOW_DIM = "#4b0f73"      # dim purple used behind glow text
TEXT = "#e0e0e0"          # primary text
TEXT_DIM = "#888888"      # secondary text
DANGER = "#ff3355"        # "cut" status color
BADGE_BG = "#f5f0fa"      # light badge background
BADGE_FG = "#2a0845"      # dark purple text on light badges

# pf ruleset that drops everything except loopback.
BLOCK_RULES = "set block-policy drop\nset skip on lo0\nblock drop all\n"
BLOCK_CONF_PATH = "/tmp/lagswitch_block.conf"
DEFAULT_PF_CONF = "/etc/pf.conf"

# System sounds, per platform (no bundled audio needed).
if IS_WIN:
    SOUND_START = "SystemAsterisk"
    SOUND_ARM = "SystemExclamation"
    SOUND_DISARM = "SystemHand"
else:
    SOUND_START = "/System/Library/Sounds/Tink.aiff"
    SOUND_ARM = "/System/Library/Sounds/Glass.aiff"
    SOUND_DISARM = "/System/Library/Sounds/Pop.aiff"

# SHA-256 of the one valid access token -- the plaintext is never stored here.
TOKEN_HASH = "15266e80c93db00dde82b79c2144c1cfe7592c533032be9d613a7a7c20f9658f"


def check_token(token):
    return hashlib.sha256(token.strip().encode("utf-8")).hexdigest() == TOKEN_HASH


def app_dir():
    """Per-user folder for settings (and, on Windows, the cached payload)."""
    if IS_WIN:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        path = os.path.join(base, "Lagswitch")
    elif IS_MAC:
        path = os.path.expanduser("~/Library/Application Support/Lagswitch")
    else:
        path = os.path.expanduser("~/.lagswitch")
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    return path


SETTINGS_PATH = os.path.join(app_dir(), "settings.json")


def load_settings():
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(data):
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    except OSError:
        pass


def play_sound(name_or_path):
    """Fire-and-forget playback; never blocks the UI."""
    try:
        if IS_WIN:
            winsound.PlaySound(
                name_or_path, winsound.SND_ALIAS | winsound.SND_ASYNC
            )
        else:
            subprocess.Popen(
                ["afplay", name_or_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except Exception:  # noqa: BLE001 - a missing sound shouldn't break the app
        pass


PFCTL = "/sbin/pfctl"
FW_RULE_OUT = "LagswitchBlockOut"
FW_RULE_IN = "LagswitchBlockIn"


# --- Disconnect engine -----------------------------------------------------
class _EngineBase:
    """Shared state for whichever platform strategy is active."""

    def __init__(self):
        self._cutting = False
        self._lock = threading.Lock()

    @staticmethod
    def _run(args, timeout=15):
        try:
            kwargs = {}
            if IS_WIN:
                kwargs["creationflags"] = CREATE_NO_WINDOW
            result = subprocess.run(
                args, capture_output=True, text=True, timeout=timeout, **kwargs
            )
            return result.returncode == 0, (result.stderr or result.stdout).strip()
        except Exception as exc:  # noqa: BLE001 - surface anything to the UI
            return False, str(exc)

    @property
    def is_cutting(self):
        return self._cutting


class MacEngine(_EngineBase):
    """Cuts and restores internet access via pfctl.

    Relies on a one-time sudoers rule (see README) that allows this exact
    user to run only these three pfctl invocations without a password --
    so arming never has to prompt.
    """

    def check_permission(self):
        """Verify the passwordless sudo rule is installed. Returns (ok, msg).

        Probes with "-d" specifically because that's one of the three exact
        commands the sudoers rule whitelists -- any other pfctl invocation
        (e.g. "-s info") isn't covered and would always report failure.
        """
        ok, err = self._run(["sudo", "-n", PFCTL, "-d"])
        if not ok and "not enabled" not in (err or "").lower():
            return False, (
                f"passwordless sudo for pfctl isn't set up -- see README ({err})"
                if err else
                "passwordless sudo for pfctl isn't set up -- see README"
            )
        return True, "ready"

    def cut(self):
        """Block all traffic. Returns (ok, message)."""
        with self._lock:
            try:
                with open(BLOCK_CONF_PATH, "w") as fh:
                    fh.write(BLOCK_RULES)
            except OSError as exc:
                return False, f"could not write ruleset: {exc}"
            ok, err = self._run(["sudo", "-n", PFCTL, "-e", "-f", BLOCK_CONF_PATH])
            if ok:
                self._cutting = True
                return True, "cut"
            return False, err or "pfctl failed"

    def restore(self):
        """Reload the default ruleset and disable pf. Returns (ok, message)."""
        with self._lock:
            ok_load, err_load = self._run(["sudo", "-n", PFCTL, "-f", DEFAULT_PF_CONF])
            ok_dis, err_dis = self._run(["sudo", "-n", PFCTL, "-d"])
            self._cutting = False
            if not ok_load:
                return False, err_load or "reload failed"
            if not ok_dis and "pf not enabled" not in (err_dis or "").lower():
                return False, err_dis or "disable failed"
            return True, "restored"


class WindowsEngine(_EngineBase):
    """Cuts and restores internet access via Windows Defender Firewall rules.

    Block rules take precedence over allow rules, so an outbound+inbound
    block-all rule cuts the internet instantly while the adapter stays
    associated. The packaged .exe auto-elevates through UAC (see the
    PyInstaller --uac-admin build), so no setup is required ahead of time.
    """

    def check_permission(self):
        try:
            is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            is_admin = False
        if not is_admin:
            return False, "Lagswitch needs to run as administrator -- relaunch the .exe"
        return True, "ready"

    def cut(self):
        with self._lock:
            ok_out, err_out = self._run([
                "netsh", "advfirewall", "firewall", "add", "rule",
                f"name={FW_RULE_OUT}", "dir=out", "action=block",
            ])
            ok_in, err_in = self._run([
                "netsh", "advfirewall", "firewall", "add", "rule",
                f"name={FW_RULE_IN}", "dir=in", "action=block",
            ])
            if ok_out and ok_in:
                self._cutting = True
                return True, "cut"
            self._delete_rules()
            return False, err_out or err_in or "netsh failed"

    def restore(self):
        with self._lock:
            ok = self._delete_rules()
            self._cutting = False
            return ok, "restored" if ok else "reconnect issue"

    def _delete_rules(self):
        ok_out, _ = self._run([
            "netsh", "advfirewall", "firewall", "delete", "rule",
            f"name={FW_RULE_OUT}",
        ])
        ok_in, _ = self._run([
            "netsh", "advfirewall", "firewall", "delete", "rule",
            f"name={FW_RULE_IN}",
        ])
        return ok_out and ok_in


def make_engine():
    return WindowsEngine() if IS_WIN else MacEngine()


# --- Application -----------------------------------------------------------
class LagswitchApp:
    def __init__(self, root, update_available=False):
        self.root = root
        self.engine = make_engine()
        self.update_available = update_available
        self.exit_action = "quit"

        settings = load_settings()
        self.unlocked = bool(settings.get("unlocked", False))

        self.armed = False
        self.bound_key = self._deserialize_key(settings.get("bound_key"))
        self.bound_key_label = self._key_name(self.bound_key) if self.bound_key else "Bind"
        self.duration = tk.DoubleVar(value=float(settings.get("duration", 3.0)))
        self.duration_display = tk.StringVar(value=f"{self.duration.get():.1f}s")
        self._capturing = False

        root.title("7amany's Lagswitch")
        root.configure(bg=BG)
        root.geometry("560x420")
        root.minsize(420, 320)
        root.resizable(True, True)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        # F11 toggles full-screen on Windows (macOS users have the native
        # green-button full-screen instead); Esc exits it.
        if IS_WIN:
            root.bind("<F11>", self._toggle_fullscreen)
            root.bind("<Escape>", self._exit_fullscreen)
            self._fullscreen = False

        # Fonts
        font_family = "Segoe UI" if IS_WIN else "Avenir Next"
        self.title_font = tkfont.Font(family=font_family, size=30, weight="bold")
        self.header_font = tkfont.Font(family=font_family, size=16, weight="bold")
        self.body_font = tkfont.Font(family=font_family, size=13, weight="bold")
        self.button_font = tkfont.Font(family=font_family, size=14, weight="bold")
        self.small_font = tkfont.Font(family=font_family, size=11)

        # Container that gets rebuilt per screen.
        self.container = tk.Frame(root, bg=BG)
        self.container.pack(fill="both", expand=True)

        # Long-lived global key listener (runs whole app lifetime). Only one
        # Listener is ever created -- running a second one concurrently (e.g.
        # for keybind capture) has been observed to crash the process on macOS.
        try:
            self.listener = keyboard.Listener(on_press=self._on_global_key)
            self.listener.daemon = True
            self.listener.start()
        except Exception as exc:  # noqa: BLE001
            self.listener = None
            print(f"[Lagswitch] Could not start global key listener: {exc}")

        self.show_intro()

    # -- Screen helpers -----------------------------------------------------
    def _clear(self):
        for child in self.container.winfo_children():
            child.destroy()

    def _make_button(self, parent, text, command, big=False):
        btn = tk.Button(
            parent,
            text=text,
            command=command,
            font=self.button_font if big else self.small_font,
            bg=ACCENT,
            fg="white",
            activebackground=ACCENT_HI,
            activeforeground="white",
            relief="flat",
            bd=0,
            highlightthickness=0,
            padx=18,
            pady=10 if big else 6,
            cursor="hand2",
        )
        btn.bind("<Enter>", lambda e: btn.config(bg=ACCENT_HI))
        btn.bind("<Leave>", lambda e: btn.config(bg=ACCENT))
        return btn

    def _make_label(self, parent, text, font, fg=TEXT, bg=BG):
        # tk.Label text fails to render on the old system Tk shipped by
        # macOS; tk.Button text renders fine, so labels are inert buttons.
        return tk.Button(
            parent,
            text=text,
            font=font,
            bg=bg,
            fg=fg,
            activebackground=bg,
            activeforeground=fg,
            relief="flat",
            bd=0,
            highlightthickness=0,
            cursor="arrow",
            takefocus=0,
            command=lambda: None,
        )

    def _badge(self, parent, text, command=None, dark=False):
        bg, fg = (PANEL, ACCENT) if dark else (BADGE_BG, BADGE_FG)
        btn = tk.Button(
            parent,
            text=text,
            font=self.body_font,
            bg=bg,
            fg=fg,
            activebackground=ACCENT_HI if command else bg,
            activeforeground="white" if command else fg,
            relief="flat",
            bd=0,
            highlightthickness=2,
            highlightbackground=ACCENT,
            padx=16,
            pady=11,
            cursor="hand2" if command else "arrow",
            takefocus=0,
            command=command if command else (lambda: None),
        )
        return btn

    def _glow_text(self, parent, text, font, fg=ACCENT, glow=GLOW_DIM):
        """Canvas-drawn text with a stacked-offset halo to fake a neon glow."""
        pad = 26
        w = font.measure(text) + pad
        h = font.metrics("linespace") + pad
        canvas = tk.Canvas(parent, width=w, height=h, bg=BG, highlightthickness=0)
        cx, cy = w // 2, h // 2
        for dx, dy in ((2, 0), (-2, 0), (0, 2), (0, -2), (2, 2), (-2, -2), (2, -2), (-2, 2)):
            canvas.create_text(cx + dx, cy + dy, text=text, font=font, fill=glow)
        canvas.create_text(cx, cy, text=text, font=font, fill=fg)
        return canvas

    def _connector_row(self, parent, label_text, value_factory):
        """value_factory(row) builds and returns the right-hand widget,
        since it must be parented to this row, not the caller's frame."""
        row = tk.Frame(parent, bg=BG, width=440, height=56)
        row.pack_propagate(False)
        row.pack(fill="x", pady=10)
        row.grid_columnconfigure(1, weight=1)
        self._badge(row, label_text).grid(row=0, column=0, sticky="w")
        tk.Frame(row, bg=ACCENT, height=2).grid(row=0, column=1, sticky="ew", padx=10)
        value_widget = value_factory(row)
        value_widget.grid(row=0, column=2, sticky="e")
        return row, value_widget

    def _force_redraw(self):
        # Works around stale pixels left behind after switching screens,
        # also caused by the old system Tk's buggy Cocoa redraw handling.
        self.root.update_idletasks()
        w, h = self.root.winfo_width(), self.root.winfo_height()
        self.root.geometry(f"{w}x{h + 1}")
        self.root.update_idletasks()
        self.root.geometry(f"{w}x{h}")

    def show_intro(self):
        self._clear()
        frame = tk.Frame(self.container, bg=BG)
        frame.place(relx=0.5, rely=0.5, anchor="center")

        self._glow_text(
            frame, "7AMANY'S LAGSWITCH", self.title_font
        ).pack(pady=(0, 28))

        self._make_button(frame, "Start", self._start_clicked, big=True).pack()

        if not self.unlocked:
            self.token_entry = self._make_token_entry(frame)
            self.token_entry.pack(pady=(16, 6))

        self.token_error = self._make_label(frame, "", self.small_font, DANGER)
        self.token_error.pack()

        if self.update_available:
            self._make_button(frame, "Update", self._do_update).pack(pady=(16, 0))

        self._force_redraw()

    def _make_token_entry(self, parent):
        placeholder = "Enter Token"
        entry = tk.Entry(
            parent,
            font=self.body_font,
            bg=BADGE_BG,
            fg=TEXT_DIM,
            insertbackground=BADGE_FG,
            relief="flat",
            bd=0,
            highlightthickness=2,
            highlightbackground=ACCENT,
            highlightcolor=ACCENT_HI,
            justify="center",
        )
        entry.insert(0, placeholder)
        entry.placeholder_active = True

        def on_focus_in(_e):
            if entry.placeholder_active:
                entry.delete(0, "end")
                entry.config(fg=BADGE_FG, show="*")
                entry.placeholder_active = False
            self.token_error.config(text="")

        def on_focus_out(_e):
            if not entry.get():
                entry.insert(0, placeholder)
                entry.config(fg=TEXT_DIM, show="")
                entry.placeholder_active = True

        entry.bind("<FocusIn>", on_focus_in)
        entry.bind("<FocusOut>", on_focus_out)
        entry.bind("<Return>", lambda _e: self._start_clicked())
        return entry

    def _get_token_input(self):
        if not hasattr(self, "token_entry"):
            return ""
        if getattr(self.token_entry, "placeholder_active", False):
            return ""
        return self.token_entry.get().strip()

    def _start_clicked(self):
        if not self.unlocked:
            token = self._get_token_input()
            if not token:
                self.token_error.config(text="You Must Enter Your Token First")
                return
            if not check_token(token):
                self.token_error.config(text="Invalid Token")
                return
            self.unlocked = True
            self._save_settings()
        if self.update_available:
            self.token_error.config(text="Update Required")
            return
        play_sound(SOUND_START)
        self.show_config()

    def _do_update(self):
        self.exit_action = "update"
        self.on_close()

    def show_config(self):
        self._clear()
        outer = tk.Frame(self.container, bg=BG)
        outer.place(relx=0.5, rely=0.5, anchor="center")

        self._glow_text(
            outer, "LAGSWITCH SETTINGS", self.header_font
        ).pack(anchor="w", pady=(0, 18))

        _, self.key_button = self._connector_row(
            outer,
            "TRIGGER KEY",
            lambda row: self._badge(row, self.bound_key_label, command=self.begin_capture),
        )

        self._connector_row(outer, "DISCONNECT FOR", self._build_duration_slider)

        self.arm_button = self._badge(outer, "Arm", command=self.toggle_arm, dark=True)
        self.arm_button.pack(pady=(26, 10))

        self.status_label = self._make_label(outer, "Disarmed", self.small_font, TEXT_DIM)
        self.status_label.pack()

        self._text_link(self.container, "← Back", self.show_intro).place(
            relx=0.0, rely=1.0, x=20, y=-20, anchor="sw"
        )

        self._refresh_key_button()
        self._force_redraw()

    def _text_link(self, parent, text, command, font=None, fg=ACCENT, hover=ACCENT_HI):
        font = font or self.body_font
        btn = tk.Button(
            parent,
            text=text,
            font=font,
            bg=BG,
            fg=fg,
            activebackground=BG,
            activeforeground=hover,
            relief="flat",
            bd=0,
            highlightthickness=0,
            cursor="hand2",
            takefocus=0,
            command=command,
        )
        btn.bind("<Enter>", lambda e: btn.config(fg=hover))
        btn.bind("<Leave>", lambda e: btn.config(fg=fg))
        return btn

    def _build_duration_slider(self, parent):
        wrap = tk.Frame(parent, bg=BG)

        value_badge = self._make_label(wrap, self.duration_display.get(), self.body_font, BADGE_FG, BADGE_BG)
        value_badge.config(width=5, highlightthickness=2, highlightbackground=ACCENT)
        value_badge.pack(side="right", padx=(10, 0))

        def on_move(_value):
            seconds = round(self.duration.get(), 1)
            self.duration_display.set(f"{seconds:.1f}s")
            value_badge.config(text=f"{seconds:.1f}s")

        def on_release(_event):
            self._save_settings()

        slider = tk.Scale(
            wrap,
            from_=0.5,
            to=10.0,
            resolution=0.1,
            orient="horizontal",
            variable=self.duration,
            command=on_move,
            length=180,
            showvalue=False,
            bg=BG,
            fg=ACCENT,
            troughcolor=PANEL,
            activebackground=ACCENT_HI,
            highlightthickness=0,
            bd=0,
            sliderrelief="flat",
            takefocus=0,
        )
        slider.bind("<ButtonRelease-1>", on_release)
        slider.pack(side="left")
        return wrap

    # -- Keybind capture ----------------------------------------------------
    def begin_capture(self):
        if self._capturing:
            return
        if self.armed:
            self.set_status("Disarm before changing the keybind.", TEXT_DIM)
            return
        self._capturing = True
        self.key_button.config(text="Press a key…")
        # The next key is caught by the single long-lived listener below.

    def _finish_capture(self, key):
        self.bound_key = key
        self.bound_key_label = self._key_name(key)
        self._capturing = False
        self._refresh_key_button()
        self._save_settings()

    def _refresh_key_button(self):
        if hasattr(self, "key_button"):
            self.key_button.config(text=self.bound_key_label)

    @staticmethod
    def _key_name(key):
        try:
            if hasattr(key, "char") and key.char is not None:
                return key.char.upper()
        except AttributeError:
            pass
        # Special keys like Key.f8 -> "F8"
        name = str(key).replace("Key.", "")
        return name.upper()

    @staticmethod
    def _serialize_key(key):
        if key is None:
            return None
        char = getattr(key, "char", None)
        if char is not None:
            return {"type": "char", "value": char}
        return {"type": "special", "value": str(key).replace("Key.", "")}

    @staticmethod
    def _deserialize_key(data):
        if not isinstance(data, dict):
            return None
        try:
            if data.get("type") == "char":
                return keyboard.KeyCode.from_char(data["value"])
            return keyboard.Key[data["value"]]
        except (KeyError, AttributeError, ValueError):
            return None

    def _save_settings(self):
        save_settings({
            "bound_key": self._serialize_key(self.bound_key),
            "duration": self.duration.get(),
            "unlocked": self.unlocked,
        })

    # -- Global trigger -----------------------------------------------------
    def _on_global_key(self, key):
        if self._capturing:
            self.root.after(0, self._finish_capture, key)
            return
        if not self.armed or self.bound_key is None:
            return
        if self.engine.is_cutting:
            return
        if self._keys_equal(key, self.bound_key):
            self.root.after(0, self.trigger_cut)

    @staticmethod
    def _keys_equal(a, b):
        # Compare character keys by char, special keys by identity/value.
        try:
            ca = getattr(a, "char", None)
            cb = getattr(b, "char", None)
            if ca is not None and cb is not None:
                return ca == cb
        except AttributeError:
            pass
        return a == b

    # -- Arm / disarm -------------------------------------------------------
    def toggle_arm(self):
        if self.armed:
            self.disarm()
        else:
            self.arm()

    def arm(self):
        if self.bound_key is None:
            self.set_status("Set a trigger key first.", DANGER)
            return
        ok, msg = self.engine.check_permission()
        if not ok:
            self.set_status(msg, DANGER)
            return
        self.armed = True
        self.arm_button.config(text="Disarm")
        self.set_status(f"Armed — press {self.bound_key_label}", ACCENT)
        play_sound(SOUND_ARM)

    def disarm(self):
        self.armed = False
        # Safety: if somehow mid-cut, make sure we're reconnected.
        if self.engine.is_cutting:
            self.engine.restore()
        self.arm_button.config(text="Arm")
        self.set_status("Disarmed", TEXT_DIM)
        play_sound(SOUND_DISARM)

    # -- The cut ------------------------------------------------------------
    def trigger_cut(self):
        if self.engine.is_cutting or not self.armed:
            return
        seconds = self.duration.get()
        worker = threading.Thread(
            target=self._cut_worker, args=(seconds,), daemon=True
        )
        worker.start()

    def _cut_worker(self, seconds):
        ok, msg = self.engine.cut()
        if not ok:
            self._ui(lambda: self.set_status(f"Cut failed: {msg}", DANGER))
            return
        # Count down while cut.
        end = time.monotonic() + seconds
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0:
                break
            self._ui(
                lambda r=remaining: self.set_status(
                    f"CUT — {max(r, 0):.1f}s left", DANGER
                )
            )
            time.sleep(min(0.1, remaining))
        self._ui(lambda: self.set_status("Reconnecting…", TEXT_DIM))
        ok, msg = self.engine.restore()
        if not ok:
            self._ui(
                lambda: self.set_status(
                    f"Reconnect issue: {msg}", DANGER
                )
            )
        else:
            self._ui(
                lambda: self.set_status(
                    f"Armed — press {self.bound_key_label}", ACCENT
                )
            )

    # -- Full-screen (Windows only; macOS has the native green button) ------
    def _toggle_fullscreen(self, _event=None):
        self._fullscreen = not self._fullscreen
        self.root.attributes("-fullscreen", self._fullscreen)

    def _exit_fullscreen(self, _event=None):
        self._fullscreen = False
        self.root.attributes("-fullscreen", False)

    # -- Utilities ----------------------------------------------------------
    def _ui(self, fn):
        """Schedule a callable on the Tk main thread."""
        try:
            self.root.after(0, fn)
        except RuntimeError:
            pass

    def set_status(self, text, color=TEXT_DIM):
        if hasattr(self, "status_label") and self.status_label.winfo_exists():
            self.status_label.config(text=text, fg=color)

    def on_close(self):
        # Never leave the user offline.
        if self.engine.is_cutting:
            self.engine.restore()
        self._save_settings()
        try:
            if self.listener:
                self.listener.stop()
        except Exception:  # noqa: BLE001
            pass
        self.root.destroy()


def run(update_available=False):
    """Build and run the app; returns "update" or "quit" once the window closes."""
    root = tk.Tk()
    app = LagswitchApp(root, update_available=update_available)
    root.mainloop()
    return app.exit_action


def main():
    run(False)


if __name__ == "__main__":
    main()
