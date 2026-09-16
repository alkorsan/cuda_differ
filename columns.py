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

Drawing uses the overview panel's technique -- an 'image' control with an
embedded bitmap that survives resize/minimize/restore automatically --
added via the 'p' control prop as a child of panel_ed (column A) or of
the split bar (column B) (CudaText's prop serializer applies 'p' first,
then the geometry keys, so one PROP_SET call seeds the position while
the control is still alNone and lets the final 'align' key promote the
seed into the aligned slot in a single realignment). Painting is cheap:
a background fill plus 3 canvas lines per visible hunk, and only visible
hunks are painted at all.

Vertical alignment is pixel-exact: the top/bottom of every bracket come
from ed.convert(CONVERT_CARET_TO_PIXELS) queries, which are gap-aware,
wrap-aware and scroll-aware (they return the same Y the editor itself
uses to draw that line, relative to the editor control). Each column
spans the SAME rows as its editor: column A is an edge-aligned child of
the grouping panel, column B of the split bar, and both the panel and
the split bar span the editors' full height -- so a Y computed against
the editor is directly usable as the column's Y.

Geometry (per diffmap entry [a0, a1, b0, b1], exclusive-end ranges -- the
same records jump()/copy() navigate):

  top:
    The hunk's footprint starts at the first line's top -- EXCEPT when the
    engine put a compensating band ABOVE that side's first hunk line. With
    'Beautify line alignment' on, _find_best_pairs can leave a hunk's
    leading lines unpaired, and the shorter side's compensating band then
    sits directly above the shorter side's first hunk line. The footprint
    top is then the OTHER side's first hunk line top (the two are the same
    row: that is exactly what the band compensates). So:
      - B has OUR compensating band at index b0-1  -> top = y_A(a0)
      - A has OUR compensating band at index a0-1  -> top = y_B(b0)
      - otherwise                                  -> top = y_A(a0)
    ("OUR" = the band compensates lines of THIS hunk on the other side --
    a previous hunk's trailing band may sit at the very same index and
    must NOT move our top; bands are matched by their compensated ranges.)

  bottom:
    The footprint ends below the last line and any trailing bands, i.e. at
    the top of the first line AFTER the hunk (y(k+1) already includes all
    gap bands sitting after line k):
      - a1 < line_count_a  -> bottom = y_A(a1)
      - b1 < line_count_b  -> bottom = y_B(b1)
      - both at EOF        -> convert() clamps a line_count query to the
                             bottom of the last line EXCLUDING trailing
                             gap bands (verified in ATSynEdit's
                             GenericCaretPosToClientPos: the bAfterEnd
                             path adds one char height to the last line's
                             last wrap row but only sums gaps up to index
                             count-2), so the plugin-recorded comp+align
                             band pixels at index count-1 are added back:
                             bottom = max(convert(A, count_a) + bandsA,
                                          convert(B, count_b) + bandsB)

  By the engine's alignment invariant (every line-count and wrap-count
  difference is compensated by a band), the [top, bottom] footprint is the
  SAME on both sides -- both columns draw the same bracket rows for a
  hunk.

Performance: painting runs only for hunks intersecting the visible line
range (PROP_LINE_TOP .. PROP_LINE_BOTTOM, a cheap property read; the
diffmap is scanned with the visible window, not binary-searched, which is
fine because the scan is a tuple-compare loop over integers -- for a
100k-line file with 10k hunks that is ~10k comparisons, far cheaper than
a single canvas call). On scroll, paint() is throttled to ~33 fps by
track_paint() (wall-clock gate, the same approach as the overview slider)
plus a trailing one-shot repaint from the plugin's 150 ms overview timer.
The convert() calls only run for the visible hunks (a couple dozen at
most), each O(log n) in the wrap table. The 400 ms layout guard reads a
handful of control props per tick -- negligible.

Colors come from the active CudaText theme -- background EdGutterBg, line
EdGutterFont -- so the columns visually read as part of the editors'
gutters in every theme. (See _ed_gutter_colors() in __init__.py for the
lookup order.) No plugin color option is involved, per the feature
request.

See: https://github.com/Alexey-T/CudaText/issues/6477
"""

import time

import cudatext as ct

# Default width of one hunk edge column, in pixels. Narrow on purpose
# ("the columns should not occupy a lot of width space"): wide enough for
# a visible bracket (a vertical bar plus the top/bottom arms), narrow
# enough to cost less horizontal space than a scrollbar. Configurable via
# differ.advanced.hunk_edges_width (clamped 6..40).
COLUMNS_WIDTH_DEFAULT = 12

# Wall-clock interval (seconds) between immediate repaints while a scroll
# burst is running -- the same gate the overview slider uses
# (OVERVIEW_TRACK_INTERVAL). ~33 fps looks instant, and paint() here is
# cheap (one background fill + 3 lines per visible hunk).
TRACK_INTERVAL = 0.030

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
        # --- Hunk data, refreshed once per compare via set_data() ---
        # diffmap entries [a0, a1, b0, b1] (exclusive ends), in file order.
        self.hunks = []
        # Line counts of both editors at compare time.
        self.line_count_a = 0
        self.line_count_b = 0
        # Compensating bands per side: {index: [(other_start, other_end)]}
        # -- which other-side line range the band compensates. Used to
        # decide whether a band sitting at a hunk's top-edge index belongs
        # to THIS hunk (staggered beautify band -> the bracket top must
        # move up to the other side's first line) or to the PREVIOUS
        # hunk (trailing band -> the bracket top stays put).
        self.comp_bands_a = {}
        self.comp_bands_b = {}
        # Compensating + wrap-align band pixel sizes per side:
        # {index: px}. Used ONLY for the both-sides-at-EOF bottom edge,
        # where convert() cannot see the trailing bands (see module
        # docstring).
        self.eof_bands_a = {}
        self.eof_bands_b = {}
        # Wall-clock timestamp of the last track_paint(); gates immediate
        # repaints to TRACK_INTERVAL.
        self._track_last_paint = 0.0

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
        self.hunks = []
        self.comp_bands_a = {}
        self.comp_bands_b = {}
        self.eof_bands_a = {}
        self.eof_bands_b = {}

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
                ct.timer_proc(ct.TIMER_STOP, self._timer_cb)
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
        changes by itself; nothing else is touched."""
        try:
            width = int(width)
        except (TypeError, ValueError):
            width = COLUMNS_WIDTH_DEFAULT
        self.width = max(6, min(40, width))
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

    def set_data(self, hunks, line_count_a, line_count_b,
                 comp_bands_a=None, comp_bands_b=None,
                 eof_bands_a=None, eof_bands_b=None):
        """Store the fresh compare's hunk records for painting.

        Args:
            hunks: diffmap entries [a0, a1, b0, b1] (exclusive ends),
                in file order. Ignored (suppressed) differences are not
                in the diffmap and never get brackets.
            line_count_a/b: line counts of the two editors.
            comp_bands_a/b: {index: [(other_start, other_end), ...]} --
                compensating bands per side, with the other-side line
                range each band compensates (see the module docstring's
                top-edge rule). A band recorded at index i is drawn by
                the engine between lines i and i+1.
            eof_bands_a/b: {index: px} -- pixel sizes of compensating +
                wrap-align bands per side; only the entries at index
                count-1 are used (the both-sides-at-EOF bottom edge).
        """
        self.hunks = list(hunks or [])
        self.line_count_a = line_count_a
        self.line_count_b = line_count_b
        self.comp_bands_a = comp_bands_a or {}
        self.comp_bands_b = comp_bands_b or {}
        self.eof_bands_a = eof_bands_a or {}
        self.eof_bands_b = eof_bands_b or {}

    # ------------------------------------------------------------------
    # painting
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

    def _y_of(self, ed, line):
        """Pixel Y of a line's top edge, relative to the editor control
        (gap/wrap/scroll aware), via CONVERT_CARET_TO_PIXELS. For
        line == line_count (one past the last line) ATSynEdit returns the
        BOTTOM of the last line's last wrap row, excluding trailing gap
        bands. Returns None when the conversion is unavailable."""
        try:
            pt = ed.convert(ct.CONVERT_CARET_TO_PIXELS, 0, line)
            if pt:
                return pt[1]
        except Exception:
            pass
        return None

    def _band_is_ours(self, bands, index, r0, r1):
        """Does a compensating band recorded at `index` belong to the
        hunk whose OTHER-side range is [r0, r1)? True when the band's
        compensated other-side range overlaps [r0, r1) -- a previous
        hunk's trailing band sits at the same index but compensates the
        PREVIOUS hunk's lines, so it does not overlap and is not ours."""
        for other_start, other_end in bands.get(index, ()):
            if other_start < r1 and other_end > r0:
                return True
        return False

    def _bracket(self, h):
        """Compute the [top, bottom] pixel rows of one hunk's footprint
        (see the module docstring for the rules). Returns (top, bottom)
        or None when a needed conversion failed."""
        a0, a1, b0, b1 = h
        # --- top ---
        if self._band_is_ours(self.comp_bands_b, b0 - 1, a0, a1):
            # B's compensating band sits directly above B's first hunk
            # line and belongs to THIS hunk (beautify-staggered leading
            # lines on A): the footprint starts at A's first hunk line.
            top = self._y_of(self.a_ed, a0)
        elif self._band_is_ours(self.comp_bands_a, a0 - 1, b0, b1):
            # Mirror case: A's band above A's first hunk line.
            top = self._y_of(self.b_ed, b0)
        else:
            # No staggered band above either first line: both first
            # lines sit on the same row (the engine's alignment
            # invariant), so either query works.
            top = self._y_of(self.a_ed, a0)
        # --- bottom ---
        if a1 < self.line_count_a:
            bottom = self._y_of(self.a_ed, a1)
        elif b1 < self.line_count_b:
            bottom = self._y_of(self.b_ed, b1)
        else:
            # Both sides end at EOF: convert() clamps to the bottom of
            # the last line EXCLUDING trailing bands, so add the recorded
            # comp+align band pixels back, per side, and take the lower
            # edge of the two (they are the same row by the alignment
            # invariant; max() is just belt-and-braces).
            ya = self._y_of(self.a_ed, self.line_count_a)
            if ya is not None:
                ya += self.eof_bands_a.get(self.line_count_a - 1, 0)
            yb = self._y_of(self.b_ed, self.line_count_b)
            if yb is not None:
                yb += self.eof_bands_b.get(self.line_count_b - 1, 0)
            vals = [v for v in (ya, yb) if v is not None]
            bottom = max(vals) if vals else None
        if top is None or bottom is None:
            return None
        # A footprint is at least one line tall; guard against a
        # degenerate 0-height bracket (should not happen, but a flat
        # bracket would be invisible).
        if bottom <= top:
            bottom = top + 1
        return top, bottom

    def _paint_column(self, idx, ed, line_count):
        """Paint one column: background fill plus a bracket per hunk
        intersecting the editor's visible line range. Hunks fully above
        or below the viewport are skipped entirely -- with a huge file
        and thousands of hunks, only the visible couple dozen get any
        convert() calls and canvas lines."""
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
        # Visible line window of THIS column's editor (cheap property
        # reads; PROP_LINE_BOTTOM considers word wrap).
        try:
            vis_top = ed.get_prop(ct.PROP_LINE_TOP) or 0
            vis_bot = ed.get_prop(ct.PROP_LINE_BOTTOM)
            if vis_bot is None:
                vis_bot = line_count
        except Exception:
            vis_top, vis_bot = 0, line_count
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
            # Skip hunks entirely outside the visible window (with a
            # one-hunk margin so a bracket poking in from just above or
            # below the viewport still draws).
            if r1 - 1 < vis_top - 1 or r0 > vis_bot + 1:
                continue
            br = self._bracket(hunk)
            if br is None:
                continue
            top, bottom = br
            # Pure Y clip against the column canvas: partially visible
            # hunks draw their visible part.
            if bottom <= 0 or top >= h:
                continue
            top = max(0, top)
            bottom = min(h - 1, bottom)
            # The bracket: top arm, vertical bar, bottom arm.
            ct.canvas_proc(c, ct.CANVAS_LINE,
                           x=bar_x1, y=top, x2=arm_x2, y2=top)
            ct.canvas_proc(c, ct.CANVAS_LINE,
                           x=bar_x1, y=top, x2=bar_x1, y2=bottom)
            ct.canvas_proc(c, ct.CANVAS_LINE,
                           x=bar_x1, y=bottom, x2=arm_x2, y2=bottom)

    def paint(self):
        """Repaint both columns with the current data."""
        if not self.is_created():
            return
        self._paint_column(0, self.a_ed, self.line_count_a)
        self._paint_column(1, self.b_ed, self.line_count_b)

    def track_paint(self):
        """Immediate repaint for scroll bursts, wall-clock throttled to
        ~33 fps (the same approach as the overview's slider: a wall-clock
        gate, never a timer, because a scroll/drag floods the message
        queue and starves WM_TIMER). The plugin's one-shot 150 ms timer
        does the final full repaint after scrolling stops."""
        now = time.perf_counter()
        if now - self._track_last_paint < TRACK_INTERVAL:
            return
        self._track_last_paint = now
        self.paint()
