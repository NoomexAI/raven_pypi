"""SQLite storage for a knowledge database's files and sections."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ..core.errors import ErrorCode, RavenError

SCHEMA_VERSION = 1


class KnowledgeFileStore:
    """Persist one knowledge database's file and section catalog."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()


    def open(self) -> None:
        """Open the database and initialize its schema."""
        with self._lock:
            if self._connection is not None:
                return

            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                self.path,
                timeout=5.0,
                check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row

            try:
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = FULL")
                connection.execute("PRAGMA busy_timeout = 5000")

                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version not in {0, SCHEMA_VERSION}:
                    raise RavenError(
                        ErrorCode.UNSUPPORTED_METADATA_VERSION,
                        f"Unsupported file database version for '{self.path.parent.name}'.",
                        details={"schema_version": version},
                    )

                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS files (
                        file_id TEXT PRIMARY KEY,
                        file_name TEXT NOT NULL UNIQUE,
                        section_count INTEGER NOT NULL,
                        chunk_count INTEGER NOT NULL,
                        ingested_at TEXT NOT NULL,
                        navigation_type TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS sections (
                        section_id TEXT PRIMARY KEY,
                        file_id TEXT NOT NULL,
                        section_index INTEGER NOT NULL,
                        summary TEXT NOT NULL,
                        keywords TEXT NOT NULL,
                        conditions TEXT NOT NULL,
                        definitions TEXT NOT NULL,
                        raw_content TEXT NOT NULL,
                        source_element_ids TEXT NOT NULL,
                        source_range TEXT,
                        FOREIGN KEY (file_id) REFERENCES files(file_id) ON DELETE CASCADE,
                        UNIQUE (file_id, section_index)
                    );

                    CREATE INDEX IF NOT EXISTS idx_sections_file_id
                    ON sections(file_id, section_index);
                    """
                )
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                connection.commit()
            except Exception:
                connection.close()
                raise

            self._connection = connection


    def close(self) -> None:
        """Checkpoint pending WAL pages and close the database."""
        with self._lock:
            connection = self._connection
            if connection is None:
                return

            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.close()
            self._connection = None


    def file_exists(self, file_name: str) -> bool:
        with self._lock:
            row = self._require_connection().execute(
                "SELECT 1 FROM files WHERE file_name = ? LIMIT 1",
                (file_name,),
            ).fetchone()
        return row is not None


    def list_files(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._require_connection().execute(
                """
                SELECT file_id, file_name, section_count, chunk_count,
                       ingested_at, navigation_type
                FROM files
                ORDER BY rowid
                """
            ).fetchall()
        return [self._file_from_row(row) for row in rows]


    def get_file(self, file_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._require_connection().execute(
                """
                SELECT file_id, file_name, section_count, chunk_count,
                       ingested_at, navigation_type
                FROM files
                WHERE file_id = ?
                """,
                (file_id,),
            ).fetchone()
        return None if row is None else self._file_from_row(row)


    def list_sections(self, file_name: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._require_connection().execute(
                """
                SELECT s.*, f.file_name
                FROM sections AS s
                JOIN files AS f ON f.file_id = s.file_id
                WHERE f.file_name = ?
                ORDER BY s.section_index
                """,
                (file_name,),
            ).fetchall()
        return [self._section_from_row(row) for row in rows]


    def get_section(self, section_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._require_connection().execute(
                """
                SELECT s.*, f.file_name
                FROM sections AS s
                JOIN files AS f ON f.file_id = s.file_id
                WHERE s.section_id = ?
                """,
                (section_id,),
            ).fetchone()
        return None if row is None else self._section_from_row(row)


    def add_file(
        self,
        file: dict[str, Any],
        sections: Sequence[dict[str, Any]],
    ) -> None:
        """Atomically insert one file and all of its sections."""
        with self._lock:
            connection = self._require_connection()
            try:
                with connection:
                    connection.execute(
                        """
                        INSERT INTO files (
                            file_id, file_name, section_count, chunk_count,
                            ingested_at, navigation_type
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            file["file_id"],
                            file["file_name"],
                            file["section_count"],
                            file["chunk_count"],
                            file["ingested_at"],
                            file["navigation_type"],
                        ),
                    )
                    connection.executemany(
                        """
                        INSERT INTO sections (
                            section_id, file_id, section_index, summary,
                            keywords, conditions, definitions, raw_content,
                            source_element_ids, source_range
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [self._section_values(section) for section in sections],
                    )
            except sqlite3.IntegrityError as exc:
                raise RavenError(
                    ErrorCode.FILE_ALREADY_EXISTS,
                    f"File '{file['file_name']}' already exists.",
                ) from exc


    def remove_file(self, file_id: str) -> dict[str, Any] | None:
        """Atomically remove a file and return data that can restore it."""
        with self._lock:
            connection = self._require_connection()
            file_row = connection.execute(
                "SELECT * FROM files WHERE file_id = ?",
                (file_id,),
            ).fetchone()
            if file_row is None:
                return None

            section_rows = connection.execute(
                "SELECT * FROM sections WHERE file_id = ? ORDER BY section_index",
                (file_id,),
            ).fetchall()
            bundle = {
                "file": dict(file_row),
                "sections": [dict(row) for row in section_rows],
            }
            with connection:
                connection.execute("DELETE FROM files WHERE file_id = ?", (file_id,))
            return bundle


    def restore_file(self, bundle: dict[str, Any]) -> None:
        """Restore a bundle returned by :meth:`remove_file`."""
        file = bundle["file"]
        sections = bundle["sections"]
        with self._lock:
            connection = self._require_connection()
            with connection:
                connection.execute(
                    """
                    INSERT INTO files (
                        file_id, file_name, section_count, chunk_count,
                        ingested_at, navigation_type
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        file["file_id"],
                        file["file_name"],
                        file["section_count"],
                        file["chunk_count"],
                        file["ingested_at"],
                        file["navigation_type"],
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO sections (
                        section_id, file_id, section_index, summary,
                        keywords, conditions, definitions, raw_content,
                        source_element_ids, source_range
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            section["section_id"],
                            section["file_id"],
                            section["section_index"],
                            section["summary"],
                            section["keywords"],
                            section["conditions"],
                            section["definitions"],
                            section["raw_content"],
                            section["source_element_ids"],
                            section["source_range"],
                        )
                        for section in sections
                    ],
                )


    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RavenError(
                ErrorCode.KNOWLEDGE_NOT_STARTED,
                f"File database '{self.path}' is not open.",
            )
        return self._connection


    @staticmethod
    def _file_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "file_id": str(row["file_id"]),
            "file_name": str(row["file_name"]),
            "section_count": int(row["section_count"]),
            "chunk_count": int(row["chunk_count"]),
            "ingested_at": str(row["ingested_at"]),
            "navigation_type": str(row["navigation_type"]),
        }


    @staticmethod
    def _section_values(section: dict[str, Any]) -> tuple[Any, ...]:
        source_range = section.get("source_range")
        return (
            section["section_id"],
            section["file_id"],
            section["section_index"],
            str(section.get("summary", "")),
            json.dumps(section.get("keywords", []), ensure_ascii=False),
            json.dumps(section.get("conditions", []), ensure_ascii=False),
            json.dumps(section.get("definitions", []), ensure_ascii=False),
            str(section.get("raw_content", "")),
            json.dumps(section.get("source_element_ids", []), ensure_ascii=False),
            None if source_range is None else json.dumps(source_range),
        )


    @staticmethod
    def _section_from_row(row: sqlite3.Row) -> dict[str, Any]:
        source_range = row["source_range"]
        return {
            "file_id": str(row["file_id"]),
            "file_name": str(row["file_name"]),
            "section_index": int(row["section_index"]),
            "summary": str(row["summary"]),
            "keywords": json.loads(row["keywords"]),
            "conditions": json.loads(row["conditions"]),
            "definitions": json.loads(row["definitions"]),
            "raw_content": str(row["raw_content"]),
            "source_element_ids": json.loads(row["source_element_ids"]),
            "source_range": None if source_range is None else json.loads(source_range),
            "section_id": str(row["section_id"]),
        }
