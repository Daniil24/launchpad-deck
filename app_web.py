"""
Launchpad Deck — web UI (pywebview + Edge WebView2) with the same Python engine.
The engine (audio / MIDI / light show) is untouched; only the UI is HTML/CSS/JS,
so rendering is GPU-composited: crisp, anti-aliased, smooth, no smearing.
"""
import os
import sys
import json
import time
import threading
import subprocess

import webview

import deck as D
import i18n

try:
    import ctypes
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("magicpad.launchpaddeck")
except Exception:
    pass

# ---------------------------------------------------------------- paths
if getattr(sys, "frozen", False):
    HERE = os.path.dirname(sys.executable)
    RES = sys._MEIPASS                     # bundled assets (web/, deck_icon.png/.ico) live here
    WEB = os.path.join(sys._MEIPASS, "web")
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
    RES = HERE
    WEB = os.path.join(HERE, "web")
PYW = os.path.join(HERE, ".venv", "Scripts", "pythonw.exe")
CONFIG = D.CONFIG
CFG_DIR = os.path.dirname(CONFIG) or HERE
SETTINGS = os.path.join(CFG_DIR, "settings.json")
PROF_DIR = os.path.join(CFG_DIR, "profiles")
PLUGINS_DIR = os.path.join(CFG_DIR, "plugins")
STARTUP = os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs\Startup")
AUTO_LNK = os.path.join(STARTUP, "Launchpad Deck.lnk")
CREATE_NO_WINDOW = 0x08000000
N = 8

VERSION = "1.5"
GITHUB_REPO = "Daniil24/launchpad-deck"
TON_ADDRESS = "UQAK1sIJqPVn9ND8JTOEUlrBFyAiVU0j6IiiXczTM7YmX4CB"
TON_LINK = "https://app.tonkeeper.com/transfer/" + TON_ADDRESS
_update = [None]   # {version, url} if a newer release exists

# ready-made layout profiles (seeded on first run; not overwritten)
LP_PRESETS = {
    "Работа": {
        "0,7": {"label": "Prev", "color": "blue", "type": "media", "param": "prev"},
        "1,7": {"label": "Play/Pause", "color": "green", "type": "media", "param": "playpause"},
        "2,7": {"label": "Next", "color": "blue", "type": "media", "param": "next"},
        "4,7": {"label": "Vol -", "color": "orange", "type": "media", "param": "voldown"},
        "5,7": {"label": "Vol +", "color": "orange", "type": "media", "param": "volup"},
        "7,7": {"label": "Mute", "color": "red", "type": "media", "param": "mute"},
        "0,5": {"label": "Chrome", "color": "orange", "type": "app", "param": "chrome"},
        "1,5": {"label": "Telegram", "color": "cyan", "type": "app", "param": "telegram"},
        "2,5": {"label": "Spotify", "color": "spotify", "type": "app", "param": "spotify"},
        "3,5": {"label": "Discord", "color": "discord", "type": "app", "param": "discord"},
        "0,4": {"label": "Рабочий набор", "color": "purple", "type": "multi", "param": "chrome;telegram;spotify"},
        "0,0": {"label": "Часы", "color": "cyan", "type": "clock", "param": ""},
        "1,0": {"label": "Скриншот", "color": "pink", "type": "hotkey", "param": "win+shift+s"},
        "2,0": {"label": "Блок ПК", "color": "yellow", "type": "lock", "param": ""},
        "7,0": {"label": "Свет", "color": "purple", "type": "lightshow", "param": ""},
    },
    "Игры": {
        "0,7": {"label": "Prev", "color": "blue", "type": "media", "param": "prev"},
        "1,7": {"label": "Play/Pause", "color": "green", "type": "media", "param": "playpause"},
        "2,7": {"label": "Next", "color": "blue", "type": "media", "param": "next"},
        "4,7": {"label": "Vol -", "color": "orange", "type": "media", "param": "voldown"},
        "5,7": {"label": "Vol +", "color": "orange", "type": "media", "param": "volup"},
        "0,5": {"label": "Discord", "color": "discord", "type": "app", "param": "discord"},
        "1,5": {"label": "Spotify", "color": "spotify", "type": "app", "param": "spotify"},
        "2,5": {"label": "SteelSeries", "color": "orange", "type": "app", "param": "steelseries"},
        "3,5": {"label": "Мут Discord", "color": "red", "type": "appvol", "param": "discord:mute"},
        "0,0": {"label": "Мут микро", "color": "red", "type": "sysmute", "param": ""},
        "1,0": {"label": "Скриншот", "color": "pink", "type": "hotkey", "param": "win+shift+s"},
        "2,0": {"label": "Блок ПК", "color": "yellow", "type": "lock", "param": ""},
        "7,0": {"label": "Свет", "color": "purple", "type": "lightshow", "param": ""},
    },
    "Стрим (OBS)": {
        "0,7": {"label": "Сцена 1", "color": "purple", "type": "obs", "param": "scene:1"},
        "1,7": {"label": "Сцена 2", "color": "purple", "type": "obs", "param": "scene:2"},
        "2,7": {"label": "Сцена 3", "color": "purple", "type": "obs", "param": "scene:3"},
        "3,7": {"label": "Запись", "color": "red", "type": "obs", "param": "record"},
        "4,7": {"label": "Эфир", "color": "red", "type": "obs", "param": "stream"},
        "5,7": {"label": "Повтор", "color": "orange", "type": "obs", "param": "replay"},
        "6,7": {"label": "Вирт-камера", "color": "cyan", "type": "obs", "param": "virtualcam"},
        "0,5": {"label": "Мут микро", "color": "red", "type": "obs", "param": "mute:Mic/Aux"},
        "1,5": {"label": "Мут звука", "color": "red", "type": "obs", "param": "mute:Desktop Audio"},
        "2,5": {"label": "Мут звука ПК", "color": "orange", "type": "sysmute", "param": ""},
        "0,0": {"label": "Свет", "color": "purple", "type": "lightshow", "param": ""},
        "1,0": {"label": "Часы", "color": "cyan", "type": "clock", "param": ""},
    },
}

# previous shipped preset bodies — used to auto-upgrade a seeded profile ONLY if the
# user hasn't modified it (byte-identical to what we shipped), so their edits are never lost.
LP_PRESETS_OLD = {
    "Стрим (OBS)": {
        "0,7": {"label": "Сцена 1", "color": "purple", "type": "obs", "param": "scene:Scene 1"},
        "1,7": {"label": "Сцена 2", "color": "purple", "type": "obs", "param": "scene:Scene 2"},
        "3,7": {"label": "Запись", "color": "red", "type": "obs", "param": "record"},
        "4,7": {"label": "Эфир", "color": "red", "type": "obs", "param": "stream"},
        "5,7": {"label": "Повтор", "color": "orange", "type": "obs", "param": "replay"},
        "6,7": {"label": "Вирт-камера", "color": "cyan", "type": "obs", "param": "virtualcam"},
        "0,5": {"label": "Мут источника", "color": "red", "type": "obs", "param": "mute:Mic/Aux"},
        "1,5": {"label": "Мут микро", "color": "red", "type": "sysmute", "param": ""},
        "2,5": {"label": "Пауза", "color": "yellow", "type": "obs", "param": "pause"},
        "0,0": {"label": "Свет", "color": "purple", "type": "lightshow", "param": ""},
        "1,0": {"label": "Часы", "color": "cyan", "type": "clock", "param": ""},
    },
}

# ---------------------------------------------------------------- metadata (reused from the engine)
COLOR_NAMES = [n for n in D.C if n not in ("off", "white")]
C_HEX = {n: "#%02x%02x%02x" % (min(255, r * 2), min(255, g * 2), min(255, b * 2))
         for n, (r, g, b) in D.C.items()}
TYPES = ["media", "appvol", "obs", "mic", "sysmute", "lightshow", "clock", "lock",
         "multi", "app", "hotkey", "run", "url", "color"]
NO_PARAM = ("mic", "sysmute", "lightshow", "clock", "lock", "color")
SUGGEST = {
    "media": ["playpause", "next", "prev", "volup", "voldown", "mute", "stop"],
    "appvol": ["spotify:up", "spotify:down", "chrome:up", "chrome:down", "discord:mute", "spotify:set:50"],
    "obs": ["scene:Scene 1", "record", "stream", "pause", "mute:Mic/Aux", "replay", "virtualcam"],
    "multi": ["magic;steelseries;spotify;telegram;chrome;discord"],
    "app": ["spotify", "discord", "browser", "telegram", "chrome", "steelseries"],
    "hotkey": ["ctrl+shift+alt+m", "ctrl+shift+alt+d", "ctrl+shift+esc", "win+shift+s"],
    "run": [r"C:\Windows\System32\notepad.exe"],
    "url": ["https://google.com", "https://youtube.com"],
}
TOP_CTL = ["auto", "prev", "next", "palette", "darker", "brighter", None, "random"]
RIGHT_CTL = ["sens_down", "sens_up", "gain_up", "gain_down", None, None, None, None]
CTL_LBL = {"auto": "AUTO", "prev": "◀︎", "next": "▶︎", "palette": "PAL", "darker": "B−",
           "brighter": "B+", "random": "RND", "sens_down": "S−", "sens_up": "S+",
           "gain_up": "G+", "gain_down": "G−"}
CTL_CMD = {"auto": "toggle_auto", "prev": "prev_scene", "next": "next_scene", "palette": "palette",
           "darker": "darker", "brighter": "brighter", "random": "random_scene",
           "sens_down": "sens_down", "sens_up": "sens_up", "gain_up": "gain_up", "gain_down": "gain_down"}

TUT_KINDS = [("👋", "hello", "1"), ("▶", "start", "2"), ("🎛", "editor", "3"), ("🎨", "colors", "4"),
             ("🗂", "types", "TYPES"), ("⌨", "params", "PARAMS"), ("🎥", "obs", "OBS"),
             ("🚀", "multi", "5"), ("🎆", "light", "6"), ("🎚", "sliders", "7"), ("🎛", "controls", "C"),
             ("👁", "preview", "8"), ("🗂", "profiles", "P"), ("🧩", "plugins", "PLUG"),
             ("💾", "io", "9"), ("🌍", "lang", "LANG"), ("✅", "done", "10")]


# ---------------------------------------------------------------- settings / config
def detect_device():
    """Which Launchpad is connected: 'pro' (10x10), 'x' or 'mini' (8x8 + top/right controls)."""
    try:
        import winmidi
        f = winmidi.find_output()
        if f:
            return {0x0E: "pro", 0x0D: "mini", 0x0C: "x"}.get(f[1], "mini")
    except Exception:
        pass
    return "mini"


def load_settings():
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(d):
    try:
        with open(SETTINGS, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


i18n.set_current(load_settings().get("lang", "ru"))

layout = D.load_layout()
_s = load_settings()
light_ui = {"sens": float(_s.get("sens", 1.85)), "gain": float(_s.get("gain", 1.25)),
            "bright": float(_s.get("bright", 1.0)),
            "bass": float(_s.get("bass", 1.0)), "treble": float(_s.get("treble", 1.0))}
engine = None
want_running = False
last_attempt = 0.0
MAIN = [None]        # the normal app window (with OS min/max/close)
SPL = [None]         # the frameless splash/closing window
_mode = ["in"]       # 'in' = startup bloom, 'out' = closing bloom
_want_close = [False]
_splash_done = [False]
TRAY = [None]          # pystray icon (minimize-to-tray)
_want_show = [False]   # tray -> "show window" request (applied from JS/API context)
_min_notified = [False]


def save_config():
    try:
        with open(CONFIG, "w", encoding="utf-8") as f:
            json.dump(layout, f, ensure_ascii=False, indent=2)
        save_profile(active_profile())
    except Exception as e:
        print("save error", e)


def is_running():
    return engine is not None and engine.is_alive()


def _spawn():
    global engine
    engine = D.DeckEngine()
    engine.light_cfg.update(light_ui)
    engine.start()


def _stop_engine():
    global engine
    if engine is not None:
        engine.stop(); engine.join(timeout=4); engine = None


# ---------------------------------------------------------------- profiles
def _prof_path(name):
    return os.path.join(PROF_DIR, name + ".json")


def list_profiles():
    try:
        return sorted(f[:-5] for f in os.listdir(PROF_DIR) if f.endswith(".json"))
    except Exception:
        return []


def active_profile():
    return load_settings().get("profile", "default")


def save_profile(name):
    try:
        os.makedirs(PROF_DIR, exist_ok=True)
        with open(_prof_path(name), "w", encoding="utf-8") as f:
            json.dump(layout, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("save profile", e)


def ensure_profiles():
    global layout
    os.makedirs(PROF_DIR, exist_ok=True)
    if not list_profiles():
        save_profile(active_profile())
    # seed ready-made profiles (Work / Games / OBS) — don't overwrite user edits
    for name, lay in LP_PRESETS.items():
        p = _prof_path(name)
        write = False
        if not os.path.exists(p):
            write = True
        elif name in LP_PRESETS_OLD:              # auto-upgrade ONLY if still the untouched old preset
            try:
                with open(p, encoding="utf-8") as f:
                    cur = json.load(f)
                if cur == LP_PRESETS_OLD[name]:
                    write = True
            except Exception:
                pass
        if write:
            try:
                with open(p, "w", encoding="utf-8") as f:
                    json.dump(lay, f, ensure_ascii=False, indent=2)
            except Exception:
                pass
    # seamless live-layout upgrade: if the active profile is a preset whose live
    # layout is still the untouched old version, refresh it to the new preset
    act = active_profile()
    if act in LP_PRESETS and act in LP_PRESETS_OLD and layout == LP_PRESETS_OLD[act]:
        layout = json.loads(json.dumps(LP_PRESETS[act]))
        save_config()


def _newer(a, b):
    def parse(s):
        out = []
        for part in str(s).split("."):
            num = ""
            for ch in part:
                if ch.isdigit():
                    num += ch
                else:
                    break
            out.append(int(num) if num else 0)
        return out
    pa, pb = parse(a), parse(b)
    n = max(len(pa), len(pb)); pa += [0] * (n - len(pa)); pb += [0] * (n - len(pb))
    return pa > pb


def _check_update():
    try:
        import urllib.request
        url = "https://api.github.com/repos/%s/releases/latest" % GITHUB_REPO
        req = urllib.request.Request(url, headers={"User-Agent": "LaunchpadDeck"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8"))
        tag = (data.get("tag_name") or "").lstrip("vV")
        if tag and _newer(tag, VERSION):
            _update[0] = {"version": tag,
                          "url": data.get("html_url") or "https://github.com/%s/releases/latest" % GITHUB_REPO}
    except Exception:
        pass


# ---------------------------------------------------------------- plugins
_SAMPLE_PLUGIN = '''# Launchpad Deck effect plugin example.
# Available without imports: Effect, hsv2rgb, np, N, GX, GY, CX, CY, DIST.
# ctx: flow, energy, bass, mid, treble, beat, hue, kick, snare, hihat, drop, bands, wave


class MyWave(Effect):
    name = "my wave"

    def frame(self, ctx):
        t = ctx.flow
        p = (np.sin(GX * 0.7 + t * 2) + np.sin(GY * 0.5 - t)) * 0.5
        val = (0.2 + 0.8 * ctx.energy) * (0.4 + 0.6 * (p + 1) / 2)
        return hsv2rgb(ctx.hue + p * 0.15, 0.9, val)
'''


def ensure_plugins():
    try:
        os.makedirs(PLUGINS_DIR, exist_ok=True)
        s = os.path.join(PLUGINS_DIR, "example_effect.py")
        if not os.path.exists(s):
            open(s, "w", encoding="utf-8").write(_SAMPLE_PLUGIN)
    except Exception:
        pass


# ---------------------------------------------------------------- autostart
def set_autostart(enable):
    if enable:
        target = sys.executable if getattr(sys, "frozen", False) else PYW
        args = "" if getattr(sys, "frozen", False) else f'"{os.path.join(HERE, "app_web.py")}"'
        workdir = HERE
        ps = ('$ws=New-Object -ComObject WScript.Shell; '
              f'$s=$ws.CreateShortcut("{AUTO_LNK}"); $s.TargetPath="{target}"; '
              f'$s.Arguments=' + f"'{args}'" + f'; $s.WorkingDirectory="{workdir}"; '
              f'$s.IconLocation="{target}"; $s.Save()')
        try:
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], creationflags=CREATE_NO_WINDOW,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        try:                                     # remember which exe/version autostart now points to
            save_settings({**load_settings(), "autostart_version": VERSION, "autostart_exe": target})
        except Exception:
            pass
    else:
        try:
            os.remove(AUTO_LNK)
        except Exception:
            pass


def _read_shortcut_target(path):
    try:
        ps = f'(New-Object -ComObject WScript.Shell).CreateShortcut("{path}").TargetPath'
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], creationflags=CREATE_NO_WINDOW,
                           capture_output=True, text=True, timeout=8)
        return (r.stdout or "").strip()
    except Exception:
        return ""


def refresh_autostart():
    """Self-heal autostart: if it points to a different exe and THIS build is newer
    than the one autostart last recorded (or the old exe is gone), re-point it to the
    exe the user is actually running now. Never downgrades."""
    if not getattr(sys, "frozen", False) or not os.path.exists(AUTO_LNK):
        return
    cur = os.path.normcase(os.path.abspath(sys.executable))
    tgt = os.path.normcase(os.path.abspath(_read_shortcut_target(AUTO_LNK) or "x"))
    if tgt == cur:
        return
    stored = load_settings().get("autostart_version", "0")
    # re-point to the exe the user is running now, unless it is strictly OLDER than
    # the version autostart last recorded (never downgrade), or the old target is gone.
    if not _newer(stored, VERSION) or not os.path.exists(tgt):
        set_autostart(True)                      # recreate the shortcut pointing to this exe
        try:
            D.log("[deck] autostart re-pointed to v%s (%s)" % (VERSION, sys.executable))
        except Exception:
            pass


# ---------------------------------------------------------------- i18n bundle for the frontend
def _strings():
    keys = ["subtitle", "tutorial_btn", "st_running", "st_searching", "st_stopped", "start", "restart",
            "stop", "light_toggle", "light_hint", "editor", "preview", "editor_hint", "preview_hint",
            "light_settings", "sens", "gain", "bright", "bass", "treble", "light_settings_hint",
            "sup_title", "sup_desc", "copy", "copied", "send_ton", "upd_text", "download", "later",
            "ver_label", "autostart",
            "autostart_switch", "autostart_hint", "layout", "export", "import", "language",
            "tutorial_section", "tutorial_again", "rights_badge", "author_name", "author_rights",
            "tg_btn", "mail_btn", "dlg_sub", "f_name", "f_color", "f_type", "f_param", "browse", "save",
            "cancel", "scene_ctrl", "scene_prev", "scene_next", "scene_random", "scene_palette",
            "scene_auto", "scene_hint", "ctl_popup_title", "ctl_note", "grid_ctl_hint", "profiles",
            "prof_new", "prof_rename", "prof_del", "prof_name_q", "prof_hint", "obs_section",
            "obs_pass_ph", "obs_hint", "obs_backend_lbl", "obs_backend_auto", "obs_backend_obs",
            "obs_backend_slobs", "obs_pw_lbl", "obs_test", "obs_test_wait", "obs_test_ok",
            "obs_test_fail", "plugins_section", "plugins_open", "plugins_hint", "tut_back",
            "tut_next", "tut_done", "tut_skip"]
    out = {k: i18n.t(k) for k in keys}
    for tp in TYPES:
        out["type_" + tp] = i18n.t("type_" + tp)
        out["hint_" + tp] = i18n.t("hint_" + tp)
    for nm in CTL_CMD:
        out["ctl_" + nm + "_t"] = i18n.t("ctl_" + nm + "_t")
        out["ctl_" + nm + "_d"] = i18n.t("ctl_" + nm + "_d")
    for _e, _k, n in TUT_KINDS:
        for suf in ("t", "b", "e"):
            out[f"tut{n}_{suf}"] = i18n.t(f"tut{n}_{suf}")
    return out


# ---------------------------------------------------------------- JS <-> Python API
class Api:
    def meta(self):
        return {
            "colors": COLOR_NAMES, "chex": C_HEX, "types": TYPES, "suggest": SUGGEST,
            "no_param": list(NO_PARAM), "top_ctl": TOP_CTL, "right_ctl": RIGHT_CTL,
            "ctl_lbl": CTL_LBL, "tut": TUT_KINDS,
            "lang": i18n.current(), "langs": i18n.LANG_NAMES, "lang_order": i18n.LANG_ORDER,
            "obs_password": load_settings().get("obs_password", ""),
            "obs_backend": load_settings().get("obs_backend", "auto"),
            "obs_port": int(load_settings().get("obs_port", 4455)),
            "tutorial_seen": os.path.exists(os.path.join(CFG_DIR, "tutorial_seen.txt")),
            "device": detect_device(),
            # Launchpad Pro extra outer buttons (assignable macros), keyed "o<sysex-index>"
            "pro_bottom": ["o%d" % i for i in range(1, 9)],            # bottom row, left -> right
            "pro_left": ["o%d" % (i * 10) for i in range(8, 0, -1)],   # left column, top -> bottom
            "version": VERSION, "ton_address": TON_ADDRESS, "ton_link": TON_LINK,
            "s": _strings(),
        }

    def update_info(self):
        return _update[0]

    def state(self):
        return {"running": is_running(), "want": want_running, "layout": layout,
                "light": light_ui, "profiles": list_profiles(), "active": active_profile(),
                "autostart": os.path.exists(AUTO_LNK),
                "show_req": _want_show[0], "update": _update[0]}

    def show_main(self):
        # called from the page's poll loop (API context) when the tray asked to show
        _want_show[0] = False
        try:
            MAIN[0].show()
        except Exception:
            pass
        try:
            self._center(680, 860, MAIN[0])
        except Exception:
            pass
        return True

    def start(self):
        global want_running
        want_running = True
        _stop_engine(); _spawn()
        return True

    def stop(self):
        global want_running
        want_running = False
        _stop_engine()
        return True

    def toggle_light(self):
        if is_running():
            engine.toggle_light()
        return True

    def light_cmd(self, name):
        if is_running():
            engine.light_cmd(name)
        return True

    def logo_egg(self):
        if is_running():
            engine.logo_egg()
        return True

    # ---- window centring & splash / closing ----
    def _center(self, w, h, win):
        try:
            u = ctypes.windll.user32
            sw, sh = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
            win.move(max(0, (sw - w) // 2), max(0, (sh - h) // 2))
        except Exception:
            pass

    def center_splash(self):
        self._center(440, 470, SPL[0]); return True

    def prepare_main(self):           # main page loaded: keep it off-screen until splash ends
        if _splash_done[0]:           # language reload -> it's already visible, just re-centre
            self._center(680, 860, MAIN[0])
        return True                   # first load: leave it off-screen (created at -3000,-3000)

    def splash_mode(self):
        return _mode[0]               # 'in' (startup) or 'out' (closing); splash.html polls this

    def splash_in_done(self):
        _splash_done[0] = True
        self._center(680, 860, MAIN[0])   # move the app on-screen, centred
        try:
            SPL[0].hide()                 # hide the frameless splash
        except Exception:
            pass
        return True

    def do_exit(self):
        _stop_engine()                # ensure pad shutdown finished & the port is free
        os._exit(0)                   # force-exit — reliable, never hangs

    def set_light(self, key, val):
        light_ui[key] = round(float(val), 2)
        if is_running():
            engine.light_cfg[key] = light_ui[key]
        save_settings({**load_settings(), **light_ui})
        return True

    def save_pad(self, key, data):
        layout[key] = data
        save_config()
        return True

    def delete_pad(self, key):
        layout.pop(key, None)
        save_config()
        return True

    def grid(self):
        g = getattr(engine, "grid", None) if is_running() else None
        if not g:
            return {}
        return {str(idx): "#%02x%02x%02x" % (min(255, c[0] * 2), min(255, c[1] * 2), min(255, c[2] * 2))
                for idx, c in g.items()}

    def pad_index(self, c, r):
        return D.pad_index(c, r)

    def switch_profile(self, name):
        global layout
        save_profile(active_profile())
        d = load_settings(); d["profile"] = name; save_settings(d)
        try:
            with open(_prof_path(name), encoding="utf-8") as f:
                layout = json.load(f)
        except Exception:
            layout = {}
        save_config()
        return self.state()

    def new_profile(self, name):
        name = (name or "").strip()
        if not name or name in list_profiles():
            return self.state()
        save_profile(active_profile())
        d = load_settings(); d["profile"] = name; save_settings(d)
        save_profile(name)
        return self.state()

    def rename_profile(self, name):
        old = active_profile(); name = (name or "").strip()
        if not name or name == old or name in list_profiles():
            return self.state()
        try:
            os.rename(_prof_path(old), _prof_path(name))
        except Exception:
            return self.state()
        d = load_settings(); d["profile"] = name; save_settings(d)
        return self.state()

    def delete_profile(self):
        if len(list_profiles()) <= 1:
            return self.state()
        try:
            os.remove(_prof_path(active_profile()))
        except Exception:
            pass
        rest = list_profiles()
        return self.switch_profile(rest[0] if rest else "default")

    def set_autostart(self, on):
        set_autostart(bool(on))
        return True

    def set_obs_password(self, pw):
        save_settings({**load_settings(), "obs_password": pw or ""})
        D._OBS["cl"] = None                      # force reconnect with new password
        return True

    def set_obs_backend(self, backend):
        b = backend if backend in ("auto", "obs", "streamlabs") else "auto"
        save_settings({**load_settings(), "obs_backend": b})
        D._OBS["cl"] = None
        return True

    def set_obs_port(self, port):
        try:
            p = int(port)
        except Exception:
            p = 4455
        save_settings({**load_settings(), "obs_port": p})
        D._OBS["cl"] = None
        return True

    def obs_test(self):
        try:
            ok, backend, msg = D.obs_test()
            return {"ok": ok, "backend": backend, "msg": msg}
        except Exception as e:
            return {"ok": False, "backend": None, "msg": str(e)}

    def open_plugins(self):
        ensure_plugins()
        try:
            os.startfile(PLUGINS_DIR)
        except Exception:
            pass
        return True

    def open_url(self, url):
        try:
            os.startfile(url)
        except Exception:
            pass
        return True

    def set_tutorial_seen(self):
        try:
            open(os.path.join(CFG_DIR, "tutorial_seen.txt"), "w").write("1")
        except Exception:
            pass
        return True

    def set_lang(self, code):
        if code not in i18n.LANG_NAMES:
            return False
        save_settings({**load_settings(), "lang": code})
        i18n.set_current(code)       # apply live — the page just reloads, the engine keeps running
        return True


def _poller():
    """Keep the engine alive (auto-reconnect to the pad), like the Tk version."""
    global last_attempt
    while True:
        if want_running and not is_running() and time.time() - last_attempt > 3.0:
            last_attempt = time.time()
            try:
                _spawn()
            except Exception:
                pass
        time.sleep(1.0)


def _tray_image():
    from PIL import Image
    for name in ("deck_icon.png", "icon.png"):
        for base in (RES, HERE):
            try:
                p = os.path.join(base, name)
                if os.path.exists(p):
                    return Image.open(p).convert("RGBA").resize((64, 64))
            except Exception:
                pass
    return Image.new("RGBA", (64, 64), (124, 92, 255, 255))


def build_tray(api):
    import pystray
    from pystray import Menu, MenuItem

    def run_text(item):
        return "■  Остановить" if want_running else "▶  Запустить"

    def toggle_run(icon, item):
        api.stop() if want_running else api.start()

    def toggle_light(icon, item):
        api.toggle_light()

    def show(icon, item):
        try:
            MAIN[0].show()           # bring back from tray (works from the pystray thread on edgechromium)
        except Exception:
            _want_show[0] = True     # fallback: the page's poll loop calls api.show_main()

    def quit_(icon, item):
        try:
            _stop_engine()
        except Exception:
            pass
        os._exit(0)

    menu = Menu(
        MenuItem("Launchpad Deck", show, default=True),
        Menu.SEPARATOR,
        MenuItem(run_text, toggle_run),
        MenuItem("Свето-музыка", toggle_light),
        Menu.SEPARATOR,
        MenuItem("Показать окно", show),
        MenuItem("Выход", quit_),
    )
    return pystray.Icon("launchpad_deck", _tray_image(), "Launchpad Deck", menu)


def main():
    ensure_profiles(); ensure_plugins()
    api = Api()
    # main app window — normal (has OS min / max / close); created OFF-SCREEN, centred when splash ends
    MAIN[0] = webview.create_window("Launchpad Deck", url=os.path.join(WEB, "index.html"),
                                    js_api=api, width=680, height=860, min_size=(620, 600),
                                    x=-3000, y=-3000, background_color="#0b0b11")
    # frameless splash / closing window (no OS controls) — small & centred
    SPL[0] = webview.create_window("Launchpad Deck", url=os.path.join(WEB, "splash.html"),
                                   js_api=api, width=440, height=470, frameless=True,
                                   easy_drag=False, resizable=False, on_top=True,
                                   background_color="#0b0b11")

    def on_closing():                # X on the main window
        global want_running
        if _want_close[0]:
            return True
        if TRAY[0] is not None:      # minimize to tray (engine keeps running) — no bloom, no exit
            try:
                MAIN[0].hide()       # fully hide -> disappears from the taskbar
            except Exception:
                try:
                    MAIN[0].move(-3000, -3000)
                except Exception:
                    pass
            if not _min_notified[0]:
                _min_notified[0] = True
                try:
                    TRAY[0].notify("Свёрнуто в трей. Управление — по клику на иконке.", "Launchpad Deck")
                except Exception:
                    pass
            return False
        # no tray -> play the closing bloom, then exit
        _want_close[0] = True
        want_running = False
        _mode[0] = "out"
        threading.Thread(target=_stop_engine, daemon=True).start()   # pad shutdown + free port
        try:
            MAIN[0].move(-3000, -3000)   # slide the app off-screen
        except Exception:
            pass
        try:
            SPL[0].show()            # frameless window shows the closing bloom (splash.html polls mode)
        except Exception:
            pass
        return False                # keep main alive; do_exit() kills the whole process

    try:
        MAIN[0].events.closing += on_closing
    except Exception:
        pass

    def on_start():
        global want_running
        want_running = True
        _spawn()
        threading.Thread(target=_poller, daemon=True).start()
        threading.Thread(target=_check_update, daemon=True).start()
        threading.Thread(target=refresh_autostart, daemon=True).start()
        try:
            TRAY[0] = build_tray(api)
            TRAY[0].run_detached()
        except Exception:
            TRAY[0] = None

    ico = os.path.join(RES, "deck_icon.ico")
    if not os.path.exists(ico):
        ico = os.path.join(HERE, "deck_icon.ico")
    kw = {"gui": "edgechromium"}
    if os.path.exists(ico):
        kw["icon"] = ico
    try:
        webview.start(on_start, **kw)
    except TypeError:                    # older pywebview without icon= support
        webview.start(on_start, gui="edgechromium")


if __name__ == "__main__":
    main()
