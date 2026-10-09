"""A YAML subset, just enough for the config file - with no dependency on PyYAML.

Why not PyYAML: this tool has to run on a machine that is touched twice a year and has nothing
installed (that is v1's whole reason to exist), so "pip install pyyaml first" is not an option. The
config file is a FLAT mapping of scalars, inline lists and block lists - a grammar small enough to
read and write correctly here - and a file a human edited by hand has to keep working, so the reader
is deliberately forgiving: unknown keys pass through to the caller (which ignores them), plain
unquoted scalars are accepted, comments and blank lines are skipped, `# comment` after a value is
dropped, and yes/no/on/off are read as booleans the way YAML 1.1 does.

What it does NOT do: nesting, anchors, multi-document, folded scalars, tags. If the config ever needs
those, it needs a different config.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

_BOOL_TRUE = ("true", "yes", "on")
_BOOL_FALSE = ("false", "no", "off")
_NULLS = ("null", "~")
_INT_RE = re.compile(r"^-?\d+$")
_FLOAT_RE = re.compile(r"^-?\d+\.\d*([eE][-+]?\d+)?$|^-?\d+[eE][-+]?\d+$")


# ----------------------------------------------------------------------------- writing
def dump(data: Dict[str, Any], header: str = "", comments: Optional[Dict[str, str]] = None,
         order: Optional[List[str]] = None) -> str:
    """Render a flat mapping as YAML text, with an optional header and per-key comments."""
    comments = comments or {}
    keys = [k for k in (order or []) if k in data]
    keys += [k for k in sorted(data) if k not in keys]
    out: List[str] = []
    if header:
        out += [_comment(line) for line in header.splitlines()]
        out.append("")
    for key in keys:
        note = comments.get(key)
        if note:
            out += [_comment(line) for line in note.splitlines()]
        value = data[key]
        if isinstance(value, (list, tuple)):
            if not value:
                out.append("%s: []" % key)
            else:
                out.append("%s:" % key)
                out += ["  - %s" % _format(item) for item in value]
        else:
            out.append("%s: %s" % (key, _format(value)))
    return "\n".join(out) + "\n"


def _comment(line: str) -> str:
    """A comment line, whether or not the caller already wrote the '#'."""
    stripped = line.strip()
    if not stripped:
        return "#"
    return line if stripped.startswith("#") else "# " + line


def _format(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    # A string is always quoted, and the characters a line-based format cannot carry are escaped:
    # an unescaped newline used to truncate the value at the line break when read back.
    return '"%s"' % (str(value).replace("\\", "\\\\").replace('"', '\\"')
                     .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))


# ----------------------------------------------------------------------------- reading
def load(text: str) -> Dict[str, Any]:
    """Parse the subset back into a dict. Anything unparseable is skipped, never raised."""
    out: Dict[str, Any] = {}
    current: Optional[str] = None       # key collecting a block list
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if re.match(r"^-\s", stripped) or stripped == "-":
            if current is not None:
                item = _scalar(stripped[1:].strip())
                if isinstance(out.get(current), list):
                    out[current].append(item)
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if not key or key.startswith("#"):
            continue
        value = value.strip()
        if not value:
            # `key:` with nothing after it: either an empty block list or an empty value; the item
            # lines that follow decide, and `validate()` decides what an empty value means
            out[key] = []
            current = key
            continue
        current = None
        if value.startswith("[") and value.endswith("]"):
            out[key] = [_scalar(part.strip()) for part in _split_inline(value[1:-1])
                        if part.strip() != ""]
        else:
            out[key] = _scalar(value)
    return out


def _split_inline(body: str) -> List[str]:
    """Split `a, "b, c", d` on the commas that are NOT inside quotes."""
    parts: List[str] = []
    buf = ""
    quote = ""
    for ch in body:
        if quote:
            buf += ch
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
            buf += ch
        elif ch == ",":
            parts.append(buf)
            buf = ""
        else:
            buf += ch
    parts.append(buf)
    return parts


def _scalar(text: str) -> Any:
    text = text.strip()
    if not text:
        return ""
    if text[0] in "\"'":
        return _unquote(text)
    # a plain scalar: drop a trailing comment, then guess the type
    text = re.sub(r"\s+#.*$", "", text).strip()
    low = text.lower()
    if low in _BOOL_TRUE:
        return True
    if low in _BOOL_FALSE:
        return False
    if low in _NULLS:
        return None
    if _INT_RE.match(text):
        return int(text)
    if _FLOAT_RE.match(text):
        return float(text)
    return text


def _unquote(text: str) -> str:
    quote = text[0]
    body = text[1:]
    if quote == "'":
        # single quotes: '' is the only escape
        return body.replace("''", "'").split("'")[0] if "'" in body else body
    out = ""
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            out += {"n": "\n", "t": "\t", "r": "\r"}.get(nxt, nxt)
            i += 2
            continue
        if ch == '"':
            break
        out += ch
        i += 1
    return out