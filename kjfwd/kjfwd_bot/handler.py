from __future__ import annotations

import hashlib
import logging
import queue
import threading
from typing import Callable, Dict, Optional, Tuple

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
    return "uia:" + ".".join(str(part) for part in runtime_id)


def trigger_key(event: MessageEvent, source_key: Optional[str], dedupe_seconds: int) -> str:
    if source_key:
        raw = f"{event.group}|{source_key}"
    else:
        # event.raw 缺失时的保守降级；极短时间内完全相同的 @ 视为重复投递。
        slot = int(float(event.timestamp) / max(1, dedupe_seconds))
        normalized = " ".join(str(event.content or "").split())
        raw = f"{event.group}|{slot}|{normalized}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def trigger_fingerprint(event: MessageEvent) -> str:
    normalized = " ".join(str(event.content or "").split())
    return hashlib.sha256(f"{event.group}|{normalized}".encode("utf-8")).hexdigest()


def append_reference_notice(reply: str) -> str:
    text = str(reply or "").strip()
    if text.endswith(REFERENCE_NOTICE):
        return text
    return f"{text}\n{REFERENCE_NOTICE}"


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
        trigger_dedupe_seconds: int = 5,
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
        if not event.is_at_me:
            return None

        key = trigger_key(event, source_key, self.trigger_dedupe_seconds)
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

        nickname = self.bot_nicknames.get(event.group, event.group_nickname or "")
        request = strip_at(event.content, nickname)
        if not request:
            self.history.mark_trigger_failed(claim.trigger_id, "empty_request")
            return None
        snapshot = self.history.snapshot(
            message, max_messages=self.max_messages, max_characters=self.max_characters
        )
        job = ReplyJob(claim.trigger_id, snapshot, request, explicit_skill_names(request))
        target_queue = self._queues.get(event.group)
        if target_queue is None:
            self.history.mark_trigger_failed(claim.trigger_id, "unknown_group")
            return None
        try:
            target_queue.put_nowait(job)
        except queue.Full:
            self.history.mark_trigger_failed(claim.trigger_id, "queue_full")
            logger.warning("群回复队列已满，丢弃触发消息：%s", event.group)
        return None

    def _worker(self, group_name: str) -> None:
        jobs = self._queues[group_name]
        while True:
            job = jobs.get()
            if job is None:
                jobs.task_done()
                break
            try:
                system_prompt, user_prompt = self.prompt_builder.build(
                    job.snapshot, job.clean_request, job.explicit_skills
                )
                reply = self.model.complete(system_prompt, user_prompt).strip()
                if not reply:
                    raise RuntimeError("模型返回空回复")
                reply = append_reference_notice(reply)
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
