# Changelog

All notable changes to this project are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> Before 1.0, minor releases may contain breaking changes. Each one is listed under
> a **Breaking** heading.

## [Unreleased]

### Fixed

- **The calendar spells the month.** It took its month and weekday names from
  the browser's `Intl`, which has none for Uzbek -- one of the eleven languages
  the admin speaks -- and falls back to CLDR root: an otherwise Uzbek page
  showed "2026 M09" over a row of English weekdays. The names now come from the
  catalogue wherever `Intl` cannot supply them, and from `Intl` where it can, so
  the other ten keep its own composition -- Japanese writes the year first and
  Russian appends "г.", and no list of names knows that.
- **A `time` column gets the admin's own clock** rather than the browser's
  dropdown. A form where one control is the admin's picker and the field beside
  it is Chrome's blue list of hours is two design systems in one row. The panel
  is the clock without the calendar.
- **"Now" sets the hour it says it does.** It wrote the 24-hour number into a
  box holding 1-12, so in every twelve-hour locale an afternoon click selected
  nothing; and it wrote "09" where the option's value was "9", so it selected
  nothing before ten in the morning either.
- **Arabic weekday headings are seven different letters.** They were cut to two
  characters, and every abbreviated Arabic weekday begins with the article
  "ال" -- so all seven columns read the same. The cut is kept only where it
  leaves seven distinct headings.

- **A Tortoise connection the lifespan opened is now reachable from the views,
  or else says why not.** Tortoise 1.1 keeps its connections in a `contextvars`
  variable, and an ASGI server runs the lifespan in a different task from the
  requests -- so `await Tortoise.init(...)` in a lifespan is invisible to every
  view. Start-up looked healthy and the first page that touched the database was
  a 500 carrying a bare "No TortoiseContext is currently active", which names no
  fix. The backend now raises `ImproperlyConfigured` naming both ways out:
  `_enable_global_fallback=True`, or `RegisterTortoise` from
  `tortoise.contrib.fastapi`. The README's Tortoise snippet passes the flag.
- **Sorting a list by a to-one relation** no longer returns a 500 on either
  backend. The list header renders every sortable field as a link, so a list
  showing a `category` column was one click from an error page: SQLAlchemy
  raised `NotImplementedError` from `.asc()` on a relationship and Tortoise
  answered "Filtering by relation is not possible". Both now order through the
  key column beside the relation -- the same column filtering already used, and
  what `order_by("category")` means to Django.
- **A backward one-to-one is no longer offered as sortable or filterable.** Its
  key lives on the other table, so there is no column on this side to name:
  Tortoise raised, and SQLAlchemy quietly fell back to this model's own primary
  key, which is the worse of the two because nothing failed.
- The Tortoise adapter reads the column behind a relation from Tortoise's own
  `source_field` rather than assembling `f"{name}_id"`, so a project that named
  that column itself can be filtered and sorted like any other.

## [0.3.0] - 2026-08-07

### Added

- **A Tortoise ORM backend**, and with it the proof of what the layering has
  claimed since the first release: adding it changed nothing in
  `fastfort/admin/`, `fastfort/ui/` or `fastfort/spec/`. Everything above
  `fastfort/orm/` reads a `ModelSpec` and a `ListQuery` and cannot tell which
  ORM produced them.
  - `fastfort.orm.tortoise.TortoiseBackend` -- introspection, query building,
    CRUD, relations, bulk actions and deletion planning, behind the same
    protocols the SQLAlchemy backend implements.
  - `tests/orm/test_conformance.py` asks both backends the same questions over
    identical model shapes: the spec each produces, every filter operator,
    search, ordering, paging, writes, the mass-assignment boundary and rollback.
    A second backend is correct when it answers the way the first does, and now
    that is a test rather than a hope.
  - `fastfort/orm/coerce.py` holds the one copy of "turn a query string into the
    type this column compares against", shared by both. Two copies would drift,
    and the first symptom would be a filter quietly matching different rows on
    one backend than the other -- which nothing would fail on.
  - Two architecture tests keep the pair honest: neither backend may import the
    other's ORM-specific modules, and neither ORM may be reached from the
    package root, so `fastfort[tortoise]` installs without SQLAlchemy.
- **A second sandbox, `test_api_tortoise/`** -- a library rather than a shop, on
  Tortoise and SQLite, with its own `Everything` covering every type Tortoise
  can express. Run it beside `make sandbox` and the models, the ORM and the
  database all differ while the admin does not, which is the whole of what the
  layering buys. `make sandbox-tortoise`.
- `test_api/` now covers **every one of the 36 `FieldType`s**. The last five --
  `EMAIL`, `URL`, `PASSWORD`, `FILE`, `IMAGE` -- cannot be reached by
  introspecting a stock column, because all five *are* `VARCHAR` and only the
  project knows which is which; `test_api/types.py` declares them and one
  `register_type` rule classifies every column that uses them.

- **Vector search.** A pgvector column is recognised by the type registry the
  same way a PostGIS one is -- by where its type class lives, so a project that
  embeds nothing never pays for an import of pgvector -- and carries its width
  and kind on `FieldSpec.vector`.
  - `?embedding__near=[0.1,0.2,...]` orders the list nearest first, with
    `__metric` (cosine, l2, l1, inner), `__k` for how many neighbours and
    `__within` for a maximum distance. Bracketed and bare-number spellings both
    read, because the first is what pgvector prints and the second is what
    people type.
  - An *ordering*, not a filter, which is what a nearest-neighbour search is --
    so it beats any sort the page was also asked for: "the nearest, and break
    ties by name", never the alphabet.
  - The query vector is re-rendered from numbers this package parsed and bound
    as a parameter, never interpolated; a vector of the wrong width is refused
    at the query boundary rather than by the database, and the page still
    renders.
  - `k` closes the page window as well as the ordering, so page three of a
    two-neighbour search is empty rather than paging on into rows the search
    already ruled out.
  - Offered only where the backend reports the extension, which it now probes
    for PostGIS and pgvector in one query at start-up.
- `docker/postgres.Dockerfile` builds a PostgreSQL image carrying both PostGIS
  and pgvector, because no published one carries both and a project using
  geography columns and embeddings needs them together. The suite still runs
  green without it: everything spatial and everything vector asks the server
  first and skips.

## [0.2.1] - 2026-08-07

### Added

- **Import.** The mirror of export, in the same three formats: a file the writer
  produced is a file the reader takes back, because download-edit-upload is the
  loop people actually use. Off by default -- `ModelAdmin.importable` is opt-in,
  unlike `exportable`, since writing several thousand rows in one request is a
  different thing to hand somebody by accident.
  - Headers match fields by label *or* by name, so an export and a hand-written
    file both land on the same columns. Anything unmatched is ignored rather
    than refused, or a single extra column would break the round trip.
  - Every value goes through `values.parse_value` and `check_bounds` -- the
    change form's own parsers. Import cannot be more permissive than the form,
    which would be a way to write values the admin rejects, and must not be
    stricter, or a row somebody can type by hand is a row the file cannot carry.
  - Relations are resolved against the database by name or by id, and a name
    that matches nothing comes back as a line number and a column rather than as
    a dangling key or a sentence about a foreign-key constraint. An ambiguous
    name resolves to nothing: picking one of two on the person's behalf is a
    silent wrong answer.
  - Every bad cell is reported at once, not the first. A downloadable template
    carries the columns and a row saying what each one wants, because a
    duration, a range and a point have no spelling anybody guesses right.
  - Nothing is written unless everything parses, and the whole import is one
    transaction. A half-applied spreadsheet is worse than a rejected one.
  - An `id` column updates that row instead of duplicating it; an id that
    matches nothing is an error rather than an insert.
  - `FieldSpec.editable` bounds it as it bounds every other write, and sensitive
    columns are excluded outright -- a spreadsheet must not become the one place
    in the admin where an API key can be set in the clear.

### Fixed

- The map stopped at zoom 19 with no feedback of any kind -- no movement, no
  disabled button -- which reads as the map having broken rather than as the map
  having run out of pictures. Requests still stop at the deepest level the tile
  server has; the view now goes further and scales the last level up, which is
  what every map does past its own imagery.
- A page with several geometry columns drew every map at once: nine maps of
  twenty-odd tiles each, about two hundred requests in one burst to one tile
  server. OpenStreetMap's policy says not to and answers by throttling, and a
  throttled tile is `opacity: 0` -- so the map came out with a rectangular hole
  in it. Maps are built when scrolled near, and a failed tile is retried once.
- **An export could not be imported back**, which is the whole point of the
  pair. Three separate reasons, all of them the same shape -- the writer emitting
  something the reader cannot take:
  - A date was written as text, so a spreadsheet converted whichever cells it
    recognised and left the rest, and a column came back half real dates and
    half strings. The `.xlsx` writer emits real date cells now, with a `styles.xml`
    carrying an ISO number format, so the whole column is one thing.
  - A JSON export writes a real array for a many-valued cell, because JSON has
    one. Splitting `["new", "sale"]` on commas produces two fragments that match
    nothing, so both spellings read back.
  - A geometry exported as its list-cell summary -- "Polygon · 14 points" --
    which no importer can turn back into a polygon. `ModelAdmin.export_cell`
    writes the round-trippable form instead.
- **The template could not be uploaded**, which is the first thing anybody does
  with one: its hint row was read as data and produced one parse error per
  column, from the file that exists to explain the format. The hint row is a
  comment now, and so is any row whose first cell starts with `#`.
- A date edited in a spreadsheet came back as a five-figure integer. A date in
  an `.xlsx` is not a date -- it is a *number* of days since 1899-12-30 with a
  number format applied, and the format lives in `styles.xml` under an index the
  cell refers to. So an export written as ISO text became a real date cell the
  moment somebody opened and saved the file, and the upload reported "46218" as
  not being a date. The reader now reads the styles, including custom format
  codes and the 1904 epoch, which is the difference between the round trip
  working and only appearing to.
- A boolean column had no parser of its own. The change form never needed one --
  it decides from whether the control submitted anything at all -- but an import
  reads the word, and without this a boolean was written the *string* `"false"`,
  which is true in every language that has a truthiness rule.

## [0.2.0] - 2026-08-05

Column types. FastFort classified a column into one of twenty-odd kinds and
rendered whatever that kind implied; anything it did not recognise became a
read-only row. This release makes the classification pluggable, makes the
description rich enough for a control to be built from it, and covers every
column type PostgreSQL ships bar three.

### Added

- `FieldType` gains `BINARY`, `INET`, `MACADDR`, `HSTORE`, `MONEY`, `BITS`,
  `SEARCH_VECTOR`, `RANGE` and `MULTIRANGE`. Each one previously fell through to
  `UNKNOWN` and rendered as text nobody could edit
- `FieldSpec` describes as well as classifies: `item` carries an array's element
  spec, `geometry` carries a shape's kind, SRID, dimension and whether it is a
  geography, `bounds` carries a range's endpoint type, and `precision` pairs with
  the scale that was already there
- `fastfort.orm.sqlalchemy.register_type`, the way a project teaches FastFort its
  own SQLAlchemy type instead of forking. Rules run in order, first match wins,
  and `first=True` puts a project's rule ahead of the built-ins
- `fastfort.admin.widgets.register_widget`, the same extension point for controls.
  The renderer already searches a project's template directory first, so a project
  can ship the partial to go with it
- `fastfort.spec.geo`: a complete WKB/EWKB ↔ GeoJSON ↔ EWKT codec in pure Python,
  covering all seven geometry kinds, both byte orders and the EWKB SRID prefix.
  No Shapely, no GeoAlchemy2 import
- Controls for every new type: a tag input for arrays that validates each entry
  against the item type, a key/value editor for hstore, two typed boxes and a
  bounds selector for a range, pattern-checked address boxes, a JSON editor that
  formats and reports a parse error as you type
- A geometry editor for every shape, not just points: draw and edit a line or a
  ring by clicking, dragging a vertex or shift-clicking to remove one; multi-
  geometries and collections are drawn faithfully and edited as text
- Spatial filters: `within`, `contains`, `intersects`, `overlaps`, `touches`,
  `crosses`, `dwithin` and `bbox`, offered only where the backend reports PostGIS.
  A radius arrives in kilometres and both sides go through PostGIS's `geography()`
  cast, so it means metres on a geometry column too
- Range filters for numbers, times and durations. An exact match is the one
  question nobody asks of a price
- CHECK constraints are read at introspection time into `min_value`/`max_value`,
  so the browser rejects the value before the database does -- and when the
  database rejects one anyway, the constraint name is matched back to the field
  and reported as "Rating must be between 1 and 5" rather than as SQL

### Changed

- **The front end is three bundles.** `fastfort.js` is what every page loads and
  publishes a small kit through `window.FastFort`; `fastfort-geo.js` and
  `fastfort-data.js` are requested only by a page whose fields need them. The
  everyday page went from 65,884 to 61,317 bytes gzipped -- lighter than before,
  with the geometry editor and four data editors added
- The size budget is two numbers: what every page downloads, and what ships in
  total. One number alone would let any weight escape counting by moving into an
  on-demand file
- `admin/forms.py` split into `forms.py`, `widgets.py`, `values.py` and the codec
  that moved to `spec/geo.py`
- `model/form.html` split into `model/_widgets.html`, one macro per control
- A `Numeric(precision, scale)` derives its own `max_value`; `Identity()` joins
  `Computed` as a generated-column signal
- A unique foreign key is reported as a one-to-one. The parent side already
  reported it as one, so the same relationship was two different kinds depending
  on which model's page you were looking at
- `docker-compose.test.yml` runs `postgis/postgis:16-3.4`, a drop-in superset, so
  the spatial filters are tested against a database rather than a SQL string

### Fixed

- A column classified read-only -- a raster, a search vector, an OID -- kept
  `editable=True`, so the only thing stopping a hand-crafted POST from reaching it
  was the absent input. `FieldSpec.editable` is documented as the mass-assignment
  boundary with no second flag allowed to disagree
- An unknown widget name raised under `StrictUndefined` instead of falling back to
  a text box, which is exactly what a newly registered widget name always is
- A geometry that was not a point printed raw WKB hex at whoever opened the page:
  the old decoder gave up past 21 bytes
- A `CITEXT` column rendered as a four-row textarea. It subclasses `TEXT`, so it
  needs a rule ahead of the one for prose
- An hstore or JSON object rendered `str(dict)` in a list cell, so a settings
  column read `{'theme': 'dark'}` -- quotes, braces and all
- Duration filtering reached the database as the string it arrived as, which
  PostgreSQL refuses against an `interval` column; the filter that looked
  available was a 500 waiting to be clicked
- Latitude and longitude range checks applied at every SRID. They are meaningless
  in a projected system measured in metres

### Not included

Raster, PostgreSQL composite types and domain types classify through the registry
and render read-only. Each needs a project-declared SQLAlchemy type to be
reachable at all, so the hook is there and the fallback is honest.

## [0.1.0] - 2026-08-04

The first release. Everything below is new, because there was nothing before it.

### Added

- Project foundation: `uv`-managed package layout on the hatchling build backend
- Quality gates: `ruff` (lint and format), `mypy --strict`, `pytest` with coverage
- `tests/test_architecture.py`, which enforces the layer boundaries through AST
  analysis — ORM imports are rejected outside `fastfort/orm/`
- Multi-database test infrastructure with a `--db=sqlite|postgres|mysql|all` option
- `fastfort.core.exceptions`: an exception hierarchy built around actionable messages
- CI matrix covering three Python versions and three databases, plus a dependency
  audit and a wheel installation smoke test
- `fastfort.spec`: the intermediate representation every layer above the ORM
  reads -- `FieldSpec`, `ModelSpec`, `ListQuery`, `Page` and `ChangeSet`
- `fastfort.core`: settings, admin registry, hook dispatcher, user-model mapping
  and the `FastFort` application object
- `fastfort.orm.base`: the `Backend`, `ModelAdapter` and `UnitOfWork` protocols
- `fastfort.orm.sqlalchemy`: SQLAlchemy 2.0 backend with introspection, a query
  builder and a dialect layer that keeps SQLite, PostgreSQL and MySQL behaving
  identically
- `scripts/verify_databases.py`: prints a read/write cycle's results per database

- `fastfort.ui`: the design token system, the Jinja2 renderer and the admin shell
- `fastfort.admin`: `ModelAdmin`, and a server-rendered dashboard and list view
  with sorting, search, filters and pagination

- `fastfort.auth`: Argon2id password hashing, a signed session cookie, signed
  double-submit CSRF tokens, and per-address/per-identity lockout
- Sign-in and sign-out pages, an account menu, and a gate in front of every admin
  route, plus CSP and clickjacking headers on admin responses

- `@admin.register` as a decorator, so an `admin.py` never imports the application
- Create, change and delete views with a generated form per model, per-field
  validation messages, delete confirmation and flash messages

- Password columns render as a new-password plus confirmation control, hashed on
  save, detected from the spec without the project declaring anything
- `05-admin.css`: a visual pass over the sidebar, filter bar, table, stat tiles
  and forms, including a two-column form grid and a sticky action bar

- `fastfort.i18n`: the admin's own interface in nine languages -- English,
  Uzbek, Russian, Turkish, German, French, Spanish, Chinese and Korean -- with a
  filterable language switcher on every page including sign-in, and
  `Accept-Language` negotiation. The catalogues ship inside the package, so a
  project installs FastFort and its admin is already translated; there is
  nothing to configure and no catalogue to write

- Relation and date-range filters, alongside the existing boolean and enum ones
- The list updates in place: searching, filtering, sorting and paging swap the
  results without reloading the page, and still work with JavaScript disabled

- A `fastfort` command: `createsuperuser`, `check`, `registered-models`,
  `generate-secret` and `version`

- `UISettings.locale_dir`: a project can override any of FastFort's own
  interface strings, or add a language FastFort does not ship

- `fastfort.ui.icons`: a hand-drawn SVG icon set, inlined once per page as a
  sprite. No font, no CDN, no second request. `ModelAdmin.icon` names one and is
  validated against the set at declaration time, so a typo is a start-up error
  rather than a blank slot in the sidebar
- `UISettings.environment_tone`: how loudly to draw `environment_label`, which
  now appears in the header rather than the sidebar footer

- `06-widgets.css` and a component runtime in `fastfort.js`: a combobox, a
  many-to-many chip picker, dropdown menus, a modal dialog, toasts, tooltips and
  a command palette. No framework and no build step -- with JavaScript disabled
  every one of them falls back to the native control it replaces
- Bulk actions. Rows carry a checkbox, selecting any of them raises an action
  bar, and `POST {model}/action` runs the chosen one over the selected keys.
  `delete` is built in; `@admin.action("Label", icon=...)` marks a `ModelAdmin`
  method as another. `ModelAdmin.actions` is the allow-list, checked on the
  server, so an action removed from it cannot be reached by posting its name
- `GET {model}/autocomplete?field=&q=`: relation options searched on the server.
  A foreign key onto a large table now stays usable instead of costing megabytes
  of `<option>` markup, and it sits behind the same gate as every other view
  because its answers are rows of the target table
- Active filters are listed as chips above the table, each one a link that
  removes just that filter
- Numbered pagination, so page 12 is one click rather than eleven
- Ctrl+K opens a command palette over every registered model
- The buttons Django puts beside a foreign key: **add**, **change** and **view**.
  Add opens the target's own form in a popup and drops the row it creates into
  the picker; change and view open whatever is selected. Without them, "the
  category does not exist yet" means abandoning the half-filled form you are on.
  Many-to-many pickers get add, and leave change and view off — a multi-valued
  control has no single subject
- `?_popup=1` renders a form without the shell, and saving it returns the new row
  instead of redirecting. The value is handed back in data attributes rather than
  an inline script, because the admin's CSP is `script-src 'self'` with no nonce
- A **Buttons** setting in the appearance panel: the primary action stays neutral
  by default -- near-black on light, near-white on dark -- or takes the accent
  colour. Neutral gives a page one focal point; wanting the brand colour anyway
  is a preference rather than a mistake, so it is offered rather than argued with
- Export to CSV, Excel or JSON from the list toolbar, downloading the current
  view -- the same search, filters and ordering, not the whole model and not just
  the page on screen. `ModelAdmin.exportable` opts a model out (enforced at the
  endpoint, not by hiding the button) and `export_fields` widens the columns.
  All three formats are produced without a dependency: CSV and JSON from the
  standard library, and XLSX written directly, because openpyxl is a 3 MB install
  carrying a formula parser and a chart engine that an admin export will never
  call. JSON uses the types JSON already has -- `null` for an empty column and
  `false` for a false boolean, rather than the `""` and `"false"` a spreadsheet
  cell has to be given, since a string reading `"false"` is *true* in every
  language with a truthiness rule
- `AdminSettings.export_limit` and `export_chunk_size`, so a mis-clicked export
  of a very large table is bounded rather than a request that never finishes

- Editable controls for three column types that used to degrade to a read-only
  row: an `Interval` (`HH:MM:SS`, or `2d HH:MM:SS`), a native array
  (comma-separated), and a PostGIS geometry (`latitude, longitude`)
- `FieldType.GEOMETRY`, detected without importing GeoAlchemy2 — a project that
  does not use PostGIS pays nothing for this being supported. A point is decoded
  from WKB with `struct`, so there is no Shapely dependency either; the admin
  previously printed the raw hex at whoever opened the page
- `test_api` runs on PostgreSQL with PostGIS by default
  (`docker compose -f docker-compose.sandbox.yml up -d`) and carries an
  `Everything` model holding one column of every type the spec layer knows. It
  has already earned its place: it is what found the three gaps above and the
  two fixes below

- File and image fields. `FieldType.FILE`/`FieldType.IMAGE` existed in the spec
  layer with nothing behind them; a project opts a `String` column into either
  widget through `formfield_overrides`, exactly like `color` or `richtext`.
  Uploads are stored under `MediaSettings.root` at a server-generated path --
  never the browser's own filename, which is attacker-controlled input and a
  perfectly ordinary place for `../../etc/passwd` to show up -- and served back
  through `{admin}/media/…`, gated the same as every other view rather than
  through an unauthenticated static mount. A stored value cannot be shown
  inside a native file input (browsers refuse), so it renders beside it instead:
  a thumbnail for an image, a link for anything else, and a "Clear" checkbox
  that removes it without a replacement. `MediaSettings.upload_limit` bounds one
  upload, checked by reading one byte past it rather than the whole thing, so a
  rejected file is a validation error rather than however many gigabytes it
  actually was sitting fully in memory first. Writing the new file and
  deleting the old one both wait until the row itself has actually saved: a
  replacement staged alongside some other field that fails validation must not
  already have discarded the original over a save that never went through

- **An upload control worth using.** `<input type="file">` is the worst thing the
  platform ships: a grey button reading "No file chosen", drawn differently by
  every browser, unstyleable, undroppable-onto, and once something is chosen it
  shows a truncated filename and nothing else — so the commonest question about
  an upload, "is that the right picture", is one it cannot answer. It is now a
  card that can be clicked or dropped onto, previewing what is stored and what
  is about to replace it: the image itself, a video's first frame, the file's
  name, its size, and whether it is over the limit before anything is sent. The
  native input is still inside it, still named, and still what carries the bytes
  — hidden the accessible way rather than with `display: none` — so with script
  off the field is exactly the input and stored-value block it always was. No
  library: the preview is a `URL.createObjectURL` of the file the person just
  picked, which is the browser drawing their own selection with nothing uploaded
  and no request made
- **Brotli, with gzip behind it**, on the admin's own CSS and JavaScript — the
  two largest things it serves and the two that are on every page. The
  stylesheet goes out at 24 KB instead of 138 KB, the script at 29 KB instead of
  122 KB, and a browser that asks for gzip instead gets 30 KB and 34 KB. Both
  are produced once per process and kept, because these files ship in the wheel
  and cannot change while it is running; `Vary: Accept-Encoding` is set so a
  cache in front of the admin cannot serve one browser's Brotli to another that
  cannot read it. Brotli is an optional dependency
  (`pip install "fastfort[compression]"`), because a project behind a proxy that
  already compresses needs nothing here and a missing package should mean "gzip,
  then" rather than a failed import.

  Rendered pages are deliberately **not** compressed. A page carries a CSRF
  token *and* text the request chose — a search term, a filter value — and
  compressing a response holding both is the BREACH side channel: the compressed
  length leaks how much of a guess at the secret matched. A proxy in front of an
  admin should be configured the same way.
- The duration field is one bordered control with hairlines between its parts,
  rather than four separate number boxes in a row that read as four questions
- Japanese and Arabic, taking the admin to eleven languages. Arabic is the first
  right-to-left one, and the whole layout turns around from a single `dir`
  attribute — the stylesheet was already written in logical properties
  throughout, so there is no second sheet and no mirrored rule. A test counts
  the physical direction properties left in the CSS and fails when one is added
- The date picker's title is now a button, and it zooms out: days to months to a
  decade of years. Paging a month at a time is fine for "next Tuesday" and
  useless for a date of birth, which was four hundred clicks on one arrow
- A `datetime-local` field gets a clock: hours, minutes and — in locales that
  write the time that way — AM or PM, as three of the admin's own dropdowns
  rather than a control of the picker's own. The panel drew a calendar and
  nothing else, so the time half of the column could only be reached by typing
  into the input behind it, around a value the picker had just written. Whether
  the clock runs to twelve or to twenty-four comes from `Intl`, and so do the
  names of the two halves of the day, because both are properties of the locale
  rather than interface strings. Choosing a day no longer closes the panel on
  those fields, because the answer is not complete until the time is set;
  "Today" becomes "Now" and sets both, and a "Done" button says when to put it
  away
- The map now behaves like a map. The cursor is an open hand that closes while
  the map is being dragged — it was a crosshair, which says "click to place a
  point" and says nothing about the map moving, so the one gesture that makes a
  map a map was invisible. The marker is a pin anchored at its point rather than
  a dot whose centre had to be guessed at. The zoom controls are one boxed,
  stacked control instead of two floating buttons, and there is a third:
  "My location", which centres on where the browser says the device is, offered
  only where `navigator.geolocation` exists. The tile service's credit line
  moved onto the map, where every other map puts it and where it reads as a
  notice about the pictures instead of as help text for the field

- A delete confirmation that says what a delete would actually do.
  `ModelAdapter.deletion_plan` walks the rows pointing at the one being removed
  and reports, per related model, which of three things happens to them:
  **deleted** (an ORM cascade, or `ON DELETE CASCADE`), **kept with the link
  cleared** (a nullable foreign key, which is what SQLAlchemy does by default and
  what nobody writes down), or **protected** — a `NOT NULL` foreign key with
  nothing cascading, which the database will refuse. The page names them, counts
  them and lists a few by name; a protected relation blocks the delete with a
  sentence saying what is holding it, instead of a constraint violation from
  inside the transaction. Relations are found from the child side, so a foreign
  key whose model never declared a back reference is not missed, and cascades are
  followed, because "4 categories" is not a useful warning when what goes is the
  six hundred products under them. Both the depth and the rows read are capped:
  confirming a delete must not scan the table. The same check runs before a bulk
  delete, where half the selection going and the rest failing is the worst
  available outcome — and again at the point of the write, since a confirmation
  page may have been open for an hour

### Changed

- The CLI suite runs against all three databases instead of being skipped on
  two. `createsuperuser` calls `asyncio.run`, so it reaches the database from a
  loop that is not the test's — which is exactly what happens in production,
  where the CLI is a separate process — and asyncpg and aiomysql bind a
  connection to the loop it was opened on. Giving the command an engine with
  `NullPool` is what makes that work: nothing is held between calls, so it dials
  a fresh connection on whatever loop it is running on. `uv run pytest --db=all`
  now reports no skips at all.
- The language switcher is a filterable menu of buttons carrying a flag, the
  language's own name and its code, instead of a native `<select>`. A select
  opens the operating system's own popup: unstyleable, unlike anything else on
  the page, and on a phone it takes over the whole screen. Each row is a submit
  button, so it still works with JavaScript disabled.
- **Model and field names are no longer translated.** A model's name is the
  project's word for its own domain, and FastFort has no business guessing it in
  nine languages — the same reason Django does not translate your model names
  either. FastFort translates its own interface: the buttons, the filters, the
  messages, the dates. `verbose_name`, `verbose_name_plural`, `group_name` and
  `field_labels` are plain strings again.
- Switching language returns to the same page including its query string. It
  previously dropped the query, so changing language from a filtered, sorted,
  paginated list landed on page one of an unfiltered one.
- The theme control offers Light, Dark and **System** rather than a two-state
  switch. A switch can only say one of two things, so touching it pinned the
  admin and left no way back to following the operating system.
- Which icon the theme toggle shows is decided in CSS from the theme attribute
  rather than by script, so it is correct on the first paint.
- The account card moved from the top-right corner to the foot of the sidebar,
  and now shows the identity signed in with rather than only a name.
- The sign-in page reads the project's own catalogue directory. It was the one
  screen that ignored `UISettings.locale_dir`.
- Sidebar entries take an icon, and the group label becomes the rule between
  groups when the sidebar is collapsed rather than disappearing with the labels.
- Controls that cannot work without JavaScript are hidden until the script has
  run, instead of sitting on the page doing nothing when clicked.
- Every control is drawn rather than left native: select, checkbox and a new
  switch. A native select beside a styled input changes shape per platform and is
  the clearest sign an interface was not finished.
- The primary action is neutral (near-black, inverted in dark mode) rather than
  the brand colour, so a page has one focal point. The brand hue now means one
  thing: the focus ring, links and the active navigation item.
- Radius derives from a single `--ff-radius-base`, so a project can round the
  whole interface by changing one number.
- Filter labels and column headers go through the translator.
- Table headers, sidebar section labels and stat-tile labels are sentence case
  rather than uppercase, which reads as dated and mangles scripts where casing
  carries meaning or does not exist.
- `UISettings.language` defaults to None, meaning "follow the browser". It
  previously defaulted to "en", which matched before `Accept-Language` was ever
  consulted and made browser negotiation dead code.
- Every `<select>` in the admin -- filters, enumerations, foreign keys -- is a
  searchable listbox drawn in the page. The native control opens the operating
  system's own popup, which cannot be styled, cannot be searched, and looks like
  nothing else on the page.
- A many-to-many field is a set of removable chips with a picker, instead of a
  `<select multiple>` and the instruction to hold Ctrl.
- Row actions are icon buttons that are always drawn. They were text links at
  `opacity: 0` until the row was hovered, so they were invisible until the mouse
  found them and unreachable on a touch screen.
- Deleting from the list asks in a dialog rather than navigating to a
  confirmation page and back. The page is still there and is still what the link
  points at, so this is an accelerator and not a requirement.
- A date-range filter collapses behind a button. Two date inputs side by side
  were as wide as the rest of the toolbar and are empty almost all of the time.
- A relation filter with more rows than the cap becomes a searching control
  rather than disappearing. Vanishing silently as a table grows is worse than it
  sounds: the person who set it up saw it working.
- The "sortable but not sorted" arrow appears on hover rather than on every
  column at once, where it was a row of identical marks competing with the one
  arrow that means something.

- A rows-per-page control: 20, 50 or 100, and `AdminSettings.page_size_choices`
  to change the offer. The default page size is now 20 rather than 25
- Filters accept several values at once. Ticking Cancelled and Refunded is one
  query over both, not two runs of the same report; the control submits
  `field__in`, which the query layer already understood

- Six more languages for the admin interface: Turkish, German, French, Spanish,
  Chinese and Korean, each a complete catalogue rather than a partial one. The
  switcher filters as you type, on the name or the code, because nine languages
  is past the point where a list is scanned rather than read
- Clicking anywhere in a row opens the record, the way Django admin behaves.
  Hunting for a 16px pencil to open a row you have already pointed at is a step
  with no purpose; the checkbox, the delete button and any link in a cell are
  excluded, and a text selection is not a click
- An **Appearance** panel: theme, twelve accent colours, density and the sidebar
  state, all per-person in local storage. The swatches set a hue rather than a
  hex code, because the whole palette derives from one number — so a swatch moves
  every accent in the interface, including the ones dark mode computes at its own
  lightness
- Date and datetime filters offer presets — today, yesterday, last 7 or 30 days,
  this week, this month, last month, this year — alongside the two bounds. Nobody
  types two dates to ask "this month"
- A default admin favicon, so the tab is findable without the project supplying
  one

### Changed

- `test_api`, the sandbox application, has no routes, serialisers or queries left
  in it — only the FastFort configuration and a seed. Anything it had to write by
  hand was a gap in the library, which is the point of keeping it short. It went
  from 674 lines to 147
- Every filter lives behind one **Filters** button that opens a panel, instead of
  a control per column across the top of the list. A box per filterable column
  does not survive a model with eight of them: the toolbar wraps onto three
  lines, the table is pushed off the screen, and there is still nowhere to put a
  filter that needs more than a line. The button carries the number that are on.
- Choice filters are checkbox lists rather than dropdowns. In a panel there is
  room to show every value at once, and several can be on together.
- Relation pickers always have a search box, whatever the option count. A picker
  that offers search only past some threshold is one people learn is not
  searchable.
- Autocomplete matches the columns the target's own `ModelAdmin.search_fields`
  names, falling back to the conventional ones. The label a person reads is the
  target's `__str__`, which cannot be searched in SQL, so the columns it is built
  from have to be named — and the target's admin is where a project has already
  said which those are.
- A false boolean is drawn in red rather than grey. "No" is an answer, and in an
  admin it is usually the one being looked for.
- A map beside a geometry field, drawn from tiles and written by hand — pan,
  zoom, and click to drop a point. A pair of coordinates is a number nobody can
  check: "51.5074, -0.1278" is either the right place or a transposed pair a
  thousand miles away, and the only way to tell is to look. Off unless
  `UISettings.map_tile_url` names a tile service, because turning it on means
  the admin fetches images from somebody else's server — that host learns which
  rows are being looked at and roughly where they are, and most tile services
  have terms about it. Naming the URL is also what adds that one host to the
  admin's `img-src`; `default-src 'none'` blocks it otherwise.
  `map_attribution` renders the credit line those services require, and
  `map_center` says where a map with no point yet opens. The coordinates stay
  the control that submits, so typing or pasting them still works and the field
  is unchanged with script off
- A signups chart on the dashboard: new accounts per day over the last month.
  The counts above it answer "how much is there"; this answers "is it growing",
  which is the question anyone opening an admin every morning is asking. The
  column is found by name (`date_joined`, `created_at`, ...), so a project that
  has one gets the chart without configuring anything, and a project that does
  not simply has no chart rather than an error. Drawn by the server as elements
  with a height -- no canvas, no charting library, no second request — so it is
  in the first paint, inherits the theme, and survives script being off. The
  same numbers are also a visually-hidden table, because bars mean nothing read
  one at a time. `AdminSettings.dashboard_days` sets the window (0 switches it
  off; each day is one indexed count) and `signup_field` names the column when
  it is called something unconventional
- A calendar drawn by FastFort on every date and datetime field, replacing the
  native picker. The platform ones are a different control on every operating
  system and browser, and none of them can be made to match the admin around
  them. Keyboard-navigable with the arrow keys, locale-aware — the first day of
  the week and the month and weekday names come from `Intl` rather than being
  assumed to be Monday and English. No library and no build step: the native
  input stays the element that holds and submits the value, so with script off
  it is exactly the control it always was.
- A duration is four labelled boxes — days, hours, minutes, seconds — instead of
  a text box with `HH:MM:SS — or 2d HH:MM:SS` written underneath it. Values
  carry, so 90 minutes becomes an hour and a half rather than an error message.
  The format hint is now script-only, since it describes a control that is no
  longer on the page. A stored duration with a fractional second keeps the text
  box rather than being rounded to fit boxes that cannot show it.

### Fixed

- **The map could not be dragged, and two rounds of arithmetic fixes changed
  nothing anybody could see.** A tile is an `<img>`, and an image is draggable by
  default — so pressing on one and moving started the browser's own
  drag-and-drop, which fires `pointercancel` and hands back a ghost of the tile.
  Every pan died two pixels in, on every tile, in every browser. The tile layer
  is now inert (`pointer-events: none`), the images carry `draggable="false"`
  for the browsers that only honour the attribute, and `dragstart` is cancelled
  on the canvas.
- **The clock's dropdowns were unreadable.** A combobox panel is pinned to both
  edges of its trigger, and on a box sized for two digits that gave a list four
  characters wide: "12" came out as "1..", every minute as "3.", and the search
  box as "Searc". The list now unpins from the far edge and takes the width it
  needs. The hours and the AM/PM control lost their search box as well — twelve
  options fit on screen, and a search above them is a control to walk past
  rather than a shortcut.
- **Every page visibly assembled itself for a frame.** `data-ff-js` — the "script
  is on" flag half the stylesheet keys off — was set by the main bundle, which is
  deferred and therefore runs *after* the browser has painted. So on every
  navigation the theme switch, the settings button and the command palette
  blinked into existence a moment late, every `<select>` about to become a
  combobox was drawn in the operating system's own styling and then swapped, and
  the export and page-size menus were rendered fully open before being shut. The
  flag now comes from `boot.js`, which runs before the first paint and is itself
  the proof that scripting is on; the dropdown panels are rendered `hidden` by
  the server; and a control waiting to be upgraded reserves its box without
  drawing the native one, so nothing moves when the real control lands in it.
- **The map would not pan.** Longitude was clamped to the width of the world
  minus the canvas — and at the zoom levels the field opens on, that bound is
  *smaller* than the canvas, so the low end came out above the high end, `clamp`
  returned the high one whatever it was given, and every drag snapped the centre
  to the same spot. East and west are now unbounded, because the world is: the
  tile column already wrapped, so panning past the anti-meridian arrives back
  where it started, and the marker is drawn in whichever copy of the world the
  view is over. North and south stay bounded, since the world does end there.
- **An unreachable database did not say which database.** `check_connection`
  passed the driver's own text through, and the driver reports the address it
  dialled and nothing else — "Connection refused, 127.0.0.1:55433" is true and no
  help in working out which of a project's databases that is or what was supposed
  to be listening there. The message now names the configured URL, with the
  password masked by SQLAlchemy rather than by string surgery, because an error
  that prints a password puts it in every log that catches it. `test_api`'s
  start-up also checks the connection before creating the schema, so an unstarted
  container is that sentence instead of sixty lines of connection-pool internals.
- **A failed commit was a 500 on every write path.** Adapters translate a
  constraint violation at flush into something a form can show, but a constraint
  the database only checks at commit — a deferred one, or a cascade that turns
  out to violate a foreign key — was raised on the way out of the unit of work,
  past every handler that could have explained it. Create, change, delete and
  bulk actions now commit explicitly, inside the guard that already catches the
  flush, and the unit of work rolls back before translating so the session is
  usable for the response.
- **Deleting a row and then failing crashed while reporting the failure.**
  `delete_submit` rolled back and only afterward read the instance's primary key
  to build the URL to send the person back to — the same expired-attribute crash
  that was fixed in `change_submit`, on the path that exists to explain what went
  wrong. Everything the error path needs is now read while the row is live.
- **A rejected submission while editing an existing row was a 500.**
  `change_submit` rolled the transaction back and only afterward built the
  form's action URL from the instance's primary key. A rollback expires every
  attribute on every object still in the session, and reading one back
  afterward is a synchronous refresh attempt against an async session outside
  the greenlet bridge that makes those work -- on every edit that failed
  validation, not a fraction of them. The response is now rendered before the
  rollback, while the instance is still fresh.
- **Choosing an option in a multi-select combobox closed the whole picker
  after the first pick.** Picking a tag rebuilds the option list, which
  detaches the row that was just clicked from the document before the click
  finishes bubbling to the document-level "close on an outside click" listener
  -- so a click *inside* the panel looked like one *outside* it, the instant
  the panel's own content changed in response to it. The date picker had the
  same bug: choosing "next month" rebuilds its grid, detaching the button that
  was clicked, and closed the calendar the same way. Every "click outside
  closes this" check now reads `event.composedPath()`, captured before
  dispatch, rather than walking up from `event.target` after the fact.
- The map re-centred itself on every click. Dropping a pin dispatches a
  `change` event so anything bound to the field's value learns it changed; the
  map's own listener for that event was learning about its own dispatch and
  recentring on the point that was just clicked, so a click both dropped a pin
  and yanked the whole map to put it in the middle.
- Zooming the map cleared every tile before the replacements had loaded, which
  read as the whole map flashing blank and repainting on every scroll of the
  wheel. Tiles from the zoom level being left now stay on screen as a
  placeholder until the new ones have loaded (or failed), rather than being
  removed on the spot.
- **Zooming the map still stalled, then jumped.** Keeping the previous level's
  tiles was only half of it: they stayed at the *old* level's positions, so the
  view was frozen at the zoom it was leaving until the last new tile arrived and
  the whole thing snapped to the new one. Tiles now live in a layer per zoom
  level, and a layer carries the scale — so the level already on screen is
  scaled to line up with the new one in the same frame the button was pressed,
  and the sharp tiles fade in over it as they arrive. This is what every slippy
  map does, and it is why one feels immediate. A tile that is discarded before
  it loads is accounted for too, so a pan mid-zoom cannot leave the old level
  stranded underneath the new one for good.
- "Cancel" in a popup navigated that same window to the parent's list instead
  of closing it, leaving an abandoned form's window open on a page nothing
  points back to. It now closes the window when one opened it, and falls back
  to the link otherwise.
- The map's zoom buttons kept their English labels in every language. The script
  reads its strings off `<html>`, so `t("ZoomIn")` looks for `data-ff-t-zoom-in`
  and the server was sending `data-ff-t-zoomin`. Nothing failed: the string was
  in all nine catalogues, the test that checks they are complete passed, and the
  label on screen stayed in English. Every string the script asks for is now
  checked against the ones the server sends.
- **`chevron-left` was drawn as a single diagonal stroke.** Its path retraced
  the line it had just drawn (`m15 18-6-6 6 6`) instead of turning, so the arrow
  had one arm. It was the "previous page" control on every list, visible to
  anyone who reached page two, and invisible to a test suite that could only
  check the markup was well-formed and the symbol was defined. Every icon is now
  checked for a segment that cancels out the one before it.

- **The admin flashed the wrong theme on every load**, and the collapsed sidebar
  opened before snapping shut. Both were applied by the deferred main script,
  which by definition runs after the first paint. A small `boot.js` now runs
  blocking in the head, before the stylesheets, so the first paint is already
  right. The collapsed state moved from `.ff-app` to the root element, which is
  the only one that exists that early.
- **A lost CSRF cookie left every form on every page dead.** The gate mints a
  token when the browser has none and hands it to the templates, but nothing
  wrote it back — and it is a session cookie, so closing the browser and
  returning rendered pages whose forms all carried a token with no cookie behind
  it. The failure said "reload the page and try again", which could not help:
  the reload minted another token and dropped that one too. The only way out was
  to sign out and back in. Pages now persist the token they rendered.
- The sidebar drawer button showed at desktop widths, where it does nothing —
  the drawer it opens only exists on narrow screens. `.ff-btn` declares its own
  `display` from a stylesheet that loads after the one hiding the button, so the
  hide lost the specificity tie.
- **Twenty interface strings had no catalogue entry**, so an admin switched to
  Uzbek or Russian showed "20 per page", "Filters" and "Apply" in English in the
  middle of an otherwise translated page. A test now scans the templates and the
  Python layer for every string passed through the translator and fails on any
  the catalogues do not cover, so this fails on the commit that adds the string.
- The form heading, the Save button and every flash message were built with
  f-strings, so the catalogue could never match them — it carried translations
  for "Save changes" and "Create {name}" that nothing could ever look up. They
  go through the translator with placeholders now.
- **A database constraint violation was a 500.** A duplicate value on a unique
  column, a check constraint, a foreign key pointing at a row that had gone —
  each showed a stack trace instead of the field to change, and lost everything
  typed into the form. They render as a form error naming the constraint now.
- A duration rendered as `2 days, 4:15:00` but only parsed `2d 04:15:00`, so
  opening a row with a multi-day interval and pressing Save failed validation on
  a field nobody had touched.
- The `settings` icon was a circle with eight spokes, which at 18px is a sun --
  so the appearance button and the theme toggle sat side by side in the topbar
  looking identical. It is a toothed gear now.
- A tooltip on a control at the edge of the window was clipped by the window.
  They point downward in the topbar, where there is no room above, and pin to
  the control's own edge rather than centring on it.
- **A relation's target was named by a key nothing could look up.** Introspection
  derives one from the model's module, and `FastFort` never told the backend
  about its registry — so `Product.category`, registered as `shop.category`,
  came back as `myapp.category`. Every feature that resolves a relation to the
  admin behind it was therefore quietly doing nothing: the autocomplete fell back
  to guessing which columns to search rather than reading the target's
  `search_fields`, and the related-object buttons could not appear at all.
  `Backend.set_key_resolver` now takes a rule that consults the registry, with
  the derived key still used for a target that has no admin of its own.
- Icons rendered as solid black silhouettes. A `<use>` clones its symbol into the
  shadow tree of the *referencing* element, so `fill="none" stroke="currentColor"`
  on the sprite root never reached the cloned paths; the properties now sit on
  `.ff-icon`, which is also what lets an icon take its colour from `currentColor`.
- Stylesheets and the script are re-read from disk when `debug` is on. They were
  cached for the life of the process, so under `--reload` an edit to a stylesheet
  did nothing until the server was restarted -- while the response it served
  already declared itself uncacheable.
- The stylesheet tests took their list of sheets from a second, hand-maintained
  copy, so a newly added sheet was covered by none of them. They now read the
  list the router uses. The budget test weighs the script as well as the CSS,
  which is the half a front-end framework would actually arrive in.

- The `mysql` extra installs `aiomysql` instead of `asyncmy`. PYSEC-2026-286 is an
  unfixed SQL injection affecting every released version of `asyncmy` (0.2.11 is
  the latest and the advisory covers "thru 0.2.11"), which is not a defensible
  default for an authentication framework. `aiomysql` audits clean and the full
  suite passes against MySQL 8.4 with it.
- The dependency audit in CI runs against the exported lockfile rather than the
  installed environment, so the unpublished project itself is not treated as an
  unauditable dependency.

[Unreleased]: https://github.com/Matnazar-Matnazarov/fastfort/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/Matnazar-Matnazarov/fastfort/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/Matnazar-Matnazarov/fastfort/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/Matnazar-Matnazarov/fastfort/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Matnazar-Matnazarov/fastfort/releases/tag/v0.1.0
