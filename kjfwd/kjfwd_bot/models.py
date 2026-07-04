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


@dataclass(frozen=True)
class ReplyJob:
    trigger_id: int
    snapshot: ContextSnapshot
    clean_request: str
    explicit_skills: Tuple[str, ...]
    context_generation: int
