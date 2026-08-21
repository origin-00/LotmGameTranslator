# Lord of the Mysteries — live translation overlay

Reads Chinese text off a region of your screen, translates it, and draws each English
translation **positioned over its own piece of text** in a click-through window. A
cluttered menu becomes a set of labels over their own buttons, not one jumbled block.

**It never touches the game's files, memory, or process.** It only looks at pixels
already on screen, exactly like a screenshot tool. That's deliberate: the game ships
with ACE kernel anti-cheat, and anything that modified game data would risk your
account. This does not.

## Run it

```
python lotm_overlay.py
```

## First-time setup

1. **Select window to translate** — pick the game from the list (it's surfaced at
   the top). The overlay then tracks that window: if you move or resize it, the
   capture follows automatically. Text inside is grouped and translated per element.
   *Or* use "…or drag a screen region instead" to capture a fixed rectangle.
2. **Start.**
3. Play. When the text changes, the labels update in place.

The overlay excludes itself from screen capture, so it never re-reads its own
English labels.

Use **Test translator** at any point to push a known line through the pipeline and
confirm it's working, without needing the game open.

## Important: run the game in Borderless or Windowed mode

Nothing can draw over exclusive fullscreen. If the overlay is invisible, this is
almost always why.

## Controls

| Control | What it does |
| --- | --- |
| Select text region | Re-drag the capture box |
| Translator | `ollama` (local, free) · `anthropic` (API key) · `openai` (any compatible endpoint) · `none` (show Chinese only) |
| Model | Which model to translate with |
| Text size / Opacity | Overlay appearance. Labels auto-shrink to fit small UI text; Text size is the cap |
| Combine into one panel | Off = a label over each element (default). On = all translations stacked in one box at the top-left — better for pure dialogue |
| Show original Chinese too | Shows the OCR text above each translation — useful for spotting misreads |

The overlay is always click-through, so your clicks go to the game.

Settings persist to `config.json` next to the script.

## Current setup

- **OCR**: RapidOCR (PP-OCR), runs locally on CPU, ~0.5–1.3s per scan
- **Translation**: `qwen2.5:3b-instruct` via Ollama, ~200–350ms per line, fully offline

Measured together: roughly **1–1.5s** from new text appearing to English on screen.

OCR only runs when the pixels in your region actually change, so idle scenes cost
nothing. Repeated lines are cached and return instantly.

## Translation quality

The prompt carries a series glossary (Sequence, Pathway, Beyonder, Klein Moretti,
Tarot Club, and so on) so the recurring terminology comes out right rather than
being literally translated. It also distinguishes UI labels from prose:

```
开始游戏                        -> Start Game
背包                            -> Inventory
克莱恩·莫雷蒂看着镜子，灰雾在他眼前浮现。
   -> Klein Moretti stared at the mirror, grey fog materializing before his eyes.
克莱恩晋升为序列8：小丑。        -> Klein ascended to Sequence 8: Clown.
```

`序列`/`途径` (Sequence/Pathway) are additionally corrected in code, because small
models reliably confuse the two and they're distinct concepts in this series.

### Want better prose?

`qwen2.5:3b-instruct` is the speed choice. For noticeably better narrative quality
at ~1-2s per line:

```
ollama pull qwen2.5:7b-instruct
```

then pick it in the Model box. Or switch Translator to `anthropic` and set
`ANTHROPIC_API_KEY` for the best results (costs per use; a Claude Code login will
not work — it needs a real API key from console.anthropic.com).

## Tuning

Edit `config.json`:

| Key | Meaning |
| --- | --- |
| `poll_interval` | Seconds between screen checks (default 0.4) |
| `change_threshold` | How much the region must change to trigger OCR. Raise it if animated backgrounds cause needless re-scans |
| `ocr_side_len` | OCR detection resolution (default 384). Raise to 736 for small or low-contrast text, at roughly 2x the cost |
| `ocr_threads` | CPU threads for OCR (default 4). Keep this well under your core count so the game keeps its CPU |

## Troubleshooting

**Overlay not visible** — game is in exclusive fullscreen; switch to borderless.

**First line takes ~25s** — the model is loading into VRAM. It warms up on launch
and stays resident for 30 minutes; after that it's fast.

**Garbled or missing text** — tighten the region to just the text, and raise
`ocr_side_len` to 736 for small fonts. Tick "Show original Chinese too" to see
whether the fault is OCR or translation.

**Wrong area captured on a scaled display** — the app sets per-monitor DPI awareness
at startup; if you change display scaling while it's running, restart it.

## Requirements

```
pip install rapidocr-onnxruntime pillow numpy
```

Ollama, for local translation: <https://ollama.com>
