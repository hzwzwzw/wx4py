from __future__ import annotations

import json
import logging
import re
from typing import Protocol

from .llm import OpenAIChatClient

logger = logging.getLogger(__name__)


class MessageClassifier(Protocol):
    def should_reply(self, *, group_name: str, content: str) -> bool:
        ...


class LLMQuestionClassifier:
    def __init__(self, client: OpenAIChatClient):
        self.client = client

    def should_reply(self, *, group_name: str, content: str) -> bool:
        text = str(content or "").strip()
        if not text:
            return False
        system = (
            "你只负责判断群聊消息是否应触发电脑维修/软硬件使用答疑机器人回复。"
            "不要回答消息内容，只输出 JSON。"
            "如果消息是在询问、求助、反馈故障结果、请求解释电脑软硬件相关内容，输出 true。"
            "如果只是闲聊、感谢、表情、无明确求助、与电脑软硬件严重无关，输出 false。"
            "不要求消息带问号；“还是不行”“报错了”“这是什么意思”“什么意思”“这个啥意思”也可能是问题。"
        )
        user = (
            f"群名：{group_name}\n"
            f"消息：{text}\n"
            '请只输出 JSON：{"should_reply":true} 或 {"should_reply":false}'
        )
        try:
            logger.info("开始 LLM 问题分类：group=%s message=%s", group_name, _excerpt(text))
            message = self.client.chat(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                thinking=False,
            )
            payload = parse_json_object(str(message.get("content") or ""))
            decision = bool(payload.get("should_reply"))
            logger.info("LLM 问题分类完成：group=%s should_reply=%s message=%s", group_name, decision, _excerpt(text))
            return decision
        except Exception as exc:
            logger.warning("问题分类失败，默认不触发回复：group=%s error=%s", group_name, exc)
            return False


class KeywordQuestionClassifier:
    """测试和无 LLM 场景下的保守 fallback。"""

    QUESTION_RE = re.compile(
        r"(?:\?|？|怎么办|怎么|如何|为啥|为什么|哪里|在哪|不行|报错|故障|坏了|无法|不能|可以吗|什么意思|啥意思|什么含义)"
    )

    def should_reply(self, *, group_name: str, content: str) -> bool:
        return bool(self.QUESTION_RE.search(str(content or "")))


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
        raise ValueError("classifier output is not an object")
    return payload


def _excerpt(value: str, limit: int = 120) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"
