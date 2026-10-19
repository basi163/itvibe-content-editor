from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Repository:
    def __init__(self, path: Path) -> None:
        self.path = path

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[aiosqlite.Connection]:
        db = await aiosqlite.connect(self.path)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
        finally:
            await db.close()

    @staticmethod
    async def _fetchone(
        db: aiosqlite.Connection,
        query: str,
        parameters: tuple[Any, ...] = (),
    ) -> aiosqlite.Row | None:
        cursor = await db.execute(query, parameters)
        return await cursor.fetchone()

    async def initialize(self) -> None:
        async with self.connect() as db:
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS owners (
                    user_id INTEGER PRIMARY KEY,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS feed_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_key TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    link TEXT NOT NULL,
                    title TEXT NOT NULL,
                    published_at TEXT,
                    status TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    error TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(source_key, external_id)
                );

                CREATE TABLE IF NOT EXISTS drafts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    origin_type TEXT NOT NULL,
                    origin_ref TEXT NOT NULL,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    hashtags_json TEXT NOT NULL,
                    sources_json TEXT NOT NULL,
                    media_json TEXT NOT NULL,
                    material_json TEXT NOT NULL,
                    triage_json TEXT NOT NULL,
                    preview_index INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    editor_chat_id INTEGER,
                    editor_message_id INTEGER,
                    editor_media_message_id INTEGER,
                    target_chat_id INTEGER,
                    target_message_id INTEGER,
                    target_media_message_id INTEGER,
                    scheduled_at TEXT,
                    schedule_attempts INTEGER NOT NULL DEFAULT 0,
                    schedule_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS pending_actions (
                    user_id INTEGER PRIMARY KEY,
                    action TEXT NOT NULL,
                    draft_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(draft_id) REFERENCES drafts(id) ON DELETE CASCADE
                );
                """
            )
            cursor = await db.execute("PRAGMA table_info(drafts)")
            columns = {str(row[1]) for row in await cursor.fetchall()}
            draft_migrations = {
                "editor_media_message_id": "INTEGER",
                "target_media_message_id": "INTEGER",
                "scheduled_at": "TEXT",
                "schedule_attempts": "INTEGER NOT NULL DEFAULT 0",
                "schedule_error": "TEXT",
            }
            for column, definition in draft_migrations.items():
                if column not in columns:
                    await db.execute(
                        f"ALTER TABLE drafts ADD COLUMN {column} {definition}"
                    )
            cursor = await db.execute("PRAGMA table_info(feed_items)")
            feed_columns = {str(row[1]) for row in await cursor.fetchall()}
            if "attempt_count" not in feed_columns:
                await db.execute(
                    "ALTER TABLE feed_items ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0"
                )
            await db.commit()

    async def get_setting(self, key: str) -> str | None:
        async with self.connect() as db:
            row = await self._fetchone(db, "SELECT value FROM settings WHERE key = ?", (key,))
            return str(row["value"]) if row else None

    async def set_setting(self, key: str, value: str) -> None:
        now = _now()
        async with self.connect() as db:
            await db.execute(
                """
                INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value, now),
            )
            await db.commit()

    async def claim_owner(self, user_id: int) -> bool:
        async with self.connect() as db:
            row = await self._fetchone(db, "SELECT COUNT(*) AS count FROM owners")
            if row and int(row["count"]) > 0:
                return False
            await db.execute(
                "INSERT OR IGNORE INTO owners(user_id, created_at) VALUES (?, ?)",
                (user_id, _now()),
            )
            await db.commit()
            return True

    async def is_owner(self, user_id: int) -> bool:
        async with self.connect() as db:
            row = await self._fetchone(
                db,
                "SELECT 1 FROM owners WHERE user_id = ?",
                (user_id,),
            )
            return row is not None

    async def owner_ids(self) -> list[int]:
        async with self.connect() as db:
            cursor = await db.execute("SELECT user_id FROM owners ORDER BY created_at")
            rows = await cursor.fetchall()
            return [int(row["user_id"]) for row in rows]

    async def feed_has_items(self, source_key: str) -> bool:
        async with self.connect() as db:
            row = await self._fetchone(
                db,
                "SELECT 1 FROM feed_items WHERE source_key = ? LIMIT 1",
                (source_key,),
            )
            return row is not None

    async def feed_item_exists(self, source_key: str, external_id: str) -> bool:
        async with self.connect() as db:
            row = await self._fetchone(
                db,
                "SELECT 1 FROM feed_items WHERE source_key = ? AND external_id = ?",
                (source_key, external_id),
            )
            return row is not None

    async def get_feed_item_state(
        self,
        source_key: str,
        external_id: str,
    ) -> dict[str, Any] | None:
        async with self.connect() as db:
            row = await self._fetchone(
                db,
                """
                SELECT id, status, error, attempt_count, updated_at
                FROM feed_items
                WHERE source_key = ? AND external_id = ?
                """,
                (source_key, external_id),
            )
            return dict(row) if row else None

    async def add_feed_item(
        self,
        source_key: str,
        external_id: str,
        link: str,
        title: str,
        published_at: str | None,
        status: str,
        raw: dict[str, Any],
    ) -> int:
        now = _now()
        async with self.connect() as db:
            cursor = await db.execute(
                """
                INSERT INTO feed_items(
                    source_key, external_id, link, title, published_at, status,
                    raw_json, attempt_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_key,
                    external_id,
                    link,
                    title,
                    published_at,
                    status,
                    json.dumps(raw, ensure_ascii=False),
                    1 if status == "processing" else 0,
                    now,
                    now,
                ),
            )
            await db.commit()
            return int(cursor.lastrowid)

    async def mark_feed_processing(self, item_id: int) -> None:
        async with self.connect() as db:
            await db.execute(
                """
                UPDATE feed_items
                SET status = 'processing', error = NULL,
                    attempt_count = attempt_count + 1, updated_at = ?
                WHERE id = ?
                """,
                (_now(), item_id),
            )
            await db.commit()

    async def update_feed_status(self, item_id: int, status: str, error: str | None = None) -> None:
        async with self.connect() as db:
            await db.execute(
                "UPDATE feed_items SET status = ?, error = ?, updated_at = ? WHERE id = ?",
                (status, error, _now(), item_id),
            )
            await db.commit()

    async def create_draft(
        self,
        *,
        origin_type: str,
        origin_ref: str,
        title: str,
        body: str,
        hashtags: list[str],
        sources: list[dict[str, Any]],
        media: list[dict[str, Any]],
        material: dict[str, Any],
        triage: dict[str, Any],
    ) -> int:
        now = _now()
        async with self.connect() as db:
            cursor = await db.execute(
                """
                INSERT INTO drafts(
                    origin_type, origin_ref, title, body, hashtags_json,
                    sources_json, media_json, material_json, triage_json,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)
                """,
                (
                    origin_type,
                    origin_ref,
                    title,
                    body,
                    json.dumps(hashtags, ensure_ascii=False),
                    json.dumps(sources, ensure_ascii=False),
                    json.dumps(media, ensure_ascii=False),
                    json.dumps(material, ensure_ascii=False),
                    json.dumps(triage, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            await db.commit()
            return int(cursor.lastrowid)

    async def get_draft(self, draft_id: int) -> dict[str, Any] | None:
        async with self.connect() as db:
            row = await self._fetchone(db, "SELECT * FROM drafts WHERE id = ?", (draft_id,))
            if not row:
                return None
            return self._decode_draft(dict(row))

    async def update_draft(self, draft_id: int, **fields: Any) -> None:
        allowed = {
            "title",
            "body",
            "hashtags_json",
            "sources_json",
            "media_json",
            "material_json",
            "triage_json",
            "preview_index",
            "status",
            "editor_chat_id",
            "editor_message_id",
            "editor_media_message_id",
            "target_chat_id",
            "target_message_id",
            "target_media_message_id",
            "scheduled_at",
            "schedule_attempts",
            "schedule_error",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unsupported draft fields: {', '.join(sorted(unknown))}")
        if not fields:
            return
        fields["updated_at"] = _now()
        columns = ", ".join(f"{key} = ?" for key in fields)
        values = list(fields.values()) + [draft_id]
        async with self.connect() as db:
            await db.execute(f"UPDATE drafts SET {columns} WHERE id = ?", values)
            await db.commit()

    async def recent_drafts(self, limit: int = 10) -> list[dict[str, Any]]:
        async with self.connect() as db:
            cursor = await db.execute(
                "SELECT * FROM drafts ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            return [self._decode_draft(dict(row)) for row in await cursor.fetchall()]

    async def due_scheduled_drafts(
        self,
        now: datetime | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        current = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        async with self.connect() as db:
            cursor = await db.execute(
                """
                SELECT * FROM drafts
                WHERE status = 'scheduled'
                  AND scheduled_at IS NOT NULL
                  AND scheduled_at <= ?
                ORDER BY scheduled_at, id
                LIMIT ?
                """,
                (current, limit),
            )
            return [self._decode_draft(dict(row)) for row in await cursor.fetchall()]

    async def scheduled_publication_times(
        self,
        after: datetime | None = None,
    ) -> list[datetime]:
        current = (after or datetime.now(UTC)).astimezone(UTC).isoformat()
        async with self.connect() as db:
            cursor = await db.execute(
                """
                SELECT scheduled_at FROM drafts
                WHERE status = 'scheduled'
                  AND scheduled_at IS NOT NULL
                  AND scheduled_at > ?
                ORDER BY scheduled_at
                """,
                (current,),
            )
            values: list[datetime] = []
            for row in await cursor.fetchall():
                try:
                    value = datetime.fromisoformat(str(row["scheduled_at"]))
                except (TypeError, ValueError):
                    continue
                if value.tzinfo is None:
                    value = value.replace(tzinfo=UTC)
                values.append(value.astimezone(UTC))
            return values

    async def next_scheduled_publication(self) -> datetime | None:
        async with self.connect() as db:
            row = await self._fetchone(
                db,
                """
                SELECT scheduled_at FROM drafts
                WHERE status = 'scheduled' AND scheduled_at IS NOT NULL
                ORDER BY scheduled_at
                LIMIT 1
                """,
            )
            if not row:
                return None
            try:
                value = datetime.fromisoformat(str(row["scheduled_at"]))
            except (TypeError, ValueError):
                return None
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)
            return value.astimezone(UTC)

    async def scheduled_drafts(self, limit: int = 50) -> list[dict[str, Any]]:
        async with self.connect() as db:
            cursor = await db.execute(
                """
                SELECT * FROM drafts
                WHERE status = 'scheduled' AND scheduled_at IS NOT NULL
                ORDER BY scheduled_at, id
                LIMIT ?
                """,
                (limit,),
            )
            return [self._decode_draft(dict(row)) for row in await cursor.fetchall()]

    async def set_pending_action(self, user_id: int, action: str, draft_id: int) -> None:
        async with self.connect() as db:
            await db.execute(
                """
                INSERT INTO pending_actions(user_id, action, draft_id, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    action = excluded.action,
                    draft_id = excluded.draft_id,
                    created_at = excluded.created_at
                """,
                (user_id, action, draft_id, _now()),
            )
            await db.commit()

    async def pop_pending_action(self, user_id: int) -> tuple[str, int] | None:
        async with self.connect() as db:
            row = await self._fetchone(
                db,
                "SELECT action, draft_id FROM pending_actions WHERE user_id = ?",
                (user_id,),
            )
            if not row:
                return None
            await db.execute("DELETE FROM pending_actions WHERE user_id = ?", (user_id,))
            await db.commit()
            return str(row["action"]), int(row["draft_id"])

    async def clear_pending_actions(self) -> None:
        async with self.connect() as db:
            await db.execute("DELETE FROM pending_actions")
            await db.commit()

    async def stats(self) -> dict[str, int]:
        async with self.connect() as db:
            result: dict[str, int] = {}
            for table in ("feed_items", "drafts"):
                row = await self._fetchone(db, f"SELECT COUNT(*) AS count FROM {table}")
                result[table] = int(row["count"]) if row else 0
            cursor = await db.execute(
                "SELECT status, COUNT(*) AS count FROM drafts GROUP BY status"
            )
            for row in await cursor.fetchall():
                result[f"drafts_{row['status']}"] = int(row["count"])
            return result

    @staticmethod
    def _decode_draft(row: dict[str, Any]) -> dict[str, Any]:
        row["hashtags"] = json.loads(row.pop("hashtags_json"))
        row["sources"] = json.loads(row.pop("sources_json"))
        row["media"] = json.loads(row.pop("media_json"))
        row["material"] = json.loads(row.pop("material_json"))
        row["triage"] = json.loads(row.pop("triage_json"))
        return row
