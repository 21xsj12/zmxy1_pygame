"""运行时装备纸娃娃合成器。

角色不是由骨骼拼起来的：每个动作帧都自带衣服/武器图层，按制作时给定的
层级深度与 2x3 变换矩阵摆放；图层的帧号就是外观编号，图层自身的帧事件
再决定布料/武器外观编号。

``tools/build_paperdoll.py`` 把上述信息固化为：

* ``assets/sprites/<hero>/<action>/frame_XXX.png``  -- 不带装备的身体图
* ``assets/paperdoll/<hero>/<layer>/<n>.png``       -- 每种外观一张 PNG
* ``data/paperdoll.json``                           -- 每帧的 ``(depth, layer,
  matrix)``，以及锚点与身体偏移

本模块只负责回放：先画身体，再按深度顺序逐层绘制，每层绕自身锚点做矩阵
变换；不额外发明任何视觉元素。
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

import pygame

from .constants import ASSETS_DIR, DATA_DIR

Matrix = tuple[float, float, float, float, float, float]

# 装备层的“临时改帧”来自动作内部的帧事件：
#   攻击一动作第 3 帧：遍历容器子剪辑，让它们跳到第 2 帧
#   攻击二动作第 2 帧：遍历容器子剪辑，让它们跳到第 4 帧
# hit1/hit2 的装备容器共 3 帧，时间轴为
#   第 1 帧：空   第 2 帧：灰甲（像素偏移约 -24.2,-13.0）
#   第 3 帧：移除灰甲 + 红甲（像素偏移约 5.6,1.4）
# 选外观时按布料编号把容器停到对应帧（编号 >3 隐藏），
# 帧事件再对**子剪辑**强制跳帧；而灰甲/红甲的帧 1..4 逐字节相同，
# 所以这个强制帧在视觉上没有任何变化（复核结论，早先的“换甲”判断作废）。
# 本实现把该容器抽成单独的装备层（6 帧：1=空、2=灰甲 f1、3=红甲 f1、
# 4=灰甲 f2(≡2)、5=红甲 f2(≡3)、6=红甲 f4(≡3)），这里仍返回“强制帧”的层号，
# 从而让逐帧控制流程保持统一，但绘制结果与基础外观相同。
LAYER_FRAME_OVERRIDES: dict[str, list[tuple[int, str]]] = {
    "hit1": [(2, "bodyEquipA")],
    "hit2": [(1, "bodyEquipA")],
}
# 强制跳帧重播的是**同一个**子剪辑，所以
# 强制变体必须沿用选外观时挑中的那个子剪辑：
#   布料 2 -> 灰甲（变体 4 = 灰甲 f2），布料 3 -> 红甲（5 = 红甲 f2、6 = 红甲 f4）
#   布料 1 -> 容器第 1 帧没有任何子剪辑 -> 不应发生任何改变。
# （灰甲/红甲的帧 1..4 逐字节相同，所以 4/5/6 看起来与 2/3 一样。）
FORCED_VARIANTS: dict[str, dict[int, int]] = {
    "hit1": {2: 4, 3: 5},
    "hit2": {2: 4, 3: 6},
}


def action_layer_overrides(action: str, frame: int,
                           cloth_id: int = 1) -> dict[str, int] | None:
    """``(action, frame_index, cloth)`` -> {图层名: 强制变体帧}（无则 None）。

    cloth 为 1 时容器第 1 帧是空的（没有子剪辑），逐帧循环执行 0 次 → 不能凭空
    冒出甲；cloth>3 时选外观逻辑直接隐藏该层。只有 cloth 2/3 需要返回“强制帧”，
    而灰甲/红甲容器的帧 1..4 画面完全相同，所以这个强制帧看起来与基础外观一致。
    """
    rules = LAYER_FRAME_OVERRIDES.get(action)
    if not rules:
        return None
    cloth = int(cloth_id)
    forced = FORCED_VARIANTS.get(action, {}).get(cloth)
    if forced is None:
        return None
    out: dict[str, int] = {}
    for start, layer in rules:
        if frame >= start:
            out[layer] = forced
    return out or None


# ---------------------------------------------------------------------------
# 对已知映射偏差的有意修正（本实现有意偏离数据文件里的规则；见
# 设计文档中的纸娃娃问题修复方案）。
#
# 1) 晚期布料组的映射规则是“编号 > 2 时跳到编号 - 2 帧”：
#    编号 3 -> v1、4 -> v2、5 -> v3、6 -> v3（封顶）。
#    但美术帧序实际是 v1=家传衣裳、v2=麒麟袍、v3=天煞羽袍（按整理后的
#    帧表复核），于是编号 3 的衣领/袖口显示家传、4 显示麒麟、5 显示天煞——
#    数据里的映射整体差一档。本实现按“装备对档”修正：编号 <=3 隐藏，
#    4/5/6 -> v1/v2/v3。
# 2) 未包含在本修正里的已知问题（成本更高，见方案文档 §4）：优秀袈裟
#    hit1/hit2 的静态甲与身体前倾对不齐；天煞羽袍 hit5 的外袍袖与
#    身体手臂叠加成“两条手臂”。这两类需要改美术/逐帧补偿，不在运行时一行规则内。
# 这两条只改运行时解释：data/paperdoll.json 仍保持数据原样，便于
# verify_paperdoll/verify_tangseng 继续做“数据忠实度”审计；
# 关闭这组运行时修正即可复现问题映射（测试/对照用）。
LATE_CLOTH_LAYERS = frozenset({"cloth810", "cloth856", "cloth903", "cloth987"})
ORIGINAL_BUG_FIXES_ENABLED = True


def resolve_original_bug_fix(action: str, name: str,
                             cloth: int) -> tuple[bool, int] | None:
    """已知映射偏差的运行时修正；None = 按 ``data`` 里的同步规则原样解析。

    目前只修正晚期布料组差一档的映射；``action`` 预留给后续逐动作修正
    （hit5 天煞外袍臂、优秀 hit1/hit2 甲偏移需要美术级处理，未在此处硬改）。
    """
    if not ORIGINAL_BUG_FIXES_ENABLED:
        return None
    if name not in LATE_CLOTH_LAYERS:
        return None
    c = int(cloth)
    if c <= 3:
        return False, c
    return True, min(c - 3, 3)


@dataclass(frozen=True)
class LayerInfo:
    name: str
    slot: str          # "cloth"（衣服）| "weapon"（武器）
    frames: int
    anchor: tuple[float, float]
    path: str
    empty: tuple[int, ...]
    hide_above: int = 0     # 旧字段：布料编号 > 3 时隐藏该层
    sync: tuple = ()        # (kind, var, maximum, minimum, offset)，见 layer_sync.py
    #: 逐帧身体装备外观：(布料编号 -> (path, frames))，见 data/paperdoll.json
    variants: tuple = ()

    def resolve(self, value: int) -> tuple[bool, int]:
        """按数据里的同步规则算 (是否显示, 该层第几帧)；不可见时帧号原样返回。"""
        kind, var, maximum, minimum, offset = self.sync or ("direct", "cloth", 0, 0, 0)
        value = int(value)
        if kind == "hidden_above":
            return value <= maximum, value
        if kind == "window":
            if value < minimum:
                return False, value
            return True, value - offset
        return True, value

    def variant_for(self, cloth: int):
        """取逐帧身体装备外观的 (path, frames)；没有对应项时返回 None。"""
        for entry in self.variants:
            if int(entry[0]) == int(cloth):
                return entry[1], int(entry[2])
        return None


class PaperDoll:
    def __init__(self, root: Path = ASSETS_DIR, data_dir: Path = DATA_DIR):
        self.root = root
        path = data_dir / "paperdoll.json"
        self.heroes: dict = {}
        # 碰撞椭圆高 90，脚底在英雄原点下方 45px；Player.draw() 传进来的是
        # body 锚点（脚底线），所以图层要往上挪回去。
        self.feet_offset = 0.0
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                self.heroes = raw["heroes"]
                self.feet_offset = float(raw.get("feet_offset", 0.0))
            except (OSError, ValueError, KeyError):
                self.heroes = {}
        self._layers: dict[tuple[str, str], LayerInfo] = {}
        self._images: dict[tuple[str, str, int], pygame.Surface] = {}
        self._variant_images: dict[tuple[str, str, int, int], pygame.Surface] = {}
        self._transformed: dict[tuple, tuple[pygame.Surface, tuple[float, float]]] = {}
        # 动作名 -> {分段序号: (path, anchor, frames)}
        self._bands: dict[tuple[str, str], dict[int, tuple[str, tuple[float, float], int]]] = {}
        self._band_images: dict[tuple[str, str, int, int], pygame.Surface] = {}
        self._band_flipped: dict[tuple[str, str, int, int], pygame.Surface] = {}
        for hero, cfg in self.heroes.items():
            for action, info in cfg.get("actions", {}).items():
                bands = {}
                for key, band in (info.get("bands") or {}).items():
                    bands[int(key)] = (
                        band["path"],
                        (float(band["anchor"][0]), float(band["anchor"][1])),
                        int(band.get("frames", info.get("frames", 1))),
                    )
                if bands:
                    self._bands[(hero, action)] = bands
        for hero, cfg in self.heroes.items():
            for name, info in cfg.get("layers", {}).items():
                if "path" not in info:
                    continue
                hide_above = int(info.get("hide_above", 0))
                raw_sync = info.get("sync") or {}
                if raw_sync.get("kind"):
                    sync = (raw_sync["kind"], raw_sync.get("var", "cloth"),
                            int(raw_sync.get("max", 0)), int(raw_sync.get("min", 0)),
                            int(raw_sync.get("offset", 0)))
                elif hide_above:
                    sync = ("hidden_above", "cloth", hide_above, 0, 0)
                else:
                    sync = ("direct", "cloth", 0, 0, 0)
                self._layers[(hero, name)] = LayerInfo(
                    name=name,
                    slot=info.get("slot", "cloth"),
                    frames=int(info.get("frames", 1)),
                    anchor=(float(info.get("anchor", (0, 0))[0]),
                            float(info.get("anchor", (0, 0))[1])),
                    path=info["path"],
                    empty=tuple(int(v) for v in info.get("empty", ())),
                    hide_above=hide_above,
                    sync=sync,
                    variants=tuple(
                        (int(k), raw_v["path"], int(raw_v.get("frames", 1)))
                        for k, raw_v in (info.get("variants") or {}).items()
                        if raw_v.get("path")),
                )
        # 单独绘制的嵌套特效（hit3 玄冰阵金环）：在 body 之前、depth 1 绘制，
        # 因此运行时也画在 body 之下。字段定义见 data/paperdoll.json 的
        # 路径 actions.<action>.effects.<name>，包含 path/frames/anchor/depth/
        # matrix/first_frame/last_frame 等字段。
        self._effect_defs: dict[tuple[str, str], list[tuple]] = {}
        self._effect_images: dict[tuple[str, str, int], pygame.Surface] = {}
        for hero, cfg in self.heroes.items():
            for action, info in cfg.get("actions", {}).items():
                defs = []
                for name, ent in (info.get("effects") or {}).items():
                    if "path" not in ent:
                        continue
                    anchor = ent.get("anchor", (0, 0))
                    defs.append((name, ent["path"],
                                 (float(anchor[0]), float(anchor[1])),
                                 int(ent.get("frames", 1)),
                                 tuple(float(v) for v in ent.get("matrix") or
                                       (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)),
                                 int(ent.get("depth", 1)),
                                 int(ent.get("first_frame", 1)),
                                 int(ent.get("last_frame", 10 ** 6))))
                if defs:
                    self._effect_defs[(hero, action)] = defs

    # ------------------------------------------------------------------ 接口
    @property
    def ready(self) -> bool:
        return any(cfg.get("layers") for cfg in self.heroes.values())

    def has_action(self, hero: str, action: str) -> bool:
        cfg = self.heroes.get(hero)
        return bool(cfg and action in cfg.get("actions", {})
                    and cfg["actions"][action].get("layers"))

    def draw(self, surface: pygame.Surface, hero: str, action: str, frame: int,
             x: float, y: float, facing: int, cloth_id: int, weapon_id: int,
             alpha: int = 255, flash: tuple[int, int, int] | None = None,
             phase: str | None = None,
             override: dict[str, int] | None = None) -> None:
        """绘制某一帧的装备图层，英雄原点位于 (x, y)。

        ``phase='behind'`` 只绘制武器（jump2 里武器的 depth 3 比身体的
        depth 6 更靠后）；``phase='front'`` 只绘制衣服，调用方可以把身体
        夹在两次绘制之间。

        ``override`` 是逐帧事件对图层的强制改帧（见 ``LAYER_FRAME_OVERRIDES``），
        形如 ``{"装备层名": 2}``；默认 None，背包预览等调用方不受影响。
        """
        cfg = self.heroes.get(hero)
        if not cfg:
            return
        info = cfg.get("actions", {}).get(action)
        if not info:
            return
        per_frame = info.get("layers") or []
        if not per_frame:
            return
        idx = min(max(frame, 0), len(per_frame) - 1)
        entries = per_frame[idx]
        if not entries:
            return
        ox, oy = info.get("body_offset", (0.0, 0.0))
        oy -= self.feet_offset
        bands = self._bands.get((hero, action), {})
        if action == "jump2":
            # 红线（W1/P-3/P-8）：jump2 保留 behind→body→front 双段绘制，body
            # 资产是「整身去装备」的 83×84 版本（非 band0 切片）；band 表只用于
            # 结构校验/新 schema，运行时不能再重复画一遍身体零件。
            bands = {}
        # jump2 的 behind→body→front 双段绘制保留（装备嵌在一个旋转的容器里）。
        # 新构建把身体拆成 band 0..N：band 0 就是 body 剪辑本身（在双段之间画），
        # 其它 band 必须在该循环里跟装备层一起交错绘制，所以有 band 时 behind
        # 段不画任何东西、front 段退化为普通单段。没有 band 的旧数据（悟空）保持
        # 原语义：behind 只画武器、front 只画衣服。
        if phase == "behind" and bands:
            return
        if phase == "front" and bands:
            phase = None
        for i, entry in enumerate(entries):
            depth, name, m = entry[0], entry[1], entry[2]
            art = entry[3] if len(entry) > 3 else None
            slot = self._layers.get((hero, name))
            is_weapon = bool(slot and slot.slot == "weapon")
            if phase == "behind" and not is_weapon:
                continue
            if phase == "front" and is_weapon:
                continue
            self._draw_layer(surface, hero, name, m, x, y, ox, oy, facing,
                             cloth_id, weapon_id, alpha, flash, override,
                             art_index=art, action=action)
            # band i+1 = 紧挨在该图层上方的身体零件
            if phase is None:
                self._draw_band(surface, hero, action, i + 1, idx, x, y, facing,
                                alpha, flash)
        # 位于所有装备层之上的身体零件
        if phase is None:
            for band in sorted(bands):
                if band > len(entries):
                    self._draw_band(surface, hero, action, band, idx, x, y, facing,
                                    alpha, flash)

    def draw_effects(self, surface: pygame.Surface, hero: str, action: str,
                     frame: int, x: float, y: float, facing: int,
                     alpha: int = 255,
                     flash: tuple[int, int, int] | None = None) -> None:
        """独立绘制的嵌套特效（hit3 玄冰阵地面金环），在身体之前绘制。

        该特效在动作第 15 帧起以 depth 1 放置：50 帧的地面光环，位于所有身体
        零件之下。构建时把它从身体与分段图层中剔除并单独导出，这里按
        ``art = (F - first_frame) % frames + 1`` 的相位与放置矩阵复现同一画面。
        """
        cfg = self.heroes.get(hero)
        info = cfg.get("actions", {}).get(action) if cfg else None
        if not info:
            return
        defs = self._effect_defs.get((hero, action))
        if not defs:
            return
        fi = int(frame) + 1                     # 从 1 开始计数的动作帧号
        ox, oy = info.get("body_offset", (0.0, 0.0))
        oy -= self.feet_offset
        for (name, path, anchor, frames, m, _depth, first, last) in defs:
            if not (first <= fi <= last):
                continue
            art = (fi - first) % max(1, frames) + 1
            key = (hero, name, art)
            img = self._effect_images.get(key)
            if img is None:
                file = self.root / path / f"frame_{art - 1:03d}.png"
                try:
                    img = pygame.image.load(str(file)).convert_alpha()
                except (pygame.error, FileNotFoundError):
                    img = pygame.Surface((1, 1), pygame.SRCALPHA)
                self._effect_images[key] = img
            a, b, c, d, tx, ty = m
            if facing > 0:
                a, c, tx = -a, -c, -tx
                dest = (x - ox + tx, y + oy + ty)
            else:
                dest = (x + ox + tx, y + oy + ty)
            surf, anc = self._transform(img, anchor, a, b, c, d)
            if alpha < 255:
                surf = surf.copy()
                surf.set_alpha(alpha)
            if flash is not None:
                surf = _tint(surf, flash)
            surface.blit(surf, (round(dest[0] - anc[0]),
                                round(dest[1] - anc[1])))

    def _draw_layer(self, surface: pygame.Surface, hero: str, name: str,
                    m: list, x: float, y: float, ox: float, oy: float,
                    facing: int, cloth_id: int, weapon_id: int, alpha: int,
                    flash: tuple[int, int, int] | None,
                    override: dict[str, int] | None = None,
                    art_index: int | None = None,
                    action: str = "") -> None:
        layer = self._layers.get((hero, name))
        if layer is None:
            return
        # 逐帧事件里装备容器只覆盖帧 1..3；唐僧两组装饰分别从第 4 / 3 帧起，
        # 帧号 = 布料编号 - 3 / - 2；悟空、头饰与武器直接使用外观编号。
        # 晚期布料组（见 LATE_CLOTH_LAYERS）的 -2 映射差一档，需要
        # 运行时按装备对档修正（data 仍保留原始规则，具体规则见上方说明）。
        value = cloth_id if layer.slot == "cloth" else weapon_id
        fix = (resolve_original_bug_fix(action, name, value)
               if layer.slot != "weapon" else None)
        if fix is not None:
            visible, variant = fix
        else:
            visible, variant = layer.resolve(value)
        if not visible:
            return
        variant_info = layer.variant_for(value) if layer.slot != "weapon" else None
        if variant_info is not None and art_index is not None:
            # 逐帧身体装备外观：第 4 项 art_index 由构建阶段按子剪辑相位算出；
            # 旧的 LAYER_FRAME_OVERRIDES 静态变体逻辑对带 variants 的层不再生效。
            img, anchor = self._variant_image(hero, layer, value, int(art_index),
                                              variant_info)
        else:
            if override and name in override:
                # 逐帧事件的强制跳帧：可见性仍由选外观结果决定，只改帧号。
                variant = int(override[name])
            variant = min(max(1, int(variant)), layer.frames)
            if variant in layer.empty:
                return
            img, anchor = self._image(hero, layer, variant)
        if img is None:
            return
        a, b, c, d, tx, ty = m
        if facing > 0:
            # 美术朝左；朝右时把 x 那一行取反（x' = a·x + c·y → −x'），
            # 即 (a, c, tx) 取反，b/d 不动。早期实现取反的是 a、b（x 轴基向量的两个
            # 分量），等于把图层转了 180°，朝右时武器/披风整体反到另一边。
            a, c, tx = -a, -c, -tx
            dest = (x - ox + tx, y + oy + ty)
        else:
            dest = (x + ox + tx, y + oy + ty)
        surf, anc = self._transform(img, anchor, a, b, c, d)
        if alpha < 255:
            surf = surf.copy()
            surf.set_alpha(alpha)
        if flash is not None:
            surf = _tint(surf, flash)
        surface.blit(surf, (round(dest[0] - anc[0]), round(dest[1] - anc[1])))

    def _draw_band(self, surface: pygame.Surface, hero: str, action: str,
                   band: int, frame: int, x: float, y: float, facing: int,
                   alpha: int, flash: tuple[int, int, int] | None) -> None:
        info = self._bands.get((hero, action), {}).get(band)
        if info is None:
            return
        path, anchor, frames = info
        key = (hero, action, band, min(frame, frames - 1))
        img = self._band_images.get(key)
        if img is None:
            file = self.root / path / f"frame_{min(frame, frames - 1):03d}.png"
            try:
                img = pygame.image.load(str(file)).convert_alpha()
            except (pygame.error, FileNotFoundError):
                img = pygame.Surface((1, 1), pygame.SRCALPHA)
            self._band_images[key] = img
        ax = anchor[0]
        if facing > 0:                        # 源素材朝左
            fkey = (hero, action, band, key[3])
            flipped = self._band_flipped.get(fkey)
            if flipped is None:
                flipped = pygame.transform.flip(img, True, False)
                if len(self._band_flipped) > 384:
                    self._band_flipped.clear()
                self._band_flipped[fkey] = flipped
            img = flipped
            ax = img.get_width() - ax
        if alpha < 255:
            img = img.copy()
            img.set_alpha(alpha)
        if flash is not None:
            img = _tint(img, flash)
        surface.blit(img, (round(x - ax), round(y - anchor[1])))

    # -------------------------------------------------------------- 加载
    def _image(self, hero: str, layer: LayerInfo,
               variant: int) -> tuple[pygame.Surface | None, tuple[float, float]]:
        key = (hero, layer.name, variant)
        cached = self._images.get(key)
        if cached is None:
            path = self.root / layer.path / f"{variant}.png"
            try:
                cached = pygame.image.load(str(path)).convert_alpha()
            except (pygame.error, FileNotFoundError):
                cached = pygame.Surface((1, 1), pygame.SRCALPHA)
            self._images[key] = cached
        return cached, layer.anchor

    def _variant_image(self, hero: str, layer: LayerInfo, cloth: int,
                       art_index: int, variant_info: tuple[str, int]
                       ) -> tuple[pygame.Surface | None, tuple[float, float]]:
        """逐帧身体装备外观：读取 ``variants.<cloth>/<art>.png``（帧号从 1 开始）。"""
        path, frames = variant_info
        art = min(max(1, int(art_index)), max(1, int(frames)))
        key = (hero, layer.name, int(cloth), art)
        cached = self._variant_images.get(key)
        if cached is None:
            file = self.root / path / f"{art}.png"
            try:
                cached = pygame.image.load(str(file)).convert_alpha()
            except (pygame.error, FileNotFoundError):
                cached = pygame.Surface((1, 1), pygame.SRCALPHA)
            if len(self._variant_images) > 512:
                self._variant_images.clear()
            self._variant_images[key] = cached
        return cached, layer.anchor

    def _transform(self, img: pygame.Surface, anchor: tuple[float, float],
                   a: float, b: float, c: float, d: float
                   ) -> tuple[pygame.Surface, tuple[float, float]]:
        """围绕 ``anchor`` 应用 2x3 线性部分；按唯一矩阵缓存结果。"""
        key = (id(img), round(a, 4), round(b, 4), round(c, 4), round(d, 4))
        hit = self._transformed.get(key)
        if hit is not None:
            return hit
        surf, anc = _affine(img, anchor, a, b, c, d)
        if len(self._transformed) > 512:       # 控制缓存内存占用
            self._transformed.clear()
        self._transformed[key] = (surf, anc)
        return surf, anc


def _tint(img: pygame.Surface, color: tuple[int, int, int]) -> pygame.Surface:
    out = img.copy()
    overlay = pygame.Surface(out.get_size(), pygame.SRCALPHA)
    overlay.fill((*color, 0))
    out.blit(overlay, (0, 0), special_flags=pygame.BLEND_RGBA_ADD)
    return out


def _center_map(anchor: tuple[float, float], src: tuple[int, int],
                dst: tuple[int, int], angle: float,
                scale: float = 1.0) -> tuple[float, float]:
    """锚点 ``anchor`` 经过以中心为基准的缩放+旋转后落到的位置。"""
    cx, cy = src[0] / 2.0, src[1] / 2.0
    dx, dy = anchor[0] - cx, anchor[1] - cy
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    return (dst[0] / 2.0 + scale * (cos_a * dx + sin_a * dy),
            dst[1] / 2.0 + scale * (-sin_a * dx + cos_a * dy))


def _affine(img: pygame.Surface, anchor: tuple[float, float],
            a: float, b: float, c: float, d: float
            ) -> tuple[pygame.Surface, tuple[float, float]]:
    """在 ``anchor`` 处按 2x3 仿射矩阵变换，效果等价于绘制期的矩阵映射。

    矩阵约定是 ``x' = a*x + c*y + tx`` / ``y' = b*x + d*y + ty``（x 轴基向量
    ``(a, b)``、y 轴基向量 ``(c, d)``），**不是** ``[[a, b], [c, d]]``。
    早期实现把 b/c 用反了，等于把每层的旋转镜像了一次：悟空武器的 157° 旋转
    变成 −157°（棍子方向整个翻掉），衣服的小角度旋转也各偏了 2 倍。
    """
    w, h = img.get_size()
    det = a * d - b * c
    if det < 0:                                 # 反射：先水平翻转源图
        img = pygame.transform.flip(img, True, False)
        anchor = (w - anchor[0], anchor[1])
        a, b, c, d = -a, -b, c, d              # 相当于 M * diag(-1, 1)
        det = -det
    sx = math.hypot(a, b)                       # |x 轴基向量|
    sy = math.hypot(c, d)                       # |y 轴基向量|
    if sx < 1e-6 or sy < 1e-6:
        return pygame.Surface((1, 1), pygame.SRCALPHA), (0.0, 0.0)
    # x 轴基向量 = sx*(cosθ, -sinθ) → pygame 的逆时针角
    angle = math.atan2(-b, a)
    if abs(sx - sy) <= 1e-3 * max(sx, sy):
        out = pygame.transform.rotozoom(img, math.degrees(angle), sx)
        return out, _center_map(anchor, (w, h), out.get_size(), angle, sx)
    w1, h1 = max(1, round(w * sx)), max(1, round(h * sy))
    scaled = pygame.transform.smoothscale(img, (w1, h1))
    anchor1 = (anchor[0] * w1 / w, anchor[1] * h1 / h)
    out = pygame.transform.rotate(scaled, math.degrees(angle))
    return out, _center_map(anchor1, (w1, h1), out.get_size(), angle)
