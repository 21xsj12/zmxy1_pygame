"""新游戏开始前的英雄选择场景（四张卷轴卡片）。

四张 236x597 的卡片横向排开，左上角 x 依次为 0、235、470、705，y 均为 13；
前两张是可点击的按钮卡（孙悟空、唐僧），后两张是未解锁的装饰图。
默认显示灰色态；鼠标移入后换成彩色态，并叠加“1P”/“2P”角标。
角标为 84x84 的“1P”与 83x45 的“2P”，绘制在 (button_x-30, 40) 处。
已点选的卡片保持彩色且角标常驻，因为点选后不再监听移出事件。
"""
from __future__ import annotations

import pygame

from .menu_data import load_select


class HeroSelectScene:
    def __init__(self, app, play_num: int, game_mode: int):
        self.app = app
        self.data = load_select()
        self.play_num = int(play_num)
        self.game_mode = int(game_mode)
        self.cur = 1
        self.picked = []
        self.dead: set[str] = set()
        self.hover = None
        self.pressed = None
        self.key_index = 0

    # ------------------------------------------------------------------ 数据
    @property
    def cards(self):
        return self.data.cards

    def selectable(self) -> list:
        return [c for c in self.cards
                if c.selectable and c.key not in self.dead]

    def picked_keys(self) -> set[str]:
        return {c.key for c in self.picked}

    def first_selectable(self):
        items = self.selectable()
        return items[0] if items else None

    # --------------------------------------------------------------- 几何
    def hit(self, mx: int, my: int):
        for card in reversed(self.cards):
            if not card.selectable or card.key in self.dead:
                continue
            rect = card.hit_rect()
            if rect and pygame.Rect(rect).collidepoint(mx, my):
                return card
        return None

    # ------------------------------------------------------------------ 输入
    def handle_mouse(self, event: pygame.event.Event) -> bool:
        if event.type == pygame.MOUSEMOTION:
            self.hover = self.hit(*event.pos)
            return True
        if event.type == pygame.MOUSEBUTTONDOWN and getattr(event, "button", 1) == 1:
            self.hover = self.hit(*event.pos)
            self.pressed = self.hover
            return True
        if event.type == pygame.MOUSEBUTTONUP and getattr(event, "button", 1) == 1:
            target = self.hit(*event.pos)
            activated = target is not None and target is self.pressed
            self.pressed = None
            if activated:
                self.choose(target)
            return True
        return False

    def handle_key(self, event: pygame.event.Event) -> bool:
        if event.type != pygame.KEYDOWN:
            return False
        items = self.selectable()
        if event.key in (pygame.K_LEFT, pygame.K_a, pygame.K_RIGHT, pygame.K_d):
            if not items:
                return True
            delta = -1 if event.key in (pygame.K_LEFT, pygame.K_a) else 1
            if self.hover in items:
                self.key_index = (items.index(self.hover) + delta) % len(items)
            else:
                self.key_index = 0
            self.hover = items[self.key_index]
            return True
        if event.key in (pygame.K_RETURN, pygame.K_SPACE, pygame.K_j):
            if self.hover is None:
                self.hover = self.first_selectable()
            if self.hover is not None:
                self.choose(self.hover)
            return True
        # 选人画面不提供返回或 ESC 操作；这里也保持同样的行为。
        if event.key == pygame.K_ESCAPE:
            return True
        return False

    # ------------------------------------------------------------------ 逻辑
    def choose(self, card) -> None:
        if card is None or card.key in self.dead or not card.selectable:
            return
        self.picked.append(card)
        self.dead.add(card.key)
        self.hover = None
        if self.play_num == 1 or len(self.picked) == 2:
            self.finish()
        else:
            # 双人模式下不会自动预选第二张卡：剩余卡片保持灰色，
            # 直到 2P 移入或点击它才切换为高亮。
            self.cur = 2
            self.hover = None
            self.key_index = 0

    def finish(self) -> None:
        roles = [c.role for c in self.picked]
        self.app.finish_hero_select(roles, self.play_num, self.game_mode)

    # ------------------------------------------------------------------- 绘制
    def draw(self, surface: pygame.Surface) -> None:
        surface.fill((255, 255, 255))
        picked_keys = self.picked_keys()
        for card in self.cards:
            if card.selectable:
                colored = card is self.hover or card.key in picked_keys
                path = card.color_path if colored else card.gray_path
            else:
                # 未解锁卡片的父级滤镜把饱和度压到 0，
                # 因此它们始终显示为灰色。
                path = card.gray_path if card.gray_only else card.color_path
            surface.blit(self.app.assets.image(path), (card.x, card.y))

        tags: list[tuple[object, str]] = []
        for i, card in enumerate(self.picked):
            tags.append((card, "1P" if i == 0 else "2P"))
        if self.hover is not None:
            tags.append((self.hover, "1P" if self.cur == 1 else "2P"))
        drawn: set[tuple[str, str]] = set()
        for card, key in tags:
            ident = (card.key, key)
            if ident in drawn or card.button_x is None:
                continue
            drawn.add(ident)
            tag = self.data.tags[key]
            surface.blit(self.app.assets.image("menu/" + tag["png"]),
                         (round(card.button_x - 30), 40))

    # ------------------------------------------------------------------ 工具
    def state_summary(self) -> dict:
        return {
            "play_num": self.play_num,
            "game_mode": self.game_mode,
            "cur": self.cur,
            "picked": [c.key for c in self.picked],
            "dead": sorted(self.dead),
            "hover": self.hover.key if self.hover else None,
        }
