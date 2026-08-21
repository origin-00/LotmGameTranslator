# Lord of the Mysteries — live translation overlay

Reads Chinese text off a region of your screen, translates it, and draws each English
translation **positioned over its own piece of text** in a click-through window. A
cluttered menu becomes a set of labels over their own buttons, not one jumbled block.

**Chinese only.** Lines with no Han characters — English UI, numbers, version strings,
logos — are dropped right after OCR. They never reach the model and never get drawn
over, so nothing you can already read is covered up.

**New text shows up the moment it appears.** Each block is independent. Anything
translated before is painted instantly from memory; a new line appears in Chinese
straight away; and its English is **streamed in as the model writes it**, word by word,
rather than appearing once the sentence is finished. Nothing waits on the rest of the
screen. If the screen changes again mid-sentence, that reply is cut off at the socket
and the outdated work is dropped before it costs anything.

**It never touches the game's files, memory, or process.** It only looks at pixels
already on screen, exactly like a screenshot tool. That's deliberate: the game ships
with ACE kernel anti-cheat, and anything that modified game data would risk your
account. This does not.

**It gets faster the longer you play.** Every translation is written to
`memory.json` next to the script, keyed by model. A line you saw last night is on
screen tonight with no model call at all — and with the second pass on, what is
remembered is the *better* translation, not the fast one.

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
3. Play. When the text changes, the labels update in place — one at a time, as each
   translation comes back.

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
| Show new Chinese instantly | On (default) = a new line appears in Chinese, dimmed, the moment it is on screen, and turns into English when its translation lands. Off = the spot stays empty until then |
| Second pass | Off by default. On = the fast model keeps up with the game, and the strongest model you have installed quietly re-translates the prose a moment later and replaces it. Picks the model itself the first time you tick it |

The overlay is always click-through, so your clicks go to the game.

Settings persist to `config.json` next to the script.

## Current setup

- **Capture**: BitBlt straight out of the region, roughly twice as fast as grabbing
  the whole screen and cropping it, and checked against the old path on first use
- **OCR**: RapidOCR (PP-OCR), runs locally on CPU, detection capped by longest side
- **Translation**: `qwen2.5:3b-instruct` via Ollama, streamed, fully offline

Translation timings measured on this machine with a resident model, CPU only — add
the poll and the OCR read on top, and expect different numbers on other hardware:

| Reading the screen | |
| --- | --- |
| Poll: is anything different? | 13 ms |
| Read the part that changed (202×75) | **131 ms** |
| Read a whole 1920×1080 menu of 27 labels | 647 ms — was 1814 ms before it was read at reduced size |

| Translating it | |
| --- | --- |
| First English words of a line | **~0.3 s** |
| Complete line of dialogue | 0.4–0.9 s |
| A menu of 27 labels, one request | first at ~0.4 s, all 27 by ~1.7 s |
| A line translated before | instant, no request at all |

The screen figures are from the game actually running, which matters: a full read is
the stage that suffers when the game is using the CPU, swinging from 0.45 s to 2.2 s,
while a crop read stays at 131–145 ms. That stability is the reason for reading only
what moved, as much as the raw speed is.

The first line after launch is slower while Ollama loads the model; after that it
stays resident for `keep_alive`. Two things do most of the work here. The prompt is
byte-identical on every call, so the server re-uses its cached evaluation of it and
only the new line costs anything. And the reply is streamed: before that, a line of
dialogue showed nothing at all until it was completely finished.

### How it keeps up with the game

Each stage exists to avoid work rather than to do it faster:

1. **Watch** — the region is sampled every `poll_interval` and compared on a 64×32
   grid. No change, no work. Idle scenes cost nothing.
2. **Read only what moved** — the changed cells give a bounding box, and only that
   crop goes through OCR. A subtitle is a small slice of a game window, and OCR cost
   tracks pixels. Everything outside the crop is carried over from the last read: those
   pixels did not change, so neither did their text. A full read still runs on a timer
   and whenever the change is large, so nothing drifts out of date.
3. **Remember** — text already translated, this session or any previous one, is
   painted with no model call.
4. **Stream** — what is left goes to the model in as few requests as possible,
   biggest text first: a whole screen of labels is one request, because each one
   costs about 280 ms before a single token is written. The reply is one translation
   per line with no numbering, which measured 54% fewer tokens written for a 27-label
   menu — the numbers alone had been doubling the wait. It is streamed, so English
   appears while the model is still writing, and the line count is checked before
   anything is remembered.
5. **Give up early** — when the screen changes again, in-flight replies are cut off
   mid-sentence and queued work is dropped before it is sent. A typewriter text effect
   therefore costs one call, not one per character.
6. **Improve afterwards** — with the second pass on, prose is re-translated by a
   stronger model in the background and swapped in when it is ready.
7. **Give ground when the game needs the machine** — the same full read measured
   0.8 s with the game idle and 6–7 s with it rendering flat out. When reads get
   slow the overlay reads a smaller picture, and goes back up when there is room,
   between `ocr_min_px` and `ocr_max_px`.

The visible effect is that new Chinese is on screen within a poll, and its English
follows word by word instead of arriving in one lump.

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

Junk is filtered before it can become a confident wrong label. A lone Han character
read off a texture or an icon is dropped unless the OCR is sure of it — `米` came back
from the model as "Begin Game" during testing — while two-character controls, which is
what this game's UI actually uses, are untouched.

Two more things keep a small model honest:

- **The examples are real conversation turns, not arrows in the prompt.** Shown
  `开始游戏 -> Start Game` inside its instructions, a 3B model will happily answer
  `I am Death. -> "I am Death," he said softly.` — it imitates the shape it was given.
  Shown a user turn and a bare English answer, it imitates that instead.
- **A reply that isn't a translation is retried once, plainly.** Small models
  sometimes hand back the Chinese, or a fragment of it, or the rules. That is detected
  (a reply with no Latin letters isn't English) and the line is asked again without the
  examples. It costs a second call only on the lines that actually failed.

### Want better prose?

`qwen2.5:3b-instruct` is the speed choice. The best of both is to keep it and turn on
**Second pass**: the fast model still puts English on screen immediately, and a
stronger one replaces the prose a moment later. Install one first:

```
ollama pull qwen2.5:7b-instruct
```

then tick "Second pass" — it picks the largest model you have. Note that two models
resident at once needs the memory for both; if yours is tight, pick the 7B in the Model
box and leave the second pass off instead.

Or switch Translator to `anthropic` and set `ANTHROPIC_API_KEY` for the best results
(costs per use; a Claude Code login will not work — it needs a real API key from
console.anthropic.com).

## Tuning

Edit `config.json`:

| Key | Meaning |
| --- | --- |
| `poll_interval` | Seconds between screen checks (default 0.15) |
| `change_threshold` | How much the region must change *on average* to trigger OCR. Raise it if animated backgrounds cause needless re-scans |
| `local_delta` / `local_cells` | The other trigger: that many cells of a 64×32 grid changing by that much counts as a change too. This is what catches one new subtitle line in a large window, which barely moves the average at all. Raise either if an animated background keeps re-triggering |
| `translate_threads` | Translations in flight at once (default 3). Each block reaches the screen as it finishes |
| `show_pending` | Show the Chinese, dimmed, in place until its English arrives (default true). Set false to leave the spot blank until then |
| `stream` | Paint each translation as the model writes it (default true). False waits for the complete reply |
| `crop_scan` | Re-read only the part of the screen that changed (default true). Turn off if labels ever seem to linger from an earlier screen |
| `full_scan_every` | Seconds before a full re-read is forced regardless (default 4) |
| `num_ctx` | Model context size (default 2048). A line of dialogue needs very little, and smaller is faster |
| `keep_alive` | How long Ollama keeps the model resident (default `60m`) |
| `refine` / `refine_model` | The second pass and which model runs it. The checkbox fills these in for you |
| `ocr_max_px` / `ocr_min_px` | Anything bigger than `ocr_max_px` (default 1280) is shrunk before being read — 2.2–2.8x faster on a 1080p frame, reading the same text. Under load the overlay drops toward `ocr_min_px` (default 768) on its own. Raise both if small text is being missed |
| `batch_size` | Short labels per request (default 48 — one request for a whole screen) |
| `ocr_side_len` | OCR detection resolution, longest side (default 736). Lower is faster; on a real game frame 736 read everything 1920 did |
| `ocr_threads` | CPU threads for OCR (default 4). Keep this well under your core count so the game keeps its CPU |
| `ocr_angle_cls` | Rotated-text classifier (default off). Screen text is never rotated |

## Troubleshooting

**Overlay not visible** — game is in exclusive fullscreen; switch to borderless.

**First line takes ~25s** — the model is loading into VRAM. It warms up on launch
and stays resident for 30 minutes; after that it's fast.

**A full read takes seconds** — cost tracks how much text is on screen, not how big
the window is. A menu packed with text is genuinely slow to read the first time; the
crop reads that follow are not, and everything already read comes back from memory.
It is also much slower while the game is rendering hard: the same read measured 0.8 s
idle and 6–7 s at full tilt, on a machine that was only half busy on paper. The
overlay drops its reading resolution on its own when that happens.

**A label lingers from an earlier screen** — a carried-over block whose pixels changed
too subtly to notice. Lower `full_scan_every`, or set `crop_scan` to false.

**Translations look worse after switching models** — memory is keyed by model, so each
model keeps its own. Switching back restores the old one; deleting `memory.json` starts
both over.

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
