"""``data/actions.json`` 的类型化访问层。

该 JSON 由 ``tools/build_actions.py`` 根据 ``../analysis/role_action.json`` 中
的素材解析结果生成。把加载器与播放器拆开，是为了在不启动 pygame 的情况下
也能单独测试帧数据。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .constants import DATA_DIR

ACTION_FILE = DATA_DIR / "actions.json"


@dataclass(frozen=True)
class StickWindow:
    first: int
    last: int
    boxes: tuple[tuple[float, float, float, float] | None, ...]
    clip: int = 0

    def box_at(self, frame: int):
        if not self.first <= frame <= self.last:
            return None
        i = frame - self.first
        if i < 0 or i >= len(self.boxes):
            return None
        return self.boxes[i]


@dataclass(frozen=True)
class ActionDef:
    name: str
    frames: int
    loop: bool
    baked: bool
    body: int
    body_offset: tuple[float, float]
    sticks: tuple[StickWindow, ...]
    events: tuple[dict, ...] = ()
    timers: int = 0
    mp: int = 0
    damage: dict | None = None

    def stick_at(self, frame: int):
        for w in self.sticks:
            box = w.box_at(frame)
            if box is not None:
                return box
        return None


@dataclass
class HeroActions:
    hero: str
    actions: dict[str, ActionDef] = field(default_factory=dict)
    sprite: int = 0

    def get(self, name: str) -> ActionDef:
        try:
            return self.actions[name]
        except KeyError as exc:
            raise KeyError(f"unknown action {self.hero}/{name}") from exc


class ActionDB:
    """按需加载并缓存全部角色动作定义。"""

    _cache: dict[Path, ActionDB] = {}

    def __init__(self, path: Path = ACTION_FILE):
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        self.fps = float(raw.get("fps", 24))
        self.heroes: dict[str, HeroActions] = {}
        for hero, cfg in raw["heroes"].items():
            ha = HeroActions(hero=hero, sprite=int(cfg.get("sprite", 0)))
            for name, a in cfg["actions"].items():
                sticks = tuple(
                    StickWindow(
                        first=int(s["first"]), last=int(s["last"]),
                        boxes=tuple(None if b is None else tuple(float(v) for v in b)
                                    for b in s["boxes"]),
                        clip=int(s.get("clip") or 0),
                    )
                    for s in a.get("sticks", [])
                )
                ha.actions[name] = ActionDef(
                    name=name,
                    frames=int(a["frames"]),
                    loop=bool(a["loop"]),
                    baked=bool(a.get("baked", False)),
                    body=int(a.get("body", 0)),
                    body_offset=tuple(float(v) for v in a.get("body_offset", (0, 0))),
                    sticks=sticks,
                    events=tuple(a.get("events", ())),
                    timers=int(a.get("timers", 0)),
                    mp=int(a.get("mp", 0)),
                    damage=a.get("damage"),
                )
            self.heroes[hero] = ha

    @classmethod
    def load(cls, path: Path | None = None) -> "ActionDB":
        key = Path(path) if path is not None else ACTION_FILE
        if key not in cls._cache:
            cls._cache[key] = cls(key)
        return cls._cache[key]

    def for_hero(self, hero: str) -> HeroActions:
        try:
            return self.heroes[hero]
        except KeyError as exc:
            raise KeyError(f"no action data for hero {hero}") from exc
