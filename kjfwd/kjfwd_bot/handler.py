from __future__ import annotations

import hashlib
import logging
import queue
import re
import threading
from collections import deque
from typing import Callable, Dict, Optional, Sequence, Tuple

try:
    from wx4py import MessageEvent, MessageHandler, ReplyAction
except ImportError:  # 直接从源码目录运行时的兼容路径
    from src import MessageEvent, MessageHandler, ReplyAction

from .history import HistoryStore
from .llm import ChatModel
from .models import ReplyJob
from .prompt import PromptBuilder, explicit_skill_names, strip_at

logger = logging.getLogger(__name__)
REFERENCE_NOTICE = "（内容仅供参考）"
RESET_COMMAND_RE = re.compile(r"^/(?:clear|new)(?:\s+(.*))?$", re.IGNORECASE | re.DOTALL)
SEARCH_COMMAND_RE = re.compile(r"^/search(?:\s+(.*))?$", re.IGNORECASE | re.DOTALL)
HELP_COMMAND_RE = re.compile(r"^/help\s*$", re.IGNORECASE)
NATURAL_HELP_PATTERNS = (
    re.compile(
        r"^(?:请问)?(?:如何|怎么|怎样)(?:使用|用)(?:你|这个机器人|机器人|这个\s*(?:bot|agent)|bot|agent)[？?。！!\s]*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:你|这个机器人|机器人|这个\s*(?:bot|agent)|bot|agent)(?:怎么用|如何使用|能做什么|有什么功能)[？?。！!\s]*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:你|这个机器人|机器人|这个\s*(?:bot|agent)|bot|agent)(?:有|支持)?哪些(?:可用)?(?:指令|命令)[？?。！!\s]*$",
        re.IGNORECASE,
    ),
    re.compile(r"^(?:有|支持)?哪些(?:可用)?(?:指令|命令)[？?。！!\s]*$"),
    re.compile(r"^(?:查看|显示)?(?:指令列表|命令列表)[？?。！!\s]*$"),
)


def event_source_key(event: MessageEvent) -> Optional[str]:
    raw = getattr(event, "raw", None)
    if raw is None:
        return None
    try:
        runtime_id = tuple(raw.GetRuntimeId() or ())
    except Exception:
        return None
    if not runtime_id:
        return None
    # RuntimeId 属于虚拟化 UI 控件，滚动或刷新后可能被新消息复用，不能永久当作消息 ID。
    normalized = " ".join(str(event.content or "").split())
    raw_key = f"{event.group}|{runtime_id}|{normalized}|{float(event.timestamp):.6f}"
    return "event:" + hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def trigger_key(event: MessageEvent, source_key: Optional[str]) -> str:
    if source_key:
        raw = f"{event.group}|{source_key}"
    else:
        normalized = " ".join(str(event.content or "").split())
        raw = f"{event.group}|{float(event.timestamp):.6f}|{normalized}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def trigger_fingerprint(event: MessageEvent) -> str:
    normalized = " ".join(str(event.content or "").split())
    return hashlib.sha256(f"{event.group}|{normalized}".encode("utf-8")).hexdigest()


def append_reference_notice(reply: str) -> str:
    text = str(reply or "").strip()
    if text.endswith(REFERENCE_NOTICE):
        return text
    return f"{text}\n{REFERENCE_NOTICE}"


def raw_control_mentions_bot(
    raw: object,
    nickname: str,
    *,
    max_depth: int = 3,
    max_nodes: int = 64,
) -> bool:
    """只读检查当前消息的 UIA 子控件，兼容行内 mention 被拆成富文本节点。"""
    if raw is None or not nickname or max_depth < 0 or max_nodes <= 0:
        return False

    target = "@" + _normalize_mention_text(nickname)
    pending = deque([(raw, 0)])
    visited = set()
    fragments = []
    checked = 0

    while pending and checked < max_nodes:
        control, depth = pending.popleft()
        identity = id(control)
        if identity in visited:
            continue
        visited.add(identity)
        checked += 1

        try:
            name = str(getattr(control, "Name", "") or "")
        except Exception:
            name = ""
        normalized = _normalize_mention_text(name)
        if normalized:
            fragments.append(normalized)
            if target in normalized:
                return True

        if depth >= max_depth:
            continue
        try:
            children = list(control.GetChildren() or [])
        except Exception:
            children = []
        remaining = max_nodes - checked - len(pending)
        if remaining <= 0:
            continue
        pending.extend((child, depth + 1) for child in children[:remaining])

    # 微信可能把“@”和昵称拆成相邻文本节点；按 UIA 顺序拼接后再检查。
    return target in "".join(fragments)


def _normalize_mention_text(text: str) -> str:
    value = str(text or "").replace("＠", "@").replace("\u2005", "")
    return re.sub(r"[\s\u00a0\u200b-\u200f\u202a-\u202e\u2060]", "", value)


class KJFWDHandler(MessageHandler):
    requires_group_nickname = True

    def __init__(
        self,
        *,
        groups: Tuple[str, ...],
        bot_nicknames: Dict[str, str],
        history: HistoryStore,
        model: ChatModel,
        prompt_builder: PromptBuilder,
        max_messages: int = 100,
        max_characters: int = 16000,
        trigger_dedupe_seconds: float = 1.0,
        queue_size_per_group: int = 5,
    ):
        self.groups = groups
        self.bot_nicknames = dict(bot_nicknames)
        self.history = history
        self.model = model
        self.prompt_builder = prompt_builder
        self.max_messages = max_messages
        self.max_characters = max_characters
        self.trigger_dedupe_seconds = trigger_dedupe_seconds
        self._queues = {name: queue.Queue(maxsize=queue_size_per_group) for name in groups}
        self._threads: Dict[str, threading.Thread] = {}
        self._emit_action: Optional[Callable] = None
        self._stop_event = threading.Event()
        self._context_lock = threading.RLock()
        self._context_generations = {name: 0 for name in groups}

    def set_action_emitter(self, emit_action) -> None:
        self._emit_action = emit_action
        self._stop_event.clear()
        for group_name in self.groups:
            if group_name in self._threads and self._threads[group_name].is_alive():
                continue
            thread = threading.Thread(
                target=self._worker, args=(group_name,), daemon=True, name=f"kjfwd-{group_name}"
            )
            self._threads[group_name] = thread
            thread.start()

    def handle(self, event: MessageEvent):
        source_key = event_source_key(event)
        message, _inserted = self.history.record_group_message(
            event.group, event.content, float(event.timestamp), source_key
        )
        nickname = self.bot_nicknames.get(event.group, event.group_nickname or "")
        is_at_me = event.is_at_me
        if not is_at_me:
            is_at_me = raw_control_mentions_bot(event.raw, nickname)
            if is_at_me:
                logger.info("行内 @ UIA 回退命中：group=%s", event.group)
        if not is_at_me:
            return None

        key = trigger_key(event, source_key)
        claim = self.history.claim_trigger(
            key,
            trigger_fingerprint(event),
            event.group,
            message.id,
            float(event.timestamp),
            self.trigger_dedupe_seconds,
        )
        if not claim.accepted:
            logger.info("忽略重复 @：group=%s trigger_id=%s status=%s", event.group, claim.trigger_id, claim.status)
            return None

        request = strip_at(event.content, nickname)
        if not request:
            self.history.mark_trigger_failed(claim.trigger_id, "empty_request")
            return None

        reset_match = RESET_COMMAND_RE.fullmatch(request)
        if reset_match:
            return self._handle_reset(event.group, message, claim.trigger_id, reset_match.group(1))

        if is_help_request(request):
            return self._send_direct_reply(
                event.group,
                message.session_id,
                claim.trigger_id,
                self._help_text(),
            )

        force_search, request = self._parse_search_command(request)
        if force_search and not request:
            return self._send_direct_reply(
                event.group,
                message.session_id,
                claim.trigger_id,
                "请在 /search 后写明需要查询的问题。",
            )

        with self._context_lock:
            generation = self._context_generations[event.group]
        snapshot = self.history.snapshot(
            message, max_messages=self.max_messages, max_characters=self.max_characters
        )
        job = ReplyJob(
            claim.trigger_id,
            snapshot,
            request,
            explicit_skill_names(request),
            generation,
            force_search,
        )
        self._enqueue_job(event.group, job)
        return None

    def _handle_reset(
        self,
        group_name: str,
        message,
        trigger_id: int,
        following_request: Optional[str],
    ):
        with self._context_lock:
            self._context_generations[group_name] += 1
            generation = self._context_generations[group_name]
            message = self.history.start_new_session(message.id)

            request = str(following_request or "").strip()
            if not request:
                return self._send_direct_reply(
                    group_name,
                    message.session_id,
                    trigger_id,
                    "已清除此前的聊天上下文，我们开始一次新对话。",
                )

            if is_help_request(request):
                return self._send_direct_reply(
                    group_name,
                    message.session_id,
                    trigger_id,
                    self._help_text(),
                )

            force_search, request = self._parse_search_command(request)
            if force_search and not request:
                return self._send_direct_reply(
                    group_name,
                    message.session_id,
                    trigger_id,
                    "请在 /search 后写明需要查询的问题。",
                )

            snapshot = self.history.snapshot(
                message, max_messages=self.max_messages, max_characters=self.max_characters
            )
            job = ReplyJob(
                trigger_id,
                snapshot,
                request,
                explicit_skill_names(request),
                generation,
                force_search,
            )
        self._enqueue_job(group_name, job)
        return None

    def _help_text(self) -> str:
        return build_help_text(self.prompt_builder.capabilities.command_entries)

    @staticmethod
    def _parse_search_command(request: str) -> Tuple[bool, str]:
        match = SEARCH_COMMAND_RE.fullmatch(request)
        if not match:
            return False, request
        return True, str(match.group(1) or "").strip()

    def _send_direct_reply(
        self, group_name: str, session_id: str, trigger_id: int, content: str
    ):
        try:
            reply = append_reference_notice(content)
            stored = self.history.record_assistant_message(group_name, session_id, reply)
            if self._emit_action is None:
                raise RuntimeError("消息发送器尚未初始化")
            self._emit_action(ReplyAction(group=group_name, content=reply))
            self.history.mark_trigger_sent(trigger_id, stored.id)
        except Exception as exc:
            self.history.mark_trigger_failed(trigger_id, str(exc))
            logger.exception("发送直接回复失败：group=%s", group_name)
        return None

    def _enqueue_job(self, group_name: str, job: ReplyJob) -> None:
        target_queue = self._queues.get(group_name)
        if target_queue is None:
            self.history.mark_trigger_failed(job.trigger_id, "unknown_group")
            return
        try:
            target_queue.put_nowait(job)
        except queue.Full:
            self.history.mark_trigger_failed(job.trigger_id, "queue_full")
            logger.warning("群回复队列已满，丢弃触发消息：%s", group_name)

    def _worker(self, group_name: str) -> None:
        jobs = self._queues[group_name]
        while True:
            job = jobs.get()
            if job is None:
                jobs.task_done()
                break
            try:
                with self._context_lock:
                    if job.context_generation != self._context_generations[group_name]:
                        self.history.mark_trigger_failed(job.trigger_id, "cleared")
                        continue
                system_prompt, user_prompt = self.prompt_builder.build(
                    job.snapshot, job.clean_request, job.explicit_skills
                )
                reply = self.model.complete(
                    system_prompt, user_prompt, force_search=job.force_search
                ).strip()
                if not reply:
                    raise RuntimeError("模型返回空回复")
                reply = append_reference_notice(reply)
                with self._context_lock:
                    if job.context_generation != self._context_generations[group_name]:
                        self.history.mark_trigger_failed(job.trigger_id, "cleared")
                        continue
                    stored = self.history.record_assistant_message(
                        group_name, job.snapshot.session_id, reply
                    )
                    if self._emit_action is None:
                        raise RuntimeError("消息发送器尚未初始化")
                    self._emit_action(ReplyAction(group=group_name, content=reply))
                    # sent 表示已经交给 wx4py 的串行发送队列，不代表微信提供了送达回执。
                    self.history.mark_trigger_sent(job.trigger_id, stored.id)
            except Exception as exc:
                self.history.mark_trigger_failed(job.trigger_id, str(exc))
                logger.exception("生成群回复失败：group=%s trigger_id=%s", group_name, job.trigger_id)
            finally:
                jobs.task_done()

    def stop(self) -> None:
        self._stop_event.set()
        for jobs in self._queues.values():
            jobs.put(None)
        for thread in self._threads.values():
            thread.join(timeout=10)


def is_help_request(request: str) -> bool:
    text = str(request or "").strip()
    if HELP_COMMAND_RE.fullmatch(text):
        return True
    return any(pattern.fullmatch(text) for pattern in NATURAL_HELP_PATTERNS)


def build_help_text(skill_entries: Sequence[Tuple[str, str]]) -> str:
    lines = [
        "我是柯基服务队群聊答疑助手，可以结合当前群聊记录回答电脑软硬件使用和维修问题，并在需要时联网核对资料。",
        "",
        "使用方法：在群里 @我并直接描述问题。普通提问不需要添加指令。",
        "",
        "可用指令：",
        "/help：查看介绍和指令列表。",
        "/new [新问题]：忽略此前聊天，开始新会话；可以直接接新问题。",
        "/clear [新问题]：与 /new 相同。",
        "/search <问题>：强制本次回答联网搜索；不写该指令时，我也会按需要主动搜索。",
    ]
    if skill_entries:
        lines.extend(("", "可用技能指令："))
        lines.extend(f"/{name} <问题>：{title}。" for name, title in skill_entries)
    return "\n".join(lines)
