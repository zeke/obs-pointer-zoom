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

5. **Settings > Hotkeys**, find "Hold: Zoom current source toward pointer
   (2x)", and bind it to Left Shift (or anything else you like — it works
   as a hold, not a toggle).

## Usage

Select a Display Capture source in the Sources list (or leave nothing
selected and it'll fall back to the topmost visible Display Capture
source in the current scene), then hold your bound key. The source zooms
in 2x toward wherever your mouse currently is on that screen, and eases
back to 1x when you let go.

Only Display Capture sources are supported — the zoom is anchored to a
real pixel on a real screen, which only makes sense for a source that's
literally mirroring one.
