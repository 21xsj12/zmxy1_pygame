"""连击数显示。

每次有效命中都会累加同一个计数，并在画面坐标 (694.95, 234.95)
处绘制连击面板。数字由五帧动画组成：先以约 2.5 倍尺寸
带模糊显示，再收束到 1.0 倍的清晰画面，最后停在第 5 帧。
面板在 3.5 秒内按二次缓出曲线淡出；连击数达到 2 之后，
每次新的命中都会同时重启淡出计时和数字弹跳。

本模块把计数与显示分离，因此不依赖战斗也能单独测试。``World``
以 24 Hz 调用 ``logic_tick()``，每次有效命中调用 ``on_hit()``，
在真实渲染帧上调用 ``update()``，并在角色信息 HUD 之后调用
``draw()``。
"""
from __future__ import annotations

import pygame

# 连击面板素材在画面上的坐标（x、y、宽、高）。
ART_X = 694.95
ART_Y = 234.95
ART_W = 181.65
ART_H = 51.65

# 数字位布局：纵坐标偏移 28.3，字距 50，起始横坐标由位数决定。
DIGIT_Y = 28.3
SPACING = 50.0
X0_1 = 4.0
X0_2 = -44.6
X0_3PLUS = -95.6

# 数字素材是 315x315 画面中裁出的 200x200 区域，绘制时按中心对齐。
NUM_ANCHOR = 100.5

FADE_SECONDS = 3.5
ANIM_FPS = 24.0
POLL_FRAMES = 20


def _x0(count: int) -> float:
    if count <= 1:
        return X0_1
    if count == 2:
        return X0_2
    return X0_3PLUS


def _blit_alpha(dst: pygame.Surface, img: pygame.Surface,
                x: int, y: int, alpha: float) -> None:
    """按整体透明度绘制，同时保留图像自身的逐像素 alpha。"""
    a = max(0.0, min(1.0, alpha))
    if a >= 0.999:
        dst.blit(img, (x, y))
        return
    tmp = img.copy()
    # 填充色的 RGB 固定为 255，因此只有 A 通道会被缩放。
    # pygame 的直通 alpha 混合随后把未改动的 RGB 乘以 A，正好等价于整体透明度。
    # 不要连 RGB 一起缩放，否则半透明边缘会被压暗两次。
    # 这里刻意只缩放 A 通道：RGB 保持原值，边缘颜色才不会发灰。
    tmp.fill((255, 255, 255, int(round(255 * a))),
             special_flags=pygame.BLEND_RGBA_MULT)
    dst.blit(tmp, (x, y))


class ComboCounter:
    """连击计数与面板显隐的状态机。"""

    def __init__(self) -> None:
        self.count = 0
        self.pre_count = 0
        self.tick = 0
        self.panel_alive = False
        self.last_hit = -1e9
        self.digit_start = -1e9
        self.digits: list[int] = []

    # ------------------------------------------------------------- 计数
    def reset(self) -> None:
        """同时清空计数和面板；仅在一关重新开始时使用。"""
        self.count = 0
        self.pre_count = 0
        self.tick = 0
        self.panel_alive = False
        self.last_hit = -1e9
        self.digit_start = -1e9
        self.digits = []

    def logic_tick(self) -> None:
        """每个 24 Hz 逻辑帧调用一次，需早于怪物与英雄的更新。

        计数轮询每 20 帧进行一次；连续两次轮询结果相同就把连击数清零。
        若命中发生在第 h 帧，清零会延迟 21..40 帧（24 fps 下约
        0.875..1.667 秒）。
        """
        self.tick += 1
        if self.tick % POLL_FRAMES != 0:
            return
        if self.pre_count == self.count:
            self.count = 0
            self.pre_count = 0
        else:
            self.pre_count = self.count

    def on_hit(self, now: float) -> bool:
        """登记一次有效命中；面板需要显示时返回 True。"""
        self._expire(now)
        self.count += 1
        if self.count < 2:
            return False
        if not self.panel_alive:
            self.panel_alive = True
        self.last_hit = now
        self.digit_start = now
        self.digits = [int(ch) for ch in str(self.count)]
        return True

    # ---------------------------------------------------------- 生命周期
    def _expire(self, now: float) -> None:
        if self.panel_alive and now - self.last_hit >= FADE_SECONDS:
            self.panel_alive = False
            self.digits = []

    def update(self, now: float) -> None:
        """按真实时间推进淡出；在每个渲染帧调用都安全。"""
        self._expire(now)

    def alpha(self, now: float) -> float:
        if not self.panel_alive:
            return 0.0
        u = (now - self.last_hit) / FADE_SECONDS
        u = max(0.0, min(1.0, u))
        return (1.0 - u) ** 2

    def frame_index(self, now: float) -> int:
        """返回 0..4 的弹跳帧序号；第 4 帧（即第 5 帧画面）会保持不动。"""
        if not self.panel_alive:
            return 4
        age = max(0.0, now - self.digit_start)
        return min(4, int(age * ANIM_FPS + 1e-6))

    @property
    def visible(self) -> bool:
        return self.panel_alive and bool(self.digits)

    @property
    def value(self) -> int:
        """当前显示的数值；面板未显示时为 0。"""
        if not self.panel_alive or not self.digits:
            return 0
        value = 0
        for d in self.digits:
            value = value * 10 + d
        return value

    # ------------------------------------------------------------- 绘制
    def draw(self, surface: pygame.Surface, assets, now: float) -> None:
        """在画面固定坐标处绘制连击面板底图和当前数字。"""
        if not self.panel_alive or not self.digits:
            return
        alpha = self.alpha(now)
        if alpha <= 0.001:
            return
        try:
            brush = assets.image("ui/batter.png")
        except (KeyError, FileNotFoundError):
            brush = None
        if brush is not None:
            _blit_alpha(surface, brush, round(ART_X), round(ART_Y), alpha)

        x0 = _x0(len(self.digits))
        frame = self.frame_index(now) + 1
        for i, digit in enumerate(self.digits):
            digit = max(0, min(9, int(digit)))
            try:
                img = assets.image(f"ui/combo/num{digit}_{frame}.png")
            except (KeyError, FileNotFoundError):
                return
            cx = ART_X + x0 + SPACING * i
            cy = ART_Y + DIGIT_Y
            _blit_alpha(surface, img,
                        round(cx - NUM_ANCHOR), round(cy - NUM_ANCHOR), alpha)
