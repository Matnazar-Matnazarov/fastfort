"""The `fastfort` command line interface.

Kept out of the core dependencies: `typer` is only needed by someone running the
commands, not by an application serving the admin. Installed with the `cli`
extra, or with `all`.
"""

from __future__ import annotations

__all__ = ["main"]


def main() -> None:
    """Entry point for the `fastfort` script.

    Imported lazily so that a missing `typer` produces an instruction rather than
    a traceback about a package the person never asked for.
    """
    try:
        from .main import main as run
    except ImportError as exc:  # pragma: no cover - depends on how it was installed
        raise SystemExit(
            "The fastfort command needs its optional dependencies.\n\n"
            'How to fix: install them with `uv add "fastfort[cli]"`.'
        ) from exc

    run()
