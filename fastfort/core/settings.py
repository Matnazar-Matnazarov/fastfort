"""Configuration for a FastFort installation.

Settings are grouped rather than flat, so a name says which subsystem owns it and
new options can be added without turning the top level into a hundred fields.
Every value can come from the environment using the ``FASTFORT_`` prefix and a
double underscore for nesting::

    FASTFORT_SECRET_KEY = ...
    FASTFORT_ADMIN__PAGE_SIZE = 50
    FASTFORT_SECURITY__COOKIE_SECURE = true

There is no default secret key. A framework that ships one guarantees that some
deployment will run with it, so the failure is moved to start-up where it is
loud and cheap to fix.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .exceptions import ImproperlyConfigured

__all__ = [
    "AdminSettings",
    "AuthSettings",
    "FastFortSettings",
    "MediaSettings",
    "SecuritySettings",
    "UISettings",
]

#: Minimum entropy we accept for the signing key, in characters.
MIN_SECRET_KEY_LENGTH = 32

#: Substrings that mark a key as copied from a tutorial rather than generated.
#: Matched anywhere in the value, because padding a placeholder out to the
#: minimum length is exactly what someone in a hurry does.
_PLACEHOLDER_MARKERS = (
    "change-this",
    "change-me",
    "changeme",
    "your-secret",
    "secret-key",
    "supersecret",
    "insecure",
    "placeholder",
    "example",
    "fastfort",
)

#: Distinct characters required. Catches "aaaa..." and "1234512345...", which pass
#: a length check while carrying almost no entropy.
MIN_SECRET_KEY_ALPHABET = 8


class AuthSettings(BaseModel):
    """Token lifetimes, password rules and lockout behaviour."""

    model_config = {"extra": "forbid"}

    #: Short-lived on purpose: a leaked access token expires before it is useful.
    access_token_ttl: Annotated[int, Field(ge=60, le=86_400)] = 900

    #: How long an admin browser session stays valid. Separate from the API token
    #: lifetimes: a person's working day is not an API client's refresh window.
    session_ttl: Annotated[int, Field(ge=300, le=2_592_000)] = 1_209_600
    refresh_token_ttl: Annotated[int, Field(ge=300, le=31_536_000)] = 1_209_600

    #: Each refresh issues a new token and retires the old one. Replaying a
    #: retired token means it was stolen, so the whole family is revoked.
    rotate_refresh_tokens: bool = True
    revoke_family_on_reuse: bool = True

    #: Signing algorithm. HS256 needs only the secret key; RS256 and EdDSA are
    #: available for deployments that verify tokens in another service.
    algorithm: Literal["HS256", "HS384", "HS512", "RS256", "EdDSA"] = "HS256"
    issuer: str | None = None
    audience: str | None = None

    password_min_length: Annotated[int, Field(ge=8, le=128)] = 10
    password_reject_common: bool = True

    #: Failed attempts before an identity or address is locked out, and how long
    #: the base lockout lasts. The delay grows with each further failure.
    lockout_threshold: Annotated[int, Field(ge=1, le=100)] = 5
    lockout_seconds: Annotated[int, Field(ge=1, le=86_400)] = 60
    lockout_window_seconds: Annotated[int, Field(ge=60, le=86_400)] = 900


class AdminSettings(BaseModel):
    """Where the admin lives and how much data it will hand out at once."""

    model_config = {"extra": "forbid"}

    url: str = "/admin"
    page_size: Annotated[int, Field(ge=1, le=1000)] = 20

    #: The sizes the list view offers as a control. The active `page_size` is
    #: added if it is not already here, so a project that configures 25 still
    #: sees its own value in the menu rather than a list that cannot express it.
    page_size_choices: tuple[int, ...] = (20, 50, 100)

    #: Hard ceiling on ``?ps=``. Without it a single request can ask the database
    #: for every row in the table.
    max_page_size: Annotated[int, Field(ge=1, le=10_000)] = 200

    #: ``exact`` counts every matching row. On large tables that dominates the
    #: query time, so ``capped`` stops counting past a threshold and the UI shows
    #: "1000+", while ``estimated`` asks the database for its own statistics.
    count_strategy: Literal["exact", "capped", "estimated"] = "exact"
    count_cap: Annotated[int, Field(ge=100, le=1_000_000)] = 10_000

    #: Number of related rows an autocomplete endpoint may return per query.
    autocomplete_limit: Annotated[int, Field(ge=1, le=100)] = 20

    #: Hard ceiling on one export. An export runs the current query with no
    #: pagination, so without a cap a mis-clicked "export everything" on a table
    #: of ten million rows is a request that never finishes and a database that
    #: stops answering anyone else.
    export_limit: Annotated[int, Field(ge=1, le=1_000_000)] = 50_000

    #: Rows fetched per round trip while streaming an export.
    export_chunk_size: Annotated[int, Field(ge=100, le=10_000)] = 1_000

    #: Days covered by the dashboard's signups chart. Each day is one indexed
    #: count, so this is also how many extra queries the dashboard runs; the
    #: ceiling keeps a mistyped value from turning one page into a thousand.
    #: Zero switches the chart off.
    dashboard_days: Annotated[int, Field(ge=0, le=365)] = 30

    #: The user-model column recording when an account was created. Detected
    #: from the usual names (`date_joined`, `created_at`, ...) when left empty;
    #: set it when the column is called something else. A name that is not a
    #: date column leaves the chart off rather than raising.
    signup_field: str = ""

    @field_validator("url")
    @classmethod
    def _normalise_url(cls, value: str) -> str:
        value = "/" + value.strip().strip("/")
        if value == "/":
            raise ValueError("admin url must not be the site root; use '/admin' or similar")
        return value

    @model_validator(mode="after")
    def _page_size_within_maximum(self) -> AdminSettings:
        if self.page_size > self.max_page_size:
            raise ValueError(
                f"page_size ({self.page_size}) cannot exceed max_page_size ({self.max_page_size})"
            )
        return self


class MediaSettings(BaseModel):
    """Where files uploaded through a file or image field are stored.

    Local disk, because that is what every project can rely on without
    provisioning anything first -- object storage is a project's own
    integration to add, not something worth a dependency here. Uploaded files
    are served through the admin itself, behind the same gate as every other
    view, rather than through a separate unauthenticated static mount: an
    uploaded file is a record like any other row, not a public asset.
    """

    model_config = {"extra": "forbid"}

    #: Directory files are written to and served from. Created on first use if
    #: it does not exist yet.
    root: Path = Path("media")

    #: Hard ceiling on one upload, in bytes. A request reads at most one byte
    #: past this before deciding the upload is over the limit, so an oversized
    #: file is a validation error on the field rather than however many
    #: gigabytes it actually was sitting fully in memory first.
    upload_limit: Annotated[int, Field(ge=1, le=1_000_000_000)] = 10_000_000


class UISettings(BaseModel):
    """Appearance. Everything here maps onto a CSS custom property at runtime.

    Rebranding is a number, not a rebuild: the whole palette is derived from one
    hue, so there is no build step and no stylesheet to fork.
    """

    model_config = {"extra": "forbid"}

    #: Accent hue in the OKLCH colour space, 0-360.
    accent_hue: Annotated[int, Field(ge=0, le=360)] = 255
    accent_chroma: Annotated[float, Field(ge=0.0, le=0.37)] = 0.16

    theme: Literal["system", "light", "dark"] = "system"
    density: Literal["comfortable", "compact"] = "comfortable"

    logo_url: str | None = None
    favicon_url: str | None = None

    #: A script that upgrades every `richtext` field into an editor.
    #:
    #: The editor itself is the project's choice: CKEditor, TinyMCE, Quill --
    #: whichever it already uses or licences. FastFort renders the textarea and
    #: marks it `data-ff-richtext`; this names the script that finds them. None
    #: leaves a plain textarea, which is a working control rather than a broken
    #: editor.
    #:
    #: Bundling one instead was the alternative, and it is the wrong trade: a
    #: half-megabyte editor in the wheel for every project, most of which want a
    #: different one or none at all. If the URL is on another origin, its origin
    #: is added to the admin's CSP -- which is exactly why it is configuration
    #: and not something a project can inject from a template.
    richtext_url: str | None = None
    #: Extra stylesheet loaded after the built-in one, for per-project overrides.
    custom_css_url: str | None = None

    #: Tile template for the map beside a geometry field, e.g.
    #: ``"https://tile.openstreetmap.org/{z}/{x}/{y}.png"``. Empty means no map,
    #: and the field stays the pair of coordinates it already was.
    #:
    #: Off by default and configuration rather than a bundled default, because
    #: turning it on means the admin fetches images from somebody else's server:
    #: that host learns which rows are being looked at and roughly where they
    #: are, and most tile services have terms about it. Naming a URL is a project
    #: saying it has read them. The host is added to the admin's CSP `img-src`,
    #: which is why it cannot come from a template.
    map_tile_url: str = ""

    #: Credit line under the map. Tile services generally require one, and the
    #: project is the party bound by that, so it writes it.
    map_attribution: str = ""

    #: Where a map with no coordinates yet opens, as ``latitude, longitude``.
    #: Defaults to a whole-world view rather than to somebody's capital city.
    map_center: str = "0, 0"

    #: None means "follow the browser". A project that wants its admin in one
    #: language regardless of who opens it sets this; leaving it unset must not
    #: silently pin everyone to English.
    language: str | None = None

    #: A directory of `<language>.json` files whose entries take precedence over
    #: FastFort's own. This is where a project translates its model and field
    #: names, which FastFort cannot know.
    locale_dir: str | None = None

    timezone: str = "UTC"

    #: Rendered in the header; useful for telling staging apart from production.
    environment_label: str | None = None

    #: How loudly to draw that label. "danger" for production, so the difference
    #: between the two windows someone has open is visible without reading.
    environment_tone: Literal["info", "warning", "danger"] = "warning"


class SecuritySettings(BaseModel):
    """Session cookies, CSRF and response headers.

    The defaults are the strict ones. Relaxing any of them takes an explicit
    change that shows up in review.
    """

    model_config = {"extra": "forbid"}

    cookie_name: str = "fastfort_session"
    cookie_secure: bool = True
    cookie_httponly: bool = True
    cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    cookie_domain: str | None = None
    cookie_path: str = "/"

    csrf_enabled: bool = True
    csrf_header_name: str = "X-CSRF-Token"
    csrf_field_name: str = "_csrf"

    #: Emit CSP, X-Frame-Options, X-Content-Type-Options and Referrer-Policy.
    security_headers: bool = True
    hsts_seconds: Annotated[int, Field(ge=0, le=63_072_000)] = 0

    #: Trust X-Forwarded-For when resolving the client address. Only enable this
    #: behind a proxy that overwrites the header, otherwise lockout and audit
    #: records can be forged by the client.
    trust_forwarded_for: bool = False

    @model_validator(mode="after")
    def _samesite_none_requires_secure(self) -> SecuritySettings:
        if self.cookie_samesite == "none" and not self.cookie_secure:
            raise ValueError("cookie_samesite='none' requires cookie_secure=True")
        return self


class FastFortSettings(BaseSettings):
    """Top-level configuration object."""

    model_config = SettingsConfigDict(
        env_prefix="FASTFORT_",
        env_nested_delimiter="__",
        extra="forbid",
        validate_default=True,
    )

    #: Signs sessions, CSRF tokens and JWTs. Rotating it invalidates all three.
    secret_key: SecretStr
    project_name: str = "FastFort"
    auth_url: str = "/auth"

    #: Shows tracebacks and disables template caching. Never enable in production.
    debug: bool = False

    auth: AuthSettings = Field(default_factory=AuthSettings)
    admin: AdminSettings = Field(default_factory=AdminSettings)
    ui: UISettings = Field(default_factory=UISettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    media: MediaSettings = Field(default_factory=MediaSettings)

    @field_validator("secret_key")
    @classmethod
    def _validate_secret_key(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        lowered = raw.lower()

        # Checked before length, so a padded placeholder gets the specific
        # message rather than a generic "too short".
        marker = next((m for m in _PLACEHOLDER_MARKERS if m in lowered), None)
        if marker is not None:
            raise ValueError(
                f"secret_key contains the placeholder {marker!r}, so it was almost "
                "certainly copied from documentation. Generate a real one with "
                "`fastfort generate-secret`."
            )
        if len(raw) < MIN_SECRET_KEY_LENGTH:
            raise ValueError(
                f"secret_key must be at least {MIN_SECRET_KEY_LENGTH} characters "
                f"(got {len(raw)}). Generate one with `fastfort generate-secret`."
            )
        if len(set(raw)) < MIN_SECRET_KEY_ALPHABET:
            raise ValueError(
                f"secret_key uses only {len(set(raw))} distinct characters, which is "
                "too little entropy regardless of its length. Generate one with "
                "`fastfort generate-secret`."
            )
        return value

    @field_validator("auth_url")
    @classmethod
    def _normalise_auth_url(cls, value: str) -> str:
        return "/" + value.strip().strip("/")

    @model_validator(mode="after")
    def _urls_must_not_collide(self) -> FastFortSettings:
        if self.admin.url == self.auth_url:
            raise ValueError(f"admin.url and auth_url are both {self.auth_url!r}")
        return self

    # -- deployment review --------------------------------------------------

    def deployment_issues(self) -> list[str]:
        """Return configuration problems that matter in production.

        Used by ``fastfort check --deploy``. Returning strings rather than
        raising lets the command report every problem in one pass instead of one
        per run.
        """
        issues: list[str] = []

        if self.debug:
            issues.append(
                "debug=True exposes tracebacks and internal state. Set FASTFORT_DEBUG=false."
            )
        if not self.security.cookie_secure:
            issues.append(
                "security.cookie_secure=False sends the session cookie over plain HTTP. "
                "Set FASTFORT_SECURITY__COOKIE_SECURE=true and serve over HTTPS."
            )
        if not self.security.cookie_httponly:
            issues.append(
                "security.cookie_httponly=False lets JavaScript read the session cookie, "
                "which turns any XSS into a session takeover."
            )
        if not self.security.csrf_enabled:
            issues.append(
                "security.csrf_enabled=False leaves every admin form open to "
                "cross-site request forgery."
            )
        if not self.security.security_headers:
            issues.append(
                "security.security_headers=False disables CSP and clickjacking protection."
            )
        if self.auth.access_token_ttl > 3600:
            issues.append(
                f"auth.access_token_ttl is {self.auth.access_token_ttl}s. A leaked access "
                "token stays valid that long; 900s is the recommended ceiling."
            )
        if not self.auth.rotate_refresh_tokens:
            issues.append(
                "auth.rotate_refresh_tokens=False means a stolen refresh token can be "
                "used indefinitely without detection."
            )
        return issues

    def require_production_ready(self) -> None:
        """Raise if the configuration is not safe to expose to the internet."""
        issues = self.deployment_issues()
        if issues:
            listed = "\n".join(f"  - {issue}" for issue in issues)
            raise ImproperlyConfigured(
                f"Configuration is not production ready:\n{listed}",
                hint="Fix the items above, or run with debug=True if this is a local machine.",
            )
