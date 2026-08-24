"""Differ module for native algorithms (native_histogram, native_myers).

This file is self-contained: it contains everything needed to run the
native diff algorithms (Myers and Histogram, implemented in Free Pascal
in cudadiff.pas and exposed via cudatext.diff_proc). It does NOT depend
sdqs
qsdqsd
qsdqsd
qsdqsd
dqs
dq
sdqsdqs
on differ_python.py.

The native engine is 10-30x faster than the pure-Python matchers on
large files. For Python-only algorithms (hybrid, myers, vscode, patience,
difflib), use differ_python.py instead.
