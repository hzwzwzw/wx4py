from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class StoredMessage:
    id: int
    group_name: str
    role: str
    content: str
    observed_at: float
    session_id: str
    source_key: Optional[str] = None
    conversation_id: Optional[str] = None


@dataclass(frozen=True)
class TriggerClaim:
    accepted: bool
    trigger_id: int
    sent: bool
    status: str


@dataclass(frozen=True)
class ContextSnapshot:
    group_name: str
    session_id: str
    trigger_message_id: int
    messages: Tuple[StoredMessage, ...]
    conversation_id: Optional[str] = None
    global_messages: Tuple[StoredMessage, ...] = ()
    ambiguous: bool = False


@dataclass(frozen=True)
class ReplyJob:
    trigger_id: int
    snapshot: ContextSnapshot
    clean_request: str
    explicit_skills: Tuple[str, ...]
    context_generation: int
    force_search: bool = False
    reply_groups: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Conversation:
    id: str
    group_name: str
    title: str
    status: str
    created_at: float
    updated_at: float
    last_trigger_at: Optional[float]
    message_count: int


@dataclass(frozen=True)
class ConversationRoute:
    action: str
    conversation_id: Optional[str] = None
    title: str = ""
    reason: str = ""
