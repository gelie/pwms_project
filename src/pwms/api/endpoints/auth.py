"""Client-credential and refresh-token endpoints."""

from django.contrib.auth.hashers import check_password, make_password
from django.utils import timezone
from ninja.errors import HttpError

from ..app import auth_router
from ..models import ApiClient
from ..schemas import (
    ErrorOut,
    RefreshRequest,
    RevokeRequest,
    TokenPairOut,
    TokenRequest,
)
from ..throttling import TokenEndpointThrottle
from ..tokens import (
    issue_access_token,
    issue_refresh_token,
    redeem_refresh_token,
    refresh_token_ttl,
    revoke_refresh_token,
)

# Used to spend the same PBKDF2 work when an unknown client_id is presented, so
# response timing does not reveal whether a client id exists.
_DUMMY_SECRET_HASH = make_password("not-a-real-secret")


def _token_pair(client: ApiClient, refresh_token: str | None = None) -> dict:
    access_token, expires_in = issue_access_token(client)
    refresh_token = refresh_token or issue_refresh_token(client)
    ApiClient.objects.filter(pk=client.pk).update(last_used_at=timezone.now())
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "Bearer",
        "expires_in": expires_in,
        "refresh_expires_in": int(refresh_token_ttl().total_seconds()),
        "scope": " ".join(client.enabled_scopes),
    }


@auth_router.post(
    "/token/",
    response={200: TokenPairOut, 400: ErrorOut, 401: ErrorOut},
    throttle=TokenEndpointThrottle(),
    summary="Exchange client credentials for an access/refresh token pair",
)
def obtain_token(request, payload: TokenRequest):
    if payload.grant_type != "client_credentials":
        raise HttpError(400, "Only grant_type=client_credentials is supported.")

    client = ApiClient.objects.filter(client_id=payload.client_id).first()
    if client is None:
        # Equalise timing with the known-client path.
        check_password(payload.client_secret, _DUMMY_SECRET_HASH)
        raise HttpError(401, "Invalid client credentials.")

    # Attribute failed attempts to the key as well as successful ones, so the
    # usage log answers "who tried what" even when authentication failed.
    request.api_client = client
    secret_ok = client.check_client_secret(payload.client_secret)
    if not client.is_active or not secret_ok:
        raise HttpError(401, "Invalid client credentials.")

    return _token_pair(client)


@auth_router.post(
    "/token/refresh/",
    response={200: TokenPairOut, 401: ErrorOut},
    throttle=TokenEndpointThrottle(),
    summary="Rotate a refresh token and return a new token pair",
)
def refresh_token(request, payload: RefreshRequest):
    redeemed = redeem_refresh_token(payload.refresh_token)
    if redeemed is None:
        raise HttpError(401, "Invalid, expired or already used refresh token.")
    client, new_refresh_token = redeemed
    # Reuse the replacement minted by the rotation instead of creating a second
    # live refresh token that the caller would never see.
    return _token_pair(client, refresh_token=new_refresh_token)


@auth_router.post(
    "/token/revoke/",
    response={204: None},
    throttle=TokenEndpointThrottle(),
    summary="Revoke a refresh token",
)
def revoke_token(request, payload: RevokeRequest):
    revoke_refresh_token(payload.refresh_token)
    return 204, None
