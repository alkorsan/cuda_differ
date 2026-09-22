"""Image-based overview panel for the Differ plugin.

Replaces the micromap with a custom image control docked to the right side
of the editor's parent form (PROP_HANDLE_PARENT). Unlike the micromap, the
overview is gap-aware: it accounts for the inter-line gaps inserted by Differ
for visual alignment, so the overview stays in sync with what the user
actually sees.

Architecture:
  - PaintboxOverview creates a non-modal dialog with an 'image' control.
  - The dialog is docked to the RIGHT side of PROP_HANDLE_PARENT.
    (Earlier versions used PROP_HANDLE_PARENT2 — the parent-of-parent.
    That was needed because PROP_HANDLE_PARENT was the splitter, which
    caused the overview to jump when side panels were toggled. The
    CudaText author added a grouping panel-control for EdFirst/EdSecond/
    Splitter, so they now are parented by that additional panel.
    PROP_HANDLE_PARENT now returns that stable grouping panel and is the
    recommended docking target. See:
    https://github.com/CudaText-addons/cuda_differ/issues/29)
  - The 'image' control has an embedded bitmap that handles resize/minimize/
    restore automatically — no need for on_act/on_resize/on_show handlers.
  - Uses a two-bitmap optimization (see _ensure_static_bitmap / paint):
    * Static bitmap: a separate persistent bitmap (via bitmap_proc) that
      stores the colored diff segments, the scroll buttons and the
      separator line. Only repainted on compare/resize via
      repaint_static().
    * Dynamic: on each paint(), copy the static bitmap to the image's
      embedded bitmap via CANVAS_BITMAP, then draw the slider and
      grabber on top. This is cheap (one bitmap copy + a few
      CANVAS_RECT / CANVAS_LINE calls).

  - PANEL LAYOUT (like a usual scrollbar):
      +----------------+
      |       ▲        |  BUTTON_HEIGHT px, ▲ scrolls one line up
      |----------------|
      | left | map  |  |  track: the two editors' mini-maps side by side
      | line | area |  |  slider lives here (between the buttons)
      |----------------|
      |       ▼        |  BUTTON_HEIGHT px, ▼ scrolls one line down
      +----------------+
      x=0..SEP_LINE_WIDTH is a grey vertical separator line spanning the
      FULL panel height (the button boxes included), visually separating
      the overview from the editor / the editor's scrollbar.
    The ▲/▼ boxes use the OVERVIEW background color (the theme's
    EdTextBg) so they blend into the panel, and the theme's ScrollArrow
    color for the arrow glyph (read via PROC_THEME_UI_DICT_GET). The
    arrow is a solid triangle drawn with CANVAS_POLYGON (no font glyph:
    a font's ▲/▼ ink and its line box are slightly larger than the
    measured text cell, which made the glyph bleed past the button
    rectangle's edges). It is centered in its box and the box has no
    border, like the scrollbar buttons of editors and browsers.
    Pressing a button scrolls one line at once, and after a short hold it
    starts auto-repeating (like real scrollbar buttons) until released.

  - THEME-COLORED SLIDER (guaranteed visible on every theme):
    The slider starts from the SAME theme colors the editor's own
    scrollbars use for their thumb (CudaText's ATScrollbarTheme):
    fill = ScrollFill, border = ScrollRect, grip lines = ScrollRect
    (read via PROC_THEME_UI_DICT_GET). But those colors are designed
    against the scrollbar TRACK (ScrollBack) -- the overview slider sits
    on the EDITOR background (EdTextBg) instead, and in some themes the
    two collide: ScrollFill == EdTextBg makes the slider invisible,
    ScrollRect == ScrollFill makes the 3 grip lines invisible. So each
    color is then passed through _color_contrast(): minimal ~10% lightness
    steps (hue preserved) until its luminance differs from what it sits
    on by a safe margin -- 0.07 for the big fill block on LIGHT
    backgrounds, where the fill is additionally scaled UP into a light
    band just under the background luminance (the theme's own ScrollFill
    is a mid grey there, and left as-is it reads as "a little bit
    darker" on white themes), 0.20 on dark backgrounds, 0.28 for the
    thin border / grip lines / arrows. This works on black, white and
    grey theme families alike: a fill lighter than the background and
    thin elements already contrasting are kept EXACTLY as the theme
    defines them, a colliding one is shifted just far enough to be
    clearly visible. Computed once per compare in set_colors() -- zero
    per-paint overhead.

  - WINMERGE-STYLE PAINTING (the "Location Pane" approach):
    With huge files (1M lines, 200k diffs) painting every colored line is
    pointless: the panel is only ~600 pixels tall, so thousands of
    consecutive sub-pixel segments would just keep overwriting the same
    pixel rows — you cannot paint half a pixel. WinMerge's LocationView
    solves this by converting only the diff blocks to pixels and SKIPPING
    every block whose end pixel equals the previous block's end pixel
    (it is fully covered by what is already drawn). This plugin does the
    same, plus run coalescing:
      * consecutive same-colored lines merge into one segment;
      * each side's line/gap positions are converted to visual rows ONCE
        via a prefix-sum pass (itertools.accumulate, C speed) instead of
        per-line O(n) Python loops;
      * the draw loop skips every segment whose end pixel equals the
        previous segment's end pixel (the WinMerge
        `nPrevEndY != bottom_coord` rule), so the number of actual
        canvas calls is bounded by the panel's pixel HEIGHT, not by the
        number of diffs — 200k diffs still paint at most ~600 rects per
        side.

  - PROPORTIONAL SLIDER HEIGHT:
    Like real scrollbars in browsers/editors, the slider grows/shrinks
    based on the visible-page vs total-content ratio. CudaText reports:
      smooth_max     = total content height (pixels, INCLUDES page)
      smooth_page    = visible viewport height (pixels)
      smooth_pos     = current scroll position (pixels)
    So slider_height = track_h * smooth_page / smooth_max, clamped to a
    30px minimum so the slider stays grabbable. The slider_top formula
    `py_top = track_y0 + track_h * smooth_pos / smooth_max` is
    mathematically consistent: when smooth_pos reaches its max
    (smooth_max - smooth_page), py_top lands flush at the track bottom.

  - LIVE SLIDER DRAG (the native-scrollbar pipeline):
    A native scrollbar never lets the thumb wait for the text: the
    scrollbar control paints its own thumb at the mouse position
    immediately, while the editor's text repaints asynchronously at
    whatever rate it can paint. The overview mirrors that split
    exactly, with two decoupled streams per RAW mouse move:

    1) THUMB: the slider bitmap is repainted at the mouse-derived
    position (_thumb_preview_y -- the position being DRIVEN to, not the
    round-tripped editor position) and the message queue is pumped
    (app_proc(PROC_IDLE)) so its WM_PAINT is delivered right away --
    but ONLY while the editors are clean. The pump (ProcessMessages)
    delivers EVERYTHING pending: pumping right after invalidating the
    editors would deliver both editors' full viewport paints
    SYNCHRONOUSLY inside the mouse handler -- 100-200 ms per move on
    million-line compares, the "slider waits to follow the mouse" bug.
    Pumping while clean delivers only the tiny overview repaint (~1ms).
    The pump also dispatches pending INPUT messages: mouse MOVES are
    dropped while pumping (breaking the move→pump→move recursion; the
    newest position arrives with the next event); UP / EXIT / DOWN are
    always processed — a release dispatched during a pump must never be
    swallowed, or the drag and the ▲/▼ auto-repeat would survive the
    released button (the "mouse is not released" bug).

    2) TEXT: the scroll position is written to both editors
    (set_prop + the async cmd_RepaintEditor invalidate) at a bounded
    rate — at most every OVERVIEW_TRACK_INTERVAL AND only after the
    editors have PAINTED the previous apply (_editors_dirty cleared by
    note_scroll_painted: TATSynEdit fires on_scroll at the END of every
    viewport paint, a free completion signal wired in from
    __init__.py's on_scroll). At most ONE apply is ever pending, and
    the editors' paints are always delivered by the natural
    message-loop drain between mouse handlers — never synchronously
    inside a handler. Identical consecutive writes are skipped, and a
    deferred one-shot timer applies the pending newest position when
    the mouse stops (WM_TIMER starvation), so the thumb always lands
    exactly under the cursor.

    Net effect: the thumb is glued to the mouse at the input rate; the
    text follows at the editors' paint rate — on huge files that is
    exactly what the native scrollbar delivers (its paint cost is the
    floor for everyone), on small files both are instant.

  - SCROLLING LIKE A NATIVE SCROLLBAR (big-file performance):
    A native scrollbar thumb drag does, per thumb move:
    write the scroll position, then invalidate (InvalidateEx) — the
    editor paints its viewport on the next message-queue drain, never
    synchronously in the scroll handler. The overview does exactly the
    same: every overview-driven scroll (slider drag, track jump, ▲/▼
    repeat) writes set_prop(PROP_SCROLL_VERT_INFO) and follows it with
    ed.cmd(cmd_RepaintEditor) (= Ed.Update(false, true, false) =
    InvalidateEx, the identical async invalidate) — and then returns
    WITHOUT pumping, so the paint lands in the next natural drain.
    No EDACTION_UPDATE is ever issued on these paths: that is a forced
    SYNCHRONOUS full repaint (Invalidate + Update + Repaint on
    Windows), which costs 100+ ms per call on million-line compare
    views.

    Why set_prop alone was not enough (the "text scrolls slowly" bug):
    EditorStringToScrollInfo writes the scroll records and updates
    scrollbar data but never invalidates the editor -- with the built-in
    scrollbars hidden (the overview replaces them) NOTHING repainted the
    editors after an overview-driven write, so the text only moved when
    some unrelated invalidation happened to arrive. The explicit
    cmd_RepaintEditor closes that gap and makes the overview drive the
    editors through the editor's exact native scroll+repaint path.

    The ▲/▼ buttons use the same order per repeat tick (thumb preview
    + cheap pump first, then write + invalidate) but write on EVERY
    tick like a native arrow button: the editor data advances one line
    per 50 ms, so each delivered paint shows the accumulated position
    and the text glides in multi-line steps instead of crawling one
    line per paint (the old "one line every 100-200 ms" — the tick's
    own pump ran the paint pair synchronously and delayed the next
    tick).

    While the overview is driving both halves, __init__.py's on_scroll
    also skips the ScrollSplittedTab mirror (is_driving_scroll): the
    mirror would re-write the lagging half and force a synchronous
    EDACTION_UPDATE full repaint of it.

  See: https://github.com/CudaText-addons/cuda_differ/issues/29
"""

import time
from itertools import accumulate
from operator import itemgetter

import cudatext as ct
import cudatext_cmd as ct_cmd

# Overview dialog width in pixels (docked to the right)
OVERVIEW_WIDTH = 40
# Default width of the overview panel docked to the right of the compare
# view. Was 80px; reduced to 40px (50% smaller) — the overview shows
# both files side-by-side at half-width each, which still gives enough
# resolution to spot colored diff blocks while taking less horizontal
# space. The panel is docked and resizable, so users can drag it wider
# if they want more detail.

# Wall-clock interval (seconds) between slider repaints on the
# track_paint() path (user-initiated scrolls: the editor's own scrollbar,
# keyboard, mouse wheel) and between whole APPLY units while dragging the
# overview slider (one apply unit = write the scroll position to both
# editors + invalidate them). ~33 fps looks instant to the eye, and
# paint() is cheap here (one cached-bitmap copy + the slider drawing --
# the expensive static rectangles never run). During a DRAG the thumb
# itself is NOT gated by this -- it is painted on every raw mouse move
# (see the module docstring, LIVE SLIDER DRAG); this interval only caps
# how often the text position is (re)written and how often on_scroll-
# driven repaints run. The gate is WALL-CLOCK, not a timer: during a drag
# the message queue is flooded with mouse moves and WM_TIMER is only
# delivered when the queue drains, so a timer-driven repaint would
# freeze the slider until the drag ends.
# On huge files the paint-completion gate (OVERVIEW_APPLY_STALL /
# note_scroll_painted) paces applies further to the editors' actual
# paint rate, so no backlog can ever build up: every editor scroll write
# is O(total gap count) inside ATSynEdit (GapsSizeForRange walks) and
# every delivered editor repaint is O(gaps) too -- the differ's
# inter-line alignment gaps make that ~10^5 items on million-line
# compares. A deferred one-shot timer (see OVERVIEW_DEFER_MS) applies
# the newest target when the mouse STOPS, so the thumb always lands
# exactly under the cursor, like a native scrollbar thumb.
OVERVIEW_TRACK_INTERVAL = 0.030

# Deferred-apply one-shot timer interval (ms): armed when a drag move fell
# inside a closed throttle window. Must be >= OVERVIEW_TRACK_INTERVAL so
# a continuing drag never sees it fire; it only fires after the mouse
# stops (WM_TIMER starvation) to apply the coalesced newest position.
OVERVIEW_DEFER_MS = 40

# Safety timeout (seconds) for the paint-completion gate (see
# _editors_ready): apply a new drag position even if no editor paint was
# noted since the last apply. Normally the gate re-opens as soon as the
# editors paint (on_scroll fires at the END of every editor viewport
# paint -- TATSynEdit.Paint -> DoEventScroll), so this only matters if
# paint notifications are delayed or lost (e.g. a hidden window).
OVERVIEW_APPLY_STALL = 0.25

# Control-size cache refresh interval (seconds) for paint(): a cached
# (w, h) avoids a DLG_CTL_PROP_GET bridge round-trip on every slider
# repaint (the drag path paints per RAW mouse move, up to 125 times/s).
# The cache is also refreshed by repaint_static(), so resizes are picked
# up immediately on compare/resize and within this TTL otherwise.
OVERVIEW_SIZE_TTL = 0.20

# Width of the grey vertical separator line at the very left of the
# overview panel (full panel height, buttons included). Separates the
# overview from the editor / the editor's scrollbar.
SEP_LINE_WIDTH = 1
SEP_LINE_COLOR = 0x808080  # grey — visible on both light and dark themes

# --- Slider visibility: guaranteed-contrast color derivation -----------
# Minimum luminance difference between the slider FILL and the overview
# background on DARK backgrounds (bg luminance < 0.5). 0.20 is the
# contrast real scrollbar thumbs have on their track; enough to be
# clearly visible, not garish.
SLIDER_FILL_LUM_DIFF = 0.20
# Minimum luminance difference for the slider FILL on LIGHT backgrounds
# (bg luminance >= 0.5). Deliberately subtle: on a white/light overview
# the thumb must stay a LIGHT grey close to the background (like the
# modern native scrollbar thumb of light themes) instead of the mid-grey
# the theme's own ScrollFill has (e.g. #CDCDCD on the white 'syn'
# theme -- "the slider is a little bit darker on white themes, make it
# more lighter"); the thin ScrollRect border and the 3 grip lines,
# contrasted at SLIDER_LINE_LUM_DIFF, carry the thumb's visual
# definition, so the big fill block only needs a subtle 0.07.
SLIDER_FILL_LUM_DIFF_LIGHT = 0.07
# Minimum luminance difference for the THIN elements: the slider border,
# the 3 grip lines, the ▲/▼ arrows. Thin 1-2px strokes need more contrast
# than a big solid block to read as visible.
SLIDER_LINE_LUM_DIFF = 0.28

# Height of the ▲/▼ scroll button boxes at the top and bottom of the
# overview panel (like a scrollbar's arrow buttons).
BUTTON_HEIGHT = 16
# ▲/▼ arrow geometry: the solid triangle drawn in each button box is
# (2*ARROW_HALF_WIDTH+1) x (2*ARROW_HALF_HEIGHT+1) px centered in the box
# (9x7 px in a 16px box, leaving >=3px margin on every side). Drawn with
# CANVAS_POLYGON -- a fixed geometric shape always fits inside the button
# rectangle, unlike a font glyph whose ink + line box bleed past the
# measured text cell (the "arrows drawn outside their rectangle" bug).
ARROW_HALF_WIDTH = 4
ARROW_HALF_HEIGHT = 3

# Auto-repeat behaviour of the ▲/▼ buttons, mimicking Windows scrollbar
# arrow buttons: the first click scrolls one line; if the button is kept
# pressed, after this initial delay the scrolling repeats at the repeat
# interval until the button is released.
BUTTON_INITIAL_DELAY_MS = 400
BUTTON_REPEAT_MS = 50


def _color_lum(color):
    """Perceptual luminance (0..1) of a CudaText BGR int color.

    CudaText colors are 0xBBGGRR ints (low byte = red), so the channel
    extraction below is r/g/b. The weights are the standard Rec. 709
    luminance weights -- human eyes are far more sensitive to green than
    to blue, so a green shift matters much more than the same blue shift.
    """
    r = (color & 0xFF) / 255.0
    g = ((color >> 8) & 0xFF) / 255.0
    b = ((color >> 16) & 0xFF) / 255.0
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _color_shift(color, darker):
    """One lightness step (per channel) of a BGR int color.

    The step is max(8, ~10% of the channel value) so even a pure black
    color makes progress (a purely multiplicative step would keep 0 at 0
    forever, and pure white could not be lifted). Scaling preserves the
    HUE, so a shifted theme color still reads as the theme's own color,
    just lighter/darker. 40 such steps span the whole 0..255 range.
    """
    r = color & 0xFF
    g = (color >> 8) & 0xFF
    b = (color >> 16) & 0xFF
    if darker:
        r = max(0, r - max(8, r // 10))
        g = max(0, g - max(8, g // 10))
        b = max(0, b - max(8, b // 10))
    else:
        r = min(255, r + max(8, r // 10))
        g = min(255, g + max(8, g // 10))
        b = min(255, b + max(8, b // 10))
    return r | (g << 8) | (b << 16)


def _slider_fill_min_diff(bg):
    """Minimum fill-vs-background luminance difference for a given
    background color: light backgrounds (lum >= 0.5) use the subtler
    SLIDER_FILL_LUM_DIFF_LIGHT so the thumb stays a LIGHT grey close to
    the theme's own ScrollFill; dark backgrounds keep the strong
    SLIDER_FILL_LUM_DIFF."""
    if _color_lum(bg) >= 0.5:
        return SLIDER_FILL_LUM_DIFF_LIGHT
    return SLIDER_FILL_LUM_DIFF


def _color_scale_to_lum(color, target_lum):
    """Scale a color's channels proportionally so its luminance becomes
    target_lum (hue preserved).

    Returns None when scaling is impossible (pure black) or the result
    would clamp away the target (the caller checks the luminance anyway).
    Luminance is linear in the channels, so a single factor k hits the
    target exactly for greys and near-exactly for colors -- unlike the
    ~10% channel steps of _color_shift, which always OVERSHOOT the
    threshold."""
    r = color & 0xFF
    g = (color >> 8) & 0xFF
    b = (color >> 16) & 0xFF
    l0 = _color_lum(color)
    if l0 <= 1e-9:
        return None
    k = target_lum / l0
    nr = min(255, max(0, int(round(r * k))))
    ng = min(255, max(0, int(round(g * k))))
    nb = min(255, max(0, int(round(b * k))))
    return nr | (ng << 8) | (nb << 16)


def _color_contrast_light(color, base, min_diff):
    """_color_contrast for the slider FILL on LIGHT backgrounds
    (bg luminance >= 0.5): the fill is scaled to a FIXED light target
    luminance just under the background's (base_lum - min_diff)
    instead of _color_contrast's minimal-nudge behavior.

    WHY: the theme's own ScrollFill is designed against the scrollbar
    TRACK (ScrollBack), and on light themes it is usually a MID grey
    (e.g. #CDCDCD on the white 'syn' theme) -- kept as-is it reads as
    "the slider is a little bit darker" on the white/light overview
    background. The user asked for a LIGHT thumb (the modern native
    scrollbar look): scale the fill UP to a light band just under the
    background luminance (hue preserved by the proportional channel
    scaling, so tinted themes keep their tint); the thin ScrollRect
    border and grip lines (contrasted at SLIDER_LINE_LUM_DIFF) provide
    the thumb's definition. A fill already on the LIGHT side of the
    background keeps the exact-contrast contract: unchanged when
    already visible, otherwise lifted to base_lum + min_diff.

    Falls back to _color_contrast when the exact scaling cannot meet
    the difference (pure black / saturation clamping). Dark
    backgrounds and the thin elements (border/grips/arrows) keep the
    step-loop look (dark themes were approved as-is).
    """
    base_lum = _color_lum(base)
    out_lum = _color_lum(color)
    if out_lum > base_lum:
        # Lighter than the background: keep it when already visible
        # (native light-on-grey look); else lift it further up.
        if out_lum - base_lum >= min_diff - 1e-9:
            return color
        for margin in (0.0, 0.02):
            out = _color_scale_to_lum(color, base_lum + min_diff + margin)
            if out is not None and \
                    _color_lum(out) - base_lum >= min_diff - 1e-9:
                return out
        return _color_contrast(color, base, min_diff)
    # On the dark side of the background (or equal): scale UP to
    # exactly base_lum - min_diff -- even when the raw contrast would
    # already pass, a mid-grey fill is exactly the "a little bit
    # darker" look light themes must not keep.
    for margin in (0.0, 0.02):
        target = base_lum - (min_diff + margin)
        if target >= 0.0:
            out = _color_scale_to_lum(color, target)
            if out is not None and \
                    base_lum - _color_lum(out) >= min_diff - 1e-9:
                return out
    return _color_contrast(color, base, min_diff)


def _color_contrast(color, base, min_diff):
    """Return `color` minimally lightness-shifted until its luminance
    differs from `base` by at least min_diff (or the color unchanged when
    it already does).

    WHY THIS EXISTS: the native scrollbar theme colors (ScrollFill /
    ScrollRect / ScrollArrow) are designed to sit on the scrollbar TRACK
    (ScrollBack) -- but the overview slider sits on the EDITOR background
    (EdTextBg). In several themes those two backgrounds differ, and a
    ScrollFill that is clearly visible on ScrollBack is EXACTLY the
    editor background color (e.g. all-white themes: white thumb on a
    white overview -> invisible slider; themes where ScrollRect ==
    ScrollFill -> invisible grip lines). Starting from the theme's own
    color and shifting lightness ONLY while the contrast is insufficient
    keeps the theme's hue/character while guaranteeing visibility on
    every theme family (black, white, grey).

    Shifting direction: away from `base`'s luminance on whichever side
    `color` already is (a color lighter than base gets lighter, a darker
    one darker). If that direction saturates at pure white / pure black,
    the direction flips once and keeps going (e.g. a white grip on a
    light-grey slider becomes a darker grey instead -- any side with
    enough contrast wins).

    Args:
        color: BGR int color to fix (theme color)
        base: BGR int color it must be visible against (the background)
        min_diff: required luminance difference, 0..1
    """
    base_lum = _color_lum(base)
    out = color
    out_lum = _color_lum(out)
    darker = not (out_lum > base_lum or
                  (out_lum == base_lum and base_lum < 0.5))
    for _ in range(40):
        out_lum = _color_lum(out)
        if abs(out_lum - base_lum) >= min_diff - 1e-9:
            return out
        nxt = _color_shift(out, darker)
        if nxt == out:
            # saturated at pure black / pure white: go the other way
            darker = not darker
            nxt = _color_shift(out, darker)
            if nxt == out:
                break  # both directions saturated: impossible here
        out = nxt
    # fallback (not reachable in practice): maximum contrast
    return 0x000000 if _color_lum(out) < base_lum else 0xFFFFFF


class PaintboxOverview:
    """Manages a docked image overview panel for a compare tab.

    The overview shows a gap-aware mini-map of both editors side by side.
    Created once per compare tab, destroyed when the tab closes.
    """

    def __init__(self):
        """Initialize the overview with empty state and default colors."""
        self.h_dlg = None       # dialog handle
        self.h_image = None     # image control handle
        self.h_bitmap = None    # image's embedded bitmap handle
        self.h_canvas = None    # image's embedded bitmap canvas handle
        self._ctl_index = None  # control index in the dialog
        self._owns_dlg = False  # True if we created a separate dialog
        # Static bitmap (persistent — only repainted on compare/resize).
        # Stores the colored diff segments, the scroll buttons and the
        # separator line so they don't need to be repainted on every
        # scroll. See paint() for how it's used.
        self._h_static_bmp = None
        self._h_static_cnv = None
        self._static_w = 0
        self._static_h = 0
        # Line states per side: {line: color}. Filled by add_line_state()
        # while the compare events are painted.
        self.line_states_a = {}
        self.line_states_b = {}
        # Gap info: list of (after_line, gap_visual_rows, ignored) per gap
        self.gaps_a = []  # list of (after_line, gap_visual_rows, ignored)
        self.gaps_b = []
        # Per-line visual row counts (for wrap-aware height computation).
        # If None, each line is 1 visual row. Set via set_wrap_counts().
        self.wrap_counts_a = None  # list where [i] = visual rows for line i
        self.wrap_counts_b = None
        # Editor references and line counts
        self.a_ed = None
        self.b_ed = None
        self.a_line_count = 0
        self.b_line_count = 0
        # Colors (overridden by set_colors() with theme + config values)
        self.color_bg = 0xFFFFFF       # overridden by set_colors() with theme bg
        self.color_deleted = 0xAAAAAA  # overridden by set_colors()
        self.color_added = 0xAAAAAA    # overridden by set_colors()
        self.color_changed = 0xAAAAAA  # overridden by set_colors()
        self.color_gap = 0xEEEEEE      # overridden by set_colors()
        self.color_ignored_gap = 0xEEEEEE  # overridden by set_colors()
        # ▲/▼ button colors: the box background is the OVERVIEW
        # background (set by set_colors -- the buttons blend into the
        # panel); the arrow glyph is the theme's ScrollArrow color,
        # refreshed by set_colors() from PROC_THEME_UI_DICT_GET.
        self.color_btn_bg = 0xFFFFFF
        self.color_btn_arrow = 0x000000
        # Slider state for drag-to-scroll
        self._slider_top = 0
        self._slider_height = 0
        self._overview_height = 0
        self._track_y0 = 0
        self._track_h = 0
        self._dragging = False
        self._drag_offset = 0
        # Newest requested drag position (slider-top pixel) not yet written
        # to the editors -- set by drag moves that fell inside a closed
        # throttle window; consumed by the next open-window move, the
        # deferred catch-up timer, or the drag end (up/exit/self-heal).
        # None = no pending position (everything applied).
        self._drag_target_y = None
        # True while the deferred catch-up timer (_drag_deferred_tick) is
        # armed; avoids re-arming the same one-shot on every skipped move.
        self._defer_armed = False
        self._smooth_max = 0
        # Wall-clock timestamp of the last track_paint(); gates the
        # immediate slider repaints to OVERVIEW_TRACK_INTERVAL.
        self._track_last_paint = 0.0
        # True while the overview is DRIVING both editors' scroll
        # positions itself (slider drag, track jump, ▲/▼ auto-repeat).
        # __init__.py's on_scroll checks is_driving_scroll() to skip the
        # ScrollSplittedTab mirror then -- the overview writes both
        # halves itself, and the mirror's redundant write + synchronous
        # EDACTION_UPDATE full repaint of the lagging half is exactly the
        # 300-500ms-per-move cost that made big-file drags feel slow.
        # (See the module docstring, "SCROLLING LIKE A NATIVE SCROLLBAR".)
        self._driving = False
        # Re-entrancy guard for the message pump (_repaint_control_now):
        # True while app_proc(PROC_IDLE) dispatches pending messages so
        # a nested pump can never recurse.
        self._pumping = False

        # --- Native-scrollbar drag pipeline state -------------------
        # Track-pixel y the slider is shown at while the overview drives
        # the scroll (slider drag / track jump / ▲/▼ repeat): the thumb is
        # painted at the position we are DRIVING to (straight from the
        # mouse / the tick target), NOT at the round-tripped editor
        # position -- a native scrollbar thumb is also glued to the mouse
        # while the editor's text repaint catches up. None = not driving,
        # paint() reads the truth from PROP_SCROLL_VERT_INFO.
        self._thumb_preview_y = None
        # True from our own ed.cmd(cmd_RepaintEditor) invalidation until
        # an editor viewport paint is noted (note_scroll_painted -- on_scroll
        # fires at the END of every editor paint). While True the drag
        # pipeline does NOT pump the message queue (the pump would deliver
        # the pending editor paints SYNCHRONOUSLY inside the mouse
        # handler -- the old 100-200 ms per-move freeze on million-line
        # compares) and does not apply a new position (no backlog: at
        # most one scroll write is ever pending).
        self._editors_dirty = False
        # Wall clock of the invalidation that set _editors_dirty.
        self._editors_dirty_at = 0.0
        # Wall clock of the last drag APPLY unit (write+invalidate).
        self._last_apply = 0.0
        # Last smooth position written by _write_scroll_position (drag /
        # jump / repeat). Identical consecutive writes are skipped: the
        # write is a pure no-op for the editor data, and the paired
        # cmd_RepaintEditor would only force a full viewport repaint of
        # identical content (50-100 ms wasted per call on huge files).
        # Reset to None when an interaction starts/ends so external
        # scrolls can never make the dedup stale.
        self._last_written_pos = None
        # smooth_pos_last / smooth_page from the last truth read of
        # PROP_SCROLL_VERT_INFO (used to clamp written targets and map
        # positions to preview thumb pixels).
        self._smooth_pos_last = 0
        self._smooth_page = 0
        # Cached control size (w, h, fetched_at) -- see OVERVIEW_SIZE_TTL.
        self._size_cache = (0, 0, 0.0)

        # --- ▲/▼ button auto-repeat state ---
        # _btn_dir: None while no button is held, -1 while ▲ is held,
        # +1 while ▼ is held. Drives the one-line scroll + auto-repeat.
        self._btn_dir = None

        # Slider fill color: theme ScrollFill, then lightness-adjusted by
        # _color_contrast in set_colors() until clearly visible against
        # the OVERVIEW background (EdTextBg). Solid fill -- one CANVAS_RECT
        # call, no transparency, no per-row overhead.
        self._slider_fill = 0xEAEAEA
        # Border color: theme ScrollRect, contrast-corrected against the
        # overview background. Refreshed by set_colors() from the UI theme.
        self._slider_border = 0x666666
        # Grabber line color (3 horizontal lines in the slider middle):
        # theme ScrollRect (the native thumb decor color), contrast-
        # corrected against the slider FILL so the grips stay visible even
        # in themes where ScrollRect == ScrollFill.
        self._slider_grabber_dark = 0x666666
        # Grabber geometry: 3 lines, 2px thick, 6px apart (center-to-center).
        # Triple the original 2px spacing; 2x the original 1px thickness.
        self._slider_grabber_thickness = 2
        self._slider_grabber_spacing = 6
        # Min slider height in pixels — keeps the slider grabbable even
        # when the file is much taller than the viewport.
        self._slider_min_height = 30

    def is_created(self):
        """Return True if the overview dialog has been created."""
        return self.h_dlg is not None

    def create(self, a_ed, b_ed):
        """Create the overview as a separate dialog docked to the right
        side of the editor's parent form (PROP_HANDLE_PARENT).

        Uses PROP_HANDLE_PARENT — the handle of the (borderless) dialog
        parenting the editor. As of recent CudaText builds, an additional
        grouping panel-control parents EdFirst/EdSecond/Splitter, so
        PROP_HANDLE_PARENT now returns a stable handle suitable for
        docking. Earlier versions of this plugin used PROP_HANDLE_PARENT2
        (parent-of-parent) because PROP_HANDLE_PARENT used to be the
        splitter itself, which caused the overview to jump to the middle
        when side panels (like the Tabs sidebar) were toggled. With the
        new grouping panel in place, PROP_HANDLE_PARENT2 is no longer
        needed and is also not compatible with dlg_proc() (it returns
        the raw TFrame Lazarus handle). The CudaText author confirmed:
        "PROP_HANDLE_PARENT is enough for docked micromap, PROP_HANDLE_PARENT2
        is not needed anymore."

        Args:
            a_ed: left editor (primary)
            b_ed: right editor (secondary)
        """
        self.a_ed = a_ed
        self.b_ed = b_ed
        # PROP_HANDLE_PARENT: handle of the (borderless) dialog parenting
        # the editor. Since CudaText added the grouping panel for
        # EdFirst/EdSecond/Splitter, this returns a stable handle suitable
        # for DLG_DOCK. PROP_HANDLE_PARENT2 is no longer used.
        h_parent = a_ed.get_prop(ct.PROP_HANDLE_PARENT)
        if not h_parent:
            h_parent = 0

        # Create a separate dialog for the overview
        self.h_dlg = ct.dlg_proc(0, ct.DLG_CREATE)
        self._owns_dlg = True
        ct.dlg_proc(self.h_dlg, ct.DLG_PROP_SET, prop={
            'cap': 'Overview',
            'w': OVERVIEW_WIDTH,
            'h': 600,
            'border': ct.DBORDER_NONE,
            'color': self.color_bg,
            # No on_resize/on_show/on_act handlers needed — the 'image'
            # control handles resize/minimize/restore automatically via
            # its embedded bitmap.
        })

        # Add 'image' control (replaces deprecated 'paintbox'). The image
        # control has an embedded bitmap that handles resize/minimize/
        # restore automatically — no flicker, no manual resize handling.
        self._ctl_index = ct.dlg_proc(self.h_dlg, ct.DLG_CTL_ADD, 'image')
        ct.dlg_proc(self.h_dlg, ct.DLG_CTL_PROP_SET, index=self._ctl_index, prop={
            'name': 'overview_image',
            'align': ct.ALIGN_CLIENT,
            'on_mouse_down': self._on_mouse_down,
            'on_mouse_move': self._on_mouse_move,
            'on_mouse_up': self._on_mouse_up,
            # Without mouse capture, a button released OUTSIDE the control
            # never delivers on_mouse_up — stop the ▲/▼ auto-repeat (and
            # any drag) when the pointer leaves the panel instead.
            'on_mouse_exit': self._on_mouse_exit,
        })

        # Get the image control handle and its embedded bitmap + canvas
        self.h_image = ct.dlg_proc(self.h_dlg, ct.DLG_CTL_HANDLE, index=self._ctl_index)
        self.h_bitmap = ct.image_proc(self.h_image, ct.IMAGE_GET_BITMAP)
        self.h_canvas = ct.bitmap_proc(self.h_bitmap, ct.BITMAP_GET_CANVAS)

        # Dock to the RIGHT side of the editor's parent-of-parent form
        ct.dlg_proc(self.h_dlg, ct.DLG_SHOW_NONMODAL)
        ct.dlg_proc(self.h_dlg, ct.DLG_DOCK, prop='R', index=h_parent)

    def destroy(self):
        """Undock and free the overview dialog and static bitmap."""
        self._stop_button_repeat()
        self._driving = False
        self._dragging = False
        self._drag_target_y = None
        self._thumb_preview_y = None
        self._last_written_pos = None
        if self._defer_armed:
            self._defer_armed = False
            ct.timer_proc(ct.TIMER_STOP, self._drag_deferred_tick,
                          OVERVIEW_DEFER_MS)
        self._free_static_bitmap()
        if self.h_dlg is not None:
            try:
                if self._owns_dlg:
                    ct.dlg_proc(self.h_dlg, ct.DLG_UNDOCK)
                    ct.dlg_proc(self.h_dlg, ct.DLG_FREE)
                else:
                    ct.dlg_proc(self.h_dlg, ct.DLG_CTL_DELETE, index=self._ctl_index)
            except Exception:
                pass
            self.h_dlg = None
            self.h_image = None
            self.h_bitmap = None
            self.h_canvas = None
            self._ctl_index = None

    def _free_static_bitmap(self):
        """Free the persistent static bitmap if it exists."""
        if self._h_static_bmp is not None:
            try:
                ct.bitmap_proc(self._h_static_bmp, ct.BITMAP_FREE)
            except Exception:
                pass
            self._h_static_bmp = None
            self._h_static_cnv = None

    def _ensure_static_bitmap(self, w, h):
        """Create or resize the persistent static bitmap to match (w, h).

        The static bitmap stores the colored diff segments, the scroll
        buttons and the separator line (the expensive part). It's only
        repainted here when the size changes or when repaint_static() is
        called after a fresh compare.

        Once created, the static bitmap is reused on every paint() call
        — paint() just copies it to the image's embedded bitmap and draws
        the slider on top, which is very fast.
        """
        if self._h_static_bmp is not None:
            if self._static_w == w and self._static_h == h:
                return  # still valid, reuse
            self._free_static_bitmap()
        self._h_static_bmp = ct.bitmap_proc(0, ct.BITMAP_CREATE, w, h)
        self._h_static_cnv = ct.bitmap_proc(self._h_static_bmp, ct.BITMAP_GET_CANVAS)
        self._static_w = w
        self._static_h = h
        self._paint_static(self._h_static_cnv, w, h)

    def set_colors(self, color_bg, color_deleted, color_added, color_changed,
                   color_gap, color_ignored_gap=None):
        """Set the colors used for painting the overview.

        Also refreshes the button and slider colors from the current UI
        theme (PROC_THEME_UI_DICT_GET) — called on every compare start,
        so a theme switch is picked up by the next compare:

          * ▲/▼ boxes: the OVERVIEW background (color_bg, the theme's
            EdTextBg) so the buttons blend into the panel;
          * slider / arrows: the theme's native scrollbar colors
            (ScrollFill / ScrollRect / ScrollArrow), each then passed
            through _color_contrast() -- minimal lightness shifts that
            guarantee the slider is visible against the overview
            background and the 3 grip lines are visible against the
            slider fill. This is needed because native scrollbar colors
            are designed against the scrollbar TRACK (ScrollBack), not
            the editor background the overview uses: in white themes
            ScrollFill often == EdTextBg (invisible slider) and in
            several themes ScrollRect == ScrollFill (invisible grips).
            Themes whose colors already contrast well are kept EXACTLY
            as-is.

        Falls back to neutral values if the theme dict cannot be read.

        Args:
            color_bg: background color (theme EdTextBg)
            color_deleted: color for deleted lines (config color_deleted)
            color_added: color for added lines (config color_added)
            color_changed: color for changed lines (config color_changed)
            color_gap: color for gap rectangles (config color_gaps)
            color_ignored_gap: color for the gap rectangles that compensate
                DIFF_IGN_BLANK_LINES-suppressed ("ignored") differences
                (config color_ignored_gap). Optional: when omitted, ignored
                gaps fall back to color_gap (pre-extension behavior).
        """
        self.color_bg = color_bg
        self.color_deleted = color_deleted
        self.color_added = color_added
        self.color_changed = color_changed
        self.color_gap = color_gap
        if color_ignored_gap is not None:
            self.color_ignored_gap = color_ignored_gap
        # The ▲/▼ boxes use the overview background so they blend into
        # the panel (the old ScrollBack box made the buttons stand out
        # as a colored strip against the map area).
        self.color_btn_bg = color_bg
        # Button arrow + slider colors from the UI theme (the same
        # source the editor's own scrollbars use).
        try:
            ui = ct.app_proc(ct.PROC_THEME_UI_DICT_GET, '')
            arrow = ui.get('ScrollArrow', {}).get('color', 0x000000)
            fill = ui.get('ScrollFill', {}).get('color', 0xEAEAEA)
            border = ui.get('ScrollRect', {}).get('color', 0x666666)
        except Exception:
            arrow = 0x000000
            fill = 0xEAEAEA
            border = 0x666666
        # Guaranteed-visibility pass (see the module docstring and
        # _color_contrast): the native scrollbar colors sit on the
        # scrollbar track in real scrollbars, but here they sit on the
        # editor background / the slider fill -- nudge lightness until
        # each element is clearly visible on ANY theme (black, white,
        # grey). Already-contrasting colors pass through unchanged, so
        # well-designed themes keep their exact look.
        #
        # The FILL threshold is background-dependent: dark backgrounds
        # keep the strong 0.20 diff with the approved step-loop look,
        # LIGHT backgrounds use the subtle 0.07 and scale the fill to a
        # FIXED light band just under the background luminance -- the
        # theme's own ScrollFill is a mid grey on light themes (designed
        # against the scrollbar track, not the editor background), and
        # left as-is it reads as "the slider is a little bit darker on
        # white themes, make it more lighter" -- the 0.28-contrasted
        # border + grips carry the thumb's visual definition.
        if _color_lum(color_bg) >= 0.5:
            self._slider_fill = _color_contrast_light(
                fill, color_bg, SLIDER_FILL_LUM_DIFF_LIGHT)
        else:
            self._slider_fill = _color_contrast(fill, color_bg,
                                                SLIDER_FILL_LUM_DIFF)
        self._slider_border = _color_contrast(border, color_bg,
                                              SLIDER_LINE_LUM_DIFF)
        self._slider_grabber_dark = _color_contrast(
            border, self._slider_fill, SLIDER_LINE_LUM_DIFF)
        self.color_btn_arrow = _color_contrast(arrow, color_bg,
                                               SLIDER_LINE_LUM_DIFF)

    def set_line_counts(self, a_count, b_count):
        """Set the total line counts for both editors (without gaps)."""
        self.a_line_count = a_count
        self.b_line_count = b_count

    def set_wrap_counts(self, wrap_a, wrap_b):
        """Set per-line visual row counts for wrap-aware height computation.

        Args:
            wrap_a: list where wrap_a[i] = visual rows for line i in a_ed,
                    or None if wrapping is off (each line = 1 row).
            wrap_b: same for b_ed.
        """
        self.wrap_counts_a = wrap_a
        self.wrap_counts_b = wrap_b

    def add_line_state(self, side, line, color):
        """Record that a line has a specific color (deleted/added/changed).

        Args:
            side: 'a' or 'b'
            line: line index (0-based)
            color: RGB int color
        """
        if side == 'a':
            self.line_states_a[line] = color
        else:
            self.line_states_b[line] = color

    def add_gap(self, side, after_line, gap_visual_rows, ignored=False):
        """Record a gap inserted after a line.

        Args:
            side: 'a' or 'b'
            after_line: the line index after which the gap was inserted
            gap_visual_rows: number of visual rows the gap occupies
            ignored: True for gaps that compensate a suppressed all-blank
                hunk (DIFF_IGN_BLANK_LINES "ignored differences") -- they
                are painted with color_ignored_gap instead of color_gap.
        """
        if side == 'a':
            self.gaps_a.append((after_line, gap_visual_rows, bool(ignored)))
        else:
            self.gaps_b.append((after_line, gap_visual_rows, bool(ignored)))

    def clear_data(self):
        """Clear all collected line states and gaps. Called before a
        fresh compare."""
        self.line_states_a.clear()
        self.line_states_b.clear()
        self.gaps_a.clear()
        self.gaps_b.clear()

    # ------------------------------------------------------------------
    # Layout helpers
    # ------------------------------------------------------------------

    def _track_rect(self, h):
        """(y, height) of the slider track — the map area between the
        ▲/▼ button boxes. The slider and the mini-maps live here."""
        track_h = h - 2 * BUTTON_HEIGHT
        if track_h < 1:
            # Degenerate tiny panel: no room for buttons, use everything.
            return 0, max(1, h)
        return BUTTON_HEIGHT, track_h

    def _get_size(self):
        """Get the current image control size (w, h) in pixels."""
        props = ct.dlg_proc(self.h_dlg, ct.DLG_CTL_PROP_GET, index=self._ctl_index)
        w = props.get('w', OVERVIEW_WIDTH)
        h = props.get('h', 600)
        return w, h

    def _get_size_cached(self):
        """Get the control size through the OVERVIEW_SIZE_TTL cache.

        paint() runs per RAW mouse move while dragging (up to ~125/s);
        a DLG_CTL_PROP_GET marshals the whole control-property dict across
        the Python<->Pascal bridge each call, so the cached entry keeps the
        per-move cost to the bitmap copy + slider drawing only. Resizes
        are picked up by repaint_static() (immediate) or within the TTL."""
        w, h, fetched = self._size_cache
        if w > 0 and h > 0:
            now = time.monotonic()
            if now - fetched < OVERVIEW_SIZE_TTL:
                return w, h
        w, h = self._get_size()
        self._size_cache = (w, h, time.monotonic())
        return w, h

    # ------------------------------------------------------------------
    # Static painting (WinMerge "Location Pane" model)
    # ------------------------------------------------------------------

    def _paint_static(self, c, w, h):
        """Paint the static part (background, diff segments, buttons,
        separator) on the given canvas. Only runs on compare/resize.

        The heavy work is the per-side segment painting
        (_paint_side_segments), which uses the WinMerge Location Pane
        approach: lines are converted to pixel segments once (prefix
        sums), consecutive same-colored lines are coalesced into runs,
        and every segment that would collapse onto already-painted pixels
        is skipped — so the canvas call count is bounded by the panel's
        pixel height, not by the file's line/diff count.

        Args:
            c: canvas handle (static bitmap canvas)
            w: width in pixels
            h: height in pixels
        """
        # Clear background
        ct.canvas_proc(c, ct.CANVAS_SET_BRUSH, color=self.color_bg, style=ct.BRUSH_SOLID)
        ct.canvas_proc(c, ct.CANVAS_RECT_FILL, x=0, y=0, x2=w, y2=h)

        # Map area: between the buttons, right of the separator line.
        track_y0, track_h = self._track_rect(h)
        x_start = SEP_LINE_WIDTH
        half_w = (w - SEP_LINE_WIDTH) // 2
        if track_h > 0:
            self._paint_side_segments(c, 'a', x_start, x_start + half_w,
                                      track_y0, track_h)
            self._paint_side_segments(c, 'b', x_start + half_w, w,
                                      track_y0, track_h)

        # ▲/▼ buttons and the separator line on top
        self._paint_buttons(c, w, h)
        self._paint_separator(c, w, h)

    def _prefix_sums(self, side):
        """One-pass conversion of a side's line/gap layout to visual rows.

        Returns (cum, total, gap_map):
          cum[i]     visual row where line i starts (all gaps above it
                     included); cum[n] = end of the last line (trailing
                     gaps excluded);
          total      total visual rows (lines + all gaps, trailing ones
                     included);
          gap_map    position -> [total_rows, ignored_rows]: a gap
                     "after line L" sits before line L+1, so its position
                     is L+1 clamped to n (positions >= n are trailing
                     gaps after the last line); several gaps at the same
                     position accumulate.

        Built with itertools.accumulate (C speed) — a 1M-line side is
        one pass instead of the per-line Python loops the old code ran
        (separate O(n) walks in _compute_visual_height, _get_scale and
        the paint loop itself).
        """
        if side == 'a':
            n, gaps, wrap = self.a_line_count, self.gaps_a, self.wrap_counts_a
        else:
            n, gaps, wrap = self.b_line_count, self.gaps_b, self.wrap_counts_b

        gap_map = {}
        for after, rows, ign in gaps:
            # Gap "after line L" sits before line L+1. Clamp to [0, n]:
            # L >= n is a trailing gap (after the last line), and a
            # defensive L < 0 is treated as "before line 0" so an
            # out-of-range position can never write through a negative
            # list index below.
            if after < 0:
                pos = 0
            elif after > n:
                pos = n
            else:
                pos = after
            ent = gap_map.setdefault(pos, [0, 0])
            ent[0] += rows
            if ign:
                ent[1] += rows

        # Gaps that shift line starts (positions < n); gaps at n are
        # trailing and only extend the total height.
        gap_shift = [0] * (n + 1)
        trailing = 0
        for pos, ent in gap_map.items():
            if pos < n:
                gap_shift[pos] += ent[0]
            else:
                trailing += ent[0]

        # cum[i] = (visual rows of lines 0..i-1) + (gap rows at
        # positions <= i) — the accumulated gap shift is aligned with the
        # line prefix by construction. Shortcut when no gaps shift
        # anything: cum is just the line prefix itself (range object,
        # no 1M-element list allocation).
        if wrap is not None and len(wrap) >= n:
            line_prefix = accumulate(wrap[:n], initial=0)
        else:
            line_prefix = range(n + 1)
        has_shifting_gaps = any(ent[0] for pos, ent in gap_map.items()
                                if pos < n)
        if has_shifting_gaps:
            gap_acc = accumulate(gap_shift)
            cum = [lp + ga for lp, ga in zip(line_prefix, gap_acc)]
        elif wrap is not None and len(wrap) >= n:
            cum = list(line_prefix)
        else:
            cum = range(n + 1)
        total = cum[n] + trailing
        return cum, total, gap_map

    def _build_segments(self, side, cum=None, gap_map=None):
        """Build the colored segments of one side, in visual order.

        Returns a list of (start_row, end_row, color) tuples:
          - line runs: consecutive lines with the same color, merged
            when they are visually adjacent (no gap between them) —
            one rect instead of per-line rects;
          - gaps: one rect per gap position, split into its ignored
            portion (if any) and its regular portion.

        The list is ascending by start_row (paint order).

        cum / gap_map may be passed in (from a _prefix_sums call the
        caller already made) or computed here when omitted.
        """
        if side == 'a':
            n, wrap = self.a_line_count, self.wrap_counts_a
            states = self.line_states_a
        else:
            n, wrap = self.b_line_count, self.wrap_counts_b
            states = self.line_states_b
        if cum is None or gap_map is None:
            cum, _total, gap_map = self._prefix_sums(side)

        # Inlined line_rows() (was one FUNCTION CALL per state line --
        # 1.6M calls on a 1M-line compare): line >= 0 is guaranteed by
        # the range filter below, so the wrap lookup is a plain index.
        # wrap[i] > 0 -> i rows, else 1 -- same as the original method.
        wl = len(wrap) if wrap is not None else 0
        wrap_hot = wrap if wl else None

        # Line runs: consecutive same-colored lines merge while they are
        # visually adjacent (line == prev+1 AND no gap before this line).
        runs = []
        prev_line = -2
        for line, color in sorted(states.items()):
            if line < 0 or line >= n:
                continue
            if wrap_hot is not None and line < wl:
                rows = wrap[line]
                if rows < 1:
                    rows = 1
            else:
                rows = 1
            end_row = cum[line] + rows
            if (runs and line == prev_line + 1
                    and line not in gap_map
                    and runs[-1][2] == color):
                runs[-1][1] = end_row  # extend the current run
            else:
                runs.append([cum[line], end_row, color])
            prev_line = line

        # Gap segments (positions in ascending order). The ignored
        # portion of a gap (a DIFF_IGN_BLANK_LINES-suppressed hunk's
        # compensating rows) is emitted as its OWN segment covering the
        # top of the rect, and the regular gap color covers the rest —
        # two non-overlapping segments instead of a paint-on-top
        # overlay, so the dedup rule can never discard the ignored
        # color of a fully-ignored gap (same final look as the old
        # base-then-overlay layering).
        gap_segs = []
        for pos in sorted(gap_map):
            rows_total, rows_ign = gap_map[pos]
            if rows_total <= 0:
                continue
            if pos < n:
                start = cum[pos] - rows_total
            else:
                start = cum[n]  # trailing: right after the last line
            ign_rows = min(rows_ign, rows_total)
            if ign_rows > 0:
                gap_segs.append((start, start + ign_rows,
                                 self.color_ignored_gap))
            if ign_rows < rows_total:
                gap_segs.append((start + ign_rows, start + rows_total,
                                 self.color_gap))

        # Merge both ascending streams by start row; a gap before line i
        # ends where line i starts, so ties cannot happen between a run
        # and a gap. The sort is stable, keeping a gap's split portions
        # in their emitted (ignored, then regular) order. itemgetter(0)
        # extracts the same key the lambda did, at C speed.
        segments = gap_segs + [tuple(r) for r in runs]
        segments.sort(key=itemgetter(0))
        return segments

    def _paint_side_segments(self, c, side, x_start, x_end, y0, track_h):
        """Paint one side's colored segments into the track area.

        The WinMerge Location Pane draw loop: convert each segment to
        pixels once, then SKIP every segment whose end pixel equals the
        previous segment's end pixel — it would only repaint pixels that
        are already covered ("we cannot write to half a pixel"). A
        segment that collapses to zero height is bumped to 1 pixel so
        the first sub-pixel diff of a region stays visible. The number
        of CANVAS_RECT_FILL calls is therefore bounded by the track
        height in pixels, not by the line/diff count.
        """
        cum, total, gap_map = self._prefix_sums(side)
        if total <= 0 or track_h <= 0:
            return
        segments = self._build_segments(side, cum, gap_map)
        if not segments:
            return
        scale = track_h / total

        prev_end = -1     # raw end pixel of the previous segment (WinMerge's nPrevEndY)
        last_color = None
        for start_row, end_row, color in segments:
            ps = y0 + int(start_row * scale)
            pe = y0 + int(end_row * scale)
            if pe == prev_end:
                # Collapses onto pixels the previous segment already
                # covered — useless write, skip it.
                continue
            if pe <= ps:
                pe = ps + 1  # sub-pixel segment: draw at least one pixel
            if color != last_color:
                ct.canvas_proc(c, ct.CANVAS_SET_BRUSH, color=color,
                               style=ct.BRUSH_SOLID)
                last_color = color
            ct.canvas_proc(c, ct.CANVAS_RECT_FILL,
                           x=x_start, y=ps, x2=x_end, y2=pe)
            prev_end = y0 + int(end_row * scale)

    def _paint_buttons(self, c, w, h):
        """Paint the ▲/▼ scroll button boxes at the top and bottom of
        the panel.

        Background: the OVERVIEW background color (color_bg, set by
        set_colors from the theme's EdTextBg) so the boxes blend into
        the panel; the arrow glyph: the theme's ScrollArrow color,
        centered both vertically and horizontally. No borders on the
        boxes, like the scrollbar arrow buttons of usual editors and
        browsers.
        """
        if h <= 2 * BUTTON_HEIGHT:
            return  # degenerate tiny panel: buttons don't fit
        ct.canvas_proc(c, ct.CANVAS_SET_BRUSH, color=self.color_btn_bg,
                       style=ct.BRUSH_SOLID)
        for y0 in (0, h - BUTTON_HEIGHT):
            ct.canvas_proc(c, ct.CANVAS_RECT_FILL,
                           x=0, y=y0, x2=w, y2=y0 + BUTTON_HEIGHT)
        self._paint_arrow(c, 0, 0, w, BUTTON_HEIGHT, up=True)
        self._paint_arrow(c, 0, h - BUTTON_HEIGHT, w, BUTTON_HEIGHT, up=False)

    def _paint_arrow(self, c, bx, by, bw, bh, up):
        """Draw one ▲/▼ arrow as a solid triangle centered in its button
        box.

        A canvas POLYGON is used (NOT a font glyph): the font's ▲/▼ ink
        plus its line box are slightly larger than the measured text
        cell, so the rendered glyph bled past the button rectangle's
        edges (a dark smudge below the ▲ box / above the ▼ box). The
        polygon is a fixed 9x7 px triangle (ARROW_HALF_WIDTH/HEIGHT)
        centered in the box, so it always fits inside with a >=3px
        margin — and it is resolution-independent (no font, no DPI, no
        antialiased text-cell quirks).

        The triangle uses the theme's ScrollArrow color, like the arrows
        of the editor's native scrollbars.
        """
        cx = bx + bw // 2
        cy = by + bh // 2
        hw = ARROW_HALF_WIDTH
        hh = ARROW_HALF_HEIGHT
        if up:
            # apex on top, base on the bottom
            pts = (cx, cy - hh, cx + hw, cy + hh, cx - hw, cy + hh)
        else:
            # apex on the bottom, base on the top
            pts = (cx, cy + hh, cx + hw, cy - hh, cx - hw, cy - hh)
        ct.canvas_proc(c, ct.CANVAS_SET_PEN,
                       color=self.color_btn_arrow, size=1)
        ct.canvas_proc(c, ct.CANVAS_SET_BRUSH,
                       color=self.color_btn_arrow, style=ct.BRUSH_SOLID)
        ct.canvas_proc(c, ct.CANVAS_POLYGON, text=pts)

    def _paint_separator(self, c, w, h):
        """Paint the grey vertical separator line at the very left of
        the overview panel (x = 0..SEP_LINE_WIDTH), spanning the FULL
        panel height — the ▲/▼ button boxes included — so the overview
        is visually separated from the editor / the editor's scrollbar
        by the same line all the way down."""
        ct.canvas_proc(c, ct.CANVAS_SET_BRUSH, color=SEP_LINE_COLOR,
                       style=ct.BRUSH_SOLID)
        ct.canvas_proc(c, ct.CANVAS_RECT_FILL,
                       x=0, y=0, x2=SEP_LINE_WIDTH, y2=h)

    def repaint_static(self):
        """Force a full repaint of the static bitmap. Called after a
        fresh compare or when colors change.

        Frees the old static bitmap and creates a new one with fresh
        content, then calls paint() to display it with the dynamic part.
        """
        if self.h_canvas is None:
            return
        w, h = self._get_size()
        if w <= 0 or h <= 0:
            return
        # Refresh the size cache too: paint() (drag path) reuses it for
        # up to OVERVIEW_SIZE_TTL, and a stale entry right after a resize
        # would blit a mismatched static bitmap.
        self._size_cache = (w, h, time.monotonic())
        self._free_static_bitmap()
        self._ensure_static_bitmap(w, h)
        self.paint()

    def paint(self):
        """Repaint the overview using the static/dynamic bitmap approach.

        The static bitmap (diff segments + buttons + separator) is
        reused — it's only repainted when the diff changes (via
        repaint_static). On scroll, this method just:
        1. Ensures the static bitmap exists and matches current size.
        2. Resizes the image's embedded bitmap to match (if needed).
        3. Copies the static bitmap to the image's embedded bitmap via
           CANVAS_BITMAP (fast — one bitmap copy).
        4. Draws the dynamic part (slider + grabber) on top.

        This avoids the expensive segment loop on every scroll. The
        image control's embedded bitmap handles resize/minimize/restore
        automatically, so no on_act/on_resize/on_show handlers are
        needed.
        """
        if self.h_canvas is None:
            return

        w, h = self._get_size_cached()
        if w <= 0 or h <= 0:
            return

        # Ensure static bitmap exists and matches current size
        self._ensure_static_bitmap(w, h)

        # Resize the image's embedded bitmap to match the control size.
        # The image control starts with a 0x0 bitmap; we must resize it
        # before painting on it, otherwise nothing shows (checkerboard pattern).
        ct.bitmap_proc(self.h_bitmap, ct.BITMAP_SET_SIZE, w, h)

        # Copy static bitmap to the image's embedded bitmap.
        # CudaText API change (recent versions): CANVAS_BITMAP now takes
        # the source bitmap handle as the integer param "p1", NOT as the
        # string param "text" (the change was to avoid str-int
        # conversions). The old form `text=str(handle)` still works on
        # older builds but is deprecated and may be removed. Using p1
        # works on both old and new builds.
        ct.canvas_proc(self.h_canvas, ct.CANVAS_BITMAP,
                       p1=self._h_static_bmp, x=0, y=0)

        # Draw dynamic part (slider) on top
        self._paint_dynamic(w, h)

    def _paint_dynamic(self, w, h):
        """Draw the dynamic part: the scrollbar slider.

        Uses pure pixel-based mapping (no line/gap calculations):
        - Get the editor's total content height (smooth_max), current
          scroll position (smooth_pos), and visible page (smooth_page)
          from PROP_SCROLL_VERT_INFO.
        - Map to the track area between the ▲/▼ buttons:
            slider_height = track_h * smooth_page / smooth_max
                            (proportional, clamped to min 30px so it
                            stays grabbable, and to the track height)
            slider_top    = track_y0 + track_h * smooth_pos / smooth_max
        - Draw via _paint_slider_solid: one CANVAS_RECT call (pen border
          + solid brush fill) + the grabber lines — no transparency
          blending, so zero overhead per paint.

        The slider_top formula is mathematically consistent: when
        smooth_pos reaches its max (smooth_max - smooth_page),
        py_top = track_y0 + track_h - slider_height, so the slider lands
        flush at the bottom of the track. No special-casing needed for
        the bottom edge.

        While the overview is DRIVING the scroll (slider drag / track
        jump / ▲/▼ repeat) _thumb_preview_y is set: the thumb is drawn
        at the pixel the interaction is driving to (the raw mouse
        position / the tick target), NOT the round-tripped editor
        position. This is exactly how a native scrollbar behaves -- the
        thumb is glued to the mouse while the editor's text repaint
        catches up asynchronously. It also skips the get_prop bridge
        round-trip on the per-move paint path. The preview is cleared on
        interaction end (release / exit / self-heal), where the final
        paint reads the editor truth (which reflects any clamping).

        Args:
            w: width in pixels
            h: height in pixels
        """
        if self.a_ed is None:
            return

        c = self.h_canvas

        track_y0, track_h = self._track_rect(h)

        # --- Compute proportional slider height (like a real scrollbar) ---
        # smooth_max is the total content height in pixels (includes the
        # page size, per CudaText API docs). smooth_page is the visible
        # viewport height. slider_height proportional to page/total,
        # clamped to [min_height, track_h] so the slider always fits the
        # track and stays grabbable. Both values use round-half-up so
        # the slider lands EXACTLY flush at the track bottom when the
        # scroll position reaches its max (plain int() truncation would
        # leave a 1px gap: int(t*p/m) + int(t*p/m) != t).
        min_h = self._slider_min_height
        if self._thumb_preview_y is not None and self._smooth_max > 0:
            # Driving: page/max/height don't change mid-interaction --
            # reuse the geometry from the last truth read (no get_prop).
            smooth_max = self._smooth_max
            smooth_page = self._smooth_page
            if smooth_page > 0:
                py_height = int(track_h * smooth_page / smooth_max + 0.5)
                py_height = max(min_h, min(track_h, py_height))
            else:
                py_height = min(min_h, track_h)
            # Thumb glued to the driven position (already track-clamped
            # by the callers; clamp again for safety).
            py_top = self._thumb_preview_y
            py_top = max(track_y0, min(py_top,
                                       track_y0 + track_h - py_height))
        else:
            # Get pixel-based scroll info from editor a_ed.
            scroll_info = self.a_ed.get_prop(ct.PROP_SCROLL_VERT_INFO)
            if not scroll_info:
                return

            smooth_pos = scroll_info.get('smooth_pos', 0)
            smooth_max = scroll_info.get('smooth_max', 1)
            smooth_page = scroll_info.get('smooth_page', 0)

            if smooth_max <= 0:
                smooth_max = 1

            if smooth_page > 0:
                py_height = int(track_h * smooth_page / smooth_max + 0.5)
                py_height = max(min_h, min(track_h, py_height))
            else:
                # No page info (e.g., very early init) — fall back to min.
                py_height = min(min_h, track_h)

            # Map editor scroll position to track pixels:
            # slider_top = track_top + track_h * editor_scroll / editor_total
            py_top = track_y0 + int(track_h * smooth_pos / smooth_max + 0.5)

            # Clamp slider within the track
            py_top = max(track_y0, min(py_top,
                                       track_y0 + track_h - py_height))
            self._smooth_pos_last = scroll_info.get(
                'smooth_pos_last', max(0, smooth_max - smooth_page))
            self._smooth_page = smooth_page

        # Solid slider: one CANVAS_RECT call (pen border + brush fill).
        # No transparency blending — zero per-row overhead.
        self._paint_slider_solid(c, w, py_top, py_height)

        # Store slider geometry and scroll info for click/drag handling
        self._slider_top = py_top
        self._slider_height = py_height
        self._overview_height = h
        self._track_y0 = track_y0
        self._track_h = track_h
        self._smooth_max = smooth_max

    def _paint_slider_solid(self, c, w, py_top, py_height):
        """Solid-fill slider: opaque fill + border + grabber.

        The only slider-paint method: one CANVAS_RECT call draws both
        the fill (brush, _slider_fill) and the border (pen,
        _slider_border) in one shot, then the grabber lines
        (_slider_grabber_dark) — minimal work per paint, no transparency,
        no per-row blending loop, nothing to configure. The colors come
        from the theme's native scrollbar palette (ScrollFill /
        ScrollRect), contrast-corrected once in set_colors() so the
        slider is visible against the overview background and the grips
        against the fill on every theme.
        """
        ct.canvas_proc(c, ct.CANVAS_SET_PEN, color=self._slider_border, size=1)
        ct.canvas_proc(c, ct.CANVAS_SET_BRUSH, color=self._slider_fill, style=ct.BRUSH_SOLID)
        ct.canvas_proc(c, ct.CANVAS_RECT, x=SEP_LINE_WIDTH, y=py_top, x2=w - 1, y2=py_top + py_height)
        self._paint_slider_grabber(c, w, py_top, py_height)

    def _paint_slider_grabber(self, c, w, py_top, py_height):
        """Draw the 3 horizontal grabber lines centered in the slider.

        Pattern (matches modern scrollbar look — WinMerge, VS Code, etc.):
          - 3 horizontal lines, evenly spaced around the slider's vertical
            center (mid_y - spacing, mid_y, mid_y + spacing)
          - Each line is 2px thick (pen size = 2) so it stays clearly
            visible against the fill
          - Lines are 6px apart center-to-center (triple the original
            2px spacing), giving a clean modern look with proper
            visual padding from the slider's top/bottom border
          - Color = the slider BORDER base color (theme ScrollRect),
            contrast-corrected against the slider FILL in set_colors()
            (set_colors passes it through _color_contrast) — in themes
            where ScrollRect == ScrollFill the grips would otherwise
            be invisible; the correction guarantees they read clearly
            against the fill (white grips on a grey thumb on light
            themes, dark grips on a light thumb on dark themes);
            no white bevel, flat modern look
        """
        mid_y = py_top + py_height // 2
        grab_x1 = max(2 + SEP_LINE_WIDTH, w // 4)
        grab_x2 = min(w - 3, w * 3 // 4)
        spacing = self._slider_grabber_spacing  # 6px center-to-center
        thickness = self._slider_grabber_thickness  # 2px per line

        # 3 grabber lines in the border color (theme ScrollRect),
        # 2px thick, 6px apart (center-to-center). No white bevel.
        ct.canvas_proc(c, ct.CANVAS_SET_PEN,
                       color=self._slider_grabber_dark, size=thickness)
        ct.canvas_proc(c, ct.CANVAS_LINE,
                       x=grab_x1, y=mid_y - spacing, x2=grab_x2, y2=mid_y - spacing)
        ct.canvas_proc(c, ct.CANVAS_LINE,
                       x=grab_x1, y=mid_y,             x2=grab_x2, y2=mid_y)
        ct.canvas_proc(c, ct.CANVAS_LINE,
                       x=grab_x1, y=mid_y + spacing, x2=grab_x2, y2=mid_y + spacing)

    # ------------------------------------------------------------------
    # Mouse handling: slider drag, track jump, ▲/▼ buttons
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_mouse(data, info):
        """Extract (x, y) from a mouse-event callback's arguments.

        data is the dict {'btn', 'state', 'x', 'y'} the live-callback
        form delivers; info is the legacy 'x,y' string form. Returns
        (None, None) when neither carries usable coordinates.
        """
        if isinstance(data, dict):
            return data.get('x', 0), data.get('y', 0)
        if info:
            try:
                parts = info.split(',')
                return int(parts[0]), int(parts[1])
            except (ValueError, IndexError):
                return None, None
        return None, None

    def _on_mouse_down(self, id_dlg, id_ctl, data='', info=''):
        """Called on mouse down. Route the press by the panel layout:
        ▲/▼ button boxes scroll one line (with auto-repeat while held);
        inside the track, a press on the slider starts a drag, a press
        elsewhere jumps the slider there (centered on the click).

        Runs even while _pumping is True: a click dispatched by the
        message pump must not be swallowed, or rapid clicks during the
        ▲/▼ auto-repeat repaints get lost (dead clicks). All work here
        is synchronous; the nested _repaint_control_now() calls guard
        themselves."""
        x, y = self._parse_mouse(data, info)
        if x is None:
            return
        w, h = self._get_size()
        if h <= 0:
            return
        # A new interaction: the write-dedup and the paint-completion
        # gate start from a clean slate (an external scroll may have
        # moved the editors since the last interaction).
        self._last_written_pos = None
        self._editors_dirty = False

        # ▲/▼ button boxes (top / bottom).
        if y < BUTTON_HEIGHT and h > 2 * BUTTON_HEIGHT:
            self._start_button_repeat(-1)
            return
        if y >= h - BUTTON_HEIGHT and h > 2 * BUTTON_HEIGHT:
            self._start_button_repeat(1)
            return

        # Track: slider drag or centered jump.
        if self._slider_top <= y <= self._slider_top + self._slider_height:
            # Click inside slider — start drag
            self._dragging = True
            self._driving = True
            self._drag_offset = y - self._slider_top
        else:
            # Click outside slider — jump (centered on click). The
            # overview drives both halves from here until the button
            # is released (see is_driving_scroll).
            # THUMB FIRST, WRITE SECOND (the native-scrollbar order):
            # the thumb is painted at the clicked position and pumped
            # to the screen while the editors are still clean -- the
            # pump then only delivers the tiny overview repaint. Only
            # after that do we write the scroll position + invalidate
            # the editors; their (expensive on huge files) viewport
            # paints are then delivered asynchronously by the natural
            # message-loop drain, never synchronously inside this
            # handler.
            self._dragging = False
            self._driving = True
            target = self._pixel_to_smooth_pos(y, center=True)
            if target is not None:
                preview = self._preview_y_for_pos(target)
                if preview is not None:
                    self._thumb_preview_y = preview
                self.paint()
                if not self._editors_dirty:
                    self._repaint_control_now()
                self._write_scroll_position(target)

    @staticmethod
    def _left_button_held(data):
        """True / False when the event's data says whether the LEFT mouse
        button is held; None when the data carries no button state.

        CudaText encodes the held buttons in data['state']: 'L' = left,
        'R' = right, 'M' = middle (proc_miscutils.pas
        ConvertShiftStateToString). The legacy 'x,y' info-string form
        carries no button info — None (unknown) so callers cannot
        mistake it for "button up".
        """
        if isinstance(data, dict) and 'state' in data:
            return 'L' in data['state']
        return None

    def _on_mouse_move(self, id_dlg, id_ctl, data='', info=''):
        """Called on mouse move. If dragging, drive the scroll after the
        mouse like a native scrollbar thumb:

        1) THUMB GLUED TO THE MOUSE (every raw move, ungated): the
           slider bitmap is repainted at the mouse-derived position and
           the message queue is pumped -- but ONLY while the editors are
           clean (_editors_dirty is False). The pump (ProcessMessages)
           delivers EVERYTHING pending, including both editors' viewport
           paints; on million-line compares each paint walks the ~200k
           alignment gaps several times and takes 50-100 ms, so pumping
           right after invalidating the editors (what the old code did)
           froze the handler for 100-200 ms per move -- the slider
           "waiting" to follow the mouse. Pumping while clean costs
           ~1-2 ms (only the tiny overview repaint is pending).
        2) APPLY (write the scroll position to both editors + invalidate
           them) at a bounded rate: at most every OVERVIEW_TRACK_INTERVAL
           AND only when the editors have painted the previous apply
           (on_scroll fires at the end of every editor paint --
           note_scroll_painted). At most ONE apply is ever pending, so
           the text always shows the newest applied position and the
           editors' paints run in natural queue-drain windows between
           mouse handlers -- exactly the pipeline of a native scrollbar
           drag. A deferred one-shot timer applies the pending target
           when the mouse stops, so the thumb always lands exactly under
           the cursor.

        Also SELF-HEALS a lost release: a move arriving without the
        left button held (data['state'] lacks 'L') while a drag or a
        ▲/▼ auto-repeat is running means the button is physically up
        and the mouse-up was lost somewhere (any event path we did not
        foresee) — end the interaction right here instead of letting
        the slider keep following a mouse that presses nothing.
        """
        if self._pumping:
            return
        x, y = self._parse_mouse(data, info)
        if x is None:
            return
        if (self._dragging or self._btn_dir is not None) and \
                self._left_button_held(data) is False:
            # Left button is physically up but the release never
            # reached us -- treat this move as the release.
            self._stop_button_repeat()
            was_dragging = self._dragging
            self._dragging = False
            self._driving = False
            self._thumb_preview_y = None
            self._last_written_pos = None
            if was_dragging:
                self._apply_pending_drag_target()
                self.track_paint(force=True)
                self._repaint_control_now()
            return
        if not self._dragging:
            return
        # Drag: the slider top follows the mouse (accounting for the
        # press offset), clamped to the track (the visible thumb can
        # never leave the track -- the written position clamps at the
        # editor side too).
        target_y = y - self._drag_offset
        max_y = self._track_y0 + self._track_h - self._slider_height
        target_y = max(self._track_y0, min(target_y, max_y))
        self._drag_target_y = target_y

        # --- 1) thumb glued to the mouse ---
        self._thumb_preview_y = target_y
        self.paint()
        if not self._editors_dirty:
            self._repaint_control_now()

        # --- 2) apply at a bounded rate, no backlog ---
        now = time.monotonic()
        applied = False
        if now - self._last_apply >= OVERVIEW_TRACK_INTERVAL and \
                self._editors_ready(now):
            self._last_apply = now
            self._apply_pending_drag_target()
            applied = True
        if not applied and not self._defer_armed:
            # Arm the deferred catch-up timer (fires only when the mouse
            # STOPS -- WM_TIMER starves while moves keep flooding the
            # queue) so the newest position is still applied even if no
            # further move opens the gates.
            self._defer_armed = True
            ct.timer_proc(ct.TIMER_START_ONE, self._drag_deferred_tick,
                          OVERVIEW_DEFER_MS)

    def _apply_pending_drag_target(self):
        """Write the newest not-yet-applied drag position (if any) to the
        editors and clear it.

        Called from every path that ends or catches up a drag: the
        open-gate branch of _on_mouse_move, the deferred catch-up
        timer, and mouse up / exit / self-heal. Consuming the pending
        target on release is what makes the thumb land EXACTLY where the
        cursor was released even when the last move was coalesced away.
        """
        if self._drag_target_y is None:
            return
        target = self._drag_target_y
        self._drag_target_y = None
        pos = self._pixel_to_smooth_pos(target, center=False)
        if pos is not None:
            self._write_scroll_position(pos)

    def _drag_deferred_tick(self, tag=''):
        """One-shot timer callback (OVERVIEW_DEFER_MS): a drag move was
        not applied (closed throttle window or editors still painting
        the previous apply) and no further move has arrived -- the mouse
        STOPPED with the button still held. Apply the pending newest
        position now so the thumb and text catch up to the cursor (a
        native scrollbar does exactly this). WM_TIMER is only delivered
        when the message queue drains, so this never fires while the
        mouse is still moving -- the newest move always wins.

        If the editors have not painted the previous apply yet (huge
        files: the paint pair is still running), re-arm the one-shot
        once more instead of dropping the pending target: WM_PAINT beats
        WM_TIMER, so by the next tick the paints are done, the gate is
        open, and the newest position lands right after them. The
        OVERVIEW_APPLY_STALL timeout bounds the total wait.
        """
        self._defer_armed = False
        if not self._dragging or self._drag_target_y is None:
            return
        if not self._editors_ready(time.monotonic()):
            if not self._defer_armed:
                self._defer_armed = True
                ct.timer_proc(ct.TIMER_START_ONE, self._drag_deferred_tick,
                              OVERVIEW_DEFER_MS)
            return
        self._apply_pending_drag_target()
        # The thumb bitmap already shows the pending position (painted
        # per move); no repaint needed here. The write's invalidate
        # delivers the text in the next natural queue drain.

    def _on_mouse_up(self, id_dlg, id_ctl, data='', info=''):
        """Called on mouse up. Stops the ▲/▼ auto-repeat and dragging,
        applies the newest drag position (so the thumb and text land
        exactly under the release point even when the last move was
        coalesced away), clears the thumb preview and repaints the
        slider from the editor truth (which reflects any position
        clamping), then pumps the queue ONCE for an atomic final frame
        -- the interaction is over, so the pump's cost (it also delivers
        the editors' last pending paints) is no longer on the
        interaction's latency path.

        CRITICAL: the release is processed EVEN WHEN _pumping is True.
        _repaint_control_now() pumps the message queue, and the pump
        dispatches pending mouse messages — a WM_LBUTTONUP pending at
        that moment re-enters this handler with _pumping set. The old
        `if self._pumping: return` swallowed that release, and the
        drag then kept following the mouse / the ▲/▼ auto-repeat kept
        scrolling forever after the button was no longer held (the
        "sometimes the mouse is not released" bug — timing-dependent,
        because the up is only lost when it is queued exactly during
        a pump). _repaint_control_now() guards itself against nested
        pumps, and the pump that dispatched this very event delivers
        the pending WM_PAINT when it returns, so skipping the explicit
        control repaint while pumping loses nothing."""
        self._stop_button_repeat()
        was_dragging = self._dragging
        was_button = self._btn_dir is not None
        self._dragging = False
        self._driving = False
        self._thumb_preview_y = None
        self._last_written_pos = None
        if was_dragging:
            # Final landing: always apply, regardless of the gates.
            self._apply_pending_drag_target()
        if was_dragging or was_button:
            # Truth repaint (drops the preview; the editor's clamped
            # position wins over the un-clamped drag target).
            self.track_paint(force=True)
            if was_dragging:
                self._repaint_control_now()

    def note_scroll_painted(self):
        """Note that an editor viewport paint has completed.

        Called from __init__.py's on_scroll while the overview is
        driving: TATSynEdit fires OnScroll at the END of every viewport
        paint (Paint -> ScrollEventNeeded -> DoEventScroll), so this is
        a free, reliable 'the editors painted' completion signal. It
        re-opens the drag pipeline's paint-completion gate
        (_editors_ready): the next apply is only written once the
        editors have shown the previous one -- no backlog ever builds
        up, and the message pump in the per-move path stays cheap
        (pumping while the editors are dirty would deliver their
        pending viewport paints synchronously inside the mouse
        handler)."""
        self._editors_dirty = False

    def _editors_ready(self, now):
        """True when a new drag apply may be written: the editors have
        painted the previous apply (note_scroll_painted) or the
        OVERVIEW_APPLY_STALL safety timeout has passed (paint
        notifications delayed or lost)."""
        if not self._editors_dirty:
            return True
        return now - self._editors_dirty_at >= OVERVIEW_APPLY_STALL

    def is_driving_scroll(self):
        """True while the overview is driving BOTH editors' scroll
        positions itself (slider drag in progress, track jump pressed,
        ▲/▼ auto-repeat running).

        __init__.py's on_scroll checks this to SKIP the ScrollSplittedTab
        mirror while the overview writes both halves back-to-back itself:
        the mirror would see the half that is written one set_prop behind
        as "lagging", re-write it, and force a synchronous EDACTION_UPDATE
        FULL repaint of it. On huge compare files that full repaint costs
        100+ ms, so with the mirror active every overview-driven mouse
        move / repeat tick paid it -- the slider lagged 300-500 ms behind
        the mouse and the ▲/▼ buttons scrolled one line only every
        300-500 ms. With the mirror skipped, each editor repaints itself
        through its native optimized scroll path (like when its own
        scrollbar is used), which is what makes the overview feel
        instantaneous on any file size.
        """
        return self._driving

    def _on_mouse_exit(self, id_dlg, id_ctl, data='', info=''):
        """Called when the pointer leaves the overview panel. On
        platforms where the press is not captured (a button released
        outside the control never delivers on_mouse_up), this is the
        only signal that interaction ended — stop the ▲/▼ auto-repeat
        and any running drag here. The newest pending drag position is
        applied first (same as mouse up) so the thumb lands where the
        pointer left the panel, not one throttle window behind; the
        preview is cleared and the slider repainted from the editor
        truth. No message pump here: the pointer is already outside
        the panel and the natural queue drain delivers the final frames
        (input messages outrank generated WM_PAINTs anyway).

        Like _on_mouse_up, this runs even while _pumping is True: a
        stop event must never be swallowed by the pump."""
        self._stop_button_repeat()
        was_dragging = self._dragging
        was_button = self._btn_dir is not None
        self._dragging = False
        self._driving = False
        self._thumb_preview_y = None
        self._last_written_pos = None
        if was_dragging:
            self._apply_pending_drag_target()
        if was_dragging or was_button:
            self.track_paint(force=True)

    # ------------------------------------------------------------------
    # ▲/▼ button auto-repeat (like scrollbar arrow buttons)
    # ------------------------------------------------------------------

    def _start_button_repeat(self, direction):
        """Begin a ▲/▼ button press: scroll one line immediately, then
        arm the initial-delay one-shot timer which starts the repeating
        timer for the auto-scroll (like holding a usual scrollbar's
        arrow button: one line per click, continuous scrolling after a
        short hold). While the button is held the overview drives both
        halves (is_driving_scroll) — see _scroll_one_line for why the
        ScrollSplittedTab mirror must stay out of the way then."""
        self._stop_button_repeat()
        self._btn_dir = direction
        self._driving = True
        self._scroll_one_line(direction)
        ct.timer_proc(ct.TIMER_START_ONE, self._btn_delay_tick,
                      BUTTON_INITIAL_DELAY_MS)

    def _stop_button_repeat(self):
        """Stop the ▲/▼ auto-repeat (mouse released / left the panel /
        dialog destroyed). Disables both timers; the one-shot delay
        timer may already have fired and deleted itself — stopping a
        missing timer is a harmless no-op."""
        if self._btn_dir is None:
            return
        self._btn_dir = None
        ct.timer_proc(ct.TIMER_STOP, self._btn_delay_tick,
                      BUTTON_INITIAL_DELAY_MS)
        ct.timer_proc(ct.TIMER_STOP, self._btn_repeat_tick,
                      BUTTON_REPEAT_MS)

    def _btn_delay_tick(self, tag=''):
        """One-shot timer callback fired BUTTON_INITIAL_DELAY_MS after
        the button went down: the button is still held, so switch to
        the repeating auto-scroll timer."""
        if self._btn_dir is None:
            return
        ct.timer_proc(ct.TIMER_START, self._btn_repeat_tick,
                      BUTTON_REPEAT_MS)

    def _btn_repeat_tick(self, tag=''):
        """Repeating timer callback: the button is still held — scroll
        one more line (stops itself via _stop_button_repeat when the
        mouse is released or leaves the panel)."""
        if self._btn_dir is None:
            return
        self._scroll_one_line(self._btn_dir)

    def _scroll_one_line(self, direction):
        """Scroll both editors one line up (direction -1) or down
        (+1), like a scrollbar's arrow button. One line = the editor's
        char_size (row height in pixels) of smooth scrolling.

        Per tick (the 50 ms auto-repeat / the initial press):
        1) the thumb is previewed at the new position and pumped to the
           screen while the editors are clean (instant thumb, cheap
           pump), then
        2) the position is written to both halves + they are invalidated
           with ed.cmd(cmd_RepaintEditor) — the exact async invalidate
           (InvalidateEx) a native scrollbar's arrow path makes. Their
           viewport paints are delivered by the natural message-loop
           drain, never synchronously inside this tick.

        The WRITE runs on every tick (not gated by paint completion,
        exactly like a native arrow button's repeats): the editor data
        advances one line per 50 ms, and each delivered paint shows the
        accumulated position — the text glides in multi-line steps at
        the editors' paint rate instead of crawling one line per paint
        (the old bug: the tick's message pump ran the paint pair
        synchronously, so the next 50 ms tick only fired 100-200 ms
        later — "one line every 100-200 ms"). No-op writes (already at
        the top/bottom edge) are skipped: the write would not move the
        text and the paired invalidate would only force a full repaint
        of identical content.

        The old code also issued ed.action(EDACTION_UPDATE) on both
        halves per tick; that is a forced synchronous FULL repaint,
        which costs 100+ ms on million-line compare views.
        """
        if self.a_ed is None:
            return
        scroll_info = self.a_ed.get_prop(ct.PROP_SCROLL_VERT_INFO)
        if not scroll_info:
            return
        char_size = scroll_info.get('char_size', 0)
        if char_size <= 0:
            return
        smooth_max = scroll_info.get('smooth_max', 0)
        smooth_page = scroll_info.get('smooth_page', 0)
        pos_last = scroll_info.get(
            'smooth_pos_last', max(0, smooth_max - smooth_page))
        target = scroll_info.get('smooth_pos', 0) + direction * char_size
        # Clamp to the valid smooth range (no useless writes at the
        # edges: at the top/bottom the position cannot move).
        target = max(0, min(target, pos_last))
        if target == scroll_info.get('smooth_pos', 0):
            return  # editor data already there: nothing to write/paint
        # Keep the mapping caches fresh (used by _preview_y_for_pos).
        if smooth_max > 0:
            self._smooth_max = smooth_max
            self._smooth_pos_last = pos_last
            self._smooth_page = smooth_page
        # 1) thumb first
        preview = self._preview_y_for_pos(target)
        if preview is not None:
            self._thumb_preview_y = preview
            self.paint()
            if not self._editors_dirty:
                self._repaint_control_now()
        # 2) write + invalidate
        self._write_scroll_position(target)

    # ------------------------------------------------------------------
    # Repaint scheduling
    # ------------------------------------------------------------------

    def track_paint(self, force=False):
        """Repaint the overview immediately, throttled by WALL CLOCK to
        OVERVIEW_TRACK_INTERVAL (~33 fps).

        Used by __init__.py's on_scroll so the slider tracks live scroll
        events (the editor's own scrollbar, keyboard, mouse wheel) at
        up to ~33 repaints per second instead of waiting for the 150ms
        debounce timer. That timer is useless during a drag: mouse
        moves flood the message queue and WM_TIMER is only delivered
        when the queue drains, so a timer-debounced slider appears
        FROZEN until the drag stops.

        While the overview is DRIVING an interaction the slider is
        painted per raw mouse move by _on_mouse_move itself (the thumb
        preview path), so this method mostly matters for user-initiated
        scrolls and the final truth repaints on interaction end (those
        pass force=True: landing on the exact position matters more
        than the rate limit).

        paint() is cheap on this path (one cached-bitmap copy + the
        slider drawing -- the expensive static segments never run),
        so ~33 repaints per second cost little CPU; the wall-clock gate
        keeps the rate bounded no matter how fast the mouse moves or
        how densely scroll events arrive.

        Args:
            force: bypass the throttle (final repaints on mouse-up and
                after a click jump, where landing on the exact position
                matters more than the rate limit).
        """
        now = time.monotonic()
        if not force and now - self._track_last_paint < OVERVIEW_TRACK_INTERVAL:
            return
        self._track_last_paint = now
        self.paint()

    def _repaint_control_now(self):
        """Force the on-screen image control to repaint IMMEDIATELY.

        paint() draws into the image's embedded bitmap, but the control
        itself only repaints when a WM_PAINT is delivered — and WM_PAINT
        is generated only while the message queue is EMPTY. During a
        slider drag the queue is continuously fed with mouse moves, so
        the thumb would stay frozen on screen (while the bitmap in
        memory is already up to date) until the drag stops.

        Pumping the queue once here (app_proc(PROC_IDLE) —
        Application.ProcessMessages + one idle pass) delivers the
        pending WM_PAINT right away, so the thumb moves with the mouse
        like in usual scrollbars.

        Re-entrancy: the pump also dispatches pending INPUT messages,
        which re-enter the mouse handlers. The _pumping flag makes the
        nested handlers behave correctly: mouse MOVES return at once
        (the newest position is picked up by the next event after the
        pump returns, and dropping them breaks the move→pump→move
        recursion), while mouse UP / EXIT / DOWN are still fully
        processed — a release dispatched by the pump must never be
        swallowed, or the drag / ▲/▼ auto-repeat would keep running
        with the button no longer held (the "mouse is not released"
        bug). _repaint_control_now itself can never nest.
        """
        if self._pumping or self.h_dlg is None:
            return
        self._pumping = True
        try:
            ct.app_proc(ct.PROC_IDLE, 'false')
        except Exception:
            pass
        finally:
            self._pumping = False

    # ------------------------------------------------------------------
    # Scroll mapping
    # ------------------------------------------------------------------

    def _pixel_to_smooth_pos(self, overview_y, center=True):
        """Map an overview pixel Y to an editor smooth scroll position.

        Pure pixel-based mapping onto the TRACK area (between the ▲/▼
        buttons):
            editor_target_scroll = smooth_max * (y - track_y0) / track_h

        If center=True, the clicked position becomes the center of the
        viewport (the target is pulled up by half the slider height,
        which corresponds to half the visible page).
        If center=False (dragging), the position becomes the slider top.

        The result is clamped to the editor's valid smooth range
        [0, smooth_pos_last] — so a drag past the track ends writes the
        end-of-file position instead of a no-op-beyond-end value (the
        editor would clamp it internally anyway, but clamping here
        keeps the thumb preview and the written position consistent).

        Returns None when the geometry/mapping is not usable yet.
        """
        h = self._overview_height
        if h <= 0:
            w, h = self._get_size_cached()
        if h <= 0:
            return None
        track_y0, track_h = self._track_rect(h)
        if track_h <= 0:
            return None

        ty = overview_y - track_y0
        if center:
            ty -= self._slider_height // 2

        smooth_max = self._smooth_max
        if smooth_max <= 0:
            # Get it from the editor
            scroll_info = self.a_ed.get_prop(ct.PROP_SCROLL_VERT_INFO) if self.a_ed else None
            if scroll_info:
                smooth_max = scroll_info.get('smooth_max', 0)
            if smooth_max <= 0:
                return None
            self._smooth_max = smooth_max

        # Map overview track pixel to editor scroll pixel:
        # editor_scroll = smooth_max * overview_y / track_h
        target_smooth_pos = int(smooth_max * ty / track_h)

        # Clamp to the valid smooth range
        pos_last = self._smooth_pos_last
        if pos_last <= 0:
            scroll_info = self.a_ed.get_prop(ct.PROP_SCROLL_VERT_INFO) if self.a_ed else None
            pos_last = scroll_info.get(
                'smooth_pos_last', 0) if scroll_info else 0
            if pos_last <= 0 and scroll_info:
                pos_last = max(0, smooth_max -
                               scroll_info.get('smooth_page', 0))
            self._smooth_pos_last = pos_last
        return max(0, min(target_smooth_pos, pos_last))

    def _preview_y_for_pos(self, smooth_pos):
        """Map a smooth scroll position to the track-pixel Y the slider
        thumb should be shown at (the inverse of the paint mapping:
        py_top = track_y0 + track_h * smooth_pos / smooth_max), clamped
        to the track. Returns None when the mapping is not usable yet
        (no truth read done, smooth_max unknown)."""
        smooth_max = self._smooth_max
        track_h = self._track_h
        track_y0 = self._track_y0
        if smooth_max <= 0 or track_h <= 0:
            return None
        py_top = track_y0 + int(track_h * smooth_pos / smooth_max + 0.5)
        py_height = self._slider_height
        return max(track_y0, min(py_top, track_y0 + track_h - py_height))

    def _write_scroll_position(self, target_smooth_pos):
        """Write the smooth scroll position to both editors and
        invalidate them for an asynchronous repaint.

        The write pair (set_prop + ed.cmd(cmd_RepaintEditor)) is exactly
        what a native scrollbar performs per scroll step: the position
        write (EditorStringToScrollInfo updates the scroll records and
        scrollbar data) plus the invalidate (InvalidateEx). The set_prop
        alone does NOT repaint the editor, and with the built-in
        scrollbars hidden (the overview replaces them) nothing else
        would ever repaint them — that was the "text scrolls slowly"
        half of the old bug. No EDACTION_UPDATE is ever issued on these
        paths: that is a forced synchronous FULL repaint (Invalidate +
        Update + Repaint on Windows = 100+ ms per half on million-line
        compare views).

        Identical consecutive writes are skipped (the editor data
        already shows the position; the paired invalidate would only
        force a full repaint of identical content). The invalidation
        also sets _editors_dirty: the drag pipeline then waits for the
        paint completion signal (note_scroll_painted) before writing
        the next position, and never pumps the message queue while the
        editors' paints are pending (the pump would deliver them
        synchronously inside the mouse handler).
        """
        if target_smooth_pos == self._last_written_pos:
            return
        self._last_written_pos = target_smooth_pos
        for e in (self.a_ed, self.b_ed):
            if e is not None:
                e.set_prop(ct.PROP_SCROLL_VERT_INFO,
                           {'smooth_pos': target_smooth_pos})
                e.cmd(ct_cmd.cmd_RepaintEditor)
        self._editors_dirty = True
        self._editors_dirty_at = time.monotonic()
