import json
import time
import uuid
import asyncio
import threading
import re
import os
import copy
from pathlib import Path
from typing import List, Optional

import numpy as np
import httpx

from .llm_helpers import record_token_usage
from .tls import verified_context

CHUNK_SIZE_DEFAULT = 500
OVERLAP_DEFAULT = 80


class RAGManager:
    def __init__(self, config, data_path: Path):
        self.config = config
        self.root = Path(data_path) / "rag"
        self.docs_dir = self.root / "docs"
        self.index_file = self.root / "index.json"
        self.docs_dir.mkdir(parents=True, exist_ok=True)
        self.index: List[dict] = []
        self._index_load_failed = False
        self._lock = threading.RLock()
        self.load_index()

    # ---------------- 索引管理 ----------------
    def load_index(self):
        from .jsonio import load_json_ex
        data, readable = load_json_ex(self.index_file, [])
        self._index_load_failed = not readable
        if readable and (not isinstance(data, list) or any(not isinstance(d, dict)
                or not re.fullmatch(r"[0-9a-f]{12}", str(d.get("id", ""))) for d in data)):
            self._index_load_failed = True
        self.index = data if isinstance(data, list) else []

    def save_index(self):
        if self._index_load_failed:
            raise OSError("RAG 索引无法读取，保留原文件")
        try:
            from .jsonio import save_json
            save_json(self.index_file, self.index)
        except Exception as e:
            raise OSError(f"保存 RAG 索引失败: {e}") from e

    def list_docs(self) -> list:
        with self._lock:
            return [{k: d.get(k) for k in ("id", "name", "chunks", "size", "added_at")}
                    for d in self.index]

    # ---------------- 嵌入 ----------------
    async def embed_texts(self, texts: List[str]) -> Optional[np.ndarray]:
        if not texts:
            return await self._embed_texts_once(texts)
        vecs = []
        for i in range(0, len(texts), 64):
            part = await self._embed_texts_once(texts[i:i + 64])
            if part is None:
                return None
            vecs.append(part)
        return np.concatenate(vecs, axis=0)

    async def _embed_texts_once(self, texts: List[str]) -> Optional[np.ndarray]:
        from .model_endpoints import embedding_target
        target = embedding_target(self.config)
        backend, base_url, model = target.backend, target.base_url, target.model
        api_key, embedding_url = target.api_key, target.endpoint
        if not model:
            print("RAG 嵌入跳过：未配置嵌入模型")
            return None
        try:
            if backend == "ollama":
                async with httpx.AsyncClient(timeout=120, trust_env=False,
                                             verify=verified_context()) as client:
                    errors = []
                    resp = await client.post(embedding_url if not embedding_url.endswith("/api/embeddings")
                                             else f"{base_url}/api/embed",
                                             json={"model": model, "input": texts})
                    if resp.status_code == 200:
                        body = resp.json()
                        record_token_usage("会话回忆检索", backend, body)
                        embs = body.get("embeddings")
                        if embs:
                            return np.array(embs, dtype=np.float32)
                        errors.append("/api/embed[200]: 响应缺少 embeddings 字段")
                    else:
                        errors.append(f"/api/embed[{resp.status_code}]: {_response_error(resp)}")
                    vecs = []
                    for t in texts:
                        r = await client.post(embedding_url if embedding_url.endswith("/api/embeddings")
                                              else f"{base_url}/api/embeddings",
                                              json={"model": model, "prompt": t})
                        if r.status_code != 200:
                            errors.append(f"/api/embeddings[{r.status_code}]: {_response_error(r)}")
                            vecs = []
                            break
                        vecs.append(r.json().get("embedding", []))
                    if vecs and vecs[0]:
                        return np.array(vecs, dtype=np.float32)
                    msg = "；".join(errors)
                    low = msg.lower()
                    if model.lower() in low or "try pulling" in low:
                        msg += f"（提示：嵌入模型未安装，请先执行: ollama pull {model}）"
                    elif "does not support" in low:
                        msg += "（提示：该模型不支持生成嵌入，请在 WebUI 将 rag_embedding_model 换成嵌入模型，如 nomic-embed-text 或 bge-m3）"
                    raise RuntimeError(msg)
            else:
                endpoint = embedding_url or f"{base_url}/embeddings"
                headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
                async with httpx.AsyncClient(timeout=120, trust_env=False,
                                             verify=verified_context()) as client:
                    resp = await client.post(endpoint,
                                             json={"model": model, "input": texts},
                                             headers=headers)
                    resp.raise_for_status()
                    body = resp.json()
                    record_token_usage("会话回忆检索", backend, body)
                    data = sorted(body.get("data", []), key=lambda x: x.get("index", 0))
                    vecs = [d.get("embedding") for d in data]
                    if not vecs or not vecs[0]:
                        return None
                    return np.array(vecs, dtype=np.float32)
        except Exception as e:
            target = (f"{base_url}/api/embed（或 /api/embeddings）" if backend == "ollama"
                      else (embedding_url or f"{base_url}/embeddings"))
            model_hint = ("rag_embedding_model" if backend == "ollama" else "llm_embedding_model")
            extra = ""
            if backend != "ollama" and not embedding_url:
                extra = ("；「Embedding 端点」留空时会拼在 llm_base_url 后面，"
                         "嵌入服务不在同一个地址就把它填成完整地址"
                         "（例如 http://127.0.0.1:1234/v1/embeddings）")
            print(f"RAG 嵌入失败: {type(e).__name__}: {e}\n"
                  f"（目标端点：{target}，嵌入模型：{model}。"
                  f"请确认该端点可达，且 {model_hint} 配置正确{extra}。）")
            return None

    # ---------------- 分块 ----------------
    @staticmethod
    def chunk_text(text: str, chunk_size: int, overlap: int) -> List[str]:
        text = re_sub_spaces(text)
        chunks = []
        step = max(1, chunk_size - max(0, overlap))
        for i in range(0, len(text), step):
            piece = text[i:i + chunk_size].strip()
            if len(piece) >= 20:
                chunks.append(piece)
            if i + chunk_size >= len(text):
                break
        return chunks or ([text.strip()] if text.strip() else [])

    def _ensure_index_loaded(self) -> bool:
        if not self._index_load_failed:
            return True
        self.load_index()
        return not self._index_load_failed

    # ---------------- 文档操作 ----------------
    async def add_document(self, name: str, raw_text: str) -> dict:
        if not self._ensure_index_loaded():
            return {"success": False, "error": "RAG 索引读取失败，已中止本次添加以免覆盖原有索引"}
        if len(raw_text) > MAX_DOCUMENT_CHARS:
            return {"success": False, "error": "文档文字超限，请拆分后上传"}
        chunk_size = max(100, min(10000, int(self.config.get("rag_chunk_size", CHUNK_SIZE_DEFAULT))))
        overlap = max(0, min(chunk_size - 100, int(self.config.get("rag_chunk_overlap", OVERLAP_DEFAULT))))
        chunks = self.chunk_text(raw_text, chunk_size, overlap)
        if not chunks:
            return {"success": False, "error": "文档内容为空"}
        if len(chunks) > MAX_DOCUMENT_CHUNKS:
            return {"success": False, "error": "文档分块过多，请拆分后上传"}
        vectors = await self.embed_texts(chunks)
        if vectors is None or len(vectors) != len(chunks):
            return {"success": False, "error": "向量化失败，请检查嵌入模型是否可用"}
        doc_id = uuid.uuid4().hex[:12]
        norm = _normalize(vectors)
        entry = {"id": doc_id, "name": name, "chunks": len(chunks),
                 "size": len(raw_text), "added_at": time.time()}
        await asyncio.to_thread(self._commit_document, entry, norm, chunks)
        return {"success": True, "doc": entry}

    def _commit_document(self, entry, norm, chunks):
        from .jsonio import file_lock, save_json
        with self._lock, file_lock(self.index_file):
            self.load_index()
            if self._index_load_failed:
                raise OSError("RAG 索引无法读取，保留原文件")
            doc_id = entry["id"]
            array = self.docs_dir / f"{doc_id}.npy"
            metadata = self.docs_dir / f"{doc_id}.json"
            temporary = self.docs_dir / f".{doc_id}.{uuid.uuid4().hex}.tmp"
            try:
                with temporary.open("wb") as stream:
                    np.save(stream, norm, allow_pickle=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, array)
                save_json(metadata, {"name": entry["name"], "chunks": chunks})
                updated = [*self.index, entry]
                save_json(self.index_file, updated)
                self.index = updated
            except BaseException:
                for path in (temporary, array, metadata):
                    path.unlink(missing_ok=True)
                raise

    def delete_document(self, doc_id: str) -> bool:
        if not re.fullmatch(r"[0-9a-f]{12}", str(doc_id or "")):
            return False
        from .jsonio import file_lock, save_json
        with self._lock, file_lock(self.index_file):
            self.load_index()
            if self._index_load_failed:
                return False
            updated = [d for d in self.index if d.get("id") != doc_id]
            if len(updated) == len(self.index):
                return False
            save_json(self.index_file, updated)
            self.index = updated
        for suffix in (".npy", ".json"):
            try:
                (self.docs_dir / f"{doc_id}{suffix}").unlink(missing_ok=True)
            except Exception:
                pass
        return True

    # ---------------- 检索 ----------------
    async def search(self, query: str, top_k: int = None, min_sim: float = None,
                     verbose: bool = False) -> List[dict]:
        if not self.index:
            return []
        top_k = int(top_k or self.config.get("rag_top_k", 3))
        min_sim = float(min_sim if min_sim is not None else self.config.get("rag_min_similarity", 0.35))
        qvec = await self.embed_texts([query])
        if qvec is None:
            return []
        q = _normalize(qvec)[0]
        return await asyncio.to_thread(self._search_vectors, q, top_k, min_sim, verbose)

    def _search_vectors(self, q, top_k, min_sim, verbose):
        hits = []
        best_sim = 0.0
        with self._lock:
            documents = copy.deepcopy(self.index)
        for doc in documents:
            if not re.fullmatch(r"[0-9a-f]{12}", str(doc.get("id", ""))):
                continue
            try:
                mat = np.load(self.docs_dir / f"{doc['id']}.npy")
                chunk_data = json.loads((self.docs_dir / f"{doc['id']}.json").read_text(encoding="utf-8"))
                sims = mat @ q
            except Exception as e:
                print(f"读取 RAG 文档失败 {doc.get('name')}: {e}")
                continue
            chunks = chunk_data.get("chunks") if isinstance(chunk_data, dict) else None
            if not isinstance(chunks, list):
                chunks = []
            order = np.argsort(-sims)[:top_k]
            for idx in order:
                sim = float(sims[idx])
                best_sim = max(best_sim, sim)
                if sim >= min_sim:
                    text = chunks[int(idx)] if int(idx) < len(chunks) else ""
                    hits.append({"doc": doc.get("name", ""), "sim": round(sim, 3), "text": text})
        if not hits and best_sim > 0 and verbose:
            print(f"RAG 检索：没有片段达到相似度下限 {min_sim}（最接近的一段 {best_sim:.3f}）。"
                  f"查询越短越泛分数越低，想更宽松可调小 rag_min_similarity。")
        hits.sort(key=lambda h: -h["sim"])
        return hits[:top_k]

    async def build_context(self, query: str) -> str:
        if not self.config.get("rag_enabled", False) or not self.index:
            return ""
        hits = await self.search(query)
        if not hits:
            return ""
        max_chars = int(self.config.get("rag_max_context_chars", 1000))
        lines, used = [], 0
        for h in hits:
            piece = f"- ({h['doc']} 相关度{h['sim']}) {h['text']}"
            if used + len(piece) > max_chars:
                break
            lines.append(piece)
            used += len(piece)
        if not lines:
            return ""
        template = str(self.config.get("rag_context_template", "") or
                       "【参考资料】以下是知识库中可能相关的内容，回答时可以参考（不确定时以你的角色身份自然回答）：\n{refs}")
        return template.replace("{refs}", "\n".join(lines))


def _normalize(mat: np.ndarray) -> np.ndarray:
    mat = np.asarray(mat, dtype=np.float32)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms


def _response_error(resp) -> str:
    try:
        data = resp.json()
        if isinstance(data, dict) and data.get("error"):
            return str(data["error"])
    except Exception:
        pass
    text = (resp.text or "").strip()
    return text[:200] or f"HTTP {resp.status_code}"


def re_sub_spaces(text: str) -> str:
    import re
    return re.sub(r"[ \t\r]+", " ", text).strip()


MAX_DOCUMENT_CHARS = 1_000_000
MAX_DOCUMENT_CHUNKS = 4096


def extract_text_from_file(path: Path) -> str:
    if path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("文档超过 20MB 上限")
    if path.suffix.lower() == ".pdf":
        try:
            from pypdf import PdfReader, filters
            from pypdf.generic import StreamObject
        except ImportError:
            raise RuntimeError("解析 PDF 需要安装 pypdf：pip install pypdf")
        # Bound decompression before parsing any potentially compressed objects.
        for name in ("ZLIB_MAX_OUTPUT_LENGTH", "LZW_MAX_OUTPUT_LENGTH",
                     "MAX_ARRAY_BASED_STREAM_OUTPUT_LENGTH", "RUN_LENGTH_MAX_OUTPUT_LENGTH"):
            if not hasattr(filters, name):
                raise RuntimeError("pypdf 版本缺少解压限额，请安装项目锁定版本")
            setattr(filters, name, min(getattr(filters, name), 2 * 1024 * 1024))
        reader = PdfReader(str(path))
        if len(reader.pages) > 100 or sum(len(table) for table in reader.xref.values()) > 5000:
            raise ValueError("PDF 页数或对象数量超限，请拆分后上传")
        decoded, streams = 0, 0
        for generation, table in reader.xref.items():
            for number in table:
                if number == 0:
                    continue
                from pypdf.generic import IndirectObject
                obj = reader.get_object(IndirectObject(number, generation, reader))
                if isinstance(obj, StreamObject):
                    streams += 1
                    if streams > 128:
                        raise ValueError("PDF 数据流数量超限，请拆分后上传")
                    decoded += len(obj.get_data())
                    if decoded > 32 * 1024 * 1024:
                        raise ValueError("PDF 解压后数据超过 32MB，请拆分后上传")
        pages, total = [], 0
        for page in reader.pages:
            text = page.extract_text() or ""
            total += len(text)
            if total > MAX_DOCUMENT_CHARS:
                raise ValueError("文档文字超过 100 万字符，请拆分后上传")
            pages.append(text)
        return "\n".join(pages)
    with path.open(encoding="utf-8", errors="replace") as stream:
        text = stream.read(MAX_DOCUMENT_CHARS + 1)
    if len(text) > MAX_DOCUMENT_CHARS:
        raise ValueError("文档文字超过 100 万字符，请拆分后上传")
    return text
