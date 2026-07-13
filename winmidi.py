"""Robust Windows winmm MIDI SysEx output (avoids pygame.midi's native crash)."""
import ctypes
import time
from ctypes import wintypes

_winmm = ctypes.windll.winmm

MMSYSERR_NOERROR = 0
MIDIERR_STILLPLAYING = 33
DWORD_PTR = ctypes.c_void_p          # pointer-sized on 32/64-bit


class MIDIOUTCAPS(ctypes.Structure):
    _fields_ = [
        ("wMid", wintypes.WORD), ("wPid", wintypes.WORD),
        ("vDriverVersion", wintypes.UINT),
        ("szPname", wintypes.WCHAR * 32),
        ("wTechnology", wintypes.WORD), ("wVoices", wintypes.WORD),
        ("wNotes", wintypes.WORD), ("wChannelMask", wintypes.WORD),
        ("dwSupport", wintypes.DWORD),
    ]


class MIDIHDR(ctypes.Structure):
    _fields_ = [
        ("lpData", ctypes.c_char_p),
        ("dwBufferLength", wintypes.DWORD),
        ("dwBytesRecorded", wintypes.DWORD),
        ("dwUser", DWORD_PTR),
        ("dwFlags", wintypes.DWORD),
        ("lpNext", ctypes.c_void_p),
        ("reserved", DWORD_PTR),
        ("dwOffset", wintypes.DWORD),
        ("dwReserved", DWORD_PTR * 4),
    ]


# known Novation Launchpads with RGB SysEx (name-substring -> device-id byte in the header)
LAUNCHPADS = [
    ("LPProMK3", 0x0E), ("Pro MK3", 0x0E),          # Launchpad Pro MK3 (10x10, we use inner 8x8)
    ("LPMiniMK3", 0x0D), ("Mini MK3", 0x0D),        # Launchpad Mini MK3
    ("LPX", 0x0C), ("Launchpad X", 0x0C),           # Launchpad X
]


def _out_name(i):
    caps = MIDIOUTCAPS()
    _winmm.midiOutGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(caps))
    return caps.szPname


def find_output():
    """Return (device_id, header_byte, name) of the first supported Launchpad, else None."""
    n = _winmm.midiOutGetNumDevs()
    for avoid_daw in (True, False):                 # prefer the main port over the DAW port
        for i in range(n):
            name = _out_name(i)
            if avoid_daw and "MIDIOUT2" in name:
                continue
            for pat, hdr in LAUNCHPADS:
                if pat in name:
                    return i, hdr, name
    return None


class WinMidiOut:
    def __init__(self, dev_id):
        self.h = wintypes.HANDLE()
        rc = _winmm.midiOutOpen(ctypes.byref(self.h), dev_id, 0, 0, 0)
        if rc != MMSYSERR_NOERROR:
            raise RuntimeError(f"midiOutOpen failed: {rc}")

    def write_sys_ex(self, when, msg):
        data = bytes(msg)
        buf = ctypes.create_string_buffer(data, len(data))
        hdr = MIDIHDR()
        hdr.lpData = ctypes.cast(buf, ctypes.c_char_p)
        hdr.dwBufferLength = len(data)
        hdr.dwBytesRecorded = len(data)
        if _winmm.midiOutPrepareHeader(self.h, ctypes.byref(hdr), ctypes.sizeof(hdr)) != MMSYSERR_NOERROR:
            return
        _winmm.midiOutLongMsg(self.h, ctypes.byref(hdr), ctypes.sizeof(hdr))
        # wait until the message is fully sent before freeing the buffer
        for _ in range(2000):
            if _winmm.midiOutUnprepareHeader(self.h, ctypes.byref(hdr), ctypes.sizeof(hdr)) != MIDIERR_STILLPLAYING:
                break
            time.sleep(0.0003)

    def close(self):
        try:
            _winmm.midiOutReset(self.h)
            _winmm.midiOutClose(self.h)
        except Exception:
            pass


if __name__ == "__main__":
    HDR = [0x00, 0x20, 0x29, 0x02, 0x0D]
    dev = find_output()
    print("winmm out device:", dev)
    if dev is None:
        raise SystemExit("Launchpad output not found")
    out = WinMidiOut(dev)
    out.write_sys_ex(0, [0xF0] + HDR + [0x0E, 0x01, 0xF7])   # programmer mode
    time.sleep(0.2)
    # light a diagonal red, then stress-send 2000 full-grid frames
    import random
    t0 = time.time()
    for f in range(2000):
        body = []
        for r in range(8):
            for c in range(8):
                body += [0x03, (r + 1) * 10 + (c + 1), random.randint(0, 127), random.randint(0, 127), random.randint(0, 127)]
        out.write_sys_ex(0, [0xF0] + HDR + [0x03] + body + [0xF7])
    dt = time.time() - t0
    print(f"sent 2000 full-grid sysex in {dt:.1f}s ({2000/dt:.0f} fps) — NO CRASH")
    out.write_sys_ex(0, [0xF0] + HDR + [0x03] + sum([[0x03, (r + 1) * 10 + (c + 1), 0, 0, 0] for r in range(8) for c in range(8)], []) + [0xF7])
    out.write_sys_ex(0, [0xF0] + HDR + [0x0E, 0x00, 0xF7])   # live mode
    out.close()
    print("winmm OK")
