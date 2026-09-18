"""Persistent polling cursors, deduplication claims and rate limits."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import time


@dataclass(frozen=True)
class StateConfig:
    path: Path
    dry_run: bool = False
    handled_keep: int = 1000
    dm_cursor_keep: int = 200


def default_state():
    return {
        'last_comment_id': None,
        'last_chat_id': None,
        'dm_cursors': {},
        'dm_primed': False,
        'private_memory_primed': False,
        'public_blogs_primed': False,
        'known_public_blog_ids': [],
        'handled_comments': [],
        'handled_chat': [],
        'comment_reply_times': [],
        'chat_reply_times': [],
        'dm_reply_times': [],
        'last_chat_reply_at': 0,
    }


class StateStore:
    """Owns all state-file IO and state-shape normalization."""

    def __init__(self, config):
        self.config = config

    def load(self):
        state = default_state()
        try:
            with self.config.path.open('r', encoding='utf-8') as file:
                saved = json.load(file)
            if isinstance(saved, dict):
                for key in state:
                    if key in saved:
                        state[key] = saved[key]
            print(f'已读到状态文件：{self.config.path}')
        except FileNotFoundError:
            print(f'还没有状态文件，按第一次运行处理喵：{self.config.path}')
        except Exception as error:
            print(f'读状态文件失败（忽略，按第一次运行处理）：{error}')
        return state

    def trim(self, state):
        keep = self.config.handled_keep
        state['handled_comments'] = list(state['handled_comments'])[-keep:]
        state['handled_chat'] = list(state['handled_chat'])[-keep:]
        cursors = state.get('dm_cursors') or {}
        if len(cursors) > self.config.dm_cursor_keep:
            state['dm_cursors'] = dict(sorted(
                cursors.items(), key=lambda item: item[1] or 0
            )[-self.config.dm_cursor_keep:])
        state['known_public_blog_ids'] = list(state.get('known_public_blog_ids') or [])[-500:]
        cutoff = time.time() - 24 * 3600
        for key in ('comment_reply_times', 'chat_reply_times', 'dm_reply_times'):
            state[key] = [stamp for stamp in state[key] if stamp > cutoff]

    def save(self, state):
        if self.config.dry_run:
            return
        self.trim(state)
        temporary = Path(str(self.config.path) + '.tmp')
        try:
            self.config.path.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open('w', encoding='utf-8') as file:
                json.dump(state, file, ensure_ascii=False, indent=1)
            os.replace(temporary, self.config.path)
        except Exception as error:
            print(f'写状态文件失败：{error}')

    @staticmethod
    def handled_key(kind):
        return 'handled_comments' if kind == 'comments' else 'handled_chat'

    @staticmethod
    def times_key(kind):
        return {
            'comments': 'comment_reply_times',
            'chat': 'chat_reply_times',
            'dm': 'dm_reply_times',
        }[kind]

    def already_handled(self, state, kind, trigger_id):
        return trigger_id in state[self.handled_key(kind)]

    def claim(self, state, kind, trigger_id):
        key = self.handled_key(kind)
        if trigger_id not in state[key]:
            state[key].append(trigger_id)
        self.save(state)

    def rate_ok(self, state, kind, limit_per_hour, now=None):
        now = time.time() if now is None else now
        used = sum(1 for stamp in state[self.times_key(kind)] if stamp > now - 3600)
        if used >= limit_per_hour:
            print(f'这一小时已经回了 {used} 条（上限 {limit_per_hour}），先歇着喵。')
            return False
        return True

    def note_reply(self, state, kind, now=None):
        now = time.time() if now is None else now
        state[self.times_key(kind)].append(now)
        if kind == 'chat':
            state['last_chat_reply_at'] = now
        self.save(state)
