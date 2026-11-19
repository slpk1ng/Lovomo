"""Shared provider and endpoint resolution for model loading and retrieval."""
import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit


@dataclass(frozen=True)
class EmbeddingTarget:
    backend: str
    model: str
    base_url: str
    endpoint: str
    api_key: str


def _http_origin(url):
    try:
        parsed = urlsplit(str(url or "").strip())
        if parsed.scheme in ("http", "https") and parsed.hostname:
            return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        pass
    return None


def service_api_key(config, field, endpoint):
    explicit = str(config.get(field, "") or "").strip()
    if explicit:
        return explicit
    origin = _http_origin(endpoint)
    if origin is not None and origin == _http_origin(config.get("llm_base_url")):
        return str(config.get("llm_api_key", "") or "").strip()
    return ""


def embedding_target(config):
    backend = str(config.get("rag_embedding_backend", "") or config.get("llm_backend", "ollama")).lower()
    explicit = str(config.get("llm_embedding_url", "") or "").strip().rstrip("/")
    base = str(config.get("llm_base_url", "") or "").strip().rstrip("/")
    # A separate Ollama provider must not inherit an OpenAI chat endpoint.
    if not explicit and backend == "ollama" and config.get("llm_backend", "ollama") != "ollama":
        base = ""
    base = explicit or base or ("http://127.0.0.1:11434" if backend == "ollama" else "")
    parsed = urlsplit(base)
    path = parsed.path.rstrip("/")
    endings = ("/api/embed", "/api/embeddings") if backend == "ollama" else ("/embeddings",)
    matched = next((suffix for suffix in endings if path.lower().endswith(suffix)), "")
    endpoint = base if matched else base + ("/api/embed" if backend == "ollama" else "/embeddings")
    if matched:
        base = urlunsplit((parsed.scheme, parsed.netloc, path[:-len(matched)], "", ""))
    model = str(config.get("llm_embedding_model", "") or config.get("rag_embedding_model", "")
                or ("nomic-embed-text" if backend == "ollama" else config.get("llm_model_name", "")))
    key = service_api_key(config, "rag_embedding_api_key", endpoint)
    return EmbeddingTarget(backend, model, base.rstrip("/"), endpoint, key)


_VERSION_SEG_RE = re.compile(r"/v\d+(?:\.\d+)*$")
_CHAT_ENDPOINT_SUFFIXES = ("/chat/completions", "/completions", "/api/chat",
                           "/api/generate")
_MODEL_LIST_ENDPOINT_SUFFIXES = ("/models", "/api/tags")


def _ends_with_any(base: str, suffixes) -> bool:
    low = base.lower()
    return any(low.endswith(suffix) for suffix in suffixes)


def _strip_endpoint_suffix(base: str, suffixes) -> str:
    low = base.lower()
    for suffix in suffixes:
        if low.endswith(suffix):
            return base[: -len(suffix)]
    return base


def chat_endpoint(base_url: str, backend: str = "openai") -> str:
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        return ""
    if _ends_with_any(base, _CHAT_ENDPOINT_SUFFIXES):
        return base
    base = _strip_endpoint_suffix(base, _MODEL_LIST_ENDPOINT_SUFFIXES)
    if backend == "ollama":
        return f"{base}/api/chat"
    try:
        parsed = urlsplit(base)
        # Accept the default LM Studio server address copied without its API prefix.
        if (not parsed.path and not parsed.query and not parsed.fragment
                and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
                and parsed.port == 1234):
            base += "/v1"
    except ValueError:
        pass
    return f"{base}/chat/completions"


def model_list_endpoints(base_url: str, backend: str = "openai") -> list[str]:
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        return []
    if _ends_with_any(base, _MODEL_LIST_ENDPOINT_SUFFIXES):
        return [base]
    stripped = _strip_endpoint_suffix(base, _CHAT_ENDPOINT_SUFFIXES)
    if stripped != base:
        base = stripped
    if backend == "ollama":
        return [f"{base}/api/tags"]
    if _VERSION_SEG_RE.search(base):
        return [f"{base}/models"]
    return [f"{base}/v1/models", f"{base}/models"]


def looks_like_full_endpoint(base_url: str) -> bool:
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        return False
    base = _strip_endpoint_suffix(base, _CHAT_ENDPOINT_SUFFIXES
                                  + _MODEL_LIST_ENDPOINT_SUFFIXES)
    try:
        path = urlsplit(base).path
    except ValueError:
        return False
    segments = [seg for seg in path.split("/") if seg]
    if segments and _VERSION_SEG_RE.search("/" + segments[-1]):
        segments = segments[:-1]
    return len(segments) >= 2
