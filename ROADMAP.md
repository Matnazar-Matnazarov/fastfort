# Roadmap

Where FastFort goes after 0.5.0, and why each thing is on the list.

This is a working document, not a promise. It is ordered by what unblocks real
projects first, and every entry is checked against the five constraints that
actually govern what can land here: no Node.js, progressive enhancement with
JavaScript off, a strict CSP with no inline scripts, a hard front-end size
budget, and a single-hue OKLCH palette.

Researched against Django admin and Django Unfold, Laravel Filament v4/v5,
Wagtail, Refine and react-admin, the shadcn/Next.js admin template ecosystem,
and the dense keyboard-first tools (Linear, Attio, Notion, Retool) that current
admin UX is measured against.

---

## Where 0.5.0 actually stands

Shipped and not to be re-proposed: list views with sorting, search, filters,
pagination, bulk actions and deletion planning; **column visibility** and
**saved views**; **soft delete** with a trash view and restore; CSV/Excel/JSON
import and export; a widget dashboard with server-drawn SVG charts; every
common field type including PostGIS geometry and pgvector; Argon2id, sessions,
JWT, CSRF, lockout, rate limiting, sign-in records and account protection; an
OKLCH single-hue theme with light/dark/system, 11 languages and RTL; a command
palette, related-object popups and live list updates; two ORM backends behind
one adapter contract.

`fastfort/contrib/` is still an empty placeholder. Its docstring promises an
audit log, soft delete and rate limiting — soft delete landed in 0.5.0 but
lives in `admin/`, and the other two are unwritten.

---

## Tier 1 — the two parity gaps that block modelling

These are the only items on this roadmap that a project can hit a wall on. Both
are form-layer, both are server-rendered by nature, and neither needs a new
adapter method.

### 1.1 `inlines` — related objects on the parent's form

**The gap.** There is no way to edit an order and its lines on one page. A
project models `Order` and `OrderLine`, registers both, and gets two unrelated
list views: adding three lines to an order means three round trips through a
separate page, picking the parent from a dropdown each time.

This is the single most-used advanced feature of the Django admin
(`TabularInline` / `StackedInline`), and Filament's equivalent — relation
managers — is what its own users describe as the reason to choose it. FastFort
has `ModelAdmin` options for display, filtering, search, ordering, relations,
read-only fields, passwords, labels, icons, widgets, export, import, actions
and soft delete. It has nothing for this.

```python
class OrderLineInline(admin.TabularInline):
    model = OrderLine
    fk_name = "order"  # inferred when unambiguous
    extra = 1  # blank rows offered
    fields = ("sku", "quantity", "unit_price")
    can_delete = True


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    inlines = (OrderLineInline,)
```

**Why it fits.** Inline formsets are server-rendered by construction — a
`<fieldset>` of repeated rows inside the parent's `<form>`, posted in one
request — which is how the Django admin has always done it and why it works
with scripting off. Script adds "Add another row" and "Remove"; without it the
server renders `extra` blank rows and a delete checkbox, which is Django's own
fallback too.

**What it costs.** The real work is transactional, not visual: parent and
children have to save inside one `UnitOfWork`, a child's validation error has
to re-render the whole parent form with every typed value intact, and a child
row deleted in the same request as a parent edit must not leave a half-write
behind. Both backends need a way to fetch children for a parent, which
`ListQuery` plus a filter already expresses.

The introspection is already there: `spec/` types reverse foreign keys as
`FieldType.REVERSE_FK`, and `Form._visible_specs` skips them today with a
docstring saying *"editing them belongs on the other model's page"*. That
sentence is the assumption this feature revisits — the relation is already
described, it was simply never given anywhere to render.

**Size:** the largest item on this roadmap. Worth doing first anyway.

---

### 1.2 `fieldsets` — sections, and tabs on a long form

**The gap.** `model/form.html` renders `{% for field in form.fields %}` into one
`auto-fit` grid. A model with 25 columns produces 25 controls in a single wall,
in spec order, with no grouping, no headings and nothing collapsed. Every admin
this project is measured against solved this years ago: Django's `fieldsets`
with a `collapse` class, Unfold's tabs, Filament v4's per-page schemas.

```python
class ProductAdmin(admin.ModelAdmin):
    fieldsets = (
        (None, {"fields": ("name", "sku", "category")}),
        ("Pricing", {"fields": ("price", "cost", "tax_rate")}),
        ("Logistics", {"fields": ("weight", "dimensions"), "collapsed": True}),
    )
```

**Why it fits.** `Form` already builds `list[FormField]`; this adds a grouping
pass over the same list and a template that iterates sections. A collapsed
section is `<details>` — open without script, exactly like the filter panel and
the export menu already are.

**What it costs.** Small, and it makes 1.1 better: an inline is naturally its
own section, so the two share a layout primitive. Validation should reject a
field named twice or omitted entirely, the same way `list_display` is checked
today, and a `ConfigurationError` naming every problem at once.

**Size:** small. Highest value-per-line on this list.

---

## Tier 2 — the things that make an admin feel fast

This is what current admin design is actually about. The research is
consistent: *density, inline editing, and keyboard support*, with "quiet
chrome" — borders and shadows receding, hierarchy carried by type weight and
spacing, colour reserved for state and meaning. FastFort's visual language is
already there; what is missing is the interaction layer on top of the table.

### 2.1 Expose the density control that already exists

`UISettings.density` retunes every `--ff-space-*` token through one
`--ff-density` multiplier, and `Theme.root_attributes()` writes
`data-ff-density` on the root. It has worked since before 0.4.0 and **there is
no control anywhere in the interface that sets it** — a deployment can only
change it in Python, and a person reading the table cannot change it at all.

Add it to the appearance panel beside theme and accent, stored per browser like
the other two. Nearly free: the CSS, the settings field and the boot-time
attribute all exist.

*(Found while building the 0.5.0 column menu, which briefly grew a second,
table-only density toggle before the collision was noticed. Same attribute
name, two meanings — the reason this belongs in one place.)*

### 2.2 Row keyboard navigation

`j`/`k` or arrows to move the focused row, `Enter` to open it, `x` to tick it
for a bulk action, `/` to jump to search. Linear's table is the reference and
the reason people call it fast; the mechanics are a roving `tabindex` and a
visible focus ring, both of which the stylesheet already knows how to draw.

Pure enhancement — every row is already a link.

### 2.3 Inline cell editing

Edit a scalar cell in the list without opening the form. The constraint decides
the shape: each editable cell is a real per-row `<form>` that posts and
re-renders without script, and `fastfort.js` intercepts it into a `PATCH`-like
update with the live-list machinery that already exists. Restricted to simple
scalars — text, number, boolean, enum, date — because a geometry editor or an
upload card in a table cell is a worse experience than the form it replaced.

Gated behind an opt-in `list_editable`, following Django's own name.

### 2.4 Bulk edit, not just bulk delete

Ticking forty rows currently offers delete and whatever `@admin.action` methods
a project wrote. Setting one field across forty rows — a status, an owner, a
category — is the other thing people select rows for, and it is a form with one
field posted against the same selection the bulk bar already carries.

### 2.5 The loading state during a live update

`data-ff-live` swaps the results fragment; the spinner in the toolbar is the
only signal. A skeleton that preserves row height stops the page jumping, and
is the difference between "fast" and "flickering" on a slow connection.

---

## Tier 3 — governance and operations

Everything here was on the earlier competitive audit and none of it has moved.
Ordered by how cheaply it lands.

### 3.1 API token management — *cheapest real feature left*

Create, scope, expire and revoke personal access tokens from the admin, with a
last-used column and a write-once secret shown exactly once. This is close to a
`ModelAdmin` over a token table plus the hashing `auth/` already does, and it is
the piece a project needs before anything outside the browser can talk to it.

### 3.2 Audit log, and the per-record activity timeline

`core/hooks.py` already declares `BEFORE_CREATE`, `AFTER_UPDATE`,
`BEFORE_DELETE` and the rest, with documented kwargs — and **nothing in
`admin/site.py` ever emits them.** The mechanism is built and unit-tested; the
call sites are missing. Emitting them is a small change; the audit log that
listens is a `contrib/` module with its own table, and the timeline on a
record's page is the UI for it.

Order matters: emit the hooks first, as their own release. They are useful to
projects immediately, independent of whether FastFort ever ships the listener.

### 3.3 TOTP two-factor authentication

A redirect and a form — no JavaScript, no CSP exception, no new dependency
beyond a TOTP library. Passkeys are the harder half and should not gate this
one: WebAuthn has no non-JS path by design of the standard, so it can only ever
be an *additional* option beside password + TOTP, never the only way in.

### 3.4 Impersonation

"Sign in as this user" from their page, with a persistent banner and a way
back, for reproducing a support report. Fits as a fifth setting in
`admin/protection.py` beside the four that already govern what a deployment
lets the admin do to an account. Must be logged, so it wants 3.2 first, and
must never allow impersonating a superuser from a lower-privileged account.

### 3.5 Notification centre

A bell with unread state and a dismissible list. Idiomatic here: the server
renders it `hidden` and a `:root:not([data-ff-js])` rule unfolds it inline
without script, which is the pattern the filter panel and export menu already
use.

---

## Tier 4 — bigger bets, and one scope question

Not scheduled. Listed so the reasoning is on record.

- **Object- and field-level permissions.** The most requested thing in every
  admin ecosystem. Field-level has one hard rule here: it must be
  `FieldSpec.editable` becoming role-aware, never a second flag beside it —
  CLAUDE.md states that boundary is deliberately singular, and a second gate
  that could disagree is a mass-assignment bug waiting to be written.
- **Version history with a field-level diff and revert.** Wagtail and Payload
  both have it; it needs revision storage per model and is a real subsystem.
- **Kanban and calendar views.** A calendar is a server-rendered month grid and
  is easy. Kanban's drag-to-reassign needs a non-JS fallback — a per-card
  "move to…" select — to stay inside the progressive-enhancement rule.
- **Webhooks, scheduled reports, job monitoring.** All three sit behind one
  piece of infrastructure FastFort does not have: a background worker. Build
  the worker once and three features move from "bends a rule" to "fits".
- **Auto-generated public REST/GraphQL API.** Directus and Strapi do this.
  It is not a technical conflict — it is an identity question. It would push
  FastFort from "admin framework" toward "backend-as-a-service", with a second
  HTTP surface carrying its own auth, versioning and support burden. Answer it
  deliberately or not at all.

---

## Suggested sequence

| Release | Contents |
|---|---|
| 0.6.0 | `fieldsets` (1.2), density control (2.1), row keyboard navigation (2.2) |
| 0.7.0 | `inlines` (1.1) — on its own, it is large enough |
| 0.8.0 | CRUD hook emission (3.2 first half), API tokens (3.1) |
| 0.9.0 | Inline cell editing (2.3), bulk edit (2.4), loading states (2.5) |
| 1.0 | Audit log + timeline (3.2), TOTP (3.3), impersonation (3.4) |

`fieldsets` leads because `inlines` renders better on top of it, and because it
is the smallest change that visibly improves every existing form.

---

## The rule this list is written against

Every admin framework surveyed for this roadmap is heavier than FastFort, and
most of them are heavier because they said yes to a list like this one without
asking what each item cost. The budget test exists to make that cost visible:
the whole front end is 93 KB gzipped, and React with ReactDOM is more than that
before a single component is written.

So: an item earns its place by being a thing a project cannot do without it,
not by being a thing another admin has.

## Sources

- [Django admin reference](https://docs.djangoproject.com/en/5.1/ref/contrib/admin/) and [Django Admin UI Modernization (GSoC 2026)](https://forum.djangoproject.com/t/gsoc-2026-django-admin-ui-modernization-responsive-layout-and-visual-refresh/44703)
- [Django Unfold features](https://unfoldadmin.com/features/) · [django-unfold](https://github.com/unfoldadmin/django-unfold)
- [What's new in Filament v4](https://filamentphp.com/insights/leandrocfe-whats-new-in-filament-v4) · [Filament nested resources](https://filamentphp.com/docs/4.x/resources/nesting)
- [Data table UI design reference 2026](https://www.setproduct.com/blog/data-table-ui-design) · [Enterprise data table UX patterns](https://www.pencilandpaper.io/articles/ux-pattern-analysis-enterprise-data-tables)
- [Bulk action UX guidelines](https://www.eleken.co/blog-posts/bulk-actions-ux) · [Bulk editing — Basis Design System](https://design.basis.com/patterns/bulk-editing)
- [Next.js + shadcn admin dashboards 2026](https://adminlte.io/blog/nextjs-admin-dashboards-shadcn/) · [SaaS dashboard design trends 2026](https://adminlte.io/blog/saas-dashboard-design-examples/)
