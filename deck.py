"""
Launchpad Mini MK3 — macro deck (Stream-Deck style).

Programs pads to run actions: launch apps, media play/pause, next/prev,
volume up/down/mute, Discord mute/deafen (via a global hotkey you set in
Discord). Pads are colour-coded by category, flash white on press, and a
legend is printed / shown so you know what each pad does.

Layout is loaded from deck_config.json if present, else a sensible default
is used (and written to that file so you can edit it).

Run:  python deck.py            (add --list to see MIDI ports)
"""
import argparse
import collections
import ctypes
import glob
import json
import math
import os
import random
import subprocess
import sys
import threading
import time
import warnings

sys.coinit_flags = 0        # COINIT_MULTITHREADED for comtypes (matches soundcard) -> no COM clash

import numpy as np
import pygame.midi as pm
import soundcard as sc
import lightshow as LS
import winmidi

warnings.filterwarnings("ignore")

HDR = [0x00, 0x20, 0x29, 0x02, 0x0D]
SYSEX_PROGRAMMER = [0xF0] + HDR + [0x0E, 0x01, 0xF7]
SYSEX_LIVE = [0xF0] + HDR + [0x0E, 0x00, 0xF7]
N = 8
HERE = os.path.dirname(os.path.abspath(__file__))
if getattr(sys, "frozen", False):        # bundled .exe -> writable user config
    _cfgdir = os.path.join(os.environ.get("APPDATA", HERE), "LaunchpadDeck")
    try:
        os.makedirs(_cfgdir, exist_ok=True)
    except Exception:
        _cfgdir = HERE
    CONFIG = os.path.join(_cfgdir, "deck_config.json")
else:
    CONFIG = os.path.join(HERE, "deck_config.json")

LOGDIR = os.path.dirname(CONFIG)
LOGFILE = os.path.join(LOGDIR, "deck.log")
try:
    import faulthandler
    _logfh = open(LOGFILE, "a", buffering=1, encoding="utf-8")
    faulthandler.enable(_logfh)              # dumps native (C-level) crashes here too
except Exception:
    _logfh = None


def log(msg):
    print(msg, flush=True)
    try:
        if _logfh:
            import datetime
            _logfh.write(f"{datetime.datetime.now():%H:%M:%S} {msg}\n")
    except Exception:
        pass

# ---------------- colours (RGB 0-127) ----------------
C = {
    "blue": (0, 40, 127), "green": (0, 127, 25), "red": (127, 0, 0),
    "orange": (127, 45, 0), "cyan": (0, 110, 120), "purple": (85, 0, 127),
    "yellow": (120, 110, 0), "white": (127, 127, 127), "pink": (127, 0, 70),
    "spotify": (20, 120, 45), "discord": (70, 60, 127), "off": (0, 0, 0),
}
TOP_ROW = [91, 92, 93, 94, 95, 96, 97, 98]     # top round buttons (CC) -> light-show controls
RIGHT_COL = [19, 29, 39, 49, 59, 69, 79, 89]   # right column (CC) -> sensitivity / input level

_LIB_CACHE = None


def get_clip_library():
    """Load the project light-show clips once (from Desktop *\\Lights\\*.mid)."""
    global _LIB_CACHE
    if _LIB_CACHE is None:
        try:
            desk = os.path.join(os.path.expanduser("~"), "Desktop")
            folders = [d for d in glob.glob(os.path.join(desk, "*", "Lights")) if os.path.isdir(d)]
            _LIB_CACHE = LS.load_library(folders, cap=200) if folders else []
            print(f"[deck] loaded {len(_LIB_CACHE)} project clips", flush=True)
        except Exception as e:
            print(f"[deck] clip load failed: {e}", flush=True); _LIB_CACHE = []
    return _LIB_CACHE


# ---- scrolling clock (deck 'clock' mode) ----
_FONT = {
    "0": ["111", "101", "101", "101", "111"], "1": ["010", "110", "010", "010", "111"],
    "2": ["111", "001", "111", "100", "111"], "3": ["111", "001", "111", "001", "111"],
    "4": ["101", "101", "111", "001", "001"], "5": ["111", "100", "111", "001", "111"],
    "6": ["111", "100", "111", "101", "111"], "7": ["111", "001", "010", "010", "010"],
    "8": ["111", "101", "111", "101", "111"], "9": ["111", "101", "111", "001", "111"],
    ":": ["000", "010", "000", "010", "000"],
}


class ClockView:
    def _columns(self):
        import datetime
        s = datetime.datetime.now().strftime("%H:%M")
        cols = []
        for ch in s:
            g = _FONT.get(ch, ["000"] * 5)
            for x in range(3):
                cols.append([g[y][x] == "1" for y in range(5)])
            cols.append([False] * 5)          # gap column
        return cols

    def frame(self):                          # -> dict pad_index -> (r,g,b)
        cols = self._columns(); width = len(cols)
        pos = int(time.time() * 4.0) % (width + N)
        col_rgb = (0, 110, 120)
        out = {}
        for x in range(N):
            ci = pos - N + x
            if 0 <= ci < width:
                bits = cols[ci]
                for y in range(5):
                    if bits[y]:
                        out[pad_index(x, 6 - y)] = col_rgb    # digits on rows 6..2
        return out


def pad_index(col, row):     # row 0 = bottom
    return (row + 1) * 10 + (col + 1)


def rgb_sysex(colours):      # colours: dict pad_index -> (r,g,b)
    body = []
    for idx, (r, g, b) in colours.items():
        body += [0x03, idx, int(r), int(g), int(b)]
    return [0xF0] + HDR + [0x03] + body + [0xF7]


# ---- section blocks (visual zones, dim background tint) ----
SECTIONS = [
    ("Media",    [6, 7], [0, 1, 2, 3],             (0, 1, 3)),
    ("Volume",   [6, 7], [4, 5, 6, 7],             (3, 1, 0)),
    ("Programs", [3, 4], [0, 1, 2, 3, 4, 5, 6, 7], (0, 2, 2)),
    ("Voice",    [0, 1], [0, 1, 2, 3, 4, 5, 6, 7], (0, 2, 1)),
]
SECTION_BG = {}
for _name, _rows, _cols, _tint in SECTIONS:
    for _r in _rows:
        for _c in _cols:
            SECTION_BG[pad_index(_c, _r)] = _tint


# ---- 8x8 action icons (drawn full-grid on press) ----
def _parse_icon(rows):
    cells = []
    for i, line in enumerate(rows):
        for c, ch in enumerate(line):
            if ch == "#":
                cells.append((c, 7 - i))     # row 0 = bottom
    return cells


ICONS = {}


def _icon(name, rows, color):
    ICONS[name] = (_parse_icon(rows), color)


_icon("play", ["..#.....", "..##....", "..###...", "..####..",
               "..####..", "..###...", "..##....", "..#....."], C["green"])
_icon("stop", ["........", ".######.", ".######.", ".######.",
               ".######.", ".######.", ".######.", "........"], C["pink"])
_icon("next", [".#....#.", ".##...#.", ".###..#.", ".####.#.",
               ".####.#.", ".###..#.", ".##...#.", ".#....#."], C["blue"])
_icon("prev", [".#....#.", ".#...##.", ".#..###.", ".#.####.",
               ".#.####.", ".#..###.", ".#...##.", ".#....#."], C["blue"])
_icon("volup", ["...##...", "...##...", "...##...", "########",
                "########", "...##...", "...##...", "...##..."], C["green"])
_icon("voldown", ["........", "........", "........", "########",
                  "########", "........", "........", "........"], C["orange"])
_icon("mute", ["#......#", ".#....#.", "..#..#..", "...##...",
               "...##...", "..#..#..", ".#....#.", "#......#"], C["red"])
_icon("mic", ["...##...", "..####..", "..####..", "..####..",
              "...##...", "...##...", ".######.", "........"], C["green"])
_icon("camera", ["........", ".#....#.", ".######.", ".##..##.",
                 ".#.##.#.", ".##..##.", ".######.", "........"], C["cyan"])
_icon("lock", ["..####..", ".##..##.", ".##..##.", "########",
               "########", "##.##.##", "########", "........"], C["yellow"])
_icon("rocket", ["...##...", "..####..", "..####..", ".######.",
                 ".######.", ".#.##.#.", "#......#", "..#..#.."], C["cyan"])
_icon("light", ["...##...", "#..##..#", ".######.", "..####..",
                ".######.", "#..##..#", "...##...", "........"], C["purple"])
_icon("heart", ["........", ".##..##.", "########", "########",
                ".######.", "..####..", "...##...", "........"], C["red"])
_icon("note", ["....###.", "....#.#.", "....#...", "....#...",
               "....#...", ".####...", ".####...", "..##...."], C["spotify"])
_icon("globe", ["..####..", ".#.##.#.", "#..##..#", "########",
                "#..##..#", "#..##..#", ".#.##.#.", "..####.."], C["cyan"])
_icon("folder", ["........", "###.....", "#######.", "#######.",
                 "#######.", "#######.", "#######.", "........"], C["yellow"])
_icon("gear", ["...##...", ".#.##.#.", "..####..", "###..###",
               "###..###", "..####..", ".#.##.#.", "...##..."], C["yellow"])
_icon("plane", ["......#.", ".....##.", "...####.", ".#####..",
                "...####.", ".....##.", "......#.", "........"], C["blue"])
_icon("apps", ["........", ".##..##.", ".##..##.", "........",
               ".##..##.", ".##..##.", "........", "........"], C["green"])
_icon("calc", [".######.", ".#.##.#.", ".######.", ".#.#.#.#",
               ".######.", ".#.#.#.#", ".######.", "........"], C["purple"])
_icon("headphone", ["..####..", ".#....#.", "##....##", "##....##",
                    "##....##", "##....##", "##....##", "........"], C["purple"])
_icon("clock", ["..####..", ".#.##.#.", "#..#...#", "#..###.#",
                "#......#", "#......#", ".#....#.", "..####.."], C["cyan"])


def get_icon(e):
    t = e.get("type"); p = (e.get("param", "") or "").lower()
    key = None
    if t == "media":
        key = {"playpause": "play", "stop": "stop", "next": "next", "prev": "prev",
               "volup": "volup", "voldown": "voldown", "mute": "mute"}.get(e.get("param", ""))
    elif t == "sysmute":
        key = "mute"
    elif t == "mic":
        key = "mic"
    elif t == "lock":
        key = "lock"
    elif t == "lightshow":
        key = "light"
    elif t == "clock":
        key = "clock"
    elif t == "multi":
        key = "apps"
    elif t == "app":
        key = {"spotify": "note", "browser": "globe", "chrome": "globe",
               "discord": "rocket", "telegram": "plane", "steelseries": "gear"}.get(p, "rocket")
    elif t == "run":
        if "explorer" in p:
            key = "folder"
        elif "calc" in p:
            key = "calc"
        else:
            key = "rocket"
    elif t == "hotkey":
        if "shift+s" in p:
            key = "camera"
        elif p.replace(" ", "") == "win+l":
            key = "lock"
        elif "esc" in p:
            key = "gear"
        elif p.replace(" ", "").endswith("+d"):
            key = "headphone"
    return ICONS.get(key) if key else None


# ---------------- Windows key / media sender ----------------
user32 = ctypes.windll.user32
KEYUP = 0x0002
VK = {"ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B,
      "enter": 0x0D, "space": 0x20, "tab": 0x09, "esc": 0x1B}
MEDIA = {"playpause": 0xB3, "next": 0xB0, "prev": 0xB1,
         "volup": 0xAF, "voldown": 0xAE, "mute": 0xAD, "stop": 0xB2}


def _key(vk, up=False):
    user32.keybd_event(vk, 0, KEYUP if up else 0, 0)


def send_media(name, repeat=1):
    vk = MEDIA.get(name)
    if vk is None:
        return
    for _ in range(repeat):
        _key(vk); _key(vk, True); time.sleep(0.01)


def send_combo(combo):
    keys = combo.lower().replace(" ", "").split("+")
    vks = []
    for k in keys:
        if k in VK:
            vks.append(VK[k])
        elif len(k) == 1:
            vks.append(ord(k.upper()))
    for v in vks:
        _key(v)
    time.sleep(0.02)
    for v in reversed(vks):
        _key(v, True)


def launch_app(name):
    name = name.lower()
    try:
        if name == "spotify":
            try:
                os.startfile("spotify:")
            except OSError:
                p = os.path.expandvars(r"%APPDATA%\Spotify\Spotify.exe")
                if os.path.exists(p):
                    os.startfile(p)
        elif name == "discord":
            upd = os.path.expandvars(r"%LOCALAPPDATA%\Discord\Update.exe")
            if os.path.exists(upd):
                subprocess.Popen([upd, "--processStart", "Discord.exe"])
            else:
                os.startfile("discord:")
        elif name == "browser":
            os.startfile("https://google.com")
        else:
            os.startfile(name)
    except Exception as e:
        print(f"[deck] launch '{name}' failed: {e}", flush=True)


# ---------------- system mic / speaker mute (works everywhere incl. Discord) ----
_AUDIO = {}


def _make_epvol(a, flow, role):   # flow: 0=speaker,1=mic;  role: 0=console,2=communications
    dev = a["enum"].GetDefaultAudioEndpoint(flow, role)
    return a["cast"](dev.Activate(a["iaev"]._iid_, a["ctx"], None), a["ptr"](a["iaev"]))


def init_audio():
    try:
        import comtypes
        try:
            comtypes.CoInitialize()          # may already be initialised (soundcard) -> ignore
        except Exception:
            pass
        from pycaw.pycaw import IMMDeviceEnumerator, IAudioEndpointVolume
        from pycaw.constants import CLSID_MMDeviceEnumerator
        from comtypes import CoCreateInstance, CLSCTX_ALL, cast, POINTER
        _AUDIO.update(enum=CoCreateInstance(CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, CLSCTX_ALL),
                      cast=cast, ptr=POINTER, iaev=IAudioEndpointVolume, ctx=CLSCTX_ALL)
        _AUDIO["mic"] = _make_epvol(_AUDIO, 1, 0)          # cache endpoints (avoid COM churn)
        _AUDIO["spk"] = _make_epvol(_AUDIO, 0, 0)
        try:
            _AUDIO["mic_comm"] = _make_epvol(_AUDIO, 1, 2)
        except Exception:
            _AUDIO["mic_comm"] = None
        log(f"[deck] audio ready (mic muted={is_muted(1)})")
    except Exception as e:
        log(f"[deck] audio init failed: {e}")


def _ep(flow):
    return _AUDIO.get("mic" if flow == 1 else "spk")


def toggle_mute(flow):
    try:
        v = _ep(flow)
        if v is None:
            return
        newmute = 0 if v.GetMute() else 1
        v.SetMute(newmute, None)
        if flow == 1 and _AUDIO.get("mic_comm") is not None:
            try:
                _AUDIO["mic_comm"].SetMute(newmute, None)
            except Exception:
                pass
    except Exception as e:
        log(f"[deck] mute toggle failed: {e}")


def is_muted(flow):
    try:
        v = _ep(flow)
        return bool(v.GetMute()) if v is not None else False
    except Exception:
        return False


def launch_named(name):
    """Best-effort launch of a well-known program by name."""
    n = name.lower().strip()
    desktop = os.path.join(os.path.expanduser("~"), "Desktop")

    def lnk(fname):
        p = os.path.join(desktop, fname)
        return p if os.path.exists(p) else None

    def first_exe(*paths):
        for p in paths:
            if p and os.path.exists(p):
                os.startfile(p); return True
        return False

    def start_menu_lnk(*parts):                          # find a Start-Menu shortcut by name
        roots = [os.path.join(os.environ.get("ProgramData", ""), r"Microsoft\Windows\Start Menu\Programs"),
                 os.path.join(ap, r"Microsoft\Windows\Start Menu\Programs")]
        for root in roots:
            if not os.path.isdir(root):
                continue
            for dp, _dn, fn in os.walk(root):
                for f in fn:
                    lf = f.lower()
                    if lf.endswith(".lnk") and all(p in lf for p in parts):
                        return os.path.join(dp, f)
        return None

    pf = os.environ.get("ProgramFiles", ""); pf86 = os.environ.get("ProgramFiles(x86)", "")
    la = os.environ.get("LOCALAPPDATA", ""); ap = os.environ.get("APPDATA", "")
    try:
        if "magic" in n:
            first_exe(lnk("MAGIC VPN.lnk"))
        elif "steel" in n:
            if not first_exe(os.path.join(pf, "SteelSeries", "GG", "SteelSeriesGG.exe"),
                             os.path.join(pf86, "SteelSeries", "GG", "SteelSeriesGG.exe"),
                             os.path.join(pf, "SteelSeries", "GG", "SteelSeries GG.exe"),
                             os.path.join(pf86, "SteelSeries", "GG", "SteelSeries GG.exe"),
                             lnk("SteelSeries GG.lnk")):
                first_exe(start_menu_lnk("steelseries"))
        elif "spotify" in n:
            try:
                os.startfile("spotify:")
            except Exception:
                first_exe(lnk("Spotify.lnk"), os.path.join(ap, "Spotify", "Spotify.exe"))
        elif "telegram" in n:
            first_exe(lnk("Telegram.lnk"), os.path.join(ap, "Telegram Desktop", "Telegram.exe"))
        elif "chrome" in n:
            if not first_exe(os.path.join(pf, "Google", "Chrome", "Application", "chrome.exe"),
                             os.path.join(pf86, "Google", "Chrome", "Application", "chrome.exe")):
                first_exe(lnk("Google Chrome.lnk"))
        elif "discord" in n:
            launch_app("discord")
        else:
            os.startfile(name)
    except Exception as e:
        print(f"[deck] launch_named '{name}' failed: {e}", flush=True)


def set_app_volume(spec):
    """spec = 'name:action' — action: up / down / mute / set:NN (0-100). Adjusts one app's volume."""
    try:
        from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume
        parts = [x.strip() for x in spec.split(":")]
        name = parts[0].lower()
        action = parts[1].lower() if len(parts) > 1 else "up"
        step = 0.08
        hit = False
        for s in AudioUtilities.GetAllSessions():
            if not s.Process:
                continue
            pname = (s.Process.name() or "").lower()
            if name and name not in pname:
                continue
            hit = True
            vol = s._ctl.QueryInterface(ISimpleAudioVolume)
            if action == "mute":
                vol.SetMute(0 if vol.GetMute() else 1, None)
            elif action == "down":
                vol.SetMasterVolume(max(0.0, vol.GetMasterVolume() - step), None)
            elif action == "set" and len(parts) > 2:
                vol.SetMasterVolume(max(0.0, min(1.0, int(parts[2]) / 100.0)), None)
            else:                                    # up (default)
                vol.SetMasterVolume(min(1.0, vol.GetMasterVolume() + step), None)
        if not hit:
            log(f"[deck] app volume: '{name}' not playing")
    except Exception as e:
        log(f"[deck] app volume '{spec}' failed: {e}")


_OBS = {"cl": None}


def _obs_creds():
    try:
        with open(os.path.join(os.path.dirname(CONFIG) or HERE, "settings.json"), encoding="utf-8") as f:
            s = json.load(f)
        return s.get("obs_host", "localhost"), int(s.get("obs_port", 4455)), s.get("obs_password", "")
    except Exception:
        return "localhost", 4455, ""


def _obs_client():
    if _OBS.get("cl") is not None:
        return _OBS["cl"]
    try:
        import obsws_python as obs
        host, port, pw = _obs_creds()
        _OBS["cl"] = obs.ReqClient(host=host, port=port, password=pw, timeout=3)
        return _OBS["cl"]
    except Exception as e:
        log(f"[deck] OBS connect failed: {e}")
        return None


def obs_action(spec):
    """spec: scene:Name / record / stream / pause / mute:Source / replay / virtualcam."""
    parts = [x.strip() for x in spec.split(":", 1)]
    cmd = parts[0].lower(); arg = parts[1] if len(parts) > 1 else ""
    cl = _obs_client()
    if cl is None:
        return
    try:
        if cmd == "scene" and arg:
            cl.set_current_program_scene(arg)
        elif cmd == "record":
            cl.toggle_record()
        elif cmd == "stream":
            cl.toggle_stream()
        elif cmd == "pause":
            cl.toggle_record_pause()
        elif cmd == "mute" and arg:
            cl.toggle_input_mute(arg)
        elif cmd in ("replay", "save_replay"):
            cl.save_replay_buffer()
        elif cmd == "virtualcam":
            cl.toggle_virtual_cam()
    except Exception as e:
        _OBS["cl"] = None                    # drop stale connection, reconnect next time
        log(f"[deck] OBS action '{spec}' failed: {e}")


def run_action(a):
    t = a.get("type"); p = a.get("param", "")
    try:
        if t == "multi":
            for name in [x.strip() for x in p.replace(",", ";").split(";") if x.strip()]:
                launch_named(name); time.sleep(0.35)
        elif t == "media":
            send_media(p)
        elif t == "hotkey":
            send_combo(p)
        elif t == "app":
            launch_app(p)
        elif t == "mic":
            toggle_mute(1)
        elif t == "sysmute":
            toggle_mute(0)
        elif t == "appvol":
            set_app_volume(p)
        elif t == "obs":
            obs_action(p)
        elif t == "lock":
            ctypes.windll.user32.LockWorkStation()
        elif t == "color":
            return                       # decorative colour pad, no action
        elif t in ("run", "url", "open"):
            os.startfile(p)
        print(f"[deck] ran: {a.get('label', t)}", flush=True)
    except Exception as e:
        print(f"[deck] action error: {e}", flush=True)


# ---------------- default layout ----------------
# each entry: (col, row): {label, color, type, param}
def default_layout():
    L = {}

    def put(col, row, label, color, type, param):
        L[f"{col},{row}"] = {"label": label, "color": color, "type": type, "param": param}

    # --- MEDIA block (top-left) ---
    put(0, 7, "Prev", "blue", "media", "prev")
    put(1, 7, "Play/Pause", "green", "media", "playpause")
    put(2, 7, "Next", "blue", "media", "next")
    put(1, 6, "Stop", "pink", "media", "stop")

    # --- VOLUME block (top-right) ---
    put(4, 7, "Vol -", "orange", "media", "voldown")
    put(5, 7, "Vol +", "orange", "media", "volup")
    put(6, 7, "Sys Mute", "red", "sysmute", "")           # speaker mute (green/red state)

    # --- PROGRAMS block (middle) ---
    put(0, 4, "Spotify", "spotify", "app", "spotify")
    put(1, 4, "Discord", "discord", "app", "discord")
    put(2, 4, "Browser", "cyan", "app", "browser")
    put(3, 4, "Проводник", "blue", "run", r"C:\Windows\explorer.exe")
    put(0, 3, "Калькулятор", "purple", "run", "calc.exe")
    put(1, 3, "Диспетчер", "yellow", "hotkey", "ctrl+shift+esc")

    # --- VOICE / SYSTEM block (bottom) ---
    put(0, 1, "Микро", "green", "mic", "")                # system mic mute (works in Discord!)
    put(1, 1, "DC Deafen", "purple", "hotkey", "ctrl+shift+alt+d")   # set same keybind in Discord
    put(2, 1, "Скриншот", "cyan", "hotkey", "win+shift+s")
    put(3, 1, "Блок ПК", "red", "lock", "")
    put(4, 1, "Свет", "purple", "lightshow", "")     # toggle the light show on the pad
    put(5, 1, "Часы", "cyan", "clock", "")           # toggle a scrolling clock on the pad

    # one button (bottom-right) that launches all main programs at once
    put(7, 0, "Всё сразу", "green", "multi",
        "magic;steelseries;spotify;telegram;chrome;discord")

    return L


def load_layout():
    if os.path.exists(CONFIG):
        try:
            with open(CONFIG, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[deck] bad config, using default: {e}", flush=True)
    lay = default_layout()
    try:
        with open(CONFIG, "w", encoding="utf-8") as f:
            json.dump(lay, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return lay


_LP_PATTERNS = ("LPMiniMK3", "LPProMK3", "LPX", "Launchpad", "Mini MK3", "Mini MK2", "Launchpad X")


def find_port(want_output):
    pm.init()
    idxs = []
    for i in range(pm.get_count()):
        _i, raw, is_in, is_out, _o = pm.get_device_info(i)
        name = raw.decode(errors="replace")
        if not any(p in name for p in _LP_PATTERNS):
            continue
        if want_output and is_out and "MIDIOUT2" not in name:
            return i
        if (not want_output) and is_in:
            idxs.append(i)
    return idxs if not want_output else None


def print_legend(layout):
    print("\n=== DECK LAYOUT (what each pad does) ===")
    for r in range(N - 1, -1, -1):
        line = ""
        for c in range(N):
            e = layout.get(f"{c},{r}")
            line += f"[{e['label'][:9]:^9}]" if e else "[ . . . . ]"
        print(line)
    print("\nActions:")
    for key, e in layout.items():
        print(f"  {e['label']:14} -> {e['type']}:{e['param']}")
    print("=" * 40 + "\n", flush=True)


class LightEngine:
    """Embedded audio-reactive light show (reuses lightshow effects) so the
    deck can toggle it on the pad within the same process (no MIDI conflict)."""
    SR = 48000
    BLOCK = 1024

    def __init__(self, cfg=None):
        spk = sc.default_speaker()
        self.rec_cm = sc.get_microphone(spk.name, include_loopback=True).recorder(
            samplerate=self.SR, channels=2, blocksize=self.BLOCK)
        self.rec = self.rec_cm.__enter__()
        self.cfg = cfg if cfg is not None else {"sens": 1.85, "gain": 1.25, "bright": 1.0}
        self.cfg.setdefault("sens", 1.85); self.cfg.setdefault("gain", 1.25); self.cfg.setdefault("bright", 1.0)
        self.effects = [E() for E in LS.GEN_EFFECTS]     # generative modes only (no clips)
        try:                                             # + user plugins (custom light effects)
            plugdir = os.path.join(os.path.dirname(CONFIG) or HERE, "plugins")
            os.makedirs(plugdir, exist_ok=True)
            for E in LS.load_plugins(plugdir):
                self.effects.append(E())
            log(f"[deck] plugins: {len(self.effects) - len(LS.GEN_EFFECTS)} custom effect(s)")
        except Exception as e:
            log(f"[deck] plugin load failed: {e}")
        self.cur = 0; self.scene_t = time.time()
        self.auto = True; self.palette_shift = 0.0
        self.hud_until = 0.0; self.hud_frac = 0.0; self.hud_color = (1.0, 1.0, 1.0)
        self.mid_max = 1e-6; self.mid_hist = collections.deque(maxlen=43); self.since_snare = 99
        self.nxt = None; self.fade = 0.0
        self.energy_slow = 0.3; self.last_drop = 0.0; self.drop_until = 0.0; self.silent_since = None
        self.drop_effect = LS.DropBurst(); self.idle_effect = LS.IdleAnim()
        self.ripples = []          # pad-press ripples (colour from where you tap)
        self.ctx = LS.Ctx()
        self.flow = 0.0; self.hue_drift = 0.0
        self.running_max = 1e-4; self.bass_max = 1e-6; self.treb_max = 1e-6
        self.band_max = np.full(8, 1e-6); self.bass_hist = collections.deque(maxlen=43)
        self.since_beat = 99; self.bass_kick = 0.0
        self.freqs = np.fft.rfftfreq(self.BLOCK, 1 / self.SR)
        self.bass_bins = np.where((self.freqs >= 30) & (self.freqs <= 160))[0]
        self.mid_bins = np.where((self.freqs > 160) & (self.freqs <= 2000))[0]
        self.treb_bins = np.where((self.freqs > 2000) & (self.freqs <= 16000))[0]
        edges = np.logspace(np.log10(40), np.log10(16000), 9)
        self.band_bins = [np.where((self.freqs >= edges[b]) & (self.freqs < edges[b + 1]))[0] for b in range(8)]
        self.win = np.hanning(self.BLOCK).astype(np.float32)
        self.last = time.time()

    def frame(self):
        try:
            data = self.rec.record(numframes=self.BLOCK)
        except Exception:
            time.sleep(0.02); return None
        mono = data.mean(axis=1)
        if len(mono) < self.BLOCK:
            return None
        spec = np.abs(np.fft.rfft(mono[:self.BLOCK] * self.win)).astype(np.float32)
        rms = float(np.sqrt(np.mean(mono ** 2)))
        self.running_max = max(self.running_max * 0.9995, rms, 1e-4)
        mag = spec + 1e-9
        cen = float((self.freqs * mag).sum() / mag.sum())
        bands = np.array([spec[b].mean() if len(b) else 0 for b in self.band_bins])
        self.band_max = np.maximum(self.band_max * 0.997, bands)
        bands = np.clip(np.sqrt(bands / (self.band_max + 1e-9)), 0, 1)

        e_bass = float(spec[self.bass_bins].sum())
        e_mid = float(spec[self.mid_bins].sum()) if len(self.mid_bins) else 0.0
        e_treb = float(spec[self.treb_bins].sum()) if len(self.treb_bins) else 0.0
        self.bass_max = max(self.bass_max * 0.999, e_bass, 1e-6)
        self.mid_max = max(self.mid_max * 0.999, e_mid, 1e-6)
        self.treb_max = max(self.treb_max * 0.999, e_treb, 1e-6)
        bass_lvl = min(e_bass / self.bass_max, 1.0)
        treb_lvl = min(e_treb / self.treb_max, 1.0)

        self.bass_hist.append(e_bass); self.since_beat += 1
        beat = (len(self.bass_hist) > 10
                and e_bass > float(np.mean(self.bass_hist)) + self.cfg["sens"] * float(np.std(self.bass_hist))
                and self.since_beat > 3)
        if beat:
            self.since_beat = 0
        self.bass_kick = max(self.bass_kick * 0.74, bass_lvl if beat else 0.0)
        self.mid_hist.append(e_mid); self.since_snare += 1
        snare = (len(self.mid_hist) > 10
                 and e_mid > float(np.mean(self.mid_hist)) + (self.cfg["sens"] + 0.1) * float(np.std(self.mid_hist))
                 and self.since_snare > 4)
        if snare:
            self.since_snare = 0

        energy = min(rms / self.running_max, 1.0) ** 0.5
        energy = min(max(energy * self.cfg["gain"], 0.12), 1.0)
        now = time.time(); dt = now - self.last; self.last = now

        self.energy_slow = self.energy_slow * 0.97 + energy * 0.03
        drop = (energy > 0.85 and self.energy_slow < 0.5
                and energy - self.energy_slow > 0.30 and now - self.last_drop > 5.0)
        if drop:
            self.last_drop = now; self.drop_until = now + 1.1
        if rms < 0.0015:
            if self.silent_since is None:
                self.silent_since = now
        else:
            self.silent_since = None
        idle = self.silent_since is not None and now - self.silent_since >= 3.0

        self.flow += dt * (0.4 + energy * 2.2 + self.bass_kick * 2.0)
        self.hue_drift += dt * 0.03
        c = self.ctx
        c.dt = dt; c.flow = self.flow; c.energy = min(1.0, 0.4 * energy + 0.6 * bass_lvl)
        c.bass = bass_lvl; c.mid = float(bands[3]); c.treble = treb_lvl
        c.centroid = min(max((cen - 100) / 4000.0, 0.0), 1.0)
        c.beat = beat; c.kick = beat; c.snare = snare; c.hihat = treb_lvl > 0.5
        c.hot = energy; c.drop = drop; c.bands = bands
        c.hue = (c.centroid * 0.9 + self.hue_drift + self.palette_shift) % 1.0
        c.wave = mono[np.linspace(0, self.BLOCK - 1, 8).astype(int)] / (float(np.max(np.abs(mono))) + 1e-6)

        if self.auto and self.nxt is None and not idle and now - self.scene_t > 9:
            self._switch(random.choice(self._pool())); self.scene_t = now

        if idle:
            fb = self.idle_effect.frame(c).copy() * self.cfg["bright"]
            self.scene_t = now
        elif now < self.drop_until:
            fb = self.drop_effect.frame(c).copy() * self.cfg["bright"]
            self.scene_t = now
        else:
            try:
                fb = self.effects[self.cur].frame(c)
                if self.nxt is not None:
                    fbn = self.effects[self.nxt].frame(c)
                    self.fade += dt * 2.5; m = min(self.fade, 1)
                    fb = fb * (1 - m) + fbn * m
                    if self.fade >= 1:
                        self.cur = self.nxt; self.nxt = None; self.scene_t = now
                fb = fb.copy() * self.cfg["bright"] * (0.30 + 0.30 * energy + 0.8 * self.bass_kick)
            except Exception:
                fb = np.zeros((N, N, 3), np.float32)
                self.nxt = None; self.cur = random.randrange(len(self.effects))

        if not idle and treb_lvl > 0.45:                   # hi-hat sparkles (fewer/softer)
            for _ in range(int((treb_lvl - 0.45) * 18)):
                fb[np.random.randint(0, 8), np.random.randint(0, 8)] = LS.hsv2rgb((c.hue + 0.15) % 1.0, 0.12, 0.9)
        if self.ripples:                                   # ripples from tapping pads
            keep = []
            for rp in self.ripples:
                rp[2] += dt * 10; rp[3] -= dt * 1.8
                if rp[3] > 0:
                    dd = np.hypot(LS.GX - rp[0], LS.GY - rp[1])
                    band = np.exp(-((dd - rp[2]) ** 2) / 0.4) * rp[3]
                    fb += band[:, :, None] * LS.hsv2rgb(rp[4], 1.0, 1.0)
                    keep.append(rp)
            self.ripples = keep
        if now < self.hud_until:                           # settings feedback bar
            fb = fb * 0.2
            n = int(round(self.hud_frac * 8)); hcol = np.array(self.hud_color, np.float32)
            dim = np.array([0.05, 0.05, 0.05], np.float32)
            for cc in range(8):
                fb[0, cc] = hcol if cc < n else dim
        return np.clip(fb, 0, 1)

    # ---- live controls (top row + right column of the pad while in light mode) ----
    def _pool(self): return list(range(len(self.effects)))
    def _switch(self, to):
        if to != self.cur:
            self.nxt = to; self.fade = 0.0
    def _step(self, d):
        pool = self._pool(); base = pool.index(self.cur) if self.cur in pool else 0
        self._switch(pool[(base + d) % len(pool)]); self.auto = False
    def next_scene(self): self._step(1)
    def prev_scene(self): self._step(-1)
    def random_scene(self): self._switch(random.randrange(len(self.effects))); self.auto = False
    def toggle_auto(self): self.auto = not self.auto
    def _hud(self, frac, color):
        self.hud_until = time.time() + 0.8; self.hud_frac = min(max(frac, 0.0), 1.0); self.hud_color = color
    def brighter(self): self.cfg["bright"] = min(1.6, self.cfg["bright"] + 0.15); self._hud(self.cfg["bright"] / 1.6, (1, 1, 1))
    def darker(self): self.cfg["bright"] = max(0.25, self.cfg["bright"] - 0.15); self._hud(self.cfg["bright"] / 1.6, (1, 1, 1))
    def palette(self):
        self.palette_shift = (self.palette_shift + 0.12) % 1.0
        self._hud(1.0, tuple(float(x) for x in LS.hsv2rgb(self.palette_shift, 1.0, 1.0)))
    def sens_down(self): self.cfg["sens"] = max(0.6, self.cfg["sens"] - 0.2); self._hud((self.cfg["sens"] - 0.6) / 3.4, (1, 1, 0))
    def sens_up(self): self.cfg["sens"] = min(4.0, self.cfg["sens"] + 0.2); self._hud((self.cfg["sens"] - 0.6) / 3.4, (1, 1, 0))
    def gain_up(self): self.cfg["gain"] = min(4.0, self.cfg["gain"] + 0.2); self._hud((self.cfg["gain"] - 0.4) / 3.6, (0, 1, 1))
    def gain_down(self): self.cfg["gain"] = max(0.4, self.cfg["gain"] - 0.2); self._hud((self.cfg["gain"] - 0.4) / 3.6, (0, 1, 1))
    def add_ripple(self, c, r): self.ripples.append([float(c), float(r), 0.0, 1.0, random.random()])

    def close(self):
        try:
            self.rec_cm.__exit__(None, None, None)
        except Exception:
            pass


class DeckEngine(threading.Thread):
    """Runs the whole deck (macros + embedded light show) in one thread,
    so the GUI can start/stop it without any sub-processes."""
    def __init__(self):
        super().__init__(daemon=True)
        self._stop = threading.Event()
        self.light_req = threading.Event()
        self.grid = None                 # latest full-grid colours, for the GUI live preview
        self.light_cfg = {"sens": 1.85, "gain": 1.25, "bright": 1.0}   # live-editable from GUI
        self.light_cmds = []             # scene/brightness/palette commands from the GUI
        self.egg_req = threading.Event()  # play the logo easter-egg animation on the pad

    def stop(self):
        self._stop.set()

    def toggle_light(self):
        self.light_req.set()

    def light_cmd(self, name):
        self.light_cmds.append(name)     # e.g. next_scene / prev_scene / palette / brighter …

    def logo_egg(self):
        self.egg_req.set()

    def _set_frame(self, g):
        self.grid = g

    def run(self):
        try:
            deck_loop(self._stop.is_set, self.light_req, self._set_frame, self.light_cfg,
                      self.light_cmds, self.egg_req)
        except Exception:
            import traceback
            log("[deck] ENGINE CRASH:\n" + traceback.format_exc())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    if args.list:
        pm.init()
        for i in range(pm.get_count()):
            _i, raw, is_in, is_out, _o = pm.get_device_info(i)
            print(f"  [{i}] {'IN ' if is_in else 'OUT'} {raw.decode(errors='replace')}")
        return
    deck_loop()


def _logo_path():
    base = getattr(sys, "_MEIPASS", HERE) if getattr(sys, "frozen", False) else HERE
    return os.path.join(base, "deck_icon.png")


_LOGO = [None]


def _load_logo_grid():
    if _LOGO[0] is not None:
        return _LOGO[0]
    try:
        from PIL import Image
        im = Image.open(_logo_path()).convert("RGB").resize((N, N), Image.LANCZOS)
        _LOGO[0] = [[im.getpixel((c, N - 1 - r)) for c in range(N)] for r in range(N)]  # r=0 bottom
    except Exception:
        _LOGO[0] = False
    return _LOGO[0]


def play_logo_egg(out):
    """Easter egg: the app logo fades in on the pad, holds, then fades out."""
    logo = _load_logo_grid()
    if not logo:
        return
    seq = [i / 9 for i in range(1, 10)] + [1.0] * 22 + [(11 - i) / 11 for i in range(1, 12)]
    for b in seq:
        col = {}
        for r in range(N):
            for c in range(N):
                R, G, B = logo[r][c]
                col[pad_index(c, r)] = (int(R * 0.5 * b), int(G * 0.5 * b), int(B * 0.5 * b))
        out.write_sys_ex(0, rgb_sysex(col))
        time.sleep(0.03)
    out.write_sys_ex(0, rgb_sysex({pad_index(c, r): (0, 0, 0) for r in range(N) for c in range(N)}))


def deck_loop(should_stop=lambda: False, light_request=None, set_frame=None, light_cfg=None,
              light_cmds=None, egg_req=None):
    found = winmidi.find_output()                   # detect any supported Launchpad
    if found is None:
        log("[X] Launchpad not found."); return
    dev_id, dev_hdr, dev_name = found
    HDR[4] = dev_hdr; LS.HDR[4] = dev_hdr            # adapt SysEx to the detected model
    prog = [0xF0] + HDR + [0x0E, 0x01, 0xF7]
    live = [0xF0] + HDR + [0x0E, 0x00, 0xF7]
    log(f"[deck] Launchpad: {dev_name} (hdr={dev_hdr:#04x})")

    in_idxs = find_port(False)                      # pygame for INPUT (calls pm.init)
    out = winmidi.WinMidiOut(dev_id)                # winmm for OUTPUT (crash-free SysEx)
    inps = [pm.Input(i) for i in in_idxs]
    out.write_sys_ex(0, prog)
    time.sleep(0.2)
    init_audio()

    GRID_NOTES = {pad_index(c, r) for r in range(N) for c in range(N)}

    def build(layout):
        by_note = {}; base = {}
        for key, e in layout.items():
            if key.startswith("o"):                       # Launchpad Pro outer button: "o<index>"
                try:
                    note = int(key[1:])
                except ValueError:
                    continue
            else:
                c, r = (int(x) for x in key.split(","))
                note = pad_index(c, r)
            by_note[note] = e
            base[note] = C.get(e["color"], (60, 60, 60))
        return by_note, base

    # startup animation (~5s): rainbow bloom + orbiting sparks + converging flash
    STEPS = 165
    for step in range(STEPS):
        if should_stop():
            break
        t = step / (STEPS - 1)                 # 0..1 progress
        tt = t * 5.0                           # ~seconds
        col = {}
        # 3 orbiting sparks
        orbit = []
        for k in range(3):
            oa = tt * 1.7 + k * 2.0944
            orad = 2.6 + 0.6 * math.sin(tt * 1.2 + k)
            orbit.append((3.5 + math.cos(oa) * orad, 3.5 + math.sin(oa) * orad))
        for r in range(N):
            for c in range(N):
                dx = c - 3.5; dy = r - 3.5
                dist = math.hypot(dx, dy); ang = math.atan2(dy, dx)
                front = (t * 2.0 % 1.0) * 6.5              # expanding ring, repeats
                ring = max(0.0, 1 - abs(dist - front) * 0.7)
                swirl = 0.5 + 0.5 * math.sin(ang * 3 + tt * 4 - dist * 1.2)
                v = ring * (0.5 + 0.5 * swirl)
                for ox, oy in orbit:                       # bright orbiting sparks
                    v = max(v, max(0.0, 1 - math.hypot(c - ox, r - oy) * 0.85))
                if t > 0.82:                               # converge into a centre burst
                    f = (t - 0.82) / 0.18
                    v = max(v * (1 - f), (1 - dist / 5.0) * f * 1.35)
                if t > 0.95:                               # fade out
                    v *= max(0.0, 1 - (t - 0.95) / 0.05)
                hue = (dist * 0.09 + ang / (2 * math.pi) + tt * 0.15) % 1.0
                rgb = LS.hsv2rgb(hue, 1.0, min(1.0, v))
                col[pad_index(c, r)] = (int(rgb[0] * 127), int(rgb[1] * 127), int(rgb[2] * 127))
        out.write_sys_ex(0, rgb_sysex(col))
        time.sleep(0.03)
    out.write_sys_ex(0, rgb_sysex({pad_index(c, r): (0, 0, 0) for r in range(N) for c in range(N)}))

    layout = load_layout()
    by_note, base = build(layout)
    cfg_mtime = os.path.getmtime(CONFIG) if os.path.exists(CONFIG) else 0.0
    log(f"MIDI out [winmm {dev_id}] in {in_idxs} — deck running.")
    print_legend(layout)

    flash = {}
    last_check = 0.0; last_state = 0.0
    mic_muted = is_muted(1); spk_muted = is_muted(0)
    last_sent = {}
    out.write_sys_ex(0, rgb_sysex({pad_index(c, r): (0, 0, 0) for r in range(N) for c in range(N)}))

    anim = None                 # press burst: {"col","row","color","t0"}
    ANIM_DUR = 0.5
    light_mode = False          # embedded light show toggled by a 'lightshow' pad
    light_engine = None
    clock_mode = False          # scrolling clock toggled by a 'clock' pad
    clock_view = ClockView()

    def pad_color(c, r, now):
        note = pad_index(c, r)
        e = by_note.get(note)
        if e is None:
            return SECTION_BG.get(note, (0, 0, 0))          # dim section background
        if note in flash and now < flash[note]:
            return C["white"]
        t = e.get("type")
        muted = mic_muted if t == "mic" else spk_muted if t == "sysmute" else None
        if muted is not None:
            if muted:
                v = 0.45 + 0.55 * abs(math.sin(now * 6))     # red blink = muted
                return (int(127 * v), 0, 0)
            return (0, 110, 25)                              # green = live/on
        rr, gg, bb = base.get(note, (60, 60, 60))
        p = 0.6 + 0.4 * (0.5 + 0.5 * math.sin(now * 2.2 + (note % 10) * 0.5))  # breathe
        return (int(rr * p), int(gg * p), int(bb * p))

    def note_color(note, now):                            # colour for a Pro outer button (by index)
        e = by_note.get(note)
        if e is None:
            return (0, 0, 0)
        if note in flash and now < flash[note]:
            return C["white"]
        t = e.get("type")
        muted = mic_muted if t == "mic" else spk_muted if t == "sysmute" else None
        if muted is not None:
            if muted:
                v = 0.45 + 0.55 * abs(math.sin(now * 6))
                return (int(127 * v), 0, 0)
            return (0, 110, 25)
        rr, gg, bb = base.get(note, (60, 60, 60))
        p = 0.6 + 0.4 * (0.5 + 0.5 * math.sin(now * 2.2 + (note % 10) * 0.5))
        return (int(rr * p), int(gg * p), int(bb * p))

    def anim_frame(a, now):
        f = (now - a["t0"]) / ANIM_DUR
        frame = {pad_index(c, r): (0, 0, 0) for r in range(N) for c in range(N)}
        if a.get("icon"):
            b = 0.5 + 0.5 * math.cos(f * math.pi)          # smooth pop then fade
            col = a["color"]
            for (c, r) in a["cells"]:
                frame[pad_index(c, r)] = (int(col[0] * b), int(col[1] * b), int(col[2] * b))
        else:
            style = a.get("style", "rings")
            oc, orow = a["col"], a["row"]
            front = f * 8.5
            for r in range(N):
                for c in range(N):
                    d = math.hypot(c - oc, r - orow)
                    ang = math.atan2(r - orow, c - oc)
                    if style == "spiral":
                        v = max(0.0, 1 - f) * (0.5 + 0.5 * math.sin(ang * 3 + d * 1.5 - f * 10))
                        h = (ang / (2 * math.pi) + f) % 1.0
                    elif style == "spark":
                        v = max(0.0, 1 - f) if (abs(d - front) < 0.8 and int((ang + math.pi) * 4 / math.pi) % 2 == 0) else 0.0
                        h = (d * 0.1 + f) % 1.0
                    else:  # rings
                        ring = math.exp(-((d - front) ** 2) / 1.4) * (1 - f)
                        v = min(1.0, ring + max(0.0, 0.45 * (1 - f * 3)))
                        h = (d * 0.11 + f * 0.6) % 1.0
                    if style == "rings" and d < 0.6:
                        frame[pad_index(c, r)] = C["white"]
                    else:
                        rgb = LS.hsv2rgb(h, 1.0, max(0.0, min(1.0, v)))
                        frame[pad_index(c, r)] = (int(rgb[0] * 127), int(rgb[1] * 127), int(rgb[2] * 127))
        return frame

    def toggle_light():
        nonlocal light_mode, light_engine
        if light_mode:
            if light_engine:
                light_engine.close(); light_engine = None
            light_mode = False; last_sent.clear()
            out.write_sys_ex(0, rgb_sysex({pad_index(c, r): (0, 0, 0)
                                           for r in range(N) for c in range(N)}))
            print("[deck] light OFF", flush=True)
        else:
            try:
                light_engine = LightEngine(light_cfg); light_mode = True; last_sent.clear()
                print("[deck] light ON", flush=True)
            except Exception as ex:
                print(f"[deck] light start failed: {ex}", flush=True)

    def toggle_clock():
        nonlocal clock_mode
        clock_mode = not clock_mode
        last_sent.clear()
        if not clock_mode:
            out.write_sys_ex(0, rgb_sysex({pad_index(c, r): (0, 0, 0)
                                           for r in range(N) for c in range(N)}))
        print(f"[deck] clock {'ON' if clock_mode else 'OFF'}", flush=True)

    try:
        while not should_stop():
            now = time.time()
            if egg_req is not None and egg_req.is_set():
                egg_req.clear()
                play_logo_egg(out)
                last_sent.clear()                        # force a full repaint afterwards
            if light_request is not None and light_request.is_set():
                light_request.clear()
                toggle_light()
            if not light_mode and not clock_mode:
                if now - last_check > 0.4:
                    last_check = now
                    try:
                        m = os.path.getmtime(CONFIG)
                        if m != cfg_mtime:
                            cfg_mtime = m
                            layout = load_layout(); by_note, base = build(layout)
                            flash.clear(); last_sent.clear(); anim = None
                            out.write_sys_ex(0, rgb_sysex({pad_index(c, r): (0, 0, 0)
                                                           for r in range(N) for c in range(N)}))
                            print("[deck] layout reloaded", flush=True)
                    except Exception:
                        pass
                if now - last_state > 0.35:
                    last_state = now
                    mic_muted = is_muted(1); spk_muted = is_muted(0)
            for inp in inps:
                while inp.poll():
                    for ev in inp.read(32):
                        (status, d1, d2, _), _t = ev[0], ev[1]
                        typ = status & 0xF0
                        if d2 <= 0:
                            continue
                        if typ == 0x90:
                            e = by_note.get(d1)
                            et = e.get("type") if e else None
                            if et == "lightshow":
                                toggle_light(); continue
                            if et == "clock":
                                toggle_clock(); continue
                            if light_mode and light_engine:      # grid press -> ripple from that pad
                                r, c = d1 // 10 - 1, d1 % 10 - 1
                                if 0 <= r < N and 0 <= c < N:
                                    light_engine.add_ripple(c, r)
                                continue
                            if clock_mode or e is None or et == "color":
                                continue                         # asleep / empty / colour-only pad
                            flash[d1] = now + 0.12
                            if d1 in GRID_NOTES:             # press burst only on the 8x8 grid
                                ic = get_icon(e)
                                if ic:
                                    anim = {"icon": True, "cells": ic[0], "color": ic[1], "t0": now}
                                else:
                                    anim = {"icon": False, "col": d1 % 10 - 1, "row": d1 // 10 - 1,
                                            "color": base.get(d1, (90, 90, 90)), "t0": now,
                                            "style": random.choice(("rings", "spiral", "spark"))}
                            run_action(e)
                            mic_muted = is_muted(1); spk_muted = is_muted(0)
                        elif typ == 0xB0:
                            e = by_note.get(d1)
                            # Launchpad Pro outer button assigned as a macro (sent as CC)
                            if e is not None and d1 not in TOP_ROW and d1 not in RIGHT_COL:
                                et = e.get("type")
                                flash[d1] = now + 0.12
                                if et == "lightshow":
                                    toggle_light()
                                elif et == "clock":
                                    toggle_clock()
                                elif et != "color":
                                    run_action(e)
                                    mic_muted = is_muted(1); spk_muted = is_muted(0)
                            elif light_mode and light_engine:
                                le = light_engine
                                if d1 in TOP_ROW:
                                    ctl = {0: le.toggle_auto, 1: le.prev_scene, 2: le.next_scene,
                                           3: le.palette, 4: le.darker, 5: le.brighter,
                                           7: le.random_scene}.get(d1 - 91)
                                    if ctl:
                                        ctl()
                                elif d1 in RIGHT_COL:
                                    ri = RIGHT_COL.index(d1)      # 0 bottom .. 7 top
                                    ctl = {7: le.sens_down, 6: le.sens_up,
                                           5: le.gain_up, 4: le.gain_down}.get(ri)
                                    if ctl:
                                        ctl()
            # ---- light show mode ----
            if light_mode and light_engine:
                if light_cmds:                       # apply scene/palette/brightness commands from GUI
                    while light_cmds:
                        nm = light_cmds.pop(0)
                        fn = getattr(light_engine, nm, None)
                        if fn:
                            try:
                                fn()
                            except Exception:
                                pass
                try:
                    fb = light_engine.frame()
                    if fb is not None:
                        out.write_sys_ex(0, LS.rgb_sysex(fb))
                        if set_frame:
                            set_frame({pad_index(c, r): (int(fb[r, c, 0] * 127), int(fb[r, c, 1] * 127),
                                                         int(fb[r, c, 2] * 127)) for r in range(N) for c in range(N)})
                except Exception as ex:
                    print(f"[deck] light frame error: {ex}", flush=True)
                continue
            # ---- clock mode ----
            if clock_mode:
                frame = {pad_index(c, r): (0, 0, 0) for r in range(N) for c in range(N)}
                frame.update(clock_view.frame())
                changed = {n: col for n, col in frame.items() if last_sent.get(n) != col}
                if changed:
                    out.write_sys_ex(0, rgb_sysex(changed)); last_sent.update(changed)
                if set_frame:
                    set_frame(frame)
                time.sleep(1 / 20)
                continue
            # ---- deck render ----
            if anim and now - anim["t0"] < ANIM_DUR:
                frame = anim_frame(anim, now)
            else:
                if anim:
                    anim = None; last_sent.clear()          # burst ended -> repaint
                frame = {pad_index(c, r): pad_color(c, r, now) for r in range(N) for c in range(N)}
            for note in by_note:                            # Launchpad Pro outer buttons
                if note not in GRID_NOTES:
                    frame[note] = note_color(note, now)
            changed = {n: col for n, col in frame.items() if last_sent.get(n) != col}
            if changed:
                out.write_sys_ex(0, rgb_sysex(changed))
                last_sent.update(changed)
            if set_frame:
                set_frame(frame)
            time.sleep(1 / 30)
    except KeyboardInterrupt:
        pass
    finally:
        if light_engine:
            light_engine.close()
        # shutdown animation (~1.4s): swirl implodes to the centre, white flash, fade
        try:
            S2 = 45
            for step in range(S2):
                t = step / (S2 - 1)
                r_in = (1 - t) * 6.5                       # ring collapsing inward
                col = {}
                for r in range(N):
                    for c in range(N):
                        dx = c - 3.5; dy = r - 3.5
                        d = math.hypot(dx, dy); ang = math.atan2(dy, dx)
                        ring = max(0.0, 1 - abs(d - r_in) * 0.8)
                        swirl = 0.5 + 0.5 * math.sin(ang * 3 - t * 12 + d)
                        v = ring * (0.5 + 0.5 * swirl) * (1 - t * 0.55)
                        if t > 0.7:                        # centre flash then fade
                            fl = (t - 0.7) / 0.3
                            v = max(v, (1 - d / 5.0) * (1 - fl) * 1.1)
                        rgb = LS.hsv2rgb((d * 0.1 - t) % 1.0, 0.9, min(1.0, v))
                        col[pad_index(c, r)] = (int(rgb[0] * 127), int(rgb[1] * 127), int(rgb[2] * 127))
                out.write_sys_ex(0, rgb_sysex(col))
                time.sleep(0.03)
        except Exception:
            pass
        try:
            out.write_sys_ex(0, rgb_sysex({pad_index(c, r): (0, 0, 0)
                                           for r in range(N) for c in range(N)}))
            out.write_sys_ex(0, live)
            time.sleep(0.1)
            out.close()
            for inp in inps:
                inp.close()
            pm.quit()               # needed so the next start can re-open the device
        except Exception:
            pass
        log("[deck] stopped.")


if __name__ == "__main__":
    main()
