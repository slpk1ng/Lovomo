"""Versioned portable snapshots, restored before runtime managers start."""
import hashlib
import io
import json
import os
import re
import tempfile
import time
import zipfile
from pathlib import Path

from .jsonio import file_lock
from .memory_store import _is_memory_filename
from .security import _SECRET_FIELD_KEYS, _CONNECTION_SECRET_KEYS, _EMBEDDED_SECRET_RE

MAX_ARCHIVE = 20 * 1024**2
MAX_EXPANDED = 128 * 1024**2
MAX_MANIFEST = 1024**2
MAX_ENTRIES = 4096
PENDING = ".lovomo-restore.zip"
JOURNAL = ".lovomo-restore-journal"
DATA_FILES = {"model_library.json", "learned_terms.json", "moods.json", "mood_diary.json",
              "affection.json", "promises.json", "open_topics.json", "adventures.json",
              "user_profiles.json"}
LOCAL_CONFIG = {"connections", "memory_data_path", "auto_start_tts", "tts_start_script",
                "asr_auto_start", "asr_start_script", "llm_start_script", "ref_audio_root",
                "emotion_mimic_root", "model_dir", "asr_local_root",
                "tts_gpt_weights", "tts_sovits_weights", "startup_gpu_device",
                "llm_auto_load_local", "plugins_enabled"}
SECRET_FIELDS = set(_SECRET_FIELD_KEYS + _CONNECTION_SECRET_KEYS) | {
    "api_key", "password", "context_tokens", "context_token", "authorization", "client_secret"}
LOCAL_ROLE_PATHS = ("ref_audio_root", "emotion_mimic_root")


# Credentials retained from this machine must stay paired with their service.
CREDENTIAL_ENDPOINTS = {
    "llm_api_key": ("llm_backend", "llm_base_url", "chat_provider"),
    "image_caption_api_key": ("image_caption_backend", "image_caption_base_url",
                              "caption_provider", "llm_base_url", "chat_provider"),
    "rag_embedding_api_key": ("rag_embedding_backend", "llm_embedding_url",
                               "embedding_provider", "llm_backend", "llm_base_url", "chat_provider"),
    "cloud_tts_api_key": ("cloud_tts_protocol", "cloud_tts_base_url", "cloud_tts_provider"),
    "asr_api_key": ("asr_api_style", "asr_base_url", "asr_provider", "llm_base_url", "chat_provider"),
    "napcat_token": ("napcat_ws_url",),
}


def _portable(value, *, config=False):
    if isinstance(value, dict):
        return {key: _portable(item, config=config) for key, item in value.items()
                if key.lower() not in SECRET_FIELDS
                and not key.lower().endswith(("_api_key", "_secret", "_token", "_password"))
                and not (config and (key in LOCAL_CONFIG or key.startswith("webui_")
                                     or key.endswith("_start_script")))}
    if isinstance(value, list):
        return [_portable(item, config=config) for item in value]
    if isinstance(value, str):
        if value.startswith(("enc:", "enc2:", "enc3:")):
            return ""
        if "://" in value and re.search(r"://[^/\s]+@", value):
            return ""
        return _EMBEDDED_SECRET_RE.sub("[已隐藏]", value)
    return value


def _allowed(name):
    if name == "config.json":
        return True
    parts = name.split("/")
    if not parts or parts[0] != "data" or len(name) > 512:
        return False
    for part in parts:
        if (not part or part.endswith((".", " ")) or re.search(r'[\\:<>"|?*\x00-\x1f]', part)
                or re.fullmatch(r"(?i:con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³])(?:\..*)?", part)):
            return False
    if len(parts) == 2:
        return parts[1] in DATA_FILES or _is_memory_filename(parts[1])
    if len(parts) == 3:
        return ((parts[1] == "config_presets" and parts[2].endswith(".json"))
                or (parts[1] == "learned_terms" and re.fullmatch(r"[a-f0-9]{64}\.json", parts[2]))
                or parts[1:] == ["rag", "index.json"])
    return (len(parts) == 4 and parts[1:3] == ["rag", "docs"]
            and re.fullmatch(r"[a-f0-9]{12}\.(json|npy)", parts[3]))


def _target(name, root, config_path):
    if not _allowed(name):
        raise ValueError("备份文件包含不支持的路径")
    if name == "config.json":
        config_path = Path(config_path)
        if config_path.is_symlink():
            raise ValueError("配置文件是链接，无法安全恢复")
        return config_path
    root = Path(root).resolve()
    target = root.joinpath(*name.split("/")[1:])
    for path in (target, *target.parents):
        if path == root:
            break
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise ValueError("备份数据目录中存在链接")
    target.resolve().relative_to(root)
    return target


def _json_bytes(item):
    return json.dumps(item, ensure_ascii=False, allow_nan=False).encode("utf-8")


def _candidates(root):
    # Scan only data owned by these managers. Assets and runtime credentials stay local.
    for pattern in ("*.json", "config_presets/*.json", "learned_terms/*.json",
                    "rag/index.json", "rag/docs/*.json", "rag/docs/*.npy"):
        yield from root.glob(pattern)


def create_archive(root, config):
    root = Path(root)
    with file_lock(root / "rag" / "index.json"):
        entries = _collect_entries(root, config)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        manifest = {"format": 1, "files": {name: hashlib.sha256(raw).hexdigest()
                                            for name, raw in entries.items()}}
        archive.writestr("manifest.json", _json_bytes(manifest))
        for name, raw in entries.items():
            archive.writestr(name, raw)
    if len(_json_bytes(manifest)) > MAX_MANIFEST or output.tell() > MAX_ARCHIVE:
        raise ValueError("压缩包超过 20 MB，请分别导出记忆与配置")
    return output.getvalue()


def _collect_entries(root, config):
    entries = {"config.json": _json_bytes(_portable(config, config=True))}
    total = len(entries["config.json"])
    for file in _candidates(root):
        name = "data/" + file.relative_to(root).as_posix()
        if not _allowed(name) or not file.is_file():
            continue
        file = _target(name, root, None)
        if len(entries) >= MAX_ENTRIES or file.stat().st_size + total > MAX_EXPANDED:
            raise ValueError("数据超过单个备份包上限，请分别导出记忆与配置")
        raw = file.read_bytes()
        if name.startswith("data/config_presets/") or name == "data/model_library.json":
            raw = _json_bytes(_portable(json.loads(raw), config=True))
        total += len(raw)
        if total > MAX_EXPANDED:
            raise ValueError("备份原始数据超过 128 MB")
        entries[name] = raw
    _validate_entries(entries)
    return entries


def read_archive(raw):
    if not raw or len(raw) > MAX_ARCHIVE:
        raise ValueError("备份包为空或超过 20 MB")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = archive.infolist()
        names = [member.filename for member in members]
        if (len(members) > MAX_ENTRIES + 1 or len({name.casefold() for name in names}) != len(names)
                or "manifest.json" not in names
                or sum(member.file_size for member in members) > MAX_EXPANDED + MAX_MANIFEST
                or any(member.is_dir() for member in members)):
            raise ValueError("备份包的文件数量、大小或结构不正确")
        if archive.getinfo("manifest.json").file_size > MAX_MANIFEST:
            raise ValueError("备份清单过大")
        manifest = json.loads(archive.read("manifest.json"))
        if not isinstance(manifest, dict) or manifest.get("format") != 1:
            raise ValueError("备份版本不受支持")
        hashes = manifest.get("files")
        if not isinstance(hashes, dict) or set(hashes) != set(names) - {"manifest.json"}:
            raise ValueError("备份清单与实际文件不一致")
        if not hashes:
            raise ValueError("备份包未包含数据")
        data = {}
        total = 0
        for name, digest in hashes.items():
            if not _allowed(name):
                raise ValueError("备份中包含不支持的文件")
            total += archive.getinfo(name).file_size
            if total > MAX_EXPANDED:
                raise ValueError("备份原始数据超过 128 MB")
            item = archive.read(name)
            if hashlib.sha256(item).hexdigest() != digest:
                raise ValueError("备份校验失败")
            data[name] = item
        _validate_entries(data)
        return data


def _validate_entries(entries):
    for name, raw in entries.items():
        if name.endswith(".json"):
            value = json.loads(raw)
            if not isinstance(value, (dict, list)) or (name in {"config.json", "data/model_library.json"} and not isinstance(value, dict)):
                raise ValueError("备份数据格式错误")
            if name == "data/model_library.json" and any(
                    not isinstance(value.get(key, []), list)
                    or any(not isinstance(entry, dict) for entry in value.get(key, []))
                    for key in ("roles", "providers")):
                raise ValueError("备份模型库格式错误")
        else:
            from numpy.lib import format as np_format
            from math import prod
            stream = io.BytesIO(raw)
            try:
                version = np_format.read_magic(stream)
                header = {(1, 0): np_format.read_array_header_1_0,
                          (2, 0): np_format.read_array_header_2_0}[version]
                shape, _, dtype = header(stream)
            except (ValueError, KeyError, EOFError) as error:
                raise ValueError("知识库向量格式错误") from error
            if len(shape) != 2 or dtype.kind != "f" or prod(shape) * dtype.itemsize != len(raw) - stream.tell():
                raise ValueError("知识库向量不完整或格式错误")
    index = entries.get("data/rag/index.json")
    if index is None and any(name.startswith("data/rag/") for name in entries):
        raise ValueError("备份缺少知识库索引")
    if index is not None:
        documents = json.loads(index)
        if not isinstance(documents, list):
            raise ValueError("知识库索引格式错误")
        for document in documents:
            if not isinstance(document, dict):
                raise ValueError("知识库索引格式错误")
            doc_id = str(document.get("id") or "")
            if not re.fullmatch(r"[a-f0-9]{12}", doc_id) or any(
                f"data/rag/docs/{doc_id}.{extension}" not in entries for extension in ("json", "npy")
            ):
                raise ValueError("备份缺少知识库文档")


def preview(raw, root, config_path):
    entries = read_archive(raw)
    files = [{"name": name, "bytes": len(value), "exists": _target(name, root, config_path).exists()}
             for name, value in sorted(entries.items())]
    return {"count": len(files), "replace_count": sum(item["exists"] for item in files),
            "files": files, "sha256": hashlib.sha256(raw).hexdigest()}


def _replace(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".lovomo-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def stage(raw, digest, root, config_path):
    report = preview(raw, root, config_path)
    if report["sha256"] != digest:
        raise ValueError("恢复包已变化，请重新预览")
    root = Path(root)
    if (root / JOURNAL).exists():
        raise ValueError("已有恢复事务待重启处理")
    _replace(root / PENDING, raw)
    return report


def pending_status(root):
    return (Path(root) / PENDING).is_file()


def cancel_pending(root):
    root = Path(root)
    if (root / JOURNAL).exists():
        raise ValueError("已有恢复事务待重启处理")
    (root / PENDING).unlink(missing_ok=True)


def _retain_role_paths(restored, current, *, identity):
    if not isinstance(restored, list) or not isinstance(current, list):
        return
    def key(role):
        return tuple(str(role.get(field) or "") for field in identity)
    existing = {key(role): role for role in current if isinstance(role, dict) and all(key(role))}
    for role in restored:
        if isinstance(role, dict) and key(role) in existing:
            local = existing[key(role)]
            role.update({path: local[path] for path in LOCAL_ROLE_PATHS if path in local})


def _prepare_entry(name, raw, target):
    if name == "config.json":
        current = json.loads(target.read_bytes()) if target.exists() else {}
        incoming = _portable(json.loads(raw), config=True)
        _retain_role_paths(incoming.get("roles"), current.get("roles"), identity=("character_key",))
        restored = {**current, **incoming}
        for key, selectors in CREDENTIAL_ENDPOINTS.items():
            if any(restored.get(field) != current.get(field) for field in selectors):
                restored.pop(key, None)
        return _json_bytes(restored)
    if name.startswith("data/config_presets/"):
        return _json_bytes(_portable(json.loads(raw), config=True))
    if name == "data/model_library.json":
        restored = _portable(json.loads(raw), config=True)
        current = json.loads(target.read_bytes()) if target.exists() else {}
        _retain_role_paths(restored.get("roles"), current.get("roles"), identity=("id", "character_key"))
        def provider_key(provider):
            return tuple(str(provider.get(field) or "") for field in
                         ("id", "kind", "backend", "base_url", "protocol", "api_style"))
        local_keys = {provider_key(p): p["api_key"] for p in current.get("providers", [])
                      if isinstance(p, dict) and p.get("api_key")}
        for provider in restored.get("providers", []):
            if isinstance(provider, dict) and provider_key(provider) in local_keys:
                provider["api_key"] = local_keys[provider_key(provider)]
        return _json_bytes(restored)
    return raw


def _recover(journal, root, config_path):
    records = json.loads((journal / "files.json").read_bytes())
    for index, (name, existed) in reversed(list(enumerate(records))):
        target = _target(name, root, config_path)
        if existed:
            _replace(target, (journal / str(index)).read_bytes())
        else:
            target.unlink(missing_ok=True)


def _retain_journal(journal):
    os.replace(journal, journal.with_name(f".lovomo-before-restore-{time.time_ns()}"))


def apply_pending(root, config_path):
    root = Path(root)
    pending = root / PENDING
    journal = root / JOURNAL
    if journal.is_dir():
        if pending.is_file():
            _recover(journal, root, config_path)
        _retain_journal(journal)
    if not pending.is_file():
        return 0
    if pending.stat().st_size > MAX_ARCHIVE:
        raise ValueError("待恢复备份包过大")
    data = read_archive(pending.read_bytes())
    changes = []
    with tempfile.TemporaryDirectory(dir=root, prefix=".lovomo-prepare-restore-") as temporary:
        snapshot = Path(temporary) / "snapshot"
        snapshot.mkdir()
        records = []
        for name, raw in data.items():
            target = _target(name, root, config_path)
            raw = _prepare_entry(name, raw, target)
            existed = target.exists()
            if existed:
                _replace(snapshot / str(len(records)), target.read_bytes())
            records.append([name, existed])
            changes.append((target, raw))
        _replace(snapshot / "files.json", _json_bytes(records))
        os.replace(snapshot, journal)
    try:
        for target, raw in changes:
            _replace(target, raw)
        pending.unlink()
    except Exception:
        _recover(journal, root, config_path)
        _retain_journal(journal)
        raise
    _retain_journal(journal)
    return len(changes)


def restore_on_start(root, config_path):
    try:
        return apply_pending(root, config_path)
    except (OSError, ValueError, zipfile.BadZipFile, TypeError, RuntimeError) as error:
        # A retained active journal means rollback did not finish. Prevent the app
        # from writing additional data to those files until recovery succeeds.
        root = Path(root)
        if (root / JOURNAL).exists():
            raise
        pending = root / PENDING
        if pending.is_file():
            os.replace(pending, root / f".lovomo-restore-failed-{time.time_ns()}.zip")
        print(f"[备份] 已保留原数据，无法应用恢复包：{type(error).__name__}")
        return 0
