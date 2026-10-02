#!/usr/bin/env python3
"""Tests and worked examples for the opcode beautify pass
(_absorb_trivial_equal_blocks -- one copy at the end of
differ_python.py, one at the end of differ_native.py).

Runs STANDALONE with plain python3 -- no CudaText, no plugin
installation: cudatext / cudax_lib are stubbed, and the two differ
modules are imported through a dummy 'cuda_differ2' package (the
plugin's real __init__.py needs the full CudaText API and is never
executed). Run it from anywhere:

    python3 _dev/__tests/test_absorb_trivial_equal_blocks.py

WHAT _absorb_trivial_equal_blocks DOES (the short version)
==========================================================

diff engines return an opcode list [(tag, i1, i2, j1, j2), ...] that
tiles both files. When several equally-cheap edit scripts exist, an
engine may pick one that matches a TRIVIAL line (empty line, lone
brace) instead of a meaningful one, producing two artifacts:

  STEP 1 target -- INSERT + EQUAL(trivial) + DELETE:
      the same content is shown as "one added line" + "one deleted
      line" instead of a paired change. Happens when the engine
      matches a blank/brace line across the change instead of pairing
      the changed lines with each other.

  STEP 2 target -- replace + EQUAL(trivial) + replace (one replace
      large): one big changed region is fragmented into several
      pieces by a matched trivial line inside it (a blank line, a
      '}'). The fragments make positional pairing match a line with
      the WRONG line of the other file and read as several unrelated
      changes.

_absorb_trivial_equal_blocks() merges those into single REPLACE
blocks -- the same beautify VS Code runs unconditionally inside its
own diff algorithm (heuristicSequenceOptimizations.ts). It never
touches an EQUAL block with more than 4 non-whitespace characters, so
matched real content is never absorbed.

The pass is OPT-IN: it runs only when the config option
'differ2.algorithm.beautify.absorb_trivial_equal_blocks' is ON
(default OFF -- raw, algo-faithful engine output). The tests in the
"option gating" section verify both sides of that switch, on the
Python differ (engine_opcodes) AND the native differ
(collect_char_pairs / compare_lists); the last section verifies the
unified-diff commands always use the RAW algorithms.

Every test below prints a side-by-side rendering (LEFT file | RIGHT
file) of the opcodes before and after the pass, so the effect is
visible, not just asserted. Expected opcode lists are hard-coded --
they are the point of the test.

The two crafted engine inputs used here were verified to produce the
exact raw opcodes listed below on ALL of: difflib, myers, patience,
vscode (and the same patterns were measured on the native engines via
the Pascal test harness during the investigation -- see
readme/history.txt 2026.10.01/02).
"""

import importlib
import os
import sys
import types

# ---------------------------------------------------------------------------
# Standalone bootstrap.
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
# The plugin root is normally derived from this file's location
# (<plugin>/_dev/__tests/). The CUDA_DIFFER2_TEST_ROOT environment
# variable overrides it, so the suite can also be run FROM ANYWHERE
# against any checkout (used by the packaging deploy-check).
_PLUGIN_ROOT = os.environ.get(
    'CUDA_DIFFER2_TEST_ROOT',
    os.path.abspath(os.path.join(_HERE, '..', '..')))

# Stub the CudaText modules the differ files import (used only for the
# engine calls / i18n -- none of which these tests exercise). When the
# real 'cudatext' is already importable (running inside CudaText), it
# is kept as-is and the engine paths simply work.
if 'cudatext' not in sys.modules:
    _ct_stub = types.ModuleType('cudatext')
    for _name, _val in (('DIFF_IGN_NONE', 0), ('DIFF_IGN_CASE', 1),
                        ('DIFF_IGN_WHITESPACE', 2), ('DIFF_IGN_EOL', 4),
                        ('DIFF_IGN_NUMBERS', 8),
                        ('DIFF_IGN_BLANK_LINES', 16)):
        setattr(_ct_stub, _name, _val)
    # NO diff_proc attribute -> differ_native._HAS_NATIVE_DIFF is False:
    # the native module must still import, and its Differ falls back to
    # the Python char_diff on the paths these tests walk.
    sys.modules['cudatext'] = _ct_stub
if 'cudax_lib' not in sys.modules:
    _cudax_stub = types.ModuleType('cudax_lib')
    _cudax_stub.get_translation = lambda fn: (lambda s: s)
    sys.modules['cudax_lib'] = _cudax_stub

# Import the differ modules WITHOUT executing the plugin's real
# __init__.py: a dummy 'cuda_differ2' package whose __path__ points at
# the plugin directory. The relative imports inside the differ files
# (.py_algo.*, .profiling, .utils) resolve through that path. When the
# REAL plugin package is already imported (running inside CudaText),
# it is used as-is instead.
if 'cuda_differ2' not in sys.modules:
    _pkg = types.ModuleType('cuda_differ2')
    _pkg.__path__ = [_PLUGIN_ROOT]
    _pkg.__package__ = 'cuda_differ2'
    sys.modules['cuda_differ2'] = _pkg

dfp = importlib.import_module('cuda_differ2.differ_python')
dfn = importlib.import_module('cuda_differ2.differ_native')
uni = importlib.import_module('cuda_differ2.unidiff')

# Keep the test output readable: silence the Differ's benchmark prints.
dfp._BENCHMARK = False
dfn._BENCHMARK = False

# The two copies under test. Everything functional runs through BOTH
# and the results must be identical (the copies are deliberate
# duplicates -- see the module docstrings -- but they must not drift).
ABSORBERS = (
    ('differ_python', dfp._absorb_trivial_equal_blocks),
    ('differ_native', dfn._absorb_trivial_equal_blocks),
)

# Matchers by config name (imported into differ_python's namespace --
# the same classes engine_opcodes builds).
MATCHERS = {
    'myers': dfp.MyersSequenceMatcher,
    'patience': dfp.PatienceSequenceMatcher,
    'vscode': dfp.VSCodeSequenceMatcher,
}


def split_lines(text):
    """Minimal keepends line split on \\n / \\r\\n / \\r.

    Same convention as the plugin's utils.split_lines_safe (which we
    cannot always import here: utils.py imports cudatext -- with the
    stub above it works, but the standalone split keeps this file
    self-contained). The engine inputs in these tests use \\n only, so
    this is exact for them.
    """
    if not text:
        return []
    lines = []
    start = 0
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == '\n':
            lines.append(text[start:i + 1])
            start = i + 1
        elif c == '\r':
            if i + 1 < n and text[i + 1] == '\n':
                lines.append(text[start:i + 2])
                start = i + 2
                i += 1
            else:
                lines.append(text[start:i + 1])
                start = i + 1
        i += 1
    if start < n:
        lines.append(text[start:])
    return lines


def get_matcher(name, a, b):
    """Build one of the plugin's pure-Python matchers by config name."""
    if name == 'difflib':
        return dfp.DefaultSequenceMatcher(None, a, b, autojunk=False)
    return MATCHERS[name](None, a, b)


def fmt_ops(opcodes):
    """One opcode per line, difflib notation."""
    return '\n'.join(
        '  %-8s a[%d:%d] b[%d:%d]' % (t, i1, i2, j1, j2)
        for (t, i1, i2, j1, j2) in opcodes)


def render(a, b, opcodes, width=26):
    """Render opcodes as a side-by-side view, the way the compare tab
    paints them: '=' aligned pairs, '<' A-only (deleted), '>' B-only
    (added), '|' changed pair (replace, positional). Gaps are shown as
    blank cells -- exactly the visual artifact the absorb pass fixes."""
    out = []
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == 'equal':
            for k in range(i2 - i1):
                left = a[i1 + k].rstrip('\n\r')
                right = b[j1 + k].rstrip('\n\r')
                out.append('  %-*s = %s' % (width, left[:width], right))
        elif tag == 'delete':
            for k in range(i1, i2):
                out.append('  %-*s <' % (width, a[k].rstrip('\n\r')[:width]))
        elif tag == 'insert':
            for k in range(j1, j2):
                out.append('  %-*s   > %s'
                           % (width, '', b[k].rstrip('\n\r')))
        elif tag in ('replace', 'ignore'):
            mark = '|' if tag == 'replace' else '#'
            n = max(i2 - i1, j2 - j1)
            for k in range(n):
                left = (a[i1 + k].rstrip('\n\r')
                        if k < i2 - i1 else '')
                right = (b[j1 + k].rstrip('\n\r')
                         if k < j2 - j1 else '')
                out.append('  %-*s %s %s'
                           % (width, left[:width], mark, right))
    return '\n'.join(out)


def show(title, a, b, raw, absorbed):
    """Print a before/after block for one example."""
    print('\n' + '=' * 72)
    print(title)
    print('=' * 72)
    print('--- opcodes BEFORE _absorb_trivial_equal_blocks():')
    print(fmt_ops(raw))
    print('--- side-by-side BEFORE:')
    print(render(a, b, raw))
    print('--- opcodes AFTER _absorb_trivial_equal_blocks():')
    print(fmt_ops(absorbed))
    print('--- side-by-side AFTER:')
    print(render(a, b, absorbed))


# ---------------------------------------------------------------------------
# The two crafted inputs. Verified: difflib, myers, patience AND vscode
# all return exactly the raw opcode lists below on them.
# ---------------------------------------------------------------------------

# STEP 1 example. The engine matches the BLANK line (a[3] == b[1]) and
# shows alpha/beta as deleted + alpha2/beta2 as inserted, instead of
# pairing them as one change.
P1_A = "head\nalpha\nbeta\n\ntail\n"
P1_B = "head\n\nalpha2\nbeta2\ntail\n"
P1_RAW = [
    ('equal', 0, 1, 0, 1),
    ('delete', 1, 3, 1, 1),
    ('equal', 3, 4, 1, 2),      # the blank line -- trivial (0 non-ws)
    ('insert', 4, 4, 2, 4),
    ('equal', 4, 5, 4, 5),
]
P1_ABSORBED = [
    ('equal', 0, 1, 0, 1),
    ('replace', 1, 4, 1, 4),    # alpha/beta paired with the B lines
    ('equal', 4, 5, 4, 5),
]

# STEP 2 example. One 6-line change (p1..p3 -> P1..P3, q1..q3 ->
# Q1..Q3) is fragmented by the matched blank line in the middle; each
# half is 6 combined lines (>= MIN_LARGE_REPLACE), so the blank is
# absorbed and the region becomes one replace.
P2_A = "h\np1\np2\np3\n\nq1\nq2\nq3\nt\n"
P2_B = "h\nP1\nP2\nP3\n\nQ1\nQ2\nQ3\nt\n"
P2_RAW = [
    ('equal', 0, 1, 0, 1),
    ('replace', 1, 4, 1, 4),
    ('equal', 4, 5, 4, 5),      # the blank line -- trivial
    ('replace', 5, 8, 5, 8),
    ('equal', 8, 9, 8, 9),
]
P2_ABSORBED = [
    ('equal', 0, 1, 0, 1),
    ('replace', 1, 8, 1, 8),
    ('equal', 8, 9, 8, 9),
]


# ---------------------------------------------------------------------------
# Tests -- the pass itself (run on BOTH copies)
# ---------------------------------------------------------------------------

def test_copies_agree_and_default_off():
    """The two copies are behaviorally identical, expose the same
    thresholds, and both Differ classes default the option to OFF."""
    assert dfp.TRIVIAL_THRESHOLD == dfn.TRIVIAL_THRESHOLD
    assert dfp.MIN_LARGE_REPLACE == dfn.MIN_LARGE_REPLACE
    # the option defaults to False on both differs (raw = default)
    assert dfp.Differ().absorb_trivial_equal_blocks is False
    assert dfn.Differ().absorb_trivial_equal_blocks is False
    assert dfp.Differ().align_by_similarity is False
    assert dfn.Differ().align_by_similarity is False
    # same output on a battery of inputs
    cases = [
        (P1_A, P1_RAW),
        (P2_A, P2_RAW),
        ('h\na1\na2\n\nb1\nt\n', [
            ('equal', 0, 1, 0, 1),
            ('replace', 1, 3, 1, 3),
            ('equal', 3, 4, 3, 4),
            ('replace', 4, 5, 4, 5),
            ('equal', 5, 6, 5, 6)]),
        ('x\ndef f():\n    return 1\ny\nz\n', [
            ('equal', 0, 0, 0, 1),
            ('insert', 0, 0, 1, 2),
            ('equal', 0, 2, 2, 4),
            ('delete', 2, 4, 4, 4),
            ('equal', 4, 5, 4, 5)]),
    ]
    for text, ops in cases:
        a = split_lines(text)
        r_py = [tuple(x) for x in dfp._absorb_trivial_equal_blocks(a, ops)]
        r_nat = [tuple(x) for x in dfn._absorb_trivial_equal_blocks(a, ops)]
        assert r_py == r_nat, (text[:12], r_py, r_nat)


def test_step1_merge():
    """Step 1: insert + equal(trivial) + delete -> one replace."""
    a = split_lines(P1_A)
    for modname, absorb in ABSORBERS:
        got = absorb(a, P1_RAW)
        if modname == 'differ_python':
            show('Step 1 -- INSERT + EQUAL(blank) + DELETE becomes one '
                 'REPLACE', a, split_lines(P1_B), P1_RAW, got)
        assert [tuple(x) for x in got] == P1_ABSORBED, (modname, got)
    # The delete-first mirror merges too (a real engine order: the
    # delete before the matched blank, the insert after it).
    a2 = ['head\n', '\n', 'x1\n', 'x2\n', 'tail\n']
    mirrored = [
        ('equal', 0, 1, 0, 1),
        ('insert', 1, 1, 1, 3),
        ('equal', 1, 2, 3, 4),      # a[1:2] = the blank line -- trivial
        ('delete', 2, 4, 4, 4),
        ('equal', 4, 5, 4, 5),
    ]
    for modname, absorb in ABSORBERS:
        got2 = absorb(a2, mirrored)
        assert [tuple(x) for x in got2] == [
            ('equal', 0, 1, 0, 1),
            ('replace', 1, 4, 1, 4),
            ('equal', 4, 5, 4, 5),
        ], (modname, got2)


def test_step1_meaningful_equal_not_absorbed():
    """Guard: an equal block with real content (> 4 non-ws chars) is
    never absorbed, even between an insert and a delete."""
    a = ['x\n', 'def f():\n', '    return 1\n', 'y\n', 'z\n']
    ops = [
        ('equal', 0, 0, 0, 1),      # whatever matched before
        ('insert', 0, 0, 1, 2),     # B-only line
        ('equal', 0, 2, 2, 4),      # 'x\ndef f():\n' -- 9 non-ws chars
        ('delete', 2, 4, 4, 4),     # A-only lines
        ('equal', 4, 5, 4, 5),
    ]
    for modname, absorb in ABSORBERS:
        got = absorb(a, ops)
        assert [tuple(x) for x in got] == [tuple(x) for x in ops], \
            (modname, got)


def test_step1_same_tag_neighbors_not_merged():
    """Guard: insert + equal(trivial) + INSERT stays -- the pass only
    merges when the neighbors are one insert AND one delete (the
    'same content shown as add + del' artifact)."""
    a = ['head\n', '\n', '\n', 'tail\n']
    ops = [
        ('equal', 0, 1, 0, 1),
        ('insert', 1, 1, 1, 2),
        ('equal', 1, 2, 2, 3),      # blank, trivial
        ('insert', 2, 2, 3, 4),     # SAME tag as prev -> no merge
        ('equal', 2, 3, 4, 5),
    ]
    for modname, absorb in ABSORBERS:
        got = absorb(a, ops)
        assert [tuple(x) for x in got] == [tuple(x) for x in ops], \
            (modname, got)


def test_step2_absorb():
    """Step 2: a trivial equal between two replaces, one of them
    large, is absorbed into a single replace."""
    a = split_lines(P2_A)
    for modname, absorb in ABSORBERS:
        got = absorb(a, P2_RAW)
        if modname == 'differ_python':
            show('Step 2 -- matched blank inside one big change is '
                 'absorbed', a, split_lines(P2_B), P2_RAW, got)
        assert [tuple(x) for x in got] == P2_ABSORBED, (modname, got)


def test_step2_small_neighbors_stay_separate():
    """Guard: two SMALL changes separated by a trivial equal stay
    separate (VS Code's own '> 5 combined lines' guard)."""
    a = ['h\n', 'a1\n', 'a2\n', '\n', 'b1\n', 't\n']
    ops = [
        ('equal', 0, 1, 0, 1),
        ('replace', 1, 3, 1, 3),    # 2 + 2 = 4 lines -- small
        ('equal', 3, 4, 3, 4),      # blank, trivial
        ('replace', 4, 5, 4, 5),    # 1 + 1 = 2 lines -- small
        ('equal', 5, 6, 5, 6),
    ]
    for modname, absorb in ABSORBERS:
        got = absorb(a, ops)
        assert [tuple(x) for x in got] == [tuple(x) for x in ops], \
            (modname, got)


def test_step2_ignore_hunk_is_barrier():
    """Guard: 'ignore' hunks (native DIFF_IGN_BLANK_LINES suppressed
    all-blank differences) are BARRIERS -- merging across one would
    resurrect the suppressed lines into a shown REPLACE."""
    a = ['h\n', 'x1\n', 'x2\n', 'x3\n', 'x4\n', '\n',
         '\n', '\n', '\n', 't\n']
    ops = [
        ('equal', 0, 1, 0, 1),
        ('replace', 1, 5, 1, 5),   # 4 + 4 = 8 lines -- large
        ('equal', 5, 6, 5, 6),     # blank, trivial
        ('ignore', 6, 10, 6, 9),   # suppressed all-blank hunk
        ('equal', 10, 11, 9, 10),
    ]
    for modname, absorb in ABSORBERS:
        got = absorb(a, ops)
        assert [tuple(x) for x in got] == [tuple(x) for x in ops], \
            (modname, got)
    # The mirrored case (ignore BEFORE the trivial equal) is a barrier
    # too.
    ops2 = [
        ('equal', 0, 1, 0, 1),
        ('ignore', 1, 5, 1, 4),
        ('equal', 5, 6, 4, 5),
        ('replace', 6, 10, 5, 9),
        ('equal', 10, 11, 9, 10),
    ]
    for modname, absorb in ABSORBERS:
        got2 = absorb(a, ops2)
        assert [tuple(x) for x in got2] == [tuple(x) for x in ops2], \
            (modname, got2)


def test_short_input_untouched():
    """Lists too short to contain any pattern come back as-is."""
    ops = [('equal', 0, 1, 0, 1), ('insert', 1, 1, 1, 2)]
    for modname, absorb in ABSORBERS:
        assert absorb(['a\n', 'b\n'], ops) is ops, modname
        assert absorb(['a\n'], []) == [], modname


def test_idempotent():
    """absorb(absorb(x)) == absorb(x) on every engine's output."""
    for name in ('difflib', 'myers', 'patience', 'vscode'):
        for at, bt in ((P1_A, P1_B), (P2_A, P2_B)):
            a = split_lines(at)
            b = split_lines(bt)
            ops = list(get_matcher(name, a, b).get_opcodes())
            for modname, absorb in ABSORBERS:
                once = absorb(a, ops)
                twice = absorb(a, once)
                assert [tuple(x) for x in twice] == \
                    [tuple(x) for x in once], (modname, name, at[:8])


def test_coverage_invariant():
    """After the pass, opcodes still tile both inputs exactly and every
    equal block keeps i2-i1 == j2-j1 (the 1:1 pairing invariant the
    paint walk relies on)."""
    for name in ('difflib', 'myers', 'patience', 'vscode'):
        for at, bt in ((P1_A, P1_B), (P2_A, P2_B)):
            a = split_lines(at)
            b = split_lines(bt)
            for modname, absorb in ABSORBERS:
                ops = [tuple(x) for x in
                       absorb(a, list(get_matcher(name, a, b)
                                      .get_opcodes()))]
                assert ops, (modname, name, at[:8])
                assert ops[0][1] == 0 and ops[0][3] == 0, ops[0]
                assert ops[-1][2] == len(a) and ops[-1][4] == len(b), \
                    ops[-1]
                for k, (t, i1, i2, j1, j2) in enumerate(ops):
                    if k:
                        assert (i1 == ops[k - 1][2]
                                and j1 == ops[k - 1][4]), \
                            (modname, name, k)
                    if t == 'equal':
                        assert i2 - i1 == j2 - j1, (modname, name, k, t)


def _has_step1(a, ops):
    """True when ops contains the Step-1 pattern: an insert and a
    delete separated by an EQUAL block whose non-whitespace content is
    <= TRIVIAL_THRESHOLD characters (the pass's own criterion -- a
    ']' + blank block counts, a blank-only check would miss it)."""
    thr = dfp.TRIVIAL_THRESHOLD
    for k in range(1, len(ops) - 1):
        if (ops[k][0] == 'equal'
                and ops[k - 1][0] in ('insert', 'delete')
                and ops[k + 1][0] in ('insert', 'delete')
                and ops[k - 1][0] != ops[k + 1][0]):
            text = ''.join(a[ops[k][1]:ops[k][2]])
            non_ws = (text.replace(' ', '').replace('\t', '')
                           .replace('\n', '').replace('\r', ''))
            if len(non_ws) <= thr:
                return True
    return False


def test_engines_produce_the_pattern_and_get_fixed():
    """Engine-level: every pure-Python engine emits the Step-1 pattern
    on the crafted input, and the pass normalizes all of them to the
    SAME structure."""
    a = split_lines(P1_A)
    b = split_lines(P1_B)
    print('\n' + '=' * 72)
    print('Engine-level check -- all engines, same input, same fix')
    print('=' * 72)
    for name in ('difflib', 'myers', 'patience', 'vscode'):
        raw = [tuple(x) for x in
               get_matcher(name, a, b).get_opcodes()]
        # the pattern IS there: insert/delete separated by a trivial
        # equal block
        has_pattern = _has_step1(a, raw)
        fixed = [tuple(x) for x in
                 dfp._absorb_trivial_equal_blocks(a, raw)]
        print('%-8s raw pattern: %s   absorbed: %s'
              % (name, 'YES' if has_pattern else 'no',
                 fmt_ops(fixed).splitlines()[1].strip()))
        assert has_pattern, (name, raw)
        assert fixed == P1_ABSORBED, (name, fixed)


# ---------------------------------------------------------------------------
# Tests -- the option gating (config 'absorb_trivial_equal_blocks')
# ---------------------------------------------------------------------------

def test_option_gating_python():
    """differ_python.Differ.engine_opcodes: with the option OFF the
    engine's RAW opcodes come back (pattern present); with it ON the
    same engine's opcodes come back absorbed -- for every algorithm."""
    a = split_lines(P1_A)
    b = split_lines(P1_B)
    for algo in ('difflib', 'myers', 'patience', 'vscode'):
        d = dfp.Differ()
        d.diff_algorithm = algo
        d.absorb_trivial_equal_blocks = False
        raw = [tuple(x) for x in d.engine_opcodes(a, b)]
        assert _has_step1(a, raw), ('raw expected', algo, raw)
        d.absorb_trivial_equal_blocks = True
        fixed = [tuple(x) for x in d.engine_opcodes(a, b)]
        assert fixed == P1_ABSORBED, ('absorbed expected', algo, fixed)


def test_option_gating_native_collect():
    """differ_native.Differ.collect_char_pairs (the two-phase
    background flow): with the option ON the CALLER's opcode list is
    absorbed IN PLACE before the pair walk (the collect/replay
    invariant); with the option OFF it is left untouched."""
    # ON: in-place absorption
    d = dfn.Differ()
    d.absorb_trivial_equal_blocks = True
    ops = list(P2_RAW)
    d.collect_char_pairs(P2_A, P2_B, ops)
    assert [tuple(x) for x in ops] == P2_ABSORBED, ops
    d.drop_cached_state()
    # OFF: raw opcodes stay exactly as the engine delivered them
    d = dfn.Differ()
    d.absorb_trivial_equal_blocks = False
    ops = list(P2_RAW)
    d.collect_char_pairs(P2_A, P2_B, ops)
    assert [tuple(x) for x in ops] == P2_RAW, ops
    d.drop_cached_state()


def test_option_gating_native_compare_lists():
    """differ_native.Differ.compare_lists (the fresh-split branch --
    synchronous engine run / withdetail-off delivery): the diffmap
    reflects the absorbed hunk structure when the option is ON, the
    raw structure when it is OFF."""
    # ON: one merged replace in the diffmap
    d = dfn.Differ()
    d.absorb_trivial_equal_blocks = True
    list(d.compare_lists(P2_A, P2_B, opcodes=list(P2_RAW)))
    assert d.diffmap == [[1, 8, 1, 8]], d.diffmap
    d.drop_cached_state()
    # OFF: the two raw replaces, untouched
    d = dfn.Differ()
    d.absorb_trivial_equal_blocks = False
    list(d.compare_lists(P2_A, P2_B, opcodes=list(P2_RAW)))
    assert d.diffmap == [[1, 4, 1, 4], [5, 8, 5, 8]], d.diffmap
    d.drop_cached_state()


def test_unidiff_uses_raw_algorithms():
    """The unified-diff commands use the RAW algorithms: unidiff's
    Python path returns the engine's own opcodes (pattern present)
    regardless of any beautify option -- the flag is forced off there
    by design."""
    a = split_lines(P1_A)
    b = split_lines(P1_B)
    raw = [tuple(x) for x in uni._py_opcodes('myers', a, b)]
    assert _has_step1(a, raw), ('unidiff must be raw', raw)
    # and the raw engine output itself (same matcher, no Differ) is
    # the same list -- nothing else touched it on the way
    direct = [tuple(x) for x in
              get_matcher('myers', a, b).get_opcodes()]
    assert raw == direct, (raw, direct)


# ---------------------------------------------------------------------------
# Tests -- the real corpus
# ---------------------------------------------------------------------------

def test_real_corpus_smoke():
    """Smoke test on the plugin's real corpus files (skipped when they
    are not next to this script): patience emits the known
    insert + equal(']' + blank) + delete triple around test_2a.py:93,
    and the pass merges it into the single replace the compare view
    is supposed to paint when the option is on."""
    fa = os.path.join(_HERE, 'test_2a.py')
    fb = os.path.join(_HERE, 'test_2b.py')
    if not (os.path.exists(fa) and os.path.exists(fb)):
        print('\ncorpus smoke test SKIPPED (test_2a.py / test_2b.py '
              'not found next to this script)')
        return
    with open(fa, encoding='utf-8', newline='') as f:
        a = split_lines(f.read())
    with open(fb, encoding='utf-8', newline='') as f:
        b = split_lines(f.read())
    raw = [tuple(x) for x in
           get_matcher('patience', a, b).get_opcodes()]
    fixed = [tuple(x) for x in
             dfp._absorb_trivial_equal_blocks(a, raw)]
    fixed_nat = [tuple(x) for x in
                 dfn._absorb_trivial_equal_blocks(a, raw)]
    assert fixed == fixed_nat, 'the two copies drifted on the corpus'

    print('\n' + '=' * 72)
    print('Corpus smoke -- test_2a.py vs test_2b.py (patience engine)')
    print('=' * 72)
    print('opcodes: %d raw -> %d absorbed; Step-1 pattern: %s -> %s'
          % (len(raw), len(fixed),
             'present' if _has_step1(a, raw) else 'absent',
             'present' if _has_step1(a, fixed) else 'absent'))
    # the raw engine DOES contain the artifact on this real pair --
    # the known one is insert b[125:131] + equal a[93:95] (']' + blank,
    # 1 non-ws char) + delete a[95:96]...
    assert _has_step1(a, raw), 'expected the known pattern in raw output'
    # ...and the pass removes it.
    assert not _has_step1(a, fixed), fixed
    # coverage still exact
    assert fixed[0][1] == 0 and fixed[0][3] == 0
    assert fixed[-1][2] == len(a) and fixed[-1][4] == len(b)


# ---------------------------------------------------------------------------

def main():
    print('_absorb_trivial_equal_blocks test suite')
    print('TRIVIAL_THRESHOLD = %d, MIN_LARGE_REPLACE = %d '
          '(identical in differ_python and differ_native)'
          % (dfp.TRIVIAL_THRESHOLD, dfp.MIN_LARGE_REPLACE))
    tests = [
        test_copies_agree_and_default_off,
        test_step1_merge,
        test_step1_meaningful_equal_not_absorbed,
        test_step1_same_tag_neighbors_not_merged,
        test_step2_absorb,
        test_step2_small_neighbors_stay_separate,
        test_step2_ignore_hunk_is_barrier,
        test_short_input_untouched,
        test_idempotent,
        test_coverage_invariant,
        test_engines_produce_the_pattern_and_get_fixed,
        test_option_gating_python,
        test_option_gating_native_collect,
        test_option_gating_native_compare_lists,
        test_unidiff_uses_raw_algorithms,
        test_real_corpus_smoke,
    ]
    failed = []
    for t in tests:
        name = t.__name__
        try:
            t()
            print('PASS  %s' % name)
        except AssertionError as ex:
            failed.append(name)
            print('FAIL  %s: %r' % (name, ex))
        except Exception as ex:  # noqa: BLE001
            failed.append(name)
            import traceback
            traceback.print_exc()
            print('ERROR %s: %r' % (name, ex))
    print('\n%d/%d tests passed' % (len(tests) - len(failed), len(tests)))
    if failed:
        print('FAILED: %s' % ', '.join(failed))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
