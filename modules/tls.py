import ssl
from typing import Optional

_CONTEXT: Optional[ssl.SSLContext] = None
_CERTIFI_PATH: Optional[str] = None
_CERTIFI_LOADED = False


def _certifi_path() -> Optional[str]:
    global _CERTIFI_PATH, _CERTIFI_LOADED
    if _CERTIFI_LOADED:
        return _CERTIFI_PATH
    _CERTIFI_LOADED = True
    try:
        import certifi
        _CERTIFI_PATH = certifi.where()
    except Exception:
        _CERTIFI_PATH = None
    return _CERTIFI_PATH


def verified_context() -> ssl.SSLContext:
    global _CONTEXT
    if _CONTEXT is not None:
        return _CONTEXT
    ctx = ssl.create_default_context()
    try:
        ctx.load_default_certs()
    except Exception:
        pass
    bundle = _certifi_path()
    if bundle:
        try:
            ctx.load_verify_locations(cafile=bundle)
        except Exception:
            pass
    _CONTEXT = ctx
    return ctx


def unverified_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def is_cert_error(exc) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return ("certificate_verify_failed" in text or "certificate verify failed" in text
            or ("ssl" in text and "cert" in text))
