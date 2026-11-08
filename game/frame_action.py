"""单个动作的帧时钟。

播放头每经过一个 24 fps 帧就前进一次，随后执行该帧事件。本类只负责这一部分：
它持有从 1 开始计数的 ``frame``、待处理的帧事件，以及每帧的 ``stick`` 判定框。
状态、物理和伤害仍由玩家对象负责。
"""
from __future__ import annotations

from dataclasses import dataclass

from .action_data import ActionDB, ActionDef


@dataclass
class FrameAction:
    db: ActionDB
    hero: str
    action: ActionDef | None = None
    frame: int = 0
    finished: bool = True
    _pending: list[dict] | None = None
    _frozen: bool = False

    @property
    def name(self) -> str:
        return self.action.name if self.action else ""

    def play(self, name: str) -> None:
        self.action = self.db.for_hero(self.hero).get(name)
        self.frame = 1
        self.finished = False
        self._frozen = False
        self._pending = self._events_at(1)

    def advance(self) -> None:
        """将播放头向前推进一帧，并缓冲该帧的事件。"""
        if self.action is None or self.finished or self._frozen:
            return
        if self.frame < self.action.frames:
            self.frame += 1
            self._pending = self._events_at(self.frame)
        elif self.action.loop:
            self.frame = 1
            self._pending = self._events_at(1)
        else:
            self.finished = True
            self._pending = []

    def skip_to_end(self) -> None:
        if self.action is None:
            return
        self.frame = self.action.frames
        self._pending = self._events_at(self.frame)
        self.finished = True

    def goto_frame(self, frame: int) -> None:
        """跳转到指定帧并继续播放；目标帧的事件会进入待处理队列。"""
        if self.action is None:
            return
        self.frame = max(1, min(int(frame), self.action.frames))
        self._pending = self._events_at(self.frame)
        self.finished = False

    def freeze(self) -> None:
        self._frozen = True

    def unfreeze(self) -> None:
        self._frozen = False

    def take_events(self) -> list[dict]:
        events, self._pending = self._pending or [], []
        return events

    def peek_events(self) -> list[dict]:
        return list(self._pending or [])

    def _events_at(self, frame: int) -> list[dict]:
        if self.action is None:
            return []
        return [e for e in self.action.events if int(e.get("frame", 0)) == frame]

    def stick_box(self, facing: int) -> tuple[float, float, float, float] | None:
        """返回当前位于根空间、并按 ``facing`` 镜像后的轴对齐包围盒。

        ``facing`` 沿用本项目约定（1 = 右，-1 = 左）；素材与存储的判定框均朝左，
        所以朝右的角色需要镜像 x 坐标。
        """
        if self.action is None or self.finished:
            return None
        box = self.action.stick_at(self.frame)
        if box is None:
            return None
        x0, x1, y0, y1 = box
        if facing > 0:
            x0, x1 = -x1, -x0
        return x0, x1, y0, y1
