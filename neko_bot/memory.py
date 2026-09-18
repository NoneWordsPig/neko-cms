"""SQLite vector memory with Ebbinghaus time decay.

Retrieval remains semantic: candidates are selected and compared by cosine
similarity.  Time affects their rank through the classic retention function
``R(t) = exp(-t / S)`` instead of a discontinuous "recent" bonus.
"""

from array import array
from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time


LOCAL_EMBEDDER = 'local-hash-zh-v1'


@dataclass(frozen=True)
class MemoryConfig:
    archive_dir: Path
    database_dir: Path
    vector_dimensions: int = 384
    scan_limit: int = 5000
    inject_max_items: int = 10
    inject_max_chars: int = 1200
    stability_seconds: float = 30 * 86400
    minimum_similarity: float = 0.05
    minimum_score: float = 0.01

    def __post_init__(self):
        if self.stability_seconds <= 0:
            raise ValueError('stability_seconds must be greater than zero')


def ebbinghaus_retention(age_seconds, stability_seconds):
    """Return Ebbinghaus retention ``e^(-t/S)`` in the inclusive range 0..1."""
    if stability_seconds <= 0:
        raise ValueError('stability_seconds must be greater than zero')
    age = max(0.0, float(age_seconds))
    return math.exp(-age / float(stability_seconds))


class MemoryRepository:
    """Stores and retrieves public or physically separated private memories."""

    def __init__(self, config, embedding_client=None, embedding_model='', strip_images=None,
                 clock=time.time):
        self.config = config
        self.embedding_client = embedding_client
        self.embedding_model = embedding_model
        self.strip_images = strip_images or (lambda text: text)
        self.clock = clock

    def private_path(self, owner_name):
        owner = (owner_name or '未知用户').strip().lower()
        key = hashlib.sha256(owner.encode('utf-8')).hexdigest()[:24]
        return self.config.database_dir / 'private' / f'{key}.sqlite3'

    def public_path(self):
        return self.config.database_dir / 'public.sqlite3'

    def connection(self, path, owner_name=''):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=10)
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('PRAGMA synchronous=NORMAL')
        connection.execute('''
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_key TEXT NOT NULL UNIQUE,
                created_at REAL NOT NULL,
                kind TEXT NOT NULL,
                actor_name TEXT NOT NULL DEFAULT '',
                channel_id TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                text TEXT NOT NULL,
                embedder TEXT NOT NULL,
                dimensions INTEGER NOT NULL,
                vector BLOB NOT NULL
            )
        ''')
        connection.execute('CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at DESC)')
        connection.execute('CREATE INDEX IF NOT EXISTS idx_memories_kind ON memories(kind)')
        connection.execute('CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        if owner_name:
            connection.execute(
                'INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)',
                ('owner_name', owner_name),
            )
        connection.commit()
        return connection

    def local_embedding(self, text):
        normalized = re.sub(r'\s+', ' ', (text or '').strip().lower())
        compact = ''.join(character for character in normalized if not character.isspace())
        tokens = list(compact)
        tokens.extend(compact[index:index + 2] for index in range(max(0, len(compact) - 1)))
        tokens.extend(re.findall(r'[a-z0-9_]+', normalized))
        values = [0.0] * self.config.vector_dimensions
        for token in tokens:
            digest = hashlib.blake2b(token.encode('utf-8'), digest_size=8).digest()
            number = int.from_bytes(digest, 'little')
            values[number % self.config.vector_dimensions] += 1.0 if (number >> 63) == 0 else -1.0
        norm = math.sqrt(sum(value * value for value in values))
        if norm:
            values = [value / norm for value in values]
        return LOCAL_EMBEDDER, values

    def embedding(self, text, requested_embedder=None):
        external_name = f'openai:{self.embedding_model}' if self.embedding_client else ''
        wants_external = self.embedding_client and requested_embedder in (None, external_name)
        if wants_external:
            try:
                response = self.embedding_client.embeddings.create(
                    model=self.embedding_model,
                    input=text,
                )
                values = list(response.data[0].embedding)
                norm = math.sqrt(sum(value * value for value in values))
                if norm:
                    values = [value / norm for value in values]
                return external_name, values
            except Exception as error:
                print('向量服务失败，本次退回本地索引：', str(error)[:120])
        if requested_embedder and requested_embedder != LOCAL_EMBEDDER:
            return requested_embedder, []
        return self.local_embedding(text)

    @staticmethod
    def pack_vector(values):
        return array('f', values).tobytes()

    @staticmethod
    def unpack_vector(blob):
        values = array('f')
        values.frombytes(blob)
        return values

    def store(self, path, source_key, kind, text, created_at=None, actor_name='',
              channel_id='', title='', owner_name=''):
        text = (text or '').strip()
        if not text or not source_key:
            return False
        embedder, values = self.embedding(text)
        if not values:
            return False
        try:
            with closing(self.connection(path, owner_name)) as connection:
                with connection:
                    cursor = connection.execute('''
                        INSERT OR IGNORE INTO memories
                            (source_key, created_at, kind, actor_name, channel_id, title,
                             text, embedder, dimensions, vector)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        str(source_key), float(created_at or self.clock()), kind,
                        actor_name or '', channel_id or '', title or '', text,
                        embedder, len(values), self.pack_vector(values),
                    ))
                    return cursor.rowcount > 0
        except Exception as error:
            print('写向量记忆失败（不影响回复）：', str(error)[:160])
            return False

    def search(self, path, query, exclude_source='', limit=None):
        """Rank vector matches using cosine similarity and Ebbinghaus retention."""
        path = Path(path)
        if not query or not path.is_file():
            return []
        try:
            with closing(self.connection(path)) as connection:
                rows = connection.execute('''
                    SELECT source_key, created_at, kind, actor_name, title, text,
                           embedder, dimensions, vector
                    FROM memories
                    WHERE source_key != ?
                    ORDER BY created_at DESC
                    LIMIT ?
                ''', (str(exclude_source or ''), self.config.scan_limit)).fetchall()
        except Exception as error:
            print('读向量记忆失败（跳过）：', str(error)[:160])
            return []

        query_vectors = {}
        scored = []
        now = self.clock()
        for _, created_at, kind, actor, title, text, embedder, dimensions, blob in rows:
            if embedder not in query_vectors:
                _, query_vectors[embedder] = self.embedding(query, requested_embedder=embedder)
            query_vector = query_vectors[embedder]
            if not query_vector or len(query_vector) != dimensions:
                continue
            stored = self.unpack_vector(blob)
            similarity = sum(left * right for left, right in zip(query_vector, stored))
            if similarity < self.config.minimum_similarity:
                continue
            retention = ebbinghaus_retention(now - created_at, self.config.stability_seconds)
            score = similarity * retention
            if score >= self.config.minimum_score:
                scored.append((score, created_at, kind, actor, title, text))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return scored[:limit or self.config.inject_max_items]

    def context(self, query, scope='public', owner_name='', exclude_source=''):
        path = self.public_path() if scope == 'public' else self.private_path(owner_name)
        rows = self.search(path, query, exclude_source)
        lines = []
        used = 0
        for score, created_at, kind, _, _, text in rows:
            stamp = time.strftime('%Y-%m-%d %H:%M', time.localtime(created_at))
            label = '博客' if kind == 'blog' else ('私聊' if kind == 'dm' else '大区')
            line = f'[{stamp}·{label}·记忆权重 {score:.2f}] {text}'
            if len(lines) >= self.config.inject_max_items or used + len(line) > self.config.inject_max_chars:
                break
            lines.append(line)
            used += len(line)
        return '\n'.join(reversed(lines))

    def load_archive(self, ttl=None):
        if not self.config.archive_dir.is_dir():
            return []
        cutoff = None if ttl is None else self.clock() - ttl
        entries = []
        for path in sorted(self.config.archive_dir.glob('*.jsonl')):
            try:
                with path.open(encoding='utf-8') as file:
                    for line in file:
                        try:
                            entry = json.loads(line.strip())
                        except ValueError:
                            continue
                        if isinstance(entry, dict) and (
                            cutoff is None or (entry.get('t') or 0) > cutoff
                        ):
                            entries.append(entry)
            except Exception as error:
                print(f'读记忆文件 {path.name} 失败（跳过）：', str(error)[:120])
        return sorted(entries, key=lambda entry: entry.get('t') or 0)

    def migrate_legacy_private(self):
        marker = self.config.database_dir / '.legacy_private_migrated'
        if marker.exists():
            return 0
        migrated = 0
        for entry in self.load_archive(ttl=None):
            if entry.get('kind') != 'dm' or not entry.get('who'):
                continue
            owner = entry['who']
            text = f"{owner}: {entry.get('said') or ''}"
            if entry.get('me'):
                text += f"\nneko: {entry['me']}"
            raw_key = json.dumps(entry, ensure_ascii=False, sort_keys=True)
            source_key = 'legacy:' + hashlib.sha256(raw_key.encode('utf-8')).hexdigest()
            if self.store(
                self.private_path(owner), source_key, 'dm', text,
                entry.get('t') or self.clock(), owner, entry.get('where') or '',
                owner_name=owner,
            ):
                migrated += 1
        self.config.database_dir.mkdir(parents=True, exist_ok=True)
        try:
            marker.write_text(str(int(self.clock())), encoding='utf-8')
        except Exception as error:
            print('写私聊迁移标记失败：', str(error)[:120])
        print(f'私人记忆库：已从旧归档迁移 {migrated} 条私聊记忆。')
        return migrated
