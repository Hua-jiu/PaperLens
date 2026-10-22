"""Thread-safe local persistence, with an entirely in-memory dry-run mode."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

DEFAULT_MODULES = ("summary", "translation", "chat", "reflow")
DEFAULT_SETTINGS = {
    "providers": [],
    "modules": {name: {"provider_id": "", "model": ""} for name in DEFAULT_MODULES},
}


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, data_dir: Path, dryrun: bool = False):
        self.data_dir = Path(data_dir)
        self.dryrun = dryrun
        self.lock = threading.RLock()
        self.memory_files: dict[str, bytes] = {}
        if not dryrun:
            self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            (self.data_dir / "papers").mkdir(exist_ok=True, mode=0o700)
        database = ":memory:" if dryrun else str(self.data_dir / "library.sqlite3")
        self.db = sqlite3.connect(database, check_same_thread=False)
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("CREATE TABLE IF NOT EXISTS papers (id TEXT PRIMARY KEY, digest TEXT UNIQUE, record TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id = 1), payload TEXT NOT NULL)")
        self.db.commit()
        if not dryrun:
            os.chmod(database, 0o600)

    def settings(self, public: bool = False) -> dict:
        with self.lock:
            row = self.db.execute("SELECT payload FROM settings WHERE id = 1").fetchone()
            result = json.loads(row[0]) if row else deepcopy(DEFAULT_SETTINGS)
            for provider in result["providers"]:
                provider.setdefault("default_model", "")
            result["configured"] = bool(result.get("providers"))
            if public:
                for provider in result["providers"]:
                    provider["has_key"] = bool(provider.get("api_key"))
                    provider["api_key"] = ""
            return result

    def save_settings(self, settings: dict) -> dict:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO settings(id, payload) VALUES (1, ?)", (json.dumps(settings, ensure_ascii=False),))
            self.db.commit()
            return self.settings(public=True)

    def list_papers(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT json_extract(record, '$.paper') FROM papers ORDER BY rowid DESC").fetchall()
            return [json.loads(row[0]) for row in rows]

    def get(self, paper_id: str) -> dict:
        with self.lock:
            row = self.db.execute("SELECT record FROM papers WHERE id = ?", (paper_id,)).fetchone()
            if not row:
                raise KeyError("文献不存在或已被删除")
            return json.loads(row[0])

    def update(self, paper_id: str, mutate: Callable[[dict], None]) -> dict:
        with self.lock:
            record = self.get(paper_id)
            mutate(record)
            self.db.execute("UPDATE papers SET record = ? WHERE id = ?", (json.dumps(record, ensure_ascii=False), paper_id))
            self.db.commit()
            return record

    def add(self, filename: str, data: bytes) -> tuple[dict, bool]:
        digest = hashlib.sha256(data).hexdigest()
        with self.lock:
            row = self.db.execute("SELECT record FROM papers WHERE digest = ?", (digest,)).fetchone()
            if row:
                return json.loads(row[0]), False
            paper_id = uuid4().hex
            record = {
                "paper": {
                    "id": paper_id, "filename": filename, "title": Path(filename).stem,
                    "title_zh": "", "authors": "", "abstract": "", "summary": "",
                    "status": "processing", "stage": "等待解析", "progress": 0,
                    "page_count": 0, "created_at": timestamp(), "error": "",
                    "ai_status": "pending", "ai_error": "", "translation_status": "idle", "translation_progress": 0,
                },
                "document": {"pages": [], "toc": [], "figures": [], "warnings": [], "reading_points": []},
                "highlights": [], "chat": [], "reading": {"page": 1, "font_size": 18},
            }
            if self.dryrun:
                self.memory_files[paper_id] = data
            else:
                path = self.data_dir / "papers" / f"{paper_id}.pdf"
                with path.open("xb") as stream:
                    stream.write(data)
                os.chmod(path, 0o600)
            self.db.execute("INSERT INTO papers(id, digest, record) VALUES (?, ?, ?)", (paper_id, digest, json.dumps(record, ensure_ascii=False)))
            self.db.commit()
            return deepcopy(record), True

    def source(self, paper_id: str) -> bytes:
        with self.lock:
            self.get(paper_id)
            if self.dryrun:
                return self.memory_files[paper_id]
            return (self.data_dir / "papers" / f"{paper_id}.pdf").read_bytes()

    def delete(self, paper_id: str) -> None:
        with self.lock:
            self.get(paper_id)
            if self.dryrun:
                self.memory_files.pop(paper_id, None)
            else:
                (self.data_dir / "papers" / f"{paper_id}.pdf").unlink(missing_ok=True)
            self.db.execute("DELETE FROM papers WHERE id = ?", (paper_id,))
            self.db.commit()

    def close(self) -> None:
        with self.lock:
            self.db.close()
