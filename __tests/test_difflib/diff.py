#!/usr/bin/env python3
"""
Generate a side-by-side HTML diff using difflib.HtmlDiff.
Produces a complete, ready-to-open HTML file.

# Basic usage – full files side-by-side
python html_diff.py old.txt new.txt

# Specify output name
python html_diff.py old.py new.py -o comparison.html

# Show only differences + 3 lines of context
python html_diff.py old.txt new.txt -c -n 3

# Disable line wrapping
python html_diff.py old.txt new.txt -w 0

"""

from __future__ import annotations  # optional, but good practice

import difflib
import argparse
from pathlib import Path
from typing import Optional


def create_html_diff(
    file1: Path,
    file2: Path,
    output: Path,
    context: bool = False,
    numlines: int = 5,
    wrapcolumn: Optional[int] = 80,
) -> None:
    """
    Compare two text files and write a full HTML diff report.
    """
    # Read files as lists of lines (preserve newlines for accurate display)
    fromlines = file1.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    tolines   = file2.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)

    # Create HtmlDiff instance
    differ = difflib.HtmlDiff(wrapcolumn=wrapcolumn)

    # Generate a complete HTML document
    html = differ.make_file(
        fromlines,
        tolines,
        fromdesc=str(file1),
        todesc=str(file2),
        context=context,
        numlines=numlines,
        charset="utf-8",
    )

    # Write the ready-to-open HTML file
    output.write_text(html, encoding="utf-8")
    print(f"HTML diff written to: {output.resolve()}")
    print("Open the file in any browser to view the side-by-side comparison.")


def main():
    parser = argparse.ArgumentParser(
        description="Create a ready-to-open HTML diff using difflib.HtmlDiff"
    )
    parser.add_argument("file1", type=Path, help="Original / left file")
    parser.add_argument("file2", type=Path, help="New / right file")
    parser.add_argument(
        "-o", "--output",
        type=Path,
        default=Path("diff.html"),
        help="Output HTML file (default: diff.html)",
    )
    parser.add_argument(
        "-c", "--context",
        action="store_true",
        help="Show only changed regions + surrounding context",
    )
    parser.add_argument(
        "-n", "--numlines",
        type=int,
        default=5,
        help="Number of context lines (default: 5)",
    )
    parser.add_argument(
        "-w", "--wrap",
        type=int,
        default=80,
        help="Wrap long lines after N characters (default: 80, 0 = disable)",
    )

    args = parser.parse_args()

    if not args.file1.is_file():
        parser.error(f"File not found: {args.file1}")
    if not args.file2.is_file():
        parser.error(f"File not found: {args.file2}")

    wrapcolumn = args.wrap if args.wrap > 0 else None

    create_html_diff(
        file1=args.file1,
        file2=args.file2,
        output=args.output,
        context=args.context,
        numlines=args.numlines,
        wrapcolumn=wrapcolumn,
    )


if __name__ == "__main__":
    main()