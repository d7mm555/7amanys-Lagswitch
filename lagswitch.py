#!/usr/bin/env python3
"""7amany's Webcam.

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

Run:  python3 lagswitch.py   (macOS)   /   double-click Webcam.exe (Windows)
"""

import getpass
import hashlib
import io
import json
import math
import os
import platform
import socket
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request
import wave
from tkinter import font as tkfont

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

if IS_WIN:
    import ctypes
    from ctypes import wintypes
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
BLOCK_CONF_PATH = "/tmp/webcam_block.conf"
DEFAULT_PF_CONF = "/etc/pf.conf"

# In "both" mode, a press released within this many seconds counts as a tap
# (timed cut); anything longer is treated as a deliberate hold.
BOTH_TAP_MAX = 0.25

# --- Cloud token validation ------------------------------------------------
# Tokens are NOT stored in this (public) code -- only the endpoint URL is here,
# which holds no secret. The real list of valid tokens lives in a private Google
# Sheet behind a Google Apps Script web app (see SETUP-TOKENS.md). The endpoint
# binds each token to the first device that uses it, so a token is invalid on any
# other machine.  Paste your deployed /exec URL here:
TOKEN_API_URL = "https://script.google.com/macros/s/AKfycbwuM_Nq_lK8jl235skwJMLI_VDU7MsTRXgaZ5M6cixhYORYs1mnpnKEYdADNwdxkdc4/exec"


def device_id():
    """Stable per-machine fingerprint used to bind a token to one device."""
    raw = None
    if IS_WIN:
        try:
            out = subprocess.run(
                ["reg", "query",
                 r"HKLM\SOFTWARE\Microsoft\Cryptography", "/v", "MachineGuid"],
                capture_output=True, text=True, timeout=5,
                creationflags=CREATE_NO_WINDOW,
            )
            for line in out.stdout.splitlines():
                if "MachineGuid" in line:
                    raw = line.split()[-1].strip()
                    break
        except Exception:  # noqa: BLE001
            raw = None
    if not raw:
        try:
            raw = f"{platform.node()}|{getpass.getuser()}|{platform.machine()}"
        except Exception:  # noqa: BLE001
            raw = socket.gethostname()
    return hashlib.sha256((raw or "unknown").encode("utf-8")).hexdigest()[:32]


def validate_token(token):
    """Check a token with the cloud backend, binding it to this device.

    Returns (ok, message):
      * (True,  "valid")  -- token is valid for this device (now or already bound)
      * (False, <reason>) -- taken by another device / invalid / no connection
    """
    payload = json.dumps({"token": token.strip(), "device": device_id()}).encode("utf-8")
    try:
        req = urllib.request.Request(
            TOKEN_API_URL, data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - any network/HTTP/JSON failure -> offline
        return False, "No internet — token check needs a connection"

    status = (data.get("status") or "").lower()
    if status == "valid":
        return True, "valid"
    if status == "taken":
        return False, "Token Already Used On Another Device"
    return False, "Invalid Token"


# --- Changelog ---------------------------------------------------------------
# Bump CURRENT_VERSION and append one CHANGELOG entry every time a feature batch
# ships -- this is what drives the one-time "What's New" screen after an update
# and the manual Patch Notes view. Oldest entry first.
CURRENT_VERSION = "1.7.1"
CHANGELOG = [
    ("1.0", "First release", [
        "Bind a global trigger key and pick a disconnect duration (1-10s)",
        "Black-and-purple UI: intro screen with Start, then trigger key + duration + Arm",
        "Internet cut at the firewall layer so reconnect timing stays accurate",
    ]),
    ("1.1", "Polish pass", [
        "Sound effects for Start, Arm, and Disarm",
        "Token gate so only you can get past the Start screen",
        "Back button to return to the intro screen",
        "Passwordless firewall access on macOS so Arm never prompts for a password",
    ]),
    ("1.2", "Windows + live updates", [
        "Windows build via Windows Defender Firewall, packaged as Lagswitch.exe",
        "Live updates -- push a code change and the installed app offers an Update button",
        "Duration changed from a dropdown to a 0.5-10.0s slider (0.1s steps)",
        "Your token, trigger key, and duration are remembered between launches",
    ]),
    ("1.3", "Hold mode + per-app targeting", [
        "TOGGLE switches between timed cuts and hold-to-cut mode",
        "Select Window (Windows): cut just one app's internet instead of the whole PC",
        "Sounds replaced with original synthesized chimes",
    ]),
    ("1.4", "Arm hotkey, overlays, and safety", [
        "Bind a separate global hotkey that arms/disarms instantly",
        "On-screen overlays show Armed/Disarmed/Toggle status while you're in a game",
        "Press the trigger key again mid-cut to cancel it early instead of waiting it out",
        "Other keys held down now block a hotkey instead of misfiring, with an on-screen warning",
        "New chimes for Toggle ON/OFF and a distinct error buzz",
    ]),
    ("1.5", "Hotkeys, louder alerts, and cloud tokens", [
        "Bind a global hotkey to flip the TOGGLE mode on/off",
        "Loud high-low alert on Start, Arm/Disarm, and when the cut fires",
        "Trigger is blocked if any other key was pressed within the last 50ms",
        "Tokens are now validated in the cloud and lock to the first device that uses them",
        "Renamed the app to Webcam",
    ]),
    ("1.6", "Fixes + on-screen countdown", [
        "Fixed no sound playing on Windows",
        "Fixed the 'Input Failed' warning getting stuck until you disarmed/re-armed",
        "Added an on-screen countdown timer (top-center) while a timed cut is active",
    ]),
    ("1.7", "Three modes + always-on disconnect box", [
        "Replaced the TOGGLE switch with a MODE selector: Toggle, Hold, or Both",
        "Toggle = timed cut; Hold = cut only while the key is held; Both = tap for a timed cut, hold to cut while held",
        "New Hold slider: infinite, or cap the hold at 0.1-12s (auto-reconnect even if you're still holding)",
        "A 'Disconnected' box now shows top-left the whole time you're cut, in every mode",
        "The disconnect sound now fires the instant you press the key, with no delay",
    ]),
    ("1.7.1", "Bug fixes", [
        "Fixed Back then Start breaking the settings screen (missing Arm button, sliders, etc.)",
        "Fixed Both mode: pressing the key again to reconnect early could be mistaken for a new disconnect instead of a cancel",
        "In Both mode, Hold can no longer trigger while a tap-started timed cut is still running -- it stays locked out until you're reconnected",
    ]),
]


def app_dir():
    """Per-user folder for settings (and, on Windows, the cached payload)."""
    if IS_WIN:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        path = os.path.join(base, "Webcam")
    elif IS_MAC:
        path = os.path.expanduser("~/Library/Application Support/Webcam")
    else:
        path = os.path.expanduser("~/.webcam")
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


# --- Chime synthesis -------------------------------------------------------
# Original bell-like chimes, generated in code so no audio files are bundled
# (keeps everything inside lagswitch.py, so sounds update live too). Each note
# is a fundamental plus a couple of quieter harmonics under an exponential
# decay envelope -- the decay is what gives it that soft "chime" ring.
SAMPLE_RATE = 44100


def _make_chime(notes, note_dur=0.18, gap=0.04):
    """notes: list of frequencies played in sequence. Returns 16-bit mono WAV bytes."""
    frames = bytearray()
    decay = 5.0  # higher = shorter, more bell-like ring
    for i, freq in enumerate(notes):
        n = int(SAMPLE_RATE * note_dur)
        for s in range(n):
            t = s / SAMPLE_RATE
            env = math.exp(-decay * t)
            sample = (
                1.00 * math.sin(2 * math.pi * freq * t)
                + 0.45 * math.sin(2 * math.pi * freq * 2 * t)
                + 0.20 * math.sin(2 * math.pi * freq * 3 * t)
            )
            value = int(max(-1.0, min(1.0, sample / 1.65 * env)) * 28000)
            frames += struct.pack("<h", value)
        if i < len(notes) - 1:
            frames += b"\x00\x00" * int(SAMPLE_RATE * gap)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(bytes(frames))
    return buf.getvalue()


def _make_buzz():
    """Short dissonant double-buzz for the multi-key-input error -- two close,
    beating frequencies under a fast decay so it reads as "wrong" rather than
    a pleasant chime."""
    frames = bytearray()
    decay = 9.0
    note_dur, gap = 0.09, 0.05
    for i in range(2):
        n = int(SAMPLE_RATE * note_dur)
        for s in range(n):
            t = s / SAMPLE_RATE
            env = math.exp(-decay * t)
            sample = math.sin(2 * math.pi * 220.0 * t) + math.sin(2 * math.pi * 233.0 * t)
            value = int(max(-1.0, min(1.0, sample / 2.0 * env)) * 28000)
            frames += struct.pack("<h", value)
        if i == 0:
            frames += b"\x00\x00" * int(SAMPLE_RATE * gap)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(bytes(frames))
    return buf.getvalue()


def _make_alert():
    """Loud, punchy high->low two-tone for the key actions (Start, Arm/Disarm,
    and the cut firing). Near full-scale and minimally softened so it's clearly
    audible over a game -- deliberately louder than the gentle chimes."""
    frames = bytearray()
    decay = 6.5
    note_dur = 0.13
    for i, freq in enumerate((1174.7, 622.3)):  # D6 -> D#5, high then low
        n = int(SAMPLE_RATE * note_dur)
        for s in range(n):
            t = s / SAMPLE_RATE
            env = math.exp(-decay * t)
            # A touch of square-ish edge (3rd harmonic) for bite, kept loud.
            sample = math.sin(2 * math.pi * freq * t) + 0.25 * math.sin(2 * math.pi * freq * 3 * t)
            value = int(max(-1.0, min(1.0, sample / 1.25 * env)) * 32000)
            frames += struct.pack("<h", value)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(bytes(frames))
    return buf.getvalue()


# Distinct voices. Frequencies are musical notes (Hz).
SOUND_ALERT = _make_alert()                          # loud high-low, key actions
SOUND_TOGGLE_ON = _make_chime([523.3, 659.3], note_dur=0.12, gap=0.02)   # C5 -> E5, quick blip
SOUND_TOGGLE_OFF = _make_chime([659.3, 523.3], note_dur=0.12, gap=0.02)  # E5 -> C5, mirrored
SOUND_ERROR = _make_buzz()                           # dissonant double-buzz, "input failed"

# Both platforms play from an on-disk .wav: macOS needs a path for afplay, and on
# Windows SND_FILENAME is far more reliable than SND_MEMORY (which is picky about
# WAV headers and was producing no audio). Each chime is written once and reused.
_SOUND_FILES = {}


def _sound_file(wav_bytes):
    path = _SOUND_FILES.get(id(wav_bytes))
    if path is None:
        path = os.path.join(app_dir(), f"chime_{id(wav_bytes)}.wav")
        try:
            with open(path, "wb") as fh:
                fh.write(wav_bytes)
        except OSError:
            return None
        _SOUND_FILES[id(wav_bytes)] = path
    return path


def play_sound(wav_bytes):
    """Fire-and-forget playback of synthesized chime bytes; never blocks the UI."""
    try:
        path = _sound_file(wav_bytes)
        if not path:
            return
        if IS_WIN:
            winsound.PlaySound(
                path,
                winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
            )
        else:
            subprocess.Popen(
                ["afplay", path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except Exception:  # noqa: BLE001 - a missing sound shouldn't break the app
        pass


PFCTL = "/sbin/pfctl"
FW_RULE_OUT = "WebcamBlockOut"
FW_RULE_IN = "WebcamBlockIn"


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

    def cut(self, target_program=None):
        """Block all traffic. Returns (ok, message).

        target_program is accepted for API parity with the Windows engine but
        ignored -- pf can't cleanly scope a cut to a single process, so macOS
        always does a whole-system cut.
        """
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
            return False, "Webcam needs to run as administrator -- relaunch the .exe"
        return True, "ready"

    def cut(self, target_program=None):
        """Block traffic. If target_program (an .exe path) is given, only that
        program's traffic is blocked; otherwise everything is cut."""
        with self._lock:
            extra = [f"program={target_program}"] if target_program else []
            ok_out, err_out = self._run([
                "netsh", "advfirewall", "firewall", "add", "rule",
                f"name={FW_RULE_OUT}", "dir=out", "action=block",
            ] + extra)
            ok_in, err_in = self._run([
                "netsh", "advfirewall", "firewall", "add", "rule",
                f"name={FW_RULE_IN}", "dir=in", "action=block",
            ] + extra)
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


# --- Window enumeration (Windows only) -------------------------------------
def enumerate_windows(own_hwnd=None):
    """List visible, titled top-level windows as [{title, exe, name}].

    Windows-only: walks EnumWindows, resolves each window's owning process to
    its .exe path so the firewall can scope a cut to just that program. Returns
    [] on any other platform. De-duplicates by exe so one app shows once.
    """
    if not IS_WIN:
        return []

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    results = []
    seen_exes = set()

    EnumWindowsProc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
    )

    def _exe_for_pid(pid):
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return None
        try:
            size = wintypes.DWORD(32768)
            buf = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(
                handle, 0, buf, ctypes.byref(size)
            ):
                return buf.value
        finally:
            kernel32.CloseHandle(handle)
        return None

    def _callback(hwnd, _lparam):
        if hwnd == own_hwnd:
            return True
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        title_buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title_buf, length + 1)
        title = title_buf.value.strip()
        if not title:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe = _exe_for_pid(pid.value)
        if not exe or exe in seen_exes:
            return True
        seen_exes.add(exe)
        results.append({
            "title": title,
            "exe": exe,
            "name": os.path.basename(exe),
        })
        return True

    try:
        user32.EnumWindows(EnumWindowsProc(_callback), 0)
    except Exception:  # noqa: BLE001 - never let enumeration crash the app
        pass
    return results


# --- Application -----------------------------------------------------------
class WebcamApp:
    def __init__(self, root, update_available=False):
        self.root = root
        self.engine = make_engine()
        self.update_available = update_available
        self.exit_action = "quit"

        settings = load_settings()
        self.unlocked = bool(settings.get("unlocked", False))

        # Fresh install: nothing to announce, so silently mark the current
        # version as already seen instead of showing "What's New" on first run.
        self.last_seen_version = settings.get("last_seen_version")
        first_run = self.last_seen_version is None
        if first_run:
            self.last_seen_version = CURRENT_VERSION

        self.armed = False
        self.bound_key = self._deserialize_key(settings.get("bound_key"))
        self.bound_key_label = self._key_name(self.bound_key) if self.bound_key else "Bind"
        self.arm_key = self._deserialize_key(settings.get("arm_key"))
        self.arm_key_label = self._key_name(self.arm_key) if self.arm_key else "Bind"
        self.duration = tk.DoubleVar(value=float(settings.get("duration", 3.0)))
        self.duration_display = tk.StringVar(value=f"{self.duration.get():.1f}s")
        # Hold-mode auto-reconnect cap: 0 = infinite (cut until the key is
        # released); otherwise 0.1-12s (reconnect at the cap even if still held).
        self.hold_limit = tk.DoubleVar(value=float(settings.get("hold_limit", 0.0)))
        self._capture_mode = None  # None / "trigger" / "arm"

        # Three trigger modes, chosen with the MODE selector on the settings screen:
        #   "toggle" -> timed cut (press -> cut for the DISCONNECT FOR duration;
        #               press again mid-cut to cancel early).
        #   "hold"   -> cut only while the key is held (capped by HOLD FOR).
        #   "both"   -> a quick tap behaves like "toggle", a real hold like "hold".
        mode = settings.get("mode")
        if mode not in ("toggle", "hold", "both"):
            # Migrate the old ON/OFF flag: ON was timed (toggle), OFF was hold.
            mode = "toggle" if settings.get("toggle_on", True) else "hold"
        self.mode = mode
        self._key_down = False         # debounces key-repeat in hold/both mode
        self._arm_key_down = False     # debounces key-repeat for the arm hotkey
        self._cancel_event = threading.Event()

        # Live cut bookkeeping, read by the single cut worker (_run_cut):
        self._active_mode = None       # None / "timed" / "hold" -- what's cutting now
        self._reconnect_at = None      # monotonic deadline, or None = hold until released
        self._both_pending = False     # "both": a press awaiting tap-vs-hold resolution
        self._both_press_time = 0.0
        # Bumped on every new cut so a superseded worker's trailing cleanup can't
        # tear down the overlays a newer cut now owns (rapid successive cuts).
        self._cut_gen = 0

        # When each key was last pressed (stable key id -> time.monotonic()), so a
        # hotkey can require a ~50ms window with no other key pressed just before it.
        # Keyed by _key_id (not raw pynput objects) because on Windows the release
        # event hands back a different object than the press, which used to leak a
        # never-emptying "held keys" set and stick the multi-key warning on forever.
        self._key_press_times = {}

        # Per-app cut target (Windows only). None = whole system. Not persisted
        # because the chosen window may be gone by the next launch.
        self.target_program = None
        self.target_label = "Whole system"

        if first_run:
            save_settings({**settings, "last_seen_version": self.last_seen_version})

        root.title("7amany's Webcam")
        root.configure(bg=BG)
        root.geometry("600x620")
        root.minsize(540, 520)
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
            self.listener = keyboard.Listener(
                on_press=self._on_global_key,
                on_release=self._on_global_key_release,
            )
            self.listener.daemon = True
            self.listener.start()
        except Exception as exc:  # noqa: BLE001
            self.listener = None
            print(f"[Webcam] Could not start global key listener: {exc}")

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
        row = tk.Frame(parent, bg=BG, width=500, height=56)
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
            frame, "7AMANY'S WEBCAM", self.title_font
        ).pack(pady=(0, 28))

        self._make_button(frame, "Start", self._start_clicked, big=True).pack()

        if not self.unlocked:
            self.token_entry = self._make_token_entry(frame)
            self.token_entry.pack(pady=(16, 6))

        self.token_error = self._make_label(frame, "", self.small_font, DANGER)
        self.token_error.pack()

        if self.update_available:
            self._make_button(frame, "Update", self._do_update).pack(pady=(16, 0))

        self._text_link(self.container, "Patch Notes", self.show_patch_notes).place(
            relx=1.0, rely=1.0, x=-20, y=-20, anchor="se"
        )

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
                self.token_error.config(text="You Must Enter Your Token First", fg=DANGER)
                return
            # Validate with the cloud on a worker thread so the UI doesn't freeze
            # during the network round-trip.
            self.token_error.config(text="Checking…", fg=ACCENT)
            threading.Thread(
                target=self._validate_token_worker, args=(token,), daemon=True
            ).start()
            return
        self._proceed_after_unlock()

    def _validate_token_worker(self, token):
        ok, msg = validate_token(token)

        def done():
            if ok:
                self.unlocked = True
                self._save_settings()
                self._proceed_after_unlock()
            else:
                self.token_error.config(text=msg, fg=DANGER)

        self._ui(done)

    def _proceed_after_unlock(self):
        if self.update_available:
            self.token_error.config(text="Update Required", fg=DANGER)
            return
        play_sound(SOUND_ALERT)
        if self.last_seen_version != CURRENT_VERSION:
            self.last_seen_version = CURRENT_VERSION
            self._save_settings()
            self.show_whats_new()
        else:
            self.show_config()

    def _do_update(self):
        self.exit_action = "update"
        self.on_close()

    def show_whats_new(self):
        self._clear()
        outer = tk.Frame(self.container, bg=BG)
        outer.place(relx=0.5, rely=0.5, anchor="center")

        self._glow_text(outer, "WHAT'S NEW", self.header_font).pack(pady=(0, 10))

        version, title, bullets = CHANGELOG[-1]
        self._make_label(
            outer, f"v{version} — {title}", self.body_font, ACCENT
        ).pack(pady=(0, 12))

        text = tk.Text(
            outer,
            width=46,
            height=min(len(bullets) * 2 + 1, 12),
            wrap="word",
            font=self.small_font,
            bg=PANEL,
            fg=TEXT,
            relief="flat",
            bd=0,
            highlightthickness=2,
            highlightbackground=ACCENT,
            padx=14,
            pady=12,
        )
        for bullet in bullets:
            text.insert("end", f"•  {bullet}\n\n")
        text.config(state="disabled")
        text.pack(pady=(0, 18))

        self._make_button(outer, "Next", self.show_config, big=True).pack()
        self._force_redraw()

    def show_patch_notes(self):
        picker = tk.Toplevel(self.root)
        picker.title("Patch Notes")
        picker.configure(bg=BG)
        picker.geometry("480x520")
        picker.transient(self.root)

        self._glow_text(picker, "PATCH NOTES", self.header_font).pack(pady=(16, 10))

        wrap = tk.Frame(picker, bg=BG)
        wrap.pack(fill="both", expand=True, padx=16, pady=(0, 16))

        scrollbar = tk.Scrollbar(wrap)
        scrollbar.pack(side="right", fill="y")
        text = tk.Text(
            wrap,
            wrap="word",
            font=self.small_font,
            bg=PANEL,
            fg=TEXT,
            relief="flat",
            bd=0,
            highlightthickness=0,
            padx=14,
            pady=12,
            yscrollcommand=scrollbar.set,
        )
        text.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=text.yview)

        for version, title, bullets in CHANGELOG:  # oldest -> newest
            text.insert("end", f"v{version} — {title}\n", ("hdr",))
            for bullet in bullets:
                text.insert("end", f"   •  {bullet}\n")
            text.insert("end", "\n")
        text.tag_config("hdr", foreground=ACCENT, font=self.body_font)
        text.config(state="disabled")

    def show_config(self):
        self._clear()
        outer = tk.Frame(self.container, bg=BG)
        outer.place(relx=0.5, rely=0.5, anchor="center")

        self._glow_text(
            outer, "WEBCAM SETTINGS", self.header_font
        ).pack(anchor="w", pady=(0, 18))

        self._connector_row(outer, "MODE", self._build_mode_value)

        _, self.key_button = self._connector_row(
            outer,
            "TRIGGER KEY",
            lambda row: self._badge(row, self.bound_key_label, command=self.begin_capture),
        )

        self._connector_row(outer, "DISCONNECT FOR", self._build_duration_slider)
        self._connector_row(outer, "HOLD FOR", self._build_hold_slider)

        if IS_WIN:
            _, self.target_button = self._connector_row(
                outer,
                "TARGET",
                lambda row: self._badge(row, self._target_display(), command=self._select_window),
            )

        arm_row = tk.Frame(outer, bg=BG)
        arm_row.pack(pady=(26, 10))
        self.arm_key_button = self._badge(arm_row, self.arm_key_label, command=self.begin_capture_arm)
        self.arm_key_button.pack(side="left", padx=(0, 10))
        self.arm_button = self._badge(arm_row, "Arm", command=self.toggle_arm, dark=True)
        self.arm_button.pack(side="left")

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

    def _build_mode_value(self, parent):
        """MODE row value: a three-way Toggle / Hold / Both segmented selector."""
        wrap = tk.Frame(parent, bg=BG)
        self.mode_buttons = {}
        for m, label in (("toggle", "Toggle"), ("hold", "Hold"), ("both", "Both")):
            btn = self._badge(wrap, label, command=lambda mm=m: self._set_mode(mm))
            btn.pack(side="left", padx=(0, 6))
            self.mode_buttons[m] = btn
        self._refresh_mode_buttons()
        return wrap

    def _refresh_mode_buttons(self):
        """Highlight the selected mode badge; dim the other two."""
        if not hasattr(self, "mode_buttons"):
            return
        for m, btn in self.mode_buttons.items():
            if m == self.mode:
                btn.config(bg=ACCENT, fg="white",
                           activebackground=ACCENT_HI, activeforeground="white")
            else:
                btn.config(bg=BADGE_BG, fg=BADGE_FG,
                           activebackground=ACCENT_HI, activeforeground="white")

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
        self.duration_slider = slider
        self.duration_value_badge = value_badge
        self._apply_slider_state()
        return wrap

    def _hold_display(self):
        v = round(self.hold_limit.get(), 1)
        return "∞" if v <= 0 else f"{v:.1f}s"

    def _build_hold_slider(self, parent):
        """Hold-mode cap slider: leftmost (0) shows ∞ = hold until release,
        otherwise 0.1-12s to auto-reconnect at the cap even while still held."""
        wrap = tk.Frame(parent, bg=BG)

        value_badge = self._make_label(wrap, self._hold_display(), self.body_font, BADGE_FG, BADGE_BG)
        value_badge.config(width=5, highlightthickness=2, highlightbackground=ACCENT)
        value_badge.pack(side="right", padx=(10, 0))

        def on_move(_value):
            value_badge.config(text=self._hold_display())

        def on_release(_event):
            self._save_settings()

        slider = tk.Scale(
            wrap,
            from_=0.0,
            to=12.0,
            resolution=0.1,
            orient="horizontal",
            variable=self.hold_limit,
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
        self.hold_slider = slider
        self.hold_value_badge = value_badge
        self._apply_slider_state()
        return wrap

    def _apply_slider_state(self):
        """Grey out whichever slider the current mode doesn't use.
        DISCONNECT FOR drives Toggle (and a Both tap); HOLD FOR drives Hold
        (and a Both hold); Both uses both sliders.

        Checks winfo_exists(), not just hasattr(): this runs once from each of
        _build_duration_slider/_build_hold_slider, and on a second show_config()
        the *other* row hasn't been rebuilt yet -- self.hold_slider (or
        duration_slider) still points at the widget _clear() just destroyed.
        Touching a destroyed Tk widget raises TclError, which used to abort the
        rest of show_config() partway through (no Arm button, no Back link)."""
        timed = self.mode in ("toggle", "both")
        hold = self.mode in ("hold", "both")
        if getattr(self, "duration_slider", None) is not None and self.duration_slider.winfo_exists():
            if timed:
                self.duration_slider.config(state="normal", fg=ACCENT, troughcolor=PANEL)
                self.duration_value_badge.config(fg=BADGE_FG)
            else:
                self.duration_slider.config(state="disabled", fg=TEXT_DIM, troughcolor=BG)
                self.duration_value_badge.config(fg=TEXT_DIM)
        if getattr(self, "hold_slider", None) is not None and self.hold_slider.winfo_exists():
            if hold:
                self.hold_slider.config(state="normal", fg=ACCENT, troughcolor=PANEL)
                self.hold_value_badge.config(fg=BADGE_FG)
            else:
                self.hold_slider.config(state="disabled", fg=TEXT_DIM, troughcolor=BG)
                self.hold_value_badge.config(fg=TEXT_DIM)

    # -- Mode: toggle / hold / both -----------------------------------------
    def _mode_verb(self):
        return {"toggle": "press", "hold": "hold", "both": "tap/hold"}.get(self.mode, "press")

    def _set_mode(self, mode):
        if self.armed:
            self.set_status("Disarm before switching mode.", TEXT_DIM)
            return
        if mode == self.mode:
            return
        self.mode = mode
        self._refresh_mode_buttons()
        self._apply_slider_state()
        self._save_settings()
        play_sound(SOUND_TOGGLE_ON)
        self._flash_overlay("top_left", f"MODE — {mode.upper()}", ACCENT)

    # -- Select Window: per-app cut target (Windows only) -------------------
    def _target_display(self):
        return "Select Window" if self.target_program is None else self.target_label

    def _select_window(self):
        if self.armed:
            self.set_status("Disarm before changing the target.", TEXT_DIM)
            return
        own_hwnd = None
        try:
            own_hwnd = self.root.winfo_id()
        except Exception:  # noqa: BLE001
            pass
        windows = enumerate_windows(own_hwnd)

        picker = tk.Toplevel(self.root)
        picker.title("Select Window")
        picker.configure(bg=BG)
        picker.geometry("420x460")
        picker.transient(self.root)

        self._glow_text(picker, "SELECT WINDOW", self.header_font).pack(pady=(16, 10))

        listbox_wrap = tk.Frame(picker, bg=BG)
        listbox_wrap.pack(fill="both", expand=True, padx=16, pady=(0, 16))

        scrollbar = tk.Scrollbar(listbox_wrap)
        scrollbar.pack(side="right", fill="y")
        listbox = tk.Listbox(
            listbox_wrap,
            font=self.small_font,
            bg=PANEL,
            fg=TEXT,
            selectbackground=ACCENT,
            selectforeground="white",
            relief="flat",
            bd=0,
            highlightthickness=0,
            activestyle="none",
            yscrollcommand=scrollbar.set,
        )
        listbox.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=listbox.yview)

        # First entry reverts to a whole-system cut.
        entries = [{"title": "Whole system (all apps)", "exe": None, "name": ""}]
        entries += windows
        for item in entries:
            label = item["title"]
            if item["exe"]:
                label = f"{item['title']}  —  {item['name']}"
            listbox.insert("end", label)

        def choose():
            sel = listbox.curselection()
            if sel:
                self._apply_target(entries[sel[0]])
            picker.destroy()

        self._make_button(picker, "Select", choose, big=True).pack(pady=(0, 16))
        listbox.bind("<Double-Button-1>", lambda _e: choose())

    def _apply_target(self, item):
        self.target_program = item["exe"]
        self.target_label = item["name"] if item["exe"] else "Whole system"
        if hasattr(self, "target_button"):
            self.target_button.config(text=self._target_display())

    # -- Keybind capture ----------------------------------------------------
    def begin_capture(self):
        if self._capture_mode is not None:
            return
        if self.armed:
            self.set_status("Disarm before changing the keybind.", TEXT_DIM)
            return
        self._capture_mode = "trigger"
        self.key_button.config(text="Press a key…")
        # The next key is caught by the single long-lived listener below.

    def _finish_capture(self, key):
        self.bound_key = key
        self.bound_key_label = self._key_name(key)
        self._capture_mode = None
        self._refresh_key_button()
        self._save_settings()

    def begin_capture_arm(self):
        if self._capture_mode is not None:
            return
        if self.armed:
            self.set_status("Disarm before changing the arm hotkey.", TEXT_DIM)
            return
        self._capture_mode = "arm"
        self.arm_key_button.config(text="Press a key…")

    def _finish_capture_arm(self, key):
        self.arm_key = key
        self.arm_key_label = self._key_name(key)
        self._capture_mode = None
        self._refresh_key_button()
        self._save_settings()

    def _refresh_key_button(self):
        if hasattr(self, "key_button"):
            self.key_button.config(text=self.bound_key_label)
        if hasattr(self, "arm_key_button"):
            self.arm_key_button.config(text=self.arm_key_label)

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
            "arm_key": self._serialize_key(self.arm_key),
            "duration": self.duration.get(),
            "hold_limit": self.hold_limit.get(),
            "unlocked": self.unlocked,
            "mode": self.mode,
            "last_seen_version": self.last_seen_version,
        })

    @staticmethod
    def _key_id(key):
        """Stable, hashable identity for a key that's the *same on press and
        release* (and matches a key rebuilt from settings). Raw pynput objects
        differ between press and release on Windows, so we normalize to a scalar:
        the printable char, else the virtual-key code, else the string name."""
        char = getattr(key, "char", None)
        if char:
            return char.lower()
        vk = getattr(key, "vk", None)
        if vk is not None:
            return f"vk{vk}"
        return str(key)

    def _other_key_recent(self, hotkey, window=0.05):
        """True if any key other than `hotkey` was pressed within `window`
        seconds of now -- used to require a 50ms quiet gap before a hotkey fires.
        Self-pruning by time, so it can never get permanently stuck."""
        now = time.monotonic()
        hotkey_id = self._key_id(hotkey)
        for kid, t in self._key_press_times.items():
            if now - t < window and kid != hotkey_id:
                return True
        return False

    # -- Global trigger -----------------------------------------------------
    def _on_global_key(self, key):
        # Record this key's press time first (under a stable id), regardless of
        # what else happens below -- this drives the 50ms "quiet window" guard.
        now = time.monotonic()
        self._key_press_times[self._key_id(key)] = now
        # Prune stale timestamps so the dict can't grow without bound.
        if len(self._key_press_times) > 24:
            self._key_press_times = {
                k: t for k, t in self._key_press_times.items() if now - t < 1.0
            }

        if self._capture_mode == "trigger":
            self.root.after(0, self._finish_capture, key)
            return
        if self._capture_mode == "arm":
            self.root.after(0, self._finish_capture_arm, key)
            return

        if self.armed and self.bound_key is not None and self._keys_equal(key, self.bound_key):
            # Trigger guard: reject if any *other* key was pressed in the last 50ms
            # (covers both a held movement key, which auto-repeats, and a quick tap
            # right before the trigger). Time-pruned, so it can never stick.
            if self._other_key_recent(self.bound_key):
                self.root.after(0, self._reject_multi_key)
            elif self.mode == "toggle":
                # Timed: a press while already cutting (or still being cut --
                # see _active_mode) cancels it early.
                if self._active_mode is not None:
                    self.root.after(0, self.cancel_cut)
                else:
                    play_sound(SOUND_ALERT)  # immediate: fired off the listener thread
                    self.root.after(0, self.trigger_cut)
            elif self.mode == "hold":
                # Cut on the first press, ignore key-repeat until release.
                if not self._key_down:
                    self._key_down = True
                    play_sound(SOUND_ALERT)  # immediate
                    self.root.after(0, self._start_hold_cut)
            else:  # both
                # A running timed cut (from a prior tap) is cancelled by a press.
                # Checked via _active_mode, not engine.is_cutting: the latter only
                # flips True once the (sometimes slow, especially on Windows)
                # firewall call actually returns, so a fast "press again to
                # reconnect" could land in that gap and be mistaken for a brand
                # new disconnect attempt instead of a cancel. _active_mode is set
                # synchronously the instant a cut is requested, so it has no gap.
                if self._active_mode == "timed":
                    self.root.after(0, self.cancel_cut)
                elif not self._key_down:
                    # Start as a hold now; the release decides tap-vs-hold.
                    self._key_down = True
                    self._both_pending = True
                    self._both_press_time = now
                    play_sound(SOUND_ALERT)  # immediate
                    self.root.after(0, self._start_hold_cut)

        if self.arm_key is not None and self._keys_equal(key, self.arm_key):
            if self._other_key_recent(self.arm_key):
                self.root.after(0, self._reject_multi_key)
            elif not self._arm_key_down:
                self._arm_key_down = True
                self.root.after(0, self.toggle_arm)

    def _on_global_key_release(self, key):
        # Match by stable id so a release object that differs from the press
        # object still clears the debounce flag (else hold mode could stick on).
        rid = self._key_id(key)

        if self.bound_key is not None and rid == self._key_id(self.bound_key):
            was_down = self._key_down
            self._key_down = False  # always clear, even if the cap already fired
            if self.mode == "hold":
                if was_down:
                    self.root.after(0, self._release_hold)
            elif self.mode == "both":
                pending = self._both_pending
                self._both_pending = False
                if was_down:
                    self.root.after(0, lambda p=pending: self._release_both(p))
            # "toggle" mode ignores releases (the timer or a second press ends it).

        if self.arm_key is not None and rid == self._key_id(self.arm_key):
            self._arm_key_down = False

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
        # Clear recent-press history so a tap right before arming can't block
        # the very first trigger, and reset any stale cut bookkeeping.
        self._key_press_times = {}
        self._key_down = False
        self._both_pending = False
        self._active_mode = None
        self.armed = True
        self.arm_button.config(text="Disarm")
        verb = self._mode_verb()
        self.set_status(f"Armed — {verb} {self.bound_key_label}", ACCENT)
        play_sound(SOUND_ALERT)
        self._flash_overlay("top_left", f"ARMED — {verb} {self.bound_key_label}", ACCENT)

    def disarm(self):
        self.armed = False
        # Safety: if somehow mid-cut, make sure we're reconnected and the
        # overlays stop (also cancel so the worker thread breaks promptly).
        self._cancel_event.set()
        if self.engine.is_cutting:
            self.engine.restore()
        self._stop_countdown()
        self._hide_disconnected()
        self._active_mode = None
        self._key_down = False
        self._both_pending = False
        self.arm_button.config(text="Arm")
        self.set_status("Disarmed", TEXT_DIM)
        play_sound(SOUND_ALERT)
        self._flash_overlay("top_left", "DISARMED", TEXT)

    # -- The cut ------------------------------------------------------------
    # One worker (_run_cut) drives every cut. It reads self._reconnect_at each
    # iteration -- a monotonic deadline, or None to stay cut until released --
    # so a "both"-mode release can convert a hold into a timed cut on the fly,
    # and self._cancel_event lets any release / second press / disarm end it now.
    def trigger_cut(self):
        """Start a timed cut (Toggle press)."""
        if self._active_mode is not None or not self.armed:
            return
        self._active_mode = "timed"
        self._reconnect_at = time.monotonic() + self.duration.get()
        self._cancel_event.clear()
        self._cut_gen += 1
        threading.Thread(target=self._run_cut, args=(self._cut_gen,), daemon=True).start()

    def _start_hold_cut(self):
        """Start a hold cut (Hold press, or the opening of a Both press).
        Capped by HOLD FOR; an infinite cap (0) holds until the key is released."""
        if self._active_mode is not None or not self.armed:
            return
        self._active_mode = "hold"
        limit = self.hold_limit.get()
        self._reconnect_at = (time.monotonic() + limit) if limit > 0 else None
        self._cancel_event.clear()
        self._cut_gen += 1
        threading.Thread(target=self._run_cut, args=(self._cut_gen,), daemon=True).start()

    def cancel_cut(self):
        # _active_mode (not engine.is_cutting) is the source of truth for "a cut
        # is active or being started" -- see the "both" branch of _on_global_key
        # for why engine.is_cutting lags behind actual intent.
        if self._active_mode is not None:
            self._cancel_event.set()

    def _release_hold(self):
        """Hold mode: releasing the key reconnects (unless the cap already did)."""
        if self._active_mode == "hold":
            self._cancel_event.set()

    def _release_both(self, pending):
        """Both mode: on release, decide whether the press was a tap or a hold."""
        if not pending:
            return
        if self._active_mode != "hold":
            return  # the cap already fired, or nothing is cutting
        held = time.monotonic() - self._both_press_time
        if held <= BOTH_TAP_MAX:
            # Tap -> behave like Toggle: keep cutting for the DISCONNECT FOR
            # window measured from the original press, then auto-reconnect.
            self._active_mode = "timed"
            deadline = self._both_press_time + self.duration.get()
            if deadline <= time.monotonic():
                self._cancel_event.set()  # window already elapsed; reconnect now
            else:
                self._reconnect_at = deadline
                self._ui(self._start_countdown)  # show the timer for the new deadline
        else:
            self._cancel_event.set()  # genuine hold released -> reconnect now

    def _run_cut(self, gen):
        ok, msg = self.engine.cut(self.target_program)
        if not ok:
            if gen == self._cut_gen:
                self._active_mode = None
            self._ui(lambda: self.set_status(f"Cut failed: {msg}", DANGER))
            return
        # Persistent top-left "Disconnected" box for the whole cut, every mode.
        self._ui(self._show_disconnected)
        if self._reconnect_at is not None:
            self._ui(self._start_countdown)  # top-center timer when there's a deadline
        while True:
            if self._cancel_event.is_set():
                break
            deadline = self._reconnect_at  # re-read live (a Both release can change it)
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._ui(lambda r=remaining: self.set_status(
                    f"CUT — {max(r, 0):.1f}s left", DANGER))
                if self._cancel_event.wait(timeout=min(0.05, max(0.005, remaining))):
                    break
            else:
                self._ui(lambda: self.set_status("CUT — holding…", DANGER))
                if self._cancel_event.wait(timeout=0.1):
                    break
        # Tear down overlays only if no newer cut superseded us (these callbacks
        # re-check the generation when they actually run on the Tk thread, so a
        # stale worker can't kill a fresh cut's countdown/box).
        self._ui(lambda g=gen: g == self._cut_gen and self._stop_countdown())
        self._ui(lambda g=gen: g == self._cut_gen and self._hide_disconnected())
        self._ui(lambda g=gen: self.set_status("Reconnecting…", TEXT_DIM)
                 if g == self._cut_gen else None)
        ok, msg = self.engine.restore()
        if gen == self._cut_gen:
            self._active_mode = None

        def finish(g=gen, ok=ok, msg=msg):
            if g != self._cut_gen:
                return  # a newer cut owns the status line now
            if not ok:
                self.set_status(f"Reconnect issue: {msg}", DANGER)
            elif self.armed:
                self.set_status(f"Armed — {self._mode_verb()} {self.bound_key_label}", ACCENT)
            else:
                self.set_status("Disarmed", TEXT_DIM)
        self._ui(finish)

    # -- Full-screen (Windows only; macOS has the native green button) ------
    def _toggle_fullscreen(self, _event=None):
        self._fullscreen = not self._fullscreen
        self.root.attributes("-fullscreen", self._fullscreen)

    def _exit_fullscreen(self, _event=None):
        self._fullscreen = False
        self.root.attributes("-fullscreen", False)

    # -- Anti-ghost-input guard ----------------------------------------------
    def _reject_multi_key(self):
        play_sound(SOUND_ERROR)
        self._flash_overlay(
            "top_right", "INPUT FAILED — MULTIPLE KEYS PRESSED", DANGER
        )

    # -- On-screen overlays (separate always-on-top windows, not the app UI) -
    def _overlay(self, corner):
        attr = f"_overlay_{corner}"
        win = getattr(self, attr, None)
        if win is not None and win.winfo_exists():
            return win
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        try:
            win.attributes("-alpha", 0.94)
        except tk.TclError:
            pass
        win.configure(bg=PANEL)
        label = self._make_label(win, "", self.body_font, TEXT, PANEL)
        label.config(highlightthickness=2, highlightbackground=ACCENT, padx=16, pady=12)
        label.pack()
        win.withdraw()
        setattr(self, attr, win)
        setattr(self, f"{attr}_label", label)
        return win

    def _flash_overlay(self, corner, text, color=TEXT, duration_ms=1600):
        win = self._overlay(corner)
        label = getattr(self, f"_overlay_{corner}_label")
        label.config(text=text, fg=color)
        win.update_idletasks()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        margin = 24
        if corner == "top_left":
            # Sit just below the persistent "Disconnected" box, which owns the corner.
            x, y = margin, margin + 56
        elif corner == "top_center":
            x, y = (win.winfo_screenwidth() - w) // 2, margin
        else:  # top_right
            x, y = win.winfo_screenwidth() - w - margin, margin
        win.geometry(f"{w}x{h}+{x}+{y}")
        win.deiconify()
        win.lift()

        timer_attr = f"_overlay_{corner}_timer"
        existing = getattr(self, timer_attr, None)
        if existing is not None:
            try:
                self.root.after_cancel(existing)
            except Exception:  # noqa: BLE001
                pass
        new_timer = self.root.after(duration_ms, win.withdraw)
        setattr(self, timer_attr, new_timer)

    # -- Top-center countdown overlay (whenever the cut has a deadline) -------
    def _start_countdown(self):
        """Show a live top-center timer counting the current cut deadline
        (self._reconnect_at) -> 0 in 0.01s steps. Idempotent and runs entirely
        on the Tk main thread. Does nothing useful for an infinite hold."""
        existing = getattr(self, "_countdown_after", None)
        if existing is not None:
            try:
                self.root.after_cancel(existing)
            except Exception:  # noqa: BLE001
                pass
            self._countdown_after = None
        self._countdown_on = True
        self._tick_countdown()

    def _tick_countdown(self):
        if not getattr(self, "_countdown_on", False):
            return
        deadline = getattr(self, "_reconnect_at", None)
        if deadline is None:
            # No fixed deadline (infinite hold) -- nothing to count down.
            self._stop_countdown()
            return
        remaining = deadline - time.monotonic()
        win = self._overlay("top_center")
        label = self._overlay_top_center_label
        if remaining <= 0:
            label.config(text="0.00", fg=DANGER, font=self.title_font)
            self._place_top_center(win)
            self.root.after(120, self._stop_countdown)
            self._countdown_on = False  # stop further ticks; the after() hides it
            return
        label.config(text=f"{remaining:.2f}", fg=DANGER, font=self.title_font)
        self._place_top_center(win)
        self._countdown_after = self.root.after(10, self._tick_countdown)

    def _place_top_center(self, win):
        win.update_idletasks()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        x = (win.winfo_screenwidth() - w) // 2
        win.geometry(f"{w}x{h}+{x}+24")
        win.deiconify()
        win.lift()

    def _stop_countdown(self):
        self._countdown_on = False
        existing = getattr(self, "_countdown_after", None)
        if existing is not None:
            try:
                self.root.after_cancel(existing)
            except Exception:  # noqa: BLE001
                pass
            self._countdown_after = None
        win = getattr(self, "_overlay_top_center", None)
        if win is not None and win.winfo_exists():
            win.withdraw()

    # -- Persistent "Disconnected" box (top-left, shown for the whole cut) ---
    def _show_disconnected(self):
        win = self._overlay("disc")
        label = self._overlay_disc_label
        label.config(text="Disconnected", fg=DANGER, font=self.header_font)
        win.update_idletasks()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        win.geometry(f"{w}x{h}+24+24")
        win.deiconify()
        win.lift()

    def _hide_disconnected(self):
        win = getattr(self, "_overlay_disc", None)
        if win is not None and win.winfo_exists():
            win.withdraw()

    # -- Utilities ----------------------------------------------------------
    def _ui(self, fn):
        """Schedule a callable on the Tk main thread.

        Catches *everything*: this is called from worker threads, and a flaky
        cross-thread Tk call must never propagate and kill a worker before it
        finishes the actual network restore (which would strand the user
        offline).
        """
        try:
            self.root.after(0, fn)
        except Exception:  # noqa: BLE001
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
    app = WebcamApp(root, update_available=update_available)
    root.mainloop()
    return app.exit_action


def main():
    run(False)


if __name__ == "__main__":
    main()
