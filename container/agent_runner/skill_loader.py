from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from typing import Optional

import yaml


class _NoBoolKeysLoader(yaml.SafeLoader):
    """去掉 YAML 1.1 yes/no/on/off 布尔解析器的 SafeLoader
    """


_NoBoolKeysLoader.yaml_implicit_resolvers = {
    k: [(tag, regexp) for tag, regexp in v if tag != "tag:yaml.org,2002:bool"]
    for k, v in _NoBoolKeysLoader.yaml_implicit_resolvers.items()
}
_NoBoolKeysLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)

@dataclass
class SkillMeta:
    name: str
    description: str
    path: str
    triggers: list[dict] = field(default_factory=list)

@dataclass
class SkillContext:
    channel_type: Optional[str]
    is_first_message: bool

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)

def _log(msg: str) -> None:
    print(f"[skills] {msg}", file=sys.stderr, flush=True)



def _parse_frontmatter(path: str) -> Optional[dict]:
    """返回解析后的 YAML frontmatter 缺失/格式错误时返回 None"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            head = f.read(8192)
    except OSError as e:
        _log(f"could not read {path}: {e!r}")
        return None
    m = _FRONTMATTER_RE.match(head)
    if not m:
        return None
    raw = m.group(1)
    try:
        data = yaml.load(raw, Loader=_NoBoolKeysLoader)
    except yaml.YAMLError as e:
        _log(f"malformed YAML frontmatter in {path}: {e!r}")
        return None
    if not isinstance(data, dict):
        return None
    return data


class SkillRegistry:

    def __init__(self, skill_dirs: list[str]) -> None:
        self.all_skills: list[SkillMeta] = []
        self._index: dict[str, SkillMeta] = {}

        for d in skill_dirs:
            if not d or not os.path.isdir(d):
                # 静默跳过不存在的目录 /workspace/agent/skills 在用户创建自定义技能之前是缺失的
                continue
            try:
                entries = sorted(os.listdir(d))
            except OSError as e:
                _log(f"could not list {d}: {e!r}")
                continue

            for entry in entries:
                folder = os.path.join(d, entry)
                if not os.path.isdir(folder):
                    continue
                skill_md = os.path.join(folder, "SKILL.md")
                if not os.path.isfile(skill_md):
                    continue

                fm = _parse_frontmatter(skill_md)
                if fm is None:
                    _log(f"skipping {folder}: missing/invalid frontmatter")
                    continue
                name = fm.get("name") or entry
                description = fm.get("description") or ""
                triggers = fm.get("triggers") or []
                if not isinstance(triggers, list):
                    triggers = []
                meta = SkillMeta(
                    name=str(name),
                    description=str(description),
                    path=skill_md,
                    triggers=[t for t in triggers if isinstance(t, dict)],
                )
                self._index[meta.name] = meta  # 后扫描的目录覆盖

        self.all_skills = list(self._index.values())

    def resolve(self, ctx: SkillContext) -> tuple[list[str], list[SkillMeta]]:

        auto: list[str] = []
        lazy: list[SkillMeta] = []

        for meta in self.all_skills:
            if self._should_auto_load(meta, ctx):
                content = self.load(meta.name)
                if content is not None:
                    auto.append(content)
            else:
                lazy.append(meta)

        return auto, lazy

    def _should_auto_load(self, meta: SkillMeta, ctx: SkillContext) -> bool:
        for t in meta.triggers:
            on = t.get("on")
            if on == "always":
                return True
            if on == "first_message_in_session" and ctx.is_first_message:
                return True
            if on == "channel_type":
                value = t.get("value")
                if value is not None and value == ctx.channel_type:
                    return True
        return False

    def load(self, name: str) -> Optional[str]:
        """返回完整 SKILL.md """
        meta = self._index.get(name)
        if meta is None:
            return None
        try:
            with open(meta.path, "r", encoding="utf-8") as f:
                return f.read()
        except OSError as e:
            _log(f"could not read {meta.path}: {e!r}")
            return None


def build_skills_prompt(auto_loaded: list[str], lazy: list[SkillMeta]) -> str:

    parts: list[str] = []
    if auto_loaded:
        parts.append("\n\n---\n\n".join(auto_loaded))
    if lazy:
        index_lines = ["## Available Skills", "Call load_skill(name) for full instructions."]
        for meta in lazy:
            index_lines.append(f"- {meta.name}: {meta.description}")
        parts.append("\n".join(index_lines))
    return "\n\n".join(parts)
