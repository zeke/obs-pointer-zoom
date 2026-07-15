# AGENTS.md

Technical notes for working on this project. Update this file whenever you
make a meaningful change to the architecture, APIs relied on, or setup
steps — it's the source of truth for future agent sessions, not the README.

## What this is

A single OBS Studio Python script (`pointer_zoom.py`) that lets you hold a
hotkey to zoom the current Display Capture source 2x toward the mouse
pointer, easing in and out. No external process, no obs-websocket.

## Stack

- OBS Studio 32.1.2 on macOS (`/Applications/OBS.app`), installed via
  Homebrew cask (`obs` in `/opt/homebrew/bin`).
- OBS's built-in Python scripting (`obspython`), loaded via Tools > Scripts.
- [pyobjc](https://pyobjc.readthedocs.io/) (`Quartz` module) for global
  mouse position and display bounds lookup. Must be installed into whatever
  Python interpreter OBS's Script settings points at (a framework build).

## Key facts about OBS's Python scripting API (verified against source, not memory)

`obspython.py`/`_obspython.so` (the swig-generated bindings shipped in
`OBS.app/Contents/Resources` and `Contents/PlugIns`) do **not** include
`obs_hotkey_register_frontend`, `obs_frontend_add_event_callback`,
`obs_frontend_get_current_scene`, `timer_add`, or plain
`signal_handler_connect`. Those are injected into the script's `obspython`
module *at script-load time* by hand-written glue in OBS's C++ source
(`shared/obs-scripting/obs-scripting-python.c` and
`obs-scripting-python-frontend.c` in the `obsproject/obs-studio` repo).
They only exist when the code is actually running as a loaded OBS script —
a standalone `import obspython` outside OBS won't have them. Confirmed by
reading that source directly; don't trust "obs.X exists" claims without
either checking that source or testing inside OBS.

Relevant confirmed behavior:
- `obs.obs_hotkey_register_frontend(name, description, callback)` — the
  callback receives `pressed: bool` on both key-down and key-up. This is
  OBS's real global hotkey system (same one used for start/stop
  streaming), so it already works while OBS isn't focused, and it's
  user-rebindable in Settings > Hotkeys without touching code.
- `obs.timer_add(callback, ms)` is driven by `obs_add_tick_callback`
  internally — i.e. it only ever fires on OBS's actual video-frame tick,
  not an independent OS timer. We use `obs.obs_add_tick_callback(callback)`
  directly instead, since it gives the real per-frame delta-time and is
  exactly in phase with rendering (no risk of update/render rate jitter).
- `item_select` / `item_deselect` / `item_transform` are real signals on a
  scene source's signal handler (`obs.obs_source_get_signal_handler`),
  confirmed in `libobs/obs-scene.c`. Used to track the selected scene item
  without polling.
- `obs.obs_scene_enum_items(scene)` is **not** the raw libobs
  `obs_scene_enum_items(scene, callback, param)` signature. In this
  scripting environment it's a hand-written wrapper
  (`scene_enum_items` in `obs-scripting-python.c`) that takes just
  `(scene)` and returns a plain Python list of addref'd sceneitem
  objects. You must release it with `obs.sceneitem_list_release(items)`
  when done (mirrors `obs_enum_sources`/`source_list_release`). Calling
  it with the C signature raises `TypeError: scene_enum_items() takes
  exactly 1 argument (3 given)` wrapped in a `SystemError`. This bit us
  for real: the exception happened inside `tick()`, which runs every
  video frame, and the failure path didn't clear the "needs resolving"
  flag, so it retried and re-raised 60x/second indefinitely once
  triggered (spammed ~33k lines into the OBS log in seconds and froze
  the machine). Lesson: **any exception inside an `obs_add_tick_callback`
  handler must be caught, logged at most once, and must always leave
  state such that the same failure can't retry every single frame.**
  `tick()` in `pointer_zoom.py` is now a thin wrapper around `_tick()`
  that does exactly this — keep that structure for any future per-frame
  callback in this codebase.

## Design notes

- Only Display Capture sources are supported. Zooming "toward the pointer"
  requires mapping the global OS mouse position onto a pixel inside the
  source, which is only meaningful for a source that mirrors a real
  screen. The source's `display_uuid` setting is resolved to a
  `CGDirectDisplayID` (via `CGDisplayCreateUUIDFromDisplayID` over
  `CGGetActiveDisplayList`) to get that screen's bounds. The macOS source
  kind id for this is `screen_capture` (confirmed from this machine's OBS
  log — a newer ScreenCaptureKit-based unified capture source); the
  legacy id `display_capture` is also accepted just in case older OBS
  versions still use it. `DISPLAY_CAPTURE_KINDS` in `pointer_zoom.py`
  holds both.
- Items using a bounds-box ("scale to fit"/"stretch") transform are
  skipped in v1 — the zoom math assumes a plain position+scale transform.
- Animation is frame-time-independent exponential easing
  (`progress += (goal - progress) * (1 - exp(-dt/tau))`), driven by the
  real per-tick `seconds` from `obs_add_tick_callback`, not an assumed
  fixed interval.
- Selection state is cached and only re-resolved when a `selection_dirty`
  flag is set (by the signals above, or when the hotkey is pressed) — the
  per-frame hot path never scans the scene graph.

## pyobjc gotchas

- `CGDisplayCreateUUIDFromDisplayID` is **not** bridged by pyobjc's
  `Quartz` module at all — confirmed by enumerating `dir(Quartz)`, it's
  genuinely absent (not a naming/casing issue). Caused a real runtime
  `AttributeError` the first time this ran against a live OBS. The C
  symbol does exist, in `ColorSync.framework` and
  `ApplicationServices.framework`; call it via `ctypes.CDLL(...)`
  directly. Don't assume a CoreGraphics/ColorSync function is
  pyobjc-bridged just because the rest of `Quartz` (CGGetActiveDisplayList,
  CGDisplayBounds, CGEventGetLocation, etc.) works fine — check
  `dir(Quartz)` first. Same goes for the CFUUID→string conversion: skip
  `CFUUIDCreateString`/`CFStringRef` bridging entirely by reading raw
  bytes with `CFUUIDGetUUIDBytes` and formatting with stdlib
  `uuid.UUID(bytes=...)` instead.

## Setup / testing gotchas

- OBS's Python scripting needs a framework-style Python build. Homebrew's
  `python@X.Y` formulas are framework builds and work; system Python
  typically isn't set up the way OBS expects.
- obs-scripting's supported Python minor-version range isn't documented
  anywhere reliable found so far — if OBS rejects the configured
  interpreter, try a different Homebrew `python@` version (3.10–3.12 are
  reasonable first guesses) rather than assuming 3.14 (the newest
  available at time of writing) is supported.
- No special macOS permission (Accessibility/Input Monitoring) should be
  needed: hotkey capture reuses OBS's own existing global-hotkey
  mechanism, and `CGEventGetLocation`/display bounds lookups are
  unrestricted reads. Verify this holds in practice when testing on a
  fresh machine.
- Don't drive OBS's Settings window (the category list specifically) via
  macOS Accessibility automation (AppleScript/System Events `click`,
  `AXPress`, or raw `click at {x,y}`) — it reproducibly crashed OBS twice
  while developing this (SIGSEGV in `objc_release`/`NSArray dealloc`
  during Qt's event loop teardown). Adding/removing scripts via the
  Scripts window's named buttons (`Add Scripts`, `Close`, etc., found via
  `entire contents of window` + matching on `name`) was fine. Also: AX
  `keystroke` is **not** reliably scoped to the target process — it goes
  to whatever is actually frontmost at the OS level, which can silently
  change while a script is mid-flight. Prefer asking a human to do
  Settings/Hotkeys steps manually over automating them here.
