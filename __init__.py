import functools
import os
import re
import json
import time
import typing as tp

import cudatext as ct
import cudatext_cmd as ct_cmd
import cudax_lib as ctx

from . import differ_native as dfn
from . import differ_python as dfp
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
OPTS_META = [
    # --- chapter "theme": colors used to paint the compare view ----------
    {'opt': 'differ.theme.changed_color',
     'cmt': _('Color of changed lines\n'
              'Background color for lines that were modified (replaced with '
              'different content).\n'
              'Also colors the char-level highlights inside modified lines, '
              'the margin markers, the micromap highlights and the overview '
              'panel.\n'
              'Leave empty to use the theme default.'),
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
              'Leave empty to use the theme default.'),
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
              'Leave empty to use the theme default.'),
     'def': '',
     'frm': '#rgb-e',
     'chp': 'theme',
     },
    {'opt': 'differ.theme.gap_color',
     'cmt': _('Color of inter-line gap background\n'
              'Background color for the blank gap inserted to keep the two '
              'sides visually aligned when one side has fewer lines.\n'
              'Also colors the gap rectangles in the overview panel.\n'
              'Leave empty to use the theme default.'),
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
              'Leave empty to use the editor text background color '
              '(the ignored region then looks like normal text).'),
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
              'Leave empty to use the editor text background color '
              '(the ignored gap then looks like empty space).'),
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
    # items in the diff-tab right-click context menu, below 'Refresh') ---
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
              'menu (checkable item below "Refresh").\n'
              'Not supported by the pure-Python algorithms -- they '
              'compare strictly.\n'
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
              'menu (checkable item below "Refresh").\n'
              'Not supported by the pure-Python algorithms -- they '
              'compare strictly.\n'
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
              'menu (checkable item below "Refresh").\n'
              'Not supported by the pure-Python algorithms -- they '
              'compare strictly.\n'
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
              'menu (checkable item below "Refresh").\n'
              'Not supported by the pure-Python algorithms -- they '
              'compare strictly.\n'
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
              'menu (checkable item below "Refresh").\n'
              'Not supported by the pure-Python algorithms -- they '
              'compare strictly.\n'
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
              'must use the Refresh command manually.\n'
              'Default: off.'),
     'def': False,
     'frm': 'bool',
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
# context menu (below 'Refresh' -- see tabmenu_init) and in the config
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


_migrate_old_option_names()


class _CompareJob:
    """Context for one compare of one compare tab.

    _refresh_ex fills the job while setting the compare up. With the
    native algorithms the line-level diff runs in the engine's
    background thread: _refresh_ex returns after starting it, and the
    engine's completion callback (_on_native_diff_done, marshalled to
    the main thread) uses the job to finish the compare -- paint the
    events, set the bookmarks, repaint the overview. With the Python
    algorithms the paint phase runs inline in _refresh_ex and the job
    is just a carrier for the same data.

    The snapshot fields (a_text/b_text, lines_a/lines_b) are the texts
    the engine was kicked off with. While a background compare runs,
    LOCK_EDITORS_WHILE_COMPARING keeps both halves locked + read-only,
    so the live editors cannot drift from these snapshots; a refresh
    that arrives anyway is dropped (see _refresh_ex), never queued.

    'editor_lock' carries the whole-compare editor lock state while
    LOCK_EDITORS_WHILE_COMPARING is on and a background compare is
    running: None while nothing is held, otherwise a dict
    {'a': original_ro, 'b': original_ro} recording per half the
    PROP_RO value the lock replaced, so _release_compare_editors can
    restore exactly that (and only once -- the release is idempotent).
    """

    __slots__ = (
        'ed',               # editor that triggered the refresh
        'tab_id',           # PROP_TAB_ID of the compare tab
        'tab_id_str',       # str(tab_id) -- dict key
        'a_ed', 'b_ed',     # the two split halves
        'a_text', 'b_text',         # native: raw text snapshots
        'lines_a', 'lines_b',       # python: line lists
        'overview',         # PaintboxOverview or None
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
        self.tab_id = None
        self.tab_id_str = ''
        self.a_ed = None
        self.b_ed = None
        self.a_text = None
        self.b_text = None
        self.lines_a = None
        self.lines_b = None
        self.overview = None
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


class Command:
    def __init__(self):
        self.scroll = ScrollSplittedTab(__name__)
        self.cfg = self.get_config()
        self.diff = self._create_differ()
        # Set to True by on_exit_pre when CudaText is about to exit, so that
        # on_close (which fires next, once per closing tab) can skip
        # temp-file deletion and let compare tabs persist across restarts.
        self._app_exiting = False
        # In-memory cache of saved/unsaved state per compare tab ID.
        # Avoids redundant JSON writes when on_change fires repeatedly
        # without the state actually changing.
        self._saved_cache = {}
        # In-memory cache of PER-HALF dirty state per compare tab ID:
        # 'a' = primary (left) half, 'b' = secondary (right) half.
        # on_change marks the edited half; on_save_pre syncs ONLY these
        # halves back to their originals, so a clean half -- and its
        # original tab -- is never touched by save. Persisted to disk
        # together with the legacy 'saved' flag via _update_dirty_state.
        self._dirty_halves = {}
        # In-memory set of all compare tab IDs in the current session.
        # Used by _is_compare_tab for fast O(1) lookup without disk I/O --
        # critical because on_change fires on every keystroke.
        self._compare_tab_ids = set()
        # Cached key for the current session (relative path if inside
        # settings folder, full path otherwise). Set in on_start2 and set_files.
        self._current_session_key = ''
        # Counter of on_change events to suppress per compare tab ID.
        # Set to 2 when a compare is created (one per split half) because
        # set_text_all triggers on_change for each half. Prevents the
        # initial green color from being reset to red.
        self._suppress_change = {}
        # Overview panels per compare tab ID. Each value is a
        # PaintboxOverview instance docked to the right of the editor.
        self._overviews = {}
        # Active overview repaint timers per tab ID (for debouncing).
        self._overview_timers = {}
        # In-flight background compares (native algorithms), keyed by
        # compare-tab ID string. One compare per tab at a time; a
        # refresh that arrives while a compare is running is DROPPED
        # with a status hint (see _refresh_ex) -- the running compare
        # paints against its kick-off snapshots, which cannot drift
        # because LOCK_EDITORS_WHILE_COMPARING keeps the halves
        # read-only for the whole run. Each job carries the engine
        # job handle (diff_proc async form) and the whole-compare
        # editor lock state (job.editor_lock) so closing the tab or
        # exiting the app can cancel the engine compare via
        # diff_proc(DIF_CANCEL) and release the editors when the
        # result will never be consumed.
        self._jobs = {}

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

    def _is_compare_tab(self, tab_id):
        """Check if the given PROP_TAB_ID belongs to a compare tab.
        Uses an in-memory set for O(1) lookup -- no disk I/O."""
        return str(tab_id) in self._compare_tab_ids

    def _register_compare_tab(self, compare_tab_id, primary_orig_id, secondary_orig_id,
                              primary_orig_name='', secondary_orig_name='', session_key='',
                              saved=True, dirty=None):
        """Register a compare tab under its session key with the PROP_TAB_IDs
        and display names of its two original tabs. 'dirty' tracks which
        halves ('a' = primary/left, 'b' = secondary/right) carry unsaved
        edits -- on_save_pre syncs only those halves back to their
        originals. Callers that pass only the boolean 'saved' (legacy
        form) get the conservative mapping: unsaved -> both halves dirty."""
        if not session_key:
            session_key = self._current_session_key
        if dirty is None:
            dirty = set() if saved else {'a', 'b'}
        else:
            dirty = {h for h in dirty if h in ('a', 'b')}
        # The boolean 'saved' flag (kept for compatibility with state files
        # of older plugin versions and for the tab title color) simply
        # means "no half is dirty".
        saved = not dirty
        state = self._load_state()
        if session_key not in state['sessions']:
            state['sessions'][session_key] = {}
        state['sessions'][session_key][str(compare_tab_id)] = {
            'primary_orig_tab_id': primary_orig_id,
            'primary_orig_name': primary_orig_name or '',
            'secondary_orig_tab_id': secondary_orig_id,
            'secondary_orig_name': secondary_orig_name or '',
            'saved': saved,
            'dirty': sorted(dirty),
        }
        self._save_state(state)
        self._saved_cache[str(compare_tab_id)] = saved
        self._dirty_halves[str(compare_tab_id)] = set(dirty)
        self._compare_tab_ids.add(str(compare_tab_id))

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

    def _get_dirty_halves(self, compare_tab_id):
        """Return the set of halves with unsaved edits for a compare tab
        ('a' = primary/left, 'b' = secondary/right). Serves the in-memory
        cache; on a cache miss falls back to the persisted state (with
        legacy migration) and populates the cache."""
        key = str(compare_tab_id)
        cached = self._dirty_halves.get(key)
        if cached is not None:
            return set(cached)
        state = self._load_state()
        session = state['sessions'].get(self._current_session_key, {})
        dirty = self._entry_dirty(session.get(key))
        self._dirty_halves[key] = set(dirty)
        self._saved_cache[key] = not dirty
        return set(dirty)

    def _update_dirty_state(self, compare_tab_id, dirty_halves):
        """Single write path for the saved/dirty state of a compare tab.
        'dirty_halves' is a subset of {'a','b'} naming the halves with
        unsaved edits; the legacy boolean 'saved' flag is kept in sync
        (True iff no half is dirty). Cache-guarded so on_change firing on
        every keystroke doesn't hit the disk -- only an actual state
        change (clean half gets edited / dirty half gets synced) rewrites
        the JSON."""
        key = str(compare_tab_id)
        dirty_halves = {h for h in dirty_halves if h in ('a', 'b')}
        if self._dirty_halves.get(key) == dirty_halves:
            return
        self._dirty_halves[key] = set(dirty_halves)
        saved = not dirty_halves
        state = self._load_state()
        session = state['sessions'].get(self._current_session_key, {})
        entry = session.get(key)
        if isinstance(entry, dict):
            entry['dirty'] = sorted(dirty_halves)
            entry['saved'] = saved
            self._save_state(state)
        self._saved_cache[key] = saved

    def _unregister_compare_tab(self, compare_tab_id):
        """Remove a compare tab from the persisted state. Returns the
        removed entry dict or None if not found."""
        state = self._load_state()
        key = str(compare_tab_id)
        session = state['sessions'].get(self._current_session_key, {})
        entry = session.pop(key, None)
        if entry is not None:
            # Clean up empty session.
            if not session:
                del state['sessions'][self._current_session_key]
            self._save_state(state)
            self._compare_tab_ids.discard(key)
            # Drop the in-memory state caches for this tab.
            self._saved_cache.pop(key, None)
            self._dirty_halves.pop(key, None)
        return entry

    def _get_orig_tab_ids(self, compare_tab_id):
        """Return (primary_orig_id, secondary_orig_id) for a compare tab,
        or (None, None) if not found."""
        state = self._load_state()
        session = state['sessions'].get(self._current_session_key, {})
        entry = session.get(str(compare_tab_id))
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
        try:  # New op_ed allows to skip meta-file
            op_ed_dlg = op_ed.OptEdD(
                path_keys_info=OPTS_META, subset=subset, how=how)
        except:
            # Old op_ed requires to use meta-file
            if not os.path.exists(METAJSONFILE) \
            or os.path.getmtime(METAJSONFILE) < os.path.getmtime(__file__):
                # Create/update meta-info file
                open(METAJSONFILE, 'w').write(json.dumps(OPTS_META, indent=4))
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
    # context menu, right below 'Refresh' -- see tabmenu_init() and
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
        content and editor properties into the two halves."""
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
            
            # Load each original's content into the two split halves.
            a_ed.set_text_all(orig_texts[0])
            b_ed.set_text_all(orig_texts[1])
            
            # Register the compare tab by its PROP_TAB_ID with the original
            # tab IDs and names, plus the session key for grouping.
            compare_tab_id = ct.ed.get_prop(ct.PROP_TAB_ID)
            try:
                session_path = ct.app_path(ct.APP_FILE_SESSION) or ''
            except Exception:
                session_path = ''
            session_key = self._session_key(session_path)
            self._current_session_key = session_key
            self._register_compare_tab(
                compare_tab_id,
                orig_tab_ids[0], orig_tab_ids[1],
                orig_names[0], orig_names[1],
                session_key,
                saved=True)  # initial state: content matches originals = saved

            # Color the tab title green to indicate 'synced' (no unsaved
            # changes yet -- content is identical to the originals).
            ct.ed.set_prop(ct.PROP_TAB_COLOR_FONT, 0x00A000)  # green

            # Suppress the next 2 on_change events (one per split half)
            # because set_text_all triggers on_change, which would reset the
            # green color to red. The counter is decremented in on_change;
            # real user edits after this will work normally.
            self._suppress_change[str(compare_tab_id)] = 2

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

            self.refresh()
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
        """Handle theme syntax changes (reload config + refresh), word-wrap
        state changes (re-apply gaps with wrap-aware sizes)."""
        if state == ct.APPSTATE_THEME_SYNTAX:
            self.get_config()
            # each time we change setting using the options editor the state even APPSTATE_THEME_UI fires which triger a refresh , if files are big it take time which is frustrating, if the user needs to refresh then he can do it manualy, lets not auto refresh for him
            # self._refresh_ex(ct.ed)  # automatic -- no dialog
        elif state == ct.EDSTATE_WRAP:
            # Word-wrap mode changed on one of the split halves. The
            # inter-line gaps were sized for the previous wrap state, so
            # we must re-apply them with wrap-aware sizes to keep both
            # sides visually aligned.
            if self._is_compare_tab(ed_self.get_prop(ct.PROP_TAB_ID)):
                self._refresh_ex(ed_self)  # automatic -- no dialog

    def on_scroll(self, ed_self):
        """Forward scroll events to ScrollSplittedTab for synchronized
        scrolling. The overview repaint is debounced via a timer to
        avoid excessive CPU usage and flickering during continuous
        scrolling."""
        tab_id = ed_self.get_prop(ct.PROP_TAB_ID)
        if self._is_compare_tab(tab_id):
            self.scroll.on_scroll(ed_self)
            # Debounce overview repaint: use a one-shot timer so we
            # only repaint after scrolling stops for 150ms.
            tab_id_str = str(tab_id)
            if tab_id_str not in self._overview_timers:
                self._overview_timers[tab_id_str] = True
                callback = 'module=cuda_differ;cmd=_overview_repaint_timer;info={};'.format(tab_id_str)
                ct.timer_proc(ct.TIMER_START_ONE, callback, 150)

    def _overview_repaint_timer(self, tag='', info=''):
        """Timer callback that repaints the overview for a given tab.
        Called 150ms after the last scroll event to avoid excessive
        repaints during continuous scrolling."""
        if not info:
            return
        self._overview_timers.pop(info, None)
        overview = self._overviews.get(info)
        if overview is not None:
            overview.paint()

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
        _suppress_change) because set_text_all triggers spurious on_change
        events that would reset the initial green color.

        Performance: _is_compare_tab uses an in-memory set (no disk I/O),
        so non-compare tabs return in O(1). The handler is lightweight
        enough for on_change."""
        tab_id = ed_self.get_prop(ct.PROP_TAB_ID)
        if not self._is_compare_tab(tab_id):
            return
        key = str(tab_id)
        if key in self._suppress_change:
            self._suppress_change[key] -= 1
            if self._suppress_change[key] <= 0:
                del self._suppress_change[key]
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
            halves = self._get_dirty_halves(tab_id)
            halves.add(half)
            # Persist the unsaved state so on_start2 can restore the
            # correct color after restart. Cache-guarded: only the first
            # edit of a clean half writes to disk.
            self._update_dirty_state(tab_id, halves)

    def on_change_slow(self, ed_self):
        """Fires after the user edits and a short pause passes. Used only
        for auto-refreshing the diff markers if that option is enabled.
        Color/saved-state logic is handled in on_change (immediate)."""
        if self.cfg.get('enable_auto_refresh', False):
            self._refresh_ex(ed_self)  # automatic -- no dialog

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
        if not self._is_compare_tab(tab_id):
            return  # not a compare tab -- let CudaText handle normally

        # Cancel any in-flight background compare for this tab before
        # doing anything else. Save can be reached via the "Save
        # changes?" prompt right before a tab close, so a compare left
        # running is about to become useless work; drop it here instead
        # of racing on_close's own cancellation, which fires after this
        # handler returns. _cancel_job also releases the compare-level
        # editor lock / read-only state, so the halves are editable
        # again by the time the save finishes.
        tab_id_str = str(tab_id)
        job = self._jobs.pop(tab_id_str, None)
        if job is not None:
            self._cancel_job(job)

        # Which halves carry unsaved edits? Only those get synced.
        dirty = self._get_dirty_halves(tab_id)
        if not dirty:
            # Neither half is dirty: nothing to sync. The old behavior
            # re-synced and re-saved BOTH files on every Ctrl+S (and on
            # the "Save changes?" prompt of a closing tab) -- pure waste.
            # Clear any pending suppress counter so a later real edit is
            # not swallowed by it, keep the title green, and tell the
            # user why nothing was saved.
            self._suppress_change.pop(tab_id_str, None)
            self._update_dirty_state(tab_id, set())  # repair stale state
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
            self._suppress_change.pop(tab_id_str, None)

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
            self._update_dirty_state(tab_id, remaining)
            # Auto-refresh diff markers so the user sees updated
            # highlights without needing to click Refresh manually.
            # self._refresh_ex(ed_self)  # automatic -- no dialog

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

        # --- Rebuild in-memory caches and apply colors/markers ---
        self.scroll.tab_id = set()
        self._compare_tab_ids = set()
        for tab_id_str, entry in session.items():
            if not isinstance(entry, dict):
                continue
            try:
                self.scroll.tab_id.add(int(tab_id_str))
            except (ValueError, TypeError):
                pass
            self._compare_tab_ids.add(tab_id_str)
            # Populate the saved/dirty-state caches from disk. Entries
            # written by older plugin versions have no 'dirty' key --
            # _entry_dirty maps the legacy 'saved' flag instead (unsaved
            # -> both halves dirty, so the first Ctrl+S after upgrade
            # syncs both sides, exactly like the old always-sync-both
            # behavior).
            dirty = self._entry_dirty(entry)
            self._dirty_halves[tab_id_str] = dirty
            saved = not dirty
            self._saved_cache[tab_id_str] = saved
            
            # Find an editor for this compare tab and re-apply diff markers.
            # if the user have a lot of big diff tabs they will all run at the same time and cudatext will hang for a moment, let stop diffing after restart, if the user is still interested in the compare then a simple click to refresh is not bad experience anyway
            # for h in ct.ed_handles():
            #     e = ct.Editor(h)
            #     if str(e.get_prop(ct.PROP_TAB_ID)) == tab_id_str:
            #         self._refresh_ex(e)
            #         break
                    
            # Re-apply the title color: green only when no half is dirty.
            if saved:
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
        for job in self._jobs.values():
            self._cancel_job(job)
        self._jobs.clear()

    '''
    def on_tab_change(self, ed_self):
        self.config()
        self.scroll.toggle(self.cfg.get('sync_scroll'))
    '''

    def on_tab_menu(self, ed_self):
        """Build the right-click tab context menu (Compare with..., Refresh, etc.)."""
        self.tabmenu_init(ed_self)

    def refresh(self):
        """Manual refresh (from menu command or context menu). Shows the
        'identical' dialog if both sides are equal. Only applies to compare
        tabs managed by this plugin. With the native algorithms the
        compare runs in a background thread -- the markers are
        re-applied when it finishes."""
        self._refresh_ex(ct.ed, show_dialog=True)

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
        self._jobs -- callers pop/clear it themselves, since 'cancel
        one' and 'cancel all' remove from the dict differently."""
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
        if not self._is_compare_tab(tab_id):
            return ct.msg_status(_('Differ: not a compare tab'))
        job = self._jobs.pop(str(tab_id), None)
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
        n = len(self._jobs)
        if not n:
            return ct.msg_status(_('Differ: no compares running'))
        for job in self._jobs.values():
            self._cancel_job(job)
        self._jobs.clear()
        ct.msg_status(_('Differ: cancelled {} compare(s)').format(n))

    def _create_differ(self):
        """Create the appropriate Differ instance based on the configured
        algorithm. Returns a differ_native.Differ for native algorithms
        (when the native API is available), or a differ_python.Differ for
        all Python algorithms and as a fallback when native is unavailable.
        """
        algo = self.cfg.get('diff_algorithm', 'native_histogram')
        if algo in ('native_histogram', 'native_myers') and dfn._HAS_NATIVE_DIFF:
            ct.msg_status(_("Differ: Using Native Algo {}").format(algo))
            return dfn.Differ()
        ct.msg_status(_("Differ: Using Python Algo {}").format(algo))
        return dfp.Differ()

    def _ensure_correct_differ(self):
        """Check if self.diff matches the configured algorithm type, and
        swap it if not. Called at the start of _refresh_ex so the Differ
        is always the right type before a compare runs. Preserves the
        options (withdetail, beautify_alignment) but NOT the sequences:
        neither Differ holds sequences between compares anymore — both
        the native Differ (compare(a_text, b_text)) and the Python
        Differ (compare(lines_a, lines_b)) take their inputs as
        parameters at compare() time. _refresh_ex always re-passes fresh
        data to compare() right after this swap, before any compare()
        runs."""
        algo = self.cfg.get('diff_algorithm', 'native_histogram')
        want_native = algo in ('native_histogram', 'native_myers') and dfn._HAS_NATIVE_DIFF
        is_native = isinstance(self.diff, dfn.Differ)
        if want_native == is_native:
            return  # already the right type
        # Swap: preserve options only. We do NOT preserve sequences:
        #   - Both differs take their inputs as compare() params
        #     (native: raw texts; Python: line lists). There is nothing
        #     to preserve on the instance.
        # _refresh_ex always calls compare(...) with fresh state right
        # after this swap, before any compare() runs, so the empty new
        # Differ is fine.
        old_withdetail = getattr(self.diff, 'withdetail', True)
        old_beautify_alignment = getattr(self.diff, 'beautify_alignment', False)
        self.diff = dfn.Differ() if want_native else dfp.Differ()
        self.diff.withdetail = old_withdetail
        self.diff.beautify_alignment = old_beautify_alignment
        self.diff.diff_algorithm = algo

    def _refresh_ex(self, ed, show_dialog=False):
        """Core refresh logic. 'ed' is any editor belonging to the compare
        tab. Only applies to compare tabs managed by this plugin.

        'show_dialog' controls whether the 'two sides are identical' dialog
        is shown. Automatic refreshes (on_start2, on_change_slow, on_state)
        pass False to avoid pestering the user; manual refresh and the
        initial compare pass True.

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
            return
        if ed.get_prop(ct.PROP_EDITORS_LINKED):
            return
        tab_id = ed.get_prop(ct.PROP_TAB_ID)
        if not self._is_compare_tab(tab_id):
            return  # not a compare tab we manage -- skip

        # One compare at a time per tab. A background compare already
        # running for this tab? While it runs, LOCK_EDITORS_WHILE_COMPARING
        # keeps the halves locked + read-only, so their texts cannot drift
        # under the engine -- there is nothing a queued re-run would fix.
        # Drop this request (manual Refresh, on_change_slow auto-refresh,
        # on_state) with a status hint instead of starting a second engine
        # job or deferring work: the running compare finishes and paints
        # against its own kick-off snapshots; the NEXT refresh -- the user
        # can fire it any time after this one, or cancel first -- picks up
        # whatever the editors hold then.
        if self._jobs.get(str(tab_id)) is not None:
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

            # Set up the micromap on both editors when enabled.
            micromap_on = self.cfg.get('enable_micromap', False)
            if micromap_on:
                self._setup_micromap(a_ed, b_ed)

            # Create or reuse the paintbox overview for this compare tab.
            # The overview is a custom paintbox added to the right side
            # of the editor's parent form. It shows a gap-aware mini-map
            # of both editors side-by-side (unlike the micromap, which
            # doesn't account for the gaps we insert for alignment).
            tab_id_str = str(tab_id)
            overview = self._overviews.get(tab_id_str)
            overview_on = self.cfg.get('enable_overview', True)
            if overview_on:
                if overview is None:
                    overview = PaintboxOverview()
                    overview.create(a_ed, b_ed)
                    self._overviews[tab_id_str] = overview
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
                del self._overviews[tab_id_str]
                overview = None

            Profiler.start('refresh:get_text')
            a_text_all = a_ed.get_text_all(ends=True)
            b_text_all = b_ed.get_text_all(ends=True)
            Profiler.stop('refresh:get_text')

            if a_text_all == b_text_all:
                Profiler.start('refresh:clear')
                self.clear(a_ed)
                self.clear(b_ed)
                Profiler.stop('refresh:clear')
                self.diff.diffmap = []
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

            # Ensure the Differ instance matches the configured algorithm
            # type (native vs Python). Swaps if the user changed the
            # algorithm in config since the last compare.
            self._ensure_correct_differ()

            # Branch on the differ type to compute the Python side's
            # line lists. The native Differ doesn't need a pre-split —
            # it takes raw texts at the compare() call site below.
            # Neither Differ holds any sequences between compares:
            #   - native:  compare(a_text, b_text)
            #   - python: compare(lines_a, lines_b)
            # In both cases the inputs enter as locals inside compare()
            # and are dropped when the generator returns — zero text
            # bytes persistent between compares.
            if not isinstance(self.diff, dfn.Differ):
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
                # during _refresh_ex — the peak the user actually sees
                # when the diff runs.
                lines_a = split_lines_safe(a_text_all)
                del a_text_all
                lines_b = split_lines_safe(b_text_all)
                del b_text_all
                Profiler.stop('refresh:split_lines_safe')

            self.scroll.tab_id.add(tab_id)
            self.scroll.toggle(self.cfg.get('sync_scroll'))

            self.diff.withdetail = self.cfg.get('compare_with_details')
            self.diff.diff_algorithm = self.cfg.get('diff_algorithm')
            self.diff.beautify_alignment = self.cfg.get('beautify_alignment')
            # Ignore options -> diff_proc DIFF_IGN_* bitmask for the
            # native algorithms (applies to BOTH the line-level diff and
            # the char-level details). The pure-Python Differ simply
            # ignores this attribute -- Python algorithms compare
            # strictly by design.
            self.diff.ignore_flags = dfn.build_ignore_flags(self.cfg)

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
                __, line_h_a = a_ed.get_prop(ct.PROP_CELL_SIZE)
                __, line_h_b = b_ed.get_prop(ct.PROP_CELL_SIZE)
                Profiler.stop('refresh:wrap_counts')
            else:
                wrap_counts_a = None
                wrap_counts_b = None
                line_h_a = 0
                line_h_b = 0
            color_gaps = self.cfg.get('color_gaps')
            # Ignored-difference colors (WinMerge-style suppressed blank
            # lines + their compensating gaps) -- see 'ignored_color' /
            # 'ignored_gap_color'.
            color_ignored = self.cfg.get('color_ignored')
            color_ignored_gap = self.cfg.get('color_ignored_gap')

            # Fill the compare job -- the context the event/paint phase
            # needs. For the native algorithms the line-level diff runs
            # in the engine's background thread (the diff_proc callback
            # form): _refresh_ex returns right after starting it, and
            # _on_native_diff_done finishes the compare on the main
            # thread when the engine calls back. Python algorithms
            # paint inline here, synchronously.
            job = _CompareJob()
            job.ed = ed
            job.tab_id = tab_id
            job.tab_id_str = tab_id_str
            job.a_ed = a_ed
            job.b_ed = b_ed
            job.overview = overview
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
            if isinstance(self.diff, dfn.Differ):
                job.a_text = a_text_all
                job.b_text = b_text_all
                del a_text_all, b_text_all
            else:
                job.lines_a = lines_a
                job.lines_b = lines_b
                del lines_a, lines_b

            if isinstance(self.diff, dfn.Differ):
                # functools.partial carries the job to the callback, so
                # the engine's completion knows WHICH compare finished.
                # (The refresh-while-running case was already handled at
                # the top of _refresh_ex -- by this point no job exists
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
                    dfn.algo_id(self.diff.diff_algorithm),
                    self.diff.ignore_flags,
                    cb)
                if job_handle:
                    job.job_handle = job_handle
                    job.in_flight = True
                    job.profiler_async_token = _async_pair
                    self._jobs[tab_id_str] = job
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
                                       _profiling_enabled_here)

    def _paint_compare_events(self, job, opcodes=None):
        """Consume the Differ's event generator and paint every event
        into both editor halves.

        Shared by both compare modes: the synchronous mode (Python
        algorithms) calls it directly from _refresh_ex; the background
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
        # paintbox overview (gap-aware mini-map) when enabled.
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
        Profiler.start('refresh:compare_and_paint')
        # Both differs take their inputs as compare() parameters (no
        # set_seqs() call, no persistent storage on either Differ between
        # compares -- see differ_native.Differ and differ_python.Differ).
        # Native takes raw texts (the job's snapshots); Python takes line
        # lists (split in _refresh_ex). In the background mode the native
        # call receives the engine's opcodes, so the generator skips its
        # own engine call and walks them directly.
        if isinstance(self.diff, dfn.Differ):
            compare_iter = self.diff.compare(job.a_text, job.b_text,
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
            # (Python path already `del`d its texts in _refresh_ex
            # right after the split_lines_safe call.)
            job.a_text = None
            job.b_text = None
        else:
            compare_iter = self.diff.compare(job.lines_a, job.lines_b)
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
                    if overview is not None:
                        # Gap appears BEFORE a_line_after (between lines
                        # a_line_after-1 and a_line_after)
                        overview.add_gap('a', a_line_after, total_visual)
                else:
                    Profiler.start('paint:gap')
                    self.set_gap(a_ed, a_line_after, b_end - b_start)
                    Profiler.stop('paint:gap')
                    if overview is not None:
                        overview.add_gap('a', a_line_after, b_end - b_start)
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
                    if overview is not None:
                        overview.add_gap('b', b_line_after, total_visual)
                else:
                    Profiler.start('paint:gap')
                    self.set_gap(b_ed, b_line_after, a_end - a_start)
                    Profiler.stop('paint:gap')
                    if overview is not None:
                        overview.add_gap('b', b_line_after, a_end - a_start)
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
                    if overview is not None:
                        overview.add_gap('a', a_line_after, total_visual,
                                         ignored=True)
                else:
                    Profiler.start('paint:gap')
                    self.set_gap(a_ed, a_line_after, b_end - b_start,
                                 color=color_ignored_gap, tag=IGN_GAP_TAG)
                    Profiler.stop('paint:gap')
                    if overview is not None:
                        overview.add_gap('a', a_line_after, b_end - b_start,
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
                    if overview is not None:
                        overview.add_gap('b', b_line_after, total_visual,
                                         ignored=True)
                else:
                    Profiler.start('paint:gap')
                    self.set_gap(b_ed, b_line_after, a_end - a_start,
                                 color=color_ignored_gap, tag=IGN_GAP_TAG)
                    Profiler.stop('paint:gap')
                    if overview is not None:
                        overview.add_gap('b', b_line_after, a_end - a_start,
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
                        self._add_raw_gap(b_ed, b_line,
                                          diff_rows * line_h_b, color_gaps)
                        if overview is not None:
                            # _add_raw_gap inserts AFTER b_line (between
                            # b_line and b_line+1), so record as
                            # after_line = b_line + 1 (gap appears
                            # before line b_line+1 in paint order).
                            overview.add_gap('b', b_line + 1, diff_rows)
                    elif vb > va:
                        diff_rows = vb - va
                        self._add_raw_gap(a_ed, a_line,
                                          diff_rows * line_h_a, color_gaps)
                        if overview is not None:
                            # Same: gap is after a_line, so record
                            # as after_line = a_line + 1.
                            overview.add_gap('a', a_line + 1, diff_rows)
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
            # _refresh_ex: the dialog only appears for the initial
            # compare and manual refresh; automatic refreshes
            # (on_change_slow / on_state) stay silent to avoid
            # pestering the user.
            self.diff.diffmap = []
            Profiler.stop('refresh')
            if show_dialog:
                ct.msg_box(
                    _('No differences found (with current ignore options).'),
                    ct.MB_OK)
            return

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
        of _refresh_ex -- so what the engine produced is what the
        editors still hold, and it is painted as-is.

        Whatever the outcome, the kick-off editor lock / read-only state
        is released in a finally block (idempotent -- _cancel_job already
        released cancelled jobs): the halves become editable again only
        here, after the result is fully rendered.
        """
        # This job is finished -- free the per-tab slot first of all.
        if self._jobs.get(job.tab_id_str) is job:
            del self._jobs[job.tab_id_str]

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
                if not isinstance(self.diff, dfn.Differ):
                    # Algorithm switched to a Python one while the engine
                    # was running: the current Differ cannot paint native
                    # opcodes. Release this job's lock BEFORE the re-run
                    # so the new kick-off's lock does not stack on it,
                    # then re-run the refresh with the new algorithm.
                    self._release_compare_editors(job)
                    self._refresh_ex(job.ed, show_dialog=job.show_dialog)
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
                                           job.profiling_enabled_here)
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

    def _compare_epilogue(self, compare_start, profiling_enabled_here):
        """Show the total compare time on the status bar and print the
        profiling report. Runs for the synchronous mode (from
        _refresh_ex's finally) and for the background mode (from
        _on_native_diff_done). 'compare_start' is the kick-off time, so
        the reported duration covers the whole compare, including the
        background engine phase -- the wall time the user waited."""
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
        # crashed), also on big files.
        if profiling_enabled_here:
            profiling_report()
            enable_profiling(False)

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

    def _add_raw_gap(self, e, line_index, pixel_size, color, tag=None):
        """Add a gap at the given line index with an explicit pixel size.
        `line_index` follows the e.gap() convention: the gap is inserted
        between `line_index` and `line_index+1` (i.e. after `line_index`).
        Use -1 for a gap before the first line. Compared to set_gap(), this
        takes an explicit pixel size instead of computing n*line_height,
        which is needed when wrap is on and the gap must match the actual
        number of wrapped visual rows on the opposite side.
        tag defaults to DIFF_TAG; ignored-difference gaps pass IGN_GAP_TAG."""
        e.gap(ct.GAP_ADD, line_index, 0,
              tag=DIFF_TAG if tag is None else tag,
              size=pixel_size,
              color=color
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
        e.decor(ct.DECOR_SET, row, DIFF_TAG, text, color, bold=True)

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
        """Reload config from disk if the JSON file or theme has changed.
        Caches the result in self.cfg to avoid repeated disk reads."""
        opt_time = os.path.getmtime(JSONPATH) if os.path.exists(JSONPATH) else 0
        theme_name = ct.app_proc(ct.PROC_THEME_SYNTAX_GET, '')
        if self.cfg.get('opt_time') == opt_time and \
           self.cfg.get('theme_name') == theme_name:
            return
        self.cfg = self.get_config()
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

        def get_color(key, default_color):
            s = get_opt(key, '')
            if s:
                return ctx.html_color_to_int(s)
            else:
                return default_color

        def new_nkind(val, color):
            ct.ed.bookmark(ct.BOOKMARK_SETUP, 0,
                           nkind=val,
                           ncolor=color,
                           text=''
                           )

        def get_theme():
            data = ct.app_proc(ct.PROC_THEME_SYNTAX_DICT_GET, '')
            th = {}
            th['color_changed'] = data['LightBG2']['color_back']
            th['color_added'] = data['LightBG3']['color_back']
            th['color_deleted'] = data['LightBG1']['color_back']
            th['color_gaps'] = data['LightBG5']['color_back']
            # Ignored-difference colors (suppressed blank-line regions '
            # and their compensating gaps) default to the editor text
            # background (UI theme EdTextBg -- the same source the
            # overview background uses), so by default an ignored
            # region reads as "not a difference": lines look like
            # normal text and the gap looks like empty space. Users who
            # want WinMerge's visible "ignored difference" look can set
            # explicit colors.
            try:
                ui = ct.app_proc(ct.PROC_THEME_UI_DICT_GET, '')
                ed_bg = ui.get('EdTextBg', {}).get('color', 0xFFFFFF)
            except Exception:
                ed_bg = 0xFFFFFF
            th['color_ignored'] = ed_bg
            th['color_ignored_gap'] = ed_bg
            return th

        t = get_theme()
        config = {
            'opt_time':
                os.path.getmtime(JSONPATH) if os.path.exists(JSONPATH) else 0,
            'theme_name':
                ct.app_proc(ct.PROC_THEME_SYNTAX_GET, ''),
            # --- theme ---
            'color_changed':
                get_color('theme.changed_color', t.get('color_changed')),
            'color_added':
                get_color('theme.added_color', t.get('color_added')),
            'color_deleted':
                get_color('theme.deleted_color', t.get('color_deleted')),
            'color_gaps':
                get_color('theme.gap_color', t.get('color_gaps')),
            'color_ignored':
                get_color('theme.ignored_color', t.get('color_ignored')),
            'color_ignored_gap':
                get_color('theme.ignored_gap_color', t.get('color_ignored_gap')),
            # --- algorithm ---
            'diff_algorithm':
                get_opt('algorithm.diff_algorithm', 'native_histogram'),
            'compare_with_details':
                get_opt('algorithm.compare_with_details', True),
            'beautify_alignment':
                get_opt('algorithm.beautify_alignment', True),
            # --- ignore options (diff_proc DIFF_IGN_* flags; collected
            # into the bitmask for the native algorithms by
            # differ_native.build_ignore_flags -- see _refresh_ex) ---
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
            'diff_context':
                get_opt('advanced.diff_context', 3),
            'enable_profiling':
                get_opt('advanced.enable_profiling', False),
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
        """Jump caret to the next (or previous) diff hunk in the focused editor.
        Wraps around at the end/start of the diffmap."""
        if not self.diff.diffmap:
            self.refresh()
        cnt = len(self.diff.diffmap)
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
            for n, dif in enumerate(self.diff.diffmap):
                df_y = dif[p] if dif[p] <= line_cnt - 1 else line_cnt - 1
                if y < df_y:
                    i = n
                    break
        else: # to prev
            for n, dif in reversed(list(enumerate(self.diff.diffmap))):
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
        to = self.diff.diffmap[i]
        ct.msg_status(_("{} of {} difference").format(i+1, cnt))
        a_line_cnt = eds[0].get_line_count()
        b_line_cnt = eds[1].get_line_count()
        to0 = to[0] if to[0] <= a_line_cnt - 1 else a_line_cnt - 1
        to2 = to[2] if to[2] <= b_line_cnt - 1 else b_line_cnt - 1
        eds[0].set_caret(0, to0, id=ct.CARET_SET_ONE)
        eds[1].set_caret(0, to2, id=ct.CARET_SET_ONE)

    def jump_next(self):
        """Jump to the next diff hunk."""
        self.jump()

    def jump_prev(self):
        """Jump to the previous diff hunk."""
        self.jump(False)

    @property
    def get_current_change(self):
        """Return the diffmap entry [a0, a1, b0, b1] containing the caret
        in the focused editor, or None if the caret is not inside a diff hunk."""
        if not self.diff.diffmap:
            self.refresh()
        fc, eds = self.focused
        p = fc * 2
        y = eds[fc].get_carets()[0][1]
        for dif in self.diff.diffmap:
            if dif[p] <= y < dif[p+1]:
                return dif

    def select_current(self):
        """Select the lines of the current diff hunk in both editors."""
        cur_change = self.get_current_change
        if not cur_change:
            return
        esc = self.cfg.get('enable_sync_caret', False)
        fc, eds = self.focused
        self.cfg['enable_sync_caret'] = False
        eds[0].set_caret(0, cur_change[0], 0, cur_change[1])
        eds[1].set_caret(0, cur_change[2], 0, cur_change[3])
        self.cfg['enable_sync_caret'] = esc

    def _compare_running_here(self, eds):
        """True while a background compare is in flight for the compare
        tab the given halves belong to. Text-changing hunk commands
        (copy / copy_line) are refused then: with
        LOCK_EDITORS_WHILE_COMPARING the halves are read-only for the
        whole run, and the running compare paints its kick-off
        snapshots -- any text edit now would end up misaligned. Also
        works with the constant off, where editing IS possible but the
        running compare would still paint stale snapshots."""
        if not eds:
            return False
        try:
            tab_id = eds[0].get_prop(ct.PROP_TAB_ID)
        except Exception:
            return False
        return str(tab_id) in self._jobs

    def copy(self, to_right=True):
        """Copy the current diff hunk's text from left to right (or right to
        left), replacing the opposite side's text. Then refresh diff markers."""
        fc, eds = self.focused
        if self._compare_running_here(eds):
            return ct.msg_status(_('Differ: cannot edit while compare is running'))
        current = self.get_current_change
        if not current:
            return
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
        self.refresh()

    def copy_right(self):
        """Copy current hunk from left editor to right editor."""
        self.copy(True)

    def copy_left(self):
        """Copy current hunk from right editor to left editor."""
        self.copy(False)

    def copy_line(self, to_right=True):
        """Copy the caret's line(s) from left to right (or right to left),
        inserting at the current hunk's position. Unlike copy(), this works
        on the current caret line, not the whole hunk."""
        fc, eds = self.focused
        if self._compare_running_here(eds):
            return ct.msg_status(_('Differ: cannot edit while compare is running'))
        current = self.get_current_change

        def get_lines(ed: ct.Editor):
            carets = ed.get_carets()
            if len(carets) != 1:
                return []
            caret = carets[0]
            __, y1, __, y2 = caret
            if y2 == -1:
                return ed.get_text_line(y1) + '\n'
            else:
                return ''.join([ed.get_text_line(y)+'\n' for y in range(y1, y2)])

        if not current:
            return
        else:
            a0, a1, b0, b1 = current
        if to_right:
            if fc == 1:
                return
            text = get_lines(eds[0])
            if text:
                eds[1].insert(0, b0, text)
        else:
            if fc == 0:
                return
            text = get_lines(eds[1])
            if text:
                eds[0].insert(0, a0, text)
        self.refresh()

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
        Otherwise, map the caret line through the diffmap to the corresponding
        line on the opposite side."""
        if not self.diff.diffmap:
            return
        fc, eds = self.focused
        op = 0 if fc else 1
        x, y = eds[fc].get_carets()[0][:2]

        esc = self.cfg.get('enable_sync_caret', False)
        p = fc * 2
        for dif in self.diff.diffmap:
            if dif[p] <= y < dif[p+1]:
                self.cfg['enable_sync_caret'] = False
                eds[op].set_caret(0, dif[op*2])
                self.cfg['enable_sync_caret'] = esc
                return
        for dif in self.diff.diffmap:
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
        open tabs), and 'Refresh'. Only shown for valid compare candidates."""
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

        # Add a separator and "Refresh" entry at the end of the context menu.
        # Only enabled when the current tab is a compare tab managed by Differ.
        ct.menu_proc(self.compare_menu, ct.MENU_ADD, caption='-')
        self.menuid_refresh = ct.menu_proc(self.compare_menu, ct.MENU_ADD,
            command='module=cuda_differ;cmd=tabmenu_refresh;',
            caption=_('Refresh')
            )
        is_compare = self._is_compare_tab(cur_ed.get_prop(ct.PROP_TAB_ID))
        ct.menu_proc(self.menuid_refresh, ct.MENU_SET_ENABLED,
            command=is_compare)

        # Separator + the ignore options (see _IGNORE_OPTS), right below
        # 'Refresh'.
        # The tab context menu is rebuilt from scratch by this method on
        # every right-click (on_tab_menu fires each time), so the
        # checkmarks always mirror the current settings file: changing an
        # option in the config dialog is reflected here automatically, and
        # toggling here writes it back via set_opt (tabmenu_ignore) --
        # two-way sync with zero extra bookkeeping.
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
        # THIS tab, 'Cancel all compares' on every compare tab. Both
        # are enabled only while a compare is actually running; the
        # menu is rebuilt on every right-click, so the enabled state
        # is always fresh.
        ct.menu_proc(self.compare_menu, ct.MENU_ADD, caption='-')
        cur_tab_id_str = str(cur_ed.get_prop(ct.PROP_TAB_ID))
        self.menuid_cancel = ct.menu_proc(self.compare_menu, ct.MENU_ADD,
            command='module=cuda_differ;cmd=cancel_compare;',
            caption=_('Cancel compare')
            )
        ct.menu_proc(self.menuid_cancel, ct.MENU_SET_ENABLED,
            command=cur_tab_id_str in self._jobs)
        self.menuid_cancel_all = ct.menu_proc(self.compare_menu, ct.MENU_ADD,
            command='module=cuda_differ;cmd=cancel_all_compares;',
            caption=_('Cancel all compares')
            )
        ct.menu_proc(self.menuid_cancel_all, ct.MENU_SET_ENABLED,
            command=bool(self._jobs))

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
        """Refresh the compare tab -- re-applies diff markers."""
        callback = 'module=cuda_differ;cmd=tabmenu_refresh_timer;info=_;'
        ct.timer_proc(ct.TIMER_START_ONE, callback, 100)

    def tabmenu_refresh_timer(self, tag='', info=''):
        """Timer callback that actually runs the refresh."""
        self.refresh()

    def tabmenu_ignore(self, info):
        """Toggle one 'differ.ignoreopt.*' option from the diff-tab context
        menu (checkable items below 'Refresh'): persist it to
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
        # _refresh_ex calls config() first, which detects the settings-file
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
        if not self.diff.diffmap:
            self.refresh()
        if len(self.diff.diffmap) == 0:
            return ct.msg_status(_("No differences were found"))
        fc, eds = self.focused
        y1,y2 = (0,1) if fc == 0 else (2,3)

        for n, dif in enumerate(self.diff.diffmap):
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
        if not self._is_compare_tab(tab_id):
            return  # not a compare tab -- CudaText handles it normally
        if self._get_dirty_halves(tab_id):
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
        """Fires after the close is confirmed. For a compare tab: unregister
        it from the persisted state. If this was the last compare tab,
        disable autostart. No temp files to delete (split-tab approach).

        During app exit, the state entry and autostart are preserved so
        the compare tab can be restored after restart."""
        tab_id = ed_self.get_prop(ct.PROP_TAB_ID)
        entry = self._unregister_compare_tab(tab_id)
        if entry is None:
            return  # not a compare tab

        # Remove from in-memory scroll set.
        try:
            self.scroll.tab_id.discard(int(tab_id))
        except (ValueError, TypeError):
            pass

        # Destroy the paintbox overview for this tab.
        tab_id_str = str(tab_id)
        overview = self._overviews.pop(tab_id_str, None)
        if overview is not None:
            overview.destroy()

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
        job = self._jobs.pop(tab_id_str, None)
        if job is not None:
            self._cancel_job(job)

        # During app exit, keep the state entry and autostart subscription
        # so compare tabs persist restarts and the plugin auto-loads.
        # Re-register since we already unregistered above, preserving the
        # saved/dirty state (per-half dirty flags + legacy 'saved' flag)
        # so on_start2 can restore the correct title color and a restart
        # save still syncs only the halves that were dirty before exit.
        if getattr(self, '_app_exiting', False):
            self._register_compare_tab(
                tab_id,
                entry.get('primary_orig_tab_id'),
                entry.get('secondary_orig_tab_id'),
                entry.get('primary_orig_name', ''),
                entry.get('secondary_orig_name', ''),
                self._current_session_key,
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

        # If no more compare tabs are open in the current session,
        # disable autostart so the plugin does not load on next startup.
        state = self._load_state()
        if not state['sessions'].get(self._current_session_key, {}):
            self._disable_autostart()
