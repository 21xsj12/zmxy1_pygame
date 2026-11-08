"""游戏各类界面的绘制：标题菜单、选角、地图、背包、帮助与过场等。

本模块集中放置由项目美术素材拼出的界面绘制函数，各界面与素材的对应关系：

* 标题菜单           -> ``cover_scene.py`` + ``assets/menu``
* 选择角色           -> ``hero_select.py`` + ``assets/menu``
* 选择地图           -> ``ui/map/map_bg.jpg`` + ``map_scene.py`` 中的节点
* 角色信息面板       -> ``ui/hud/*``（game.hud.RoleInfoHud）
* 暂停菜单           -> ``ui/pause.png``
* 失败结算           -> ``ui/fail.png``
* 帮助 / 关于        -> ``ui/help.png`` / ``ui/about.png``
* 技能商店           -> ``skill_page.py`` + ``ui/skills/*``
* 背包               -> ``ui/backpack.png`` + ``ui/pack_cell.png``
* 首领血条与名字     -> ``ui/boss/bar_frames/*`` + ``ui/boss/names/*``
* 连击计数           -> ``ui/batter.png``
* 开场 / 结局叙事    -> ``game/narration.py`` + ``assets/opening|ending``
  （``draw_opening`` / ``draw_ending`` 仅在资源缺失时作为兜底画面）
"""
from __future__ import annotations

import math
import os

import pygame

from .assets import Assets, glyph_supported
from .constants import (COLOR_EXP, COLOR_GOLD, COLOR_HP, COLOR_MP, COLOR_WHITE,
                        HEIGHT, WIDTH)
from .entities import MONSTER_STATS
from .items import ITEMS, sell_price
from .skills import SKILLS, normalize, passive_cost
from . import map_data

_FALLBACK_FONTS: dict[tuple[int, bool], pygame.font.Font] = {}


def _needs_fallback(font: pygame.font.Font, text: str) -> bool:
    for ch in str(text):
        if ch.isspace():
            continue
        if not glyph_supported(font, ch):
            return True
    return False


def _fallback_font(font: pygame.font.Font) -> pygame.font.Font:
    size = max(8, int(font.get_height() * 0.82))
    bold = bool(font.get_bold())
    key = (size, bold)
    if key in _FALLBACK_FONTS:
        return _FALLBACK_FONTS[key]
    for path in ((r"C:\Windows\Fonts\msyhbd.ttc" if bold else None),
                 r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf"):
        if path and os.path.exists(path):
            try:
                f = pygame.font.Font(path, size)
                if bold:
                    f.set_bold(True)
                _FALLBACK_FONTS[key] = f
                return f
            except Exception:
                continue
    return font

# ---------------------------------------------------------------- 布局数据
# 为旧工具与旧界面保留的标题行表；当前标题界面实际由
# cover_scene.CoverScene 配合 assets/menu/menu_ui.json 绘制。
MENU_ROWS = [
    ("单人游戏", 195), ("双人游戏", 249), ("成就房间", 301), ("游戏商城", 353),
    ("游戏帮助", 404), ("游戏推荐", 457), ("关于我们", 507), ("返回首页", 554),
]
MENU_ROW_H = 26

# 键盘选择与 app.map_sel 的排列顺序，均与 map_data.SELECT_ORDER 一致。
MAP_SELECT_ORDER = list(map_data.SELECT_ORDER)

def draw_text(surface: pygame.Surface, font: pygame.font.Font, text: str, pos,
              color=COLOR_WHITE, shadow=True, anchor: str = "topleft") -> pygame.Rect:
    text = str(text)
    if _needs_fallback(font, text):
        font = _fallback_font(font)
    img = font.render(text, True, color)
    rect = img.get_rect()
    setattr(rect, anchor, pos)
    if shadow:
        sh = font.render(text, True, (0, 0, 0))
        sr = sh.get_rect()
        setattr(sr, anchor, (pos[0] + 2, pos[1] + 2))
        surface.blit(sh, sr)
    surface.blit(img, rect)
    return rect


def draw_bar(surface: pygame.Surface, x: int, y: int, w: int, h: int, ratio: float,
             color, border=(12, 12, 12), bg=(40, 40, 40)) -> None:
    ratio = max(0.0, min(1.0, ratio))
    pygame.draw.rect(surface, border, (x - 2, y - 2, w + 4, h + 4), border_radius=3)
    pygame.draw.rect(surface, bg, (x, y, w, h), border_radius=2)
    if ratio > 0:
        pygame.draw.rect(surface, color, (x, y, int(w * ratio), h), border_radius=2)


def draw_exit_fade(surface: pygame.Surface, progress: float) -> None:
    """关卡通关时的渐隐：约 1 秒内把画面透明度从 1 过渡到 0。

    本实现把角色信息栏与游戏世界合成在同一帧里，没有可以单独调透明度的
    图层，因此在 HUD 与世界绘制完成之后，再叠加一层整屏黑色遮罩来达到
    同样的渐隐效果。
    """
    progress = max(0.0, min(1.0, float(progress)))
    alpha = int(round(255 * progress))
    if alpha <= 0:
        return
    veil = pygame.Surface(surface.get_size(), pygame.SRCALPHA)
    veil.fill((0, 0, 0, alpha))
    surface.blit(veil, (0, 0))


_DIGIT_CACHE: dict[tuple[str, str], pygame.Surface] = {}


def _digit_img(assets: Assets, kind: str, ch: str) -> pygame.Surface:
    key = (kind, ch)
    cached = _DIGIT_CACHE.get(key)
    if cached is not None:
        return cached
    img = assets.image(f"ui/digits/{kind}/{ch}.png")
    rects = pygame.mask.from_surface(img).get_bounding_rects()
    if rects:
        left = min(r.left for r in rects)
        top = min(r.top for r in rects)
        right = max(r.right for r in rects)
        bottom = max(r.bottom for r in rects)
        img = img.subsurface(pygame.Rect(left, top, right - left,
                                         bottom - top)).copy()
    _DIGIT_CACHE[key] = img
    return img


def draw_damage_numbers(surface: pygame.Surface, assets: Assets, numbers,
                        camera_x: float) -> None:
    for n in numbers:
        kind = n.kind if n.kind in ("pnum", "bnum", "hurtnum", "bunum", "bulnum") else "hurtnum"
        digits = []
        for ch in str(max(0, int(n.value))):
            try:
                digits.append(_digit_img(assets, kind, ch))
            except (KeyError, FileNotFoundError):
                digits = []
                break
        if not digits:
            font = assets.font(18, bold=True, system=True)
            draw_text(surface, font, str(n.value),
                      (n.x - camera_x, n.y), (255, 240, 200), anchor="midbottom")
            continue
        scale = 1.0
        if n.ttl > 0.72:
            scale = 1.0 + (n.ttl - 0.72) * 0.9
        if scale != 1.0:
            digits = [pygame.transform.smoothscale(
                d, (max(1, int(d.get_width() * scale)),
                    max(1, int(d.get_height() * scale)))) for d in digits]
        gap = 1
        total = sum(d.get_width() for d in digits) + gap * (len(digits) - 1)
        x = n.x - camera_x - total / 2
        y = n.y
        for d in digits:
            surface.blit(d, (round(x), round(y - d.get_height())))
            x += d.get_width() + gap


def draw_world_overlay(surface: pygame.Surface, assets: Assets, world) -> None:
    """绘制首领血条、停顿点提示与各类提示文字。

    角色信息面板由 :class:`game.hud.RoleInfoHud` 负责，连击计数在叠加层
    之后由 :class:`game.combo.ComboCounter` 绘制。这些叠加层不属于角色
    信息面板的素材范围，因此留在本模块中统一绘制。
    """
    # 首领血条按素材里 100 帧的整体出场/退场节奏播放，名字直接叠在血条上方。
    # 关卡结束时角色信息栏会被销毁（血条是它的子对象），所以结算过渡一开始
    # 就不再绘制血条。
    boss_bar = getattr(world, "boss_bar", None)
    if boss_bar is not None and not getattr(world, "finished", False):
        boss_bar.draw(surface, assets)

    # “前进”提示属于 HUD 层，不随摄像机滚动，必须始终固定在屏幕坐标上。
    # 关卡结束时 HUD 会被销毁，结算界面又会冻结战斗，所以一旦完成
    # 就不再绘制该提示。
    if not getattr(world, "finished", False):
        for gogo in getattr(world, "gogos", None) or []:
            gogo.draw(surface, assets)


def draw_hud(surface: pygame.Surface, assets: Assets, world) -> None:
    """兼容入口：依次绘制角色信息面板、世界叠加层与连击计数。"""
    from .hud import draw_roleinfo
    draw_roleinfo(surface, assets, world.player)
    draw_world_overlay(surface, assets, world)
    combo = getattr(world, "combo", None)
    if combo is not None:
        combo.draw(surface, assets, getattr(world, "wall_time", 0.0))

# ------------------------------------------------------------------- 标题
# 为兼容早期工具而保留的备用绘制函数；实际入口使用
# cover_scene.CoverScene，按素材中的按钮状态绘制标题界面。
def draw_title(surface: pygame.Surface, assets: Assets, selected: int = 0,
               has_save: bool = False, t: float = 0.0) -> None:
    surface.fill((0, 0, 0))
    menu = assets.image("ui/menu.png")
    surface.blit(menu, (0, 0))
    # 在已经烘焙进背景图的文字行上叠加选中高亮
    if 0 <= selected < len(MENU_ROWS):
        y = MENU_ROWS[selected][1] - 6
        hi = pygame.Surface((170, MENU_ROW_H + 10), pygame.SRCALPHA)
        hi.fill((180, 120, 40, 70))
        surface.blit(hi, (716, y))
        pygame.draw.polygon(surface, COLOR_GOLD,
                            [(700, y + 8), (700, y + 30), (712, y + 19)])
    draw_text(surface, assets.font(13), "↑/↓ 选择   Enter 确认   C 继续游戏   F1 设置",
              (WIDTH // 2, HEIGHT - 18), (230, 224, 205), anchor="center")


def _custom_bg(surface: pygame.Surface, assets: Assets) -> None:
    """为缺少专属美术素材的少量界面提供纸质纹理背景。"""
    try:
        surface.blit(assets.image("ui/skill_bg.png"), (0, 0))
    except (KeyError, FileNotFoundError):
        surface.fill((16, 16, 18))


def draw_slots(surface: pygame.Surface, assets: Assets, selected: int,
               saves: dict[int, dict | None]) -> None:
    _custom_bg(surface, assets)
    shade = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
    shade.fill((6, 8, 10, 170))
    surface.blit(shade, (0, 0))
    # 技能商店背景图上方自带“悟空/唐僧”页签，存档界面用不到，
    # 所以先用色块把这一条盖住。
    top = pygame.Surface((WIDTH, 58), pygame.SRCALPHA)
    top.fill((8, 8, 10, 255))
    surface.blit(top, (0, 0))
    draw_text(surface, assets.font(30, bold=True), "存档",
              (WIDTH // 2, 30), COLOR_GOLD, anchor="center")
    role_names = {1: "唐僧", 2: "悟空", 3: "双人"}
    for i in range(1, 4):
        y = 120 + (i - 1) * 125
        rect = pygame.Rect(180, y, 580, 100)
        is_sel = i - 1 == selected
        pygame.draw.rect(surface, (70, 52, 28) if is_sel else (24, 28, 28),
                         rect, border_radius=10)
        pygame.draw.rect(surface, COLOR_GOLD if is_sel else (90, 90, 80),
                         rect, 2, border_radius=10)
        data = saves.get(i)
        draw_text(surface, assets.font(20, bold=True), f"存档 {i} · {role_names[i]}",
                  (rect.x + 22, rect.y + 12), COLOR_WHITE)
        if data:
            p = data.get("player", {})
            draw_text(surface, assets.font(15),
                      f"Lv.{p.get('level', 1)}   灵魂 {data.get('soul', 0)}   "
                      f"通关 {len(data.get('cleared_levels', []))} 关",
                      (rect.x + 22, rect.y + 46), (230, 228, 210))
            draw_text(surface, assets.font(11), data.get("saved_at") or "",
                      (rect.x + 22, rect.y + 71), (170, 170, 155))
        elif i == 3:
            draw_text(surface, assets.font(14), "双人战役暂未开放，可在竞技场本地双人",
                      (rect.x + 22, rect.y + 45), (170, 170, 155))
        else:
            draw_text(surface, assets.font(16), "空 槽 — 新游戏",
                      (rect.x + 22, rect.y + 45), (170, 170, 155))
    draw_text(surface, assets.font(12),
              "↑/↓ 选择    Enter 进入    Delete 删除    Esc 返回",
              (WIDTH // 2, 540), (220, 220, 200), anchor="center")


# -------------------------------------------------------------- 选择英雄
# 为兼容早期工具而保留的备用绘制函数；实际入口使用 hero_select.HeroSelectScene。
def draw_hero_select(surface: pygame.Surface, assets: Assets, selected: int,
                     has_saves: dict[str, bool] | None = None) -> None:
    surface.fill((228, 228, 220))
    art = assets.image("ui/select_role.png")
    scale = 0.9
    art = pygame.transform.smoothscale(
        art, (int(art.get_width() * scale), int(art.get_height() * scale)))
    ox, oy = 0, (HEIGHT - art.get_height()) // 2
    surface.blit(art, (ox, oy))
    # 右侧两个门洞在素材里是透明的，正好用来放英雄预览图
    # 和 1P 光标。
    doors = [(232, 62, 176, 430), (442, 62, 176, 430)]
    for i, (hero, label, door) in enumerate(zip(
            ("wukong", "tangseng"), ("孙悟空", "唐僧"), doors)):
        x, y, w, h = door
        clip = assets.clips.get(f"{hero}/wait")
        if clip:
            frame = pygame.transform.flip(clip.frames[0], True, False)
            fh = min(230, h - 36)
            fw = int(frame.get_width() * fh / max(1, frame.get_height()))
            frame = pygame.transform.smoothscale(frame, (fw, fh))
            surface.blit(frame, (x + (w - fw) // 2, y + h - fh - 28))
        draw_text(surface, assets.font(22, bold=True), label,
                  (x + w // 2, y + h - 8), (30, 28, 24), shadow=False, anchor="midbottom")
        if i == selected:
            pygame.draw.rect(surface, (245, 200, 60), (x + 4, y + 2, w - 8, h - 4), 4)
            try:
                arrow = assets.image("ui/players/1P.png")
                arrow = pygame.transform.smoothscale(arrow, (60, 60))
                surface.blit(arrow, (x + 8, y + 4))
            except (KeyError, FileNotFoundError):
                pass
        if has_saves and has_saves.get(hero):
            draw_text(surface, assets.font(11), "已有存档·继续", (x + w // 2, y + 34),
                      (250, 235, 180), anchor="midtop")
        stats = ("HP 80  MP 50  ATK 10-14" if hero == "wukong"
                 else "HP 50  MP 100  ATK 12")
        draw_text(surface, assets.font(10), stats, (x + w - 6, y + 12),
                  (30, 28, 24), shadow=False, anchor="topright")
    draw_text(surface, assets.font(12), "←/→ 选择    Enter 确认    Esc 返回",
              (WIDTH // 2, HEIGHT - 14), (30, 30, 26), shadow=False, anchor="center")


# ------------------------------------------------------------------- 暂停
def draw_pause(surface: pygame.Surface, assets: Assets, selected: int) -> None:
    """旧工具用的兜底绘制函数，已弃用；实际入口使用 game.setmenu.SetMenu。"""
    # 暂停菜单的整屏填充在素材里不透明度为 0（只用于命中判定），
    # pause.png 只画了面板本身，因此这个兜底函数需要自己补一层压暗遮罩。
    # ``pause.png`` 同样只有笔画面板，所以在这里加暗色背景。
    shade = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
    shade.fill((0, 0, 0, 150))
    surface.blit(shade, (0, 0))
    try:
        art = assets.image("ui/pause.png")
        surface.blit(art, (0, 0))
    except (KeyError, FileNotFoundError):
        pass
    rows = [145, 198, 251, 304, 357]
    if 0 <= selected < len(rows):
        hi = pygame.Rect(316, rows[selected] - 6, 300, 34)
        overlay = pygame.Surface(hi.size, pygame.SRCALPHA)
        overlay.fill((180, 120, 40, 70))
        surface.blit(overlay, hi.topleft)
        pygame.draw.rect(surface, (235, 200, 100), hi, 2, border_radius=6)


# 失败面板上的按钮文字已经烘焙进 ``ui/fail.png``：重新挑战的橙色文字
# 占 x=310..424，返回地图占 x=470..588（y=398..422）。
RESULT_BUTTON_RECTS = (
    pygame.Rect(304, 392, 124, 38),
    pygame.Rect(464, 392, 126, 38),
)


def draw_result(surface: pygame.Surface, assets: Assets, won: bool, player,
                t: float, selected: int = 0,
                show_focus: bool = True) -> None:
    shade = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
    shade.fill((0, 0, 0, 120 if won else 170))
    surface.blit(shade, (0, 0))
    if won:
        draw_text(surface, assets.font(46, bold=True), "关卡完成",
                  (WIDTH // 2, 180), COLOR_GOLD, anchor="center")
        if player:
            draw_text(surface, assets.font(20),
                      f"等级 {player.level_no}   经验 {player.exp}/{player.exp_need}",
                      (WIDTH // 2, 260), COLOR_WHITE, anchor="center")
        draw_text(surface, assets.font(18), "进度已保存   Enter 返回地图",
                  (WIDTH // 2, 340), (220, 230, 200), anchor="center")
        return
    try:
        art = assets.image("ui/fail.png")
        surface.blit(art, (0, 0))
    except (KeyError, FileNotFoundError):
        draw_text(surface, assets.font(48, bold=True), "挑战失败",
                  (WIDTH // 2, 200), (230, 90, 70), anchor="center")
    # 两个选项的文字已经烘焙在图上；下面的高亮框与文字范围对齐
    if show_focus:
        rect = RESULT_BUTTON_RECTS[max(0, min(1, selected))]
        pygame.draw.rect(surface, (245, 200, 70), rect, 2, border_radius=6)
    draw_text(surface, assets.font(12), "←/→ 选择   Enter 确认",
              (WIDTH // 2, HEIGHT - 18), (230, 230, 210), anchor="center")


# 当前背包由 game.backpack.Backpack 绘制；旧的 draw_inventory() 辅助函数
# 已经删除，避免那套过时的 54 px 行高与偏移量布局被误用。
# -------------------------------------------------------------- 技能商店
def draw_skill_shop(surface: pygame.Surface, assets: Assets, hero: str,
                    state: dict, soul: int, selected: int, msg: str,
                    tab: int = 0) -> None:
    surface.fill((10, 10, 10))
    try:
        surface.blit(assets.image("ui/skill_bg.png"), (0, 0))
    except (KeyError, FileNotFoundError):
        pass
    draw_text(surface, assets.font(14, bold=True), "孙悟空" if hero == "wukong" else "唐僧",
              (40, 18), COLOR_GOLD)
    draw_text(surface, assets.font(16, bold=True), f"灵魂 {soul}",
              (WIDTH - 30, 18), COLOR_GOLD, anchor="topright")
    draw_text(surface, assets.font(12), "Tab 切换 主动/被动",
              (WIDTH - 30, 44), (235, 228, 205), anchor="topright")

    if tab == 0:
        if hero == "wukong":
            try:
                table = assets.image("ui/skill_table_wukong.png")
                tx, ty = 110, 90
                surface.blit(table, (tx, ty))
                # 购买后用“已学会”图标盖住原来的“学习”图标
                for i in range(5):
                    if state["active"][i]:
                        try:
                            icon = assets.image(f"ui/skills/w{i + 1}_learned.png")
                            icon = pygame.transform.smoothscale(icon, (64, 64))
                            surface.blit(icon, (tx + 85, ty + 20 + i * 73))
                        except (KeyError, FileNotFoundError):
                            pass
            except (KeyError, FileNotFoundError):
                pass
        else:
            tx, ty = 95, 80
            try:
                table = assets.image("ui/skill_table_tangseng.png")
                surface.blit(table, (tx, ty))
            except (KeyError, FileNotFoundError):
                table = None
            # 唐僧技能表里已经用白色文字画好了名称、价格、按键与说明，
            # 只需把已学会条目上的“学习”图标替换掉。
            if table is not None:
                for i in range(5):
                    if state["active"][i]:
                        try:
                            icon = assets.image(f"ui/skills/t{i + 1}_learned.png")
                            icon = pygame.transform.smoothscale(icon, (64, 64))
                            surface.blit(icon, (tx + 88, ty + 28 + i * 76))
                        except (KeyError, FileNotFoundError):
                            pass
        rows = 5
    else:
        draw_text(surface, assets.font(15, bold=True), "被动技能",
                  (WIDTH // 2, 78), COLOR_GOLD, anchor="center")
        rows = 5
        for i in range(5):
            s = SKILLS[hero]["passive"][i]
            lv = state["passive"][i]
            y = 120 + i * 68
            rect = pygame.Rect(110, y, 720, 54)
            if i == selected:
                pygame.draw.rect(surface, (120, 80, 30), rect, border_radius=8)
                pygame.draw.rect(surface, COLOR_GOLD, rect, 2, border_radius=8)
            draw_text(surface, assets.font(16, bold=True), s["name"],
                      (rect.x + 16, rect.y + 8), (245, 240, 215))
            draw_text(surface, assets.font(12), f"Lv.{lv}/5",
                      (rect.x + 120, rect.y + 10), (240, 210, 120))
            draw_text(surface, assets.font(11, system=True), s["desc"],
                      (rect.x + 210, rect.y + 10), (220, 215, 200))
            cost = passive_cost(lv) if lv < 5 else 0
            draw_text(surface, assets.font(13, bold=True),
                      "已满级" if lv >= 5 else f"{cost} 灵魂",
                      (rect.right - 16, rect.y + 10), (245, 210, 120), anchor="topright")
    if msg:
        draw_text(surface, assets.font(12, system=True), msg,
                  (WIDTH // 2, HEIGHT - 26), (245, 210, 120), anchor="center")
    draw_text(surface, assets.font(10), "↑/↓ 选择   Enter 购买/升级   Tab 切换   Esc 返回",
              (WIDTH // 2, HEIGHT - 10), (230, 225, 205), anchor="center")


# ---------------------------------------------------------------- 帮助/关于
# 帮助与关于两个界面共用同一个“返回”按钮：按 58x26 的基准尺寸放大到
# (92, 41) 使用。按钮的常态图与悬停图分开导出，背景也各自独立，
# 以便像其他界面一样实现悬停高亮。
HELP_BACK_POS = (842, 10)
ABOUT_BACK_POS = (817, 530)
BACK_BTN_SIZE = (92, 41)


def _blit_back_button(surface: pygame.Surface, assets: Assets,
                      pos: tuple[int, int], hover: bool) -> None:
    name = "ui/btnback_over.png" if hover else "ui/btnback_up.png"
    try:
        img = assets.image(name)
    except (KeyError, FileNotFoundError):
        return
    img = pygame.transform.smoothscale(img, BACK_BTN_SIZE)
    surface.blit(img, pos)


def draw_help(surface: pygame.Surface, assets: Assets, hover_back: bool = False) -> None:
    try:
        surface.blit(assets.image("ui/help_bg.png"), (0, 0))
        _blit_back_button(surface, assets, HELP_BACK_POS, hover_back)
    except (KeyError, FileNotFoundError):
        # 旧的合成图里已经烘焙了常态的“返回”按钮。
        try:
            surface.blit(assets.image("ui/help.png"), (0, 0))
        except (KeyError, FileNotFoundError):
            surface.fill((20, 18, 16))


def draw_about(surface: pygame.Surface, assets: Assets, hover_back: bool = False) -> None:
    try:
        surface.blit(assets.image("ui/about_bg.png"), (0, 0))
        _blit_back_button(surface, assets, ABOUT_BACK_POS, hover_back)
    except (KeyError, FileNotFoundError):
        try:
            surface.blit(assets.image("ui/about.png"), (0, 0))
        except (KeyError, FileNotFoundError):
            surface.fill((20, 18, 16))


# --------------------------------------------------------------- 设置
def draw_settings(surface: pygame.Surface, assets: Assets, selected: int, settings: dict) -> None:
    surface.fill((14, 16, 18))
    _custom_bg(surface, assets)
    shade = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
    shade.fill((0, 0, 0, 150))
    surface.blit(shade, (0, 0))
    top = pygame.Surface((WIDTH, 58), pygame.SRCALPHA)
    top.fill((8, 8, 10, 255))
    surface.blit(top, (0, 0))
    draw_text(surface, assets.font(30, bold=True), "设 置", (WIDTH // 2, 30), COLOR_GOLD,
              anchor="center")
    rows = [("静音", "开" if settings.get("muted") else "关"),
            ("音乐音量", f"{int(float(settings.get('music', 0.35)) * 100)}%"),
            ("音效音量", f"{int(float(settings.get('sfx', 0.6)) * 100)}%"),
            ("返回标题", "")]
    for i, (name, value) in enumerate(rows):
        rect = pygame.Rect(230, 140 + i * 66, 480, 46)
        if i == selected:
            pygame.draw.rect(surface, (110, 78, 30), rect, border_radius=8)
            pygame.draw.rect(surface, COLOR_GOLD, rect, 2, border_radius=8)
        draw_text(surface, assets.font(18), name, (rect.x + 22, rect.y + 12), COLOR_WHITE)
        draw_text(surface, assets.font(18), value, (rect.right - 22, rect.y + 12),
                  COLOR_GOLD, anchor="topright")
    draw_text(surface, assets.font(11), "↑/↓ 选择  ←/→ 调整  Enter 确认  Esc 返回",
              (WIDTH // 2, 460), (230, 225, 205), anchor="center")


# ---------------------------------------------------------- 开场/结局
def _fit_image(surface: pygame.Surface, img: pygame.Surface, max_w: int, max_h: int,
               center=(WIDTH // 2, HEIGHT // 2)) -> pygame.Rect:
    scale = min(max_w / max(1, img.get_width()), max_h / max(1, img.get_height()))
    w, h = max(1, int(img.get_width() * scale)), max(1, int(img.get_height() * scale))
    img = pygame.transform.smoothscale(img, (w, h))
    rect = img.get_rect(center=center)
    surface.blit(img, rect)
    return rect


OPENING_SKIP_RECT = pygame.Rect(WIDTH - 100, HEIGHT - 48, 88, 38)


def draw_opening_skip_button(surface: pygame.Surface, assets: Assets,
                             hover: bool = False) -> None:
    """开场跳过按钮：白色圆角描边，中间写“跳过”。"""
    rect = OPENING_SKIP_RECT
    pygame.draw.rect(surface, (0, 0, 0), rect, border_radius=7)
    pygame.draw.rect(surface, (245, 245, 240), rect, 2, border_radius=7)
    draw_text(surface, assets.font(15, True), "跳过", rect.center,
              (245, 245, 240), anchor="center")


def draw_opening(surface: pygame.Surface, assets: Assets, hero: str = "wukong",
                 page: int = 0) -> None:
    """开场叙事的兜底画面：仅在资源缺失时显示。

    正式的开场剧情由 ``game/narration.py`` 按数据驱动播放；只有当
    ``data/narration.json`` 或 ``assets/opening`` 缺失时，才退回这张静态卡片。
    """
    surface.fill((0, 0, 0))
    shade = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
    shade.fill((4, 8, 6, 220))
    surface.blit(shade, (0, 0))
    title = "序 章 · 七魔王篇"
    draw_text(surface, assets.font_for(title, 32, True), title,
              (WIDTH // 2, 70), COLOR_GOLD, anchor="center")
    lines = ["牛魔王被封印在太上老君的炼丹炉里，",
             "已有千年之久……", "一股邪恶的力量解开了封印，",
             "救世主的到来……"]
    for i, line in enumerate(lines):
        draw_text(surface, assets.font_for(line, 22), line,
                  (WIDTH // 2, 210 + i * 46), (235, 235, 220), anchor="center")
    clip = assets.clips.get(f"{'tangseng' if hero == 'tangseng' else 'wukong'}/wait")
    if clip:
        frame = pygame.transform.flip(clip.frames[0], True, False)
        frame = pygame.transform.scale(
            frame, (max(1, int(frame.get_width() * 1.7)),
                    max(1, int(frame.get_height() * 1.7))))
        surface.blit(frame, (WIDTH // 2 - frame.get_width() // 2, 340))
    draw_opening_skip_button(surface, assets)


def draw_ending(surface: pygame.Surface, assets: Assets, hero: str = "wukong") -> None:
    """结局旁白的兜底绘制：即使背景图等资源缺失也照常显示。"""
    surface.fill((0, 0, 0))
    try:
        bg = assets.image("ending/bg.png")
        surface.blit(bg, (0, 0))
    except (KeyError, FileNotFoundError):
        pass
    shade = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
    shade.fill((6, 4, 4, 150))
    surface.blit(shade, (0, 0))
    draw_text(surface, assets.font_for("终 了", 46, True), "终 了",
              (WIDTH // 2, 90), COLOR_GOLD, anchor="center")
    lines = ["造梦西游一《七魔王篇》终了", "浩劫之际，决战牛魔谷。",
             "微风徐徐，乌云渐散，", "只见朝霞映天，大地初醒。"]
    for i, line in enumerate(lines):
        draw_text(surface, assets.font_for(line, 22), line,
                  (WIDTH // 2, 210 + i * 52), (235, 230, 210), anchor="center")
    draw_text(surface, assets.font(11), "任意键返回地图", (WIDTH - 18, HEIGHT - 16),
              (235, 228, 205), anchor="bottomright")


# ---------------------------------------------------------------- 竞技场状态栏
def draw_arena_hud(surface: pygame.Surface, assets: Assets, arena) -> None:
    for player, x, name in ((arena.p1, 16, "1P 悟空"), (arena.p2, WIDTH - 336, "2P 唐僧")):
        panel = pygame.Surface((320, 70), pygame.SRCALPHA)
        pygame.draw.rect(panel, (10, 10, 10, 190), panel.get_rect(), border_radius=8)
        pygame.draw.rect(panel, (190, 160, 90, 160), panel.get_rect(), 1, border_radius=8)
        surface.blit(panel, (x, 10))
        draw_text(surface, assets.font(14, bold=True), name, (x + 12, 16), COLOR_GOLD)
        draw_bar(surface, x + 12, 38, 290, 13, player.hp / max(1, player.max_hp), COLOR_HP)
        draw_bar(surface, x + 12, 55, 290, 8, player.mp / max(1, player.max_mp), COLOR_MP)
    draw_text(surface, assets.font(10),
              "1P: A/D K J U I O L      2P: ←/→ 2 1 C D E F 0      Esc 退出",
              (WIDTH // 2, HEIGHT - 14), (230, 225, 205), anchor="center")
    if arena.finished:
        shade = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
        shade.fill((0, 0, 0, 150))
        surface.blit(shade, (0, 0))
        draw_text(surface, assets.font(44, bold=True), f"{arena.winner} 胜利",
                  (WIDTH // 2, 220), COLOR_GOLD, anchor="center")
        draw_text(surface, assets.font(18), "Enter 返回地图",
                  (WIDTH // 2, 300), COLOR_WHITE, anchor="center")


# --------------------------------------------------------------- 可选界面
def draw_codex(surface: pygame.Surface, assets: Assets, tab: int, selected: int) -> None:
    """可选的本地图鉴界面（需求 FR-23）；只读取并展示数据，没有外部画面素材。"""
    surface.fill((10, 14, 12))
    draw_text(surface, assets.font(30, bold=True), "图 鉴", (WIDTH // 2, 26), COLOR_GOLD,
              anchor="center")
    rows = sorted(MONSTER_STATS.items()) if tab == 0 else ITEMS
    start = max(0, min(selected - 7, len(rows) - 15))
    for i in range(start, min(len(rows), start + 15)):
        if tab == 0:
            num, m = rows[i]
            label = f"Monster{num}  {m.get('name', '')}"
        else:
            label = rows[i].get("name", "")
        draw_text(surface, assets.font(12, system=True), label,
                  (70, 96 + (i - start) * 28), COLOR_WHITE)


def draw_item_shop(surface: pygame.Surface, assets: Assets, stock: list[dict],
                   soul: int, selected: int, msg: str) -> None:
    """装备商店界面：本地附加功能，不在主线流程内。"""
    surface.fill((14, 16, 18))
    draw_text(surface, assets.font(26, bold=True), "装 备 商 店",
              (WIDTH // 2, 30), COLOR_GOLD, anchor="center")
    for i, item in enumerate(stock[:10]):
        draw_text(surface, assets.font(12, system=True),
                  f"{item['name']}  {sell_price(item) * 4} 灵魂",
                  (70, 100 + i * 36), COLOR_WHITE)
    draw_text(surface, assets.font(14), f"灵魂 {soul}", (WIDTH - 30, 60),
              COLOR_GOLD, anchor="topright")
