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
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
from ctypes import wintypes
from tkinter import ttk

import numpy as np
from PIL import ImageGrab

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")

# ---------------------------------------------------------------- DPI / Win32

user32 = ctypes.windll.user32


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
    "poll_interval": 0.4,
    "change_threshold": 3.0,
    "ocr_side_len": 736,
    "ocr_threads": 4,
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

def rect_of(box):
    xs = [p[0] for p in box]
    ys = [p[1] for p in box]
    return int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))


def group_boxes(results, min_conf=0.55, min_h=10):
    """Cluster OCR line-boxes into positioned text blocks.

    PP-OCR already returns one box per text line. We merge lines that sit
    directly under each other with a similar width (a paragraph) and keep
    everything else separate, so scattered UI labels stay independent instead
    of being concatenated into one blob.
    """
    boxes = []
    for item in (results or []):
        box, txt, conf = item[0], item[1], item[2]
        if conf is not None and conf < min_conf:
            continue
        t = (txt or "").strip()
        if not t:
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

SYSTEM_PROMPT = (
    "You translate Chinese text from the game 'Lord of the Mysteries' into natural English.\n"
    "Output ONLY the translation: no notes, no pinyin, no original text, no restating these rules.\n"
    "\n"
    "Decide per item which it is:\n"
    "1. UI TEXT - a short label with no sentence punctuation (buttons, menus, tabs, "
    "stats). Use the plain conventional English a game prints on that control, 2-3 words "
    "max, never a sentence. 开始游戏->Start Game. 设置->Settings. 背包->Inventory. "
    "退出游戏->Quit Game. 确定->Confirm. 立即注册->Register.\n"
    "2. NARRATIVE - dialogue or prose with sentence punctuation. Translate as Victorian "
    "gothic mystery fiction: natural, literary English. Keep line breaks and speaker "
    "names. Use natural articles ('I am Death', not 'I am the Death').\n"
    "\n"
    "If an item is garbled OCR noise, output nothing for it.\n"
    "\n"
    "CRITICAL, never swap these:\n"
    "  序列 = Sequence (numbered rank, 序列9 -> Sequence 9)\n"
    "  途径 = Pathway (branch/lineage, 愚者途径 -> Fool Pathway)\n"
    "\n"
    "Established terms: " + GLOSSARY
)


def _post_json(url, payload, headers=None, timeout=25):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


class Translator:
    def __init__(self, cfg):
        self.cfg = cfg
        self.cache = {}
        self._warm = False
        self.backend = cfg["backend"]
        if self.backend == "auto":
            self.backend = self.detect()

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

    def warm_up(self):
        if self.backend in ("none", "anthropic", "openai"):
            return
        try:
            self.translate("开始")
        except Exception:
            pass

    # -- terminology fixups ------------------------------------------------

    # Small models sometimes regurgitate the instructions instead of translating.
    _ECHO = (
        "Sequence = Sequence", "numbered rank", "branch/lineage", "Established terms",
        "one translation per line", "UI TEXT", "NARRATIVE", "Translate each",
    )

    @classmethod
    def _clean(cls, out):
        if not out:
            return ""
        low = out
        for bad in cls._ECHO:
            if bad in low:
                return ""
        return out.strip()

    @staticmethod
    def _has_cjk(s):
        return bool(re.search("[一-鿿]", s))

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

    def translate(self, text):
        text = text.strip()
        if not text:
            return ""
        if text in self.cache:
            return self.cache[text]
        try:
            out = self._call(text)
        except urllib.error.HTTPError as e:
            return f"[{self.backend} HTTP {e.code}]"
        except Exception as e:
            return f"[{self.backend} {type(e).__name__}]"
        out = self._postfix(text, self._clean(out))
        self._remember(text, out)
        return out

    def _remember(self, key, val):
        self.cache[key] = val
        if len(self.cache) > 1200:
            for k in list(self.cache)[:300]:
                self.cache.pop(k, None)

    # -- block translate (batched) ----------------------------------------

    def translate_blocks(self, blocks):
        """Fill each block's ['en']. Short single-line labels are batched into
        one request for speed; prose (multi-line, or a single long line) goes
        individually so line breaks and quality survive. Blocks with no Chinese
        (version numbers, logos) pass through untranslated rather than being
        hallucinated into nonsense."""
        shorts, prose = [], []
        for b in blocks:
            if not self._has_cjk(b["text"]):
                b["en"] = b["text"]          # leave non-Chinese as-is
            elif "\n" in b["text"] or len(b["text"]) > 36:
                prose.append(b)
            else:
                shorts.append(b)

        uncached = [b for b in shorts if b["text"] not in self.cache]
        if len(uncached) >= 2 and self.backend != "none":
            try:
                self._batch(uncached)
            except Exception:
                pass  # fall back to per-item below

        for b in shorts:
            b["en"] = self.cache.get(b["text"]) or self.translate(b["text"])
        for b in prose:
            b["en"] = self.translate(b["text"])
        return blocks

    def _batch(self, blocks):
        body = "\n".join(f"{i + 1}. {b['text']}" for i, b in enumerate(blocks))
        prompt = (
            "Below are separate text items, one per numbered line. Translate each to "
            "English and reply with the SAME numbers, one translation per line, nothing "
            "else:\n" + body
        )
        raw = self._call(prompt)
        got = {}
        for line in (raw or "").splitlines():
            m = re.match(r"\s*(\d+)\s*[.．、:：)\]]\s*(.+)", line)
            if m:
                got[int(m.group(1))] = m.group(2).strip()
        for i, b in enumerate(blocks):
            en = self._clean(got.get(i + 1, ""))
            if en:
                self._remember(b["text"], self._postfix(b["text"], en))

    # -- raw backend call --------------------------------------------------

    def _call(self, text):
        b = self.backend
        if b == "none":
            return text
        if b == "ollama":
            r = _post_json(
                self.cfg["ollama_url"] + "/api/chat",
                {
                    "model": self.cfg["ollama_model"],
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": text},
                    ],
                    "stream": False,
                    "keep_alive": "30m",
                    "options": {"temperature": 0.2, "num_predict": 400},
                },
                timeout=180 if not self._warm else 40,
            )
            self._warm = True
            return r.get("message", {}).get("content", "")
        if b == "anthropic":
            key = os.environ.get("ANTHROPIC_API_KEY", "")
            r = _post_json(
                "https://api.anthropic.com/v1/messages",
                {
                    "model": self.cfg["anthropic_model"],
                    "max_tokens": 1200,
                    "system": SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": text}],
                },
                headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
            )
            return "".join(c.get("text", "") for c in r.get("content", []))
        if b == "openai":
            h = {}
            if self.cfg.get("openai_key"):
                h["Authorization"] = "Bearer " + self.cfg["openai_key"]
            r = _post_json(
                self.cfg["openai_url"] + "/chat/completions",
                {
                    "model": self.cfg["openai_model"],
                    "temperature": 0.2,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": text},
                    ],
                },
                headers=h,
            )
            return r["choices"][0]["message"]["content"]
        return text


# ---------------------------------------------------------------- OCR worker

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

    def reset(self):
        self._last_small = None
        self._last_sig = None

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
        if self.ocr is not None:
            return
        self.status_q.put(("status", "loading OCR models..."))
        from rapidocr_onnxruntime import RapidOCR

        self.ocr = RapidOCR(
            intra_op_num_threads=int(self.cfg["ocr_threads"]),
            inter_op_num_threads=1,
        )
        try:
            self.ocr.text_det.limit_side_len = int(self.cfg["ocr_side_len"])
        except Exception:
            pass
        self.status_q.put(("status", "ready"))

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

    def tick(self):
        region, err = self.current_region()
        if err:
            self.status_q.put(("status", err))
            self.out_q.put(("blocks", [], region or [0, 0, 0, 0]))
            time.sleep(0.4)
            return
        x1, y1, x2, y2 = region
        if x2 - x1 < 8 or y2 - y1 < 8:
            return
        img = ImageGrab.grab(bbox=(x1, y1, x2, y2), all_screens=True)

        small = np.asarray(img.convert("L").resize((48, 24))).astype(np.float32)
        if self._last_small is not None:
            if float(np.abs(small - self._last_small).mean()) < float(self.cfg["change_threshold"]):
                return
        self._last_small = small

        self.ensure_ocr()
        t0 = time.time()
        res, _ = self.ocr(np.asarray(img.convert("RGB")))
        ocr_ms = (time.time() - t0) * 1000

        blocks = group_boxes(
            res, float(self.cfg["min_conf"]), int(self.cfg["min_box_h"])
        )
        if not blocks:
            self.out_q.put(("blocks", [], region))
            self.status_q.put(("status", f"no text ({ocr_ms:.0f}ms)"))
            return

        sig = blocks_signature(blocks)
        if sig == self._last_sig:
            return
        self._last_sig = sig

        self.status_q.put(("status", f"OCR {ocr_ms:.0f}ms · {len(blocks)} blocks · translating"))
        t1 = time.time()
        self.translator.translate_blocks(blocks)
        tr_ms = (time.time() - t1) * 1000
        self.out_q.put(("blocks", blocks, region))
        self.status_q.put(
            ("status", f"OCR {ocr_ms:.0f}ms · translate {tr_ms:.0f}ms · "
                       f"{len(blocks)} blocks [{self.translator.backend}]")
        )


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
        self.labels = []
        self.win.geometry("400x120+200+200")
        self.win.update_idletasks()
        make_click_through(self.hwnd(), True)
        exclude_from_capture(self.hwnd())
        self.win.withdraw()

    def hwnd(self):
        return user32.GetParent(self.win.winfo_id()) or self.win.winfo_id()

    def clear(self):
        for lb in self.labels:
            lb.destroy()
        self.labels = []

    def _mk(self, text, px, wrap):
        return tk.Label(
            self.win, text=text, bg="#0b0b0f", fg="#f4ecda",
            font=("Georgia", -px), justify="left", wraplength=wrap,
            padx=6, pady=2, bd=0,
        )

    def show_blocks(self, blocks, region):
        self.clear()
        x0, y0, x1r, y1r = region
        w, h = x1r - x0, y1r - y0
        self.win.geometry(f"{w}x{h}+{x0}+{y0}")

        base = int(self.cfg["font_size"])
        if self.cfg.get("combine_box"):
            joined = "\n".join(
                (("· " + b.get("en", "")) if b["n"] == 1 else b.get("en", ""))
                for b in blocks if b.get("en")
            )
            lb = self._mk(joined, base, max(240, w // 3))
            lb.place(x=8, y=8)
            self.labels.append(lb)
        else:
            for b in blocks:
                en = b.get("en", "")
                if not en:
                    continue
                bw = b["x2"] - b["x1"]
                line_h = b["h"] / max(1, b["n"])
                px = max(11, min(base, int(line_h * 0.9)))
                txt = en
                if self.cfg.get("show_source"):
                    txt = b["text"] + "\n" + en
                lb = self._mk(txt, px, max(80, bw + 40))
                lb.place(x=max(0, b["x1"] - x0), y=max(0, b["y1"] - y0))
                self.labels.append(lb)
        make_click_through(self.hwnd(), True)

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

    def build_ui(self):
        pad = {"padx": 8, "pady": 4}
        f = ttk.Frame(self.root, padding=10)
        f.pack(fill="both", expand=True)

        ttk.Button(f, text="1 · Select window to translate", command=self.pick_window
                   ).grid(row=0, column=0, columnspan=2, sticky="ew", **pad)
        self.region_lbl = ttk.Label(f, text=self.region_text(), foreground="#666")
        self.region_lbl.grid(row=1, column=0, columnspan=2, sticky="w", **pad)
        ttk.Button(f, text="…or drag a screen region instead", command=self.pick_region
                   ).grid(row=11, column=0, columnspan=2, sticky="ew", **pad)

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

        self.status = ttk.Label(f, text="idle", foreground="#333", wraplength=440)
        self.status.grid(row=10, column=0, columnspan=2, sticky="w", **pad)
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
        self.translator.backend = self.backend_var.get()
        self.cfg["backend"] = self.backend_var.get()
        self.translator.cache.clear()
        self.translator._warm = False
        self.worker.reset()
        self.refresh_models()
        save_config(self.cfg)
        self.warm_up_async()

    def change_model(self, _=None):
        m = self.model_var.get().strip()
        if self.translator.backend == "ollama":
            self.cfg["ollama_model"] = m
        else:
            self.cfg["openai_model"] = m
        self.translator.cache.clear()
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
            t0 = time.time()
            self.translator.translate_blocks(blocks)
            ms = (time.time() - t0) * 1000
            self.out_q.put(("blocks", blocks, [0, 0, 760, 300]))
            self.status_q.put(("status", f"test {ms:.0f}ms [{self.translator.backend}]"))

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
        try:
            while True:
                kind, blocks, region = self.out_q.get_nowait()
                self.overlay.show_blocks(blocks, region)
        except queue.Empty:
            pass
        self.root.after(60, self.pump)

    def quit(self):
        self.cfg["font_size"] = int(self.font_var.get())
        self.cfg["opacity"] = float(self.op_var.get())
        save_config(self.cfg)
        self.worker.stop_flag.set()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    enable_dpi_awareness()
    App().run()


if __name__ == "__main__":
    main()
