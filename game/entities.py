"""英雄、怪物、物理与战斗逻辑。

各项数值的由来：

* 英雄  -> 基础成长曲线，以及连击伤害倍率与法力消耗
* 怪物  -> 伤害回馈表，以及生命、防御、经验与掉落表
* 成长  -> 升级时的属性增长与被动技能解析
"""
from __future__ import annotations

import json
import logging
import math
import random
from dataclasses import dataclass, field

import pygame

from .assets import Animation, Assets, Clip
from .action_data import ActionDB, ActionDef
from .constants import (
    ASSETS_DIR, DATA_DIR, GRAVITY, JUMP_SPEED, MAX_FALL, RUN_SPEED, WALK_SPEED,
    HitKey, next_hit_ns,
)
from . import tang
from .frame_action import FrameAction
from .items import appearance_id, bonuses
from .level import Level, Wall
from .paperdoll import action_layer_overrides
from .skills import normalize, passive_bonus

log = logging.getLogger(__name__)

LEVEL_EXP = [135, 145, 155, 165, 175, 185, 625, 675, 725, 775,
             825, 875, 1950, 2050, 2150, 2250, 2350, 2450, 5000]

# 英雄碰撞椭圆中心相对脚底的高度（像素）。角色贴图原始尺寸 100x90，
# 缩放系数 0.45/1.0001，纵向平移量按英雄区分：悟空 -0.05、唐僧 -4.2。
# 纸娃娃动画的锚点在根节点 +45 px 处；因此把根节点画在
# ``foot_y - collision_bottom`` 位置再叠加 45，即可得到正确的落地对齐。
HERO_COLLISION_BOTTOM = {"wukong": 44.95, "tangseng": 40.8}
HERO_MANIFEST_ANCHOR = 45.0

# ---- 人物连击保护（father / 霸体）：挂在本方角色上的常驻增益效果，
# 连续受击 5 次后触发 48 帧（2.0 秒）免伤与红/蓝辉光；
# 相邻两次受击间隔 ≥1000 ms 则计数清零；竞技场模式下不生效。
FATHER_TOTAL = 5            # 触发霸体所需的连续受击次数，累计满 5 次即触发
FATHER_FRAMES = 48          # 免伤持续 48 帧，按 24 帧/秒计为 2.0 秒
FATHER_INTERVAL_MS = 1000   # 计数清零间隔：两次受击相隔 ≥1 s 就把累计数清零


def hero_stats(hero: str, level: int) -> dict:
    """按等级计算英雄基础属性（沿用逐级成长公式）。"""
    level = max(1, int(level))
    exp_need = LEVEL_EXP[level - 1] if level <= 19 else 5000 + (level - 19) * 5000
    if hero == "tangseng":
        return {
            "max_hp": 50 + 20 * (level - 1),
            "max_mp": 100 + 40 * (level - 1),
            "atk_min": 12 + 8 * (level - 1),
            "atk_max": 12 + 8 * (level - 1),
            "defense": max(0, level - 1),
            "exp_need": exp_need,
        }
    max_hp = 80 + 50 * (level - 1)
    max_mp = 50 + 20 * (level - 1)
    atk_min = 10 + 5 * (level - 1)
    atk_max = 14 + 5 * (level - 1)
    if level < 15:
        defense = 2 + 2 * (level - 1)
    else:
        defense = 2 + 2 * (14 - 1) + (level - 14) * 5
    return {
        "max_hp": max_hp, "max_mp": max_mp, "atk_min": atk_min, "atk_max": atk_max,
        "defense": defense, "exp_need": exp_need,
    }


def wukong_stats(level: int) -> dict:
    return hero_stats("wukong", level)


def physical_damage(attack: float, defense: float) -> int:
    return max(1, int(round(attack - defense)))


def body_rect(x: float, y: float, w: float, h: float) -> pygame.Rect:
    return pygame.Rect(round(x - w / 2), round(y - h), round(w), round(h))


def _stick_has_geometry(win: dict) -> bool:
    """判断该 stick 窗口是否带矩形范围或逐帧表（而不只是 from/to 端点）。"""
    return bool(win.get("per_frame") or "ox" in win)


def apply_physics(actor, dt: float, walls: list[Wall], slopes: list[dict] | None = None,
                  gravity_scale: float = 1.0) -> None:
    """按脚底中心坐标移动角色，并处理墙体与平台的阻挡、落地和支撑。"""
    slopes = slopes or []
    was_ground = actor.on_ground
    actor.x += actor.vx * dt
    foot_inset = min(4.0, max(0.0, actor.h - 1.0))
    rect = body_rect(actor.x, actor.y - foot_inset,
                     actor.w, max(1.0, actor.h - foot_inset))
    for wall in walls:
        # "terrain" 指比地面高 20 px、两侧由斜坡衔接的台沿。若把台沿并入
        # 侧向阻挡检测，英雄走到 45 度斜坡中段就会被挡住：45 px 的碰撞椭圆
        # 会比脚底更早碰到台沿；因此本实现让台沿不参与侧向检测，
        # 斜坡才走得上去。
        if wall.kind not in ("solid", "stop"):
            continue
        wr = pygame.Rect(round(wall.x), round(wall.y), round(wall.w), round(wall.h))
        if rect.colliderect(wr):
            if actor.vx > 0:
                actor.x = wall.x - actor.w / 2
            elif actor.vx < 0:
                actor.x = wall.x + wall.w + actor.w / 2
            actor.vx = 0
            rect = body_rect(actor.x, actor.y - foot_inset,
                             actor.w, max(1.0, actor.h - foot_inset))

    prev_feet = actor.y
    # 垂直位移的积分顺序：先按当前速度移动 y，再给速度累加重力。
    # 若先累加重力，起跳第一帧的高度会少 1.5 px，
    # 跳跃最高点会从 143.5 掉到 123.5。
    actor.y += actor.vy * dt
    actor.vy = min(actor.vy + GRAVITY * gravity_scale * dt, MAX_FALL)
    actor.on_ground = False
    actor.support_wall = None
    rect = body_rect(actor.x, actor.y, actor.w, actor.h)
    for wall in walls:
        wr = pygame.Rect(round(wall.x), round(wall.y), round(wall.w), round(wall.h))
        if wall.kind == "oneway":
            if getattr(actor, "drop_timer", 0.0) > 0:
                continue
            horizontal = rect.right > wr.left + 2 and rect.left < wr.right - 2
            # 落地判定沿用 +8 px 容差，单向平台同样适用：
            # 可穿透标志只跳过天花板与侧向两个分支，不影响落地。
            # 容差用于吸收脚底与台面之间的浮点误差。
            if actor.vy >= 0 and horizontal and prev_feet <= wall.y + 8 and actor.y >= wall.y:
                actor.y = wall.y
                actor.vy = 0
                actor.on_ground = True
                actor.support_y = float(wall.y)
                actor.support_wall = wall
                rect = body_rect(actor.x, actor.y, actor.w, actor.h)
            continue
        if wall.kind == "stop":
            continue
        horizontal = rect.right > wr.left + 2 and rect.left < wr.right - 2
        # 落地容差：下落速度 > 0，且脚底不高于台面顶部 + 8 px。
        # 只用 2 px 容差会漏掉斜坡顶端与台沿的接缝
        # （接缝处脚底比台面低约 5 px），
        # 早期实现靠通用兜底把这种情况盖住，这里显式放宽到 8 px。
        if (horizontal and actor.vy >= 0
                and prev_feet <= wall.y + 8 and actor.y >= wall.y):
            actor.y = wall.y
            actor.vy = 0
            actor.on_ground = True
            actor.support_y = float(wall.y)
            actor.support_wall = wall
            rect = body_rect(actor.x, actor.y, actor.w, actor.h)
            continue
        if not rect.colliderect(wr):
            continue
        # 走到这里说明不是落地（落地分支上面已处理），而是侧面或拐角接触。
        # 这种情况下只做水平推出，既不吸附墙顶，也不穿过墙底。
        # 早期实现无条件按顶/底吸附，在地面与水面的 1 px 拐角上
        # 每隔一帧就把角色瞬移到墙顶，导致 on_ground 反复翻转、
        # 等待动画与下落动画来回闪烁。
        # 所以这里只保留水平修正，
        # 垂直方向留给落地与天花板分支。
        if actor.vy < 0 and horizontal and wr.bottom <= actor.y + 2:
            # 真正的天花板：角色上升时，上方墙体与角色水平方向确有重叠。
            # 侧面或拐角的重叠不在这里处理，
            # 交给水平推出分支和下一帧判定。
            actor.y = wall.y + wall.h + actor.h
            actor.vy = 0
            rect = body_rect(actor.x, actor.y, actor.w, actor.h)

    # ---- 斜坡：每帧把速度方向重定向到斜面，而非按距离直接吸附坐标。
    # 这样角色沿斜面移动完全由速度驱动，位置不会被强行拉扯。
    #
    # 当角色下一帧的包围盒碰到旋转矩形时，把速度重定向到斜面方向。
    # 对行走中的角色，用 ``y = surface(x_new)`` 沿表面走与速度重定向等价：
    # surface(x+vx*dt) = y + tan(rot)*vx*dt。早期实现有两处错误：
    # 一是判定窗口从 x0-half_w 扩到 x1+half_w，越过了斜坡端点；
    # 二是用 8..24 px 的吸附容差，把站在或跳过斜坡附近的角色吸到面上。
    # 本实现只按实际线段范围判定，并以每帧爬升量作为容差。
    for s in slopes:
        x0, x1 = float(s["x0"]), float(s["x1"])
        if x1 == x0:
            continue
        lo, hi = (x0, x1) if x0 <= x1 else (x1, x0)
        # 只认实际线段范围，不向两端各扩半个身体：
        # 站在 x1+22 处的角色必须保持自己原有的支撑高度。
        if not (lo - 1.0 <= actor.x <= hi + 1.0):
            continue
        rot = s.get("rot")
        if rot is None:
            rot = math.degrees(math.atan2(float(s["y1"]) - float(s["y0"]),
                                          x1 - x0))
        rad = math.radians(float(rot))
        t = min(1.0, max(0.0, (actor.x - x0) / (x1 - x0)))
        surface = float(s["y0"]) + (float(s["y1"]) - float(s["y0"])) * t
        # 本帧水平位移对应的表面高度变化量。这里保留水平输入速度
        # （若按斜面方向重定向，水平分量会变成 cos(rot)*vx，
        # 同时也会按同一系数缩小 x 位移），
        # 因此竖直目标高度的增量取 tan(rot)*vx*dt。
        if abs(math.cos(rad)) > 1e-6:
            climb = abs(math.tan(rad) * float(getattr(actor, "vx", 0.0)) * dt)
        else:
            climb = 0.0
        # (1) 从上方落到斜坡：与墙体落地的条件一致——
        # 速度 vy >= 0，且脚底本帧跨过了斜面线。
        if (float(getattr(actor, "vy", 0.0)) >= 0
                and prev_feet <= surface + 8.0 and actor.y >= surface):
            actor.y = surface
            actor.vy = 0.0
            actor.on_ground = True
            actor.support_y = surface
            actor.support_wall = s
            actor.support_slope = s
            continue
        # (2) 已经在斜坡上：把竖直分量贴合到斜面上。
        # 容差收紧为一帧爬升量 + 2.5 px，
        # 取代早期 climb+8 / climb+24 的宽松吸附。
        #
        # 例外：上面的墙体落地分支用的是 45 px 宽的身体包围盒，
        # 因此从与斜坡相接的平台走下来时，脚底可能仍被钉在平台顶面，
        # 而身体其实还与平台重叠（此时中心已在斜坡上）。
        # 只在这种接缝处允许斜坡接管：
        # 即支撑墙顶面高度与斜坡端点高度相等时。
        support = getattr(actor, "support_wall", None)
        junction = False
        if support is not None and support is not s and actor.on_ground:
            support_top = getattr(support, "y", None)
            if support_top is None and isinstance(support, dict):
                support_top = support.get("y")
            if support_top is not None:
                junction = (abs(float(support_top) - float(s["y0"])) <= 2.0
                            or abs(float(support_top) - float(s["y1"])) <= 2.0)
        tol = climb + (24.0 if junction else 2.5)
        if (abs(actor.y - surface) <= tol
                and (was_ground or float(getattr(actor, "vy", 0.0)) >= 0)):
            actor.y = surface
            actor.vy = 0.0
            actor.on_ground = True
            actor.support_y = surface
            actor.support_wall = s
            actor.support_slope = s


@dataclass
class Hit:
    """一次攻击中的一个有效命中窗口。"""
    start: float
    end: float
    mult: float = 1.0
    fixed: int = 0
    kind: str = "physics"
    kb: tuple[float, float] = (0, 0)
    interval: float = 4 / 24
    hit_max: int = 1
    stage: int = 0


@dataclass
class Action:
    name: str
    clip: Clip
    hits: list[Hit] = field(default_factory=list)
    dash: float = 0.0
    invuln: float = 0.0
    projectile: bool = False
    aoe: float = 0.0
    heal: bool = False
    on_finish: str = ""


@dataclass
class GxpAfterimage:
    """一帧残影快照（位图 + 坐标 + 已存活时长）。"""

    surface: pygame.Surface
    x: float
    y: float
    age: float = 0.0

    @property
    def alpha(self) -> int:
        # 默认缓出曲线：1-(1-t)^2，透明度随存活时间平方衰减。
        p = min(1.0, max(0.0, self.age))
        return int(255 * (1.0 - p) ** 2)


# 幻影甘露的残影生成帧：技能动作共 50 帧，加血动作占 34 帧，故从第 16 帧起生成残影。
_SHADOW_CURE_FRAME = 16


@dataclass
class Shadow:
    """唐僧的纯黑剪影分身。

    * 存活 192 帧，到时自动消失；
    * ``action`` 为 ``"walk"`` 时以 8 px/帧朝召唤时的朝向移动，
      左右边界为屏幕 ``[20,920]``，撞到即停；
    * 释放冰柱或加血技能后转为静止，动作分别停留 50 帧；
    * 不进入英雄数组，也没有近战判定循环 → 怪物不会把它当目标，
      它也不会产生近战伤害。
    """

    x: float
    y: float
    facing: int = 1
    source: "Player | None" = None
    frames: int = 0
    action: str = ""              # 空串表示行走，另可为 "hit3" / "hit5"
    action_frames: int = 0
    attack_id: int = 0            # 攻击编号，初值 0 且永不自增
    # 幻影是独立的英雄实例，拥有自己的一份身份命名空间。
    hit_ns: int = field(default_factory=next_hit_ns)
    is_gxp: bool = False          # 分身不参与幻影甘露增益，恒为 False
    anim: Animation | None = None
    w: int = 45
    h: int = 90
    collision_bottom: float = 40.8   # 与唐僧本体相同的碰撞椭圆，脚底偏移 40.8
    vx: float = 0.0
    vy: float = 0.0
    on_ground: bool = False
    support_y: float = 0.0
    support_wall: object | None = None
    dead: bool = False

    @property
    def hit_rect(self) -> pygame.Rect:
        return body_rect(self.x, self.y, self.w, self.h)

    @property
    def clip_name(self) -> str:
        return tang.SHADOW_CLIPS.get(self.action, "shadow_walk")

    @property
    def damage_owner(self) -> "Player | None":
        """伤害计算取召唤者（英雄）的攻击力与幻影甘露增益。"""
        return self.source
class Player:
    def __init__(self, assets: Assets, level: Level, saved: dict | None = None):
        self.assets = assets
        self.level = level
        self.saved = saved or {}
        # 世界反向引用（World/ArenaWorld 构造后绑定）：技能引爆的实体是同步
        # 生成的，按键处理里必须立刻拿到 world 才能挂到正确场景。
        self.world = None
        self.hero = str(self.saved.get("hero", "wukong"))
        self.level_no = int(self.saved.get("level", 1))
        self.exp = int(self.saved.get("exp", 0))
        # 经验值一变就要立刻写回实时存档对象，所以 World 把这里接到
        # 存活的 save_data 上（这样死亡或退出后经验也不会丢）。
        self.on_exp_change = None
        stats = hero_stats(self.hero, self.level_no)
        self.equipment = dict(self.saved.get("equipment") or {})
        self.skills = normalize(self.hero, self.saved.get("skills"))
        self.equip_bonus = bonuses(self.equipment)
        # 纸娃娃外观帧号：按当前装备的服装、武器查表得到
        self.cloth_id = appearance_id(self.equipment.get("zbfj"))
        self.weapon_id = appearance_id(self.equipment.get("zbwq"))
        pb = passive_bonus(self.hero, self.skills)
        self.crit = max(0.0, min(1.0, float(self.equip_bonus.get("crit", 0)) + pb["crit"]))
        self.miss = max(0.0, min(0.6, float(self.equip_bonus.get("miss", 0))))
        self.hp_regen = int(self.equip_bonus.get("hp_regen", 0)) + pb["hp_regen"]
        self.mp_regen = int(self.equip_bonus.get("mp_regen", 0)) + pb["mp_regen"]
        self.max_hp = int(stats["max_hp"]) + pb["hp"] + self.equip_bonus["hp"]
        self.max_mp = int(stats["max_mp"]) + pb["mp"] + self.equip_bonus["mp"]
        self.hp = int(self.saved.get("hp", self.max_hp))
        self.hp = max(1, min(self.hp, self.max_hp))
        # 恢复计数（T15 B-10）：自然回血、回蓝每 24 帧结算一次。
        self.regen_count = 0
        self.mp = int(self.saved.get("mp", self.max_mp))
        self.mp = max(0, min(self.mp, self.max_mp))
        self.defense = int(stats["defense"]) + self.equip_bonus["def"]
        self.exp_need = stats["exp_need"]
        self.atk_min = int(stats["atk_min"]) + self.equip_bonus["atk"]
        self.atk_max = int(stats["atk_max"]) + self.equip_bonus["atk"]

        self.collision_bottom = HERO_COLLISION_BOTTOM.get(self.hero, 45.0)
        self.x, self.y = (float(level.player_start[0]), float(level.player_start[1]))
        self.support_y = float(level.ground_y)
        self.support_wall = None
        self.spawn_x, self.spawn_y = self.x, self.y
        self.vx = self.vy = 0.0
        self.camera_speed = 0.0
        self.w, self.h = 45, 90
        self.facing = 1
        self.on_ground = True
        self.jumps = 0
        self.running = False
        self.run_timer = 0.0
        self.double_count = 0
        self.last_dir_tap = 0
        self.last_tap = {"left": -10.0, "right": -10.0}
        self.state = "idle"
        self.anim = Animation(self._clip("wait"), loop=True)
        # 动作帧数据表（data/actions.json）：帧号、判定盒与帧事件
        self.action_db = ActionDB.load()
        self.frame_action = FrameAction(self.action_db, self.hero)
        self.action_after: list[str] = []
        self.action_gravity: float | None = None
        self.action_locked = False
        self.clock = 0.0
        self.timers = 0.0          # 当前动作剩余计时，单位=帧
        self.can_next = True       # 是否允许接下一段动作
        self.hit_num = 1           # 悟空连招段数
        self.last_attack_clock = -10.0
        self.charge_frames = 0     # 唐僧 hit2 蓄力帧
        self.round_active = False  # 唐僧 hit4 的圆形判定实体是否已就位
        self.hit4_detonate_requested = False
        self.hit10_hold = 0       # 火魔斩第 2 段的 13 帧落地停顿
        self.hit10_serial = 0     # 每次火魔斩的伤害实体命名空间
        self.hit10_target = None  # 当前被抓住的怪物
        self.last_hit = ""
        self.base_power_hit = 0.0
        self.combo = 0
        self.combo_timer = 0.0
        self.attack_time = 0.0
        self.attack: Action | None = None
        self.hit_stages: set[int] = set()
        self.queued_attack = False
        self.skill_cd = 0.0
        self.invuln = 0.0
        self.hurt_timer = 0.0
        self.hurt_elapsed = 0.0
        self.dead = False
        self.hidden = False
        # 抓取类技能 hit5：被抓住期间角色隐藏，直到释放流程结束才重新显示。
        self.hidden_in_grab = False
        self.death_timer = 0.0
        self._dead_fx = False
        self.flash = 0.0
        self.attack_id = 0
        # ---- 水幻影：英雄身上的 shadow 字段只是可指挥、可换位的
        # 引用；幻影实例本身独立存在于场景中（存活 192 帧 / 冰柱 50 / 加血 50）。
        self.shadow: Shadow | None = None        # 引用（指挥或换位后置为 None）
        self.shadows: list[Shadow] = []          # 场上全部存活的幻影实例
        self._tang_release_pending = False       # 松开 J 后：下一帧恢复身体动画播放
        self._charge_bar_alpha = 0.0             # 蓄力条 0.5 秒淡入/淡出
        self._charge_bar_flicker = 0             # 满槽颜色按 24Hz 逐帧交替闪烁
        # 怒气槽取值是 0..100 的整数，必须保持整数语义：
        # 这样 99+1 恰好到 100，而 99+2 会被拒绝（上限判断是 ``<= 100``）；
        # 两个浮点累加器分别实现每秒 1 点的自然流失，
        # 以及 GXP 开启期间额外每秒 6 点的流失。
        self.gxp = 0
        # 怒气流失与幻影计数都是 24 Hz 的逐帧计数器；
        # 这里保留整数计数加一个帧累加器，
        # 让 60 Hz 的调用方仍按 24 帧/秒的节奏推进。
        self.gxp_tick_acc = 0.0
        self.gxp_shadow_count = 0
        self.gxp_natural_count = 0
        self.gxp_afterimages: list[GxpAfterimage] = []
        self.gxp_glow_blur = 15
        self.gxp_glow_alpha = 1.0
        self.gxp_glow_started = False
        # 怒气特效冻结的 1 秒内为 False：此时不推进特效动画，
        # 所以光晕要等冻结结束后才开始实时播放。
        self.gxp_visual_active = True
        self._gxp_glow_cache: dict[tuple, pygame.Surface] = {}
        self._round_mc_cache: dict[tuple, tuple] = {}
        self.is_gxp = False
        self.j_hold = 0.0
        self.water_stage = 0
        self.miss_timer = 0.0
        self.drop_timer = 0.0
        self.air_walk_until_land = False
        # 怪物身上的持续状态效果：中毒与冰冻。
        self.effects: list[dict] = []
        self.frozen = False
        self.grabbed = False
        self.immune = False
        # 命中身份（名字 + attack_id）：被其它攻击者打中时用
        # (攻击者 hit_ns, attack_id, stage) 三元组去重（见 take_damage）。
        self.hit_ns = 1                  # 英雄命中命名空间 = "1"/"2"（由 World/Arena 创建时赋值）
        self.hit_ids: set[HitKey] = set()
        # 最近一次 take_damage 的拦截原因（T2 round-2 F1/F3）："" = 真实结算，
        # 其它值 = "dead" / "immune" / "grabbed" / "dedupe" / "father" / "miss"。
        # 子弹/怪物子弹据此区分"不消耗"（father/无敌/去重）与"消耗+登记"（miss）。
        self.hit_block = ""
        # ---- father / 霸体（常驻的 "father" 效果，见模块常量）----
        self.father_frames = 0           # 霸体剩余帧数
        self.father_hits = 0             # 霸体已承受的命中次数
        self.last_be_hit_ms = 0          # 上次被击中的时间戳（毫秒）
        self.has_father_effect = True    # 初始自带霸体（个别模式除外）
        self._father_glow_cache: dict[tuple, pygame.Surface] = {}

    # ------------------------------------------------------------------ 动画片段
    def _clip(self, name: str) -> Clip:
        return self.assets.clips.get(f"{self.hero}/{name}") or self.assets.clips[f"wukong/{name}"]

    @property
    def body(self) -> pygame.Rect:
        return body_rect(self.x, self.y, self.w, self.h)

    @property
    def hit_rect(self) -> pygame.Rect:
        """英雄碰撞椭圆的轴对齐包围盒（竞技场里唐僧技能打 P1/P2 时用）。"""
        return self.body

    def attack_rect(self, aoe: float = 0.0) -> pygame.Rect:
        """``player_attack_hit`` 的默认近战判定矩形（无逐帧 stick 时）。

        仅用于**没有逐帧 body.stick 数据**的动作（唐僧普攻等）。
        顶边以 root 为基准（武器框本就相对 root 定义）；底边贴地时盖到脚下，
        空中用统一的有界下探（见 :meth:`_bounded_reach`）。
        """
        if aoe:
            return pygame.Rect(round(self.x - aoe), round(self.y - self.h - 60),
                               round(aoe * 2), round(self.h + 80))
        top = round(self.root_y - 104)
        bottom = round(self._bounded_reach())
        height = max(1, bottom - top)
        if self.facing > 0:
            return pygame.Rect(round(self.x + 4), top, 96, height)
        return pygame.Rect(round(self.x - 100), top, 96, height)

    def _bounded_reach(self) -> float:
        """无逐帧 stick 时的近战框下沿（唐僧 ``attack_rect`` 兜底）。

        v2 #3 修正了 wukong 的逐帧 stick 数据后，v1 B-2 的"补大命中框"
        （``_stick_envelope``/``root+140``）不再是 wukong 的判定路径；
        这里只保留给**没有逐帧 stick 数据**的调用（唐僧普攻）。
        """
        if not self.on_ground:
            return float(self.root_y + 140.0)
        return float(self.y - 20.0)

    @property
    def stick_rect_now(self) -> pygame.Rect | None:
        """当前帧的攻击判定框（世界坐标），对应 ``hero.body.stick``。

        可击落敌方子弹的命中判据：子弹包围盒与本帧攻击框相交。
        判断时**任意**攻击动作的当帧攻击框都算数，
        与英雄放的是哪一招无关。
        逐帧动作（悟空连招/技能、唐僧普攻）用 ``frame_action.stick_box``；
        没有逐帧 stick 数据的远程/法术招（唐僧）用 :meth:`attack_rect` 兜底。
        非攻击/施法状态返回 ``None``（走路撞到子弹不算"攻击命中"）。
        """
        if self.state not in ("attack", "skill"):
            return None
        if self.using_frame_action:
            box = self.frame_action.stick_box(self.facing)
            if box is not None:
                x0, x1, y0, y1 = box
                return pygame.Rect(round(self.x + x0),
                                   round(self.y - 45 + y0),
                                   max(1, round(x1 - x0)),
                                   max(1, round(y1 - y0)))
            if self.frame_action.action is not None and self.frame_action.action.sticks:
                # 逐帧动作里该帧本来就没有判定框（本实现同样返回 None）。
                return None
        return self.attack_rect()

    @property
    def damage_owner(self) -> "Player":
        """子弹伤害/无双归属：英雄就是自己（幻影见 ``Shadow.damage_owner``）。"""
        return self

    def _skill(self, index: int) -> dict:
        return self.assets_skills[index]

    @property
    def assets_skills(self) -> list[dict]:
        from .skills import SKILLS
        return SKILLS[self.hero]["active"]

    def learned(self, index: int) -> bool:
        return bool(self.skills["active"][index])

    @property
    def hit_sound(self) -> str:
        """怪物被攻击时按攻击者职业播放的受击音效（不含死亡音效）。"""
        return "BeattackByRole1" if self.hero == "wukong" else "BeattackByRole2"

    # ------------------------------------------------------------------ 输入
    def tap_direction(self, direction: int, now: float) -> None:
        key = "right" if direction > 0 else "left"
        if self.last_dir_tap and self.last_dir_tap != direction:
            self.double_count = 0
        if now - self.last_tap[key] < 0.5:      # 500 毫秒内二次点按同方向视为双击
            self.double_count = 1
            self.running = True
        self.last_tap[key] = now
        self.last_dir_tap = direction

    @property
    def draw_offset(self) -> int:
        """从脚底换算到纸娃娃锚点（锚点在 root 上方 45 px）。

        纸娃娃动画素材导出时统一以 ``anchor_y = root + 45`` 作为锚点。
        把 root 画在 ``foot_y - collision_bottom`` 处，再加上清单里的
        锚点，悟空（44.95）与唐僧（40.8）就能得到一致的对位结果，
        无需再为每个角色单独调偏移。

        这里刻意不加 ``ground_art_delta``：它描述的是绘制草地顶部与
        碰撞顶部之间的差；角色 root 始终对齐碰撞线，
        让草叶自然盖住脚面。
        加上它会让每个角色按关卡抬高 2.5~10 px，
        反而与截图对不上。
        """
        return int(round(-self.collision_bottom + HERO_MANIFEST_ANCHOR))

    @property
    def root_y(self) -> float:
        """角色 root 原点（= 脚底线 − collision_bottom，比脚底高约 45 px）。

        渲染用的角色 y 就是 root；灵魂吸收、攻击判定等
        都以 root 为基准（T15 B-2/B-12）。
        `draw_offset = -collision_bottom + HERO_MANIFEST_ANCHOR(45)`，
        两者的 45 px 偏移量一致。
        """
        return float(self.y - self.collision_bottom)

    # ------------------------------------------------------- 帧动作
    @staticmethod
    def _key_down(keys, code: int) -> bool:
        try:
            return bool(keys[code])
        except (KeyError, IndexError, TypeError):
            return False

    @property
    def using_frame_action(self) -> bool:
        return (self.state in ("attack", "skill")
                and self.frame_action.action is not None
                and not self.frame_action.finished)

    def hit10_stage_id(self, key: str) -> int:
        """火魔斩单次伤害实体的去重键。

        这段连招会生成三个各自命名的弹体，因此每个都有独立的攻击编号；
        而英雄本身并没有为这段连招递增攻击编号。
        若直接用 ``player.attack_id``，会与上一段 hit8 的第 0 段撞号，
        导致抓取目标的伤害被静默丢掉；
        因此改为把施放序号与实体序号编码进键值。
        """
        index = {"hit10-1": 0, "hit10-3": 1, "hit10-2": 2}[key]
        return 1000 + self.hit10_serial * 3 + index

    def start_action(self, name: str, *, timer: int | None = None,
                     attack_id: bool = True, state: str = "attack",
                     mp: int = 0) -> None:
        """开始一个由数据表驱动的动作（设计文档 §13.5）。

        ``attack_id`` 表示动作开始时是否推进攻击编号；
        注意 hit9/hit10 这类动作是刻意不递增编号的。
        """
        adef = self.action_db.for_hero(self.hero).get(name)
        self.frame_action.play(name)
        self.action_clock = 0.0
        self.action_first_tick = True
        self.action_gravity = None
        self.action_locked = False
        self.state = state
        self.attack_time = 0.0
        self.hit_stages.clear()
        self.queued_attack = False
        self.round_active = False
        self.hit10_hold = 0
        self._release_hit10_target()
        self.last_hit = name
        self.vx = 0.0
        if attack_id:
            self.attack_id += 1
        self.timers = float(adef.timers if timer is None else timer)
        if mp:
            self.mp = max(0, self.mp - mp)
        self.anim.play(self._clip(name), loop=adef.loop, restart=True)
        # T15 B-2：hit1/hit2 在腾空时会把动作切换到 "jump1"（空中攻击）。
        # 本实现的 jump1 没有逐帧判定数据 ⇒ 视觉切到 jump1，
        # 判定仍沿用本段的判定盒（数据补齐前记为已知缺口）。
        self.anim.time = 0.0
        self.attack = Action(name, self._clip(name), [],
                             dash=0.0, invuln=0.0,
                             projectile=(self.hero == "tangseng"),
                             heal=(name == "hit5"))
        self.base_power_hit = self._expected_power(adef)

    def _expected_power(self, adef: ActionDef) -> float:
        spec = adef.damage or {}
        fixed = float(spec.get("fixed", 0) or 0)
        if fixed:
            return fixed * (1.5 if self.is_gxp else 1.0)
        mult = float(spec.get("mult", 1.0))
        avg = (self.atk_min + self.atk_max) / 2.0 * mult
        return avg * (1.5 if self.is_gxp else 1.0)

    def roll_damage(self, mult: float = 1.0, fixed: float = 0.0,
                    crit: bool = True) -> tuple[int, float]:
        """返回（实际伤害骰值, 用于怒气槽估算的期望伤害）。"""
        expected = (fixed if fixed else (self.atk_min + self.atk_max) / 2.0 * mult)
        raw = fixed if fixed else random.randint(self.atk_min, self.atk_max) * mult
        if crit and not fixed and random.random() < self.crit:
            raw *= 2.0
        if self.is_gxp:
            raw *= 1.5
            expected *= 1.5
        return max(1, int(raw)), expected

    def _end_action_to(self, target: str) -> None:
        self.frame_action.finished = True
        self.attack = None
        self.action_gravity = None
        self.action_locked = False
        self.round_active = False
        self.hit10_hold = 0
        self._release_hit10_target()
        if target in ("jump1", "jump2", "jump3"):
            self.state = "jump"
            self.on_ground = False
            self.air_walk_until_land = False
            self.anim.play(self._clip(target), loop=False, restart=True)
        elif target == "walk":
            self.state = "idle"
            self.air_walk_until_land = not self.on_ground
        elif target == "run":
            self.state = "idle"
            self.running = True
            self.air_walk_until_land = not self.on_ground
        else:
            self.state = "idle"
            self.air_walk_until_land = not self.on_ground
            self.anim.play(self._clip("wait"), loop=True, restart=True)

    def _choose_fallback(self, fallback: dict, keys) -> str:
        if not self.on_ground and fallback.get("sky"):
            return str(fallback["sky"])
        moving = (self._key_down(keys, pygame.K_a) or self._key_down(keys, pygame.K_d))
        if moving and fallback.get("move"):
            return str(fallback["move"])
        return str(fallback.get("static", "wait"))

    def _drain_frame_events(self, events: list[dict], world, keys) -> None:
        for e in events:
            op = str(e.get("op", ""))
            if op == "sfx":
                self.assets.play(str(e.get("name", "")), 0.55)
            elif op == "attack_id":
                self.attack_id += 1
            elif op == "speed":
                sx = float(e.get("x", 0))
                if (self.frame_action.name == "hit6"
                        and self.frame_action.frame == 1 and self.is_gxp):
                    sx *= 2.0
                self.vx = self.facing * sx * 24.0
                self.vy = float(e.get("y", 0)) * 24.0
            elif op == "speed_mul":
                self.vx *= float(e.get("x", 1.0))
            elif op == "static":
                self.vx = 0.0
            elif op == "gravity":
                self.action_gravity = float(e.get("value", 0.0))
            elif op == "input_lock":
                self.action_locked = bool(e.get("value", True))
            elif op == "spawn_fx" and world is not None:
                name = str(e.get("name", ""))
                # 帧事件里的坐标以角色 root 为原点；Player.y 是脚底，
                # root = 脚底 - collision_bottom（唐僧为 40.8，R1-07）。
                scale = e.get("scale", 1.0)
                if name == "cure":
                    # GXP 状态下直接把横向、纵向缩放都设为 1.6，
                    # 不再经过 1.26 的折算。
                    scale = (1.6, 1.6) if self.is_gxp else 1.0
                    ttl = max(float(e.get("ttl", 1.2)), 34 / 24.0)
                else:
                    ttl = float(e.get("ttl", 1.2))
                fx_x = self.x + float(e.get("dx", 0))
                fx_y = tang.root_y(self) + float(e.get("dy", 0))
                heal_at = (fx_x, fx_y)
                if name == "cure":
                    # 外层治疗特效挂在 (root_x, root_y-60)，其内嵌子特效的矩阵是
                    # (-4.5,-24.5)；GXP 时外层特效的横向、纵向缩放为 1.6，
                    # 内嵌偏移也随之放大 1.6 倍：
                    #   内嵌子特效原点 = (root_x, root_y-60) + 1.6×(-4.5,-24.5)
                    #   代入后为 (root_x-7.2, root_y-99.2)，即再向左上偏移。
                    # 治疗点仍取外层特效的位置（修复方案 B4b，
                    # GXP 缩放见 R4-01）。
                    scale_off = 1.6 if self.is_gxp else 1.0
                    fx_x -= 4.5 * scale_off
                    fx_y -= 24.5 * scale_off
                world.add_effect(name, fx_x, fx_y, self.facing,
                                 ttl=ttl, scale=scale)
                if name == "cure":
                    # 内嵌治疗子特效在起手时独立加入场景：它的第 34 帧
                    # 派发治疗事件，与英雄当前动作无关（方案 §7.7 / R1-02）。
                    world.add_tang_cure(self, heal_at[0], heal_at[1])
            elif op == "fall_check":
                if not self.on_ground and self.vy > 0:
                    self.state = "jump"
                    self.anim.play(self._clip("jump3"), loop=False, restart=True)
                    self.frame_action.finished = True
                    self.attack = None
            elif op == "fire_dash":
                self.vx = self.facing * 45.0 * 24.0
                self.invuln = max(self.invuln, 12 / 24.0)
                self.immune = True
                if world is not None:
                    world.add_effect("hit7_fire", self.x, self.y, self.facing)
            elif op == "freeze":
                self.frame_action.freeze()
            elif op == "tang_charge_check":
                # 蓄力判定：已学会第一技能、蓝量足够且 J 仍按住
                if (self.learned(0) and self.mp >= tang.CHARGE_MP
                        and self._key_down(keys, pygame.K_j)):
                    # 蓄力后的动作不另写计时，直接沿用 hit1 的剩余值（R1-08）。
                    self.start_action("hit2", timer=self.timers,
                                      attack_id=False, state="skill")
                    self.frame_action.freeze()
                    self.charge_frames = 0
                    return
            elif op == "tang_release" and world is not None:
                # 松手流程：先播放音效并扣蓝，再发射弹体。
                charged = self.charge_frames >= tang.CHARGE_FRAMES
                self.assets.play("Role2_hit2" if charged else "Role2_hit1", 0.55)
                if charged:
                    self.mp = max(0, self.mp - tang.CHARGE_MP)
                spec = {
                    "name": "Role2Bullet2" if charged else "Role2Bullet1",
                    "speed": 15 if charged else 8,
                    "distance": 600 if charged else 400,
                    "scale": [0.8, 1.0] if charged else [0.6, 1.0],
                    "action": "hit2" if charged else "hit1",
                    "mult": 0.2 if charged else 1.0,
                    "kind": "magic" if charged else "physics",
                    "kb": [20, -5] if charged else [4, -7],
                    "hit_max": 200 if charged else 1,
                    "interval": 4 if charged else 0,
                    "attack_range": 0 if charged else 60,
                }
                world.spawn_tang_bullet(self, spec)
            elif op == "spawn_bullet" and world is not None:
                if str(e.get("name")) == "hit10Flush":
                    world.hit10_flush(self)
                else:
                    spec = dict(e)
                    adef = self.frame_action.action
                    dmg = (adef.damage if adef is not None else None) or {}
                    spec.setdefault("mult", float(dmg.get("mult", 1.0)))
                    spec.setdefault("kind", str(dmg.get("kind", "magic")))
                    spec.setdefault("kb", dmg.get("kb", [20, -5]))
                    spec.setdefault("hit_max", int(dmg.get("hit_max", 1)))
                    # 只有 hit1 的数据里带 attack_range: 60。
                    spec.setdefault("attack_range",
                                    float(dmg.get("attack_range", 0) or 0))
                    world.spawn_tang_bullet(self, spec)
            elif op == "hit10_grab" and world is not None:
                # 抓取前先把速度清零，再生成第一段伤害实体。
                self.vx = 0.0
                self.vy = 0.0
                if not world.hit10_grab(self):
                    self.frame_action.goto_frame(47)
                    self._drain_frame_events(
                        self.frame_action.take_events(), world, keys)
                    return
            elif op == "hit10_land" and world is not None:
                # 落地后的第一帧进入第二段伤害并冻结身体；
                # 该段固定持续 13 帧，随后强制把身体推进到第 70 帧，
                # 第 70 帧起继续正常的收招与落地流程。
                if (self.on_ground and self.last_hit != "hit10-2"
                        and self.hit10_hold == 0):
                    world.hit10_land(self)
                    self.last_hit = "hit10-2"
                    self.vx = 0.0
                    self.vy = 0.0
                    self.hit10_hold = 13
                    self.anim.time = max(
                        0.0, (self.frame_action.frame - 1) / 24.0)
            elif op == "hit10_finish" and world is not None:
                self.vx = 0.0  # 第 70 帧转静态：水平速度清零
                if self.last_hit != "hit10-2":
                    world.hit10_land(self)
                self.hit10_hold = 0
                self.action_gravity = None
                self.action_locked = False
                self.immune = False
                target = self._choose_fallback(
                    {"sky": "jump1", "static": "wait", "move": "walk"}, keys)
                self._end_action_to(target)
                return
            elif op == "round_marker":
                self.round_active = True
            elif op == "skip_to_end":
                self.frame_action.skip_to_end()
                self._drain_frame_events(self.frame_action.take_events(), world, keys)
                return
            elif op == "tang_aoe" and world is not None:
                world.spawn_tang_aoe(self)
            elif op == "tang_heal" and world is not None:
                world.tang_heal(self)
            elif op == "end":
                target = self._choose_fallback(e.get("fallback", {}) or {}, keys)
                if e.get("zero_speed"):
                    self.vx = 0.0
                self._end_action_to(target)
                return
            elif op == "stop":
                if self.state in ("attack", "skill"):
                    self._end_action_to("wait")
                return
            # 其余操作由各技能专属的逻辑处理，
            # 例如唐僧蓄力与悟空火魔斩的后续阶段。

    def _resolve_frame_stick(self, world) -> None:
        if world is None or not self.using_frame_action:
            return
        box = self.frame_action.stick_box(self.facing)
        if box is None:
            return
        adef = self.frame_action.action
        if adef is None or adef.damage is None:
            return
        spec = adef.damage
        mult = float(spec.get("mult", 1.0))
        fixed = float(spec.get("fixed", 0) or 0)
        kind = str(spec.get("kind", "physics"))
        kb = spec.get("kb", [0, 0])
        raw, expected = self.roll_damage(mult, fixed)
        # T15 B-2：命中框统一走 `stick_rect_now`（逐帧 body.stick；上挑段
        # hit3 会与 root 兜底框取并集），与"打掉子弹"共用同一来源。
        rect = self.stick_rect_now
        if rect is None:
            return
        hit = Hit(0.0, 0.0, mult=mult, fixed=int(fixed), kind=kind,
                  kb=(float(kb[0]), float(kb[1])),
                  hit_max=int(spec.get("hit_max", 1)), stage=0)
        world.player_attack_hit(self, int(raw), hit=hit, attack_id=self.attack_id,
                                rect=rect, base_power=expected)

    def _sync_camera_speed(self) -> None:
        """把本帧的水平速度同步给摄像机控制器。

        摄像机是在角色可移动性检查之后才读取水平速度的，
        因此所有会写 ``vx`` 的分支——走路/跑步、
        帧动作（hit7 1080 px/s）、旧有技能与受击击退——都必须把速度发布出来。
        被抓或死亡时保持 :meth:`_update_body` 开头写入的 0
        （转静态会清空水平速度）。
        这样摄像机就不会读到过期的速度值。
        """
        self.camera_speed = self.vx

    def _update_frame_action(self, dt: float, keys, world) -> None:
        self.attack_time += dt
        self._sync_hit10_target()
        if self.frame_action.name == "hit7" and self.invuln > 0:
            self.immune = True
        if self.frame_action.name == "hit10":
            self.immune = True
        if self.hit10_hold > 0:
            # 火魔斩落地停滞：身体在落地帧停住不动，直到该动作第 13 帧
            # 把角色切到第 70 帧。
            self.hit10_hold -= 1
            self.vx = self.vy = 0.0
            self.anim.time = max(0.0, (self.frame_action.frame - 1) / 24.0)
            apply_physics(self, dt, world.player_walls, world.level.slopes,
                          gravity_scale=0.0)
            self._sync_hit10_target()
            if self.hit10_hold == 0:
                self.frame_action.goto_frame(70)
                self._drain_frame_events(
                    self.frame_action.take_events(), world, keys)
                if self.state in ("attack", "skill"):
                    target = self._choose_fallback(
                        {"sky": "jump1", "static": "wait", "move": "walk"},
                        keys)
                    self._end_action_to(target)
            self._sync_camera_speed()
            return
        if self.frame_action._frozen:
            if self._tang_release_pending:
                # 松手后的这一帧只恢复播放（即 body.play()）；f2 的结算脚本
                # 要等下一帧的帧事件。hit2 f1 的冻结事件已经执行过，
                # 必须丢掉，否则会在这里被重新冻结/多吃一帧。
                self._tang_release_pending = False
                self.frame_action.unfreeze()
                self.frame_action.take_events()
                self.action_first_tick = False
                apply_physics(self, dt, world.player_walls, world.level.slopes,
                              gravity_scale=2.5 if self.is_gxp else 1.0)
                self._sync_camera_speed()
                return
            self._tang_tick_charge(dt)
            lock_x, _ = self._tang_move_lock()
            if lock_x:
                self.vx = 0.0
            apply_physics(self, dt, world.player_walls, world.level.slopes,
                          gravity_scale=2.5 if self.is_gxp else 1.0)
            self._sync_camera_speed()
            return
        # 每个 24 Hz 逻辑 tick 只推进一个动作帧。第一个 tick 显示 f1 且
        # 只执行它的帧事件；下一个 tick 才前进到 f2。也就是“先停在首帧、
        # 等下一帧再前进”的推进方式。
        if self.action_first_tick:
            self.action_first_tick = False
        else:
            self.frame_action.advance()
            self.anim.time = max(0.0, (self.frame_action.frame - 1) / 24.0)
        self._drain_frame_events(self.frame_action.take_events(), world, keys)
        if self.state not in ("attack", "skill"):
            self._sync_camera_speed()
            return
        self._resolve_frame_stick(world)
        if self.frame_action.finished and self.state in ("attack", "skill"):
            self._end_action_to("wait")
            self._sync_camera_speed()
            return
        gravity_scale = 0.0 if self.action_gravity == 0.0 else (
            2.5 if self.is_gxp else 1.0)
        lock_x, lock_y = self._tang_move_lock()
        if lock_x:
            self.vx = 0.0
        if lock_y:
            self.vy = 0.0
            gravity_scale = 0.0
        apply_physics(self, dt, world.player_walls, world.level.slopes,
                      gravity_scale=gravity_scale)
        self._sync_hit10_target()
        self._sync_camera_speed()

    def _tang_move_lock(self) -> tuple[bool, bool]:
        """唐僧在动作帧期间的移动锁 → ``(lock_x, lock_y)``。

        * 施法悬停类动作（hit3 / hit5）= ``speed.x = speed.y = 0``：
          施法期间悬停、不能水平移动；
        * 地面施法类动作（hit1 / hit2 且 ``jumps==0``，
          以及 hit4）：
          → 只清 ``speed.x``，仍受重力；
        * 其余动作（wait/walk/run/jump/hurt 以及空中的 hit1/hit2）不锁。

        锁 y 时必须同时把 ``speed.y`` 归零，否则下一帧的重力会把它带回来
        （每个逻辑步的移动结算都要清一次）。
        """
        if self.hero != "tangseng" or not self.using_frame_action:
            return False, False
        name = self.frame_action.name
        if name in ("hit3", "hit5"):
            return True, True
        if name == "hit4":
            return True, False
        if name in ("hit1", "hit2") and self.on_ground:
            return True, False
        return False, False

    def frame_action_active(self) -> bool:
        return self.using_frame_action


    def jump(self) -> None:
        if self.dead or self.hurt_timer > 0 or self.grabbed or self.frozen:
            return
        if not self.can_next or self.using_frame_action:
            return
        if self.state in ("attack", "skill"):
            return
        if not self.on_ground and self.jumps >= 2:
            return
        if self.jumps < 2:
            self.vy = JUMP_SPEED * (1.5 if self.is_gxp else 1.0)
            self.on_ground = False
            self.jumps = 2 if self.jumps >= 1 else 1
            self.state = "jump"
            clip = self._clip("jump2" if self.jumps == 2 else "jump1")
            # 唐僧 jump1 的身体只有 2 帧且没有 stop()，因此会循环；
            # 而悟空的 jump1 在唯一一帧上显式调用了 stop()。
            loop = (self.hero == "tangseng" and self.jumps == 1)
            self.anim.play(clip, loop=loop, restart=True)
            self.can_next = False

    def normal_attack(self) -> None:
        if self.dead or self.hurt_timer > 0 or self.grabbed or self.frozen:
            return
        if not self.can_next:
            return
        if self.hero == "tangseng":
            if self.state in ("attack", "skill"):
                return
            self._start_tang_normal()
            return
        # 悟空：逐帧精确的五段普攻连击。
        now = self.clock
        if self.timers > 0:
            self.last_attack_clock = now
            return
        if not self.on_ground:
            self.hit_num = 0
            self.start_action("hit3", timer=15, attack_id=True)
            self.can_next = False
            self.last_attack_clock = now
            return
        if self.state in ("attack", "skill") and not self.frame_action.finished:
            # 只有 hit1..hit4 能被下一次普攻取消。
            if not (self.frame_action.name in ("hit1", "hit2", "hit3", "hit4")
                    and self.frame_action.frame / max(1, self.frame_action.action.frames) >= 0.7):
                return
        if self.running:
            if self.learned(1) and self.mp >= 20:
                self.mp -= 20
                self.running = False
                self.double_count = 0
                self.start_action("hit6", timer=0, attack_id=True, state="skill")
                self.can_next = False
                self.last_attack_clock = now
                return
            self.running = False
            self.double_count = 0
        prev = self.hit_num
        timer = 18 if prev == 4 else 15 if prev == 3 else 8
        if now - self.last_attack_clock > 1.0:
            self.hit_num = 1
        else:
            self.hit_num += 1
            if self.hit_num > 5:
                self.hit_num = 1
        name = f"hit{self.hit_num}"
        self.start_action(name, timer=timer, attack_id=True)
        self.can_next = False
        self.last_attack_clock = now

    def _start_tang_normal(self) -> None:
        # 普通攻击命中分支里只有 timers<=0 才能起手；否则这次输入被直接吞掉
        # （方案 §4.8 最后一行）。
        if self.timers > 0:
            return
        self.combo = 1
        self.combo_timer = 0.8
        self.charge_frames = 0
        self._charge_bar_alpha = 0.0
        self.start_action("hit1", timer=18, attack_id=True, state="attack")
        self.can_next = False

    def request_tang_release(self) -> None:
        """hit2 冻结期间松开 J 键：等价于 ``body.play()``（只置标记）。

        按键抬起只恢复动作播放，f2 的结算脚本要等**下一帧**的帧事件才执行
        （方案 §4.4 / R1-08）。
        """
        if not (self.frame_action.name == "hit2" and self.frame_action._frozen):
            return
        if self.state != "skill" or self.frame_action.finished:
            return
        self._tang_release_pending = True

    def release_tang_charge(self, world=None) -> None:
        """同步入口（测试/工具）：置标记并当场执行"松手后的下一帧"。

        世界/竞技场的 KEYUP 走 :meth:`request_tang_release`（真·下一帧）。
        """
        self.request_tang_release()
        if self._tang_release_pending:
            self._advance_tang_release(world)

    def _advance_tang_release(self, world=None) -> None:
        if not self._tang_release_pending:
            return
        self._tang_release_pending = False
        self.frame_action.unfreeze()
        self.action_first_tick = False
        self.frame_action.advance()
        self.anim.time = 1.0 / 24.0
        self._drain_frame_events(self.frame_action.take_events(), world, {})

    def _tang_tick_charge(self, dt: float) -> None:
        """hit2 蓄力的逐帧推进：++hit2Count，封顶蓄满所需帧数。

        这里用整数帧计数，所以每 tick 直接 +1，
        不再用浮点累加（方案 G1-2）。
        """
        if not (self.frame_action.name == "hit2" and self.frame_action._frozen):
            return
        self.charge_frames = min(30, self.charge_frames + 1)
        self.vx = 0.0

    @property
    def charge_ratio(self) -> float:
        """蓄力槽的填充比例：min(hit2Count/30, 1)。"""
        return min(1.0, self.charge_frames / 30.0)

    def _charge_bar_active(self) -> bool:
        """hit2 动作播放期间（含松手后的 f2–f9）都要画蓄力条。"""
        return (self.frame_action.name == "hit2"
                and not self.frame_action.finished
                and self.state == "skill")

    def _tick_charge_bar(self) -> None:
        """hit2 期间每个 24Hz 逻辑步推进一次蓄力条的淡入/淡出。

        * 起手后 0.5 s 内 alpha 由 0 淡入到 1（相当于 12 个逻辑步）；
        * hit2 期间 ``hit2Count`` 不回零，所以松手后 f2–f9 仍是满/部分槽；
        * hit2 结束（``hit2Count = 0``）后用 0.5 s 淡出；
        * 满槽时填充色每个逻辑步在 0x112233 / 0xAABBCC 之间切换。
        """
        if self._charge_bar_active():
            self._charge_bar_alpha = min(1.0, self._charge_bar_alpha + 1.0 / 12.0)
            if self.charge_ratio >= 1.0:
                self._charge_bar_flicker += 1
            return
        if self._charge_bar_alpha > 0.0:
            self._charge_bar_alpha = max(0.0, self._charge_bar_alpha - 1.0 / 12.0)

    def _start_combo(self, combo: int) -> None:
        self.combo = combo if 1 <= combo <= 5 else 1
        self.state = "attack"
        self.attack_time = 0.0
        self.hit_stages.clear()
        self.queued_attack = False
        self.vx = 0
        self.attack_id += 1
        clip = self._clip(f"hit{self.combo}")
        self.anim.play(clip, loop=False, restart=True)
        self.combo_timer = self.anim.clip.duration + 0.35
        kb = {1: (0.5, -3), 2: (0.5, -3), 3: (0.5, -3),
              4: (0.5, -3), 5: (25, -4)}[self.combo]
        self.attack = Action(f"hit{self.combo}", clip, [
            Hit(0.05, 0.22, 1.0, kind="physics", kb=kb,
                interval=4 / 24, hit_max=1, stage=0),
        ])
        sound = "Role1_hit1AndHit2" if self.combo <= 2 else (
            "Role1_hit3AndHit4" if self.combo <= 4 else "Role1_hit5")
        self.assets.play(sound, 0.55)

    def add_gxp(self, damage: float, base_power: float, arena: bool = False) -> int:
        """增加无双值：按伤害与基础攻击力的比例折算。

        增量 = round(伤害 / 基础攻击力 × 2)；竞技场里系数为 8。
        若加上本次增量会超过 100，则整段增量都不生效（而不是截断到 100），
        所以槽值 99 挨一次 +2 的命中后仍然停在 99。
        """
        if self.is_gxp or base_power <= 0 or self.gxp >= 100:
            return 0
        inc = int(math.floor(float(damage) / float(base_power) *
                             (8.0 if arena else 2.0) + 0.5))
        if inc <= 0:
            return 0
        if self.gxp + inc <= 100:
            self.gxp += inc
            return inc
        return 0

    def activate_gxp(self) -> str | None:
        if self.dead or self.is_gxp or self.gxp < 100:
            return None
        self.is_gxp = True
        # 变身无双：冻结/定身由 World/Arena 自己处理。
        # 幻影数量与无双损失计数故意不重置。
        self.gxp_tick_acc = 0.0
        self.gxp_glow_blur = 15
        self.gxp_glow_alpha = 1.0
        self.gxp_glow_started = False
        self.gxp_visual_active = False
        self._gxp_glow_cache.clear()
        return "无双"

    def cast_active(self, index: int) -> str | None:
        """释放第 0..4 号技能。"""
        if self.dead or self.hurt_timer > 0 or self.grabbed or self.frozen:
            return None
        if index < 0 or index >= 5 or not self.learned(index):
            return None
        skill = self.assets_skills[index]
        # ---- 悟空帧动作（hit8/hit6/hit7/hit9） ----
        if self.hero == "wukong":
            if index == 0:                      # 升龙斩 hit8 (J+W)
                if self.state in ("attack", "skill"):
                    if not (self.frame_action.name in ("hit1", "hit2", "hit3", "hit4")
                            and self.frame_action.action is not None
                            and self.frame_action.frame / max(1, self.frame_action.action.frames) >= 0.7):
                        return None
                if self.mp < 10:
                    return None
                self.start_action("hit8", timer=21, attack_id=True,
                                  state="skill", mp=10)
                if self.jumps == 1:
                    self.jumps = 2
                return str(skill["name"])
            if self.state in ("attack", "skill"):
                return None
            if index == 1:                      # 火炎突击 hit6
                if (not self.on_ground or not self.running or self.mp < 20):
                    return None
                self.start_action("hit6", timer=0, attack_id=True,
                                  state="skill", mp=20)
                self.running = False
                self.double_count = 0
                self.can_next = False
                return str(skill["name"])
            if index == 2:                      # 烈焰闪 hit7
                if self.mp < 30:
                    return None
                self.start_action("hit7", timer=12, attack_id=True,
                                  state="skill", mp=30)
                return str(skill["name"])
            if index == 3:                      # 烈焰风暴 hit9
                if self.mp < 40:
                    return None
                # hit9 起手不分配新的攻击编号，沿用上一段的编号。
                self.start_action("hit9", timer=10, attack_id=False,
                                  state="skill", mp=40)
                if self.jumps == 1:
                    self.jumps = 2
                return str(skill["name"])
            if index == 4:                      # 火魔斩 hit10
                if self.mp < 50:
                    return None
                # hit10 起手同样不分配新的攻击编号；伤害由三个实例名不同的
                # 特效弹体分别结算，所以每段要有独立的去重身份。
                self.hit10_serial += 1
                self.start_action("hit10", timer=7, attack_id=False,
                                  state="skill", mp=50)
                self.immune = True
                return str(skill["name"])
        if self.hero == "tangseng":
            return self._cast_tang(index, skill)
        mp = 0 if self.hero == "tangseng" else int(skill.get("mp", 0))
        if self.mp < mp:
            return None
        if self.state in ("attack", "skill"):
            return None
        self.mp -= mp
        self.state = "skill"
        self.combo = 0
        self.combo_timer = 0.0
        self.attack_time = 0.0
        self.hit_stages.clear()
        self.queued_attack = False
        if self.hero != "tangseng":
            self.attack_id += 1
        self.attack = None
        self.vx = 0
        action = skill.get("action", "hit1")
        clip = self._clip(action)
        # ---- 悟空 ----
        if self.hero == "wukong":
            if index == 4:                      # 火魔斩 hit10
                self.anim.play(clip, loop=False, restart=True)
                self.attack = Action(action, clip, [
                    Hit(0.10, 0.70, 1.0, kind="physics", kb=(2, -3),
                        interval=999, hit_max=100, stage=0),
                    Hit(0.80, 1.50, 3.0, kind="magic", kb=(40, -15),
                        interval=999, hit_max=100, stage=1),
                    Hit(1.60, 2.60, 1.0, kind="magic", kb=(40, 25),
                        interval=999, hit_max=100, stage=2),
                ])
                self.assets.play("Role1_hit10_1", 0.8)
            return str(skill["name"])

    # ------------------------------------------------------------- 水幻影
    def _last_shadow(self) -> Shadow | None:
        return self.shadows[-1] if self.shadows else None

    @property
    def shadow_x(self) -> float | None:
        """幻影指针：只有指针指向的幻影才能换位。"""
        return self.shadow.x if self.shadow is not None else None

    @property
    def shadow_y(self) -> float:
        shadow = self._last_shadow()
        return shadow.y if shadow is not None else 0.0

    @property
    def shadow_facing(self) -> int:
        shadow = self._last_shadow()
        return shadow.facing if shadow is not None else 1

    @property
    def shadow_frames(self) -> int:
        shadow = self._last_shadow()
        return shadow.frames if shadow is not None else 0

    @property
    def shadow_anim(self) -> Animation | None:
        shadow = self._last_shadow()
        return shadow.anim if shadow is not None else None

    @property
    def shadow_action(self) -> str:
        shadow = self._last_shadow()
        return shadow.action if shadow is not None else ""

    @property
    def shadow_action_frames(self) -> int:
        shadow = self._last_shadow()
        return shadow.action_frames if shadow is not None else 0

    @property
    def shadow_clip_name(self) -> str:
        return tang.SHADOW_CLIPS.get(self.shadow_action, "shadow_walk")

    def _destroy_shadow(self, shadow: "Shadow | None" = None) -> None:
        """销毁幻影：移除实例；若它正是指针则置 null。

        其它旧实例不受影响（它们各自独立挂在场景里）。
        """
        targets = [shadow] if shadow is not None else list(self.shadows)
        for item in targets:
            item.dead = True
            if item in self.shadows:
                self.shadows.remove(item)
            if self.shadow is item:
                self.shadow = None

    # 旧名字保留（调用点多，语义一致）
    _clear_shadow = _destroy_shadow

    def _release_hit10_target(self) -> None:
        # 逻辑目标引用只用于后续段的伤害/特效定位，不改变目标位置或速度。
        self.hit10_target = None

    def _sync_hit10_target(self) -> None:
        target = self.hit10_target
        if target is not None and (target.dead or target.removed):
            self.hit10_target = None

    def _summon_shadow(self) -> Shadow:
        clip = self.assets.fx(tang.SHADOW_CLIPS[""])
        shadow = Shadow(x=self.x, y=self.y, facing=self.facing, source=self,
                        support_y=float(self.y),
                        collision_bottom=self.collision_bottom,
                        anim=Animation(clip, loop=True))
        # 幻影是独立的角色实体，拥有自己一份攻击实例命名空间。
        if self.world is not None and hasattr(self.world, "new_hit_ns"):
            shadow.hit_ns = self.world.new_hit_ns()
        self.shadows.append(shadow)
        self.shadow = shadow
        return shadow

    def _command_shadow(self, action: str) -> None:
        """指挥**指针指向的**幻影施放技能：之后 ``shadow = null``。"""
        shadow = self.shadow
        if shadow is None or action not in ("hit3", "hit5"):
            return
        clip = self.assets.fx(tang.SHADOW_CLIPS[action])
        shadow.action = action
        shadow.action_frames = 0
        shadow.anim = Animation(clip, loop=False)
        shadow.vx = 0.0
        shadow.vy = 0.0
        self.shadow = None

    def _update_shadow(self, dt: float, world) -> None:
        for shadow in list(self.shadows):
            self._step_shadow(shadow, dt, world)

    def _step_shadow(self, shadow: Shadow, dt: float, world) -> None:
        shadow.frames += 1
        if shadow.anim is not None:
            shadow.anim.update(dt)
        if not shadow.action:
            # 水平速度按 8 px/帧；walk 状态不禁止移动，
            # 但每帧的 ``speed.y += graity`` 会让幻影下落并落地。
            shadow.vx = shadow.facing * tang.SHADOW_SPEED * 24.0
            apply_physics(shadow, dt, world.player_walls, world.level.slopes)
        else:
            # 施法期间（hit3/hit4/hit5）把 speed.x / speed.y 清零：
            # 幻影在施法高度悬停。
            shadow.vx = 0.0
            shadow.vy = 0.0
            apply_physics(shadow, dt, world.player_walls, world.level.slopes,
                          gravity_scale=0.0)
        lo, hi = world.shadow_limits()
        shadow.x = max(lo, min(hi, shadow.x))
        if shadow.action:
            shadow.action_frames += 1
            beats = tang.shadow_beats()
            if (shadow.action == "hit3"
                    and shadow.action_frames == int(beats["hit3"]["attack_frame"])):
                # 幻影的 hit3 命中结算：不播音效；26/31 帧取的是
                # 幻影自身的 GXP 状态恒为 false → 只有 6/11/16/21 四段；
                # 但缩放用的是施法者本体（英雄）的 GXP 状态。
                tang.spawn_aoe_at(world, shadow.x, tang.shadow_root_y(shadow),
                                  shadow.attack_id, source=shadow,
                                  gxp=self.is_gxp)
            elif (shadow.action == "hit5"
                  and shadow.action_frames == _SHADOW_CURE_FRAME):
                # t23：幻影天降甘露缺的是**天使治疗特效**（幻影本体那段剪影
                # 动画本身是完整的）。在施法第 16 帧（治疗帧 50 减去特效 34 帧）
                # 先放一次治疗特效，34 帧播完正好是 f50 结算治疗的时机，
                # 与本体“天使播完→治疗”的语义一致。位置与本体同式（治疗特效
                # 内嵌天使特效的偏移，无双时随外层缩放放大 1.6 倍）；朝向跟幻影。
                scale_off = 1.6 if self.is_gxp else 1.0
                world.add_effect(
                    "cure",
                    shadow.x - 4.5 * scale_off,
                    tang.shadow_root_y(shadow) - 60.0 - 24.5 * scale_off,
                    shadow.facing,
                    ttl=34 / 24.0,
                    scale=(scale_off, scale_off) if self.is_gxp else 1.0)
            elif (shadow.action == "hit5"
                  and shadow.action_frames == int(beats["hit5"]["heal_frame"])):
                # 加血动作的 f50：结算治疗并销毁幻影。
                tang.heal_at(world, shadow.x, tang.shadow_root_y(shadow),
                             shadow.source or self)
                self._destroy_shadow(shadow)
                return
            elif (shadow.action == "hit3"
                  and shadow.action_frames >= int(beats["hit3"]["frames"])):
                self._destroy_shadow(shadow)
                return
        if shadow.frames >= int(tang.SHADOW_LIFE):
            self._destroy_shadow(shadow)

    def _interrupt_frame_action(self) -> None:
        """受击/打断：动作切换为 hurt，唐僧的帧动作与蓄力/法阵状态全部终止。"""
        if self.hero != "tangseng":
            return
        self.frame_action.finished = True
        self.frame_action.unfreeze()
        self.frame_action._pending = []
        self.charge_frames = 0
        self._tang_release_pending = False
        self.round_active = False
        self.hit4_detonate_requested = False
    def _cast_tang(self, index: int, skill: dict) -> str | None:
        # 水魔爆第二段：冲刺动作存续期间（第 10–41 帧）再按 L，只引爆水柱、不再扣 MP。
        # 空中状态不满足下面的判定条件，所以引爆同样要求站在地面上；
        # 受击后身体动作已换成受伤动作 → 不能再引爆，避免受击后隔空引爆残留的水柱。
        if (index == 4 and self.on_ground and self.round_active
                and self.state == "skill" and self.hurt_timer <= 0
                and self.frame_action.name == "hit4"
                and not self.frame_action.finished):
            # 施放第二段时同步生成水柱并切到引爆动作。
            if self.world is not None:
                tang.detonate_hit4(self.world, self)
            else:
                self.hit4_detonate_requested = True
            return "水魔爆·引爆"
        if index == 0:                          # 冰龙波是按住 J 的衍生效果，不是独立按键
            return None
        if self.state in ("attack", "skill"):
            return None
        if self.hurt_timer > 0:
            return None
        if index == 1:                          # 玄冰阵（范围冰冻技能）
            if self.mp < tang.AOE_MP:
                return None
            if self.shadow is not None:         # 有幻影时让幻影同步施放
                self._command_shadow("hit3")
            # 整段施法不生成新的攻击编号，避免同一动作被重复结算
            self.start_action("hit3", timer=0, attack_id=False,
                              state="skill", mp=tang.AOE_MP)
            return str(skill["name"])
        if index == 2:                          # 水幻影（召唤与换位）
            if self.shadow is not None:
                # 换位不消耗 MP、横纵坐标一起交换、不带无敌，并播放换位音效。
                shadow = self.shadow
                self.x, self.y = shadow.x, shadow.y
                self.vx = self.vy = 0.0
                self._destroy_shadow(shadow)
                self.state = "idle"
                self.anim.play(self._clip("wait"), loop=True, restart=True)
                self.assets.play("Role2_hit6", 0.6)
                return "水幻影·换位"
            if self.mp < tang.SHADOW_MP:
                return None
            self.mp -= tang.SHADOW_MP
            self._summon_shadow()
            self.state = "idle"
            self.anim.play(self._clip("wait"), loop=True, restart=True)
            self.assets.play("Role2_hit6", 0.6)
            return "水幻影"
        if index == 3:                          # 天降甘露（治疗）
            if self.mp < tang.HEAL_MP:
                return None
            # 起手就播放音效，不等身体动作进入第一帧。
            self.assets.play("Role2_hit5", 0.55)
            if self.shadow is not None:         # 有幻影时同步施放
                # 有幻影时同一帧再播一次回复音效，两个播放叠加，听感更厚。
                self.assets.play("Role2_hit5", 0.55)
                self._command_shadow("hit5")
            self.start_action("hit5", timer=0, attack_id=False,
                              state="skill", mp=tang.HEAL_MP)
            return str(skill["name"])
        if index == 4:                          # 水魔爆（对地爆发）
            if not self.on_ground or self.mp < tang.HIT4_MP:
                return None
            self.start_action("hit4", timer=0, attack_id=True,
                              state="skill", mp=tang.HIT4_MP)
            return str(skill["name"])
        return str(skill["name"])


    # ------------------------------------------------------------------ 更新
    def update(self, dt: float, keys, world) -> None:
        """更新身体与动作状态，并按 24 Hz 推进一次 GXP 计时。

        GXP 计数放在身体更新之后，这样同一帧的快照能看到本帧动画；
        世界层播放特定大招特效（无双特效）时会整体冻结本方法。
        """
        self._update_body(dt, keys, world)
        self._update_afterimages(dt)
        self._update_gxp(dt, keys)

    def _update_body(self, dt: float, keys, world) -> None:
        now = world.time
        self.clock = now
        self.camera_speed = 0.0
        self.skill_cd = max(0.0, self.skill_cd - dt)
        self.invuln = max(0.0, self.invuln - dt)
        self.flash = max(0.0, self.flash - dt)
        if self.father_frames > 0:
            # 霸体剩余帧数每帧递减，归零即解除霸体。
            self.father_frames -= 1
        self.miss_timer = max(0.0, self.miss_timer - dt)
        self.drop_timer = max(0.0, self.drop_timer - dt)
        self.combo_timer = max(0.0, self.combo_timer - dt)
        # timers 是按帧递减的攻击冷却，按 24 帧/秒折算成秒。
        self.timers = max(0.0, self.timers - dt * 24.0)
        self.running = self.double_count == 1
        self._update_shadow(dt, world)
        self._tick_charge_bar()
        if self.combo_timer <= 0 and self.state not in ("attack", "skill"):
            self.combo = 0
        self.immune = False
        self._update_effects(dt, world)
        if self.grabbed:
            # 被怪物抓取：英雄被隐藏且无法行动，速度和受击计时都清零。
            self.vx = self.vy = 0.0
            self.hurt_timer = 0.0
            return
        if not self.dead:
            # 生命/魔法回复每 24 帧（1 秒）结算一次整数值，而不是逐帧累加，
            # 这样长时间挂机也不会因为浮点误差多回或少回。
            self.regen_count += 1
            if self.regen_count >= 24:
                self.regen_count = 0
                if self.hp_regen:
                    self.hp = min(self.max_hp, self.hp + self.hp_regen)
                if self.mp_regen:
                    self.mp = min(self.max_mp, self.mp + self.mp_regen)
        else:
            self.regen_count = 0

        if self.dead:
            self.death_timer -= dt
            self.vx *= max(0.0, 1 - dt * 6)
            apply_physics(self, dt, world.player_walls, self.level.slopes)
            self.anim.update(dt)
            return

        if self.hurt_timer > 0:
            self.hurt_timer -= dt
            self.hurt_elapsed += dt
            # 受伤动作第 7 帧起停止水平位移，受伤计时归零后切回待机。
            if self.hurt_elapsed >= 7.0 / 24.0:
                self.vx = 0.0
            apply_physics(self, dt, world.player_walls, self.level.slopes,
                          gravity_scale=2.5 if self.is_gxp else 1.0)
            self.anim.update(dt)
            if self.hurt_timer <= 0:
                self.state = "idle"
                self.combo = 0
                self.hurt_elapsed = 0.0
            self._sync_camera_speed()
            return

        if self.using_frame_action:
            self._update_frame_action(dt, keys, world)
            return

        if self.state in ("attack", "skill") and self.attack is not None:
            self.attack_time += dt
            if self.attack.dash:
                self.vx = self.facing * self.attack.dash
            else:
                self.vx = 0
            if self.attack.invuln:
                self.invuln = max(self.invuln, self.attack.invuln)
                self.immune = True
            apply_physics(self, dt, world.player_walls, self.level.slopes,
                          gravity_scale=2.5 if self.is_gxp else 1.0)
            self.anim.update(dt)
            for hit in self.attack.hits:
                if hit.stage in self.hit_stages:
                    continue
                if hit.start <= self.attack_time <= hit.end:
                    self.hit_stages.add(hit.stage)
                    self._resolve_hit(world, hit)
            if self.attack.heal and 0.20 <= self.attack_time <= 0.30:
                if 0 not in self.hit_stages:
                    self.hit_stages.add(0)
                    amount = int(self.max_mp / 8 + 8)
                    self.hp = min(self.max_hp, self.hp + amount)
                    world.add_damage_number(self.x, self.y - self.h - 10, amount, "bunum")
                    world.add_message(f"回复 {amount} 生命", 0.8)
            if self.anim.finished:
                self.attack = None
                self.state = "idle"
                if self.queued_attack:
                    self.queued_attack = False
                    if self.hero == "tangseng":
                        self._start_tang_normal()
                    else:
                        self._start_combo(self.combo + 1)
                else:
                    self.anim.play(self._clip("wait"), loop=True, restart=True)
            self._sync_camera_speed()
            return

        left = keys[pygame.K_a] and not self.frozen
        right = keys[pygame.K_d] and not self.frozen
        moving = left ^ right
        if left:
            self.facing = -1
        elif right:
            self.facing = 1
        speed = (RUN_SPEED if self.running else WALK_SPEED) * (1.5 if self.is_gxp else 1.0)
        self.vx = (-speed if left else speed if right else 0)

        if self.on_ground:
            self.jumps = 0
            if moving:
                target = self._clip("run" if self.running else "walk")
                if self.anim.clip is not target:
                    self.anim.play(target, loop=True, restart=True)
            else:
                target = self._clip("wait")
                if self.anim.clip is not target:
                    self.anim.play(target, loop=True, restart=True)
        else:
            if self.air_walk_until_land:
                target = self._clip("run" if (self.running and moving) else
                                    "walk" if moving else "wait")
                if self.anim.clip is not target:
                    self.anim.play(target, loop=True, restart=True)
            elif self.hero == "wukong" and self.jumps == 1:
                target = self._clip("jump1")
            elif (self.jumps >= 2
                  and self.anim.clip is self._clip("jump2")
                  and not self.anim.finished):
                target = self._clip("jump2")
            elif self.jumps >= 2:
                # 第二段跳播完后必须停在第三段，不能再回到第二段：
                # 否则 10 帧的空翻会在第三段之后重新播放一遍，
                # 看上去像多翻了一次。
                target = self._clip("jump3")
            elif self.vy > 0:
                target = self._clip("jump3")
            else:
                target = self._clip("jump1")
            if self.anim.clip is not target:
                self.anim.play(target, loop=False, restart=True)

        apply_physics(self, dt, world.player_walls, self.level.slopes,
                      gravity_scale=2.5 if self.is_gxp else 1.0)
        self._sync_camera_speed()
        if self.on_ground:
            self.air_walk_until_land = False
            if self.state == "jump":
                self.state = "idle"
        self.anim.update(dt)

    # ------------------------------------------------------------ GXP 帧推进
    def _gxp_is_static(self, keys) -> bool:
        """判断角色是否静止：只看左右移动键是否按下。"""
        return not (self._key_down(keys, pygame.K_a)
                    or self._key_down(keys, pygame.K_d))

    def _gxp_cur_action(self) -> str:
        if getattr(self.frame_action, "name", ""):
            return str(self.frame_action.name)
        clip = getattr(self.anim, "clip", None)
        return clip.name.rsplit("/", 1)[-1] if clip is not None else ""

    def _advance_gxp_glow(self) -> None:
        # GXP 光晕：模糊半径在 15 与 7 之间往返，
        # 两端不透明度为 1、半径为 7 时为 0.8；半径到 6 的那一帧会先减后加，看起来像在 7 处停了一拍。
        if self.gxp_glow_blur > 6 and self.gxp_glow_alpha == 1.0:
            self.gxp_glow_blur -= 1
            if self.gxp_glow_blur == 6:
                self.gxp_glow_alpha = 0.8
        if self.gxp_glow_blur < 15 and self.gxp_glow_alpha == 0.8:
            self.gxp_glow_blur += 1
            if self.gxp_glow_blur == 15:
                self.gxp_glow_alpha = 1.0

    def _update_gxp(self, dt: float, keys) -> None:
        """GXP 强化状态的衰减与特效推进，固定按 24 Hz 结算。

        自然衰减先于每 4 帧一次的 GXP 结算执行：先掉点，再判定是否还有强化。
        残影与燃烧计时跨多次开启持续累加，不会因为重新进入强化而清零。
        """
        self.gxp_tick_acc += float(dt) * 24.0
        frames = int(self.gxp_tick_acc)
        if frames <= 0:
            return
        self.gxp_tick_acc -= frames
        # 单次渲染卡顿不能一口气掉 100 点，最多按 5 帧结算。
        if frames > 5:
            frames = 5
            self.gxp_tick_acc = 0.0
        for _ in range(frames):
            # 自然衰减：每秒 1 点，只在低于 100 时生效；计数到 24 才扣 1 点并清零，
            # 因此第一次扣点发生在第 25 次调用，之后每 24 次调用扣一点。
            if self.gxp < 100:
                if self.gxp_natural_count >= 24:
                    self.gxp = max(0, self.gxp - 1)
                    self.gxp_natural_count = 0
                self.gxp_natural_count += 1
            if not self.is_gxp:
                continue
            self.gxp_shadow_count += 1
            if not self.gxp_glow_started:
                # 首次进入强化时先建立发光滤镜（半径 15），
                # 脉动从下一帧才开始，所以第一帧亮度是满的。
                self.gxp_glow_started = True
                self.gxp_visual_active = True
            else:
                self._advance_gxp_glow()
            if self.gxp_shadow_count % 4 != 0:
                continue
            # 先取当前动作快照再扣减或结束，保证残影用的是本帧姿势。
            action = self._gxp_cur_action()
            if not (self.hero == "tangseng" and action in ("hit4", "hit5")):
                self._spawn_gxp_afterimage(self._gxp_is_static(keys))
            if self.gxp > 0:
                self.gxp -= 1
            else:
                self.gxp = 0
                self.is_gxp = False
                self.gxp_visual_active = False
                self._gxp_glow_cache.clear()

    def _update_afterimages(self, dt: float) -> None:
        if not self.gxp_afterimages:
            return
        for trail in self.gxp_afterimages:
            trail.age += dt
        self.gxp_afterimages = [t for t in self.gxp_afterimages
                                if t.age < 1.0 - 1e-9]

    def _spawn_gxp_afterimage(self, static: bool) -> None:
        snap = self._snapshot_body()
        if snap is None:
            return
        x = self.x
        if static:
            x += (random.random() - 0.5) * 5.0
        y = self.y + self.draw_offset
        self.gxp_afterimages.append(GxpAfterimage(snap, x, y))
        if len(self.gxp_afterimages) > 8:
            del self.gxp_afterimages[0]

    def _resolve_hit(self, world, hit: Hit) -> None:
        if self.attack is not None and self.attack.heal:
            return
        # 基准伤害的估算：取本次命中的平均期望值，不含随机浮动、
        # 暴击与 GXP 倍率；结算时用实际伤害除以该值来累计强化点数，
        # 因此暴击一次给 4 点、普通命中给 2 点，
        # 随机浮动不会让累计点数忽高忽低。
        expected = (float(hit.fixed) if hit.fixed else
                    (self.atk_min + self.atk_max) / 2.0 * hit.mult)
        base = 0
        if hit.fixed:
            base = int(hit.fixed)
        else:
            base = random.randint(self.atk_min, self.atk_max) * hit.mult
        if self.is_gxp:
            base *= 1.5
        if random.random() < self.crit:
            base *= 2.0
            world.add_message("暴击！", 0.5)
        base = int(base)
        if self.hero == "tangseng" and (self.attack and self.attack.projectile):
            world.spawn_player_bullet(self, base, hit.kind, self.attack_id, hit.stage,
                                      self.attack.name, base_power=expected)
            return
        world.player_attack_hit(self, base, hit, self.attack_id,
                                self.attack.aoe if self.attack else 0.0,
                                base_power=expected)
        if (self.hero == "wukong" and self.attack is not None
                and self.attack.name == "hit10" and hit.stage in (1, 2)):
            self.assets.play("Role1_hit10_2" if hit.stage == 1 else "Role1_hit10_3", 0.7)

    # ------------------------------------------------------------------ 战斗
    def kill(self) -> None:
        """直接致死：用于中毒跳伤害或被抓取处决，绕过受击判定。"""
        if self.dead:
            return
        self.hp = 0
        self.dead = True
        self.death_timer = 1.2
        self.state = "dead"
        self.attack = None
        self.hit_stages.clear()
        self.vx = self.vy = 0.0
        self.assets.stop_music()
        self.assets.play("over", 0.7)

    def add_effect(self, effect: dict | None) -> None:
        """添加状态效果：从怪物攻击配置里读取中毒、结冰等效果。"""
        if not effect:
            return
        name = str(effect.get("name", ""))
        frames = int(effect.get("frames", 0) or 0)
        if not name or frames <= 0:
            return
        self.effects = [e for e in self.effects if e["name"] != name]
        self.effects.append({
            "name": name,
            "frames": frames,
            "timer": frames / 24.0,
            "power": float(effect.get("power", 0) or 0),
            "poison_acc": 0.0,
        })
        # 效果加入时立即生效：结冰当场禁止操作，不用等下一次状态更新。
        self.frozen = any(e["name"] == "ice" for e in self.effects)

    def _ice_frame_index(self) -> int | None:
        """结冰特效的帧序号：共 35 帧，第 35 帧停止且不循环播放。

        返回当前应显示的帧下标（从 0 开始）；没有结冰效果时返回 None。
        """
        for effect in self.effects:
            if effect["name"] != "ice":
                continue
            total = float(effect["frames"]) / 24.0
            elapsed = total - float(effect["timer"])
            return min(max(int(elapsed * 24.0), 0), 34)
        return None

    def _update_effects(self, dt: float, world) -> None:
        alive = []
        for effect in self.effects:
            effect["timer"] -= dt
            if effect["name"] == "poison":
                effect["poison_acc"] += dt
                while effect["poison_acc"] >= 1.0 and not self.dead:
                    effect["poison_acc"] -= 1.0
                    self.hp = max(0, self.hp - effect["power"])
                    world.add_damage_number(self.x, self.y - self.h - 8,
                                            int(effect["power"]), "pnum")
                    if self.hp <= 0:
                        self.kill()
            if effect["timer"] > 0:
                alive.append(effect)
        self.effects = alive
        self.frozen = any(e["name"] == "ice" for e in self.effects)

    def take_damage(self, raw: float, source_x: float, kind: str = "physics",
                    attack_id: int = 0, kb: tuple[float, float] = (0, 0),
                    stage: int = 0, src_ns: int = 0,
                    register: bool = True) -> int:
        """英雄受击结算：处理近距离攻击与法术攻击的伤害与命中登记。

        判定顺序：命中身份去重 → 霸体免伤 → 闪避（**闪避不登记命中**）→
        伤害计算 → 登记命中编号 → 播放受击表现（连带落地重力重置与
        连续受击计数刷新）。顺序不能调换，否则会出现闪避后仍被登记、
        或霸体期间白白消耗命中编号的问题。

        ``src_ns`` 是攻击者实例命名空间，与 ``attack_id``、``stage`` 一起
        构成一次命中的唯一身份；``src_ns=0 && attack_id=0`` 视为"无身份"
        （历史直接调用），既不去重也不登记。

        ``register=False`` 表示**强制溅射**语义：跳过去重检查、也不登记
        命中编号（远程溅射只对主目标登记，避免一次溅射把同一目标的其他
        伤害段吞掉），但霸体/无敌/闪避/伤害照常结算。

        每次调用都会写 ``self.hit_block`` 暴露拦截原因（返回值语义不变，
        0 表示未造成伤害）：``""`` = 真实结算，``"dead"`` / ``"immune"`` /
        ``"grabbed"`` / ``"dedupe"`` / ``"father"`` / ``"miss"``。子弹据此区分
        "霸体/无敌挡下（不消耗）"与"闪避（消耗并登记）"。
        """
        self.hit_block = ""
        if self.dead or self.immune or self.grabbed:
            self.hit_block = ("dead" if self.dead
                              else "immune" if self.immune else "grabbed")
            return 0
        record = register and bool(src_ns or attack_id)
        key: HitKey = (int(src_ns), int(attack_id), int(stage))
        if record and key in self.hit_ids:      # 同一攻击编号只结算一次，防止重复命中
            self.hit_block = "dedupe"
            return 0
        if self.father_frames > 0:              # 霸体期间免伤
            self.hit_block = "father"
            return 0
        if random.random() < self.miss:         # 闪避判定
            # 闪避直接返回：近战路径不登记命中编号；远程路径由子弹侧登记，
            # 这样同一发子弹的后续帧不会再次判定。
            self.hit_block = "miss"
            self.invuln = 0.2
            self.miss_timer = 0.6
            return 0
        dmg = physical_damage(raw, self.defense) if kind == "physics" else max(1, int(raw))
        self.hp = max(0, self.hp - dmg)
        # 英雄没有受击无敌帧（只有命中登记）；invuln 只驱动闪白效果。
        self.invuln = 0.08
        self.flash = 0.18
        if record:
            self.hit_ids.add(key)               # 只有成功结算才登记命中编号
        self._be_attack_doing(source_x, kb)     # 播放受击表现（内含重力重置与霸体刷新）
        if self.hp <= 0:
            self.dead = True
            self.death_timer = 1.2
            self.state = "dead"
            self.assets.stop_music()
            self.assets.play("over", 0.7)
        return dmg

    def _be_attack_doing(self, source_x: float, kb: tuple[float, float]) -> None:
        """受击表现：进入受伤动作、施加击退，并刷新重力与霸体计数。"""
        # 强化状态下的孙悟空不会被击退，也不会中断当前技能或动作；
        # 唐僧没有这层保护，受击必定进入受伤动作。
        armored = self.hero == "wukong" and self.is_gxp
        if armored:
            self.vx = self.vy = 0.0
        else:
            # 受击时当前动作切到受伤，唐僧的帧动作（蓄力冻结 / 水魔爆引导）
            # 也随之终止，避免受伤后仍在施法。
            self._interrupt_frame_action()
            self.hurt_timer = 15.0 / 24.0
            self.hurt_elapsed = 0.0
            self.state = "hurt"
            self.combo = 0
            self.attack = None
            self.hit_stages.clear()
            self.anim.play(self._clip("hurt"), loop=False, restart=True)
            direction = 1 if self.x >= source_x else -1
            if kb != (0, 0):
                self.vx = float(kb[0]) * 24.0 * direction
                self.vy = float(kb[1]) * 24.0
            else:
                self.vx = self.vy = 0.0
        self.assets.play("Role1_beAttack" if self.hero == "wukong" else "Role2_beAttack", 0.6)
        self.father_frames = 0                  # 受击瞬间清空剩余霸体帧
        self._update_father()                   # 刷新连续受击计数

    def _update_father(self) -> None:
        """刷新连续受击计数与霸体时长。

        连续 5 次受击触发 48 帧霸体；相邻两次受击间隔达到 1000 ms 时
        计数清零，重新从第 1 次开始累积。
        """
        if not self.has_father_effect:          # 只有带霸体效果的角色才统计连续受击
            return
        now = int(round(float(getattr(self.world, "time", 0.0) or 0.0) * 1000.0))
        if now - self.last_be_hit_ms >= FATHER_INTERVAL_MS:
            self.father_hits = 0
        if self.father_hits < FATHER_TOTAL:
            self.father_hits += 1
            if self.father_hits >= FATHER_TOTAL:
                self.father_frames = FATHER_FRAMES   # 第 5 次连续受击触发 48 帧霸体
                self.father_hits = 0                 # 连击计数从头累积
        self.last_be_hit_ms = now                    # 记录本次受击时间

    def recalc_stats(self) -> None:
        """重新汇总装备与被动加成，刷新各项属性。"""
        stats = hero_stats(self.hero, self.level_no)
        self.equip_bonus = bonuses(self.equipment)
        pb = passive_bonus(self.hero, self.skills)
        old_hp, old_mp = self.max_hp, self.max_mp
        self.max_hp = int(stats["max_hp"]) + pb["hp"] + self.equip_bonus["hp"]
        self.max_mp = int(stats["max_mp"]) + pb["mp"] + self.equip_bonus["mp"]
        # 装备加成变动时，当前值与上限会一起加减（换装后上限可能变小），
        # 所以这里按上限的差值同步调整当前值，
        # 而不是只把当前生命往上加。
        self.hp = max(0, min(self.max_hp, self.hp + (self.max_hp - old_hp)))
        self.mp = max(0, min(self.max_mp, self.mp + (self.max_mp - old_mp)))
        self.defense = int(stats["defense"]) + self.equip_bonus["def"]
        self.atk_min = int(stats["atk_min"]) + self.equip_bonus["atk"]
        self.atk_max = int(stats["atk_max"]) + self.equip_bonus["atk"]
        self.crit = max(0.0, min(1.0, float(self.equip_bonus.get("crit", 0)) + pb["crit"]))
        self.miss = max(0.0, min(0.6, float(self.equip_bonus.get("miss", 0))))
        self.hp_regen = int(self.equip_bonus.get("hp_regen", 0)) + pb["hp_regen"]
        self.mp_regen = int(self.equip_bonus.get("mp_regen", 0)) + pb["mp_regen"]
        self.cloth_id = appearance_id(self.equipment.get("zbfj"))
        self.weapon_id = appearance_id(self.equipment.get("zbwq"))

    def gain_exp(self, amount: int) -> bool:
        if self.dead:
            return False
        self.exp += amount
        leveled = False
        while self.exp >= self.exp_need:
            self.exp -= self.exp_need
            self.level_no += 1
            leveled = True
            self.exp_need = hero_stats(self.hero, self.level_no)["exp_need"]
        if leveled:
            self.recalc_stats()
            self.hp = self.max_hp
            self.mp = self.max_mp
        if self.on_exp_change is not None:
            self.on_exp_change(self)
        return leveled

    # ------------------------------------------------------------------ 绘制
    def draw_shadow(self, surface: pygame.Surface, camera_x: float) -> None:
        """绘制所有存活幻影的纯黑剪影。

        * 不透明度 204/205 已经烘焙进 PNG，再调 ``set_alpha(204)`` 会二次
          相乘（0.8×0.8=0.64），所以这里按原样绘制；
        * 身体部件在幻影根时间轴里各有偏移（行走 -17.6/11.65、玄冰阵
          13.2/7.75、天降甘露 -4.95/0.75），绘制原点 = 根部坐标 + 身体偏移
          （朝向翻转时取镜像），具体数值见 data/tang_fx.json 的 ``shadow.bodies``。
        """
        for shadow in self.shadows:
            if shadow.anim is None:
                continue
            clip = shadow.anim.clip
            img = shadow.anim.frame
            off_x, off_y = tang.shadow_body_offset(shadow.action)
            ax = clip.anchor_x
            if shadow.facing > 0:
                img = pygame.transform.flip(img, True, False)
                ax = clip.width - ax
            x = shadow.x - shadow.facing * off_x
            y = tang.shadow_root_y(shadow) + off_y
            surface.blit(img, (round(x - camera_x - ax),
                               round(y - clip.anchor_y)))

    def round_mc_pose(self) -> tuple[float, float, int] | None:
        """水魔爆第一段的法阵姿态：返回 (x, y, 帧序号)；超出播放窗口或受击时返回 None。"""
        if self.frame_action.name != "hit4" or not self.round_active:
            return None
        if self.state != "skill" or self.frame_action.finished:
            return None                     # 受击后已切换到受伤动作，法阵不再跟随
        if self.hurt_timer > 0 or self.dead:
            return None
        frame = self.frame_action.frame
        offset = tang.round_mc_offset(frame)
        if offset is None:
            return None
        return offset[0], offset[1], tang.round_mc_frame_index(frame)

    def round_mc_draw_origin(self) -> tuple[float, float] | None:
        """法阵的绘制原点（世界坐标）：角色根位置 + 受击第四帧的身体偏移 + 法阵自身偏移。"""
        pose = self.round_mc_pose()
        if pose is None:
            return None
        body_x, body_y = tang.round_mc_body_offset()
        return (self.x + self.facing * (abs(pose[0]) - body_x),
                tang.root_y(self) + body_y + pose[1])

    def draw_round_mc(self, surface: pygame.Surface, camera_x: float) -> None:
        """地面蓝色法阵（随身体帧滑出的贴图，纵向缩放 0.18756）。

        素材帧本身是白光片，直接贴会偏白；因此按「地面蓝色法阵 + 轻微发光」
        的思路处理：等比压扁后做蓝色乘算，并把结果缓存起来。
        """
        pose = self.round_mc_pose()
        clip = self.assets.fx("round_mc")
        if pose is None or clip is None:
            return
        offset_x, offset_y, index = pose
        index = min(index, len(clip.frames) - 1)
        key = (id(clip), index, self.facing < 0)
        cached = self._round_mc_cache.get(key)
        if cached is None:
            scale_y = tang.round_mc_scale_y()
            img = clip.frames[index]
            nw = max(1, round(img.get_width()))
            nh = max(1, round(img.get_height() * scale_y))
            img = pygame.transform.smoothscale(img, (nw, nh)).copy()
            img.fill((150, 205, 255), special_flags=pygame.BLEND_RGB_MULT)
            if self.facing >= 0:               # 朝右：水平镜像（素材默认朝左）
                img = pygame.transform.flip(img, True, False)
                ax = nw - clip.anchor_x
            else:
                ax = clip.anchor_x
            cached = (img, ax, clip.anchor_y * scale_y)
            if len(self._round_mc_cache) > 32:
                self._round_mc_cache.clear()
            self._round_mc_cache[key] = cached
        img, ax, ay = cached
        # 法阵跟随身体：绘制位置 = 角色根位置 + 身体偏移 + 法阵偏移；
        # 而引爆判定点只叠加法阵偏移，与绘制位置不同。
        origin = self.round_mc_draw_origin()
        if origin is None:
            return
        x, y = origin
        surface.blit(img, (round(x - camera_x - ax), round(y - ay)))

    def charge_bar_rect(self, camera_x: float = 0.0) -> pygame.Rect:
        """蓄力条在屏幕上的矩形：以角色 x 为中心、位于角色根位置上方 30 像素。"""
        geo = tang.charge_bar_geometry()
        width = geo["width"]
        left = self.x - width / 2.0 - camera_x
        top = tang.root_y(self) - geo["height"] / 2.0 - 30.0
        return pygame.Rect(round(left), round(top), round(width),
                           round(geo["bar_height"]))

    def draw_charge_bar(self, surface: pygame.Surface, camera_x: float) -> None:
        """头顶蓄力条：宽度取自角色根帧的包围盒，条高固定为 9。

        包围盒尺寸为 67.2 × 92.05，位置由 ``(x - w/2, y - h/2 - 30)``
        算出（根坐标）；填充**屏幕始终从左到右**：
        角色镜像时父级翻转与 ``param2 = matrix.a`` 相互抵消，
        两个朝向的填充区间因此相同；出现时用 0.5 s（12 帧）淡入，
        满槽后填充色在两色之间交替闪烁。
        """
        alpha = self._charge_bar_alpha
        if alpha <= 0.0:
            return
        active = self._charge_bar_active()
        ratio = self.charge_ratio if active else 0.0
        frame = self.charge_bar_rect(camera_x)
        layer = pygame.Surface(frame.size, pygame.SRCALPHA)
        pygame.draw.rect(layer, (255, 0, 0, round(255 * alpha)),
                         layer.get_rect(), 1)
        fill_w = max(0, round((frame.width - 2) * ratio))
        if fill_w > 0:
            if ratio >= 1.0:
                # 满槽后每一步交替一次填充色：两色分别为 0x112233 与
                # 0xAABBCC；首次满槽取 0x112233，与不满槽时的绿色不同。
                color = ((0x11, 0x22, 0x33) if self._charge_bar_flicker % 2 == 0
                         else (0xAA, 0xBB, 0xCC))
            else:
                color = (0, 255, 0)
            # 无论朝向都从左边第 1 像素开始填充：角色镜像时父级翻转与
            # 纹理矩阵的横向缩放相互抵消，两个朝向的填充区间保持一致。
            bar = pygame.Rect(1, 1, fill_w, max(0, frame.height - 2))
            pygame.draw.rect(layer, (*color, round(255 * alpha)), bar)
        surface.blit(layer, frame.topleft)

    def _draw_actor(self, surface: pygame.Surface, sx: float, sy: float,
                    alpha: int, flash: tuple[int, int, int] | None) -> None:
        paper = getattr(self.assets, "paperdoll", None)
        action = self.anim.clip.name.rsplit("/", 1)[-1] if self.anim.clip else ""
        jump2 = action == "jump2"
        # 受击第 1 段的第 3 帧与受击第 2 段的第 2 帧会覆盖装备层帧：
        # 让装备外观切到第 2 套（共 6 种变体），具体映射见
        # game/paperdoll.py 中的层级覆盖表。
        override = None
        if paper is not None and paper.ready:
            override = action_layer_overrides(action, self.anim.frame_index,
                                              self.cloth_id)
        if jump2 and paper is not None and paper.ready:
            # 二段跳时装备被放进旋转的容器里：武器位于第 3 层、在身体部件
            # （第 6 层及更深）之后，所以先画武器再画身体；衣服则盖在身体
            # 上方。
            paper.draw(surface, self.hero, action, self.anim.frame_index,
                       sx, sy, self.facing, self.cloth_id, self.weapon_id,
                       alpha=alpha, flash=flash, phase="behind", override=override)
        # 嵌套特效（受击第 3 段的玄冰阵金环）在第 1 层，位于身体之下。
        if paper is not None and paper.ready:
            paper.draw_effects(surface, self.hero, action, self.anim.frame_index,
                               sx, sy, self.facing, alpha=alpha, flash=flash)
        self.anim.draw(surface, sx, sy, self.facing, alpha=alpha, flash=flash)
        # 装备层：按逐帧的层级与矩阵叠加在身体之上（见 game/paperdoll.py）
        if paper is not None and paper.ready:
            paper.draw(surface, self.hero, action, self.anim.frame_index,
                       sx, sy, self.facing, self.cloth_id, self.weapon_id,
                       alpha=alpha, flash=flash,
                       phase="front" if jump2 else None, override=override)

    def _snapshot_body(self) -> pygame.Surface | None:
        """截取当前帧的角色与装备画面（不含 HUD、辉光与残影）。"""
        try:
            snap = pygame.Surface((360, 360), pygame.SRCALPHA)
            self._draw_actor(snap, 180.0, 180.0, 255, None)
        except (pygame.error, ValueError):
            return None
        return snap

    def _draw_gxp_afterimages(self, surface: pygame.Surface,
                              camera_x: float) -> None:
        for trail in self.gxp_afterimages:
            alpha = trail.alpha
            if alpha <= 0:
                continue
            trail.surface.set_alpha(alpha)
            surface.blit(trail.surface, (
                round(trail.x - camera_x - trail.surface.get_width() / 2),
                round(trail.y - trail.surface.get_height() / 2),
            ))

    def _draw_gxp_glow(self, surface: pygame.Surface, sx: float,
                       sy: float) -> None:
        clip = getattr(self.anim, "clip", None)
        key = (
            clip.name if clip is not None else "",
            self.anim.frame_index,
            self.facing,
            self.cloth_id,
            self.weapon_id,
            self.gxp_glow_blur,
        )
        glow = self._gxp_glow_cache.get(key)
        if glow is None:
            snap = self._snapshot_body()
            if snap is None:
                return
            sil = snap.copy()
            # 保留透明度，同时把颜色整体推到白色。
            sil.fill((255, 255, 255, 0), special_flags=pygame.BLEND_RGB_MAX)
            radius = max(1, int(round(self.gxp_glow_blur * 0.75)))
            glow = pygame.transform.gaussian_blur(sil, radius)
            if len(self._gxp_glow_cache) > 24:
                self._gxp_glow_cache.clear()
            self._gxp_glow_cache[key] = glow
        glow.set_alpha(int(255 * self.gxp_glow_alpha))
        surface.blit(glow, (
            round(sx - glow.get_width() / 2),
            round(sy - glow.get_height() / 2),
        ))

    def _draw_father_glow(self, surface: pygame.Surface, sx: float,
                          sy: float) -> None:
        """霸体辉光：沿角色轮廓向外扩散的彩色柔光。

        悟空红 / 唐僧蓝；进入 GXP 状态时转白，且优先于角色颜色。
        模糊半径在 6↔15 之间呼吸（每帧 ±1，透明度在 0.8 与 1.0 间切换），
        这里按 24 Hz 时钟取整数帧做同样的三角波。
        """
        color = ((255, 255, 255) if self.is_gxp
                 else (255, 0, 0) if self.hero == "wukong" else (0, 0, 255))
        phase = int(self.clock * 24.0) % 18
        blur = 6 + (phase if phase <= 9 else 18 - phase)
        clip = getattr(self.anim, "clip", None)
        key = (color, blur,
               clip.name if clip is not None else "",
               self.anim.frame_index, self.facing,
               self.cloth_id, self.weapon_id)
        glow = self._father_glow_cache.get(key)
        if glow is None:
            snap = self._snapshot_body()
            if snap is None:
                return
            sil = snap.copy()
            sil.fill((*color, 0), special_flags=pygame.BLEND_RGB_MAX)
            glow = pygame.transform.gaussian_blur(sil, max(1, int(round(blur * 0.75))))
            if len(self._father_glow_cache) > 24:
                self._father_glow_cache.clear()
            self._father_glow_cache[key] = glow
        glow.set_alpha(200)
        surface.blit(glow, (
            round(sx - glow.get_width() / 2),
            round(sy - glow.get_height() / 2),
        ))

    def draw(self, surface: pygame.Surface, camera_x: float) -> None:
        if self.grabbed or self.hidden or self.hidden_in_grab:
            return
        sx = self.x - camera_x
        sy = self.y + self.draw_offset
        alpha = 255
        if self.dead:
            alpha = max(0, int(255 * (self.death_timer / 1.2)))
        flash = (255, 60, 40) if self.flash > 0 else None
        self._draw_gxp_afterimages(surface, camera_x)
        if self.father_frames > 0 and not self.dead:
            self._draw_father_glow(surface, sx, sy)
        if self.is_gxp and self.gxp_visual_active and not self.dead:
            self._draw_gxp_glow(surface, sx, sy)
        # 水魔爆第一段的地面法阵属于地面层，先于人物绘制。
        self.draw_round_mc(surface, camera_x)
        self._draw_actor(surface, sx, sy, alpha, flash)
        # 幻影在英雄之后挂载到场景，因此要画在英雄之上。
        self.draw_shadow(surface, camera_x)
        self.draw_charge_bar(surface, camera_x)
        # 附加状态特效：冰冻覆盖层与中毒气泡。
        if self.frozen:
            ice = self.assets.fx("ice")
            idx = self._ice_frame_index()
            if ice is not None and idx is not None:
                # 冰冻贴图挂在**角色根位置**的 (x=-90, y=-115)，而不是纸娃娃
                # 锚点（根位置再偏移 +45）；图内原点取遮罩单元左上角 (2,2)，
                # 并随角色根矩阵的横向缩放做水平镜像：朝右时贴图翻转、
                # 本地 x=-90 映射为世界 +90，朝左时方向与偏移保持不变。
                # 这样两个朝向的冰冻区域都贴合角色实际画面。
                img = ice.frames[min(idx, len(ice.frames) - 1)]
                root_y = self.root_y
                ax, ay = ice.anchor_x, ice.anchor_y
                dx = -90.0
                if self.facing > 0:
                    img = pygame.transform.flip(img, True, False)
                    ax = ice.width - ax
                    dx = 90.0
                surface.blit(img, (round(sx + dx - ax),
                                   round(root_y - 115 - ay)))
            else:
                overlay = pygame.Surface((self.w + 16, self.h + 16), pygame.SRCALPHA)
                pygame.draw.rect(overlay, (140, 220, 255, 90),
                                 overlay.get_rect(), border_radius=8)
                surface.blit(overlay, (round(sx - self.w / 2 - 8),
                                       round(sy - self.h - 8)))
        if any(e["name"] == "poison" for e in self.effects):
            for i in range(3):
                by = sy - self.h - 14 - ((pygame.time.get_ticks() / 6 + i * 14) % 26)
                pygame.draw.circle(surface, (90, 210, 90),
                                   (round(sx - 10 + i * 10), round(by)), 4)
                pygame.draw.circle(surface, (190, 255, 160),
                                   (round(sx - 10 + i * 10), round(by)), 2)




FRAME_DT = 1.0 / 24.0


def _load_monster_defs() -> dict:
    path = DATA_DIR / "monsters.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {int(k): v for k, v in raw.items()}
    except (OSError, ValueError):
        return {}


MONSTER_DEFS: dict[int, dict] = _load_monster_defs()
# 兼容旧接口的别名（v2 之前这里是扁平的属性字典，测试与工具仍按旧名访问）。
MONSTER_STATS = MONSTER_DEFS


def _level_ids(level) -> tuple[int, int]:
    lid = getattr(level, "id", "") or ""
    try:
        stage, sub = lid.split("-")
        return int(stage), int(sub)
    except (ValueError, AttributeError):
        return 1, 1


def _apply_effect_patch(attacks: dict, patch: dict) -> None:
    for action, changes in (patch or {}).items():
        base = attacks.setdefault(action, {"power": 0.0, "kind": "physics",
                                           "kb": [2.0, -5.0], "hit_max": 1,
                                           "interval": 4})
        for key, value in changes.items():
            if key == "effect":
                base["effect"] = dict(value)
            else:
                base[key] = value


class BossHand:
    """Boss 的一只可独立移动、下砸的手。"""

    def __init__(self, boss: "Monster", side: int):
        self.boss = boss
        self.side = side
        self.offset = side * 300.0
        self._direct = 0 if side > 0 else 1      # 朝向索引：0=向左、1=向右、2=静止
        self.direction = (-1, 1, 0)[self._direct]
        self.direct_timer = 24 * random.randint(2, 5)
        self.move_timer = 24 * random.randint(2, 5)
        self.down = False
        self.down_y = 0.0
        self.down_speed = 3.0
        self.down_wait = 0
        self.y = 0.0

    def hand_rect(self) -> pygame.Rect:
        """手自身的判定框：用角色碰撞椭圆与手的像素轮廓做相交测试。

        手的贴图内容范围为 (-140,-76.5)–(140,76.5)，原点位于手心。
        """
        boss = self.boss
        hx = boss.x + self.offset
        hy = boss._root_y() + 18 + self.y
        return pygame.Rect(round(hx - 140), round(hy - 76.5), 280, 153)

    def step(self, world) -> None:
        boss = self.boss
        if self.down_wait > 0:
            self.down_wait -= 1
            if self.down_wait == 0:
                # 举起保持 24 帧后重置攻击编号，并清空本轮的接触命中记录。
                boss.attack_id += 1
                boss.contact_hits.clear()
                self.y = 0.0
                self.down_y = 0.0
                self.down_speed = 2.0
                self.move_timer = 24 * random.randint(3, 6)
            return
        if self.down:
            self.down_y += self.down_speed
            self.down_speed += 0.5
            if self.down_y >= 126:
                world.shake = max(world.shake, 0.2)
                # 落点在 y=126（手心到 root+18+126），判定用落点位置而不是上一帧
                self.y = 126.0
                player = world.player
                if not player.dead and self.hand_rect().colliderect(player.hit_rect):
                    dmg = player.take_damage(
                        400, boss.x + self.offset, "magic",
                        attack_id=boss.attack_id,
                        kb=(6 * (1 if self.offset >= 0 else -1), -8),
                        src_ns=boss.hit_ns)
                    if dmg:
                        world.add_damage_number(player.x, player.y - player.h - 8,
                                                dmg, "pnum")
                        world.add_effect("hero_hurt", player.x,
                                         player.y - player.h / 2)
                self.down = False
                self.down_y = 126.0
                self.y = 126.0
                self.down_wait = 24
            else:
                self.y = self.down_y
            return
        # 横移规则：向左每帧 -3 px、向右每帧 +3 px、静止不动；横向偏移夹在 ±400 内，越界即翻转方向。
        self.offset += self.direction * 3.0
        if self.offset < -400:
            self.offset, self.direction = -400.0, 1
            self._direct = 1
        elif self.offset > 400:
            self.offset, self.direction = 400.0, -1
            self._direct = 0
        self.direct_timer -= 1
        if self.direct_timer <= 0:
            # 朝向计时到期：索引按 0→1→2→0 循环，并重新随机 2~5 秒的间隔。
            self._direct = (self._direct + 1) % 3
            self.direction = (-1, 1, 0)[self._direct]
            self.direct_timer = 24 * random.randint(2, 5)
        self.move_timer -= 1
        if self.move_timer <= 0:
            self.down = True
            self.down_y = 0.0
            self.down_speed = 3.0


class MonsterBullet:
    """怪物发射的弹丸，也用于生成带特效的飞行物。"""

    def __init__(self, monster: "Monster", spec: dict, x: float, y: float,
                 facing: int = 1, hit_ns: int | None = None):
        self.monster = monster
        self.spec = dict(spec)
        self.x, self.y = float(x), float(y)
        self.facing = facing
        self.vx = float(spec.get("vx", 0.0)) * (facing if spec.get("facing_mul", True) else 1)
        self.vy = float(spec.get("vy", 0.0))
        self.accelerate = float(spec.get("accelerate", 0.0))
        self.max_speed = float(spec.get("max_speed", 0.0))
        self.radius = int(spec.get("radius", 18))
        life = spec.get("life", 2.0)
        # ``life=None`` 表示这种弹丸没有销毁帧事件（例如只有 1 帧、
        # 永不 destroy 的弹丸）：不做倒计时，只靠 hit_max/出屏/父对象回收。
        self.life: float | None = None if life is None else float(life)
        # 直线弹丸飞满 1000 px 后自动销毁，
        # 此时 life 只是兜底的上限。
        dist = spec.get("distance")
        self.distance: float | None = None if dist is None else float(dist)
        self.travelled = 0.0
        self.age = 0.0
        # 弹丸内的子图元（3 团火焰）使用独立时间轴：
        # 时间 = max(0, age - fade_in)，淡入期间冻结在第 1 帧。
        self.child_time = 0.0
        self.arm_delay = float(spec.get("arm_delay", 0.0))
        self.can_be_hit = bool(spec.get("can_be_hit", False))
        # 被击落后进入禁用状态，用 1 秒 alpha 淡出到透明再销毁。
        self.disabled = False
        self.fade = 1.0
        self.pre = bool(spec.get("pre", False))
        self.pre_timer = (float(self.life) if self.life is not None
                          else float(spec.get("pre_life", 0.4)))
        # 出场时 alpha 从 0 在 0.6 秒内过渡到 1：
        # 这里只影响视觉，伤害判定仍按每帧照常进行，停掉子动画
        # 只会冻结子时间轴，不会停止命中检测。
        self.fade_in = float(spec.get("fade_in", 0.0) or 0.0)
        self.fade_t = 0.0
        # 预警用的弹丸是普通动画对象而非伤害弹丸：预警阶段不结算伤害。
        self.no_hit = bool(spec.get("no_hit", False))
        self.dead = False
        attack = monster.attacks.get(spec.get("damage") or monster.action) or {}
        self.power = float(attack.get("power", 0.0))
        self.kind = attack.get("kind", "physics")
        self.kb = tuple(attack.get("kb") or (2, -5))
        self.effect = attack.get("effect")
        self.hit_max = int(spec.get("hit_max", attack.get("hit_max", 1)) or 1)
        self.interval = float(spec.get("interval", attack.get("interval", 4))) / 24.0
        self.hit_cd = 0.0
        self.hit_serial = 0
        monster._bullet_serial = int(getattr(monster, "_bullet_serial", 0)) + 1
        # 命中身份 = (hit_ns, attack_id, stage)，相当于给每颗弹丸一份实例名加攻击编号。
        # 普通子弹是独立实例，因此每颗拥有自己的命名空间；
        # Boss 的持续火焰复用本体攻击号，因此同一轮火共用一个号。
        if spec.get("shared_attack_id"):
            self.hit_ns = monster.hit_ns
            self.base_id = monster.attack_id - 1
        else:
            self.hit_ns = int(hit_ns) if hit_ns is not None else next_hit_ns()
            self.base_id = monster.attack_id
        self.clip_name = spec.get("clip", "")
        self.anim: Animation | None = None
        self._warned_missing_clip = False

    def _ensure_anim(self, assets: Assets) -> None:
        if self.anim is not None or not self.clip_name:
            return
        clip = assets.fx(self.clip_name)
        if clip is not None:
            self.anim = Animation(clip, loop=True)

    def _child_anim(self, child: dict) -> "Animation | None":
        """子图元动画（T15 C-7.1）：由 ``update()`` 写入独立的 ``child_time``。"""
        key = str(child.get("clip") or "")
        if not key:
            return None
        cache = getattr(self, "_child_anims", None)
        if cache is None:
            cache = {}
            self._child_anims = cache
        anim = cache.get(key)
        if anim is None:
            clip = self.monster.assets.fx(key)
            if clip is None:
                cache[key] = False
                return None
            anim = Animation(clip, loop=False)
            cache[key] = anim
        if anim is False:
            return None
        anim.time = min(self.child_time, 15.0 / 24.0)
        return anim

    def rect(self) -> pygame.Rect:
        """当前帧的命中/可见矩形（世界坐标）。

        ``spec["rect_frames"]`` 是逐帧表（第 n 项 = 动画第 n 帧，缺项 null 表示该帧
        没有可见内容、不判定），由探针脚本逐帧解析素材中的贴图坐标得出
        （贴图局部坐标 = 贴图原点坐标系）；没有逐帧表时退回半径为
        ``radius`` 的方形，保持其它怪物子弹的判定行为。
        """
        frames = self.spec.get("rect_frames")
        if frames:
            idx = (self.anim.frame_index + 1) if self.anim is not None else 1
            rec = frames[idx - 1] if 0 < idx <= len(frames) else None
            if not rec:
                return pygame.Rect(0, 0, 0, 0)
            ox, oy, w, h = (float(rec[0]), float(rec[1]),
                            float(rec[2]), float(rec[3]))
            if self.facing > 0:
                # 素材贴图默认朝左；朝右时以 x=0 为轴水平镜像。
                ox = -(ox + w)
            return pygame.Rect(round(self.x + ox), round(self.y + oy),
                               max(1, round(w)), max(1, round(h)))
        r = float(self.radius)
        return pygame.Rect(round(self.x - r), round(self.y - r),
                           max(1, round(2 * r)), max(1, round(2 * r)))

    def shot_down(self) -> bool:
        """把可击落的弹丸打成禁用状态。

        进入禁用状态后用 1 秒 alpha 淡出，淡出结束才销毁。返回本次是否
        首次命中（避免同一颗子弹被同一帧的两条判定重复加特效）。
        """
        if self.disabled or self.dead:
            return False
        self.disabled = True
        self.fade = 1.0
        return True

    def update(self, dt: float, world) -> None:
        if self.dead:
            return
        # ``pre`` 分支也必须加载贴图（预警动画共 24 帧），
        # 不能只在普通子弹路径加载，否则 draw() 会退化成自绘圆。
        self._ensure_anim(world.assets)
        if self.disabled:
            # 进入禁用状态后不再推进移动和命中（更新逻辑直接返回），
            # 只用 1 秒把 alpha 过渡到 0 再销毁。
            # 淡出期间不结算伤害、不移动。
            self.fade -= dt
            if self.fade <= 0:
                self.fade = 0.0
                self.dead = True
            return
        if self.pre:
            self.pre_timer -= dt
            if self.pre_timer <= 0:
                # 预警动画播到末帧时把自己移除，并触发一次范围命中；
                # 爆炸效果由 ``next`` 描述，交给新的弹丸对象继续播放。
                nxt = dict(self.spec.get("next") or {})
                child = dict(self.spec)
                child.update(nxt)
                child["pre"] = False
                child.pop("next", None)
                # 预警阶段专用的键不能继承给爆炸（pre 不结算伤害）。
                child.pop("no_hit", None)
                if "life" not in nxt:
                    child["life"] = max(0.35, float(self.spec.get("pre_life", 0.4)))
                world.spawn_monster_bullet(self.monster, child, x=self.x, y=self.y)
                self.dead = True
            return
        self._ensure_anim(world.assets)
        self.age += dt
        if self.fade_in > 0.0 and self.fade_t < self.fade_in:
            self.fade_t = min(self.fade_in, self.fade_t + dt)
        if self.life is not None:
            self.life -= dt
            if self.life <= 0:
                self.dead = True
                return
        if self.accelerate:
            sign = 1 if self.vx >= 0 else -1
            self.vx += sign * self.accelerate * dt
            if self.max_speed and abs(self.vx) > self.max_speed:
                self.vx = sign * self.max_speed
        self.x += self.vx * dt
        self.y += self.vy * dt
        if self.distance is not None:
            self.travelled += abs(self.vx) * dt
            if self.travelled >= self.distance:
                self.dead = True
                return
        if self.anim:
            self.anim.update(dt)
        # 三团子图元有**自己的**时间轴。
        # 宿主动画只有 1 帧（循环回绕），不能拿它的时间做同步；
        # 子时间轴从淡入结束（子图元开始播放）起算 15 帧，
        # 满 15 帧后由父弹丸销毁整个对象。
        children = self.spec.get("children")
        if children:
            self.child_time = max(0.0, self.age - self.fade_in)
            for child in children:
                anim = self._child_anim(child)
                if anim is not None:
                    anim.time = self.child_time
                    anim.update(0.0)
            if self.child_time >= 15.0 / 24.0:
                self.dead = True
                return
        self.hit_cd = max(0.0, self.hit_cd - dt)
        player = world.player
        if (self.age >= self.arm_delay and self.hit_cd <= 0 and not self.no_hit
                and self.hit_max > 0 and not player.dead):
            if self.rect().colliderect(player.body):
                bullet_attack_id = self.base_id + self.hit_serial + 1
                dmg = player.take_damage(
                    self.power, self.x, self.kind,
                    attack_id=bullet_attack_id, kb=self.kb,
                    src_ns=self.hit_ns)
                block = getattr(player, "hit_block", "")
                if dmg:
                    self.hit_serial += 1
                    world.add_damage_number(player.x, player.y - player.h - 8,
                                            dmg, "pnum")
                    world.add_effect("hero_hurt", player.x,
                                     player.y - player.h / 2)
                    world.assets.play("BeattackByRole1", 0.35)
                    if self.effect:
                        player.add_effect(self.effect)
                elif block == "miss":
                    # 魔法护盾格挡时只弹出 MISS 并返回命中成功，
                    # 由**子弹**侧记录命中 → miss 同样消耗一次命中次数。
                    self.hit_serial += 1
                    player.hit_ids.add((int(self.hit_ns),
                                        int(bullet_attack_id), 0))
                else:
                    # 霸体/技能无敌/该命中身份已结算：受击判定返回未命中，
                    # 因此不记录命中、不扣 hit_max、弹丸也不销毁；
                    # 持续火焰只是跳过伤害，本体继续飞行。这里同样不消耗次数，
                    # 下一帧继续判定（霸体结束后仍可命中）。
                    return
                self.hit_cd = max(self.interval, 0.05)
                self.hit_max -= 1
                if self.hit_max <= 0:
                    self.dead = True

    def draw(self, surface: pygame.Surface, camera_x: float) -> None:
        sx = round(self.x - camera_x)
        sy = round(self.y)
        # 兜底：调用方没走 update()（工具/首帧预览）时也要先尝试加载贴图，
        # 只有在 clip 真的不存在时才落回圆（B19-F1）。
        self._ensure_anim(self.monster.assets)
        alpha = 1.0
        if self.disabled:
            alpha = max(0.0, self.fade)
        elif self.fade_in > 0.0:
            # 出场淡入：alpha 从 0 在 0.6 秒内渐变到完全不透明。
            alpha = min(1.0, self.fade_t / self.fade_in)
        children = self.spec.get("children")
        if children and self.anim is not None:
            # 宿主弹丸内含 3 个火焰子图元（各自的 ox/oy/scale/rot 不同），
            # 三份共用同一帧计数（同时开始播放）。
            # 其中两个子图元带固定旋转角（+23.44°/-25.20°），
            # 因此三团火呈"三方向散开"的效果，而不是平行平移。
            for child in children:
                child_anim = self._child_anim(child)
                if child_anim is None:
                    continue
                img = child_anim.frame
                sc = float(child.get("scale", 1.0) or 1.0)
                rot = float(child.get("rot", 0.0) or 0.0)
                base_w = max(1, int(round(img.get_width() * sc)))
                base_h = max(1, int(round(img.get_height() * sc)))
                if sc != 1.0:
                    img = pygame.transform.smoothscale(img, (base_w, base_h))
                ax = child_anim.clip.anchor_x * sc
                ay = child_anim.clip.anchor_y * sc
                ox = float(child.get("ox", 0.0))
                if self.facing > 0:
                    # 翻转朝向时整个弹丸容器一起镜像：图像、旋转角
                    # 以及子图元偏移都以 x=0 为轴对称翻转。
                    img = pygame.transform.flip(img, True, False)
                    ax = base_w - ax
                    ox = -ox
                    rot = -rot
                if abs(rot) > 1e-6:
                    # 素材中旋转角为正表示顺时针，而 pygame.rotate 以逆时针为正，故取负值。
                    img = pygame.transform.rotate(img, -rot)
                    # pygame 旋转围绕图像中心进行，因此先把子图元原点（锚点）
                    # 相对中心的偏移一起做同样的旋转，再把图贴回子图元
                    # 的目标位置。
                    dx = ax - base_w / 2.0
                    dy = ay - base_h / 2.0
                    phi = math.radians(-rot)
                    rx = dx * math.cos(phi) + dy * math.sin(phi)
                    ry = -dx * math.sin(phi) + dy * math.cos(phi)
                    ax = img.get_width() / 2.0 + rx
                    ay = img.get_height() / 2.0 + ry
                if alpha < 1.0:
                    img = img.copy()
                    img.set_alpha(max(0, min(255, int(255 * alpha))))
                surface.blit(img, (round(sx + ox - ax),
                                   round(sy + float(child.get("oy", 0.0)) - ay)))
            return
        if self.anim is not None:
            img = self.anim.frame
            ax = self.anim.clip.anchor_x
            # 朝向翻转规则：只有整体水平镜像（朝右）时才翻转图像；
            # 本实现用 facing>0 表示朝右，与这一条件等价。
            # 不能用 vx 判断方向：
            # vx==0 的定点特效会永远不镜像，而朝左时反而画在右边。
            if self.facing > 0:
                img = pygame.transform.flip(img, True, False)
                ax = self.anim.clip.width - ax
            if alpha < 1.0:
                img = img.copy()
                img.set_alpha(max(0, min(255, int(255 * alpha))))
            surface.blit(img, (round(sx - ax),
                               round(sy - self.anim.clip.anchor_y)))
            return
        if self._warned_missing_clip is False:
            self._warned_missing_clip = True
            log.warning("monster bullet clip %r missing; fallback circle drawn",
                        self.clip_name)
        pygame.draw.circle(surface, (210, 160, 60), (sx, sy), self.radius)
        pygame.draw.circle(surface, (255, 240, 180), (sx, sy), max(2, self.radius // 3))


class Monster:
    """数据驱动的怪物实现。

    根时间轴上的动画片段充当状态选择器：``action`` 指定当前片段，片段以
    24 帧/秒播放，片段内的帧事件（``actions[name]["events"]``）驱动具体
    逻辑。AI 模板由通用的智能逻辑和各个首领的专用覆盖组合而成。
    """

    # 界面基准点的横向世界坐标：画面原点固定在 (0,0)，只有场景层随镜头滚动，
    # 基准点本身被放在横向 约 470 像素处。首领的屏幕内判定需要
    # 先做本地坐标到全局坐标的换算，再减去这个偏移才是屏幕横坐标；若漏减，
    # 所有首领的入场与出场边界都会整体偏移 470 像素。
    POINTMC_X = 470.0

    def __init__(self, enemy_type: int, x: float, ground_y: float, assets: Assets,
                 level: Level | None = None, stop_idx: int = -1,
                 player_level: int | None = None):
        self.assets = assets
        self.type = int(enemy_type)
        self.level = level
        self.stop_idx = stop_idx
        base = MONSTER_DEFS.get(self.type) or MONSTER_DEFS.get(1) or {}
        self.data = json.loads(json.dumps(base)) if base else {}
        if not self.data:
            self.data = {"id": self.type, "name": f"怪物{self.type}", "hp": 50,
                         "def": 0, "exp": 5, "gxp": 1, "speed": 72.0, "see": 300.0,
                         "range": 150.0, "attback": 0.5, "is_boss": False,
                         "probability": 0.15, "fallList": [], "attacks": {},
                         "actions": {"wait": {"frames": 1, "loop": True,
                                              "events": [], "sticks": []}},
                         "ai": {"type": "melee"}, "bullets": {}, "specials": []}
        self.stats = self.data          # 兼容旧接口约定：掉落表、掉落概率等字段直接挂在 stats 上
        self.name = self.data.get("name") or f"怪物{self.type}"
        self.attacks: dict = self.data.get("attacks") or {}
        self.actions: dict = self.data.get("actions") or {
            "wait": {"frames": 1, "loop": True, "events": [], "sticks": []}}
        self.behavior: dict = self.data.get("ai") or {"type": "melee"}
        self.specials = set(self.data.get("specials") or [])
        self.bullet_specs: dict = self.data.get("bullets") or {}
        self.hitbox = self.data.get("hitbox") or {"w": 40, "h": 60, "ox": 0, "oy": 0}

        hp = int(self.data.get("hp", 50))
        defense = int(self.data.get("def", 0))
        exp = int(self.data.get("exp", 0))
        gxp = int(self.data.get("gxp", 0))
        is_boss = bool(self.data.get("is_boss"))
        attback = float(self.data.get("attback", 0.5))
        fall_list = list(self.data.get("fallList") or [])
        probability = float(self.data.get("probability", 0.15))
        scale = self.data.get("level_scale")
        if scale:
            kind = scale.get("kind")
            if kind == "level_is":
                same = (level is not None and
                        getattr(level, "id", "") == scale.get("level"))
                variant = scale.get("true" if same else "false") or {}
                hp = int(variant.get("hp", hp))
                exp = int(variant.get("exp", exp))
                gxp = int(variant.get("gxp", gxp))
                is_boss = bool(variant.get("is_boss", is_boss))
                if not variant.get("falls", True):
                    fall_list = []
            elif kind == "team_level":
                high = int(player_level or 1) > int(scale.get("threshold", 20))
                variant = scale.get("true" if high else "false") or {}
                hp = int(variant.get("hp", hp))
                defense = int(variant.get("def", defense))
                exp = int(variant.get("exp", exp))
                gxp = int(variant.get("gxp", gxp))
                attback = float(variant.get("attback", attback))
                _apply_effect_patch(self.attacks, variant.get("attacks") or {})
            elif kind == "total_stage":
                stage, sub = _level_ids(level)
                total = (stage - 1) * 3 + sub if stage < 4 else 10
                try:
                    hp = int(eval(scale.get("hp", "100"),
                                  {"__builtins__": {}}, {"T": total}))
                except Exception:
                    hp = int(self.data.get("hp", 100))

        self.is_boss = is_boss
        self.level_boss = bool(level is not None and
                               getattr(level, "boss_enemy_type", None) == self.type)
        self.chain_spawned = False
        # 召唤者被销毁时，先销毁它召唤出来的从属怪物。
        self.summoner: "Monster | None" = None
        self.max_hp = self.hp = max(1, hp)
        self.defense = defense
        self.exp = exp
        self.gxp = gxp
        self.probability = probability
        self.fallList = fall_list
        self.speed = float(self.data.get("speed", 0.0))
        self.see = float(self.data.get("see", 300.0))
        self.range = float(self.data.get("range", 150.0))
        self.attback = attback
        self.is_fly = bool(self.data.get("is_fly")) or self.behavior.get("type") in (
            "flying", "flying_dive")

        self.colipse_bottom = float(self.data.get("colipse_bottom", 0.0) or 0.0)
        self.x, self.y = float(x), float(ground_y)
        self.support_y = float(ground_y)
        self.support_wall = None
        self.vx = self.vy = 0.0
        self.on_ground = False
        self.facing = -1
        self.w = max(24, int(self.hitbox.get("w", 40)))
        self.h = max(24, int(self.hitbox.get("h", 60)))
        self.drop_timer = 0.0
        self.state = "idle"
        self.action = "wait"
        self.action_spec = self.actions.get("wait")
        self.frame = 1
        self.tick = 0
        # 巡逻计时：一个 0~240 的帧计数器配合 72/96 帧的行走窗口。``walk_dir``
        # 记录当前行走方向（0 表示停下），必须跨帧保留，因为每一帧的横向速度
        # 都是从这个方向重算出来的。若每帧都重新随机方向，怪物就会在原地左右
        # 抖动，走不出一个完整的 72/96 帧窗口。
        self.count = 0
        # 行走窗口长度默认 72 帧；随机到 96 帧时走得更远，形成步幅差异。
        self.stop_num = 72
        self.walk_dir = 0
        self.attack_id = 1
        self.contact_hits: set[int] = set()
        # 命中身份命名空间：World 在生成怪物时会用 ``world.new_hit_ns()``
        # 覆盖；直接构造（测试/工具）时则退回全局兜底计数器。
        self.hit_ns: int = next_hit_ns()
        self.hit_ids: set[HitKey] = set()
        self.cd = {1: 0, 2: 0, 3: 0, 4: 0}
        self.target = None
        self.bcount = 0
        self.was_hit = False
        self.back_time = 0
        self.invuln_frames = 0
        self.skip_frame = False
        self.hurt_frames = 0
        self.dead = False
        self.death_timer = 0.0
        self.death_duration = 1.2
        self.death_notified = False
        self._death_dropped = False
        self._death_destroyed = False
        self._boss_dead_fired = False
        self.removed = False
        self.hp_slip = 0.0
        self.lifetime = 0.0
        self.pending_death = False
        self.acc = 0.0
        self._world = None
        self._bullet_serial = 0
        self.frozen = False
        self.grabbed = False
        self._idle_variant = "wait"
        self._walk_variant = "walk"
        self._last_idle = ""

        self.patrol_dir = -1
        self.patrol_timer = random.randint(36, 90)
        self.patrol_stop = 0
        # 飞行相关状态
        self.hover = 90.0
        # 凤凰形态（9 号怪物）
        self.floor_timer = 24 * random.randint(10, 20)
        self.fly_timer = 0
        self.egg = False
        self.egg_timer = 0
        self.egg_hits = 0
        self.used_egg = False
        # 19 号怪物的阶段切换
        self.summoned = False
        # 22 号怪物的冲锋
        self.charge_dir = -1
        self.charge_timer = 0
        self.charge_invuln = False
        self.charge_run = 0
        self.charge_returning = False
        self._charge_last_x = self.x
        # 23 号怪物的抓取
        self.grab_target = None
        self.grab_ticks = 0
        # 24 号怪物
        self.heart_alpha = 0.0
        self.heart_phase = "hide"
        self.heart_timer = random.randint(120, 216)
        # 24 号怪物的心脏：预先画进身体图里的心脏在绘制时要裁掉，改用独立的
        # 23 帧心脏特效层（相对根节点偏移 (17.5,116.1)，帧号 = tick % 23）。
        self._heart_clip = None
        if self.type == 24:
            self._heart_clip = self.assets.fx("boss24_heart")
        self.fire_timer = 24 * random.randint(2, 6)
        self.fire_count = 2
        self.hands: list[BossHand] = []
        if "hands" in self.specials:
            self.hands = [BossHand(self, -1), BossHand(self, 1)]
        # 18 号怪物的俯冲
        self.invisible = False
        self.dive_timer = 0
        self.dive_target = (0.0, 0.0)
        self.lunge_sign = 1

        self._select_idle_variant()
        self._play(self._idle_variant)

    # ------------------------------------------------------------------ 动作片段
    def _attack_actions(self) -> list[str]:
        """返回既有攻击数据又有对应动作片段的攻击动作名（供关卡检查工具使用）。"""
        return [name for name in self.attacks if name in self.actions]

    def _clip(self, action: str):
        clip = self.assets.clips.get(f"monster/{self.type}/{action}")
        if clip is None:
            clip = self.assets.clips.get(f"monster/{self.type}/wait")
        if clip is None:
            clip = self.assets.clips.get("monster/1/wait")
        return clip

    def _select_idle_variant(self) -> None:
        if self.type == 19:
            low = self._low_hp()
            self._idle_variant = "wait2" if low else "wait1"
            self._walk_variant = "walk2" if low else "walk1"
        elif self.type == 9:
            self._idle_variant = "fly" if self.is_fly else "wait"
            self._walk_variant = "fly" if self.is_fly else "walk"
        else:
            self._idle_variant = "wait"
            self._walk_variant = "walk"

    def _play(self, action: str) -> None:
        if action not in self.actions:
            action = self._idle_variant if self._idle_variant in self.actions else "wait"
        if action not in self.actions:
            return
        self.action = action
        self.action_spec = self.actions[action]
        self.frame = 1
        self._fire_events(1)

    def _fire_events(self, frame: int) -> None:
        # 同一帧内的 ``end``（切换动作）要排在生成类操作之后执行：怪物身体
        # 的帧事件（例如命中特效、命中三段的前置动作）先跑，根时间轴最后才
        # 切动作。动作数据里两者落在同一帧，并固定按 [end, spawn] 导出；若照
        # 这个顺序直接执行，生成操作会落在已经切换的动作上，导致某些怪物的
        # 毒雾等弹幕一颗都生成不出来。
        pending_end: list[dict] = []
        for ev in self.action_spec.get("events") or []:
            if int(ev.get("frame", -1)) != frame:
                continue
            for op in ev.get("ops") or []:
                if op.get("op") == "end":
                    pending_end.append(op)
                else:
                    self._do_op(op)
        for op in pending_end:
            self._do_op(op)

    def _advance_clip(self) -> None:
        spec = self.action_spec
        if not spec:
            return
        frames = max(1, int(spec.get("frames", 1)))
        if self.frame < frames:
            self.frame += 1
            self._fire_events(self.frame)
            return
        if spec.get("loop"):
            self.frame = 1
            self._fire_events(1)
            return
        self._on_clip_end()

    def _on_clip_end(self) -> None:
        if self.state == "attack":
            if self.grab_target is not None:
                return
            self._set_idle()
        elif self.state == "hurt":
            self._set_idle()
        elif self.state == "dead":
            pass
        elif self.state != "egg":
            self._set_idle()

    def _set_idle(self) -> None:
        self.state = "idle"
        self.vx = 0
        self._select_idle_variant()
        if self.action != self._idle_variant:
            self._play(self._idle_variant)

    def _set_walk(self) -> None:
        self.state = "walk"
        if self.action != self._walk_variant:
            self._play(self._walk_variant)

    # ------------------------------------------------------------------ 操作指令
    def _do_op(self, op: dict) -> None:
        kind = op.get("op")
        if kind == "end":
            self._op_end(str(op.get("state", "wait")))
        elif kind == "new_attack":
            self.attack_id += 1
            self.contact_hits.clear()
        elif kind == "spawn":
            self._op_spawn(str(op.get("fn", "")))
        elif kind == "shake":
            if self._world is not None:
                self._world.shake = max(self._world.shake,
                                        int(op.get("value", 5)) / 24.0)
        elif kind == "lunge":
            speed = {(9, "hit2"): 600.0, (23, "hit2"): 360.0,
                     (18, "hit1"): 480.0}.get((self.type, self.action), 300.0)
            if (self.type, self.action) == (18, "hit1"):
                # 18 号怪物连续突进时在第 34/37/40/43/46 帧左右交替，速度方向逐次反号。
                self.lunge_sign = -self.lunge_sign
                self.vx = self.lunge_sign * speed
            else:
                self.vx = self.facing * speed
        elif kind == "stop":
            self.vx = 0
        elif kind == "grab":
            self._try_grab()
        elif kind == "grab_tick":
            self._grab_tick()
        elif kind == "drop":
            # 尸体播到死亡动作的最后一帧时才结算掉落，避免怪物刚倒下就弹出物品。
            if self._world is not None:
                self._world.drop_on_death(self)
        elif kind == "destroy":
            # 通用销毁：移除怪物实体。27 号怪物（宝箱）被击杀时还要补一次
            # 掉落结算，因为它的死亡流程不会经过常规的掉落帧。
            self._death_destroyed = True
            if (self._world is not None and self.type == 27
                    and self.hp <= 0):
                self._world.drop_on_death(self)
        elif kind == "boss_dead":
            if self._world is not None and not self._boss_dead_fired:
                self._boss_dead_fired = True
                # 首领死亡动画播放中途就会隐藏身体；这里只标记不可见，并让实体
                # 至少保留到第 13 帧的结算点，避免死亡特效到场前尸体先被移除。
                self.invisible = True
                self.death_timer = max(self.death_timer, 13 * FRAME_DT)
                self._world.spawn_boss_dead(self)

    def _op_end(self, state: str) -> None:
        if self.action == "hit5":
            if self.frame <= 28 and self.grab_target is None:
                self._try_grab()
            if self.grab_target is not None and self.frame < 130:
                return
            if self.frame >= 130:
                self._release_grab()
                self.cd[4] = 360
        if state == "fly" and not self.is_fly:
            state = "wait"
        if self.is_fly and state == "wait" and "fly" in self.actions:
            state = "fly"
        self.vx = 0
        if state in self.actions:
            self._play(state)
            self.state = ("walk" if state in ("walk", "walk1", "walk2", "fly")
                          else "idle")
        else:
            self._set_idle()

    def _op_spawn(self, fn: str) -> None:
        world = self._world
        if world is None:
            return
        low = fn.lower()
        if low == "dohit2pre":
            self._start_dive()
            return
        if low == "showhit1":
            self._show_hit1()
            return
        self._spawn_action_bullet(self.action)

    def _spawn_action_bullet(self, action: str) -> None:
        world = self._world
        spec = self.bullet_specs.get(action)
        if world is None or not spec:
            return
        world.spawn_monster_bullet(self, spec)

    # ------------------------------------------------------------- 目标选择
    def _low_hp(self) -> bool:
        return self.max_hp > 0 and self.hp / self.max_hp < 0.7

    def _acquire(self, player) -> None:
        if self.target is not None:
            if self.target.dead:
                self.target = None
            return
        if "passive_until_hit" in self.specials and not self.was_hit:
            return
        if player.dead:
            return
        # 当前锁定关卡段刷出的怪物始终把英雄当作目标：即使距离超过 300 的
        # 常规视野，也直接锁定，保证锁定区域内的怪物一定会主动应战。
        stop = getattr(self._world, "active_stop", None)
        if stop is not None and self.stop_idx == getattr(stop, "idx", None):
            self.target = player
            return
        if (abs(player.y - self.y) <= self.range
                and abs(player.x - self.x) < max(self.see, 40.0)):
            self.target = player

    def _check_can_pass(self, world) -> bool:
        """处理“禁止继续前进”与“掉头”两类关卡标记。

        怪物横坐标距“禁止继续前进”标记 30 像素以内时，取消“攻击中的怪物
        忽略掉头标记”这一豁免；当掉头标记的纵向范围覆盖怪物中心点
        （y - 高度/2）且横向距离在 30 像素以内时，让怪物掉头；若在 20 像素
        以内，则放行并延迟 AI 24 帧。

        这里只写 ``bcount``、翻转朝向并清空目标，速度由后续每帧的移动逻辑
        按新方向重算，怪物因此能立刻走出 20~30 像素的夹缝。此处绝不能把
        ``vx`` 归零或调用 ``_set_idle()``：位置一旦被钉住，标记每帧都会
        刷新 ``bcount``，AI 再也没有机会执行，朝向还会逐帧反复翻转，表现为
        贴墙原地抽搐。具体推导见“怪物空气墙抽搐修复方案”第 4、6.2 节。
        """
        level = getattr(world, "level", None)
        if level is None:
            return True
        can = True
        for m in getattr(level, "no_continue", []):
            if abs(self.x - float(m["x"])) <= 30.0:
                can = False
        my = self.y - self.h / 2.0
        for m in getattr(level, "turnbacks", []):
            if self.state == "attack" and can:
                continue
            top = float(m["y"]) - float(m["h"]) / 2.0
            bottom = float(m["y"]) + float(m["h"]) / 2.0
            if not (top < my < bottom):
                continue
            dx = float(m["x"]) - self.x            # 标记位置减去怪物位置
            if abs(dx) > 30.0:
                continue
            self.bcount = 24                       # 只延迟 AI，不限制移动
            if abs(dx) <= 20.0:
                # 标记命中后先延迟 AI，再放行：不转向，保持前进。这里还必须
                # 把正在发呆的怪物推走：只要停留在 20 像素窗口内，``bcount``
                # 每帧都会被刷新，发呆的怪物将永远等不到下一次 AI 计算，只能
                # 原地卡死。
                if self.state == "idle" and self.speed > 0:
                    self.facing = -1 if dx > 0 else 1
                    self.vx = self.facing * float(self.speed)
                    self._set_walk()
                return True
            # 迟滞处理：已经在远离标记的怪物不再翻转方向，否则它会在
            # 20~30 像素的夹层里每帧来回掉头，形成抖动循环。
            if self.state == "walk" and self.vx != 0 and self.vx * dx < 0:
                return False
            # 掉头处理：只翻转朝向并继续移动，不打断正在攻击、受击或静止的
            # 怪物，也不清空它们的动作片段和击退状态。
            if self.state in ("idle", "walk") and self.speed > 0:
                self.facing = -1 if dx > 0 else 1
                self.vx = self.facing * float(self.speed)
                if self.state != "walk" or self.action != self._walk_variant:
                    self._set_walk()
                self.target = None                 # 掉头后清空当前目标
                return False
            return False
        return True

    # ------------------------------------------------------------------- 决策
    def _think(self) -> None:
        world = self._world
        if world is None:
            return
        player = world.player
        self._acquire(player)
        kind = self.behavior.get("type", "melee")
        if kind == "static":
            self.vx = 0
            if self.state != "idle":
                self._set_idle()
            return
        if kind == "charge":
            self._think_charge(world)
            return
        if kind == "boss24":
            self.vx = 0
            return
        if kind == "phoenix":
            self._think_phoenix(world)
            return
        if kind == "boss23":
            self._think_boss23(world)
            return
        if kind == "boss26":
            self._think_boss26(world)
            return
        if kind == "flying_dive":
            self._think_dive(world)
            return
        if kind == "flying":
            self._think_flying(world)
            return
        if self.target is None:
            self._patrol()
            return
        if kind == "kiter":
            self._think_kiter(world)
            return
        if kind == "tiered":
            self._think_tiered(world)
            return
        self._think_melee(world)

    def _patrol(self) -> None:
        self.patrol_timer -= 1
        if self.patrol_timer <= 0:
            self.patrol_timer = random.randint(72, 126)
            direction = random.choice((-1, 1))
            if self._world is not None:
                screen_x = self.x - self._world.camera_x
                if screen_x < 120:
                    direction = 1
                elif screen_x > 820:
                    direction = -1
            self.patrol_dir = direction
            self.patrol_stop = 0 if random.random() < 0.5 else random.randint(12, 36)
        if self.patrol_stop > 0:
            self.patrol_stop -= 1
            self.vx = 0
            if self.state != "idle":
                self._set_idle()
            return
        self.facing = self.patrol_dir
        self.vx = self.facing * self.speed
        if self.state != "walk":
            self._set_walk()

    def _chase(self, target) -> None:
        self.facing = 1 if target.x > self.x else -1
        self.vx = self.facing * self.speed
        if self.state != "walk":
            self._set_walk()

    def _screen_x(self) -> float:
        """屏幕横坐标：把本地横坐标换算到全局后，再减去界面基准点的偏移。"""
        cam = self._world.camera_x if self._world is not None else 0.0
        return self.x - cam - self.POINTMC_X

    def _normal_walk(self) -> None:
        """普通巡逻行走：每个 241 帧周期重新选一次方向。

        周期走到 ``count == 12`` 时根据屏幕边缘挑方向，然后连续行走
        72 或 96 帧，结束时有一半概率停下，再等 ``rand*30`` 帧才把计数器
        归零。与追目标不同，这里的方向一旦选定就不会每帧从目标位置重算。
        """
        if self.count == 12:
            pct = 50
            screen_x = self._screen_x()
            if screen_x < -400.0:
                pct = 0
            elif screen_x > 400.0:
                pct = 100
            # 按百分比随机选向：随机数小于 pct 就向左，否则向右。
            self.walk_dir = -1 if random.uniform(0, 100) < pct else 1
            self.stop_num = 72 if random.uniform(0, 100) < 50 else 96
        if self.count >= self.stop_num:
            if random.uniform(0, 100) < 50:        # 50% 概率停下
                self.walk_dir = 0
            if self.count >= self.stop_num + random.random() * 30:
                self.count = 0                     # 等待结束，计数器归零
        if self.walk_dir:
            self.facing = self.walk_dir
            self.vx = self.facing * self.speed
            if self.state != "walk":
                self._set_walk()
        else:
            self.vx = 0
            if self.state != "idle":
                self._set_idle()

    def _stop(self) -> None:
        self.vx = 0
        if self.state not in ("attack", "hurt", "dead"):
            self._set_idle()

    def _try_attack(self, action: str, father: int = 0) -> bool:
        if action not in self.actions:
            return False
        if self.target is not None:
            self.facing = 1 if self.target.x > self.x else -1
        self.state = "attack"
        self.vx = 0
        self.attack_id += 1
        self.contact_hits.clear()
        if father:
            self.invuln_frames = max(self.invuln_frames, father)
        self._play(action)
        spec = self.bullet_specs.get(action)
        if spec and int(spec.get("frame", 0)) == 0:
            self._spawn_action_bullet(action)
        return True

    def _think_flying(self, world) -> None:
        target = self.target
        if target is None:
            self.vx = self.facing * self.speed * 0.6
            if self.state != "walk":
                self._set_walk()
            return
        dx = target.x - self.x
        adx, ady = abs(dx), abs(target.y - self.y)
        self.facing = 1 if dx > 0 else -1
        if self.back_time > 0:
            self.vx = -self.facing * self.speed
            self.vy = -80.0 if self.y > 150 else 0.0
            if self.state != "walk":
                self._set_walk()
            return
        if ady > self.range * 2:
            self._hover(target)
            return
        if adx > self.range:
            self._chase(target)
            self._hover(target)
            return
        self.vx = 0
        if self.y < target.y - self.hover:
            self.vy = min(self.vy + 12, 4 * 24)
        elif self.y > target.y - self.hover + 20:
            self.vy = max(self.vy - 12, -4 * 24)
        else:
            self.vy *= 0.8
        if self.state != "idle":
            self._set_idle()
        if self.tick % 36 == 0 and random.random() < self.attback:
            if self._try_attack("hit1"):
                self.back_time = int(self.behavior.get("back_time", 72))

    def _think_melee(self, world) -> None:
        target = self.target
        dx = target.x - self.x
        adx, ady = abs(dx), abs(target.y - self.y)
        if ady > self.range * 2:
            self._patrol()
            return
        if adx > self.range:
            self._chase(target)
            return
        self.facing = 1 if dx > 0 else -1
        self.vx = 0
        if self.tick % 36 == 0:
            if random.random() < self.attback:
                self._try_attack("hit1")
            else:
                self._stop()
        elif self.state != "idle":
            self._stop()

    def _think_tiered(self, world) -> None:
        target = self.target
        dx = target.x - self.x
        adx, ady = abs(dx), abs(target.y - self.y)
        if ady > self.range * 2:
            self._patrol()
            return
        self.facing = 1 if dx > 0 else -1
        matched = None
        for tier in self.behavior.get("tiers") or []:
            lo = float(tier.get("min_dx", 0))
            hi = float(tier.get("max_dx", 1e9))
            if lo <= adx <= hi:
                matched = tier
                break
        if matched is None or matched.get("follow"):
            self._chase(target)
            return
        skill = matched.get("skill") or {}
        slot = int(skill.get("slot", 1))
        mod = matched.get("mod")
        ready = self.cd.get(slot, 0) <= 0
        if ready and (mod is None or self.tick % int(mod) == 0):
            action = skill.get("action", "hit1")
            if skill.get("low_action") and self._low_hp():
                action = skill["low_action"]
            if self._try_attack(action, father=int(skill.get("father", 0))):
                self.cd[slot] = int(skill.get("cd", 120))
            return
        if adx > self.range:
            self._chase(target)
            return
        self.vx = 0
        mod_fb = int(matched.get("mod") or 36)
        if self.tick % mod_fb == 0 and random.random() < self.attback:
            fallback = self.behavior.get("fallback", "hit1")
            if self._low_hp() and f"{fallback}-2" in self.actions:
                fallback = f"{fallback}-2"
            self._try_attack(fallback)
        elif self.state != "idle":
            self._stop()

    def _think_kiter(self, world) -> None:
        target = self.target
        dx = target.x - self.x
        adx = abs(dx)
        self.facing = 1 if dx > 0 else -1
        retreat = float(self.behavior.get("retreat_dx", 200))
        approach = float(self.behavior.get("approach_dx", 350))
        # 攻击目标判定：每 24 帧先掷一次攻击判定，
        # 其余帧维持与英雄的距离。
        if self.tick % int(self.behavior.get("mod", 24)) == 0:
            if random.random() < self.attback:
                if self._try_attack(self.behavior.get("attack_action", "hit1")):
                    return
        if adx <= retreat:
            self.facing = -1 if dx > 0 else 1
            self.vx = self.facing * self.speed
            if self.state != "walk":
                self._set_walk()
            return
        if adx >= approach:
            self._chase(target)
            return
        self.vx = 0
        if self.state != "idle":
            self._set_idle()

    def _think_charge(self, world) -> None:
        """冲锋技的移动决策。

        判定量取角色屏幕坐标相对画面参考点（本实现固定在 x=470）的偏移；
        偏移小于 -360 或大于 400 即到达边界，分别对应屏幕 x=110 与 x=870；
        冲锋期间每帧据此判断是否该停下或掉头。
        """
        if self.charge_invuln:
            self.charge_run += 1
            self.facing = self.charge_dir
            self.vx = self.charge_dir * self.speed
            if self.state != "walk":
                self._set_walk()
            rel_x = self.x - world.camera_x - self.POINTMC_X
            outside = rel_x < -360 or rel_x > 400
            # 贴墙兜底：位移被关卡夹取后 x 不再变化，
            # 此时屏幕坐标可能永远到不了阈值 ⇒ 按“被挡的那一侧”当作边界停冲。
            if (self.charge_run > 2 and self.speed > 0
                    and abs(self.x - self._charge_last_x) < 0.5):
                self._stop_charge(-400.0 if self.charge_dir < 0 else 400.0)
                return
            self._charge_last_x = self.x
            if self.charge_returning:
                # 从边界掉头回来时先跑回场内，再允许再次触发停冲（否则会在
                # 阈值线上被反复停下，等于卡死）。
                if not outside:
                    self.charge_returning = False
            elif outside:
                self._stop_charge(rel_x)
            return
        self.vx = 0
        if self.state != "idle":
            self._set_idle()
        self.charge_timer -= 1
        if self.charge_timer <= 0:
            self._start_charge(world)

    def _start_charge(self, world) -> None:
        # 起手换新的攻击编号、进入霸体并开启冲锋位移。
        last = int(self.behavior.get("last_charge_dir", 0) or 0)
        if last:
            # 上次停在边界后按上次的边界方向反向起跑（不再朝玩家，避免贴墙打转）
            self.charge_dir = -last
        else:
            self.charge_dir = 1 if self.x < world.player.x else -1
        rel_x = self.x - world.camera_x - self.POINTMC_X
        self.charge_invuln = True
        self.charge_run = 0
        # 从停冲状态、或本身就在界外起步时，先朝场内跑；回场后本标志清除，
        # 之后再遇到边界就能正常停冲（不会因为出生在界外而永久免停）。
        self.charge_returning = bool(last) or rel_x > 400 or rel_x < -360
        self.attack_id += 1
        self.contact_hits.clear()

    def _stop_charge(self, rel_x: float) -> None:
        # 停冲：关闭霸体与冲锋位移，速度清零。
        self.charge_invuln = False
        self.charge_run = 0
        self.vx = 0
        # 这里记录的是**边界侧**（左/右），不是移动方向：
        # 出生在屏幕外时下一次冲锋才会朝场内。贴墙兜底传入 ±400 时也要
        # 落到正确一侧，故用 >=（正常流程只传 rel_x，恰好 400 不会触发）。
        self.behavior["last_charge_dir"] = 1 if rel_x >= 400 else -1
        self.charge_timer = int(self.behavior.get("stop_frames", 72))
        self._set_idle()

    def _think_phoenix(self, world) -> None:
        if self.egg:
            return
        if self.is_fly:
            self.fly_timer -= 1
            if self.fly_timer <= 0:
                self.is_fly = False
                self.y = 150.0
                self.vy = 0
                self.floor_timer = 24 * random.randint(10, 20)
                self._select_idle_variant()
                self._set_idle()
        else:
            self.floor_timer -= 1
            if self.floor_timer <= 0:
                self.is_fly = True
                self.vy = -10 * 24
                self.invuln_frames = max(self.invuln_frames, 72)
                self.fly_timer = 24 * random.randint(10, 20)
                self._select_idle_variant()
                self._set_idle()
        target = self.target
        if target is None:
            self._patrol()
            return
        dx = target.x - self.x
        adx, ady = abs(dx), abs(target.y - self.y)
        self.facing = 1 if dx > 0 else -1
        if ady > self.range * 2:
            self._patrol()
            return
        if self.tick % 24 != 0:
            if self.is_fly:
                self._hover(target)
            elif adx > self.range:
                self._chase(target)
            elif self.state != "idle":
                self._stop()
            return
        if self.is_fly:
            # 空中阶段出手判定：
            #   距离 > 攻击距离   → 50% 概率放技能 1（空中即 hit1）并继续贴近
            #   距离 <= 150       → 只清速度，不起手
            #   150 < 距离 <= 400 → 30% 概率放技能 2（hit2），否则技能 1
            if adx > self.range:
                self._hover(target)
                if random.random() < 0.5 and self.cd[1] <= 0:
                    self._try_attack("hit1")
                    self.cd[1] = 48
            elif adx > 150:
                if self.cd[2] <= 0 and random.random() < 0.3:
                    self._try_attack("hit2")
                    self.cd[2] = 130
                elif self.cd[1] <= 0:
                    self._try_attack("hit1")
                    self.cd[1] = 48
                else:
                    self._hover(target)
            else:
                self._hover(target)
        else:
            # 地面阶段：距离 <= 150 → 直接放 hit4；
            # 150 < 距离 <= 攻击距离 → 30% 概率放 hit2；
            # 否则放 hit3；再远则继续贴近。
            if adx > self.range:
                self._chase(target)
            elif adx <= 150:
                self._try_attack("hit4")
            elif self.cd[2] <= 0 and random.random() < 0.3:
                self._try_attack("hit2")
                self.cd[2] = 130
            elif self.cd[1] <= 0:
                self._try_attack("hit3")
                self.cd[1] = 48
            elif self.state != "idle":
                self._stop()

    def _hover(self, target) -> None:
        if self.y < target.y - self.hover:
            self.vy = min(self.vy + 0.5 * 24, 4 * 24)
        elif self.y > target.y - self.hover + 20:
            self.vy = max(self.vy - 0.5 * 24, -4 * 24)
        else:
            self.vy *= 0.8
        self.vx = self.facing * self.speed

    def _think_boss23(self, world) -> None:
        target = self.target
        if target is None:
            self._patrol()
            return
        dx = target.x - self.x
        adx = abs(dx)
        self.facing = 1 if dx > 0 else -1
        if adx <= 80:
            if self.cd[4] <= 0 and self._try_attack("hit5"):
                self.cd[4] = 360
            else:
                self._stop()
            return
        if adx <= 100:
            if self.cd[3] <= 0 and self._try_attack("hit4", father=54):
                self.cd[3] = 360
            elif random.random() < 0.8:
                self._try_attack("hit1")
            else:
                self._patrol()
            return
        if adx <= 200:
            if self.cd[1] <= 0 and self.cd[2] <= 0:
                if random.random() < 0.5:
                    self._try_attack("hit2", father=60)
                    self.cd[1] = 360
                else:
                    self._try_attack("hit3", father=67)
                    self.cd[2] = 360
            elif self.cd[1] <= 0:
                self._try_attack("hit2", father=60)
                self.cd[1] = 360
            elif self.cd[2] <= 0:
                self._try_attack("hit3", father=67)
                self.cd[2] = 360
            elif random.random() < 0.8:
                self._try_attack("hit1")
            else:
                self._patrol()
            return
        self._chase(target)

    def _think_boss26(self, world) -> None:
        target = self.target
        if target is None:
            self._patrol()
            return
        dx = target.x - self.x
        adx = abs(dx)
        high = self.max_hp > 20000
        if adx <= 100:
            # 近距离：能放技能 3 就放 hit4，否则按概率放 hit1，再否则随机漫步。
            # 随机漫步会自己选方向，这里不要先按目标预置 ``facing``，
            # 否则漫步方向会被覆盖。
            if self.cd[3] <= 0 and self._try_attack("hit4", father=24):
                self.cd[3] = 300 if high else 400
            elif self.count % 36 == 0 and random.random() < self.attback:
                self._try_attack("hit1")
            else:
                self._normal_walk()
            return
        if adx <= 450:
            if self.cd[1] <= 0 and self.cd[2] <= 0:
                if random.random() < 0.5:
                    self._try_attack("hit2", father=90)
                    self.cd[1] = 550 if high else 700
                else:
                    self._try_attack("hit3", father=85)
                    self.cd[2] = 400 if high else 500
            elif self.cd[1] <= 0:
                self._try_attack("hit2", father=90)
                self.cd[1] = 550 if high else 700
            elif self.cd[2] <= 0:
                self._try_attack("hit3", father=85)
                self.cd[2] = 400 if high else 500
            elif self.count % 24 == 0 and random.random() < self.attback:
                self._try_attack("hit1")
            else:
                # 原先这里只做原地停步，现改为向目标贴近。
                self._chase(target)
            return
        self._chase(target)

    def _think_dive(self, world) -> None:
        if self.invisible:
            return
        target = self.target
        if target is None:
            self._patrol()
            return
        dx = target.x - self.x
        adx = abs(dx)
        self.facing = 1 if dx > 0 else -1
        if adx > self.range:
            self._chase(target)
            self._hover(target)
            return
        self.vx = 0
        if self.tick % 24 == 0:
            if random.random() < 0.5:
                self._try_attack("hit1")
            elif self.cd[1] <= 0:
                self._try_attack("hit2")
                self.cd[1] = 120

    # ----------------------------------------------------------- 帧操作
    def _start_dive(self) -> None:
        world = self._world
        if world is None:
            return
        self.invisible = True
        self.dive_timer = 25
        self.dive_target = (world.player.x, world.level.ground_y)
        # 先在英雄脚下放一个持续 25 帧的预警标记；
        # 标记到期后才生成真正的下落攻击弹体。
        spec = {"clip": "bullet_m18", "pre": True, "life": 25.0 / 24.0,
                "pre_life": 0.6, "radius": 30, "damage": "hit2",
                "vx": 0, "vy": 0}
        world.spawn_monster_bullet(self, spec, x=self.dive_target[0],
                                   y=self.dive_target[1] - 6)

    def _show_hit1(self) -> None:
        world = self._world
        if world is None:
            return
        player = world.player
        spec = {"clip": "bullet_m18", "life": 0.5, "radius": 60,
                "damage": "hit1-1", "vx": 0, "vy": 0}
        world.spawn_monster_bullet(self, spec, x=player.x - 100,
                                   y=player.y - 100)
        self._set_idle()

    def _grab_candidates(self) -> list:
        """抓取判定的候选英雄列表（遍历当前场景内的所有英雄）。"""
        world = self._world
        if world is None:
            return []
        players = getattr(world, "players", None)
        if not players:
            players = [getattr(world, "player", None)]
        return [p for p in players if p is not None]

    def _try_grab(self) -> None:
        """抓取判定：锁定第一个满足条件的英雄（只锁一次）。

        目标必须在 hit5 起手时锁定，整段技能内不得在两名英雄之间重选，
        否则被抓的角色会在悟空/唐僧之间横跳。
        """
        if self.grab_target is not None:
            return
        world = self._world
        if world is None:
            return
        if self.frame > 28:                       # 只在起手窗口判定
            return
        for player in self._grab_candidates():
            if player.dead or player.immune:      # 跳过死亡或处于无敌状态的英雄
                continue
            if abs(player.x - self.x) > 200:
                continue
            self.grab_target = player
            player.grabbed = True
            # hit5 是“抓住英雄”：把英雄定住并隐藏，
            # 之后每帧不再把它拖到 Boss 身上（否则玩家贴图会在两个动作间横跳）。
            player.hidden_in_grab = True
            player.vx = player.vy = 0
            player.x = self.x
            player.y = self.y - 10
            self.grab_ticks = 0
            return

    def _grab_tick(self) -> None:
        world = self._world
        if world is None or self.grab_target is None:
            return
        player = self.grab_target
        self.grab_ticks += 1
        player.hp = max(0, player.hp - 50)
        self.hp = min(self.max_hp, self.hp + 500)
        world.add_damage_number(player.x, player.y - player.h - 8, 50, "pnum")
        world.add_damage_number(self.x, self.y - self.h - 8, 500, "bunum")
        world.add_effect("hero_hurt", player.x, player.y - player.h / 2)
        if player.hp <= 0:
            player.kill()

    def _release_grab(self) -> None:
        if self.grab_target is not None:
            self.grab_target.grabbed = False
            self.grab_target.hidden_in_grab = False
            self.grab_target = None

    # ------------------------------------------------------------- 每帧更新
    def update(self, dt: float, player, world) -> None:
        self._world = world
        self.player_ref = player
        if self.removed:
            return
        if self.pending_death and not self.dead:
            self.pending_death = False
            self.die(world)
            return
        self.acc += dt
        guard = 0
        while self.acc >= FRAME_DT and guard < 12:
            self.acc -= FRAME_DT
            self._tick(world)
            guard += 1

    def _tick(self, world) -> None:
        if self.removed:
            return
        self.tick += 1
        # 所有怪物共用这个 0..240 的 AI 帧计数器；
        # 远距离的随机漫步会按该计数器分支。
        self.count += 1
        if self.count > 240:
            self.count = 0
        self.hp_slip = max(0.0, self.hp_slip - FRAME_DT)
        if self.lifetime > 0:
            self.lifetime -= FRAME_DT
            if self.lifetime <= 0:
                self.die(world)
                self.removed = True
                return
        if self.dead:
            self._advance_clip()
            self.death_timer -= FRAME_DT
            if self.death_timer <= 0:
                self.removed = True
            return
        if self.pending_death:
            self.die(world)
            return
        if self.egg:
            self._tick_egg(world)
            self._advance_clip()
            # 蛋期同样要走物理步进：蛋按重力 1.5 下落并落地
            # （进入蛋形态时已把 is_fly 置为 False）。
            self._physics(world)
            return
        # 抓取期间不再每帧把英雄拖到 Boss 身上：定身只清速度并隐藏角色，
        # 位置在抓住那一刻就定了；逐帧吸附会让角色贴图在两个动作之间
        # 反复横跳（表现为一条“横条”）。
        self._advance_clip()
        if self.skip_frame:
            self.skip_frame = False
            return
        self._adjust_speed()
        self.invuln_frames = max(0, self.invuln_frames - 1)
        for slot in self.cd:
            self.cd[slot] = max(0, self.cd[slot] - 1)
        if self.back_time > 0:
            self.back_time -= 1
        if self.type == 24:
            self._tick_boss24(world)
        if self.type == 19:
            self._tick_phase(world)
        if self.type == 18 and self.invisible:
            self.dive_timer -= 1
            if self.dive_timer <= 0:
                self.invisible = False
        self._select_idle_variant()
        self.bcount = max(0, self.bcount - 1)
        can_pass = self._check_can_pass(world)
        if self.state in ("idle", "walk"):
            if can_pass and self.bcount <= 0:
                self._think()
        elif self.state == "attack":
            if self.type == 18 and self.action == "hit1":
                self._check_dive_hit1(world)
        self._contact_damage(world)
        self._physics(world)
        if self.type == 24:
            for hand in self.hands:
                hand.step(world)

    def _tick_phase(self, world) -> None:
        if not self.summoned and self._low_hp():
            self.summoned = True
            # 低血量时召唤一只 18 号小怪，生成位置与本 Boss 相同。
            world.spawn_minion(18, self.x, self._root_y(), summoner=self)

    def _tick_egg(self, world) -> None:
        self.egg_timer -= 1
        # 蛋期计时每帧 +1：每 24 帧喷一次火，计满 168 帧时
        # 这一帧既放最后一团火，也立刻孵化复活。
        if self.egg_timer % 24 == 0:
            spec = self.bullet_specs.get("egg")
            if spec:
                world.spawn_monster_bullet(self, spec)
        if self.egg_timer <= 0:
            self.egg = False
            # 复活：血量回满，并复位蛋期受击计数。
            self.egg_hits = 0
            self.hp = self.max_hp
            self.state = "idle"
            self._play("reBurn" if "reBurn" in self.actions else self._idle_variant)
            self._select_idle_variant()

    # ------------------------------------------------------------- 战斗
    def _stick_active(self) -> bool:
        spec = self.action_spec or {}
        for win in spec.get("sticks") or []:
            if int(win.get("from", 0)) <= self.frame <= int(win.get("to", 0)):
                return True
        return False

    def _reach(self) -> float:
        # 飞行类怪物的攻击距离数据里是 450，地面近战为 100-200；
        # 因此把最终触及距离夹在 460 以内，让数值由数据决定。
        return min(max(self.range * 0.9, 46.0), 460.0)

    def _stick_window(self) -> dict | None:
        for win in (self.action_spec or {}).get("sticks") or []:
            if int(win.get("from", 0)) <= self.frame <= int(win.get("to", 0)):
                return win
        return None

    def _root_y(self) -> float:
        """根原点纵坐标：``self.y`` 记录的是脚底线，减去脚底偏移即得根原点。"""
        clip = self._clip(self.action)
        bottom = float(getattr(clip, "colipse_bottom", 0.0) or 0.0)
        if not bottom:
            bottom = float(self.colipse_bottom)
        return self.y - bottom

    def _data_stick_rect(self, win: dict) -> pygame.Rect | None:
        """R4：stick 命中框 = body 里 ``stick`` 子对象当帧的真实矩形。

        数据来自 ``data/monsters.json`` 的 ``actions[action].sticks[*]``：
        ``{from,to,ox,oy,w,h}`` 或 ``{from,to,per_frame:[{ox,oy,w,h}, …]}``，
        坐标是 body 局部坐标（这些 body 在 root 里都放在 (0,0)，故等于 root 坐标）。
        """
        frames = win.get("per_frame")
        if frames:
            idx = self.frame - 1
            rec = frames[idx] if 0 <= idx < len(frames) else None
            if not rec:
                return None
            ox, oy = float(rec[0]), float(rec[1])
            w, h = float(rec[2]), float(rec[3])
        elif "ox" in win and "w" in win:
            ox, oy = float(win["ox"]), float(win["oy"])
            w, h = float(win["w"]), float(win["h"])
        else:
            return None
        root_x, root_y = self.x, self._root_y()
        if win.get("space") == "body":
            # C-22-1：stick 是 body 的子对象，body 在根节点里有自己的放置矩阵
            # （22 号怪物行走时 body 偏移约为 (0.35,-19.75)）。
            root_x += float(win.get("body_tx", 0.0))
            root_y += float(win.get("body_ty", 0.0))
        if self.facing > 0:
            # 素材默认朝向为左；转为朝右（matrix.a==-1）时以 x=0 为轴做镜像。
            x0 = root_x - (ox + w)
        else:
            x0 = root_x + ox
        return pygame.Rect(round(x0), round(root_y + oy),
                           max(1, round(w)), max(1, round(h)))

    def stick_rect(self) -> pygame.Rect | None:
        """近战命中框：``body.stick`` 子对象在当前帧的实际包围盒。

        带 ``ox/oy/w/h`` 或 ``per_frame`` 的数据窗口按数据给出；当前帧没有
        stick 内容时返回 ``None``（该帧不存在 stick ⇒ 不做判定）。只有
        from/to 的窗口继续沿用按 range 估算的做法，保证 CSV 标记"无问题"的
        怪物行为不变；一旦该动作有几何数据，就完全不再用 range 近似。
        """
        wins = (self.action_spec or {}).get("sticks") or []
        win = self._stick_window()
        if win is not None:
            rect = self._data_stick_rect(win)
            if rect is not None:
                return rect
            if _stick_has_geometry(win):
                return None
        if any(_stick_has_geometry(w) for w in wins):
            return None
        return self._legacy_stick_rect()

    def _legacy_stick_rect(self) -> pygame.Rect:
        reach = self._reach()
        if self.is_fly:
            # 飞行怪物悬停在英雄上方，再俯冲下来攻击。
            # 俯冲动画本身已包含爪击贴图，因此这里把判定框
            # 向下延伸到目标脚底。
            bottom = self.y + self.h * 0.5
            if self.target is not None and self.target.y > bottom:
                bottom = float(self.target.y)
            top = min(self.y - self.h * 0.5, bottom - 60.0)
            height = bottom - top
        else:
            height = max(40.0, self.h * 0.9)
            bottom = self.y
            top = bottom - height
        if self.facing > 0:
            return pygame.Rect(round(self.x - 6), round(top),
                               round(reach), round(height))
        return pygame.Rect(round(self.x + 6 - reach), round(top),
                           round(reach), round(height))

    def _contact_damage(self, world) -> None:
        if self.state not in ("attack", "walk") or not self._stick_active():
            return
        if self.attack_id in self.contact_hits:
            return
        spec = self.attacks.get(self.action)
        if not spec:
            return
        player = world.player
        if player.dead or player.immune or player.grabbed:
            return
        rect = self.stick_rect()
        if rect is None or not rect.colliderect(player.body):
            return
        dmg = player.take_damage(float(spec.get("power", 0)), self.x,
                                 spec.get("kind", "physics"),
                                 attack_id=self.attack_id,
                                 kb=tuple(spec.get("kb") or (2, -5)),
                                 src_ns=self.hit_ns)
        if dmg:
            self.contact_hits.add(self.attack_id)
            world.add_damage_number(player.x, player.y - player.h - 8, dmg, "pnum")
            world.add_effect("hero_hurt", player.x, player.y - player.h / 2)
            world.assets.play("BeattackByRole1", 0.35)
            if spec.get("effect"):
                player.add_effect(spec["effect"])

    def _check_dive_hit1(self, world) -> None:
        if not self._stick_active():
            return
        player = world.player
        if player.dead or player.immune:
            return
        if abs(player.x - self.x) <= 120 and abs(player.y - self.y) <= 150:
            self._show_hit1()

    def take_damage(self, damage: float, kind: str = "physics", attack_id: int = 0,
                    interval: float = 0.1, hit_max: int = 1,
                    kbx: float = 0, kby: float = 0, stage: int = 0,
                    source_facing: int | None = None,
                    source_x: float | None = None,
                    src_ns: int = 0,
                    register: bool = True) -> int | None:
        """怪物受击处理。

        本实现不提供"受击后 N 帧免疫"：怪物**没有连击保护**（按需求约定）。
        唯一能挡住伤害的是数据与状态条件：蛋阶段、Boss 技能的无敌帧
        （``invuln_frames``）、22 号怪物的冲锋、24 号怪物的心脏。

        命中身份 = ``(src_ns, attack_id, stage)``，等价于"技能名 + 攻击序号"；
        去重表只记录**真的结算了**的攻击（``attack_id==0`` 也照常登记），
        同一次攻击不会重复扣血。

        ``register=False`` 表示**强制溅射**：不查重、不登记到受击去重表
        （只有主目标会压栈记录攻击序号），
        照常结算伤害并走蛋/无敌等数据门。

        ``interval`` / ``hit_max`` 仅为兼容旧签名而保留：近战结算不使用
        ``hit_max``（该字段只用于子弹的逐帧判定表）。
        """
        if self.dead or self.removed:
            return None
        if self.invuln_frames > 0:
            return None
        if self.type == 22 and self.charge_invuln:
            return None
        if self.type == 24 and self.heart_alpha < 0.999 and damage < self.max_hp:
            return None
        record = register and bool(src_ns or attack_id)
        key: HitKey = (int(src_ns), int(attack_id), int(stage))
        if record and key in self.hit_ids:       # 同一命中身份只结算一次
            return None
        if self.egg:
            # 蛋阶段的受击分支：**先**查命中去重表，**后**扣蛋的剩余受击次数
            # 再把该次攻击登记进表；同一次攻击只扣 1 点，
            # 多帧连击不会瞬间打碎。
            if record:
                self.hit_ids.add(key)
            self.egg_hits -= 1
            if self.egg_hits <= 0:
                self.egg = False
                self.die(self._world)
            return None
        if self.type == 10 and source_facing is not None and source_facing == self.facing:
            dmg = 1
        elif kind == "physics":
            dmg = physical_damage(float(damage), self.defense)
        else:
            dmg = max(1, int(damage))
        self.hp -= dmg
        self.hp_slip = 2.0
        if record:
            # 命中去重表在怪物存活期内不清空、也没有容量上限（B5-③）。
            self.hit_ids.add(key)
        self.was_hit = True
        if self.target is None and self._world is not None:
            self.target = self._world.player
        if self.grab_target is not None:
            self._release_grab()
        if self.hp <= 0:
            self.hp = 0
            self._on_lethal()
        else:
            self._enter_hurt(kbx, kby)
        return dmg

    def _enter_hurt(self, kbx: float = 0, kby: float = 0) -> None:
        if "hurt" not in self.actions:
            # 22/24 号怪物没有受击动作，因此不能把状态机卡在受击上
            # （否则 22 号怪物再也不会进入冲锋）。
            return
        self.state = "hurt"
        self.hurt_frames = int((self.actions.get("hurt") or {}).get("frames", 8))
        if kbx or kby:
            # 击退速度在数据里按"像素/帧"给出（如 6 号受击 35、9 号受击 20），
            # 而本实现的速度单位是"像素/秒"，所以要乘 24 换算。
            # 不能按数值大小猜测单位："大就是像素/秒"是错的，
            # 包括 35/40 在内，全部击退值都是每帧速度。
            self.vx = kbx * 24.0
            self.vy = kby * 24.0
            if kbx and self.behavior.get("type") != "static":
                # 击退也会同时翻转朝向标记。没有 AI 的怪物（21 号的石像）
                # 从不主动转向，其素材也就始终保持一个朝向；
                # 若每次受击都强制翻转，石像会来回抖动（v2 #6），
                # 因此这里只对会行走/会转向的怪物翻转朝向。
                self.facing = 1 if kbx > 0 else -1
        self._play("hurt" if "hurt" in self.actions else self._idle_variant)
        self.skip_frame = True

    def _on_lethal(self) -> None:
        # 每次判定为致命伤都会先转入蛋形态，复活后可以再次变蛋，
        # 因此这里没有"只变一次"的开关（B9-F5）。
        # 重复的致命伤由 ``dead`` 提前返回、攻击序号去重、
        # 以及蛋分支（egg=True 时不再回到这里）挡住。
        if "phoenix_egg" in self.specials:
            self.egg = True
            self.egg_timer = 168                 # 蛋形态持续帧数
            # 蛋形态的受击次数上限：单人 5 次、双人 10 次；
            # 这里以双人/竞技场模式作为人数判据。
            self.egg_hits = 10 if getattr(self._world, "mode3", False) else 5
            self.hp = 0
            self.state = "egg"
            self.vx = self.vy = 0
            # T15 C-8：转入蛋形态时关闭飞行、改为受重力下落，
            # 并切换为静止状态 ⇒ 蛋会按重力落到地面。
            self.is_fly = False
            self.on_ground = False
            self._play("turnToEgg" if "turnToEgg" in self.actions else self._idle_variant)
            return
        if self._world is not None:
            self.die(self._world)
        else:
            self.hp = 0
            self.pending_death = True

    def die(self, world=None) -> None:
        if self.dead:
            return
        self.dead = True
        self.state = "dead"
        self.hp = 0
        self.vx = self.vy = 0
        # 尸体必须存活到死亡动画的最后一帧，否则尾部的 ``drop`` / ``destroy`` /
        # ``boss_dead`` 指令永远不会触发：18 号怪物有 40 帧死亡动画（1.67 s），
        # 15/20 号则有 30 帧（1.25 s）。
        self.death_duration = self._dead_duration()
        self.death_timer = self.death_duration
        self._release_grab()
        self._play("dead" if "dead" in self.actions else self._idle_variant)
        world = world or self._world
        if world is not None and not self.death_notified:
            self.death_notified = True
            world.on_monster_death(self)

    def despawn(self) -> None:
        """1 秒 alpha 淡出后从场景移除。

        与 :meth:`die` 的区别：**不**走 on_monster_death（不计经验、不掉落、
        不播死亡动画）。19 号怪物用它回收自己召唤出来的鲨鱼，
        回收时只做淡出，不触发死亡回调。
        """
        if self.removed or self.dead:
            return
        self.dead = True
        self.death_notified = True
        self.hp = 0
        self.state = "dead"
        self.vx = self.vy = 0
        self.death_duration = 1.0
        self.death_timer = 1.0

    def _dead_duration(self) -> float:
        if self.type == 24:
            # 24 号怪物的死亡演出：整体透明度在 2 秒内渐隐到 0，随后进入结算。
            return 2.0
        spec = self.actions.get("dead") if isinstance(self.actions, dict) else None
        if not spec:
            return 1.2
        frames = max(1, int(spec.get("frames", 1) or 1))
        return max(FRAME_DT, frames * FRAME_DT)

    # -------------------------------------------------------------- 物理
    def _adjust_speed(self) -> None:
        """受击后的水平速度修正：水平击退按每帧 20% 衰减。

        只处理水平速度，并把衰减后的速度夹到怪物的行走速度；
        垂直速度交给重力处理。22 号的 hit1 与 23 号的 hit2 会覆盖
        移动锁，因此这两种动作保留水平速度。
        """
        if self.type == 22 and self.action == "hit1":
            return
        if self.type == 23 and self.action == "hit2":
            return
        walk = max(1.0, float(self.speed))
        if abs(self.vx) > walk:
            self.vx *= 0.8
            if abs(self.vx) < walk:
                self.vx = walk if self.vx > 0 else -walk

    def _physics(self, world) -> None:
        if self.grabbed:
            return
        if self.behavior.get("type") == "static":
            # 21 号怪物没有移动逻辑：石像零重力、零位移；
            # 27 号属于同类。刷新时按 root 坐标直接落位（见 World.spawn_monster）。
            return
        if self.behavior.get("type") == "boss24":
            # 24 号怪物的移动是空实现：不受重力，固定停在 y=300。
            return
        if self.is_fly and not (self.behavior.get("fly_attack_gravity")
                                and self.state == "attack"):
            # 飞行状态下重力系数为 0（8/15 号怪物始终保持飞行）；
            # 9 号的 hit2 是"贴地冲刺"（只有 hit2 允许边攻击边移动），
            # 空中 hit2 也要落回地面 ⇒ 只有不死鸟的攻击动作走重力分支。
            self.x += self.vx * FRAME_DT
            self.y += self.vy * FRAME_DT
            self.on_ground = False
            self.x = max(world.camera_x + 20, min(world.camera_x + 920, self.x))
            self.y = max(80.0, min(world.level.ground_y - 20, self.y))
            return
        apply_physics(self, FRAME_DT, world.level.walls, world.level.slopes)
        # D-13：水平坐标恒被夹在 [100, 场景宽度-150]；
        # "跑到屏幕外"是相机效果，不是位移越界。
        # 22 号的可达性由 _stop_charge 的屏幕坐标阈值 + 贴墙兜底保证。
        self.x = max(100.0, min(world.level.width - 150.0, self.x))

    @property
    def body(self) -> pygame.Rect:
        return body_rect(self.x, self.y, self.w, self.h)

    @property
    def hit_rect(self) -> pygame.Rect:
        if self.type == 24:
            return pygame.Rect(round(self.x - 90), round(self.y - 300), 180, 260)
        hb = self.hitbox
        cx = self.x + float(hb.get("ox", 0))
        cy = self.y + float(hb.get("oy", 0))
        w = max(12.0, float(hb.get("w", self.w)))
        h = max(12.0, float(hb.get("h", self.h)))
        return pygame.Rect(round(cx - w / 2), round(cy - h / 2), round(w), round(h))

    # ------------------------------------------------------------- 24 号首领
    def _tick_boss24(self, world) -> None:
        if self.heart_phase == "hide":
            self.heart_alpha = 0.0
            self.heart_timer -= 1
            if self.heart_timer <= 0:
                self.heart_phase = "in"
                self.heart_timer = 48
        elif self.heart_phase == "in":
            self.heart_alpha = 1.0 - max(0, self.heart_timer) / 48.0
            self.heart_timer -= 1
            if self.heart_timer <= 0:
                self.heart_phase = "show"
                self.heart_alpha = 1.0
                self.heart_timer = random.randint(48, 120)
        elif self.heart_phase == "show":
            self.heart_alpha = 1.0
            self.heart_timer -= 1
            if self.heart_timer <= 0:
                self.heart_phase = "out"
                self.heart_timer = 48
        else:
            self.heart_alpha = max(0.0, self.heart_timer / 48.0)
            self.heart_timer -= 1
            if self.heart_timer <= 0:
                self.heart_phase = "hide"
                self.heart_alpha = 0.0
                self.heart_timer = random.randint(120, 216)
        self.fire_timer -= 1
        if self.fire_timer <= 0:
            self.fire_timer = 24 * random.randint(2, 6)
            count = self.fire_count
            self.fire_count = 2 if count >= 6 else count + 2
            spec = {"clip": "boss24_fire", "life": 1.5, "radius": 60,
                    "damage": "hit2", "vx": 0, "vy": 0, "hit_max": 1,
                    "arm_delay": 1.0, "shared_attack_id": True}
            for _ in range(count):
                # 火焰生成点：x 在 [900, 1700] 内随机（绝对坐标），y 取 520；
                # 换算到本实现的地面高度后等效于 ground_y + 13.4。
                fx = random.uniform(900, 1700)
                fx = max(world.camera_x + 50, min(world.camera_x + 890, fx))
                fire_y = float(world.level.ground_y) + 13.4
                world.spawn_monster_bullet(self, spec, x=fx, y=fire_y)

    # -------------------------------------------------------------- 绘制
    def _frame_variant_hero(self) -> str:
        """被抓英雄的类型，用于 hit5 按角色选择画稿（v2 #11）。

        该动作的画稿在运行时按被抓角色的类型固定选择，
        双人模式下以被抓的那个英雄为准，否则退回主角。
        """
        target = getattr(self, "grab_target", None)
        hero = getattr(target, "hero", None) if target is not None else None
        if hero:
            return str(hero)
        world = getattr(self, "_world", None)
        player = getattr(world, "player", None) if world is not None else None
        return str(getattr(player, "hero", "wukong") or "wukong")

    def _display_frame_index(self, clip) -> int:
        """逻辑帧号 → 导出帧下标（frame_variants 存在时按角色重映射）。"""
        idx = min(max(self.frame - 1, 0), len(clip.frames) - 1)
        variants = (self.action_spec or {}).get("frame_variants")
        if variants:
            table = variants.get(self._frame_variant_hero()) or variants.get("wukong")
            if table:
                logical = min(max(self.frame - 1, 0), len(table) - 1)
                idx = min(max(int(table[logical]) - 1, 0), len(clip.frames) - 1)
        return idx

    def draw(self, surface: pygame.Surface, camera_x: float) -> None:
        if self.removed:
            return
        sx = self.x - camera_x
        clip = self._clip(self.action)
        if clip is None:
            return
        # 怪物贴图的锚点指向根原点，而不是裁切后图片的底边。
        # 根原点 = 脚底线 - colipse_bottom；这里不要再叠加
        # ground_art_delta（原因见 Player.draw_offset）。
        colipse_bottom = float(getattr(clip, "colipse_bottom", 0.0)
                               or getattr(self, "colipse_bottom", 0.0))
        sy = self.y - colipse_bottom
        idx = self._display_frame_index(clip)
        img = clip.frames[idx]
        ax = clip.anchor_x
        if self.facing > 0:
            img = pygame.transform.flip(img, True, False)
            ax = clip.width - ax
        alpha = 255
        if self.dead:
            alpha = max(0, int(255 * (self.death_timer
                                      / max(FRAME_DT, self.death_duration))))
        if self.invisible:
            alpha = 0
        if self.type == 24:
            # v2 #12 / v1 C-24-3 方案 2，H1（零导出风险）：
            # - 存活时：身体图只画心脏上沿以上（rows 0..split，即 heart_top_ratio），
            #   不画烘焙进身体的半边心脏；再单独画 23 帧的心脏特效。
            # - 死亡时：整体透明度渐隐到 0；死亡动作的图已含眼睛和心脏，
            #   直接整帧绘制，**不**再叠画独立心脏（避免双绘/空图），资源与
            #   manifest 条目保持不动（md5 守卫见 tests）。
            px, py = round(sx - ax), round(sy - clip.anchor_y)
            if self.dead:
                if alpha < 255:
                    img = img.copy()
                    img.set_alpha(alpha)
                surface.blit(img, (px, py))
            else:
                # 绘制层次：心脏(1) → 身体眼睛(4/6)
                self._draw_boss24_heart(surface, sx, sy, alpha)
                ratio = float(self.data.get("heart_split_ratio", 0.559) or 0.559)
                split = max(1, min(img.get_height(),
                                   int(round(img.get_height() * ratio))))
                top = img.subsurface((0, 0, img.get_width(), split))
                if alpha < 255:
                    top = top.copy()
                    top.set_alpha(alpha)
                surface.blit(top, (px, py))
            self._draw_boss24(surface, sx, sy, alpha)
        else:
            if alpha < 255:
                img = img.copy()
                img.set_alpha(alpha)
            surface.blit(img, (round(sx - ax), round(sy - clip.anchor_y)))
        if self.egg:
            self._draw_egg(surface, sx, sy)
        # T15 C-5：血条是挂在 **根节点上的固定子对象**（x=-23, y=-66），
        # 与当前动作的 clip.anchor 无关。
        self._draw_hp_slip(surface, sx, sy)

    def _draw_hp_slip(self, surface: pygame.Surface, sx: float,
                      root_y: float) -> None:
        """受击后显示 2 秒的 50x5 红色血条。

        ``root_y`` 是角色根节点（不是当前动作图的 top）——血条挂在根节点上，
        固定偏移 (-23, -66)，所以换动作不会让血条飞出去（T15 C-5）。
        """
        if (self.dead or self.is_boss or self.invisible
                or self.hp_slip <= 0 or self.hp <= 0):
            return
        ratio = max(0.0, min(1.0, self.hp / max(1, self.max_hp)))
        bar = pygame.Surface((50, 5), pygame.SRCALPHA)
        pygame.draw.rect(bar, (0, 0, 0), (0, 0, 50, 5), 1)
        width = int(48 * ratio)
        if width > 0:
            fx = 1 if self.facing > 0 else 49 - width
            pygame.draw.rect(bar, (255, 0, 0), (fx, 1, width, 3))
        bar.set_alpha(int(255 * min(1.0, self.hp_slip / 2.0)))
        surface.blit(bar, (round(sx - 23), round(root_y - 66)))

    def _draw_boss24_heart(self, surface: pygame.Surface, sx: float, sy: float,
                           alpha: int) -> None:
        """24 号怪物独立的心脏图层（v2 #12 H1）。

        位置 = root + (17.5,116.1)，锚点 (48.1,53.0)，alpha = heart_alpha；
        帧号由 ``self.tick % 23`` 驱动（该图层自己循环播放，不能被
        ``Animation(loop=False)`` 停在末帧）。
        """
        clip = getattr(self, "_heart_clip", None)
        if clip is None or not clip.frames or self.heart_alpha <= 0.01:
            return
        off = self.data.get("heart_offset") or [17.5, 116.1]
        img = clip.frames[self.tick % len(clip.frames)]
        a = int(max(0, min(255, alpha * float(self.heart_alpha))))
        if a < 255:
            img = img.copy()
            img.set_alpha(a)
        surface.blit(img, (round(sx + float(off[0]) - clip.anchor_x),
                           round(sy + float(off[1]) - clip.anchor_y)))

    def _draw_boss24(self, surface: pygame.Surface, sx: float, sy: float,
                     alpha: int) -> None:
        hands = self.assets.fx("boss24_hands")
        if hands is not None:
            base = hands.frames[0]
            for hand in self.hands:
                # 初始化时 h2.transform.matrix.a = -1 ⇒ offset>0 的那只手做镜像
                img = base if hand.side < 0 else pygame.transform.flip(base, True, False)
                if alpha < 255:
                    img = img.copy()
                    img.set_alpha(alpha)
                hx = sx + hand.offset
                # 挂点 y = 18（手部容器在根坐标下的位置）；手的原点在手心
                hy = sy + 18 + hand.y
                surface.blit(img, img.get_rect(center=(round(hx), round(hy))))

    def _draw_egg(self, surface: pygame.Surface, sx: float, sy: float) -> None:
        pulse = 0.5 + 0.5 * math.sin(self.tick / 3.0)
        radius = 34
        ring = pygame.Surface((radius * 2 + 8, radius * 2 + 8), pygame.SRCALPHA)
        pygame.draw.circle(ring, (255, 200, 80, int(60 + 100 * pulse)),
                           (radius + 4, radius + 4), radius, 3)
        surface.blit(ring, (round(sx - radius - 4), round(sy - 90 - radius)))
