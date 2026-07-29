# Security Policy

FastFort is an authentication and administration framework. A vulnerability here
directly affects the applications built on top of it, so reports are treated as a
priority.

## Reporting a vulnerability

**Please do not open a public issue.**

Report through a GitHub Security Advisory:
<https://github.com/Matnazar-Matnazarov/fastfort/security/advisories/new>

Including the following speeds up triage considerably:

- affected version(s)
- steps to reproduce, or a minimal proof of concept
- the impact you believe it has (information disclosure, privilege escalation, ...)

## Response targets

| Stage | Target |
|---|---|
| Acknowledge the report | within 48 hours |
| Initial assessment | within 7 days |
| Fix for a critical issue | within 14 days |
| Public advisory | after the fix is released |

We follow coordinated disclosure: an advisory is published only once a fix is
available. Reporters are credited unless they prefer otherwise.

## Supported versions

Until 1.0, only the latest minor release is supported.

## Hardening a deployment

Run the built-in checker before you deploy. It performs more than twenty checks and
exits non-zero on an unsafe configuration:

```bash
uv run fastfort check --deploy
```

The essentials:

1. `FASTFORT_SECRET_KEY` holds at least 32 random bytes, supplied through the
   environment and never committed (generate one with `fastfort generate-secret`).
2. Serve over HTTPS; cookies are set `Secure`, `HttpOnly` and `SameSite=Lax`.
3. Run with `debug=False`, otherwise error pages expose internal details.
4. Forward `X-Forwarded-For` correctly through your reverse proxy, so lockout and
   audit records attribute the right client address.
5. Keep the FastFort schema current: `fastfort db status`.

## Built-in controls

- Argon2id password hashing with automatic rehashing on parameter changes
- Refresh tokens stored only as SHA-256 hashes, with rotation and family-wide
  revocation when a used token is replayed
- Login lockout per IP and per identity, with timing-safe credential checks
- Signed double-submit CSRF tokens on every state-changing request
- Mass-assignment protection: only fields marked editable in the spec are written
- Object-level, row-level and field-level authorisation
- Ordering, filtering and search restricted to an allow-list derived from the model
  spec, so user input never reaches SQL
- Security headers: CSP, `X-Frame-Options`, `X-Content-Type-Options`, `Referrer-Policy`
- Audit log with sensitive values masked
