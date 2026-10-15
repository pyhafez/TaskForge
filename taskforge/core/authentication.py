import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import jwt
from django.conf import settings
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.exceptions import AuthenticationFailed

from taskforge.config import env
from taskforge.core.models import APIKey, User


def issue_token(user):
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": str(user.pk),
            "iat": now,
            "exp": now + timedelta(minutes=env.jwt_ttl_minutes),
            "iss": "taskforge",
            "aud": "taskforge-api",
            "jti": uuid.uuid4().hex,
        },
        settings.SECRET_KEY,
        algorithm="HS256",
    )


def create_key(user, name):
    raw = "tf_" + secrets.token_urlsafe(32)
    key = APIKey.objects.create(
        user=user, name=name, prefix=raw[:12], digest=hashlib.sha256(raw.encode()).hexdigest()
    )
    return key, raw


class TokenOrKeyAuthentication(BaseAuthentication):
    def authenticate_header(self, request):
        return "Bearer"

    def authenticate(self, request):
        raw_key = request.headers.get("X-API-Key")
        if raw_key is not None:
            if len(raw_key) > 200:
                raise AuthenticationFailed("Invalid API key.")
            digest = hashlib.sha256(raw_key.encode()).hexdigest()
            key = (
                APIKey.objects.select_related("user")
                .filter(digest=digest, revoked_at__isnull=True, user__is_active=True)
                .first()
            )
            if not key:
                raise AuthenticationFailed("Invalid or revoked API key.")
            return key.user, key
        header = get_authorization_header(request).split()
        if not header:
            return None
        if len(header) != 2 or header[0].lower() != b"bearer":
            raise AuthenticationFailed("Expected a Bearer token.")
        try:
            claims = jwt.decode(
                header[1],
                settings.SECRET_KEY,
                algorithms=["HS256"],
                audience="taskforge-api",
                issuer="taskforge",
                options={"require": ["exp", "iat", "sub", "jti"]},
            )
            user = User.objects.get(pk=int(claims["sub"]), is_active=True)
        except (
            jwt.InvalidTokenError,
            User.DoesNotExist,
            ValueError,
            TypeError,
            OverflowError,
        ) as exc:
            raise AuthenticationFailed("Invalid or expired token.") from exc
        return user, claims
