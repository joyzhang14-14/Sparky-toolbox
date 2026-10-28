"""有界、任务隔离的临时证据存储（规格 3、6.8）。

每个任务一个目录（宿主 bind mount 进容器的 /artifacts）；保存原始内容、解析产物、页图与元数据。
研究模型只拿 artifact ID，不接触路径。
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Artifact:
    artifact_id: str
    kind: str  # page / document / render / pageimage / parsed
    url: str | None
    final_url: str | None
    content_type: str | None
    content_hash: str
    size: int
    fetched_at: str
    title: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def path_name(self) -> str:
        return self.artifact_id


class EvidenceStore:
    def __init__(self, root: Path, *, max_bytes: int = 2 * 1024**3) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._max = max_bytes
        self._index: dict[str, Artifact] = {}
        self._load_index()

    # ---------- 写 ----------
    def put(self, kind: str, content: bytes, *, url: str | None, final_url: str | None = None, content_type: str | None = None,
            title: str | None = None, fetched_at: str | None = None, meta: dict[str, Any] | None = None, ext: str = "bin") -> Artifact:
        digest = hashlib.sha256(content).hexdigest()
        art_id = f"{kind[:4]}_{digest[:12]}_{secrets.token_hex(2)}"
        if self.total_size() + len(content) > self._max:
            raise RuntimeError(f"证据存储超过容量上限 {self._max} 字节")
        path = self.root / f"{art_id}.{ext}"
        path.write_bytes(content)
        art = Artifact(
            artifact_id=art_id, kind=kind, url=url, final_url=final_url, content_type=content_type,
            content_hash="sha256:" + digest, size=len(content), title=title,
            fetched_at=fetched_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), meta=meta or {},
        )
        art.meta["_file"] = path.name
        self._index[art_id] = art
        self._save_index()
        return art

    def put_json(self, kind: str, obj: Any, *, parent: str | None, meta: dict[str, Any] | None = None) -> Artifact:
        data = json.dumps(obj, ensure_ascii=False).encode()
        m = dict(meta or {})
        if parent:
            m["parent"] = parent
        p = self._index.get(parent) if parent else None
        return self.put(kind, data, url=p.url if p else None, final_url=p.final_url if p else None, content_type="application/json", meta=m, ext="json")

    # ---------- 读 ----------
    def get(self, artifact_id: str) -> Artifact | None:
        return self._index.get(artifact_id)

    def read_bytes(self, artifact_id: str) -> bytes:
        art = self._require(artifact_id)
        return (self.root / art.meta["_file"]).read_bytes()

    def read_json(self, artifact_id: str) -> Any:
        return json.loads(self.read_bytes(artifact_id))

    def path(self, artifact_id: str) -> Path:
        return self.root / self._require(artifact_id).meta["_file"]

    def children(self, parent_id: str, kind: str | None = None) -> list[Artifact]:
        return [a for a in self._index.values() if a.meta.get("parent") == parent_id and (kind is None or a.kind == kind)]

    def all(self) -> list[Artifact]:
        return list(self._index.values())

    def total_size(self) -> int:
        return sum(a.size for a in self._index.values() if not a.meta.get("_file", "").startswith("prev/"))

    def _require(self, artifact_id: str) -> Artifact:
        art = self._index.get(artifact_id)
        if art is None:
            raise KeyError(f"未知 artifact: {artifact_id}")
        return art

    # ---------- 索引持久化（容器销毁后主 Agent 可经 evidence_request 二次补取） ----------
    def _load_index(self) -> None:
        p = self.root / "index.json"
        if p.exists():
            for k, v in json.loads(p.read_text()).items():
                self._index[k] = Artifact(**v)
        # 上次任务的证据目录以只读方式挂在 prev/<task_id>/ 下（evidence_request 二次补取）
        prev = self.root / "prev"
        if prev.is_dir():
            for sub in prev.iterdir():
                idx = sub / "index.json"
                if not idx.is_file():
                    continue
                for k, v in json.loads(idx.read_text()).items():
                    if k in self._index:
                        continue
                    art = Artifact(**v)
                    art.meta["_file"] = f"prev/{sub.name}/{art.meta['_file']}"
                    self._index[k] = art

    def _save_index(self) -> None:  # 只保存本任务产生的 artifact
        tmp = self.root / "index.json.tmp"
        own = {k: asdict(v) for k, v in self._index.items() if not v.meta.get("_file", "").startswith("prev/")}
        tmp.write_text(json.dumps(own, ensure_ascii=False))
        os.replace(tmp, self.root / "index.json")
