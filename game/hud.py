"""战斗内左下角状态面板：负责各个元素的绘制与命中检测。

所有坐标都取自 ``analysis/hud/layout.json`` 记录的画面坐标：

* 笔触背景图 226x86，位于 (1,6)
* 头像 75x70，位于 (16,5)
* 底部水墨底图 322x141，位于 (0,486)
* 五个 45x45 技能框，中心 x=130.5/170.5/210.5/250.45/290.45，y=552.95
* 无双值下划线 221x8，位于 (100,571)；无双印 68x68，位于 (40,517)
* 血、蓝、经验条 143x11，分别位于 (85,20)/(87,39)/(87,59)
* 背包 (18,495,34,45)、技能 (54,479,46,45)、设置 (3,540,34,45)

这些切图由 ``tools/build_hud_assets.py`` 生成。早期烘焙好的整张面板贴图不在
这里使用：那张图上血条已经是画满的状态，头像也固定为悟空，唐僧和实时变化的
数值都没法正确显示。
"""
from __future__ import annotations

import math

import pygame

from .assets import Assets

# ---------------------------------------------------------------- 布局
BG_POS = (1, 6)
HEAD_POS = (16, 5)
INK_POS = (0, 486)

SKILL_CENTERS = ((130.5, 552.95), (170.5, 552.95), (210.5, 552.95),
                 (250.45, 552.95), (290.45, 552.95))
SKILL_SIZE = 45

WSMC_POS = (100, 571)
WSMC_SIZE = (221, 8)
SEAL_IDLE_POS = (40, 517)
# 148x148 的贴图 = 68 px 无双印 + 四周各 40 px 光晕留边。
SEAL_READY_POS = (0, 477)
SEAL_SIZE = 68

HP_BAR = pygame.Rect(85, 20, 143, 11)
MP_BAR = pygame.Rect(87, 39, 143, 11)
EXP_BAR = pygame.Rect(87, 59, 143, 11)
HP_TEXT = pygame.Rect(114, 18, 71, 16)
MP_TEXT = pygame.Rect(114, 38, 71, 16)
EXP_TEXT = pygame.Rect(114, 57, 71, 16)
LEVEL_TEXT = pygame.Rect(5, 63, 27, 23)
LABELS_POS = (93, 22)

# 命中矩形按按钮造型的实际边界取值，而不是可见文字的包围盒。
BUTTON_RECTS = {
    "bag": pygame.Rect(18, 495, 34, 45),
    "study": pygame.Rect(54, 479, 46, 45),
    "set": pygame.Rect(3, 540, 34, 45),
}
_BUTTON_IMAGE = {"bag": "bag", "study": "skill", "set": "set"}

_WUKONG_FRAMES = (2, 3, 4, 5, 6)
_TANGSENG_FRAMES = (7, 8, 9, 10, 11)


class RoleInfoHud:
    """绘制并命中检测单个玩家的左下角状态面板。"""

    def __init__(self, assets: Assets) -> None:
        self.assets = assets
        self._hover: str | None = None
        self.mouse_pos: tuple[int, int] | None = None
        self._seal_lit = False
        self._seal_t0 = 0

    # ------------------------------------------------------------- 辅助方法
    def _image(self, name: str) -> pygame.Surface:
        return self.assets.image(f"ui/hud/{name}")

    def hover_at(self, pos: tuple[int, int]) -> str | None:
        for name, rect in BUTTON_RECTS.items():
            if rect.collidepoint(pos):
                return name
        return None

    def hit_test(self, pos: tuple[int, int]) -> str | None:
        return self.hover_at(pos)

    def set_mouse_pos(self, pos: tuple[int, int]) -> None:
        """使用逻辑事件坐标；开启 pygame.SCALED 缩放时尤其重要。"""
        self.mouse_pos = (int(pos[0]), int(pos[1]))

    # ------------------------------------------------------------- 绘制
    def draw(self, surface: pygame.Surface, player) -> None:
        try:
            self._draw(surface, player)
        except (KeyError, FileNotFoundError):
            # 切图是生成产物；若某次检出缺少这些资源，
            # 游戏仍应可玩，而不是每帧都崩溃。
            self._draw_fallback(surface, player)

    def _draw(self, surface: pygame.Surface, player) -> None:
        surface.blit(self._image("bg.png"), BG_POS)

        head = "head_tangseng.png" if player.hero == "tangseng" else "head_wukong.png"
        surface.blit(self._image(head), HEAD_POS)
        surface.blit(self._image("ink.png"), INK_POS)

        # 技能框共五格，1 号贴图表示尚未学会的空槽。
        frames = _TANGSENG_FRAMES if player.hero == "tangseng" else _WUKONG_FRAMES
        for i, (cx, cy) in enumerate(SKILL_CENTERS):
            frame = frames[i] if player.learned(i) else 1
            img = self._image(f"skill_{frame:02d}.png")
            surface.blit(img, (round(cx - SKILL_SIZE / 2),
                              round(cy - SKILL_SIZE / 2)))

        # 无双值下划线：ws 为 0 或 1 时都显示 1 号贴图（不带橙色进度）。
        ws = max(0, min(100, int(round(getattr(player, "gxp", 0)))))
        surface.blit(self._image(f"wsmc_{max(1, ws):03d}.png"), WSMC_POS)

        # 无双印：只有累计值到达 100 时才点亮，
        # 同时在那一刻让光晕动画从自己的第 1 帧开始播放。
        now = pygame.time.get_ticks()
        if ws >= 100:
            if not self._seal_lit:
                self._seal_lit = True
                self._seal_t0 = now
            frame = 1 + int((now - self._seal_t0) * 24.0 / 1000.0) % 19
            surface.blit(self._image(f"seal_ready_{frame:02d}.png"),
                         SEAL_READY_POS)
        else:
            if self._seal_lit:
                self._seal_lit = False
                self._seal_t0 = 0
            surface.blit(self._image("seal_idle.png"), SEAL_IDLE_POS)

        # 血条、蓝条、经验条各有 101 帧进度贴图。
        self._draw_bar(surface, "hp", HP_BAR, player.hp, player.max_hp)
        self._draw_bar(surface, "mp", MP_BAR, player.mp, player.max_mp)
        self._draw_bar(surface, "exp", EXP_BAR, player.exp, player.exp_need)

        # 数值文本按文本框自身的中心对齐，而不是按血条的中心对齐，
        # 否则数字会整体偏移。
        self._text(surface, f"{int(player.hp)}/{int(player.max_hp)}",
                   HP_TEXT.center, 10)
        self._text(surface, f"{int(player.mp)}/{int(player.max_mp)}",
                   MP_TEXT.center, 10)
        self._text(surface, f"{int(player.exp)}/{int(player.exp_need)}",
                   EXP_TEXT.center, 10)
        self._text(surface, str(player.level_no), LEVEL_TEXT.center, 16)

        # 血、蓝、经验的文字标签，叠在进度条上方。
        surface.blit(self._image("labels.png"), LABELS_POS)

        # 三个按钮只做普通态和悬停态两种贴图。
        mouse = self.mouse_pos if self.mouse_pos is not None else pygame.mouse.get_pos()
        self._hover = self.hover_at(mouse)
        for name, rect in BUTTON_RECTS.items():
            state = "over" if self._hover == name else "up"
            img = self._image(f"btn_{_BUTTON_IMAGE[name]}_{state}.png")
            surface.blit(img, rect.topleft)

    def _draw_bar(self, surface: pygame.Surface, key: str, rect: pygame.Rect,
                  value: float, maximum: float) -> None:
        ratio = 0.0 if maximum <= 0 else float(value) / float(maximum)
        ratio = max(0.0, min(1.0, ratio))
        # 按四舍五入（0.5 向上进位）算出比例，再映射到 1..101 号贴图。
        frame = int(math.floor(100.0 * (1.0 - ratio) + 0.5)) + 1
        frame = max(1, min(101, frame))
        surface.blit(self._image(f"bar_{key}_{frame:03d}.png"), rect.topleft)

    def _text(self, surface: pygame.Surface, text: str, center: tuple[int, int],
              size: int) -> None:
        font = self.assets.font(size)
        img = font.render(text, True, (255, 255, 255))
        shadow = font.render(text, True, (0, 0, 0))
        x = round(center[0] - img.get_width() / 2)
        y = round(center[1] - img.get_height() / 2)
        surface.blit(shadow, (x + 1, y + 1))
        surface.blit(img, (x, y))

    # ------------------------------------------------------------- 兜底方案
    def _draw_fallback(self, surface: pygame.Surface, player) -> None:
        """生成好的切图缺失时使用的简易线框兜底。"""
        panel = pygame.Surface((322, 110), pygame.SRCALPHA)
        pygame.draw.rect(panel, (10, 10, 10, 210), panel.get_rect(),
                         border_radius=10)
        surface.blit(panel, (0, 480))
        for i, (key, rect) in enumerate((
                ("hp", HP_BAR), ("mp", MP_BAR), ("exp", EXP_BAR))):
            value, maximum = ((player.hp, player.max_hp),
                              (player.mp, player.max_mp),
                              (player.exp, player.exp_need))[i]
            ratio = 0.0 if maximum <= 0 else max(0.0, min(1.0, value / maximum))
            color = ((210, 40, 40), (40, 120, 220), (235, 170, 30))[i]
            pygame.draw.rect(surface, (30, 30, 30), rect)
            pygame.draw.rect(surface, color,
                             (rect.x, rect.y, int(rect.w * ratio), rect.h))
        for name, rect in BUTTON_RECTS.items():
            pygame.draw.rect(surface, (85, 70, 45), rect, border_radius=4)
            pygame.draw.rect(surface, (210, 180, 100), rect, 1, border_radius=4)
        gxp = max(0, min(100, int(round(getattr(player, "gxp", 0)))))
        pygame.draw.rect(surface, (35, 30, 25), (100, 575, 221, 5))
        pygame.draw.rect(surface, (240, 150, 30),
                         (100, 575, int(221 * gxp / 100), 5))
        self._text(surface, f"Lv.{player.level_no}", LEVEL_TEXT.center, 16)


# 兼容用的薄封装：供仍以 Assets 对象调用 ``ui.draw_hud`` 的旧代码使用，
# 世界对象本身并不使用它（世界对象常驻持有一个面板实例），
# 因此这里只作兜底，不参与每帧逻辑。
_DEFAULT: RoleInfoHud | None = None


def draw_roleinfo(surface: pygame.Surface, assets: Assets, player) -> None:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = RoleInfoHud(assets)
    _DEFAULT.draw(surface, player)
