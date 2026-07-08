from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Protocol, Sequence

from .llm import OpenAIChatClient
from .models import Conversation, ConversationRoute, StoredMessage

logger = logging.getLogger(__name__)

LOW_INFORMATION_RE = re.compile(
    r"^(?:还是)?(?:不行|没用|失败了|报错了|下一步|然后呢|怎么弄|在哪(?:里)?|"
    r"怎么打开|这个是啥|可以吗|我试了|试过了)[？?。！!\s]*$"
)


class Router(Protocol):
    def route(
        self,
        *,
        group_name: str,
        request: str,
        candidates: Sequence[Conversation],
        recent_messages: dict[str, Sequence[StoredMessage]],
    ) -> ConversationRoute:
        ...


@dataclass(frozen=True)
class ConversationPoolConfig:
    active_ttl_seconds: int = 1800
    max_active: int = 5
    global_fallback_seconds: int = 3600
    global_fallback_max_messages: int = 80
    low_information_recent_reply_seconds: int = 180


class ConversationRouter:
    def __init__(self, client: OpenAIChatClient):
        self.client = client

    def route(
        self,
        *,
        group_name: str,
        request: str,
        candidates: Sequence[Conversation],
        recent_messages: dict[str, Sequence[StoredMessage]],
    ) -> ConversationRoute:
        if not candidates:
            return ConversationRoute("create_new", title=title_from_request(request))

        system = (
            "你只负责给群聊机器人做会话路由，不回答用户问题。"
            "判断标准是“当前消息是否是某个已有会话的自然下一轮”，不是话题相似度。"
            "只有当前消息像是在补充信息、反馈结果、追问步骤、承接上一轮时，才选择已有会话。"
            "如果只是主题相近但不像同一段递进对话，选择 create_new。"
            "如果当前消息缺少锚点，且多个候选都可能承接，选择 ambiguous。"
            "只输出 JSON，不要输出 Markdown。"
        )
        user = self._build_user_prompt(group_name, request, candidates, recent_messages)
        try:
            logger.info(
                "开始 LLM 会话路由：group=%s candidates=%s request=%s",
                group_name,
                len(candidates),
                _excerpt(request),
            )
            message = self.client.chat(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                thinking=False,
            )
            payload = parse_json_object(str(message.get("content") or ""))
        except Exception as exc:
            logger.warning("会话路由失败，转入 ambiguous fallback：%s", exc)
            return ConversationRoute("ambiguous", reason=str(exc))

        action = str(payload.get("action") or "").strip()
        if action == "use_existing":
            conversation_id = str(payload.get("conversation_id") or "").strip()
            if conversation_id in {item.id for item in candidates}:
                route = ConversationRoute(
                    "use_existing",
                    conversation_id=conversation_id,
                    reason=str(payload.get("reason") or ""),
                )
                logger.info(
                    "LLM 会话路由完成：group=%s action=%s conversation=%s reason=%s",
                    group_name,
                    route.action,
                    route.conversation_id,
                    route.reason,
                )
                return route
            return ConversationRoute("ambiguous", reason="router selected unknown conversation")
        if action == "create_new":
            route = ConversationRoute(
                "create_new",
                title=str(payload.get("title") or title_from_request(request))[:80],
                reason=str(payload.get("reason") or ""),
            )
            logger.info(
                "LLM 会话路由完成：group=%s action=%s title=%s reason=%s",
                group_name,
                route.action,
                route.title,
                route.reason,
            )
            return route
        if action == "ambiguous":
            route = ConversationRoute("ambiguous", reason=str(payload.get("reason") or ""))
            logger.info(
                "LLM 会话路由完成：group=%s action=%s reason=%s",
                group_name,
                route.action,
                route.reason,
            )
            return route
        return ConversationRoute("ambiguous", reason=f"invalid action: {action}")

    @staticmethod
    def _build_user_prompt(
        group_name: str,
        request: str,
        candidates: Sequence[Conversation],
        recent_messages: dict[str, Sequence[StoredMessage]],
    ) -> str:
        blocks = [f"群名：{group_name}", f"当前消息：{request}", "", "候选会话："]
        for index, conversation in enumerate(candidates, 1):
            blocks.append(
                f"{index}. conversation_id={conversation.id}\n"
                f"title={conversation.title}\n"
                f"updated_at={int(conversation.updated_at)}；最近消息："
            )
            for message in recent_messages.get(conversation.id, ())[-6:]:
                role = "bot" if message.role == "assistant" else "群成员"
                blocks.append(f"- {role}: {message.content}")
        blocks.append(
            "\n输出格式三选一：\n"
            '{"action":"use_existing","conversation_id":"...","reason":"..."}\n'
            '{"action":"create_new","title":"简短标题","reason":"..."}\n'
            '{"action":"ambiguous","reason":"..."}'
        )
        return "\n".join(blocks)


class AlwaysNewRouter:
    def route(
        self,
        *,
        group_name: str,
        request: str,
        candidates: Sequence[Conversation],
        recent_messages: dict[str, Sequence[StoredMessage]],
    ) -> ConversationRoute:
        return ConversationRoute("create_new", title=title_from_request(request))


def is_low_information_followup(request: str) -> bool:
    return bool(LOW_INFORMATION_RE.fullmatch(str(request or "").strip()))


def title_from_request(request: str) -> str:
    text = re.sub(r"\s+", " ", str(request or "")).strip()
    text = re.sub(r"^/[\w.-]+\s*", "", text)
    if not text:
        return "新会话"
    return text[:24]


def parse_json_object(text: str) -> dict:
    value = str(text or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value)
        value = re.sub(r"\s*```$", "", value)
    match = re.search(r"\{.*\}", value, re.DOTALL)
    if match:
        value = match.group(0)
    payload = json.loads(value)
    if not isinstance(payload, dict):
        raise ValueError("router output is not an object")
    return payload


def _excerpt(value: str, limit: int = 120) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"
