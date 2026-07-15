# AGENTS.md

Technical notes for working on this project. Update this file whenever you
make a meaningful change to the architecture, APIs relied on, or setup
steps — it's the source of truth for future agent sessions, not the README.

## What this is

A single OBS Studio Python script (`pointer_zoom.py`) that lets you
trigger a hotkey to zoom the current Display Capture source toward the
mouse pointer, easing in and out. No external process, no obs-websocket.
Confirmed working end-to-end on the author's machine as of the commit
adding this line.

Configurable via the script's settings panel (Tools > Scripts, select the
script): zoom level (`zoom_factor`), ease time constant (`ease_tau`,
labeled "Zoom duration"), and trigger mode (`trigger_mode`: `"hold"` or
`"toggle"`). See `script_defaults`/`script_properties`/`script_update` in
`pointer_zoom.py`. The hotkey's internal registration name
(`HOTKEY_NAME`) must stay stable across changes so saved user bindings
aren't lost — only `HOTKEY_DESC` (display text) is safe to change freely.

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
  confirmed in `libobs/obs-scene.c`. No longer used here -- targeting is
  now always "topmost visible Display Capture", not "whatever's
  selected", so tracking selection changes became unnecessary -- but
  worth remembering these signals exist if selection-based behavior ever
  comes back.
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

- Only the macOS "Screen Capture" source kind (`screen_capture`; legacy
  `display_capture` id also accepted, see `DISPLAY_CAPTURE_KINDS`) is
  supported. Zooming "toward the pointer" requires mapping the global OS
  mouse position onto a pixel inside the source, which is only
  meaningful for a source that mirrors real on-screen content.
- That source kind is actually one unified source with a `"type"`
  setting selecting among three capture *methods* (confirmed in
  `obs-studio`'s `plugins/mac-capture/mac-sck-video-capture.m`):
  `0`=Display, `1`=Window, `2`=Application. All three are supported.
  Display and Application capture both key off a `"display_uuid"`
  setting and cover a whole display's pixel dimensions (Application
  capture just filters which windows render into that same
  display-sized frame server-side) -- geometrically identical for our
  purposes, resolved via `CGDisplayCreateUUIDFromDisplayID` over
  `CGGetActiveDisplayList` same as before. Window capture keys off a
  `"window"` `CGWindowID` setting instead, resolved via
  `CGWindowListCopyWindowInfo(kCGWindowListOptionIncludingWindow, id)` +
  `CGRectMakeWithDictionaryRepresentation` on its `kCGWindowBounds`
  entry -- both real functions, both actually bridged in pyobjc's
  `Quartz` this time (checked `dir(Quartz)` first, given the track
  record above). Unlike a display's bounds, a window's frame can
  move/resize at any moment, so `current_capture_bounds()` resolves it
  fresh on every call (every tick while zoomed) instead of caching it
  once on the target snapshot like the rest of the target's state.
- Items using a bounds-box ("scale to fit"/"stretch") transform are
  skipped in v1 — the zoom math assumes a plain position+scale transform.
- Animation is frame-time-independent exponential easing
  (`progress += (goal - progress) * (1 - exp(-dt/tau))`), driven by the
  real per-tick `seconds` from `obs_add_tick_callback`, not an assumed
  fixed interval.
- Target state is cached and only re-resolved when `target_dirty` is set
  (on zoom request or scene switch) — the per-frame hot path never scans
  the scene graph.

## Edge clamping (pointer near screen edges/corners)

`anchor_point(target, factor)` doesn't use the raw cursor fraction as the
zoom pin directly. `apply_transform`'s position formula (`new_pos = ax +
(base_pos - ax) * factor`) is a "zoom toward a fixed point" transform: it
keeps whatever canvas point `ax` represents visually stationary while the
item scales up around it. That formula is provably safe for any pin
fraction in `[0,1]` — plugging in the algebra shows the zoomed item's
edges never fall short of the base item's edges (i.e. it can never expose
blank/off-source space) for any such input — but it only lands *exactly*
flush against an edge when fed a pin fraction of precisely `0` or `1`.

A first attempt (reverted, see git history) tried to get "edges come into
view before the cursor reaches them" by clamping the fraction to
`[margin, 1-margin]`. That doesn't work: clamping just freezes the input
short of `0`/`1`, so the formula still doesn't produce a flush edge, it
just stops responding to further cursor movement partway there — the
zoom pin locks early but the edge never actually comes fully into frame.

The fix is to *remap* the fraction instead of clamp it (`_remap_edge_margin`):
squash `[0, margin]` down to exactly `0`, squash `[1-margin, 1]` up to
exactly `1`, and linearly rescale the middle band to still cover the full
`[0,1]` range. Fed into the same unmodified formula, this reaches true
flush-against-the-edge once the cursor is within `margin` of it, while
still never exposing blank space (since the formula is valid for any
input in `[0,1]`, which is all this ever produces).

`margin` must be exactly `0.5 / factor` using the **live**, instantaneous
factor (capped at 0.49 to avoid a zero-width span right at factor=1) —
not the final target `zoom_factor`. Derivation: at a flush-left pin, the
visible source-fraction range is `[0, 1/factor]`, whose center is exactly
`0.5/factor`. For the cursor to be centered in the visible portion at
the instant an edge locks flush, margin has to equal that center value at
every instant, not just once fully zoomed in. An earlier version used the
final `zoom_factor` instead (constant through the ease) reasoning that
live-factor margin would be "unstable" near factor=1 — that concern
turned out to be unfounded (the 0.49 cap already prevents any
division-by-zero or discontinuity) and the fixed-margin version only had
the centering property at rest, not during the ease-in/out transient.
Note the live-factor version can cause an edge to flush-lock briefly
during an early, low-factor part of the ease and then un-lock again as
factor keeps climbing (if the cursor isn't within the *final* factor's
margin) -- that's not a bug, it's an accurate reflection of "what a
permanently-fixed zoom at this instant's factor would look like", which
is the same recompute-fresh-every-tick model the rest of the animation
already uses.

## Scene item order / "topmost"

`obs.obs_scene_enum_items(scene)` walks `scene->first_item` forward via
`->next` (confirmed in `libobs/obs-scene.c`). That is **back-to-front**
render order, not front-to-back: `obs_scene_add` appends newly-added items
at the tail when there's no explicit insertion point, and
`scene_video_render` walks head-to-tail, alpha-blending each item in
turn -- so whatever's drawn *last* (the tail) ends up visually on top.
To find the topmost matching item, take the *last* match while iterating
the returned list, not the first. (Got this backwards on the first pass;
fixed once actually asked to target "topmost" instead of "selected or
fallback".)

## Qt widget quirks (scripting)

`obs_properties_add_float_slider` renders a value box that's a few pixels
too narrow for its own text on this OBS build (e.g. "2.0" clips to "2.("),
at least at this machine's display scaling. There's no script-level API
to set widget width. Workaround: use `obs_properties_add_float` (plain
number field, no slider) instead -- `obs_property_float_set_suffix` for a
unit label ("x", "s") reads fine there.

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
