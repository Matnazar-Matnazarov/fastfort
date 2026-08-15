"""The hand-written scripts, checked for names they call but never declare.

There is no bundler here and no linter that reads JavaScript, which is the whole
point of the project -- one wheel, no Node. The cost of that is the one thing a
bundler gives away for free: a name that is referenced and declared nowhere is a
`ReferenceError` at the moment the line runs, and nothing before that moment
says so. Every other test in `tests/ui/` reads the script as *text* and greps it
for a string, so all of them pass while the function underneath is broken.

That is not hypothetical. `formatPoint` lived beside `PointMap` in
`fastfort.js`; splitting the geometry editor into `fastfort-geo.js` carried
`parsePoint` across and left `formatPoint` behind. `write()` calls it on every
click of a POINT map, so every click threw -- before the `draw()` on the next
line and before the coordinate reached the input. The pin appeared on the next
pan, because a pan is the first redraw from anywhere else, and the box that
actually submits stayed empty: the place a person had clicked was silently not
saved. It shipped in three releases.

The check is deliberately not a scope analyser. It collects every name each file
declares anywhere in itself -- including function parameters, destructured
bindings and loop variables -- and asserts that every name it *calls* is one of
them, a browser global, or something the file is documented to take from
elsewhere. A file-wide set means the test cannot catch a name used outside the
scope that declares it, and that is a fair trade: it catches the name that is
declared in no scope at all, which is the failure that actually happened, and it
does so without a JavaScript parser written in Python.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

JS_DIR = Path(__file__).resolve().parents[2] / "fastfort" / "ui" / "static" / "js"

#: Everything the scripts are entitled to call without declaring it. The browser
#: platform, the standard library, and -- for `fastfort-geo.js` -- the handful of
#: helpers its own header says it takes off `window.FastFort`.
#: One string rather than a set literal so that the names stay grouped by what
#: they are. A set of this many short entries is reformatted to one name per
#: line, which turns a readable list into a hundred lines nobody reads.
_PROVIDED_GROUPS = (
    # Declarations and control flow, which a regex cannot tell from a call.
    "if for while switch catch return typeof function await async new delete void in of case"
    " do else",
    # The platform: globals, constructors and the standard library.
    "window document console fetch URL URLSearchParams FormData Headers Request Response Blob"
    " File FileReader Image Event CustomEvent AbortController AbortSignal IntersectionObserver"
    " ResizeObserver MutationObserver requestAnimationFrame cancelAnimationFrame setTimeout"
    " clearTimeout setInterval clearInterval queueMicrotask getComputedStyle matchMedia"
    " structuredClone DOMParser Intl Math JSON Date Number String Boolean Array Object Set Map"
    " WeakMap WeakSet Promise Symbol RegExp Error TypeError RangeError Proxy Reflect BigInt"
    " parseInt parseFloat isNaN isFinite encodeURI decodeURI encodeURIComponent"
    " decodeURIComponent btoa atob navigator location history localStorage sessionStorage CSS"
    " Node Element HTMLElement NodeList DataTransfer ClipboardItem performance",
    # `new Option(label, value)`, which the searchable select builds when a
    # remote lookup returns a value the native <select> has never held.
    "Option",
)

PROVIDED = frozenset(name for group in _PROVIDED_GROUPS for name in group.split())

#: What `fastfort-geo.js` destructures off `window.FastFort` at its top. Named
#: here as well so that renaming one in `fastfort.js` fails loudly rather than
#: quietly becoming an undeclared call in the file that borrows it.
FROM_KIT = frozenset({"el", "icon", "t", "once", "register"})

#: A call site: `name(` not preceded by a dot, so `this.draw()` and `box.at()`
#: are the object's business rather than a free name.
CALL = re.compile(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(")

#: Every way this codebase introduces a name. Broad on purpose: a false entry
#: here only weakens the check, while a missing one fails a passing file.
DECLARATIONS = (
    re.compile(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)"),
    re.compile(r"\bfunction\s*\*?\s*([A-Za-z_$][\w$]*)"),
    re.compile(r"\bclass\s+([A-Za-z_$][\w$]*)"),
    # Parameters, destructuring, catch bindings and for-of/in loop variables
    # all bind names that are then called: `({ el, icon })`, `(fn) => fn()`.
    re.compile(r"[({,\[]\s*([A-Za-z_$][\w$]*)\s*(?=[,)}\]=:])"),
    re.compile(r"\bcatch\s*\(\s*([A-Za-z_$][\w$]*)"),
    re.compile(r"\bfor\s*\(\s*(?:const|let|var)\s+([A-Za-z_$][\w$]*)"),
    # `name = (a) => ...` and `name: function ...` on an object literal.
    re.compile(r"([A-Za-z_$][\w$]*)\s*[:=]\s*(?:async\s*)?(?:function\b|\()"),
    # A class method. Its definition site -- `draw() {` -- looks exactly like a
    # call to the pattern below, so without this every method in every class
    # reports itself as a name nothing declares. Calls to them all go through
    # `this.`, which the call pattern already ignores.
    re.compile(r"^\s*(?:static\s+|async\s+|get\s+|set\s+|\*)*([A-Za-z_$][\w$]*)\s*\(", re.M),
)


#: A `/` here opens a regular expression rather than dividing. The standard
#: heuristic: division can only follow a value, so anything else means a literal.
BEFORE_REGEX = frozenset("(,=:[!&|?{};+-*%~^<>")


def _strip(source: str) -> str:
    """Comments, string bodies and regex literals blanked, keeping code alone.

    Character by character rather than by regex, because JavaScript cannot be
    tokenised with one. The first attempt here stripped `//`-comments with
    `//[^\\n]*`, which ate the second half of `"http://www.w3.org/2000/svg"` and
    left its opening quote unbalanced; every quote in the file after that point
    paired with the wrong partner, and whole functions -- including the `const
    build` this test then reported as undeclared -- disappeared into what the
    stripper believed was a string.

    Comments have to go because every script in the package documents itself
    heavily and this file's own prose names the functions it describes. String
    and regex bodies have to go because `` `translate(${x}px)` `` is not a call
    to `translate`. The holes in a template literal stay: those are real code.
    """
    out: list[str] = []
    # What we are inside, innermost last. A template literal can hold `${}`,
    # which holds code, which can hold another template literal.
    stack: list[str] = []
    depth: list[int] = []
    index = 0
    length = len(source)

    def last_significant() -> str:
        for char in reversed(out):
            if not char.isspace():
                return char
        return ""

    while index < length:
        char = source[index]
        state = stack[-1] if stack else "code"

        if state == "code":
            two = source[index : index + 2]
            if two == "//":
                stack.append("line")
                out.append("  ")
                index += 2
                continue
            if two == "/*":
                stack.append("block")
                out.append("  ")
                index += 2
                continue
            if char in "\"'":
                stack.append(char)
                out.append(" ")
                index += 1
                continue
            if char == "`":
                stack.append("`")
                out.append(" ")
                index += 1
                continue
            if char == "/" and last_significant() in BEFORE_REGEX:
                stack.append("regex")
                out.append(" ")
                index += 1
                continue
            # Inside a `${}` hole, the brace that closes it returns to the
            # template rather than to the code around it.
            if len(stack) and depth:
                if char == "{":
                    depth[-1] += 1
                elif char == "}":
                    if depth[-1] == 0:
                        depth.pop()
                        stack.pop()  # leave the hole
                        out.append(" ")
                        index += 1
                        continue
                    depth[-1] -= 1
            out.append(char)
            index += 1
            continue

        if state == "line":
            if char == "\n":
                stack.pop()
                out.append("\n")
            else:
                out.append(" ")
            index += 1
            continue

        if state == "block":
            if source[index : index + 2] == "*/":
                stack.pop()
                out.append("  ")
                index += 2
                continue
            out.append("\n" if char == "\n" else " ")
            index += 1
            continue

        if state in {'"', "'", "regex"}:
            if char == "\\":
                out.append("  ")
                index += 2
                continue
            closing = state if state != "regex" else "/"
            if char == closing or (char == "\n" and state != "regex"):
                stack.pop()
            out.append("\n" if char == "\n" else " ")
            index += 1
            continue

        # A template literal.
        if char == "\\":
            out.append("  ")
            index += 2
            continue
        if source[index : index + 2] == "${":
            stack.append("code")
            depth.append(0)
            out.append("  ")
            index += 2
            continue
        if char == "`":
            stack.pop()
            out.append(" ")
            index += 1
            continue
        out.append("\n" if char == "\n" else " ")
        index += 1

    return "".join(out)


def _scripts() -> list[Path]:
    found = sorted(JS_DIR.glob("*.js"))
    assert found, "the check cannot pass by reading nothing"
    return found


@pytest.mark.parametrize("script", _scripts(), ids=lambda path: path.name)
def test_every_name_the_scripts_call_is_one_they_declare(script: Path) -> None:
    """A call to a name nothing declares is a ReferenceError waiting for a click."""
    source = _strip(script.read_text(encoding="utf-8"))

    declared = set(FROM_KIT)
    for pattern in DECLARATIONS:
        declared.update(pattern.findall(source))

    called = set(CALL.findall(source))
    undeclared = sorted(called - declared - PROVIDED)

    assert not undeclared, (
        f"{script.name} calls {undeclared}, which nothing in the file declares. "
        "Either the helper was left behind when code moved between files -- the "
        "way formatPoint was -- or the name is new and belongs in PROVIDED."
    )
