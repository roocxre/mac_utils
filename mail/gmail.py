"""The one Gmail API client every email agent uses (internal: not one of the commands).

    from mail import gmail
    api = gmail.client()
    api.message(message_id, "metadata")

Retries 429, 5xx and Gmail's rate-limit 403s with exponential backoff (honouring Retry-After).
Every call first takes its quota units from a token bucket kept in a locked file, so all the
processes together stay well under Gmail's per-user limit (250 units a second).
"""
import base64
import json
import random
import time

from lib.common import state_path
from mail import auth, store

API = "https://gmail.googleapis.com/gmail/v1/users/me"
RATE = 100                  # quota units a second, all processes together
BURST = 500
MAX_TRIES = 6
MAX_BACKOFF = 64
TIMEOUT = 30
RETRY_STATUS = {429, 500, 502, 503, 504}
RATE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded"}
# Quota units per method (developers.google.com/gmail/api/reference/quota).
COST = {"profile": 1, "history": 2, "messages.list": 5, "messages.get": 5, "messages.modify": 5,
        "messages.trash": 5, "messages.untrash": 5, "messages.send": 100, "threads.get": 10, "labels.list": 1, "labels.create": 5,
        "attachments.get": 5}


class GmailError(Exception):
    def __init__(self, status, text):
        super().__init__(f"gmail {status}: {text}")
        self.status = status


class NotFound(GmailError):
    pass


NotSignedIn = auth.NotSignedIn


def _take(units):
    """Wait until the shared bucket holds `units`, then take them."""
    path = state_path("gmail-bucket.json")
    while True:
        with store.file_lock("gmail-bucket"):
            try:
                with open(path, encoding="utf-8") as f:
                    bucket = json.load(f)
            except (OSError, ValueError):
                bucket = {"tokens": BURST, "at": time.time()}
            now = time.time()
            tokens = min(BURST, bucket["tokens"] + (now - bucket["at"]) * RATE)
            if tokens >= units:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump({"tokens": tokens - units, "at": now}, f)
                return
            wait = (units - tokens) / RATE
        time.sleep(wait)


def _reason(response):
    try:
        errors = response.json().get("error", {})
        return (errors.get("errors") or [{}])[0].get("reason", ""), errors.get("message", "")
    except ValueError:
        return "", response.text[:200]


class Client:
    def __init__(self):
        import requests
        self.session = requests.Session()

    def call(self, method, path, cost_key, params=None, body=None):
        refreshed = False
        for attempt in range(MAX_TRIES):
            _take(COST[cost_key])
            response = self.session.request(method, API + path, params=params, json=body, timeout=TIMEOUT,
                                            headers={"Authorization": f"Bearer {auth.access_token()}"})
            if response.status_code == 401 and not refreshed:
                auth.invalidate()
                refreshed = True
                continue
            reason, text = _reason(response) if response.status_code >= 400 else ("", "")
            last = f"{reason} {text}".strip()
            if response.status_code in RETRY_STATUS or (response.status_code == 403 and reason in RATE_REASONS):
                after = response.headers.get("Retry-After", "")
                delay = float(after) if after.isdigit() else min(MAX_BACKOFF, 2 ** attempt) + random.random()
                time.sleep(delay)
                continue
            if response.status_code == 404:
                raise NotFound(404, text or path)
            if response.status_code >= 400:
                raise GmailError(response.status_code, f"{reason} {text}".strip())
            return response.json() if response.content else {}
        raise GmailError(response.status_code, f"{last} (still failing after {MAX_TRIES} tries)")

    # ------------------------------------------------------------ reading

    def profile(self):
        return self.call("GET", "/profile", "profile")

    def history(self, start_id, types=("messageAdded", "labelAdded", "labelRemoved")):
        """Every history record since start_id, and the mailbox's current historyId. Raises NotFound when expired."""
        records, page, latest = [], None, None
        while True:
            params = {"startHistoryId": start_id, "historyTypes": list(types), "maxResults": 500}
            if page:
                params["pageToken"] = page
            answer = self.call("GET", "/history", "history", params)
            records += answer.get("history", [])
            latest = answer.get("historyId", latest)
            page = answer.get("nextPageToken")
            if not page:
                return records, latest

    def message(self, message_id, fmt="metadata", headers=None):
        params = {"format": fmt}
        if headers:
            params["metadataHeaders"] = list(headers)
        return self.call("GET", f"/messages/{message_id}", "messages.get", params)

    def thread(self, thread_id, fmt="metadata"):
        return self.call("GET", f"/threads/{thread_id}", "threads.get", {"format": fmt})

    def list_messages(self, query, include_spam_trash=False, limit=2000):
        """Ids (with thread ids) of messages matching a Gmail search, newest first."""
        found, page = [], None
        while len(found) < limit:
            params = {"q": query, "maxResults": min(500, limit - len(found)), "includeSpamTrash": include_spam_trash}
            if page:
                params["pageToken"] = page
            answer = self.call("GET", "/messages", "messages.list", params)
            found += answer.get("messages", [])
            page = answer.get("nextPageToken")
            if not page:
                break
        return found

    def labels(self):
        return self.call("GET", "/labels", "labels.list").get("labels", [])

    # ------------------------------------------------------------ changing

    def modify(self, message_id, add=(), remove=()):
        return self.call("POST", f"/messages/{message_id}/modify", "messages.modify",
                         body={"addLabelIds": list(add), "removeLabelIds": list(remove)})

    def trash(self, message_id):
        return self.call("POST", f"/messages/{message_id}/trash", "messages.trash")

    def untrash(self, message_id):
        return self.call("POST", f"/messages/{message_id}/untrash", "messages.untrash")

    def send(self, mime_message, thread_id=None):
        """Send an email.message.EmailMessage. Returns {id, threadId, labelIds}."""
        body = {"raw": base64.urlsafe_b64encode(mime_message.as_bytes()).decode()}
        if thread_id:
            body["threadId"] = thread_id
        return self.call("POST", "/messages/send", "messages.send", body=body)

    def create_label(self, name):
        return self.call("POST", "/labels", "labels.create",
                         body={"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"})

    def label_id(self, name):
        """The id of a user label, created if it does not exist yet."""
        for label in self.labels():
            if label["name"].lower() == name.lower():
                return label["id"]
        return self.create_label(name)["id"]


_client = None


def client():
    """The shared client for this process."""
    global _client
    if _client is None:
        _client = Client()
    return _client


def compose(to, subject, body, in_reply_to=None, references=None):
    """A plain-text message from the signed-in address, ready for Client.send."""
    from email.message import EmailMessage

    message = EmailMessage()
    message["From"] = store.get("address") or "me"
    message["To"] = to
    message["Subject"] = subject
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
        message["References"] = references or in_reply_to
    message.set_content(body)
    return message
