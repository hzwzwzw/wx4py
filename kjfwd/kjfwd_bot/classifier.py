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
            "群聊里同时有前来咨询的客户和正在协助的科服队员。机器人只应该回答像客户发出的求助消息。"
            "如果消息像客户在询问、求助、反馈自己遇到的故障结果、请求解释电脑软硬件相关内容，输出 true。"
            "如果消息像科服队员在继续追问信息、给出诊断判断、指导客户执行步骤、提醒风险、建议线下处理，输出 false。"
            "典型不触发：你先重启一下、把报错截图发一下、先进设备管理器看一下、建议先备份、这个应该是驱动问题、可以拿到线下看看。"
            "如果只是闲聊、感谢、表情、无明确求助、与电脑软硬件严重无关，输出 false。"
            "不要求客户消息带问号；“还是不行”“报错了”“这是什么意思”“什么意思”“这个啥意思”也可能是客户问题。"
            "如果无法判断是客户求助还是队员指导，宁可输出 false，避免机器人插话干扰真人队员。"
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

    STAFF_GUIDANCE_RE = re.compile(
        r"^(?:你(?:先|这边)?|先|把|麻烦|发一下|截图|提供一下|确认一下|检查一下|"
        r"重启一下|卸载|安装|更新|打开|进|看一下|试试|建议|可以先|需要先|最好先|"
        r"不要|别|拿到|带到).{0,80}(?:一下|看看|发一下|试试|处理|线下|值班|备份|截图|设备管理器|驱动|BIOS)"
    )
    QUESTION_RE = re.compile(
        r"(?:\?|？|怎么办|怎么|如何|为啥|为什么|哪里|在哪|不行|报错|故障|坏了|无法|不能|可以吗|什么意思|啥意思|什么含义)"
    )

    def should_reply(self, *, group_name: str, content: str) -> bool:
        text = str(content or "").strip()
        if self.STAFF_GUIDANCE_RE.search(text):
            return False
        return bool(self.QUESTION_RE.search(text))


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
