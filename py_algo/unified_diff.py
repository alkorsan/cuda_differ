"""Unified-diff output (a fork of difflib.unified_diff with autojunk disabled).

Why this module exists instead of calling difflib.unified_diff directly:

  difflib.unified_diff builds its internal SequenceMatcher with the
  default autojunk=True and does not expose any way to change it -- see
  https://github.com/python/cpython/issues/118150. The differ plugin always
  disables the autojunk heuristic (it treats lines that appear more than 1%
  of the time as 'junk' and skips them, which produces one giant 'replace'
  block on files with duplicated boilerplate); without this fork the
  unified-diff path (the "Diff current document with file..." /
  "Diff current document with tab..." commands) would silently apply
  autojunk=True.

  Implementation is a verbatim copy of difflib.unified_diff from CPython
  with two differences:
    1. SequenceMatcher(None, a, b, autojunk=False) instead of
       SequenceMatcher(None, a, b) -- the whole point.
    2. fromfiledate/tofiledate of None fall back to an empty
       string instead of the current system timestamp.

Why the unified-diff commands always use this module (and never the chosen
diff_algorithm):

  The unified-diff output is a *patch* format -- a machine-readable stream
  consumed by patch, git apply, CI tooling, code-review bots, etc.
  It is almost never read end-to-end by a human; when a human does read it
  they read hunk headers and +/- prefixes, not line alignment. The chosen
  differ.diff_algorithm (Native Histogram, Myers, VS Code, Patience,
  ...) only changes how lines are *paired* inside REPLACE blocks for the
  side-by-side view -- it does not change the unified-diff format itself,
  and the chosen algorithm can be 10-30x slower than difflib on large
  files. Applying it to the unified-diff path would be wasted work for no
  user-visible benefit, so the unified-diff commands always go through
  difflib here (with autojunk=False to avoid the giant-replace-block
  problem on files with duplicated boilerplate).

Both differ_native.Differ.unidiff and differ_python.Differ.unidiff
import unified_diff from this module -- the function is intentionally
duplicated in neither, since the unified-diff path does not depend on
whether the side-by-side compare is being driven by a native or a Python
algorithm.
"""

from difflib import SequenceMatcher as DefaultSequenceMatcher


def _format_range_unified(start, stop):
    """Convert a (start, stop) line range to unified-diff hunk-header format.

    Verbatim copy of difflib._format_range_unified. It is a private
    helper in CPython, so we keep our own to stay independent of internal
    renames.
    """
    beginning = start + 1  # lines start numbering with one
    length = stop - start
    if length == 1:
        return '{}'.format(beginning)
    if not length:
        beginning -= 1  # empty ranges begin at line just before the range
    return '{},{}'.format(beginning, length)


def unified_diff(a, b, fromfile='', tofile='',
                 fromfiledate='', tofiledate='',
                 n=3, lineterm='\n'):
    """Produce unified diff output, same as `difflib.unified_diff` but with
    `autojunk` always disabled. See the module docstring above for why this
    exists.
    """
    if fromfiledate is None:
        fromfiledate = ''
    if tofiledate is None:
        tofiledate = ''

    started = False
    for group in DefaultSequenceMatcher(
            None, a, b, autojunk=False).get_grouped_opcodes(n):
        if not started:
            started = True
            fromdate = '\t{}'.format(fromfiledate) if fromfiledate else ''
            todate = '\t{}'.format(tofiledate) if tofiledate else ''
            yield '--- {}{}{}'.format(fromfile, fromdate, lineterm)
            yield '+++ {}{}{}'.format(tofile, todate, lineterm)
        first, last = group[0], group[-1]
        file1_range = _format_range_unified(first[1], last[2])
        file2_range = _format_range_unified(first[3], last[4])
        yield '@@ -{} +{} @@{}'.format(file1_range, file2_range, lineterm)
        for tag, i1, i2, j1, j2 in group:
            if tag == 'equal':
                for line in a[i1:i2]:
                    yield ' ' + line
                continue
            if tag in {'replace', 'delete'}:
                for line in a[i1:i2]:
                    yield '-' + line
            if tag in {'replace', 'insert'}:
                for line in b[j1:j2]:
                    yield '+' + line
