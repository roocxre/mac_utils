"""Registers the helper with Edge and gives this install's extension its own fixed id.

    .venv/bin/python -m dataflow install

1. A signing key for the extension, made once per install: dataflow-extension.pem in the state
   folder (never in the repo). Its public half goes into the installed extension/manifest.json as
   "key", which fixes the extension's id even when it is loaded unpacked, so the helper can name
   it in allowed_origins. The repo's manifest has no key. Run from a git checkout, this edits the
   tracked manifest.json: don't commit that line. Copying fresh code over an installed copy drops
   the key again, so run this after every update (it keeps the same key, so the same id).
2. The host launcher (dataflow-host in the state folder) and its manifest in Edge's
   NativeMessagingHosts folder (every Edge channel that is installed).
3. The toolbar icons, drawn from an SF Symbol.

Then, once, in Edge: edge://extensions → Developer mode → Load unpacked → the extension folder
this prints (dataflow/extension of the installed copy).
"""
import base64
import hashlib
import json
import os
import subprocess

from dataflow import EXTENSION_DIR, HOST_NAME
from lib.common import state_path
from lib.identity import APP_NAME

KEY = state_path("dataflow-extension.pem")
LAUNCHER = state_path("dataflow-host")
SUPPORT = os.path.expanduser("~/Library/Application Support")
BROWSERS = ("Microsoft Edge", "Microsoft Edge Beta", "Microsoft Edge Dev", "Microsoft Edge Canary")


def _public_der():
    if not os.path.exists(KEY):
        subprocess.run(["openssl", "genrsa", "-out", KEY, "2048"], check=True, capture_output=True)
        os.chmod(KEY, 0o600)
    done = subprocess.run(["openssl", "rsa", "-in", KEY, "-pubout", "-outform", "DER"], check=True,
                          capture_output=True)
    return done.stdout


def extension_id(der=None):
    """Chromium's id for a public key: the first 32 hex digits of its SHA-256, 0-f spelled a-p."""
    digest = hashlib.sha256(der or _public_der()).hexdigest()[:32]
    return "".join(chr(ord("a") + int(c, 16)) for c in digest)


def write_key():
    der = _public_der()
    path = os.path.join(EXTENSION_DIR, "manifest.json")
    with open(path, encoding="utf-8") as f:
        manifest = json.load(f)
    key = base64.b64encode(der).decode()
    if manifest.get("key") != key:
        manifest["key"] = key
        with open(path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)
            f.write("\n")
    return extension_id(der)


def write_launcher():
    import manage
    with open(LAUNCHER, "w", encoding="utf-8") as f:
        f.write(f'#!/bin/sh\n# Edge starts this for the "Where does my email go" extension (dataflow/host.py).\n'
                f'cd "{manage.ROOT}" || exit 1\nexec "{manage.PYTHON}" -m dataflow.host "$@"\n')
    os.chmod(LAUNCHER, 0o755)
    return LAUNCHER


def host_manifest(ext_id):
    return {"name": HOST_NAME, "description": f"{APP_NAME}: where does my email go (dataflow helper)",
            "path": LAUNCHER, "type": "stdio", "allowed_origins": [f"chrome-extension://{ext_id}/"]}


def register(ext_id, folders=None):
    """Write the host manifest into each browser's NativeMessagingHosts folder. Returns the paths."""
    written = []
    targets = folders or [os.path.join(SUPPORT, b) for b in BROWSERS if os.path.isdir(os.path.join(SUPPORT, b))]
    for folder in targets:
        hosts = os.path.join(folder, "NativeMessagingHosts")
        os.makedirs(hosts, exist_ok=True)
        path = os.path.join(hosts, f"{HOST_NAME}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(host_manifest(ext_id), f, indent=2)
        written.append(path)
    return written


def draw_icons():
    """The toolbar icon: SF Symbol envelope.badge.shield.half.filled, drawn with AppKit."""
    folder = os.path.join(EXTENSION_DIR, "icons")
    os.makedirs(folder, exist_ok=True)
    try:
        import AppKit
    except ImportError:
        return False
    for size in (16, 32, 48, 128):
        rep = AppKit.NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
            None, size, size, 8, 4, True, False, AppKit.NSDeviceRGBColorSpace, 0, 0)
        AppKit.NSGraphicsContext.saveGraphicsState()
        AppKit.NSGraphicsContext.setCurrentContext_(AppKit.NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep))
        config = AppKit.NSImageSymbolConfiguration.configurationWithPointSize_weight_(size * 0.7,
                                                                                      AppKit.NSFontWeightSemibold)
        image = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            "envelope.badge.shield.half.filled", None) or AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            "envelope", None)
        image = image.imageWithSymbolConfiguration_(config)
        tinted = AppKit.NSImage.alloc().initWithSize_(image.size())
        tinted.lockFocus()
        image.drawAtPoint_fromRect_operation_fraction_((0, 0), AppKit.NSZeroRect, AppKit.NSCompositingOperationSourceOver, 1.0)
        AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(0.04, 0.39, 0.79, 1).set()
        AppKit.NSRectFillUsingOperation(((0, 0), image.size()), AppKit.NSCompositingOperationSourceAtop)
        tinted.unlockFocus()
        w, h = image.size()
        scale = min(size / w, size / h) * 0.92
        dw, dh = w * scale, h * scale
        tinted.drawInRect_fromRect_operation_fraction_((((size - dw) / 2, (size - dh) / 2), (dw, dh)),
                                                       AppKit.NSZeroRect, AppKit.NSCompositingOperationSourceOver, 1.0)
        AppKit.NSGraphicsContext.restoreGraphicsState()
        data = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, None)
        data.writeToFile_atomically_(os.path.join(folder, f"{size}.png"), True)
    return True


def install():
    ext_id = write_key()
    write_launcher()
    paths = register(ext_id)
    icons = draw_icons()
    return {"extension_id": ext_id, "host": HOST_NAME, "launcher": LAUNCHER, "registered": paths,
            "icons": icons, "next": "in Edge: edge://extensions → Developer mode on → Load unpacked → "
                                    f"{EXTENSION_DIR}"}


def status():
    try:
        ext_id = extension_id() if os.path.exists(KEY) else None      # looking never makes a key
    except Exception:
        ext_id = None
    registered = [b for b in BROWSERS if os.path.exists(os.path.join(SUPPORT, b, "NativeMessagingHosts",
                                                                     f"{HOST_NAME}.json"))]
    return {"extension_id": ext_id, "registered_with": registered, "launcher": os.path.exists(LAUNCHER),
            "loaded_in_edge": loaded_in_edge(ext_id) if ext_id else None}


def loaded_in_edge(ext_id):
    """Whether any Edge profile has the extension (its folder under Extensions or in Preferences)."""
    root = os.path.join(SUPPORT, "Microsoft Edge")
    try:
        for profile in os.listdir(root):
            prefs = os.path.join(root, profile, "Secure Preferences")
            if os.path.exists(prefs):
                with open(prefs, encoding="utf-8", errors="ignore") as f:
                    if ext_id in f.read():
                        return True
    except OSError:
        pass
    return False
