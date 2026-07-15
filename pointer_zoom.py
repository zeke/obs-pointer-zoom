"""
pointer_zoom.py - Hold a hotkey to zoom the current source toward the mouse pointer.

Drop this into OBS (Tools > Scripts > +), then bind the "Hold: Zoom current
source toward pointer (2x)" action in Settings > Hotkeys to whatever key you
want (Left Shift works fine, and can be bound alone).

Target source resolution, per tick:
  1. The scene item currently selected in the Sources list, if it's a
     Display Capture source.
  2. Otherwise, the topmost visible Display Capture item in the scene.
  3. Otherwise, nothing happens (no-op).

Only Display Capture sources are supported: zooming "toward the pointer"
only makes sense when the source is literally a mirror of a physical
screen, since that's what lets us map the global mouse position onto a
pixel inside the source.

Requires pyobjc (Quartz) in whatever Python interpreter OBS's Script
settings points to:

    pip3 install pyobjc

See README.md in this repo for setup details.
"""

import math

import obspython as obs

try:
    import Quartz

    try:
        from Quartz import CFUUIDCreateString
    except ImportError:
        from CoreFoundation import CFUUIDCreateString

    QUARTZ_OK = True
    QUARTZ_ERROR = None
except Exception as exc:  # noqa: BLE001 - report any import failure, don't guess which
    QUARTZ_OK = False
    QUARTZ_ERROR = str(exc)

HOTKEY_NAME = "pointer_zoom.hold"
HOTKEY_DESC = "Hold: Zoom current source toward pointer (2x)"
DISPLAY_CAPTURE_KIND = "display_capture"

ZOOM_FACTOR = 2.0
EASE_TAU = 0.12  # seconds; smaller = snappier, larger = floatier
EPS = 0.002

hotkey_id = obs.OBS_INVALID_HOTKEY_ID
shift_held = False
current_scene_source = None  # owned ref, released on rebind/unload
selection_dirty = True

_target = None  # cached base-state snapshot of the item we're animating
_active_key = None  # sceneitem id of the item _target refers to
progress = 0.0  # 0 = rest (1x), 1 = fully zoomed (ZOOM_FACTOR x)


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
    return (
        "<h3>Pointer Zoom</h3>"
        "<p>Bind the hotkey below (Settings &gt; Hotkeys &gt; \"%s\") to "
        "Left Shift, or anything else. While held, the current/topmost "
        "Display Capture source zooms in 2x centered on your mouse "
        "pointer, and eases back out on release.</p>" % HOTKEY_DESC
    ) + warning


def script_load(settings):
    global hotkey_id

    hotkey_id = obs.obs_hotkey_register_frontend(HOTKEY_NAME, HOTKEY_DESC, on_hotkey)
    saved = obs.obs_data_get_array(settings, HOTKEY_NAME)
    obs.obs_hotkey_load(hotkey_id, saved)
    obs.obs_data_array_release(saved)

    obs.obs_frontend_add_event_callback(on_frontend_event)
    rebind_scene_signals()
    obs.obs_add_tick_callback(tick)

    if not QUARTZ_OK:
        obs.script_log(obs.LOG_WARNING, "pointer_zoom: Quartz unavailable (%s); zoom disabled" % QUARTZ_ERROR)


def script_unload():
    obs.obs_remove_tick_callback(tick)
    obs.obs_frontend_remove_event_callback(on_frontend_event)
    restore_target_if_any()
    unbind_scene_signals()


def script_save(settings):
    saved = obs.obs_hotkey_save(hotkey_id)
    obs.obs_data_set_array(settings, HOTKEY_NAME, saved)
    obs.obs_data_array_release(saved)


# --------------------------------------------------------------------------
# Hotkey + selection tracking (event-driven, not polled)
# --------------------------------------------------------------------------


def on_hotkey(pressed):
    global shift_held, selection_dirty
    shift_held = pressed
    if pressed:
        # Force a fresh look at "what's selected" the moment the key goes
        # down, in case selection changed while we weren't tracking it.
        selection_dirty = True


def on_frontend_event(event):
    if event == obs.OBS_FRONTEND_EVENT_SCENE_CHANGED:
        restore_target_if_any()
        rebind_scene_signals()


def rebind_scene_signals():
    global current_scene_source, selection_dirty

    unbind_scene_signals()
    current_scene_source = obs.obs_frontend_get_current_scene()
    if current_scene_source:
        handler = obs.obs_source_get_signal_handler(current_scene_source)
        obs.signal_handler_connect(handler, "item_select", on_selection_changed)
        obs.signal_handler_connect(handler, "item_deselect", on_selection_changed)
    selection_dirty = True


def unbind_scene_signals():
    global current_scene_source

    if current_scene_source:
        handler = obs.obs_source_get_signal_handler(current_scene_source)
        obs.signal_handler_disconnect(handler, "item_select", on_selection_changed)
        obs.signal_handler_disconnect(handler, "item_deselect", on_selection_changed)
        obs.obs_source_release(current_scene_source)
        current_scene_source = None


def on_selection_changed(calldata):
    global selection_dirty
    selection_dirty = True


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

    found = {"selected": None, "fallback": None}

    def enum_cb(_scene, item, _param):
        source = obs.obs_sceneitem_get_source(item)
        if obs.obs_sceneitem_selected(item):
            found["selected"] = item
        elif (
            found["fallback"] is None
            and obs.obs_sceneitem_visible(item)
            and obs.obs_source_get_unversioned_id(source) == DISPLAY_CAPTURE_KIND
        ):
            found["fallback"] = item
        return True

    obs.obs_scene_enum_items(scene, enum_cb, None)

    item = found["selected"] or found["fallback"]
    if item is None:
        return None

    source = obs.obs_sceneitem_get_source(item)
    if obs.obs_source_get_unversioned_id(source) != DISPLAY_CAPTURE_KIND:
        return None  # selected item isn't a screen capture; nothing sane to do

    if obs.obs_sceneitem_get_bounds_type(item) != obs.OBS_BOUNDS_NONE:
        return None  # v1 doesn't support "scale to fit" bounds-box items

    settings = obs.obs_source_get_settings(source)
    display_uuid = obs.obs_data_get_string(settings, "display_uuid")
    obs.obs_data_release(settings)

    display_bounds = display_bounds_for_uuid(display_uuid)
    if display_bounds is None:
        return None

    src_w = obs.obs_source_get_width(source)
    src_h = obs.obs_source_get_height(source)
    if src_w <= 0 or src_h <= 0:
        return None

    pos = obs.vec2()
    obs.obs_sceneitem_get_pos(item, pos)
    scale = obs.vec2()
    obs.obs_sceneitem_get_scale(item, scale)

    return {
        "item_id": obs.obs_sceneitem_get_id(item),
        "base_pos": (pos.x, pos.y),
        "base_scale": (scale.x, scale.y),
        "display_bounds": display_bounds,  # (x, y, w, h) in points
        "src_size": (src_w, src_h),
    }


# --------------------------------------------------------------------------
# macOS display / cursor helpers
# --------------------------------------------------------------------------


def display_bounds_for_uuid(uuid_str):
    """(x, y, w, h) in points for the display matching display_uuid, or None."""
    if not QUARTZ_OK or not uuid_str:
        return None

    err, display_ids, count = Quartz.CGGetActiveDisplayList(16, None, None)
    if err != 0:
        return None

    for display_id in list(display_ids)[:count]:
        cfuuid = Quartz.CGDisplayCreateUUIDFromDisplayID(display_id)
        if not cfuuid:
            continue
        if str(CFUUIDCreateString(None, cfuuid)).lower() != uuid_str.lower():
            continue
        bounds = Quartz.CGDisplayBounds(display_id)
        return (
            bounds.origin.x,
            bounds.origin.y,
            bounds.size.width,
            bounds.size.height,
        )
    return None


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


def anchor_point(target):
    """Canvas-space point (in the item's *base* layout) under the cursor."""
    bx, by = target["base_pos"]
    bsx, bsy = target["base_scale"]
    sw, sh = target["src_size"]

    loc = mouse_location()
    dx, dy, dw, dh = target["display_bounds"]
    if loc is None or dw <= 0 or dh <= 0:
        return (bx + (sw * bsx) / 2.0, by + (sh * bsy) / 2.0)

    fx = min(1.0, max(0.0, (loc[0] - dx) / dw))
    fy = min(1.0, max(0.0, (loc[1] - dy) / dh))
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
    global selection_dirty, _target, _active_key, progress

    need_target = shift_held or progress > EPS

    if selection_dirty and need_target:
        candidate = resolve_target()
        candidate_key = candidate["item_id"] if candidate else None

        if _active_key is not None and candidate_key != _active_key and progress > EPS:
            # Selection changed mid-zoom: snap the old item back before
            # switching so nothing gets left stuck zoomed in.
            apply_transform(_target, 1.0)
            progress = 0.0

        _target = candidate
        _active_key = candidate_key
        selection_dirty = False

    goal = 1.0 if (shift_held and _target is not None) else 0.0

    if progress <= EPS and goal == 0.0:
        return

    alpha = 1.0 - math.exp(-seconds / EASE_TAU) if seconds > 0 else 1.0
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

    apply_transform(_target, ZOOM_FACTOR ** progress)
