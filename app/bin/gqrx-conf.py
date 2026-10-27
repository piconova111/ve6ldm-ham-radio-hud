#!/usr/bin/env python3
"""Sets or removes a few keys in a Gqrx settings file, leaving every other line as it was.

Gqrx keeps its settings in a Qt INI file (~/.config/gqrx/station.conf). Some of its
values are Qt's own encoding (gains=@Variant(...)), so this edits line by line instead
of rewriting the whole file.

    gqrx-conf.py FILE section/key=value ...     set a key (the section is added if missing)
    gqrx-conf.py FILE section/key= ...          remove a key

Example:
    gqrx-conf.py ~/.config/gqrx/station.conf General/crashed=false input/lnb_lo=
"""
import os
import sys
import tempfile


def apply(lines, changes):
    """lines: the file's lines (no newlines). changes: [(section, key, value or None)]."""
    out = list(lines)
    for section, key, value in changes:
        start = end = None
        for i, line in enumerate(out):
            text = line.strip()
            if text.startswith("[") and text.endswith("]"):
                if start is not None and end is None:
                    end = i
                if text[1:-1] == section:
                    start, end = i, None
        if start is not None and end is None:
            end = len(out)
        found = None
        if start is not None:
            for i in range(start + 1, end):
                name = out[i].split("=", 1)[0].strip()
                if "=" in out[i] and name == key:
                    found = i
                    break
        if value is None:
            if found is not None:
                del out[found]
            continue
        entry = "%s=%s" % (key, value)
        if found is not None:
            out[found] = entry
        elif start is not None:
            insert = end
            while insert > start + 1 and not out[insert - 1].strip():
                insert -= 1  # keep the blank line before the next section
            out.insert(insert, entry)
        else:
            if out and out[-1].strip():
                out.append("")
            out += ["[%s]" % section, entry]
    return out


def parse(args):
    changes = []
    for arg in args:
        name, sep, value = arg.partition("=")
        section, slash, key = name.partition("/")
        if not sep or not slash or not section or not key:
            raise SystemExit("Not understood: %r (use section/key=value)" % arg)
        changes.append((section, key, value if value != "" else None))
    return changes


def main(argv):
    if len(argv) < 3:
        raise SystemExit(__doc__)
    path = argv[1]
    changes = parse(argv[2:])
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        lines = []
    new = apply(lines, changes)
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".gqrx-conf-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(new) + "\n")
    os.replace(tmp, path)


if __name__ == "__main__":
    main(sys.argv)
