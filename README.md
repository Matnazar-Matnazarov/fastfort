<div align="center">

# FastFort

**Batteries-included authentication and admin framework for FastAPI.**

Django-style model registration · Professional admin UI · JWT + session auth ·
Roles & permissions · Audit logging · SQLAlchemy & Tortoise · **No Node.js required**

[![CI](https://github.com/Matnazar-Matnazarov/fastfort/actions/workflows/ci.yml/badge.svg)](https://github.com/Matnazar-Matnazarov/fastfort/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)

</div>

> [!WARNING]
> **Early stage.** `0.1.0` is the first release and the public API is not stable
> yet — before 1.0, a minor release may contain breaking changes, each one listed
> under a **Breaking** heading in [the changelog](CHANGELOG.md). Pin a version.

---

## Why FastFort?

Every FastAPI project rebuilds the same things from scratch: an admin panel, login,
refresh tokens, roles, an audit trail. Django ships all of that out of the box.
FastAPI does not.

FastFort fills that gap.

```bash
uv add "fastfort[sqlalchemy]"
```

```python
# main.py
from fastapi import FastAPI
from fastfort import FastFort, FastFortSettings
from fastfort.orm.sqlalchemy import SQLAlchemyBackend

from app.db import Base, session_factory
from app.models import User

app = FastAPI()

fort = FastFort(
    settings=FastFortSettings(project_name="Shop"),
    backend=SQLAlchemyBackend(session_factory=session_factory, base=Base),
)
fort.set_user_model(User, identity_field="email")
fort.autodiscover("app")
fort.mount(app)
```

```python
# app/products/admin.py
from fastfort import admin

from app.models import Product


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "price", "is_active", "created_at")
    list_filter = ("is_active", "category")
    search_fields = ("name", "description")
    ordering = ("-created_at",)
    select_related = ("category",)
    icon = "box"  # drawn beside the sidebar entry

    # Offered once rows are selected. "delete" is built in; this adds another.
    actions = ("delete", "archive")

    @admin.action("Archive", icon="box")
    async def archive(self, adapter, objects):
        for product in objects:
            await adapter.update(product, {"is_active": False})
        return f"{len(objects)} products archived."
```

Create the first account and start the server:

```bash
uv run fastfort generate-secret --export      # a signing key
FF_PASSWORD=... uv run fastfort createsuperuser \
    --identity you@example.com --password-env FF_PASSWORD --no-input
uv run uvicorn main:app
```

Open `http://127.0.0.1:8000/admin`, sign in, and you have a list, a search box,
filters, sortable columns, numbered pagination, row selection with bulk actions,
and working create, edit and delete pages. Foreign keys become searchable
pickers -- backed by an autocomplete endpoint once the target table outgrows a
dropdown -- and many-to-many fields become removable chips.

None of it needs JavaScript to work. Every control is a real form input and
every sort header a real link; the browser-side code upgrades them in place and
gets out of the way when it is not there.

Field names are checked against the model when the admin is built, so a typo in
`list_display` is a start-up error naming every problem at once -- not a 500 the
first time someone opens that page.

---

## Features

| | |
|---|---|
| 🎛 **Django-style admin** | `@admin.register`, `list_display`, `list_filter`, `search_fields`, `fieldsets`, `actions` |
| 🗑 **Deletes you can trust** | The confirmation counts what actually goes: rows that cascade, rows kept with the link cleared, and rows that block the delete outright — refused with a sentence instead of a constraint violation |
| 🎨 **A UI you will not want to replace** | Light and dark themes, brand colour from a single setting, ⌘K command palette, full keyboard navigation, real mobile layout |
| 🔐 **Production-grade auth** | Argon2id hashing, JWT access/refresh, token rotation with reuse detection, login lockout, CSRF protection |
| 👥 **Roles and permissions** | Object-level, row-level and field-level access control |
| 📝 **Audit log** | Who changed what and when, with an old → new diff |
| 🗄 **Three databases** | SQLite · PostgreSQL · MySQL, with identical behaviour |
| 🔌 **ORM-agnostic** | SQLAlchemy 2.0 (async and sync) and Tortoise ORM behind one adapter contract |
| 🌍 **Eleven languages** | English, Uzbek, Russian, Turkish, German, French, Spanish, Chinese, Japanese, Korean and Arabic — the last of them right-to-left, which turns the whole layout around from one attribute. The catalogues ship in the package, so the admin is already translated the moment you install it — there is nothing to configure |
| ⌨️ **A CLI that matters** | `createsuperuser` so a fresh install has a way in, and `check --deploy` that exits non-zero |
| 📤 **Export** | The current view as CSV or Excel — filters, search and ordering included. No openpyxl, no pandas |
| 📦 **No Node.js** | CSS and JavaScript ship pre-built inside the package, served Brotli-compressed with a gzip fallback — 24 KB of CSS and 29 KB of JavaScript on the wire |

---

## How it is put together

```
UI (Jinja2 · CSS · HTMX)   ─┐
Admin (ModelAdmin, forms)  ─┤
Auth (tokens, permissions) ─┼──►  Spec layer (immutable, JSON-serialisable)  ◄── ORM adapters
Core (settings, registry)  ─┘
```

Everything above the spec layer is ORM-agnostic, and that boundary is enforced by
tests rather than by convention. Adding a new ORM therefore never touches the admin
or the UI, and a JSON API for a future SPA front end comes for free from the same
specs the templates render.

---

## Installation

```bash
uv add "fastfort[sqlalchemy,postgres]"    # PostgreSQL
uv add "fastfort[sqlalchemy,mysql]"       # MySQL
uv add "fastfort[sqlalchemy,sqlite]"      # SQLite
uv add "fastfort[all]"                    # everything
```

Requires Python 3.11 or newer.

---

## Development

The only prerequisite is [uv](https://docs.astral.sh/uv/). No Node.js, ever.

```bash
uv sync --all-extras          # set up the environment
uv run pytest                 # tests (SQLite)
uv run pytest --db=all        # tests against all three databases (needs Docker)
uv run ruff check .           # linting
uv run mypy fastfort          # type checking
make check                    # every gate at once
```

See [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request, and
[SECURITY.md](SECURITY.md) to report a vulnerability.

---

## License

[MIT](LICENSE) © Matnazar
