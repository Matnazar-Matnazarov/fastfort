"""A token endpoint, a refresh endpoint, and a dependency to put on your routes.

FastFort's claim is that the tedious half of an application is already written.
Authentication is the clearest case of that and was the biggest hole in it: a
project got a working admin and then wrote its own `/token`, its own refresh
rotation, its own `Depends(get_current_user)` and its own decision about what to
do when a refresh token is replayed -- against the same user model, with the same
password hashes, next to settings that already described exactly those things.

Four routes, mounted at `settings.auth_url` when `auth.api_enabled` is on:

    POST {auth_url}/token     identity + password  -> access + refresh
    POST {auth_url}/refresh   refresh              -> access + refresh
    POST {auth_url}/logout    refresh              -> 204
    GET  {auth_url}/me        Bearer access        -> who that is

Off by default, because mounting public endpoints onto somebody's application is
not something a library should do without being asked. Turning it on is one line.

The dependency is the part worth reading even if the routes are not wanted:

    from fastfort.auth import bearer_user

    current_user = bearer_user(fort)

    @app.get("/orders")
    async def orders(user: Annotated[Any, Depends(current_user)]) -> list[Order]:
        ...

`bearer_user` verifies the signature, checks the token is an *access* token,
loads the row, and rejects a user who has since been deactivated -- which a
signature check alone would not, and which is the difference between a token
expiring in fifteen minutes and an account staying usable for fifteen minutes
after it was closed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from fastfort.core.exceptions import SecurityError

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from fastfort.core.app import FastFort

__all__ = ["RefreshRequest", "TokenResponse", "bearer_user", "build_auth_router", "token_user"]

#: `auto_error=False` so a missing header reaches our own handler and becomes the
#: same 401 as a bad one. Left to FastAPI it is a 403, which is the wrong code
#: for "you did not say who you are" and sends clients looking for a permission
#: problem that does not exist.
_bearer = HTTPBearer(auto_error=False)


class TokenResponse(BaseModel):
    """The OAuth 2 shape, because every HTTP client already knows it."""

    access_token: str
    refresh_token: str
    # The RFC 6750 scheme name, not a credential -- hence the exemption.
    token_type: str = "Bearer"  # noqa: S105
    expires_in: int


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


def bearer_user(fort: FastFort) -> Callable[..., Coroutine[Any, Any, Any]]:
    """A FastAPI dependency resolving the bearer token to a user row.

    Returns the project's own user object, not a claims dict: a route that has to
    look the row up again in order to do anything with it has been handed half an
    answer, and the lookup it would write is the one below.
    """

    async def dependency(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    ) -> Any:
        if credentials is None or not credentials.credentials:
            raise _unauthorized("No bearer token was sent.")

        try:
            claims = fort.tokens.verify(credentials.credentials)
        except SecurityError as error:
            raise _unauthorized(str(error)) from error

        user = await fort.auth._get_user(str(claims.get("sub", "")))
        if user is None or not fort.user_config.is_active(user):
            # Deliberately the same answer as a bad signature. A deactivated
            # account and a forged token are both "this token does not identify
            # anyone", and distinguishing them tells whoever is holding a token
            # whether the account behind it exists.
            raise _unauthorized("That token does not identify an active account.")
        return user

    return dependency


def token_user(fort: FastFort) -> Callable[..., Coroutine[Any, Any, Any]]:
    """A FastAPI dependency resolving a personal access token to a user row::

        from fastfort.auth import token_user

        machine = token_user(fort)

        @app.get("/orders")
        async def orders(user: Annotated[Any, Depends(machine)]) -> list[Order]:
            ...

    A separate dependency from `bearer_user` rather than one that accepts
    either. They authenticate different things -- a JWT is a session with a
    person behind it, a token is a standing grant to a machine -- and a route
    that accepts both should say so by depending on both, rather than
    inheriting it from a helper that quietly widened.

    Every refusal is the same sentence: unknown, revoked, expired, or owned by
    an account that has been deleted or deactivated. Telling them apart tells
    whoever is holding the token which of those it is.
    """

    async def dependency(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    ) -> Any:
        if fort.api_tokens is None:
            raise _unauthorized("API tokens are not enabled.")
        if credentials is None or not credentials.credentials:
            raise _unauthorized("No bearer token was sent.")

        user = await fort.api_tokens.resolve(credentials.credentials)
        if user is None or not fort.user_config.is_active(user):
            raise _unauthorized("That token does not identify an active account.")
        return user

    return dependency


def build_auth_router(fort: FastFort) -> APIRouter:
    """The token endpoints, ready to include under `settings.auth_url`."""
    router = APIRouter(tags=["auth"])
    settings = fort.settings
    #: Bound here and used as a default value below rather than inside an
    #: `Annotated[...]`. This module has `from __future__ import annotations`, so
    #: every annotation is a string that FastAPI resolves against *module*
    #: globals -- and `fort` is a local of this function. Written the modern way,
    #: `Depends(bearer_user(fort))` fails to resolve and the parameter silently
    #: becomes a required query string called `user`, which is a 422 on every
    #: request rather than an error anybody sees at import time.
    current_user = bearer_user(fort)

    @router.post("/token", response_model=TokenResponse, name="fastfort:token")
    async def token(
        request: Request,
        identity: Annotated[str, Form()],
        password: Annotated[str, Form()],
    ) -> TokenResponse:
        """Exchange a password for a pair of tokens.

        Form-encoded rather than JSON because that is what the OAuth 2 password
        grant specifies, and therefore what every generated client and every
        `curl` example already sends.

        `authenticate` is the same call the admin's sign-in form makes, so the
        lockout counters, the timing-safe comparison and the rehash-on-login are
        all the ones already tested -- with `require_staff` off unless a project
        asked for it, because an API's users are not necessarily staff.
        """
        try:
            result = await fort.auth.authenticate(
                identity=identity,
                password=password,
                address=fort.auth.client_address(request),
                request=request,
                require_staff=settings.auth.api_requires_staff,
            )
        except SecurityError as error:
            # 401 with the generic message `authenticate` chose. It does not say
            # whether the account exists, and neither does this.
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=str(error),
                headers={"WWW-Authenticate": "Bearer"},
            ) from error

        subject = str(getattr(result.user, fort.user_config.id_field))
        pair = await fort.tokens.issue(subject)
        return TokenResponse(**pair.to_dict())

    @router.post("/refresh", response_model=TokenResponse, name="fastfort:refresh")
    async def refresh(body: RefreshRequest) -> TokenResponse:
        """Trade a refresh token for a new pair, retiring the one presented.

        A replayed token is a 401 *and* signs every session for that account out;
        `TokenService.refresh` explains why that is the proportionate answer
        rather than an overreaction.
        """
        try:
            pair = await fort.tokens.refresh(body.refresh_token)
        except SecurityError as error:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=str(error),
                headers={"WWW-Authenticate": "Bearer"},
            ) from error
        return TokenResponse(**pair.to_dict())

    @router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, name="fastfort:logout")
    async def logout(body: RefreshRequest) -> Response:
        """Retire the family this token belongs to.

        204 whether or not the token was still live. The caller asked for it to
        stop working; it does not work. An error would only tell somebody holding
        a stolen token whether it was worth trying.
        """
        await fort.tokens.revoke(body.refresh_token)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.get("/me", name="fastfort:me")
    async def me(user: Any = Depends(current_user)) -> dict[str, Any]:
        """Who the access token identifies.

        Deliberately three fields. A project's user model is its own, and a
        framework guessing which of its columns are safe to publish is a framework
        that eventually publishes a password reset token. Anything more belongs in
        a route the project writes, using the dependency above.
        """
        config = fort.user_config
        return {
            "id": str(getattr(user, config.id_field)),
            "identity": config.identity_of(user),
            "is_staff": config.is_staff(user),
        }

    return router


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        # Without this a client cannot tell a 401 it should retry with
        # credentials from one it should give up on.
        headers={"WWW-Authenticate": "Bearer"},
    )
