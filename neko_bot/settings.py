"""Environment-backed application settings.

Keeping settings in one immutable object makes dependencies visible and avoids
the former situation where every subsystem reached into ``neko.py`` globals.
"""

from dataclasses import dataclass
import os
from pathlib import Path


FALSE_VALUES = {'', '0', 'false', 'no'}


def env_flag(name, default=False):
    raw_default = '1' if default else ''
    return os.getenv(name, raw_default).strip().lower() not in FALSE_VALUES


@dataclass(frozen=True)
class Settings:
    script_dir: Path
    base_url: str
    username: str
    password: str
    api_key: str
    bot_usernames: frozenset
    dry_run: bool
    replay_backlog: bool
    vision_enabled: bool
    state_file: Path
    memory_archive_dir: Path
    memory_db_dir: Path
    embedding_base_url: str
    embedding_api_key: str
    embedding_model: str
    forgetting_stability_seconds: float

    @classmethod
    def from_env(cls, script_file):
        script_dir = Path(script_file).resolve().parent
        username = os.getenv('NEKO_USERNAME', 'neko')
        bots = {
            name.strip().lower()
            for name in os.getenv('NEKO_BOT_USERNAMES', 'neko,NebulaFera,Logos').split(',')
            if name.strip()
        }
        bots.add(username.strip().lower())
        dry_run = env_flag('NEKO_DRY_RUN')
        stability_days = float(os.getenv('NEKO_MEMORY_STABILITY_DAYS', '30'))
        if stability_days <= 0:
            raise ValueError('NEKO_MEMORY_STABILITY_DAYS must be greater than zero')
        return cls(
            script_dir=script_dir,
            base_url=os.getenv('RARICY_BASE_URL', 'https://raricy.com/').rstrip('/'),
            username=username,
            password=os.getenv('NEKO_PASSWORD', ''),
            api_key=os.getenv('API_KEY') or os.getenv('api_key') or '',
            bot_usernames=frozenset(bots),
            dry_run=dry_run,
            replay_backlog=dry_run and env_flag('NEKO_REPLAY_BACKLOG'),
            vision_enabled=env_flag('NEKO_VISION', default=True),
            state_file=script_dir / ('neko_state.dryrun.json' if dry_run else 'neko_state.json'),
            memory_archive_dir=script_dir / ('neko_memory.dryrun' if dry_run else 'neko_memory'),
            memory_db_dir=script_dir / ('neko_memory_db.dryrun' if dry_run else 'neko_memory_db'),
            embedding_base_url=os.getenv('NEKO_EMBEDDING_BASE_URL', '').strip(),
            embedding_api_key=os.getenv('NEKO_EMBEDDING_API_KEY', '').strip(),
            embedding_model=os.getenv('NEKO_EMBEDDING_MODEL', '').strip(),
            forgetting_stability_seconds=stability_days * 86400,
        )
