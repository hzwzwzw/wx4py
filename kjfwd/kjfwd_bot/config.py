from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Mapping, Optional, Tuple


def load_dotenv(path: Path) -> None:
    """读取简单的 KEY=VALUE 文件，不覆盖已有环境变量。"""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


def _env_value(primary: str, fallback: Optional[str] = None) -> str:
    value = os.getenv(primary, "").strip()
    if not value and fallback:
        value = os.getenv(fallback, "").strip()
    return value


@dataclass(frozen=True)
class GroupConfig:
    name: str
    bot_nickname: str


@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    model: str
    api_key: str
    temperature: float = 0.3
    max_tokens: int = 700
    timeout_seconds: float = 60.0
    retries: int = 2


@dataclass(frozen=True)
class HistoryConfig:
    database_path: Path
    idle_timeout_seconds: int = 1800
    max_messages: int = 100
    max_characters: int = 16000
    retention_days: int = 30
    trigger_dedupe_seconds: float = 1.0


@dataclass(frozen=True)
class SearchConfig:
    enabled: bool
    api_key: str
    endpoint: str = "https://api.search.brave.com/res/v1/llm/context"
    timeout_seconds: float = 20.0
    retries: int = 2
    max_results: int = 5
    max_context_tokens: int = 4096
    max_snippets: int = 20
    cache_seconds: int = 900
    minimum_request_interval_seconds: float = 1.1
    max_tool_rounds: int = 2


@dataclass(frozen=True)
class ConversationPoolConfig:
    active_ttl_seconds: int = 1800
    max_active: int = 5
    global_fallback_seconds: int = 3600
    global_fallback_max_messages: int = 80
    low_information_recent_reply_seconds: int = 180


@dataclass(frozen=True)
class DebugConfig:
    conversation_id_in_reply: bool = True
    router_decision_log: bool = True


@dataclass(frozen=True)
class BotConfig:
    groups: Tuple[GroupConfig, ...]
    llm: LLMConfig
    search: SearchConfig
    history: HistoryConfig
    conversation_pool: ConversationPoolConfig
    debug: DebugConfig
    system_prompt_path: Path
    skills_path: Path
    queue_size_per_group: int = 5

    @property
    def group_names(self) -> Tuple[str, ...]:
        return tuple(group.name for group in self.groups)

    @property
    def group_nicknames(self) -> Dict[str, str]:
        return {group.name: group.bot_nickname for group in self.groups}


def load_config(
    config_path: Path,
    *,
    env_path: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> BotConfig:
    config_path = config_path.resolve()
    if env_path:
        load_dotenv(env_path.resolve())
    if environ:
        for key, value in environ.items():
            os.environ[str(key)] = str(value)

    data = json.loads(config_path.read_text(encoding="utf-8-sig"))
    base_dir = config_path.parent

    groups = tuple(
        GroupConfig(
            name=str(item.get("name", "")).strip(),
            bot_nickname=str(item.get("bot_nickname", "")).strip(),
        )
        for item in data.get("groups", [])
    )
    if not groups or any(not group.name or not group.bot_nickname for group in groups):
        raise ValueError("groups 必须包含非空的 name 和 bot_nickname")
    if len({group.name for group in groups}) != len(groups):
        raise ValueError("groups 中不能有重复群名")

    llm_data = data.get("llm", {})
    api_key = _env_value(str(llm_data.get("api_key_env", "API_KEY")))
    base_url = str(llm_data.get("base_url", "")).strip() or _env_value(
        str(llm_data.get("base_url_env", "BASE_URL"))
    )
    model = str(llm_data.get("model", "")).strip() or _env_value(
        str(llm_data.get("model_env", "MODEL"))
    )
    if not api_key:
        raise ValueError("未找到 LLM API Key 环境变量")
    if not base_url or not model:
        raise ValueError("LLM base_url 和 model 不能为空")

    history_data = data.get("history", {})
    history = HistoryConfig(
        database_path=_resolve_path(base_dir, history_data.get("database_path", "data/kjfwd.db")),
        idle_timeout_seconds=int(history_data.get("idle_timeout_seconds", 1800)),
        max_messages=int(history_data.get("max_messages", 100)),
        max_characters=int(history_data.get("max_characters", 16000)),
        retention_days=int(history_data.get("retention_days", 30)),
        trigger_dedupe_seconds=float(history_data.get("trigger_dedupe_seconds", 1.0)),
    )
    if min(history.idle_timeout_seconds, history.max_messages, history.max_characters) <= 0:
        raise ValueError("history 的时间、消息数和字符数限制必须大于 0")

    search_data = data.get("search", {})
    search_enabled = bool(search_data.get("enabled", True))
    search_api_key = _env_value(str(search_data.get("api_key_env", "BRAVE_KEY")))
    if search_enabled and not search_api_key:
        raise ValueError("联网搜索已启用，但未找到 Brave API Key 环境变量")
    search = SearchConfig(
        enabled=search_enabled,
        api_key=search_api_key,
        endpoint=str(
            search_data.get("endpoint", "https://api.search.brave.com/res/v1/llm/context")
        ).strip(),
        timeout_seconds=float(search_data.get("timeout_seconds", 20)),
        retries=int(search_data.get("retries", 2)),
        max_results=int(search_data.get("max_results", 5)),
        max_context_tokens=int(search_data.get("max_context_tokens", 4096)),
        max_snippets=int(search_data.get("max_snippets", 20)),
        cache_seconds=int(search_data.get("cache_seconds", 900)),
        minimum_request_interval_seconds=float(
            search_data.get("minimum_request_interval_seconds", 1.1)
        ),
        max_tool_rounds=int(search_data.get("max_tool_rounds", 2)),
    )
    if search.enabled and min(
        search.timeout_seconds,
        search.max_results,
        search.max_context_tokens,
        search.max_snippets,
        search.max_tool_rounds,
    ) <= 0:
        raise ValueError("search 的超时、结果数、上下文和工具轮数必须大于 0")

    pool_data = data.get("conversation_pool", {})
    conversation_pool = ConversationPoolConfig(
        active_ttl_seconds=int(pool_data.get("active_ttl_seconds", 1800)),
        max_active=int(pool_data.get("max_active", 5)),
        global_fallback_seconds=int(pool_data.get("global_fallback_seconds", 3600)),
        global_fallback_max_messages=int(pool_data.get("global_fallback_max_messages", 80)),
        low_information_recent_reply_seconds=int(
            pool_data.get("low_information_recent_reply_seconds", 180)
        ),
    )
    if min(
        conversation_pool.active_ttl_seconds,
        conversation_pool.max_active,
        conversation_pool.global_fallback_seconds,
        conversation_pool.global_fallback_max_messages,
        conversation_pool.low_information_recent_reply_seconds,
    ) <= 0:
        raise ValueError("conversation_pool 的时间和数量限制必须大于 0")

    debug_data = data.get("debug", {})
    debug = DebugConfig(
        conversation_id_in_reply=bool(debug_data.get("conversation_id_in_reply", True)),
        router_decision_log=bool(debug_data.get("router_decision_log", True)),
    )

    return BotConfig(
        groups=groups,
        llm=LLMConfig(
            base_url=base_url,
            model=model,
            api_key=api_key,
            temperature=float(llm_data.get("temperature", 0.3)),
            max_tokens=int(llm_data.get("max_tokens", 700)),
            timeout_seconds=float(llm_data.get("timeout_seconds", 60)),
            retries=int(llm_data.get("retries", 2)),
        ),
        search=search,
        history=history,
        conversation_pool=conversation_pool,
        debug=debug,
        system_prompt_path=_resolve_path(base_dir, data.get("system_prompt_path", "prompts/system.md")),
        skills_path=_resolve_path(base_dir, data.get("skills_path", "skills")),
        queue_size_per_group=int(data.get("queue_size_per_group", 5)),
    )


def _resolve_path(base_dir: Path, value: object) -> Path:
    path = Path(str(value))
    return path if path.is_absolute() else (base_dir / path).resolve()
