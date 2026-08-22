#!/usr/bin/env python3
"""
Shared JWT cache for the test campaign — don't spam-mint tokens.

First call mints fresh against the Coordinator's /api/auth/login (returns
the JWT in the hermod_token Set-Cookie header per the post-2026 cookie
flow) and writes {token, exp} to /tmp/hermod-test-token.json. Subsequent
calls return the cached token until exp < now + 30 s, then re-mint.

Usage (bash):
    TOKEN=$(python3 get-token.py)
    curl -H "Authorization: Bearer $TOKEN" …

Usage (Python):
    from get_token import get_token
    tok = get_token()

Environment overrides:
    HERMOD_TOKEN_BASE       default http://<pi-ip>:32069
    HERMOD_TOKEN_EMAIL      default v@l.l
    HERMOD_TOKEN_PASSWORD   default change-me-in-production-user
    HERMOD_TOKEN_CACHE      default /tmp/hermod-test-token.json
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

CACHE = os.environ.get("HERMOD_TOKEN_CACHE", "/tmp/hermod-test-token.json")
BASE = os.environ.get("HERMOD_TOKEN_BASE", "http://<pi-ip>:42069")
EMAIL = os.environ.get("HERMOD_TOKEN_EMAIL", "v@l.l")
PASSWORD = os.environ.get("HERMOD_TOKEN_PASSWORD", "change-me-in-production-user")

JWT_RX = re.compile(r"=\s*(eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+)")


def _decode_exp(token: str) -> int:
    """Return the JWT's exp claim (unix seconds), or 0 if undecodable."""
    try:
        _, payload, _ = token.split(".")
        pad = "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload + pad))
        return int(data.get("exp") or 0)
    except Exception:
        return 0


def _read_cache() -> str | None:
    try:
        d = json.loads(open(CACHE).read())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    tok = d.get("token")
    exp = int(d.get("exp") or 0)
    if not tok:
        return None
    if exp - int(time.time()) < 30:
        return None
    return tok


def _write_cache(token: str, exp: int) -> None:
    try:
        with open(CACHE, "w") as f:
            json.dump({"token": token, "exp": exp}, f)
        os.chmod(CACHE, 0o600)
    except OSError:
        pass   # best-effort; bad disk shouldn't kill the runner


def _mint(base: str, email: str, password: str) -> str:
    body = json.dumps({"email": email, "password": password}).encode()
    req = urllib.request.Request(
        f"{base}/api/auth/login",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    delay = 2
    for _ in range(5):
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                # Cookie path (current Coord builds): scrape Set-Cookie.
                for cookie in (resp.headers.get_all("Set-Cookie") or []):
                    m = JWT_RX.search(cookie)
                    if m:
                        return m.group(1)
                # Legacy body path.
                raw = resp.read().decode()
                try:
                    data = json.loads(raw)
                    tok = data.get("access_token") or data.get("accessToken")
                    if tok:
                        return tok
                except ValueError:
                    pass
        except urllib.error.HTTPError as e:
            if e.code not in (429, 502, 503, 504):
                raise
        except Exception:
            pass
        time.sleep(delay)
        delay = min(delay * 2, 32)
    raise RuntimeError(f"login at {base} returned no token after retries")


def get_token() -> str:
    # AuthBypass mode: Coord skips JWT validation when started with
    # Hermod__Security__AuthBypass=true. The Bearer header still has to
    # carry SOMETHING for ASP.NET to assign the AuthBypass scheme, but
    # the value is ignored. Skip the vault42 mint round-trip.
    if os.environ.get("HERMOD_AUTH_BYPASS") == "1":
        return "bypass"
    cached = _read_cache()
    if cached:
        return cached
    token = _mint(BASE, EMAIL, PASSWORD)
    _write_cache(token, _decode_exp(token))
    return token


if __name__ == "__main__":
    try:
        print(get_token())
    except Exception as e:
        print(f"get-token: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
