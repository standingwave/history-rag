"""Permission-free mic-in-use probe (macOS). CoreAudio's
kAudioDevicePropertyDeviceIsRunningSomewhere reports whether any process is
capturing from an input device — that the mic is live, never audio content.
In-process ctypes (no subprocess, no TCC prompt); any failure reads as "not
in use" so the daemon degrades to pre-feature behavior.
"""
import ctypes
import ctypes.util
import struct

_SYSTEM_OBJECT = 1          # kAudioObjectSystemObject

def _fourcc(code: str) -> int:
    return struct.unpack(">I", code.encode())[0]

class _AOPA(ctypes.Structure):  # AudioObjectPropertyAddress
    _fields_ = [("mSelector", ctypes.c_uint32),
                ("mScope", ctypes.c_uint32),
                ("mElement", ctypes.c_uint32)]

_ca = None

def _coreaudio():
    """The CoreAudio CDLL, loaded once; None where unavailable (non-mac)."""
    global _ca
    if _ca is None:
        path = ctypes.util.find_library("CoreAudio")
        _ca = ctypes.CDLL(path) if path else False
    return _ca or None

def _devices(ca):
    addr = _AOPA(_fourcc("dev#"), _fourcc("glob"), 0)   # kAudioHardwarePropertyDevices
    size = ctypes.c_uint32()
    if ca.AudioObjectGetPropertyDataSize(_SYSTEM_OBJECT, ctypes.byref(addr),
                                         0, None, ctypes.byref(size)):
        return []
    devs = (ctypes.c_uint32 * (size.value // 4))()
    if ca.AudioObjectGetPropertyData(_SYSTEM_OBJECT, ctypes.byref(addr),
                                     0, None, ctypes.byref(size), devs):
        return []
    return list(devs)

def _has_input(ca, dev) -> bool:
    """Any input streams? (StreamConfiguration size on the input scope —
    more than an empty AudioBufferList means at least one input buffer.)"""
    addr = _AOPA(_fourcc("slay"), _fourcc("inpt"), 0)
    size = ctypes.c_uint32()
    return (ca.AudioObjectGetPropertyDataSize(dev, ctypes.byref(addr),
                                              0, None, ctypes.byref(size)) == 0
            and size.value > 8)

def _running(ca, dev) -> bool:
    addr = _AOPA(_fourcc("gone"), _fourcc("glob"), 0)   # DeviceIsRunningSomewhere
    size = ctypes.c_uint32(4)
    val = ctypes.c_uint32(0)
    err = ca.AudioObjectGetPropertyData(dev, ctypes.byref(addr),
                                        0, None, ctypes.byref(size),
                                        ctypes.byref(val))
    return err == 0 and val.value != 0

def mic_in_use() -> bool:
    """True while any input-capable audio device is capturing."""
    try:
        ca = _coreaudio()
        if not ca:
            return False
        return any(_has_input(ca, d) and _running(ca, d) for d in _devices(ca))
    except Exception:
        return False
