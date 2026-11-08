"""子弹基类、敌人移动子弹与特效子弹三类弹体的实现。

三类子弹共用同一套命中管线：

* 每 24 fps 推进一帧 ``step``，逐帧检查命中；
* 判定 = 整颗子弹美术的外接框 × 目标的碰撞椭圆（AABB 预筛），
  再叠加像素级测试（本实现用「该帧美术并集包围盒」近似，见方案 §3.4）；
* 命中之后才开始计时，按固定间隔切换命中身份，而不是“每 N 帧强制命中”；
* 剩余命中次数每命中一次减一，减到 0 自毁；
* 命中范围：命中后对周围目标“跳过判定”强制结算一次溅射。

判定盒来自 ``data/tang_fx.json``（由 ``tools/build_tang_fx.py`` 从动画数据生成），
不再用 ``radius`` 点判，也不再做 ``min(200, radius*4)`` 归一化。
"""
from __future__ import annotations

import pygame

from .assets import Animation, Assets
from .constants import HitKey, next_hit_ns

# 水平翻转的语义取决于素材绘制方向：这些特效是按右向手绘的，
# 因此翻转方向与其它美术相反（见 world.RIGHT_AUTHORED_FX 的注释）。
RIGHT_AUTHORED_FX = {"hit10_pre", "hit10_flush"}


class Projectile:
    def __init__(self, x: float, y: float, vx: float, vy: float, damage: float,
                 owner: str, kind: str, attack_id: int, clip_name: str = "",
                 radius: int = 12, life: float = 2.0, stage: int = 0,
                 kb: tuple[float, float] = (4, -7), base_power: float = 0.0,
                 hit_max: int = 1, interval: float = 0.0,
                 distance: float | None = None, frame_box: bool = False,
                 frame_ids: tuple[int, ...] = (),
                 gxp_frames: tuple[int, ...] = (), source=None,
                 art_scale: tuple[float, float] = (1.0, 1.0),
                 mult: float | None = None, fixed: float | None = None,
                 attack_range: float = 0.0,
                 art_bounds: list | None = None,
                 native_art: bool = False,
                 sfx_frames: dict | None = None,
                 frame_limit: int | None = None,
                 spawn_delay: bool = False,
                 hit_ns: int | None = None) -> None:
        self.frame_box = frame_box
        self.x, self.y = x, y
        self.vx, self.vy = vx, vy
        self.damage = damage
        self.base_power = base_power
        self.owner = owner
        self.kind = kind
        self.attack_id = attack_id
        self.stage = stage
        # 子弹的命中身份由**子弹自身**的实例名与 ``attack_id`` 共同决定；
        # 本实现用 ``hit_ns`` 承载"实例名"这一维。每颗子弹一份命名空间，所以
        # 两次施法即使 ``attack_id`` 相同也不会互相吞伤（早期实现假设实例名为空、
        # 只按 ``attack_id`` 去重，该假设已被《受击判定与连击保护实现方案》§7 推翻）。
        self.hit_ns = int(hit_ns) if hit_ns is not None else next_hit_ns()
        self.clip_name = clip_name
        self.radius = radius
        self.life = life
        self.dead = False
        self.t = 0.0
        self.kb = kb
        self.hit_max = int(hit_max)
        self.interval = float(interval)
        self.distance = distance
        # 命中身份：每个目标各维护一份"已命中身份"集合，子弹身份 = 子弹自身的
        # 实例名 + ``attack_id``。本实现用 ``hit_ns`` 承载实例名这一维，去重表放在
        # **目标自己**的 ``hit_ids`` 上（怪死即消失），不用 world 级
        # {id(target): ...} 表（id 复用会造成伪去重，B5-①）；目标没有 hit_ids
        # 时才回落到 ``_local_hit_ids``。
        self._local_hit_ids: dict[int, set[HitKey]] = {}
        self.hit_ids = set()          # 兼容旧接口（当前不参与判定）
        self._interval_t = 0.0
        self._interval_hits = 0
        self.frame_ids = tuple(int(v) for v in frame_ids)
        self.gxp_frames = tuple(int(v) for v in gxp_frames)
        self._frame_id_index = 0
        self._gxp_done: set[int] = set()
        self.source = source
        self.gxp_source = source
        self.art_scale = (float(art_scale[0]), float(art_scale[1]))
        self.mult = mult
        self.fixed = fixed
        self.attack_range = float(attack_range)
        self.art_bounds = art_bounds
        self.native_art = bool(native_art)
        self.sfx_frames = dict(sfx_frames or {})
        self._sfx_done: set[int] = set()
        self.anim: Animation | None = None
        self.facing = 1 if vx >= 0 else -1
        # 对象加入场景后的第一帧只显示 f1、不跑逐帧事件；帧事件里生成的
        # 静止判定体（玄冰阵 46 帧 / 水柱 25 帧）用 ``spawn_delay`` 跳过出生
        # tick，否则帧数会少一帧。直接构造的子弹不跳过。
        self.age = -1 if spawn_delay else 0
        self.frame_limit = int(frame_limit) if frame_limit else None

    @property
    def damage_owner(self):
        """伤害与无双的归属者：幻影发射的子弹归英雄本体所有。"""
        source = self.source
        return getattr(source, "damage_owner", source) or source

    # --------------------------------------------------------- 命中身份
    def _target_ids(self, target) -> set[HitKey]:
        ids = getattr(target, "hit_ids", None)
        if ids is not None:
            return ids
        return self._local_hit_ids.setdefault(id(target), set())

    def hit_seen(self, target, key: HitKey) -> bool:
        return key in self._target_ids(target)

    def record_hit(self, target, key: HitKey) -> None:
        self._target_ids(target).add(key)


    # ------------------------------------------------------------- 几何计算
    @property
    def _flipped(self) -> bool:
        if self.clip_name in RIGHT_AUTHORED_FX:
            return self.facing < 0
        return self.facing > 0

    def _clip(self, assets: Assets):
        return assets.fx(self.clip_name) if self.clip_name else None

    def _ensure_anim(self, assets: Assets) -> None:
        if self.anim is not None or not self.clip_name:
            return
        clip = self._clip(assets)
        if clip is not None:
            # 静止判定体（玄冰阵 46 帧 / 水柱 25 帧）不循环；移动子弹只有 1 帧。
            self.anim = Animation(clip, loop=False)

    def _frame_index(self) -> int:
        return self.anim.frame_index if self.anim is not None else 0

    def _frame_rect(self) -> pygame.Rect | None:
        """该帧的判定盒（逐帧美术外接框）。

        ``art_bounds`` 存的是「相对 origin」的并集包围盒（1 基帧号），
        乘以 ``art_scale``（缩放系数）后按朝向镜像。
        """
        if self.anim is None:
            return None
        sx, sy = self.art_scale
        if self.art_bounds is not None:
            idx = self._frame_index()
            if idx >= len(self.art_bounds):
                idx = len(self.art_bounds) - 1
            box = self.art_bounds[idx]
            if not box:
                return None
            x0, x1, y0, y1 = (float(v) for v in box)
        else:
            img = self.anim.frame
            bbox = img.get_bounding_rect()
            if bbox.width == 0 or bbox.height == 0:
                return None
            clip = self.anim.clip
            x0, x1 = bbox.left - clip.anchor_x, bbox.right - clip.anchor_x
            y0, y1 = bbox.top - clip.anchor_y, bbox.bottom - clip.anchor_y
        x0, x1 = x0 * sx, x1 * sx
        y0, y1 = y0 * sy, y1 * sy
        if self._flipped:
            x0, x1 = -x1, -x0
        left, top = self.x + x0, self.y + y0
        return pygame.Rect(round(left), round(top),
                           max(1, round(x1 - x0)), max(1, round(y1 - y0)))

    def overlaps(self, target) -> bool:
        """AABB 预筛：先做矩形粗判，再交给上层叠加像素级判定。

        用目标的 ``body``（碰撞椭圆的落地近似：脚下 rect，向上 h 像素）而不是
        ``hit_rect``（那个盒子以脚底为中心，会让贴地小怪漏判）。
        """
        box = getattr(target, "body", None)
        if box is None:
            box = target.hit_rect
        if self.art_bounds is not None or self.frame_box:
            rect = self._frame_rect()
            if rect is not None:
                return rect.colliderect(box)
        area = box.inflate(self.radius * 2, self.radius * 2)
        return area.collidepoint(self.x, self.y)

    # ------------------------------------------------------------- 帧数据
    def _process_frame_ids(self, world=None) -> None:
        """子弹逐帧事件：第 6/11/16/21 帧换一次命中身份，GXP 变体再追加 26/31。"""
        if not self.frame_ids and not self.gxp_frames:
            return
        frame = int(self.t * 24.0) + 1
        changed = False
        while (self._frame_id_index < len(self.frame_ids)
               and frame >= self.frame_ids[self._frame_id_index]):
            self.attack_id += 1
            self._frame_id_index += 1
            changed = True
        if getattr(self.gxp_source, "is_gxp", False):
            for fid in self.gxp_frames:
                if frame >= fid and fid not in self._gxp_done:
                    self._gxp_done.add(fid)
                    self.attack_id += 1
                    changed = True
        if changed and world is not None:
            # 换身份的每一帧（第 6/11/16/21 帧，GXP 再追加 26/31）都触发屏幕震动，幅度 0.2。
            setattr(world, "shake", max(float(getattr(world, "shake", 0.0)), 0.2))

    def _play_frame_sfx(self, world) -> None:
        if not self.sfx_frames or world is None:
            return
        frame = int(self.t * 24.0) + 1
        for key, name in self.sfx_frames.items():
            number = int(key)
            if frame >= number and number not in self._sfx_done:
                self._sfx_done.add(number)
                world.assets.play(str(name), 0.7)

    # ------------------------------------------------------------- 单帧推进
    def update(self, dt: float, world) -> None:
        self._ensure_anim(world.assets)
        self.age += 1
        if self.age <= 0:
            # 出生帧：对象刚加入场景，本帧只显示第 1 帧，不执行逐帧事件。
            return
        self.t += dt
        self._process_frame_ids(world)
        self._play_frame_sfx(world)
        self.life -= dt
        self.x += self.vx * dt
        self.y += self.vy * dt
        if self.distance is not None:
            self.distance -= abs(self.vx) * dt
            if self.distance <= 0:
                self.dead = True
                return
        if self.interval > 0 and self._interval_hits > 0:
            # 只有命中成功后才开始累计 interval，到点就递增一次攻击身份。
            self._interval_t += dt * 24.0
            while self._interval_t >= self.interval:
                self._interval_t -= self.interval
                self.attack_id += 1
        if self.anim:
            self.anim.update(dt)
        if self.life <= 0:
            self.dead = True
            return
        if self.distance is None and self.y > world.level.ground_y + 80:
            self.dead = True
            return
        if self.owner == "player":
            for target in world.bullet_targets(self):
                if not self.overlaps(target):
                    continue
                key = (int(self.hit_ns), int(self.attack_id), int(self.stage))
                if self.hit_seen(target, key):
                    continue
                dmg = world.apply_bullet_hit(self, target)
                if dmg is None:
                    # 未结算：父级保护、技能无敌、已吃过该身份等前置判定未通过
                    # → 不压栈、不扣次数，子弹继续飞。
                    continue
                # dmg > 0：真实命中；dmg == 0：魔法攻击被闪避
                # → 同样要记录命中身份并消耗一次命中次数。
                self.record_hit(target, key)
                self._interval_hits += 1
                # 溅射在扣减剩余命中次数之前结算。
                if self.attack_range > 0:
                    world.apply_bullet_splash(self, target)
                self.hit_max -= 1
                if self.hit_max <= 0:
                    self.dead = True
                    return
        else:
            p = getattr(world, "player", None)
            if p is None or p.dead:
                return
            if self.overlaps(p):
                dmg = p.take_damage(self.damage, self.x, self.kind,
                                    self.attack_id, self.kb,
                                    src_ns=self.hit_ns)
                block = getattr(p, "hit_block", "")
                if dmg:
                    world.add_damage_number(p.x, p.y - p.h - 8, dmg, "pnum")
                    world.add_effect("hero_hurt", p.x, p.y - p.h / 2)
                    world.assets.play("BeattackByRole1", 0.4)
                    self.dead = True
                elif block == "miss":
                    # 闪避（miss）同样算一次命中结算：记录命中身份 + 扣减
                    # 剩余命中次数，子弹照常销毁。
                    p.hit_ids.add((int(self.hit_ns), int(self.attack_id), 0))
                    self.dead = True
                # 其它 block（父级保护/免疫/被抓/去重）：法术结算是未命中
                # → 不压栈、不消耗，子弹继续飞行（父级保护结束后还能再命中）。
        if not self.dead and self._frame_expired():
            # 第 frame_limit 帧的帧事件才销毁；逐帧命中检测先跑
            # （age=0 是出生帧，age=k 播第 k+1 帧）→ 存活帧数 = frame_limit。
            self.dead = True

    def _frame_expired(self) -> bool:
        return (self.frame_limit is not None
                and self.age >= self.frame_limit - 1)

    # ------------------------------------------------------------- 绘制
    def draw(self, surface: pygame.Surface, camera_x: float) -> None:
        if self.anim:
            img = self.anim.frame
            sx, sy = self.art_scale
            if self.native_art:
                nw = max(1, round(img.get_width() * sx))
                nh = max(1, round(img.get_height() * sy))
                ax = self.anim.clip.anchor_x * sx
                ay = self.anim.clip.anchor_y * sy
            else:
                # 旧资源（Boss 大子弹等）仍按 200 px 归一化：
                target_w = min(200, max(24, self.radius * 4))
                base = min(1.0, target_w / img.get_width())
                nw = max(1, round(img.get_width() * base * sx))
                nh = max(1, round(img.get_height() * base * sy))
                ax = img.get_width() * base * sx / 2
                ay = img.get_height() * base * sy / 2
            if (nw, nh) != img.get_size():
                img = pygame.transform.smoothscale(img, (nw, nh))
            if self._flipped:
                img = pygame.transform.flip(img, True, False)
                ax = img.get_width() - ax
            surface.blit(img, (round(self.x - camera_x - ax),
                               round(self.y - ay)))
            return
        sx, sy = round(self.x - camera_x), round(self.y)
        pygame.draw.circle(surface, (210, 160, 60), (sx, sy), self.radius)
        pygame.draw.circle(surface, (255, 240, 180), (sx, sy),
                           max(2, self.radius // 3))
