"""
pointer_zoom.py - Hold (or toggle) a hotkey to zoom the current source toward the mouse pointer.

Drop this into OBS (Tools > Scripts > +), then bind the "Zoom current source
toward pointer" action in Settings > Hotkeys to whatever key you want (a
lone modifier like Left Shift, or a remapped key like F18, works fine).

Configurable in the script's own settings panel (Tools > Scripts, select
this script): zoom level, zoom duration, and hold-to-zoom vs
click-to-toggle.

Target source resolution: always the topmost enabled/visible macOS
Screen Capture item in the current scene, regardless of what's selected
in the Sources list. "Topmost" = last in render order (rendered last =
drawn on top; confirmed via libobs's obs-scene.c: obs_scene_add appends
new items to the tail of the list, and scene_video_render walks
head-to-tail with sequential alpha blending, so the tail is what's
visually in front).

Only the macOS "Screen Capture" source is supported, in any of its three
capture methods (Display, Window, or Application) -- zooming "toward the
pointer" only makes sense when the source is a mirror of real on-screen
content, since that's what lets us map the global mouse position onto a
pixel inside the source. Display and Application capture both cover a
whole display and are handled identically; Window capture tracks that
window's live on-screen frame, which can move/resize at any time.

Requires pyobjc (Quartz) in whatever Python interpreter OBS's Script
settings points to:

    pip3 install pyobjc

See README.md in this repo for setup details.
"""

import ctypes
import math
import uuid as uuid_lib

import obspython as obs

try:
    import Quartz

    QUARTZ_OK = True
    QUARTZ_ERROR = None
except Exception as exc:  # noqa: BLE001 - report any import failure, don't guess which
    QUARTZ_OK = False
    QUARTZ_ERROR = str(exc)

try:
    # CGDisplayCreateUUIDFromDisplayID isn't in pyobjc's Quartz bridge at
    # all (confirmed via dir(Quartz) -- genuinely absent, not a naming
    # issue). Call it directly via ctypes instead, and read the raw UUID
    # bytes rather than going through CFUUIDCreateString/CFStringRef,
    # which avoids needing any CF<->Python string bridging.
    _colorsync = ctypes.CDLL("/System/Library/Frameworks/ColorSync.framework/ColorSync")
    _corefoundation = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")

    class _CFUUIDBytes(ctypes.Structure):
        _fields_ = [("b%d" % i, ctypes.c_ubyte) for i in range(16)]

    _colorsync.CGDisplayCreateUUIDFromDisplayID.restype = ctypes.c_void_p
    _colorsync.CGDisplayCreateUUIDFromDisplayID.argtypes = [ctypes.c_uint32]
    _corefoundation.CFUUIDGetUUIDBytes.restype = _CFUUIDBytes
    _corefoundation.CFUUIDGetUUIDBytes.argtypes = [ctypes.c_void_p]
    _corefoundation.CFRelease.restype = None
    _corefoundation.CFRelease.argtypes = [ctypes.c_void_p]

    CTYPES_UUID_OK = True
    CTYPES_UUID_ERROR = None
except Exception as exc:  # noqa: BLE001
    CTYPES_UUID_OK = False
    CTYPES_UUID_ERROR = str(exc)


def _display_uuid_string(display_id):
    """UUID string for a CGDirectDisplayID, or None."""
    if not CTYPES_UUID_OK:
        return None
    ref = _colorsync.CGDisplayCreateUUIDFromDisplayID(display_id)
    if not ref:
        return None
    try:
        raw_bytes = _corefoundation.CFUUIDGetUUIDBytes(ref)
        raw = bytes(getattr(raw_bytes, "b%d" % i) for i in range(16))
        return str(uuid_lib.UUID(bytes=raw))
    finally:
        _corefoundation.CFRelease(ref)

HOTKEY_NAME = "pointer_zoom.hold"
HOTKEY_DESC = "Zoom current source toward pointer"
# Both ids show up in the wild depending on OS/OBS version: "screen_capture"
# is the current macOS kind id (confirmed via this machine's OBS log);
# "display_capture" was the legacy id. Accept either.
DISPLAY_CAPTURE_KINDS = ("screen_capture", "display_capture")

# The unified "screen_capture" source has one "type" setting selecting
# among these three capture methods (confirmed in obs-studio's
# plugins/mac-capture/mac-sck-video-capture.m). Display and Application
# capture both key off a "display_uuid" setting and cover a whole
# display's pixel dimensions (Application capture just filters which
# windows render into that same display-sized frame) -- geometrically
# identical for our purposes. Window capture keys off a "window"
# CGWindowID instead, and its frame can move/resize anytime, so it's
# looked up fresh every tick rather than cached like a display's bounds.
CAPTURE_TYPE_DISPLAY = 0
CAPTURE_TYPE_WINDOW = 1
CAPTURE_TYPE_APPLICATION = 2

EPS = 0.002

MODE_HOLD = "hold"
MODE_TOGGLE = "toggle"
DEFAULT_ZOOM_FACTOR = 3.0
DEFAULT_ZOOM_DURATION = 0.15  # seconds (exponential-ease time constant)
DEFAULT_TRIGGER_MODE = MODE_TOGGLE

# User-configurable (see script_properties/script_update below).
zoom_factor = DEFAULT_ZOOM_FACTOR
ease_tau = DEFAULT_ZOOM_DURATION
trigger_mode = DEFAULT_TRIGGER_MODE

hotkey_id = obs.OBS_INVALID_HOTKEY_ID
zoom_requested = False  # true = should be zoomed in, regardless of hold/toggle mode
current_scene_source = None  # owned ref, released on rebind/unload
target_dirty = True

_target = None  # cached base-state snapshot of the item we're animating
_active_key = None  # sceneitem id of the item _target refers to
progress = 0.0  # 0 = rest (1x), 1 = fully zoomed (zoom_factor x)
_error_logged = False  # rate-limit: never spam the script log every tick


# --------------------------------------------------------------------------
# OBS script lifecycle
# --------------------------------------------------------------------------


def script_description():
    warning = ""
    if not QUARTZ_OK:
        warning = (
            "<p style='color:#d33'><b>pyobjc (Quartz) not importable in this "
            "Python: %s</b><br>Run <code>pip3 install pyobjc</code> in the "
            "interpreter selected under Tools &gt; Scripts &gt; Python "
            "Settings.</p>" % QUARTZ_ERROR
        )
    if not CTYPES_UUID_OK:
        warning += (
            "<p style='color:#d33'><b>Display UUID lookup unavailable: "
            "%s</b></p>" % CTYPES_UUID_ERROR
        )
    return (
        "<h3>Pointer Zoom</h3>"
        "<p>Bind the hotkey below (Settings &gt; Hotkeys &gt; \"%s\") to "
        "whatever key you like. In <b>Hold</b> mode, the topmost visible "
        "Display Capture source zooms in while held and eases back out on "
        "release. In <b>Toggle</b> mode, each press flips zoomed on/off.</p>" % HOTKEY_DESC
    ) + warning


def script_load(settings):
    global hotkey_id

    hotkey_id = obs.obs_hotkey_register_frontend(HOTKEY_NAME, HOTKEY_DESC, on_hotkey)
    saved = obs.obs_data_get_array(settings, HOTKEY_NAME)
    obs.obs_hotkey_load(hotkey_id, saved)
    obs.obs_data_array_release(saved)

    obs.obs_frontend_add_event_callback(on_frontend_event)
    refresh_current_scene()
    obs.obs_add_tick_callback(tick)

    if not QUARTZ_OK:
        obs.script_log(obs.LOG_WARNING, "pointer_zoom: Quartz unavailable (%s); zoom disabled" % QUARTZ_ERROR)
    if not CTYPES_UUID_OK:
        obs.script_log(obs.LOG_WARNING, "pointer_zoom: display UUID lookup unavailable (%s); zoom disabled" % CTYPES_UUID_ERROR)


def script_unload():
    obs.obs_remove_tick_callback(tick)
    obs.obs_frontend_remove_event_callback(on_frontend_event)
    restore_target_if_any()
    release_current_scene()


def script_save(settings):
    saved = obs.obs_hotkey_save(hotkey_id)
    obs.obs_data_set_array(settings, HOTKEY_NAME, saved)
    obs.obs_data_array_release(saved)


def script_defaults(settings):
    obs.obs_data_set_default_double(settings, "zoom_factor", DEFAULT_ZOOM_FACTOR)
    obs.obs_data_set_default_double(settings, "zoom_duration", DEFAULT_ZOOM_DURATION)
    obs.obs_data_set_default_string(settings, "trigger_mode", DEFAULT_TRIGGER_MODE)


def script_properties():
    # Plain number fields, not obs_properties_add_float_slider: the
    # slider variant's value box is a few pixels too narrow for its own
    # text on this OBS build (Retina scaling?) and clips e.g. "2.0" to
    # "2.(". Scripts can't set widget width directly, so drop the slider.
    props = obs.obs_properties_create()

    zoom_prop = obs.obs_properties_add_float(props, "zoom_factor", "Zoom level", 1.1, 8.0, 0.1)
    obs.obs_property_float_set_suffix(zoom_prop, "x")

    duration_prop = obs.obs_properties_add_float(props, "zoom_duration", "Zoom duration", 0.02, 1.0, 0.01)
    obs.obs_property_float_set_suffix(duration_prop, "s")

    mode_prop = obs.obs_properties_add_list(
        props, "trigger_mode", "Trigger mode", obs.OBS_COMBO_TYPE_LIST, obs.OBS_COMBO_FORMAT_STRING
    )
    obs.obs_property_list_add_string(mode_prop, "Hold to zoom", MODE_HOLD)
    obs.obs_property_list_add_string(mode_prop, "Click to toggle", MODE_TOGGLE)
    return props


def script_update(settings):
    global zoom_factor, ease_tau, trigger_mode
    zoom_factor = obs.obs_data_get_double(settings, "zoom_factor") or DEFAULT_ZOOM_FACTOR
    ease_tau = obs.obs_data_get_double(settings, "zoom_duration") or DEFAULT_ZOOM_DURATION
    trigger_mode = obs.obs_data_get_string(settings, "trigger_mode") or DEFAULT_TRIGGER_MODE


# --------------------------------------------------------------------------
# Hotkey handling and current-scene tracking
# --------------------------------------------------------------------------


def on_hotkey(pressed):
    global zoom_requested, target_dirty, _error_logged

    if trigger_mode == MODE_TOGGLE:
        if not pressed:
            return  # toggle mode only reacts to key-down, ignores key-up
        zoom_requested = not zoom_requested
    else:
        zoom_requested = pressed

    if zoom_requested:
        # Force a fresh look at what's currently topmost the moment zoom
        # is requested, in case the scene's composition changed since.
        target_dirty = True
        _error_logged = False


def on_frontend_event(event):
    if event == obs.OBS_FRONTEND_EVENT_SCENE_CHANGED:
        restore_target_if_any()
        refresh_current_scene()


def refresh_current_scene():
    """Track which source is the current program scene.

    We don't care about scene item selection at all anymore -- the target
    is always "whatever is topmost right now", resolved fresh each time
    zoom is requested. This just keeps current_scene_source pointing at
    the right scene and marks the target stale on scene switches.
    """
    global current_scene_source, target_dirty

    release_current_scene()
    current_scene_source = obs.obs_frontend_get_current_scene()
    target_dirty = True


def release_current_scene():
    global current_scene_source

    if current_scene_source:
        obs.obs_source_release(current_scene_source)
        current_scene_source = None


# --------------------------------------------------------------------------
# Target resolution (only runs when something actually needs it)
# --------------------------------------------------------------------------


def resolve_target():
    """Return a base-state snapshot for the item to zoom, or None."""
    if not QUARTZ_OK or not current_scene_source:
        return None

    scene = obs.obs_scene_from_source(current_scene_source)
    if not scene:
        return None

    # NOTE: obs.obs_scene_enum_items in this scripting environment is a
    # hand-written wrapper (not the raw libobs C API) that takes just
    # (scene) and returns a plain Python list of addref'd sceneitem
    # objects -- it does NOT take a callback+param like the C signature.
    # The list must be released with obs.sceneitem_list_release(items).
    items = obs.obs_scene_enum_items(scene)
    try:
        # obs_scene_enum_items walks scene->first_item -> ->next, which is
        # back-to-front render order (confirmed in libobs/obs-scene.c:
        # obs_scene_add appends new items at the tail, and
        # scene_video_render walks head-to-tail alpha-blending each in
        # turn, so the tail is drawn last = on top). So the *last*
        # matching item in this iteration is the topmost one, not the
        # first.
        item = None
        for candidate_item in items:
            source = obs.obs_sceneitem_get_source(candidate_item)
            if obs.obs_sceneitem_visible(candidate_item) and obs.obs_source_get_unversioned_id(
                source
            ) in DISPLAY_CAPTURE_KINDS:
                item = candidate_item

        if item is None:
            return None

        source = obs.obs_sceneitem_get_source(item)

        if obs.obs_sceneitem_get_bounds_type(item) != obs.OBS_BOUNDS_NONE:
            return None  # v1 doesn't support "scale to fit" bounds-box items

        settings = obs.obs_source_get_settings(source)
        capture_type = obs.obs_data_get_int(settings, "type")
        display_uuid = obs.obs_data_get_string(settings, "display_uuid")
        window_id = obs.obs_data_get_int(settings, "window")
        obs.obs_data_release(settings)

        target = {
            "item_id": obs.obs_sceneitem_get_id(item),
            "capture_type": capture_type,
            "display_uuid": display_uuid,
            "window_id": window_id,
        }
        if current_capture_bounds(target) is None:
            return None  # e.g. window capture pointed at a closed window

        src_w = obs.obs_source_get_width(source)
        src_h = obs.obs_source_get_height(source)
        if src_w <= 0 or src_h <= 0:
            return None

        pos = obs.vec2()
        obs.obs_sceneitem_get_pos(item, pos)
        scale = obs.vec2()
        obs.obs_sceneitem_get_scale(item, scale)

        target["base_pos"] = (pos.x, pos.y)
        target["base_scale"] = (scale.x, scale.y)
        target["src_size"] = (src_w, src_h)
        return target
    finally:
        obs.sceneitem_list_release(items)


# --------------------------------------------------------------------------
# macOS display / cursor helpers
# --------------------------------------------------------------------------


def current_capture_bounds(target):
    """(x, y, w, h) in points for whatever this target currently captures.

    Looked up fresh on every call rather than cached on the target: a
    captured window can move or resize at any time, so its bounds can't
    be snapshotted once like a display's can.
    """
    if target["capture_type"] == CAPTURE_TYPE_WINDOW:
        return window_bounds_for_id(target["window_id"])
    return display_bounds_for_uuid(target["display_uuid"])


def display_bounds_for_uuid(uuid_str):
    """(x, y, w, h) in points for the display matching display_uuid, or None."""
    if not QUARTZ_OK or not CTYPES_UUID_OK or not uuid_str:
        return None

    err, display_ids, count = Quartz.CGGetActiveDisplayList(16, None, None)
    if err != 0:
        return None

    for display_id in list(display_ids)[:count]:
        if _display_uuid_string(display_id) != uuid_str.lower():
            continue
        bounds = Quartz.CGDisplayBounds(display_id)
        return (
            bounds.origin.x,
            bounds.origin.y,
            bounds.size.width,
            bounds.size.height,
        )
    return None


def window_bounds_for_id(window_id):
    """(x, y, w, h) in points for a window by CGWindowID, or None."""
    if not QUARTZ_OK or not window_id:
        return None

    info_list = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionIncludingWindow, window_id)
    if not info_list:
        return None  # window closed, or we lack permission to see it

    bounds_dict = info_list[0].get("kCGWindowBounds")
    if not bounds_dict:
        return None

    ok, rect = Quartz.CGRectMakeWithDictionaryRepresentation(bounds_dict, None)
    if not ok:
        return None
    return (rect.origin.x, rect.origin.y, rect.size.width, rect.size.height)


def mouse_location():
    """Global mouse position in points (top-left origin), or None."""
    if not QUARTZ_OK:
        return None
    event = Quartz.CGEventCreate(None)
    point = Quartz.CGEventGetLocation(event)
    return (point.x, point.y)


# --------------------------------------------------------------------------
# Animation
# --------------------------------------------------------------------------


def _remap_edge_margin(fraction):
    """Remap [0,1] so [0,margin]->0 and [1-margin,1]->1, linear in between.

    Plain clamping (restricting the input to [margin, 1-margin]) doesn't
    give a flush edge -- the point-invariant formula in apply_transform
    only lands exactly flush against an edge when fed a pin fraction of
    exactly 0 or 1 (see AGENTS.md "Edge clamping" for the derivation), so
    short of that it just freezes early without ever reaching flush. This
    remaps the input so the margin band actually collapses to the true 0
    or 1 the formula needs, instead of merely bounding it.

    margin is based on the *final* zoom_factor (a constant for the
    duration of a given zoom), not the live in-animation factor. It has
    to be constant: since apply_transform's position formula moves
    proportionally to (factor-1), using a live-factor margin here would
    make the pin itself drift over time even for a perfectly stationary
    cursor (margin shrinks as factor ramps up), decoupling position's
    timing from scale's and causing a visible lurch. A constant margin
    keeps the pin fixed for a stationary cursor, so position and scale
    stay strictly proportional to the same factor(t) curve and always
    complete in lockstep. The tradeoff: the cursor lands exactly centered
    in the visible portion at the moment of edge-lock only once fully
    zoomed in, not continuously through the ease transient -- see
    AGENTS.md "Edge clamping" for the previous attempt and why it lurched.
    """
    margin = min(0.49, 0.5 / zoom_factor) if zoom_factor > 1 else 0.0
    if margin <= 0:
        return fraction
    span = 1.0 - 2 * margin
    return min(1.0, max(0.0, (fraction - margin) / span))


def anchor_point(target):
    """Canvas-space point (in the item's *base* layout) used as the zoom pin.

    Based on the cursor's fraction across the source, but remapped via
    _remap_edge_margin so a screen edge/corner comes fully into view once
    the cursor is within a margin of it, rather than requiring the cursor
    to reach the exact physical edge pixel.
    """
    bx, by = target["base_pos"]
    bsx, bsy = target["base_scale"]
    sw, sh = target["src_size"]

    loc = mouse_location()
    bounds = current_capture_bounds(target)
    if loc is None or bounds is None or bounds[2] <= 0 or bounds[3] <= 0:
        fx = fy = 0.5
    else:
        dx, dy, dw, dh = bounds
        fx = min(1.0, max(0.0, (loc[0] - dx) / dw))
        fy = min(1.0, max(0.0, (loc[1] - dy) / dh))

    fx = _remap_edge_margin(fx)
    fy = _remap_edge_margin(fy)
    return (bx + fx * sw * bsx, by + fy * sh * bsy)


def apply_transform(target, factor):
    if not current_scene_source:
        return
    scene = obs.obs_scene_from_source(current_scene_source)
    if not scene:
        return
    item = obs.obs_scene_find_sceneitem_by_id(scene, target["item_id"])
    if not item:
        return

    bx, by = target["base_pos"]
    bsx, bsy = target["base_scale"]
    ax, ay = anchor_point(target)

    new_pos = obs.vec2()
    new_pos.x = ax + (bx - ax) * factor
    new_pos.y = ay + (by - ay) * factor

    new_scale = obs.vec2()
    new_scale.x = bsx * factor
    new_scale.y = bsy * factor

    obs.obs_sceneitem_defer_update_begin(item)
    obs.obs_sceneitem_set_pos(item, new_pos)
    obs.obs_sceneitem_set_scale(item, new_scale)
    obs.obs_sceneitem_defer_update_end(item)


def restore_target_if_any():
    global _target, _active_key, progress
    if _target is not None and progress > EPS:
        apply_transform(_target, 1.0)
    _target = None
    _active_key = None
    progress = 0.0


def tick(seconds):
    """Safety wrapper: never let an exception repeat every single frame.

    A bug in _tick(...) previously threw on every tick once triggered
    (60x/sec, indefinitely, while spamming the script log) because the
    failure path left state such that it kept retrying. This wrapper
    guarantees at most one log line per failure and always resets to a
    safe rest state instead of leaving things stuck mid-retry.
    """
    global target_dirty, _target, _active_key, progress, _error_logged
    try:
        _tick(seconds)
    except Exception as exc:  # noqa: BLE001 - must never propagate from here
        if not _error_logged:
            obs.script_log(obs.LOG_ERROR, "pointer_zoom: tick failed, disabling until next zoom request: %r" % exc)
            _error_logged = True
        _target = None
        _active_key = None
        progress = 0.0
        target_dirty = False


def _tick(seconds):
    global target_dirty, _target, _active_key, progress

    need_target = zoom_requested or progress > EPS

    if target_dirty and need_target:
        candidate = resolve_target()
        candidate_key = candidate["item_id"] if candidate else None

        if _active_key is not None and candidate_key != _active_key and progress > EPS:
            # Topmost item changed mid-zoom: snap the old item back
            # before switching so nothing gets left stuck zoomed in.
            apply_transform(_target, 1.0)
            progress = 0.0

        _target = candidate
        _active_key = candidate_key
        target_dirty = False

    goal = 1.0 if (zoom_requested and _target is not None) else 0.0

    if progress <= EPS and goal == 0.0:
        return

    alpha = 1.0 - math.exp(-seconds / ease_tau) if seconds > 0 else 1.0
    progress += (goal - progress) * alpha

    if _target is None:
        progress = 0.0
        return

    if progress < EPS and goal == 0.0:
        apply_transform(_target, 1.0)
        progress = 0.0
        _target = None
        _active_key = None
        return

    apply_transform(_target, zoom_factor ** progress)
