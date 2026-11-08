"""学习技能页面：整页背景、英雄页签、技能表格与技能购买交互。

页面按 940x590 的坐标布局绘制，各区域职责如下：

    整页背景、英雄页签与返回按钮
    表格主体与底部页签
    技能内容与技能格（这里改为按技能数据实时绘制）
    被动技能行

坐标取自项目的页面布局整理文档，
以及技能分析数据。
"""
from __future__ import annotations

import pygame

from .save import write_save
from .skills import (PASSIVE_MAX, SKILLS, active_cost, active_key, normalize,
                     passive_effect_text, passive_name, passive_next_cost,
                     try_buy_active, try_upgrade_passive)

WIDTH = 940
HEIGHT = 590

# 表格区域按 746x429 的布局栅格精确取值，各项坐标见技能布局分析数据。
TABLE = pygame.Rect(100, 85, 746, 429)
HEADER_H = 34
ROW_Y = [119, 195, 275, 356, 436, 514]
ROW_CENTER = [157.0, 235.0, 315.5, 396.0, 475.0]
ACTIVE_COLS = [100, 180, 265, 349, 434, 846]
PASSIVE_COLS = [100, 180, 265, 447, 635, 741, 846]

HERO_TABS = {
    "wukong": pygame.Rect(89, 7, 60, 34),
    "tangseng": pygame.Rect(228, 6, 61, 35),
}
BACK_RECT = pygame.Rect(854, 15, 58, 26)
TAB_ACTIVE = pygame.Rect(100, 551, 103, 26)
# 两个页签的文字要保持同一基线。被动页签的按钮框比主动页签高 4 像素，
# 这是按钮素材本身的偏差，这里让可见文字对齐，而不去保留那处误偏移。
# 这样切换页签时，两行文字不会上下跳动。
TAB_PASSIVE = pygame.Rect(208, 551, 112, 26)
BOTTOM_TAB_Y = 564
TAB_ART_SCALE = 0.94

SOUL_ICON = (714, 509)
SOUL_TEXT = (797, 548)

ICON_X = 200
ICON_SIZE = 65
UPGRADE_RECTS = [pygame.Rect(646, int(round(c)) - 17, 84, 34) for c in ROW_CENTER]
ICON_RECTS = [pygame.Rect(ICON_X, int(round(c - ICON_SIZE / 2)),
                          ICON_SIZE, ICON_SIZE) for c in ROW_CENTER]

INK = (245, 243, 233)
INK_DIM = (190, 186, 175)
GOLD = (240, 192, 68)
GOLD_HI = (255, 224, 120)
BLACK = (0, 0, 0)
PANEL_TINT = (245, 200, 80, 24)


class SkillPage:
    """单人技能页面，沿用既定布局与技能规则。

    当前存档每份只对应一名英雄，因此第二个英雄页签固定绘制为禁用状态，
    与单人存档的可用范围一致。页面把英雄保存在字段里，日后若支持双人
    战役，可以传入另一份上下文，绘制代码无需改动；
    这样页面的绘制逻辑与存档结构保持解耦。
    """

    def __init__(self, app, back: str = "map"):
        self.app = app
        self.back = back
        data = app.save_data or {}
        hero = str(data.get("hero") or "wukong")
        state = (data.get("player") or {}).get("skills")
        battle = getattr(app, "battle", None)
        battle_player = getattr(battle, "player", None) if battle is not None else None
        if back == "battle" and battle_player is not None:
            hero = str(getattr(battle_player, "hero", hero) or hero)
            state = getattr(battle_player, "skills", state)
        if hero not in SKILLS:
            hero = "wukong"
        self.hero = hero
        self.state = normalize(hero, state)
        self.soul = max(0, int(data.get("soul", 0)))
        self.tab = 0                 # 0 表示主动技能页，1 表示被动技能页
        self.selected = 0
        self.hover: tuple[str, int] | None = None
        self.msg = ""
        self.msg_t = 0.0
        self.keyboard_mode = False
        self.time = 0.0
        self.dirty = False
        self._page_open = True

    # ------------------------------------------------------------------ 数据
    @property
    def active(self) -> list[dict]:
        return SKILLS[self.hero]["active"]

    @property
    def passive(self) -> list[dict]:
        return SKILLS[self.hero]["passive"]

    @property
    def hero_cn(self) -> str:
        return "孙悟空" if self.hero == "wukong" else "唐僧"

    @property
    def icon_prefix(self) -> str:
        return "w" if self.hero == "wukong" else "t"

    def _sync_save(self) -> None:
        data = self.app.save_data
        if data is None:
            return
        data["hero"] = self.hero
        data["soul"] = int(self.soul)
        player = data.setdefault("player", {})
        player["hero"] = self.hero
        player["skills"] = normalize(self.hero, self.state)
        write_save(data, slot=self.app.slot)
        self.app.has_save = True

    def _sync_battle(self) -> None:
        battle = getattr(self.app, "battle", None)
        player = getattr(battle, "player", None) if battle is not None else None
        if player is not None and getattr(player, "hero", None) == self.hero:
            player.skills = self.state
            player.recalc_stats()

    def _commit(self) -> None:
        # 购买成功后立即写盘，这样退出页面或意外中断都不会丢失已学的技能。
        # 失败、已学会或已满级的点击不会把 dirty 置位，因此不会触发保存。
        # 只在状态真正变化时落盘，避免无意义的磁盘写入。
        if not self.dirty:
            return
        self._sync_save()
        self._sync_battle()
        self.dirty = False

    def _set_msg(self, text: str, ttl: float = 1.6) -> None:
        self.msg = text
        self.msg_t = ttl

    # -------------------------------------------------------------- 购买处理
    def learn(self, index: int) -> bool:
        result = try_buy_active(self.hero, self.state, self.soul, index)
        if not result.ok:
            if result.reason == "learned":
                self._set_msg("已学会")
            elif result.reason == "poor":
                self._set_msg("灵魂不足")
            return False
        self.soul -= result.cost
        self.dirty = True
        self._commit()
        self._set_msg("学习成功")
        return True

    def upgrade(self, index: int) -> bool:
        result = try_upgrade_passive(self.hero, self.state, self.soul, index)
        if not result.ok:
            if result.reason == "max":
                self._set_msg("已满级")
            elif result.reason == "poor":
                self._set_msg("灵魂不足")
            return False
        self.soul -= result.cost
        self.dirty = True
        self._commit()
        self._set_msg("升级成功")
        return True

    # ------------------------------------------------------------------ 输入
    def _hit(self, pos) -> tuple[str, int] | None:
        x, y = pos
        if BACK_RECT.inflate(12, 12).collidepoint(x, y):
            return ("back", -1)
        if TAB_ACTIVE.collidepoint(x, y):
            return ("tab", 0)
        if TAB_PASSIVE.collidepoint(x, y):
            return ("tab", 1)
        if self.tab == 0:
            for i, rect in enumerate(ICON_RECTS):
                if rect.collidepoint(x, y):
                    return ("skill", i)
        else:
            for i, rect in enumerate(UPGRADE_RECTS):
                if rect.collidepoint(x, y) and self.state["passive"][i] < PASSIVE_MAX:
                    return ("upgrade", i)
        return None

    def _set_cursor(self, hand: bool) -> None:
        try:
            self.app._set_hand_cursor(hand)
        except Exception:
            try:
                pygame.mouse.set_cursor(
                    pygame.SYSTEM_CURSOR_HAND if hand else pygame.SYSTEM_CURSOR_ARROW)
            except Exception:
                pass

    def handle_mouse(self, event: pygame.event.Event) -> bool:
        if event.type == pygame.MOUSEMOTION:
            self.keyboard_mode = False
            self.hover = self._hit(event.pos)
            self._set_cursor(self.hover is not None)
            return True
        if event.type == pygame.MOUSEBUTTONDOWN and getattr(event, "button", 1) == 1:
            self.keyboard_mode = False
            hit = self._hit(event.pos)
            if hit is None:
                return False
            self._activate(*hit)
            return True
        return False

    def handle_key(self, event: pygame.event.Event) -> bool:
        if event.type != pygame.KEYDOWN:
            return False
        self.keyboard_mode = True
        self.hover = None
        self._set_cursor(False)
        if event.key == pygame.K_ESCAPE:
            self.close()
            return True
        if event.key == pygame.K_v and self.back == "battle":
            self.close()
            return True
        if event.key in (pygame.K_TAB, pygame.K_LEFT, pygame.K_RIGHT):
            self.tab = 1 - self.tab
            self.selected = 0
            return True
        if event.key in (pygame.K_UP, pygame.K_w):
            self.selected = (self.selected - 1) % 5
            return True
        if event.key in (pygame.K_DOWN, pygame.K_s):
            self.selected = (self.selected + 1) % 5
            return True
        if event.key in (pygame.K_RETURN, pygame.K_j, pygame.K_SPACE):
            if self.tab == 0:
                self.learn(self.selected)
            else:
                self.upgrade(self.selected)
            return True
        return False

    def _activate(self, kind: str, index: int) -> None:
        if kind == "back":
            self.close()
        elif kind == "tab":
            self.tab = int(index)
            self.selected = 0
        elif kind == "skill":
            self.selected = int(index)
            self.learn(int(index))
        elif kind == "upgrade":
            self.selected = int(index)
            self.upgrade(int(index))

    def update(self, dt: float) -> None:
        self.time += dt
        if self.msg_t > 0:
            self.msg_t = max(0.0, self.msg_t - dt)
            if self.msg_t <= 0:
                self.msg = ""

    def close(self) -> None:
        if not self._page_open:
            return
        self._page_open = False
        self._commit()
        self._set_cursor(False)
        self.app.skill_page = None
        if self.back == "battle" and getattr(self.app, "battle", None) is not None:
            self.app.state = "battle"
            self.app.paused = False
            self.app.menu_back = "battle"
        else:
            self.app.to_map()

    # --------------------------------------------------------------- 绘制
    def _font(self, text: str, size: int, bold: bool = False) -> pygame.font.Font:
        return self.app.assets.font_for(str(text), size, bold)

    def _label(self, surface, text, pos, size=14, bold=False, color=INK,
               anchor="midleft", outline=True) -> None:
        text = str(text)
        if not text:
            return
        font = self._font(text, size, bold)
        if outline:
            for ox, oy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                img = font.render(text, True, BLACK)
                rect = img.get_rect()
                setattr(rect, anchor, (pos[0] + ox, pos[1] + oy))
                surface.blit(img, rect)
        img = font.render(text, True, color)
        rect = img.get_rect()
        setattr(rect, anchor, pos)
        surface.blit(img, rect)

    def _wrap(self, text: str, max_width: int, size: int, bold: bool = False) -> list[str]:
        text = str(text).replace("\r", "")
        font = self._font(text.replace("\n", ""), size, bold)
        lines: list[str] = []
        for paragraph in text.split("\n"):
            line = ""
            for ch in paragraph:
                candidate = line + ch
                if line and font.size(candidate)[0] > max_width:
                    lines.append(line)
                    line = ch
                else:
                    line = candidate
            if line:
                lines.append(line)
        return lines or [""]

    def _centered_lines(self, surface, text, center_x: float, center_y: float,
                        max_width: int, size: int = 14, bold: bool = False,
                        color=INK) -> None:
        lines = self._wrap(text, max_width, size, bold)
        line_h = 20
        for i, line in enumerate(lines):
            y = center_y + (i - (len(lines) - 1) / 2) * line_h
            self._label(surface, line, (center_x, y), size=size, bold=bold,
                        color=color, anchor="center")

    def _row_fill(self, surface, row_index: int, hovered: bool) -> None:
        if not hovered and not (self.keyboard_mode and row_index == self.selected):
            return
        y0 = ROW_Y[row_index] + 1
        h = ROW_Y[row_index + 1] - y0 - 2
        tint = pygame.Surface((TABLE.width - 2, h), pygame.SRCALPHA)
        tint.fill(PANEL_TINT)
        surface.blit(tint, (TABLE.x + 1, y0))

    def _draw_back(self, surface) -> None:
        if self.hover == ("back", -1):
            asset = "ui/btnback_over.png"
        else:
            asset = "ui/btnback_up.png"
        try:
            surface.blit(self.app.assets.image(asset), (BACK_RECT.x, BACK_RECT.y))
        except (KeyError, FileNotFoundError):
            self._label(surface, "返回", BACK_RECT.center, size=16, bold=True,
                        color=GOLD, anchor="center")

    def _draw_hero_tabs(self, surface) -> None:
        # 背景图里已经预先画好了两个英雄页签，这里只遮住页签本身的范围
        # （不碰 y=47 以下的背景纹理），再按当前英雄状态重绘略小一点的页签。
        # 覆盖区高度在 47 像素处截断，避免在名字下方留下黑色矩形。
        # 重绘的页签按 0.94 缩放，既能贴合原位置，又不会压到边框。
        for key, rect in HERO_TABS.items():
            cover = pygame.Rect(rect.x - 3, rect.y - 2, rect.w + 6, 47 - (rect.y - 2))
            pygame.draw.rect(surface, BLACK, cover)
            active = key == self.hero
            suffix = "" if active else "_disabled"
            path = f"ui/skills/hero_{key}{suffix}.png"
            try:
                image = self.app.assets.image(path)
                size = (max(1, round(image.get_width() * TAB_ART_SCALE)),
                        max(1, round(image.get_height() * TAB_ART_SCALE)))
                image = pygame.transform.smoothscale(image, size)
                surface.blit(image, image.get_rect(center=rect.center))
            except (KeyError, FileNotFoundError):
                self._label(surface, "悟空" if key == "wukong" else "唐僧",
                            rect.center, size=18, bold=True,
                            color=GOLD if active else INK_DIM, anchor="center")

    def _draw_header(self, surface, passive: bool) -> None:
        y = TABLE.y + HEADER_H / 2
        if not passive:
            for text, x in (("技能名称", 116), ("技能图标", 198),
                            ("所需灵魂", 282), ("技能按键", 366)):
                self._label(surface, text, (x, y), size=14, bold=True,
                            color=INK, anchor="midleft")
            self._label(surface, "技能说明", (623, y), size=14, bold=True,
                        color=INK, anchor="center")
        else:
            for text, x, anchor in (("技能名称", 118, "midleft"),
                                    ("当前等级", 223, "center"),
                                    ("效 果", 356, "center"),
                                    ("下一级效果", 530, "center"),
                                    ("升 级", 685, "center"),
                                    ("所需灵魂", 803, "center")):
                self._label(surface, text, (x, y), size=14, bold=True,
                            color=INK, anchor=anchor)

    def _draw_active(self, surface) -> None:
        for i in range(5):
            center = ROW_CENTER[i]
            hovered = self.hover == ("skill", i)
            self._row_fill(surface, i, hovered)
            learned = bool(self.state["active"][i])
            icon_name = f"ui/skills/{self.icon_prefix}{i + 1}_" + \
                        ("learned" if learned else "locked") + ".png"
            try:
                icon = self.app.assets.image(icon_name)
                surface.blit(icon, (ICON_RECTS[i].x, ICON_RECTS[i].y))
            except (KeyError, FileNotFoundError):
                pygame.draw.rect(surface, (42, 42, 42), ICON_RECTS[i])
                pygame.draw.rect(surface, (210, 210, 200), ICON_RECTS[i], 1)
                self._label(surface, "学习", ICON_RECTS[i].center, size=17,
                            bold=True, color=GOLD, anchor="center")

            skill = self.active[i]
            self._label(surface, skill.get("name", ""), (118, center),
                        size=16, bold=True, color=INK)
            self._label(surface, str(active_cost(self.hero, i)),
                        (289, center), size=14, color=INK)
            self._label(surface, active_key(self.hero, i), (391.5, center),
                        size=15, bold=True, color=INK, anchor="center")
            self._centered_lines(surface, skill.get("desc", ""), 639, center,
                                 max_width=400, size=14, color=INK)

            if hovered or (self.selected == i and self.keyboard_mode):
                pygame.draw.rect(surface, GOLD, ICON_RECTS[i].inflate(4, 4),
                                 2, border_radius=3)

    def _draw_passive(self, surface) -> None:
        for i in range(5):
            center = ROW_CENTER[i]
            hovered = self.hover == ("upgrade", i)
            self._row_fill(surface, i, hovered)
            level = int(self.state["passive"][i])
            self._label(surface, passive_name(self.hero, i), (118, center),
                        size=16, bold=True, color=INK)
            self._label(surface, str(level), (222, center), size=14,
                        color=INK, anchor="center")
            self._label(surface, passive_effect_text(i, level),
                        (356, center), size=14, color=INK, anchor="center")
            next_text = "----" if level >= PASSIVE_MAX else passive_effect_text(i, level + 1)
            self._label(surface, next_text, (550, center), size=14,
                        color=INK, anchor="center")
            cost = passive_next_cost(level)
            if cost:
                self._label(surface, str(cost), (797, center), size=14,
                            color=INK, anchor="center")
            else:
                self._label(surface, "—", (797, center), size=14,
                            color=INK_DIM, anchor="center")

            if level < PASSIVE_MAX:
                color = GOLD_HI if hovered else GOLD
                self._label(surface, "升级", (685, center), size=19, bold=True,
                            color=color, anchor="center")
            else:
                # 满级后隐藏升级按钮，价格列显示“已满级”而不是继续给出数值，
                # 因为此时再显示价格会让玩家误以为仍然可以购买。
                # 这是有意做出的易用性调整。
                self._label(surface, "已满级", (688, center), size=14,
                            color=INK_DIM, anchor="center")

    def tab_label_color(self, tab_index: int):
        """返回页签文字颜色：选中为金色，未选中为白色（此前曾把两者写反）。"""
        if self.hover == ("tab", tab_index):
            return GOLD_HI
        return GOLD if self.tab == tab_index else INK

    def _draw_bottom_tabs(self, surface) -> None:
        active_color = self.tab_label_color(0)
        passive_color = self.tab_label_color(1)
        self._label(surface, "主动技能", (TAB_ACTIVE.centerx, BOTTOM_TAB_Y),
                    size=16, bold=True, color=active_color, anchor="center")
        self._label(surface, "被动技能", (TAB_PASSIVE.centerx, BOTTOM_TAB_Y),
                    size=16, bold=True, color=passive_color, anchor="center")

    def _draw_soul(self, surface) -> None:
        try:
            surface.blit(self.app.assets.image("ui/skills/soul.png"), SOUL_ICON)
        except (KeyError, FileNotFoundError):
            pygame.draw.circle(surface, GOLD, (SOUL_ICON[0] + 37, SOUL_ICON[1] + 33), 30, 2)
        self._label(surface, str(self.soul), SOUL_TEXT, size=23, bold=True,
                    color=INK, anchor="midleft")

    def draw(self, surface: pygame.Surface) -> None:
        surface.fill((0, 0, 0))
        try:
            surface.blit(self.app.assets.image("ui/skill_bg.png"), (0, 0))
        except (KeyError, FileNotFoundError):
            surface.fill((20, 20, 20))

        # 背景顶栏里已经预先画好了两个英雄页签和“返回”，这里只覆盖这几块区域，
        # 让当前英雄与返回按钮的状态能够实时刷新。
        self._draw_hero_tabs(surface)
        pygame.draw.rect(surface, BLACK, (846, 6, 72, 38))
        self._draw_back(surface)

        try:
            grid = "ui/skills/table_passive.png" if self.tab else "ui/skills/table_active.png"
            surface.blit(self.app.assets.image(grid), TABLE.topleft)
        except (KeyError, FileNotFoundError):
            pygame.draw.rect(surface, (18, 18, 18), TABLE)
            pygame.draw.rect(surface, (210, 190, 130), TABLE, 1)

        self._draw_header(surface, passive=(self.tab == 1))
        if self.tab == 0:
            self._draw_active(surface)
        else:
            self._draw_passive(surface)
        self._draw_bottom_tabs(surface)
        self._draw_soul(surface)

        if self.msg:
            self._label(surface, self.msg, (470, 535), size=13, bold=True,
                        color=GOLD_HI, anchor="center")
        if self.keyboard_mode:
            self._label(surface, "↑/↓ 选择   Enter 学习/升级   Tab 切换   Esc 返回",
                        (470, 580), size=11, color=INK_DIM, anchor="center")
