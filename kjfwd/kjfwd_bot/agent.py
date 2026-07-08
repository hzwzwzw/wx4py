from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Iterable, List, Sequence, Tuple
from urllib.parse import urlparse

from .capabilities import ToolCapability
from .llm import OpenAIChatClient


CURRENT_REQUEST_RE = re.compile(r"<current_request>\s*(.*?)\s*</current_request>", re.DOTALL)
SEARCH_ACCURACY_RE = re.compile(
    r"(?:参数|规格|型号|核心数|线程数|功耗|TDP|接口|尺寸|兼容|支持列表|频率|显存|"
    r"处理器|CPU|GPU|显卡|主板|硬盘|SSD|内存|错误码|报错代码|官方文档|官方说明|"
    r"最新|当前版本|近期更新|驱动版本|发布|上市|首发)",
    re.IGNORECASE,
)
MODEL_TOKEN_RE = re.compile(
    r"(?:[A-Za-z]{1,8}\d{1,4}[- ]\d{2,}[A-Za-z0-9-]*|"
    r"[A-Za-z]{1,12}[- ]?\d{2,}[A-Za-z0-9-]*|[A-Za-z]{2,}\d[A-Za-z0-9-]*|"
    r"\d{3,}[A-Za-z]+)"
)
CHINESE_MODEL_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]{1,8}\d{1,4}[A-Za-z0-9-]*")
PHYSICAL_SERVICE_RE = re.compile(
    r"(?:清灰|硅脂|液金|拆机|散热|风扇|升级|更换|维修|加装|扩容)"
)
BANNED_FILLER_RE = re.compile(
    r"(?:这种问题比较常见|我们不着急|先别着急|可以尝试一个简单的方法|我来帮你一步步排查)"
)
MARKDOWN_RE = re.compile(
    r"(?:```|\*\*|__(?=\S)|^\s*(?:#{1,6}\s+|[-*+]\s+|\d+[.)、]\s+))",
    re.MULTILINE,
)
DETAIL_COMMAND_RE = re.compile(r"(?:^|\s)/explain(?:\s|$)", re.IGNORECASE)
SOURCE_SECTION_RE = re.compile(r"\n\s*(?:参考)?来源\s*[:：].*$", re.DOTALL)
OFFICIAL_DOMAIN_SUFFIXES = (
    "microsoft.com",
    "intel.com",
    "amd.com",
    "nvidia.com",
    "apple.com",
    "google.com",
    "lenovo.com",
    "dell.com",
    "hp.com",
    "asus.com",
    "acer.com",
    "samsung.com",
)

logger = logging.getLogger(__name__)


class ToolCallingAgent:
    def __init__(
        self,
        client: OpenAIChatClient,
        tools: Iterable[ToolCapability],
        *,
        max_tool_rounds: int = 2,
        max_answer_characters: int = 700,
    ):
        self.client = client
        self.tools = {tool.name: tool for tool in tools}
        self.max_tool_rounds = max_tool_rounds
        self.max_answer_characters = max_answer_characters

    def complete(
        self, system_prompt: str, user_prompt: str, *, force_search: bool = False
    ) -> str:
        logger.info(
            "开始 LLM 回答：force_search=%s tools=%s request=%s",
            force_search,
            ",".join(self.tools) or "none",
            _request_excerpt(user_prompt),
        )
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        definitions = [tool.definition() for tool in self.tools.values()]
        sources: List[Dict[str, str]] = []
        grounding_contents: List[str] = []
        if not definitions:
            if force_search:
                raise RuntimeError("联网搜索未启用")
            text = self.client.complete(system_prompt, user_prompt)
            logger.info("LLM 回答完成：tools=none chars=%s", len(text))
            return self._finalize(
                text, system_prompt, user_prompt, sources, grounding_contents
            )

        for round_index in range(self.max_tool_rounds):
            require_search = force_search or should_require_search(user_prompt)
            logger.info(
                "LLM 工具轮：round=%s require_search=%s force_search=%s",
                round_index + 1,
                require_search,
                force_search,
            )
            if require_search and round_index == 0:
                choice: Any = {
                    "type": "function",
                    "function": {"name": "web_search"},
                }
            else:
                choice = "auto"
            # DeepSeek 思考模式不接受 required/指定函数等 tool_choice；工具轮统一关闭思考。
            assistant = self.client.chat(
                messages, tools=definitions, tool_choice=choice, thinking=False
            )
            tool_calls = assistant.get("tool_calls") or []
            if not tool_calls:
                text = str(assistant.get("content") or "").strip()
                if not text:
                    raise RuntimeError("LLM 未返回回复或工具调用")
                logger.info("LLM 回答完成：tool_calls=0 chars=%s", len(text))
                return self._finalize(
                    text, system_prompt, user_prompt, sources, grounding_contents
                )

            messages.append(self._assistant_tool_message(assistant))
            for call in tool_calls:
                function = call.get("function") or {}
                logger.info(
                    "执行 LLM 工具调用：name=%s arguments=%s",
                    function.get("name"),
                    str(function.get("arguments") or "")[:300],
                )
                tool_message, tool_sources = self._execute_tool_call(call)
                messages.append(tool_message)
                sources.extend(tool_sources)
                grounding_contents.append(str(tool_message.get("content") or ""))

        final = self.client.chat(
            messages, tools=definitions, tool_choice="none", thinking=False
        )
        text = str(final.get("content") or "").strip()
        if not text:
            raise RuntimeError("工具调用结束后 LLM 未返回最终回复")
        logger.info("LLM 工具调用后回答完成：sources=%s chars=%s", len(sources), len(text))
        return self._finalize(text, system_prompt, user_prompt, sources, grounding_contents)

    @staticmethod
    def _assistant_tool_message(message: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "role": "assistant",
            "content": message.get("content"),
            "tool_calls": message.get("tool_calls") or [],
        }

    def _execute_tool_call(
        self, call: Dict[str, Any]
    ) -> Tuple[Dict[str, Any], List[Dict[str, str]]]:
        call_id = str(call.get("id") or "")
        function = call.get("function") or {}
        name = str(function.get("name") or "")
        tool = self.tools.get(name)
        if tool is None:
            content = json.dumps({"error": f"未知工具：{name}"}, ensure_ascii=False)
        else:
            try:
                arguments = json.loads(function.get("arguments") or "{}")
                if not isinstance(arguments, dict):
                    raise ValueError("工具参数必须是 JSON 对象")
                content = tool.execute(arguments)
            except Exception as exc:
                content = json.dumps({"error": f"工具参数无效：{exc}"}, ensure_ascii=False)
        sources = extract_sources(content)
        return {"role": "tool", "tool_call_id": call_id, "content": content}, sources

    def _finalize(
        self,
        text: str,
        system_prompt: str,
        user_prompt: str,
        sources: Sequence[Dict[str, str]],
        grounding_contents: Sequence[str],
    ) -> str:
        limit = 2500 if is_detailed_request(user_prompt) else self.max_answer_characters
        draft = str(text or "").strip()
        if sources or response_needs_rewrite(draft, limit):
            try:
                draft = self._rewrite_response(
                    draft,
                    system_prompt,
                    user_prompt,
                    limit,
                    grounding_contents,
                )
            except Exception:
                # 风格修整失败不应吞掉原始有效回答，继续做确定性清理。
                pass
        draft = sanitize_plain_text(draft)
        if len(draft) > limit:
            draft = truncate_at_sentence(draft, limit)
        return append_sources(draft, rank_sources(sources))

    def _rewrite_response(
        self,
        draft: str,
        system_prompt: str,
        user_prompt: str,
        limit: int,
        grounding_contents: Sequence[str],
    ) -> str:
        editor_system = (
            system_prompt
            + "\n\n你现在只负责重写候选回复。保留有来源支持的事实，不添加新事实，不引用规则。"
            + f"使用纯文本，删除寒暄、安抚语、Markdown 和不必要的产品推荐；控制在 {limit} 字以内。"
            + "若不是 /explain，只保留当前最必要的信息或下一小组操作。不要输出来源，来源将由程序附加。"
            + "若提供了网页资料，逐项核对候选回复中的型号、数字、日期和适用范围；删除资料不支持的断言。"
            + "特别注意官方页面中的限制条件，不要把仅适用于新设备、特定版本或特定场景的结论扩大。"
        )
        grounding = "\n".join(grounding_contents)[:24000]
        editor_user = (
            user_prompt
            + ("\n\n<web_grounding>\n" + grounding + "\n</web_grounding>" if grounding else "")
            + "\n\n<draft_response>\n"
            + draft
            + "\n</draft_response>\n请只输出重写后的回复正文。"
        )
        message = self.client.chat(
            [
                {"role": "system", "content": editor_system},
                {"role": "user", "content": editor_user},
            ],
            thinking=False,
        )
        rewritten = str(message.get("content") or "").strip()
        return rewritten or draft


def should_require_search(user_prompt: str) -> bool:
    match = CURRENT_REQUEST_RE.search(user_prompt)
    request = match.group(1) if match else str(user_prompt or "")
    # 出现可识别的具体型号时直接搜索，避免模型因“记得这个型号”而跳过核验。
    if MODEL_TOKEN_RE.search(request):
        return True
    if CHINESE_MODEL_TOKEN_RE.search(request) and PHYSICAL_SERVICE_RE.search(request):
        return True
    # “最新/官方/错误码”等本身要求外部核对；硬件参数类还需出现像型号的数字标识。
    if re.search(
        r"(?:错误码|报错代码|官方文档|官方说明|最新|当前版本|近期更新|驱动版本|发布|上市|首发)",
        request,
    ):
        return True
    return False


def extract_sources(tool_content: str) -> List[Dict[str, str]]:
    try:
        payload = json.loads(tool_content)
    except (TypeError, json.JSONDecodeError):
        return []
    sources = []
    for item in payload.get("results", []) or []:
        url = str(item.get("url") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            continue
        sources.append({"title": str(item.get("title") or "网页来源").strip(), "url": url})
    return sources


def append_sources(text: str, sources: Sequence[Dict[str, str]], limit: int = 3) -> str:
    unique = []
    seen = set()
    for source in sources:
        url = source.get("url", "")
        if not url or url in seen:
            continue
        seen.add(url)
        unique.append(source)
        if len(unique) >= limit:
            break
    if not unique:
        return text.strip()
    body = SOURCE_SECTION_RE.sub("", text.strip()).rstrip()
    lines = ["来源："]
    for source in unique:
        title = source.get("title") or "网页来源"
        lines.append(f"{title}：{source['url']}")
    return body + "\n\n" + "\n".join(lines)


def rank_sources(sources: Sequence[Dict[str, str]]) -> List[Dict[str, str]]:
    indexed = list(enumerate(sources))

    def score(item: Tuple[int, Dict[str, str]]) -> Tuple[int, int]:
        index, source = item
        hostname = (urlparse(source.get("url", "")).hostname or "").lower()
        title = source.get("title", "").lower()
        official = any(
            hostname == suffix or hostname.endswith("." + suffix)
            for suffix in OFFICIAL_DOMAIN_SUFFIXES
        )
        title_hint = any(word in title for word in ("official", "官方", "specification"))
        return (int(official) * 100 + int(title_hint) * 10, -index)

    return [source for _, source in sorted(indexed, key=score, reverse=True)]


def is_detailed_request(user_prompt: str) -> bool:
    match = CURRENT_REQUEST_RE.search(user_prompt)
    request = match.group(1) if match else user_prompt
    return bool(DETAIL_COMMAND_RE.search(request))


def response_needs_rewrite(text: str, limit: int) -> bool:
    return len(text) > limit or bool(BANNED_FILLER_RE.search(text) or MARKDOWN_RE.search(text))


def sanitize_plain_text(text: str) -> str:
    value = str(text or "").strip()
    value = re.sub(r"^\s*(?:你好|您好)[！!，,：:\s]*", "", value)
    value = BANNED_FILLER_RE.sub("", value)
    value = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1：\2", value)
    value = value.replace("```", "").replace("**", "").replace("__", "").replace("`", "")
    value = re.sub(r"(?m)^\s*(?:#{1,6}\s+|[-*+]\s+|\d+[.)、]\s+)", "", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def truncate_at_sentence(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut = max(head.rfind(mark) for mark in ("。", "！", "？", "\n"))
    if cut < int(limit * 0.6):
        cut = limit
    return head[: cut + 1].rstrip()


def _request_excerpt(user_prompt: str, limit: int = 120) -> str:
    match = CURRENT_REQUEST_RE.search(user_prompt)
    request = match.group(1) if match else str(user_prompt or "")
    request = " ".join(request.split())
    if len(request) <= limit:
        return request
    return request[: limit - 1] + "…"
