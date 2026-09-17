import functools
import os
import re
import json
import time
import typing as tp

import cudatext as ct
import cudatext_cmd as ct_cmd
import cudatext_keys as ct_keys
import cudax_lib as ctx

from . import differ_native as dfn
from . import differ_python as dfp
from .columns import (COLUMNS_WIDTH_DEFAULT, HunkColumns,
                      equalize_split_clients)
from .overview import PaintboxOverview
from .profiling import Profiler, enable_profiling, profiling_report, reset_profiling
from .utils import split_lines_safe, ScrollSplittedTab
from difflib import unified_diff
from cudax_lib import get_translation
_ = get_translation(__file__)  # I18N

# df is used as a namespace for event constants (A_LINE_DEL, B_LINE_ADD, etc.).
# Both differ_native and differ_python define identical constants, so we alias
# df to differ_native for backwards-compatible constant access. The Differ
# class itself is chosen at runtime based on the configured algorithm — see
# Command._create_differ below.
df = dfn

DIFF_TAG = 148
# Gap tag for ignored-difference gaps (DIFF_IGN_BLANK_LINES suppressed
# hunks). Separate from DIFF_TAG so the compensating gaps WinMerge-style
# "ignored differences" insert are identifiable (and deletable) on
# their own — they are also painted with the ignored color instead of
# the regular gap color.
IGN_GAP_TAG = 149
NKIND_DELETED = 24
NKIND_ADDED = 25
NKIND_CHANGED = 26
GAP_WIDTH = 5000
DECOR_CHAR = '■'
DEFAULT_SYNC_SCROLL = '1'
U_PREFIX = 'untitled:'

# Hotkeys for compare tabs, dispatched by Command.on_key. The plugin
# subscribes to the lazy on_key~ event AT RUNTIME with a key-code filter
# (see _sync_on_key_subscription) -- install.inf does NOT list on_key.
# Key code -> tuple of (Command method name, required modifier state)
# entries, tried in order until one matches EXACTLY:
#   'a'  = Alt and only Alt,  'ca' = Ctrl+Alt and only those two,
#   ''   = no modifiers at all.
#   Alt+Left       -> copy_left        "Copy current difference to the left"
#   Alt+Right      -> copy_right       "Copy current difference to the right"
#   Ctrl+Alt+Left  -> copy_line_left   "Copy current line to the left (at
#                                       the caret line's horizontal level)"
#   Ctrl+Alt+Right -> copy_line_right  "Copy current line to the right (at
#                                       the caret line's horizontal level)"
#   Alt+Down       -> jump_next        "Jump to next difference"
#   Alt+Up         -> jump_prev        "Jump to previous difference"
#   F5             -> refresh_compare  "Recompare"
# Hotkeys fire only with the EXACT modifier state and only inside the
# two halves of a compare tab this plugin manages; every other editor
# keeps its normal key behavior, user bindings included. Governed by
# the 'enable_keyboard_capture' setting (default on).
_HOTKEYS = {
    ct_keys.VK_LEFT:   (('copy_left', 'a'), ('copy_line_left', 'ca')),
    ct_keys.VK_RIGHT:  (('copy_right', 'a'), ('copy_line_right', 'ca')),
    ct_keys.VK_DOWN:   (('jump_next', 'a'),),
    ct_keys.VK_UP:     (('jump_prev', 'a'),),
    ct_keys.VK_F5:     (('refresh_compare', ''),),
}

# Key-code filter for the runtime on_key event subscription
# (PROC_EVENTS_SUB): the event fires only for these key codes, any
# modifiers -- the exact-modifier check happens in on_key. Derived from
# the map so a new hotkey can never be left out of the filter.
_HOTKEY_KEY_FILTER = ','.join(str(k) for k in sorted(_HOTKEYS))

# HARD-CODED MODULE CONSTANT — deliberately NOT a config option. Flips
# the whole-compare editor lock for background (native) compares:
#
#   True  (default): from kick-off until the result is fully rendered,
#          both compare-tab editors are
#            - EDACTION_LOCKed: the paint lock makes each half repaint
#              the 'busy' placeholder (hourglass) — the user-visible
#              "a compare is running" signal, for the whole engine run;
#            - PROP_RO: typing is blocked — EDACTION_LOCK alone does
#              NOT block input, the user could still write blind.
#          The lock/RO pair is released ONLY after the compare finished
#          AND everything is painted (or the compare was cancelled).
#
#   False: pre-lock behavior — the editors stay fully editable (and
#          paintable) for the whole compare: no kick-off lock, no
#          read-only. The paint phase runs unlocked too.
#
# The synchronous (Python-algorithm) mode never takes this lock: it
# runs inline on the main thread, so no keystroke can land mid-compare
# and the UI cannot repaint the busy screen anyway.
LOCK_EDITORS_WHILE_COMPARING = True

PLG_NAME = _('Differ')
METAJSONFILE = os.path.dirname(__file__) + os.sep + 'differ_opts.json'
JSONFILE = 'cuda_differ.json'  # To store in settings/cuda_differ.json
JSONPATH = ct.app_path(ct.APP_DIR_SETTINGS) + os.sep + JSONFILE

# Option metadata for the config dialog (Options Editor). Chapters ('chp')
# group the options in the dialog tree; the order below is the display order:
# theme -> algorithm -> advanced -> micromap. Every option name carries its
# category, so 'differ.X' became 'differ.<category>.X'. Note that
# cudax_lib.get_opt()/set_opt() only treat '/' as a nested-path separator --
# dotted names are stored as flat keys in settings/cuda_differ.json, so the
# category is purely a grouping/naming convention.
# --- diff_algorithm dropdown ------------------------------------------------
# Method 1 (used here): value/label pairs via 'str2s' + 'dct'.
#   The dropdown shows the second element of each tuple; on save, the keys
#   ('difflib'/'patience') are extracted, so the stored value is the raw
#   plain string. load_definitions auto-derives 'jdc' from 'dct', so 'jdc'
#   does not need to be set by hand. Use this when you want friendlier
#   dropdown labels than the raw config value.
#
# Method 2 (alternative, kept here as a reminder): plain string list via
# 'strs' + 'lst'. The combobox is populated straight from 'lst'; on save
# the raw string itself is stored. Minimal, no separate labels.
#
#     {'opt': 'differ.diff_algorithm',
#      'cmt': _('Diff algorithm to use. Patience anchors on unique matching '
#               'lines and often produces more human-readable diffs when '
#               'blocks of code are moved; difflib is Python\'s stdlib '
#               'SequenceMatcher. Default: patience.'),
#      'def': 'patience',
#      'frm': 'strs',
#      'lst': ['difflib', 'patience'],
#      'chp': 'config',
#      },
# ----------------------------------------------------------------------------
# Compare-color themes (option 'differ.theme.color_theme'):
#   auto   detect the light family of the current UI theme and use its
#          preset (the default)
#   white/black/grey   use that family's preset unconditionally
#   custom   use the six differ.theme.*_color options; empty slots are
#          filled from the auto-detected preset
# ----------------------------------------------------------------------------
# UI-theme name -> light family the preset colors are tuned for ('' is
# CudaText's built-in default theme). Names are normalized (lowercased)
# before the lookup -- see _ui_theme_name. 
_THEME_UI_TYPES = {
    '': 'grey',
    'amy': 'black',
    'cobalt': 'black',
    'darkwolf': 'black',
    'ebony': 'black',
    'green': 'grey',
    'navy': 'grey',
    'sub': 'black',
    'syn': 'white',
}

# Preset compare colors per family (hex strings; '#'rgb-e' format the
# color options use). A None entry means "the live editor background"
# (PROC_THEME_UI_DICT_GET's EdTextBg): ignored differences then keep
# blending into whatever theme is active -- the same trick empty
# ignored_*_color options always used. For calibration, the bundled
# themes' editor backgrounds per family:
#   white: syn           #FFFFFF
#   grey:  green, navy   #E0E0E0 (the default theme's grey is close)
#   black: ebony #202020, amy #200020, cobalt #002240,
#          darkwolf #293134, sub #272822
_COLOR_PRESETS = {
    'white': {
        'changed': '#f8dfad',
        'added': '#b3ffb3',
        'deleted': '#ffc4c4',
        'gap': '#e3e3e3',
        'ignored': '#ffffff',
        'ignored_gap': '#ffffff',
    },
    'grey': {
        # The white family's colors deepened ~25 units, so they hold up
        # against light-grey (#E0E0E0) editor backgrounds (on #E0E0E0 the
        # white preset's #e3e3e3 gap would be invisible).
        'changed': '#ebd493',
        'added': '#a2e3a2',
        'deleted': '#f4b6b6',
        'gap': '#cdcdcd',
        'ignored': None,
        'ignored_gap': None,
    },
    'black': {
        # Muted, desaturated blocks that don't glare on dark backgrounds.
        'changed': '#55482e',
        'added': '#2d5230',
        'deleted': '#5c3232',
        'gap': '#3d3d3d',
        'ignored': None,
        'ignored_gap': None,
    },
}

# Preset key -> config key (the one difference: 'gap' -> 'color_gaps').
_PRESET_CFG_KEYS = {
    'changed': 'color_changed',
    'added': 'color_added',
    'deleted': 'color_deleted',
    'gap': 'color_gaps',
    'ignored': 'color_ignored',
    'ignored_gap': 'color_ignored_gap',
}

# EdTextBg fallback per family, used only when the live lookup fails
# (missing key / API error) -- basically never in a running CudaText.
_PRESET_BG_FALLBACK = {
    'white': 0xFFFFFF,
    'grey': 0xE0E0E0,
    'black': 0x202020,
}

_COLOR_THEME_MODES = ('auto', 'white', 'black', 'grey', 'custom')


def _ed_text_bg():
    """Live editor text background color from the current UI theme, or
    None when it cannot be read."""
    try:
        ui = ct.app_proc(ct.PROC_THEME_UI_DICT_GET, '')
        return ui.get('EdTextBg', {}).get('color')
    except Exception:
        return None


def _ed_gutter_colors():
    """Live gutter colors (background, font) for the hunk edge columns,
    from the active CudaText theme.

    The EdGutter* keys are UI-theme keys (TAppThemeColor -- the
    .cuda-theme-ui files); PROC_THEME_UI_DICT_GET is the definitive
    and only source queried: it returns
    {'EdGutterBg': {'color': int}, 'EdGutterFont': {'color': int}, ...}.
    (PROC_THEME_SYNTAX_DICT_GET is deliberately NOT consulted: it only
    serves lexer styles -- TAppThemeStyle has no Ed* entries.)

    Fallbacks when the dict has no key (custom/minimal themes):
    background = the live editor text background, then the classic
    gutter grey #F0F0F0; font = a neutral grey that reads on both.
    Colors are BGR ints, ready for canvas_proc."""
    bg = None
    font = None
    try:
        ui = ct.app_proc(ct.PROC_THEME_UI_DICT_GET, '')
        if ui:
            bg = (ui.get('EdGutterBg') or {}).get('color')
            font = (ui.get('EdGutterFont') or {}).get('color')
    except Exception:
        pass
    if bg is None:
        bg = _ed_text_bg()
    if bg is None:
        bg = 0xF0F0F0
    if font is None:
        font = 0x808080
    return bg, font


def _color_luminance(color_int):
    """Relative luminance (0..1) of a 0xRRGGBB int, perceptual weights."""
    r = (color_int >> 16) & 0xFF
    g = (color_int >> 8) & 0xFF
    b = color_int & 0xFF
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0


def _ui_theme_name():
    """Current UI-theme name, normalized for the _THEME_UI_TYPES lookup
    (PROC_THEME_UI_GET returns the bare theme name, e.g. 'syn'; '' is the
    default theme -- custom themes come back as their plain names too)."""
    try:
        name = ct.app_proc(ct.PROC_THEME_UI_GET, '') or ''
    except Exception:
        name = ''
    return name.strip().lower()


def _detect_theme_type():
    """'white' | 'grey' | 'black' -- the light family of the current UI
    theme, i.e. which preset's colors fit the current editor background.

    Known UI themes carry a fixed family (CudaText's own dark/light
    split -- see _THEME_UI_TYPES). Unknown/custom themes fall back to
    the luminance of the live editor background; the thresholds put the
    bundled themes into the same family the table assigns them
    (#FFFFFF -> white, #E0E0E0 -> grey, #202020 and darker -> black)."""
    ttype = _THEME_UI_TYPES.get(_ui_theme_name())
    if ttype is not None:
        return ttype
    bg = _ed_text_bg()
    if bg is None:
        return 'grey'  # unreadable background -- middle-of-the-road pick
    lum = _color_luminance(bg)
    if lum >= 0.92:
        return 'white'
    if lum <= 0.35:
        return 'black'
    return 'grey'


def _preset_colors(theme_type):
    """The six compare colors (ints) of a preset family. None entries
    resolve to the live editor background, so ignored differences always
    blend into the active theme."""
    preset = _COLOR_PRESETS[theme_type]
    ed_bg = None
    out = {}
    for key, val in preset.items():
        if val is None:
            if ed_bg is None:
                ed_bg = _ed_text_bg()
                if ed_bg is None:
                    ed_bg = _PRESET_BG_FALLBACK[theme_type]
            out[key] = ed_bg
        else:
            out[key] = ctx.html_color_to_int(val)
    return out


# ----------------------------------------------------------------------------
OPTS_META = [
    # --- chapter "theme": colors used to paint the compare view ----------
    {'opt': 'differ.theme.color_theme',
     'cmt': _('Color theme\n'
              'Which compare colors to use. The six color options below '
              'only apply in the "Custom" mode.\n'
              '- Auto-detect -- detect the light family of the current UI '
              'theme (by theme name, luminance of the editor background as '
              'fallback) and use its preset. Recommended.\n'
              '- White / Grey / Black -- use that family\'s preset colors, '
              'tuned for white / light-grey / dark editor backgrounds.\n'
              '- Custom -- use the six color options below; each option '
              'left empty is filled from the auto-detected preset.\n'
              'Note: in the Grey and Black presets the ignored-difference '
              'colors always resolve to the live editor background, so '
              'ignored regions blend into the active theme.\n'
              'Default: Auto-detect.'),
     'def': 'auto',
     'frm': 'str2s',
     'dct': [('auto',   _('Auto-detect (recommended)')),
             ('white',  _('White themes preset')),
             ('grey',   _('Grey themes preset')),
             ('black',  _('Black themes preset')),
             ('custom', _('Custom (use the color options below)'))],
     'chp': 'theme',
     },
    {'opt': 'differ.theme.changed_color',
     'cmt': _('Color of changed lines\n'
              'Background color for lines that were modified (replaced with '
              'different content).\n'
              'Also colors the char-level highlights inside modified lines, '
              'the margin markers, the micromap highlights and the overview '
              'panel.\n'
              'Only used when "Color theme" is Custom; leave empty to fill '
              'this slot from the auto-detected preset.'),
     'def': '',
     'frm': '#rgb-e',
     'chp': 'theme',
     },
    {'opt': 'differ.theme.added_color',
     'cmt': _('Color of added lines\n'
              'Background color for lines that exist only in the right file '
              '(added).\n'
              'Also colors the char-level highlights inside added lines, the '
              'margin markers, the micromap highlights and the overview '
              'panel.\n'
              'Only used when "Color theme" is Custom; leave empty to fill '
              'this slot from the auto-detected preset.'),
     'def': '',
     'frm': '#rgb-e',
     'chp': 'theme',
     },
    {'opt': 'differ.theme.deleted_color',
     'cmt': _('Color of deleted lines\n'
              'Background color for lines that exist only in the left file '
              '(removed).\n'
              'Also colors the char-level highlights inside deleted lines, '
              'the margin markers, the micromap highlights and the overview '
              'panel.\n'
              'Only used when "Color theme" is Custom; leave empty to fill '
              'this slot from the auto-detected preset.'),
     'def': '',
     'frm': '#rgb-e',
     'chp': 'theme',
     },
    {'opt': 'differ.theme.gap_color',
     'cmt': _('Color of inter-line gap background\n'
              'Background color for the blank gap inserted to keep the two '
              'sides visually aligned when one side has fewer lines.\n'
              'Also colors the gap rectangles in the overview panel.\n'
              'Only used when "Color theme" is Custom; leave empty to fill '
              'this slot from the auto-detected preset.'),
     'def': '',
     'frm': '#rgb-e',
     'chp': 'theme',
     },
    {'opt': 'differ.theme.ignored_color',
     'cmt': _('Color of ignored differences\n'
              'Background color for lines whose difference is suppressed '
              'by the "Ignore blank lines" option (WinMerge-style '
              'ignored differences). Also colors the micromap highlights '
              'and the overview panel.\n'
              'Only used when "Color theme" is Custom; leave empty to fill '
              'this slot from the auto-detected preset -- which for the '
              'Grey and Black presets is the editor text background '
              'color, so the ignored region then looks like normal text.'),
     'def': '',
     'frm': '#rgb-e',
     'chp': 'theme',
     },
    {'opt': 'differ.theme.ignored_gap_color',
     'cmt': _('Color of ignored difference gaps\n'
              'Background color for the compensating inter-line gap '
              'inserted next to a suppressed blank-line difference '
              '("Ignore blank lines" option), so the two sides stay '
              'aligned. Separate from "Color of ignored differences" '
              '(the lines) and from "Color of inter-line gap background" '
              '(regular alignment gaps); also colors the ignored-gap '
              'rectangles in the overview panel.\n'
              'Only used when "Color theme" is Custom; leave empty to fill '
              'this slot from the auto-detected preset -- which for the '
              'Grey and Black presets is the editor text background '
              'color, so the ignored gap then looks like empty space.'),
     'def': '',
     'frm': '#rgb-e',
     'chp': 'theme',
     },
    # --- chapter "algorithm": which diff engine runs and how the result
    # is rendered ----------------------------------------------------------
    {'opt': 'differ.algorithm.diff_algorithm',
     'cmt': _('Diff algorithm\n'
              'Selects the diff algorithm used by the side-by-side compare.\n'
              '\n'
              'Native algorithms run in compiled Pascal code and are 10-30x '
              'faster than the pure-Python implementations on large files. '
              'They require a CudaText build that includes the diff_proc '
              'API; if it is not available, they silently fall back to the '
              'closest Python equivalent (native_histogram -> hybrid, '
              'native_myers -> myers).\n'
              '- native_histogram -- Native Histogram diff (port of JGit\'s '
              'Histogram Diff, with JGit\'s Myers O(ND) Diff as internal '
              'fallback for sub-regions -- the same algorithm git uses for '
              '"git diff --histogram"). Behaves like Patience diff when '
              'unique common lines exist, with graceful fallback when they '
              'don\'t. Fast and high-quality (more human-readable in some '
              'cases).\n'
              'This is the default and recommended option for regular '
              'files.\n'
              '- native_myers -- Native Myers diff (port of WinMerge\'s '
              'bundled GNU diffutils Myers O(ND), the same algorithm git '
              'uses for "git diff --myers"), the fastest on large/very '
              'different files. It is faster because it builds on top of '
              'Myers with additions from diffutils and WinMerge that JGit '
              'lacks, such as Paul Eggert\'s TOO_EXPENSIVE heuristic, '
              'line-purging heuristics like DiscardConfusingLines, and '
              'other optimizations.\n'
              '\n'
              'The other algorithms are pure-Python so they may be slower '
              'with very big files:\n'
              '- hybrid -- Pure-Python Hybrid, combines Patience (anchoring '
              'on unique lines) with Myers O(NP) for the gaps. Best '
              'pure-Python quality (more human-readable in some cases).\n'
              '- myers -- Pure-Python Myers O(NP) (Wu/Manber/Myers/Miller), '
              'ported from Meld.\n'
              '- vscode -- Pure-Python VS Code diff algorithm. This is a '
              'port of Microsoft VS Code\'s diff implementation, which uses '
              'dynamic programming with equality scoring (best alignment on '
              'files with many duplicated lines, more human-readable in some '
              'cases, but this is the slowest).\n'
              '- patience -- Pure-Python Patience diff (via the embedded '
              'patiencediff library), anchors on unique matching lines, more '
              'human-readable in some cases. Bad alignment on files with '
              'many duplicated lines like log files.\n'
              '- difflib -- Python\'s standard difflib SequenceMatcher with '
              'autojunk=False.\n'
              '\n'
              'If some parts of a diff are hard to read, try testing a '
              'different algorithm: Histogram, Hybrid, Patience, or VSCode '
              'tend to generate much cleaner results.\n'
              'If you\'re comparing massive files and need maximum speed, '
              'stick with the Native Myers algorithm instead.\n'
              'See the "Diff algorithms and best practices" section in '
              'readme.txt for recommended option combinations.\n'
              'Default: native_histogram.'),
     'def': 'native_histogram',
     'frm': 'str2s',
     'dct': [('native_histogram', _('Native Histogram (More human-readable, recommended)')),
             ('native_myers',     _('Native Myers O(ND) (Fastest on large/different files)')),
             ('hybrid',           _('Hybrid (Python: Patience + Myers O(NP))')),
             ('myers',            _('Myers O(NP) (Python)')),
             ('vscode',           _('VSCode (Python: DP + Myers O(ND))')),
             ('patience',         _('Patience Diff (Python)')),
             ('difflib',          _('Python difflib stdlib'))],
     'chp': 'algorithm',
     },
    {'opt': 'differ.algorithm.compare_with_details',
     'cmt': _('Detailed comparison\n'
              'When enabled, modified lines are compared character-by-character, '
              'highlighting specific differences within the line. This uses a ported '
              'implementation of WinMerge\'s character-diff engine, combining Myers O(NP) '
              'with custom heuristics and optimizations.\n'
              'When disabled, modified lines are highlighted as a whole.\n'
              'Disabling this speeds up the compare of big files.\n'
              'Default: on.'),
     'def': True,
     'frm': 'bool',
     'chp': 'algorithm',
     },
    {'opt': 'differ.algorithm.beautify_alignment',
     'cmt': _('Improve line alignment\n'
              'Beautify line alignment inside REPLACE blocks where the two '
              'sides have DIFFERENT line counts.\n'
              '- When OFF (algo-faithful): lines are paired top-down by '
              'position for the first min(da, db) lines, and leftover lines '
              'on the longer side are shown as plain added/deleted lines '
              'against a gap at the bottom of the shorter side. Nothing is '
              're-paired or re-ordered.\n'
              'This renders exactly the way the algorithm dictates; for '
              'example if native_myers is used it renders the way WinMerge / '
              'GNU diffutils side-by-side (sdiff) output does.\n'
              '- When ON (VS Code-like): the engine\'s hunks are re-paired '
              'by similarity -- finds best pairs anchored on the longest '
              'unique exact match or the best prefix/suffix-similar pair, '
              'char-diffs them, and recurses on both sides. Lines with < 3 '
              'chars of similarity are shown as separate delete+add. This '
              're-arranges the engine\'s output for a more "aligned" look '
              'but is no longer a faithful rendering of the diff.\n'
              'This results in a more human-readable diff in some cases, '
              'but the compare becomes slower with very big files.\n'
              'Applies to both native and Python algorithms.\n'
              'Equal-count REPLACE blocks (da == db) are positional in BOTH '
              'modes, so this option only affects unequal-count REPLACE '
              'blocks.\n'
              'Default: on.'),
     'def': True,
     'frm': 'bool',
     'chp': 'algorithm',
     },
    # --- chapter "ignoreopt": comparison ignore options (the diff_proc
    # DIFF_IGN_* flags of the native engines; also exposed as checkable
    # items in the diff-tab right-click context menu, below 'Recompare') ---
    {'opt': 'differ.ignoreopt.ignore_case',
     'cmt': _('Ignore case\n'
              'Case-insensitive comparison for the native diff algorithms '
              '(Native Histogram / Native Myers).\n'
              'Lines that differ only in ASCII letter case (A-Z vs a-z) '
              'are shown as equal, and case-only changes are not '
              'highlighted in the char-level details inside modified '
              'lines. Non-ASCII text is compared as-is (no full Unicode '
              'case folding).\n'
              'Can also be toggled from the compare-tab right-click context '
              'menu (checkable item below "Recompare").\n'
              'Not supported by the pure-Python algorithms -- '
              'they compare strictly, so the option is hidden (config '
              'dialog and compare-tab context menu) while a Python '
              'algorithm is selected.\n'
              'Default: off.'),
     'def': False,
     'frm': 'bool',
     'chp': 'ignoreopt',
     },
    {'opt': 'differ.ignoreopt.ignore_whitespace',
     'cmt': _('Ignore whitespace\n'
              'All whitespace ignored by the native diff algorithms '
              '(Native Histogram / Native Myers).\n'
              'Spaces and tabs are skipped wherever they appear in a '
              'line -- leading, interior and trailing -- so "abc def" '
              'compares equal to "abcdef".\n'
              'Can also be toggled from the compare-tab right-click context '
              'menu (checkable item below "Recompare").\n'
              'Not supported by the pure-Python algorithms -- '
              'they compare strictly, so the option is hidden (config '
              'dialog and compare-tab context menu) while a Python '
              'algorithm is selected.\n'
              'Default: off.'),
     'def': False,
     'frm': 'bool',
     'chp': 'ignoreopt',
     },
    {'opt': 'differ.ignoreopt.ignore_blank_lines',
     'cmt': _('Ignore blank lines\n'
              'Changes that only insert or delete blank lines are ignored '
              'by the native diff algorithms (Native Histogram / Native '
              'Myers) -- like WinMerge\'s "Ignore blank lines" and GNU '
              'diff\'s -B option.\n'
              'A hunk is ignored when ALL of its lines are blank on both '
              'sides. A line is blank when it is empty, or when "Ignore '
              'whitespace" is also enabled and it contains only spaces '
              'and tabs.\n'
              'Ignored regions keep the two sides aligned, WinMerge-style: '
              'a small compensating gap fills in for the missing lines. '
              'By default both the ignored lines and the ignored gap use '
              'the editor text background color (an ignored region looks '
              'like normal text) -- see "Color of ignored differences" '
              '(the lines) and "Color of ignored difference gaps" (the '
              'gap) to make them visible. They are '
              'NOT counted as differences: no bookmarks, skipped by '
              'Next/Previous Difference, and a file differing only in '
              'blank lines reports "No differences found".\n'
              'Can also be toggled from the compare-tab right-click context '
              'menu (checkable item below "Recompare").\n'
              'Not supported by the pure-Python algorithms -- '
              'they compare strictly, so the option is hidden (config '
              'dialog and compare-tab context menu) while a Python '
              'algorithm is selected.\n'
              'Default: off.'),
     'def': False,
     'frm': 'bool',
     'chp': 'ignoreopt',
     },
    {'opt': 'differ.ignoreopt.ignore_eol',
     'cmt': _('Ignore line endings\n'
              'CR/LF line-ending differences are ignored by the native '
              'diff algorithms (Native Histogram / Native Myers).\n'
              'CRLF vs LF vs CR line endings compare as equal, so a file '
              're-saved with different line endings shows no differences. '
              'End-of-line tokens also compare equal in the char-level '
              'details inside modified lines.\n'
              'Can also be toggled from the compare-tab right-click context '
              'menu (checkable item below "Recompare").\n'
              'Not supported by the pure-Python algorithms -- '
              'they compare strictly, so the option is hidden (config '
              'dialog and compare-tab context menu) while a Python '
              'algorithm is selected.\n'
              'Default: off.'),
     'def': False,
     'frm': 'bool',
     'chp': 'ignoreopt',
     },
    {'opt': 'differ.ignoreopt.ignore_numbers',
     'cmt': _('Ignore numbers\n'
              'Digit runs are treated as equal by the native diff '
              'algorithms (Native Histogram / Native Myers) -- useful '
              'for comparing logs with timestamps, counters or version '
              'numbers.\n'
              'Only ASCII digits 0-9 count; non-ASCII digits are not '
              'affected. "12:34:56.789" matches "12:34:56.790". In the '
              'char-level details inside modified lines, number-only '
              'changes are not highlighted.\n'
              'Can also be toggled from the compare-tab right-click context '
              'menu (checkable item below "Recompare").\n'
              'Not supported by the pure-Python algorithms -- '
              'they compare strictly, so the option is hidden (config '
              'dialog and compare-tab context menu) while a Python '
              'algorithm is selected.\n'
              'Default: off.'),
     'def': False,
     'frm': 'bool',
     'chp': 'ignoreopt',
     },
    # --- chapter "advanced": behavior tweaks and debugging tools ----------
    {'opt': 'differ.advanced.sync_scroll',
     'cmt': _('Synchronized scrolling\n'
              'When enabled, scrolling one side of the compare view also '
              'scrolls the other side, both vertically and horizontally.\n'
              'Default: on.'),
     'def': True,
     'frm': 'bool',
     'chp': 'advanced',
     },
    {'opt': 'differ.advanced.enable_sync_caret',
     'cmt': _('Keep carets visible on sync\n'
              'When enabled, moving the cursor in one side also moves the '
              'cursor in the other side to the corresponding difference '
              'block, so both carets stay visible in the current screen '
              'area.\n'
              'Default: off.'),
     'def': False,
     'frm': 'bool',
     'chp': 'advanced',
     },
    {'opt': 'differ.advanced.enable_auto_refresh',
     'cmt': _('Auto-refresh after changes\n'
              'When enabled, the diff markers are automatically re-calculated '
              'after you stop editing for 1-2 seconds. When disabled, you '
              'must use the Recompare command manually.\n'
              'Default: off.'),
     'def': False,
     'frm': 'bool',
     'chp': 'advanced',
     },
    {'opt': 'differ.advanced.enable_keyboard_capture',
     'cmt': _('Keyboard shortcuts in compare tabs\n'
              'When enabled, the plugin captures these keys inside compare '
              'tabs: Alt+Left/Alt+Right copy the current difference to the '
              'other side, Alt+Down/Alt+Up jump to the next/previous '
              'difference, F5 recompares (runs the Recompare command). '
              'In all other tabs the '
              'keys keep their normal behavior. The plugin subscribes/ '
              'unsubscribes to the key events at runtime, so changing this '
              'option takes effect at once.\n'
              'Default: on.'),
     'def': True,
     'frm': 'bool',
     'chp': 'advanced',
     },
    {'opt': 'differ.advanced.enable_hunk_edges',
     'cmt': _('Hunk edge columns\n'
              'When enabled, two narrow columns are added at the left '
              'edge of EACH editor of the compare view -- one next to '
              'the left editor, one directly right of the splitter, '
              'next to the right editor -- each drawing a bracket '
              'around every difference block (hunk):\n'
              '  +----\n'
              '  |\n'
              '  +----\n'
              'The bracket spans the hunk\'s full visual extent -- the '
              'text lines AND the compensating gap band inserted inside '
              'the hunk -- so you always see exactly what Alt+Left/'
              'Alt+Right will move, like the rule lines Beyond Compare '
              'draws around its difference blocks. One-sided differences '
              '(a colored gap on one side) get the bracket around the '
              'gap too.\n'
              'The columns are separate controls outside the editors '
              '(the left column takes a strip at the panel\'s left '
              'edge, and the split bar between the editors is widened '
              'by the column width to host the right column -- so the '
              'text areas are not touched and nothing is added to the '
              'editors\' heights, and the splitter drag keeps working '
              'as usual), use the theme\'s gutter colors '
              '(EdGutterBg / EdGutterFont), stay pixel-aligned with the '
              'text rows while scrolling (also with word wrap and the '
              'Beautify line alignment option), and keep their position '
              'when the window is resized or the splitter is dragged '
              '(the split bar carries its column along; a background '
              'layout guard repaints and cleans up). Only visible '
              'hunks are painted, so the cost stays small on huge '
              'files.\n'
              'Takes effect on the next Recompare (F5).\n'
              'Default: on.'),
     'def': True,
     'frm': 'bool',
     'chp': 'advanced',
     },
    {'opt': 'differ.advanced.hunk_edges_width',
     'cmt': _('Hunk edge column width in pixels\n'
              'Width of one hunk edge column (see "Hunk edge '
              'columns"), in pixels. The left editor gives up this many '
              'pixels of width for each of the two columns (its own '
              'left strip plus the split bar\'s widening). The columns '
              'are intentionally narrow -- a vertical bar plus short '
              'top/bottom arms.\n'
              'Range: 6-40. Default: 12.'),
     'def': 12,
     'frm': 'int',
     'chp': 'advanced',
     },
    {'opt': 'differ.advanced.diff_context',
     'cmt': _('Context lines in unified diff\n'
              'Number of unchanged context lines shown around each change in '
              'the unified diff output (produced by the "Diff current '
              'document with..." commands).\n'
              'Default: 3.'),
     'def': 3,
     'frm': 'int',
     'chp': 'advanced',
     },
    {'opt': 'differ.advanced.enable_profiling',
     'cmt': _('Enable profiling\n'
              'Enable profiling to trace where compare time is consumed.\n'
              'When enabled, prints a detailed timing report to the console '
              'after each compare, breaking down time spent in the diff '
              'algorithm, opcode realignment, event generation, char-level '
              'diffing (native vs Python), and UI painting (bookmarks, '
              'decor, gaps, attributes). Use for debugging performance '
              'issues only -- adds small overhead (~1-2us per timing '
              'point).\n'
              'Default: off.'),
     'def': False,
     'frm': 'bool',
     'chp': 'advanced',
     },
    # --- chapter "micromap": mini-map style helpers (overview panel and
    # built-in micromap) ----------------------------------------------------
    {'opt': 'differ.micromap.enable_micromap',
     'cmt': _('Enable built-in micromap\n'
              'When enabled, switches on CudaText\'s native micromap '
              '(mini-map) column in both halves of the compare split, with '
              'diff-colored line highlights.\n'
              'The micromap is fast but does NOT account for the inter-line '
              'gaps Differ inserts for visual alignment, so it may drift '
              'out of sync with the text when gaps are present -- for a '
              'gap-aware alternative, enable '
              'differ.micromap.enable_overview instead (or both).\n'
              'Default: off.'),
     'def': False,
     'frm': 'bool',
     'chp': 'micromap',
     },
    {'opt': 'differ.micromap.enable_overview',
     'cmt': _('Enable gap-aware overview panel\n'
              'When enabled, adds a micromap alternative docked to the right '
              'side of the compare view: a miniature of both editors '
              'side-by-side with colored rectangles for deleted (red), added '
              '(green) and changed (yellow) lines, gray rectangles for gaps '
              'and white for unchanged lines.\n'
              'Unlike the built-in micromap, the overview accounts for the '
              'inter-line gaps inserted for visual alignment, so it stays in '
              'sync with what you actually see (it works like the micromap '
              'but is slower).\n'
              'Can be used together with the built-in micromap.\n'
              'Default: on.'),
     'def': True,
     'frm': 'bool',
     'chp': 'micromap',
     },
    {'opt': 'differ.micromap.enable_overview_slider_opacity',
     'cmt': _('Enable overview slider transparency\n'
              'When enabled, the overview panel\'s slider is rendered with '
              'simulated alpha blending so the colored diff lines remain '
              'visible through the slider, like in WinMerge. When disabled, '
              'the slider uses a fast opaque solid fill.\n'
              'Only has an effect when differ.micromap.enable_overview is '
              'on.\n'
              'Default: on.'),
     'def': True,
     'frm': 'bool',
     'chp': 'micromap',
     },
    {'opt': 'differ.micromap.overview_slider_opacity',
     'cmt': _('Overview slider opacity in percent\n'
              'Opacity of the overview panel slider.\n'
              'Only used when enable_overview_slider_opacity is on.\n'
              'Range: 0-100. Default: 40.'),
     'def': 40,
     'frm': 'int',
     'chp': 'micromap',
     },
]

DIFF_TAB_COUNT = 1
# Persistent state file: stores compare-tab state grouped by session.
# session_key is the session file path, relative to the settings folder if
# the session is inside it (at any depth), or the full path if outside.
STATE_FILE = os.path.join(ct.app_path(ct.APP_DIR_SETTINGS), 'cuda_differ_state.json')
# Path to plugins.ini -- used to persistently subscribe to on_start2 so the
# plugin auto-loads on next CudaText startup when compare tabs are active.
PLUGINS_INI = os.path.join(ct.app_path(ct.APP_DIR_SETTINGS), 'plugins.ini')
PLUGINS_INI_SECTION = 'events'
MODULE_NAME = __name__.split('.')[-1]  # e.g. 'cuda_differ'


_homedir = os.path.expanduser('~')


def collapse_filename(fn):
    """Shorten a filename by replacing the home directory with '~'."""
    if (fn+'/').startswith(_homedir+'/'):
        fn = fn.replace(_homedir, '~', 1)
    return fn


def get_opt(key, def_val: tp.Any = ''):
    """Read a 'differ.*' option from the plugin's JSON settings file."""
    return ctx.get_opt('differ.' + key, def_val, user_json=JSONFILE)


def set_opt(key, val):
    """Write a 'differ.*' option to the plugin's JSON settings file
    (settings/cuda_differ.json). Mirrors get_opt above; cudax_lib's
    set_opt does the comment-preserving line-based update, so hand-made
    comments in the JSON survive."""
    return ctx.set_opt('differ.' + key, val, user_json=JSONFILE)


# Ignore options exposed as checkable items in the diff-tab right-click
# context menu (below 'Recompare' -- see tabmenu_init) and in the config
# dialog (chapter 'ignoreopt' -- see OPTS_META). Order = context-menu
# display order.
# Each entry: (config key suffix under 'ignoreopt.', menu caption).
# The values feed differ_native.build_ignore_flags() which builds the
# diff_proc DIFF_IGN_* bitmask for the native algorithms.
_IGNORE_OPTS = (
    ('ignore_case',        _('Ignore case')),
    ('ignore_whitespace',  _('Ignore whitespace')),
    ('ignore_blank_lines', _('Ignore blank lines')),
    ('ignore_eol',         _('Ignore line endings')),
    ('ignore_numbers',     _('Ignore numbers')),
)

def msg(s, level=0):
    """Print a plugin message to the console. level: 0=info, 1=warning, 2=error."""
    if level == 0:
        print(PLG_NAME + ':', s)
    elif level == 1:
        print(PLG_NAME + _(' WARNING:'), s)
    elif level == 2:
        print(PLG_NAME + _(' ERROR:'), s)


# Migration map from the old flat option names to the new categorized names.
# Applied once at plugin import so settings saved by older plugin versions
# survive the rename (see _migrate_old_option_names).
_OLD_OPT_NAMES = {
    # theme
    'differ.changed_color': 'differ.theme.changed_color',
    'differ.added_color': 'differ.theme.added_color',
    'differ.deleted_color': 'differ.theme.deleted_color',
    'differ.gap_color': 'differ.theme.gap_color',
    # algorithm
    'differ.diff_algorithm': 'differ.algorithm.diff_algorithm',
    'differ.compare_with_details': 'differ.algorithm.compare_with_details',
    'differ.beautify_alignment': 'differ.algorithm.beautify_alignment',
    # advanced
    'differ.sync_scroll': 'differ.advanced.sync_scroll',
    'differ.enable_sync_caret': 'differ.advanced.enable_sync_caret',
    'differ.enable_auto_refresh': 'differ.advanced.enable_auto_refresh',
    'differ.diff_context': 'differ.advanced.diff_context',
    'differ.enable_profiling': 'differ.advanced.enable_profiling',
    # micromap
    'differ.enable_micromap': 'differ.micromap.enable_micromap',
    'differ.enable_overview': 'differ.micromap.enable_overview',
    'differ.enable_overview_slider_opacity': 'differ.micromap.enable_overview_slider_opacity',
    'differ.overview_slider_opacity': 'differ.micromap.overview_slider_opacity',
}


def _migrate_old_option_names():
    """One-time migration of option names to the categorized scheme.

    Older plugin versions stored options under flat names like
    'differ.sync_scroll'. The current version groups options into
    categories ('theme', 'algorithm', 'advanced', 'micromap'), so the
    names became 'differ.<category>.<name>'. This renames the old flat
    keys inside settings/cuda_differ.json so existing user settings
    survive the plugin update.

    The rename is line-based (same approach cudax_lib.set_opt uses for
    simple keys), so comments and formatting in the JSON file are
    preserved. An old key is only renamed when the new key is not
    already present, so re-running the migration is safe (idempotent).
    """
    if not os.path.exists(JSONPATH):
        return
    try:
        with open(JSONPATH, 'r', encoding='utf8') as f:
            body = f.read()
    except OSError:
        return
    changed = 0
    for old, new in _OLD_OPT_NAMES.items():
        # Match real key lines only ('^  "differ.sync_scroll":'); commented
        # out lines (// "differ.sync_scroll": ...) do not match because of
        # the '^\s*"' anchor.
        cre_old = re.compile(r'(?m)^(\s*)"%s"(\s*:)' % re.escape(old))
        cre_new = re.compile(r'(?m)^\s*"%s"\s*:' % re.escape(new))
        if cre_old.search(body) and not cre_new.search(body):
            body = cre_old.sub(r'\1"%s"\2' % new, body)
            changed += 1
    if not changed:
        return
    try:
        with open(JSONPATH, 'w', encoding='utf8') as f:
            f.write(body)
        msg('migrated {} old option name(s) to the categorized scheme '
            '(differ.<category>.<name>)'.format(changed))
    except OSError as ex:
        msg('failed to migrate old option names: {}'.format(ex), level=1)


def _migrate_color_theme_choice():
    """One-time migration for the color_theme option.

    Older versions had no color_theme: the six differ.theme.*_color
    options ALWAYS overrode the theme-derived defaults. The new default
    'auto' ignores them (preset colors win), so a user who configured
    custom compare colors would silently lose them after the update.
    Detect that case -- any *_color option set to a non-empty value
    while color_theme is not yet chosen -- and switch that user to
    'custom' mode, which restores the old colors-take-precedence
    behavior (empty slots still fill from the detected preset).
    """
    if not os.path.exists(JSONPATH):
        return
    try:
        with open(JSONPATH, 'r', encoding='utf8') as f:
            body = f.read()
    except OSError:
        return
    if re.search(r'(?m)^\s*"differ\.theme\.color_theme"\s*:', body):
        return  # already chose a mode -- leave the choice alone
    cre_color = re.compile(
        r'(?m)^\s*"differ\.theme\.(?:changed|added|deleted|gap|ignored|ignored_gap)_color"'
        r'\s*:\s*"([^"]*)"')
    if not any(val for val in cre_color.findall(body)):
        return  # no configured colors -- the 'auto' default is right
    set_opt('theme.color_theme', 'custom')
    msg('color theme set to "custom" to keep your configured compare colors')


_migrate_old_option_names()
_migrate_color_theme_choice()


class _CompareJob:
    """Context for one compare of one compare tab.

    refresh_compare fills the job while setting the compare up. With the
    native algorithms the line-level diff runs in the engine's
    background thread: refresh_compare returns after starting it, and the
    engine's completion callback (_on_native_diff_done, marshalled to
    the main thread) uses the job to finish the compare -- paint the
    events, set the bookmarks, repaint the overview. With the Python
    algorithms the paint phase runs inline in refresh_compare and the job
    is just a carrier for the same data.

    The snapshot fields (a_text/b_text, lines_a/lines_b) are the texts
    the engine was kicked off with. While a background compare runs,
    LOCK_EDITORS_WHILE_COMPARING keeps both halves locked + read-only,
    so the live editors cannot drift from these snapshots; a refresh
    that arrives anyway is dropped (see refresh_compare), never queued.

    'editor_lock' carries the whole-compare editor lock state while
    LOCK_EDITORS_WHILE_COMPARING is on and a background compare is
    running: None while nothing is held, otherwise a dict
    {'a': original_ro, 'b': original_ro} recording per half the
    PROP_RO value the lock replaced, so _release_compare_editors can
    restore exactly that (and only once -- the release is idempotent).
    """

    __slots__ = (
        'ed',               # editor that triggered the refresh
        'session',          # the compare tab's _TabSession (owner)
        'tab_id',           # PROP_TAB_ID of the compare tab
        'tab_id_str',       # str(tab_id) -- dict key
        'a_ed', 'b_ed',     # the two split halves
        'a_text', 'b_text',         # native: raw text snapshots
        'lines_a', 'lines_b',       # python: line lists
        'overview',         # PaintboxOverview or None
        'columns',          # HunkColumns or None
        'micromap_on', 'wrap_on',
        'wrap_counts_a', 'wrap_counts_b',
        'line_h_a', 'line_h_b',
        'color_gaps', 'color_ignored', 'color_ignored_gap',
        'show_dialog',
        'compare_start',            # kick-off perf_counter()
        'profiling_enabled_here',   # profiling enabled by this compare
        'profiler_async_token',     # start_async_pair token (engine wait)
        'job_handle',       # engine job handle for the background compare (0 = none)
        'editor_lock',      # whole-compare lock/RO state (see class docstring)
        'stale',            # job dropped (tab closed / app exiting)
        'in_flight',        # background engine call was started
    )

    def __init__(self):
        self.ed = None
        self.session = None
        self.tab_id = None
        self.tab_id_str = ''
        self.a_ed = None
        self.b_ed = None
        self.a_text = None
        self.b_text = None
        self.lines_a = None
        self.lines_b = None
        self.overview = None
        self.columns = None
        self.micromap_on = False
        self.wrap_on = False
        self.wrap_counts_a = None
        self.wrap_counts_b = None
        self.line_h_a = 0
        self.line_h_b = 0
        self.color_gaps = None
        self.color_ignored = None
        self.color_ignored_gap = None
        self.show_dialog = False
        self.compare_start = 0.0
        self.profiling_enabled_here = False
        self.profiler_async_token = None
        self.job_handle = 0
        self.editor_lock = None
        self.stale = False
        self.in_flight = False


class _TabSession:
    """Standalone per-tab session: EVERY piece of runtime state that
    belongs to one compare tab lives here, and nothing is shared between
    diff tabs.

    Before the session refactor the plugin kept ONE global Differ (with
    its .diffmap of line-index tuples) on the Command object, so the
    diff records of whichever tab compared LAST were served to ALL
    tabs -- in tab A, Ctrl+Alt+Left picked hunks from tab B's compare
    (line numbers pointed at the wrong lines), and when the caret
    matched none of the foreign hunks the hunk/copy/jump commands
    silently did nothing ("shortcuts stop working after switching
    tabs"). The diffmap, the Differ, the overview panel, the in-flight
    compare job, the on_change suppression counter and the
    saved/dirty caches are per-tab by definition; they now live on this
    object, keyed by str(PROP_TAB_ID) in Command._sessions.

    Fields:
      tab_id           int PROP_TAB_ID of the compare tab
      tab_id_str       str(tab_id) -- the _sessions dict key
      state_key        key of the PERSISTED session group (session file)
                       this tab was registered under -- carried per tab
                       so a diff tab keeps writing to its own persisted
                       group even after the CudaText session switches
      diff             this tab's Differ (diffmap lives on it); None
                       until the first compare needs it (restored-after-
                       restart tabs defer creation -- no status spam at
                       startup), created by _session_diff()
      overview         PaintboxOverview docked to this tab, or None
      columns          HunkColumns (the two hunk edge columns) attached
                       to this tab's split view, or None
      panels_sig       signature of the side-panel layout the client
                       widths were last equalized for -- (hunk edges on,
                       hunk edge width, overview on, micromap on,
                       columns attachment generation). A refresh
                       re-equalizes the split position only when this
                       changes, so a deliberately dragged splitter
                       survives F5; None = never equalized
      job              in-flight background _CompareJob, or None
      overview_timer   True while the trailing 150ms overview repaint
                       timer is armed for this tab
      suppress_change  remaining on_change events to swallow (the two
                       spurious events set_text_all fires when a compare
                       tab is created)
      saved            cached 'no half dirty' flag (persisted 'saved')
      dirty            cached set of halves with unsaved edits
    """

    __slots__ = (
        'tab_id', 'tab_id_str', 'state_key',
        'diff', 'overview', 'columns', 'panels_sig', 'job',
        'overview_timer', 'suppress_change',
        'saved', 'dirty',
    )

    def __init__(self, tab_id, state_key=''):
        self.tab_id = tab_id
        self.tab_id_str = str(tab_id)
        self.state_key = state_key
        self.diff = None
        self.overview = None
        self.columns = None
        self.panels_sig = None
        self.job = None
        self.overview_timer = False
        self.suppress_change = 0
        self.saved = True
        self.dirty = set()


class Command:
    def __init__(self):
        self.scroll = ScrollSplittedTab(__name__)
        self.cfg = self.get_config()
        # Subscribe to the on_key event when keyboard capture is enabled
        # (runtime subscription -- install.inf no longer lists on_key).
        self._sync_on_key_subscription()
        # Set to True by on_exit_pre when CudaText is about to exit, so that
        # on_close (which fires next, once per closing tab) can skip
        # temp-file deletion and let compare tabs persist across restarts.
        self._app_exiting = False
        # THE session registry: one _TabSession per compare tab, keyed by
        # str(PROP_TAB_ID). Each diff tab is a standalone world -- its
        # Differ/diffmap, overview panel, in-flight job, timers and
        # saved/dirty caches all live on the session object, never on the
        # Command. _is_compare_tab is now exactly "has a session".
        self._sessions = {}
        # Cached key for the current session (relative path if inside
        # settings folder, full path otherwise). Set in on_start2 and
        # set_files; individual sessions remember their OWN key.
        self._current_session_key = ''

        self.compare_menu = None
        self.menuid_sep = None
        self.menuid_withfile = None
        self.menuid_withtab = None

    def _session_key(self, session_path):
        """Convert a session file path to a state-file key. If the session
        is inside the CudaText settings folder (at any depth), use a path
        relative to settings -- this makes the state file portable. If the
        session is outside settings, use the full path."""
        if not session_path:
            return ''
        settings_dir = ct.app_path(ct.APP_DIR_SETTINGS)
        if settings_dir:
            try:
                rel = os.path.relpath(session_path, settings_dir)
                # If rel doesn't start with '..', session is inside settings.
                if not rel.startswith('..' + os.sep) and rel != '..':
                    return rel
            except ValueError:
                pass
        return session_path

    def _load_state(self):
        """Load the persisted state from disk."""
        try:
            with open(STATE_FILE, 'r', encoding='utf8') as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {'sessions': {}}
        if not isinstance(data, dict):
            return {'sessions': {}}
        if not isinstance(data.get('sessions'), dict):
            data['sessions'] = {}
        return data

    def _save_state(self, state):
        """Save the state to disk."""
        try:
            with open(STATE_FILE, 'w', encoding='utf8') as f:
                json.dump(state, f, indent=2)
        except OSError as ex:
            msg('failed to save state file: {}'.format(ex), level=2)

    def _new_session(self, tab_id, state_key=''):
        """Create and register the standalone session for a compare tab
        (or return the existing one). All per-tab runtime state hangs off
        the returned _TabSession; the Command object stays stateless with
        respect to individual compares."""
        key = str(tab_id)
        session = self._sessions.get(key)
        if session is not None:
            return session
        session = _TabSession(tab_id, state_key or self._current_session_key)
        self._sessions[key] = session
        return session

    def _session_for(self, tab_id):
        """The _TabSession of a compare tab (tab_id int or str), or None
        when the tab is not a compare tab this plugin manages. This is
        the single routing lookup every event handler and command uses
        to reach the right tab's world."""
        if tab_id is None:
            return None
        return self._sessions.get(str(tab_id))

    def _session_diff(self, session):
        """The session's Differ, created on demand. Restored-after-restart
        tabs carry diff=None until their first compare (no 'Using ... Algo'
        status spam at startup); the first refresh / hunk command that
        needs the diffmap creates the Differ here."""
        if session.diff is None:
            session.diff = self._create_differ()
        return session.diff

    def _focused_session(self):
        """The _TabSession of the currently focused tab, or None when the
        focus is not inside a compare tab. The hunk/copy/jump commands
        route through this so they can never act on another tab's diff
        records."""
        try:
            tab_id = ct.ed.get_prop(ct.PROP_TAB_ID)
        except Exception:
            return None
        return self._session_for(tab_id)

    def _is_compare_tab(self, tab_id):
        """Check if the given PROP_TAB_ID belongs to a compare tab.
        A tab is a compare tab exactly when it has a session -- the
        registry and the runtime state are one and the same now, so
        they can never drift apart. O(1) dict lookup, no disk I/O."""
        return str(tab_id) in self._sessions

    def _register_compare_tab(self, session, primary_orig_id, secondary_orig_id,
                              primary_orig_name='', secondary_orig_name='',
                              saved=True, dirty=None):
        """Persist a compare tab's registration under ITS OWN session key
        (session.state_key -- the CudaText session file group the tab was
        created in, not whatever session is current now), with the
        PROP_TAB_IDs and display names of its two original tabs. 'dirty'
        tracks which halves ('a' = primary/left, 'b' = secondary/right)
        carry unsaved edits -- on_save_pre syncs only those halves back
        to their originals. Callers that pass only the boolean 'saved'
        (legacy form) get the conservative mapping: unsaved -> both
        halves dirty. Also fills the session's saved/dirty caches."""
        if dirty is None:
            dirty = set() if saved else {'a', 'b'}
        else:
            dirty = {h for h in dirty if h in ('a', 'b')}
        # The boolean 'saved' flag (kept for compatibility with state files
        # of older plugin versions and for the tab title color) simply
        # means "no half is dirty".
        saved = not dirty
        state = self._load_state()
        if session.state_key not in state['sessions']:
            state['sessions'][session.state_key] = {}
        state['sessions'][session.state_key][session.tab_id_str] = {
            'primary_orig_tab_id': primary_orig_id,
            'primary_orig_name': primary_orig_name or '',
            'secondary_orig_tab_id': secondary_orig_id,
            'secondary_orig_name': secondary_orig_name or '',
            'saved': saved,
            'dirty': sorted(dirty),
        }
        self._save_state(state)
        session.saved = saved
        session.dirty = set(dirty)

    @staticmethod
    def _entry_dirty(entry):
        """Dirty-halves set from a persisted state entry. Entries written
        by older plugin versions have no 'dirty' key -- derive it from the
        legacy boolean 'saved' flag: unsaved -> both halves dirty (the old
        save always synced both sides, so this maps the old behavior 1:1
        onto the new selective sync)."""
        if not isinstance(entry, dict):
            return set()
        raw = entry.get('dirty')
        if raw is None:
            return set() if entry.get('saved', True) else {'a', 'b'}
        return {h for h in raw if h in ('a', 'b')}

    def _get_dirty_halves(self, session):
        """Return the set of halves with unsaved edits for a compare
        tab's session ('a' = primary/left, 'b' = secondary/right).
        The session's cache is the live value -- it is initialized at
        registration / on_start2 restore (from the persisted state,
        with legacy migration via _entry_dirty) and maintained by
        _update_dirty_state, so no disk access is needed here."""
        return set(session.dirty)

    def _update_dirty_state(self, session, dirty_halves):
        """Single write path for the saved/dirty state of a compare tab.
        'dirty_halves' is a subset of {'a','b'} naming the halves with
        unsaved edits; the legacy boolean 'saved' flag is kept in sync
        (True iff no half is dirty). Cache-guarded so on_change firing on
        every keystroke doesn't hit the disk -- only an actual state
        change (clean half gets edited / dirty half gets synced) rewrites
        the JSON."""
        dirty_halves = {h for h in dirty_halves if h in ('a', 'b')}
        if session.dirty == dirty_halves:
            return
        session.dirty = set(dirty_halves)
        saved = not dirty_halves
        state = self._load_state()
        group = state['sessions'].get(session.state_key, {})
        entry = group.get(session.tab_id_str)
        if isinstance(entry, dict):
            entry['dirty'] = sorted(dirty_halves)
            entry['saved'] = saved
            self._save_state(state)
        session.saved = saved

    def _unregister_compare_tab(self, session):
        """Remove a compare tab from the persisted state (under the
        tab's OWN state_key, so closing a tab created in another
        CudaText session never touches that session's group). Returns
        the removed entry dict or None if not found."""
        state = self._load_state()
        group = state['sessions'].get(session.state_key, {})
        entry = group.pop(session.tab_id_str, None)
        if entry is not None:
            # Clean up empty session group.
            if not group:
                del state['sessions'][session.state_key]
            self._save_state(state)
        return entry

    def _get_orig_tab_ids(self, tab_id):
        """Return (primary_orig_id, secondary_orig_id) for a compare tab,
        or (None, None) if not found. Reads the tab's OWN persisted
        session group."""
        session = self._session_for(tab_id)
        state_key = session.state_key if session is not None else self._current_session_key
        state = self._load_state()
        group = state['sessions'].get(state_key, {})
        key = str(tab_id)
        entry = group.get(key)
        if not isinstance(entry, dict):
            return (None, None)
        return (entry.get('primary_orig_tab_id'), entry.get('secondary_orig_tab_id'))

    def _enable_autostart(self):
        """Persistently subscribe to on_start2 via plugins.ini so the plugin
        auto-loads on next CudaText startup to restore compare tabs."""
        current = ct.ini_read(PLUGINS_INI, PLUGINS_INI_SECTION, MODULE_NAME, '')
        if current == 'on_start2':
            return
        ct.ini_write(PLUGINS_INI, PLUGINS_INI_SECTION, MODULE_NAME, 'on_start2')

    def _disable_autostart(self):
        """Remove the on_start2 subscription from plugins.ini so the plugin
        does NOT auto-load on next startup (no overhead when no compare
        tabs are active)."""
        current = ct.ini_read(PLUGINS_INI, PLUGINS_INI_SECTION, MODULE_NAME, '')
        if not current:
            return
        ct.ini_proc(ct.INI_DELETE_KEY, PLUGINS_INI, PLUGINS_INI_SECTION, MODULE_NAME)

    # ------------------------------------------------------------------
    # on_key event subscription (runtime, via app_proc -- install.inf
    # does NOT list on_key). Driven by the 'enable_keyboard_capture'
    # setting: subscribed while enabled, unsubscribed while disabled.
    # ------------------------------------------------------------------

    def _subscribe_on_key(self):
        """Subscribe to the on_key event at runtime (PROC_EVENTS_SUB).

        The lazy 'on_key~' form keeps keystrokes from ever auto-loading
        the plugin: the event fires only while the plugin is loaded, and
        a compare tab (the only place the hotkeys do anything) can only
        exist with the plugin loaded anyway. The key-code filter
        (_HOTKEY_KEY_FILTER) makes the event fire only for the hotkey
        key codes instead of every keystroke; the exact-modifier check
        stays in on_key. PROC_EVENTS_SUB first unsubscribes the listed
        events (all lexer/filter combinations), so repeated calls never
        stack duplicate subscriptions."""
        ct.app_proc(ct.PROC_EVENTS_SUB,
                    '{};on_key~;;{}'.format(MODULE_NAME, _HOTKEY_KEY_FILTER))

    def _unsubscribe_on_key(self):
        """Unsubscribe from the on_key event at runtime
        (PROC_EVENTS_UNSUB): key events stop reaching the plugin until
        _subscribe_on_key runs again (re-enabling the setting, or the
        next plugin load with the setting on)."""
        ct.app_proc(ct.PROC_EVENTS_UNSUB,
                    '{};on_key'.format(MODULE_NAME))

    def _sync_on_key_subscription(self):
        """Make the on_key subscription track the current value of the
        'enable_keyboard_capture' setting: subscribe when enabled,
        unsubscribe when disabled. Called at plugin load (Command
        constructor) and on every config reload (config()), so both the
        Options dialog and hand-edits to cuda_differ.json take effect
        without a restart."""
        if self.cfg.get('enable_keyboard_capture', True):
            self._subscribe_on_key()
        else:
            self._unsubscribe_on_key()

    def _visible_opts_meta(self):
        """OPTS_META filtered for what the current algorithm can use.

        The five 'ignore' options (chapter 'ignoreopt') are implemented by
        the native diff engines only -- the pure-Python algorithms compare
        strictly and ignore the flags bitmask. While a Python algorithm is
        the effective one (configured Python algo, or a native algo that
        fell back to Python because cudatext.diff_proc is missing), the
        options do nothing, so they are hidden from the config dialog and
        the compare-tab context menu instead of sitting there inert.

        The dialog is static (the Options Editor cannot show/hide items
        while it is open), so switching the algorithm takes effect the
        next time the dialog / menu is opened -- after OK, config() has
        already reloaded the new value."""
        use_native = self._resolve_algorithm()[1]
        if use_native:
            return OPTS_META
        return [m for m in OPTS_META if m.get('chp') != 'ignoreopt']

    def change_config(self):
        """Open the options dialog (cuda_options_editor (Options Editor plugin)
        or cuda_prefs (Options Editor Lite builtin plugin)) for
        the 'differ.*' settings. After the dialog closes, reload config and
        re-apply sync scroll setting."""
        try:
            import cuda_options_editor as op_ed
        except ImportError:
            import cuda_prefs as op_ed
        op_ed_dlg = None
        subset = 'differ.'  # Key to isolate settings for op_ed plugin
        how = dict(hide_lex_fil=True,  # If option has not setting for lexer/cur.file
                   stor_json=JSONFILE)
        # The ignore options are hidden while a Python algorithm is
        # selected -- see _visible_opts_meta.
        opts_meta = self._visible_opts_meta()
        try:  # New op_ed allows to skip meta-file
            op_ed_dlg = op_ed.OptEdD(
                path_keys_info=opts_meta, subset=subset, how=how)
        except:
            # Old op_ed requires to use meta-file. Always rewrite it (the
            # content now depends on the current algorithm, so a stale
            # file could miss the ignore options after a switch back to a
            # native algorithm).
            open(METAJSONFILE, 'w').write(json.dumps(opts_meta, indent=4))
            op_ed_dlg = op_ed.OptEdD(
                path_keys_info=METAJSONFILE, subset=subset, how=how)
        if op_ed_dlg.show(_('Differ Options')):  # Dialog caption
            # Need to use updated options
            self.config()
            self.scroll.toggle(self.cfg['sync_scroll'])
            # self.scroll.enable_sync_caret = self.cfg['enable_sync_caret']

    # ------------------------------------------------------------------
    # Ignore options
    # ------------------------------------------------------------------
    # The diff_proc DIFF_IGN_* ignore options live in
    # settings/cuda_differ.json under 'differ.ignoreopt.*' (chapter
    # 'ignoreopt' in the config dialog -- see OPTS_META; built into the
    # flags bitmask by differ_native.build_ignore_flags at compare time).
    # They are ALSO exposed as checkable items in the diff-tab right-click
    # context menu, right below 'Recompare' -- see tabmenu_init() and
    # tabmenu_ignore().

    def on_cli(self, fn1, fn2):
        """Called when CudaText gets command-line param -p=cuda_differ#file1#file2.
        Opens both files first (so they exist as tabs), then compares them."""
        # Open both files. file_open activates the tab, so after opening fn2,
        # ct.ed points to fn2. We pass the filenames to set_files which finds
        # them by filename.
        ct.file_open(fn1)
        ct.file_open(fn2)
        self.set_files(fn1, fn2)

    def compare_with(self):
        """Compare current document with a file picked from a dialog.
        If the chosen file is not already open, open it first."""
        fn0 = self.get_name(ct.ed)
        fn = ct.dlg_file(True, '!', '', '')
        if not fn:
            return
        # Check if the file is already open in a tab.
        already_open = False
        for h in ct.ed_handles():
            if ct.Editor(h).get_prop(ct.PROP_FN, '') == fn:
                already_open = True
                break
        if not already_open:
            # Open the file so set_files can find it as a tab.
            ct.file_open(fn)
        self.set_files(fn0, fn)

    def compare_with_tab(self):
        """Compare current document with another open tab, picked from a menu."""
        name0 = self.get_name(ct.ed)
        names = []
        for h in ct.ed_handles():
            e = ct.Editor(h)
            if self.is_match_name(e, name0):
                continue
            names.append(self.get_name(e))
        if not names:
            return

        res = ct.dlg_menu(ct.DMENU_LIST, names, caption=_('Compare file with tab'))
        if res is None:
            return
        name = names[res]
        self.set_files(name0, name)

    def diff_with(self):
        """Create a unified-diff output (read-only tab) comparing current
        document with a file picked from a dialog."""
        fn0 = self.get_name(ct.ed)
        fn = ct.dlg_file(True, '!', '', '')
        if not fn:
            return

        a = ct.ed.get_text_all(ends=True)
        # Read file b by opening it in CudaText (handles all encodings
        # correctly -- CudaText's encoding names like utf16le, koi8u, etc.
        # don't always match Python's codec names).
        h_orig = ct.ed.get_prop(ct.PROP_HANDLE_SELF)
        ct.file_open(fn, options='/nohistory')
        b = ct.ed.get_text_all(ends=True)
        ct.ed.cmd(ct_cmd.cmd_FileClose)
        # Restore focus to the original editor.
        if h_orig:
            ct.Editor(h_orig).focus()
        self.create_diff(a, b, fn0, fn)

    def diff_with_tab(self):
        """Create a unified-diff output comparing current document with
        another open tab, picked from a menu."""
        name0 = self.get_name(ct.ed)

        names = []
        ed = []
        for h in ct.ed_handles():
            e = ct.Editor(h)
            if self.is_match_name(e, name0):
                continue
            names.append(self.get_name(e))
            ed.append(h)
        if not names:
            return

        res = ct.dlg_menu(ct.DMENU_LIST, names, caption=_('Diff file with tab'))
        if res is None:
            return

        name = names[res]
        a = ct.ed.get_text_all(ends=True)
        b = ct.Editor(ed[res]).get_text_all(ends=True)

        self.create_diff(a, b, name0, name)

    def format_untitled(self, e):
        """Return a display name for an untitled tab: 'untitled:TITLE [TAB_ID]'.
        The [TAB_ID] suffix is used by is_match_name for robust identification."""
        return U_PREFIX + e.get_prop(ct.PROP_TAB_TITLE) + ' [%d]'%e.get_prop(ct.PROP_TAB_ID)

    def is_match_name(self, e, name):
        """Check if editor 'e' matches the given 'name' identifier.

        For untitled tabs: extracts the tab ID from the [id] suffix in the
        name string and matches on ID alone. This is robust against title
        changes between menu build and menu action execution -- only the
        persistent, immutable PROP_TAB_ID is compared.

        For titled tabs: matches by filename (the file's identity)."""
        if name.startswith(U_PREFIX):
            # Extract the tab ID from the [id] suffix at the end of the string.
            # format_untitled always appends ' [ID]' at the end, so the last
            # [...] is always the ID. rfind finds it even if the title itself
            # contains brackets.
            i = name.rfind('[')
            j = name.rfind(']')
            if i > 0 and j > i:
                id_str = name[i+1:j]
                if id_str.isdigit():
                    return str(e.get_prop(ct.PROP_TAB_ID)) == id_str
            # Fallback: full string comparison (for old/odd-format strings)
            return name == self.format_untitled(e)
        fn = e.get_prop(ct.PROP_FN, '')
        if fn:
            return fn==name
        return False

    def set_files(self, file0, file1):
        """Compare two files/tabs in a single split tab without temp files.

        Creates a new untitled tab, unlinks the split editors (so each half
        has independent text), splits vertically, then loads each original's
        content and editor properties into the two halves.

        ORDER MATTERS (widths and wrap): every side panel (micromap, hunk
        edge columns, overview) eats editor width, so they are created
        while the editors are still EMPTY, right after the split, and the
        split position is equalized -- then the texts are loaded into
        their final geometry and the widths are re-equalized once more
        (the line-number gutters grow with the line counts, which shifts
        the client widths again). Loading the texts first and docking
        the panels afterwards would re-wrap the already-visible lines at
        per-side DIFFERENT widths (with word-wrap on, the same line then
        wraps at different points in the two halves and the side-by-side
        pairing breaks)."""
        files = [file0, file1]
        # Properties to copy from originals to the compare halves.
        # These affect how text is displayed and interpreted.
        _PROPS_TO_COPY = [
            ct.PROP_LEXER_FILE,
            ct.PROP_NEWLINE,
            ct.PROP_ENC,
            ct.PROP_TAB_SPACES,
            ct.PROP_TAB_SIZE,
            ct.PROP_WRAP,
        ]
        orig_props = [None, None]  # list of prop-value dicts per half
        orig_tab_ids = [None, None]
        orig_texts = [None, None]
        orig_names = ['', '']

        # Find the two original tabs, grab their content, properties, tab ID,
        # and a display name (file path for real files, title for untitled).
        for (index, name) in enumerate(files):
            for h in ct.ed_handles():
                e = ct.Editor(h)
                if self.is_match_name(e, name):
                    orig_tab_ids[index] = e.get_prop(ct.PROP_TAB_ID)
                    orig_texts[index] = e.get_text_all(ends=True)
                    fn = e.get_prop(ct.PROP_FN, '')
                    if fn:
                        orig_names[index] = fn
                    else:
                        orig_names[index] = e.get_prop(ct.PROP_TAB_TITLE) or ''
                    # Capture all properties to copy.
                    orig_props[index] = {p: e.get_prop(p) for p in _PROPS_TO_COPY}
                    break

        # Bail out if we couldn't find both originals.
        if orig_texts[0] is None or orig_texts[1] is None:
            return

        # Create a new untitled tab for the compare view.
        ct.file_open('')

        # Unlink the split editors so each half has independent text,
        # then split vertically into two halves.
        ct.ed.set_prop(ct.PROP_EDITORS_LINKED, False)
        ct.ed.set_prop(ct.PROP_SPLIT, ('v', 500))

        a_ed = ct.Editor(ct.ed.get_prop(ct.PROP_HANDLE_PRIMARY))
        b_ed = ct.Editor(ct.ed.get_prop(ct.PROP_HANDLE_SECONDARY))
        try:
            a_ed.action(ct.EDACTION_LOCK)
            b_ed.action(ct.EDACTION_LOCK)

            # Set a readable combined title (just the basenames/titles, no tab IDs).
            title0 = os.path.basename(orig_names[0]) if orig_names[0] else _('Untitled')
            title1 = os.path.basename(orig_names[1]) if orig_names[1] else _('Untitled')
            ct.ed.set_prop(ct.PROP_TAB_TITLE, 'Diff: {} | {}'.format(title0, title1))

            # Copy editor properties (lexer, newline, encoding, tabs, wrap) from
            # each original to its corresponding compare half.
            for ed, props in ((a_ed, orig_props[0]), (b_ed, orig_props[1])):
                if not props:
                    continue
                for prop, val in props.items():
                    if val is not None:
                        try:
                            ed.set_prop(prop, val)
                        except Exception:
                            pass  # some props may not be settable on untitled tabs

            # Register the compare tab by its PROP_TAB_ID with the original
            # tab IDs and names, plus the session key for grouping. The
            # _TabSession is the tab's standalone world from here on.
            # (This happens BEFORE the texts are loaded: the session must
            # exist for the side panels to be stored on it, and for the
            # suppress_change counter below to be armed when the two
            # spurious on_change events of set_text_all arrive.)
            compare_tab_id = ct.ed.get_prop(ct.PROP_TAB_ID)
            try:
                session_path = ct.app_path(ct.APP_FILE_SESSION) or ''
            except Exception:
                session_path = ''
            session_key = self._session_key(session_path)
            self._current_session_key = session_key
            session = self._new_session(compare_tab_id, session_key)
            self._register_compare_tab(
                session,
                orig_tab_ids[0], orig_tab_ids[1],
                orig_names[0], orig_names[1],
                saved=True)  # initial state: content matches originals = saved

            # Color the tab title green to indicate 'synced' (no unsaved
            # changes yet -- content is identical to the originals).
            ct.ed.set_prop(ct.PROP_TAB_COLOR_FONT, 0x00A000)  # green

            # Suppress the next 2 on_change events (one per split half)
            # because set_text_all triggers on_change, which would reset the
            # green color to red. The counter is decremented in on_change;
            # real user edits after this will work normally.
            session.suppress_change = 2

            # Create the side panels (micromap, hunk edge columns,
            # overview) NOW -- on the still-EMPTY editors, directly
            # after the split -- and equalize the two halves' client
            # widths: the texts then load into the final geometry and
            # the first compare's wrap counts are taken at the final
            # widths. (refresh_compare re-checks the signature and
            # re-equalizes only when a panel option changed.)
            self.config()
            self._setup_side_panels(session, a_ed, b_ed)
            equalize_split_clients(a_ed, b_ed)

            # Load each original's content into the two split halves.
            a_ed.set_text_all(orig_texts[0])
            b_ed.set_text_all(orig_texts[1])

            # Re-equalize after the load: the line-number gutters grow
            # with the line counts (a 100-line side and a 10,000-line
            # side have different digit counts), which shifts the client
            # widths by a few pixels. Still safe -- the compare has not
            # run yet, no gaps/markers exist to destroy.
            equalize_split_clients(a_ed, b_ed)

            # Persistently subscribe to on_start2 so the plugin auto-loads on
            # next startup to restore compare tabs.
            self._enable_autostart()

            # Track this tab for scroll sync.
            self.scroll.tab_id.add(compare_tab_id)
            self.scroll.toggle(self.cfg.get('sync_scroll'))

            # app sets LastLineOnTop automatically on adding 'gaps', but if file
            # don't have gaps, we must set it manually.
            a_ed.set_prop(ct.PROP_LAST_LINE_ON_TOP, True)
            b_ed.set_prop(ct.PROP_LAST_LINE_ON_TOP, True)

            # if file was in group-2, and now group-2 is empty, set "one group" mode
            if ct.app_proc(ct.PROC_GET_GROUPING, '') in [ct.GROUPS_2VERT, ct.GROUPS_2HORZ]:
                e = ct.ed_group(1)
                if not e:
                    ct.app_proc(ct.PROC_SET_GROUPING, ct.GROUPS_ONE)

            self.refresh_compare()
        finally:
            a_ed.action(ct.EDACTION_UNLOCK)
            b_ed.action(ct.EDACTION_UNLOCK)

    def create_diff(self, txt0, txt1, fn0, fn1):
        """Create a read-only unified-diff tab from two text strings.
        Used by diff_with and diff_with_tab commands.

        The unified-diff output is always produced with Python's stdlib
        difflib.unified_diff, never the chosen
        differ.algorithm.diff_algorithm --
        the algorithm only affects side-by-side line pairing, not the
        patch format itself, and unified diff is a machine-consumed patch
        stream (patch / git apply / CI / code-review bots). difflib's
        default autojunk=True is left in place here: the autojunk
        heuristic only triggers on files with >200 occurrences of a
        single line at >1% of file size (rare in real source files),
        and even when it triggers the patch is still valid for `patch`/
        `git apply`. See readme.txt ("Diff current document with
        file..." section) for the user-facing note.
        """
        a = split_lines_safe(txt0)
        b = split_lines_safe(txt1)
        r = ''.join(unified_diff(a, b, fn0, fn1,
                                 n=self.cfg.get('diff_context')))

        global DIFF_TAB_COUNT
        tab = 'Diff ' + str(DIFF_TAB_COUNT)
        DIFF_TAB_COUNT += 1

        ct.file_open('')
        ct.ed.set_text_all(r)
        ct.ed.set_prop(ct.PROP_LEXER_FILE, 'Diff')
        ct.ed.set_prop(ct.PROP_RO, True)
        ct.ed.set_prop(ct.PROP_TAB_TITLE, tab)
        ct.ed.set_prop(ct.PROP_SAVE_HISTORY, False)

    def on_state(self, ed_self, state):
        """App-level state changes: reload the config when a UI or syntax
        theme switch may have moved the compare colors.

        Note: EDSTATE_* values (word-wrap, read-only, zoom...) never
        arrive here. Since CudaText 1.94.0 (api 1.0.320) on_state only
        carries APPSTATE_* constants -- editor states are delivered to
        on_state_ed (see the next handler). The plugin requires api
        1.0.483, so relying on that event split is safe."""
        if state == ct.APPSTATE_THEME_UI:
            # UI theme switched: re-resolve the compare colors -- 'auto'
            # may now detect a different family, and the grey/black
            # presets' ignored colors (live editor background) moved with
            # the theme. config() also re-registers the bookmark kinds
            # with the new colors. No auto-refresh afterwards: the repaint
            # cost on big compares outweighs the benefit (the next refresh,
            # manual or automatic, paints with the new colors).
            self.config()
            self._refresh_column_colors()
        elif state == ct.APPSTATE_THEME_SYNTAX:
            self.config()
            self._refresh_column_colors()

    def _refresh_column_colors(self):
        """Live-update the hunk edge columns' theme colors (EdGutterBg /
        EdGutterFont) on theme switches, for every open compare tab --
        without waiting for the next Recompare. Cheap: two dict lookups
        and one one-shot strip repaint per tab (background fill plus
        every bracket, whole file)."""
        for session in self._sessions.values():
            columns = session.columns
            if columns is None:
                continue
            try:
                columns.set_colors(*_ed_gutter_colors())
                columns.paint()
            except Exception:
                pass

    def on_state_ed(self, ed_self, state):
        """Editor-level state changes (EDSTATE_* constants -- this event,
        not on_state, is where CudaText delivers them).

        EDSTATE_WRAP on a half of a compare tab: propagate the new wrap
        mode to the other half first, then refresh the compare so the
        inter-line gaps are re-sized with wrap-aware visual-row counts
        (see _sync_wrap_state, which does both)."""
        if state == ct.EDSTATE_WRAP:
            # Word-wrap mode changed on one of the split halves.
            if self._is_compare_tab(ed_self.get_prop(ct.PROP_TAB_ID)):
                self._sync_wrap_state(ed_self)  # also refreshes

    def _sync_wrap_state(self, ed_self):
        """Word-wrap sync: make both halves of ed_self's compare tab use
        the wrap mode ed_self just got, then refresh the compare.

        Entered from on_state_ed(EDSTATE_WRAP) -- the ONLY event that
        carries wrap changes (on_state stopped supporting EDSTATE_*
        values in CudaText 1.94.0; before the on_state_ed subscription
        existed, this whole feature was dead code because the wrap
        event never arrived).

        The user toggles wrap on ONE half (menu command / hotkey); without
        this mirror the halves would wrap independently and the side-by-
        side alignment breaks. We copy ed_self's new PROP_WRAP value to the
        opposite half BEFORE refreshing, so the gap re-sizing inside
        refresh_compare reads the wrap counts of two halves that are
        already in the new wrap mode.

        Echo suppression: setting PROP_WRAP may itself fire EDSTATE_WRAP
        for the other half (synchronously during our set_prop, or later).
        By the time such an echo arrives, both halves already carry the
        same wrap mode, and 'the two halves already match' is exactly the
        condition under which there is nothing left to propagate or
        re-size -- so the handler simply returns. A genuine user toggle
        always fires with the halves still mismatched (the opposite half
        still holds the old mode), which is the only path that propagates
        and refreshes. This works the same whether CudaText delivers the
        echo synchronously or after our handler has returned, and needs
        no flags or timers."""
        wrap_mode = ed_self.get_prop(ct.PROP_WRAP)
        a_ed = ct.Editor(ed_self.get_prop(ct.PROP_HANDLE_PRIMARY))
        b_ed = ct.Editor(ed_self.get_prop(ct.PROP_HANDLE_SECONDARY))
        mismatched = False
        for e in (a_ed, b_ed):
            try:
                if e.get_prop(ct.PROP_WRAP) != wrap_mode:
                    e.set_prop(ct.PROP_WRAP, wrap_mode)
                    mismatched = True
            except Exception:
                pass  # dead handle: tab is being closed, nothing to sync
        if not mismatched:
            # Echo of our own propagation (both halves already match) --
            # the refresh ran when the real toggle was handled.
            return
        # Automatic -- no dialog. The refresh re-reads the (now equal)
        # wrap modes of both halves and re-sizes the inter-line gaps with
        # wrap-aware visual-row counts.
        self.refresh_compare(ed_self, show_dialog=False)

    def on_scroll(self, ed_self):
        """Forward scroll events to ScrollSplittedTab for synchronized
        scrolling, keep the overview slider tracking the position, and
        re-copy the hunk edge columns' pre-painted strip windows.
        Routed to the scrolled tab's OWN session -- one tab's scroll never
        touches another tab's overview or timers.

        The overview update is two-layered:
        - immediate: overview.track_paint() repaints the slider at up to
          ~33 fps (wall-clock throttled), so the thumb follows scrolling
          as it happens. A timer alone cannot do this during a slider
          drag: mouse moves flood the message queue, and WM_TIMER is
          only delivered when the queue drains -- a timer-debounced
          slider stays FROZEN until the drag stops.
        - trailing: a one-shot 150ms timer does the final full repaint
          after scrolling stops (catches the settled position, e.g. the
          end-of-scroll clamping)."""
        tab_id = ed_self.get_prop(ct.PROP_TAB_ID)
        session = self._session_for(tab_id)
        if session is not None:
            self.scroll.on_scroll(ed_self)
            if session.overview is not None:
                session.overview.track_paint()
            # Hunk edge columns: the edges are ALREADY painted (whole
            # file, once, at 1:1 -- see columns.py); this only copies
            # the viewport's window of the pre-painted strip -- ONE
            # sub-rect blit per column, SYNCHRONOUSLY in this very
            # scroll event (CudaText fires on_scroll right after
            # painting the scrolled editor), so the columns move in
            # the same display frame as the text. No timers, no
            # throttle; a mirrored scroll echo or a horizontal scroll
            # is skipped inside (same window already on screen).
            if session.columns is not None:
                session.columns.present()
            # Trailing repaint 150ms after the last scroll event.
            if not session.overview_timer:
                session.overview_timer = True
                callback = 'module=cuda_differ;cmd=_overview_repaint_timer;info={};'.format(session.tab_id_str)
                ct.timer_proc(ct.TIMER_START_ONE, callback, 150)

    def _overview_repaint_timer(self, tag='', info=''):
        """Timer callback that finalizes a tab's OVERVIEW update 150ms
        after the last scroll event (the overview's slider repaint is
        throttled, so this catches the settled position after scroll
        clamping). The hunk edge columns need NOTHING here: their
        window copy runs synchronously inside every on_scroll event
        (see on_scroll), so they are always at the settled position
        already -- no trailing repaint, no bracket drawing, ever
        (see columns.py)."""
        if not info:
            return
        session = self._sessions.get(info)
        if session is None:
            return  # tab closed with the timer still in flight
        session.overview_timer = False
        if session.overview is not None:
            session.overview.paint()

    def _columns_layout_timer(self, tag='', info=''):
        """Recurring per-tab layout guard for the hunk edge columns
        (armed by columns.HunkColumns._start_timer, one timer per compare
        tab). The LCL align system keeps the columns in their slots
        through window resizes and splitter drags on its own (the right
        column rides inside the split bar); the guard only re-applies a
        split-bar width that was reset behind our back, repaints after
        size changes, and destroys the columns when the split tree is
        gone. See HunkColumns.check_layout()."""
        if not info:
            return
        session = self._sessions.get(info)
        if session is None or session.columns is None:
            # The tab (or its columns) is gone but the timer still
            # fires -- stop it by its callback string (destroy() normally
            # does this; this is the leaked-timer safety net). The
            # interval argument is required by the API signature but
            # ignored for TIMER_STOP (the timer is matched by callback).
            ct.timer_proc(ct.TIMER_STOP,
                          'module=cuda_differ;cmd=_columns_layout_timer;'
                          'info={};'.format(info), 0)
            return
        try:
            session.columns.check_layout()
        except Exception:
            pass

    def on_caret(self, ed_self):
        """Mirror caret to opposite editor when sync_caret is enabled."""
        if self.cfg.get('enable_sync_caret', False):
            self.sync_caret()

    def on_change(self, ed_self):
        """Fires immediately on every keystroke. Used for:
        - Resetting the compare tab title color from green to default (red)
          when the user makes changes (indicating unsaved edits).
        - Marking the edited HALF dirty ('a' = primary/left, 'b' =
          secondary/right) so on_save_pre later syncs ONLY that half back
          to its original tab -- clean halves and their originals are
          never touched by save.
        - Persisting the 'unsaved' state so it survives restarts.

        Uses on_change (not on_change_slow) because on_change_slow has a
        1-2 second delay which causes race conditions: if the user edits
        then quickly saves, the delayed on_change_slow would fire AFTER
        on_save_pre and reset the green color back to red.

        The first 2 calls after a compare is created are suppressed (see
        the session's suppress_change counter) because set_text_all
        triggers spurious on_change events that would reset the initial
        green color.

        Performance: the session lookup is one dict get (no disk I/O),
        so non-compare tabs return in O(1). The handler is lightweight
        enough for on_change."""
        tab_id = ed_self.get_prop(ct.PROP_TAB_ID)
        session = self._session_for(tab_id)
        if session is None:
            return
        if session.suppress_change > 0:
            session.suppress_change -= 1
            # Skip color change and state write for this spurious event.
        else:
            # Real user edit -- reset title color to default (red).
            ed_self.set_prop(ct.PROP_TAB_COLOR_FONT, ct.COLOR_NONE)
            # Remember WHICH half was edited ('a' = primary/left,
            # 'b' = secondary/right) so on_save_pre syncs only the dirty
            # halves instead of always rewriting both originals. The
            # 'copy hunk left/right' commands also land here: their
            # insert/delete on the target half fires on_change with that
            # half as ed_self, so merged hunks are tracked too.
            h_self = ed_self.get_prop(ct.PROP_HANDLE_SELF)
            h_primary = ed_self.get_prop(ct.PROP_HANDLE_PRIMARY)
            half = 'a' if h_self == h_primary else 'b'
            halves = self._get_dirty_halves(session)
            halves.add(half)
            # Persist the unsaved state so on_start2 can restore the
            # correct color after restart. Cache-guarded: only the first
            # edit of a clean half writes to disk.
            self._update_dirty_state(session, halves)

    def on_change_slow(self, ed_self):
        """Fires after the user edits and a short pause passes. Used only
        for auto-refreshing the diff markers if that option is enabled.
        Color/saved-state logic is handled in on_change (immediate)."""
        if self.cfg.get('enable_auto_refresh', False):
            self.refresh_compare(ed_self, show_dialog=False)  # automatic -- no dialog

    def on_key(self, ed_self, key, state):
        """Hotkeys inside compare tabs (subscribed at RUNTIME to the lazy
        on_key~ event with a key-code filter -- see
        _sync_on_key_subscription -- so this only runs while the plugin
        is loaded, and only for the hotkey key codes).

        Mapping (the same commands as the menu items, see _HOTKEYS):
            Alt+Left       -> "Copy current difference to the left"
            Alt+Right      -> "Copy current difference to the right"
            Ctrl+Alt+Left  -> "Copy current line to the left"
            Ctrl+Alt+Right -> "Copy current line to the right"
            Alt+Down       -> "Jump to next difference"
            Alt+Up         -> "Jump to previous difference"
            F5             -> "Recompare"

        Everything else passes through untouched: keys not in the map;
        hotkey keys without their EXACT modifier state (the Left/Right
        arrows carry TWO bindings each -- plain Alt moves the whole
        difference, Ctrl+Alt moves the caret's single line -- so the
        arrows need Alt-only or Ctrl+Alt-only, never a mix with Shift/
        Meta; Down/Up need Alt and ONLY Alt; F5 needs no modifiers at
        all -- Alt+Shift/Ctrl/Meta combos and modified F5 keep their
        normal behavior, so user bindings are never shadowed); and
        hotkeys pressed outside the two halves of a compare tab this
        plugin manages -- in any other editor the keys keep whatever
        meaning the user's keybindings give them.

        The 'enable_keyboard_capture' setting gates the event
        subscription itself (subscribed when enabled, unsubscribed when
        disabled -- see _sync_on_key_subscription); the cfg check below
        is a cheap safety net for a stale subscription.

        Returns False when a hotkey was recognized and its command ran,
        which makes CudaText drop the key instead of also running the
        editor's own action (Alt+Arrow caret movement etc.) on top of it
        (returning False also stops the event's propagation to other
        plugins -- exactly what we want for a consumed hotkey). Returns
        None otherwise so the key propagates unchanged.

        The dispatched commands carry their own guards: copy_left/right
        and copy_line_left/right refuse to edit while a background
        compare is running ("cannot edit while compare is running"
        status hint), jump_next/prev report "No differences were found"
        on a clean compare, and refresh_compare drops with "compare
        already running" while one is in flight -- the hotkey then just
        surfaces that status message, same as the menu command would.
        The hunk commands also tell you when the caret is not at any
        difference ("caret is not on a difference" / "caret is not on a
        changed line") instead of silently doing nothing.
        """
        # Early-out for keys not in the hotkey map. With the event's
        # key-code filter this should not even happen, but on_key must
        # stay correct if the filter is ever bypassed.
        entries = _HOTKEYS.get(key)
        if not entries:
            return
        # Safety net: with the setting off the subscription should be
        # gone; honor the setting anyway if an event slips through.
        if not self.cfg.get('enable_keyboard_capture', True):
            return
        # EXACT modifier match per entry: 'a' = Alt and only Alt,
        # 'ca' = Ctrl+Alt and only those, '' = no modifiers at all.
        # set() comparison is order-insensitive, so any state-string
        # order CudaText may produce works.
        mods = set(state) if isinstance(state, str) else set()
        method = None
        for entry_method, need_mods in entries:
            if mods == set(need_mods):
                method = entry_method
                break
        if method is None:
            return
        # Only the two halves of a compare tab we manage. Both halves of
        # the split share one PROP_TAB_ID, so a single lookup covers
        # whichever side the caret is in.
        try:
            tab_id = ed_self.get_prop(ct.PROP_TAB_ID)
        except Exception:
            return  # dead/unusual editor handle -- let the key pass
        if not self._is_compare_tab(tab_id):
            return
        getattr(self, method)()
        # Eat the key so the editor does not ALSO run its default
        # action after our command.
        return False

    def on_save_pre(self, ed_self):
        """Intercept Ctrl+S in a compare tab. Instead of saving the untitled
        compare tab to disk (which would show a Save dialog), sync the DIRTY
        halves' content back to their original tabs and block the save.
        Returns False to block the default save behavior.

        Only halves with unsaved edits (tracked per half by on_change /
        _update_dirty_state) are synced -- a clean half is left alone, so
        its original tab is not rewritten (no pointless replace_lines undo
        step, no disk write, no clobbering of edits the original may have
        received outside the compare view). With NO dirty half at all, the
        save is a complete no-op: nothing is synced, the tab just stays
        green. If only one half is dirty, only that half's original is
        synced and saved.

        On successful sync, the compare tab's title font is colored green
        to indicate 'synced' -- but only once no dirty half remains (if a
        dirty half failed to sync, e.g. its original tab was closed, the
        tab correctly stays red). The color is reset to COLOR_NONE (which
        CudaText re-colors red) when the user edits again -- see
        on_change. We do NOT clear PROP_MODIFIED, because that would
        prevent CudaText's session auto-save/restore from persisting the
        compare tab's content across restarts."""
        tab_id = ed_self.get_prop(ct.PROP_TAB_ID)
        session = self._session_for(tab_id)
        if session is None:
            return  # not a compare tab -- let CudaText handle normally

        # Cancel any in-flight background compare for this tab before
        # doing anything else. Save can be reached via the "Save
        # changes?" prompt right before a tab close, so a compare left
        # running is about to become useless work; drop it here instead
        # of racing on_close's own cancellation, which fires after this
        # handler returns. _cancel_job also releases the compare-level
        # editor lock / read-only state, so the halves are editable
        # again by the time the save finishes.
        job = session.job
        session.job = None
        if job is not None:
            self._cancel_job(job)

        # Which halves carry unsaved edits? Only those get synced.
        dirty = self._get_dirty_halves(session)
        if not dirty:
            # Neither half is dirty: nothing to sync. The old behavior
            # re-synced and re-saved BOTH files on every Ctrl+S (and on
            # the "Save changes?" prompt of a closing tab) -- pure waste.
            # Clear any pending suppress counter so a later real edit is
            # not swallowed by it, keep the title green, and tell the
            # user why nothing was saved.
            session.suppress_change = 0
            self._update_dirty_state(session, set())  # repair stale state
            ed_self.set_prop(ct.PROP_TAB_COLOR_FONT, 0x00A000)  # green
            ct.msg_status(_('Differ: no unsaved changes'))
            # Block the default save (which would show a Save dialog for
            # the untitled compare tab).
            return False

        # Get both split editors.
        a_ed = ct.Editor(ed_self.get_prop(ct.PROP_HANDLE_PRIMARY))
        b_ed = ct.Editor(ed_self.get_prop(ct.PROP_HANDLE_SECONDARY))

        # Look up the original tab IDs from the persisted state.
        orig_a_id, orig_b_id = self._get_orig_tab_ids(tab_id)

        # Sync ONLY the dirty halves (short-circuit: a clean half's text
        # is not even extracted, and its original is not searched for).
        synced_a = ('a' in dirty and orig_a_id is not None and
                    self._sync_to_original_by_id(
                        orig_a_id, a_ed.get_text_all(ends=True)))
        synced_b = ('b' in dirty and orig_b_id is not None and
                    self._sync_to_original_by_id(
                        orig_b_id, b_ed.get_text_all(ends=True)))

        if synced_a or synced_b:
            # Clear any pending suppress counter -- save overrides the
            # initial-creation suppress.
            session.suppress_change = 0

            # Drop the synced halves from the dirty set. A dirty half
            # whose sync failed (e.g. its original tab is gone) stays
            # dirty, so the tab title correctly stays red.
            remaining = set(dirty)
            if synced_a:
                remaining.discard('a')
            if synced_b:
                remaining.discard('b')

            # Green only when BOTH halves are synced; while any half is
            # still unsaved the tab must stay red.
            if remaining:
                ed_self.set_prop(ct.PROP_TAB_COLOR_FONT, ct.COLOR_NONE)
            else:
                ed_self.set_prop(ct.PROP_TAB_COLOR_FONT, 0x00A000)  # green
            # Persist the state (legacy 'saved' flag + dirty halves) so
            # on_start2 can restore the correct color after restart.
            self._update_dirty_state(session, remaining)
            # Auto-refresh diff markers so the user sees updated
            # highlights without needing to click Recompare manually.
            # self.refresh_compare(ed_self, show_dialog=False)  # automatic -- no dialog

        # Block the default save (which would show a Save dialog for the
        # untitled compare tab).
        return False

    def _sync_to_original_by_id(self, orig_tab_id, new_text):
        """Find the original tab by PROP_TAB_ID and overwrite its content
        (preserving Undo via replace_lines). If the original is a real file
        on disk, save it. If untitled, mark it modified. Returns True if
        the original was found."""
        target = str(orig_tab_id)
        for h in ct.ed_handles():
            e = ct.Editor(h)
            if str(e.get_prop(ct.PROP_TAB_ID)) == target:
                # replace_lines() items may carry their line terminator
                # ('\r\n', '\r', '\n') at the end, so every line keeps
                # its own ending (the old new_text.split('\n') left the
                # CR of CRLF inside the line content -> CRCRLF).
                caret = e.get_carets()
                try:
                    lines = split_lines_safe(new_text) or ['']
                    count = e.get_line_count()
                    if count > 0:
                        e.replace_lines(0, count - 1, lines)
                    else:
                        e.insert(0, 0, new_text)
                except Exception as ex:
                    msg('replace_lines failed, falling back to '
                        'set_text_all: {}'.format(ex), level=1)
                    e.set_text_all(new_text)
                if caret:
                    x, y, x2, y2 = caret[0]
                    try:
                        e.set_caret(x, y, x2, y2)
                    except Exception:
                        pass
                orig_fn = e.get_prop(ct.PROP_FN, '')
                if orig_fn:
                    # Real file on disk -- save it immediately.
                    e.save()
                else:
                    # Untitled tab -- mark modified, no Save dialog.
                    e.set_prop(ct.PROP_MODIFIED, True)
                ct.msg_status(_('Differ: synced changes to original tab'))
                return True
        ct.msg_status(_('Differ: original tab no longer open'))
        return False

    def on_start2(self, ed_self):
        """Called once on program start, after configs are applied and just
        before the main form shows.

        Performs startup cleanup: removes dead records (compare tabs that
        were not restored by CudaText -- e.g. empty untitled tabs are
        discarded by CudaText on restart). Then rebuilds in-memory caches,
        re-subscribes to on_scroll, re-applies title colors, and re-applies
        diff markers for each surviving compare tab.

        We use on_start2 (not on_start) because on_start fires too early --
        before session restore completes. By on_start2, all editors exist
        and CudaText has finished restoring the modified flag/tab colors."""
        # Get the current session and cache its key.
        try:
            session_path = ct.app_path(ct.APP_FILE_SESSION) or ''
        except Exception:
            session_path = ''
        self._current_session_key = self._session_key(session_path)

        state = self._load_state()

        # --- Cleanup dead records ---
        # Get all open tab IDs so we can check which compare tabs still exist.
        open_tab_ids = set()
        for h in ct.ed_handles():
            e = ct.Editor(h)
            open_tab_ids.add(str(e.get_prop(ct.PROP_TAB_ID)))

        # Check the current session's compare tabs. If a compare tab ID is
        # not in open_tab_ids, it's a dead record (CudaText didn't restore it
        # -- e.g. it was an empty untitled tab that CudaText discards).
        session = state['sessions'].get(self._current_session_key, {})
        dead_keys = [k for k in session if k not in open_tab_ids]
        for k in dead_keys:
            del session[k]
        if not session and self._current_session_key in state['sessions']:
            # Clean up empty session.
            del state['sessions'][self._current_session_key]
        if dead_keys:
            self._save_state(state)

        # --- Rebuild the sessions for the surviving compare tabs ---
        self.scroll.tab_id = set()
        self._sessions = {}
        for tab_id_str, entry in session.items():
            if not isinstance(entry, dict):
                continue
            try:
                tab_id_int = int(tab_id_str)
            except (ValueError, TypeError):
                tab_id_int = tab_id_str
            self.scroll.tab_id.add(tab_id_int)
            # A restored tab gets a full standalone session, but its
            # Differ stays None until the first compare needs it (no
            # 'Using ... Algo' status spam at startup; a restored tab's
            # diff markers are not re-painted on purpose -- see the
            # commented-out refresh above).
            tab_session = _TabSession(tab_id_int, self._current_session_key)
            # Populate the saved/dirty caches from disk. Entries
            # written by older plugin versions have no 'dirty' key --
            # _entry_dirty maps the legacy 'saved' flag instead (unsaved
            # -> both halves dirty, so the first Ctrl+S after upgrade
            # syncs both sides, exactly like the old always-sync-both
            # behavior).
            tab_session.dirty = self._entry_dirty(entry)
            tab_session.saved = not tab_session.dirty
            self._sessions[tab_id_str] = tab_session

            # Re-apply the title color: green only when no half is dirty.
            if tab_session.saved:
                self._apply_color_to_tab(tab_id_str, 0x00A000)  # green
        # Re-subscribe to on_scroll event if sync_scroll is enabled.
        if self.cfg.get('sync_scroll') and self.scroll.tab_id:
            ct.app_proc(ct.PROC_EVENTS_SUB, self.scroll.name+';on_scroll;;')

    def _apply_color_to_tab(self, tab_id_str, color):
        """Apply a title font color to a compare tab by its PROP_TAB_ID.
        Used by on_start2 to restore the green/default color after restart."""
        target = str(tab_id_str)
        for h in ct.ed_handles():
            e = ct.Editor(h)
            if str(e.get_prop(ct.PROP_TAB_ID)) == target:
                e.set_prop(ct.PROP_TAB_COLOR_FONT, color)
                return

    def on_exit_pre(self, ed_self):
        """Called before CudaText is about to exit. Sets a flag so that
        on_close (which fires next, once per closing tab) can skip
        temp-file deletion and let compare tabs persist across restarts."""
        self._app_exiting = True
        # Cancel every in-flight background engine compare: the app is
        # exiting, so no result can ever be consumed. diff_proc(DIF_CANCEL)
        # stops each engine thread cooperatively (a couple of seconds at
        # most); the completion callback of a cancelled compare is never
        # invoked. The stale flags below are belt-and-braces for the
        # finishing race (a compare that completed right before its
        # cancellation arrived), and _app_exiting guards the callback
        # path too.
        for tab_session in self._sessions.values():
            job = tab_session.job
            tab_session.job = None
            if job is not None:
                self._cancel_job(job)

    '''
    def on_tab_change(self, ed_self):
        self.config()
        self.scroll.toggle(self.cfg.get('sync_scroll'))
    '''

    def on_tab_menu(self, ed_self):
        """Build the right-click tab context menu (Compare with..., Recompare, etc.)."""
        self.tabmenu_init(ed_self)

    def _lock_compare_editors(self, job):
        """Take the whole-compare editor lock for a background compare
        (only when the hardcoded module constant
        LOCK_EDITORS_WHILE_COMPARING is on; no-op otherwise).

        For BOTH halves of the compare tab, in this order:
          - save the current PROP_RO value and set PROP_RO=True — typing
            is blocked for the engine's whole run (EDACTION_LOCK does
            NOT block input; without RO the user could still write
            blind into the locked editor);
          - EDACTION_LOCK — the paint lock: from the first repaint on,
            each half shows the 'busy' placeholder (hourglass), which is
            what tells the user a compare is running.

        Each half is acquired independently and dead-handle-safe: the
        get_prop/set_prop probe raises for a dead handle, and that half
        is simply skipped (nothing acquired, nothing to release). The
        original PROP_RO values are recorded in job.editor_lock as
        {'a': original_ro, 'b': original_ro} and restored EXACTLY once
        by _release_compare_editors, no matter how the compare ends.
        """
        if not LOCK_EDITORS_WHILE_COMPARING:
            return
        state = {}
        for half, e in (('a', job.a_ed), ('b', job.b_ed)):
            try:
                orig_ro = bool(e.get_prop(ct.PROP_RO, False))
                e.set_prop(ct.PROP_RO, True)
            except Exception:
                continue  # dead handle: nothing acquired for this half
            e.action(ct.EDACTION_LOCK)
            state[half] = orig_ro
        if state:
            job.editor_lock = state

    def _release_compare_editors(self, job):
        """Release what _lock_compare_editors acquired: restore each
        half's original PROP_RO value and EDACTION_UNLOCK both halves.
        Idempotent (guarded by job.editor_lock, which is cleared
        first), so EVERY abandonment path can call it without
        double-unlocking the counted paint lock: normal completion,
        engine error, cancel (user command / tab close / app exit /
        save-before-close), a stale callback arriving after the cancel,
        an exception mid-paint.

        Per half, read-only is restored BEFORE the unlock so the
        repaint that the unlock triggers shows the finished compare
        with writing already re-enabled. All calls are guarded — the
        tab may already be gone (dead editor handles must not crash
        the cancel/close/exit paths that reach here).
        """
        state = job.editor_lock
        if not state:
            return
        job.editor_lock = None  # idempotency guard: release exactly once
        for half, e in (('a', job.a_ed), ('b', job.b_ed)):
            if half not in state:
                continue
            try:
                e.set_prop(ct.PROP_RO, state[half])
            except Exception:
                pass
            try:
                e.action(ct.EDACTION_UNLOCK)
            except Exception:
                pass  # dead handle: the tab is already gone

    def _cancel_job(self, job):
        """Cancel one in-flight background compare job: mark it stale (so
        its completion callback, if the engine delivers one after all,
        turns into a no-op -- see _CompareJob.stale in
        _on_native_diff_done), release the whole-compare editor lock /
        read-only state the job's kick-off acquired (the editors must
        become editable again the moment the compare is gone), and tell
        the engine to stop cooperatively via diff_proc(DIF_CANCEL) if a
        background call was actually started. No-op for a Python-
        algorithm job, which never gets a job_handle. Does not touch
        the session's job slot -- callers clear session.job themselves
        (close / save / cancel command / exit each handle it
        differently but identically safe)."""
        job.stale = True
        # Close the engine-wait profiling pair: a cancelled compare's
        # completion callback never fires, so this is the ONLY place the
        # pair is closed for cancels (tab close / app exit / save /
        # cancel commands). Token-guarded no-op when the callback (or a
        # newer kick-off's reset) already handled it.
        Profiler.stop_async_pair(job.profiler_async_token)
        job.profiler_async_token = None
        self._release_compare_editors(job)
        if job.job_handle:
            dfn.cancel_async_line_diff(job.job_handle)
            job.job_handle = 0

    def cancel_compare(self):
        """Command: cancel the in-flight background compare for the
        current compare tab, if any is running. Does not close the tab
        or touch its existing diff markers -- it only stops a compare
        that is still in progress (e.g. one kicked off by auto-refresh
        or a prior manual refresh on a large file). Same effect as
        on_close's cancellation, but callable directly without closing
        the tab, and without needing on_save_pre's close-then-save
        path."""
        tab_id = ct.ed.get_prop(ct.PROP_TAB_ID)
        session = self._session_for(tab_id)
        if session is None:
            return ct.msg_status(_('Differ: not a compare tab'))
        job = session.job
        session.job = None
        if job is None:
            return ct.msg_status(_('Differ: no compare running'))
        self._cancel_job(job)
        ct.msg_status(_('Differ: compare cancelled'))

    def cancel_all_compares(self):
        """Command: cancel every in-flight background compare across all
        compare tabs (not just the current one). Same cancellation as
        on_exit_pre, but triggerable on demand instead of only at app
        exit -- e.g. after refreshing several large-file compares at
        once and deciding none of the results are needed."""
        jobs = [s.job for s in self._sessions.values() if s.job is not None]
        if not jobs:
            return ct.msg_status(_('Differ: no compares running'))
        for job in jobs:
            job.session.job = None
            self._cancel_job(job)
        ct.msg_status(_('Differ: cancelled {} compare(s)').format(len(jobs)))

    # native_histogram / native_myers only work when cudatext.diff_proc
    # is present. On older CudaText builds the plugin falls back to the
    # closest pure-Python algorithm (documented in OPTS_META):
    #   native_histogram -> hybrid
    #   native_myers     -> myers
    _NATIVE_TO_PYTHON_FALLBACK = {
        'native_histogram': 'hybrid',
        'native_myers': 'myers',
    }

    def _resolve_algorithm(self):
        """Return (effective_algo, use_native, fell_back).

        effective_algo is what the Differ instance should run.
        use_native is True only when a native algo is configured AND
        cudatext.diff_proc is available.
        fell_back is True when the user configured a native algo but
        the native API is missing, so a Python equivalent is used.
        """
        algo = self.cfg.get('diff_algorithm', 'native_histogram')
        if algo in self._NATIVE_TO_PYTHON_FALLBACK:
            if dfn._HAS_NATIVE_DIFF:
                return algo, True, False
            return self._NATIVE_TO_PYTHON_FALLBACK[algo], False, True
        return algo, False, False

    def _create_differ(self):
        """Create the appropriate Differ instance based on the configured
        algorithm. Returns a differ_native.Differ for native algorithms
        (when the native API is available), or a differ_python.Differ for
        all Python algorithms and as a fallback when native is unavailable.

        When a native algorithm is configured on an old CudaText build
        without diff_proc, the status bar reports the fallback to the
        equivalent Python algorithm (native_histogram -> hybrid,
        native_myers -> myers).
        """
        algo, use_native, fell_back = self._resolve_algorithm()
        if use_native:
            ct.msg_status(_("Differ: Using Native Algo {}").format(algo))
            return dfn.Differ()
        if fell_back:
            configured = self.cfg.get('diff_algorithm', 'native_histogram')
            ct.msg_status(
                _('Differ: native API not available — falling back to Python algo {} '
                  '(configured: {})').format(algo, configured))
        else:
            ct.msg_status(_("Differ: Using Python Algo {}").format(algo))
        return dfp.Differ()

    def _ensure_correct_differ(self, session):
        """Check if the SESSION's Differ matches the configured algorithm
        type, and swap it if not. Called at the start of refresh_compare
        (per compare tab -- each session owns its Differ, so an algorithm
        switch never disturbs another tab's records) so the Differ is
        always the right type before a compare runs. Preserves the
        options (withdetail, beautify_alignment) but NOT the sequences:
        neither Differ holds sequences between compares — both
        the native Differ (compare(a_text, b_text)) and the Python
        Differ (compare(lines_a, lines_b)) take their inputs as
        parameters at compare() time. refresh_compare always re-passes
        fresh data to compare() right after this swap, before any
        compare() runs. Returns the (possibly new) Differ.

        Also applies the native→Python algorithm mapping when the native
        API is missing, so the Python Differ never receives a
        'native_*' name it cannot run.
        """
        diff = self._session_diff(session)
        algo, want_native, fell_back = self._resolve_algorithm()
        is_native = isinstance(diff, dfn.Differ)
        if want_native == is_native:
            # Still refresh the effective algorithm name (handles a
            # config change between two native algos, or the fallback
            # mapping on a Python Differ that already exists).
            diff.diff_algorithm = algo
            return diff
        # Swap: preserve options only. We do NOT preserve sequences:
        #   - Both differs take their inputs as compare() params
        #     (native: raw texts; Python: line lists). There is nothing
        #     to preserve on the instance.
        # refresh_compare always calls compare(...) with fresh state right
        # after this swap, before any compare() runs, so the empty new
        # Differ is fine. The old Differ (with the PREVIOUS diffmap)
        # simply dies with the swap -- a hunk command reading the
        # diffmap mid-swap sees either the old map or the new empty one,
        # both consistent states.
        old_withdetail = getattr(diff, 'withdetail', True)
        old_beautify_alignment = getattr(diff, 'beautify_alignment', False)
        diff = dfn.Differ() if want_native else dfp.Differ()
        diff.withdetail = old_withdetail
        diff.beautify_alignment = old_beautify_alignment
        diff.diff_algorithm = algo
        session.diff = diff
        # Fallback status is reported once in refresh_compare when the
        # resolved algorithm is applied — avoid a duplicate message here.
        return diff

    def _setup_side_panels(self, session, a_ed, b_ed):
        """Create / re-point / destroy the compare tab's side panels
        (micromap, hunk edge columns, overview) according to the config,
        and return (columns, overview, micromap_on).

        Called from refresh_compare on every re-compare, and from
        set_files BEFORE the texts are loaded there: every panel eats
        editor width, and a panel docked after the texts are in would
        re-wrap the already-loaded lines (with word-wrap on, at
        DIFFERENT widths per side -- the side-by-side pairing breaks).
        Creating them on the still-empty editors plus the immediate
        client-width equalization (see the callers) means the texts
        load into their final geometry.

        The panels live on the tab's OWN session -- two tabs' panels
        can never mix.
        """
        # The micromap (CudaText's per-editor mini-map) eats client
        # width INSIDE each editor; it must be on before the widths are
        # equalized.
        micromap_on = self.cfg.get('enable_micromap', False)
        if micromap_on:
            self._setup_micromap(a_ed, b_ed)

        # Hunk edge columns: two narrow custom-drawn columns, one at the
        # LEFT edge of each editor. Column A is Align=alLeft in the
        # grouping panel that parents the two editors; column B is an
        # Align=alRight child of the split bar, which is widened by the
        # column width so the column lands at the LEFT edge of editor 2.
        # Hosting column B inside the split bar keeps it OUT of the
        # panel's align chain, so the LCL splitter's drag target stays
        # editor 2 and the splitter drag keeps working. The editors
        # themselves are never modified. Each column draws a bracket
        # around every hunk's full visual footprint (text + compensating
        # gap band) -- see columns.py, which also runs the per-tab
        # layout guard that re-applies the split-bar widening and
        # repaints after size changes.
        columns = session.columns
        columns_on = self.cfg.get('enable_hunk_edges', True)
        if columns_on:
            if columns is None:
                columns = HunkColumns()
                columns.create(a_ed, b_ed)
                session.columns = columns
            else:
                # Re-point at the (possibly re-created) editors: sync
                # rebuilds the column controls when the split tree
                # changed, and re-checks the layout otherwise.
                columns.sync(a_ed, b_ed)
            # Colors come from the ACTIVE THEME (EdGutterBg /
            # EdGutterFont), so the columns match the editors' gutters
            # in every theme -- no plugin color option involved.
            columns.set_colors(*_ed_gutter_colors())
            columns.set_width(self.cfg.get('hunk_edges_width',
                                           COLUMNS_WIDTH_DEFAULT))
            # Clear the previous compare's records (hunks + gap bands),
            # exactly like the overview's clear_data() below: the paint
            # loop re-feeds the gaps DURING the compare while the hunk
            # records only land at the end, so the fresh compare must
            # start from empty data (columns.py paints the whole file
            # overview-style from these records).
            columns.clear_data()
        elif columns is not None:
            columns.destroy()
            session.columns = None
            columns = None

        # Overview: a custom paintbox added to the right side of the
        # editor's parent form. It shows a gap-aware mini-map of both
        # editors side-by-side (unlike the micromap, which doesn't
        # account for the gaps we insert for alignment).
        overview = session.overview
        overview_on = self.cfg.get('enable_overview', True)
        if overview_on:
            if overview is None:
                overview = PaintboxOverview()
                overview.create(a_ed, b_ed)
                session.overview = overview
            else:
                overview.a_ed = a_ed
                overview.b_ed = b_ed
            # Get the editor text background color from the UI theme so
            # the overview matches the editor (works with both light and
            # dark themes).
            try:
                ui_theme = ct.app_proc(ct.PROC_THEME_UI_DICT_GET, '')
                color_bg = ui_theme.get('EdTextBg', {}).get('color', 0xFFFFFF)
            except Exception:
                color_bg = 0xFFFFFF
            overview.set_colors(
                color_bg,
                self.cfg.get('color_deleted'),
                self.cfg.get('color_added'),
                self.cfg.get('color_changed'),
                self.cfg.get('color_gaps'),
                self.cfg.get('color_ignored_gap'))
            # Pass slider opacity options. Config stores opacity as
            # int 0..100; convert to float 0..1 for
            # PaintboxOverview.set_slider_options().
            # See overview.py for the three paint methods dispatched
            # based on these values (SOLID / CLEAR / BLENDED).
            overview.set_slider_options(
                opacity_enabled=self.cfg.get('enable_overview_slider_opacity', True),
                opacity=self.cfg.get('overview_slider_opacity', 40) / 100.0)
            overview.clear_data()
        elif overview is not None:
            overview.destroy()
            session.overview = None
            overview = None

        return columns, overview, micromap_on

    def refresh_compare(self, ed=None, show_dialog=None):
        """Unified refresh / re-compare entry point.

        Re-reads both sides of a compare tab, runs the configured diff
        algorithm, and (re)applies markers, gaps, overview and bookmarks.

        'ed' is any editor belonging to the compare tab. When omitted
        (plugin menu command / context-menu Recompare), uses the focused
        editor ct.ed.

        'show_dialog' controls whether the 'two sides are identical'
        dialog is shown. Automatic refreshes (on_start2, on_change_slow,
        on_state_ed wrap sync) pass False to avoid pestering the user;
        the no-arg menu command and other manual entry points default
        to True.

        With the native algorithms, the line-level diff runs in a
        background thread (the callback form of cudatext.diff_proc):
        this method does the whole setup -- reads the texts, clears
        the old markers, prepares the overview and the Differ --
        starts the engine call, locks both halves (EDACTION_LOCK busy
        placeholder + PROP_RO read-only) for the whole run when
        LOCK_EDITORS_WHILE_COMPARING is on, and returns at once so
        the UI stays responsive. The engine call returns a job
        handle, which the job keeps so it can be cancelled
        (diff_proc DIF_CANCEL) when the result will never be consumed
        (tab closed / app exiting). When the compare finishes, it
        calls back on the main thread and _on_native_diff_done
        finishes the compare: it paints the events
        (_paint_compare_events), sets the bookmarks, repaints the
        overview, shows the timing epilogue, and only then releases
        the editor lock / read-only state. Python algorithms run the
        paint phase inline, synchronously."""
        if ed is None:
            ed = ct.ed
        if show_dialog is None:
            show_dialog = True  # menu / no-arg call is a manual refresh
        if ed.get_prop(ct.PROP_EDITORS_LINKED):
            return
        tab_id = ed.get_prop(ct.PROP_TAB_ID)
        session = self._session_for(tab_id)
        if session is None:
            # Not a compare tab we manage. A restored-after-restart tab
            # that on_start2 did not rebuild (different CudaText session
            # group) lands here too -- nothing to refresh without a
            # session world of its own.
            return

        # One compare at a time per tab -- PER TAB: another tab's
        # running compare never blocks this one (each session has its
        # own job slot). While a background compare runs for THIS tab,
        # LOCK_EDITORS_WHILE_COMPARING keeps the halves locked +
        # read-only, so their texts cannot drift under the engine --
        # there is nothing a queued re-run would fix. Drop this request
        # (manual Recompare, on_change_slow auto-refresh, on_state_ed
        # wrap sync) with a status hint instead of starting a second
        # engine job or deferring work: the running compare finishes and
        # paints against its own kick-off snapshots; the NEXT refresh
        # -- the user can fire it any time after this one, or cancel
        # first -- picks up whatever the editors hold then.
        if session.job is not None:
            ct.msg_status(_('Differ: compare already running'))
            return

        # Load config FIRST so the profiling check below sees the current
        # value of enable_profiling. Without this, the first compare after
        # enabling profiling in the config dialog would not be profiled
        # (self.cfg would still have the old value).
        self.config()

        # Enable/disable profiling based on config. reset() clears any
        # stale data from a previous compare so the report only shows
        # this compare's timings.
        _profiling_was_enabled = Profiler.is_enabled()
        _profiling_enabled_here = False
        if not _profiling_was_enabled:
            try:
                _do_profile = self.cfg.get('enable_profiling', False)
            except Exception:
                _do_profile = False
            if _do_profile:
                enable_profiling(True)
                _profiling_enabled_here = True
        if Profiler.is_enabled():
            reset_profiling()

        # Wrap the entire compare in try/finally so the profiling report
        # is always printed — even if the compare crashes with an exception.
        #
        # _compare_start times the WHOLE refresh (algorithm + event
        # generation + painting + bookmarks + overview) using a plain
        # time.perf_counter(). This is INDEPENDENT of the Profiler: it
        # runs whether profiling is on or off, and is what gets shown on
        # the status bar after every compare so you always know how long
        # the last compare took.
        _compare_start = time.perf_counter()
        # Run the timing/profiling epilogue in the finally block?
        # Cleared when the compare continues in the background --
        # the epilogue then runs in _on_native_diff_done instead.
        _epilogue = True
        try:
            Profiler.start('refresh')

            a_ed = ct.Editor(ed.get_prop(ct.PROP_HANDLE_PRIMARY))
            b_ed = ct.Editor(ed.get_prop(ct.PROP_HANDLE_SECONDARY))

            # Side panels: micromap, hunk edge columns, overview.
            # Created / re-pointed / destroyed per the config BEFORE the
            # texts are read and any wrap counts are taken, so the
            # editors' client widths are FINAL for this compare.
            columns, overview, micromap_on = \
                self._setup_side_panels(session, a_ed, b_ed)
            tab_id_str = str(tab_id)

            # Equal client widths: the panels eat editor width
            # asymmetrically (column A takes its pixels from editor 1
            # only), so after any PANEL CHANGE move the split position
            # until the two halves' client (text-area) widths match --
            # BEFORE the compare reads wrap counts and paints gaps, so
            # word-wrap sees the final equal widths (with wrap on,
            # unequal widths wrap the same line differently in the two
            # halves and the side-by-side pairing breaks). The signature
            # (feature flags + width + the columns' attachment
            # generation, which bumps on a re-split) changes only when
            # the panel layout really changed -- a user splitter DRAG
            # does not change it, so an intentionally unequal layout
            # survives F5. See columns.equalize_split_clients().
            _panels_sig = (
                bool(self.cfg.get('enable_hunk_edges', True)),
                self.cfg.get('hunk_edges_width', COLUMNS_WIDTH_DEFAULT),
                bool(self.cfg.get('enable_overview', True)),
                micromap_on,
                columns.generation if columns is not None else 0,
            )
            if session.panels_sig != _panels_sig:
                Profiler.start('refresh:equalize_widths')
                equalize_split_clients(a_ed, b_ed)
                Profiler.stop('refresh:equalize_widths')
                session.panels_sig = _panels_sig

            Profiler.start('refresh:get_text')
            a_text_all = a_ed.get_text_all(ends=True)
            b_text_all = b_ed.get_text_all(ends=True)
            Profiler.stop('refresh:get_text')

            if a_text_all == b_text_all:
                Profiler.start('refresh:clear')
                self.clear(a_ed)
                self.clear(b_ed)
                Profiler.stop('refresh:clear')
                # Clear THIS tab's diff records (the session's Differ --
                # another tab's diffmap is never touched).
                self._ensure_correct_differ(session).diffmap = []
                # Identical sides: no hunks -> no brackets. Wipe the
                # columns' data and repaint them empty.
                if columns is not None:
                    columns.set_data([], a_ed.get_line_count(),
                                     b_ed.get_line_count())
                    columns.paint()
                if show_dialog:
                    t = _('The two sides are identical.')
                    ct.msg_box(t, ct.MB_OK)
                Profiler.stop('refresh')
                return

            # NOTE: Do NOT force word-wrap off here. The user may legitimately
            # want to compare files with wrap on (it makes long lines easier to
            # read). Instead, we detect the wrap state below and size the
            # inter-line gaps using the actual number of wrapped visual rows,
            # so the two sides stay visually aligned even when corresponding
            # lines wrap to different heights. See _get_wrap_counts and the
            # A_GAP/B_GAP/ALIGN handling in the loop below.
            # a_ed.set_prop(ct.PROP_WRAP, ct.WRAP_OFF)
            # b_ed.set_prop(ct.PROP_WRAP, ct.WRAP_OFF)

            Profiler.start('refresh:clear')
            self.clear(a_ed)
            self.clear(b_ed)
            Profiler.stop('refresh:clear')

            # config() was already called above (before the profiling check).
            # Don't call it again here.

            # Ensure the SESSION's Differ instance matches the configured
            # algorithm type (native vs Python). Swaps if the user changed
            # the algorithm in config since this tab's last compare -- the
            # swap is per session, other tabs keep their own differs.
            diff = self._ensure_correct_differ(session)

            # Branch on the differ type to compute the Python side's
            # line lists. The native Differ doesn't need a pre-split —
            # it takes raw texts at the compare() call site below.
            # Neither Differ holds any sequences between compares:
            #   - native:  compare(a_text, b_text)
            #   - python: compare(lines_a, lines_b)
            # In both cases the inputs enter as locals inside compare()
            # and are dropped when the generator returns — zero text
            # bytes persistent between compares.
            if not isinstance(diff, dfn.Differ):
                # Python differ: consumes line lists directly (the
                # pure-Python matchers take sequences), so the split
                # is genuinely needed here.
                Profiler.start('refresh:split_lines_safe')
                # SEQUENTIAL SPLIT — release each raw text the instant
                # its line list is built. split_lines_safe returns
                # INDEPENDENT string objects per line (CPython string
                # slices are COPIES of the char data, not views into
                # the source str's buffer), so lines_a owns its own
                # char data and a_text_all is redundant the moment
                # lines_a exists. Releasing a_text_all BEFORE splitting
                # b_text_all means the split-phase peak is 3× raw text
                # (b_text_all + lines_a + lines_b-being-built) instead
                # of 4× (a_text_all + b_text_all + lines_a +
                # lines_b-being-built). On a 33k-line / 10MB file
                # that's a ~3.5MB reduction in the OVERALL peak memory
                # during refresh_compare — the peak the user actually sees
                # when the diff runs.
                lines_a = split_lines_safe(a_text_all)
                del a_text_all
                lines_b = split_lines_safe(b_text_all)
                del b_text_all
                Profiler.stop('refresh:split_lines_safe')

            self.scroll.tab_id.add(tab_id)
            self.scroll.toggle(self.cfg.get('sync_scroll'))

            diff.withdetail = self.cfg.get('compare_with_details')
            # Use the resolved algorithm (native→Python mapping when
            # cudatext.diff_proc is missing). Do not pass a 'native_*'
            # name into the pure-Python Differ.
            _algo, _use_native, _fell_back = self._resolve_algorithm()
            diff.diff_algorithm = _algo
            if _fell_back:
                ct.msg_status(
                    _('Differ: native API not available — falling back to Python algo {} '
                      '(configured: {})').format(
                        _algo, self.cfg.get('diff_algorithm', 'native_histogram')))
            diff.beautify_alignment = self.cfg.get('beautify_alignment')
            # Ignore options -> diff_proc DIFF_IGN_* bitmask for the
            # native algorithms (applies to BOTH the line-level diff and
            # the char-level details). The pure-Python Differ simply
            # ignores this attribute -- Python algorithms compare
            # strictly by design.
            diff.ignore_flags = dfn.build_ignore_flags(self.cfg)

            # Detect word-wrap on either side. When wrap is on, gaps must be
            # sized by the actual number of visual rows on the opposite side
            # (not by logical line count), and matched line pairs that wrap to
            # different heights need an extra compensating gap.
            wrap_a = a_ed.get_prop(ct.PROP_WRAP)
            wrap_b = b_ed.get_prop(ct.PROP_WRAP)
            wrap_on = (wrap_a != ct.WRAP_OFF) or (wrap_b != ct.WRAP_OFF)
            if wrap_on:
                Profiler.start('refresh:wrap_counts')
                wrap_counts_a = self._get_wrap_counts(a_ed)
                wrap_counts_b = self._get_wrap_counts(b_ed)
                Profiler.stop('refresh:wrap_counts')
            else:
                wrap_counts_a = None
                wrap_counts_b = None
            # Char-cell heights are ALWAYS needed: wrap-on gap sizing uses
            # them, and the hunk edge columns' EOF band records (see the
            # A_GAP/B_GAP handlers) need the pixel height of a non-wrapped
            # compensating band (set_gap() computes n*cell_h internally,
            # so the same value is recorded alongside).
            Profiler.start('refresh:wrap_counts')
            __, line_h_a = a_ed.get_prop(ct.PROP_CELL_SIZE)
            __, line_h_b = b_ed.get_prop(ct.PROP_CELL_SIZE)
            Profiler.stop('refresh:wrap_counts')
            color_gaps = self.cfg.get('color_gaps')
            # Ignored-difference colors (WinMerge-style suppressed blank
            # lines + their compensating gaps) -- see 'ignored_color' /
            # 'ignored_gap_color'.
            color_ignored = self.cfg.get('color_ignored')
            color_ignored_gap = self.cfg.get('color_ignored_gap')

            # Fill the compare job -- the context the event/paint phase
            # needs. For the native algorithms the line-level diff runs
            # in the engine's background thread (the diff_proc callback
            # form): refresh_compare returns right after starting it, and
            # _on_native_diff_done finishes the compare on the main
            # thread when the engine calls back. Python algorithms
            # paint inline here, synchronously.
            job = _CompareJob()
            job.ed = ed
            job.session = session
            job.tab_id = tab_id
            job.tab_id_str = tab_id_str
            job.a_ed = a_ed
            job.b_ed = b_ed
            job.overview = overview
            job.columns = columns
            job.micromap_on = micromap_on
            job.wrap_on = wrap_on
            job.wrap_counts_a = wrap_counts_a
            job.wrap_counts_b = wrap_counts_b
            job.line_h_a = line_h_a
            job.line_h_b = line_h_b
            job.color_gaps = color_gaps
            job.color_ignored = color_ignored
            job.color_ignored_gap = color_ignored_gap
            job.show_dialog = show_dialog
            job.compare_start = _compare_start
            job.profiling_enabled_here = _profiling_enabled_here
            if isinstance(diff, dfn.Differ):
                job.a_text = a_text_all
                job.b_text = b_text_all
                del a_text_all, b_text_all
            else:
                job.lines_a = lines_a
                job.lines_b = lines_b
                del lines_a, lines_b

            if isinstance(diff, dfn.Differ):
                # functools.partial carries the job to the callback, so
                # the engine's completion knows WHICH compare finished.
                # (The refresh-while-running case was already handled at
                # the top of refresh_compare -- by this point no job exists
                # for this tab.)
                #
                # Profile the engine wait BEFORE starting the engine (so
                # the argument marshalling into the engine job is
                # included): the pair uses the SAME section names the
                # synchronous path books its engine time under
                # (compare:algorithm wrapping line_diff:native_engine),
                # so the report shows the real bottleneck at the top in
                # BOTH modes. Closed in _on_native_diff_done / _cancel_job
                # (every abandonment path); token-guarded, so a late
                # callback after a cancel cannot double-count.
                _async_pair = Profiler.start_async_pair(
                    'compare:algorithm', 'line_diff:native_engine')
                cb = functools.partial(self._on_native_diff_done, job)
                job_handle = dfn.start_async_line_diff(
                    job.a_text, job.b_text,
                    dfn.algo_id(diff.diff_algorithm),
                    diff.ignore_flags,
                    cb)
                if job_handle:
                    job.job_handle = job_handle
                    job.in_flight = True
                    job.profiler_async_token = _async_pair
                    session.job = job
                    # Editors are locked + read-only for the whole engine
                    # run (kick-off -> fully-rendered result / cancel):
                    # the paint lock shows the 'busy' placeholder in both
                    # halves and PROP_RO blocks typing. Released in
                    # _on_native_diff_done / _cancel_job.
                    self._lock_compare_editors(job)
                    # The timing/profiling epilogue runs in the
                    # completion callback, not in the finally below.
                    _epilogue = False
                    ct.msg_status(_('Differ: comparing in background...'))
                    return
                # Engine refused to start the background compare: report
                # and stop (no synchronous fallback -- it would freeze
                # the UI on exactly the big files the background form
                # exists for; the next refresh retries in the background).
                # Close the engine-wait pair now (elapsed covers the
                # failed start attempt; no report prints on this path, so
                # the rows are pure bookkeeping hygiene).
                Profiler.stop_async_pair(_async_pair)
                msg('diff_proc failed to start the background compare', level=1)
                _epilogue = False
                Profiler.stop('refresh')
                return

            # Synchronous compare: the paint phase runs inline. The
            # Differ's generator runs the engine itself while being
            # consumed (opcodes=None).
            self._paint_compare_events(job)
        finally:
            # Compare-time epilogue: status-bar timing message + profiling
            # report. Runs here for every synchronous completion path
            # (identical texts, all differences ignored, normal paint,
            # exception). The background mode clears _epilogue at
            # kick-off and runs the same epilogue in _on_native_diff_done
            # when the paint phase finishes on the main thread.
            if _epilogue:
                self._compare_epilogue(_compare_start,
                                       _profiling_enabled_here,
                                       tab_id)

    def _paint_compare_events(self, job, opcodes=None):
        """Consume the Differ's event generator and paint every event
        into both editor halves.

        Shared by both compare modes: the synchronous mode (Python
        algorithms) calls it directly from refresh_compare; the background
        mode calls it from _on_native_diff_done, passing the opcodes the
        engine produced on its background thread.

        Locking: this method takes NO lock of its own. When the whole-
        compare lock is held (job.editor_lock -- the background mode's
        kick-off lock, see _lock_compare_editors), the whole burst runs
        under it already and the single EDACTION_UNLOCK that releases it
        repaints everything in one pass. When it is not held (sync mode
        always; background mode with LOCK_EDITORS_WHILE_COMPARING off),
        the paint runs unlocked: every attr/gap/decor call repaints by
        itself, and the bookmark appends -- which do NOT repaint on
        their own -- are followed by an explicit EDACTION_UPDATE pair
        (see the end of this method).

        'job' carries everything the paint phase needs (editor halves,
        colors, wrap state, overview, dialog flag). 'opcodes' is the
        precomputed line-level opcode list for the background mode;
        None for the synchronous mode -- the Differ's generator then
        runs the engine itself while being consumed.
        """
        a_ed = job.a_ed
        b_ed = job.b_ed
        micromap_on = job.micromap_on
        overview = job.overview
        columns = job.columns
        wrap_on = job.wrap_on
        wrap_counts_a = job.wrap_counts_a
        wrap_counts_b = job.wrap_counts_b
        line_h_a = job.line_h_a
        line_h_b = job.line_h_b
        color_gaps = job.color_gaps
        color_ignored = job.color_ignored
        color_ignored_gap = job.color_ignored_gap
        show_dialog = job.show_dialog
        # The for loop below consumes events from diff.compare() (a
        # generator) and paints each event. Profiling the loop as a whole
        # captures both compare time (inside the generator) and paint time
        # (inside the loop body). The paint:* sub-sections break down the
        # paint time by operation type. The compare:* sub-sections (from
        # differ.py) break down the compare time by algorithm phase.
        #
        # Bookmarks are NOT set immediately in the loop. Instead, they
        # are collected into pending_bkm_a / pending_bkm_b lists and
        # appended in sorted order after the loop using BOOKMARK2_APPEND
        # (which is much faster than BOOKMARK2_SET but requires sorted
        # input and a manual repaint).
        #
        # Overview line states and gaps are also collected for the
        # paintbox overview (gap-aware mini-map) when enabled; the same
        # gap records (in visual rows) feed the hunk edge columns'
        # overview-style scaled brackets via _feed_gap() below.
        # Micromap line highlights are painted via attr(show_on_map=1)
        # when micromap is enabled.
        pending_bkm_a = []  # list of (line, nkind) for a_ed
        pending_bkm_b = []  # list of (line, nkind) for b_ed
        # Count of events that actually colorize something (line
        # marks, char highlights, line decors). Gaps and ALIGN events
        # are pure visual alignment and don't count. When this stays
        # 0, the ignore options made every difference invisible --
        # e.g. two files differing only in line endings with
        # 'ignore line endings' on, or digits-only differences with
        # 'ignore numbers' on -- and the user must be told the sides
        # are equal instead of staring at an uncolored compare tab.
        n_diff_events = 0

        def _feed_gap(side, after_line, rows, ignored=False):
            """Feed one compensating gap band, in VISUAL ROWS, to every
            consumer: the paintbox overview (for its gap fills) and the
            hunk edge columns (for their overview-style scaled brackets
            -- the columns use the same visual-row records to place each
            hunk's footprint; see columns.py). Defined once here so the
            10 feed sites below can never drift apart."""
            if overview is not None:
                overview.add_gap(side, after_line, rows, ignored)
            if columns is not None:
                columns.add_gap(side, after_line, rows, ignored)

        Profiler.start('refresh:compare_and_paint')
        # Both differs take their inputs as compare() parameters (no
        # set_seqs() call, no persistent storage on either Differ between
        # compares -- see differ_native.Differ and differ_python.Differ).
        # Native takes raw texts (the job's snapshots); Python takes line
        # lists (split in refresh_compare). In the background mode the native
        # call receives the engine's opcodes, so the generator skips its
        # own engine call and walks them directly. The Differ used is the
        # JOB'S SESSION's -- each compare tab paints from its own records.
        diff = self._session_diff(job.session)
        if isinstance(diff, dfn.Differ):
            compare_iter = diff.compare(job.a_text, job.b_text,
                                        opcodes=opcodes)
            # RELEASE the job's refs to the raw texts now that the
            # generator has its own (param) refs. The native generator
            # splits the texts into line lists inside compare() and then
            # `del`s its own param refs, so by the time the first event
            # is yielded, the only remaining refs to the raw texts are
            # the JOB's fields. Drop them here, BEFORE the for loop
            # starts driving the generator, so that when the generator's
            # `del a_text, b_text` executes during the first `next()`
            # call, the strings' refcount actually hits 0 and they are
            # freed instead of lingering through the whole paint loop.
            # (Python path already `del`d its texts in refresh_compare
            # right after the split_lines_safe call.)
            job.a_text = None
            job.b_text = None
        else:
            compare_iter = diff.compare(job.lines_a, job.lines_b)
            job.lines_a = None
            job.lines_b = None
        for d in compare_iter:
            diff_id, y = d[0], d[1]
            if diff_id == df.A_LINE_DEL:
                n_diff_events += 1
                pending_bkm_a.append((y, NKIND_DELETED))
                Profiler.start('paint:decor')
                self.set_decor(a_ed, y, DECOR_CHAR, self.cfg.get('color_deleted'))
                Profiler.stop('paint:decor')
                if micromap_on:
                    Profiler.start('paint:micromap')
                    self.set_attr(a_ed, y=y, bg=self.cfg.get('color_deleted'),
                                 mptag=1, map_only=1)
                    Profiler.stop('paint:micromap')
                if overview is not None:
                    overview.add_line_state('a', y, self.cfg.get('color_deleted'))
            elif diff_id == df.B_LINE_ADD:
                n_diff_events += 1
                pending_bkm_b.append((y, NKIND_ADDED))
                Profiler.start('paint:decor')
                self.set_decor(b_ed, y, DECOR_CHAR, self.cfg.get('color_added'))
                Profiler.stop('paint:decor')
                if micromap_on:
                    Profiler.start('paint:micromap')
                    self.set_attr(b_ed, y=y, bg=self.cfg.get('color_added'),
                                 mptag=1, map_only=1)
                    Profiler.stop('paint:micromap')
                if overview is not None:
                    overview.add_line_state('b', y, self.cfg.get('color_added'))
            elif diff_id == df.A_LINE_CHANGE:
                n_diff_events += 1
                pending_bkm_a.append((y, NKIND_CHANGED))
                if micromap_on:
                    Profiler.start('paint:micromap')
                    self.set_attr(a_ed, y=y, bg=self.cfg.get('color_changed'),
                                 mptag=1, map_only=1)
                    Profiler.stop('paint:micromap')
                if overview is not None:
                    overview.add_line_state('a', y, self.cfg.get('color_changed'))
            elif diff_id == df.B_LINE_CHANGE:
                n_diff_events += 1
                pending_bkm_b.append((y, NKIND_CHANGED))
                if micromap_on:
                    Profiler.start('paint:micromap')
                    self.set_attr(b_ed, y=y, bg=self.cfg.get('color_changed'),
                                 mptag=1, map_only=1)
                    Profiler.stop('paint:micromap')
                if overview is not None:
                    overview.add_line_state('b', y, self.cfg.get('color_changed'))
            elif diff_id == df.A_GAP:
                a_line_after, b_start, b_end = d[1], d[2], d[3]
                if wrap_on:
                    Profiler.start('paint:wrap_calc')
                    total_visual = self._sum_visual_rows(
                        wrap_counts_b, b_start, b_end)
                    Profiler.stop('paint:wrap_calc')
                    Profiler.start('paint:gap')
                    self._add_raw_gap(a_ed, a_line_after - 1,
                                      total_visual * line_h_a, color_gaps)
                    Profiler.stop('paint:gap')
                    # Gap appears BEFORE a_line_after (between lines
                    # a_line_after-1 and a_line_after)
                    _feed_gap('a', a_line_after, total_visual)
                else:
                    Profiler.start('paint:gap')
                    self.set_gap(a_ed, a_line_after, b_end - b_start)
                    Profiler.stop('paint:gap')
                    _feed_gap('a', a_line_after, b_end - b_start)
            elif diff_id == df.B_GAP:
                b_line_after, a_start, a_end = d[1], d[2], d[3]
                if wrap_on:
                    Profiler.start('paint:wrap_calc')
                    total_visual = self._sum_visual_rows(
                        wrap_counts_a, a_start, a_end)
                    Profiler.stop('paint:wrap_calc')
                    Profiler.start('paint:gap')
                    self._add_raw_gap(b_ed, b_line_after - 1,
                                      total_visual * line_h_b, color_gaps)
                    Profiler.stop('paint:gap')
                    _feed_gap('b', b_line_after, total_visual)
                else:
                    Profiler.start('paint:gap')
                    self.set_gap(b_ed, b_line_after, a_end - a_start)
                    Profiler.stop('paint:gap')
                    _feed_gap('b', b_line_after, a_end - a_start)
            elif diff_id == df.A_GAP_IGN:
                # Compensating gap for a suppressed all-blank hunk
                # (DIFF_IGN_BLANK_LINES): same geometry as A_GAP but
                # painted with the ignored-gap color and carrying the
                # dedicated IGN_GAP_TAG, so ignored regions look
                # distinct from regular alignment gaps. Pure visual
                # alignment — not a difference, so no n_diff_events.
                a_line_after, b_start, b_end = d[1], d[2], d[3]
                if wrap_on:
                    Profiler.start('paint:wrap_calc')
                    total_visual = self._sum_visual_rows(
                        wrap_counts_b, b_start, b_end)
                    Profiler.stop('paint:wrap_calc')
                    Profiler.start('paint:gap')
                    self._add_raw_gap(a_ed, a_line_after - 1,
                                      total_visual * line_h_a,
                                      color_ignored_gap, tag=IGN_GAP_TAG)
                    Profiler.stop('paint:gap')
                    _feed_gap('a', a_line_after, total_visual, ignored=True)
                else:
                    Profiler.start('paint:gap')
                    self.set_gap(a_ed, a_line_after, b_end - b_start,
                                 color=color_ignored_gap, tag=IGN_GAP_TAG)
                    Profiler.stop('paint:gap')
                    _feed_gap('a', a_line_after, b_end - b_start,
                              ignored=True)
            elif diff_id == df.B_GAP_IGN:
                b_line_after, a_start, a_end = d[1], d[2], d[3]
                if wrap_on:
                    Profiler.start('paint:wrap_calc')
                    total_visual = self._sum_visual_rows(
                        wrap_counts_a, a_start, a_end)
                    Profiler.stop('paint:wrap_calc')
                    Profiler.start('paint:gap')
                    self._add_raw_gap(b_ed, b_line_after - 1,
                                      total_visual * line_h_b,
                                      color_ignored_gap, tag=IGN_GAP_TAG)
                    Profiler.stop('paint:gap')
                    _feed_gap('b', b_line_after, total_visual, ignored=True)
                else:
                    Profiler.start('paint:gap')
                    self.set_gap(b_ed, b_line_after, a_end - a_start,
                                 color=color_ignored_gap, tag=IGN_GAP_TAG)
                    Profiler.stop('paint:gap')
                    _feed_gap('b', b_line_after, a_end - a_start,
                              ignored=True)
            elif diff_id == df.A_LINE_IGN:
                # Line of a suppressed all-blank hunk: painted with
                # the ignored color, but NOT a difference — no
                # bookmark, no diffmap entry, not counted in
                # n_diff_events (a file differing only in blank
                # lines still reports "No differences found").
                Profiler.start('paint:decor')
                self.set_decor(a_ed, y, DECOR_CHAR, color_ignored)
                Profiler.stop('paint:decor')
                if micromap_on:
                    Profiler.start('paint:micromap')
                    self.set_attr(a_ed, y=y, bg=color_ignored,
                                 mptag=1, map_only=1)
                    Profiler.stop('paint:micromap')
                if overview is not None:
                    overview.add_line_state('a', y, color_ignored)
            elif diff_id == df.B_LINE_IGN:
                Profiler.start('paint:decor')
                self.set_decor(b_ed, y, DECOR_CHAR, color_ignored)
                Profiler.stop('paint:decor')
                if micromap_on:
                    Profiler.start('paint:micromap')
                    self.set_attr(b_ed, y=y, bg=color_ignored,
                                 mptag=1, map_only=1)
                    Profiler.stop('paint:micromap')
                if overview is not None:
                    overview.add_line_state('b', y, color_ignored)
            elif diff_id == df.ALIGN:
                if wrap_on:
                    a_line, b_line = d[1], d[2]
                    Profiler.start('paint:wrap_calc')
                    va = self._visual_rows(wrap_counts_a, a_line)
                    vb = self._visual_rows(wrap_counts_b, b_line)
                    Profiler.stop('paint:wrap_calc')
                    Profiler.start('paint:gap')
                    if va > vb:
                        diff_rows = va - vb
                        align_px = diff_rows * line_h_b
                        self._add_raw_gap(b_ed, b_line,
                                          align_px, color_gaps)
                        # _add_raw_gap inserts AFTER b_line (between
                        # b_line and b_line+1), so record as
                        # after_line = b_line + 1 (gap appears
                        # before line b_line+1 in paint order).
                        _feed_gap('b', b_line + 1, diff_rows)
                    elif vb > va:
                        diff_rows = vb - va
                        align_px = diff_rows * line_h_a
                        self._add_raw_gap(a_ed, a_line,
                                          align_px, color_gaps)
                        # Same: gap is after a_line, so record
                        # as after_line = a_line + 1.
                        _feed_gap('a', a_line + 1, diff_rows)
                    Profiler.stop('paint:gap')
            elif diff_id == df.A_SYMBOL_DEL:
                n_diff_events += 1
                Profiler.start('paint:attr')
                self.set_attr(a_ed, d[2], y, d[3], self.cfg.get('color_deleted'))
                Profiler.stop('paint:attr')
                if overview is not None:
                    overview.add_line_state('a', y, self.cfg.get('color_deleted'))
            elif diff_id == df.B_SYMBOL_ADD:
                n_diff_events += 1
                Profiler.start('paint:attr')
                self.set_attr(b_ed, d[2], y, d[3], self.cfg.get('color_added'))
                Profiler.stop('paint:attr')
                if overview is not None:
                    overview.add_line_state('b', y, self.cfg.get('color_added'))
            elif diff_id == df.A_DECOR_YELLOW:
                n_diff_events += 1
                Profiler.start('paint:decor')
                self.set_decor(a_ed, y, DECOR_CHAR, self.cfg.get('color_changed'))
                Profiler.stop('paint:decor')
                if overview is not None:
                    overview.add_line_state('a', y, self.cfg.get('color_changed'))
            elif diff_id == df.B_DECOR_YELLOW:
                n_diff_events += 1
                Profiler.start('paint:decor')
                self.set_decor(b_ed, y, DECOR_CHAR, self.cfg.get('color_changed'))
                Profiler.stop('paint:decor')
                if overview is not None:
                    overview.add_line_state('b', y, self.cfg.get('color_changed'))
            elif diff_id == df.A_DECOR_RED:
                n_diff_events += 1
                Profiler.start('paint:decor')
                self.set_decor(a_ed, y, DECOR_CHAR, self.cfg.get('color_deleted'))
                Profiler.stop('paint:decor')
                if overview is not None:
                    overview.add_line_state('a', y, self.cfg.get('color_deleted'))
            elif diff_id == df.B_DECOR_GREEN:
                n_diff_events += 1
                Profiler.start('paint:decor')
                self.set_decor(b_ed, y, DECOR_CHAR, self.cfg.get('color_added'))
                Profiler.stop('paint:decor')
                if overview is not None:
                    overview.add_line_state('b', y, self.cfg.get('color_added'))
        Profiler.stop('refresh:compare_and_paint')

        if n_diff_events == 0:
            # Nothing to colorize: every difference was ignored by
            # the current ignore options. Mirror the raw-identical
            # early path above: clear the diffmap, skip bookmarks and
            # overview (there is nothing to show), and tell the user
            # -- the compare looks 'empty' otherwise and it is not
            # obvious whether the plugin even ran.
            # Same convention as the raw-identical early path in
            # refresh_compare: the dialog only appears for the initial
            # compare and manual refresh; automatic refreshes
            # (on_change_slow / on_state_ed wrap sync) stay silent to
            # avoid pestering the user. Clears THIS session's diffmap.
            diff.diffmap = []
            # No real differences -> no brackets. Wipe the columns'
            # data and repaint them empty.
            if columns is not None:
                columns.set_data([], a_ed.get_line_count(),
                                 b_ed.get_line_count())
                columns.paint()
            Profiler.stop('refresh')
            if show_dialog:
                ct.msg_box(
                    _('No differences found (with current ignore options).'),
                    ct.MB_OK)
            return

        # Hunk edge columns: feed the fresh compare's hunk records (the
        # diffmap is final at this point -- the paint loop has consumed
        # the whole compare generator; the gap bands were already fed
        # DURING the loop by _feed_gap) and the wrap counts (the same
        # feed the overview gets), then paint ONCE: every hunk edge,
        # file start to end, at 1:1 pixels into the pre-painted strips,
        # and show the current window. Scrolling never repaints the
        # edges -- it only copies the strip window, synchronously in
        # on_scroll (see columns.py).
        # Gated by the enable option (the columns object is None when
        # disabled -- see refresh_compare). See columns.HunkColumns for
        # the strip geometry.
        if columns is not None:
            Profiler.start('paint:hunk_edges')
            columns.set_data(diff.diffmap, a_ed.get_line_count(),
                             b_ed.get_line_count())
            if wrap_on:
                columns.set_wrap_counts(wrap_counts_a, wrap_counts_b)
            else:
                columns.set_wrap_counts(None, None)
            columns.paint()
            Profiler.stop('paint:hunk_edges')

        # Append all collected bookmarks in sorted order using
        # BOOKMARK2_APPEND (much faster than BOOKMARK2_SET — skips
        # duplicate search, sorting, event firing, and repainting).
        # BOOKMARK2_APPEND requires bookmarks to be added in ascending
        # line order, so we sort first.
        Profiler.start('paint:bookmark')
        pending_bkm_a.sort()
        pending_bkm_b.sort()
        for row, nk in pending_bkm_a:
            a_ed.bookmark(ct.BOOKMARK2_APPEND, row,
                          nkind=nk, text='', auto_del=True,
                          show=False, tag=DIFF_TAG)
        for row, nk in pending_bkm_b:
            b_ed.bookmark(ct.BOOKMARK2_APPEND, row,
                          nkind=nk, text='', auto_del=True,
                          show=False, tag=DIFF_TAG)
        Profiler.stop('paint:bookmark')

        # BOOKMARK2_APPEND doesn't repaint on its own. When the halves
        # are NOT already locked for the whole compare (job.editor_lock
        # -- sync mode always; background mode with
        # LOCK_EDITORS_WHILE_COMPARING off), force the repaint of both
        # so the new bookmark icons show up in the gutter. When they
        # ARE locked, skip it: the EDACTION_UNLOCK that releases the
        # compare-level lock right after this paint invalidates both
        # editors and repaints everything in one pass -- a forced
        # EDACTION_UPDATE under the lock would be redundant, and on
        # Windows (synchronous Repaint) would paint the 'busy'
        # placeholder screen mid-lock.
        outer_lock = job.editor_lock or {}
        if 'a' not in outer_lock:
            a_ed.action(ct.EDACTION_UPDATE)
        if 'b' not in outer_lock:
            b_ed.action(ct.EDACTION_UPDATE)

        # Repaint the overview with the collected line states and gaps.
        # repaint_static() rebuilds the static bitmap, then paint()
        # copies it + draws the cursor marker.
        if overview is not None:
            Profiler.start('paint:overview')
            overview.set_line_counts(a_ed.get_line_count(), b_ed.get_line_count())
            # Pass wrap counts so the overview can compute wrap-aware
            # visual heights. Without this, each line is counted as 1
            # visual row, causing desync when wrapping is on (lines
            # that wrap to 2+ rows have more visual height than 1).
            if wrap_on:
                overview.set_wrap_counts(wrap_counts_a, wrap_counts_b)
            else:
                overview.set_wrap_counts(None, None)
            overview.repaint_static()
            Profiler.stop('paint:overview')

        Profiler.stop('refresh')

    def _on_native_diff_done(self, job, opcodes):
        """diff_proc completion callback for a background line-level
        compare (native algorithms).

        The engine invokes this on the main thread when its background
        thread finishes, passing one argument: the opcode list -- the
        same list the synchronous diff_proc form returns -- or None when
        the compare failed. The callback arrives through the
        functools.partial(self._on_native_diff_done, job) created at
        kick-off, so the job context travels with it. A compare cancelled
        through diff_proc(DIF_CANCEL) never reaches this callback at all
        (the engine drops the result instead), so normally only completed
        compares arrive here.

        The job is validated against the live state before painting:
        when the compare tab was closed, CudaText is exiting, or the
        configured algorithm switched to a Python one, the result is
        discarded (the Python case re-runs the refresh with the new
        algorithm). The engine's own texts cannot have drifted: while
        the engine ran, LOCK_EDITORS_WHILE_COMPARING kept both halves
        read-only, and a refresh arriving mid-run was dropped at the top
        of refresh_compare -- so what the engine produced is what the
        editors still hold, and it is painted as-is.

        Whatever the outcome, the kick-off editor lock / read-only state
        is released in a finally block (idempotent -- _cancel_job already
        released cancelled jobs): the halves become editable again only
        here, after the result is fully rendered.
        """
        # This job is finished -- free the session's job slot first of all.
        if job.session is not None and job.session.job is job:
            job.session.job = None

        # Close the engine-wait profiling pair FIRST, before anything
        # else: its elapsed (kick-off -> now) is booked to
        # line_diff:native_engine and deducted from 'refresh' BEFORE the
        # paint sections below open, and it must close on EVERY outcome
        # (stale / exiting / closed tab / algo-switch re-run / engine
        # error / normal paint), not just the happy path. Token-guarded:
        # after a cancel already closed it (or a newer kick-off's reset
        # wiped the token) this is a no-op, so a late callback cannot
        # double-count the engine wait.
        Profiler.stop_async_pair(job.profiler_async_token)
        job.profiler_async_token = None

        try:
            if job.stale:
                return
            if self._app_exiting:
                return
            # Compare tab closed while the engine was running?
            if not self._is_compare_tab(job.tab_id):
                return

            try:
                if not isinstance(self._session_diff(job.session), dfn.Differ):
                    # Algorithm switched to a Python one while the engine
                    # was running: the session's Differ cannot paint native
                    # opcodes. Release this job's lock BEFORE the re-run
                    # so the new kick-off's lock does not stack on it,
                    # then re-run the refresh with the new algorithm.
                    self._release_compare_editors(job)
                    self.refresh_compare(job.ed, show_dialog=job.show_dialog)
                    return

                if opcodes is None:
                    # Engine error (CudaText logs it to the console):
                    # mirror the synchronous path's defensive fallback and
                    # paint one big REPLACE covering both texts, so the
                    # compare view still shows something sensible.
                    opcodes = [
                        ('replace', 0, len(split_lines_safe(job.a_text)),
                         0, len(split_lines_safe(job.b_text)))]

                # Paint the result. The timing epilogue (status-bar message
                # + profiling report) covers the WHOLE compare, from
                # kick-off (job.compare_start) to paint done -- the wall
                # time the user actually waited.
                try:
                    self._paint_compare_events(job, opcodes)
                finally:
                    self._compare_epilogue(job.compare_start,
                                           job.profiling_enabled_here,
                                           job.tab_id)
            finally:
                # Compare finished and everything is rendered: make the
                # halves editable again / drop the busy placeholder.
                # Idempotent, so the stale/exiting/closed/re-run paths
                # above (and _cancel_job for cancelled jobs) are all
                # covered by this single call.
                self._release_compare_editors(job)
        except Exception:
            # Never let an exception escape into the engine's callback
            # dispatcher: print the traceback and leave the tab in its
            # cleared state -- the next refresh (manual or automatic)
            # re-applies the markers.
            import traceback
            traceback.print_exc()

    def _compare_epilogue(self, compare_start, profiling_enabled_here, tab_id=None):
        """Show the total compare time on the status bar and print the
        profiling report. Runs for the synchronous mode (from
        refresh_compare's finally) and for the background mode (from
        _on_native_diff_done). 'compare_start' is the kick-off time, so
        the reported duration covers the whole compare, including the
        background engine phase -- the wall time the user waited.

        'tab_id' identifies the compare tab the report is about; the
        profiling report header names what it compares -- per side the
        original file's PATH when it is a file on disk, otherwise the
        original tab's title (untitled tabs have no path). See
        _compared_names_for_report."""
        _compare_elapsed = time.perf_counter() - compare_start
        if _compare_elapsed < 1.0:
            ct.msg_status(_('Differ: compared in {:.0f}ms').format(
                _compare_elapsed * 1000.0))
        elif _compare_elapsed < 60.0:
            ct.msg_status(_('Differ: compared in {:.1f}s').format(
                _compare_elapsed))
        else:
            _mins = int(_compare_elapsed // 60)
            _secs = _compare_elapsed - _mins * 60
            ct.msg_status(_('Differ: compared in {}m {:.0f}s').format(
                _mins, _secs))

        # Print the profiling report -- even if the compare crashed with
        # an exception. This shows WHERE the time was spent (or where it
        # crashed), also on big files. The header names the compared
        # files: per side the original's PATH when it is a file on disk,
        # otherwise the original tab's title.
        if profiling_enabled_here:
            profiling_report(files=self._compared_names_for_report(tab_id))
            enable_profiling(False)

    def _compared_names_for_report(self, tab_id):
        """Return [('Left', name), ('Right', name)] describing what the
        given compare tab compares -- printed in the profiling report
        header (see _compare_epilogue).

        Per side, first match wins:
        1. The LIVE original tab (looked up by its PROP_TAB_ID from the
           persisted state): its file path (PROP_FN) when it is a file
           on disk -- a real path; otherwise its tab title
           (PROP_TAB_TITLE) -- untitled tabs have no path. Live lookup
           keeps the names correct after renames / Save-As done since
           the compare was created.
        2. The display name captured at compare time (the persisted
           state's primary/secondary_orig_name: path for files, title
           for untitled) -- used when the original tab is already
           closed.
        3. '(unknown)' when even that is missing (only reachable for a
           state file written by an older/edited version -- set_files
           always registers both names).

        Returns [('Left', '(unknown)'), ('Right', '(unknown)')] for a
        tab_id that resolves to no state entry at all."""
        orig_a_id, orig_b_id = (None, None)
        entry = None
        if tab_id is not None:
            orig_a_id, orig_b_id = self._get_orig_tab_ids(tab_id)
            # Read the entry from the tab's OWN persisted session group
            # (state_key carried by its session; fallback to the current
            # group for tab ids without one).
            tab_session = self._session_for(tab_id)
            state_key = (tab_session.state_key if tab_session is not None
                         else self._current_session_key)
            state = self._load_state()
            group = state['sessions'].get(state_key, {})
            entry = group.get(str(tab_id))
        if not isinstance(entry, dict):
            entry = {}
        names = []
        for orig_id, fallback, label in (
                (orig_a_id, entry.get('primary_orig_name', ''), 'Left'),
                (orig_b_id, entry.get('secondary_orig_name', ''), 'Right')):
            name = ''
            if orig_id is not None:
                # Find the live original tab; prefer its file path,
                # else its tab title.
                for h in ct.ed_handles():
                    e = ct.Editor(h)
                    if str(e.get_prop(ct.PROP_TAB_ID)) == str(orig_id):
                        fn = e.get_prop(ct.PROP_FN, '')
                        if fn:
                            name = fn
                        else:
                            name = e.get_prop(ct.PROP_TAB_TITLE) or ''
                        break
            if not name and fallback:
                # Original tab closed: the name captured at compare time
                # (path for a real file, title for an untitled tab).
                name = fallback
            if not name:
                name = '(unknown)'
            names.append((label, name))
        return names

    def set_attr(self, e, x=0, y=0, nlen=0, bg=0, mptag=-1, map_only=0):
        """Add a colored attribute (background highlight) on editor e.

        Used for char-level text highlights (e.g., highlighting the
        changed characters within a modified line). The overview panel
        is handled separately by PaintboxOverview, not by this method.

        Args:
            e:        Editor instance.
            x:        Column (0-based). Default 0 (start of line).
            y:        Line number (0-based). Required.
            nlen:     Number of characters to highlight. Default 0.
            bg:       Background color (int, e.g. 0xFF0000 for red).
            mptag:    Micromap column tag. -1 = don't show on micromap
                      (default). 1..127 = show on micromap column with
                      that tag. Not used for the overview panel.
            map_only: 0 = text area only (default), 1 = micromap only,
                      2 = both text area and micromap.
        """
        e.attr(ct.MARKERS_ADD, DIFF_TAG,
               x,
               y,
               nlen,
               color_bg=bg,
               show_on_map=mptag,
               map_only=map_only
               )

    def set_gap(self, e, row, n=1, color=None, tag=None):
        """Add a gap of n line-heights after 'row' on editor e. Used to
        compensate for inserted/deleted lines on the opposite side.
        color/tag override the regular gap look (cfg 'color_gaps' /
        DIFF_TAG) — ignored-difference gaps pass the ignored color and
        IGN_GAP_TAG so they are visually distinct (WinMerge-style)."""
        __, h = e.get_prop(ct.PROP_CELL_SIZE)
        h_size = h * n
        e.gap(ct.GAP_ADD, row-1, 0,
              tag=DIFF_TAG if tag is None else tag,
              size=h_size,
              color=self.cfg.get('color_gaps') if color is None else color
              )

    def _add_raw_gap(self, e, line_index, pixel_size, color, tag=None,
                     on_top=False):
        """Add a gap at the given line index with an explicit pixel size.
        `line_index` follows the e.gap() convention: the gap is inserted
        between `line_index` and `line_index+1` (i.e. after `line_index`).
        Use -1 for a gap before the first line. Compared to set_gap(), this
        takes an explicit pixel size instead of computing n*line_height,
        which is needed when wrap is on and the gap must match the actual
        number of wrapped visual rows on the opposite side.
        tag defaults to DIFF_TAG; ignored-difference gaps pass IGN_GAP_TAG.
        on_top=True asks CudaText to insert the gap BEFORE all gaps
        already sitting at the same line index (see GAP_ADD's on_top
        param) instead of after them -- currently unused by callers,
        kept as a documented passthrough of the GAP_ADD capability."""
        e.gap(ct.GAP_ADD, line_index, 0,
              tag=DIFF_TAG if tag is None else tag,
              size=pixel_size,
              color=color,
              on_top=on_top
              )

    def _get_wrap_counts(self, ed):
        """Return a list where wrap_counts[i] = number of visual rows that
        line i occupies on screen. Uses Editor.get_wrapinfo() to query the
        editor's current word-wrap state. For a non-wrapped editor every
        line has 1 visual row. The returned list is indexed by logical line
        index (0-based) and has the same length as the editor's line count.
        Used to size inter-line gaps correctly when word-wrap is on: a gap
        that compensates for N missing logical lines must actually be
        (sum of those lines' visual rows) * line_height pixels tall,
        otherwise the two compare sides drift apart visually."""
        line_count = ed.get_line_count()
        if line_count <= 0:
            return []
        counts = [1] * line_count
        # Force CudaText to refresh its internal WrapInfo structure before
        # we query it. After text changes CudaText usually detects the
        # change automatically, but not always -- EDACTION_UPDATE with
        # param1="1" forces it.
        ed.action(ct.EDACTION_UPDATE, "1")
        try:
            info = ed.get_wrapinfo()
        except Exception:
            return counts
        if not info:
            return counts
        temp = [0] * line_count
        for item in info:
            if isinstance(item, dict):
                line = item.get('line', -1)
            else:
                continue
            if 0 <= line < line_count:
                temp[line] += 1
        for i in range(line_count):
            if temp[i] > 0:
                counts[i] = temp[i]
        return counts

    @staticmethod
    def _visual_rows(wrap_counts, line_index):
        """Visual row count for a single line, with bounds-safe fallback."""
        if wrap_counts is None:
            return 1
        if 0 <= line_index < len(wrap_counts):
            return wrap_counts[line_index]
        return 1

    @staticmethod
    def _sum_visual_rows(wrap_counts, line_start, line_end):
        """Total visual rows for lines [line_start, line_end). Used to size
        a gap that compensates for a whole block of missing lines."""
        if wrap_counts is None:
            return max(0, line_end - line_start)
        total = 0
        for i in range(line_start, line_end):
            if 0 <= i < len(wrap_counts):
                total += wrap_counts[i]
            else:
                total += 1
        return total

    def set_decor(self, e, row, text, color):
        """Set a line decorator (margin symbol) on editor e at row.
        Shows a colored DECOR_CHAR in the left margin to mark changed/added/
        deleted lines."""
        # API 1.0.485+: "bold" and "italic" params were removed from
        # decor(); the styles are set via style="b" / "i" instead.
        e.decor(ct.DECOR_SET, row, DIFF_TAG, text, color, style="b")

    def clear(self, e):
        """Remove all diff markers, gaps, decorators, and bookmarks tagged
        with DIFF_TAG from editor e. Called before re-applying a fresh diff."""
        if e is None:
            return
        e.attr(ct.MARKERS_DELETE_BY_TAG, DIFF_TAG)
        e.gap(ct.GAP_DELETE_ALL, 0, 0)
        e.decor(ct.DECOR_DELETE_BY_TAG, tag=DIFF_TAG)
        e.bookmark(ct.BOOKMARK2_DELETE_BY_TAG, 0, tag=DIFF_TAG)

    def config(self):
        """Reload config from disk if the JSON file or a theme has changed.
        Caches the result in self.cfg to avoid repeated disk reads."""
        opt_time = os.path.getmtime(JSONPATH) if os.path.exists(JSONPATH) else 0
        theme_name = ct.app_proc(ct.PROC_THEME_SYNTAX_GET, '')
        ui_theme_name = _ui_theme_name()
        if self.cfg.get('opt_time') == opt_time and \
           self.cfg.get('theme_name') == theme_name and \
           self.cfg.get('ui_theme_name') == ui_theme_name:
            return
        self.cfg = self.get_config()
        # Keep the runtime on_key subscription in step with the (possibly
        # changed) enable_keyboard_capture setting -- takes effect at
        # once, without a restart.
        self._sync_on_key_subscription()
        # (No menu re-sync needed anymore: the diff-tab context menu is
        # rebuilt from the settings file by tabmenu_init on every
        # right-click, so it always mirrors the current values.)

    def _setup_micromap(self, a_ed, b_ed):
        """Set up the micromap on both split editors when enable_micromap
        is on. Per editor:

        1. Delete default micromap columns:
           - 0 (line states) — not needed, Differ uses its own colors
           - 2 (selections) — disabled because we only want to see
             compare changes, not selection highlights
           Column 1 (bookmarks) is KEPT because it also shows the cursor
           position (like a usual scrollbar), which is useful for
           navigation. We paint diff-colored line highlights on column 1
           via attr(show_on_map=1).
        2. Enable PROP_MICROMAP so the micromap is visible.
        3. Set PROP_MICROMAP_AT_LEFT:
           - Left editor (a_ed): False (default) — micromap on the right
             side, facing the right editor.
           - Right editor (b_ed): True — micromap on the left side,
             facing the left editor.
           This way both micromaps are visible between the two editors,
           in the split gutter area.

        When enable_micromap is off, does nothing.
        """
        try:
            for e in (a_ed, b_ed):
                e.micromap(ct.MICROMAP_DELETE, 0)
                e.micromap(ct.MICROMAP_DELETE, 2)
                e.set_prop(ct.PROP_MICROMAP, True)
            # Place the micromap on the side that faces the other editor:
            # - a_ed (left): micromap on the right (default, PROP_MICROMAP_AT_LEFT=False)
            # - b_ed (right): micromap on the left (PROP_MICROMAP_AT_LEFT=True)
            a_ed.set_prop(ct.PROP_MICROMAP_AT_LEFT, False)
            b_ed.set_prop(ct.PROP_MICROMAP_AT_LEFT, True)
        except Exception as ex:
            msg('failed to set up micromap: {}'.format(ex), level=1)

    @staticmethod
    def get_config():
        """Read all differ.* options from JSON + current theme, and return
        a config dict. Also registers bookmark kinds (NKIND_*) with their
        colors so CudaText can render them."""

        def new_nkind(val, color):
            ct.ed.bookmark(ct.BOOKMARK_SETUP, 0,
                           nkind=val,
                           ncolor=color,
                           text=''
                           )

        def get_theme():
            """Resolve the six compare colors from the 'color_theme'
            option (see the _COLOR_PRESETS / _detect_theme_type block at
            module level):

            - 'auto' -- preset of the detected theme family;
            - 'white'/'grey'/'black' -- that family's preset;
            - 'custom' -- the six differ.theme.*_color options, each
              option left EMPTY filled from the auto-detected preset
              (a half-configured custom theme never falls back to
              nothing).

            The ignored-difference colors of the grey/black presets (and
            of custom when their options are empty) resolve to the LIVE
            editor background, so ignored regions keep reading as "not
            a difference" whatever theme is active."""
            mode = get_opt('theme.color_theme', 'auto')
            if mode not in _COLOR_THEME_MODES:
                mode = 'auto'
            if mode in ('white', 'black', 'grey'):
                ttype = mode
            else:
                ttype = _detect_theme_type()
            preset = _preset_colors(ttype)
            th = {
                'color_theme': mode,
                'theme_type': ttype,
            }
            for key, cfg_key in _PRESET_CFG_KEYS.items():
                if mode == 'custom':
                    s = get_opt('theme.' + key + '_color', '')
                    th[cfg_key] = ctx.html_color_to_int(s) if s else preset[key]
                else:
                    th[cfg_key] = preset[key]
            return th

        t = get_theme()
        config = {
            'opt_time':
                os.path.getmtime(JSONPATH) if os.path.exists(JSONPATH) else 0,
            'theme_name':
                ct.app_proc(ct.PROC_THEME_SYNTAX_GET, ''),
            # Current UI theme name -- part of the config() cache key so a
            # UI theme switch re-resolves the auto/custom colors.
            'ui_theme_name':
                _ui_theme_name(),
            # --- theme ---
            'color_theme':
                t['color_theme'],
            'theme_type':
                t['theme_type'],
            'color_changed':
                t['color_changed'],
            'color_added':
                t['color_added'],
            'color_deleted':
                t['color_deleted'],
            'color_gaps':
                t['color_gaps'],
            'color_ignored':
                t['color_ignored'],
            'color_ignored_gap':
                t['color_ignored_gap'],
            # --- algorithm ---
            'diff_algorithm':
                get_opt('algorithm.diff_algorithm', 'native_histogram'),
            'compare_with_details':
                get_opt('algorithm.compare_with_details', True),
            'beautify_alignment':
                get_opt('algorithm.beautify_alignment', True),
            # --- ignore options (diff_proc DIFF_IGN_* flags; collected
            # into the bitmask for the native algorithms by
            # differ_native.build_ignore_flags -- see refresh_compare) ---
            'ignore_case':
                get_opt('ignoreopt.ignore_case', False),
            'ignore_whitespace':
                get_opt('ignoreopt.ignore_whitespace', False),
            'ignore_blank_lines':
                get_opt('ignoreopt.ignore_blank_lines', False),
            'ignore_eol':
                get_opt('ignoreopt.ignore_eol', False),
            'ignore_numbers':
                get_opt('ignoreopt.ignore_numbers', False),
            # --- advanced ---
            'sync_scroll':
                get_opt('advanced.sync_scroll', DEFAULT_SYNC_SCROLL == '1'),
            'enable_sync_caret':
                get_opt('advanced.enable_sync_caret', False),
            'enable_auto_refresh':
                get_opt('advanced.enable_auto_refresh', False),
            'enable_keyboard_capture':
                get_opt('advanced.enable_keyboard_capture', True),
            'diff_context':
                get_opt('advanced.diff_context', 3),
            'enable_profiling':
                get_opt('advanced.enable_profiling', False),
            # --- hunk edge columns (bracket columns at both edges) ---
            'enable_hunk_edges':
                get_opt('advanced.enable_hunk_edges', True),
            'hunk_edges_width':
                max(6, min(40, get_opt('advanced.hunk_edges_width',
                                       COLUMNS_WIDTH_DEFAULT))),
            # --- micromap ---
            'enable_micromap':
                get_opt('micromap.enable_micromap', False),
            'enable_overview':
                get_opt('micromap.enable_overview', True),
            # Overview slider opacity. Stored as int 0..100, passed
            # to PaintboxOverview as a float 0..1. See
            # overview.set_slider_options().
            'enable_overview_slider_opacity':
                get_opt('micromap.enable_overview_slider_opacity', True),
            'overview_slider_opacity':
                max(0, min(100, get_opt('micromap.overview_slider_opacity', 40))),
        }

        new_nkind(NKIND_DELETED, config.get('color_deleted'))
        new_nkind(NKIND_ADDED, config.get('color_added'))
        new_nkind(NKIND_CHANGED, config.get('color_changed'))

        return config

    @property
    def focused(self):
        """Return (focused_index, (a_ed, b_ed)) where focused_index is 0 if
        the primary (left) editor is focused, 1 if the secondary (right).
        """
        hndl_self = ct.ed.get_prop(ct.PROP_HANDLE_SELF)
        hndl_primary = ct.ed.get_prop(ct.PROP_HANDLE_PRIMARY)
        hndl_secondary = ct.ed.get_prop(ct.PROP_HANDLE_SECONDARY)
        eds = (ct.Editor(hndl_primary), ct.Editor(hndl_secondary))
        if hndl_self == hndl_primary:
            return 0, eds
        else:
            return 1, eds

    def jump(self, to_next=True):
        """Jump caret to the next (or previous) diff hunk. Wraps around at
        the end/start of the FOCUSED tab's own diffmap (the session's
        Differ -- never another tab's records).

        One-sided hunks (pure added/deleted lines, shown as a gap on the
        other side) put the caret AND THE FOCUS on the side that has the
        text: on the gap side the caret would have to sit on an unchanged
        line next to the gap, where it is not obvious which difference it
        belongs to and where the copy commands find no hunk."""
        session = self._focused_session()
        if session is None:
            return ct.msg_status(_('Differ: not a compare tab'))
        diff = self._session_diff(session)
        if not diff.diffmap:
            self.refresh_compare()
            diff = self._session_diff(session)
        cnt = len(diff.diffmap)
        if cnt == 0:
            return ct.msg_status(_("No differences were found"))
        fc, eds = self.focused

        i = None
        if fc == 0:
            p = 0 if to_next else 1
        else:
            p = 2 if to_next else 3
        y = eds[fc].get_carets()[0][1]
        line_cnt = eds[fc].get_line_count()

        if to_next:
            for n, dif in enumerate(diff.diffmap):
                # An empty range on the focused side (the gap side of a
                # one-sided hunk) can start AT line_cnt -- a gap after the
                # last line. With the plain line_cnt-1 clamp such a hunk
                # is skipped by the scan and the jump needlessly wraps
                # around (to an earlier hunk, or to this one with a
                # misleading wrap-around count).
                limit = line_cnt if dif[p] == dif[p+1] else line_cnt - 1
                df_y = dif[p] if dif[p] <= limit else limit
                if y < df_y:
                    i = n
                    break
        else: # to prev
            for n, dif in reversed(list(enumerate(diff.diffmap))):
                _y = y if dif[p] == dif[p-1] else y + 1 # adjust y for empty diff fragments
                if _y > dif[p]:
                    i = n
                    break

        if i is None:
            i = 0 if to_next else cnt - 1
        elif i >= cnt:
            i = 0
        elif i < 0:
            i = cnt - 1
        to = diff.diffmap[i]
        ct.msg_status(_("{} of {} difference").format(i+1, cnt))
        a_line_cnt = eds[0].get_line_count()
        b_line_cnt = eds[1].get_line_count()
        to0 = to[0] if to[0] <= a_line_cnt - 1 else a_line_cnt - 1
        to2 = to[2] if to[2] <= b_line_cnt - 1 else b_line_cnt - 1
        eds[0].set_caret(0, to0, id=ct.CARET_SET_ONE)
        eds[1].set_caret(0, to2, id=ct.CARET_SET_ONE)
        # WinMerge-like: when the hunk has no lines on the focused side
        # (the gap side of a one-sided hunk), the focused half's caret
        # sits on an unchanged line next to the gap -- move the focus to
        # the half that has the text, so the next Alt+Left/Right or
        # Ctrl+Alt+Left/Right acts on this hunk.
        s = fc * 2
        if to[s] == to[s+1]:
            self.set_focus_to_opposite_panel()

    def jump_next(self):
        """Jump to the next diff hunk."""
        self.jump()

    def jump_prev(self):
        """Jump to the previous diff hunk."""
        self.jump(False)

    def _find_hunk_at_caret(self, adjacent=False):
        """Return the FOCUSED tab's own diffmap entry [a0, a1, b0, b1] at
        the caret, or None (also None when the focus is not in a compare
        tab). Shared lookup for the hunk commands; the diffmap comes from
        the tab's session -- a second diff tab comparing in between can
        no longer swap the records under this lookup (that was the
        original cross-tab bug).

        Strict mode (adjacent=False): the caret line must lie INSIDE the
        hunk's range on the focused side. Used where the caret's own line
        must be a changed line of the hunk (copy_line, caret sync).

        Adjacent mode (adjacent=True): a one-sided hunk -- pure added or
        deleted lines, shown as a GAP on the focused side -- is also found
        when the caret sits on the line just above or just below that gap.
        Such a hunk has NO line on this side, so "the caret is at this
        difference" can only mean next to the gap. Used by the whole-hunk
        commands (copy, select_current), so they keep working right after
        a jump or with the caret parked next to a gap."""
        session = self._focused_session()
        if session is None:
            return None
        diff = self._session_diff(session)
        if not diff.diffmap:
            self.refresh_compare()
            diff = self._session_diff(session)
        fc, eds = self.focused
        p = fc * 2
        y = eds[fc].get_carets()[0][1]
        for dif in diff.diffmap:
            if dif[p] <= y < dif[p+1]:
                return dif
            if adjacent and dif[p] == dif[p+1] and y in (dif[p] - 1, dif[p]):
                return dif
        return None

    @property
    def get_current_change(self):
        """Return the FOCUSED tab's own diffmap entry [a0, a1, b0, b1]
        containing the caret in the focused editor, or None if the caret
        is not inside a diff hunk (or the focus is not in a compare tab).
        Strict containment on the focused side -- see _find_hunk_at_caret
        for the gap-adjacent variant used by the whole-hunk copy
        commands."""
        return self._find_hunk_at_caret(adjacent=False)

    def select_current(self):
        """Select the lines of the current diff hunk in both editors.
        One-sided hunks (a gap on one side) are found from the line next
        to their gap too, so the difference the caret is at is always
        the one selected."""
        cur_change = self._find_hunk_at_caret(adjacent=True)
        if not cur_change:
            return ct.msg_status(_('Differ: caret is not on a difference'))
        esc = self.cfg.get('enable_sync_caret', False)
        fc, eds = self.focused
        self.cfg['enable_sync_caret'] = False
        eds[0].set_caret(0, cur_change[0], 0, cur_change[1])
        eds[1].set_caret(0, cur_change[2], 0, cur_change[3])
        self.cfg['enable_sync_caret'] = esc

    def _compare_running_here(self, eds):
        """True while a background compare is in flight for the compare
        tab the given halves belong to (THIS tab's session job -- another
        tab's running compare never blocks editing here). Text-changing
        hunk commands (copy / copy_line) are refused then: with
        LOCK_EDITORS_WHILE_COMPARING the halves are read-only for the
        whole run, and the running compare would paint its kick-off
        snapshots -- any text edit now would end up misaligned. Also
        works with the constant off, where editing IS possible but the
        running compare would still paint stale snapshots."""
        if not eds:
            return False
        try:
            tab_id = eds[0].get_prop(ct.PROP_TAB_ID)
        except Exception:
            return False
        session = self._session_for(tab_id)
        return session is not None and session.job is not None

    def copy(self, to_right=True):
        """Copy the current diff hunk's text from left to right (or right to
        left), replacing the opposite side's text. Then refresh diff markers.

        The hunk is found gap-aware (see _find_hunk_at_caret): a one-sided
        hunk is also found from the line next to its gap, so copying works
        right after a jump. Copying the text side over the gap fills the
        gap in; copying the gap side's (empty) text over the other side
        deletes the difference."""
        fc, eds = self.focused
        if self._compare_running_here(eds):
            return ct.msg_status(_('Differ: cannot edit while compare is running'))
        current = self._find_hunk_at_caret(adjacent=True)
        if not current:
            return ct.msg_status(_('Differ: caret is not on a difference'))
        else:
            a0, a1, b0, b1 = current
        if to_right:
            text = eds[0].get_text_substr(0, a0, 0, a1)
            eds[1].delete(0, b0, 0, b1)
            if text:
                eds[1].insert(0, b0, text)
        else:
            text = eds[1].get_text_substr(0, b0, 0, b1)
            eds[0].delete(0, a0, 0, a1)
            if text:
                eds[0].insert(0, a0, text)
        eds[0].set_caret(0, a0)
        eds[1].set_caret(0, b0)
        self.refresh_compare()

    def copy_right(self):
        """Copy current hunk from left editor to right editor."""
        self.copy(True)

    def copy_left(self):
        """Copy current hunk from right editor to left editor."""
        self.copy(False)

    def copy_line(self, to_right=True):
        """Copy the caret's line(s) from left to right (or right to left),
        inserting at the caret line's SAME HORIZONTAL LEVEL on the other
        side. Within a hunk the k-th source line is visually aligned with
        the k-th target-hunk line (positional pairing -- the sdiff/WinMerge
        alignment), and source lines past the target hunk's end align with
        its gap, so they insert at the hunk's end. Copying from the FIRST
        line of a hunk, or into a one-sided hunk's gap, still inserts at
        the hunk start exactly like before. Unlike copy(), this works on
        the current caret line, not the whole hunk: the caret must be ON a
        changed line of the hunk (a gap has no line to copy -- jumping to
        a one-sided difference puts the caret on the changed line). With
        beautify_alignment the intra-hunk pairing can be anchored instead
        of positional; the diffmap-based formula below is then the closest
        line-index approximation."""
        fc, eds = self.focused
        if self._compare_running_here(eds):
            return ct.msg_status(_('Differ: cannot edit while compare is running'))
        current = self.get_current_change

        def get_src(ed: ct.Editor):
            """Return (text, first_line) of the caret's line(s): the single
            caret line, or the selection's whole-line range. ('', -1) for
            multi-caret -- no unambiguous line to copy then."""
            carets = ed.get_carets()
            if len(carets) != 1:
                return '', -1
            caret = carets[0]
            __, y1, __, y2 = caret
            if y2 == -1:
                return ed.get_text_line(y1) + '\n', y1
            else:
                return ''.join([ed.get_text_line(y)+'\n'
                                for y in range(y1, y2)]), y1

        if not current:
            return ct.msg_status(_('Differ: caret is not on a changed line'))
        else:
            a0, a1, b0, b1 = current
        if to_right:
            if fc == 1:
                return ct.msg_status(_('Differ: caret must be in the left editor to copy right'))
            text, y = get_src(eds[0])
            if text:
                # Same level: the caret line is the k-th line of the source
                # hunk, so it inserts before the k-th line of the target
                # hunk -- or, when k is past the target hunk's end (the
                # line is aligned with the target's gap), at the hunk's
                # end: b0 + min(k, b1-b0).
                ins = b0 + min(max(0, y - a0), b1 - b0)
                eds[1].insert(0, ins, text)
        else:
            if fc == 0:
                return ct.msg_status(_('Differ: caret must be in the right editor to copy left'))
            text, y = get_src(eds[1])
            if text:
                ins = a0 + min(max(0, y - b0), a1 - a0)
                eds[0].insert(0, ins, text)
        self.refresh_compare()

    def copy_line_right(self):
        """Copy caret line from left editor to right editor."""
        self.copy_line(True)

    def copy_line_left(self):
        """Copy caret line from right editor to left editor."""
        self.copy_line(False)

    @staticmethod
    def set_focus_to_opposite_panel():
        """Toggle focus between the two split editors."""
        ct.ed.cmd(ct_cmd.cmd_ToggleFocusSplitEditors)

    def sync_caret(self):
        """Mirror the caret position to the opposite editor. If the caret is
        inside a diff hunk, jump the opposite caret to the hunk's start.
        Otherwise, map the caret line through the FOCUSED tab's own
        diffmap to the corresponding line on the opposite side."""
        session = self._focused_session()
        if session is None:
            return
        diff = self._session_diff(session)
        if not diff.diffmap:
            return
        fc, eds = self.focused
        op = 0 if fc else 1
        x, y = eds[fc].get_carets()[0][:2]

        esc = self.cfg.get('enable_sync_caret', False)
        p = fc * 2
        for dif in diff.diffmap:
            if dif[p] <= y < dif[p+1]:
                self.cfg['enable_sync_caret'] = False
                eds[op].set_caret(0, dif[op*2])
                self.cfg['enable_sync_caret'] = esc
                return
        for dif in diff.diffmap:
            if y < dif[p]:
                self.cfg['enable_sync_caret'] = False
                eds[op].set_caret(x, dif[op*2]-dif[p]+y)
                self.cfg['enable_sync_caret'] = esc
                return

    def get_name(self, e):
        """Return a display name for editor e: filename for real files,
        or 'untitled:TITLE [TAB_ID]' for untitled tabs."""
        fn = e.get_prop(ct.PROP_FN, '')
        if fn:
            return fn
        else:
            return self.format_untitled(e)

    def tabmenu_editor_ok(self, e, disabled_fn):
        """Check if editor 'e' is a valid candidate for the 'Compare with tab'
        list. Returns False if the tab should be excluded (not linked, not
        text, or is the disabled/current tab).

        For untitled tabs, compares by tab ID (extracted from disabled_fn)
        instead of full name string, so title changes don't cause the
        current tab to appear in its own compare list."""
        if not e.get_prop(ct.PROP_EDITORS_LINKED):
            return False
        if e.get_prop(ct.PROP_KIND) != 'text':
            return False
        if bool(disabled_fn):
            if disabled_fn.startswith(U_PREFIX):
                # Untitled tab: compare by tab ID, not full name string.
                i = disabled_fn.rfind('[')
                j = disabled_fn.rfind(']')
                if i > 0 and j > i:
                    id_str = disabled_fn[i+1:j]
                    if id_str.isdigit() and str(e.get_prop(ct.PROP_TAB_ID)) == id_str:
                        return False
            else:
                # Titled tab: compare by filename.
                fn = e.get_prop(ct.PROP_FN, '')
                if fn and fn == disabled_fn:
                    return False
        return True

    def tabmenu_init(self, cur_ed: ct.Editor):
        """Build the right-click tab context menu: 'Compare with...',
        'Compare with focused tab', 'Compare with tab' (submenu of all
        open tabs), and 'Recompare'. Only shown for valid compare candidates."""
        cur_fn = self.get_name(cur_ed)
        path_focused = self.get_name(ct.ed)

        if self.menuid_sep is None:
            self.menuid_sep = ct.menu_proc('tab', ct.MENU_ADD,
                caption='-'
                )
            self.compare_menu = ct.menu_proc('tab', ct.MENU_ADD,
                caption=PLG_NAME
                )

        ct.menu_proc(self.compare_menu, ct.MENU_CLEAR)
        self.menuid_withfile = ct.menu_proc(self.compare_menu, ct.MENU_ADD,
            command='module=cuda_differ;cmd=tabmenu_chooser;',
            caption=_('Compare with...')
            )
        self.menuid_withfocused = ct.menu_proc(self.compare_menu, ct.MENU_ADD,
            command='module=cuda_differ;cmd=tabmenu_files;info='+cur_fn+'::'+path_focused+';',
            caption=_('Compare with focused tab')
            )
        self.menuid_withtab = ct.menu_proc(self.compare_menu, ct.MENU_ADD,
            caption=_('Compare with tab')
            )

        handles = ct.ed_handles()

        paths = []
        if len(handles) > 1:
            for h in handles:
                e = ct.Editor(h)
                if self.tabmenu_editor_ok(e, cur_fn):
                    path = self.get_name(e)
                    paths.append(path)

            if paths:
                ct.menu_proc(self.menuid_withtab, ct.MENU_CLEAR)

                # Add a "More tabs..." entry at the top that opens the
                # native CudaText dialog (which has a scrollbar) -- useful
                # when the popup menu is too long to fit on screen.
                ct.menu_proc(self.menuid_withtab, ct.MENU_ADD,
                    command='module=cuda_differ;cmd=tabmenu_chooser_tab;',
                    caption=_('More tabs...')
                    )

                for path in paths:
                    ct.menu_proc(self.menuid_withtab, ct.MENU_ADD,
                        command='module=cuda_differ;cmd=tabmenu_files;info='+cur_fn+'::'+path+';',
                        caption=collapse_filename(path)
                        )

        cur_ok = self.tabmenu_editor_ok(cur_ed, '')
        cur_is_focused = cur_ed.get_prop(ct.PROP_HANDLE_PRIMARY) == \
                         ct.ed.get_prop(ct.PROP_HANDLE_PRIMARY)

        ct.menu_proc(self.menuid_withtab, ct.MENU_SET_ENABLED, command=cur_ok and bool(paths))
        ct.menu_proc(self.menuid_withfile, ct.MENU_SET_ENABLED, command=cur_ok)
        # "Compare with focused tab" is disabled when:
        # - current tab is not a valid compare candidate (cur_ok)
        # - current tab IS the focused tab (nothing to compare with itself)
        # - focused tab is a Differ-managed compare tab (can't compare with a diff tab)
        focused_is_diff = self._is_compare_tab(ct.ed.get_prop(ct.PROP_TAB_ID))
        ct.menu_proc(self.menuid_withfocused, ct.MENU_SET_ENABLED,
            command=cur_ok and not cur_is_focused and not focused_is_diff)

        # Add a separator and "Recompare" entry at the end of the context
        # menu. Only enabled when the current tab is a compare tab managed
        # by Differ.
        ct.menu_proc(self.compare_menu, ct.MENU_ADD, caption='-')
        self.menuid_refresh = ct.menu_proc(self.compare_menu, ct.MENU_ADD,
            command='module=cuda_differ;cmd=tabmenu_refresh;',
            caption=_('Recompare')
            )
        is_compare = self._is_compare_tab(cur_ed.get_prop(ct.PROP_TAB_ID))
        ct.menu_proc(self.menuid_refresh, ct.MENU_SET_ENABLED,
            command=is_compare)

        # Separator + the ignore options (see _IGNORE_OPTS), right below
        # 'Recompare' -- but ONLY while a native algorithm is the effective
        # one (_visible_opts_meta explains why): the pure-Python algorithms
        # compare strictly, the flags do nothing there, so the items are
        # hidden instead of sitting inert. The menu is rebuilt on every
        # right-click, so switching the algorithm in the config dialog
        # shows/hides them on the next right-click.
        # The tab context menu is rebuilt from scratch by this method on
        # every right-click (on_tab_menu fires each time), so the
        # checkmarks always mirror the current settings file: changing an
        # option in the config dialog is reflected here automatically, and
        # toggling here writes it back via set_opt (tabmenu_ignore) --
        # two-way sync with zero extra bookkeeping.
        # Reload self.cfg from disk when the settings file changed, so the
        # algorithm check below (like the checkmarks above) always mirrors
        # the current settings -- hand-edited JSON included.
        self.config()
        # (index-unpack: a throwaway named '_' here would shadow the
        # module-level _() translation function for the whole method)
        use_native = self._resolve_algorithm()[1]
        if use_native:
            ct.menu_proc(self.compare_menu, ct.MENU_ADD, caption='-')
            for key, caption in _IGNORE_OPTS:
                item = ct.menu_proc(self.compare_menu, ct.MENU_ADD,
                    command='module=cuda_differ;cmd=tabmenu_ignore;info='+key+';',
                    caption=caption
                    )
                ct.menu_proc(item, ct.MENU_SET_CHECKED,
                    command=bool(get_opt('ignoreopt.' + key, False)))
                ct.menu_proc(item, ct.MENU_SET_ENABLED, command=is_compare)

        # Separator + the cancel commands at the very bottom, below
        # everything else. Mirrors the 'Differ\Cancel compare' and
        # 'Differ\Cancel all compares' plugin commands: they stop an
        # in-flight background compare (engine told to stop, editors
        # unlocked + made writable again). 'Cancel compare' acts on
        # THIS tab (enabled while THIS tab's session has a job in
        # flight -- another tab's running compare never enables it),
        # 'Cancel all compares' on every compare tab (enabled while ANY
        # session has one). Both are enabled only while a compare is
        # actually running; the menu is rebuilt on every right-click,
        # so the enabled state is always fresh.
        ct.menu_proc(self.compare_menu, ct.MENU_ADD, caption='-')
        cur_session = self._session_for(cur_ed.get_prop(ct.PROP_TAB_ID))
        self.menuid_cancel = ct.menu_proc(self.compare_menu, ct.MENU_ADD,
            command='module=cuda_differ;cmd=cancel_compare;',
            caption=_('Cancel compare')
            )
        ct.menu_proc(self.menuid_cancel, ct.MENU_SET_ENABLED,
            command=cur_session is not None and cur_session.job is not None)
        self.menuid_cancel_all = ct.menu_proc(self.compare_menu, ct.MENU_ADD,
            command='module=cuda_differ;cmd=cancel_all_compares;',
            caption=_('Cancel all compares')
            )
        ct.menu_proc(self.menuid_cancel_all, ct.MENU_SET_ENABLED,
            command=any(s.job is not None for s in self._sessions.values()))

    def tabmenu_chooser(self):
        """Launch 'Compare with...' via a 100ms timer (needed because menu
        callbacks can't call dlg_file directly)."""
        callback = 'module=cuda_differ;cmd=tabmenu_chooser_timer;info=_;'
        ct.timer_proc(ct.TIMER_START_ONE, callback, 100)

    def tabmenu_chooser_timer(self, tag='', info=''):
        """Timer callback that actually opens the file dialog."""
        self.compare_with()

    def tabmenu_chooser_tab(self):
        """Opens the 'Compare current document with tab...' command, which
        shows the native CudaText tab-picker dialog (with scrollbar)."""
        callback = 'module=cuda_differ;cmd=tabmenu_chooser_tab_timer;info=_;'
        ct.timer_proc(ct.TIMER_START_ONE, callback, 100)

    def tabmenu_chooser_tab_timer(self, tag='', info=''):
        """Timer callback that actually opens the tab-picker dialog."""
        self.compare_with_tab()

    def tabmenu_refresh(self):
        """Recompare the compare tab -- re-applies diff markers (menu item "Recompare")."""
        callback = 'module=cuda_differ;cmd=tabmenu_refresh_timer;info=_;'
        ct.timer_proc(ct.TIMER_START_ONE, callback, 100)

    def tabmenu_refresh_timer(self, tag='', info=''):
        """Timer callback that actually runs the refresh."""
        self.refresh_compare()

    def tabmenu_ignore(self, info):
        """Toggle one 'differ.ignoreopt.*' option from the diff-tab context
        menu (checkable items below 'Recompare'): persist it to
        settings/cuda_differ.json via set_opt -- so the config dialog sees
        it too -- then refresh the compare on a 100ms one-shot timer (same
        convention as the other tabmenu_* callbacks, so the menu can close
        first). The context menu itself is rebuilt by tabmenu_init on every
        right-click, so the new checkmark shows up automatically the next
        time the menu opens."""
        key = info
        old = bool(get_opt('ignoreopt.' + key, False))
        set_opt('ignoreopt.' + key, not old)
        captions = dict(_IGNORE_OPTS)
        state = _('enabled') if not old else _('disabled')
        ct.msg_status('{}: {} -- {}'.format(
            _('Differ ignore option'), captions.get(key, key), state))
        # Re-run the compare so the change is visible immediately.
        # refresh_compare calls config() first, which detects the settings-file
        # mtime change and reloads self.cfg, so this very refresh already
        # uses the new flags.
        callback = 'module=cuda_differ;cmd=tabmenu_refresh_timer;info=_;'
        ct.timer_proc(ct.TIMER_START_ONE, callback, 100)

    def tabmenu_files(self, info):
        """Launch a compare between two files specified in 'info' (format:
        'fn0::fn1') via a 100ms timer."""
        callback = 'module=cuda_differ;cmd=tabmenu_files_timer;info='+info+';'
        ct.timer_proc(ct.TIMER_START_ONE, callback, 100)

    def tabmenu_files_timer(self, tag='', info=''):
        """Timer callback that actually calls set_files with the two filenames."""
        fn0, fn1 = info.split('::', maxsplit=1)
        self.set_files(fn0, fn1)

    def select_all_diff(self):
        """Select all diff hunks in the focused editor as multi-caret selections."""
        session = self._focused_session()
        if session is None:
            return ct.msg_status(_('Differ: not a compare tab'))
        diff = self._session_diff(session)
        if not diff.diffmap:
            self.refresh_compare()
            diff = self._session_diff(session)
        if len(diff.diffmap) == 0:
            return ct.msg_status(_("No differences were found"))
        fc, eds = self.focused
        y1,y2 = (0,1) if fc == 0 else (2,3)

        for n, dif in enumerate(diff.diffmap):
            id = ct.CARET_SET_ONE if n == 0 else ct.CARET_ADD
            eds[fc].set_caret(0, dif[y1], 0, dif[y2], id=id)

    def on_close_pre(self, ed_self: ct.Editor):
        """Fires when any tab is about to close, BEFORE CudaText reads
        the tab's modified state -- i.e. before it would show the
        'Save changes to ...?' dialog (the event can also cancel the
        close by returning False; we never do).

        A diff tab is untitled and holds no real file on disk, and its
        two halves are deliberately kept PROP_MODIFIED=True so
        CudaText's session keeps the tab (including the SECOND half's
        text -- the session file only persists a split tab's secondary
        editor when its modified flag is set). The side effect: closing
        a diff tab always asked 'Save changes?' even when nothing needs
        syncing.

        The plugin tracks the REAL unsaved state itself (per-half dirty
        flags -- see _get_dirty_halves). So here, when NEITHER half is
        dirty, we clear PROP_MODIFIED on both halves: CudaText's
        subsequent modified check then reads False and the tab closes
        silently, no dialog. When a half IS dirty (unsynced edits), the
        flags are left True and the dialog shows as usual -- the user
        can still choose to run the Ctrl+S sync path from it.

        Single-tab close fires this once (for the focused half); app
        exit fires it for EVERY half of every tab, then -- if the tab
        really closes -- on_close (also per half). on_close's exit
        branch puts the halves back to PROP_MODIFIED=True before
        CudaText writes the session, so restart-restore is unaffected
        by the clearing done here.

        Residual edge case: if ANOTHER plugin cancels the close after
        we cleared the flags, the tab stays open with Modified=False
        until the next edit (any edit re-sets it) or the app exit
        (on_close restores it). No session data can be lost by that
        alone: only a crash before any of those would save the session
        without the second half's text.
        """
        tab_id = ed_self.get_prop(ct.PROP_TAB_ID)
        session = self._session_for(tab_id)
        if session is None:
            return  # not a compare tab -- CudaText handles it normally
        if self._get_dirty_halves(session):
            # Dirty half(es): unsynced edits exist -- keep the dialog.
            return
        # Clean diff tab: suppress the save dialog. Clear the flag on
        # BOTH halves -- CudaText treats a split tab as modified when
        # EITHER half is modified.
        try:
            a_ed = ct.Editor(ed_self.get_prop(ct.PROP_HANDLE_PRIMARY))
            b_ed = ct.Editor(ed_self.get_prop(ct.PROP_HANDLE_SECONDARY))
        except Exception:
            return
        for e in (a_ed, b_ed):
            try:
                e.set_prop(ct.PROP_MODIFIED, False)
            except Exception:
                pass

    def on_close(self, ed_self: ct.Editor):
        """Fires after the close is confirmed. For a compare tab: destroy
        its ENTIRE standalone session (persisted registration, overview
        panel, in-flight job, all per-tab caches) so the tab's world is
        fully gone and can never leak into another tab. If this was the
        last compare tab, disable autostart. No temp files to delete
        (split-tab approach).

        During app exit, the state entry and autostart are preserved so
        the compare tab can be restored after restart."""
        tab_id = ed_self.get_prop(ct.PROP_TAB_ID)
        session = self._session_for(tab_id)
        if session is None:
            return  # not a compare tab

        # Remove the persisted registration (under the tab's OWN
        # state_key).
        entry = self._unregister_compare_tab(session)

        # Remove from in-memory scroll set.
        try:
            self.scroll.tab_id.discard(int(tab_id))
        except (ValueError, TypeError):
            pass

        # Destroy the paintbox overview and the hunk edge columns for
        # this tab.
        overview = session.overview
        session.overview = None
        if overview is not None:
            overview.destroy()
        columns = session.columns
        session.columns = None
        if columns is not None:
            columns.destroy()

        # Drop any in-flight background compare for this tab and CANCEL
        # the engine compare: closing the tab means the result will
        # never be consumed, so the engine's background thread is told
        # to stop cooperatively (diff_proc DIF_CANCEL) instead of
        # burning CPU until it finishes. Cancellation takes at most a
        # couple of seconds, and the completion callback of a cancelled
        # compare is never invoked. The stale flag is belt-and-braces
        # for the finishing race: a compare that completed right before
        # the cancellation arrived still delivers its callback, which
        # the stale check below turns into a no-op.
        job = session.job
        session.job = None
        if job is not None:
            self._cancel_job(job)

        # The session itself goes last -- after this the tab's world
        # (Differ/diffmap, caches, timer flags) is fully gone.
        self._sessions.pop(session.tab_id_str, None)

        # During app exit, keep the state entry and autostart subscription
        # so compare tabs persist restarts and the plugin auto-loads.
        # Re-register since we already unregistered above, preserving the
        # saved/dirty state (per-half dirty flags + legacy 'saved' flag)
        # so on_start2 can restore the correct title color and a restart
        # save still syncs only the halves that were dirty before exit.
        if getattr(self, '_app_exiting', False):
            if entry is not None:
                self._register_compare_tab(
                    session,
                    entry.get('primary_orig_tab_id'),
                    entry.get('secondary_orig_tab_id'),
                    entry.get('primary_orig_name', ''),
                    entry.get('secondary_orig_name', ''),
                    entry.get('saved', True),
                    entry.get('dirty')
                )
            # Put both halves back to PROP_MODIFIED=True. on_close_pre
            # (which fires for every half before the exit dialogs) may
            # have cleared the flags on a CLEAN tab to skip the save
            # dialog -- but the session is written AFTER on_close, and
            # it only persists a split tab's SECOND half text when that
            # half is modified. Without this restore, a clean diff tab
            # would come back after restart with an empty right side.
            # The tab is closing anyway, so Modified=True here has no
            # other visible effect.
            try:
                for h in (ed_self.get_prop(ct.PROP_HANDLE_PRIMARY),
                          ed_self.get_prop(ct.PROP_HANDLE_SECONDARY)):
                    if h:
                        ct.Editor(h).set_prop(ct.PROP_MODIFIED, True)
            except Exception:
                pass
            return

        # If no more compare tabs are open in this tab's persisted
        # session group, disable autostart so the plugin does not load
        # on next startup.
        state = self._load_state()
        if not state['sessions'].get(session.state_key, {}):
            self._disable_autostart()
