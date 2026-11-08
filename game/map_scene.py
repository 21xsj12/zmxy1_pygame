"""选关地图场景：鼠标驱动，键盘操作保留为无障碍备用方案。

本实现的布局与行为约定：

* 背景是一张 940x590 的静态图，没有滚动，也没有摄像机。
* 12 个节点在固定位置预先摆好；4-2、4-3 是空位并停在屏幕外，
  因此实际可玩的只有 1-1 到 4-1。
* 每个节点各有 3 帧：常态（1）、悬停（2）、当前进度（3）。
* 当前节点播放 19 帧的发光脉冲；3-3 则改播 48 帧的漩涡动画。
* 是否解锁通过挂不挂鼠标监听来区分：更早的关卡三个小关都能点，
  当前关卡只开放到当前大关。未解锁节点外观和常态完全一样（没有锁图标），
  也不响应悬停。
* 节点名称直接画在悬停用的美术图里，不再额外渲染文字。
"""
from __future__ import annotations

import math
from typing import Iterable

import pygame

from . import map_data
from .constants import HEIGHT, WIDTH
from .save import new_save, write_save


class MapScene:
    """鼠标驱动的选关界面，键盘操作保留作为无障碍备用方案。"""

    def __init__(self, app):
        self.app = app
        self.assets = app.assets
        self.time = 0.0
        self.hover_id: str | None = None
        self.focus_id = self.current_id
        self.focus_index = 0
        self.pressed_id: str | None = None
        self.pressed_t = 0.0
        self.shop_anim_t = 0.0
        self.save_cd = float(getattr(app, "save_cd", 0.0) or 0.0)
        self.msg = ""
        self.msg_t = 0.0
        self.modal: str | None = None
        self._cursor_hand = False
        self._scaled: dict[tuple[str, float, float], pygame.Surface] = {}
        self._masks: dict[tuple[str, int, int], pygame.mask.Mask] = {}
        self._last_app_sel: int | None = None
        self._bg = self.assets.image(map_data.BACKGROUND)
        self._sync_from_app(force=True, set_hover=False)
        self._preload_current()

    def _preload_current(self) -> None:
        """只预热马上要播放的动画，避免切换到该动画时第一帧卡顿。"""
        for node in map_data.NODES:
            if node["id"] == self.current_id:
                self._preload_frames(node["current"], int(node["current_frames"]))
                break
        if int((self.app.save_data or {}).get("soul", 0)) >= 160:
            ui = map_data.UI["shop"]
            self._preload_frames(ui["current"], int(ui["current_frames"]))
        shop = map_data.UI["shop"]
        if shop.get("hover_anim"):
            self._preload_frames(shop["hover_anim"], int(shop["hover_frames"]))
        if self.arena_enabled:
            ui = map_data.UI["arena"]
            self._preload_frames(ui["current"], int(ui["current_frames"]))

    def _preload_frames(self, folder: str, count: int) -> None:
        if not folder:
            return
        for i in range(max(0, count)):
            self.assets.image(f"{folder}/{i:02d}.png")

    # ------------------------------------------------------------------ 状态
    def exit(self) -> None:
        self.app.save_cd = self.save_cd
        try:
            pygame.mouse.set_cursor(pygame.SYSTEM_CURSOR_ARROW)
        except pygame.error:
            pass
        self._cursor_hand = False

    @property
    def current_id(self) -> str:
        stage, level = self.app.progress
        return f"{stage}-{level}"

    @property
    def arena_enabled(self) -> bool:
        # 竞技场入口只在双人模式下挂鼠标监听，也就是 play_num 为 2 时。
        # 目前本实现还没有双人战役流程，因此这里先返回 False，
        # 等以后的双人入口把 app.play_num 设为 2 后再开放。
        return int(getattr(self.app, "play_num", 1)) == 2

    def _listeners(self) -> list[str]:
        ids = []
        for node in map_data.NODES:
            if self.app.is_level_unlocked(node["id"]):
                ids.append(node["id"])
        if self.arena_enabled:
            ids.append("arena")
        return ids

    def _is_interactive(self, item_id: str) -> bool:
        if item_id in ("shop", "activity", "save"):
            return True
        if item_id == "arena":
            return self.arena_enabled
        return self.app.is_level_unlocked(item_id)

    # ------------------------------------------------------------------ 焦点
    def _sync_from_app(self, force: bool = False, set_hover: bool = True) -> None:
        order = map_data.SELECT_ORDER
        sel = int(getattr(self.app, "map_sel", 0) or 0)
        if not 0 <= sel < len(order):
            sel = 0
        if force or sel != self._last_app_sel:
            self._last_app_sel = sel
            self.focus_index = sel
            self.focus_id = order[sel]
            if set_hover and self._is_interactive(self.focus_id):
                self.hover_id = self.focus_id

    def set_focus(self, item_id: str) -> None:
        if item_id not in map_data.SELECT_ORDER:
            return
        idx = map_data.SELECT_ORDER.index(item_id)
        self.focus_index = idx
        self.focus_id = item_id
        self.hover_id = item_id if self._is_interactive(item_id) else None
        self.app.map_sel = idx
        self._last_app_sel = idx

    def _move_focus(self, delta: int) -> None:
        ids = self._listeners()
        if not ids:
            return
        try:
            pos = ids.index(self.focus_id)
        except ValueError:
            pos = 0
        self.set_focus(ids[(pos + delta) % len(ids)])

    # ------------------------------------------------------------------ 事件
    def handle_event(self, event: pygame.event.Event) -> bool:
        if event.type == pygame.MOUSEMOTION:
            if self.modal:
                return True
            old = self.hover_id
            hit = self._hit_test(event.pos)
            if hit == "shop" and old != "shop":
                self.shop_anim_t = 0.0
            self.hover_id = hit
            if hit and hit in map_data.SELECT_ORDER:
                self.set_focus(hit)
            self._apply_cursor()
            return True

        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            if self.modal:
                # 活动面板右上角已经画好了关闭叉号，命中区按素材实际位置单独校准；
                # 点击面板外面的任意区域也同样可以关闭面板。
                panel = pygame.Rect(213, 88, 541, 415)
                close = pygame.Rect(690, 70, 70, 60)
                if close.collidepoint(event.pos) or not panel.collidepoint(event.pos):
                    self.modal = None
                return True
            hit = self._hit_test(event.pos)
            if hit:
                self.pressed_id = hit
                self.pressed_t = 0.10
                self._activate(hit)
            return True

        if event.type == pygame.MOUSEBUTTONUP and event.button == 1:
            self.pressed_id = None
            return True

        if event.type != pygame.KEYDOWN:
            return False

        if self.modal:
            if event.key in (pygame.K_ESCAPE, pygame.K_RETURN, pygame.K_SPACE):
                self.modal = None
            return True

        if event.key in (pygame.K_UP, pygame.K_LEFT, pygame.K_a, pygame.K_w):
            self._move_focus(-1)
        elif event.key in (pygame.K_DOWN, pygame.K_RIGHT, pygame.K_d, pygame.K_s):
            self._move_focus(1)
        elif event.key in (pygame.K_RETURN, pygame.K_j, pygame.K_SPACE):
            self._activate(self.hover_id or self.focus_id)
        elif event.key == pygame.K_ESCAPE:
            self.app.to_title()
        elif event.key == pygame.K_b:
            self.app.to_inventory("map")
        elif event.key == pygame.K_k:
            self.app.to_skill_shop("map")
        elif event.key == pygame.K_F5:
            self._save()
        else:
            return False
        return True

    def _activate(self, item_id: str) -> None:
        if item_id == "shop":
            self.app.to_skill_shop("map")
            return
        if item_id == "activity":
            self.modal = "activity"
            return
        if item_id == "save":
            self._save()
            return
        if item_id == "arena":
            if self.arena_enabled:
                self.app.start_arena()
            else:
                self._toast("双人模式暂未开放")
            return
        if item_id.startswith(("1-", "2-", "3-", "4-")):
            if not self.app.is_level_unlocked(item_id):
                self._toast("尚未解锁")
                return
            self.app.level_id = item_id
            self.app.start_battle()
            return

    # ------------------------------------------------------------------ 更新
    def update(self, dt: float) -> None:
        self.time += dt
        self.pressed_t = max(0.0, self.pressed_t - dt)
        if self.pressed_t <= 0:
            self.pressed_id = None
        self.msg_t = max(0.0, self.msg_t - dt)
        if self.msg_t <= 0:
            self.msg = ""
        if self.save_cd > 0:
            self.save_cd = max(0.0, self.save_cd - dt)
        self.app.save_cd = self.save_cd
        shop = map_data.UI["shop"]
        if self.hover_id == "shop" and shop.get("hover_anim"):
            last = max(0.0, int(shop["hover_frames"]) / 24.0)
            self.shop_anim_t = min(self.shop_anim_t + dt, last)
        else:
            self.shop_anim_t = 0.0
        self._sync_from_app()
        self._apply_cursor()

    def _toast(self, text: str, ttl: float = 1.6) -> None:
        self.msg = text
        self.msg_t = ttl

    # ------------------------------------------------------------------ 存档
    def _save(self) -> None:
        if self.save_cd > 0:
            self._toast(f"存档冷却 {math.ceil(self.save_cd)} 秒")
            return
        try:
            if self.app.save_data is None:
                self.app.save_data = new_save(self.app.slot)
                self.app.has_save = True
            data = self.app.save_data
            stage, level = self.app.progress
            data["cur_stage"] = stage
            data["cur_level"] = level
            write_save(data, slot=self.app.slot)
        except Exception as exc:  # noqa: BLE001 - surface failure to the player
            self._toast(f"存档失败：{exc}")
            return
        self.save_cd = 30.0
        self.app.save_cd = 30.0
        self._toast("存档成功")

    # ------------------------------------------------------------------ 绘制
    def draw(self, surface: pygame.Surface) -> None:
        self._sync_from_app()
        surface.fill((0, 0, 0))
        surface.blit(self._bg, (0, 0))

        for _depth, kind, item in self._draw_items():
            if kind == "node":
                self._draw_node(surface, item)
            elif kind == "arena":
                self._draw_arena(surface, item)
            elif kind == "shop":
                self._draw_shop(surface, item)
            elif kind == "furnace":
                self._draw_base(surface, item)
            elif kind == "activity":
                self._draw_button(surface, "activity", item)
            elif kind == "save":
                self._draw_button(surface, "save", item)

        self._draw_save_text(surface)
        if self.msg:
            self._draw_toast(surface)
        if self.modal:
            self._draw_modal(surface)

    def _draw_items(self) -> list[tuple[int, str, dict]]:
        items: list[tuple[int, str, dict]] = []
        for node in map_data.NODES:
            items.append((int(node["depth"]), "node", node))
        items.append((int(map_data.UI["arena"]["depth"]), "arena", map_data.UI["arena"]))
        items.append((int(map_data.UI["furnace"]["depth"]), "furnace", map_data.UI["furnace"]))
        items.append((int(map_data.UI["shop"]["depth"]), "shop", map_data.UI["shop"]))
        items.append((int(map_data.BUTTONS["activity"]["depth"]), "activity",
                      map_data.BUTTONS["activity"]))
        items.append((int(map_data.BUTTONS["save"]["depth"]), "save",
                      map_data.BUTTONS["save"]))
        items.sort(key=lambda row: row[0])
        return items

    def _draw_node(self, surface: pygame.Surface, node: dict) -> None:
        state = self._node_state(node)
        if state == "current":
            self._draw_frames(surface, node, node["current"],
                              int(node["current_frames"]), node["cur_offset"])
        elif state == "hover":
            self._draw_image(surface, node["hover"], node["anchor"],
                             node["base_offset"], node["scale"])
        else:
            if node["transparent_idle"]:
                return
            self._draw_image(surface, node["idle"], node["anchor"],
                             node["base_offset"], node["scale"])

    def _node_state(self, node: dict) -> str:
        if self.hover_id == node["id"] and self.app.is_level_unlocked(node["id"]):
            return "hover"
        if node["id"] == self.current_id:
            return "current"
        return "idle"

    def _draw_arena(self, surface: pygame.Surface, ui: dict) -> None:
        if self.arena_enabled:
            if self.hover_id == "arena":
                self._draw_image(surface, ui["hover"], ui["anchor"],
                                 ui["base_offset"], (1.0, 1.0))
                return
            self._draw_frames(surface, ui, ui["current"], int(ui["current_frames"]),
                              ui["cur_offset"])
            return
        self._draw_image(surface, ui["idle"], ui["anchor"], ui["base_offset"], (1.0, 1.0))

    def _draw_shop(self, surface: pygame.Surface, ui: dict) -> None:
        if self.hover_id == "shop":
            if ui.get("hover_anim"):
                frame = min(int(self.shop_anim_t * 24), int(ui["hover_frames"]) - 1)
                rel = f"{ui['hover_anim']}/{frame:02d}.png"
                self._draw_image(surface, rel, ui["anchor"], ui["hover_offset"],
                                 (1.0, 1.0))
            else:
                self._draw_image(surface, ui["hover"], ui["anchor"],
                                 ui["base_offset"], (1.0, 1.0))
            if ui.get("tooltip"):
                tip = self.assets.image(ui["tooltip"])
                surface.blit(tip, (round(ui["tooltip_pos"][0]),
                                   round(ui["tooltip_pos"][1])))
            return
        soul = int((self.app.save_data or {}).get("soul", 0))
        if soul >= 160:
            self._draw_frames(surface, ui, ui["current"], int(ui["current_frames"]),
                              ui["cur_offset"])
            return
        self._draw_image(surface, ui["idle"], ui["anchor"], ui["base_offset"], (1.0, 1.0))

    def _draw_base(self, surface: pygame.Surface, ui: dict) -> None:
        self._draw_image(surface, ui["idle"], ui["anchor"], ui["base_offset"], (1.0, 1.0))

    def _draw_button(self, surface: pygame.Surface, name: str, btn: dict) -> None:
        state = "up"
        if self.pressed_id == name and self.pressed_t > 0:
            state = "down"
        elif self.hover_id == name:
            state = "over"
        scale = float(btn["scale"])
        self._draw_image(surface, btn[state], btn["anchor"], btn["offset"],
                         (scale, scale))

    # ------------------------------------------------------------------ 图片辅助
    def _draw_image(self, surface: pygame.Surface, rel: str, anchor: tuple[float, float],
                    offset: tuple[float, float], scale: tuple[float, float]) -> None:
        img = self.assets.image(rel)
        if scale != (1.0, 1.0):
            img = self._scaled_image(rel, img, scale)
        x = anchor[0] + offset[0] * scale[0]
        y = anchor[1] + offset[1] * scale[1]
        surface.blit(img, (round(x), round(y)))

    def _draw_frames(self, surface: pygame.Surface, item: dict, folder: str,
                     count: int, offset: tuple[float, float]) -> None:
        if not folder or count <= 0:
            return
        frame = int(self.time * 24) % count
        rel = f"{folder}/{frame:02d}.png"
        self._draw_image(surface, rel, item["anchor"], offset, item.get("scale", (1.0, 1.0)))

    def _scaled_image(self, rel: str, img: pygame.Surface,
                      scale: tuple[float, float]) -> pygame.Surface:
        key = (rel, scale[0], scale[1])
        cached = self._scaled.get(key)
        if cached is None:
            cached = pygame.transform.smoothscale(
                img, (max(1, round(img.get_width() * scale[0])),
                      max(1, round(img.get_height() * scale[1]))))
            self._scaled[key] = cached
        return cached

    # ------------------------------------------------------------------ 命中检测
    def _hit_items(self) -> list[tuple[int, str, pygame.Rect, tuple[str, int, int] | None]]:
        rows: list[tuple[int, str, pygame.Rect, tuple[str, int, int] | None]] = []
        for node in map_data.NODES:
            if not self.app.is_level_unlocked(node["id"]):
                continue
            # 命中按当前帧的实际几何形状判断；常态帧用的是一块很小的
            # 不可见矩形（3-x 关卡尤其明显），远小于悬停美术图，
            # 从而避免「整块大矩形」造成的误触。
            state = self._node_state(node)
            box = (node["hit_idle"] if state == "idle" else
                   node["hit_hover"] if state == "hover" else node["hit_current"])
            rect = _rect(box)
            mask = None
            # 3-1/3-2 的悬停帧与当前帧只有发光效果，几乎没有实体像素，
            # 因此改用填色后的形状图做掩码，而不是矩形包围盒。
            if state != "idle" and node.get("hit_mask"):
                mask = (node["hit_mask"], rect.width, rect.height)
            rows.append((int(node["depth"]), node["id"], rect, mask))
        if self.arena_enabled:
            ui = map_data.UI["arena"]
            box = ui["hit_hover"] if self.hover_id == "arena" else ui["hit_current"]
            rows.append((int(ui["depth"]), "arena", _rect(box), None))
        ui = map_data.UI["shop"]
        if self.hover_id == "shop":
            box = ui["hit_hover"]
        elif int((self.app.save_data or {}).get("soul", 0)) >= 160:
            box = ui["hit_current"]
        else:
            box = ui["hit_idle"]
        rows.append((int(ui["depth"]), "shop", _rect(box), None))
        for name in ("activity", "save"):
            btn = map_data.BUTTONS[name]
            x = btn["anchor"][0] + btn["offset"][0] * btn["scale"]
            y = btn["anchor"][1] + btn["offset"][1] * btn["scale"]
            w = btn["size"][0] * btn["scale"]
            h = btn["size"][1] * btn["scale"]
            rows.append((int(btn["depth"]), name,
                         pygame.Rect(round(x), round(y), round(w), round(h)), None))
        rows.sort(key=lambda row: row[0], reverse=True)
        return rows

    def _hit_test(self, pos: Iterable[int]) -> str | None:
        for _depth, item_id, rect, mask in self._hit_items():
            if not rect.collidepoint(pos):
                continue
            if mask is not None and not self._mask_hit(mask, rect, pos):
                continue
            return item_id
        return None

    def _mask_hit(self, spec: tuple[str, int, int], rect: pygame.Rect,
                  pos: Iterable[int]) -> bool:
        rel, w, h = spec
        key = (rel, w, h)
        mask = self._masks.get(key)
        if mask is None:
            img = self.assets.image(rel)
            if img.get_size() != (w, h):
                img = pygame.transform.smoothscale(img, (w, h))
            mask = pygame.mask.from_surface(img, 32)
            self._masks[key] = mask
        mw, mh = mask.get_size()
        x = int((pos[0] - rect.x) * mw / max(rect.w, 1))
        y = int((pos[1] - rect.y) * mh / max(rect.h, 1))
        if not (0 <= x < mw and 0 <= y < mh):
            return False
        return bool(mask.get_at((x, y)))

    def _apply_cursor(self) -> None:
        hand = (not self.modal and bool(self.hover_id)
                and self._is_interactive(self.hover_id))
        if hand == self._cursor_hand:
            return
        try:
            pygame.mouse.set_cursor(pygame.SYSTEM_CURSOR_HAND if hand
                                    else pygame.SYSTEM_CURSOR_ARROW)
        except pygame.error:
            pass
        self._cursor_hand = hand

    # ------------------------------------------------------------------ 浮层
    def _draw_save_text(self, surface: pygame.Surface) -> None:
        if self.save_cd <= 0:
            return
        text = f"({math.ceil(self.save_cd)})"
        font = self.assets.font(int(map_data.SAVE_TEXT["size"]), bold=True)
        img = font.render(text, True, (0, 0, 0))
        rect = img.get_rect(center=(round(map_data.SAVE_TEXT["center"][0]),
                                    round(map_data.SAVE_TEXT["center"][1])))
        surface.blit(img, rect)

    def _draw_toast(self, surface: pygame.Surface) -> None:
        font = self.assets.font_for(self.msg, 15, bold=True)
        img = font.render(self.msg, True, (245, 240, 220))
        pad = 10
        rect = img.get_rect(midbottom=(WIDTH // 2, HEIGHT - 24))
        panel = rect.inflate(pad * 2, pad)
        panel.midbottom = rect.midbottom
        shade = pygame.Surface(panel.size, pygame.SRCALPHA)
        shade.fill((10, 10, 10, 205))
        surface.blit(shade, panel.topleft)
        surface.blit(img, rect)

    def _draw_modal(self, surface: pygame.Surface) -> None:
        try:
            panel = self.assets.image(map_data.ACTIVITY_PANEL)
            surface.blit(panel, (0, 0))
            return
        except (KeyError, FileNotFoundError):
            pass
        # 活动面板美术资源缺失时的兜底绘制。
        shade = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
        shade.fill((0, 0, 0, 150))
        surface.blit(shade, (0, 0))
        panel = pygame.Rect(0, 0, 540, 240)
        panel.center = (WIDTH // 2, HEIGHT // 2)
        pygame.draw.rect(surface, (28, 24, 20), panel, border_radius=8)
        pygame.draw.rect(surface, (210, 170, 90), panel, 2, border_radius=8)
        hint = self.assets.font_for("活动面板资源缺失；点击任意处关闭", 16)
        img = hint.render("活动面板资源缺失；点击任意处关闭", True, (225, 220, 205))
        surface.blit(img, img.get_rect(center=(WIDTH // 2, panel.centery)))


def _rect(box: tuple[float, float, float, float]) -> pygame.Rect:
    x, y, w, h = box
    return pygame.Rect(round(x), round(y), max(1, round(w)), max(1, round(h)))
