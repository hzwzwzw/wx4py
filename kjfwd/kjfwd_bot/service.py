from __future__ import annotations

import logging
from pathlib import Path

try:
    from wx4py import WeChatClient
except ImportError:
    from src import WeChatClient

from .capabilities import CapabilityRegistry
from .agent import ToolCallingAgent
from .classifier import LLMQuestionClassifier
from .config import BotConfig, load_config
from .handler import KJFWDHandler
from .history import HistoryStore
from .llm import OpenAIChatClient
from .prompt import PromptBuilder
from .router import ConversationRouter
from .search import BraveSearchClient, WebSearchTool

logger = logging.getLogger(__name__)


def process_group_names(config: BotConfig) -> tuple[str, ...]:
    names = list(config.group_names)
    seen = set(names)
    for targets in config.reply_groups.values():
        for group_name in targets:
            if group_name not in seen:
                names.append(group_name)
                seen.add(group_name)
    return tuple(names)


def build_handler(config: BotConfig, history: HistoryStore) -> KJFWDHandler:
    capabilities = CapabilityRegistry.from_skill_directory(config.skills_path)
    logger.info("已加载 skills: %s", ", ".join(capabilities.names) or "无")
    llm_client = OpenAIChatClient(config.llm)
    tools = []
    if config.search.enabled:
        tools.append(WebSearchTool(BraveSearchClient(config.search)))
    agent = ToolCallingAgent(
        llm_client,
        tools,
        max_tool_rounds=config.search.max_tool_rounds,
    )
    return KJFWDHandler(
        groups=config.group_names,
        bot_nicknames=config.group_nicknames,
        listen_modes=config.listen_modes,
        reply_groups=config.reply_groups,
        history=history,
        model=agent,
        router=ConversationRouter(llm_client),
        classifier=LLMQuestionClassifier(llm_client),
        prompt_builder=PromptBuilder(config.system_prompt_path, capabilities),
        max_messages=config.history.max_messages,
        max_characters=config.history.max_characters,
        trigger_dedupe_seconds=config.history.trigger_dedupe_seconds,
        queue_size_per_group=config.queue_size_per_group,
        conversation_pool=config.conversation_pool,
        reply_debounce=config.reply_debounce,
        show_conversation_id=config.debug.conversation_id_in_reply,
    )


def run(config_path: Path, env_path: Path) -> None:
    config = load_config(config_path, env_path=env_path)
    history = HistoryStore(config.history.database_path, config.history.idle_timeout_seconds)
    history.prune(config.history.retention_days)
    handler = build_handler(config, history)
    try:
        with WeChatClient(auto_connect=True) as wx:
            wx.process_groups(
                process_group_names(config),
                [handler],
                group_nicknames=config.group_nicknames,
                block=True,
                tick=0.1,
                batch_size=8,
                tail_size=20,
            )
    finally:
        history.close()
