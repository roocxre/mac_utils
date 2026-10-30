"""Signing in to Gmail, and keeping a working access token.

One-time setup (or again whenever Google revokes the token):

    .venv/bin/python -m mail setup ~/Downloads/client_secret_….json

The file is the "Desktop app" OAuth client from your own Google Cloud project. Its consent screen
must be set to "In production": while it is "Testing", Google's refresh tokens die after 7 days and
every email agent stops. Setup opens the browser, you sign in once for both scopes, and the
tokens go into the login Keychain (service SERVICE). Nothing is written to disk in the clear.

Access tokens last an hour. Every process reads the current one from the Keychain; only one at a
time refreshes it (a file lock), and a refresh that Google refuses is reported once as a banner.
"""
import base64
import hashlib
import json
import os
import secrets
import subprocess
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

from lib.identity import keychain_service
from mail import store

SERVICE = keychain_service("gmail")
SCOPES = ["https://www.googleapis.com/auth/gmail.modify",
          "https://www.googleapis.com/auth/gmail.send"]
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SIGN_IN_SECONDS = 300
EARLY = 120                 # refresh this long before the token runs out

_cached = {}                # this process's access token: {"token", "expires_at"}


class NotSignedIn(Exception):
    """No usable token: run setup."""


# ------------------------------------------------------------ the Keychain

def _keychain_get(account):
    done = subprocess.run(["security", "find-generic-password", "-a", account, "-s", SERVICE, "-w"],
                          capture_output=True, text=True)
    if done.returncode != 0:
        return None
    try:
        return json.loads(base64.b64decode(done.stdout.strip()))
    except ValueError:
        return None


def _keychain_put(account, value):
    # Through `security -i` on stdin, so the secret never appears in a process listing. Base64
    # keeps quotes and spaces out of the line it parses.
    encoded = base64.b64encode(json.dumps(value).encode()).decode()
    done = subprocess.run(["security", "-i"], input=f'add-generic-password -U -a {account} -s {SERVICE} -w "{encoded}"\n',
                          capture_output=True, text=True)
    if done.returncode != 0 or "error" in done.stderr.lower():
        raise OSError(f"could not save to the Keychain: {done.stderr.strip()[:200]}")


def _keychain_delete(account):
    subprocess.run(["security", "delete-generic-password", "-a", account, "-s", SERVICE], capture_output=True)


# ------------------------------------------------------------ setup

def _client_from_file(path):
    with open(os.path.expanduser(path), encoding="utf-8") as f:
        data = json.load(f)
    client = data.get("installed")
    if not client:
        raise ValueError("this is not a Desktop app OAuth client file (no \"installed\" section). Make one in your own "
                         "Google Cloud project: APIs & Services > Credentials > Create credentials > OAuth client ID > "
                         "Desktop app, then download its JSON")
    return {"client_id": client["client_id"], "client_secret": client["client_secret"]}


def _wait_for_code(server, expected_state):
    """Serve the loopback redirect until Google sends the code back, or SIGN_IN_SECONDS pass."""
    got = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            query = parse_qs(urlsplit(self.path).query)
            if "code" in query or "error" in query:
                got.update({k: v[0] for k, v in query.items()})
            ok = "code" in got and got.get("state") == expected_state
            body = ("Signed in. You can close this tab." if ok else "Sign-in did not finish: "
                    + got.get("error", "no code came back")).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server.RequestHandlerClass = Handler
    server.timeout = 1
    deadline = time.time() + SIGN_IN_SECONDS
    while not got and time.time() < deadline:
        server.handle_request()
    if not got:
        raise TimeoutError("no sign-in within 5 minutes")
    if got.get("error"):
        raise PermissionError(f"Google said: {got['error']}")
    if got.get("state") != expected_state:
        raise PermissionError("the answer did not match this sign-in (state); try again")
    return got["code"]


def setup(client_file, say=print):
    """Sign in through the browser and keep the tokens. Returns {"address", "missing_scopes"}."""
    import requests

    client = _client_from_file(client_file)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    server = HTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    redirect = f"http://127.0.0.1:{server.server_port}"
    url = AUTH_URL + "?" + urlencode({
        "client_id": client["client_id"], "redirect_uri": redirect, "response_type": "code",
        "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent",
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state})
    say("opening the browser to sign in to Gmail. If Google warns the app is unverified, that is your own "
        "app: Advanced > Go to … (unsafe). Leave every permission ticked.")
    subprocess.run(["open", url], check=False)
    try:
        code = _wait_for_code(server, state)
    finally:
        server.server_close()
    response = requests.post(TOKEN_URL, data={
        "code": code, "client_id": client["client_id"], "client_secret": client["client_secret"],
        "redirect_uri": redirect, "grant_type": "authorization_code", "code_verifier": verifier}, timeout=30)
    tokens = response.json()
    if response.status_code != 200 or "refresh_token" not in tokens:
        raise PermissionError(f"Google did not give a refresh token: {tokens.get('error_description') or tokens}")
    granted = set(tokens.get("scope", "").split())
    _keychain_put("client", client)
    _keychain_put("token", {"refresh_token": tokens["refresh_token"], "scopes": sorted(granted), "obtained_at": time.time()})
    _remember_access(tokens)
    store.put("auth_error", None)
    store.put("auth_notified", None)

    from mail import gmail
    address = gmail.client().profile()["emailAddress"]
    store.put("address", address)
    missing = [s.rsplit("/", 1)[1] for s in SCOPES if s not in granted]
    say(f"signed in as {address}" + (f". NOT granted: {', '.join(missing)}; run setup again and leave them ticked"
                                      if missing else ""))
    return {"address": address, "missing_scopes": missing}


def sign_out():
    for account in ("client", "token", "access"):
        _keychain_delete(account)
    _cached.clear()
    store.put("address", None)


# ------------------------------------------------------------ access tokens

def _remember_access(tokens):
    access = {"token": tokens["access_token"], "expires_at": time.time() + int(tokens.get("expires_in", 3600))}
    _keychain_put("access", access)
    _cached.update(access)
    store.put("auth_refreshed_at", time.time())


def _fresh(access):
    return bool(access) and access.get("expires_at", 0) - EARLY > time.time()


def access_token():
    """A working access token, refreshing it if needed. Raises NotSignedIn."""
    if _fresh(_cached):
        return _cached["token"]
    access = _keychain_get("access")
    if _fresh(access):
        _cached.update(access)
        return access["token"]
    with store.file_lock("gmail-token"):
        access = _keychain_get("access")       # another process may have refreshed while we waited
        if _fresh(access):
            _cached.update(access)
            return access["token"]
        return _refresh()


def invalidate():
    """Forget the access token (the API said 401), so the next call refreshes."""
    _cached.clear()
    _keychain_delete("access")


def _refresh():
    import requests

    client, token = _keychain_get("client"), _keychain_get("token")
    if not client or not token:
        raise NotSignedIn("not signed in to Gmail: python -m mail setup CLIENT_FILE")
    response = requests.post(TOKEN_URL, data={
        "client_id": client["client_id"], "client_secret": client["client_secret"],
        "refresh_token": token["refresh_token"], "grant_type": "refresh_token"}, timeout=30)
    answer = response.json() if response.content else {}
    if response.status_code == 200 and "access_token" in answer:
        _remember_access(answer)
        store.put("auth_error", None)
        return answer["access_token"]
    error = answer.get("error", f"http {response.status_code}")
    if error == "invalid_grant":
        _fail("Google revoked the Gmail sign-in (or it expired: is the consent screen still in Testing?). "
              "Run: python -m mail setup CLIENT_FILE")
    raise NotSignedIn(f"token refresh failed: {error}")


def _fail(text):
    store.put("auth_error", {"text": text, "at": time.time()})
    if not store.get("auth_notified"):
        from lib.common import notify
        notify("Gmail agents stopped", text)
        store.put("auth_notified", time.time())
    raise NotSignedIn(text)


def health():
    """What status reports: who is signed in, missing scopes, the last refresh and any error."""
    token = _keychain_get("token")
    error = store.get("auth_error")
    refreshed = store.get("auth_refreshed_at")
    return {
        "signed_in": bool(token) and not error,
        "address": store.get("address"),
        "missing_scopes": [s.rsplit("/", 1)[1] for s in SCOPES if token and s not in token.get("scopes", [])],
        "signed_in_days_ago": round((time.time() - token["obtained_at"]) / 86400, 1) if token else None,
        "last_refresh_min_ago": round((time.time() - refreshed) / 60) if refreshed else None,
        "error": error["text"] if error else None,
    }
