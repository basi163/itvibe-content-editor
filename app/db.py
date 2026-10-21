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
                    publications_json TEXT NOT NULL DEFAULT '[]',
                    editor_touched_at TEXT,
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

                CREATE TABLE IF NOT EXISTS navigation_contexts (
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    draft_id INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, chat_id),
                    FOREIGN KEY(draft_id) REFERENCES drafts(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS content_sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    source_key TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_external_id TEXT,
                    last_checked_at TEXT,
                    last_success_at TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS source_input_requests (
                    chat_id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    prompt_message_id INTEGER,
                    created_at TEXT NOT NULL
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
                "publications_json": "TEXT NOT NULL DEFAULT '[]'",
                "editor_touched_at": "TEXT",
            }
            for column, definition in draft_migrations.items():
                if column not in columns:
                    await db.execute(
                        f"ALTER TABLE drafts ADD COLUMN {column} {definition}"
                    )
            touched_backfill = await self._fetchone(
                db,
                "SELECT 1 FROM settings WHERE key = 'editor_touched_backfill_v1'",
            )
            if not touched_backfill:
                await db.execute(
                    """
                    UPDATE drafts
                    SET editor_touched_at = updated_at
                    WHERE editor_touched_at IS NULL
                      AND editor_message_id IS NOT NULL
                      AND status IN ('draft', 'scheduled')
                      AND (
                          strftime('%s', updated_at)
                          - strftime('%s', created_at)
                      ) >= 60
                    """
                )
                await db.execute(
                    """
                    INSERT INTO settings(key, value, updated_at)
                    VALUES ('editor_touched_backfill_v1', 'done', ?)
                    """,
                    (_now(),),
                )
            cursor = await db.execute(
                """
                SELECT id, target_chat_id, target_message_id, scheduled_at,
                       schedule_attempts, schedule_error
                FROM drafts
                WHERE status = 'scheduled'
                  AND target_chat_id IS NOT NULL
                  AND scheduled_at IS NOT NULL
                  AND (publications_json IS NULL OR publications_json = '[]')
                """
            )
            for row in await cursor.fetchall():
                publication = {
                    "target_chat_id": int(row["target_chat_id"]),
                    "scheduled_at": str(row["scheduled_at"]),
                    "status": "scheduled",
                    "attempts": int(row["schedule_attempts"] or 0),
                    "error": row["schedule_error"],
                    "target_message_id": row["target_message_id"],
                }
                await db.execute(
                    "UPDATE drafts SET publications_json = ? WHERE id = ?",
                    (json.dumps([publication], ensure_ascii=False), int(row["id"])),
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

    async def get_draft_by_editor_message(
        self,
        chat_id: int,
        message_id: int,
    ) -> dict[str, Any] | None:
        async with self.connect() as db:
            row = await self._fetchone(
                db,
                """
                SELECT * FROM drafts
                WHERE editor_chat_id = ?
                  AND (editor_message_id = ? OR editor_media_message_id = ?)
                ORDER BY id DESC
                LIMIT 1
                """,
                (chat_id, message_id, message_id),
            )
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
            "publications_json",
            "editor_touched_at",
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

    async def unpublished_draft_count(self) -> int:
        async with self.connect() as db:
            row = await self._fetchone(
                db,
                """
                SELECT COUNT(*) AS count
                FROM drafts
                WHERE status IN ('draft', 'scheduled')
                """,
            )
            return int(row["count"]) if row else 0

    async def unpublished_drafts(
        self,
        *,
        limit: int = 8,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        async with self.connect() as db:
            cursor = await db.execute(
                """
                SELECT *
                FROM drafts
                WHERE status IN ('draft', 'scheduled')
                ORDER BY id DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            )
            return [self._decode_draft(dict(row)) for row in await cursor.fetchall()]

    async def scheduled_publications_by_channel(
        self,
    ) -> dict[int, list[datetime]]:
        async with self.connect() as db:
            cursor = await db.execute(
                "SELECT * FROM drafts WHERE status = 'scheduled'"
            )
            result: dict[int, list[datetime]] = {}
            for row in await cursor.fetchall():
                draft = self._decode_draft(dict(row))
                for publication in draft["publications"]:
                    if publication.get("status") != "scheduled":
                        continue
                    try:
                        channel_id = int(publication["target_chat_id"])
                        scheduled_at = datetime.fromisoformat(
                            str(publication.get("scheduled_at") or "")
                        )
                    except (KeyError, TypeError, ValueError):
                        continue
                    if scheduled_at.tzinfo is None:
                        scheduled_at = scheduled_at.replace(tzinfo=UTC)
                    result.setdefault(channel_id, []).append(
                        scheduled_at.astimezone(UTC)
                    )
            return {
                channel_id: sorted(values)
                for channel_id, values in result.items()
            }

    async def set_navigation_context(
        self,
        user_id: int,
        chat_id: int,
        draft_id: int,
    ) -> None:
        async with self.connect() as db:
            await db.execute(
                """
                INSERT INTO navigation_contexts(
                    user_id, chat_id, draft_id, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, chat_id) DO UPDATE SET
                    draft_id = excluded.draft_id,
                    updated_at = excluded.updated_at
                """,
                (user_id, chat_id, draft_id, _now()),
            )
            await db.commit()

    async def get_navigation_context(
        self,
        user_id: int,
        chat_id: int,
    ) -> dict[str, Any] | None:
        async with self.connect() as db:
            row = await self._fetchone(
                db,
                """
                SELECT drafts.*
                FROM navigation_contexts
                JOIN drafts ON drafts.id = navigation_contexts.draft_id
                WHERE navigation_contexts.user_id = ?
                  AND navigation_contexts.chat_id = ?
                """,
                (user_id, chat_id),
            )
            if not row:
                return None
            return self._decode_draft(dict(row))

    async def due_scheduled_drafts(
        self,
        now: datetime | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        async with self.connect() as db:
            cursor = await db.execute(
                """
                SELECT * FROM drafts
                WHERE status = 'scheduled'
                """,
            )
            due: list[dict[str, Any]] = []
            for row in await cursor.fetchall():
                draft = self._decode_draft(dict(row))
                for publication in draft["publications"]:
                    if publication.get("status") != "scheduled":
                        continue
                    try:
                        scheduled_at = datetime.fromisoformat(
                            str(publication.get("scheduled_at") or "")
                        )
                    except ValueError:
                        continue
                    if scheduled_at.tzinfo is None:
                        scheduled_at = scheduled_at.replace(tzinfo=UTC)
                    if scheduled_at.astimezone(UTC) > current:
                        continue
                    item = dict(draft)
                    item["publication_target_chat_id"] = int(
                        publication["target_chat_id"]
                    )
                    item["publication_scheduled_at"] = scheduled_at.astimezone(UTC)
                    item["publication_attempts"] = int(
                        publication.get("attempts") or 0
                    )
                    due.append(item)
            due.sort(
                key=lambda item: (
                    item["publication_scheduled_at"],
                    int(item["id"]),
                    int(item["publication_target_chat_id"]),
                )
            )
            return due[:limit]

    async def scheduled_publication_times(
        self,
        after: datetime | None = None,
        target_chat_id: int | None = None,
    ) -> list[datetime]:
        current = (after or datetime.now(UTC)).astimezone(UTC)
        async with self.connect() as db:
            cursor = await db.execute(
                "SELECT * FROM drafts WHERE status = 'scheduled'"
            )
            values: list[datetime] = []
            for row in await cursor.fetchall():
                draft = self._decode_draft(dict(row))
                for publication in draft["publications"]:
                    if publication.get("status") != "scheduled":
                        continue
                    if (
                        target_chat_id is not None
                        and int(publication.get("target_chat_id") or 0)
                        != target_chat_id
                    ):
                        continue
                    try:
                        value = datetime.fromisoformat(
                            str(publication.get("scheduled_at") or "")
                        )
                    except (TypeError, ValueError):
                        continue
                    if value.tzinfo is None:
                        value = value.replace(tzinfo=UTC)
                    value = value.astimezone(UTC)
                    if value > current:
                        values.append(value)
            return sorted(values)

    async def next_scheduled_publication(self) -> datetime | None:
        values = await self.scheduled_publication_times(after=datetime.min.replace(tzinfo=UTC))
        return values[0] if values else None

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

    async def create_content_source(
        self,
        *,
        kind: str,
        source_key: str,
        title: str,
        url: str,
        last_external_id: str | None,
    ) -> int:
        now = _now()
        async with self.connect() as db:
            cursor = await db.execute(
                """
                INSERT INTO content_sources(
                    kind, source_key, title, url, enabled, last_external_id,
                    last_checked_at, last_success_at, last_error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    kind,
                    source_key,
                    title,
                    url,
                    last_external_id,
                    now,
                    now,
                    now,
                    now,
                ),
            )
            await db.commit()
            return int(cursor.lastrowid)

    async def content_sources(
        self,
        *,
        kind: str | None = None,
        enabled_only: bool = False,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        parameters: list[Any] = []
        if kind:
            conditions.append("kind = ?")
            parameters.append(kind)
        if enabled_only:
            conditions.append("enabled = 1")
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        async with self.connect() as db:
            cursor = await db.execute(
                f"SELECT * FROM content_sources {where} ORDER BY title, id",
                tuple(parameters),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def get_content_source(self, source_id: int) -> dict[str, Any] | None:
        async with self.connect() as db:
            row = await self._fetchone(
                db,
                "SELECT * FROM content_sources WHERE id = ?",
                (source_id,),
            )
            return dict(row) if row else None

    async def get_content_source_by_key(self, source_key: str) -> dict[str, Any] | None:
        async with self.connect() as db:
            row = await self._fetchone(
                db,
                "SELECT * FROM content_sources WHERE source_key = ?",
                (source_key,),
            )
            return dict(row) if row else None

    async def update_content_source(self, source_id: int, **fields: Any) -> None:
        allowed = {
            "title",
            "url",
            "enabled",
            "last_external_id",
            "last_checked_at",
            "last_success_at",
            "last_error",
        }
        invalid = set(fields) - allowed
        if invalid:
            raise ValueError(f"Unsupported source fields: {sorted(invalid)}")
        if not fields:
            return
        fields["updated_at"] = _now()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        async with self.connect() as db:
            await db.execute(
                f"UPDATE content_sources SET {assignments} WHERE id = ?",
                (*fields.values(), source_id),
            )
            await db.commit()

    async def delete_content_source(self, source_id: int) -> None:
        async with self.connect() as db:
            await db.execute("DELETE FROM content_sources WHERE id = ?", (source_id,))
            await db.commit()

    async def set_source_input_request(
        self,
        chat_id: int,
        user_id: int,
        prompt_message_id: int | None,
    ) -> None:
        async with self.connect() as db:
            await db.execute(
                """
                INSERT INTO source_input_requests(
                    chat_id, user_id, prompt_message_id, created_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    user_id = excluded.user_id,
                    prompt_message_id = excluded.prompt_message_id,
                    created_at = excluded.created_at
                """,
                (chat_id, user_id, prompt_message_id, _now()),
            )
            await db.commit()

    async def get_source_input_request(self, chat_id: int) -> dict[str, Any] | None:
        async with self.connect() as db:
            row = await self._fetchone(
                db,
                "SELECT * FROM source_input_requests WHERE chat_id = ?",
                (chat_id,),
            )
            return dict(row) if row else None

    async def clear_source_input_request(self, chat_id: int) -> None:
        async with self.connect() as db:
            await db.execute(
                "DELETE FROM source_input_requests WHERE chat_id = ?",
                (chat_id,),
            )
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
        row["publications"] = json.loads(row.pop("publications_json", "[]") or "[]")
        return row
