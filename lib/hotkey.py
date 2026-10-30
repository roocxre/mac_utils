"""Global (system-wide) hotkeys through the Carbon event API, which needs no permissions."""
import ctypes
import ctypes.util
from ctypes import POINTER, byref, c_int32, c_uint32, c_void_p

# Modifier masks for register_hotkey. Key codes are macOS virtual key codes
# (49 is Space, 19 is "2", 5 is "G").
CMD, SHIFT, OPTION, CONTROL = 0x0100, 0x0200, 0x0800, 0x1000

_carbon = ctypes.CDLL(ctypes.util.find_library("Carbon"))


class EventTypeSpec(ctypes.Structure):
    _fields_ = [("eventClass", c_uint32), ("eventKind", c_uint32)]


class EventHotKeyID(ctypes.Structure):
    _fields_ = [("signature", c_uint32), ("id", c_uint32)]


EventHandlerProc = ctypes.CFUNCTYPE(c_int32, c_void_p, c_void_p, c_void_p)
_carbon.GetApplicationEventTarget.restype = c_void_p
_carbon.InstallEventHandler.argtypes = [c_void_p, EventHandlerProc, c_uint32, POINTER(EventTypeSpec),
                                        c_void_p, POINTER(c_void_p)]
_carbon.InstallEventHandler.restype = c_int32
_carbon.RegisterEventHotKey.argtypes = [c_uint32, c_uint32, EventHotKeyID, c_void_p, c_uint32,
                                        POINTER(c_void_p)]
_carbon.RegisterEventHotKey.restype = c_int32

EVENT_CLASS_KEYBOARD = int.from_bytes(b"keyb", "big")
EVENT_HOTKEY_PRESSED = 5

_keep_alive = []  # ctypes callbacks and Carbon refs must outlive registration


def register_hotkey(key_code, modifiers, callback, hotkey_id=1):
    """Register a system-wide hotkey. Returns the Carbon status (0 on success)."""
    def _handler(_call_ref, _event, _user_data):
        callback()
        return 0

    proc = EventHandlerProc(_handler)
    spec = EventTypeSpec(EVENT_CLASS_KEYBOARD, EVENT_HOTKEY_PRESSED)
    handler_ref, hotkey_ref = c_void_p(), c_void_p()
    target = _carbon.GetApplicationEventTarget()
    status = _carbon.InstallEventHandler(target, proc, 1, byref(spec), None, byref(handler_ref))
    if status == 0:
        hotkey_id = EventHotKeyID(int.from_bytes(b"mUtl", "big"), hotkey_id)
        status = _carbon.RegisterEventHotKey(key_code, modifiers, hotkey_id, target, 0, byref(hotkey_ref))
    _keep_alive.extend([proc, handler_ref, hotkey_ref])
    return status
