# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this project is

**FastFort** is a batteries-included authentication and admin framework for FastAPI —
Django's admin, `auth` and `contrib` conveniences, rebuilt for an async FastAPI stack.
It is a **library published to PyPI**, not an application. Everything under `fastfort/`
ships inside the wheel; everything else in the repo is development scaffolding.

The defining constraint: **no Node.js, no build step, no CDN**. The admin's CSS, JS,
icons and templates are hand-written files served straight out of the package. A
project installs one wheel and gets a working admin.

Status: pre-1.0 (`0.1.0.dev0`). The public API is not frozen yet.

```python
app = FastAPI()
fort = FastFort(
    settings=FastFortSettings(project_name="Shop"),
    backend=SQLAlchemyBackend(session_factory=session_factory, base=Base),
)
fort.set_user_model(User, identity_field="email")
fort.autodiscover("app")  # imports every app/**/admin.py
fort.mount(app)  # attaches the router under settings.admin.url
```

## Commands

```bash
make install        # uv sync --all-extras
make test           # pytest against SQLite (no services needed)
make test-all       # SQLite + PostgreSQL + MySQL (needs `make services-up`)
make lint           # ruff check + ruff format --check
make types          # mypy --strict over fastfort/
make check          # lint + types + test  -- run this before any commit
make cov            # coverage report (gates: 90% overall, 95% core/auth/spec)
make sandbox        # run test_api/ against PostGIS on http://127.0.0.1:8000/admin
```

Single test: `uv run pytest tests/ui/test_admin_crud.py -k delete -q`.
`pytest-randomly` shuffles order — a failure that only appears sometimes is a real
inter-test dependency, not flakiness to ignore.

## Layout

```
fastfort/
  core/       FastFort object, registry, settings, hooks, exceptions
  spec/       ModelSpec / FieldSpec / ListQuery -- pure data, no web, no ORM
  orm/        base.py = the protocols; sqlalchemy/ and tortoise/ implement them
  admin/      the HTTP surface: site.py (router), options.py (ModelAdmin), forms.py
  auth/       sessions, CSRF, Argon2 passwords, lockout, user-model detection
  ui/         Jinja renderer, theming, icons, static/{css,js}, templates/
  i18n/       catalogue loading + language negotiation (11 languages, Arabic RTL)
  cli/        the `fastfort` command (check, create-superuser, generate-secret)
  contrib/    empty placeholder -- audit log, soft delete, rate limiting
test_api/     scratch app used for manual QA -- one model of every column type
tests/        unit, integration, orm, ui, security, cli + test_architecture.py
```

### The layering, and why it is enforced

`tests/test_architecture.py` fails the build on violations:

1. **No ORM import** (`sqlalchemy`, `tortoise`, `asyncpg`, `aiomysql`, `aiosqlite`)
   anywhere outside `fastfort/orm/`. The admin, UI and spec layers must never see a
   `Column` or a `Session`.
2. **`fastfort/spec/` imports no web framework** — no `fastapi`, `starlette`, `jinja2`.
3. Every package directory has an `__init__.py`.

The payoff was that a second ORM adapter could be added without touching a line of
the admin, and `fastfort/orm/tortoise/` is now that adapter -- added without a
change anywhere above `fastfort/orm/`. `tests/orm/test_conformance.py` asks both
backends the same questions over identical model shapes, so "the second one is
correct" is a test rather than a claim. `fastfort/orm/coerce.py` is the one thing
they share, because turning a query string into a column's type is about the
column rather than about either ORM.

### How a request flows

```
introspect(model) -> ModelSpec          # once, cached on the backend
ModelAdmin(spec)                        # once, validated at build time
request -> ListQuery.from_params(...)   # the validation boundary
        -> backend.unit_of_work()       # one transaction per request
        -> backend.adapter(model, uow)  # request-scoped, never cached
        -> adapter.list/get/create/...  # flushes, never commits
        -> Renderer.render(template)    # server-side HTML
```

## Non-negotiable invariants

These are load-bearing. Breaking one is a security or data bug, not a style choice.

**Adapters never commit.** They flush; the `UnitOfWork` context manager commits on
clean exit and rolls back on an exception. A half-failed request must leave nothing
behind. (`fastfort/orm/base.py` states this in its module docstring.)

**Rollback expires every attribute in the session.** After `await uow.rollback()`,
reading `instance.id` is a synchronous refresh against an async session outside the
greenlet bridge — it crashes. So: read every value you still need (label, primary
key) *before* rolling back, or render the response first and roll back afterwards.
`change_submit` in `admin/site.py` documents this in place; it is the single most
common way to break this codebase.

**Primary keys are tuples**, always — a single-column key is a one-element tuple.
Composite keys travel in a URL joined by `~`.

**`FieldSpec.editable` is the mass-assignment boundary.** `SQLAlchemyAdapter._writable`
filters every write against it. There is deliberately no second flag that could
disagree.

**`ListQuery.from_params` is the only place raw query-string input becomes a query.**
Sort keys, filter fields and operators are checked against allow-lists derived from
the spec; nothing downstream re-validates.

**The CSP starts at `default-src 'none'` with `script-src 'self'` and no nonce.** So:
no inline `<script>`, no inline event handlers, no CDN. Data reaches the browser
through `data-` attributes, which `fastfort.js` reads. The only directives a project
can widen are `img-src` (by setting `ui.map_tile_url`) and `script-src` (by setting
`ui.richtext_url`), each by exactly one origin — see `admin/security.py`.

**Every admin route is behind one gate**, a router-level dependency, not a check
inside each view. `public` carries only static assets, the login page and the
language switch.

**Progressive enhancement.** Sorting, filtering, paging and every form work with
JavaScript disabled. `fastfort.js` upgrades controls; it never owns them. The map
writes into the text input beside it rather than replacing it; the upload card
keeps the native `<input type="file">` inside it as the thing that submits.

**`data-ff-js` comes from `boot.js`, never from the deferred bundle.** It is the
flag `.ff-js-only` and `.ff-no-js` key off, and `boot.js` is the only script that
runs before the first paint. Set anywhere else it is a frame late, and a frame
late is a visible flash of native controls on every navigation. Anything that
must be *closed* on arrival (a dropdown panel) is rendered `hidden` by the server,
with a `:root:not([data-ff-js])` rule that unfolds it inline without script.

**The stylesheet is written in logical properties.** `inset-inline-start`, not
`left`; `padding-inline`, not `padding-left`. Arabic turns the entire admin around
from one `dir` attribute because of this, and `test_the_stylesheet_is_written_in_logical_directions`
counts the physical ones and fails when a new one appears. The two deliberate
exceptions are the checkbox tick (a tick is a tick in any language) and the map,
which is a coordinate space placed by script in physical pixels.

**A missing row is 404, never 403** — confirming existence to someone who may not
see it is a leak.

## Conventions

- **English everywhere**: code, comments, docstrings, commit messages.
- **Comments explain *why*, and usually name the failure that motivated them.** This
  codebase's comments read like short incident reports ("Without this guard, that
  dispatch would loop back here and recentre the map…"). Match that register — do not
  write comments that restate the next line.
- Files stay under ~300 lines where practical. `admin/site.py` is the standing
  exception; new presentation helpers go in the helpers section at its foot.
- Error messages say what happened *and* what to do about it — see
  `core/exceptions.py`.
- `ruff` (line length 100) and `mypy --strict` are both CI gates.
- Conventional Commits: `feat(admin): …`, `fix(ui): …`, `test(orm): …`.
  Branches match: `feat/…`, `fix/…`, `docs/…`.

## Testing

| Change | Required coverage |
|---|---|
| New public API | `tests/unit/` + `tests/integration/` |
| ORM adapter | the shared conformance suite in `tests/orm/` |
| Security control | a regression test in `tests/security/` |
| UI change | a render smoke test in `tests/ui/` |
| Bug fix | a test that fails before the fix |

UI tests drive the real ASGI stack through `httpx.ASGITransport` and sign in through
the actual login form (`tests/conftest.py::sign_in`), so the gate and CSRF are
exercised rather than bypassed. `filterwarnings = ["error"]` — a new
`DeprecationWarning` fails the suite.

The suite runs on SQLite by default; `--db=postgres`, `--db=mysql`, `--db=all` need
`make services-up`. Anything touching SQL that differs between engines belongs in
`orm/sqlalchemy/dialects.py`, which exists precisely so "works on all three" is
testable.

## The browser side

`ui/static/js/fastfort.js` (~2500 lines) is one IIFE, no modules, no framework. Its
shape:

- `enhancers.push((scope) => …)` — each widget registers an upgrade pass. Passes run
  over the whole document at boot and over any fragment swapped in later, so every
  enhancer must be idempotent; `once(element, flag)` is the guard.
- Widgets: command palette (Ctrl+K), searchable/multi select, date & time picker
  (three views — days/months/years — plus a clock for `datetime-local`), duration,
  the upload card (drag-drop, image and video previews from `URL.createObjectURL`),
  the slippy map, related-object popups, live list updates, bulk-action bar, theme
  and accent switching.
- Anything a widget draws for itself is a string the *server* sends: `_ui_text()`
  in `admin/site.py` → `data-ff-t-*` on `<html>` → `t("Key")`. `FALLBACK_TEXT` in
  the script and `_ui_text` must stay in step — a test compares them, because a
  mismatch leaves a control in English in every language with nothing failing.
- Strings come from the server via `_ui_text()` in `admin/site.py` → data attributes →
  `t(key)`. Never hard-code user-visible English in the script: the script is one
  cached file for every language.
- Icons come from an SVG sprite (`ui/icons.py`), referenced with `<use>`.

**Static assets are compressed; pages are not.** `ui/compression.py` negotiates
Brotli → gzip → identity for the CSS, JS and favicon routes, caching each encoding
per process. HTML is deliberately excluded: a page holds a CSRF token *and*
request-chosen text, which is the BREACH pattern. Don't "fix" that by adding
`GZipMiddleware`.

**The front end has a size budget** — `test_the_front_end_stays_within_budget`
weighs the CSS and every script, gzipped, against a stated ceiling. It exists to
stop the admin acquiring a framework by degrees. Raising it is allowed and has
happened three times; each raise names the feature that caused it in the test's
docstring. Trimming comments to squeeze under it is not the intended fix.

CSS is seven cascade layers loaded in order (`00-reset` … `06-widgets`) and
concatenated into one response by `_read_css()`. The whole palette derives from one
OKLCH hue custom property, which is why rebranding is a number in `UISettings` and
not a forked stylesheet.

The map is **hand-rolled Web Mercator**, not Leaflet — 256px tiles positioned by
`transform`, one layer per zoom level so a zoom is instant and the previous level
stays underneath until the new tiles have loaded. Tiles are only ever fetched from
the single host named in `ui.map_tile_url`.

## Deletion semantics

`ModelAdapter.deletion_plan(objects)` walks the graph before anything is written and
reports, per related model, what would happen:

| Effect | When | What the page says |
|---|---|---|
| `DELETE` | ORM cascade, or `ON DELETE CASCADE` | "will be deleted too" |
| `CLEAR` | nullable FK, or `ON DELETE SET NULL` | "kept, without the link" |
| `PROTECT` | `NOT NULL` FK with nothing cascading | blocks the delete |

Rules and cost live in `orm/sqlalchemy/deletion.py`:

- Relations are found from the **child** side (`_incoming`), by scanning the registry's
  mappers for many-to-one relationships that land here — so a foreign key whose model
  never declared a back reference is still found. Many-to-many is deliberately
  excluded: only the association rows go, not the related objects.
- Cascades are **followed** (`DELETION_DEPTH = 3`). Deeper levels are reached through a
  subquery, not through the rows the level above sampled, or the second number would
  be a fraction printed as a total. A composite foreign key stops the recursion.
- `DELETION_SAMPLE = 5` rows are named; the count stops at `DELETION_COUNT_CAP = 1000`
  and sets `truncated`, which the page renders as "1000+".

The plan is checked twice: on the confirmation page, and again in `delete_submit` and
the bulk-delete action just before the write, because a confirmation page may have
been open for an hour. `_cascades()` in `site.py` is the cheap spec-level hint used by
the inline delete buttons — no query, no counts.

## Gotchas worth knowing before you debug

- **`MissingGreenlet`** almost always means a lazy relation was touched outside the
  async bridge. Either eager-load it (`form_relations()` in `site.py` does this for
  every to-one and M2M on a form) or read it before the rollback that expired it.
- **`_cascades` / cascade metadata comes from the SQLAlchemy mapper**, so a model that
  never declared `cascade="all, delete-orphan"` gets SQLAlchemy's default behaviour:
  children are *nulled*, not deleted. The admin reports that honestly rather than
  claiming a cascade the ORM will not perform.
- **`expire_on_commit=False`** is expected on the project's session factory; the views
  read attributes off objects after the unit of work commits.
- **The `_popup` query parameter** changes what a save returns (a page that hands the
  new value to `window.opener`), so any change to the create/change views has to keep
  the popup branch working.
- **Debug mode re-reads CSS/JS from disk** (`_bundled_css`/`_bundled_js`); production
  caches them per process, so an edit to a stylesheet needs a restart unless
  `settings.debug` is on.
- **`test_api/` needs PostGIS** (`make sandbox-up`) because `Everything.location` is a
  `Geography` column. The pytest suite deliberately does not — it fakes the geometry
  widget through `formfield_overrides`.
