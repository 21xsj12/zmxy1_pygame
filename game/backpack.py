"""背包控制器与渲染器。

下面的布局常量都取自实际画面坐标：

* 面板位图：(130,20) 701x489，5x5 格子的原点 (484.3,120.35)，格宽 50，间距 7
* 12 个文本域：位置、尺寸、对齐都按实测值
* 四个装备容器、人物预览、页签与关闭按钮的命中区

模块沿用现有的 JSON 存档格式（一个背包列表 + 一张装备槽映射表），但在读取时
把存档里的物品字段实时统一格式，因此旧存档仍可正常载入。
"""
from __future__ import annotations

import math
from typing import Any

import pygame

from . import ui
from .constants import WIDTH
from .entities import hero_stats
from .items import (MAX_BAG, appearance_id, bonuses, can_equip, exchange_catalog,
                    item_gongxun, item_icon, item_value, quality_color,
                    roll_exchange, sell_price)
from .save import new_save, write_save
from .skills import normalize, passive_bonus

# 画面坐标。整张面板图贴在 (126,14)，这样它 701x489 的内容正好落在 (130,20)；
# 位图四周自带透明留边，所以贴图位置比内容区左上角各少 4 像素和 6 像素。
PANEL_BLIT = (126, 14)
PANEL_RECT = pygame.Rect(130, 20, 701, 489)
GRID_X, GRID_Y = 484.3, 120.35
CELL = 50
GAP = 7
PITCH = CELL + GAP
COLS = ROWS = 5
SLOT_POS = {
    "zbwq": (314.15, 159.35),
    "zbsp": (378.0, 159.35),
    "zbfj": (314.15, 222.35),
    "zbfb": (378.0, 222.35),
}
TAB_RECTS = {
    "equip": pygame.Rect(481, 85, 75, 29),
    "merit": pygame.Rect(556, 85, 73, 27),
}
CLOSE_RECT = pygame.Rect(771, 29, 40, 40)
MENU_THREE = (87, 115)
MENU_ONE = (87, 41)
SLOT_ORDER = ("zbwq", "zbfj", "zbsp", "zbfb")
HERO_NAMES = {"wukong": "孙悟空", "tangseng": "唐僧"}
HERO_CLASS = {"wukong": "战士", "tangseng": "法师"}

# 属性行（txt_hp 到 txt_hl）的文本中心坐标，各行的位置互相独立。
TEXT_POS = {
    "txt_hp": (260.3, 304.7),
    "txt_mp": (391.98, 304.28),
    "txt_att": (258.8, 335.28),
    "txt_def": (392.4, 335.28),
    "txt_baoji": (260.4, 365.73),
    "txt_sb": (392.4, 366.28),
    "txt_hx": (260.4, 396.28),
    "txt_hl": (392.4, 396.28),
}

# 这四个文本域的位置需要额外校正：文本域的实际绘制区 = 放置坐标 + 文本边界
# 偏移，而它们都把左上边界声明为 -2 像素，所以真实文本框比下面
# 记录的放置坐标再向左上各偏 2 像素：
#   txt_name 在 (233,116)，尺寸 105x18；txt_exp 在 (275,418.45)，尺寸 97.95x20.45
#   txt_lh 在 (523.75,420.45)，尺寸 78.4x20.45
#   txt_zg 在 (679.75,420.45)，尺寸 78.4x20.45
NAME_FIELD = (231.0, 114.0, 105.0, 18.0)
EXP_FIELD = (273.0, 416.45, 97.95, 20.45)
LH_FIELD = (521.75, 418.45, 78.4, 20.45)
ZG_FIELD = (677.75, 418.45, 78.4, 20.45)
# 14 像素行高的文字在文本框内实际墨迹的位置，按实机截图测量得到：
#   "999999" 的墨迹 (525,424)-(570,433)  -> 相对 LH_FIELD 偏移 +3.25 / +5.55
#   exp 的墨迹 (300,422)-(345,431)       -> 相对 EXP_FIELD 偏移 +3.25 / +5.55
#   name 的墨迹 (255,116)-(309,129)      -> 中心 y 122.5，落在 (231,114,105,18) 内
FLASH_INK_DX = 3.25
FLASH_INK_DY = 5.55
NAME_INK_CY = 122.5
EXP_LINE_H = 17

# 人物预览：在 240x260 画布上按战斗里的站立动作合成（朝左、不镜像——预览用的
# 层变换与战斗中站立时完全一致，没有额外翻转），**不缩放**：预览层的缩放比例
# 固定为 1.0。
PREVIEW_W, PREVIEW_H = 240, 260
PREVIEW_OX, PREVIEW_OY = 120, 190
PREVIEW_SCALE = 1.0                 # 预览缩放固定 1.0（早期实现用过 1.15，会把人放大）
PREVIEW_FPS = 24
# 待机动作：悟空用 62 帧循环的待机片段（第 63 帧之后是爬杆过渡，预览不需要）；
# 唐僧的待机素材只有 1 帧，所以画面静止不动。
# 实机确认：悟空站立时有待机动作、唐僧不动。
PREVIEW_FRAMES_WUKONG = 62
PREVIEW_STATIC_HEROES = ("tangseng",)
# 合成图贴在画面的哪个位置：把合成后的美术包围盒对齐实机测得的人物包围盒。
#   唐僧 实机 (204,162)-(280,266)，合成 bbox (74,83)-(148,185)  -> +(131,80)
#   悟空 实机 (197,173)-(310,269)，合成 union bbox (70,93)-(198,194) -> +(127,75)
# 实机包围盒 = 12 帧连拍取时间维静态部分 + 高通滤波（剔除会动的光晕）。
# 预览合成用的是战斗站立美术：金箍棒右端到 327，比背包待机美术的 310 长 17px，
# 会盖住第一个装备槽 (314,159)，所以绘制时再按素材包围盒裁边
# （见 PREVIEW_ART_BBOX / build_preview_surface），保证四边都落在量得的范围内。
PREVIEW_POS = {"tangseng": (131, 80), "wukong": (127, 75)}
PREVIEW_POS_DEFAULT = (131, 80)
# 人物预览允许出现的美术包围盒（画面坐标，含 1px 容差）。合成图只允许画在这
# 个矩形里：超出部分是当前美术自带的差异，会压住槽位，必须裁掉。
PREVIEW_ART_BBOX = {"tangseng": (204, 162, 280, 266),
                    "wukong": (197, 173, 310, 269)}

# 光晕动画：48 帧 = 5 张 192x192 的光柱格（每格 2 帧 @24fps）配 48 帧 alpha。
# 可见矩形由嵌套的变换矩阵累乘算出：
#   面板层把光晕放在 (254.55, 209.00)；
#   光晕层再缩放 (-1.2125549, -0.67329407)、平移 (116.4, 64.65)；
#   其中 192x192 的遮罩用裁剪深度 3 裁切，内部以 5x6 张 192x192 光柱格
#   的位图作填充。
# 可见矩形 = (138.14, 144.38) 232.81x129.27，双轴翻转，不裁剪。
HALO_RECT = (138.14, 144.38, 232.81, 129.27)
HALO_CELLS = ((0, 4), (1, 0), (1, 1), (1, 2), (1, 3))
HALO_FPS = 24
HALO_PEAK_ALPHA = 255
HALO_ALPHAS = (
    118, 124, 130, 136, 142, 148, 154, 160, 166, 172, 178, 184, 190, 196, 202,
    208, 214, 220, 226, 232, 238, 244, 250, 256, 250, 245, 239, 233, 227, 222,
    216, 210, 204, 198, 193, 187, 181, 176, 170, 164, 158, 153, 147, 141, 135,
    129, 124, 118,
)
# 面板底图用的是面板动画的第 1 帧，里面已经烘焙了光晕第 1 帧的亮度
# （alpha 合成上限 118）。活体光晕只补画剩余部分，避免两层叠加把亮度翻倍：
#     补画比例 drawn = (alpha 合成值 - 118) / (256 - 118)
# 于是第 1 帧 = 面板原样（最暗），第 24 帧 = 满亮度（最亮）。
HALO_BAKED_ALPHA = 118
HALO_ASSET = "ui/backpack_halo/{}.png"
_halo_cache: dict[int, pygame.Surface] = {}


def halo_alpha(t: float) -> int:
    """第 t 秒活体光晕要补画的 alpha（0..255，已扣掉面板里烘焙的第 1 帧）。"""
    value = HALO_ALPHAS[int(t * HALO_FPS) % len(HALO_ALPHAS)]
    drawn = (value - HALO_BAKED_ALPHA) / (256.0 - HALO_BAKED_ALPHA)
    return round(HALO_PEAK_ALPHA * max(0.0, min(1.0, drawn)))


def halo_cell_index(t: float) -> int:
    """第 t 秒显示 5 张光柱格里的哪一张（每格 2 帧）。"""
    frame = int(t * HALO_FPS) % len(HALO_ALPHAS)
    return (frame // 2) % len(HALO_CELLS)


def halo_surface(assets: Any, index: int) -> pygame.Surface:
    surf = _halo_cache.get(index)
    if surf is None:
        surf = assets.image(HALO_ASSET.format(index + 1))
        _halo_cache[index] = surf
    return surf


def preview_frame_index(hero: str, frames: int, t: float,
                        fps: int = PREVIEW_FPS) -> int:
    """背包预览播放到第几帧。

    * 悟空：62 帧循环的待机动作，24 fps，2.58 s 一圈。
    * 唐僧：待机素材只有 1 帧 —— 实机确认他站着不动，所以恒为第 1 帧。
    """
    if hero in PREVIEW_STATIC_HEROES:
        return 0
    cycle = max(1, min(frames, PREVIEW_FRAMES_WUKONG))
    return int(t * fps) % cycle


def preview_pos(hero: str) -> tuple[int, int]:
    return PREVIEW_POS.get(hero, PREVIEW_POS_DEFAULT)


def build_preview_surface(assets: Any, hero: str, frame: int,
                          cloth_id: int, weapon_id: int) -> pygame.Surface:
    """背包「人物预览」：战斗站立动作的合成图，朝左、不做镜像。

    合成后按 ``PREVIEW_ART_BBOX`` 裁边：预览用的站立美术金箍棒比背包里的待机
    美术长 17px，会压到第一个装备槽；裁掉的只是多出来的部分，四边与实机测得的
    包围盒对齐。
    """
    clip = assets.clips[f"{hero}/wait"]
    index = min(max(frame, 0), len(clip.frames) - 1)
    tmp = pygame.Surface((PREVIEW_W, PREVIEW_H), pygame.SRCALPHA)
    img = clip.frames[index]
    tmp.blit(img, (round(PREVIEW_OX - clip.anchor_x),
                   round(PREVIEW_OY - clip.anchor_y)))
    paper = getattr(assets, "paperdoll", None)
    if paper is not None and paper.ready:
        paper.draw(tmp, hero, "wait", index, PREVIEW_OX, PREVIEW_OY, -1,
                   cloth_id, weapon_id)
    if PREVIEW_SCALE != 1.0:
        tmp = pygame.transform.smoothscale(
            tmp, (round(PREVIEW_W * PREVIEW_SCALE), round(PREVIEW_H * PREVIEW_SCALE)))
    return _clip_preview_to_art_bbox(tmp, hero)


def _clip_preview_to_art_bbox(surface: pygame.Surface, hero: str) -> pygame.Surface:
    """只保留合成图里落在人物美术包围盒内的部分。"""
    box = PREVIEW_ART_BBOX.get(hero)
    if box is None:
        return surface
    x0, y0, x1, y1 = box
    px, py = preview_pos(hero)
    area = pygame.Rect(x0 - px, y0 - py, x1 - x0 + 1, y1 - y0 + 1)
    area = area.clip(surface.get_rect())
    if area.width <= 0 or area.height <= 0:
        return pygame.Surface(surface.get_size(), pygame.SRCALPHA)
    if area.size == surface.get_size():
        return surface
    out = pygame.Surface(surface.get_size(), pygame.SRCALPHA)
    out.blit(surface, area.topleft, area)
    return out


# ------------------------------------------------------------- 经验条
# 经验条动画共 30 帧，各层尺寸如下：
#   底槽：局部坐标 (-106.45,-9.9)，212.9x19.8，黑色，圆角 6.5
#   内槽：局部坐标 (-104.95,-8.4)，209.9x16.8，70% 黑，圆角 5
#   填充：240.1x17.75 的竖向线性渐变 (224,162,0) -> (244,237,58)；
#         左端 x = -225.1 + 7.3*(n-1)，因此可见宽度正好是 7.3*(n-1)
#   帧号 n = clamp(round(30 * cur / need), 1, 30)
#   txt_exp 文字在 depth 20，比条本身（depth 4）更靠上
EXP_TRACK = (221.7, 416.45, 212.9, 19.8)
EXP_TRACK_RADIUS = 6.5
EXP_INNER = (223.2, 417.95, 209.9, 16.8)
EXP_INNER_RADIUS = 5
EXP_FILL_STEP = 7.3
EXP_GRAD_TOP = (244, 237, 58)
EXP_GRAD_BOTTOM = (224, 162, 0)
EXP_FRAMES = 30
_exp_fill_cache: dict[int, pygame.Surface] = {}
_exp_mask: pygame.Surface | None = None


def exp_frame_index(cur: int, need: int) -> int:
    """经验条的帧号；取到第 0 帧会触发动画越界错误，所以夹到 1..30。"""
    if need <= 0:
        return 1
    n = int(math.floor(EXP_FRAMES * float(cur) / float(need) + 0.5))
    return max(1, min(EXP_FRAMES, n))


def exp_fill_width(frame: int) -> float:
    return min(EXP_INNER[2], EXP_FILL_STEP * (max(1, frame) - 1))


def _exp_mask_surface() -> pygame.Surface:
    global _exp_mask
    if _exp_mask is None:
        w, h = round(EXP_INNER[2]), round(EXP_INNER[3])
        _exp_mask = pygame.Surface((w, h), pygame.SRCALPHA)
        pygame.draw.rect(_exp_mask, (255, 255, 255, 255), _exp_mask.get_rect(),
                         border_radius=EXP_INNER_RADIUS)
    return _exp_mask


def exp_fill_surface(width: int) -> pygame.Surface:
    """竖向渐变填充，按内槽圆角裁切（宽到右端时右边也是圆角）。"""
    width = max(1, min(round(EXP_INNER[2]), int(width)))
    cached = _exp_fill_cache.get(width)
    if cached is not None:
        return cached
    h = round(EXP_INNER[3])
    surf = pygame.Surface((width, h), pygame.SRCALPHA)
    for row in range(h):
        t = row / max(1, h - 1)
        color = tuple(round(EXP_GRAD_TOP[i] + (EXP_GRAD_BOTTOM[i] - EXP_GRAD_TOP[i]) * t)
                      for i in range(3))
        surf.fill((*color, 255), (0, row, width, 1))
    surf.blit(_exp_mask_surface(), (0, 0), area=pygame.Rect(0, 0, width, h),
              special_flags=pygame.BLEND_RGBA_MULT)
    _exp_fill_cache[width] = surf
    return surf


# ------------------------------------------------------------ 文字墨迹
_INK_CACHE: dict[tuple[int, str], tuple[int, int, int, int]] = {}


def _ink_offset(font: pygame.font.Font, text: str,
                img: pygame.Surface) -> tuple[int, int, int, int]:
    """``img`` 里实际有像素的字形包围盒（按文字缓存）。"""
    key = (id(font), text)
    box = _INK_CACHE.get(key)
    if box is None:
        rects = pygame.mask.from_surface(img).get_bounding_rects()
        if rects:
            r = rects[0].copy()
            for q in rects[1:]:
                r.union_ip(q)
            box = (r.left, r.top, r.right, r.bottom)
        else:
            box = (0, 0, img.get_width(), img.get_height())
        if len(_INK_CACHE) > 512:
            _INK_CACHE.clear()
        _INK_CACHE[key] = box
    return box


def draw_ink_text(surface: pygame.Surface, font: pygame.font.Font, text: str,
                  color: tuple[int, int, int], *, left: float | None = None,
                  center_x: float | None = None, ink_top: float | None = None,
                  ink_center_y: float | None = None) -> pygame.Rect:
    """绘制 ``text``，让它的墨迹落在指定的锚点上。

    14 像素行高的文字，墨迹位于文本框顶部下方约 5.5 像素；而 pygame 渲染出的
    表面自带行 ascent 留白，所以要对齐的是墨迹框，而不是表面本身的左上角。
    """
    text = str(text)
    if not text:
        return pygame.Rect(0, 0, 0, 0)
    img = font.render(text, True, color)
    x0, y0, x1, y1 = _ink_offset(font, text, img)
    if left is not None:
        dx = left - x0
    elif center_x is not None:
        dx = center_x - (x0 + x1) / 2.0
    else:
        dx = 0
    if ink_top is not None:
        dy = ink_top - y0
    elif ink_center_y is not None:
        dy = ink_center_y - (y0 + y1) / 2.0
    else:
        dy = 0
    rect = img.get_rect()
    rect.topleft = (int(math.floor(dx + 0.5)), int(math.floor(dy + 0.5)))
    surface.blit(img, rect)
    return rect


def _wrap(text: str, font: pygame.font.Font, max_width: int) -> list[str]:
    """中文在任意字之间折行，并保留原文里的显式换行。"""
    lines, current = [], ""
    for ch in str(text):
        if ch == "\n":
            lines.append(current)
            current = ""
            continue
        if font.size(current + ch)[0] > max_width and current:
            lines.append(current)
            current = ch
        else:
            current += ch
    if current:
        lines.append(current)
    return lines


def _wrap_words(text: str, font: pygame.font.Font, max_width: int) -> list[str]:
    """按整词折行：拉丁文只在空格处断开，不把单词拆到两行。"""
    lines: list[str] = []
    for para in str(text).split("\n"):
        current = ""
        for word in para.split(" "):
            candidate = word if not current else current + " " + word
            if current and font.size(candidate)[0] > max_width:
                lines.append(current)
                current = word
            else:
                current = candidate
        lines.append(current)
    return lines


# ------------------------------------------------------------ 物品提示框
# 提示框的布局按实测值逐项确定：
#   info.x/y = 20/10，背景 = (info.width+35) x (info.height+10)，圆角 5，
#   1 像素白描边 + 70% 黑填充，info.width = 各行最右边缘的最大值。
TIP_INFO_XY = (20, 10)
TIP_ROW = 25
TIP_INS_W = 135
TIP_MEASURE_W = 100       # 测量行数时文本域仍保持默认的 100 像素宽
TIP_LINE_H14 = 17
TIP_VALUE_H = 27          # 14 像素的价值行：行高 17 + 10 边距
TIP_BOX_PAD = (35, 10)
TIP_BOX_RADIUS = 5
TIP_BOX_ALPHA = 178       # 0.7 * 255
TIP_MIN_W = 135           # 描述行和价值行都是 135 宽
TIP_ANCHOR = (25, 28)     # 锚点 (25,28)：对象放置点为 (25,30)，再上移 2 像素


def tooltip_fonts(assets: Any) -> dict[str, pygame.font.Font]:
    """提示框没有指定字体，因此回落到系统中文字体。"""
    return {
        "name": assets.font(16, system=True),
        "label": assets.font(16, bold=True, system=True),
        "value": assets.font(16, system=True),
        "attr": assets.font(16, bold=True, system=True),
        "small": assets.font(14, system=True),
        "measure": assets.font(16, system=True),
    }


def _pct(value: Any) -> str:
    try:
        return f"{float(value) * 100:g}%"
    except (TypeError, ValueError):
        return "0%"


def tooltip_rows(item: dict, is_gongxun: bool = False) -> list[str]:
    """8 条属性行，顺序固定，数值为 0 的条目不显示。"""
    stats = item.get("stats", {}) or {}
    out: list[str] = []
    for label, key, pct in (
            ("生命:", "hp", False), ("魔法:", "mp", False),
            ("攻击力:", "eatt", False), ("防御力:", "def", False),
            ("暴击率:", "crit", True), ("闪避率:", "miss", True),
            ("回血:", "hp_regen", False), ("回魔:", "mp_regen", False)):
        value = stats.get(key, 0)
        if key == "eatt" and not value:
            value = stats.get("atk", 0)
        if not value:
            continue
        out.append(f"{label} {_pct(value) if pct else value}")
    return out


def tooltip_layout(item: dict, fonts: dict[str, pygame.font.Font],
                   is_gongxun: bool = False) -> dict:
    """计算一件物品的完整提示框布局（外框尺寸 + 每一行的位置）。"""
    name = str(item.get("name", ""))
    etype = str(item.get("etype") or item.get("slot_name") or "")
    user = str(item.get("user") or "")
    type_text = f"{etype}·{user}" if user else etype
    pz = [("品质", str(item.get("quality", ""))),
          ("类型", type_text)]
    attrs = tooltip_rows(item, is_gongxun)
    instruction = str(item.get("instruction") or "")
    ins_lines = _wrap(instruction, fonts["small"], TIP_INS_W) if instruction else []
    # 统计行数时文本域仍保持默认的 100 像素宽；14 号中文字体每字前进 16 像素，
    # 因此一行大约放 6 个字。
    ins_100 = _wrap(instruction, fonts["measure"], TIP_MEASURE_W) if instruction else []
    ins_h = (len(ins_100) * TIP_LINE_H14 + 10) if instruction else 10
    if is_gongxun:
        value_text = f"兑换 : {item_gongxun(item)} 战功"
    else:
        value_text = f"价值 : {item_value(item)} 灵魂"

    def width(font, text: str) -> int:
        return int(font.size(text)[0])

    i = 1
    info_w = width(fonts["name"], name) + 10
    info_h = 25
    rows: list[dict] = []
    for label, value in pz:
        lw = width(fonts["label"], label) + 10
        vw = width(fonts["value"], "  " + value) + 10
        rows.append({"kind": "pz", "label": label, "value": value,
                     "label_y": i * TIP_ROW - 1, "label_w": lw,
                     "value_y": i * TIP_ROW, "value_x": lw})
        info_w = max(info_w, lw + vw)
        info_h = max(info_h, i * TIP_ROW + 25)
        i += 1
    for text in attrs:
        rows.append({"kind": "attr", "text": text, "y": i * TIP_ROW,
                     "w": width(fonts["attr"], text) + 10})
        info_w = max(info_w, width(fonts["attr"], text) + 10)
        info_h = max(info_h, i * TIP_ROW + 25)
        i += 1
    ins_y = i * TIP_ROW
    info_h = max(info_h, ins_y + ins_h)
    i += int(math.floor(ins_h / TIP_ROW + 0.5))          # 占用的行数，四舍五入
    value_y = i * TIP_ROW
    info_h = max(info_h, value_y + TIP_VALUE_H)
    info_w = max(info_w, TIP_MIN_W)
    return {
        "name": name,
        "rows": rows,
        "attrs": attrs,
        "ins_lines": ins_lines,
        "ins_y": ins_y,
        "ins_h": ins_h,
        "value_text": value_text,
        "value_y": value_y,
        "info_w": info_w,
        "info_h": info_h,
        "box_w": info_w + TIP_BOX_PAD[0],
        "box_h": info_h + TIP_BOX_PAD[1],
    }


class Backpack:
    def __init__(self, app: Any):
        self.app = app
        self.back = "map"
        self.tab = "equip"
        self.hover = -1
        self.equip_hover: str | None = None
        self.menu_slot = -1
        self.menu_hover = ""
        self.msg = ""
        self.msg_t = 0.0
        self.preview_t = 0.0
        self.keyboard_nav = False
        self.merit_items: list[dict] = []
        self.panel: pygame.Surface | None = None
        self._preview_gear_key: tuple | None = None
        self._preview_frames: dict[int, pygame.Surface] = {}

    # ------------------------------------------------------------------ 打开
    def open(self, back: str = "map") -> None:
        app = self.app
        if app.save_data is None:
            app.save_data = new_save(app.slot)
            app.has_save = True
        self.back = back
        self.tab = "equip"
        self.hover = -1
        self.equip_hover: str | None = None
        self.menu_slot = -1
        self.menu_hover = ""
        self.msg = ""
        self.msg_t = 0.0
        self.preview_t = 0.0
        self.keyboard_nav = False
        if not self.merit_items:
            for entry in exchange_catalog():
                item = roll_exchange(entry["fill"], self._next_uid())
                if item is not None:
                    self.merit_items.append(item)
        app.inv_sel = 0
        app.inv_msg = ""
        if back == "battle" and app.battle is not None:
            app.paused = True

    def close(self) -> None:
        app = self.app
        self.menu_slot = -1
        self.hover = -1
        self.equip_hover = None
        app.inv_msg = ""
        if self.back == "battle" and app.battle is not None:
            app.state = "battle"
            app.paused = False
        else:
            app.to_map()

    # ------------------------------------------------------------------ 数据
    def _data(self) -> dict:
        return self.app.save_data or {}

    def _hero(self) -> str:
        return str(self._data().get("hero", "wukong"))

    def _inv(self) -> list[dict]:
        data = self._data()
        inv = data.get("inventory")
        if not isinstance(inv, list):
            inv = []
            data["inventory"] = inv
        return inv

    def _pdata(self) -> dict:
        data = self._data()
        player = data.get("player")
        if not isinstance(player, dict):
            player = {}
            data["player"] = player
        return player

    def _equipment(self) -> dict:
        pdata = self._pdata()
        equipment = pdata.get("equipment")
        if not isinstance(equipment, dict):
            equipment = {}
            pdata["equipment"] = equipment
        return equipment

    def _battle_player(self):
        bp = getattr(self.app, "battle", None)
        if bp is not None and getattr(bp, "player", None) is not None:
            if bp.player.hero == self._hero():
                return bp.player
        return None

    def _next_uid(self) -> int:
        data = self._data()
        n = int(data.get("next_uid", 1))
        data["next_uid"] = n + 1
        return n

    def _items(self) -> list[dict]:
        if self.tab == "merit":
            return self.merit_items
        return self._inv()

    def _item(self, index: int) -> dict | None:
        items = self._items()
        if 0 <= index < len(items):
            return items[index]
        return None

    def _selected(self) -> int:
        return max(0, min(MAX_BAG - 1, int(getattr(self.app, "inv_sel", 0))))

    def _set_selected(self, index: int) -> None:
        self.app.inv_sel = max(0, min(MAX_BAG - 1, int(index)))

    def _msg(self, text: str) -> None:
        self.msg = text
        self.msg_t = 2.2
        self.app.inv_msg = text

    def _clear_msg(self) -> None:
        self.msg = ""
        self.msg_t = 0.0
        self.app.inv_msg = ""

    def _write(self) -> None:
        if self.app.save_data is not None:
            write_save(self.app.save_data, slot=self.app.slot)

    def _sync_battle(self) -> None:
        bp = self._battle_player()
        if bp is None:
            return
        bp.equipment = dict(self._equipment())
        bp.recalc_stats()

    # ------------------------------------------------------------------ 属性
    def _values(self) -> dict:
        hero = self._hero()
        pdata = self._pdata()
        bp = self._battle_player()
        if bp is not None:
            level = int(bp.level_no)
            exp = int(bp.exp)
            exp_need = int(bp.exp_need)
            hp, max_hp = int(bp.hp), int(bp.max_hp)
            mp, max_mp = int(bp.mp), int(bp.max_mp)
            atk_min, atk_max = int(bp.atk_min), int(bp.atk_max)
            defense = int(bp.defense)
            crit, miss = float(bp.crit), float(bp.miss)
            hp_regen, mp_regen = int(bp.hp_regen), int(bp.mp_regen)
        else:
            level = int(pdata.get("level", 1))
            stats = hero_stats(hero, level)
            eq = bonuses(self._equipment())
            pb = passive_bonus(hero, normalize(hero, pdata.get("skills")))
            max_hp = int(stats["max_hp"]) + pb["hp"] + eq["hp"]
            max_mp = int(stats["max_mp"]) + pb["mp"] + eq["mp"]
            hp = min(max_hp, int(pdata.get("hp", max_hp)))
            mp = min(max_mp, int(pdata.get("mp", max_mp)))
            atk_min = int(stats["atk_min"]) + eq["atk"]
            atk_max = int(stats["atk_max"]) + eq["atk"]
            defense = int(stats["defense"]) + eq["def"]
            crit = float(eq["crit"]) + pb["crit"]
            miss = float(eq["miss"])
            hp_regen = int(eq["hp_regen"]) + pb["hp_regen"]
            mp_regen = int(eq["mp_regen"]) + pb["mp_regen"]
            exp = int(pdata.get("exp", 0))
            exp_need = int(stats["exp_need"])
        att = str(atk_min) if atk_min == atk_max else f"{atk_min}~{atk_max}"
        return {
            "name": HERO_NAMES.get(hero, hero),
            "level": max(1, level),
            "hp": f"{hp} / {max_hp}",
            "mp": f"{mp} / {max_mp}",
            "att": att,
            "def": str(defense),
            "crit": f"{int(round(crit * 100))} %",
            "miss": f"{int(round(miss * 100))} %",
            "hx": str(hp_regen),
            "hl": str(mp_regen),
            "exp": f"{exp} / {exp_need}",
            "exp_cur": exp,
            "exp_need": exp_need,
            "exp_ratio": 0.0 if exp_need <= 0 else max(0.0, min(1.0, exp / exp_need)),
            "lh": str(int(self._data().get("soul", 0))),
            "zg": str(int(self._data().get("gongxun", 0))),
        }

    # ------------------------------------------------------------------ 面板
    def _make_panel(self, assets) -> pygame.Surface:
        panel = assets.image("ui/backpack.png").copy()

        def local(x: float, y: float, w: float, h: float) -> pygame.Rect:
            return pygame.Rect(round(x) - PANEL_BLIT[0], round(y) - PANEL_BLIT[1],
                               round(w), round(h))

        # 面板底图是合成图，仍带着烘焙进去的悟空人像；
        # ``ui/backpack_portrait.png`` 是去掉人像的同款面板背景，
        # 也就是人像卡的底板，且已烘焙好光柱动画的第 1 帧。
        # 它的最上面 6 行仍是旧昵称框的残边（y 128..133：白 247 / 灰 217 / 灰 39），
        # 直接贴会盖掉面板自己的昵称框下边线 y=131，所以从 y=134 开始、跳过源图
        # 前 6 行 —— 面板 (194,128)-(336,134) 条带保持不动，昵称框四边完整。
        card = local(194, 134, 116, 158)
        try:
            patch = assets.image("ui/backpack_portrait.png")
            panel.blit(patch, card.topleft, pygame.Rect(0, 6, card.width, card.height))
        except (KeyError, FileNotFoundError):
            for yy in range(card.height):
                for xx in range(card.width):
                    dx = (xx - card.width * 0.5) / (card.width * 0.5)
                    dy = (yy - card.height * 0.56) / (card.height * 0.56)
                    glow = max(0.0, 1.0 - (dx * 1.35) ** 2)
                    glow *= 0.58 + 0.42 * max(0.0, 1.0 - abs(dy))
                    beam = max(0.0, 1.0 - abs(dx) * 3.4) ** 2
                    beam *= 0.45 + 0.55 * max(0.0, 1.0 - abs(dy))
                    panel.set_at((card.x + xx, card.y + yy), (
                        14 + int(96 * glow) + int(42 * beam),
                        10 + int(72 * glow) + int(31 * beam),
                        6 + int(44 * glow) + int(18 * beam), 255))
        # 等级：面板底色是纯黑，所以 (350,107)-(433,146) 处烘焙的 "1" 和翅膀
        # 直接用纯黑覆盖，而不用 (24,22,20) 这种带灰的黑色。
        pygame.draw.rect(panel, (0, 0, 0), local(343, 100, 93, 49))
        # 灵魂框在面板图里本来就是空的；战功框里烘焙了一个 "0" 在
        # (681,423)-(687,433)，只用纯黑抹掉它。两个框的 1px 边线保持面板自带的样子
        # （早先的实现用 (20,20,20) 盖 78x17，会把右边线和下边线一起抹掉）。
        pygame.draw.rect(panel, (0, 0, 0), local(681, 423, 7, 11))
        return panel

    # ------------------------------------------------------------------ 绘制
    def draw(self, surface: pygame.Surface) -> None:
        app = self.app
        if not (self.back == "battle" and app.battle is not None):
            surface.fill((12, 12, 12))
        if self.panel is None:
            self.panel = self._make_panel(app.assets)
        surface.blit(self.panel, PANEL_BLIT)
        values = self._values()
        self._draw_exp(surface, values)
        self._draw_texts(surface, values)
        self._draw_level(surface, values)
        self._draw_halo(surface)
        self._draw_slots(surface)
        self._draw_grid(surface)
        self._draw_preview(surface)
        self._draw_menu(surface)
        self._draw_tooltip(surface)
        self._draw_message(surface)

    def _draw_texts(self, surface, v: dict) -> None:
        assets = self.app.assets
        value_font = assets.font(14)
        name_font = assets.font(14, bold=True, system=True)
        rows = [
            ("txt_hp", v["hp"]), ("txt_mp", v["mp"]),
            ("txt_att", v["att"]), ("txt_def", v["def"]),
            ("txt_baoji", v["crit"]), ("txt_sb", v["miss"]),
            ("txt_hx", v["hx"]), ("txt_hl", v["hl"]),
        ]
        for key, text in rows:
            x, y = TEXT_POS[key]
            ui.draw_text(surface, value_font, text, (x, y), (250, 250, 240),
                         shadow=False, anchor="center")
        # txt_lh / txt_zg：在 (521.75/677.75, 418.45) 的 78.4x20.45 区域内左对齐；
        # 实际墨迹从 (525,424) / (681,424) 开始。
        for field, text in ((LH_FIELD, v["lh"]), (ZG_FIELD, v["zg"])):
            draw_ink_text(surface, value_font, text, (250, 250, 240),
                          left=field[0] + FLASH_INK_DX,
                          ink_top=field[1] + FLASH_INK_DY)
        # txt_name：在 (231,114,105,18) 内居中，墨迹中心 y = 122.5。
        draw_ink_text(surface, name_font, v["name"], (245, 245, 230),
                      center_x=NAME_FIELD[0] + NAME_FIELD[2] / 2.0,
                      ink_center_y=NAME_INK_CY)

    def _draw_level(self, surface, v: dict) -> None:
        level = max(1, min(40, int(v["level"])))
        try:
            frame = self.app.assets.image(f"ui/backpack_level/{level}.png")
        except (KeyError, FileNotFoundError):
            ui.draw_text(surface, self.app.assets.font(18, bold=True),
                         str(level), (389.7, 124.35), (240, 240, 230),
                         shadow=False, anchor="center")
            return
        surface.blit(frame, (round(389.7 - frame.get_width() / 2),
                             round(124.35 - frame.get_height() / 2)))

    def _draw_exp(self, surface, v: dict) -> None:
        """经验条：在既有凹槽里画分级填充，文字压在填充之上。

        凹槽槽体已经烘焙在 ``ui/backpack.png`` 里（第 1 帧），
        所以这里只补画金色填充和位于 depth 20 的文字。
        """
        frame = exp_frame_index(int(v.get("exp_cur", 0)), int(v.get("exp_need", 0)))
        width = exp_fill_width(frame)
        if width >= 1:
            surface.blit(exp_fill_surface(width),
                         (round(EXP_INNER[0]), round(EXP_INNER[1])))
        # txt_exp：区域 (273,416.45,97.95,20.45)，居中、按词换行；20.45 px 的高度
        # 只放得下第一行 14 px 的字（所以 "80000 / 160000" 只显示出 "80000 /"，
        # 墨迹范围 (300,422)-(345,431)）。
        font = self.app.assets.font(14)
        lines = _wrap_words(str(v["exp"]), font, EXP_FIELD[2])
        if lines:
            draw_ink_text(surface, font, lines[0], (250, 250, 240),
                          center_x=EXP_FIELD[0] + EXP_FIELD[2] / 2.0,
                          ink_top=EXP_FIELD[1] + FLASH_INK_DY)

    def _draw_item_icon(self, surface, item: dict, x: int, y: int) -> None:
        """把物品图精确画在 50x50 的空槽/格子上。

        素材图标本身就是 50x50。早先的实现用 y-2（以及 44px 居中对齐）
        会让物品相对空框出现视觉偏移；改用格子左上角后，图标能完整盖住
        空的装备槽/标签。
        """
        try:
            icon = self.app.assets.image(item_icon(item))
        except (KeyError, FileNotFoundError):
            pygame.draw.rect(surface, quality_color(item),
                             pygame.Rect(x + 6, y + 6, 36, 36), 2)
            return
        # 多数图标素材就是 50x50；少数几个（dhqf/jgz/miss）导出时被裁紧了。
        # 把它们在 50x50 格子里居中，避免视觉上
        # 偏到空框的左上角。
        surface.blit(icon, (x + (CELL - icon.get_width()) // 2,
                            y + (CELL - icon.get_height()) // 2))

    def _draw_halo(self, surface) -> None:
        """人像卡背后的光晕：5 张光柱格 × 48 帧 alpha，24fps 两秒一圈。

        位置/尺寸由变换矩阵推出：(254.55,209) 处再叠加 (116.4,64.65) 且双轴翻转，
        得到可见矩形 (138.14,144.38) 232.81x129.27。光晕只垫在卡片背后、装备槽
        与人物之下（depth 44 < 46..55），所以 draw() 里先画光晕再画其余图层。
        面板位图已烘焙第 1 帧，这里只补画剩余 alpha（见 HALO_BAKED_ALPHA）。
        不裁剪：光柱实测范围 x 174..332 / y 145..271，裁到人像卡会把两侧切掉。
        """
        alpha = halo_alpha(self.preview_t)
        if alpha <= 0:
            return
        img = halo_surface(self.app.assets, halo_cell_index(self.preview_t))
        previous = img.get_alpha()
        img.set_alpha(alpha)
        surface.blit(img, (round(HALO_RECT[0]), round(HALO_RECT[1])))
        img.set_alpha(previous)

    def _draw_slots(self, surface) -> None:
        equipment = self._equipment()
        for slot, (x, y) in SLOT_POS.items():
            item = equipment.get(slot)
            if not item:
                continue
            self._draw_item_icon(surface, item, round(x), round(y))

    def _draw_grid(self, surface) -> None:
        assets = self.app.assets
        items = self._items()
        selected = self._selected()
        for i in range(MAX_BAG):
            col, row = i % COLS, i // COLS
            x, y = GRID_X + col * PITCH, GRID_Y + row * PITCH
            cell = pygame.Rect(round(x), round(y), CELL, CELL)
            try:
                surface.blit(assets.image("ui/pack_cell.png"), cell.topleft)
            except (KeyError, FileNotFoundError):
                pygame.draw.rect(surface, (30, 30, 30), cell)
            if i < len(items) and items[i]:
                self._draw_item_icon(surface, items[i], cell.x, cell.y)
            if i == self.hover and self.keyboard_nav:
                pygame.draw.rect(surface, (210, 210, 210), cell, 2)
            if i == selected and self.keyboard_nav:
                pygame.draw.rect(surface, (250, 210, 90), cell, 2)

    def _draw_preview(self, surface) -> None:
        assets = self.app.assets
        hero = self._hero()
        clip = assets.clips.get(f"{hero}/wait")
        if clip is None or not clip.frames:
            return
        # 悟空待机动画 62 帧（24fps，约 2.583s 一轮），唐僧待机只有 1 帧、
        # 保持静止；装备层固定在当前外观帧上（上衣映射到 cloth1..4 与头部，
        # 武器映射到 weapon1），所以换装只换外观，不会轮播装备帧。
        frame = preview_frame_index(hero, len(clip.frames), self.preview_t,
                                    clip.fps or PREVIEW_FPS)
        equipment = self._equipment()
        cloth_id = appearance_id(equipment.get("zbfj"))
        weapon_id = appearance_id(equipment.get("zbwq"))
        gear_key = (hero, cloth_id, weapon_id)
        if self._preview_gear_key != gear_key:
            self._preview_frames.clear()
            self._preview_gear_key = gear_key
        big = self._preview_frames.get(frame)
        if big is None:
            big = build_preview_surface(assets, hero, frame, cloth_id, weapon_id)
            self._preview_frames[frame] = big
        # 预览不会被裁到人像卡（只受面板本身的边界限制），所以这里不 set_clip。
        surface.blit(big, preview_pos(hero))

    # ------------------------------------------------------------------ 菜单
    def _menu_pos(self) -> tuple[int, int]:
        slot = self.menu_slot
        col, row = slot % COLS, slot // COLS
        x, y = GRID_X + col * PITCH, GRID_Y + row * PITCH
        w, h = MENU_ONE if self.tab == "merit" else MENU_THREE
        mx = min(max(int(x + 25), PANEL_RECT.left), PANEL_RECT.right - w)
        my = min(max(int(y + 25), PANEL_RECT.top), PANEL_RECT.bottom - h)
        return mx, my

    def _menu_buttons(self) -> list[tuple[str, pygame.Rect]]:
        if self.menu_slot < 0:
            return []
        mx, my = self._menu_pos()
        if self.tab == "merit":
            return [("exchange", pygame.Rect(mx + 4, my + 6, 73, 27))]
        return [
            ("equip", pygame.Rect(mx + 3, my + 4, 77, 31)),
            ("give", pygame.Rect(mx + 3, my + 40, 77, 31)),
            ("sell", pygame.Rect(mx + 3, my + 78, 77, 31)),
        ]

    def _menu_disabled(self, action: str) -> bool:
        if action == "give":
            return int(getattr(self.app, "play_num", 1)) != 2
        if action == "equip":
            item = self._item(self.menu_slot)
            return bool(item and item.get("class")
                        and item.get("class") != HERO_CLASS.get(self._hero()))
        return False

    def _draw_menu(self, surface) -> None:
        if self.menu_slot < 0 or self._item(self.menu_slot) is None:
            return
        mx, my = self._menu_pos()
        name = "ui/pack_onebtn.png" if self.tab == "merit" else "ui/pack_threebtn.png"
        try:
            surface.blit(self.app.assets.image(name), (mx, my))
        except (KeyError, FileNotFoundError):
            pygame.draw.rect(surface, (35, 35, 35), (mx, my, *(
                MENU_ONE if self.tab == "merit" else MENU_THREE)), border_radius=6)
        for action, rect in self._menu_buttons():
            disabled = self._menu_disabled(action)
            if disabled:
                dim = pygame.Surface(rect.size, pygame.SRCALPHA)
                dim.fill((0, 0, 0, 145))
                surface.blit(dim, rect.topleft)
            elif action == self.menu_hover:
                pygame.draw.rect(surface, (255, 240, 180), rect, 1, border_radius=3)

    def _tooltip_target(self) -> tuple[dict, float, float] | None:
        if self.menu_slot >= 0:
            return None
        if self.equip_hover:
            item = self._equipment().get(self.equip_hover)
            if item:
                x, y = SLOT_POS[self.equip_hover]
                return item, x + 25, y - 2 + 30
            return None
        if self.hover < 0:
            return None
        item = self._item(self.hover)
        if item is None:
            return None
        col, row = self.hover % COLS, self.hover // COLS
        return item, GRID_X + col * PITCH + 25, GRID_Y + row * PITCH + 28

    def _draw_tooltip(self, surface) -> None:
        """物品属性提示框的绘制，各元素位置按下面的布局规则确定。

        * 信息起点 = 框左上角 + (20,10)；每一行从 ``i * 25`` 定位
          （标签的 y 为 ``i*25 - 1``）；
        * 背景 = ``info.width + 35`` x ``info.height + 10``，圆角 5，
          在纯黑 0.7 填充上画 1 px 白描边；
        * 不向面板内夹紧：允许提示框溢出面板
          （只有负的左上角会被夹紧，而这里不会出现负坐标）。
        """
        target = self._tooltip_target()
        if target is None:
            return
        item, tip_x, tip_y = target
        fonts = tooltip_fonts(self.app.assets)
        lay = tooltip_layout(item, fonts, is_gongxun=(self.tab == "merit"))
        w, h = lay["box_w"], lay["box_h"]
        tx = max(0, round(tip_x))
        ty = max(0, round(tip_y))

        box = pygame.Surface((w, h), pygame.SRCALPHA)
        box.fill((0, 0, 0, TIP_BOX_ALPHA))          # 黑色半透明填充（不透明度 0.7）
        pygame.draw.rect(box, (255, 255, 255), box.get_rect(), 1,
                         border_radius=TIP_BOX_RADIUS)
        surface.blit(box, (tx, ty))

        cx = tx + TIP_INFO_XY[0]
        cy = ty + TIP_INFO_XY[1]
        for row in lay["rows"]:
            if row["kind"] == "pz":
                self._glow_label(surface, fonts["label"], row["label"],
                                 (cx, cy + row["label_y"]))
                # 品质分隔线：2 px 白色，alpha 从 1 渐变到 0，长 80 px，
                # 锚点在 (label.x + label.width - 4, label.y + 19)。
                self._gradient_line(surface, cx + row["label_w"] - 4,
                                    cy + row["label_y"] + 20)
                color = quality_color(item) if row["label"] == "品质" else (255, 255, 255)
                ui.draw_text(surface, fonts["value"], "  " + row["value"],
                             (cx + row["value_x"], cy + row["value_y"]),
                             color, shadow=False)
            elif row["kind"] == "attr":
                ui.draw_text(surface, fonts["attr"], row["text"],
                             (cx, cy + row["y"]), (255, 153, 51), shadow=False)
        for idx, line in enumerate(lay["ins_lines"]):
            ui.draw_text(surface, fonts["small"], line,
                         (cx, cy + lay["ins_y"] + idx * TIP_LINE_H14),
                         (255, 255, 255), shadow=False)
        ui.draw_text(surface, fonts["small"], lay["value_text"],
                     (cx, cy + lay["value_y"]), (255, 153, 51), shadow=False)
        # 名称字段最后才被添加到显示列表，所以永远盖在最上层。
        ui.draw_text(surface, fonts["name"], lay["name"], (cx, cy),
                     quality_color(item), shadow=False)

    def _glow_label(self, surface, font, text: str, pos) -> None:
        """黑色粗体标签，外加一圈白色发光的光晕。"""
        img = font.render(text, True, (255, 255, 255)).convert_alpha()
        for radius, alpha in ((3, 45), (2, 70), (1, 105)):
            halo = img.copy()
            halo.set_alpha(alpha)
            for ox, oy in ((-radius, 0), (radius, 0), (0, -radius),
                           (0, radius), (-radius, -radius),
                           (radius, -radius), (-radius, radius),
                           (radius, radius)):
                surface.blit(halo, (pos[0] + ox, pos[1] + oy))
        ui.draw_text(surface, font, text, pos, (0, 0, 0), shadow=False)

    def _gradient_line(self, surface, x: int, y: int) -> None:
        line = pygame.Surface((80, 2), pygame.SRCALPHA)
        # 线性渐变的配比 120,255：前 47% 完全不透明，
        # 之后向右端逐渐淡出。
        fade_start = round(80 * 120 / 255)
        for px in range(80):
            if px < fade_start:
                alpha = 255
            else:
                alpha = max(0, int(255 * (1.0 - (px - fade_start)
                                          / max(1, 80 - fade_start))))
            line.fill((255, 255, 255, alpha), (px, 0, 1, 2))
        surface.blit(line, (x, y))

    def _outline_text(self, surface, font, text, pos, color) -> None:
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            ui.draw_text(surface, font, text, (pos[0] + dx, pos[1] + dy),
                         (255, 255, 255), shadow=False)
        ui.draw_text(surface, font, text, pos, color, shadow=False)

    def _draw_message(self, surface) -> None:
        if self.msg_t <= 0:
            return
        font = self.app.assets.font(28, bold=True)
        y = 270 - int(100 * min(1.0, (2.2 - self.msg_t) / 1.2))
        x = (WIDTH - font.size(self.msg)[0]) // 2
        self._outline_text(surface, font, self.msg, (x, y), (20, 20, 20))

    # ------------------------------------------------------------------ 输入
    def update(self, dt: float) -> None:
        self.preview_t += dt
        if self.msg_t > 0:
            self.msg_t = max(0.0, self.msg_t - dt)
            if self.msg_t == 0:
                self.msg = ""
                self.app.inv_msg = ""

    def handle_event(self, e: pygame.event.Event) -> bool:
        if e.type == pygame.KEYDOWN:
            self.keyboard_nav = True
            if e.key == pygame.K_ESCAPE:
                self.close()
                return True
            if e.key == pygame.K_TAB:
                self._switch_tab("merit" if self.tab == "equip" else "equip")
                return True
            if e.key in (pygame.K_LEFT, pygame.K_RIGHT, pygame.K_UP, pygame.K_DOWN):
                self._move_selection(e.key)
                return True
            if e.key in (pygame.K_RETURN, pygame.K_j):
                if self.tab == "merit":
                    self._action_exchange(self._selected())
                else:
                    self._action_equip(self._selected())
                return True
            if e.key == pygame.K_s and self.tab == "equip":
                self._action_sell(self._selected())
                return True
            if e.key == pygame.K_g and self.tab == "equip":
                self._action_give(self._selected())
                return True
            if e.key == pygame.K_e and self.tab == "merit":
                self._action_exchange(self._selected())
                return True
        return False

    def handle_mouse(self, e: pygame.event.Event) -> bool:
        if e.type == pygame.MOUSEMOTION:
            self.keyboard_nav = False
            self.equip_hover = self._hit_equip(e.pos)
            self.hover = -1 if self.equip_hover else self._hit_slot(e.pos)
            self.menu_hover = ""
            if self.menu_slot >= 0:
                mx, my = self._menu_pos()
                mw, mh = MENU_ONE if self.tab == "merit" else MENU_THREE
                menu_rect = pygame.Rect(mx, my, mw, mh)
                col, row = self.menu_slot % COLS, self.menu_slot // COLS
                cell_rect = pygame.Rect(round(GRID_X + col * PITCH),
                                        round(GRID_Y + row * PITCH), CELL, CELL)
                if not menu_rect.collidepoint(e.pos) and not cell_rect.collidepoint(e.pos):
                    self.menu_slot = -1
                else:
                    for action, rect in self._menu_buttons():
                        if rect.collidepoint(e.pos) and not self._menu_disabled(action):
                            self.menu_hover = action
                            break
            return True
        if e.type != pygame.MOUSEBUTTONDOWN or getattr(e, "button", 1) != 1:
            return False
        self.keyboard_nav = False
        pos = e.pos
        if CLOSE_RECT.collidepoint(pos):
            self.close()
            return True
        for tab, rect in TAB_RECTS.items():
            if rect.collidepoint(pos):
                self._switch_tab(tab)
                return True
        self.equip_hover = self._hit_equip(pos)
        if self.menu_slot >= 0:
            for action, rect in self._menu_buttons():
                if rect.collidepoint(pos):
                    if not self._menu_disabled(action):
                        self._run_action(action)
                    return True
        if self.equip_hover:
            self.menu_slot = -1
            return True
        slot = self._hit_slot(pos)
        if slot >= 0:
            item = self._item(slot)
            if item is not None:
                self._set_selected(slot)
                self.menu_slot = slot
                self.hover = -1
            else:
                self.menu_slot = -1
            return True
        if not PANEL_RECT.collidepoint(pos):
            self.menu_slot = -1
        elif self.menu_slot >= 0:
            # 点在面板上但没命中菜单按钮或格子时收起菜单；
            # 菜单只是随选中格子临时展开的，焦点一离开就收起，避免遮挡背包格。
            self.menu_slot = -1
        return True

    def _move_selection(self, key: int) -> None:
        self.keyboard_nav = True
        sel = self._selected()
        col, row = sel % COLS, sel // COLS
        if key == pygame.K_LEFT:
            col = max(0, col - 1)
        elif key == pygame.K_RIGHT:
            col = min(COLS - 1, col + 1)
        elif key == pygame.K_UP:
            row = max(0, row - 1)
        elif key == pygame.K_DOWN:
            row = min(ROWS - 1, row + 1)
        self._set_selected(row * COLS + col)

    def _switch_tab(self, tab: str) -> None:
        self.tab = tab
        self.menu_slot = -1
        self.hover = -1
        self.equip_hover = None
        self._set_selected(0)

    def _hit_equip(self, pos) -> str | None:
        for slot, (sx, sy) in SLOT_POS.items():
            if pygame.Rect(round(sx), round(sy - 2), CELL, CELL).collidepoint(pos):
                if self._equipment().get(slot):
                    return slot
        return None

    def _hit_slot(self, pos) -> int:
        x, y = pos
        if x < GRID_X or y < GRID_Y:
            return -1
        col = int((x - GRID_X) // PITCH)
        row = int((y - GRID_Y) // PITCH)
        if not (0 <= col < COLS and 0 <= row < ROWS):
            return -1
        cx = GRID_X + col * PITCH
        cy = GRID_Y + row * PITCH
        if cx <= x < cx + CELL and cy <= y < cy + CELL:
            return row * COLS + col
        return -1

    def _run_action(self, action: str) -> None:
        slot = self.menu_slot
        if action == "equip":
            self._action_equip(slot)
        elif action == "give":
            self._action_give(slot)
        elif action == "sell":
            self._action_sell(slot)
        elif action == "exchange":
            self._action_exchange(slot)

    def _action_equip(self, index: int) -> None:
        if self.tab != "equip":
            return
        item = self._item(index)
        if item is None:
            return
        if not can_equip(item, self._hero()):
            self._msg("职业不符，无法装备")
            return
        inv = self._inv()
        if item not in inv:
            return
        slot = item.get("slot")
        if slot not in SLOT_ORDER:
            self._msg("未知装备槽位")
            return
        equipment = self._equipment()
        old = equipment.get(slot)
        inv.remove(item)
        if old is not None:
            inv.append(old)
        equipment[slot] = item
        self._set_selected(min(index, max(0, len(inv) - 1)))
        self.menu_slot = -1
        self.hover = -1
        self.equip_hover = None
        self._sync_battle()
        self._write()
        self._clear_msg()

    def _action_sell(self, index: int) -> None:
        if self.tab != "equip":
            return
        item = self._item(index)
        if item is None:
            return
        inv = self._inv()
        if item not in inv:
            return
        inv.remove(item)
        value = sell_price(item)
        data = self._data()
        data["soul"] = int(data.get("soul", 0)) + value
        data["myscore"] = int(data.get("myscore", 0)) + value
        self._set_selected(min(index, max(0, len(inv) - 1)))
        self.menu_slot = -1
        self.hover = -1
        self.equip_hover = None
        self._write()
        self._clear_msg()

    def _action_give(self, index: int) -> None:
        if self.tab != "equip":
            return
        if int(getattr(self.app, "play_num", 1)) != 2:
            self._msg("单人模式无法赠送")
            return
        # 本实现还没有把第二个战役存档接入应用数据，暂时没有可赠送的对象。
        self._msg("双人存档尚未接入")

    def _action_exchange(self, index: int) -> None:
        if self.tab != "merit":
            return
        item = self._item(index)
        if item is None:
            return
        cost = item_gongxun(item)
        data = self._data()
        if int(data.get("gongxun", 0)) < cost:
            self._msg(f"战功不足,兑换需要战功 {cost}")
            return
        if len(self._inv()) >= MAX_BAG:
            self._msg("背包已满")
            return
        # 兑换得到的物品沿用战功面板提示里显示的同一组属性数值，
        # 只重新生成实例与 uid，确保放进背包的不是同一个对象的别名。
        new_item = dict(item)
        new_item["uid"] = self._next_uid()
        new_item["stats"] = dict(item.get("stats", {}))
        data["gongxun"] = int(data.get("gongxun", 0)) - cost
        self._inv().append(new_item)
        self.menu_slot = -1
        self.hover = -1
        self.equip_hover = None
        self._write()
        self._msg("兑换成功！")
