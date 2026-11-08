"""封面/标题场景的状态机与布局。

状态流转：

    MAIN  --单人或双人-->  MODE  --新游戏-->  HeroSelect
                                |--继续（1P）--> SLOT --> 地图
                                |--继续（2P）--> 地图
                                |--返回--> MAIN

按钮坐标与位图统一来自 menu_ui.json：主菜单七行从 x=751.15 开始排布；
次级菜单按钮先停在 x=1110 的画面外位置，进入模式或存档界面时才移到 x=800。
本地构建不包含第三方登录浮层。
"""
from __future__ import annotations

from enum import Enum

import pygame

from .menu_data import (DRAW_ORDER, MAIN_ITEMS, MODE_HIDDEN, SLOT_DOUBLE,
                        SLOT_TANGSENG, SLOT_WUKONG, load_menu)

X_OFFSTAGE = 1110.0
X_MENU = 751.15
X_SECONDARY = 800.0


class CoverState(str, Enum):
    MAIN = "main"
    MODE = "mode"
    SLOT = "slot"


class CoverScene:
    def __init__(self, app):
        self.app = app
        self.data = load_menu()
        self.state = CoverState.MAIN
        self.play_num = 1
        self.game_mode = 1
        self.pos: dict[str, list[float]] = {}
        self.hover: str | None = None
        self.pressed: str | None = None
        self.key_index = 0
        self.cadpa_open = False
        self.to_main()

    # ------------------------------------------------------------------ 状态
    def to_main(self) -> None:
        self.cadpa_open = False
        self.state = CoverState.MAIN
        self.layout()

    def has_save(self) -> bool:
        if self.play_num == 2 and not getattr(self.app, "double_campaign_ready", False):
            return False
        return self.app.has_save_for_mode(self.play_num)

    def layout(self) -> None:
        """按当前界面状态重新计算按钮坐标与显隐位置。"""
        pos = {name: [it.x, it.y] for name, it in self.data.items.items()}
        # 打开菜单时：待显示按钮先停在画面外 x=1110；新游戏按钮纵坐标复位到
        # y=314.35，存档位按钮恢复各自初始纵坐标。
        pos["newGame"][1] = 314.35
        if self.state is CoverState.MODE:
            for name in MODE_HIDDEN:
                pos[name][0] = X_OFFSTAGE
            pos["backbtn"] = [X_SECONDARY, 314.35]
            if self.has_save():
                pos["continueGame"] = [X_SECONDARY, 259.0]
                pos["newGame"] = [X_SECONDARY, 205.5]
            else:
                pos["newGame"] = [X_SECONDARY, 259.0]
                pos["continueGame"][0] = X_OFFSTAGE
        elif self.state is CoverState.SLOT:
            # 切到存档位界面时主菜单七行继续停在画面外，不再显示。
            for name in MODE_HIDDEN:
                pos[name][0] = X_OFFSTAGE
            pos["backbtn"] = [X_SECONDARY, 314.35]
            pos["continueGame"][0] = X_OFFSTAGE
            pos["newGame"] = [X_OFFSTAGE, 314.35]
            has_t = self.app.slot_exists(SLOT_TANGSENG)
            has_w = self.app.slot_exists(SLOT_WUKONG)
            if has_t and has_w:
                pos["wukong"] = [X_MENU, 195.0]
                pos["tangseng"] = [X_MENU, 248.8]
            elif has_w:
                pos["wukong"] = [X_MENU, 248.8]
            elif has_t:
                pos["tangseng"] = [X_MENU, 248.8]
        self.pos = pos
        self.hover = None
        self.pressed = None
        self.key_index = -1

    # --------------------------------------------------------------- 几何
    def _visible(self, name: str) -> bool:
        x, y = self.pos.get(name, (X_OFFSTAGE, 0))
        if x >= self.data.stage_w or y >= self.data.stage_h:
            return False
        if name == "btnGoBack" and not self.app.air_mode:
            return False
        return True

    def interactive(self) -> list[str]:
        if self.state is CoverState.MAIN:
            order = [n for n in MAIN_ITEMS if self._visible(n)]
            if self._visible("btnCadpa"):
                order.append("btnCadpa")
            if self._visible("btnGoBack"):
                order.append("btnGoBack")
        elif self.state is CoverState.MODE:
            order = [n for n in ("newGame", "continueGame", "backbtn")
                     if self._visible(n)]
        else:
            order = [n for n in ("wukong", "tangseng", "backbtn")
                     if self._visible(n)]
        return order

    def first_interactive(self) -> str | None:
        items = self.interactive()
        return items[0] if items else None

    def hit(self, mx: int, my: int) -> str | None:
        if self.cadpa_open:
            return None
        for name in reversed(DRAW_ORDER):
            if name not in self.pos or not self._visible(name):
                continue
            if name not in self.interactive():
                continue
            x, y = self.pos[name]
            rect = self.data.items[name].hit_rect(x, y)
            if pygame.Rect(rect).collidepoint(mx, my):
                return name
        return None

    # ------------------------------------------------------------------ 输入
    def handle_mouse(self, event: pygame.event.Event) -> bool:
        if self.cadpa_open:
            if event.type == pygame.MOUSEBUTTONUP and getattr(event, "button", 1) == 1:
                self.cadpa_open = False
            return True
        if event.type == pygame.MOUSEMOTION:
            self.hover = self.hit(*event.pos)
            return True
        if event.type == pygame.MOUSEBUTTONDOWN and getattr(event, "button", 1) == 1:
            self.hover = self.hit(*event.pos)
            self.pressed = self.hover
            return True
        if event.type == pygame.MOUSEBUTTONUP and getattr(event, "button", 1) == 1:
            target = self.hit(*event.pos)
            activated = target is not None and target == self.pressed
            self.pressed = None
            if activated:
                self.activate(target)
            return True
        return False

    def handle_key(self, event: pygame.event.Event) -> bool:
        if event.type != pygame.KEYDOWN:
            return False
        if self.cadpa_open:
            if event.key in (pygame.K_ESCAPE, pygame.K_RETURN, pygame.K_SPACE):
                self.cadpa_open = False
            return True
        items = self.interactive()
        if event.key in (pygame.K_UP, pygame.K_w):
            self._step(-1, items)
            return True
        if event.key in (pygame.K_DOWN, pygame.K_s):
            self._step(1, items)
            return True
        if event.key in (pygame.K_RETURN, pygame.K_SPACE, pygame.K_j):
            if self.hover is None:
                self.hover = self.first_interactive()
            if self.hover:
                self.activate(self.hover)
            return True
        if event.key == pygame.K_F1:
            self.app.to_settings()
            return True
        if event.key == pygame.K_ESCAPE:
            if self.state is not CoverState.MAIN:
                self.app.assets.play("SD_xz", 0.5)
                self.to_main()
            else:
                self.app._quit()
            return True
        return False

    def _step(self, delta: int, items: list[str]) -> None:
        if not items:
            return
        if self.hover not in items:
            self.hover = items[0]
            self.key_index = 0
            return
        self.key_index = (items.index(self.hover) + delta) % len(items)
        self.hover = items[self.key_index]

    def activate(self, name: str | None) -> None:
        if not name:
            return
        if name in ("simpleGame", "doubleGame"):
            self.app.assets.play("SD_xz", 0.5)
            self.play_num = 1 if name == "simpleGame" else 2
            self.game_mode = 1 if name == "simpleGame" else 2
            self.state = CoverState.MODE
            self.layout()
            return
        if name == "newGame":
            self.app.assets.play("SD_xz", 0.5)
            self.app.start_hero_select(self.play_num, self.game_mode)
            return
        if name == "continueGame":
            self.app.assets.play("SD_xz", 0.5)
            if self.play_num == 1:
                self.state = CoverState.SLOT
                self.layout()
            else:
                self.app.continue_from_slot(SLOT_DOUBLE, 2)
            return
        if name == "backbtn":
            self.app.assets.play("SD_xz", 0.5)
            self.to_main()
            return
        if name == "tangseng":
            self.app.assets.play("SD_xz", 0.5)
            self.app.continue_from_slot(SLOT_TANGSENG, 1)
            return
        if name == "wukong":
            self.app.assets.play("SD_xz", 0.5)
            self.app.continue_from_slot(SLOT_WUKONG, 1)
            return
        if name == "gameHelp":
            self.app.assets.play("SD_xz", 0.5)
            self.app.to_help("title")
            return
        if name == "aboutUs":
            self.app.assets.play("SD_xz", 0.5)
            self.app.to_about()
            return
        if name in ("gameShop", "successRoom"):
            # 商店与成功房间尚未接入，只弹提示，不播放按钮音效。
            self.app.toast("暂未开放，敬请期待")
            return
        if name == "gameRecommend":
            # 游戏推荐按钮没有绑定点击行为，按下后不产生任何动作。
            return
        if name == "btnCadpa":
            # 防沉迷提示：全屏静态公告页，点击关闭按钮或按任意键退出，
            # 打开时不播放按钮音效。
            self.cadpa_open = True
            return
        if name == "btnGoBack":
            self.app.toast("返回首页仅在原版 Air 版可用")
            return

    # ------------------------------------------------------------------- 绘制
    def draw(self, surface: pygame.Surface) -> None:
        md = self.data
        surface.blit(self.app.assets.image("menu/" + md.background),
                     md.background_pos)
        for name in DRAW_ORDER:
            if name not in self.pos or not self._visible(name):
                continue
            item = md.items[name]
            x, y = self.pos[name]
            state = "Down" if self.pressed == name else (
                "Over" if self.hover == name else "Up")
            if state not in item.states:
                state = "Up"
            png = self.app.assets.image(item.png(state))
            surface.blit(png, item.state_pos(state, x, y))
        vt = md.version_text
        if vt:
            color = vt.get("color", "#0fd0f3").lstrip("#")
            rgb = tuple(int(color[i:i + 2], 16) for i in (0, 2, 4))
            img = self.app.assets.font(int(vt.get("size", 18)), system=True).render(
                vt.get("text", ""), True, rgb)
            surface.blit(img, (round(vt.get("x", 0)), round(vt.get("y", 0))))
        if self.cadpa_open:
            surface.blit(self.app.assets.image("menu/cadpa.png"), (0, 0))
