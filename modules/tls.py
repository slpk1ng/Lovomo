"""HTTPS 证书校验上下文。

只信任 certifi 证书包的 httpx 在装了自签根证书的机器上（安全软件、网络加速器、
公司代理会把根证书装进系统证书库）连 https 站点会直接失败：
CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate。
这里把系统证书库与 certifi 证书包合并成一个上下文，兼容性与浏览器一致；
只在确实需要时才退化为不校验（由调用方显式决定）。
"""
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
    """系统证书库 + certifi 证书包合并的校验上下文（进程内复用）。"""
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
    """不校验证书的上下文（仅用于调用方明确接受风险的场景）。"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def is_cert_error(exc) -> bool:
    """异常是否是证书校验失败（用于决定是否降级重试）。"""
    text = f"{type(exc).__name__}: {exc}".lower()
    return ("certificate_verify_failed" in text or "certificate verify failed" in text
            or ("ssl" in text and "cert" in text))
