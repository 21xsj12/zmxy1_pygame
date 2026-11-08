"""唐僧五种技能的**唯一**实现（``game.world`` 与 ``game.arena`` 共用）。

数值与时机来自技能实现方案 §4–§8；几何数据由 ``tools/build_tang_fx.py``
按动画片段生成到 ``data/tang_fx.json``，不要手改，改数据时重跑生成器。

为什么集中在一个模块：早期竞技场有一套并行实现（一次性 ``aoe``、写死矩形、
错误的 P1 键位），两套逻辑各自演化后行为不一致。把生成/判定/治疗/引爆全部
放在这里，两条世界路径只提供「命中结算 / 特效 / 跳字」三个钩子，从结构上
避免再次分叉。

坐标约定（R1-07）：动画根空间里角色根坐标取碰撞椭圆底边（y[-49.2,40.8]），
站立时等于脚底减去 ``collision_bottom``（唐僧 40.8 / 悟空 44.95）；本实现的
``Player.y`` 直接是脚底，所以所有技能坐标都必须走 :func:`root_y`，不能用
``y - 45``。
"""
from __future__ import annotations

import json
from functools import lru_cache

from .constants import DATA_DIR
from .projectile import Projectile

# ---- 五个技能与普攻第二段的 MP 消耗
CHARGE_MP = 20            # 冰龙波「蓄满」才扣（普攻第二段蓄力期间不扣）
CHARGE_FRAMES = 30        # 蓄满判定所需的连续帧数
AOE_MP = 40               # 玄冰阵的 MP 消耗
SHADOW_MP = 30            # 水幻影的 MP 消耗
HEAL_MP = 60              # 天降甘露的 MP 消耗
HIT4_MP = 100             # 水魔爆的 MP 消耗

# ---- 普攻各段的伤害参数表（只有子弹会读取）。运行时的实际数值来自
# data/actions.json 的帧事件与伤害配置（生成器 tools/build_actions.py），这里保留
# 一张对照表，便于审计：
#   hit1  ：倍率 1.0 / 物理 / 击退 (4,-7) / 命中上限 1   / 间隔 4 / 射程 60
#   hit2  ：倍率 0.2 / 法术 / 击退 (20,-5) / 命中上限 200 / 间隔 4
#   hit3  ：倍率 0.8 / 法术 / 击退 (6,-7)  / 命中上限 200 / 间隔 999
#   hit4-1：倍率 8.0 / 法术 / 击退 (25,-5) / 命中上限 200 / 间隔 999
AOE = dict(mult=0.8, kind="magic", kb=(6, -7), hit_max=200, interval=0)
COLUMN = dict(mult=8.0, kind="magic", kb=(25, -5), hit_max=200, interval=999)

HEAL_REACH = (200.0, 200.0)          # 治疗范围：横向与纵向都不超过 200 像素
HEAL_FX_DY = -60                     # 治疗特效相对角色根坐标向上偏移 60 像素
HEAL_FRAMES = 34                     # 治疗特效第 34 帧才结算治疗
HEAL_DELAY_TICKS = 33                # 第 1 帧先显示，之后等 33 个逻辑帧
SHADOW_REACH = (200.0, 160.0)        # 水幻影的治疗范围：横向 200、纵向 160 像素
SHADOW_LIFE = 192                    # 水幻影存在 192 帧（约 8 秒）
SHADOW_SPEED = 8.0                   # 水幻影移动速度：每帧 8 像素
AOE_SHAKE = 0.2                      # 玄冰阵震屏强度 15 折算到本工程后的数值

BULLET_CLIPS = {
    "Role2Bullet1": "bullet_role2_1",
    "Role2Bullet2": "bullet_role2_2",
    "Role2Bullet3": "bullet_role2_3",
    "hit4FallDown": "hit4_falldown",
}
SHADOW_CLIPS = {"": "shadow_walk", "hit3": "shadow_hit3", "hit5": "shadow_hit5"}


@lru_cache(maxsize=1)
def fx_data() -> dict:
    return json.loads((DATA_DIR / "tang_fx.json").read_text(encoding="utf-8"))


def reload_fx_data() -> None:
    """生成器重跑后清缓存（测试/工具可用）。"""
    fx_data.cache_clear()


def clip_meta(name: str) -> dict:
    return fx_data()["clips"][name]


def clip_bounds(name: str) -> list:
    return clip_meta(name)["bounds"]


def anchor(name: str) -> tuple[float, float]:
    data = clip_meta(name)
    return float(data["anchor"][0]), float(data["anchor"][1])


# ------------------------------------------------------------------ 坐标
def root_y(actor) -> float:
    """角色根坐标（碰撞椭圆底边）：本实现的 ``Player.y`` 是脚底。"""
    return float(actor.y) - float(getattr(actor, "collision_bottom", 45.0))


def shadow_root_y(shadow) -> float:
    return float(shadow.y) - float(getattr(shadow, "collision_bottom", 40.8))


def round_mc_window() -> tuple[int, int]:
    first, last = fx_data()["round_mc"]["window"]
    return int(first), int(last)


def round_mc_offset(body_frame: int) -> tuple[float, float] | None:
    """水魔爆第一段的躯干动画帧号 -> 法阵当前 (x, y)（相对躯干原点，朝左为负 x）。"""
    offsets = fx_data()["round_mc"]["offsets"]
    value = offsets.get(str(int(body_frame)))
    return (float(value[0]), float(value[1])) if value else None


def round_mc_scale_y() -> float:
    return float(fx_data()["round_mc"]["scale_y"])


def round_mc_body_offset() -> tuple[float, float]:
    """躯干动画在角色根空间的偏移（法阵视觉要叠上它，判定点不叠）。"""
    value = fx_data()["round_mc"].get("body_offset") or [12.45, -8.05]
    return float(value[0]), float(value[1])


def round_mc_frame_index(body_frame: int) -> int:
    """法阵动画只有 20 帧，而躯干动画从第 10 帧播到第 42 帧 → 循环取模。"""
    first = round_mc_window()[0]
    count = len(clip_meta("round_mc")["bounds"])
    return (int(body_frame) - first) % max(1, count)


def shadow_beats() -> dict:
    return fx_data()["shadow"]


def shadow_body_offset(action: str) -> tuple[float, float]:
    """水幻影根时间轴给躯干动画的位移（行走 / 第三击 / 第五击）。"""
    bodies = shadow_beats().get("bodies") or {}
    value = bodies.get(action) or bodies.get("walk") or [0.0, 0.0]
    return float(value[0]), float(value[1])


def charge_bar_geometry() -> dict:
    """蓄力条几何：宽度取角色根范围的宽度，条高固定 9 像素。"""
    data = fx_data().get("role2_root") or {}
    return {
        "width": float(data.get("width", 67.2)),
        "height": float(data.get("height", 92.05)),
        "bar_height": 9.0,
    }


# --------------------------------------------------------------------- 生成
def _bullet(world, caster, spec: dict) -> Projectile:
    name = str(spec.get("name", "Role2Bullet1"))
    clip = BULLET_CLIPS[name]
    speed = float(spec.get("speed", 8)) * 24.0
    distance = float(spec.get("distance", 300))
    scale = spec.get("scale") or (1.0, 1.0)
    kb = spec.get("kb") or (0, 0)
    projectile = Projectile(
        caster.x, root_y(caster),
        caster.facing * speed, 0.0, 0.0, "player",
        str(spec.get("kind", "magic")), int(caster.attack_id), clip,
        radius=int(spec.get("radius", 24)),
        life=max(1.0, distance / max(1.0, abs(speed)) + 0.4),
        stage=0, kb=(float(kb[0]), float(kb[1])), base_power=0.0,
        hit_max=int(spec.get("hit_max", 1)),
        interval=float(spec.get("interval", 0)),
        distance=distance,
        art_scale=(float(scale[0]), float(scale[1])),
        mult=float(spec.get("mult", 1.0)),
        fixed=float(spec["fixed"]) if spec.get("fixed") else None,
        attack_range=float(spec.get("attack_range", 0)),
        art_bounds=clip_bounds(clip), native_art=True, source=caster,
        spawn_delay=True,
        # 子弹身份 = 子弹自身名称 + 施法者的攻击编号；每颗子弹各有一份实例命名空间。
        hit_ns=world.new_hit_ns(),
    )
    projectile.facing = caster.facing
    world.projectiles.append(projectile)
    return projectile


def spawn_bullet(world, caster, spec: dict) -> Projectile:
    """普攻法球：冰龙波与普通攻击发射的飞行子弹。"""
    return _bullet(world, caster, spec)


def spawn_aoe_at(world, x: float, y: float, attack_id: int,
                 source, gxp: bool | None = None) -> Projectile:
    """玄冰阵的生成入口（英雄本体与幻影共用同一套弹体逻辑）。

    弹体静止存在 46 帧，在第 6/11/16/21 帧切换命中身份（蓄力态再追加
    26/31 帧）；蓄力时缩放参数写的是 1.8 倍，但该弹体的缩放会递归叠加，
    实际生效为 (1.26,1.8)。

    ``gxp`` 是**缩放**判据：英雄那颗用英雄自身的 ``is_gxp``；幻影那颗用
    幻影主人的 ``is_gxp``（施法者记录的状态）。
    而 26/31 帧的判据是子弹来源角色的 ``is_gxp``（幻影恒为 False），
    所以两者必须分开传（方案 §5.7 G2-5）。
    """
    if gxp is None:
        gxp = bool(getattr(source, "is_gxp", False))
    scale = (1.26, 1.8) if gxp else (1.0, 1.0)
    projectile = Projectile(
        x, y, 0.0, 0.0, 0.0, "player", AOE["kind"], int(attack_id),
        "bullet_role2_3", radius=180, life=46 / 24.0, stage=0,
        kb=AOE["kb"], base_power=0.0, hit_max=AOE["hit_max"], interval=0.0,
        frame_ids=(6, 11, 16, 21), gxp_frames=(26, 31),
        art_scale=scale, mult=AOE["mult"],
        art_bounds=clip_bounds("bullet_role2_3"), native_art=True,
        source=source, frame_limit=46,
        spawn_delay=True,
        hit_ns=world.new_hit_ns(),
    )
    projectile.facing = getattr(source, "facing", 1)
    world.projectiles.append(projectile)
    # 生成（第 1 帧）即触发一次震屏，之后在 6/11/16/21/26/31 帧各再来一次。
    setattr(world, "shake", max(float(getattr(world, "shake", 0.0)), AOE_SHAKE))
    return projectile


def spawn_aoe(world, player) -> Projectile:
    """躯干动画第 15 帧：在自身根位置生成玄冰阵（不偏移、不翻转朝向）。"""
    return spawn_aoe_at(world, player.x, root_y(player), player.attack_id,
                        source=player)


def spawn_column(world, player, x: float, y: float) -> Projectile:
    """水魔爆第二段的水柱：静止存在 25 帧，命中身份取英雄当前攻击编号。"""
    projectile = Projectile(
        x, y, 0.0, 0.0, 0.0, "player", COLUMN["kind"], int(player.attack_id),
        "hit4_falldown", radius=140, life=25 / 24.0, stage=0,
        kb=COLUMN["kb"], hit_max=COLUMN["hit_max"], interval=999.0,
        mult=COLUMN["mult"],
        art_bounds=clip_bounds("hit4_falldown"), native_art=True,
        source=player, frame_limit=25,
        spawn_delay=True,
        # 音效在水柱自身第 14 帧（约 +0.542 秒）才播放，而不是生成瞬间。
        sfx_frames={int(fx_data()["hit4"]["sfx_frame"]): "Role2_hit4"},
        hit_ns=world.new_hit_ns(),
    )
    projectile.facing = player.facing
    world.projectiles.append(projectile)
    return projectile


def detonate_hit4(world, player) -> Projectile | None:
    """第二段：按法阵**当前**坐标放水柱，然后把躯干动画跳到第 43 帧。

    判定点取角色 ``x`` 加减法阵当前横向偏移（**不含**躯干动画偏移）；法阵
    视觉才叠躯干动画偏移，见 :func:`round_mc_body_offset`。
    """
    player.hit4_detonate_requested = False
    player.round_active = False
    frame = player.frame_action.frame
    offset = round_mc_offset(frame)
    if offset is None:
        offset = round_mc_offset(round_mc_window()[1]) or (-78.5, 50.6)
    x = player.x + player.facing * abs(offset[0])
    y = (root_y(player) + offset[1]
         + float(fx_data()["hit4"]["spawn_y_offset"]))
    projectile = spawn_column(world, player, x, y)
    player.frame_action.goto_frame(43)
    player.anim.time = 42.0 / 24.0
    return projectile


# --------------------------------------------------------------------- 治疗
def heal_amount(player) -> int:
    """治疗量 = (最大 MP / 8 + 8) × (蓄力状态 1.5 倍)，结果取整截断。"""
    amount = int(player.max_mp / 8 + 8)
    if getattr(player, "is_gxp", False):
        amount = int(amount * 1.5)
    return amount


def _cure(world, player, amount: int) -> None:
    player.hp = min(player.max_hp, player.hp + amount)
    world.add_heal_number(player, amount)


def cure_role(world, caster, point_x: float, point_y: float) -> None:
    """治疗结算：自己无条件生效，队友需 200×200 范围内且非竞技场模式。

    治疗点由治疗特效在施法瞬间固定（不跟随角色），所以调用方传入
    施法时捕获的 ``(根x, 根y-60)``。
    """
    amount = heal_amount(caster)
    for player in world.tang_players():
        if player.dead:
            continue
        if player is caster:
            _cure(world, player, amount)
            continue
        if world.mode3:
            continue
        if (abs(player.x - point_x) <= HEAL_REACH[0]
                and abs(root_y(player) - point_y) <= HEAL_REACH[1]):
            _cure(world, player, amount)


def heal(world, caster) -> None:
    """以施法者**当前**位置为治疗点（兼容入口）。"""
    cure_role(world, caster, caster.x, root_y(caster) + HEAL_FX_DY)


def heal_at(world, x: float, y: float, source) -> None:
    """水幻影的治疗结算：200×160 内所有玩家（含本体）；竞技场模式下只治唐僧。"""
    amount = heal_amount(source)
    for player in world.tang_players():
        if player.dead:
            continue
        if world.mode3 and player.hero != "tangseng":
            continue
        if (abs(player.x - x) <= SHADOW_REACH[0]
                and abs(root_y(player) - y) <= SHADOW_REACH[1]):
            _cure(world, player, amount)


def cure_point(caster) -> tuple[float, float]:
    """治疗特效的世界坐标（即治疗点）。"""
    return caster.x + 0.0, root_y(caster) + HEAL_FX_DY
