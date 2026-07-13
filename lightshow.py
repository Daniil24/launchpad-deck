"""
Launchpad Mini MK3 — audio-reactive LIGHT SHOW engine (full RGB + interactive).

A rotating show of many animations driven by whatever plays on the PC
(Spotify/browser/games) via WASAPI loopback. Scenes auto-cycle so it never
gets boring, and you can play the pads:

  * press any GRID pad   -> a colourful ripple bursts from that spot
  * TOP ROW buttons      -> 91 toggle auto-cycle | 92 prev | 93 next
                            94 palette | 95 dark | 96 bright | 98 random
Ctrl+C to quit (clears grid).

    python lightshow.py                 # full auto light show
    python lightshow.py --audio Media   # pick loopback source by name
    python lightshow.py --cycle 12      # seconds per scene (0 = manual only)
    python lightshow.py --gain 1.3 --bright 1.0 --list
"""
import argparse
import collections
import glob
import json
import math
import os
import random
import sys
import time
import warnings

import numpy as np
import soundcard as sc
from soundcard.mediafoundation import SoundcardRuntimeWarning
import pygame.midi as pm

try:
    import mido
except Exception:
    mido = None

warnings.filterwarnings("ignore", category=SoundcardRuntimeWarning)

HDR = [0x00, 0x20, 0x29, 0x02, 0x0D]
SYSEX_PROGRAMMER = [0xF0] + HDR + [0x0E, 0x01, 0xF7]
SYSEX_LIVE       = [0xF0] + HDR + [0x0E, 0x00, 0xF7]

N = 8
CX = CY = 3.5
GY, GX = np.mgrid[0:N, 0:N]                      # GX=col, GY=row(0 bottom)
DIST = np.hypot(GX - CX, GY - CY)
ANG = np.arctan2(GY - CY, GX - CX)               # -pi..pi
IDX = [[(r + 1) * 10 + (c + 1) for c in range(N)] for r in range(N)]

TOP_ROW = [91, 92, 93, 94, 95, 96, 97, 98]
RIGHT_COL = [19, 29, 39, 49, 59, 69, 79, 89]


def hsv2rgb(h, s, v):
    """Vectorised HSV->RGB. h,s,v arrays (or scalars) in 0..1 -> (...,3)."""
    h = np.asarray(h, dtype=np.float32) % 1.0
    s = np.asarray(s, dtype=np.float32)
    v = np.asarray(v, dtype=np.float32)
    i = np.floor(h * 6).astype(int) % 6
    f = h * 6 - np.floor(h * 6)
    p = v * (1 - s); q = v * (1 - f * s); t = v * (1 - (1 - f) * s)
    r = np.choose(i, [v, q, p, p, t, v])
    g = np.choose(i, [t, v, v, q, p, p])
    b = np.choose(i, [p, p, t, v, v, q])
    return np.stack([r, g, b], axis=-1).astype(np.float32)


def rgb_sysex(fb):
    v = np.clip(fb, 0, 1)
    body = []
    for r in range(N):
        for c in range(N):
            px = v[r, c]
            body += [0x03, IDX[r][c], int(px[0] * 127), int(px[1] * 127), int(px[2] * 127)]
    return [0xF0] + HDR + [0x03] + body + [0xF7]


# ------------------------------------------------------------------ effects
class Effect:
    name = "?"
    def __init__(self):
        self.buf = np.zeros((N, N, 3), np.float32)
    def frame(self, ctx):
        return self.buf


class Plasma(Effect):
    name = "plasma"
    def frame(self, ctx):
        t = ctx.flow
        p = (np.sin(GX * 0.6 + t) + np.sin(GY * 0.5 - t * 1.1)
             + np.sin((GX + GY) * 0.4 + t * 0.7)
             + np.sin(DIST * 0.9 - t * 1.3))
        p = (p + 4) / 8.0
        val = (0.2 + 0.8 * ctx.energy) * (0.35 + 0.65 * p)
        return hsv2rgb(ctx.hue + p * 0.25, 0.95, val)


class Rings(Effect):
    name = "rings"
    def __init__(self):
        super().__init__(); self.rings = []
    def frame(self, ctx):
        self.buf *= 0.40
        if ctx.beat:
            self.rings.append([0.0, (ctx.hue + 0.15 * np.random.rand()) % 1.0, 1.0])
        alive = []
        for rg in self.rings:
            rg[0] += ctx.dt * 9.0; rg[2] -= ctx.dt * 1.1
            if rg[2] > 0 and rg[0] < 8:
                band = np.exp(-((DIST - rg[0]) ** 2) / 0.35) * rg[2]
                self.buf += band[:, :, None] * hsv2rgb(rg[1], 1.0, 1.0)
                alive.append(rg)
        self.rings = alive
        return self.buf


class Bars(Effect):
    name = "bars"
    def frame(self, ctx):
        fb = np.zeros((N, N, 3), np.float32)
        for c in range(N):
            h = ctx.bands[c] * N
            hue = (0.33 - ctx.bands[c] * 0.33) % 1.0     # green->red by level
            for r in range(N):
                if r < h:
                    fb[r, c] = hsv2rgb(hue, 1.0, 1.0)
                elif r < h + 1:
                    fb[r, c] = hsv2rgb(hue, 1.0, (h - r) if h - r > 0 else 0)
        return fb


class MirrorBars(Effect):
    name = "mirror"
    def frame(self, ctx):
        fb = np.zeros((N, N, 3), np.float32)
        for c in range(N):
            h = ctx.bands[c] * 4
            for d in range(N):
                dd = abs(d - 3.5)
                if dd < h:
                    fb[d, c] = hsv2rgb(ctx.hue + c * 0.03, 1.0, 1.0)
        return fb


class Sparkle(Effect):
    name = "sparkle"
    def frame(self, ctx):
        self.buf *= 0.80
        n = int(ctx.energy * 5) + (5 if ctx.beat else 0)
        for _ in range(n):
            r, c = np.random.randint(0, N, 2)
            self.buf[r, c] = hsv2rgb((ctx.hue + np.random.rand() * 0.4) % 1.0, 1.0, 1.0)
        return self.buf


class Rain(Effect):
    name = "rain"
    def __init__(self):
        super().__init__(); self.drops = []
    def frame(self, ctx):
        self.buf *= 0.38
        if np.random.rand() < 0.3 + ctx.energy:
            self.drops.append([N - 1.0, np.random.randint(0, N),
                               (ctx.hue + np.random.rand() * 0.2) % 1.0])
        alive = []
        for d in self.drops:
            d[0] -= ctx.dt * (6 + ctx.energy * 12)
            r = int(round(d[0]))
            if r >= 0:
                self.buf[r, int(d[1])] = hsv2rgb(d[2], 0.9, 1.0)
                alive.append(d)
        self.drops = alive
        return self.buf


class Fireworks(Effect):
    name = "fireworks"
    def __init__(self):
        super().__init__(); self.parts = []
    def frame(self, ctx):
        self.buf *= 0.46
        if ctx.beat:
            cx, cy = np.random.uniform(1.5, 5.5, 2)
            hue = np.random.rand()
            for a in np.linspace(0, 2 * math.pi, 10, endpoint=False):
                self.parts.append([cx, cy, math.cos(a) * 6, math.sin(a) * 6, hue, 1.0])
        alive = []
        for p in self.parts:
            p[0] += p[2] * ctx.dt; p[1] += p[3] * ctx.dt; p[5] -= ctx.dt * 1.6
            r, c = int(round(p[1])), int(round(p[0]))
            if p[5] > 0 and 0 <= r < N and 0 <= c < N:
                self.buf[r, c] += hsv2rgb(p[4], 1.0, p[5])[0] if False else hsv2rgb(p[4], 1.0, p[5])
            if p[5] > 0:
                alive.append(p)
        self.parts = alive
        return np.clip(self.buf, 0, 1)


class Spiral(Effect):
    name = "spiral"
    def frame(self, ctx):
        t = ctx.flow
        v = 0.5 + 0.5 * np.sin(ANG * 3 + DIST * 1.2 - t * 3)
        v = v ** 2 * (0.3 + 0.7 * ctx.energy)
        return hsv2rgb(ctx.hue + ANG / (2 * math.pi), 1.0, v)


class Scanner(Effect):
    name = "scanner"
    def frame(self, ctx):
        pos = (math.sin(ctx.flow * 1.5) * 0.5 + 0.5) * (N - 1)
        col = np.exp(-((GX - pos) ** 2) / 0.6)
        val = col * (0.4 + 0.6 * ctx.energy)
        return hsv2rgb(ctx.hue + GY * 0.04, 1.0, val)


class RadialSpec(Effect):
    name = "radial"
    def frame(self, ctx):
        fb = np.zeros((N, N, 3), np.float32)
        for ring in range(N):
            lvl = ctx.bands[min(ring, N - 1)]
            mask = np.exp(-((DIST - ring) ** 2) / 0.5)
            fb += (mask * lvl)[:, :, None] * hsv2rgb((ctx.hue + ring * 0.09) % 1.0, 1.0, 1.0)
        return np.clip(fb, 0, 1)


class Matrix(Effect):
    name = "matrix"
    def __init__(self):
        super().__init__(); self.col = np.zeros(N)
    def frame(self, ctx):
        self.buf *= 0.46
        for c in range(N):
            if np.random.rand() < 0.15 + ctx.energy * 0.4:
                self.buf[N - 1, c] = np.array([0.2, 1.0, 0.3], np.float32)
        # shift down
        self.buf[:-1] = np.maximum(self.buf[:-1], self.buf[1:] * 0.9)
        return self.buf


class Wave(Effect):
    name = "wave"
    def frame(self, ctx):
        fb = np.zeros((N, N, 3), np.float32)
        amp = 1.5 + ctx.bass * 3
        for c in range(N):
            y = 3.5 + math.sin(c * 0.8 + ctx.flow * 2) * amp
            for r in range(N):
                d = abs(r - y)
                if d < 1.5:
                    fb[r, c] = hsv2rgb(ctx.hue + c * 0.05, 1.0, max(0, 1 - d / 1.5))
        return fb


class Strobe(Effect):
    name = "strobe"
    def __init__(self):
        super().__init__(); self.on = False
    def frame(self, ctx):
        if ctx.beat:
            self.on = not self.on
        self.buf *= 0.52
        board = ((GX + GY) % 2 == (1 if self.on else 0))
        col = hsv2rgb(ctx.hue, 1.0, 0.9)
        self.buf[board] = col
        return self.buf


class Comet(Effect):
    name = "comet"
    def __init__(self):
        super().__init__(); self.p = np.array([3.5, 3.5]); self.v = np.array([1.0, 0.7])
    def frame(self, ctx):
        self.buf *= 0.72
        sp = 4 + ctx.energy * 12
        self.p += self.v * ctx.dt * sp
        for i in (0, 1):
            if self.p[i] < 0 or self.p[i] > N - 1:
                self.v[i] *= -1; self.p[i] = np.clip(self.p[i], 0, N - 1)
        r, c = int(round(self.p[1])), int(round(self.p[0]))
        self.buf[r, c] = hsv2rgb(ctx.hue, 1.0, 1.0)
        return self.buf


class Twinkle(Effect):
    name = "twinkle"
    def frame(self, ctx):
        self.buf *= 0.88
        for _ in range(1 + int(ctx.energy * 4)):
            r, c = np.random.randint(0, N, 2)
            self.buf[r, c] = hsv2rgb((ctx.hue + np.random.rand()) % 1.0, 0.7, 1.0)
        return self.buf


class Kaleido(Effect):
    name = "kaleido"
    def frame(self, ctx):
        t = ctx.flow
        v = 0.5 + 0.5 * np.sin(np.abs(GX - CX) * 1.3 + np.abs(GY - CY) * 1.3 - t * 2.5)
        v = v ** 2 * (0.3 + 0.7 * ctx.energy)
        return hsv2rgb(ctx.hue + np.abs(GX - CX) * 0.05, 1.0, v)


class Tunnel(Effect):
    name = "tunnel"
    def frame(self, ctx):
        t = ctx.flow
        v = 0.5 + 0.5 * np.sin(DIST * 2.0 - t * 4)
        v = v ** 2 * (0.4 + 0.6 * ctx.energy)
        return hsv2rgb(ctx.hue + DIST * 0.08, 1.0, v)


class VUCenter(Effect):
    name = "vucenter"
    def frame(self, ctx):
        fb = np.zeros((N, N, 3), np.float32)
        lvl = ctx.energy * 4.5
        for r in range(N):
            d = abs(r - 3.5)
            if d < lvl:
                hue = (0.33 - (d / 4) * 0.4) % 1.0
                fb[r, :] = hsv2rgb(hue, 1.0, 1.0)
        return fb


# ---------- project clip player (uses the .mid light animations) ----------
def load_clip(path):
    if mido is None:
        return None
    try:
        mid = mido.MidiFile(path)
    except Exception:
        return None
    evs = []
    for tr in mid.tracks:
        t = 0
        for m in tr:
            t += m.time
            if m.type in ("note_on", "note_off"):
                vel = m.velocity if m.type == "note_on" else 0
                r, c = m.note // 10, m.note % 10
                if 1 <= r <= 8 and 1 <= c <= 8:
                    evs.append((t, r - 1, c - 1, vel))
    if len(evs) < 3:
        return None
    end = max(e[0] for e in evs)
    if end <= 0:
        end = 1
    return {"tpb": mid.ticks_per_beat or 96, "end": end, "evs": evs}


def load_library(folders, cap=250):
    lib = []
    paths = []
    for f in folders:
        paths += glob.glob(os.path.join(f, "*.mid"))
    random.shuffle(paths)
    for p in paths:
        clip = load_clip(p)
        if clip:
            lib.append(clip)
        if len(lib) >= cap:
            break
    return lib


class ClipPlayer(Effect):
    """Plays ONE project animation at a time (clean), switching clips musically."""
    name = "clips"
    def __init__(self, lib):
        super().__init__(); self.lib = lib; self.state = np.zeros((N, N), np.float32)
        self.base_hue = 0.0; self._new()
    def _new(self):
        self.clip = random.choice(self.lib) if self.lib else None
        self.head = 0.0; self.i = 0; self.state *= 0.0
        self.base_hue = random.random()                 # each clip = its own colour theme
    def frame(self, ctx):
        if not self.lib:
            return self.buf
        clip = self.clip
        self.head += ctx.dt * clip["tpb"] * 1.8
        while self.i < len(clip["evs"]) and clip["evs"][self.i][0] <= self.head:
            _, r, c, vel = clip["evs"][self.i]; self.i += 1
            self.state[r, c] = min(vel / 7.0, 1.0) if vel > 0 else 0.0
        # let each clip play fully, then cut to the next on a beat (or after a short hold)
        finished = self.head > clip["end"]
        if finished and (ctx.beat or self.head > clip["end"] + clip["tpb"] * 2):
            self._new()
        # crisp render: no trails, per-clip colour theme
        hue = (self.base_hue + 0.12 * ctx.hue + GY * 0.02) % 1.0
        val = np.clip(self.state, 0, 1) * (0.7 + 0.3 * ctx.energy)
        return hsv2rgb(hue, 0.9, val)


class Fire(Effect):
    name = "fire"
    def __init__(self):
        super().__init__(); self.heat = np.zeros((N, N), np.float32)
    def frame(self, ctx):
        self.heat *= 0.80
        self.heat[0] = np.maximum(self.heat[0], np.random.rand(N) * (0.4 + ctx.energy))
        if ctx.beat:
            self.heat[0] = 1.0
        up = np.zeros_like(self.heat)
        src = self.heat[:-1]
        up[1:] = (src * 0.5 + np.roll(src, 1, 1) * 0.25 + np.roll(src, -1, 1) * 0.25) * 0.97
        self.heat = np.maximum(self.heat * 0.5, up)
        return hsv2rgb(0.02 + self.heat * 0.12, 1.0, np.clip(self.heat, 0, 1))


class Aurora(Effect):
    name = "aurora"
    def frame(self, ctx):
        t = ctx.flow
        band = np.sin(GX * 0.5 + t) + np.sin(GY * 0.3 - t * 0.7)
        v = (0.5 + 0.5 * np.sin(GY * 0.8 + t + band)) ** 2 * (0.3 + 0.7 * ctx.energy)
        h = 0.33 + 0.2 * np.sin(GX * 0.3 + t * 0.5) + ctx.hue * 0.3
        return hsv2rgb(h, 0.9, v)


class Helix(Effect):
    name = "helix"
    def frame(self, ctx):
        fb = np.zeros((N, N, 3), np.float32)
        t = ctx.flow * 2
        for c in range(N):
            for off, hue in ((0.0, ctx.hue), (math.pi, (ctx.hue + 0.5) % 1.0)):
                r = int(round(3.5 + 3 * math.sin(c * 0.6 + t + off)))
                if 0 <= r < N:
                    fb[r, c] = hsv2rgb(hue, 1.0, 1.0)
        return fb


class Bounce(Effect):
    name = "bounce"
    def __init__(self):
        super().__init__(); self.y = 7.0; self.vy = 0.0; self.x = 3.5; self.vx = 1.4
    def frame(self, ctx):
        self.buf *= 0.52
        self.vy -= 18 * ctx.dt
        self.y += self.vy * ctx.dt; self.x += self.vx * ctx.dt
        if self.y < 0:
            self.y = 0.0; self.vy = abs(self.vy) * 0.9
        if self.y > N - 1:
            self.y = float(N - 1); self.vy = -abs(self.vy) * 0.9
        if ctx.beat:
            self.vy = 9.0
        if self.x < 0 or self.x > N - 1:
            self.vx *= -1; self.x = float(np.clip(self.x, 0, N - 1))
        r = int(np.clip(round(self.y), 0, N - 1)); c = int(np.clip(round(self.x), 0, N - 1))
        self.buf[r, c] = hsv2rgb(ctx.hue, 1.0, 1.0)
        return self.buf


class Starburst(Effect):
    name = "starburst"
    def __init__(self):
        super().__init__(); self.r = 99.0
    def frame(self, ctx):
        self.buf *= 0.40
        if ctx.beat:
            self.r = 0.0
        self.r += ctx.dt * 11
        for a in range(8):
            ang = a * math.pi / 4
            for rad in np.arange(0, min(self.r, 7), 0.5):
                ri = int(round(CY + math.sin(ang) * rad)); ci = int(round(CX + math.cos(ang) * rad))
                if 0 <= ri < N and 0 <= ci < N:
                    self.buf[ri, ci] = hsv2rgb((ctx.hue + a * 0.05) % 1.0, 1.0, max(0, 1 - rad / 6))
        return self.buf


class ColorWipe(Effect):
    name = "wipe2"
    def frame(self, ctx):
        diag = (GX + GY) / 14.0
        v = (0.5 + 0.5 * np.sin((diag - ctx.flow * 0.3) * 6)) ** 2 * (0.3 + 0.7 * ctx.energy)
        return hsv2rgb(ctx.hue + diag, 1.0, v)


class RainbowRoll(Effect):
    name = "rainbow"
    def frame(self, ctx):
        h = (GX + GY) / 16.0 + ctx.flow * 0.15
        return hsv2rgb(h, 1.0, 0.4 + 0.6 * ctx.energy)


class SpinRainbow(Effect):
    name = "spinbow"
    def frame(self, ctx):
        h = (ANG / (2 * math.pi) + 0.5 + ctx.flow * 0.12)
        return hsv2rgb(h, 1.0, 0.4 + 0.6 * ctx.energy)


class PlasmaRainbow(Effect):
    name = "plasma2"
    def frame(self, ctx):
        t = ctx.flow
        p = (np.sin(GX * 0.7 + t) + np.sin(GY * 0.6 - t)
             + np.sin((GX + GY) * 0.5 + t * 0.8) + np.sin(DIST - t))
        h = (p / 4 + t * 0.05) % 1.0
        return hsv2rgb(h, 1.0, 0.4 + 0.6 * ctx.energy)


class RippleField(Effect):
    name = "ripplefield"
    def frame(self, ctx):
        v = (0.5 + 0.5 * np.sin(DIST * 1.5 - ctx.flow * 3)) ** 2 * (0.3 + 0.7 * ctx.energy)
        h = (DIST * 0.12 + ctx.flow * 0.1) % 1.0
        return hsv2rgb(h, 1.0, v)


class Confetti(Effect):
    name = "confetti"
    def frame(self, ctx):
        self.buf *= 0.85
        for _ in range(2 + int(ctx.energy * 6) + (6 if ctx.beat else 0)):
            r, c = np.random.randint(0, N, 2)
            self.buf[r, c] = hsv2rgb(np.random.rand(), 1.0, 1.0)
        return self.buf


class Diamonds(Effect):
    name = "diamonds"
    def __init__(self):
        super().__init__(); self.rings = []
    def frame(self, ctx):
        self.buf *= 0.40
        if ctx.beat:
            self.rings.append([0.0, np.random.rand(), 1.0])
        man = np.abs(GX - CX) + np.abs(GY - CY)
        alive = []
        for rg in self.rings:
            rg[0] += ctx.dt * 8; rg[2] -= ctx.dt * 1.2
            if rg[2] > 0 and rg[0] < 10:
                band = np.exp(-((man - rg[0]) ** 2) / 0.5) * rg[2]
                self.buf += band[:, :, None] * hsv2rgb(rg[1], 1.0, 1.0)
                alive.append(rg)
        self.rings = alive
        return self.buf


class Meteor(Effect):
    name = "meteor"
    def __init__(self):
        super().__init__(); self.parts = []
    def frame(self, ctx):
        self.buf *= 0.52
        if np.random.rand() < 0.12 + ctx.energy * 0.5 or ctx.beat:
            self.parts.append([float(np.random.randint(N)), N - 1.0, np.random.rand()])
        alive = []
        for p in self.parts:
            p[1] -= ctx.dt * (6 + ctx.energy * 10); p[0] -= ctx.dt * 2
            r, c = int(round(p[1])), int(round(p[0]))
            if p[1] > -1:
                if 0 <= r < N and 0 <= c < N:
                    self.buf[r, c] = hsv2rgb(p[2], 1.0, 1.0)
                alive.append(p)
        self.parts = alive
        return self.buf


class CheckerColor(Effect):
    name = "checker"
    def __init__(self):
        super().__init__(); self.on = False
    def frame(self, ctx):
        if ctx.beat:
            self.on = not self.on
        v = 0.3 + 0.7 * ctx.energy
        a = ((GX + GY) % 2 == (1 if self.on else 0))[:, :, None]
        c1 = hsv2rgb(ctx.hue, 1.0, v); c2 = hsv2rgb((ctx.hue + 0.5) % 1.0, 1.0, v)
        return np.where(a, c1, c2).astype(np.float32)


def _cells(rows):
    out = []
    for i, line in enumerate(rows):
        for c, ch in enumerate(line):
            if ch == "#":
                out.append((c, 7 - i))       # row 0 = bottom
    return out


DANCE = [
    _cells(["...##...", "...##...", ".######.", "...##...", "...##...", "..#..#..", ".#....#.", "#......#"]),
    _cells([".#....#.", "..#..#..", "...##...", ".######.", "...##...", "...##...", "..#..#..", ".#....#."]),
    _cells(["...##...", "...##...", "...##...", ".#.##.#.", "...##...", "...##...", "...##...", "..#..#.."]),
    _cells(["#......#", ".#....#.", "..####..", "...##...", "..####..", "...##...", "..#..#..", ".#....#."]),
]
EGGS = [
    _cells(["..####..", ".#....#.", "#.#..#.#", "#......#", "#.#..#.#", "#..##..#", ".#....#.", "..####.."]),  # smiley
    _cells([".##..##.", "########", "########", "########", ".######.", "..####..", "...##...", "........"]),  # heart
    _cells(["...#....", "...#....", ".#####..", "..###...", ".#####..", ".#...#..", "#.....#.", "........"]),  # star
    _cells(["....##..", "....#.#.", "....#...", "....#...", ".####...", ".####...", "..##....", "........"]),  # note
]


class Snake(Effect):
    """An auto-playing snake that grows with the music, sparkling on hi-hats."""
    name = "snake"
    def __init__(self):
        super().__init__(); self.body = [(4, 4)]; self.dir = (1, 0); self.t = 0.0
    def frame(self, ctx):
        self.buf *= 0.5; self.t += ctx.dt
        if self.t > 0.09:
            self.t = 0.0
            if random.random() < 0.3:
                self.dir = random.choice([(1, 0), (-1, 0), (0, 1), (0, -1)])
            hx, hy = self.body[0]
            self.body.insert(0, ((hx + self.dir[0]) % N, (hy + self.dir[1]) % N))
            while len(self.body) > 6 + int(ctx.energy * 5):
                self.body.pop()
        for i, (x, y) in enumerate(self.body):
            self.buf[y, x] = hsv2rgb((ctx.hue + i * 0.05) % 1.0, 1.0, 1 - i / max(len(self.body), 1) * 0.6)
        if ctx.hihat:
            for _ in range(3):
                self.buf[np.random.randint(0, N), np.random.randint(0, N)] = hsv2rgb((ctx.hue + 0.4) % 1.0, 0.3, 1.0)
        return self.buf


class DancingMan(Effect):
    """A little figure dancing — changes pose on the beat, sparkles on hi-hats."""
    name = "dance"
    def __init__(self):
        super().__init__(); self.pose = 0
    def frame(self, ctx):
        fb = np.zeros((N, N, 3), np.float32)
        if ctx.beat:
            self.pose = (self.pose + 1) % len(DANCE)
        col = hsv2rgb(ctx.hue, 1.0, 1.0)
        for (x, y) in DANCE[self.pose]:
            fb[min(y, N - 1), x] = col
        if ctx.hihat:
            for _ in range(2):
                fb[np.random.randint(0, N), np.random.randint(0, N)] = hsv2rgb((ctx.hue + 0.5) % 1.0, 0.3, 1.0)
        return fb


class PoseChar(Effect):
    """Base for little characters that change pose on the beat."""
    poses = []
    char_hue = None
    def __init__(self):
        super().__init__(); self.pose = 0
    def frame(self, ctx):
        fb = np.zeros((N, N, 3), np.float32)
        if ctx.beat:
            self.pose = (self.pose + 1) % len(self.poses)
        col = hsv2rgb(self.char_hue if self.char_hue is not None else ctx.hue, 1.0, 1.0)
        for (x, y) in self.poses[self.pose]:
            fb[min(y, N - 1), x] = col
        if ctx.hihat:
            for _ in range(2):
                fb[np.random.randint(0, N), np.random.randint(0, N)] = hsv2rgb((ctx.hue + 0.5) % 1.0, 0.3, 1.0)
        return fb


class Alien(PoseChar):
    name = "alien"; char_hue = 0.33
    poses = [
        _cells(["..#..#..", "#.####.#", "########", "##.##.##", "########", ".#.##.#.", "#.#..#.#", "........"]),
        _cells(["..#..#..", "#.####.#", "########", "##.##.##", "########", "..#..#..", ".#....#.", "#......#"]),
    ]


class Cat(PoseChar):
    name = "cat"; char_hue = 0.08
    poses = [
        _cells(["#......#", "##....##", "########", "#.#..#.#", "########", "#..##..#", "########", ".######."]),
        _cells(["#......#", "##....##", "########", "#......#", "########", "#..##..#", "########", ".######."]),
    ]


class Robot(PoseChar):
    name = "robot"; char_hue = 0.55
    poses = [
        _cells([".#....#.", "..####..", ".#.##.#.", "..####..", ".######.", "..#..#..", "..#..#..", ".##..##."]),
        _cells([".#....#.", "..####..", ".#.##.#.", "..####..", ".######.", "..#..#..", ".#....#.", "#......#"]),
    ]


class RunningMan(PoseChar):
    name = "run"
    poses = [
        _cells(["...##...", "...##...", "..####..", "...##...", "..###...", "..#.....", "..#..#..", ".#....#."]),
        _cells(["...##...", "...##...", "..####..", "...##...", "...###..", "....#...", "..#..#..", "#......#"]),
    ]


class ConfettiPop(Effect):
    name = "confetti"
    def frame(self, ctx):
        self.buf *= 0.7
        for _ in range(2 + int(ctx.energy * 5) + (8 if ctx.beat else 0)):
            self.buf[np.random.randint(0, N), np.random.randint(0, N)] = hsv2rgb(np.random.rand(), 1.0, 1.0)
        return self.buf


class ColorRain(Effect):
    name = "colorrain"
    def __init__(self):
        super().__init__(); self.drops = []
    def frame(self, ctx):
        self.buf *= 0.55
        if np.random.rand() < 0.4 + ctx.energy:
            self.drops.append([N - 1.0, np.random.randint(0, N), np.random.rand()])
        alive = []
        for d in self.drops:
            d[0] -= ctx.dt * (6 + ctx.energy * 10); r = int(round(d[0]))
            if r >= 0:
                self.buf[r, int(d[1])] = hsv2rgb(d[2], 0.9, 1.0); alive.append(d)
        self.drops = alive
        return self.buf


class Swirl(Effect):
    name = "swirl"
    def frame(self, ctx):
        v = (0.5 + 0.5 * np.sin(ANG * 3 + DIST * 2 - ctx.flow * 3)) ** 2 * (0.4 + 0.6 * ctx.energy)
        return hsv2rgb(ctx.hue + ANG / (2 * math.pi) + DIST * 0.05, 1.0, v)


class IdleAnim(Effect):
    """Calm standby animation with occasional random easter-egg sprites."""
    name = "idle"
    def __init__(self):
        super().__init__(); self.t = 0.0; self.egg = None
        self.egg_until = 0.0; self.next_egg = random.uniform(5, 10)
    def frame(self, ctx):
        self.t += ctx.dt; now = self.t
        if self.egg is None and now > self.next_egg:
            self.egg = random.choice(EGGS); self.egg_until = now + 3.0
        if self.egg is not None:
            if now > self.egg_until:
                self.egg = None; self.next_egg = now + random.uniform(6, 12)
            else:
                fb = np.zeros((N, N, 3), np.float32)
                col = hsv2rgb((now * 0.12) % 1.0, 1.0, 0.55 + 0.45 * math.sin(now * 4))
                for (x, y) in self.egg:
                    fb[y, x] = col
                return fb
        pulse = 0.30 + 0.22 * math.sin(self.t * 0.8)
        p = (np.sin(GX * 0.5 + self.t * 0.4) + np.sin(GY * 0.5 - self.t * 0.3)
             + np.sin(DIST * 0.8 - self.t * 0.5) + 3) / 6.0
        v = pulse * (0.4 + 0.6 * p) * np.exp(-DIST * 0.10)
        hue = (0.55 + 0.10 * np.sin(self.t * 0.15) + p * 0.10) % 1.0
        return hsv2rgb(hue, 0.85, v)


class Equalizer(Effect):
    """Spectrum bars with white peak-hold caps — the classic, super readable."""
    name = "eq"
    def __init__(self):
        super().__init__(); self.peak = np.zeros(N)
    def frame(self, ctx):
        fb = np.zeros((N, N, 3), np.float32)
        self.peak = np.maximum(self.peak - 0.12, ctx.bands * N)
        for c in range(N):
            h = ctx.bands[c] * N
            hue = (0.33 - ctx.bands[c] * 0.33) % 1.0        # green -> red by level
            for r in range(N):
                if r < h:
                    fb[r, c] = hsv2rgb(hue, 1.0, 1.0)
            pk = int(min(self.peak[c], N - 1))
            fb[pk, c] = np.array([1, 1, 1], np.float32)     # white peak cap
        return fb


class DualVU(Effect):
    """Bass fills from the bottom (warm), treble fills from the top (cool)."""
    name = "dualvu"
    def frame(self, ctx):
        fb = np.zeros((N, N, 3), np.float32)
        bh = ctx.bass * 4.5; th = ctx.treble * 4.5
        for r in range(4):
            if r < bh:
                fb[r] = hsv2rgb(0.02, 1.0, 1.0)             # bass: rows 0..3, red/orange
        for r in range(4):
            if r < th:
                fb[7 - r] = hsv2rgb(0.55, 0.9, 1.0)         # treble: rows 7..4, cyan/blue
        return fb


class Scope(Effect):
    """Live waveform (oscilloscope) across the grid."""
    name = "scope"
    def frame(self, ctx):
        fb = np.zeros((N, N, 3), np.float32)
        w = ctx.wave
        for c in range(N):
            y = 3.5 + float(w[c]) * 3.5
            for r in range(N):
                d = abs(r - y)
                if d < 1.2:
                    fb[r, c] = hsv2rgb(ctx.hue + c * 0.03, 1.0, max(0.0, 1 - d / 1.2))
        return fb


class BassBloom(Effect):
    """A filled disc that grows and shrinks with the bass."""
    name = "bloom"
    def __init__(self):
        super().__init__(); self.rad = 0.0
    def frame(self, ctx):
        self.rad += (ctx.bass * 5.5 - self.rad) * 0.5
        v = np.clip(self.rad - DIST + 0.5, 0, 1) * (0.5 + 0.5 * ctx.energy)
        return hsv2rgb(ctx.hue + DIST * 0.04, 1.0, v)


class BeatColumns(Effect):
    """Each kick lights a random column full-height, then it fades."""
    name = "beatcol"
    def __init__(self):
        super().__init__(); self.col = np.zeros(N)
    def frame(self, ctx):
        self.col *= 0.70
        if ctx.beat:
            self.col[np.random.randint(N)] = 1.0
        fb = np.zeros((N, N, 3), np.float32)
        for c in range(N):
            if self.col[c] > 0.05:
                fb[:, c] = hsv2rgb((ctx.hue + c * 0.05) % 1.0, 1.0, self.col[c])
        return fb


class PulseGrid(Effect):
    """Whole rainbow grid pulses brightness with the bass/kick."""
    name = "pulse"
    def frame(self, ctx):
        v = 0.12 + 0.88 * ctx.bass
        return hsv2rgb((GX + GY) / 16.0 + ctx.hue, 1.0, v)


MAN = np.abs(GX - CX) + np.abs(GY - CY)           # manhattan distance (diamonds)
CHEB = np.maximum(np.abs(GX - CX), np.abs(GY - CY))  # chebyshev (squares)
SHAPE_TYPES = ("ring", "diamond", "square", "cross", "x", "triangle", "star", "dot")


class Shape:
    """An expanding geometric figure (concert 'plushka'), at any position."""
    def __init__(self, typ, hue, cx=CX, cy=CY, speed=7.0):
        self.typ = typ; self.hue = hue; self.cx = cx; self.cy = cy
        self.r = 0.0; self.life = 1.0; self.speed = speed
    def draw(self, fb, dt):
        self.r += dt * self.speed; self.life -= dt * 2.0
        if self.life <= 0:
            return False
        dx = GX - self.cx; dy = GY - self.cy
        dist = np.hypot(dx, dy); man = np.abs(dx) + np.abs(dy)
        cheb = np.maximum(np.abs(dx), np.abs(dy)); r = self.r
        if self.typ == "ring":
            m = np.exp(-((dist - r) ** 2) / 0.4)
        elif self.typ == "diamond":
            m = np.exp(-((man - r) ** 2) / 0.4)
        elif self.typ == "square":
            m = np.exp(-((cheb - r) ** 2) / 0.3)
        elif self.typ == "cross":
            m = ((np.abs(dx) < 0.6) | (np.abs(dy) < 0.6)).astype(np.float32) * np.clip(1 - dist / (r + 1.0), 0, 1)
        elif self.typ == "x":
            m = ((np.abs(dx - dy) < 0.7) | (np.abs(dx + dy) < 0.7)).astype(np.float32) * np.clip(1 - dist / (r + 1.0), 0, 1)
        elif self.typ == "triangle":
            m = np.exp(-((dist - r) ** 2) / 0.5) * (dy > -r * 0.6).astype(np.float32)
        elif self.typ == "star":
            ang = np.arctan2(dy, dx)
            m = np.exp(-((dist - r * (0.65 + 0.35 * np.cos(ang * 5))) ** 2) / 0.35)
        else:  # dot
            m = np.exp(-(dist ** 2) / 0.6) * max(0.0, 1 - self.r / 3.0)
        fb += (m * self.life)[:, :, None] * hsv2rgb(self.hue, 1.0, 1.0)
        return True


class DrumKit(Effect):
    """Kick -> centre burst, snare -> white diamond ring, hi-hat -> top sparkles."""
    name = "drums"
    def __init__(self):
        super().__init__(); self.k = 0.0; self.snares = []
    def frame(self, ctx):
        self.buf *= 0.52
        if ctx.kick:
            self.k = 1.0
        self.k *= 0.85
        if self.k > 0.02:
            self.buf += np.clip(2.4 - DIST, 0, 1)[:, :, None] * hsv2rgb(0.0, 1.0, self.k)
        if ctx.snare:
            self.snares.append([0.0, 1.0])
        alive = []
        for sn in self.snares:
            sn[0] += ctx.dt * 9; sn[1] -= ctx.dt * 1.6
            if sn[1] > 0:
                self.buf += (np.exp(-((MAN - sn[0]) ** 2) / 0.4) * sn[1])[:, :, None] * np.array([1, 1, 1], np.float32)
                alive.append(sn)
        self.snares = alive
        if ctx.hihat:
            for _ in range(4):
                self.buf[np.random.randint(5, N), np.random.randint(0, N)] = hsv2rgb(0.5, 0.6, 1.0)
        return np.clip(self.buf, 0, 1)


class HiHats(Effect):
    """Highs (hi-hats/cymbals) shimmer on top, bass glows at the bottom."""
    name = "hihat"
    def frame(self, ctx):
        self.buf *= 0.52
        n = int(ctx.treble * ctx.treble * 8) + (4 if ctx.hihat else 0)
        for _ in range(n):
            self.buf[np.random.randint(4, N), np.random.randint(0, N)] = \
                hsv2rgb((0.5 + np.random.rand() * 0.2) % 1.0, 0.5, 1.0)
        self.buf[0] = np.maximum(self.buf[0], hsv2rgb(0.02, 1.0, ctx.bass * 0.7))
        return self.buf


class PianoKeys(Effect):
    """Melodic mid-range lights columns like piano keys that fade."""
    name = "piano"
    def __init__(self):
        super().__init__(); self.lit = np.zeros(N)
    def frame(self, ctx):
        self.lit *= 0.70
        for c in range(N):
            if ctx.bands[c] > 0.45:
                self.lit[c] = max(self.lit[c], ctx.bands[c])
        fb = np.zeros((N, N, 3), np.float32)
        for c in range(N):
            if self.lit[c] > 0.05:
                h = (c / N * 0.9 + ctx.hue * 0.2) % 1.0
                for r in range(int(self.lit[c] * N)):
                    fb[r, c] = hsv2rgb(h, 0.9, self.lit[c])
        return fb


class ShapeFlash(Effect):
    """Different geometric figures burst on each drum hit."""
    name = "shapes"
    def __init__(self):
        super().__init__(); self.shapes = []
    def frame(self, ctx):
        self.buf *= 0.4
        if ctx.kick or ctx.snare:
            self.shapes.append(Shape(random.choice(SHAPE_TYPES), random.random()))
        self.shapes = [s for s in self.shapes if s.draw(self.buf, ctx.dt)]
        return np.clip(self.buf, 0, 1)


class DropBurst(Effect):
    """Concert 'drop' moment: rainbow strobe + shapes flying out."""
    name = "drop"
    def __init__(self):
        super().__init__(); self.shapes = []; self.t = 0.0
    def frame(self, ctx):
        self.t += ctx.dt
        base = hsv2rgb((GX + GY) / 16.0 + self.t * 2, 1.0,
                       0.5 + 0.5 * abs(math.sin(self.t * 18)))
        fb = base * 0.55
        if np.random.rand() < 0.6:
            self.shapes.append(Shape(random.choice(SHAPE_TYPES), random.random()))
        self.shapes = [s for s in self.shapes if s.draw(fb, ctx.dt)]
        return np.clip(fb, 0, 1)


class Bursts(Effect):
    """Shapes explode at random positions on every drum hit."""
    name = "bursts"
    def __init__(self):
        super().__init__(); self.shapes = []
    def frame(self, ctx):
        self.buf *= 0.4
        if ctx.kick or ctx.snare:
            for _ in range(2):
                self.shapes.append(Shape(random.choice(SHAPE_TYPES), random.random(),
                                         random.uniform(1, 6), random.uniform(1, 6)))
        self.shapes = [s for s in self.shapes if s.draw(self.buf, ctx.dt)]
        return np.clip(self.buf, 0, 1)


class Neon(Effect):
    """Endless neon outlines pulsing out from the centre."""
    name = "neon"
    def __init__(self):
        super().__init__(); self.shapes = []; self.t = 0.0
    def frame(self, ctx):
        self.buf *= 0.40; self.t += ctx.dt
        if ctx.beat or self.t > 0.4:
            self.t = 0.0
            self.shapes.append(Shape(random.choice(("ring", "diamond", "square", "star")),
                                     (ctx.hue + 0.3) % 1.0, speed=5.0))
        self.shapes = [s for s in self.shapes if s.draw(self.buf, ctx.dt)]
        return np.clip(self.buf, 0, 1)


class Popcorn(Effect):
    """Little shapes pop everywhere on snares and hi-hats."""
    name = "popcorn"
    def __init__(self):
        super().__init__(); self.shapes = []
    def frame(self, ctx):
        self.buf *= 0.35
        n = (2 if ctx.hihat else 0) + (2 if ctx.snare else 0) + (1 if ctx.kick else 0)
        for _ in range(n):
            self.shapes.append(Shape("dot", random.random(),
                                     random.uniform(0, N - 1), random.uniform(0, N - 1), speed=3.0))
        self.shapes = [s for s in self.shapes if s.draw(self.buf, ctx.dt)]
        return np.clip(self.buf, 0, 1)


class KaleidoShapes(Effect):
    """Symmetric shape explosions (kaleidoscope) on the kick."""
    name = "kaleido2"
    def __init__(self):
        super().__init__(); self.shapes = []
    def frame(self, ctx):
        self.buf *= 0.34
        if ctx.kick:
            self.shapes.append(Shape(random.choice(SHAPE_TYPES), random.random(), speed=8.0))
        self.shapes = [s for s in self.shapes if s.draw(self.buf, ctx.dt)]
        m = np.maximum(self.buf, self.buf[::-1])       # mirror vertically
        m = np.maximum(m, m[:, ::-1])                  # mirror horizontally
        return np.clip(m, 0, 1)


CORNERS = [(0, 0), (0, N - 1), (N - 1, 0), (N - 1, N - 1)]


class CornerBurst(Effect):
    """Figures fly out from a random CORNER on each hit."""
    name = "corners"
    def __init__(self):
        super().__init__(); self.shapes = []
    def frame(self, ctx):
        self.buf *= 0.38
        if ctx.kick or ctx.snare:
            cx, cy = random.choice(CORNERS)
            self.shapes.append(Shape(random.choice(SHAPE_TYPES), random.random(), cx, cy, speed=9.0))
        self.shapes = [s for s in self.shapes if s.draw(self.buf, ctx.dt)]
        return np.clip(self.buf, 0, 1)


class SideSweep(Effect):
    """A bright line sweeps across from a random SIDE; new direction on the beat."""
    name = "sweep"
    def __init__(self):
        super().__init__(); self.dir = 0; self.pos = 0.0
    def frame(self, ctx):
        self.buf *= 0.40
        if ctx.beat:
            self.dir = random.randint(0, 3); self.pos = 0.0
        self.pos += ctx.dt * (6 + ctx.energy * 10)
        p = self.pos % N
        if self.dir == 0:   line = np.exp(-((GX - p) ** 2) / 0.5)
        elif self.dir == 1: line = np.exp(-((GX - (N - 1 - p)) ** 2) / 0.5)
        elif self.dir == 2: line = np.exp(-((GY - p) ** 2) / 0.5)
        else:               line = np.exp(-((GY - (N - 1 - p)) ** 2) / 0.5)
        col = hsv2rgb(ctx.hue, 1.0, 1.0) * (0.5 + 0.5 * ctx.energy)
        self.buf = np.maximum(self.buf, line[:, :, None] * col)
        return self.buf


class EdgeRipples(Effect):
    """Rings spread inward from random points on the EDGES."""
    name = "edges"
    def __init__(self):
        super().__init__(); self.shapes = []
    def frame(self, ctx):
        self.buf *= 0.38
        if ctx.kick or ctx.snare:
            side = random.randint(0, 3)
            if side == 0:   pt = (random.uniform(0, N - 1), 0)
            elif side == 1: pt = (random.uniform(0, N - 1), N - 1)
            elif side == 2: pt = (0, random.uniform(0, N - 1))
            else:           pt = (N - 1, random.uniform(0, N - 1))
            self.shapes.append(Shape("ring", random.random(), pt[0], pt[1], speed=8.0))
        self.shapes = [s for s in self.shapes if s.draw(self.buf, ctx.dt)]
        return np.clip(self.buf, 0, 1)


class DiagWipe(Effect):
    """A wave expands from a random CORNER diagonally."""
    name = "diagwipe"
    def __init__(self):
        super().__init__(); self.corner = 0; self.pos = 0.0
    def frame(self, ctx):
        if ctx.beat:
            self.corner = random.randint(0, 3); self.pos = 0.0
        self.pos += ctx.dt * (5 + ctx.energy * 8)
        cx, cy = CORNERS[self.corner]
        d = np.hypot(GX - cx, GY - cy)
        v = np.exp(-((d - self.pos) ** 2) / 1.0) * (0.4 + 0.6 * ctx.energy)
        return hsv2rgb(ctx.hue + d * 0.05, 1.0, v)


class FilledShape:
    """A solid filled figure that pops in and fades."""
    def __init__(self, typ, hue, cx, cy, size):
        self.typ = typ; self.hue = hue; self.cx = cx; self.cy = cy
        self.size = size; self.life = 1.0
    def draw(self, fb, dt):
        self.life -= dt * 2.2
        if self.life <= 0:
            return False
        dx = GX - self.cx; dy = GY - self.cy
        if self.typ == "square":
            m = np.maximum(np.abs(dx), np.abs(dy)) < self.size
        elif self.typ == "diamond":
            m = (np.abs(dx) + np.abs(dy)) < self.size
        elif self.typ == "triangle":
            m = (np.hypot(dx, dy) < self.size) & (dy > -self.size * 0.4)
        else:  # circle
            m = np.hypot(dx, dy) < self.size
        fb += (m.astype(np.float32) * self.life)[:, :, None] * hsv2rgb(self.hue, 1.0, 1.0)
        return True


class FilledPop(Effect):
    """Solid squares / triangles / diamonds pop up on every drum hit."""
    name = "popshapes"
    def __init__(self):
        super().__init__(); self.shapes = []
    def frame(self, ctx):
        self.buf *= 0.35
        if ctx.kick or ctx.snare:
            for _ in range(1 + int(ctx.energy * 2)):
                self.shapes.append(FilledShape(
                    random.choice(("square", "triangle", "diamond", "circle")), random.random(),
                    random.uniform(1, N - 2), random.uniform(1, N - 2), random.uniform(1.2, 2.8)))
        self.shapes = [s for s in self.shapes if s.draw(self.buf, ctx.dt)]
        return np.clip(self.buf, 0, 1)


class ZoomShapes(Effect):
    """Figures rush toward you from the centre (tunnel / zoom)."""
    name = "zoom"
    def __init__(self):
        super().__init__(); self.shapes = []; self.t = 0.0
    def frame(self, ctx):
        self.buf *= 0.5; self.t += ctx.dt
        if ctx.beat or self.t > 0.25:
            self.t = 0.0
            self.shapes.append(Shape(random.choice(("square", "diamond", "ring")),
                                     random.random(), speed=12.0))
        self.shapes = [s for s in self.shapes if s.draw(self.buf, ctx.dt)]
        return np.clip(self.buf, 0, 1)


class NestedSquares(Effect):
    """Concentric squares pulsing outward with the bass."""
    name = "squares"
    def frame(self, ctx):
        v = (0.5 + 0.5 * np.sin(CHEB * 2 - ctx.flow * 3 - ctx.bass * 4)) ** 2 * (0.4 + 0.6 * ctx.energy)
        return hsv2rgb(ctx.hue + CHEB * 0.1, 1.0, v)


class SpinPoly(Effect):
    """A rotating multi-point star; spins faster when it's loud."""
    name = "spin"
    def frame(self, ctx):
        ang = ANG + ctx.flow * 2
        v = (0.5 + 0.5 * np.cos(ang * 5 - DIST * 1.5)) ** 2 * (0.4 + 0.6 * ctx.energy)
        return hsv2rgb(ctx.hue + ang / (2 * math.pi), 1.0, v)


class Rotator(Effect):
    """A rotating beam sweeping around the centre with a rainbow trail."""
    name = "rotator"
    def frame(self, ctx):
        a = ctx.flow * 2
        d = np.abs(((ANG - a + math.pi) % (2 * math.pi)) - math.pi)
        d = np.minimum(d, np.abs(d - math.pi))
        v = np.exp(-(d ** 2) / 0.12) * (0.4 + 0.6 * ctx.energy)
        return hsv2rgb(ctx.hue + DIST * 0.06, 1.0, v)


class Warp(Effect):
    """Stars streaming out from the centre — a space warp / hyperjump."""
    name = "warp"
    def __init__(self):
        super().__init__(); self.stars = []
    def frame(self, ctx):
        self.buf *= 0.42
        if np.random.rand() < 0.5 + ctx.energy:
            ang = np.random.uniform(0, 2 * math.pi)
            self.stars.append([CX, CY, math.cos(ang), math.sin(ang), random.random()])
        sp = (2 + ctx.energy * 9) * ctx.dt
        alive = []
        for s in self.stars:
            r = math.hypot(s[0] - CX, s[1] - CY)
            s[0] += s[2] * sp * (0.5 + r * 0.5); s[1] += s[3] * sp * (0.5 + r * 0.5)
            xi, yi = int(round(s[0])), int(round(s[1]))
            if 0 <= yi < N and 0 <= xi < N:
                self.buf[yi, xi] = hsv2rgb(s[4], 0.4, min(1.0, 0.3 + r * 0.25))
                alive.append(s)
        self.stars = alive
        return self.buf


class Heart(Effect):
    """A beating heart that pulses with the bass."""
    name = "heart"
    def frame(self, ctx):
        s = 2.3 + ctx.bass * 1.1 + 0.2 * math.sin(ctx.flow * 2)
        x = (GX - CX) / s; y = (CY - GY) / s + 0.35
        val = (x * x + y * y - 1) ** 3 - x * x * (y ** 3)
        v = (val < 0).astype(np.float32) * (0.5 + 0.5 * ctx.bass)
        return hsv2rgb(0.97 + 0.03 * math.sin(ctx.flow), 1.0, v)


class Fireflies(Effect):
    """Soft glowing dots wandering and twinkling."""
    name = "fireflies"
    def __init__(self):
        super().__init__()
        self.f = [[random.uniform(0, N - 1), random.uniform(0, N - 1),
                   random.random(), random.uniform(0, 2 * math.pi)] for _ in range(6)]
    def frame(self, ctx):
        self.buf *= 0.6
        for fl in self.f:
            fl[3] += random.uniform(-0.4, 0.4)
            fl[0] = min(max(fl[0] + math.cos(fl[3]) * ctx.dt * 2.5, 0), N - 1)
            fl[1] = min(max(fl[1] + math.sin(fl[3]) * ctx.dt * 2.5, 0), N - 1)
            tw = 0.5 + 0.5 * math.sin(ctx.flow * 3 + fl[2] * 6)
            self.buf[int(fl[1]), int(fl[0])] = hsv2rgb(fl[2], 0.6, tw * (0.4 + 0.6 * ctx.energy))
        return self.buf


class Orbit(Effect):
    """Coloured dots orbiting the centre on different rings."""
    name = "orbit"
    def frame(self, ctx):
        self.buf *= 0.55
        for i in range(3):
            rad = 1.5 + i * 1.2
            a = ctx.flow * (1.6 - i * 0.3) + i * 2
            xi = int(round(CX + math.cos(a) * rad)); yi = int(round(CY + math.sin(a) * rad))
            if 0 <= yi < N and 0 <= xi < N:
                self.buf[yi, xi] = hsv2rgb((ctx.hue + i * 0.22) % 1.0, 1.0, 1.0)
        return self.buf


class Interference(Effect):
    """Two wave sources interfering — hypnotic ripples."""
    name = "ripple2"
    def frame(self, ctx):
        t = ctx.flow
        s1 = np.hypot(GX - 2, GY - 2); s2 = np.hypot(GX - (N - 3), GY - (N - 3))
        w = np.sin(s1 * 2 - t * 3) + np.sin(s2 * 2 - t * 3)
        v = ((w + 2) / 4) ** 2 * (0.4 + 0.6 * ctx.energy)
        return hsv2rgb(ctx.hue + w * 0.1, 1.0, v)


class MeteorShower(Effect):
    """Colourful meteors streaking diagonally."""
    name = "meteors"
    def __init__(self):
        super().__init__(); self.m = []
    def frame(self, ctx):
        self.buf *= 0.5
        if np.random.rand() < 0.15 + ctx.energy * 0.4 or ctx.beat:
            self.m.append([float(np.random.randint(0, N)), float(N), random.random()])
        alive = []
        for p in self.m:
            p[1] -= ctx.dt * (6 + ctx.energy * 10); p[0] -= ctx.dt * 3
            yi, xi = int(round(p[1])), int(round(p[0]))
            if p[1] > -1:
                if 0 <= yi < N and 0 <= xi < N:
                    self.buf[yi, xi] = hsv2rgb(p[2], 0.8, 1.0)
                alive.append(p)
        self.m = alive
        return self.buf


# ---- symmetric "project-style" kaleidoscope effects (Launchpad-cover look) ----
AX = np.abs(GX - CX)           # 4-fold symmetric coords
AY = np.abs(GY - CY)
CHEBS = np.maximum(AX, AY)
MANS = AX + AY


class Mandala(Effect):
    """A 4-fold symmetric mandala that blooms with the bass."""
    name = "mandala"
    def frame(self, ctx):
        t = ctx.flow
        r = np.hypot(AX, AY)
        v = (0.5 + 0.5 * np.sin(r * 2.0 - t * 3 + ctx.bass * 4)) * \
            (0.5 + 0.5 * np.sin(AX * AY * 0.55 + t))
        v = v ** 1.4 * (0.35 + 0.65 * ctx.energy)
        return hsv2rgb(ctx.hue + r * 0.11 + t * 0.05, 1.0, v)


class KaleidoFlow(Effect):
    """Mirror-symmetric flowing plasma (kaleidoscope)."""
    name = "kflow"
    def frame(self, ctx):
        t = ctx.flow
        f = np.sin(AX * 1.2 + t) + np.sin(AY * 1.2 - t * 1.1) + np.sin((AX + AY) * 0.8 + t * 0.7)
        v = ((f + 3) / 6.0) ** 1.5 * (0.4 + 0.6 * ctx.energy)
        return hsv2rgb(ctx.hue + f * 0.08, 1.0, v)


class Petals(Effect):
    """A rotating flower whose petals pulse with the beat."""
    name = "petals"
    def frame(self, ctx):
        t = ctx.flow
        petal = 0.5 + 0.5 * np.cos(ANG * 6 + t * 2)
        target = 2.0 + petal * 2.0 + ctx.bass * 2.2
        v = np.exp(-((DIST - target) ** 2) / 1.2) * (0.4 + 0.6 * ctx.energy)
        return hsv2rgb(ctx.hue + ANG / (2 * math.pi), 1.0, v)


class SymSpectrum(Effect):
    """Spectrum as concentric squares blooming from the centre (symmetric)."""
    name = "symspec"
    def frame(self, ctx):
        fb = np.zeros((N, N, 3), np.float32)
        ring = np.clip((CHEBS - 0.5).astype(int), 0, 3)
        for k in range(4):
            lvl = float(ctx.bands[k * 2])
            fb[ring == k] = hsv2rgb((0.33 - lvl * 0.33) % 1.0, 1.0, lvl)
        return fb


class SymBloom(Effect):
    """Symmetric diamond rings bloom outward on every beat."""
    name = "symbloom"
    def __init__(self):
        super().__init__(); self.rings = []
    def frame(self, ctx):
        self.buf *= 0.45
        if ctx.beat:
            self.rings.append([0.0, random.random(), 1.0])
        alive = []
        for rg in self.rings:
            rg[0] += ctx.dt * 8; rg[2] -= ctx.dt * 1.3
            if rg[2] > 0 and rg[0] < 9:
                band = np.exp(-((MANS - rg[0]) ** 2) / 0.5) * rg[2]
                self.buf += band[:, :, None] * hsv2rgb(rg[1], 1.0, 1.0)
                alive.append(rg)
        self.rings = alive
        return np.clip(self.buf, 0, 1)


class DiamondTunnel(Effect):
    """A rainbow diamond tunnel rushing with the music."""
    name = "dtunnel"
    def frame(self, ctx):
        t = ctx.flow
        v = (0.5 + 0.5 * np.sin(MANS * 1.8 - t * 4 - ctx.bass * 3)) ** 2 * (0.4 + 0.6 * ctx.energy)
        return hsv2rgb(ctx.hue + MANS * 0.12, 1.0, v)


class Lightning(Effect):
    """Electric bolts strike from the top on the beat."""
    name = "lightning"
    def __init__(self):
        super().__init__(); self.bolts = []
    def frame(self, ctx):
        self.buf *= 0.4
        if ctx.beat:
            self.bolts.append([np.random.randint(0, N), 0.0])
        alive = []
        for b in self.bolts:
            b[1] += ctx.dt * 22
            y = int(b[1])
            if y < N:
                b[0] = int(np.clip(b[0] + np.random.randint(-1, 2), 0, N - 1))
                self.buf[N - 1 - y, b[0]] = hsv2rgb(0.58, 0.2, 1.0)
                alive.append(b)
        self.bolts = alive
        return self.buf


class HueBands(Effect):
    """Horizontal rainbow bands — each row a spectrum band, hue scrolling."""
    name = "huebands"
    def frame(self, ctx):
        fb = np.zeros((N, N, 3), np.float32)
        for r in range(N):
            fb[r, :] = hsv2rgb((r / N + ctx.flow * 0.1) % 1.0, 1.0, 0.2 + 0.8 * ctx.bands[r])
        return fb


class RadialBurst(Effect):
    """A filled colour ring bursts outward on every beat."""
    name = "rburst"
    def __init__(self):
        super().__init__(); self.r = 99.0; self.hue = 0.0
    def frame(self, ctx):
        self.buf *= 0.5
        if ctx.beat:
            self.r = 0.0; self.hue = random.random()
        self.r += ctx.dt * 12
        mask = np.clip(1 - np.abs(DIST - self.r), 0, 1)
        self.buf = np.maximum(self.buf, mask[:, :, None] * hsv2rgb(self.hue, 1.0, 1.0))
        return self.buf


class Galaxy(Effect):
    """Rotating spiral-galaxy arms."""
    name = "galaxy"
    def frame(self, ctx):
        v = (0.5 + 0.5 * np.sin(ANG * 2 + DIST * 1.5 - ctx.flow * 2.5)) ** 2 * (0.3 + 0.7 * ctx.energy)
        return hsv2rgb(ctx.hue + DIST * 0.06 + ANG / (2 * math.pi) * 0.2, 1.0, v)


class Vortex(Effect):
    """A swirling colour vortex."""
    name = "vortex"
    def frame(self, ctx):
        v = (0.5 + 0.5 * np.sin(DIST * 2.5 - ctx.flow * 4 + ANG * 2)) ** 2 * (0.4 + 0.6 * ctx.energy)
        return hsv2rgb(ctx.hue + DIST * 0.1, 1.0, v)


# Curated: clear, readable, pretty, obviously music-reactive
GEN_EFFECTS = [Snake, DancingMan, Alien, Cat, Robot, RunningMan,
               Lightning, HueBands, RadialBurst, Galaxy, Vortex, Swirl,
               ConfettiPop, ColorRain,
               Mandala, KaleidoFlow, Petals, SymSpectrum, SymBloom, DiamondTunnel,
               Bars, MirrorBars, VUCenter, Rings, Diamonds, RadialSpec,
               Fire, Fireworks, Starburst, Wave, RainbowRoll, SpinRainbow,
               Equalizer, DualVU, Scope, BassBloom, BeatColumns, PulseGrid,
               DrumKit, HiHats, PianoKeys, ShapeFlash,
               Bursts, Neon, Popcorn, KaleidoShapes,
               CornerBurst, SideSweep, EdgeRipples, DiagWipe,
               FilledPop, ZoomShapes, NestedSquares, SpinPoly, Rotator,
               Warp, Heart, Fireflies, Orbit, Interference, MeteorShower]


def build_effects(lib):
    effs = [E() for E in GEN_EFFECTS]
    if lib:
        effs = [ClipPlayer(lib)] + effs     # clips are the default show (index 0)
    return effs


def load_plugins(folder):
    """Import every .py in `folder`; return Effect subclasses found (custom user effects)."""
    import importlib.util
    import glob
    import os as _os
    out = []
    try:
        for path in sorted(glob.glob(_os.path.join(folder, "*.py"))):
            try:
                nm = "lp_plugin_" + _os.path.splitext(_os.path.basename(path))[0]
                spec = importlib.util.spec_from_file_location(nm, path)
                mod = importlib.util.module_from_spec(spec)
                # expose the toolkit so plugins don't need imports
                for k, v in {"Effect": Effect, "hsv2rgb": hsv2rgb, "np": np, "N": N,
                             "GX": GX, "GY": GY, "CX": CX, "CY": CY, "DIST": DIST}.items():
                    setattr(mod, k, v)
                spec.loader.exec_module(mod)
                for obj in vars(mod).values():
                    if isinstance(obj, type) and issubclass(obj, Effect) and obj is not Effect:
                        out.append(obj)
            except Exception as e:
                print(f"[lightshow] plugin '{path}' failed: {e}", flush=True)
    except Exception as e:
        print(f"[lightshow] plugin scan failed: {e}", flush=True)
    return out


# ------------------------------------------------------------------ context
class Ctx:
    __slots__ = ("dt", "flow", "energy", "bass", "mid", "treble",
                 "centroid", "beat", "hue", "bands", "wave",
                 "kick", "snare", "hihat", "hot", "drop")


# ------------------------------------------------------------------ ripples (pad presses)
class Ripple:
    def __init__(self, c, r, hue):
        self.c, self.r, self.hue, self.rad, self.life = c, r, hue, 0.0, 1.0
    def draw(self, fb, dt):
        self.rad += dt * 10; self.life -= dt * 2.0
        if self.life <= 0:
            return False
        d = np.hypot(GX - self.c, GY - self.r)
        band = np.exp(-((d - self.rad) ** 2) / 0.4) * self.life
        fb += band[:, :, None] * hsv2rgb(self.hue, 1.0, 1.0)
        return True


def find_port(name_sub, want_output):
    pm.init()
    for i in range(pm.get_count()):
        _i, raw, is_in, is_out, _o = pm.get_device_info(i)
        name = raw.decode(errors="replace")
        match = is_out if want_output else is_in
        if match and name_sub in name and "MIDIOUT2" not in name and "MIDIIN2" not in name:
            return i
    return None


def find_loopback_mic(name_sub=None):
    mics = [m for m in sc.all_microphones(include_loopback=True) if m.isloopback]
    if name_sub:
        for m in mics:
            if name_sub.lower() in m.name.lower():
                return m
    try:
        return sc.get_microphone(sc.default_speaker().name, include_loopback=True)
    except Exception:
        return mics[0] if mics else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", default=None)
    ap.add_argument("--gain", type=float, default=1.4)
    ap.add_argument("--bright", type=float, default=1.0)
    ap.add_argument("--cycle", type=float, default=9.0, help="seconds per scene (0=manual)")
    ap.add_argument("--sens", type=float, default=1.6, help="beat sensitivity (lower=more beats)")
    ap.add_argument("--sr", type=int, default=48000)
    ap.add_argument("--project", default=None,
                    help="path to a project's Lights folder (default: auto-scan Desktop)")
    ap.add_argument("--clips-cap", type=int, default=400)
    ap.add_argument("--no-clips", action="store_true")
    ap.add_argument("--idle-timeout", type=float, default=3.0,
                    help="seconds of silence before the calm standby animation (0=disable)")
    ap.add_argument("--quiet", action="store_true", help="hide telemetry output")
    ap.add_argument("--config", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "runtime_config.json"),
                    help="JSON file polled live for gain/sens/bright/cycle/idle")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    if args.list:
        pm.init()
        for i in range(pm.get_count()):
            _i, raw, is_in, is_out, _o = pm.get_device_info(i)
            print(f"  [{i}] {'IN ' if is_in else 'OUT'} {raw.decode(errors='replace')}")
        print("Loopback:", [m.name for m in sc.all_microphones(include_loopback=True) if m.isloopback])
        print("Effects:", [e.name for e in GEN_EFFECTS])
        return

    out_idx = find_port("LPMiniMK3 MIDI", True)
    if out_idx is None:
        print("[X] Launchpad output not found."); sys.exit(1)
    # In programmer mode this device sends control on MIDIIN2 -> read ALL inputs.
    in_idxs = []
    for i in range(pm.get_count()):
        _i, raw, is_in, is_out, _o = pm.get_device_info(i)
        if is_in and "LPMiniMK3" in raw.decode(errors="replace"):
            in_idxs.append(i)
    mic = find_loopback_mic(args.audio)
    if mic is None:
        print("[X] No loopback device."); sys.exit(1)

    out = pm.Output(out_idx)
    inps = [pm.Input(i) for i in in_idxs]
    out.write_sys_ex(0, SYSEX_PROGRAMMER)
    time.sleep(0.2)

    print(f"MIDI out [{out_idx}]  inputs {in_idxs}   audio: {mic.name}")
    print("--- CONTROLS ---")
    print("  TOP row: 1:auto  2/3:next  4:color  5:darker  6:brighter  7:clips<->fx  8:random")
    print("  RIGHT col (top->down): sens- | sens+ | input+ | input- | then scene jumps")
    print("  grid pad = ripple")
    print("Light show running. Ctrl+C to quit.")

    sr, block = args.sr, 1024
    freqs = np.fft.rfftfreq(block, 1 / sr)
    bass_bins = np.where((freqs >= 30) & (freqs <= 160))[0]
    mid_bins = np.where((freqs > 160) & (freqs <= 2000))[0]
    treb_bins = np.where((freqs > 2000) & (freqs <= 16000))[0]
    band_edges = np.logspace(np.log10(40), np.log10(16000), N + 1)
    band_bins = [np.where((freqs >= band_edges[b]) & (freqs < band_edges[b + 1]))[0] for b in range(N)]
    band_max = np.full(N, 1e-6)
    win = np.hanning(block).astype(np.float32)

    # ---- load project clip animations ----
    lib = []
    if not args.no_clips:
        if args.project:
            folders = [args.project]
        else:
            desk = os.path.join(os.path.expanduser("~"), "Desktop")
            folders = [d for d in glob.glob(os.path.join(desk, "*", "Lights")) if os.path.isdir(d)]
        if folders:
            print(f"Loading project animations from {len(folders)} folder(s)...")
            lib = load_library(folders, cap=args.clips_cap)
            print(f"  loaded {len(lib)} clip animations")

    effects = build_effects(lib)
    clip_idxs = [i for i, e in enumerate(effects) if e.name == "clips"]
    gen_idxs = [i for i in range(len(effects)) if i not in clip_idxs]
    clips_only = False                   # default: lively generative show (clips on button 7)
    cur = gen_idxs[0] if gen_idxs else 0
    nxt = None
    fade = 0.0
    auto = args.cycle > 0
    scene_t = time.time()
    ripples = []
    idle_effect = IdleAnim()
    silent_since = None
    SIL_THRESH = 0.0015

    ctx = Ctx()
    ctx.hue = 0.0; ctx.flow = 0.0
    running_max = 1e-4; since_beat = 99
    bass_max = 1e-6; treb_max = 1e-6; mid_max = 1e-6; bass_kick = 0.0
    bass_hist = collections.deque(maxlen=43)
    mid_hist = collections.deque(maxlen=43)
    treb_hist = collections.deque(maxlen=43)
    since_snare = 99; since_hat = 99
    energy_slow = 0.3; drop_until = 0.0; last_drop = 0.0
    drop_effect = DropBurst()
    palette_shift = 0.0
    hue_drift = 0.0
    bright = args.bright
    sens = args.sens          # beat/onset threshold (higher = fewer, only strong hits)
    gain = args.gain          # input level
    cycle = args.cycle        # seconds per scene (live-tunable)
    idle_to = args.idle_timeout
    cfg_path = args.config; cfg_mtime = 0.0
    hud_until = 0.0; hud_frac = 0.0; hud_color = (1.0, 1.0, 1.0)
    last = time.time()
    # telemetry
    tele_t = last; beat_count = 0; frame_ct = 0; energy_acc = 0.0

    def switch(to):
        nonlocal nxt, fade, cur
        if to != cur:
            nxt = to; fade = 0.0

    def pick(direction):
        pool = clip_idxs if (clips_only and clip_idxs) else gen_idxs
        pool = pool or list(range(len(effects)))
        if direction is None:
            return random.choice(pool)
        base = pool.index(cur) if cur in pool else 0
        return pool[(base + direction) % len(pool)]

    def show_hud(frac, color):
        nonlocal hud_until, hud_frac, hud_color
        hud_until = time.time() + 0.8
        hud_frac = float(min(max(frac, 0.0), 1.0)); hud_color = color

    try:
        with mic.recorder(samplerate=sr, channels=2, blocksize=block) as rec:
            while True:
                data = rec.record(numframes=block)
                mono = data.mean(axis=1)
                if len(mono) < block:
                    continue

                # ---- input (pads + buttons) on all ports ----
                for inp in inps:
                    while inp.poll():
                        for ev in inp.read(32):
                            (status, d1, d2, _), _t = ev[0], ev[1]
                            typ = status & 0xF0
                            if d2 <= 0:
                                continue
                            if typ == 0xB0 and d1 in TOP_ROW:          # top round row
                                b = d1 - 91
                                cur_is_clip = cur in clip_idxs
                                if b == 0: auto = not auto
                                elif b == 1:                            # prev / new clip
                                    if cur_is_clip: effects[cur]._new()
                                    else: switch(pick(-1)); auto = False
                                elif b == 2:                            # next / new clip
                                    if cur_is_clip: effects[cur]._new()
                                    else: switch(pick(+1)); auto = False
                                elif b == 3:
                                    palette_shift = (palette_shift + 0.12) % 1.0
                                    show_hud(1.0, tuple(hsv2rgb(palette_shift, 1.0, 1.0)))
                                elif b == 4:
                                    bright = max(0.25, bright - 0.15); show_hud(bright / 1.6, (1.0, 1.0, 1.0))
                                elif b == 5:
                                    bright = min(1.6, bright + 0.15); show_hud(bright / 1.6, (1.0, 1.0, 1.0))
                                elif b == 6:                            # toggle clips <-> effects
                                    clips_only = not clips_only
                                    if clips_only and clip_idxs:
                                        switch(clip_idxs[0]); auto = False
                                    else:
                                        gens = [i for i in range(len(effects)) if i not in clip_idxs]
                                        switch(random.choice(gens or [0])); auto = True
                                elif b == 7:                            # random
                                    switch(pick(None)); auto = False
                            elif typ == 0xB0 and d1 in RIGHT_COL:      # right column (top->bottom)
                                ri = RIGHT_COL.index(d1)               # 0 bottom .. 7 top
                                if ri == 7:
                                    sens = max(0.6, sens - 0.2); show_hud((sens - 0.6) / 3.4, (1.0, 1.0, 0.0))
                                elif ri == 6:
                                    sens = min(4.0, sens + 0.2); show_hud((sens - 0.6) / 3.4, (1.0, 1.0, 0.0))
                                elif ri == 5:
                                    gain = min(4.0, gain + 0.2); show_hud((gain - 0.4) / 3.6, (0.0, 1.0, 1.0))
                                elif ri == 4:
                                    gain = max(0.4, gain - 0.2); show_hud((gain - 0.4) / 3.6, (0.0, 1.0, 1.0))
                                else: switch(ri % len(effects)); auto = False    # bottom 4 -> jump scene
                            elif typ == 0x90:                          # grid pad -> ripple
                                r, c = d1 // 10 - 1, d1 % 10 - 1
                                if 0 <= r < N and 0 <= c < N:
                                    ripples.append(Ripple(c, r, (ctx.hue + 0.35 + np.random.rand() * 0.3) % 1.0))

                # ---- audio features (split by frequency band) ----
                spec = np.abs(np.fft.rfft(mono[:block] * win)).astype(np.float32)
                rms = float(np.sqrt(np.mean(mono ** 2)))
                running_max = max(running_max * 0.9995, rms, 1e-4)
                mag = spec + 1e-9
                cen = float((freqs * mag).sum() / mag.sum())
                bands = np.array([spec[b].mean() if len(b) else 0 for b in band_bins])
                band_max = np.maximum(band_max * 0.997, bands)
                bands = np.clip(np.sqrt(bands / (band_max + 1e-9)), 0, 1)

                e_bass = float(spec[bass_bins].sum())
                e_treb = float(spec[treb_bins].sum()) if len(treb_bins) else 0.0
                bass_max = max(bass_max * 0.999, e_bass, 1e-6)
                treb_max = max(treb_max * 0.999, e_treb, 1e-6)
                bass_lvl = min(e_bass / bass_max, 1.0)
                treb_lvl = min(e_treb / treb_max, 1.0)

                # BASS-onset (kick) detector = the main beat
                bass_hist.append(e_bass)
                since_beat += 1
                if len(bass_hist) > 10:
                    bthr = float(np.mean(bass_hist)) + sens * float(np.std(bass_hist))
                    beat = e_bass > bthr and e_bass > bass_max * 0.30 and since_beat > 3
                else:
                    beat = False
                if beat:
                    since_beat = 0; beat_count += 1
                bass_kick = max(bass_kick * 0.74, bass_lvl if beat else 0.0)   # visible but snappy

                # snare (mid onset) and hi-hat (treble onset)
                e_mid = float(spec[mid_bins].sum()) if len(mid_bins) else 0.0
                mid_max = max(mid_max * 0.999, e_mid, 1e-6)
                mid_hist.append(e_mid); since_snare += 1
                snare = False
                if len(mid_hist) > 10 and e_mid > float(np.mean(mid_hist)) + (sens + 0.1) * float(np.std(mid_hist)) and since_snare > 4:
                    snare = True; since_snare = 0
                treb_hist.append(e_treb); since_hat += 1
                hihat = False
                if len(treb_hist) > 10 and e_treb > float(np.mean(treb_hist)) + max(0.7, sens - 0.2) * float(np.std(treb_hist)) and since_hat > 2:
                    hihat = True; since_hat = 0

                energy = min(rms / running_max, 1.0) ** 0.5
                energy = min(max(energy * gain, 0.10), 1.0)

                now = time.time(); dt = now - last; last = now

                # ---- drop detection: only a real surge after a calmer section ----
                energy_slow = energy_slow * 0.97 + energy * 0.03
                drop = (energy > 0.85 and energy_slow < 0.5
                        and energy - energy_slow > 0.30 and now - last_drop > 5.0)
                if drop:
                    last_drop = now; drop_until = now + 1.1

                # ---- silence / standby detection ----
                if rms < SIL_THRESH:
                    if silent_since is None:
                        silent_since = now
                else:
                    silent_since = None
                idle = (idle_to > 0 and silent_since is not None
                        and now - silent_since >= idle_to)

                ctx.dt = dt
                ctx.flow += dt * (0.4 + energy * 2.2 + bass_kick * 2.0)
                ctx.energy = min(1.0, 0.4 * energy + 0.6 * bass_lvl)   # pump on bass
                ctx.bass = bass_lvl
                ctx.mid = float(bands[3])
                ctx.treble = treb_lvl
                ctx.centroid = min(max((cen - 100) / 4000.0, 0.0), 1.0)
                ctx.beat = beat
                ctx.kick = beat; ctx.snare = snare; ctx.hihat = hihat
                ctx.hot = energy; ctx.drop = drop
                ctx.bands = bands
                ctx.wave = mono[np.linspace(0, block - 1, N).astype(int)] / (float(np.max(np.abs(mono))) + 1e-6)
                hue_drift += dt * 0.03        # slow global colour cycling -> more colours
                ctx.hue = (ctx.centroid * 0.9 + palette_shift + hue_drift) % 1.0

                # beat pulse from centre -> makes the generative show feel alive
                if beat and not idle and cur not in clip_idxs:
                    ripples.append(Ripple(CX, CY, (ctx.hue + 0.5) % 1.0))

                # ---- scene switching: timer OR musical (on strong beats) ----
                if auto and nxt is None and not idle:
                    if now - scene_t > cycle or (beat and now - scene_t > 3 and np.random.rand() < 0.12):
                        switch(pick(None)); scene_t = now

                # ---- render: standby / concert-drop / full show ----
                if idle:
                    fb = idle_effect.frame(ctx).copy() * bright
                    scene_t = now        # freeze cycle timer while idle
                elif now < drop_until:
                    fb = drop_effect.frame(ctx).copy() * bright   # concert drop moment
                    scene_t = now
                else:
                    fb = effects[cur].frame(ctx)
                    if nxt is not None:
                        fbn = effects[nxt].frame(ctx)
                        fade += dt * 2.5
                        fb = fb * (1 - min(fade, 1)) + fbn * min(fade, 1)
                        if fade >= 1:
                            cur = nxt; nxt = None; scene_t = now
                    fb = fb.copy() * bright * (0.30 + 0.30 * energy + 0.8 * bass_kick)  # strong punch on bass

                # ---- overlays ----
                ripples = [rp for rp in ripples if rp.draw(fb, dt)]      # your pad presses
                if not idle and treb_lvl > 0.4:                          # highs -> sparkles
                    for _ in range(int((treb_lvl - 0.4) * 22)):
                        rr = np.random.randint(0, N); cc = np.random.randint(0, N)
                        fb[rr, cc] = hsv2rgb((ctx.hue + 0.15) % 1.0, 0.1, 1.0)

                # settings HUD: dim scene + show a level bar on the bottom row
                if now < hud_until:
                    fb *= 0.2
                    n = int(round(hud_frac * N))
                    col = np.array(hud_color, np.float32)
                    dim = np.array([0.05, 0.05, 0.05], np.float32)
                    for c in range(N):
                        fb[0, c] = col if c < n else dim

                np.clip(fb, 0, 1, out=fb)
                out.write_sys_ex(0, rgb_sysex(fb))

                # ---- live config from the control panel (only when the file changes) ----
                frame_ct += 1; energy_acc += energy
                if frame_ct % 20 == 0:
                    try:
                        m = os.path.getmtime(cfg_path)
                        if m != cfg_mtime:
                            cfg_mtime = m
                            with open(cfg_path) as f:
                                d = json.load(f)
                            gain = float(d.get("gain", gain)); sens = float(d.get("sens", sens))
                            bright = float(d.get("bright", bright)); cycle = float(d.get("cycle", cycle))
                            idle_to = float(d.get("idle", idle_to))
                    except Exception:
                        pass
                if now - tele_t >= 1.5:
                    if not args.quiet:
                        print(f"rms={rms:.4f} E={energy_acc/frame_ct:.2f} "
                              f"beats/s={beat_count/(now-tele_t):.1f} fps={frame_ct/(now-tele_t):.0f} "
                              f"scene={effects[cur].name} auto={auto} sens={sens:.1f} gain={gain:.1f}", flush=True)
                    tele_t = now; beat_count = 0; frame_ct = 0; energy_acc = 0.0
    except KeyboardInterrupt:
        pass
    finally:
        out.write_sys_ex(0, rgb_sysex(np.zeros((N, N, 3), np.float32)))
        out.write_sys_ex(0, SYSEX_LIVE)
        time.sleep(0.1)
        out.close()
        if inp: inp.close()
        pm.quit()
        print("\nStopped, grid cleared.")


if __name__ == "__main__":
    main()
