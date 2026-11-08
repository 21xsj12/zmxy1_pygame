"""屏幕顶部的 Boss 血条。

血条使用 100 帧序列，按血量百分比取整后查表显示：

* 第 1 帧为满血，第 100 帧为空血；
* 每次怪物逻辑步进都按 ``100 - round(100 * 当前血量 / 最大血量)`` 求帧号，
  所以显示是 1% 一档的离散跳变，而不是按比例连续缩放的矩形；
* 全局只保留一个血条槽：首次出现 Boss 时选定名字图案，之后只更新帧号。

100 张纯血条帧与 11 张名字图均按素材的实际像素边界重建，
绘制原点由素材内部偏移逐层累加得到。

血条一旦显示就常驻，直到界面整体重建才消失。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pygame

from .assets import Assets

if TYPE_CHECKING:                        # pragma: no cover - typing only
    from .entities import Monster

# 血条挂在全局界面坐标 (450, 50) 处，即屏幕顶部偏中的位置。
BOSS_BAR_ORIGIN = (450.0, 50.0)
# 血条图形相对血条槽的绘制原点：条形素材左上角位于 (-164.5, -14)。
BAR_ART_ORIGIN = (-164.5, -14.0)
# 名字容器偏移 (-186, 5) 再加上名字图像自身偏移 (-56.5, -23)，得到 (-242.5, -18)。
NAME_ART_ORIGIN = (-242.5, -18.0)
# pygame 只能按整数坐标绘制，而布局位置带半像素，因此统一四舍五入取整。
# 半像素取整结果：285.5 -> 286，207.5 -> 208。
BAR_BLIT = (286, 36)
NAME_BLIT = (208, 32)

BAR_FRAME_DIR = "ui/boss/bar_frames"
NAME_DIR = "ui/boss/names"

# 名字帧映射：Boss 类型编号 -> 名字图案序号；名字用字沿用手写异体写法，
# 例如彭魔王/鲛魔王/蝙蝠洞，而非常见的鹏/蛟/蝙蝠石像写法。
BOSS_NAME_FRAME: dict[int, int] = {
    3: 1, 4: 2, 6: 3, 21: 4, 11: 5, 9: 6, 17: 7,
    10: 8, 19: 9, 22: 10, 23: 10, 24: 10, 26: 11,
}

# 名字图案序号 -> 中文名字，供测试与工具核对映射关系。
NAME_LABELS = {
    1: "大猩猩", 2: "彌猴王", 3: "禺狨王", 4: "蝙蝠洞", 5: "狮驼王",
    6: "彭魔王", 7: "龟丞相", 8: "鲛魔王", 9: "鲨魔王", 10: "牛魔王",
    11: "龙王",
}


def boss_bar_frame(hp: float, max_hp: float) -> int:
    """根据当前血量返回血条帧号。

    怪物每帧逻辑步进按以下公式求值::

        frame = 100 - floor(100 * (当前血量 / 最大血量) + 0.5)

    这里按“正数逢 .5 进位”的规则取整；Python 内置 ``round`` 是银行家舍入，
    因此这里显式用 floor(x + 0.5) 实现同样的取整规则。

    第 1 帧同时吸收 frame=0（满血）的情况，第 100 帧吸收大于 100 的取值
    （血量为负），保证显示帧号始终被限制在 1 到 100 之间。
    """
    if max_hp <= 0:
        return 1
    if hp <= 0:
        return 100
    ratio = float(hp) / float(max_hp)
    if ratio >= 1.0:
        return 1
    remaining = math.floor(ratio * 100.0 + 0.5)
    damage = 100 - int(remaining)
    return max(1, min(100, damage))


@dataclass
class BossBar:
    """全局唯一的 Boss 血条槽；同一时刻最多显示一条血条。"""

    visible: bool = False
    name_frame: int | None = None
    frame: int = 1
    boss_type: int | None = None

    def reset(self) -> None:
        """切换关卡或重建世界后调用：界面对象重新创建，因此血条不存在。"""
        self.visible = False
        self.name_frame = None
        self.frame = 1
        self.boss_type = None

    def on_spawn(self, boss: "Monster") -> None:
        """Boss 首次步进时调用：显示血条并选定名字图案。

        同一关后续替换的 Boss（例如 4-1 里的 22 -> 23 -> 24）会再次调用，
        但名字图案沿用第一次选定的结果，只刷新血条帧号。
        """
        self.visible = True
        self.frame = 1
        if self.name_frame is None:
            self.boss_type = int(boss.type)
            self.name_frame = BOSS_NAME_FRAME.get(int(boss.type))

    def update(self, boss: "Monster | None") -> None:
        """每个 24 Hz 逻辑步进都按当前 Boss 血量刷新血条帧号。

        刷新发生在怪物步进之前，因此读到的是上一逻辑帧结束时的血量；
        ``World._step`` 在 ``monster.update()`` 之前调用本方法。
        已死亡但尚未移除的 Boss 仍显示第 100 帧；``removed`` 一旦置位，
        血条就冻结在当前帧，直到界面重建才消失。
        """
        if boss is None:
            return
        if not self.visible:
            self.on_spawn(boss)
        if self.name_frame is None:
            self.name_frame = BOSS_NAME_FRAME.get(int(boss.type))
        if getattr(boss, "removed", False):
            return
        self.frame = boss_bar_frame(getattr(boss, "hp", 0),
                                    getattr(boss, "max_hp", 1))

    def draw(self, surface: pygame.Surface, assets: Assets) -> None:
        """先画血条再画名字图案；名字覆盖血条的左端接缝。"""
        if not self.visible:
            return
        frame = max(1, min(100, int(self.frame)))
        try:
            bar = assets.image(f"{BAR_FRAME_DIR}/{frame:03d}.png")
        except (KeyError, FileNotFoundError):
            return
        surface.blit(bar, BAR_BLIT)
        if not self.name_frame:
            return
        try:
            name = assets.image(f"{NAME_DIR}/{int(self.name_frame):02d}.png")
        except (KeyError, FileNotFoundError):
            return
        surface.blit(name, NAME_BLIT)
