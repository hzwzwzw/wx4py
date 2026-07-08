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
from .models import ConversationRoute, ReplyJob
from .prompt import PromptBuilder, explicit_skill_names, strip_at
from .config import ConversationPoolConfig
from .classifier import KeywordQuestionClassifier, MessageClassifier
from .router import AlwaysNewRouter, Router, is_low_information_followup, title_from_request

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
        listen_modes: Optional[Dict[str, str]] = None,
        reply_groups: Optional[Dict[str, Tuple[str, ...]]] = None,
        router: Optional[Router] = None,
        classifier: Optional[MessageClassifier] = None,
        max_messages: int = 100,
        max_characters: int = 16000,
        trigger_dedupe_seconds: float = 1.0,
        queue_size_per_group: int = 5,
        conversation_pool: ConversationPoolConfig = ConversationPoolConfig(),
        show_conversation_id: bool = True,
    ):
        self.groups = groups
        self.bot_nicknames = dict(bot_nicknames)
        self.listen_modes = {name: "mention_only" for name in groups}
        if listen_modes:
            self.listen_modes.update(listen_modes)
        self.reply_groups = {name: (name,) for name in groups}
        if reply_groups:
            self.reply_groups.update({name: tuple(targets) for name, targets in reply_groups.items()})
        self.history = history
        self.model = model
        self.router = router or AlwaysNewRouter()
        self.classifier = classifier or KeywordQuestionClassifier()
        self.prompt_builder = prompt_builder
        self.max_messages = max_messages
        self.max_characters = max_characters
        self.trigger_dedupe_seconds = trigger_dedupe_seconds
        self.conversation_pool = conversation_pool
        self.show_conversation_id = show_conversation_id
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
        if event.group not in self.groups:
            return None
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
        request = strip_at(event.content, nickname) if is_at_me else str(event.content or "").strip()
        mode = self.listen_modes.get(event.group, "mention_only")
        if not self._should_trigger(event.group, mode, is_at_me, request):
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

        if not request:
            self.history.mark_trigger_failed(claim.trigger_id, "empty_request")
            return None

        reset_match = RESET_COMMAND_RE.fullmatch(request)
        if reset_match:
            return self._handle_reset(
                event.group,
                message,
                claim.trigger_id,
                reset_match.group(1),
                self._reply_groups_for(event.group),
            )

        if is_help_request(request):
            conversation = self.history.create_conversation(
                event.group, title="帮助", now=float(event.timestamp), status="ambiguous"
            )
            message = self.history.bind_message_to_conversation(
                message.id, conversation.id, trigger_at=float(event.timestamp)
            )
            return self._send_direct_reply(
                event.group,
                message.session_id,
                claim.trigger_id,
                self._help_text(),
                conversation_id=conversation.id,
                reply_groups=self._reply_groups_for(event.group),
                original_message=request,
            )

        force_search, request = self._parse_search_command(request)
        if force_search and not request:
            conversation = self.history.create_conversation(
                event.group, title="搜索指令", now=float(event.timestamp), status="ambiguous"
            )
            message = self.history.bind_message_to_conversation(
                message.id, conversation.id, trigger_at=float(event.timestamp)
            )
            return self._send_direct_reply(
                event.group,
                message.session_id,
                claim.trigger_id,
                "请在 /search 后写明需要查询的问题。",
                conversation_id=conversation.id,
                reply_groups=self._reply_groups_for(event.group),
                original_message="/search",
            )

        with self._context_lock:
            generation = self._context_generations[event.group]
        message, route = self._route_message(event.group, message, request, float(event.timestamp))
        snapshot = self._snapshot_for_route(message, route)
        job = ReplyJob(
            claim.trigger_id,
            snapshot,
            request,
            explicit_skill_names(request),
            generation,
            force_search,
            self._reply_groups_for(event.group),
            request,
        )
        self._enqueue_job(event.group, job)
        return None

    def _handle_reset(
        self,
        group_name: str,
        message,
        trigger_id: int,
        following_request: Optional[str],
        reply_groups: Tuple[str, ...],
    ):
        with self._context_lock:
            self._context_generations[group_name] += 1
            generation = self._context_generations[group_name]
            message = self.history.start_new_session(message.id)

            request = str(following_request or "").strip()
            conversation = self.history.create_conversation(
                group_name, title=title_from_request(request) if request else "新会话", now=message.observed_at
            )
            message = self.history.bind_message_to_conversation(
                message.id, conversation.id, trigger_at=message.observed_at
            )
            if not request:
                return self._send_direct_reply(
                    group_name,
                    message.session_id,
                    trigger_id,
                    "已清除此前的聊天上下文，我们开始一次新对话。",
                    conversation_id=conversation.id,
                    reply_groups=reply_groups,
                    original_message="/clear",
                )

            if is_help_request(request):
                return self._send_direct_reply(
                    group_name,
                    message.session_id,
                    trigger_id,
                    self._help_text(),
                    conversation_id=conversation.id,
                    reply_groups=reply_groups,
                    original_message=request,
                )

            force_search, request = self._parse_search_command(request)
            if force_search and not request:
                return self._send_direct_reply(
                    group_name,
                    message.session_id,
                    trigger_id,
                    "请在 /search 后写明需要查询的问题。",
                    conversation_id=conversation.id,
                    reply_groups=reply_groups,
                    original_message="/search",
                )

            snapshot = self.history.conversation_snapshot(
                message,
                conversation.id,
                max_messages=self.max_messages,
                max_characters=self.max_characters,
            )
            job = ReplyJob(
                trigger_id,
                snapshot,
                request,
                explicit_skill_names(request),
                generation,
                force_search,
                reply_groups,
                request,
            )
        self._enqueue_job(group_name, job)
        return None

    def _route_message(self, group_name: str, message, request: str, timestamp: float):
        candidates = self.history.list_active_conversations(
            group_name,
            now=timestamp,
            ttl_seconds=self.conversation_pool.active_ttl_seconds,
            limit=self.conversation_pool.max_active,
        )
        if is_low_information_followup(request):
            recent = [
                item
                for item in candidates
                if timestamp - item.updated_at
                <= self.conversation_pool.low_information_recent_reply_seconds
            ]
            if len(recent) == 1:
                route = ConversationRoute("use_existing", conversation_id=recent[0].id, reason="low_information_single_recent")
            elif candidates:
                route = ConversationRoute("ambiguous", reason="low_information_multiple_candidates")
            else:
                route = ConversationRoute("create_new", title=title_from_request(request), reason="low_information_no_candidates")
        else:
            recent_messages = {
                item.id: self.history.conversation_recent_messages(item.id, limit=6)
                for item in candidates
            }
            route = self.router.route(
                group_name=group_name,
                request=request,
                candidates=candidates,
                recent_messages=recent_messages,
            )

        if route.action == "use_existing" and route.conversation_id:
            message = self.history.bind_message_to_conversation(
                message.id, route.conversation_id, trigger_at=timestamp
            )
        elif route.action == "create_new":
            conversation = self.history.create_conversation(
                group_name, title=route.title or title_from_request(request), now=timestamp
            )
            route = ConversationRoute("create_new", conversation_id=conversation.id, title=conversation.title, reason=route.reason)
            message = self.history.bind_message_to_conversation(
                message.id, conversation.id, trigger_at=timestamp
            )
        else:
            ambiguous = self.history.create_conversation(
                group_name, title="未判定追问", now=timestamp, status="ambiguous"
            )
            route = ConversationRoute("ambiguous", conversation_id=ambiguous.id, reason=route.reason)
            message = self.history.bind_message_to_conversation(
                message.id, ambiguous.id, trigger_at=timestamp
            )
        logger.info("会话路由：group=%s action=%s conversation=%s reason=%s", group_name, route.action, route.conversation_id, route.reason)
        return message, route

    def _snapshot_for_route(self, message, route: ConversationRoute):
        if route.action == "ambiguous":
            return self.history.ambiguous_snapshot(
                message,
                conversation_id=route.conversation_id,
                global_seconds=self.conversation_pool.global_fallback_seconds,
                global_max_messages=self.conversation_pool.global_fallback_max_messages,
                max_characters=self.max_characters,
            )
        if route.conversation_id:
            return self.history.conversation_snapshot(
                message,
                route.conversation_id,
                max_messages=self.max_messages,
                max_characters=self.max_characters,
            )
        return self.history.snapshot(
            message, max_messages=self.max_messages, max_characters=self.max_characters
        )

    def _help_text(self) -> str:
        return build_help_text(self.prompt_builder.capabilities.command_entries)

    @staticmethod
    def _parse_search_command(request: str) -> Tuple[bool, str]:
        match = SEARCH_COMMAND_RE.fullmatch(request)
        if not match:
            return False, request
        return True, str(match.group(1) or "").strip()

    def _send_direct_reply(
        self,
        group_name: str,
        session_id: str,
        trigger_id: int,
        content: str,
        conversation_id: Optional[str] = None,
        reply_groups: Tuple[str, ...] = (),
        original_message: str = "",
    ):
        try:
            reply = self._finalize_reply(content, conversation_id)
            stored = self.history.record_assistant_message(
                group_name, session_id, reply, conversation_id=conversation_id
            )
            if self._emit_action is None:
                raise RuntimeError("消息发送器尚未初始化")
            for target_group in reply_groups or self._reply_groups_for(group_name):
                self._emit_action(
                    ReplyAction(
                        group=target_group,
                        content=self._outbound_reply(
                            group_name, target_group, reply, original_message
                        ),
                    )
                )
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
                logger.info(
                    "开始生成回复：group=%s trigger_id=%s conversation=%s ambiguous=%s force_search=%s reply_groups=%s request=%s",
                    group_name,
                    job.trigger_id,
                    job.snapshot.conversation_id,
                    job.snapshot.ambiguous,
                    job.force_search,
                    ",".join(job.reply_groups or self._reply_groups_for(group_name)),
                    _log_excerpt(job.clean_request),
                )
                reply = self.model.complete(
                    system_prompt, user_prompt, force_search=job.force_search
                ).strip()
                if not reply:
                    raise RuntimeError("模型返回空回复")
                reply = self._finalize_reply(reply, job.snapshot.conversation_id, job.snapshot.ambiguous)
                with self._context_lock:
                    if job.context_generation != self._context_generations[group_name]:
                        self.history.mark_trigger_failed(job.trigger_id, "cleared")
                        continue
                    stored = self.history.record_assistant_message(
                        group_name,
                        job.snapshot.session_id,
                        reply,
                        conversation_id=job.snapshot.conversation_id,
                    )
                    if self._emit_action is None:
                        raise RuntimeError("消息发送器尚未初始化")
                    for target_group in job.reply_groups or self._reply_groups_for(group_name):
                        self._emit_action(
                            ReplyAction(
                                group=target_group,
                                content=self._outbound_reply(
                                    group_name, target_group, reply, job.original_message
                                ),
                            )
                        )
                    # sent 表示已经交给 wx4py 的串行发送队列，不代表微信提供了送达回执。
                    self.history.mark_trigger_sent(job.trigger_id, stored.id)
                    logger.info(
                        "回复已入队发送：group=%s trigger_id=%s conversation=%s reply_groups=%s reply_chars=%s",
                        group_name,
                        job.trigger_id,
                        job.snapshot.conversation_id,
                        ",".join(job.reply_groups or self._reply_groups_for(group_name)),
                        len(reply),
                    )
            except Exception as exc:
                self.history.mark_trigger_failed(job.trigger_id, str(exc))
                logger.exception("生成群回复失败：group=%s trigger_id=%s", group_name, job.trigger_id)
            finally:
                jobs.task_done()

    def _finalize_reply(
        self,
        content: str,
        conversation_id: Optional[str],
        ambiguous: bool = False,
    ) -> str:
        text = str(content or "").strip()
        if self.show_conversation_id:
            label = "ambiguous" if ambiguous else (conversation_id[:8] if conversation_id else "direct")
            if not text.startswith("[conv:"):
                text = f"[conv: {label}]\n{text}"
        return append_reference_notice(text)

    def _should_trigger(self, group_name: str, mode: str, is_at_me: bool, request: str) -> bool:
        logger.info(
            "判断触发：group=%s mode=%s is_at=%s request=%s",
            group_name,
            mode,
            is_at_me,
            _log_excerpt(request),
        )
        if is_at_me:
            if mode != "question_only":
                logger.info("触发通过：group=%s reason=at_mention", group_name)
                return True
            if self._is_command_request(request):
                logger.info("触发通过：group=%s reason=command_or_help", group_name)
                return True
            decision = self.classifier.should_reply(group_name=group_name, content=request)
            logger.info("问题分类结果：group=%s should_reply=%s request=%s", group_name, decision, _log_excerpt(request))
            return decision
        if mode == "mention_only":
            logger.info("触发跳过：group=%s reason=mention_only_without_at", group_name)
            return False
        if mode == "all_messages":
            decision = bool(str(request or "").strip())
            logger.info("触发结果：group=%s mode=all_messages should_reply=%s", group_name, decision)
            return decision
        if mode == "question_only":
            if self._is_command_request(request):
                logger.info("触发通过：group=%s reason=command_or_help", group_name)
                return True
            decision = self.classifier.should_reply(group_name=group_name, content=request)
            logger.info("问题分类结果：group=%s should_reply=%s request=%s", group_name, decision, _log_excerpt(request))
            return decision
        logger.warning("未知监听模式，跳过触发：group=%s mode=%s", group_name, mode)
        return False

    @staticmethod
    def _is_command_request(request: str) -> bool:
        text = str(request or "").strip()
        return bool(
            RESET_COMMAND_RE.fullmatch(text)
            or HELP_COMMAND_RE.fullmatch(text)
            or SEARCH_COMMAND_RE.fullmatch(text)
            or is_help_request(text)
        )

    def _reply_groups_for(self, group_name: str) -> Tuple[str, ...]:
        targets = tuple(value for value in self.reply_groups.get(group_name, (group_name,)) if value)
        return targets or (group_name,)

    @staticmethod
    def _outbound_reply(
        source_group: str, target_group: str, reply: str, original_message: str = ""
    ) -> str:
        if target_group == source_group:
            return reply
        original = _log_excerpt(original_message, limit=300) or "（空）"
        prefix = f"[来源群：{source_group}]\n[原始消息：{original}]"
        if str(reply or "").startswith(prefix):
            return reply
        return prefix + "\n" + reply

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


def _log_excerpt(value: str, *, limit: int = 120) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"
