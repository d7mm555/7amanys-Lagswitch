# 7amany's Webcam

> Internally this project began as a "lagswitch"; the app is now named **Webcam**
> everywhere except its patch notes. The source file is still `lagswitch.py` (that's
> the path the live-update fetch relies on) and the GitHub repo keeps its name.

A desktop app (macOS + Windows) that instantly cuts your computer off the
internet while a chosen key is pressed, holds the cut for a set duration
(0.5–10 seconds, adjustable via slider), then **automatically reconnects**.

It cuts traffic at the firewall layer — `pfctl` on macOS, Windows Defender
Firewall on Windows — so the network link stays associated; both the cut and
the reconnect are near-instant, which keeps the short timer accurate. The
trigger key is captured **globally**, so it fires even while a game or
another app is focused. A **MODE** selector picks between **Toggle** (timed cut — press again mid-cut
to cancel early), **Hold** (cut only while the key is held, with an optional
∞/0.1–12 s cap), and **Both** (a quick tap acts like Toggle, a real hold acts
like Hold). A separate hotkey can Arm/Disarm directly. A loud high-low alert
fires the instant the cut key is pressed (plus on Start and Arm/Disarm), and a
**Disconnected** box shows top-left the whole time you're cut. The trigger is
blocked if another key is held **or** was pressed within the last 50 ms, with
an on-screen warning instead of misfiring. Sounds are original synthesized
tones. Your trigger key, arm hotkey, durations, and mode are remembered
between launches. A **Patch Notes** view (bottom-right of the intro screen)
lists every version's changes.

Access is gated by a **cloud-validated token** that locks to the first device
that uses it — set up your own token backend via
[SETUP-TOKENS.md](SETUP-TOKENS.md) (no tokens live in this public code).

**On Windows?** See [SETUP-WINDOWS.md](SETUP-WINDOWS.md) — no Windows machine
needed to build the `.exe`, GitHub's cloud runner does it for you. The
Windows build adds two things macOS can't do: **live updates** (push a change
to `lagswitch.py` on GitHub and the installed `.exe` offers an **Update**
button next launch — no redownload) and **Select Window** (cut just one
app's internet instead of the whole PC). Everything below this point is the
macOS setup.

---

## One-time setup: passwordless firewall access

Without this, macOS will ask for your admin password every single time you
press **Arm**. This grants your account permission to run *only* these three
exact `pfctl` commands without a password — nothing else gets elevated access.

Run this once in Terminal (you'll enter your password this one time):

```sh
echo "$(whoami) ALL=(root) NOPASSWD: /sbin/pfctl -e -f /tmp/webcam_block.conf, /sbin/pfctl -f /etc/pf.conf, /sbin/pfctl -d" | sudo tee /etc/sudoers.d/webcam > /dev/null
sudo chmod 440 /etc/sudoers.d/webcam
sudo visudo -c
```

The last command validates the file. It should print something like
`/etc/sudoers.d/webcam: parsed OK`. If it instead reports an error, undo
with `sudo rm /etc/sudoers.d/webcam` and don't proceed until it's fixed.

To remove this permission later: `sudo rm /etc/sudoers.d/webcam`.

> If you set this up under the old name, remove the stale rule too:
> `sudo rm -f /etc/sudoers.d/lagswitch`.

## Option A: run the script directly

1. **Install the dependency:**
   ```sh
   pip3 install -r requirements.txt
   ```

2. **Grant permission** for the global key listener — open
   **System Settings → Privacy & Security**, then under both **Accessibility**
   and **Input Monitoring**: click **+**, press **Cmd+Shift+G**, type the path
   to whatever `python3` you're running (check with `which python3`), and
   toggle it on in both lists.

3. **Run it:**
   ```sh
   cd "Project 2 - Lagswitch"
   python3 lagswitch.py
   ```

## Option B: build a real double-clickable .app (no Terminal at all)

This packages the script into `Webcam.app` using `py2app`.

1. **Install the build tool:**
   ```sh
   pip3 install py2app
   ```
2. **Build it:**
   ```sh
   cd "Project 2 - Lagswitch"
   python3 setup.py py2app
   ```
   This produces `dist/Webcam.app`.
3. **Install it:** drag `dist/Webcam.app` into `/Applications`.
4. **Double-click it to launch.** The first time, macOS will ask for
   Accessibility + Input Monitoring permission for **Webcam** itself (not
   Terminal/python3) — grant both under
   **System Settings → Privacy & Security**, then relaunch the app.
5. To rebuild after editing `lagswitch.py`, delete the `build/` and `dist/`
   folders first, then repeat step 2.

## Using it

1. The intro screen shows **7AMANY'S LAGSWITCH** — click **Start**.
2. Click the **Bind** badge, then press the key you want (e.g. `F8`).
3. Click the seconds badge and pick a duration (1–10 seconds).
4. Click **Arm**. If the one-time sudoers setup above was done, this happens
   instantly with no password prompt.
5. Press your key anytime — even inside a game — to cut the internet for the
   chosen duration. It reconnects on its own.

Click **Disarm** to stop, or just close the window. Drag the green
full-screen button in the title bar (or the native macOS full-screen
gesture) to use it full-screen — the window is resizable.

## Safety

- Closing the window (or disarming) always restores your connection, so a cut
  can't leave you stranded offline.
- Pressing the key again while a cut is already running is ignored.
- If the one-time sudoers setup wasn't done, clicking **Arm** fails instantly
  with an error in the status line instead of cutting your internet partway.

## How it works

- **Cut:** writes a `block drop all` ruleset to `/tmp/lagswitch_block.conf` and
  loads it with `sudo -n pfctl -e -f …`.
- **Restore:** reloads the default `/etc/pf.conf` and disables pf with
  `sudo -n pfctl -d`, returning to the normal (default-off) firewall state.
- The `-n` flag means "never prompt" — these calls only succeed because of the
  one-time sudoers rule above; without it they fail cleanly rather than
  hanging on a password prompt with no Terminal to show it in.

## Caveats

- The restore step resets the firewall to macOS's default (pf off). This is
  correct for a normal desktop, but **if you run your own custom `pf` rules,
  this app will reset them.** Don't use it as-is if you depend on a custom pf
  config.
- The sudoers rule lets *any* process running as your user invoke those three
  exact `pfctl` commands without a password — scoped tightly (just those three
  firewall toggles, nothing else), but worth knowing it's not limited to this
  app specifically.
