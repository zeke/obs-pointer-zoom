# obs-pointer-zoom

Hold a hotkey in OBS Studio to zoom the current Display Capture source 2x,
centered on your mouse pointer. Release to smoothly zoom back out.

Runs entirely as a single OBS Python script, no separate process, no
obs-websocket. See [AGENTS.md](AGENTS.md) for how it works and how to
develop it further.

## Requirements

- macOS, OBS Studio (built/tested against 32.1.2).
- A Python **framework** build that OBS's scripting can load, with
  [pyobjc](https://pyobjc.readthedocs.io/) installed into it.

## Setup

1. Install a framework Python if you don't already have one OBS can use, e.g.:

   ```
   brew install python@3.11
   ```

2. Point OBS at it: **Tools > Scripts > Python Settings**, and set the path to:

   ```
   /opt/homebrew/opt/python@3.11/Frameworks/Python.framework/Versions/3.11
   ```

3. Install pyobjc into that same interpreter:

   ```
   /opt/homebrew/opt/python@3.11/bin/pip3.11 install pyobjc
   ```

4. In OBS: **Tools > Scripts > +**, add `pointer_zoom.py` from this repo.

5. **Settings > Hotkeys**, find "Zoom current source toward pointer", and
   bind it to whatever key you like — a lone modifier (Left Shift) works,
   as does a remapped key (e.g. Caps Lock remapped to F18, handy if you
   don't want Shift itself triggering the zoom while typing in other apps).

## Usage

Select a Display Capture source in the Sources list (or leave nothing
selected and it'll fall back to the topmost visible Display Capture
source in the current scene), then trigger your bound key. The source
zooms toward wherever your mouse currently is on that screen, and eases
back out when you're done.

## Configuration

In OBS: **Tools > Scripts**, select `pointer_zoom.py`, and use its
settings panel to adjust:

- **Zoom level (x)** — how far it zooms in, default 2x
- **Zoom duration (seconds)** — roughly how long the ease in/out takes,
  default 0.12s
- **Trigger mode** — **Hold to zoom** (zoomed while the key is down,
  eases back out on release) or **Click to toggle** (each press flips
  zoomed on/off)

Only Display Capture sources are supported — the zoom is anchored to a
real pixel on a real screen, which only makes sense for a source that's
literally mirroring one.
