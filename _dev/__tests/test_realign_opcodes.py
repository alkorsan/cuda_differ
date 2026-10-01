#!/usr/bin/env python3
"""Tests and worked examples for the opcode realignment pass
(py_algo/realign.py -- realign_opcodes).

Runs STANDALONE with plain python3 -- no CudaText, no plugin
installation: the only imports are the plugin's own pure-Python
algorithm packages (py_algo/*) and the standard library. Run it from
anywhere:

    python3 _dev/__tests/test_realign_opcodes.py

WHAT _realign... sorry, WHAT realign_opcodes() DOES (the short version)
======================================================================

diff engines return an opcode list [(tag, i1, i2, j1, j2), ...] that
tiles both files. When several equally-cheap edit scripts exist, an
engine may pick one that matches a TRIVIAL line (empty line, lone
brace) instead of a meaningful one, producing two artifacts:

  Pass 1 target -- INSERT + EQUAL(trivial) + DELETE:
      the same content is shown as "one added line" + "one deleted
      line" instead of a paired change. Happens when the engine
      matches a blank/brace line across the change instead of pairing
      the changed lines with each other.

  Pass 2 target -- replace + EQUAL(trivial) + replace (one replace
      large): one big changed region is fragmented into several
      pieces by a matched trivial line inside it (a blank line, a
      '}'). The fragments make positional pairing match a line with
      the WRONG line of the other file and read as several unrelated
      changes.

realign_opcodes() merges those into single REPLACE blocks -- the same
normalization VS Code runs unconditionally inside its own diff
algorithm (heuristicSequenceOptimizations.ts). It never touches an
EQUAL block with more than 4 non-whitespace characters, so matched
real content is never absorbed.

Every test below prints a side-by-side rendering (LEFT file | RIGHT
file) of the opcodes before and after the pass, so the effect is
visible, not just asserted. Expected opcode lists are hard-coded --
they are the point of the test.

The two crafted engine inputs used here were verified to produce the
exact raw opcodes listed below on ALL of: difflib, myers, patience,
vscode (and the same patterns were measured on the native engines via
the Pascal test harness during the investigation -- see
readme/history.txt 2026.10.01).
"""

import os
import sys

# ---------------------------------------------------------------------------
# Standalone bootstrap: make the plugin's py_algo importable without
# CudaText. py_algo/* are pure Python (no cudatext imports); the plugin
# package's __init__ is NOT importable outside CudaText, so the plugin
# root is put on sys.path directly and py_algo is imported as a
# top-level (namespace) package -- the same modules differ_python.py /
# differ_native.py import via relative import inside CudaText.
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_PLUGIN_ROOT = os.path.abspath(os.path.join(_HERE, '..', '..'))
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)

from py_algo.realign import (  # noqa: E402
    realign_opcodes, TRIVIAL_THRESHOLD, MIN_LARGE_REPLACE)
from py_algo.myers_onp_diff import MyersSequenceMatcher  # noqa: E402
from py_algo.patience_diff.patiencediff import (  # noqa: E402
    PatienceSequenceMatcher)
from py_algo.vscode_diff import VSCodeSequenceMatcher  # noqa: E402
from difflib import SequenceMatcher as DefaultSequenceMatcher  # noqa: E402


def split_lines(text):
    """Minimal keepends line split on \\n / \\r\\n / \\r.

    Same convention as the plugin's utils.split_lines_safe (which we
    cannot import here: utils.py imports cudatext). The engine inputs
    in these tests use \\n only, so this is exact for them.
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
    if name == 'myers':
        return MyersSequenceMatcher(None, a, b)
    if name == 'patience':
        return PatienceSequenceMatcher(None, a, b)
    if name == 'vscode':
        return VSCodeSequenceMatcher(None, a, b)
    if name == 'difflib':
        return DefaultSequenceMatcher(None, a, b, autojunk=False)
    raise ValueError(name)


def fmt_ops(opcodes):
    """One opcode per line, difflib notation."""
    return '\n'.join(
        '  %-8s a[%d:%d] b[%d:%d]' % (t, i1, i2, j1, j2)
        for (t, i1, i2, j1, j2) in opcodes)


def render(a, b, opcodes, width=26):
    """Render opcodes as a side-by-side view, the way the compare tab
    paints them: '=' aligned pairs, '<' A-only (deleted), '>' B-only
    (added), '|' changed pair (replace, positional). Gaps are shown as
    blank cells -- exactly the visual artifact realign fixes."""
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


def show(title, a, b, raw, realigned):
    """Print a before/after block for one example."""
    print('\n' + '=' * 72)
    print(title)
    print('=' * 72)
    print('--- opcodes BEFORE realign_opcodes():')
    print(fmt_ops(raw))
    print('--- side-by-side BEFORE:')
    print(render(a, b, raw))
    print('--- opcodes AFTER realign_opcodes():')
    print(fmt_ops(realigned))
    print('--- side-by-side AFTER:')
    print(render(a, b, realigned))


# ---------------------------------------------------------------------------
# The two crafted inputs. Verified: difflib, myers, patience AND vscode
# all return exactly the raw opcode lists below on them.
# ---------------------------------------------------------------------------

# Pass 1 example. The engine matches the BLANK line (a[3] == b[1]) and
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
P1_REALIGNED = [
    ('equal', 0, 1, 0, 1),
    ('replace', 1, 4, 1, 4),    # alpha/beta paired with the B lines
    ('equal', 4, 5, 4, 5),
]

# Pass 2 example. One 6-line change (p1..p3 -> P1..P3, q1..q3 ->
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
P2_REALIGNED = [
    ('equal', 0, 1, 0, 1),
    ('replace', 1, 8, 1, 8),
    ('equal', 8, 9, 8, 9),
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_pass1_merge():
    """Pass 1: insert + equal(trivial) + delete -> one replace."""
    a = split_lines(P1_A)
    got = realign_opcodes(a, P1_RAW)
    show('Pass 1 -- INSERT + EQUAL(blank) + DELETE becomes one REPLACE',
         a, split_lines(P1_B), P1_RAW, got)
    assert [tuple(x) for x in got] == P1_REALIGNED, got
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
    got2 = realign_opcodes(a2, mirrored)
    assert [tuple(x) for x in got2] == [
        ('equal', 0, 1, 0, 1),
        ('replace', 1, 4, 1, 4),
        ('equal', 4, 5, 4, 5),
    ], got2


def test_pass1_meaningful_equal_not_absorbed():
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
    got = realign_opcodes(a, ops)
    assert [tuple(x) for x in got] == [tuple(x) for x in ops], got


def test_pass1_same_tag_neighbors_not_merged():
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
    got = realign_opcodes(a, ops)
    assert [tuple(x) for x in got] == [tuple(x) for x in ops], got


def test_pass2_absorb():
    """Pass 2: a trivial equal between two replaces, one of them
    large, is absorbed into a single replace."""
    a = split_lines(P2_A)
    got = realign_opcodes(a, P2_RAW)
    show('Pass 2 -- matched blank inside one big change is absorbed',
         a, split_lines(P2_B), P2_RAW, got)
    assert [tuple(x) for x in got] == P2_REALIGNED, got


def test_pass2_small_neighbors_stay_separate():
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
    got = realign_opcodes(a, ops)
    assert [tuple(x) for x in got] == [tuple(x) for x in ops], got


def test_pass2_ignore_hunk_is_barrier():
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
    got = realign_opcodes(a, ops)
    assert [tuple(x) for x in got] == [tuple(x) for x in ops], got
    # The mirrored case (ignore BEFORE the trivial equal) is a barrier
    # too.
    ops2 = [
        ('equal', 0, 1, 0, 1),
        ('ignore', 1, 5, 1, 4),
        ('equal', 5, 6, 4, 5),
        ('replace', 6, 10, 5, 9),
        ('equal', 10, 11, 9, 10),
    ]
    got2 = realign_opcodes(a, ops2)
    assert [tuple(x) for x in got2] == [tuple(x) for x in ops2], got2


def test_short_input_untouched():
    """Lists too short to contain any pattern come back as-is."""
    ops = [('equal', 0, 1, 0, 1), ('insert', 1, 1, 1, 2)]
    assert realign_opcodes(['a\n', 'b\n'], ops) is ops
    assert realign_opcodes(['a\n'], []) == []


def test_idempotent():
    """realign(realign(x)) == realign(x) on every engine's output."""
    for name in ('difflib', 'myers', 'patience', 'vscode'):
        for at, bt in ((P1_A, P1_B), (P2_A, P2_B)):
            a = split_lines(at)
            b = split_lines(bt)
            ops = list(get_matcher(name, a, b).get_opcodes())
            once = realign_opcodes(a, ops)
            twice = realign_opcodes(a, once)
            assert [tuple(x) for x in twice] == [tuple(x) for x in once], \
                (name, at[:8])


def test_coverage_invariant():
    """After realign, opcodes still tile both inputs exactly and every
    equal block keeps i2-i1 == j2-j1 (the 1:1 pairing invariant the
    paint walk relies on)."""
    for name in ('difflib', 'myers', 'patience', 'vscode'):
        for at, bt in ((P1_A, P1_B), (P2_A, P2_B)):
            a = split_lines(at)
            b = split_lines(bt)
            ops = [tuple(x) for x in
                   realign_opcodes(a, list(get_matcher(name, a, b)
                                           .get_opcodes()))]
            assert ops, (name, at[:8])
            assert ops[0][1] == 0 and ops[0][3] == 0, ops[0]
            assert ops[-1][2] == len(a) and ops[-1][4] == len(b), ops[-1]
            for k, (t, i1, i2, j1, j2) in enumerate(ops):
                if k:
                    assert (i1 == ops[k - 1][2]
                            and j1 == ops[k - 1][4]), (name, k)
                if t == 'equal':
                    assert i2 - i1 == j2 - j1, (name, k, t)


def _has_p1(a, ops):
    """True when ops contains the Pass-1 pattern: an insert and a
    delete separated by an EQUAL block whose non-whitespace content is
    <= TRIVIAL_THRESHOLD characters (the pass's own criterion -- a
    ']' + blank block counts, a blank-only check would miss it)."""
    for k in range(1, len(ops) - 1):
        if (ops[k][0] == 'equal'
                and ops[k - 1][0] in ('insert', 'delete')
                and ops[k + 1][0] in ('insert', 'delete')
                and ops[k - 1][0] != ops[k + 1][0]):
            text = ''.join(a[ops[k][1]:ops[k][2]])
            non_ws = (text.replace(' ', '').replace('\t', '')
                           .replace('\n', '').replace('\r', ''))
            if len(non_ws) <= TRIVIAL_THRESHOLD:
                return True
    return False


def test_engines_produce_the_pattern_and_get_fixed():
    """Engine-level: every pure-Python engine emits the Pass-1 pattern
    on the crafted input, and realign_opcodes normalizes all of them to
    the SAME structure."""
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
        has_pattern = _has_p1(a, raw)
        fixed = [tuple(x) for x in realign_opcodes(a, raw)]
        print('%-8s raw pattern: %s   realigned: %s'
              % (name, 'YES' if has_pattern else 'no',
                 fmt_ops(fixed).splitlines()[1].strip()))
        assert has_pattern, (name, raw)
        assert fixed == P1_REALIGNED, (name, fixed)


def test_real_corpus_smoke():
    """Smoke test on the plugin's real corpus files (skipped when they
    are not next to this script): patience emits the known
    insert + equal(']' + blank) + delete triple around test_2a.py:93,
    and realign merges it into the single replace the compare view is
    supposed to paint."""
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
    fixed = [tuple(x) for x in realign_opcodes(a, raw)]

    print('\n' + '=' * 72)
    print('Corpus smoke -- test_2a.py vs test_2b.py (patience engine)')
    print('=' * 72)
    print('opcodes: %d raw -> %d realigned; Pass-1 pattern: %s -> %s'
          % (len(raw), len(fixed),
             'present' if _has_p1(a, raw) else 'absent',
             'present' if _has_p1(a, fixed) else 'absent'))
    # the raw engine DOES contain the artifact on this real pair --
    # the known one is insert b[125:131] + equal a[93:95] (']' + blank,
    # 1 non-ws char) + delete a[95:96]...
    assert _has_p1(a, raw), 'expected the known pattern in raw output'
    # ...and the pass removes it.
    assert not _has_p1(a, fixed), fixed
    # coverage still exact
    assert fixed[0][1] == 0 and fixed[0][3] == 0
    assert fixed[-1][2] == len(a) and fixed[-1][4] == len(b)


# ---------------------------------------------------------------------------

def main():
    print('realign_opcodes test suite')
    print('TRIVIAL_THRESHOLD = %d, MIN_LARGE_REPLACE = %d'
          % (TRIVIAL_THRESHOLD, MIN_LARGE_REPLACE))
    tests = [
        test_pass1_merge,
        test_pass1_meaningful_equal_not_absorbed,
        test_pass1_same_tag_neighbors_not_merged,
        test_pass2_absorb,
        test_pass2_small_neighbors_stay_separate,
        test_pass2_ignore_hunk_is_barrier,
        test_short_input_untouched,
        test_idempotent,
        test_coverage_invariant,
        test_engines_produce_the_pattern_and_get_fixed,
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
