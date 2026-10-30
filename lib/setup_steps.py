"""The setup steps components.json names, run by `./manage.py setup <id ...>`.

    models           download the components' models (models.lock.json) into models/<name>/ and check
                     each file's SHA-256; files that already match are kept
    browser          Playwright's Chromium: the build that uv.lock's playwright pins
    ocr-helper       bin/ocr-helper: the prebuilt one, else built with swiftc when the developer tools are there
    field-head       train the extension's fallback field classifier (needs all-MiniLM-L6-v2)
    native-host      register the extension's helper with Edge and key the extension; again after every
                     update, since new code drops the key from extension/manifest.json
    broker-registry  build brokers/registry from the public lists when it's empty (it's never shipped)
    gmail-sign-in, your-details, load-extension: yours to do, reported with what to do

Each step reports start, then done, skipped (with needs_you when it's yours to do) or failed, through
manage.Events. None of them asks anything or opens a window. MACUTILS_MODELS_ONLY=name:path,... limits
`models` to those files (for tests).
"""
import glob
import hashlib
import http.client
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.request

TITLES = {
    "models": "Download the models",
    "browser": "Install Playwright's Chromium",
    "ocr-helper": "Set up the text recognition helper",
    "field-head": "Train the field classifier",
    "native-host": "Register the extension's helper with Edge",
    "broker-registry": "Build the list of data brokers",
    "gmail-sign-in": "Sign in to Gmail",
    "your-details": "Enter your details for broker requests",
    "load-extension": "Load the extension in Edge",
}
USER_STEPS = ("gmail-sign-in", "your-details", "load-extension")
CHUNK = 1 << 20
ATTEMPTS = 3


class Failed(Exception):
    pass


def needs_you(detail, **extra):
    return "needs-you", detail, extra


def plan(components):
    """[(step, [component ids])]: models first, then each component's steps in order, the user's last."""
    steps = {}
    with_models = [c["id"] for c in components if c.get("models")]
    if with_models:
        steps["models"] = with_models
    for c in components:
        for step in c.get("setup", []):
            steps.setdefault(step, []).append(c["id"])
    order = sorted(steps, key=lambda s: s in USER_STEPS)   # sorted is stable: the rest keep their order
    return [(s, steps[s]) for s in order]


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _matches(path, entry):
    return os.path.isfile(path) and os.path.getsize(path) == entry["size"] and _sha256(path) == entry["sha256"]


def _mb(size):
    return f"{size / 1e6:.1f} MB" if size >= 1e5 else f"{size / 1e3:.1f} kB"


def _tls():
    import ssl
    try:
        import certifi   # comes with requests; the same certificates whichever Python runs this
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


class Setup:
    def __init__(self, root, python, events, components):
        self.root, self.python, self.events, self.components = root, python, events, components

    def steps(self, only=None):
        """[(step, [component ids])] that run() runs."""
        return [(s, ids) for s, ids in plan(self.components) if not only or s in only] + [
            (s, []) for s in only or () if s not in dict(plan(self.components))]

    def run(self, only=None):
        """Run every step of the components (only these, if given). True unless a step failed."""
        ok = True
        steps = [(s, ids) for s, ids in plan(self.components) if not only or s in only]
        if only:
            unknown = [s for s in only if s not in dict(plan(self.components))]
            for step in unknown:
                self.events.step(step, TITLES.get(step, step), "failed", "not a setup step of these components")
            ok = not unknown
        for step, ids in steps:
            title = TITLES.get(step, step)
            self.events.step(step, title, "start", components=ids)
            try:
                method = getattr(self, "_" + step.replace("-", "_"), None)
                if method is None:
                    raise Failed(f"unknown setup step {step!r} in components.json")
                status, detail, *extra = method()
                extra = extra[0] if extra else {}
            except Failed as error:
                status, detail, extra = "failed", str(error), {}
            except Exception as error:
                status, detail, extra = "failed", f"{type(error).__name__}: {error}", {}
            if status == "needs-you":
                status, extra = "skipped", {**extra, "needs_you": True}
            ok = ok and status != "failed"
            self.events.step(step, title, status, detail, components=ids, **extra)
        return ok

    # ------------------------------------------------------------ helpers

    def _need_python(self):
        if not os.path.exists(self.python):
            raise Failed(f"no virtualenv at {os.path.dirname(os.path.dirname(self.python))}: run ./install.sh first")

    def _python_json(self, args, timeout):
        """Run `python -m <args>` in the virtualenv; its JSON answer (or {} if it printed none)."""
        self._need_python()
        try:
            done = subprocess.run([self.python, "-m", *args], cwd=self.root, capture_output=True, text=True,
                                  timeout=timeout, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise Failed(f"python -m {' '.join(args)} took over {timeout} s")
        if done.returncode != 0:
            tail = (done.stderr.strip() or done.stdout.strip()).splitlines()[-3:]
            raise Failed(f"python -m {' '.join(args)} failed: {' '.join(tail)[-400:]}")
        try:
            return json.loads(done.stdout)
        except ValueError:
            return {}

    # ------------------------------------------------------------ steps

    def _models(self):
        with open(os.path.join(self.root, "models.lock.json"), encoding="utf-8") as f:
            lock = json.load(f)["models"]
        only = os.environ.get("MACUTILS_MODELS_ONLY")
        only = {tuple(item.split(":", 1)) for item in only.split(",") if ":" in item} if only else None
        names = []
        for c in self.components:
            names += [m for m in c.get("models", []) if m not in names]
        todo, kept = [], 0
        for name in names:
            if name not in lock:
                raise Failed(f"{name} isn't in models.lock.json")
            for entry in lock[name]["files"]:
                if only is not None and (name, entry["path"]) not in only:
                    continue
                path = os.path.join(self.root, "models", name, entry["path"])
                if _matches(path, entry):
                    kept += 1
                else:
                    todo.append((name, entry, path))
        if not todo:
            return "skipped", f"{', '.join(names)}: all {kept} file(s) already downloaded and checked"
        total = sum(entry["size"] for _, entry, _ in todo)
        done = 0
        for name, entry, path in todo:
            done = self._download(f"{name}/{entry['path']}", entry, path, done, total)
        return "done", f"{', '.join(names)}: downloaded {len(todo)} file(s), {_mb(total)}" + (
            f"; {kept} already there" if kept else "")

    def _download(self, name, entry, path, base, total):
        """Fetch one file into a temporary file next to it, check its SHA-256, then move it into place.
        Returns base + its size, the bytes done so far."""
        folder = os.path.dirname(path)
        os.makedirs(folder, exist_ok=True)
        last_error, shown = None, time.monotonic()
        for attempt in range(ATTEMPTS):
            handle, part = tempfile.mkstemp(prefix=".download-", dir=folder)
            got = 0
            try:
                digest = hashlib.sha256()
                request = urllib.request.Request(entry["url"], headers={"User-Agent": "mac-utils-installer"})
                with os.fdopen(handle, "wb") as out, urllib.request.urlopen(request, timeout=60,
                                                                            context=_tls()) as response:
                    while chunk := response.read(CHUNK):
                        out.write(chunk)
                        digest.update(chunk)
                        got += len(chunk)
                        if time.monotonic() - shown > 0.5:
                            shown = time.monotonic()
                            self.events.progress("models", min(base + got, total), total, detail=name)
                if digest.hexdigest() != entry["sha256"]:
                    # Not retried: the same URL would serve the same wrong file.
                    raise Failed(f"{name}: the download doesn't match its SHA-256 in models.lock.json")
                os.chmod(part, 0o644)
                os.replace(part, path)
                self.events.progress("models", base + entry["size"], total, detail=name)
                return base + entry["size"]
            except (OSError, http.client.HTTPException) as error:   # urllib's errors are OSErrors
                last_error = error
                if attempt + 1 < ATTEMPTS:
                    time.sleep(2 * (attempt + 1))
            finally:
                if os.path.exists(part):
                    os.remove(part)
        raise Failed(f"{name}: {last_error}")

    def _browser(self):
        self._need_python()
        percent = re.compile(r"(\d+)% of")
        process = subprocess.Popen([self.python, "-m", "playwright", "install", "chromium"], cwd=self.root,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                   text=True)
        tail = []
        for line in process.stdout:     # text mode splits on \r too, so each progress update is a line
            line = line.strip()
            if not line:
                continue
            tail = (tail + [line])[-3:]
            found = percent.search(line)
            if found:
                self.events.progress("browser", int(found.group(1)), 100, detail=line.split("|")[0].strip()[:60])
        if process.wait() != 0:
            raise Failed(f"playwright install chromium failed: {' '.join(tail)[-400:]}")
        return "done", "Chromium for Playwright is installed (~/Library/Caches/ms-playwright)"

    def _ocr_helper(self):
        helper = os.path.join(self.root, "bin", "ocr-helper")
        if os.path.isfile(helper) and os.access(helper, os.X_OK):
            return "done", "the prebuilt bin/ocr-helper is in place"
        from lib.common import developer_tools_installed
        # Without the developer tools, swiftc only offers to install them, in a dialog: never call it then.
        if not developer_tools_installed():
            return needs_you("bin/ocr-helper is missing and this Mac can't build it: use the installer app, which "
                             "ships it, or install the developer tools (xcode-select --install) and run "
                             "./manage.py setup screentext")
        os.makedirs(os.path.dirname(helper), exist_ok=True)
        done = subprocess.run(["xcrun", "--sdk", "macosx", "swiftc", "-O", "-target", "arm64-apple-macos14.0",
                               "-o", "bin/ocr-helper", "lib/ocr.swift"], cwd=self.root, capture_output=True,
                              text=True, timeout=600, stdin=subprocess.DEVNULL)
        if done.returncode != 0:
            raise Failed("swiftc failed: " + " ".join(done.stderr.strip().splitlines()[-3:])[-400:])
        os.chmod(helper, 0o755)
        return "done", "built bin/ocr-helper with swiftc"

    def _field_head(self):
        out = self._python_json(["dataflow", "train-fields"], timeout=900)
        return "done", f"trained on {out.get('examples', '?')} examples"

    def _native_host(self):
        out = self._python_json(["dataflow", "install"], timeout=300)
        folder = os.path.join(self.root, "dataflow", "extension")
        if not out.get("registered"):
            return needs_you("Microsoft Edge isn't installed: install it, then run "
                             "./manage.py setup dataflow --only native-host", path=folder)
        return "done", f"extension id {out.get('extension_id')}; Load unpacked folder: {folder}", {"path": folder}

    def _broker_registry(self):
        pattern = os.path.join(self.root, "brokers", "registry", "*.yaml")
        found = len(glob.glob(pattern))
        if found:
            return "skipped", f"{found} brokers already listed"
        # Fetched from the public sources on this Mac; the list is never redistributed.
        self._python_json(["brokers", "import"], timeout=1800)
        found = len(glob.glob(pattern))
        if not found:
            raise Failed("the import listed no brokers (see .venv/bin/python -m brokers import)")
        return "done", f"{found} brokers listed from the public sources"

    def _gmail_sign_in(self):
        try:
            # Sign-in saves the address here; the token is in the Keychain, which this never reads.
            from mail import store
            address, error = store.get("address"), store.get("auth_error")
        except Exception:
            address, error = None, None
        if address and not error:
            return "done", f"signed in as {address}"
        return needs_you("Make a Desktop OAuth client in your own Google Cloud project (README: Gmail), then run: "
                         f'cd "{self.root}" && .venv/bin/python -m mail setup ~/Downloads/client_secret_….json')

    def _your_details(self):
        return needs_you("Press Ctrl+Option+Space, type \"broker report\" and fill in Your details. Nothing is "
                         "sent to a broker until you approve it.")

    def _load_extension(self):
        folder = os.path.join(self.root, "dataflow", "extension")
        try:
            from dataflow import install
            loaded = install.status()["loaded_in_edge"]
        except Exception:
            loaded = False
        if loaded:
            return "done", "the extension is loaded in Edge", {"path": folder}
        return needs_you(f"In Edge: edge://extensions → turn on Developer mode → Load unpacked → {folder}",
                         path=folder)
