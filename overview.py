"""Paintbox-based overview panel for the Differ plugin.

Replaces the micromap with a custom paintbox docked to the right side of
the editor's parent-of-parent form. Unlike the micromap, the paintbox
overview is gap-aware: it accounts for the inter-line gaps inserted by
Differ for visual alignment, so the overview stays in sync with what the
user actually sees.

Architecture:
  - PaintboxOverview creates a non-modal dialog with a paintbox control.
  - The dialog is docked to the RIGHT side of PROP_HANDLE_PARENT2.
  - Uses a two-bitmap optimization (see _ensure_static_bitmap / paint):
    * Static bitmap: persistent, stores the colored line/gap rectangles.
      Only repainted on compare/resize via repaint_static(). This is the
      expensive part (hundreds of CANVAS_RECT_FILL calls).
    * Dynamic bitmap: created on each paint(), copies the static bitmap
      via CANVAS_BITMAP, then draws the cursor marker on top. This is
      cheap (one bitmap copy + 2 CANVAS_LINE calls).
  - On scroll: paint() is called (debounced 150ms). It only creates the
    dynamic bitmap — the static bitmap is reused. This avoids repainting
    hundreds of rectangles on every scroll, dramatically reducing CPU.
  - On compare/resize: repaint_static() is called first, which frees the
    old static bitmap and creates a new one with fresh content. Then
    paint() draws the cursor on top.

  See: https://github.com/CudaText-addons/cuda_differ/issues/29
"""

import cudatext as ct
from .profiling import Profiler

# Overview dialog width in pixels (docked to the right)
OVERVIEW_WIDTH = 80


class PaintboxOverview:
    """Manages a docked paintbox overview panel for a compare tab.

    The overview shows a gap-aware mini-map of both editors side by side.
    Created once per compare tab, destroyed when the tab closes.
    """

    def __init__(self):
        """Initialize the overview with empty state and default colors."""
        self.h_dlg = None       # dialog handle
        self.h_canvas = None    # paintbox canvas handle
        self._ctl_index = None  # control index in the parent form
        self._owns_dlg = False  # True if we created a separate dialog
        # Static bitmap (persistent — only repainted on compare/resize).
        self._h_static_bmp = None
        self._h_static_cnv = None
        self._static_w = 0
        self._static_h = 0
        # Line states: {('a', line): color, ('b', line): color}
        self.line_states = {}
        # Gap info: list of (after_line, gap_visual_rows) for each gap
        self.gaps_a = []  # list of (after_line, gap_visual_rows)
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
        self.color_cursor = 0x000000   # overridden by set_colors() with theme font color

    def is_created(self):
        """Return True if the overview dialog has been created."""
        return self.h_dlg is not None

    def create(self, a_ed, b_ed):
        """Create the overview as a separate dialog docked to the right
        side of the editor's parent-of-parent form.

        Uses PROP_HANDLE_PARENT2 (the parent of the editor's parent) for
        docking. PROP_HANDLE_PARENT (the editor's direct parent) is the
        split container — docking to it places the overview inside the
        split area, causing it to jump to the middle when side panels
        (like the Tabs sidebar) are toggled. PROP_HANDLE_PARENT2 is the
        outer form that holds the split container + side panels + tab
        bar. Docking to it keeps the overview stable regardless of side
        panel state.

        Args:
            a_ed: left editor (primary)
            b_ed: right editor (secondary)
        """
        self.a_ed = a_ed
        self.b_ed = b_ed
        # PROP_HANDLE_PARENT2: parent-of-parent of the editor.
        h_parent = a_ed.get_prop(ct.PROP_HANDLE_PARENT2)
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
            'on_resize': self._on_resize,
            'on_show': self._on_resize,
        })

        # Add paintbox control, filling the entire dialog
        self._ctl_index = ct.dlg_proc(self.h_dlg, ct.DLG_CTL_ADD, 'paintbox')
        ct.dlg_proc(self.h_dlg, ct.DLG_CTL_PROP_SET, index=self._ctl_index, prop={
            'name': 'paint',
            'align': ct.ALIGN_CLIENT,
            'on_click': self._on_click,
            'on_mouse_down': self._on_mouse_down,
        })
        self.h_canvas = ct.dlg_proc(self.h_dlg, ct.DLG_CTL_HANDLE, index=self._ctl_index)

        # Dock to the RIGHT side of the editor's parent-of-parent form
        ct.dlg_proc(self.h_dlg, ct.DLG_SHOW_NONMODAL)
        ct.dlg_proc(self.h_dlg, ct.DLG_DOCK, prop='R', index=h_parent)

    def destroy(self):
        """Undock and free the overview dialog and static bitmap."""
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

        The static bitmap stores the colored line/gap rectangles (the
        expensive part that takes hundreds of CANVAS_RECT_FILL calls).
        It's only repainted here when the size changes or when
        repaint_static() is called after a fresh compare.

        Once created, the static bitmap is reused on every paint() call
        — paint() just copies it to a temp bitmap and draws the cursor
        marker on top, which is very fast.
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

    def set_colors(self, color_bg, color_deleted, color_added, color_changed, color_gap, color_cursor=None):
        """Set the colors used for painting the overview.

        Args:
            color_bg: background color (theme EdTextBg)
            color_deleted: color for deleted lines (config color_deleted)
            color_added: color for added lines (config color_added)
            color_changed: color for changed lines (config color_changed)
            color_gap: color for gap rectangles (config color_gaps)
            color_cursor: color for cursor marker (theme EdTextFont).
                          If None, keeps the previous value.
        """
        self.color_bg = color_bg
        self.color_deleted = color_deleted
        self.color_added = color_added
        self.color_changed = color_changed
        self.color_gap = color_gap
        if color_cursor is not None:
            self.color_cursor = color_cursor

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

    def _line_visual_rows(self, side, line):
        """Return the number of visual rows a line occupies (1 if no
        wrapping, or wrap_counts[i] if wrapping is on)."""
        if side == 'a':
            wc = self.wrap_counts_a
        else:
            wc = self.wrap_counts_b
        if wc is None or line < 0 or line >= len(wc):
            return 1
        return max(1, wc[line])

    def add_line_state(self, side, line, color):
        """Record that a line has a specific color (deleted/added/changed).

        Args:
            side: 'a' or 'b'
            line: line index (0-based)
            color: RGB int color
        """
        self.line_states[(side, line)] = color

    def add_gap(self, side, after_line, gap_visual_rows):
        """Record a gap inserted after a line.

        Args:
            side: 'a' or 'b'
            after_line: the line index after which the gap was inserted
            gap_visual_rows: number of visual rows the gap occupies
        """
        if side == 'a':
            self.gaps_a.append((after_line, gap_visual_rows))
        else:
            self.gaps_b.append((after_line, gap_visual_rows))

    def clear_data(self):
        """Clear all collected line states and gaps. Called before a
        fresh compare."""
        self.line_states.clear()
        self.gaps_a.clear()
        self.gaps_b.clear()

    def _compute_visual_height(self, side):
        """Compute the total visual height (lines + gaps) for one side.

        Each line's visual height is its wrap count (1 if no wrapping),
        plus gap rows. This ensures both sides have equal visual heights
        when the diff is correct.

        Returns: int — total visual rows for the given side.
        """
        if side == 'a':
            line_count = self.a_line_count
            gaps = self.gaps_a
        else:
            line_count = self.b_line_count
            gaps = self.gaps_b
        # Sum line visual rows (wrap-aware)
        total = 0
        for line in range(line_count):
            total += self._line_visual_rows(side, line)
        # Add gap rows
        for _, gap_rows in gaps:
            total += gap_rows
        return max(total, 1)

    def _sorted_gaps(self, side):
        """Return gaps for the given side, sorted by after_line.

        Gaps are collected in event order (which may not be sorted),
        so we sort them before using in position calculations.
        """
        gaps = self.gaps_a if side == 'a' else self.gaps_b
        return sorted(gaps, key=lambda g: g[0])

    def _line_to_visual_y(self, side, line):
        """Map a line index to its visual Y position (in visual rows),
        accounting for both gaps and wrapping.

        Args:
            side: 'a' or 'b'
            line: line index (0-based)

        Returns: float — visual Y position in visual-row units.
        """
        gaps = self._sorted_gaps(side)
        # Sum visual rows of all lines before this one (wrap-aware)
        y = 0
        for i in range(line):
            y += self._line_visual_rows(side, i)
        # Add gap rows for gaps at or before this line
        for after_line, gap_rows in gaps:
            if after_line <= line:
                y += gap_rows
            else:
                break
        return y

    def _visual_y_to_line(self, side, visual_y):
        """Map a visual Y position (in visual rows) back to a line index,
        accounting for gaps.

        Args:
            side: 'a' or 'b'
            visual_y: visual Y position in visual-row units

        Returns: int — line index (0-based).
        """
        gaps = self._sorted_gaps(side)
        remaining = visual_y
        for after_line, gap_rows in gaps:
            if after_line < remaining:
                # The gap is above the current position; subtract it
                remaining -= gap_rows
            else:
                break
        return max(0, int(remaining))

    def _get_scale(self, h):
        """Compute the pixel-per-visual-row scale factor.

        Both sides use the same scale (based on the taller side) so that
        visually-aligned lines in the editors are also aligned in the
        overview.

        Args:
            h: pixel height of the paintbox

        Returns: float — pixels per visual row.
        """
        vis_h_a = self._compute_visual_height('a')
        vis_h_b = self._compute_visual_height('b')
        max_vis_h = max(vis_h_a, vis_h_b)
        return h / max_vis_h if max_vis_h > 0 else 1

    def _paint_static(self, c, w, h):
        """Paint the static part (background, line states, gaps) on the
        given canvas. This is the expensive part that only needs to run
        on compare/resize, not on scroll.

        The static bitmap stores the result of this method so it can be
        reused on every paint() call without re-executing the expensive
        CANVAS_RECT_FILL loop.

        Args:
            c: canvas handle (bitmap canvas or paintbox canvas)
            w: width in pixels
            h: height in pixels
        """
        # Clear background
        ct.canvas_proc(c, ct.CANVAS_SET_BRUSH, color=self.color_bg, style=ct.BRUSH_SOLID)
        ct.canvas_proc(c, ct.CANVAS_RECT_FILL, x=0, y=0, x2=w, y2=h)

        # Paint both sides
        scale = self._get_scale(h)
        half_w = w // 2
        self._paint_side(c, 'a', 0, half_w, h, scale)
        self._paint_side(c, 'b', half_w, w, h, scale)

    def _paint_side(self, c, side, x_start, x_end, h, scale):
        """Paint one side of the overview (either a_ed or b_ed half).

        Walks the lines in order, painting each line as a 1-pixel-tall
        colored rectangle. Gaps are painted as gray rectangles. The visual
        position accounts for gaps so the overview stays in sync with
        the actual editor layout.

        Args:
            c: canvas handle
            side: 'a' or 'b'
            x_start: left X pixel of this side's area
            x_end: right X pixel of this side's area
            h: total pixel height
            scale: pixels per visual row
        """
        if side == 'a':
            line_count = self.a_line_count
            gaps = self._sorted_gaps('a')
        else:
            line_count = self.b_line_count
            gaps = self._sorted_gaps('b')

        # Build a gap map: after_line -> total gap rows at that position
        gap_map = {}
        for after_line, gap_rows in gaps:
            gap_map[after_line] = gap_map.get(after_line, 0) + gap_rows

        vis_y = 0

        # Paint gaps and lines in order
        for line in range(line_count):
            # Paint gap before this line (if any)
            if line in gap_map:
                gap_rows = gap_map[line]
                gap_h = max(1, int(gap_rows * scale))
                py = int(vis_y * scale)
                ct.canvas_proc(c, ct.CANVAS_SET_BRUSH, color=self.color_gap, style=ct.BRUSH_SOLID)
                ct.canvas_proc(c, ct.CANVAS_RECT_FILL, x=x_start, y=py, x2=x_end, y2=py + gap_h)
                vis_y += gap_rows

            # Paint the line (only if it has a state — changed lines)
            color = self.line_states.get((side, line))
            if color is not None:
                py = int(vis_y * scale)
                # Use wrap-aware line height
                line_vr = self._line_visual_rows(side, line)
                line_h = max(1, int(line_vr * scale) + 1)
                ct.canvas_proc(c, ct.CANVAS_SET_BRUSH, color=color, style=ct.BRUSH_SOLID)
                ct.canvas_proc(c, ct.CANVAS_RECT_FILL, x=x_start, y=py, x2=x_end, y2=py + line_h)
            vis_y += self._line_visual_rows(side, line)

        # Paint any remaining gaps after the last line
        for after_line, gap_rows in gaps:
            if after_line >= line_count:
                gap_h = max(1, int(gap_rows * scale))
                py = int(vis_y * scale)
                ct.canvas_proc(c, ct.CANVAS_SET_BRUSH, color=self.color_gap, style=ct.BRUSH_SOLID)
                ct.canvas_proc(c, ct.CANVAS_RECT_FILL, x=x_start, y=py, x2=x_end, y2=py + gap_h)
                vis_y += gap_rows

    def repaint_static(self):
        """Force a full repaint of the static bitmap. Called after a
        fresh compare or when colors change.

        Frees the old static bitmap and creates a new one with fresh
        content, then calls paint() to display it with the cursor marker.
        """
        if self.h_canvas is None:
            return
        props = ct.dlg_proc(self.h_dlg, ct.DLG_CTL_PROP_GET, index=self._ctl_index)
        w = props.get('w', OVERVIEW_WIDTH)
        h = props.get('h', 600)
        if w <= 0 or h <= 0:
            return

        # Debug: print gap/line data to trace ordering issues
        vis_h_a = self._compute_visual_height('a')
        vis_h_b = self._compute_visual_height('b')
        print('Differ overview debug:')
        print('  a: {} lines, {} gaps, vis_h={}'.format(
            self.a_line_count, len(self.gaps_a), vis_h_a))
        print('  b: {} lines, {} gaps, vis_h={}'.format(
            self.b_line_count, len(self.gaps_b), vis_h_b))
        print('  a gaps: {}'.format(self._sorted_gaps('a')[:20]))
        print('  b gaps: {}'.format(self._sorted_gaps('b')[:20]))
        print('  a line_states: {}'.format(
            sorted([(k[1], hex(v)) for k, v in self.line_states.items() if k[0] == 'a'])))
        print('  b line_states: {}'.format(
            sorted([(k[1], hex(v)) for k, v in self.line_states.items() if k[0] == 'b'])))
        if self.wrap_counts_a:
            print('  a wrap_counts: {}'.format(self.wrap_counts_a[:20]))
        if self.wrap_counts_b:
            print('  b wrap_counts: {}'.format(self.wrap_counts_b[:20]))
        # Trace the exact paint order for side 'a' (last 5 lines + gaps)
        print('  a paint trace (last 6 items):')
        gaps_a = self._sorted_gaps('a')
        gap_map_a = {}
        for al, gr in gaps_a:
            gap_map_a[al] = gap_map_a.get(al, 0) + gr
        vy = 0
        for line in range(self.a_line_count):
            vr = self._line_visual_rows('a', line)
            gap = gap_map_a.get(line, 0)
            state = self.line_states.get(('a', line))
            if line >= self.a_line_count - 6:
                print('    line {}: vis_y={}, wrap={}, gap={}, state={}'.format(
                    line, vy, vr, gap, hex(state) if state else 'none'))
            vy += vr + gap
        # Trace the exact paint order for side 'b'
        print('  b paint trace (all):')
        gap_map_b = {}
        for al, gr in self._sorted_gaps('b'):
            gap_map_b[al] = gap_map_b.get(al, 0) + gr
        vy = 0
        for line in range(self.b_line_count):
            vr = self._line_visual_rows('b', line)
            gap = gap_map_b.get(line, 0)
            state = self.line_states.get(('b', line))
            print('    line {}: vis_y={}, wrap={}, gap={}, state={}'.format(
                line, vy, vr, gap, hex(state) if state else 'none'))
            vy += vr + gap

        self._free_static_bitmap()
        self._ensure_static_bitmap(w, h)
        self.paint()

    def paint(self):
        """Repaint the overview using the static/dynamic bitmap approach.

        The static bitmap (line states + gaps) is reused — it's only
        repainted when the diff changes (via repaint_static). On scroll,
        this method just:
        1. Creates a temp bitmap
        2. Copies the static bitmap to it via CANVAS_BITMAP (fast)
        3. Draws the cursor marker on top (2 CANVAS_LINE calls)
        4. Copies the temp bitmap to the paintbox via CANVAS_BITMAP
        5. Frees the temp bitmap

        This avoids the expensive CANVAS_RECT_FILL loop on every scroll,
        dramatically reducing CPU usage and eliminating flicker.
        """
        if self.h_canvas is None:
            return

        # Get the paintbox size from the control properties
        props = ct.dlg_proc(self.h_dlg, ct.DLG_CTL_PROP_GET, index=self._ctl_index)
        w = props.get('w', OVERVIEW_WIDTH)
        h = props.get('h', 600)
        if w <= 0 or h <= 0:
            return

        # Ensure static bitmap exists and matches current size
        self._ensure_static_bitmap(w, h)

        # Create a temp bitmap: copy static bitmap, draw cursor on top
        h_tmp = ct.bitmap_proc(0, ct.BITMAP_CREATE, w, h)
        try:
            h_tmp_cnv = ct.bitmap_proc(h_tmp, ct.BITMAP_GET_CANVAS)
            # Copy static bitmap to temp bitmap
            ct.canvas_proc(h_tmp_cnv, ct.CANVAS_BITMAP,
                           text=str(self._h_static_bmp), x=0, y=0)
            # Draw cursor markers on temp bitmap (dynamic part)
            self._paint_cursor(h_tmp_cnv, w, h)
            # Copy temp bitmap to paintbox in one operation
            ct.canvas_proc(self.h_canvas, ct.CANVAS_BITMAP,
                           text=str(h_tmp), x=0, y=0)
        finally:
            ct.bitmap_proc(h_tmp, ct.BITMAP_FREE)

    def _paint_cursor(self, c, w, h):
        """Draw cursor position markers (dynamic part).

        Draws a thin horizontal line at the cursor's visual Y position
        for each editor. This is the only part that changes on scroll,
        so it's drawn on top of the static bitmap copy.

        Args:
            c: canvas handle (temp bitmap canvas)
            w: width in pixels
            h: height in pixels
        """
        scale = self._get_scale(h)
        half_w = w // 2

        # Cursor for left editor (a_ed) — left half
        if self.a_ed is not None:
            try:
                caret_a = self.a_ed.get_carets()
                if caret_a:
                    y_a = caret_a[0][1]
                    vis_y = self._line_to_visual_y('a', y_a)
                    py = int(vis_y * scale)
                    ct.canvas_proc(c, ct.CANVAS_SET_PEN, color=self.color_cursor, size=1)
                    ct.canvas_proc(c, ct.CANVAS_LINE, x=0, y=py, x2=half_w, y2=py)
            except Exception:
                pass

        # Cursor for right editor (b_ed) — right half
        if self.b_ed is not None:
            try:
                caret_b = self.b_ed.get_carets()
                if caret_b:
                    y_b = caret_b[0][1]
                    vis_y = self._line_to_visual_y('b', y_b)
                    py = int(vis_y * scale)
                    ct.canvas_proc(c, ct.CANVAS_SET_PEN, color=self.color_cursor, size=1)
                    ct.canvas_proc(c, ct.CANVAS_LINE, x=half_w, y=py, x2=w, y2=py)
            except Exception:
                pass

    def _on_resize(self, id_dlg, id_ctl, data='', info=''):
        """Called when the dialog is resized or shown. Triggers a full
        repaint of the static bitmap (size may have changed)."""
        self.repaint_static()

    def _on_click(self, id_dlg, id_ctl, data='', info=''):
        """Called when the paintbox is clicked. Scrolls the corresponding
        editor to the clicked position.

        info is "x,y" — the click position in paintbox coordinates.
        """
        if not info:
            return
        try:
            parts = info.split(',')
            x = int(parts[0])
            y = int(parts[1])
        except (ValueError, IndexError):
            return

        # Determine which side was clicked
        props = ct.dlg_proc(self.h_dlg, ct.DLG_CTL_PROP_GET, index=self._ctl_index)
        w = props.get('w', OVERVIEW_WIDTH)
        half_w = w // 2
        side = 'a' if x < half_w else 'b'

        # Get the paintbox height
        h = props.get('h', 600)

        # Map Y pixel to visual row
        vis_h_a = self._compute_visual_height('a')
        vis_h_b = self._compute_visual_height('b')
        max_vis_h = max(vis_h_a, vis_h_b)
        if max_vis_h <= 0:
            return
        scale = h / max_vis_h
        visual_y = y / scale if scale > 0 else 0

        # Map visual row to line index
        line = self._visual_y_to_line(side, visual_y)

        # Scroll the corresponding editor
        ed = self.a_ed if side == 'a' else self.b_ed
        if ed is not None:
            try:
                ed.action(ct.EDACTION_SHOW_POS, (0, line), (0, 0))
            except Exception:
                pass

    def _on_mouse_down(self, id_dlg, id_ctl, data='', info=''):
        """Called on mouse down in the paintbox. data is a dict with
        btn, state, x, y. We treat it like a click for scrolling."""
        if isinstance(data, dict):
            x = data.get('x', 0)
            y = data.get('y', 0)
            self._on_click(id_dlg, id_ctl, info='{},{}'.format(x, y))
