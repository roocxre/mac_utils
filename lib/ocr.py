"""Text recognition for screenshots, using the Swift helper in ocr.swift.

The helper is, in this order: the prebuilt bin/ocr-helper the installer puts next to lib/; one
compiled from ocr.swift into the state folder (rebuilt when ocr.swift is newer, if the Xcode
Command Line Tools are installed); the Swift interpreter, if compiling fails. Without any of
them, recognition raises HelperMissing, which says how to get one.

Words are placed back into rows and columns by position, with tabs between columns, so a
screenshot of a table pastes into a spreadsheet. Each line keeps its leading indentation
(measured in characters from the leftmost line), so code pastes with its structure.
"""
import json
import os
import re
import statistics
import subprocess

from lib.common import developer_tools_installed, state_path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # the folder holding lib/
SWIFT_SOURCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ocr.swift")
PREBUILT = os.path.join(ROOT, "bin", "ocr-helper")   # from the installer: arm64, macOS 14+
HELPER = state_path("ocr-helper")                     # compiled here when there is no prebuilt one
MISSING_HELPER = ("Text recognition needs its helper program, which is missing. Reinstall Mac Utils, "
                  "or install the Xcode Command Line Tools (xcode-select --install) so it can be built.")
COLUMN_GAP = 0.008  # horizontal gap, as a fraction of image width, that separates columns

# Rejoining wrapped prose (see join_wrapped_lines). Widths are fractions of the image width.
LINE_PITCH = 1.7  # lines further apart than this many line heights start a new paragraph
MIN_PROSE_WORDS = 4
MIN_PROSE_WIDTH = 0.35
WORD_SPACE = 0.006
COLUMN_GAP_CHARS = 3      # a gap at least this many characters wide is a column break
INDENT_TOLERANCE = 0.01   # left edges closer than this (fraction of width) are the same indent
MAX_INDENT_COLUMNS = 60
INDENT_WITH_TABS = False  # True: every 4 columns of indentation come out as a tab character
TAB_WIDTH = 4
LIST_MARKER = re.compile(r"^(\d+[.)]|[-–—•*·▪◦‣○●])(\s|$)")


class HelperMissing(RuntimeError):
    """No helper to run: none prebuilt, none compiled, and no Swift compiler to build one."""


def _helper_command():
    """The prebuilt helper; else the compiled one, compiled again whenever ocr.swift changes and
    swiftc is installed; else the interpreter if compiling fails. Raises HelperMissing."""
    if os.access(PREBUILT, os.X_OK):
        return [PREBUILT]
    built = os.path.exists(HELPER)
    # /usr/bin/swiftc is always there, but without the developer tools it only offers to install them.
    if (not built or os.path.getmtime(HELPER) < os.path.getmtime(SWIFT_SOURCE)) and developer_tools_installed():
        try:
            subprocess.run(["swiftc", "-O", "-o", HELPER, SWIFT_SOURCE],
                           capture_output=True, timeout=300, check=True)
            return [HELPER]
        except (OSError, subprocess.SubprocessError):
            return ["swift", SWIFT_SOURCE]
    if built:
        return [HELPER]   # up to date, or older than ocr.swift with no compiler to rebuild it
    raise HelperMissing(MISSING_HELPER)


def recognize_words(image_path):
    result = subprocess.run(_helper_command() + [image_path],
                            capture_output=True, text=True, timeout=120, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"OCR helper failed: {result.stderr.strip()[-300:]}")
    return json.loads(result.stdout or "[]")


def cluster_and_format_table(observations):
    """Group word boxes into rows by vertical position, then join them left to right."""
    if not observations:
        return ""

    for obs in observations:
        obs["y_center"] = obs["y"] + (obs["h"] / 2.0)
        obs["x_right"] = obs["x"] + obs["w"]

    observations.sort(key=lambda item: item["y_center"], reverse=True)

    rows = []
    for obs in observations:
        for row in rows:
            row_y_avg = sum(item["y_center"] for item in row) / len(row)
            row_h_avg = sum(item["h"] for item in row) / len(row)
            if abs(obs["y_center"] - row_y_avg) <= max(0.012, row_h_avg * 0.5):
                row.append(obs)
                break
        else:
            rows.append([obs])

    rows.sort(key=lambda r: sum(item["y_center"] for item in r) / len(r), reverse=True)

    # Leading indentation: how far a row starts from the leftmost row, in characters. The
    # character width comes from the words themselves (code is usually monospaced).
    widths = [o["w"] / len(o["text"]) for o in observations if len(o["text"]) >= 3 and o["w"] > 0]
    char_w = statistics.median(widths) if widths else 0
    for row in rows:
        row.sort(key=lambda item: item["x"])
    min_left = min(row[0]["x"] for row in rows)
    # A column break is a gap of several characters; a fixed fraction of the image would be
    # under one character wide on a small selection and turn every space into a tab.
    column_gap = max(COLUMN_GAP, COLUMN_GAP_CHARS * char_w)

    lines = []
    for row in rows:
        segments = []
        for i, item in enumerate(row):
            text = item["text"].strip()
            if not text:
                continue
            if i == 0:
                segments.append(text)
            elif item["x"] - row[i - 1]["x_right"] > column_gap:
                segments.append("\t" + text)
            else:
                # Keep the spacing: code aligns things with runs of spaces.
                gap = item["x"] - row[i - 1]["x_right"]
                count = max(1, round(gap / char_w)) if char_w else 1
                segments.append(" " * count + text)
        if segments:
            columns = round((row[0]["x"] - min_left) / char_w) if char_w else 0
            columns = max(0, min(columns, MAX_INDENT_COLUMNS))
            if INDENT_WITH_TABS:
                indent = "\t" * (columns // TAB_WIDTH) + " " * (columns % TAB_WIDTH)
            else:
                indent = " " * columns
            lines.append({
                "text": indent + "".join(segments),
                "words": len(row),
                "left": row[0]["x"],
                "right": max(item["x_right"] for item in row),
                "y": sum(item["y_center"] for item in row) / len(row),
                "h": sum(item["h"] for item in row) / len(row),
                "first_word_w": row[0]["w"],
            })

    return "\n".join(join_wrapped_lines(lines)).rstrip().lstrip("\n")


def join_wrapped_lines(lines):
    """Rejoin prose that was only split because it wrapped at the edge of its text block.

    A line is treated as wrapped when it is a prose-length line that runs to the right edge
    of its block, so that the next line's first word would not have fit. Table rows, list
    items, headings and short lines keep their own line.
    """
    # A block is a run of evenly spaced lines; its right edge is where text wraps.
    block_right = []
    start = 0
    for i in range(1, len(lines) + 1):
        if i == len(lines) or not _same_block(lines[i - 1], lines[i]):
            right = max(line["right"] for line in lines[start:i])
            block_right.extend([right] * (i - start))
            start = i

    output = []
    for i, line in enumerate(lines):
        previous = lines[i - 1] if i else None
        if previous and _is_wrapped(previous, line, block_right[i - 1]):
            output[-1] += " " + line["text"]
        else:
            output.append(line["text"])
    return output


def _same_block(upper, lower):
    taller, shorter = max(upper["h"], lower["h"]), min(upper["h"], lower["h"])
    return upper["y"] - lower["y"] <= LINE_PITCH * taller and taller <= 1.6 * shorter


def _is_wrapped(upper, lower, right_edge):
    if "\t" in upper["text"].lstrip() or "\t" in lower["text"].lstrip():
        return False
    if LIST_MARKER.match(lower["text"].lstrip()):
        return False
    # A line indented differently from the one above is code or structure, not a wrap
    # (except under a list item, whose continuation lines are normally hanging).
    if abs(lower["left"] - upper["left"]) > INDENT_TOLERANCE and not LIST_MARKER.match(upper["text"].lstrip()):
        return False
    if upper["words"] < MIN_PROSE_WORDS or upper["right"] - upper["left"] < MIN_PROSE_WIDTH:
        return False
    if not _same_block(upper, lower):
        return False
    # Wrapped only if the next line's first word would not have fit after this line.
    return right_edge - upper["right"] < lower["first_word_w"] + WORD_SPACE


def recognize_text(image_path):
    return cluster_and_format_table(recognize_words(image_path))


if __name__ == "__main__":
    import sys

    print(recognize_text(sys.argv[1]))
