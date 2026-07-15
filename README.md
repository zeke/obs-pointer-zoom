# obs-pointer-zoom

Trigger a hotkey in OBS Studio to zoom the topmost visible Display Capture
source toward your mouse pointer, and ease smoothly back out when you're
done.

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

Trigger your bound key. The topmost enabled/visible Display Capture
source in the current scene zooms toward wherever your mouse currently is
on that screen, regardless of what's selected in the Sources list, and
eases back out when you're done.

## Configuration

In OBS: **Tools > Scripts**, select `pointer_zoom.py`, and use its
settings panel to adjust:

- **Zoom level** — how far it zooms in, default 2x
- **Zoom duration** — roughly how long the ease in/out takes, default 0.12s
- **Trigger mode** — **Click to toggle** (default: each press flips
  zoomed on/off) or **Hold to zoom** (zoomed while the key is down, eases
  back out on release)

Only Display Capture sources are supported — the zoom is anchored to a
real pixel on a real screen, which only makes sense for a source that's
literally mirroring one.
