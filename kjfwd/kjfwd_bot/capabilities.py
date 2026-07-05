from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple


class Capability(ABC):
    """写入 system prompt 的知识与行为规范能力。"""

    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def system_prompt(self, explicitly_requested: bool) -> str:
        raise NotImplementedError


class ToolCapability(ABC):
    """由模型通过原生 tool call 调用的受控外部能力。"""

    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def definition(self) -> Dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def execute(self, arguments: Dict[str, Any]) -> str:
        raise NotImplementedError

@dataclass(frozen=True)
class PromptSkill(Capability):
    skill_name: str
    content: str
    source: Path

    @property
    def name(self) -> str:
        return self.skill_name

    @property
    def title(self) -> str:
        for line in self.content.splitlines():
            value = line.strip()
            if value.startswith("# "):
                return value[2:].strip()
        return self.skill_name

    def system_prompt(self, explicitly_requested: bool) -> str:
        mode = "用户已通过斜杠显式指定，必须优先采用" if explicitly_requested else "相关时可主动采用"
        return f"<skill name=\"{self.name}\" mode=\"{mode}\">\n{self.content.strip()}\n</skill>"


class CapabilityRegistry:
    def __init__(self, capabilities: Iterable[Capability]):
        items = list(capabilities)
        names = [item.name for item in items]
        if len(set(names)) != len(names):
            raise ValueError("skill/capability 名称不能重复")
        self._items = items

    @classmethod
    def from_skill_directory(cls, directory: Path) -> "CapabilityRegistry":
        directory = Path(directory)
        if not directory.exists():
            return cls([])
        skills: List[PromptSkill] = []
        for path in sorted(directory.rglob("*.md")):
            if path.name.lower() == "readme.md":
                continue
            content = path.read_text(encoding="utf-8-sig").strip()
            if content:
                skills.append(PromptSkill(path.stem, content, path))
        return cls(skills)

    @property
    def names(self) -> Tuple[str, ...]:
        return tuple(item.name for item in self._items)

    @property
    def command_entries(self) -> Tuple[Tuple[str, str], ...]:
        return tuple(
            (item.name, item.title if isinstance(item, PromptSkill) else item.name)
            for item in self._items
        )

    def render(self, explicitly_requested: Sequence[str]) -> str:
        explicit = set(explicitly_requested)
        blocks = [item.system_prompt(item.name in explicit) for item in self._items]
        known = ", ".join(self.names) if self.names else "（当前无 skill）"
        unknown = sorted(explicit - set(self.names))
        header = (
            "技能使用规则：\n"
            "- 可用技能：" + known + "。\n"
            "- `/技能名` 表示显式指定；若未指定，也应分析问题并主动采用相关技能。\n"
            "- skill 只是知识与行为规范，不代表已经执行任何外部操作。"
        )
        if unknown:
            header += "\n- 用户指定了不存在的技能：" + ", ".join(unknown) + "；请简短说明该技能不可用。"
        return header + ("\n\n" + "\n\n".join(blocks) if blocks else "")
