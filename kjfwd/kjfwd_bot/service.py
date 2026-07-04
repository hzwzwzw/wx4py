from __future__ import annotations

import logging
from pathlib import Path

try:
    from wx4py import WeChatClient
except ImportError:
    from src import WeChatClient

from .capabilities import CapabilityRegistry
from .config import BotConfig, load_config
from .handler import KJFWDHandler
from .history import HistoryStore
from .llm import OpenAIChatClient
from .prompt import PromptBuilder

logger = logging.getLogger(__name__)


def build_handler(config: BotConfig, history: HistoryStore) -> KJFWDHandler:
    capabilities = CapabilityRegistry.from_skill_directory(config.skills_path)
    logger.info("已加载 skills: %s", ", ".join(capabilities.names) or "无")
    return KJFWDHandler(
        groups=config.group_names,
        bot_nicknames=config.group_nicknames,
        history=history,
        model=OpenAIChatClient(config.llm),
        prompt_builder=PromptBuilder(config.system_prompt_path, capabilities),
        max_messages=config.history.max_messages,
        max_characters=config.history.max_characters,
        trigger_dedupe_seconds=config.history.trigger_dedupe_seconds,
        queue_size_per_group=config.queue_size_per_group,
    )


def run(config_path: Path, env_path: Path) -> None:
    config = load_config(config_path, env_path=env_path)
    history = HistoryStore(config.history.database_path, config.history.idle_timeout_seconds)
    history.prune(config.history.retention_days)
    handler = build_handler(config, history)
    try:
        with WeChatClient(auto_connect=True) as wx:
            wx.process_groups(
                config.group_names,
                [handler],
                group_nicknames=config.group_nicknames,
                block=True,
                tick=0.1,
                batch_size=8,
                tail_size=20,
            )
    finally:
        history.close()
