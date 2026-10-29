from __future__ import annotations

from urllib.parse import parse_qsl, urlsplit


def has_url_credentials(url: str, *, include_key: bool = False) -> bool:
    parsed = urlsplit(url)
    return (
        parsed.username is not None
        or parsed.password is not None
        or any(
            key.lower()
            in {
                "token",
                "access_token",
                "refresh_token",
                "client_secret",
                "api-key",
                "q-signature",
                "api_key",
                "apikey",
                "signature",
                "sig",
                "credential",
                "credentials",
                "auth",
                "authorization",
                "password",
                "secret",
                "awsaccesskeyid",
            }
            or key.lower().startswith(("x-amz-", "x-goog-"))
            or include_key
            and key.lower() == "key"
            for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
        )
    )
