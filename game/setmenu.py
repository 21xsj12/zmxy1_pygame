"""战斗内设置菜单：以浮层形式覆盖在战斗画面上，而不是独立场景。

浮层由一个 940x590 的透明命中区、一张 326x341 的黑色底图和七个
简单按钮组成；命中区不参与绘制，底图与按钮状态图按固定位置叠放。
浮层的显隐只改变可见性与交互状态，不会切换或重建战斗场景。

下列坐标取自界面资源的探测数据：命中矩形是按素材内浮点坐标换算
得到的像素值，绘制位置则是导出图片画布的左上角原点；两者分开记录，
是为了在按钮图与命中区存在细微偏移时仍能按视觉位置精确响应。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import pygame

from .assets import Assets

PANEL_POS = (307, 74)
PANEL_KEY = "ui/setmenu/panel.png"

# 五个文字选项，按界面自上而下的顺序排列。关闭按钮 X 不参与
# 键盘导航；键盘上与关闭等价的按键是 Esc。
SELECT_ORDER = ("continue", "sound", "back_map", "help", "back_menu")


@dataclass(frozen=True)
class MenuButton:
    key: str
    action: str
    hit: pygame.FRect
    draw_at: tuple[int, int]
    up: str
    over: str
    down: str


BUTTONS = (
    MenuButton(
        "x", "close",
        pygame.FRect(577.95, 85.95, 40.0, 40.0), (578, 84),
        "ui/setmenu/btn_x_up.png",
        "ui/setmenu/btn_x_over.png",
        "ui/setmenu/btn_x_down.png",
    ),
    MenuButton(
        "continue", "close",
        pygame.FRect(403.65, 132.0, 124.0, 42.0), (404, 132),
        "ui/setmenu/btn_continue_up.png",
        "ui/setmenu/btn_continue_over.png",
        "ui/setmenu/btn_continue_down.png",
    ),
    MenuButton(
        "back_map", "map",
        pygame.FRect(411.5, 235.95, 105.0, 34.0), (412, 236),
        "ui/setmenu/btn_back_map_up.png",
        "ui/setmenu/btn_back_map_over.png",
        "ui/setmenu/btn_back_map_down.png",
    ),
    MenuButton(
        "help", "help",
        pygame.FRect(411.35, 289.7, 105.0, 31.0), (411, 290),
        "ui/setmenu/btn_help_up.png",
        "ui/setmenu/btn_help_over.png",
        "ui/setmenu/btn_help_down.png",
    ),
    MenuButton(
        "back_menu", "menu",
        pygame.FRect(397.6, 340.15, 132.0, 32.0), (398, 340),
        "ui/setmenu/btn_back_menu_up.png",
        "ui/setmenu/btn_back_menu_over.png",
        "ui/setmenu/btn_back_menu_down.png",
    ),
)

SOUND_OPEN = MenuButton(
    "sound", "sound",
    pygame.FRect(409.65, 182.65, 111.0, 37.0), (410, 183),
    "ui/setmenu/btn_sound_open_up.png",
    "ui/setmenu/btn_sound_open_over.png",
    "ui/setmenu/btn_sound_open_down.png",
)
SOUND_CLOSE = MenuButton(
    "sound", "sound",
    pygame.FRect(414.85, 192.2, 97.65, 23.6), (415, 190),
    "ui/setmenu/btn_sound_close_up.png",
    "ui/setmenu/btn_sound_close_over.png",
    "ui/setmenu/btn_sound_close_down.png",
)


class SetMenu:
    """以鼠标操作为主的暂停与设置浮层。

    ``is_muted`` 用可调用对象而不是复制一份布尔值传入，这样
    关闭/开启的可见标签始终跟随 ``Assets.muted`` 的当前状态。
    """

    def __init__(self, assets: Assets, is_muted: Callable[[], bool]) -> None:
        self.assets = assets
        self.is_muted = is_muted
        self.active = False
        self.hover: str | None = None
        self.pressed: str | None = None
        self.selected: int | None = None

    # ------------------------------------------------------------- 生命周期
    def open(self) -> None:
        if self.active:
            return
        self.active = True
        self.hover = None
        self.pressed = None
        self.selected = None

    def close(self) -> None:
        self.active = False
        self.hover = None
        self.pressed = None
        self.selected = None

    # ---------------------------------------------------------------- 布局
    @property
    def buttons(self) -> tuple[MenuButton, ...]:
        sound = SOUND_OPEN if self.is_muted() else SOUND_CLOSE
        return BUTTONS + (sound,)

    def button_at(self, pos: tuple[int, int] | tuple[float, float]) -> MenuButton | None:
        for button in self.buttons:
            if button.hit.collidepoint(pos):
                return button
        return None

    def hit_test(self, pos: tuple[int, int]) -> str | None:
        button = self.button_at(pos)
        return button.key if button is not None else None

    def refresh_hover(self, pos: tuple[int, int]) -> None:
        button = self.button_at(pos)
        self.hover = button.key if button is not None else None
        # 鼠标移动时从键盘焦点切回鼠标焦点；
        # 否则残留的键盘选中项会让另一行也一直显示高亮。
        self.selected = None

    # ---------------------------------------------------------------- 事件
    def handle(self, event: pygame.event.Event) -> str | None:
        """处理一次鼠标事件；完成点击时返回动作名。"""
        if not self.active:
            return None
        if event.type == pygame.MOUSEMOTION:
            self.refresh_hover(event.pos)
        elif event.type == pygame.MOUSEBUTTONDOWN and getattr(event, "button", 1) == 1:
            button = self.button_at(event.pos)
            self.pressed = button.key if button is not None else None
        elif event.type == pygame.MOUSEBUTTONUP and getattr(event, "button", 1) == 1:
            button = self.button_at(event.pos)
            pressed, self.pressed = self.pressed, None
            if button is not None and button.key == pressed:
                return button.action
        return None

    def handle_key(self, event: pygame.event.Event) -> str | None:
        """可选的键盘导航（本实现额外提供的操作方式）。"""
        if not self.active or event.type != pygame.KEYDOWN:
            return None
        if event.key == pygame.K_ESCAPE:
            return "close"
        if event.key in (pygame.K_UP, pygame.K_w):
            self._move_selection(-1)
        elif event.key in (pygame.K_DOWN, pygame.K_s):
            self._move_selection(1)
        elif event.key in (pygame.K_RETURN, pygame.K_j, pygame.K_SPACE):
            # 键盘导航是本实现新增的可选操作。没有显式选中项时，
            # 回车直接执行关闭，与“继续”按钮的动作一致。
            if self.selected is None:
                return "close"
            return self._activate_selection()
        return None

    def _move_selection(self, delta: int) -> None:
        count = len(SELECT_ORDER)
        if self.selected is None:
            self.selected = 0 if delta >= 0 else count - 1
        else:
            self.selected = (self.selected + delta) % count
        self.hover = None

    def _activate_selection(self) -> str | None:
        if self.selected is None:
            return None
        key = SELECT_ORDER[self.selected]
        if key == "sound":
            return "sound"
        for button in BUTTONS:
            if button.key == key:
                return button.action
        return None

    # ------------------------------------------------------------------ 绘制
    def draw(self, surface: pygame.Surface) -> None:
        if not self.active:
            return
        surface.blit(self.assets.image(PANEL_KEY), PANEL_POS)
        for button in self.buttons:
            focused = self.hover == button.key
            if self.selected is not None:
                focused = focused or SELECT_ORDER[self.selected] == button.key
            state = "down" if self.pressed == button.key else "over" if focused else "up"
            image = {"up": button.up, "over": button.over, "down": button.down}[state]
            surface.blit(self.assets.image(image), button.draw_at)
