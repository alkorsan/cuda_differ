"""Hunk edge columns for the Differ plugin.

Implements the idea from the CudaText issue #6477 discussion
(https://github.com/Alexey-T/CudaText/issues/6477): two narrow custom-drawn
columns, one at the LEFT edge of editor 1 and one at the LEFT edge of
editor 2, each showing a bracket around every difference block (hunk):

    +----
    |
    |
    +----

A bracket spans the hunk's FULL visual footprint -- the text lines AND the
compensating gap band(s) the engine inserts inside the hunk (so on the
shorter side of a replace, or on the empty side of an insert/delete, the
bracket includes the gap). A user who wants to move a block with
Alt+Left/Alt+Right can therefore see exactly which rows will be moved, like
the rule lines Beyond Compare draws around its difference blocks.

ATTACHING THE COLUMNS (the hard part -- this module's fourth revision).

The control dump of a split tab (dlg_proc on the PROP_HANDLE_PARENT
handle) shows the tree the columns must live in:

    panel_ed   (p: '')            -- grouping panel filling the form
      splitter1 (p: 'panel_ed')   -- Align=alRight
      ed1       (p: 'panel_ed')   -- left editor,  Align=alClient
      ed2       (p: 'panel_ed')   -- right editor, Align=alRight

(From CudaText's form_frame.pas, TEditorFrame: EdFirst.Align:=alClient,
EdSecond.Align:=cSplitHorzToAlign[vertical]=alRight, the splitter the
same. The alRight pass docks ed2 at the panel's right edge and the
splitter at ed2's LEFT edge; ed1 fills what remains.)

Revision 1 docked the columns as borderless dialogs to the parent form:
docking only has form-edge slots, so column B landed at the far right of
everything, after the editor's scrollbar and the overview panel. Revision
2 added the columns as ALIGN_NONE child controls of panel_ed and shifted
the editors right by setting their x/w control props -- but the editors
are ALIGN-MANAGED, and the very next LCL realign recomputed ed1 (alClient)
back over the columns, so NOTHING was visible at all. Revision 3 worked
WITH the align system (column A alLeft, column B alRight seeded between
the splitter and ed2) and finally looked right -- but it BROKE THE
SPLITTER DRAG, and that is what revision 4 fixes:

LCL's TCustomSplitter picks the control it resizes GEOMETRICALLY
(extctrls/customsplitter.inc, FindAlignControl): among the siblings whose
Align is the splitter's own (alRight) or alClient, the one with the
smallest Left still >= the splitter's right edge -- normally ed2. A column
docked between the splitter and ed2 STEALS that spot: dragging then
grows/shrinks the COLUMN, not the editor (user-visible: the drag flashes
and snaps back, or only the column moves). So no aligned control of ours
can ever sit in panel_ed between the splitter and ed2.

Revision 4 (this one):

  * column A keeps Align=alLeft in panel_ed: it takes the panel's
    left-edge strip, and ed1 (alClient) AUTOMATICALLY shrinks to the
    remaining space. An alLeft sibling is never a drag candidate for an
    alRight splitter, so the splitter does not care about it.

  * column B is NOT given a slot of its own in panel_ed at all -- it
    moves INSIDE the split bar. The splitter control is widened by the
    column width (w 5 -> 5+W); being alRight it keeps its right edge
    glued to ed2's left edge and grows LEFTWARD, so only ed1 gives way.
    Column B is an 'image' child of the splitter ('p': 'splitter1'),
    Align=alRight INSIDE it: the splitter's internal align pass docks it
    at the splitter's right edge -- right against ed2's left edge, the
    same strip revision 3 occupied -- and keeps it there through every
    drag and resize. FindAlignControl scans only the splitter's SIBLINGS
    in panel_ed, so with no plugin control between the splitter and ed2
    the drag target is ed2 again, exactly like stock CudaText.

The editors are NEVER modified, and the widened splitter is restored on
teardown. The splitter's original width is recorded in its own 'tag'
control prop ('DifferSplitterW=<n>') when widened, so even a plugin
reload that finds a stale widening (columns gone, width still 5+W)
restores the true original instead of widening a second time.

Stability: splitter drags and window resizes move the splitter -- with
column B inside it -- automatically; the align pass re-glues it to ed2's
left edge and re-docks column A at the panel's left edge. The per-tab
recurring layout guard (400 ms) no longer fixes any geometry: it just
re-applies the splitter widening if something reset it (e.g. the tab was
un-split and re-split on the same controls), re-applies a drifted column
width, repaints when a column's size changed (window resize changed its
height), and self-destructs the columns when the split tree is gone (tab
closed, tab un-split, split switched to horizontal -- the side-by-side
columns are meaningless there).

WIDTH ASYMMETRY AND EQUALIZATION (equalize_split_clients below):
the columns eat editor width ASYMMETRICALLY. Column A (alLeft in the
grouping panel) takes its pixels from editor 1 only. The split bar's
widening is shared FAIRLY: CudaText's SetSplitPos computes editor 2's
width as ratio*(PanelEditors.Width - Splitter.Width) -- it reads the
LIVE (widened) split-bar width -- so at the stock 50% ratio each half
loses half the widening, and the two halves' PANE widths differ by
exactly column A's width (user-measured with 12px columns: client
widths 902 vs 914 px). The overview panel docks into the tab FORM and
shrinks the grouping panel, which the ratio math shares fairly too --
only column A is unfair. With word-wrap on, unequal client widths make
the SAME line wrap at different points in the two halves, and the
side-by-side pairing visibly breaks. equalize_split_clients() moves the
split position so the two halves' CLIENT (text-area) widths match,
through the OFFICIAL PROP_SPLIT property (which also updates the saved
ratio, so window resizes -- which re-apply that ratio -- keep the halves
equal) plus a pixel-exact editor-2 width fix-up. It must run BEFORE the
compare paints (no gaps inserted yet); __init__.py calls it right after
the side panels are (re)configured, and again in set_files after the
texts arrive (the line-number gutters grow with the line counts, which
re-breaks equality by a few pixels).

Drawing uses the overview panel's technique -- an 'image' control with an
embedded bitmap that survives resize/minimize/restore automatically --
added via the 'p' control prop as a child of panel_ed (column A) or of
the split bar (column B) (CudaText's prop serializer applies 'p' first,
then the geometry keys, so one PROP_SET call seeds the position while
the control is still alNone and lets the final 'align' key promote the
seed into the aligned slot in a single realignment).

Painting works EXACTLY like the overview panel (PaintboxOverview): the
whole file's hunk edges are drawn AT ONCE into the column bitmap,
permanently -- scaled to the column's full height -- and scrolling
never repaints them. There is deliberately NO viewport-dependent
painting of any kind: a previous revision computed bracket Ys from
ed.convert(CONVERT_CARET_TO_PIXELS) (scroll-aware, viewport-relative)
and repainted on every scroll frame; with big one-sided hunks the
brackets then vanished mid-hunk (a per-side visible-line filter dropped
hunks whose own-side lines had left the viewport, and scroll-gated
repaints made edges lag/disappear during fast scrolls). The overview
panel never had those problems because its picture is scroll-
independent -- so the columns now use the same model: every bracket is
always visible, at its permanent scaled position, and only a real data
or geometry change (fresh compare, wrap counts, column resize, width or
theme change) triggers a repaint. The 150 ms scroll timer and the 33 fps
scroll gate are GONE for the columns; only the overview slider still
tracks scrolling.

Geometry (per diffmap entry [a0, a1, b0, b1], exclusive-end ranges -- the
same records jump()/copy() navigate). Everything is computed in VISUAL
ROWS (wrap-aware line rows + gap rows, the same data model the overview
uses: per-line wrap counts via set_wrap_counts(), compensating gap bands
via add_gap()), then mapped to pixels with one scale factor:

  scale = column_height / max(total_visual_rows(a), total_visual_rows(b))

  ONE scale for BOTH columns, based on the taller side -- exactly the
  overview panel's _get_scale() rule: visually-aligned rows must land
  on the same pixel in both columns (level brackets), which per-side
  scales would break whenever the files' total visual heights differ.

  Per side, the hunk's footprint in visual rows is

    top    = visual_y(r0) - gap_rows(at r0)
    bottom = visual_y(r1)

  where r0/r1 are the hunk's line range on THIS side and visual_y(k) =
  wrap rows of lines 0..k-1 + gap rows recorded at positions <= k.
  visual_y(r1) automatically includes every compensating band recorded
  at the hunk's boundary positions (a leading band sits at position r0,
  a trailing band at position r1), so:
    - a two-sided hunk spans its own lines plus any bands at its edges;
    - a ONE-SIDED hunk (e.g. pure insert on B, a0 == a1) still gets a
      bracket on column A: r0 == r1, and the top extension over the gap
      rows at r0 makes the bracket cover exactly A's compensating band;
    - a band shared with the NEIGHBOURING hunk (the previous hunk's
      trailing band sits at the same index as our leading band) is
      covered by both brackets -- they simply meet there, which is the
      honest picture of a shared alignment boundary.
  Gaps INSIDE the range are included via visual_y(r1) automatically.

  By the engine's alignment invariant (every line-count and wrap-count
  difference is compensated by a band), the two sides' footprints are
  the SAME visual-row interval -- the two columns' brackets are level.

  Drawing: a bracket taller than 2 px gets the full shape (top arm,
  vertical bar, bottom arm); anything smaller collapses to a 2 px dash
  so even a one-line hunk in a 100k-line file stays visible. Brackets
  are Y-clipped to the canvas (bounds clipping, not a visibility
  filter -- pixels outside the bitmap do not exist).

Performance: one paint pass = one background fill + 3 canvas lines (or
one short dash) per hunk, for ALL hunks, plus an O(lines + gaps log
gaps) prefix-sum index build per side (line-row prefix sums and sorted
gap prefix sums, so each hunk's boundaries are two O(log n) queries --
never a per-line walk per hunk). paint() runs only on compare / wrap
change / resize / width change / theme change -- never on scroll, so a
scroll burst costs the columns literally nothing. The 400 ms layout
guard reads a handful of control props per tick and only calls paint()
when a column's size really drifted.

Colors come from the active CudaText theme -- background EdGutterBg, line
EdGutterFont -- so the columns visually read as part of the editors'
gutters in every theme. (See _ed_gutter_colors() in __init__.py for the
lookup order.) No plugin color option is involved, per the feature
request.

See: https://github.com/Alexey-T/CudaText/issues/6477
"""

import bisect

import cudatext as ct

# Default width of one hunk edge column, in pixels. Narrow on purpose
# ("the columns should not occupy a lot of width space"): wide enough for
# a visible bracket (a vertical bar plus the top/bottom arms), narrow
# enough to cost less horizontal space than a scrollbar. Configurable via
# differ.advanced.hunk_edges_width (clamped 6..40).
COLUMNS_WIDTH_DEFAULT = 12

# Interval (milliseconds) of the per-tab recurring layout-guard timer.
# The align system keeps both columns in their slots through splitter
# drags and window resizes by itself (column A at the panel's left
# edge, column B inside the re-glued split bar); the guard only has to
# (a) re-apply the split-bar widening if something reset it, (b) repaint
# when a column's size changed (window resize changed its height), and
# (c) self-destruct when the split tree is gone. 400 ms is far below
# human reaction time for a geometry restore, yet adds only a few
# control-prop reads per tick.
LAYOUT_GUARD_INTERVAL = 400

# dlg control names of the two columns (unique enough to never collide
# with CudaText's own controls 'panel_ed'/'ed1'/'ed2'/'splitterN' or
# other plugins'). Index 0 = column of editor A (left), 1 = editor B.
_CTL_NAMES = ('DifferHunkEdgesA', 'DifferHunkEdgesB')

# Callback suffix of the layout-guard timer (the plugin routes it to the
# owning tab session; see Command._columns_layout_timer in __init__.py).
_TIMER_CMD = '_columns_layout_timer'

# Bracket drawing metrics, relative to the column's client width W:
#   - the vertical bar is a 1px line at x = BAR_X (same weight as
#     ATSynEdit's own code-fold staples, EdBlockStaple)
#   - the top/bottom arms run from the bar to x = W-ARM_PAD-1
# With the default W=12: bar at x=3, arms from x=3 to x=8.
BAR_X = 3
ARM_PAD = 3


class HunkColumns:
    """Manages the two hunk edge columns of one compare tab.

    Column A is an alLeft child control of the grouping panel that
    parents the two split editors (panel_ed) -- it occupies the panel's
    left-edge strip and editor 1 (alClient) shrinks around it. Column B
    is an alRight child control INSIDE the split bar, which is widened
    by the column width for the purpose (it keeps its right edge glued
    to editor 2's left edge and grows leftward) -- this way no plugin
    control ever sits in panel_ed between the split bar and editor 2,
    where it would steal the LCL splitter's drag target
    (FindAlignControl) and break the splitter drag. Neither editor is
    ever modified; the split bar's original width is restored on
    teardown (recognized across plugin reloads via its 'tag' prop), so
    destroying the columns restores the original layout by itself. A
    recurring guard re-applies a reset split-bar width, repaints on
    size changes, and self-destructs when the split tree is gone. All
    state is per-instance, so two tabs' columns can never mix (the
    session holds one instance).
    """

    def __init__(self):
        # Handle of the split-tab form (an editor's PROP_HANDLE_PARENT):
        # the dialog whose control tree holds panel_ed / the editors /
        # our columns. None until create() succeeds.
        self.h_form = None
        # Per-column handles. Index 0 = the LEFT column (editor A),
        # index 1 = the column of editor B. h_dlgs holds, per column, the
        # dialog handle used for control lookups (the shared h_form when
        # created; tests preset other handles to drive size lookups).
        self.h_dlgs = [None, None]
        self.h_images = [None, None]
        self.h_bitmaps = [None, None]
        self.h_canvases = [None, None]
        self._ctl_indices = [None, None]
        # Editors the brackets are computed against.
        self.a_ed = None
        self.b_ed = None
        # Name of the grouping panel control that parents the editors
        # ('panel_ed' in current CudaText builds; '' when the editors sit
        # directly in the form). Column A is parented there too.
        self._panel_name = ''
        # The split bar that hosts column B: its dlg-control NAME ('' =
        # not attached) and its ORIGINAL width in pixels, recorded in
        # the splitter's own 'tag' prop as well, so a stale widening
        # left by a dead instance (plugin reload) is recognized and
        # restored instead of widened a second time.
        self._splitter_name = ''
        self._splitter_w_orig = None
        # Callback string of the recurring layout-guard timer ('' = not
        # armed).
        self._timer_cb = ''
        # Attachment generation: incremented on every _recreate() (fresh
        # create AND the rebuild-after-split-tree-change in sync()). The
        # refresh flow puts it into the tab session's panels_sig, so a
        # re-split tab (which resets the split position to 50%) gets its
        # client widths re-equalized even though no option changed.
        self.generation = 0
        # Colors: background = EdGutterBg, lines = EdGutterFont (from the
        # active theme; set via set_colors()).
        self.color_bg = 0xF0F0F0
        self.color_line = 0x404040
        # Column width in pixels (set via set_width()).
        self.width = COLUMNS_WIDTH_DEFAULT
        # (w, h) each column was last painted at -- the layout guard
        # repaints when a column's control size drifted from it (window
        # resize changes the columns' height).
        self._last_size = [None, None]
        # --- Hunk data, refreshed once per compare (the same data model
        # the overview panel uses; see the module docstring) ---
        # diffmap entries [a0, a1, b0, b1] (exclusive ends), in file order.
        self.hunks = []
        # Line counts of both editors at compare time.
        self.line_count_a = 0
        self.line_count_b = 0
        # Per-line visual row counts (word wrap), or None when wrapping
        # is off (each line = 1 row). Fed via set_wrap_counts() right
        # after the compare, together with the overview's feed.
        self.wrap_counts_a = None
        self.wrap_counts_b = None
        # Compensating gap bands per side, as (after_line, gap_rows)
        # tuples in event order -- the same records the overview gets
        # via its add_gap(). Fed during the compare's paint loop.
        self.gaps_a = []
        self.gaps_b = []
        # Per-side visual index built by _build_visual_index() at paint
        # time: side -> (line_row_prefix, gap_lines_sorted, gap_row_prefix)
        # -- see _visual_y().
        self._vis = {}

    # ------------------------------------------------------------------
    # control-tree resolution (indices are NOT identities -- everything is
    # re-resolved by name / editor handle on every pass, because any
    # plugin adding or removing a control shifts the indices)
    # ------------------------------------------------------------------

    def is_created(self):
        """Return True if the column controls have been created."""
        return self.h_dlgs[0] is not None or self.h_dlgs[1] is not None

    def _locate_editors(self):
        """Resolve the two compare editors inside the split-tab form.

        Returns (index_a, index_b, panel_name) -- the dlg-control indices
        of editor A (primary) and editor B (secondary), and the name of
        the control that parents them ('panel_ed'; '' = the form itself)
        -- or None when the tree cannot serve two editor controls (no
        split, dead handles).

        Editors are matched by control handle (DLG_CTL_HANDLE vs the
        Editor objects' PROP_HANDLE_SELF), which is unambiguous even when
        other editor controls exist; the enumeration order (ed1 before
        ed2 -- the order CudaText registers them in) is the fallback when
        handle queries are unavailable.
        """
        if self.h_form is None:
            return None
        try:
            count = ct.dlg_proc(self.h_form, ct.DLG_CTL_COUNT)
        except Exception:
            return None
        if not count:
            return None
        h_a = h_b = None
        try:
            h_a = self.a_ed.get_prop(ct.PROP_HANDLE_SELF)
        except Exception:
            h_a = None
        try:
            h_b = self.b_ed.get_prop(ct.PROP_HANDLE_SELF)
        except Exception:
            h_b = None
        found = []   # (index, handle, parent_name) of editor controls
        for i in range(count):
            try:
                props = ct.dlg_proc(self.h_form, ct.DLG_CTL_PROP_GET,
                                    index=i)
            except Exception:
                continue
            if not props or props.get('type') != 'editor':
                continue
            try:
                h_ctl = ct.dlg_proc(self.h_form, ct.DLG_CTL_HANDLE, index=i)
            except Exception:
                h_ctl = None
            found.append((i, h_ctl, props.get('p', '')))
        if len(found) < 2:
            return None
        idx_a = idx_b = None
        panel = ''
        if h_a:
            for i, h_ctl, p in found:
                if h_ctl == h_a:
                    idx_a, panel = i, p
                    break
        if h_b and idx_a is not None:
            for i, h_ctl, _p in found:
                if h_ctl == h_b and i != idx_a:
                    idx_b = i
                    break
        if idx_a is None or idx_b is None:
            # Handle matching unavailable/incomplete: fall back to the
            # registration order (ed1 = primary first, ed2 = secondary).
            idx_a, panel = found[0][0], found[0][2]
            idx_b = found[1][0]
        return idx_a, idx_b, panel

    def _find_our_control(self, idx):
        """Current dlg-control index of our column control (by NAME --
        stable across other controls being added/removed), or None."""
        if self.h_form is None:
            return None
        try:
            i = ct.dlg_proc(self.h_form, ct.DLG_CTL_FIND, _CTL_NAMES[idx])
        except Exception:
            return None
        if i is None or i < 0:
            return None
        return i

    def _ctl_index(self, idx):
        """Control index for size lookups: the fresh name-resolved index
        when attached to a form tree, else the stored index (tests preset
        it together with h_dlgs to drive size lookups directly)."""
        i = self._find_our_control(idx)
        if i is not None:
            return i
        return self._ctl_indices[idx]

    # Marker stored in the split bar's 'tag' prop while it is widened
    # ('DifferSplitterW=<original width>'): lets a later -- reloaded --
    # instance recognize a stale widening and restore the true original
    # instead of widening a second time.
    _SPLITTER_TAG = 'DifferSplitterW='

    def _find_splitter(self, panel_name):
        """Resolve the split bar control of the split tree: the
        'splitter'-typed child of the grouping panel ('splitter1' in
        current CudaText builds -- matched by TYPE, with a name-prefix
        fallback, so a renamed control still resolves). Returns
        (index, name, props) or None."""
        if self.h_form is None:
            return None
        try:
            count = ct.dlg_proc(self.h_form, ct.DLG_CTL_COUNT)
        except Exception:
            return None
        if not count:
            return None
        for i in range(count):
            try:
                props = ct.dlg_proc(self.h_form, ct.DLG_CTL_PROP_GET,
                                    index=i)
            except Exception:
                continue
            if not props:
                continue
            if panel_name and props.get('p', '') != panel_name:
                continue
            name = props.get('name', '')
            if (props.get('type') == 'splitter' or
                    name.lower().startswith('splitter')):
                return i, name, props
        return None

    def _splitter_tag_value(self, props):
        """Parse the recorded original width out of the split bar's
        props ('tag' = 'DifferSplitterW=<n>'); None when not tagged."""
        tag = props.get('tag', '') or ''
        if not tag.startswith(self._SPLITTER_TAG):
            return None
        try:
            return int(tag[len(self._SPLITTER_TAG):])
        except (TypeError, ValueError):
            return None

    def _restore_splitter_width(self):
        """Undo the split bar widening done by THIS instance -- or, via
        the 'tag' marker, a stale one left by a dead instance after a
        plugin reload. No-op when the split bar cannot be found or was
        never widened. Returns True when a width was actually restored."""
        if self.h_form is None:
            return False
        spl = self._find_splitter(self._panel_name)
        if spl is None:
            return False
        i, _name, props = spl
        w = props.get('w')
        tagged = self._splitter_tag_value(props)
        orig = self._splitter_w_orig
        if orig is None:
            orig = tagged
        if orig is None or w is None or w == orig:
            # Never widened (or already restored): just clear a stale
            # tag if one is left.
            if tagged is not None:
                try:
                    ct.dlg_proc(self.h_form, ct.DLG_CTL_PROP_SET,
                                index=i, prop={'tag': ''})
                except Exception:
                    pass
            self._splitter_name = ''
            self._splitter_w_orig = None
            return False
        try:
            ct.dlg_proc(self.h_form, ct.DLG_CTL_PROP_SET, index=i,
                        prop={'w': orig, 'tag': ''})
        except Exception:
            return False
        self._splitter_name = ''
        self._splitter_w_orig = None
        return True

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def create(self, a_ed, b_ed):
        """Create the two columns: the A column is an alLeft child of the
        grouping panel that parents the two editors (the panel's
        left-edge strip; editor 1, alClient, shrinks around it
        automatically), and the B column is an alRight child of the
        split bar, which is widened by the column width (column B then
        docks at the split bar's right edge = editor 2's left edge).
        The editors themselves are never modified -- see the module
        docstring for why the earlier revisions (docked dialogs; alNone
        columns plus editor x/w shifts undone by the align system; and
        an alRight column between the splitter and editor 2, which
        hijacked the splitter's drag target) failed.

        Args:
            a_ed: editor A (primary half)
            b_ed: editor B (secondary half)
        """
        self.a_ed = a_ed
        self.b_ed = b_ed
        self._recreate()

    def sync(self, a_ed, b_ed):
        """Re-point the columns at the (possibly new) editor objects of
        the SAME compare tab -- called on every refresh. When the split
        tree changed underneath (tab un-split and re-split, controls
        re-created), the column controls are rebuilt on the new tree;
        otherwise the layout is just re-checked (this also applies a
        changed width option at once)."""
        self.a_ed = a_ed
        self.b_ed = b_ed
        if not self.is_created():
            self._recreate()
            return
        try:
            h_form = a_ed.get_prop(ct.PROP_HANDLE_PARENT)
        except Exception:
            h_form = 0
        tree_ok = (h_form == self.h_form and
                   self._locate_editors() is not None and
                   self._find_our_control(0) is not None and
                   self._find_our_control(1) is not None)
        if not tree_ok:
            # Rebuild from scratch on the (new) tree. _teardown keeps the
            # editors/data (the caller re-feeds set_data right after).
            self._teardown()
            self._recreate()
        else:
            self.check_layout()

    def _recreate(self):
        """Attach the column controls to the current split tree. On any
        failure the object stays un-created (is_created() False) -- the
        next sync() retries, and painting simply no-ops meanwhile.

        Column A is an alLeft child of the grouping panel (the panel's
        left-edge strip; editor 1, alClient, shrinks around it). Column
        B is an alRight child INSIDE the split bar: the split bar is
        widened by the column width (alRight keeps its right edge glued
        to editor 2's left edge and grows LEFTWARD, so only editor 1
        gives way), and the column docks at the split bar's right edge,
        right against editor 2's left edge. This leaves the LCL
        splitter's drag target -- FindAlignControl: the aligned sibling
        next to the split bar, normally editor 2 -- untouched; see the
        module docstring for why an alRight column of our own in
        panel_ed would steal that spot and break the drag."""
        try:
            h_form = self.a_ed.get_prop(ct.PROP_HANDLE_PARENT)
        except Exception:
            h_form = 0
        if not h_form:
            return
        self.h_form = h_form
        self.generation += 1
        # Drop stale columns of a previous instance/plugin reload first,
        # and any stale split-bar widening left behind (recognized via
        # the split bar's 'tag' marker).
        self._delete_our_controls()
        self._restore_splitter_width()
        loc = self._locate_editors()
        if loc is None:
            return   # no two-editor split to attach to (yet)
        self._panel_name = loc[2]
        try:
            ed2 = ct.dlg_proc(self.h_form, ct.DLG_CTL_PROP_GET,
                              index=loc[1])
        except Exception:
            ed2 = None
        if not ed2 or 'x' not in ed2:
            return
        if not ed2.get('vis', True):
            # The tab is currently UN-SPLIT (ed2 hidden): the columns
            # are meaningless -- don't attach (avoids the guard having
            # to create-and-destroy them on every refresh). The next
            # sync() after a re-split attaches them.
            return
        spl = self._find_splitter(self._panel_name)
        if spl is None:
            return   # no split bar in the tree (unexpected) -- retry later
        spl_i, spl_name, spl_props = spl
        spl_w = spl_props.get('w', 0) or 0
        if not spl_name or spl_w <= 0:
            return
        made = []
        widened = False
        try:
            # --- column A: alLeft -> the panel's left-edge strip ---
            # Prop order matters and is guaranteed: CudaText's prop
            # serializer applies 'p' first (re-parent into panel_ed),
            # then the other keys in insertion order -- so x/y/w/h
            # seed the position while the control is still alNone,
            # and the final 'align' key lets ONE realignment promote
            # the seed into the aligned slot.
            i = ct.dlg_proc(self.h_form, ct.DLG_CTL_ADD, 'image')
            ct.dlg_proc(self.h_form, ct.DLG_CTL_PROP_SET, index=i,
                        prop={
                            'name': _CTL_NAMES[0],
                            'p': self._panel_name,
                            'x': 0, 'y': 0,
                            'w': self.width, 'h': 100,
                            'align': ct.ALIGN_LEFT,
                            'vis': True,
                        })
            made.append(i)
            self._ctl_indices[0] = i
            self.h_dlgs[0] = self.h_form
            h_img = ct.dlg_proc(self.h_form, ct.DLG_CTL_HANDLE,
                                index=i)
            self.h_images[0] = h_img
            self.h_bitmaps[0] = ct.image_proc(
                h_img, ct.IMAGE_GET_BITMAP)
            self.h_canvases[0] = ct.bitmap_proc(
                self.h_bitmaps[0], ct.BITMAP_GET_CANVAS)
            # --- widen the split bar by the column width: alRight keeps
            # its right edge at ed2's left edge and grows LEFTWARD, so
            # only ed1 (alClient) shrinks; ed2 and the split-ratio math
            # (which reads the live Splitter.Width) are untouched. The
            # original width is noted in the split bar's 'tag' prop so a
            # reloaded plugin can recognize and undo this. ---
            ct.dlg_proc(self.h_form, ct.DLG_CTL_PROP_SET, index=spl_i,
                        prop={
                            'w': spl_w + self.width,
                            'tag': self._SPLITTER_TAG + str(spl_w),
                        })
            widened = True
            self._splitter_name = spl_name
            self._splitter_w_orig = spl_w
            # --- column B: alRight child of the split bar, docked at
            # its right edge = editor 2's left edge (the split bar
            # spans the same rows as the editors, so editor Ys map
            # 1:1; a child of the split bar never enters panel_ed's
            # align chain, so the splitter drag keeps targeting ed2).
            # NOTE: a child's x/y are PARENT-RELATIVE -- the column's
            # slot inside the widened split bar starts at the original
            # split-bar width (x = spl_w) and runs to its right edge. ---
            i = ct.dlg_proc(self.h_form, ct.DLG_CTL_ADD, 'image')
            ct.dlg_proc(self.h_form, ct.DLG_CTL_PROP_SET, index=i,
                        prop={
                            'name': _CTL_NAMES[1],
                            'p': spl_name,
                            'x': spl_w, 'y': 0,
                            'w': self.width, 'h': 100,
                            'align': ct.ALIGN_RIGHT,
                            'vis': True,
                        })
            made.append(i)
            self._ctl_indices[1] = i
            self.h_dlgs[1] = self.h_form
            h_img = ct.dlg_proc(self.h_form, ct.DLG_CTL_HANDLE,
                                index=i)
            self.h_images[1] = h_img
            self.h_bitmaps[1] = ct.image_proc(
                h_img, ct.IMAGE_GET_BITMAP)
            self.h_canvases[1] = ct.bitmap_proc(
                self.h_bitmaps[1], ct.BITMAP_GET_CANVAS)
        except Exception:
            # Roll back whatever was added and stay un-created. The
            # editors were never touched; the split-bar widening is
            # undone via its 'tag' marker.
            for i in reversed(made):
                try:
                    ct.dlg_proc(self.h_form, ct.DLG_CTL_DELETE, index=i)
                except Exception:
                    pass
            if widened:
                self._restore_splitter_width()
            self._clear_handles()
            return
        self._start_timer()

    def _clear_handles(self):
        self.h_dlgs = [None, None]
        self.h_images = [None, None]
        self.h_bitmaps = [None, None]
        self.h_canvases = [None, None]
        self._ctl_indices = [None, None]
        self._last_size = [None, None]

    def _delete_our_controls(self):
        """Delete our column controls from the form tree (found by name;
        deleted back-to-front so the removals don't shift the indices of
        the ones still to delete)."""
        if self.h_form is None:
            return
        indexes = []
        for idx in (0, 1):
            i = self._find_our_control(idx)
            if i is not None:
                indexes.append(i)
        for i in sorted(indexes, reverse=True):
            try:
                ct.dlg_proc(self.h_form, ct.DLG_CTL_DELETE, index=i)
            except Exception:
                pass

    def _teardown(self):
        """Remove the columns from the form: restore the split bar's
        original width (the align system re-flows editor 1 around it by
        itself), delete the controls and stop the layout guard. The
        editors were never modified, so the align system re-flows the
        panel to its own layout by itself. Keeps the editors/hunk data
        (used by sync()'s rebuild path)."""
        self._stop_timer()
        self._restore_splitter_width()
        self._delete_our_controls()
        self.h_form = None
        self._panel_name = ''
        self._splitter_name = ''
        self._splitter_w_orig = None
        self._clear_handles()

    def destroy(self):
        """Tear the columns down completely: delete the controls (the
        align system restores the editors' full rects on its own), stop
        the layout guard, and drop the compare data (a destroyed column
        must never paint stale brackets if it gets recreated later)."""
        self._teardown()
        self.a_ed = None
        self.b_ed = None
        self.clear_data()
        self.wrap_counts_a = None
        self.wrap_counts_b = None
        self.line_count_a = 0
        self.line_count_b = 0

    # ------------------------------------------------------------------
    # layout guard: the align system keeps every slot (column A at the
    # panel's left edge, column B inside the re-glued split bar); this
    # only re-applies a reset split-bar width or column width, repaints
    # on size changes, and self-destructs when the split tree is gone
    # ------------------------------------------------------------------

    def check_layout(self):
        """The layout guard, armed by the per-tab recurring timer (and
        called from sync()). The LCL align system keeps both columns in
        their slots through splitter drags and window resizes by itself
        (column A is re-docked at the panel's left edge; column B rides
        inside the split bar, which the align pass re-glues to editor
        2's left edge). What the guard fixes: a split-bar width that
        was reset behind our back (e.g. the tab was un-split and
        re-split on the same controls), a column width that drifted
        from the configured one, and the painted bitmaps after a
        column resized. It also destroys the columns when the split
        tree is gone (tab closed, tab un-split, split switched to
        horizontal -- the side-by-side columns are meaningless there),
        which also stops the timer.

        Returns True while the columns are alive."""
        if not self.is_created():
            return False
        loc = self._locate_editors()
        if loc is None:
            self.destroy()
            return False
        bars = []
        for idx in (0, 1):
            bi = self._find_our_control(idx)
            if bi is None:
                self.destroy()
                return False
            bars.append(bi)
        try:
            ed1 = ct.dlg_proc(self.h_form, ct.DLG_CTL_PROP_GET,
                              index=loc[0])
            ed2 = ct.dlg_proc(self.h_form, ct.DLG_CTL_PROP_GET,
                              index=loc[1])
            bar_a = ct.dlg_proc(self.h_form, ct.DLG_CTL_PROP_GET,
                                index=bars[0])
            bar_b = ct.dlg_proc(self.h_form, ct.DLG_CTL_PROP_GET,
                                index=bars[1])
        except Exception:
            self.destroy()
            return False
        if not (ed1 and ed2 and bar_a and bar_b):
            self.destroy()
            return False
        # Tab un-split (ed2 hidden) or split switched to horizontal
        # (ed2 no longer beside ed1): the side-by-side columns are
        # meaningless -- self-destruct.
        if not ed2.get('vis', True) or ed2.get('y') != ed1.get('y'):
            self.destroy()
            return False
        moved = False
        # Column A (alLeft) always owns the panel's left strip; only its
        # width can drift (after a width-option change raced a rebuild).
        if bar_a.get('w') != self.width:
            try:
                ct.dlg_proc(self.h_form, ct.DLG_CTL_PROP_SET,
                            index=bars[0], prop={'w': self.width})
            except Exception:
                pass
            moved = True
        # Column B (alRight inside the split bar) is glued to the split
        # bar's right edge = ed2's left edge by the align pass; only its
        # width can drift.
        if bar_b.get('w') != self.width:
            try:
                ct.dlg_proc(self.h_form, ct.DLG_CTL_PROP_SET,
                            index=bars[1], prop={'w': self.width})
            except Exception:
                pass
            moved = True
        # The split bar must stay widened by the column width (its
        # position/height are align-managed; only the width is ours).
        # A reset means something re-created or resized the split bar
        # behind our back -- re-apply, and the realign re-glues it.
        if self._splitter_w_orig is not None:
            spl = self._find_splitter(self._panel_name)
            if (spl is not None and
                    spl[2].get('w') != self._splitter_w_orig + self.width):
                try:
                    ct.dlg_proc(self.h_form, ct.DLG_CTL_PROP_SET,
                                index=spl[0],
                                prop={'w': self._splitter_w_orig +
                                      self.width})
                    moved = True
                except Exception:
                    pass
        # Repaint when the guard fixed something or a column's control
        # size drifted from the last painted size (a window resize
        # changed the columns' height; the bitmap must follow).
        if moved:
            self.paint()
        else:
            for idx, bar in ((0, bar_a), (1, bar_b)):
                if (bar.get('w', 0), bar.get('h', 0)) != self._last_size[idx]:
                    self.paint()
                    break
        return True

    # ------------------------------------------------------------------
    # layout-guard timer
    # ------------------------------------------------------------------

    def _start_timer(self):
        """Arm the per-tab recurring layout guard (TIMER_START). The
        callback string embeds the tab id, so the plugin routes the tick
        to THIS tab's session (Command._columns_layout_timer)."""
        try:
            tab_id = self.a_ed.get_prop(ct.PROP_TAB_ID)
        except Exception:
            tab_id = None
        if tab_id is None:
            return
        self._timer_cb = 'module=cuda_differ;cmd={};info={};'.format(
            _TIMER_CMD, tab_id)
        try:
            ct.timer_proc(ct.TIMER_START, self._timer_cb,
                          LAYOUT_GUARD_INTERVAL)
        except Exception:
            self._timer_cb = ''

    def _stop_timer(self):
        if self._timer_cb:
            try:
                # timer_proc's signature is (code, callback, interval,
                # tag='') -- the interval value is ignored for
                # TIMER_STOP, but the argument is required (the timer is
                # matched by its callback string).
                ct.timer_proc(ct.TIMER_STOP, self._timer_cb, 0)
            except Exception:
                pass
            self._timer_cb = ''

    # ------------------------------------------------------------------
    # data / options
    # ------------------------------------------------------------------

    def set_colors(self, color_bg, color_line):
        """Set the column colors: background (theme EdGutterBg) and the
        bracket line color (theme EdGutterFont)."""
        if color_bg is not None:
            self.color_bg = color_bg
        if color_line is not None:
            self.color_line = color_line

    def set_width(self, width):
        """Set the column width in pixels (clamped to a sane range).
        When attached, both columns are resized in place and the split
        bar's widening follows (it grows/shrinks leftward from editor
        2's left edge) -- the align system re-flows editor 1 around the
        changes by itself; nothing else is touched. A no-op when the
        width did not change (every refresh calls this; skipping the
        unchanged case avoids pointless realign churn)."""
        try:
            width = int(width)
        except (TypeError, ValueError):
            width = COLUMNS_WIDTH_DEFAULT
        width = max(6, min(40, width))
        if width == self.width:
            return
        self.width = width
        if self.is_created():
            for idx in (0, 1):
                bi = self._find_our_control(idx)
                if bi is None:
                    continue
                try:
                    ct.dlg_proc(self.h_form, ct.DLG_CTL_PROP_SET,
                                index=bi, prop={'w': self.width})
                except Exception:
                    pass
            if self._splitter_w_orig is not None:
                spl = self._find_splitter(self._panel_name)
                if spl is not None:
                    try:
                        ct.dlg_proc(self.h_form, ct.DLG_CTL_PROP_SET,
                                    index=spl[0],
                                    prop={'w': self._splitter_w_orig +
                                          self.width})
                    except Exception:
                        pass
            self.paint()

    def set_data(self, hunks, line_count_a, line_count_b):
        """Store the fresh compare's hunk records for painting.

        Args:
            hunks: diffmap entries [a0, a1, b0, b1] (exclusive ends),
                in file order. Ignored (suppressed) differences are not
                in the diffmap and never get brackets.
            line_count_a/b: line counts of the two editors.
        """
        self.hunks = list(hunks or [])
        self.line_count_a = line_count_a
        self.line_count_b = line_count_b

    def set_wrap_counts(self, wrap_a, wrap_b):
        """Set per-line visual row counts for wrap-aware scaling -- the
        exact same feed the overview panel gets (and at the same point
        in the refresh flow, right after the compare).

        Args:
            wrap_a: list where wrap_a[i] = visual rows for line i in
                    a_ed, or None if wrapping is off (each line = 1 row).
            wrap_b: same for b_ed.
        """
        self.wrap_counts_a = wrap_a
        self.wrap_counts_b = wrap_b

    def add_gap(self, side, after_line, gap_rows, ignored=False):
        """Record a compensating gap band, in VISUAL ROWS -- the same
        records the overview panel gets via its add_gap(), fed from the
        same paint-loop events (see _feed_gap in __init__.py). `ignored`
        is accepted for signature parity but unused: the columns draw
        hunk brackets only, never gap fills.

        Args:
            side: 'a' or 'b'
            after_line: the gap sits between lines after_line-1 and
                after_line (it appears BEFORE line after_line in visual
                order).
            gap_rows: number of visual rows the gap occupies.
        """
        if side == 'a':
            self.gaps_a.append((after_line, gap_rows))
        else:
            self.gaps_b.append((after_line, gap_rows))

    def clear_data(self):
        """Clear the collected hunk/gap data. Called before a fresh
        compare (from _setup_side_panels, next to the overview's
        clear_data()) so a repaint during the compare can never mix the
        old compare's hunks with the new compare's gaps."""
        self.hunks = []
        self.gaps_a = []
        self.gaps_b = []
        self._vis = {}

    # ------------------------------------------------------------------
    # painting -- the overview panel's model: ALL hunks at once, scaled
    # to the column height, PERMANENT (never repainted on scroll)
    # ------------------------------------------------------------------

    def _get_size(self, idx):
        """Current client size (w, h) of one column's image control."""
        h = self.h_dlgs[idx]
        if h is None:
            return 0, 0
        try:
            props = ct.dlg_proc(h, ct.DLG_CTL_PROP_GET,
                                index=self._ctl_index(idx))
        except Exception:
            return 0, 0
        if not props:
            return 0, 0
        return props.get('w', self.width), props.get('h', 600)

    def _build_visual_index(self):
        """Build the per-side visual-row index used by painting:

            side -> (line_rows, gap_lines, gap_rows)

        line_rows[k]  = visual rows of lines 0..k-1 (wrap-aware prefix
                        sum; len = line_count + 1)
        gap_lines     = sorted list of the gaps' after_line positions
        gap_rows[k]   = prefix sum of gap rows over gap_lines[:k]

        With these, visual_y(line) = line_rows[line] +
        gap_rows[bisect(gap_lines, line)] is an O(log n) query -- never
        a per-line walk -- and total_rows = line_rows[-1] + gap_rows[-1].
        Mirrors the overview's _line_to_visual_y() /
        _compute_visual_height() math, just precomputed for hunk-size
        query traffic instead of a single linear paint walk.
        """
        self._vis = {}
        for side, line_count, wrap, gaps in (
                ('a', self.line_count_a, self.wrap_counts_a, self.gaps_a),
                ('b', self.line_count_b, self.wrap_counts_b, self.gaps_b)):
            line_rows = [0] * (line_count + 1)
            total = 0
            for i in range(line_count):
                # Same defensive clamps as the overview's
                # _line_visual_rows(): missing/short wrap lists count
                # one row per line, absurd values are clamped to >= 1.
                if wrap is not None and 0 <= i < len(wrap):
                    vr = wrap[i]
                    if not isinstance(vr, int) or vr < 1:
                        vr = 1
                else:
                    vr = 1
                total += vr
                line_rows[i + 1] = total
            sg = sorted(gaps)
            gap_lines = [g[0] for g in sg]
            gap_rows = [0] * (len(sg) + 1)
            for k, g in enumerate(sg):
                gap_rows[k + 1] = gap_rows[k] + max(0, g[1])
            self._vis[side] = (line_rows, gap_lines, gap_rows)

    def _visual_y(self, side, line):
        """Visual-row Y of a line's top: wrap rows of all lines above it
        plus all gap rows recorded at positions <= line (a gap recorded
        at position k sits between lines k-1 and k, so it is above line
        k and below line k-1)."""
        line_rows, gap_lines, gap_rows = self._vis[side]
        if line < 0:
            line = 0
        elif line > len(line_rows) - 1:
            line = len(line_rows) - 1
        # Gaps with after_line <= line: bisect_right over sorted list.
        k = bisect.bisect_right(gap_lines, line)
        return line_rows[line] + gap_rows[k]

    def _gap_rows_at(self, side, after_line):
        """Total gap rows recorded at exactly one position (the hunk's
        leading-band extension: gaps at position r0 sit directly above
        the hunk's first line and belong to its footprint)."""
        _, gap_lines, gap_rows = self._vis[side]
        lo = bisect.bisect_left(gap_lines, after_line)
        hi = bisect.bisect_right(gap_lines, after_line)
        return gap_rows[hi] - gap_rows[lo]

    def _total_rows(self, side):
        """Total visual rows on one side (lines + gaps)."""
        line_rows, _, gap_rows = self._vis[side]
        return max(1, line_rows[-1] + gap_rows[-1])

    def _shared_scale(self, h):
        """Pixels per visual row -- ONE scale for BOTH columns, based on
        the taller side, exactly like the overview panel's _get_scale():
        visually-aligned rows in the editors must land on the same pixel
        in both columns (level brackets), which per-side scales would
        break whenever the two files' total visual heights differ."""
        return (float(h) /
                max(self._total_rows('a'), self._total_rows('b')))

    def _paint_column(self, idx):
        """Paint one column the way the overview panel paints its
        static bitmap: EVERY hunk's bracket at once, scaled to the
        column's full height, permanently. There is deliberately NO
        viewport-dependent painting -- no convert() calls, no scroll
        tracking, no visible-window filter (see the module docstring
        for why that model was abandoned: big one-sided hunks lost
        their edges mid-scroll). Only canvas-bounds clipping remains
        (pixels outside the bitmap do not exist)."""
        c = self.h_canvases[idx]
        if c is None:
            return
        w, h = self._get_size(idx)
        if w <= 0 or h <= 0:
            return
        # Resize the embedded bitmap to the control size (the image
        # control starts with a 0x0 bitmap; painting without resizing
        # shows nothing).
        try:
            ct.bitmap_proc(self.h_bitmaps[idx], ct.BITMAP_SET_SIZE, w, h)
        except Exception:
            return
        self._last_size[idx] = (w, h)
        # Background: the theme gutter color.
        ct.canvas_proc(c, ct.CANVAS_SET_BRUSH, color=self.color_bg,
                       style=ct.BRUSH_SOLID)
        ct.canvas_proc(c, ct.CANVAS_RECT_FILL, x=0, y=0, x2=w, y2=h)
        if not self.hunks:
            return
        side = 'a' if idx == 0 else 'b'
        scale = self._shared_scale(h)
        # The bracket's vertical bar / arms.
        bar_x1 = BAR_X
        arm_x2 = w - ARM_PAD - 1
        ct.canvas_proc(c, ct.CANVAS_SET_PEN, color=self.color_line, size=1)
        for hunk in self.hunks:
            a0, a1, b0, b1 = hunk
            # Defensive: a diffmap entry must cover lines on at least one
            # side; skip malformed all-empty records.
            if a0 >= a1 and b0 >= b1:
                continue
            # This column's own line range for the hunk.
            r0, r1 = (a0, a1) if idx == 0 else (b0, b1)
            # Footprint in visual rows: own lines + bands at the hunk's
            # boundary positions (leading band via the top extension,
            # trailing/interior bands via visual_y(r1)). A one-sided
            # hunk (r0 == r1) degenerates to exactly its compensating
            # gap band, so it keeps a bracket on BOTH columns.
            top_rows = (self._visual_y(side, r0) -
                        self._gap_rows_at(side, r0))
            bottom_rows = self._visual_y(side, r1)
            if bottom_rows < top_rows:
                bottom_rows = top_rows
            top = int(top_rows * scale)
            bottom = int(bottom_rows * scale)
            # A bracket is at least 2 px tall -- a one-line hunk in a
            # huge file must stay visible as a small dash.
            if bottom - top < 2:
                bottom = top + 2
            # Pure Y clip against the column canvas (canvas bounds, NOT
            # a visibility filter): fully off-canvas brackets are
            # skipped, partially off-canvas brackets draw their
            # on-canvas part.
            if bottom <= 0 or top >= h:
                continue
            top = max(0, top)
            bottom = min(h - 1, bottom)
            if bottom - top < 2:
                # Tiny bracket: just a short vertical dash (arms would
                # smear into a blob at this size).
                ct.canvas_proc(c, ct.CANVAS_LINE,
                               x=bar_x1, y=top, x2=bar_x1, y2=bottom)
                continue
            # The bracket: top arm, vertical bar, bottom arm.
            ct.canvas_proc(c, ct.CANVAS_LINE,
                           x=bar_x1, y=top, x2=arm_x2, y2=top)
            ct.canvas_proc(c, ct.CANVAS_LINE,
                           x=bar_x1, y=top, x2=bar_x1, y2=bottom)
            ct.canvas_proc(c, ct.CANVAS_LINE,
                           x=bar_x1, y=bottom, x2=arm_x2, y2=bottom)

    def paint(self):
        """Repaint both columns with the current data. This is the
        overview's repaint_static(), not a scroll handler: it runs on
        compare / wrap-count / size / width / theme changes only --
        scrolling NEVER calls it (the picture is scroll-independent,
        which is the whole point of the model)."""
        if not self.is_created():
            return
        self._build_visual_index()
        self._paint_column(0)
        self._paint_column(1)


# ----------------------------------------------------------------------
# equal client widths of the two compare halves
# ----------------------------------------------------------------------

def equalize_split_clients(a_ed, b_ed, min_pane=120):
    """Equalize the CLIENT (text-area) widths of the two side-by-side
    halves of a compare tab by moving the split position.

    Why: the hunk edge columns eat editor width asymmetrically -- column
    A (alLeft in the grouping panel) takes its pixels from editor 1
    only, while the split bar's widening is shared fairly by the
    split-ratio math (CudaText's SetSplitPos reads the live, widened
    Splitter.Width). At the stock 50% ratio the halves' client widths
    therefore differ by exactly the column width (user-measured with
    12px columns: 902 vs 914 px). With word-wrap on, unequal client
    widths make the same line wrap at different points in the two
    halves, and the side-by-side pairing visibly breaks.

    How: measure both halves' client rects (PROP_RECT_CLIENT) and pane
    widths (the editor controls' 'w'), compute the shift
    X = (client_a - client_b) / 2 that equalizes the clients, then
      1. set PROP_SPLIT to the matching permille ratio -- this ALSO
         updates the frame's SAVED ratio, so window resizes (which
         re-apply it) keep the halves equal up to the permille rounding
         (~1 px); SetSplitPos immediately applies
         Round(ratio * (panel_w - splitter_w)) to editor 2's width;
      2. set editor 2's control width directly for a pixel-exact result
         (the same thing a splitter drag does).
    Client widths -- not pane widths -- are equalized, so per-editor
    chrome differences (line-number gutter digit counts, micromap) are
    compensated too.

    When to call: BEFORE the compare paints (no gaps inserted yet,
    nothing to destroy) -- refresh_compare calls it right after the
    side panels are (re)configured, and set_files calls it before the
    texts are loaded AND once more after (the line-number gutters grow
    with the line counts when the texts arrive, which re-breaks
    equality by a few pixels).

    Returns True when the split was moved. Never raises: any missing
    control/handle just makes it return False (a compare with slightly
    unequal halves still works -- it is only less pretty).
    """
    # --- resolve the split tree (the same resolution rules as
    # _locate_editors/_find_splitter: handles first, then order/type) ---
    try:
        h_form = a_ed.get_prop(ct.PROP_HANDLE_PARENT)
    except Exception:
        h_form = 0
    if not h_form:
        return False
    try:
        count = ct.dlg_proc(h_form, ct.DLG_CTL_COUNT)
    except Exception:
        return False
    if not count:
        return False
    try:
        h_a = a_ed.get_prop(ct.PROP_HANDLE_SELF)
    except Exception:
        h_a = None
    try:
        h_b = b_ed.get_prop(ct.PROP_HANDLE_SELF)
    except Exception:
        h_b = None
    editors = []   # (index, handle) of the editor-typed controls
    for i in range(count):
        try:
            props = ct.dlg_proc(h_form, ct.DLG_CTL_PROP_GET, index=i)
        except Exception:
            continue
        if not props or props.get('type') != 'editor':
            continue
        try:
            h_ctl = ct.dlg_proc(h_form, ct.DLG_CTL_HANDLE, index=i)
        except Exception:
            h_ctl = None
        editors.append((i, h_ctl))
    if len(editors) < 2:
        return False
    idx_a = idx_b = None
    if h_a:
        for i, h_ctl in editors:
            if h_ctl == h_a:
                idx_a = i
                break
    if h_b:
        for i, h_ctl in editors:
            if h_ctl == h_b and i != idx_a:
                idx_b = i
                break
    if idx_a is None or idx_b is None:
        # Handle matching unavailable: registration order (ed1, ed2).
        idx_a, idx_b = editors[0][0], editors[1][0]
    try:
        ed_a = ct.dlg_proc(h_form, ct.DLG_CTL_PROP_GET, index=idx_a)
        ed_b = ct.dlg_proc(h_form, ct.DLG_CTL_PROP_GET, index=idx_b)
    except Exception:
        return False
    if not ed_a or not ed_b:
        return False
    panel = ed_a.get('p', '')
    # Only a VERTICAL side-by-side split has width semantics here; an
    # un-split tab (editor 2 hidden) or a horizontal split must not be
    # touched (mirrors the guard's self-destruct conditions).
    if not ed_b.get('vis', True) or ed_b.get('y') != ed_a.get('y'):
        return False
    pane_a = ed_a.get('w')
    pane_b = ed_b.get('w')
    if not isinstance(pane_a, int) or not isinstance(pane_b, int):
        return False
    # The split bar of this split tree (its LIVE, possibly widened
    # width -- SetSplitPos reads the live value too).
    spl_w = None
    for i in range(count):
        try:
            props = ct.dlg_proc(h_form, ct.DLG_CTL_PROP_GET, index=i)
        except Exception:
            continue
        if not props:
            continue
        if panel and props.get('p', '') != panel:
            continue
        name = props.get('name', '')
        if (props.get('type') == 'splitter' or
                name.lower().startswith('splitter')):
            spl_w = props.get('w')
            break
    if not isinstance(spl_w, int) or spl_w <= 0:
        return False
    # Column A's width (0 when the columns are not attached): the only
    # width eater that takes from editor 1 alone.
    col_w = 0
    try:
        i = ct.dlg_proc(h_form, ct.DLG_CTL_FIND, _CTL_NAMES[0])
    except Exception:
        i = -1
    if i is not None and i >= 0:
        try:
            props = ct.dlg_proc(h_form, ct.DLG_CTL_PROP_GET, index=i)
            if props and isinstance(props.get('w'), int):
                col_w = props['w']
        except Exception:
            pass

    # --- measure the client (text-area) widths and compute the shift ---
    try:
        ra = a_ed.get_prop(ct.PROP_RECT_CLIENT)
        rb = b_ed.get_prop(ct.PROP_RECT_CLIENT)
    except Exception:
        return False
    if not ra or not rb or len(ra) < 3 or len(rb) < 3:
        return False
    try:
        client_a = int(ra[2]) - int(ra[0])
        client_b = int(rb[2]) - int(rb[0])
    except (TypeError, ValueError):
        return False
    delta = client_a - client_b
    if abs(delta) < 2:
        return False   # equal, or a single pixel -- nothing worth moving
    shift = delta // 2        # px editor 2 must GAIN (may be negative)
    new_pane_b = pane_b + shift
    # SetSplitPos's denominator (panel_w - splitter_w), from the tiling
    # invariant: panel_w = col_w + pane_a + spl_w + pane_b.
    denom = col_w + pane_a + pane_b
    if denom <= 0:
        return False
    new_pane_b = max(min_pane, min(denom - min_pane, new_pane_b))
    if new_pane_b == pane_b:
        return False          # already at the clamp -- nothing to move

    # --- 1. the durable ratio (also updates the frame's saved one) ---
    permille = int(round(new_pane_b * 1000.0 / denom))
    permille = max(1, min(999, permille))
    moved = False
    try:
        a_ed.set_prop(ct.PROP_SPLIT, ('v', permille))
        moved = True
    except Exception:
        pass
    # --- 2. pixel-exact fix-up: editor 2's width directly (what a
    # splitter drag sets); the align pass re-glues the split bar to its
    # left edge and gives editor 1 the remaining space. ---
    try:
        ct.dlg_proc(h_form, ct.DLG_CTL_PROP_SET, index=idx_b,
                    prop={'w': new_pane_b})
        moved = True
    except Exception:
        pass
    return moved
