"""Minimal CoreAudio HAL access through ctypes (PyObjC's CoreAudio bindings are unreliable)."""
import ctypes
import ctypes.util
import struct
from collections import namedtuple
from ctypes import POINTER, byref, c_int32, c_uint32, c_void_p, sizeof

_ca = ctypes.CDLL(ctypes.util.find_library("CoreAudio"))
_cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))


def fourcc(code):
    return struct.unpack(">I", code.encode("ascii"))[0]


class Address(ctypes.Structure):
    _fields_ = [("selector", c_uint32), ("scope", c_uint32), ("element", c_uint32)]


SYSTEM_OBJECT = 1
SCOPE_GLOBAL = fourcc("glob")
SCOPE_OUTPUT = fourcc("outp")

DEFAULT_OUTPUT = fourcc("dOut")
DEFAULT_SYSTEM_OUTPUT = fourcc("sOut")
DEVICES = fourcc("dev#")
RUN_LOOP = fourcc("rnlp")
NAME = fourcc("lnam")
TRANSPORT = fourcc("tran")
STREAMS = fourcc("stm#")
MUTE = fourcc("mute")

ListenerProc = ctypes.CFUNCTYPE(c_int32, c_uint32, c_uint32, POINTER(Address), c_void_p)

_ca.AudioObjectGetPropertyDataSize.argtypes = [c_uint32, POINTER(Address), c_uint32, c_void_p, POINTER(c_uint32)]
_ca.AudioObjectGetPropertyData.argtypes = [c_uint32, POINTER(Address), c_uint32, c_void_p, POINTER(c_uint32), c_void_p]
_ca.AudioObjectSetPropertyData.argtypes = [c_uint32, POINTER(Address), c_uint32, c_void_p, c_uint32, c_void_p]
_ca.AudioObjectAddPropertyListener.argtypes = [c_uint32, POINTER(Address), ListenerProc, c_void_p]
for _fn in (_ca.AudioObjectGetPropertyDataSize, _ca.AudioObjectGetPropertyData,
            _ca.AudioObjectSetPropertyData, _ca.AudioObjectAddPropertyListener):
    _fn.restype = c_int32

_cf.CFStringGetLength.argtypes = [c_void_p]
_cf.CFStringGetLength.restype = ctypes.c_long
_cf.CFStringGetCString.argtypes = [c_void_p, ctypes.c_char_p, ctypes.c_long, c_uint32]
_cf.CFStringGetCString.restype = ctypes.c_bool
_cf.CFRelease.argtypes = [c_void_p]
_CF_UTF8 = 0x08000100

# A device as it looked when we described it; transport is a four-char code such as
# 'bltn' (built-in), 'blue'/'blea' (Bluetooth), 'usb ', 'hdmi', 'dprt' (DisplayPort), 'virt'.
Device = namedtuple("Device", "id name transport")

_listeners = []  # keeps ctypes callbacks alive for the life of the process


def _get(obj, selector, scope, ctype):
    addr = Address(selector, scope, 0)
    value = ctype()
    size = c_uint32(sizeof(value))
    status = _ca.AudioObjectGetPropertyData(obj, byref(addr), 0, None, byref(size), byref(value))
    return value if status == 0 else None


def default_output():
    value = _get(SYSTEM_OBJECT, DEFAULT_OUTPUT, SCOPE_GLOBAL, c_uint32)
    return value.value if value is not None else 0


def set_default_output(device_id):
    addr = Address(DEFAULT_OUTPUT, SCOPE_GLOBAL, 0)
    value = c_uint32(device_id)
    return _ca.AudioObjectSetPropertyData(SYSTEM_OBJECT, byref(addr), 0, None, sizeof(value), byref(value)) == 0


def _has_output(device_id):
    addr = Address(STREAMS, SCOPE_OUTPUT, 0)
    size = c_uint32(0)
    status = _ca.AudioObjectGetPropertyDataSize(device_id, byref(addr), 0, None, byref(size))
    return status == 0 and size.value > 0


def output_device_ids():
    addr = Address(DEVICES, SCOPE_GLOBAL, 0)
    size = c_uint32(0)
    if _ca.AudioObjectGetPropertyDataSize(SYSTEM_OBJECT, byref(addr), 0, None, byref(size)) != 0:
        return []
    ids = (c_uint32 * (size.value // sizeof(c_uint32)))()
    if _ca.AudioObjectGetPropertyData(SYSTEM_OBJECT, byref(addr), 0, None, byref(size), ids) != 0:
        return []
    return [i for i in ids if _has_output(i)]


def describe(device_id):
    """Snapshot a device's name and transport. Returns None if the device is gone."""
    ref = _get(device_id, NAME, SCOPE_GLOBAL, c_void_p)
    if ref is None or not ref.value:
        return None
    buf = ctypes.create_string_buffer(_cf.CFStringGetLength(ref) * 4 + 1)
    ok = _cf.CFStringGetCString(ref, buf, len(buf), _CF_UTF8)
    _cf.CFRelease(ref)
    name = buf.value.decode("utf-8", "replace") if ok else ""
    transport = _get(device_id, TRANSPORT, SCOPE_GLOBAL, c_uint32)
    code = struct.pack(">I", transport.value).decode("ascii", "replace") if transport is not None else ""
    return Device(device_id, name, code)


def get_mute(device_id):
    value = _get(device_id, MUTE, SCOPE_OUTPUT, c_uint32)
    return bool(value.value) if value is not None else None


def set_mute(device_id, muted):
    addr = Address(MUTE, SCOPE_OUTPUT, 0)
    value = c_uint32(1 if muted else 0)
    return _ca.AudioObjectSetPropertyData(device_id, byref(addr), 0, None, sizeof(value), byref(value)) == 0


def listen(selector, callback):
    """Call callback() (on a CoreAudio thread) whenever a system-wide property changes."""
    if not _listeners:
        # Ask the HAL to deliver notifications on its own thread rather than our run loop.
        addr = Address(RUN_LOOP, SCOPE_GLOBAL, 0)
        null = c_void_p(None)
        _ca.AudioObjectSetPropertyData(SYSTEM_OBJECT, byref(addr), 0, None, sizeof(null), byref(null))

    def _proc(_obj, _count, _addresses, _client):
        try:
            callback()
        except Exception:
            pass
        return 0

    proc = ListenerProc(_proc)
    addr = Address(selector, SCOPE_GLOBAL, 0)
    _listeners.append((proc, addr))
    return _ca.AudioObjectAddPropertyListener(SYSTEM_OBJECT, byref(addr), proc, None) == 0
