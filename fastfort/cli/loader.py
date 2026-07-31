"""Finding a project's `FastFort` instance from the command line.

The CLI has to reach the very object the running application uses, or its answers
are about a different configuration than the one that will be deployed. So it
imports the project's own module rather than constructing anything itself.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from fastfort.core.exceptions import ImproperlyConfigured

if TYPE_CHECKING:
    from fastfort.core.app import FastFort

__all__ = ["DEFAULT_TARGETS", "ENV_VAR", "load_fort"]

#: Where the target is read from when `--app` is not given.
ENV_VAR = "FASTFORT_APP"

#: Tried in order when nothing is specified. These cover the layouts `fastfort
#: init` writes and the ones the documentation uses.
DEFAULT_TARGETS = (
    "main:fort",
    "app.main:fort",
    "src.main:fort",
    "main:admin",
)


def load_fort(target: str | None = None) -> FastFort:
    """Import `module:attribute` and return the FastFort instance it holds.

    The current directory is added to the import path, because a project is
    normally run from its own root and expects `main` to be importable.
    """
    from fastfort.core.app import FastFort

    cwd = str(Path.cwd())
    if cwd not in sys.path:
        sys.path.insert(0, cwd)

    candidates = [target] if target else [os.environ.get(ENV_VAR), *DEFAULT_TARGETS]
    tried: list[str] = []

    for candidate in candidates:
        if not candidate:
            continue
        tried.append(candidate)
        found = _try_load(candidate, explicit=bool(target))
        if isinstance(found, FastFort):
            return found
        if found is not None:
            raise ImproperlyConfigured(
                f"{candidate} exists but is a {type(found).__name__}, not a FastFort instance.",
                hint="Point --app at the object you passed to `fort.mount(app)`.",
            )

    listed = ", ".join(tried) or "nothing"
    raise ImproperlyConfigured(
        f"Could not find a FastFort instance. Tried: {listed}.",
        hint=(
            f"Pass it explicitly with `--app main:fort`, or set the {ENV_VAR} environment variable."
        ),
    )


def _try_load(target: str, *, explicit: bool) -> object | None:
    """Import one candidate.

    A guess that does not exist is not an error -- the next candidate is tried.
    An explicit target that does not exist is, and so is any import that fails
    for a different reason: swallowing a real error inside someone's `main.py`
    would turn a traceback into "not found", which is far harder to debug.
    """
    module_name, separator, attribute = target.partition(":")
    if not separator or not attribute:
        raise ImproperlyConfigured(
            f"{target!r} is not a valid target.",
            hint="Use `module:attribute`, for example `main:fort`.",
        )

    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if not explicit and exc.name == module_name.split(".")[0]:
            return None
        if exc.name == module_name.split(".")[0]:
            raise ImproperlyConfigured(
                f"Cannot import {module_name!r}.",
                hint="Run this from the project root, or pass --app with the right path.",
            ) from exc
        raise

    found = getattr(module, attribute, None)
    if found is None and explicit:
        available = ", ".join(sorted(n for n in vars(module) if not n.startswith("_"))) or "none"
        raise ImproperlyConfigured(
            f"{module_name!r} has no attribute {attribute!r}. Found: {available}",
        )
    return found
