"""本地双人竞技场（悟空对唐僧），双人按键各自独立映射。

唐僧技能在这里**不再有并行实现**：生成/判定/治疗/引爆全部走 ``game.tang``，
子弹是 ``game.projectile.Projectile``，与 ``game.world.World`` 同一份代码。
本模块只提供「命中结算 / 特效 / 跳字」三个钩子以及竞技场模式的差异。
"""
from __future__ import annotations

import pygame

from . import tang
from .assets import Assets
from .entities import Hit, Player
from .gxp_fx import GxpEffect
from .items import starter_items
from .level import load_level
from .projectile import Projectile
from .world import RIGHT_AUTHORED_FX, fx_scale_pair

LOGIC_DT = 1.0 / 24.0


class P2Keys:
    """把 P2 的按键映射为 Player.update 能识别的移动与攻击键。"""

    def __init__(self, real, left=False, right=False, attack=False):
        self.real = real
        self.left = left
        self.right = right
        self.attack = attack

    def __getitem__(self, key):
        if key == pygame.K_a:
            return self.left
        if key == pygame.K_d:
            return self.right
        if key == pygame.K_j:
            return self.attack
        return False


class ArenaWorld:
    def __init__(self, assets: Assets):
        self.assets = assets
        # 竞技场直接复用 4-1（牛魔谷）关卡的背景与地面资源。
        self.level = load_level("4-1")
        self.time = 0.0
        self.projectiles: list[Projectile] = []
        # 攻击命中身份的去重集合：与 World 同一语义，按目标分别记录。
        # 命中身份由目标自带的 ``hit_ids`` 维护，具体见 Projectile.hit_seen。
        # 延迟治疗的独立时间线：与英雄动作解耦，细节见 add_tang_cure。
        self.pending_cures: list[dict] = []
        self.fx: list[dict] = []
        self.texts: list[tuple[float, float, str, tuple[int, int, int], float]] = []
        p1 = {"hero": "wukong", "level": 5, "exp": 0, "hp": 280, "max_hp": 280,
              "mp": 130, "max_mp": 130, "equipment": starter_items("wukong")[1],
              "skills": {"active": [True] * 5, "passive": [0] * 5}}
        p2 = {"hero": "tangseng", "level": 5, "exp": 0, "hp": 130, "max_hp": 130,
              "mp": 260, "max_mp": 260, "equipment": starter_items("tangseng")[1],
              "skills": {"active": [True] * 5, "passive": [0] * 5}}
        self.p1 = Player(assets, self.level, p1)
        self.p2 = Player(assets, self.level, p2)
        self.p1.world = self
        self.p2.world = self
        # 两名英雄使用不同的命中命名空间（1 和 2），避免攻击身份互相串号。
        self.p1.hit_ns = 1
        self.p2.hit_ns = 2
        # 攻击实例命名空间计数器；每次攻击分配唯一编号，供命中去重与结算使用。
        self._hit_ns = 100
        self.p1.x, self.p1.y = 300, self.level.ground_y
        self.p2.x, self.p2.y = 640, self.level.ground_y
        # 竞技场模式会把两名英雄的生命值统一放大 500 倍。
        for fighter in (self.p1, self.p2):
            fighter.max_hp *= 500
            fighter.hp = fighter.max_hp
            # 竞技场模式不启用父级连击保护，避免连段计数影响单挑判定。
            fighter.has_father_effect = False
        self.p1.side, self.p2.side = "p1", "p2"
        self.finished = False
        self.winner = ""
        self.p2_left = False
        self.p2_right = False
        self.p2_attack = False
        self._logic_acc = 0.0
        self._pending_events: list = []
        self.freeze_timer = 0.0
        self.shake = 0.0
        self.gxp_effects: list[GxpEffect] = []

    # ------------------------------------------------------------ 竞技场模式标记
    @property
    def mode3(self) -> bool:
        """始终启用竞技场规则：队友之间的治疗在本模式下会被整段跳过。"""
        return True

    def new_hit_ns(self) -> int:
        """分配攻击实例命名空间（与 ``World.new_hit_ns`` 同一实现/语义）。"""
        self._hit_ns += 1
        return self._hit_ns

    def tang_players(self) -> list[Player]:
        """返回竞技场中的两名英雄（P1、P2）。"""
        return [self.p1, self.p2]

    def shadow_limits(self) -> tuple[float, float]:
        """限制英雄横向移动，使碰撞椭圆的左右边缘保持在屏幕 [20,920] 内。"""
        return 20.0 + 22.5, 920.0 - 22.5

    def _start_gxp_effect(self, player) -> None:
        """只展示该玩家的立绘并短暂冻结画面，用于无双技（GXP）演出。"""
        self.gxp_effects.append(GxpEffect(self.assets, player.hero, owner=player))
        self.freeze_timer = max(self.freeze_timer, 1.0)

    @property
    def player_walls(self):
        return self.level.walls

    # ------------------------------------------------------- 子弹管线
    def bullet_targets(self, projectile: Projectile) -> list[Player]:
        """返回子弹可攻击的敌人列表：竞技场模式下只挑「另一名玩家」。"""
        owner = projectile.damage_owner
        other = self.p2 if owner is self.p1 else self.p1
        return [other] if not other.dead else []

    def roll_projectile_damage(self, projectile: Projectile):
        """命中时重新读取实时攻击力：幻影子弹以召唤者为基准换算伤害。"""
        owner = projectile.damage_owner
        if owner is None or projectile.mult is None:
            return int(projectile.damage), projectile.base_power
        raw, expected = owner.roll_damage(projectile.mult,
                                          float(projectile.fixed or 0))
        projectile.damage = raw
        projectile.base_power = expected
        return raw, expected

    def apply_bullet_hit(self, projectile: Projectile, target,
                         register: bool = True) -> int | None:
        """竞技场模式下的一次命中结算。

        返回值沿用统一的命中语义：
        * ``None`` —— 未结算（连击保护 / 技能无敌 / 该命中身份已记录），
          子弹**不消耗**，留待后续判定；
        * ``0`` —— 魔法攻击未命中，子弹仍要压栈并扣减
          可用命中次数；
        * ``>0`` —— 真实命中并造成伤害。

        ``register=False`` 表示强制溅射：不查重、不登记命中身份，
        只有主目标会压栈，避免溅射重复消耗同一颗子弹。
        """
        key = (int(projectile.hit_ns), int(projectile.attack_id),
               int(projectile.stage))
        if register:
            if target.dead or target.grabbed:
                return None
            # 霸体或技能无敌时返回 None，子弹不消耗并继续飞行。
            if getattr(target, "father_frames", 0) > 0 or target.immune:
                return None
            if key in getattr(target, "hit_ids", ()):
                return None
        raw, expected = self.roll_projectile_damage(projectile)
        dmg = target.take_damage(raw, projectile.x, projectile.kind,
                                 projectile.attack_id, projectile.kb,
                                 stage=projectile.stage,
                                 src_ns=projectile.hit_ns,
                                 register=register)
        if dmg is None:
            return None
        if dmg <= 0:
            # 上方已排除连击保护、无敌和命中去重，剩下的 0 只可能来自未命中；
            # 子弹仍要压栈，由调用方 projectile.update 记录这次结算。
            return 0
        self.add_damage_text(target.x, target.y - target.h - 8, dmg)
        owner = projectile.damage_owner
        if isinstance(owner, Player):
            # 竞技场经验按 8 倍倍率结算。
            # 分子使用未减防的原始攻击力，防御减伤只影响实际造成的伤害。
            owner.add_gxp(raw, expected, arena=True)
        return dmg

    def apply_bullet_splash(self, projectile: Projectile, main) -> None:
        """攻击范围溅射（竞技场只有一名对手，通常没有额外目标）。

        与 ``World.apply_bullet_splash`` 同一口径：``force=true`` 强制结算，
        但**不查重、不登记命中身份**，避免同一目标被溅射重复结算。
        """
        for other in self.bullet_targets(projectile):
            if other is main:
                continue
            if abs(other.x - main.x) > projectile.attack_range:
                continue
            self.apply_bullet_hit(projectile, other, register=False)

    def player_attack_hit(self, player, base_damage: int, hit: Hit | None = None,
                          attack_id: int = 0, aoe: float = 0.0,
                          base_power: float = 0.0,
                          rect: pygame.Rect | None = None) -> None:
        other = self.p2 if player is self.p1 else self.p1
        if rect is not None:
            pass
        elif aoe:
            rect = pygame.Rect(round(player.x - aoe), round(player.y - player.h - 60),
                               round(aoe * 2), round(player.h + 80))
        elif player.facing > 0:
            rect = pygame.Rect(round(player.x + 4), round(player.y - 104), 96, 108)
        else:
            rect = pygame.Rect(round(player.x - 100), round(player.y - 104), 96, 108)
        if rect.colliderect(other.body):
            kind = hit.kind if hit else "physics"
            dmg = other.take_damage(base_damage, player.x, kind=kind,
                                    attack_id=attack_id,
                                    kb=hit.kb if hit else (0, 0),
                                    stage=hit.stage if hit else 0,
                                    src_ns=player.hit_ns)
            if dmg:
                self.add_damage_text(other.x, other.y - other.h - 8, dmg)
                # 竞技场经验按 8 倍倍率结算，并使用未减防的原始攻击力。
                player.add_gxp(dmg, base_power, arena=True)

    def spawn_player_bullet(self, player, damage: int, kind: str, attack_id: int,
                            stage: int, action: str,
                            base_power: float = 0.0) -> None:
        """生成玩家子弹的兼容入口；唐僧的弹道已统一由 game.tang 的帧事件生成。"""
        self.projectiles.append(Projectile(
            player.x, player.y - 45, player.facing * 620.0, 0.0, damage,
            "player", kind, attack_id, "bullet_role2_2", radius=20,
            life=1.4, stage=stage, base_power=base_power, source=player,
            hit_ns=self.new_hit_ns(),
            ))

    def add_damage_text(self, x, y, value, color=(255, 230, 120)):
        self.texts.append((x, y, str(value), color, 0.8))

    def add_heal_number(self, player, amount: int) -> None:
        self.add_damage_text(player.x, player.y - player.h - 10, amount,
                             (140, 255, 140))

    # ---- 帧动作世界接口（名称与战斗世界的 World 保持一致） ----
    def add_message(self, text: str, ttl: float = 1.0) -> None:
        self.texts.append((470, 80, text, (255, 230, 120), ttl))

    def add_effect(self, name: str, x: float, y: float, facing: int = 1,
                   ttl: float = 1.2, scale=1.0) -> None:
        clip = self.assets.fx(name)
        if clip is not None:
            flip = (facing < 0) if name in RIGHT_AUTHORED_FX else (facing > 0)
            self.fx.append({"name": name, "clip": clip, "x": x, "y": y,
                            "facing": facing, "flip": flip, "t": 0.0,
                            "ttl": ttl, "scale": scale})

    # ---- 唐僧技能（统一实现在 game/tang.py，避免出现两套分叉逻辑） ----
    def spawn_tang_bullet(self, player, spec: dict) -> Projectile:
        projectile = tang.spawn_bullet(self, player, spec)
        if spec.get("sfx"):
            self.assets.play(str(spec["sfx"]), 0.5)
        return projectile

    def spawn_tang_aoe(self, player) -> Projectile:
        return tang.spawn_aoe(self, player)

    def spawn_tang_aoe_at(self, x, y, attack_id: int, source=None) -> Projectile:
        return tang.spawn_aoe_at(self, x, y, attack_id, source=source or self.player)

    def tang_heal(self, player) -> None:
        tang.heal(self, player)

    def add_tang_cure(self, caster, x: float, y: float) -> None:
        self.pending_cures.append({"left": tang.HEAL_DELAY_TICKS,
                                   "caster": caster, "x": float(x),
                                   "y": float(y)})

    def tang_heal_at(self, x, y, radius: float = 200.0, source=None) -> None:
        tang.heal_at(self, x, y, source or self.player)

    def detonate_hit4(self, player) -> Projectile | None:
        return tang.detonate_hit4(self, player)

    @property
    def player(self) -> Player:
        """兼容 ``Player``/``tang`` 里对 ``world.player`` 的引用（P2=唐僧）。"""
        return self.p2

    def hit10_stage(self, player, key: str, target=None) -> None:
        table = {"hit10-1": (1.0, "physics", (2, -3), 0),
                 "hit10-2": (3.0, "magic", (40, -15), 2),
                 "hit10-3": (1.0, "magic", (40, 25), 1)}
        mult, kind, kb, stage = table[key]
        stage = player.hit10_stage_id(key)
        raw, expected = player.roll_damage(mult)
        other = self.p2 if player is self.p1 else self.p1
        if target is not None:
            rect = target.hit_rect.inflate(80, 60)
        elif key == "hit10-2":
            rect = pygame.Rect(round(player.x - 180), round(player.y - 360), 360, 380)
        else:
            rect = pygame.Rect(round(player.x - 120), round(player.y - 240), 240, 260)
        hit = Hit(0.0, 0.0, mult=mult, kind=kind, kb=kb, hit_max=100, stage=stage)
        self.player_attack_hit(player, int(raw), hit=hit,
                               attack_id=player.attack_id, rect=rect,
                               base_power=expected)
        if key == "hit10-1" and target is not None:
            self.add_effect("hit10_and1", target.x, target.y - 45, player.facing)
        elif key == "hit10-2":
            self.add_effect("hit10_and2", player.x, player.y - 45,
                            player.facing, ttl=13 / 24,
                            scale=(2.1, 3.0) if player.is_gxp else (1.4, 2.0))
        else:
            self.add_effect("hit10_flush", player.x, player.y - 45,
                            player.facing, ttl=2 / 24)

    def hit10_flush(self, player) -> None:
        player.last_hit = "hit10-3"
        self.hit10_stage(player, "hit10-3")

    def hit10_grab(self, player) -> bool:
        import math as _math
        other = self.p2 if player is self.p1 else self.p1
        if other.dead or _math.hypot(other.x - player.x, other.y - player.y) > 130:
            return False
        self.hit10_stage(player, "hit10-1", target=other)
        player.x, player.y = other.x, other.y
        player.last_hit = "hit10-1"
        player.timers = 40
        return True

    def hit10_land(self, player) -> None:
        if player.last_hit == "hit10-2":
            return
        self.hit10_stage(player, "hit10-2")
        player.last_hit = "hit10-2"

    def _p2_events(self, e):
        if e.type != pygame.KEYDOWN:
            return
        # 帮助界面中 P2 的攻击、跳跃、无双（GXP）分别对应小键盘 1、2、0，
        # 技能键为 C/D/E/F。
        if e.key == pygame.K_KP1:
            self.p2_attack = True
            self.p2.normal_attack()
        elif e.key == pygame.K_KP2:
            self.p2.jump()
        elif e.key == pygame.K_KP0:
            if self.p2.activate_gxp():
                self._start_gxp_effect(self.p2)
                return
        # P2 的四个主动技能：C=水魔爆(4)、D=玄冰阵(1)、E=水幻影(2)、
        # F=天降甘露(3)；小键盘 0 触发 GXP 无双技。
        # 括号里的数字对应角色技能栏槽位，与按键顺序无关。
        elif e.key == pygame.K_c:
            self.p2.cast_active(4)
        elif e.key == pygame.K_d:
            self.p2.cast_active(1)
        elif e.key == pygame.K_e:
            self.p2.cast_active(2)
        elif e.key == pygame.K_f:
            self.p2.cast_active(3)

    def update(self, dt: float, keys, events) -> None:
        """固定 24 Hz 逻辑步进，与 ``World.update`` 保持一致。"""
        if self.finished:
            return
        self._pending_events.extend(events or [])
        self._logic_acc += dt
        steps = 0
        while self._logic_acc >= LOGIC_DT and steps < 5:
            queued = self._pending_events
            self._pending_events = []
            self._step(LOGIC_DT, keys, queued)
            self._logic_acc -= LOGIC_DT
            steps += 1
            if self.finished:
                break
        if steps >= 5:
            self._logic_acc = 0.0

    def _step(self, dt: float, keys, events) -> None:
        if self.finished:
            return
        self.time += dt
        self.shake = max(0.0, self.shake - dt)
        if self.freeze_timer > 0:
            self.freeze_timer = max(0.0, self.freeze_timer - dt)
            if self.freeze_timer < 1e-9:
                self.freeze_timer = 0.0
            for eff in self.gxp_effects:
                eff.update(dt)
            self.gxp_effects = [e for e in self.gxp_effects if not e.done]
            return
        p2_keys = P2Keys(keys, self.p2_left, self.p2_right, self.p2_attack)
        for e in events:
            if e.type == pygame.KEYDOWN:
                if e.key == pygame.K_LEFT:
                    self.p2_left = True
                elif e.key == pygame.K_RIGHT:
                    self.p2_right = True
            elif e.type == pygame.KEYUP:
                if e.key == pygame.K_LEFT:
                    self.p2_left = False
                elif e.key == pygame.K_RIGHT:
                    self.p2_right = False
                if e.key == pygame.K_KP1:
                    self.p2_attack = False
                if e.key in (pygame.K_s, pygame.K_j, pygame.K_k, pygame.K_w):
                    self.p1.can_next = True
                if e.key == pygame.K_j:
                    self.p1.request_tang_release()
                if e.key in (pygame.K_KP1, pygame.K_KP2, pygame.K_UP, pygame.K_DOWN):
                    self.p2.can_next = True
                if e.key == pygame.K_KP1:
                    self.p2.request_tang_release()
            if e.type != pygame.KEYDOWN:
                continue
            if e.key == pygame.K_j:
                if keys[pygame.K_w]:
                    self.p1.cast_active(0)
                elif self.p1.running:
                    self.p1.cast_active(1)
                else:
                    self.p1.normal_attack()
            elif e.key == pygame.K_k:
                self.p1.jump()
            elif e.key == pygame.K_SPACE:
                if self.p1.activate_gxp():
                    self._start_gxp_effect(self.p1)
                    return
            elif e.key in self._p1_skill_map():
                self.p1.cast_active(self._p1_skill_map()[e.key])
            elif e.key == pygame.K_a:
                self.p1.tap_direction(-1, self.time)
            elif e.key == pygame.K_d:
                self.p1.tap_direction(1, self.time)
            self._p2_events(e)
            if self.freeze_timer > 0:
                return
        for fighter in (self.p1, self.p2):
            if getattr(fighter, "hit4_detonate_requested", False):
                self.detonate_hit4(fighter)
        self.p1.update(dt, keys, self)
        self.p2.update(dt, p2_keys, self)
        for fighter in (self.p1, self.p2):
            half = fighter.w / 2.0
            fighter.x = max(20.0 + half, min(fighter.x, 920.0 - half))
        for pr in list(self.projectiles):
            pr.update(dt, self)
        self.projectiles = [pr for pr in self.projectiles if not pr.dead]
        for i, (x, y, txt, color, ttl) in enumerate(self.texts):
            self.texts[i] = (x, y - 30 * dt, txt, color, ttl - dt)
        self.texts = [t for t in self.texts if t[4] > 0]
        for f in self.fx:
            f["t"] += dt
        self.fx = [f for f in self.fx if f["t"] < f["ttl"]]
        for cure in list(self.pending_cures):
            if cure["left"] <= 0:
                self.pending_cures.remove(cure)
                tang.cure_role(self, cure["caster"], cure["x"], cure["y"])
            else:
                cure["left"] -= 1
        for eff in self.gxp_effects:
            eff.update(dt)
        self.gxp_effects = [e for e in self.gxp_effects if not e.done]
        for fighter in (self.p1, self.p2):
            if fighter.dead and not fighter._dead_fx:
                fighter._dead_fx = True
                fighter.hidden = True
                self.add_effect("deadmb", fighter.x, fighter.y - 10,
                                fighter.facing, ttl=4.0)
        if self.p1.dead:
            self.finished, self.winner = True, "唐僧"
        elif self.p2.dead:
            self.finished, self.winner = True, "悟空"

    def _p1_skill_map(self) -> dict:
        """P1 的技能快捷键：唐僧用 U/I/O/L，悟空用 U/I/O，空格触发 GXP。

        P1 的唐僧映射为 ``{U:1, I:2, O:3, L:4}``；悟空没有 4 号主动技能，
        0/1 号技能改由 J 组合键触发，因此这里只返回 U/I/O 三个技能键。
        """
        if self.p1.hero == "tangseng":
            return {pygame.K_u: 1, pygame.K_i: 2, pygame.K_o: 3, pygame.K_l: 4}
        return {pygame.K_u: 2, pygame.K_i: 3, pygame.K_o: 4}

    def draw(self, surface: pygame.Surface) -> None:
        far = self.assets.image(self.level.far_background)
        surface.blit(far, (0, 0))
        bg = self.assets.image(self.level.background)
        bg_x = round(self.level.background_x_offset)
        bg_y = round(self.level.background_y)
        bb = getattr(self.level, "background_bbox", None)
        if bb:
            area = pygame.Rect(round(bb[0]), round(bb[1]),
                               round(bb[2]), round(bb[3]))
            surface.blit(bg, (bg_x + area.x, bg_y + area.y), area=area)
        else:
            surface.blit(bg, (bg_x, bg_y))
        for d in sorted(self.level.deco, key=lambda x: int(x.get("depth", 0))):
            img = self.assets.image(d["img"])
            if d.get("flip"):
                img = pygame.transform.flip(img, True, True)
            surface.blit(img, (round(d["x"]), round(d["y"])))
        for ent in (self.p1, self.p2):
            ent.draw(surface, 0)
        for f in self.fx:
            clip = f["clip"]
            idx = min(len(clip.frames) - 1, int(f["t"] * clip.fps))
            img = clip.frames[idx]
            sx, sy = fx_scale_pair(f.get("scale", 1.0))
            ax = clip.anchor_x * sx
            ay = clip.anchor_y * sy
            if (sx, sy) != (1.0, 1.0):
                img = pygame.transform.smoothscale(
                    img, (max(1, round(img.get_width() * sx)),
                          max(1, round(img.get_height() * sy))))
            if f.get("flip", f["facing"] > 0):
                img = pygame.transform.flip(img, True, False)
                ax = img.get_width() - ax
            surface.blit(img, (round(f["x"] - ax), round(f["y"] - ay)))
        for pr in self.projectiles:
            pr.draw(surface, 0.0)
        for x, y, txt, color, ttl in self.texts:
            font = self.assets.font(18, bold=True)
            surface.blit(font.render(txt, True, color), (x - 8, y))
        for eff in self.gxp_effects:
            eff.draw(surface)
