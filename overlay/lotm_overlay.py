"""
Lord of the Mysteries - live translation overlay.

Reads Chinese text off a screen region with OCR, translates it, and draws the
English positioned over each piece of text in a click-through, always-on-top
window. Text is grouped by screen position, so a cluttered menu becomes a set
of separate labels over their own buttons rather than one jumbled block.

Nothing touches the game's files or memory: it only reads pixels already on
screen, so there is no anti-cheat exposure.

Run:  python lotm_overlay.py
"""

import ctypes
import json
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
from ctypes import wintypes
from tkinter import ttk

import numpy as np
from PIL import Image, ImageGrab

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
MEMORY_PATH = os.path.join(APP_DIR, "memory.json")
MEMORY_MAX = 6000                 # translations kept on disk, oldest dropped

# ---------------------------------------------------------------- DPI / Win32

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32


def enable_dpi_awareness():
    """Capture coords and window geometry must agree in physical pixels, or a
    scaled display grabs / draws the wrong rectangle."""
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_V2
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000


def make_click_through(hwnd, click_through=True):
    try:
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        style |= WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        if click_through:
            style |= WS_EX_TRANSPARENT
        else:
            style &= ~WS_EX_TRANSPARENT
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
    except Exception:
        pass


WDA_EXCLUDEFROMCAPTURE = 0x11  # window stays visible to the user, absent from captures


def exclude_from_capture(hwnd):
    """Stop our own overlay from appearing in the screen grab, so we never
    re-OCR our own English labels (a feedback loop). The overlay stays fully
    visible to the user."""
    try:
        user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
    except Exception:
        pass


try:
    _dwmapi = ctypes.windll.dwmapi
except Exception:
    _dwmapi = None

_OWN_TITLES = {"LotM Translation Overlay", "Select window", "tk"}


def _is_cloaked(hwnd):
    if _dwmapi is None:
        return False
    val = ctypes.c_int(0)
    try:
        _dwmapi.DwmGetWindowAttribute(
            wintypes.HWND(hwnd), 14, ctypes.byref(val), ctypes.sizeof(val)  # DWMWA_CLOAKED
        )
    except Exception:
        return False
    return val.value != 0


_EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def list_windows():
    """Visible, real top-level windows the user could target: (hwnd, title, rect)."""
    out = []

    def cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd) or _is_cloaked(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n == 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value.strip()
        if not title or title in _OWN_TITLES:
            return True
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        if (r.right - r.left) < 120 or (r.bottom - r.top) < 80:
            return True
        out.append((int(hwnd), title, (r.left, r.top, r.right, r.bottom)))
        return True

    user32.EnumWindows(_EnumProc(cb), 0)
    return out


def window_region(hwnd):
    """Screen-coordinate rect of a window's client area (excludes borders/title)."""
    rc = wintypes.RECT()
    if not user32.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(rc)):
        return None
    pt = wintypes.POINT(rc.left, rc.top)
    user32.ClientToScreen(wintypes.HWND(hwnd), ctypes.byref(pt))
    w, h = rc.right - rc.left, rc.bottom - rc.top
    if w < 8 or h < 8 or pt.x < -30000 or pt.y < -30000:
        return None  # minimized / offscreen
    return [pt.x, pt.y, pt.x + w, pt.y + h]


# ---------------------------------------------------------------- capture

class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class ScreenGrabber:
    """Reads pixels off the screen, reusing one device context and one bitmap.

    PIL's ImageGrab copies the entire screen on every call and then throws most
    of it away; blitting just the region measured about twice as fast, and the
    poll can ask for the downscaled grid directly instead of capturing a full
    frame only to shrink it.

    The result is checked against ImageGrab the first time a monitor is used.
    Capture is the one thing here that can fail silently - an odd display setup,
    a protected surface - and a black frame would look exactly like "no text".
    If the two disagree, this steps aside and ImageGrab does the work.
    """

    SRCCOPY = 0x00CC0020
    HALFTONE = 4

    def __init__(self):
        self.dc = user32.GetDC(None)
        self.mem = gdi32.CreateCompatibleDC(self.dc) if self.dc else None
        self.dibs = {}                 # (w, h) -> (bitmap, pixel pointer)
        self.ok = bool(self.dc and self.mem)
        self.checked = None            # monitor the check was run against
        self.tries = 0

    def _dib(self, w, h):
        hit = self.dibs.get((w, h))
        if hit:
            return hit
        bi = BITMAPINFO()
        bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.bmiHeader.biWidth, bi.bmiHeader.biHeight = w, -h      # top-down rows
        bi.bmiHeader.biPlanes, bi.bmiHeader.biBitCount = 1, 32
        ptr = ctypes.c_void_p()
        bmp = gdi32.CreateDIBSection(self.dc, ctypes.byref(bi), 0,
                                     ctypes.byref(ptr), None, 0)
        if not bmp or not ptr.value:
            raise OSError("CreateDIBSection failed")
        if len(self.dibs) > 3:         # region and grid sizes; a couple spare
            old, _ = self.dibs.popitem()
            gdi32.DeleteObject(old)
        self.dibs[(w, h)] = (bmp, ptr)
        return self.dibs[(w, h)]

    def _blt(self, region, dw, dh):
        x1, y1, x2, y2 = region
        w, h = x2 - x1, y2 - y1
        bmp, ptr = self._dib(dw, dh)
        gdi32.SelectObject(self.mem, bmp)
        if (dw, dh) == (w, h):
            got = gdi32.BitBlt(self.mem, 0, 0, w, h, self.dc, x1, y1, self.SRCCOPY)
        else:
            gdi32.SetStretchBltMode(self.mem, self.HALFTONE)
            gdi32.SetBrushOrgEx(self.mem, 0, 0, None)
            got = gdi32.StretchBlt(self.mem, 0, 0, dw, dh, self.dc,
                                   x1, y1, w, h, self.SRCCOPY)
        if not got:
            raise OSError("BitBlt failed")
        buf = (ctypes.c_ubyte * (dw * dh * 4)).from_address(ptr.value)
        return np.frombuffer(buf, dtype=np.uint8).reshape(dh, dw, 4)

    def _reference(self, region):
        return np.asarray(
            ImageGrab.grab(bbox=tuple(region), all_screens=True)
            .convert("L").resize((32, 16))
        ).astype(np.float32)

    def _verify(self, region):
        """Same pixels as ImageGrab? Checked once per monitor.

        The screen is a moving target - two captures a millisecond apart differ
        on their own while the game animates - so the judgement is only made on
        a frame that held still between two reference grabs. A capture that is
        wildly different (a black frame from a protected surface) is rejected
        immediately, motion or not, because that is the failure worth catching.
        """
        mon = user32.MonitorFromPoint(wintypes.POINT(region[0] + 4, region[1] + 4), 2)
        if mon == self.checked:
            return
        before = self._reference(region)
        mine = self._blt(region, 32, 16)[:, :, :3].astype(np.float32).mean(axis=2)
        after = self._reference(region)
        off = float(np.abs(mine - after).mean())
        if off > 40.0:
            self.ok = False                      # not the same picture at all
            self.checked = mon
            return
        still = float(np.abs(before - after).mean()) < 2.0
        self.tries += 1
        if still or self.tries >= 5:
            # Judged on a still frame, or the screen never holds still and there
            # has been no sign of trouble in five looks: stop paying for this.
            if still and off > 6.0:
                self.ok = False
            self.checked = mon
            self.tries = 0

    def grid(self, region, gw, gh):
        """Region as a small greyscale grid, for change detection."""
        if self.ok:
            try:
                self._verify(region)
                if self.ok:
                    a = self._blt(region, gw, gh).astype(np.float32)
                    return a[:, :, 2] * 0.299 + a[:, :, 1] * 0.587 + a[:, :, 0] * 0.114
            except Exception:
                self.ok = False
        img = ImageGrab.grab(bbox=tuple(region), all_screens=True)
        return np.asarray(img.convert("L").resize((gw, gh))).astype(np.float32)

    def rgb(self, region):
        """Region as an HxWx3 RGB array, ready for OCR."""
        if self.ok:
            try:
                self._verify(region)
                if self.ok:
                    a = self._blt(region, region[2] - region[0], region[3] - region[1])
                    return np.ascontiguousarray(a[:, :, 2::-1])      # BGRA -> RGB
            except Exception:
                self.ok = False
        return np.asarray(
            ImageGrab.grab(bbox=tuple(region), all_screens=True).convert("RGB")
        )


# ---------------------------------------------------------------- config

DEFAULTS = {
    "mode": "window",             # "window" (track a chosen window) | "region"
    "target_title": "",           # window title to follow in window mode
    "region": None,               # [x1, y1, x2, y2] physical px (region mode)
    "backend": "auto",            # auto | ollama | anthropic | openai | none
    "ollama_url": "http://127.0.0.1:11434",
    "ollama_model": "qwen2.5:3b-instruct",
    "openai_url": "http://127.0.0.1:1234/v1",
    "openai_model": "local-model",
    "openai_key": "",
    "anthropic_model": "claude-sonnet-5",
    "poll_interval": 0.15,        # seconds between screen checks
    "change_threshold": 3.0,      # whole-region mean change that triggers a scan
    "local_delta": 16.0,          # per-cell change that reads as "new text here"
    "local_cells": 2,             # that many changed cells also trigger a scan
    "translate_threads": 3,       # translations in flight at once
    "batch_size": 48,             # short labels per batched request
    "show_pending": True,         # show the Chinese until its English lands
    "stream": True,               # paint each translation as the model writes it
    "crop_scan": True,            # re-read only the part of the screen that moved
    "full_scan_every": 12.0,      # ...but never go longer than this without a full read
    "num_ctx": 2048,              # model context; a line of dialogue needs very little
    "keep_alive": "60m",          # how long the model stays resident in Ollama
    "refine": False,              # second pass over prose with a stronger model
    "refine_model": "",           # which model that is (auto-picked when empty)
    "ocr_max_px": 1280,           # shrink anything bigger than this before reading it
    "ocr_min_px": 768,            # ...and no smaller than this when the machine is busy
    "ocr_side_len": 736,          # detection resolution, longest side
    "ocr_threads": 6,             # OCR threads; keep well under your core count
    "ocr_angle_cls": False,       # screen text is never rotated

    "min_conf": 0.55,
    "min_box_h": 10,
    "font_size": 18,
    "opacity": 0.92,
    "combine_box": False,         # True = one panel; False = positioned labels
    "show_source": False,
}


def load_config():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


# ---------------------------------------------------------------- box grouping

# Han characters only - the overlay translates Chinese and nothing else.
CJK_RE = re.compile("[㐀-䶿一-鿿豈-﫿]")


def has_cjk(s):
    return bool(CJK_RE.search(s or ""))


def rect_of(box):
    xs = [p[0] for p in box]
    ys = [p[1] for p in box]
    return int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))


def is_noise(text, conf):
    """A lone Han character read off a texture or an icon is usually not text.

    Left in, it becomes a confident nonsense label - 米 came back as "Begin
    Game" in testing - so a single character has to be read clearly to count.
    Real one-character controls in this game's UI are rare; two-character ones
    are not, and they are unaffected.
    """
    return len(text) < 2 and (conf is None or conf < 0.8)


def group_boxes(results, min_conf=0.55, min_h=10):
    """Cluster OCR line-boxes into positioned text blocks.

    PP-OCR already returns one box per text line. We merge lines that sit
    directly under each other with a similar width (a paragraph) and keep
    everything else separate, so scattered UI labels stay independent instead
    of being concatenated into one blob.

    Lines with no Han characters are dropped here: only Chinese is translated,
    so English UI, numbers and logos never reach the model or the screen.
    """
    boxes = []
    for item in (results or []):
        box, txt, conf = item[0], item[1], item[2]
        if conf is not None and conf < min_conf:
            continue
        t = (txt or "").strip()
        if not t or not has_cjk(t) or is_noise(t, conf):
            continue
        x1, y1, x2, y2 = rect_of(box)
        if y2 - y1 < min_h:
            continue
        boxes.append(
            {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "h": y2 - y1, "n": 1, "lines": [t]}
        )

    boxes.sort(key=lambda b: (b["y1"], b["x1"]))
    blocks = []
    for it in boxes:
        wi = it["x2"] - it["x1"]
        placed = False
        for bl in blocks:
            wb = bl["x2"] - bl["x1"]
            overlap = min(it["x2"], bl["x2"]) - max(it["x1"], bl["x1"])
            gap = it["y1"] - bl["y2"]
            lh = it["h"]
            wr = min(wi, wb) / max(wi, wb, 1)
            if (
                overlap > 0.35 * min(wi, wb)
                and -lh * 0.6 <= gap <= lh * 0.8
                and wr >= 0.45
            ):
                bl["x1"] = min(bl["x1"], it["x1"])
                bl["y1"] = min(bl["y1"], it["y1"])
                bl["x2"] = max(bl["x2"], it["x2"])
                bl["y2"] = max(bl["y2"], it["y2"])
                bl["lines"].append(it["lines"][0])
                bl["n"] += 1
                placed = True
                break
        if not placed:
            blocks.append(dict(it))

    for bl in blocks:
        bl["text"] = "\n".join(bl["lines"])
    return blocks


def blocks_signature(blocks):
    """Stable key for change detection; changes if text or position moves."""
    parts = [
        (round(b["x1"] / 8), round(b["y1"] / 8), b["text"]) for b in blocks
    ]
    parts.sort()
    return tuple(parts)


# ---------------------------------------------------------------- translation

GLOSSARY = (
    "序列=Sequence, 途径=Pathway, 非凡者=Beyonder, 非凡特性=Beyonder Characteristic, "
    "魔药=potion, 灵界=spirit world, 灰雾=grey fog, 愚者=The Fool, 值夜者=Nighthawks, "
    "占卜家=Seer, 小丑=Clown, 魔术师=Magician, 侦探=Anomaly-hunter, "
    "克莱恩=Klein, 莫雷蒂=Moretti, 奥黛丽=Audrey, 阿尔杰=Alger, 邓恩=Dunn, "
    "贝克兰德=Backlund, 廷根=Tingen, 鲁恩=Loen, 塔罗会=Tarot Club"
)

# Kept byte-identical between calls on purpose: an unchanged prefix is a cache
# hit in the server's KV cache, so only the new line has to be evaluated.
SYSTEM_PROMPT = (
    "You are the English translator for the Chinese game 'Lord of the Mysteries', "
    "a Victorian-era gothic occult mystery.\n"
    "Every message you are sent is Chinese text captured from the screen. Reply with "
    "its English translation and nothing else: no Chinese, no pinyin, no notes, no "
    "labels, no arrows, never the input repeated back.\n"
    "A short phrase with no sentence punctuation is a UI control: answer with the "
    "conventional English a game prints on that control, two or three words, never a "
    "sentence.\n"
    "Anything with sentence punctuation is dialogue or prose: answer in natural, "
    "literary English of a Victorian gothic register, keeping the line breaks and any "
    "speaker name, and keeping it a sentence - never a headline.\n"
    "If the text is garbled OCR noise, reply with nothing at all.\n"
    "Never swap these two: 序列 = Sequence (a numbered rank, 序列9 -> Sequence 9); "
    "途径 = Pathway (a lineage, 愚者途径 -> Fool Pathway).\n"
    "Established terms: " + GLOSSARY
)

# Examples as real turns, not as arrows inside the prompt: a small model imitates
# the shape of what it sees, and what it sees here is a bare English answer.
#
# Order matters, and prose must come first. A 3B model leans hardest on the LAST
# example, so ending on a short UI answer keeps short answers short. Measured with
# the reverse order, 开始 - a prefix of the 开始游戏 example - came back as
# "Start Sequence 8: Clown", bled from the prose example that then sat last.
FEW_SHOT = (
    ("“我是死神。”他低声说道。", '"I am Death," he said softly.'),
    ("克莱恩晋升为序列8：小丑。", "Klein ascended to Sequence 8: Clown."),
    ("开始游戏", "Start Game"),
    ("背包", "Inventory"),
)

# Numbering the reply cost 54% of everything the model wrote for a 27-label
# menu - 134 tokens against 62 - and it writes at about 115 tokens a second, so
# the numbers alone doubled the wait. One translation per line costs nothing and
# says the same thing; the reply is checked line-for-line, and the numbered form
# below is the fallback for when the model loses count.
BATCH_PROMPT = (
    "Each line below is a separate piece of Chinese screen text. Reply with one "
    "English translation per line, in the same order, exactly as many lines as you "
    "were given, and nothing else:\n"
)

BATCH_NUMBERED_PROMPT = (
    "Each numbered line below is a separate piece of Chinese screen text. Reply with "
    "the same numbers in the same order, one English translation per line, nothing "
    "else - a reply looks like:\n"
    "1. Start Game\n"
    "2. Settings\n"
    "\n"
    "Now translate:\n"
)

RETRY_PROMPT = "Translate this Chinese into English. Reply with only the English:\n"


def predict_budget(text):
    """Cap the reply length to what the input could possibly need. A wrong cap
    costs a truncated line; no cap costs a model that rambles for 400 tokens."""
    return max(48, min(400, 24 + len(text) * 3))


def _post_json(url, payload, headers=None, timeout=25):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _open_stream(url, payload, headers=None, timeout=60):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    return urllib.request.urlopen(req, timeout=timeout)


class Translator:
    def __init__(self, cfg):
        self.cfg = cfg
        self.cache = {}
        self._lock = threading.Lock()
        self._save_lock = threading.Lock()
        self._dirty = 0
        self._warm = False
        self.backend = cfg["backend"]
        if self.backend == "auto":
            self.backend = self.detect()
        self.load_memory()

    # -- backend discovery -------------------------------------------------

    def detect(self):
        if os.environ.get("ANTHROPIC_API_KEY"):
            return "anthropic"
        try:
            with urllib.request.urlopen(self.cfg["ollama_url"] + "/api/tags", timeout=2) as r:
                if json.loads(r.read().decode("utf-8")).get("models"):
                    return "ollama"
        except Exception:
            pass
        try:
            urllib.request.urlopen(self.cfg["openai_url"] + "/models", timeout=2).read()
            return "openai"
        except Exception:
            pass
        return "none"

    def available_ollama_models(self):
        try:
            with urllib.request.urlopen(self.cfg["ollama_url"] + "/api/tags", timeout=3) as r:
                return [m["name"] for m in json.loads(r.read().decode("utf-8")).get("models", [])]
        except Exception:
            return []

    def model_name(self):
        return {
            "ollama": self.cfg["ollama_model"],
            "openai": self.cfg["openai_model"],
            "anthropic": self.cfg["anthropic_model"],
        }.get(self.backend, "none")

    def pick_refine_model(self):
        """The strongest model installed that isn't the one already in use -
        for the background second pass. Bigger parameter count wins."""
        if self.backend != "ollama":
            return ""
        fast = self.cfg["ollama_model"]
        best, best_size = "", 0.0
        for m in self.available_ollama_models():
            if m == fast:
                continue
            hit = re.search(r"(\d+(?:\.\d+)?)\s*b\b", m.lower())
            size = float(hit.group(1)) if hit else 0.0
            if size > best_size:
                best, best_size = m, size
        fast_hit = re.search(r"(\d+(?:\.\d+)?)\s*b\b", fast.lower())
        fast_size = float(fast_hit.group(1)) if fast_hit else 0.0
        return best if best_size > fast_size else ""

    def warm_up(self):
        if self.backend in ("none", "anthropic", "openai"):
            return
        try:
            self.translate("开始")
        except Exception:
            pass

    # -- translation memory ------------------------------------------------
    #
    # The cache is written to disk, so a line translated last night is on
    # screen this evening with no model call at all. It is keyed by backend and
    # model: a different model translates differently, and mixing the two would
    # make the overlay inconsistent for no gain.

    def memory_key(self):
        return f"{self.backend}:{self.model_name()}"

    def load_memory(self):
        try:
            with open(MEMORY_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return
        if data.get("key") != self.memory_key():
            return
        entries = data.get("entries")
        if isinstance(entries, dict):
            with self._lock:
                for k, v in entries.items():
                    if isinstance(k, str) and isinstance(v, str) and v:
                        self.cache[k] = v

    def save_memory(self):
        with self._lock:
            snap = [(k, v) for k, v in self.cache.items() if v and not v.startswith("[")]
            self._dirty = 0
            key = self.memory_key()
        if not snap:
            return
        with self._save_lock:
            try:
                tmp = MEMORY_PATH + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump({"key": key, "entries": dict(snap[-MEMORY_MAX:])},
                              f, ensure_ascii=False)
                os.replace(tmp, MEMORY_PATH)
            except Exception:
                pass

    def forget(self):
        """Model or backend changed: the old English no longer matches."""
        with self._lock:
            self.cache.clear()
            self._dirty = 0
        self.load_memory()

    # -- terminology fixups ------------------------------------------------

    # Small models sometimes regurgitate the instructions instead of translating.
    _ECHO = (
        "Sequence = Sequence", "numbered rank", "a lineage", "Established terms",
        "one translation per line", "UI TEXT", "NARRATIVE", "Translate each",
        "game UI", "OCR noise",
    )

    @classmethod
    def _clean(cls, out):
        if not out:
            return ""
        for bad in cls._ECHO:
            if bad in out:
                return ""
        out = out.strip()
        # A reply with no Latin letters is the Chinese handed back, or noise.
        return out if re.search("[A-Za-z]", out) else ""

    @staticmethod
    def _postfix(src, out):
        if not out:
            return out
        has_seq, has_path = ("序列" in src), ("途径" in src)
        if has_seq and not has_path:
            out = re.sub(r"\bPathway\b", "Sequence", out)
        elif has_path and not has_seq:
            out = re.sub(r"\bSequence\b", "Pathway", out)
        m = re.search(r"序列\s*(\d+)", src)
        if m:
            n = m.group(1)
            if not re.search(r"Sequence\s*" + n, out):
                out = re.sub(r"\bSequence\b(?!\s*\d)", f"Sequence {n}", out, count=1)
        return out

    # -- single-item translate --------------------------------------------

    def translate(self, text, on_delta=None, alive=None, model=None, fresh=False):
        """Translate one piece of text. With on_delta, the reply is streamed and
        on_delta(partial) fires as it arrives, so English appears while the model
        is still writing it. alive() going False aborts the call mid-stream."""
        text = text.strip()
        if not text:
            return ""
        if self.backend == "none":
            return text              # "none" means: leave the Chinese on screen
        if not fresh:
            hit = self.cache.get(text)
            if hit is not None:
                return hit
        try:
            out = self._call(text, on_delta=on_delta, alive=alive, model=model)
        except urllib.error.HTTPError as e:
            return f"[{self.backend} HTTP {e.code}]"
        except Exception as e:
            return f"[{self.backend} {type(e).__name__}]"
        if alive is not None and not alive():
            return ""                    # aborted: a half-written line isn't a translation
        clean = self._postfix(text, self._clean(out))
        if not clean and self.backend != "none":
            # The model answered with the Chinese back, or with the rules, or with
            # nothing. Small models do this; asking again plainly usually fixes it,
            # and it only costs a call on the lines that actually failed.
            try:
                out = self._call(RETRY_PROMPT + text, alive=alive, model=model,
                                 examples=False)
            except Exception:
                out = ""
            if alive is not None and not alive():
                return ""
            clean = self._postfix(text, self._clean(out))
        self._remember(text, clean)
        return clean

    def _remember(self, key, val):
        if val.startswith("["):
            return                       # backend error, not a translation
        with self._lock:
            self.cache[key] = val
            if len(self.cache) > MEMORY_MAX:
                for k in list(self.cache)[:MEMORY_MAX // 5]:
                    self.cache.pop(k, None)
            self._dirty += 1
            due = self._dirty >= 25
        if due:
            threading.Thread(target=self.save_memory, daemon=True).start()

    # -- block translate (incremental) -------------------------------------

    def fill_known(self, blocks):
        """Fill any still-blank block whose text is already translated - the
        same line twice on screen costs one call, not two."""
        for b in blocks:
            if not b.get("en"):
                hit = self.cache.get(b["text"].strip())
                if hit:
                    b["en"] = hit

    def prefill(self, blocks):
        """Fill in every translation already known, without a single model
        call, and return the blocks that still need one. Blocks with no Chinese
        are left blank: only Chinese is translated, and only Chinese is drawn.
        Repeated text yields one block - the twins are filled by fill_known."""
        todo, claimed = [], set()
        for b in blocks:
            t = b["text"].strip()
            if not has_cjk(t):
                b["en"] = ""
                continue
            hit = self.cache.get(t)
            if hit is not None:
                b["en"] = hit
            elif b.get("en"):
                pass                     # carried over from the last scan
            else:
                b["en"] = ""
                if t not in claimed:
                    claimed.add(t)
                    todo.append(b)
        return todo

    @staticmethod
    def is_prose(b):
        return ("\n" in b["text"]) or len(b["text"]) > 36

    def plan_units(self, blocks):
        """Split untranslated blocks into independent units of work so each can
        reach the screen the moment it is ready.

        Prose gets its own streamed request: line breaks and quality survive, it
        starts appearing word by word, and it never waits behind the rest of the
        screen. Short labels are batched, because one request for eight buttons
        beats eight requests. Prose is ordered first - it is what the player is
        actually reading."""
        prose, shorts = [], []
        for b in blocks:
            (prose if self.is_prose(b) else shorts).append(b)
        # Biggest text first, within each kind. On a menu of 27 labels the eye is
        # on the heading, not on the volume slider, and the requests are served
        # one at a time - so the order they go out in is the order they appear.
        area = lambda b: (b["x2"] - b["x1"]) * (b["y2"] - b["y1"])
        prose.sort(key=area, reverse=True)
        shorts.sort(key=area, reverse=True)
        units = [[b] for b in prose]
        size = max(2, int(self.cfg.get("batch_size", 8)))
        if len(shorts) >= 3 and self.backend != "none":
            units += [shorts[i:i + size] for i in range(0, len(shorts), size)]
        else:
            units += [[b] for b in shorts]
        return units

    def run_unit(self, unit, on_progress=None, alive=None):
        """Translate one unit in place, streaming into the blocks as it goes.
        Safe to call from a worker thread."""
        if len(unit) == 1:
            b = unit[0]
            delta = None
            if on_progress and self.backend != "none" and self.cfg.get("stream", True):
                def delta(partial):
                    b["en"] = partial.strip()
                    on_progress()
            b["en"] = self.translate(b["text"], on_delta=delta, alive=alive)
        else:
            try:
                self._batch(unit, on_progress=on_progress, alive=alive)
            except Exception:
                pass  # fall back to per-item below
            for b in unit:
                if not b.get("en"):
                    b["en"] = self.translate(b["text"], alive=alive)
        if on_progress:
            on_progress()

    LINE_NUMBER = re.compile(r"^\s*(\d+)\s*[.．、:：)\]]\s*")

    def _batch(self, blocks, on_progress=None, alive=None):
        """One request for a screenful of short labels, one translation per line.

        Nothing is committed to memory until the whole reply is in and the line
        count matches what was asked for: an off-by-one would put the wrong
        English under every button after it, and that must not be remembered.
        """
        want = len(blocks)
        prompt = BATCH_PROMPT + "\n".join(b["text"] for b in blocks)
        shown = [0]

        def parse(acc, final=False):
            lines = acc.split("\n")
            if not final:
                lines = lines[:-1]          # the last line is still being written
            return [self.LINE_NUMBER.sub("", ln).strip() for ln in lines if ln.strip()]

        def apply(acc):
            lines = parse(acc)
            fresh = False
            for i in range(min(len(lines), want)):
                en = self._clean(lines[i])
                if en and blocks[i].get("en") != en:
                    blocks[i]["en"] = self._postfix(blocks[i]["text"], en)
                    fresh = True
            if fresh:
                shown[0] = min(len(lines), want)
                if on_progress:
                    on_progress()

        live = on_progress and self.cfg.get("stream", True)
        raw = self._call(prompt, on_delta=(apply if live else None), alive=alive)
        if alive is not None and not alive():
            return
        lines = parse(raw or "", final=True)
        if len(lines) == want:
            for b, ln in zip(blocks, lines):
                en = self._clean(ln)
                if en:
                    b["en"] = self._postfix(b["text"], en)
                    self._remember(b["text"], b["en"])
            if shown[0] and on_progress:
                on_progress()
            return
        # The model lost count, so no line can be trusted to belong to its block.
        # Throw the reply away and ask again with numbers, which say where each
        # translation goes.
        for b in blocks:
            b["en"] = ""
        self._batch_numbered(blocks, on_progress=on_progress, alive=alive)

    def _batch_numbered(self, blocks, on_progress=None, alive=None):
        """Fallback: numbers cost tokens but survive a model that drops a line."""
        prompt = BATCH_NUMBERED_PROMPT + "\n".join(
            f"{i + 1}. {b['text']}" for i, b in enumerate(blocks)
        )
        done = set()

        def apply(acc, final=False):
            lines = acc.split("\n")
            if not final:
                lines = lines[:-1]
            fresh = False
            for ln in lines:
                m = re.match(r"\s*(\d+)\s*[.．、:：)\]]\s*(.+)", ln)
                if not m:
                    continue
                i = int(m.group(1)) - 1
                if i in done or not (0 <= i < len(blocks)):
                    continue
                en = self._clean(m.group(2))
                if not en:
                    continue
                done.add(i)
                b = blocks[i]
                b["en"] = self._postfix(b["text"], en)
                self._remember(b["text"], b["en"])
                fresh = True
            if fresh and on_progress:
                on_progress()

        live = on_progress and self.cfg.get("stream", True)
        raw = self._call(prompt, on_delta=(apply if live else None), alive=alive)
        apply(raw or "", final=True)

    def refine_block(self, b, alive=None):
        """Second pass over one block with a stronger model. The fast model has
        already put English on screen; this quietly replaces it with better
        English a second later, and that better line is what gets cached."""
        model = self.cfg.get("refine_model") or ""
        if not model or self.backend not in ("ollama", "openai"):
            return False
        src = b["text"].strip()
        out = self.translate(src, alive=alive, model=model, fresh=True)
        if not out or out.startswith("[") or out == b.get("en"):
            return False
        b["en"] = out
        return True

    # -- raw backend calls -------------------------------------------------

    @staticmethod
    def _messages(text, examples=True):
        msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
        if examples:
            for zh, en in FEW_SHOT:
                msgs.append({"role": "user", "content": zh})
                msgs.append({"role": "assistant", "content": en})
        msgs.append({"role": "user", "content": text})
        return msgs

    def _call(self, text, on_delta=None, alive=None, model=None, examples=True):
        b = self.backend
        if b == "none":
            return text
        if b == "ollama":
            return self._ollama(text, on_delta, alive, model, examples)
        if b == "anthropic":
            return self._anthropic(text, on_delta, alive, model, examples)
        if b == "openai":
            return self._openai(text, on_delta, alive, model, examples)
        return text

    @staticmethod
    def _drain(resp, alive, pick):
        """Read a streamed response line by line, handing each decoded chunk to
        pick(); stops the moment alive() goes False, which closes the socket and
        frees the model for the text that is on screen now."""
        acc = []
        with resp as r:
            for raw in r:
                if alive is not None and not alive():
                    break
                line = raw.strip()
                if not line:
                    continue
                piece, done = pick(line)
                if piece:
                    acc.append(piece)
                if done:
                    break
        return "".join(acc)

    def _ollama(self, text, on_delta, alive, model=None, examples=True):
        url = self.cfg["ollama_url"] + "/api/chat"
        payload = {
            "model": model or self.cfg["ollama_model"],
            "messages": self._messages(text, examples),
            "stream": bool(on_delta),
            "keep_alive": self.cfg.get("keep_alive", "60m"),
            "options": {
                "temperature": 0.1,
                "top_p": 0.9,
                "top_k": 20,
                "repeat_penalty": 1.05,
                "num_ctx": int(self.cfg.get("num_ctx", 2048)),
                "num_predict": predict_budget(text),
            },
        }
        timeout = 180 if not self._warm else 45
        if not on_delta:
            r = _post_json(url, payload, timeout=timeout)
            self._warm = True
            return (r.get("message") or {}).get("content", "")

        acc = []

        def pick(line):
            try:
                obj = json.loads(line.decode("utf-8"))
            except Exception:
                return "", False
            piece = (obj.get("message") or {}).get("content", "")
            if piece:
                acc.append(piece)
                on_delta("".join(acc))
            return piece, bool(obj.get("done"))

        out = self._drain(_open_stream(url, payload, None, timeout), alive, pick)
        self._warm = True
        return out

    def _openai(self, text, on_delta, alive, model=None, examples=True):
        url = self.cfg["openai_url"] + "/chat/completions"
        h = {}
        if self.cfg.get("openai_key"):
            h["Authorization"] = "Bearer " + self.cfg["openai_key"]
        payload = {
            "model": model or self.cfg["openai_model"],
            "temperature": 0.1,
            "max_tokens": predict_budget(text),
            "stream": bool(on_delta),
            "messages": self._messages(text, examples),
        }
        if not on_delta:
            r = _post_json(url, payload, headers=h, timeout=60)
            return r["choices"][0]["message"]["content"]

        acc = []

        def pick(line):
            if not line.startswith(b"data:"):
                return "", False
            body = line[5:].strip()
            if body == b"[DONE]":
                return "", True
            try:
                obj = json.loads(body.decode("utf-8"))
            except Exception:
                return "", False
            piece = ((obj.get("choices") or [{}])[0].get("delta") or {}).get("content") or ""
            if piece:
                acc.append(piece)
                on_delta("".join(acc))
            return piece, False

        return self._drain(_open_stream(url, payload, h, 60), alive, pick)

    def _anthropic(self, text, on_delta, alive, model=None, examples=True):
        key = os.environ.get("ANTHROPIC_API_KEY", "")
        h = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        payload = {
            "model": model or self.cfg["anthropic_model"],
            "max_tokens": 1200,
            "system": SYSTEM_PROMPT,
            "stream": bool(on_delta),
            "messages": self._messages(text, examples)[1:],
        }
        if not on_delta:
            r = _post_json("https://api.anthropic.com/v1/messages", payload, headers=h)
            return "".join(c.get("text", "") for c in r.get("content", []))

        acc = []

        def pick(line):
            if not line.startswith(b"data:"):
                return "", False
            try:
                obj = json.loads(line[5:].strip().decode("utf-8"))
            except Exception:
                return "", False
            if obj.get("type") == "message_stop":
                return "", True
            piece = (obj.get("delta") or {}).get("text", "") \
                if obj.get("type") == "content_block_delta" else ""
            if piece:
                acc.append(piece)
                on_delta("".join(acc))
            return piece, False

        return self._drain(
            _open_stream("https://api.anthropic.com/v1/messages", payload, h, 60), alive, pick
        )


# ---------------------------------------------------------------- OCR worker

GRID_W, GRID_H = 64, 32          # change-detection grid over the capture region


def rects_overlap(b, box):
    x1, y1, x2, y2 = box
    return not (b["x2"] <= x1 or b["x1"] >= x2 or b["y2"] <= y1 or b["y1"] >= y2)


class OcrWorker(threading.Thread):
    def __init__(self, cfg, translator, out_q, status_q):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.translator = translator
        self.out_q = out_q
        self.status_q = status_q
        self.running = threading.Event()
        self.stop_flag = threading.Event()
        self.ocr = None
        self.target_hwnd = None
        self._last_small = None
        self._last_sig = None
        self.grabber = ScreenGrabber()
        self._blocks = []            # last scan, region-local coords
        self._quiet = 0              # scans in a row that turned up no new text
        self._px_cap = int(cfg.get("ocr_max_px", 0) or 0)
        self._scan_ms = []           # how long recent full reads took
        self._hot = None             # bbox of the last pixel change
        self._last_full = 0.0
        self._frame_size = None
        self._lock = threading.Lock()
        self._ocr_lock = threading.Lock()
        self._gen = 0
        # Daemon translation threads, not a pool: a model call must never keep
        # the app alive at exit, and closing the overlay has to be instant.
        self._jobs = queue.Queue()
        for i in range(max(1, int(cfg.get("translate_threads", 3)))):
            threading.Thread(
                target=self._job_loop, name=f"translate-{i}", daemon=True
            ).start()
        # The second pass gets its own thread, so a slow stronger model can
        # never delay the fast translation that is keeping up with the game.
        self._refine_jobs = queue.Queue()
        threading.Thread(target=self._refine_loop, name="refine", daemon=True).start()

    def _job_loop(self):
        while True:
            fn, arg = self._jobs.get()
            try:
                fn(arg)
            except Exception as e:
                self.status_q.put(("status", f"translate error: {type(e).__name__}: {e}"))

    def _refine_loop(self):
        while True:
            blocks, region, gen, b = self._refine_jobs.get()
            if self.stale(gen):
                continue
            try:
                if self.translator.refine_block(b, alive=lambda: not self.stale(gen)):
                    self.emit(blocks, region, gen)
            except Exception as e:
                self.status_q.put(("status", f"refine error: {type(e).__name__}: {e}"))

    # -- frame generations -------------------------------------------------
    #
    # Every screen change starts a new generation. Translations still in flight
    # from an older one are dropped rather than painted, so the overlay always
    # shows the text that is on screen now - and queued work for a screen that
    # has already moved on is skipped before it costs a model call, while work
    # already streaming is cut off mid-reply.

    def next_gen(self):
        with self._lock:
            self._gen += 1
            return self._gen

    def stale(self, gen):
        with self._lock:
            return gen != self._gen or self.stop_flag.is_set()

    def reset(self):
        self._last_small = None
        self._last_sig = None
        self._quiet = 0
        self._blocks = []
        self._hot = None
        self._frame_size = None
        self.next_gen()

    def resolve_target(self):
        """Current target window handle, re-finding it by title if the old
        handle went stale (e.g. the game was restarted)."""
        h = self.target_hwnd
        if h and user32.IsWindow(wintypes.HWND(h)) and user32.IsWindowVisible(wintypes.HWND(h)):
            return h
        title = self.cfg.get("target_title")
        if title:
            for hh, tt, _ in list_windows():
                if tt == title:
                    self.target_hwnd = hh
                    return hh
        return None

    def current_region(self):
        """(region, error). In window mode the region tracks the target window."""
        if self.cfg.get("mode") == "window":
            hwnd = self.resolve_target()
            if not hwnd:
                t = self.cfg.get("target_title") or "(none)"
                return None, f"target window not found: {t}"
            reg = window_region(hwnd)
            if not reg:
                return None, "target window minimized / offscreen"
            return reg, None
        reg = self.cfg.get("region")
        if not reg:
            return None, "no region set"
        return reg, None

    def ensure_ocr(self):
        """Load the OCR models. Called on startup from the UI thread too, so
        the first line after Start isn't paying for a cold load."""
        if self.ocr is not None:
            return
        with self._ocr_lock:
            if self.ocr is not None:
                return
            self.status_q.put(("status", "loading OCR models..."))
            from rapidocr_onnxruntime import RapidOCR

            # det_limit_type matters more than anything else here. The default
            # scales the SHORT side up to the limit, so a 1480x150 subtitle crop
            # became a 7000px-wide image: slower than the whole screen, and it
            # misread the text (0 of 2 lines exact, against 2 of 2 with "max").
            # Setting these after construction is silently ignored - the
            # preprocessing op is built from the constructor arguments.
            ocr = RapidOCR(
                intra_op_num_threads=int(self.cfg["ocr_threads"]),
                inter_op_num_threads=1,
                det_limit_side_len=float(self.cfg["ocr_side_len"]),
                det_limit_type="max",
                use_cls=bool(self.cfg.get("ocr_angle_cls", False)),
            )
            self.ocr = ocr
            self.status_q.put(("status", "OCR ready"))

    def run(self):
        while not self.stop_flag.is_set():
            if not self.running.is_set():
                time.sleep(0.15)
                continue
            try:
                self.tick()
            except Exception as e:
                self.status_q.put(("status", f"error: {type(e).__name__}: {e}"))
                time.sleep(1.0)
            time.sleep(float(self.cfg["poll_interval"]))

    # -- change detection --------------------------------------------------

    def changed(self, small, size):
        """Did the captured pixels move since the last look?

        A whole-region average is deaf to one new line appearing in a large
        window, which is exactly the case that has to feel instant - so a
        handful of strongly changed cells counts as a change too. Where those
        cells are is remembered: that is the only part worth reading again.
        """
        if self._last_small is None or self._last_small.shape != small.shape:
            self._last_small = small
            self._hot = None
            return True
        # Sparkles, embers, a breathing glow: an animation that never turns out
        # to be text would otherwise buy an OCR pass every poll forever. Each
        # scan that finds nothing new asks the next one to be more of a change,
        # and the first genuinely new line resets it - new text moves far more
        # pixels than a glow does.
        slack = 1.0 + min(self._quiet, 4)
        diff = np.abs(small - self._last_small)
        hot = diff >= float(self.cfg.get("local_delta", 16.0)) * slack
        if (float(diff.mean()) < float(self.cfg["change_threshold"]) * slack
                and int(hot.sum()) < int(self.cfg.get("local_cells", 2))):
            return False
        self._last_small = small
        self._hot = self._hot_box(hot, (small.shape[1], small.shape[0]), size)
        return True

    @staticmethod
    def _hot_box(hot, grid_size, size):
        """Bounding box of the changed cells, in region pixels."""
        ys, xs = np.nonzero(hot)
        if len(xs) == 0:
            return None
        w, h = size
        cw, ch = w / float(grid_size[0]), h / float(grid_size[1])
        return (
            max(0, int((int(xs.min()) - 1) * cw)),
            max(0, int((int(ys.min()) - 1) * ch)),
            min(w, int((int(xs.max()) + 2) * cw)),
            min(h, int((int(ys.max()) + 2) * ch)),
        )

    # -- reading the screen ------------------------------------------------

    def read(self, rgb):
        """OCR one image, shrinking it first if it is bigger than it needs to be.

        Detection already works on a scaled-down copy, but recognition runs on
        crops of whatever it is handed, so a full 1920px frame costs about 2.7x
        a 1280px one - measured on a real game frame, reading exactly the same
        text. Boxes come back in the coordinates of the image passed in.
        """
        h, w = rgb.shape[:2]
        limit = self._px_cap
        big = max(w, h)
        if limit and big > limit:
            f = limit / float(big)
            small = Image.fromarray(rgb).resize(
                (max(1, int(w * f)), max(1, int(h * f))), Image.LANCZOS
            )
            res, _ = self.ocr(np.asarray(small))
            back = 1.0 / f
            return [([[p[0] * back, p[1] * back] for p in it[0]], it[1], it[2])
                    for it in (res or [])]
        res, _ = self.ocr(rgb)
        return res or []

    def adapt(self, ms):
        """Trade resolution for responsiveness while the game is working hard.

        The same full read measured 0.8 s with the game idle and 6-7 s with it
        rendering flat out - the machine, not the code. Rather than sit at a
        resolution the machine cannot afford right now, back it off, and put it
        back when there is room again. Bounded by ocr_min_px / ocr_max_px, so it
        can never wander somewhere text stops being legible.
        """
        want = int(self.cfg.get("ocr_max_px", 0) or 0)
        if not want:
            return
        floor = max(480, int(self.cfg.get("ocr_min_px", 768)))
        self._scan_ms.append(ms)
        if len(self._scan_ms) > 4:
            self._scan_ms.pop(0)
        if len(self._scan_ms) < 3:
            return
        med = sorted(self._scan_ms)[len(self._scan_ms) // 2]
        old = self._px_cap
        if med > 1500 and self._px_cap > floor:
            self._px_cap = max(floor, int(self._px_cap * 0.8))
        elif med < 500 and self._px_cap < want:
            self._px_cap = min(want, int(self._px_cap * 1.25))
        if self._px_cap != old:
            self._scan_ms = []
            self.status_q.put(
                ("status", "reading at %dpx (full reads were %.0f ms)" % (self._px_cap, med))
            )

    def scan(self, rgb):
        """Read the text out of the capture, cropping to the part that actually
        changed when that is safe.

        OCR cost tracks pixels, and a new subtitle is a small fraction of a game
        window. Blocks outside the crop are carried over from the last scan -
        their pixels did not change, so neither did their text. A full read
        still happens on a timer and whenever the change is large, so nothing
        can quietly drift out of date.
        """
        h, w = rgb.shape[:2]
        crop = self._hot
        full = True
        if (self.cfg.get("crop_scan", True) and crop and self._blocks
                and (w, h) == self._frame_size
                and time.time() - self._last_full < float(self.cfg.get("full_scan_every", 4.0))
                and (crop[2] - crop[0]) * (crop[3] - crop[1]) < 0.35 * w * h):
            full = False

        t0 = time.time()
        conf, min_h = float(self.cfg["min_conf"]), int(self.cfg["min_box_h"])
        if full:
            blocks = group_boxes(self.read(rgb), conf, min_h)
            self._last_full = time.time()
        else:
            ox, oy = crop[0], crop[1]
            res = self.read(np.ascontiguousarray(rgb[crop[1]:crop[3], crop[0]:crop[2]]))
            res = [([[p[0] + ox, p[1] + oy] for p in it[0]], it[1], it[2])
                   for it in res]
            kept = [dict(b) for b in self._blocks if not rects_overlap(b, crop)]
            blocks = kept + group_boxes(res, conf, min_h)
        self._frame_size = (w, h)
        self._blocks = blocks
        ms = (time.time() - t0) * 1000
        if full:
            self.adapt(ms)
        return blocks, ms, full

    def emit(self, blocks, region, gen):
        """Push the frame as it currently stands to the overlay."""
        if self.stale(gen):
            return
        self.translator.fill_known(blocks)
        self.out_q.put(("blocks", [dict(b) for b in blocks], region, gen))

    def tick(self):
        region, err = self.current_region()
        if err:
            self.status_q.put(("status", err))
            self.emit([], region or [0, 0, 0, 0], self.next_gen())
            time.sleep(0.4)
            return
        x1, y1, x2, y2 = region
        if x2 - x1 < 8 or y2 - y1 < 8:
            return
        size = (x2 - x1, y2 - y1)
        if not self.changed(self.grabber.grid(region, GRID_W, GRID_H), size):
            return

        self.ensure_ocr()
        blocks, ocr_ms, full = self.scan(self.grabber.rgb(region))
        if not blocks:
            self._quiet = 0 if self._last_sig else self._quiet + 1
            self._last_sig = None
            self.emit([], region, self.next_gen())
            self.status_q.put(("status", f"no Chinese ({ocr_ms:.0f}ms)"))
            return

        sig = blocks_signature(blocks)
        if sig == self._last_sig:
            self._quiet += 1
            return
        self._last_sig = sig
        self._quiet = 0
        self.dispatch(blocks, region, self.next_gen(), ocr_ms, full)

    def dispatch(self, blocks, region, gen, ocr_ms, full=True):
        """Paint what is already known immediately, then fill the rest in as it
        arrives - a line that just appeared starts showing English while the
        model is still writing it, instead of waiting for the whole screen.

        Translation runs on the translation threads, so the capture loop keeps
        watching for the next change while the model works on this one.
        """
        todo = self.translator.prefill(blocks)
        self.emit(blocks, region, gen)
        scan = "full" if full else "crop"
        if not todo:
            self.status_q.put(
                ("status", f"OCR {ocr_ms:.0f}ms {scan} · {len(blocks)} blocks · cached")
            )
            return

        units = self.translator.plan_units(todo)
        self.status_q.put(
            ("status", f"OCR {ocr_ms:.0f}ms {scan} · {len(blocks)} blocks · "
                       f"translating {len(todo)} new")
        )
        t0 = time.time()
        left = [len(units)]
        last = [0.0]

        def alive():
            return not self.stale(gen)

        def progress():
            now = time.time()               # stream updates, not every token
            if now - last[0] >= 0.06:
                last[0] = now
                self.emit(blocks, region, gen)

        def work(unit):
            if self.stale(gen):
                return          # screen already moved on: never make the call
            try:
                self.translator.run_unit(unit, on_progress=progress, alive=alive)
            except Exception as e:
                self.status_q.put(("status", f"translate error: {type(e).__name__}: {e}"))
                return
            self.emit(blocks, region, gen)
            self.queue_refine(blocks, region, gen, unit)
            left[0] -= 1
            if left[0] <= 0 and not self.stale(gen):
                self.status_q.put(
                    ("status", f"OCR {ocr_ms:.0f}ms {scan} · translate "
                               f"{(time.time() - t0) * 1000:.0f}ms · {len(blocks)} blocks "
                               f"[{self.translator.backend}]")
                )

        for u in units:
            self._jobs.put((work, u))

    def queue_refine(self, blocks, region, gen, unit):
        """Hand the prose in this unit to the stronger model, if one is on."""
        if not (self.cfg.get("refine") and self.cfg.get("refine_model")):
            return
        if self._refine_jobs.qsize() > 8:
            return              # already behind; the fast translation stands
        for b in unit:
            if b.get("en") and self.translator.is_prose(b):
                self._refine_jobs.put((blocks, region, gen, b))


# ---------------------------------------------------------------- region picker

class RegionPicker:
    def __init__(self, root, on_done):
        self.on_done = on_done
        self.top = tk.Toplevel(root)
        self.top.attributes("-fullscreen", True)
        self.top.attributes("-topmost", True)
        self.top.attributes("-alpha", 0.28)
        self.top.configure(bg="black", cursor="crosshair")
        self.canvas = tk.Canvas(self.top, bg="black", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_text(
            self.top.winfo_screenwidth() // 2, 60,
            text="Drag over the game's text area (a whole menu is fine).   Esc to cancel.",
            fill="white", font=("Segoe UI", 22),
        )
        self.rect = None
        self.canvas.bind("<Button-1>", self.down)
        self.canvas.bind("<B1-Motion>", self.move)
        self.canvas.bind("<ButtonRelease-1>", self.up)
        self.top.bind("<Escape>", lambda e: self.cancel())
        self.top.focus_force()

    def down(self, e):
        self.start = (e.x_root, e.y_root)
        self.anchor = (e.x, e.y)
        if self.rect:
            self.canvas.delete(self.rect)
        self.rect = self.canvas.create_rectangle(e.x, e.y, e.x, e.y, outline="#7fd1ff", width=3)

    def move(self, e):
        if self.rect:
            ax, ay = self.anchor
            self.canvas.coords(self.rect, ax, ay, e.x, e.y)

    def up(self, e):
        if not getattr(self, "start", None):
            return self.cancel()
        x1, y1 = self.start
        x2, y2 = e.x_root, e.y_root
        box = [min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)]
        self.top.destroy()
        if box[2] - box[0] > 10 and box[3] - box[1] > 10:
            self.on_done(box)

    def cancel(self):
        try:
            self.top.destroy()
        except Exception:
            pass


# ---------------------------------------------------------------- overlay

TKEY = "#0a0b0c"  # color-key painted transparent + click-through


class Overlay:
    """One transparent window covering the capture region; draws a positioned
    label per translated block (or a single panel when combine is on)."""

    DONE_FG = "#f4ecda"
    PENDING_FG = "#8a8474"   # the Chinese, dimmed, while its English is in flight

    def __init__(self, root, cfg):
        self.cfg = cfg
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg=TKEY)
        try:
            self.win.attributes("-transparentcolor", TKEY)
        except Exception:
            pass
        self.win.attributes("-alpha", float(cfg["opacity"]))
        self.labels = {}
        self.win.geometry("400x120+200+200")
        self.win.update_idletasks()
        make_click_through(self.hwnd(), True)
        exclude_from_capture(self.hwnd())
        self.win.withdraw()

    def hwnd(self):
        return user32.GetParent(self.win.winfo_id()) or self.win.winfo_id()

    def clear(self):
        for lb in self.labels.values():
            lb.destroy()
        self.labels = {}

    def _mk(self, text, px, wrap, fg):
        return tk.Label(
            self.win, text=text, bg="#0b0b0f", fg=fg,
            font=("Georgia", -px), justify="left", wraplength=wrap,
            padx=6, pady=2, bd=0,
        )

    def show_blocks(self, blocks, region):
        x0, y0, x1r, y1r = region
        w, h = x1r - x0, y1r - y0
        self.win.geometry(f"{w}x{h}+{x0}+{y0}")

        base = int(self.cfg["font_size"])
        specs = []
        if self.cfg.get("combine_box"):
            joined = "\n".join(
                (("· " + b.get("en", "")) if b["n"] == 1 else b.get("en", ""))
                for b in blocks if b.get("en")
            )
            if joined:
                specs.append(("panel", joined, base, 8, 8, max(240, w // 3), False))
        else:
            for b in blocks:
                en = b.get("en", "")
                pending = not en
                if pending:
                    # Nothing back from the model yet: show the Chinese right
                    # away so a new line is on screen the instant it appears.
                    if not self.cfg.get("show_pending", True):
                        continue
                    txt = b["text"]
                elif self.cfg.get("show_source"):
                    txt = b["text"] + "\n" + en
                else:
                    txt = en
                bw = b["x2"] - b["x1"]
                line_h = b["h"] / max(1, b["n"])
                px = max(11, min(base, int(line_h * 0.9)))
                # Block coords are already relative to the captured region,
                # and this window covers exactly that region.
                x, y = max(0, b["x1"]), max(0, b["y1"])
                specs.append(
                    ((round(x / 6), round(y / 6)), txt, px, x, y, max(80, bw + 40), pending)
                )
        self._sync(specs)
        make_click_through(self.hwnd(), True)

    def _sync(self, specs):
        """Reconcile the drawn labels against the frame. Only what actually
        changed is touched, so streaming one translation in at a time updates
        that one label instead of flickering the whole overlay."""
        keep = {}
        for key, text, px, x, y, wrap, pending in specs:
            while key in keep:                      # two blocks in the same spot
                key = (key, "+")
            lb = self.labels.pop(key, None)
            fg = self.PENDING_FG if pending else self.DONE_FG
            state = (text, px, wrap, fg)
            if lb is None:
                lb = self._mk(text, px, wrap, fg)
                lb.place(x=x, y=y)
                lb._pos = (x, y)
            else:
                if lb._state != state:
                    lb.config(text=text, font=("Georgia", -px), wraplength=wrap, fg=fg)
                if lb._pos != (x, y):
                    lb.place(x=x, y=y)
                    lb._pos = (x, y)
            lb._state = state
            keep[key] = lb
        for lb in self.labels.values():
            lb.destroy()
        self.labels = keep

    def show(self):
        self.win.deiconify()
        self.win.attributes("-topmost", True)

    def hide(self):
        self.win.withdraw()

    def set_opacity(self, v):
        self.win.attributes("-alpha", float(v))


# ---------------------------------------------------------------- app

class App:
    def __init__(self):
        self.cfg = load_config()
        self.root = tk.Tk()
        self.root.title("LotM Translation Overlay")
        self.root.geometry("480x400")
        self.root.attributes("-topmost", True)
        try:
            self.root.tk.call("tk", "scaling", 1.0)
        except Exception:
            pass

        self.translator = Translator(self.cfg)
        self.out_q = queue.Queue()
        self.status_q = queue.Queue()
        self.worker = OcrWorker(self.cfg, self.translator, self.out_q, self.status_q)
        self.worker.start()

        self.overlay = Overlay(self.root, self.cfg)
        self._last_gen = 0
        self.build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        self.pump()
        self.warm_up_async()

    # -- setup helpers -----------------------------------------------------

    def warm_up_async(self):
        def work():
            self.status_q.put(("status", f"warming up {self.translator.backend}..."))
            t0 = time.time()
            self.translator.warm_up()
            self.status_q.put(
                ("status", f"ready [{self.translator.backend}] (warm-up {time.time() - t0:.1f}s)")
            )
        threading.Thread(target=work, daemon=True).start()
        # Load the OCR models now rather than on the first changed frame.
        threading.Thread(target=self.worker.ensure_ocr, daemon=True).start()

    def build_ui(self):
        pad = {"padx": 8, "pady": 4}
        f = ttk.Frame(self.root, padding=10)
        f.pack(fill="both", expand=True)

        ttk.Button(f, text="1 · Select window to translate", command=self.pick_window
                   ).grid(row=0, column=0, columnspan=2, sticky="ew", **pad)
        self.region_lbl = ttk.Label(f, text=self.region_text(), foreground="#666")
        self.region_lbl.grid(row=1, column=0, columnspan=2, sticky="w", **pad)
        ttk.Button(f, text="…or drag a screen region instead", command=self.pick_region
                   ).grid(row=13, column=0, columnspan=2, sticky="ew", **pad)

        self.btn_toggle = ttk.Button(f, text="2 · Start", command=self.toggle)
        self.btn_toggle.grid(row=2, column=0, columnspan=2, sticky="ew", **pad)

        ttk.Label(f, text="Translator").grid(row=3, column=0, sticky="w", **pad)
        self.backend_var = tk.StringVar(value=self.translator.backend)
        bb = ttk.Combobox(f, textvariable=self.backend_var, state="readonly",
                          values=["ollama", "anthropic", "openai", "none"], width=14)
        bb.grid(row=3, column=1, sticky="ew", **pad)
        bb.bind("<<ComboboxSelected>>", self.change_backend)

        ttk.Label(f, text="Model").grid(row=4, column=0, sticky="w", **pad)
        self.model_var = tk.StringVar(value=self.cfg["ollama_model"])
        self.model_box = ttk.Combobox(f, textvariable=self.model_var, width=22)
        self.model_box.grid(row=4, column=1, sticky="ew", **pad)
        self.model_box.bind("<<ComboboxSelected>>", self.change_model)
        self.model_box.bind("<Return>", self.change_model)
        self.refresh_models()

        ttk.Button(f, text="Test translator", command=self.test_translator
                   ).grid(row=5, column=0, columnspan=2, sticky="ew", **pad)

        ttk.Label(f, text="Text size").grid(row=6, column=0, sticky="w", **pad)
        self.font_var = tk.IntVar(value=int(self.cfg["font_size"]))
        ttk.Scale(f, from_=11, to=30, variable=self.font_var,
                  command=lambda v: self.cfg.update(font_size=self.font_var.get())
                  ).grid(row=6, column=1, sticky="ew", **pad)

        ttk.Label(f, text="Opacity").grid(row=7, column=0, sticky="w", **pad)
        self.op_var = tk.DoubleVar(value=float(self.cfg["opacity"]))
        ttk.Scale(f, from_=0.4, to=1.0, variable=self.op_var,
                  command=lambda v: self.overlay.set_opacity(self.op_var.get())
                  ).grid(row=7, column=1, sticky="ew", **pad)

        self.combine_var = tk.BooleanVar(value=bool(self.cfg["combine_box"]))
        ttk.Checkbutton(f, text="Combine into one panel (instead of positioned labels)",
                        variable=self.combine_var, command=self.toggle_combine
                        ).grid(row=8, column=0, columnspan=2, sticky="w", **pad)

        self.src_var = tk.BooleanVar(value=bool(self.cfg["show_source"]))
        ttk.Checkbutton(f, text="Show original Chinese too",
                        variable=self.src_var, command=self.toggle_src
                        ).grid(row=9, column=0, columnspan=2, sticky="w", **pad)

        self.pending_var = tk.BooleanVar(value=bool(self.cfg.get("show_pending", True)))
        ttk.Checkbutton(f, text="Show new Chinese instantly, before its translation lands",
                        variable=self.pending_var, command=self.toggle_pending
                        ).grid(row=10, column=0, columnspan=2, sticky="w", **pad)

        self.refine_var = tk.BooleanVar(value=bool(self.cfg.get("refine")))
        ttk.Checkbutton(f, text="Second pass: re-translate prose with a stronger model",
                        variable=self.refine_var, command=self.toggle_refine
                        ).grid(row=11, column=0, columnspan=2, sticky="w", **pad)

        self.status = ttk.Label(f, text="idle", foreground="#333", wraplength=440)
        self.status.grid(row=12, column=0, columnspan=2, sticky="w", **pad)
        f.columnconfigure(1, weight=1)

    # -- ui state ----------------------------------------------------------

    def region_text(self):
        if self.cfg.get("mode") == "window":
            t = self.cfg.get("target_title")
            return f"target window: {t}" if t else "target window: not selected"
        r = self.cfg.get("region")
        return f"region: {r[0]},{r[1]} → {r[2]},{r[3]}" if r else "region: not set"

    def pick_window(self):
        wins = list_windows()
        if not wins:
            self.status.config(text="no windows found")
            return
        top = tk.Toplevel(self.root)
        top.title("Select window")
        top.attributes("-topmost", True)
        top.geometry("560x380")
        ttk.Label(top, text="Pick the window to translate (the game must be windowed "
                            "or borderless, not exclusive fullscreen):",
                  wraplength=540).pack(anchor="w", padx=10, pady=(10, 4))
        lb = tk.Listbox(top, activestyle="dotbox")
        lb.pack(fill="both", expand=True, padx=10, pady=6)

        # Surface likely game windows first.
        def score(t):
            t = t.lower()
            return 0 if any(k in t for k in ("诡秘", "lord of", "c7-win64", "mysteries")) else 1
        wins.sort(key=lambda w: (score(w[1]), w[1].lower()))
        for _h, t, rect in wins:
            w, h = rect[2] - rect[0], rect[3] - rect[1]
            lb.insert("end", f"{t}   [{w}x{h}]")
        lb.selection_set(0)

        def choose():
            sel = lb.curselection()
            if not sel:
                return
            hwnd, title, _rect = wins[sel[0]]
            self.set_target_window(hwnd, title)
            top.destroy()

        row = ttk.Frame(top)
        row.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(row, text="Use this window", command=choose).pack(side="right")
        ttk.Button(row, text="Refresh", command=lambda: (top.destroy(), self.pick_window())
                   ).pack(side="right", padx=6)
        lb.bind("<Double-Button-1>", lambda e: choose())
        top.focus_force()

    def set_target_window(self, hwnd, title):
        self.cfg["mode"] = "window"
        self.cfg["target_title"] = title
        self.worker.target_hwnd = hwnd
        self.worker.reset()
        self.region_lbl.config(text=self.region_text())
        save_config(self.cfg)

    def refresh_models(self):
        if self.translator.backend == "ollama":
            models = self.translator.available_ollama_models()
            self.model_box["values"] = models
            if models and self.cfg["ollama_model"] not in models:
                self.model_var.set(models[0])
                self.cfg["ollama_model"] = models[0]

    def change_backend(self, _=None):
        self.translator.save_memory()
        self.translator.backend = self.backend_var.get()
        self.cfg["backend"] = self.backend_var.get()
        self.translator.forget()
        self.translator._warm = False
        self.worker.reset()
        self.refresh_models()
        save_config(self.cfg)
        self.warm_up_async()

    def change_model(self, _=None):
        m = self.model_var.get().strip()
        self.translator.save_memory()
        if self.translator.backend == "ollama":
            self.cfg["ollama_model"] = m
        else:
            self.cfg["openai_model"] = m
        self.translator.forget()
        self.translator._warm = False
        self.worker.reset()
        save_config(self.cfg)

    def toggle_combine(self):
        self.cfg["combine_box"] = self.combine_var.get()
        self.worker.reset()
        save_config(self.cfg)

    def toggle_src(self):
        self.cfg["show_source"] = self.src_var.get()
        self.worker.reset()
        save_config(self.cfg)

    def toggle_pending(self):
        self.cfg["show_pending"] = self.pending_var.get()
        self.worker.reset()
        save_config(self.cfg)

    def toggle_refine(self):
        """The fast model keeps up with the game; the second pass quietly
        replaces its prose with better English a second later."""
        on = self.refine_var.get()
        if on and not self.cfg.get("refine_model"):
            pick = self.translator.pick_refine_model()
            if not pick:
                self.refine_var.set(False)
                self.status.config(
                    text="no stronger model installed - try: ollama pull qwen2.5:7b-instruct"
                )
                return
            self.cfg["refine_model"] = pick
        self.cfg["refine"] = on
        if on:
            self.status.config(text=f"second pass: {self.cfg['refine_model']}")
        save_config(self.cfg)

    def test_translator(self):
        self.overlay.show()
        self.status.config(text=f"testing [{self.translator.backend}]...")

        def work():
            blocks = [
                {"x1": 40, "y1": 30, "x2": 240, "y2": 66, "h": 36, "n": 1, "lines": ["开始游戏"], "text": "开始游戏"},
                {"x1": 40, "y1": 90, "x2": 200, "y2": 126, "h": 36, "n": 1, "lines": ["设置"], "text": "设置"},
                {"x1": 40, "y1": 160, "x2": 700, "y2": 240, "h": 80, "n": 2,
                 "lines": ["“我是死神。”他低声说道。", "灰雾在他眼前浮现。"],
                 "text": "“我是死神。”他低声说道。\n灰雾在他眼前浮现。"},
            ]
            gen = self.worker.next_gen()
            region = [0, 0, 760, 300]

            def paint():
                self.out_q.put(("blocks", [dict(b) for b in blocks], region, gen))

            paint()
            t0 = time.time()
            first = [0.0]
            for unit in self.translator.plan_units(self.translator.prefill(blocks)):
                def progress(_u=unit):
                    if not first[0]:
                        first[0] = time.time() - t0
                    paint()
                self.translator.run_unit(unit, on_progress=progress)
            paint()
            ms = (time.time() - t0) * 1000
            self.status_q.put(
                ("status", f"test: first English {first[0] * 1000:.0f}ms · "
                           f"all {ms:.0f}ms [{self.translator.backend}]")
            )

        threading.Thread(target=work, daemon=True).start()

    # -- region / run ------------------------------------------------------

    def pick_region(self):
        self.root.iconify()
        self.root.after(220, lambda: RegionPicker(self.root, self.region_done))

    def region_done(self, box):
        self.cfg["mode"] = "region"
        self.cfg["region"] = box
        self.worker.reset()
        self.region_lbl.config(text=self.region_text())
        save_config(self.cfg)
        self.root.deiconify()

    def toggle(self):
        if self.worker.running.is_set():
            self.worker.running.clear()
            self.overlay.hide()
            self.overlay.clear()
            self.btn_toggle.config(text="2 · Start")
            self.status.config(text="stopped")
        else:
            if self.cfg.get("mode") == "window" and not self.cfg.get("target_title"):
                self.status.config(text="select a window first")
                return
            if self.cfg.get("mode") == "region" and not self.cfg.get("region"):
                self.status.config(text="select a region first")
                return
            self.worker.reset()
            self.worker.running.set()
            self.overlay.show()
            self.btn_toggle.config(text="2 · Stop")

    def pump(self):
        try:
            while True:
                kind, *rest = self.status_q.get_nowait()
                if kind == "status":
                    self.status.config(text=rest[0])
        except queue.Empty:
            pass
        latest = None
        try:
            while True:
                kind, blocks, region, gen = self.out_q.get_nowait()
                if gen >= self._last_gen:       # ignore frames the screen left behind
                    self._last_gen = gen
                    latest = (blocks, region)
        except queue.Empty:
            pass
        if latest is not None:
            self.overlay.show_blocks(*latest)
        self.root.after(30, self.pump)

    def quit(self):
        self.cfg["font_size"] = int(self.font_var.get())
        self.cfg["opacity"] = float(self.op_var.get())
        save_config(self.cfg)
        self.translator.save_memory()
        self.worker.stop_flag.set()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    """--start begins translating as soon as the window is up, for launching
    it from a shortcut or a script without reaching for the mouse."""
    enable_dpi_awareness()
    app = App()
    if "--start" in sys.argv[1:]:
        app.root.after(500, app.toggle)
    app.run()


if __name__ == "__main__":
    main()
