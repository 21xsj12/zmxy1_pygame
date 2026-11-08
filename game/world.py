"""战斗世界：刷怪调度、停点触发、投射物、掉落与渲染。"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass

import pygame

from . import ui
from .assets import Animation, Assets
from .auras import AuraSystem
from .bossbar import BossBar
from .camera import ViewCamera
from .combo import ComboCounter
from .constants import LEVEL_NAMES, WIDTH
from .entities import Hit, Monster, MonsterBullet, Player
from .gxp_fx import GxpEffect
from .hud import RoleInfoHud
from .items import MAX_BAG, drop_icon, random_drop, refresh_hou_dong
from .level import Level, SpawnSpec, Wall
from .projectile import RIGHT_AUTHORED_FX, Projectile
from . import tang

# 逻辑帧固定为 24 fps（一帧对应一次动作更新）。
# 渲染仍按 60 fps 进行；World.update() 累积真实 dt，再以固定 24 Hz 步长
# 推进模拟（设计文档 §13.1/§13.5）。
LOGIC_DT = 1.0 / 24.0

DAMAGE_CLIP = {"pnum": "pnum", "hurtnum": "hurtnum", "bnum": "bnum",
               "bunum": "bunum", "bulnum": "bulnum"}

# 药品掉落（小型/大型生命药、小型法力药）：
# kind -> (图标, 名义回复量, 飘字类型)。回复量是固定的 100/200/100，
# 不是最大生命/法力的百分比；即使最大生命/法力上限把实际回复
# 截断，飘字显示的仍是该固定值。
MEDICINE = {
    "small_hp": ("ui/small_hp.png", 100, "bunum"),
    "big_hp": ("ui/big_hp.png", 200, "bunum"),
    "small_mp": ("ui/small_mp.png", 100, "bulnum"),
}
# _draw_drop() 以 (drop.x, drop.y - DROP_DRAW_CENTER_DY) 为图标中心；
# _roll_medicine() 复用同一逻辑，让药品追加时根节点的落点
# 与常规掉落物落在同一屏幕位置。
DROP_DRAW_CENTER_DY = 20

# ------------------------------------------------ 关卡出口素材
# 出口动画：关卡停点销毁时会把一段 67 帧动画加到 HUD 的 (860,300) 处，
# 缩放 0.7；该动画素材各元素合并后的包围盒为
# (-106.2,-45.85)-(106.2,45.85) 像素（213x92）。
GOGO_X, GOGO_Y, GOGO_SCALE = 860.0, 300.0, 0.7
GOGO_MIN_X, GOGO_MIN_Y = -106.2, -45.85
GOGO_W, GOGO_H = 213.0, 92.0
GOGO_FRAMES = 67
GOGO_FPS = 24.0
# 传送门：门体对象把滚动门动画放在偏移 (-0.25,54.3) 处；该动画合并包围盒
# 为 (-90.5,-165)-(95.3,0)，即 (-90.75,-110.7)-(95.05,54.3)，尺寸 186x165。
DOOR_LEFT, DOOR_TOP = -90.75, -110.7
DOOR_W, DOOR_H = 186, 165
DOOR_RIGHT, DOOR_BOTTOM = 95.05, 54.3
# 各关卡传送门的绘制层级取自分析数据 analysis/level_impl.json 的 doors[0].depth（T5 §4.3）。
PORTAL_DEPTH = {"1-1": 501, "1-2": 501, "1-3": 501, "2-1": 256,
                "2-2": 465, "2-3": 465, "3-1": 465, "3-2": 465,
                "3-3": 465, "9-1": 25}
# 方向键状态 "0001" 表示只按 W/↑；同时按住 S/J/K（P1）时按键串不再是
# "0001"，因此不应触发传送门。
PORTAL_ACTION_KEYS = (pygame.K_s, pygame.K_j, pygame.K_k, pygame.K_DOWN)
PORTAL_FADE_SECONDS = 1.0


def fx_scale_pair(value) -> tuple[float, float]:
    """接受旧的统一缩放值，或 (x,y) 二元组（特效子弹使用）。"""
    if isinstance(value, (tuple, list)):
        return float(value[0]), float(value[1])
    scale = float(value)
    return scale, scale


class FloatingText:
    def __init__(self, x: float, y: float, text: str, color=(255, 230, 120), ttl: float = 0.85):
        self.x, self.y = x, y
        self.text = text
        self.color = color
        self.ttl = ttl
        self.max_ttl = ttl

    def update(self, dt: float) -> None:
        self.ttl -= dt
        self.y -= 64 * dt

    @property
    def alive(self) -> bool:
        return self.ttl > 0


@dataclass
class DamageNumber:
    x: float
    y: float
    value: int
    kind: str = "hurtnum"
    ttl: float = 0.9
    vy: float = -150.0

    def update(self, dt: float) -> None:
        self.ttl -= dt
        self.y += self.vy * dt
        self.vy += 240 * dt


@dataclass
class Drop:
    x: float
    y: float
    item: dict
    # 掉落物沿用 4 像素/帧的下坠速度，按 24 fps 折算即 96 像素/秒。
    # 不要给它加向上初速度：向上弹出可能让掉落物从下方
    # 穿过单向平台。
    vy: float = 96.0
    ttl: float = 22.0
    on_ground: bool = False
    medicine: str = ""       # 药品掉落时使用的 MEDICINE 键

    def update(self, dt: float, ground_y: float, surface_fn=None) -> None:
        self.ttl -= dt
        if not self.on_ground:
            prev_y = self.y
            self.vy += 900 * dt
            self.y += self.vy * dt
            surface = surface_fn(self.x, self.y, prev_y) if surface_fn else ground_y
            if surface is not None and self.y >= surface:
                self.y = surface
                self.on_ground = True
                self.vy = 0


class GogoHud:
    """关卡出口计分动画。

    非最后一个停点在运行时 ``idx < 4`` 时会向 HUD 添加这段 67 帧、213x92
    的动画，位置 (860,300)，缩放 0.7；播放到第 67 帧后从容器移除。
    它由固定的 24 Hz 世界时钟驱动，使 24 fps 时间线与设计完全一致
    （T5 §7.2）；动画固定在屏幕上，不随摄像机移动。
    """

    def __init__(self, world: "World | None" = None):
        self.world = world
        self.x = GOGO_X
        self.y = GOGO_Y
        self.scale = GOGO_SCALE
        self.frame = 1
        # 严格整数 24 Hz 时间线：每个逻辑 tick 恰好推进一帧
        # （不用浮点累计时长，避免出现跳过第 8/29 帧、
        # 重复第 6/14/59 帧的问题）。
        self.ticks = 0
        self.alive = True

    def update(self, dt: float) -> None:
        if not self.alive:
            return
        self.ticks += 1
        self.frame = 1 + self.ticks
        if self.frame >= GOGO_FRAMES:
            # 第 67 帧：从父容器移除自身并停止播放。
            self.frame = GOGO_FRAMES
            self.alive = False
            if self.world is not None:
                try:
                    self.world.gogos.remove(self)
                except ValueError:
                    pass

    def draw(self, surface: pygame.Surface, assets: Assets) -> None:
        if not self.alive:
            return
        clip = assets.fx("gogo")
        if clip is None:
            return
        idx = min(len(clip.frames) - 1, max(0, self.frame - 1))
        size = (round(GOGO_W * self.scale), round(GOGO_H * self.scale))
        img = pygame.transform.smoothscale(clip.frames[idx], size)
        surface.blit(img, (round(self.x + GOGO_MIN_X * self.scale),
                           round(self.y + GOGO_MIN_Y * self.scale)))


class Spawner:
    def __init__(self, spec: SpawnSpec):
        self.spec = spec
        self.delay = spec.delay
        self.timer = 0.0
        self.spawned = 0
        self.done = False

    def update(self, dt: float, world: "World") -> None:
        if self.done:
            return
        if self.delay > 0:
            self.delay -= dt
            return
        if self.spawned >= self.spec.total:
            self.done = True
            return
        if world.alive_count() >= getattr(world, "max_monsters", 6):
            return
        if self.timer <= 0:
            world.spawn_monster(self.spec)
            self.spawned += 1
            self.timer = max(0.05, self.spec.interval)
        else:
            self.timer -= dt
        if self.spawned >= self.spec.total:
            self.done = True


class Thorn:
    """第 2 大关的尖刺陷阱。

    静止尖刺（2-1，``is_up=False``）每 5 帧检测一次，且从不移动。
    下落尖刺（2-2/2-3，``is_up=True``）每帧检测，然后按三阶段时间线运行
    （v2 #5）：

    * 待机：第 1 帧（从 0 起算的 0），不移动；
    * 抖动：从第 2 帧播放到第 19 帧（从 0 起算的 1..18），按 24 fps 计
      18 帧共 0.75 秒，y 坐标不变；
    * 下落：停在第 20 帧（从 0 起算的 19），同时 ``++gravity; y += gravity``
      （每帧 +1、+2……的积分，等效 576 像素/秒²）。

    y >= 500 时隐藏尖刺；y >= 1000 时复位到初始位置并让攻击序号 +1。
    """

    ACCEL = 24.0 * 24.0
    #: 20 帧时间线中的第 2..19 帧（从 0 起算为 1..18）。
    SHAKE_FRAMES = 18
    FALL_FRAME = 19

    def __init__(self, rec: dict, clip, uid: int):
        self.x = float(rec["x"]) + float(rec["w"]) / 2.0
        self.y = float(rec["y"]) + float(rec["h"]) / 2.0
        self.w = float(rec["w"])
        self.h = float(rec["h"])
        self.is_up = bool(rec.get("is_up", False))
        # T15 A-3/A-6：地面尖刺在数据里是纵向缩放 -1 的镜像实例（is_up=False），
        # 绘制要上下翻转；旧数据没有该字段 → 按 False 处理（向后兼容）。
        self.flip = bool(rec.get("flip", False))
        self.depth = int(rec.get("depth", 0))
        self.clip = clip
        self.frame = 0
        self.anim_t = 0.0
        self.vy = 0.0
        # idle -> shake -> fall（对应动画标签与是否下落的状态机）
        self.state = "idle"
        self.visible = True
        self.check_t = 0.0
        self.arm_t = 0.0
        # 命中身份由实例名和攻击序号组成：uid 充当实例名，
        # attack_id 是攻击序号（静止型每 24 帧 +1，下落型落地回收 +1）。
        self.uid = int(uid)
        self.attack_id = 900000 + uid
        self.base_x, self.base_y = self.x, self.y

    @property
    def falling(self) -> bool:
        """以只读属性兼容旧的“下落中”判断（现在不再是独立标志位）。"""
        return self.state == "fall"

    @property
    def rect(self) -> pygame.Rect:
        return pygame.Rect(round(self.x - self.w / 2), round(self.y - self.h / 2),
                           round(self.w), round(self.h))

    def _overlaps_x(self, p: Player, slack: float = 5.0) -> bool:
        return (self.x - self.w / 2 <= p.x + p.w / 2 - slack
                and self.x + self.w / 2 >= p.x - p.w / 2 + slack)

    def _try_hit(self, world: "World", p: Player) -> None:
        if p.dead or not self.visible:
            return
        if abs(self.y - p.y) >= 90:
            return
        if not self.rect.colliderect(pygame.Rect(round(p.x - p.w / 2),
                                                 round(p.y - p.h),
                                                 round(p.w), round(p.h))):
            return
        dmg = p.take_damage(30, self.x, kind="magic",
                            attack_id=self.attack_id, kb=(10, 10),
                            src_ns=self.uid)
        if dmg:
            world.add_damage_number(p.x, p.y - p.h - 8, dmg, "pnum")
            world.add_effect("hero_hurt", p.x, p.y - p.h / 2)
            world.assets.play("BeattackByRole1", 0.4)

    def update(self, dt: float, world: "World") -> None:
        p = world.player
        if self.is_up:
            if self.state == "idle" and self.visible:
                if abs(self.x - p.x) < 80 and self._overlaps_x(p):
                    # 触发后从第 2 帧开始播放：18 帧抖动启动，
                    # 此时 y 还不移动。
                    self.state = "shake"
                    self.frame = 1
                    self.anim_t = 0.0
            elif self.state == "shake":
                self.anim_t += dt
                elapsed = int(self.anim_t * self.clip.fps + 1e-6)
                if elapsed >= self.SHAKE_FRAMES:
                    # 进入第 20 帧：标记为下落，本体停在第 20 帧，
                    # 由重力逐帧累加 y（+1、+2、+3... 像素）。
                    self.state = "fall"
                    self.frame = self.FALL_FRAME
                    self.vy = 0.0
                else:
                    self.frame = 1 + elapsed
            elif self.state == "fall":
                self.vy += self.ACCEL * dt
                self.y += self.vy * dt
                self.frame = self.FALL_FRAME
                if self.y >= 500:
                    self.visible = False
                if self.y >= 1000:
                    self.x, self.y = self.base_x, self.base_y
                    self.visible = True
                    self.state = "idle"
                    self.vy = 0.0
                    self.frame = 0
                    self.anim_t = 0.0
                    self.attack_id += 1
            if self.visible and abs(self.x - p.x) < 80:
                self._try_hit(world, p)
        else:
            self.check_t += dt
            if self.check_t >= 5.0 / 24.0:
                self.check_t = 0.0
                if abs(self.x - p.x) < 80:
                    self._try_hit(world, p)
            self.arm_t += dt
            if self.arm_t >= 1.0:
                self.arm_t = 0.0
                self.attack_id += 1

    def draw(self, surface: pygame.Surface, camera_x: float) -> None:
        if not self.visible or not self.clip.frames:
            return
        img = self.clip.frames[min(self.frame, len(self.clip.frames) - 1)]
        if self.flip:
            # T15 A-3/A-6：地面尖刺数据使用纵向缩放 -1 的镜像实例，只有垂直镜像。
            img = pygame.transform.flip(img, False, True)
        surface.blit(img, (round(self.x - self.w / 2 - camera_x),
                           round(self.y - self.h / 2)))


class WaterStrip:
    """第 1-2 关水面：上层图层用遮罩形状裁剪下层滚动水纹。

    层级数据：depth 1 的遮罩形状覆盖到裁剪深度 5；depth 2 是同一遮罩形状
    （可见水体）；depth 3 是 325 帧的滚动图，每帧向左移动 3.15 像素。
    因此滚动位图是被遮罩形状的 alpha 通道裁剪的，不会铺满整条 1600 像素。
    """

    def __init__(self, rec: dict, assets: Assets):
        self.x = float(rec["x"])
        self.y = float(rec["y"])
        self.w = float(rec["w"])
        self.h = float(rec["h"])
        self.depth = int(rec.get("depth", 0))
        self.surface = assets.image(rec["surface"])
        self.static = assets.image(rec["static"])
        # 白色 RGB 与原图的 alpha 相乘：把水体图层乘上这份遮罩，
        # 就能保留水体颜色并套用位图遮罩。
        self.mask = self.static.copy()
        self.mask.fill((255, 255, 255, 255), special_flags=pygame.BLEND_RGB_MAX)
        self.t = 0.0

    def update(self, dt: float) -> None:
        self.t += dt

    def draw(self, surface: pygame.Surface, camera_x: float) -> None:
        sx = round(self.x - camera_x)
        sy = round(self.y)
        # 遮罩形状在显示对象内位于原点；它的 PNG 原点取形状包围盒最小值，
        # 即世界坐标的素材框左上角再加 (29.8,0.5)。
        mx, my = sx + 30, sy + 1
        surface.blit(self.static, (mx, my))
        scroll = -((int(self.t * 24.0) % 325) * 3.15)
        layer = pygame.Surface(self.mask.get_size(), pygame.SRCALPHA)
        # 滚动水纹的世界左边缘对应素材框左边界；遮罩左边缘则再向右 29.8 像素。
        layer.blit(self.surface, (round(scroll - 29.8), 0))
        layer.blit(self.mask, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
        surface.blit(layer, (mx, my))


class World:
    def __init__(self, assets: Assets, level: Level, saved: dict | None = None,
                 inventory: list | None = None,
                 soul_data: dict | None = None):
        self.assets = assets
        self.level = level
        self.camera = ViewCamera(level)
        self.player = Player(assets, level, saved)
        self.player.world = self
        # 玩家列表：单机时只有主角；双人/竞技场模式由外部追加。
        self.players: list[Player] = [self.player]
        # 英雄的命中命名空间固定为 1/2，由角色选择流程决定；
        # 单机只有主角，1P 固定为 1。
        self.player.hit_ns = 1
        # 攻击实例命名空间计数器（为每次攻击分配唯一名字，战斗内单调递增）。
        self._hit_ns = 100
        self.hud = RoleInfoHud(assets)
        # 直接引用存档字典：加魂值要立刻写回存档，这样关卡中途打开背包
        # 也能看到最新的魂数量；所以这里保存的是同一个字典对象，不做拷贝。
        # 同理，通关结算时不能再把 soul_gain 累加一次，否则同一次获得
        # 会被计入两遍。
        self.soul_data = soul_data
        # 每次击杀都会把当前经验/等级写入存档对象；这里把同样的同步挂到
        # 主角身上，确保死亡、退出、重开都不丢经验（无双槽只在战斗内
        # 有效，不写入存档）。
        self.player.on_exp_change = self.sync_player_progress
        # 直接引用存档里的背包列表：拾取时就能按 25 格上限处理，
        # 不会把关卡结束后才发现的溢出物品悄悄丢掉，也不会产生
        # 两份互不同步的背包数据。
        self.inventory: list = inventory if inventory is not None else []
        self.ground_img = None
        self.ground_side_img = None
        self.terrain_pieces: list[tuple[float, float, pygame.Surface]] = []
        self.slope_pieces: list[tuple[float, float, pygame.Surface]] = []
        # 地面、雕像和平台贴图都按素材自身的锚点对齐绘制，
        # 与素材位图的定位方式一致。
        self.deco_items: list[dict] = []
        for d in level.deco:
            img = assets.image(d["img"])
            # T15 A-4/B-4：分轴翻转。摆放数据用横向、纵向缩放表达朝向
            # （1-3 关的 deco103 只有横向缩放 -1，因此只需 flip_x）；旧写法
            # 的 `flip` 是双轴翻转，会把草皮翻到下面。
            fx = bool(d.get("flip_x", d.get("flip", False)))
            fy = bool(d.get("flip_y", d.get("flip", False)))
            if fx or fy:
                img = pygame.transform.flip(img, fx, fy)
            self.deco_items.append({**d, "img": img})
        self.water_strips = [WaterStrip(w, assets) for w in level.water]
        self.thorns = [Thorn(t, assets.clips["thron/wait"], i)
                       for i, t in enumerate(level.thorns)]
        self.monsters: list[Monster] = []
        # 同屏怪物上限：单人 6 只，双人 8 只。
        self.max_monsters = 6
        self.projectiles: list[Projectile] = []
        # 命中身份（去重键，**每个目标各自保存一份**）：
        # 键 = ``(src_ns, attack_id, stage)``，等价于“对象名 + ``attack_id``”的组合
        # 规则（T1 §5.1/§7）：``src_ns`` 是攻击者实例命名空间（英雄 1/2，
        # 怪物、子弹、幻影、地刺各自一份），所以同类怪同计数、两次施法
        # 复用同一个 ``attack_id`` 都不会互相吞伤。去重表由目标自带的
        # ``hit_ids`` 保存，不再维护 world 级 {id(target): ...}（B5-①：id 复用会造成伪去重）。
        # 治疗特效的独立时间线（与英雄动作解耦，见 add_tang_cure）
        self.pending_cures: list[dict] = []
        self.drops: list[Drop] = []
        self.auras = AuraSystem(self)
        self.monster_bullets: list[MonsterBullet] = []
        self.loot: list[dict] = []
        self.loot_uid = 0
        self._bag_full_msg_t = -10.0
        self.spawners: list[Spawner] = []
        self.fx: list[dict] = []
        self.time = 0.0
        # 用真实流逝时间驱动的淡入淡出计时。
        # 与 self.time 不同：冻结计时或暂停期间它仍然继续走。
        self.wall_time = 0.0
        self.freeze_timer = 0.0
        self.gxp_effect: GxpEffect | None = None
        self._logic_acc = 0.0
        self._pending_events: list = []
        self.stops = sorted(level.stops, key=lambda s: s.x)
        self.stop_index = 0
        self.active_stop = None
        self.boss: Monster | None = None
        self.boss_defeated = False
        self.boss_bar = BossBar()
        # 传送门物体在加入物理世界时就以隐藏状态注册，
        # 只有 Boss 死亡动画播完时才会被显式打开。
        self.portal_active = False
        self.portal_clear_timer = 0.0
        self.portal_entered = False
        # 1 秒通关淡出结束时置为 True，且只置一次。
        self.portal_clear_done = False
        # 检查点销毁时弹出的提示图标；可能同时存在多个，
        # 因为这里既不排队也不去重。
        self.gogos: list[GogoHud] = []
        self.finished = False
        self.won = False
        self.transition_target: str | None = None
        self.gongxun = 0
        self.gxp_gain = 0
        self.soul_gain = 0
        self._delayed: list[dict] = []
        self._boss_settle_pending = False
        self._boss_settle_snapshot: list[Monster] = []
        self._boss_settle_trigger: Monster | None = None
        self._boss24_bg_anim: Animation | None = None
        self.messages: list[tuple[str, float]] = []
        self.floating: list[FloatingText] = []
        self.numbers: list[DamageNumber] = []
        # 连击面板与连击计数：数值独立于面板 3.5 秒的淡出，
        # 关卡重置也不会把它清零。
        self.combo = ComboCounter()
        self.shake = 0.0
        self.rng = random.Random(20251018)
        self.player_start = level.player_start

    # ------------------------------------------------------------ 地面绘制
    def _build_ground_art(self) -> None:
        if self.ground_img is None:
            return
        # 保留地面自带的土黄色调；早期把侧面色调成灰色后，
        # 1 关地面相对 images/59.jpg 显得发冷。
        self.ground_side_img = self.ground_img.copy()
        platform_img = None
        if self.level.id.startswith("1-"):
            try:
                platform_img = self.assets.image("bg/platform1.png")
            except (KeyError, FileNotFoundError):
                platform_img = None
        for t in self.level.terrain:
            if platform_img is not None:
                # 58.png 是完整的高台贴图，直接按素材原尺寸
                # 399x116 使用（两侧斜坡已包含在贴图里）。
                top = float(t["y"]) - 30.0
                px = float(t["x"]) + float(t["w"]) / 2 - platform_img.get_width() / 2
                self.terrain_pieces.append((px, top, platform_img))
            else:
                piece = self._ground_slice(t["x"], t["w"],
                                           self.ground_img.get_height(),
                                           self.ground_side_img)
                self.terrain_pieces.append((float(t["x"]), float(t["y"]), piece))
        if not self.level.id.startswith("1-"):
            # 其它关卡仍用地面条带拼出斜坡。
            for slope in self.level.slopes:
                self.slope_pieces.append(self._make_slope_piece(slope))

    def _ground_slice(self, world_x: float, width: float, height: float,
                      source: pygame.Surface | None = None) -> pygame.Surface:
        src = source if source is not None else self.ground_img
        gw, gh = src.get_size()
        w = max(1, int(round(width)))
        h = max(1, min(int(round(height)), gh))
        surf = pygame.Surface((w, h), pygame.SRCALPHA)
        for i in range(w):
            src_x = int(world_x + i) % gw
            surf.blit(src, (i, 0), pygame.Rect(src_x, 0, 1, h))
        return surf

    def _make_slope_piece(self, slope: dict) -> tuple[float, float, pygame.Surface]:
        x0, x1 = float(slope["x0"]), float(slope["x1"])
        y0, y1 = float(slope["y0"]), float(slope["y1"])
        src = self.ground_side_img
        gw, gh = src.get_size()
        w = max(1, int(round(x1 - x0)))
        top = int(round(min(y0, y1)))
        height = int(round(abs(y1 - y0))) + gh
        surf = pygame.Surface((w, height), pygame.SRCALPHA)
        for i in range(w):
            t = i / max(1, w - 1)
            sy = y0 + (y1 - y0) * t
            src_x = int(x0 + i) % gw
            surf.blit(src, (i, int(round(sy - top))),
                      pygame.Rect(src_x, 0, 1, gh))
        return (x0, float(top), surf)

    # ------------------------------------------------------------------ 相机
    @property
    def camera_x(self) -> float:
        return self.camera.x

    @camera_x.setter
    def camera_x(self, value: float) -> None:
        self.camera.x = float(value)

    @property
    def camera_locked(self) -> bool:
        return self.camera.locked

    @property
    def portal_visible(self) -> bool:
        """传送门当前是否可见（加载关卡时注册为隐藏，Boss 死亡后才打开）。"""
        return self.portal_active

    @portal_visible.setter
    def portal_visible(self, visible: bool) -> None:
        self.portal_active = bool(visible)

    @property
    def portal_fade_progress(self) -> float:
        """通关淡出进度：命中传送门时为 0，通关事件触发时为 1。

        进入传送门后，HUD 和关卡画面会在 1 秒内渐暗到全黑，
        这里给出这个 1 秒过渡的完成度；实际绘制放在
        ``World.draw`` 末尾，覆盖 HUD 与世界层。
        """
        if not self.portal_entered:
            return 0.0
        if self.portal_clear_done:
            return 1.0
        return max(0.0, min(1.0, 1.0 - self.portal_clear_timer
                          / PORTAL_FADE_SECONDS))

    @property
    def lock_x(self) -> float:
        return self.camera.lock_x

    @property
    def player_walls(self) -> list[Wall]:
        # 战斗锁屏范围 = 相机停靠点 + 屏幕边界 20/920，
        # 这里不需要再额外加一堵挡住玩家的墙。
        return self.level.walls

    # ------------------------------------------------------------------ 波次
    def alive_count(self) -> int:
        return sum(1 for m in self.monsters if not m.dead and not m.removed)

    def new_hit_ns(self) -> int:
        """分配攻击实例命名空间（相当于给每次攻击一个唯一的对象名）。

        只要求**战斗内**唯一、单调递增，永不复用；跨关复用无妨（去重表随对象
        新建而清空）。见《受击判定与连击保护实现方案》§5.2。
        """
        self._hit_ns += 1
        return self._hit_ns

    def spawn_monster(self, spec: SpawnSpec) -> Monster:
        # 生成点坐标沿用配置值；出生点位置常常刚好在锁定视口外，
        # 这样怪物会自己走进画面。
        #
        # T-6：生成点配置的 y 是 **root 坐标**（21 号怪的 move() 是空实现，
        # 2-1 的 21 号 y=420.1 + colipse_bottom 80.7 = 地面 500.8），
        # 而本实现的 Monster.y 是脚底线 ⇒ 这里统一 + colipse_bottom。
        cb = self._spawn_colipse_bottom(spec.enemy_type)
        x = max(20.0, min(float(spec.x), self.level.width - 20.0))
        m = Monster(spec.enemy_type, x, float(spec.y) + cb,
                    self.assets, self.level, spec.stop_idx,
                    player_level=self.player.level_no)
        m.hit_ns = self.new_hit_ns()
        # 一波怪围绕检查点生成；即使配置位置在视野外，也要让它
        # 真正加入战斗（放宽视野阈值）。
        m.see = max(m.see, abs(self.player.x - x) + 60)
        self.monsters.append(m)
        if m.level_boss:
            self.boss = m
            self.boss_defeated = False
            self.boss_bar.on_spawn(m)
        return m

    @staticmethod
    def _spawn_colipse_bottom(enemy_type: int) -> float:
        """root → 脚底线换算用的 colipse_bottom（data/monsters.json 默认值）。"""
        from .entities import MONSTER_STATS
        return float((MONSTER_STATS.get(int(enemy_type)) or {}).get(
            "colipse_bottom", 0.0) or 0.0)

    def spawn_minion(self, enemy_type: int, x: float, y: float | None = None,
                     summoner: "Monster | None" = None) -> Monster | None:
        if self.alive_count() >= 10:
            # 性能阀：只作用于 19 号怪的召唤（该处没有数量上限，见 T-5）
            return None
        root_y = self.level.ground_y if y is None else float(y)
        m = Monster(enemy_type, x, root_y + self._spawn_colipse_bottom(enemy_type),
                    self.assets, self.level,
                    player_level=self.player.level_no)
        m.hit_ns = self.new_hit_ns()
        m.summoner = summoner
        # 鲨鱼（18 号怪）没有生命周期/自毁逻辑：生命值设为 999999，
        # 由鲨魔王在销毁时一起回收（19 号怪的死亡流程）。
        self.monsters.append(m)
        return m

    def activate_stop(self, stop) -> None:
        self.active_stop = stop
        self.camera.locked = True
        self.spawners = [Spawner(s) for s in self.level.spawns if s.stop_idx == stop.idx]
        self.add_message("妖气来袭！", 1.3)

    def _stop_runtime_index(self, stop) -> int:
        """检查点的运行时序号（按 x 排序后依次编号 0..n-1）。

        工具和测试也会用只带 ``idx`` 的简易对象调用 ``clear_stop``；
        取不到序号时回退到该属性，保证这些调用方仍然可用。
        """
        try:
            return self.stops.index(stop)
        except ValueError:
            idx = int(getattr(stop, "idx", -1))
            return idx if 0 <= idx < len(self.stops) else -1

    def clear_stop(self) -> None:
        stop = self.active_stop
        if stop is None:
            return
        self.active_stop = None
        self.camera.locked = False
        self.stop_index += 1
        self.camera.auto = self.stop_index < len(self.stops)
        self.spawners = []
        runtime_idx = self._stop_runtime_index(stop)
        last = runtime_idx == len(self.stops) - 1
        if not last and 0 <= runtime_idx < 4:
            # 检查点销毁时在 (860,300) 弹出提示图标。
            # 这里刻意不做“继续前进”文字提示：设计上只有这个图标，
            # 没有额外文字。
            self.gogos.append(GogoHud(self))
        # 传送门不会在这里显形：它一直保持隐藏，
        # 直到 Boss 死亡动画播完才会被打开。

    def add_message(self, text: str, ttl: float = 1.4) -> None:
        self.messages.append((text, ttl))
        if len(self.messages) > 5:
            self.messages.pop(0)

    def add_damage_number(self, x: float, y: float, value: int, kind: str = "hurtnum") -> None:
        self.numbers.append(DamageNumber(x, y, int(value), kind))
        if len(self.numbers) > 40:
            self.numbers.pop(0)

    def damage_kind(self, damage: float) -> str:
        reference = max(1, self.player.atk_max)
        return "bnum" if damage >= reference * 1.3 else "hurtnum"

    def add_effect(self, name: str, x: float, y: float, facing: int = 1,
                   ttl: float = 1.2, scale=1.0) -> None:
        clip = self.assets.fx(name)
        if clip is None:
            return
        flip = (facing < 0) if name in RIGHT_AUTHORED_FX else (facing > 0)
        self.fx.append({"name": name, "clip": clip, "x": x, "y": y,
                        "facing": facing, "flip": flip, "t": 0.0,
                        "ttl": ttl, "scale": scale})

    # ------------------------------------------------------------------ 生成子弹
    def spawn_player_bullet(self, player: Player, damage: int, kind: str,
                            attack_id: int, stage: int, action: str,
                            base_power: float = 0.0) -> None:
        """历史路径：唐僧动作里的子弹分支。

        唐僧现在全部走 ``data/actions.json`` 帧事件（:mod:`game.tang`），这条
        路径只剩兼容用途；水魔爆二段已改为 ``hit4_falldown``(657)，不再用
        废弃的 580 号旧弹道。
        """
        if player.hero == "tangseng" and action == "hit2":
            clip, speed, radius = "bullet_role2_1", 520, 26
        elif player.hero == "tangseng" and action == "hit4":
            clip, speed, radius = "hit4_falldown", 180, 60
        else:
            clip, speed, radius = "bullet_role2_2", 620, 14
        self.projectiles.append(Projectile(
            player.x + player.facing * 30, player.y - 62,
            player.facing * speed, -25 if action == "hit1" else 0,
            damage, "player", kind, attack_id, clip, radius, 2.2, stage,
            kb=(6, -5), base_power=base_power,
            hit_ns=self.new_hit_ns()))
        self.assets.play("Role2_hit3" if action != "hit2" else "Role2_hit2", 0.5)

    def spawn_tang_bullet(self, player: Player, spec: dict) -> Projectile:
        """按 ``actions.json`` 的帧事件生成一发唐僧子弹。

        实现在 :mod:`game.tang`（与竞技场共用），这里只做入口。
        """
        projectile = tang.spawn_bullet(self, player, spec)
        sfx = spec.get("sfx")
        if sfx:
            self.assets.play(str(sfx), 0.5)
        return projectile

    def spawn_tang_aoe(self, player: Player) -> Projectile:
        """第三段技能：在施法者脚下生成持续 46 帧的静止冰场。"""
        return tang.spawn_aoe(self, player)

    def spawn_tang_aoe_at(self, x: float, y: float, attack_id: int,
                          source: Player | None = None) -> Projectile:
        return tang.spawn_aoe_at(self, x, y, attack_id,
                                 source=source or self.player)

    def tang_heal(self, player: Player) -> None:
        """治疗判定：自身无条件生效，队友需在 200x200 范围内且不处于竞技场模式。"""
        tang.heal(self, player)

    def add_tang_cure(self, caster: Player, x: float, y: float) -> None:
        """治疗特效的独立时间线：34 帧后才真正结算治疗（R1-02）。

        治疗特效在起手时就挂到场景上，即使英雄被打断也照常
        播到第 34 帧；所以治疗不能绑在英雄的 hit5 动作上。
        """
        self.pending_cures.append({"left": tang.HEAL_DELAY_TICKS,
                                   "caster": caster, "x": float(x),
                                   "y": float(y)})

    def tang_heal_at(self, x: float, y: float, radius: float = 200.0,
                     source: Player | None = None) -> None:
        """幻影治疗范围：200x160（radius 参数仅为保留旧签名，不参与判定）。"""
        tang.heal_at(self, x, y, source or self.player)

    def add_heal_number(self, player: Player, amount: int) -> None:
        """治疗飘字：绿色数字（"bunum"）。"""
        self.add_damage_number(player.x, player.y - player.h - 10, amount, "bunum")

    def detonate_hit4(self, player: Player) -> Projectile | None:
        """技能二段触发：下坠冲击（威力按 8 倍计算）。"""
        return tang.detonate_hit4(self, player)

    # ------------------------------------------------ 子弹命中流程（共用）
    def tang_players(self) -> list[Player]:
        """返回本场景的玩家列表；单机只有主角。"""
        return [self.player]

    def shadow_limits(self) -> tuple[float, float]:
        """移动范围约束：幻影的碰撞椭圆保持在屏幕横向 [20,920] 之内。"""
        half = 22.5                       # 2 号角色碰撞椭圆横向 [-22,23]
        return self.camera.x + 20.0 + half, self.camera.x + 920.0 - half

    @property
    def mode3(self) -> bool:
        """是否处于竞技场模式——单机版恒为 False。"""
        return False

    def bullet_targets(self, projectile: Projectile) -> list:
        """子弹的可命中列表（非竞技场模式时取全部存活怪物）。"""
        return [m for m in self.monsters if not (m.dead or m.removed)]

    def roll_projectile_damage(self, projectile: Projectile) -> tuple[int, float]:
        """命中时重新读取实际威力：暴击每段重掷、GXP 结束立即回落。

        ``damage_owner`` 解析：幻影的子弹按英雄的实际攻击力计算——
        基础攻击力 × 0.8 × 当前 GXP 倍率（R1-01）。
        """
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
        """一次命中结算（不含命中判定）。

        ``register=False`` 表示**强制溅射**：不查重也不写被击方身份（只有
        主目标会登记身份，见 T1 §5.5.2 红线 3），但伤害与 father/无敌等
        数据门照常结算。
        """
        raw, expected = self.roll_projectile_damage(projectile)
        kbx = projectile.kb[0] * (1 if projectile.x <= target.x else -1)
        dmg = target.take_damage(raw, projectile.kind, projectile.attack_id,
                                 kbx=kbx, kby=projectile.kb[1],
                                 stage=projectile.stage,
                                 source_facing=projectile.facing,
                                 src_ns=projectile.hit_ns,
                                 register=register)
        if dmg is None:
            return None
        self.combo.on_hit(self.wall_time)
        self.add_damage_number(target.x, target.y - 60, dmg,
                               self.damage_kind(projectile.damage))
        # 怪物受击结算携带的是**未减防**的实际攻击力；无双槽
        # 以不含暴击的基础攻击力为分母 → 普通命中 +2、暴击 +4（R1-14）。
        owner = projectile.damage_owner or self.player
        owner.add_gxp(raw, expected)
        self.assets.play(self.player.hit_sound, 0.35)
        return dmg

    def apply_bullet_splash(self, projectile: Projectile, main) -> None:
        """溅射伤害：范围内的其他目标各结算一次命中。

        强制结算时**不查重、不登记身份**：只有主目标会被写入被击身份表
        （T1 §5.5.2 红线 3）。所以这里传 ``register=False``：同一 ``attack_id``
        内主目标换人时，溅射目标仍可被再次结算
        （用 ``Monster.take_damage(register=False)`` 表达）。
        """
        reached = int(projectile.attack_range)
        for other in self.bullet_targets(projectile):
            if other is main:
                continue
            if math.hypot(other.x - main.x, other.y - main.y) > reached:
                continue
            self.apply_bullet_hit(projectile, other, register=False)

    def hit10_stage(self, player: Player, key: str,
                    target: Monster | None = None) -> None:
        """终结技三段伤害实体之一（见文档 §8.5）。"""
        table = {
            "hit10-1": (1.0, "physics", (2, -3), 0),
            "hit10-2": (3.0, "magic", (40, -15), 2),
            "hit10-3": (1.0, "magic", (40, 25), 1),
        }
        mult, kind, kb, stage = table[key]
        stage = player.hit10_stage_id(key)
        raw, expected = player.roll_damage(mult)
        if target is None:
            target = player.hit10_target
        if target is not None and (target.dead or target.removed):
            target = None
        if key == "hit10-1":
            if target is None:
                return
            rect = target.hit_rect.inflate(80, 60)
            fx_x, fx_y = target.x, target.y - 45
        elif target is not None:
            # 已抓取目标的后续结算：被抓住的怪物照常受重力/速度影响，
            # 但第三段、第二段伤害仍然落在它身上。
            rect = target.hit_rect.inflate(80, 60)
            fx_x, fx_y = target.x, target.y - 45
        elif key == "hit10-2":
            rect = pygame.Rect(round(player.x - 180), round(player.y - 360), 360, 380)
            fx_x, fx_y = player.x, player.y - 45
        else:
            rect = pygame.Rect(round(player.x - 120), round(player.y - 240), 240, 260)
            fx_x, fx_y = player.x, player.y - 45
        if key == "hit10-2":
            self.shake = max(self.shake, 0.25)
        hit = Hit(0.0, 0.0, mult=mult, kind=kind, kb=kb,
                  hit_max=100, stage=stage)
        self.player_attack_hit(player, int(raw), hit=hit,
                               attack_id=player.attack_id, rect=rect,
                               base_power=expected)
        if key == "hit10-1":
            self.add_effect("hit10_and1", target.x, target.y - 45,
                            player.facing, ttl=44 / 24)
        elif key == "hit10-2":
            # 特效缩放会再乘一次 (0.7,1)：
            # 2 -> 1.4x2.0，3 -> 2.1x3.0。
            self.add_effect("hit10_and2", fx_x, fx_y,
                            player.facing, ttl=13 / 24,
                            scale=(2.1, 3.0) if player.is_gxp else (1.4, 2.0))
        else:
            self.add_effect("hit10_flush", fx_x, fx_y,
                            player.facing, ttl=2 / 24)
        self.assets.play("Role1_hit10_2" if key == "hit10-1"
                         else "Role1_hit10_3" if key == "hit10-3"
                         else "Role1_hit10_2", 0.7)

    def hit10_flush(self, player: Player) -> None:
        """第 47 帧：抓住目标则打第三段，落空则发出一发剑气子弹。"""
        player.last_hit = "hit10-3"
        target = player.hit10_target
        if target is not None and not target.dead and not target.removed:
            self.hit10_stage(player, "hit10-3", target=target)
            return
        raw, expected = player.roll_damage(1.0)
        b = Projectile(
            player.x, player.y - 45, 0.0, 0.0, raw, "player", "magic",
            player.attack_id, "hit10_flush", radius=76, life=2 / 24,
            stage=player.hit10_stage_id("hit10-3"), kb=(40, 25),
            base_power=expected,
            hit_max=100, interval=0, distance=None,
            hit_ns=self.new_hit_ns(),
        )
        b.facing = player.facing
        self.projectiles.append(b)
        self.assets.play("Role1_hit10_3", 0.7)

    def hit10_grab(self, player: Player) -> bool:
        """抓取判定：取屏幕窗口内第一个可抓的怪物。

        横向用碰撞椭圆包围盒的屏幕坐标与 ``(20, 920)`` 比较，距离则用
        *根坐标* 与 130 比较。早期实现误用世界坐标，导致 4-1 的 Boss
        （x >= 1350）永远抓不到，整条空中终结技链条都空掉，目标因此
        脱离连击（v2 #9）。
        """
        import math as _math
        cam = self.camera_x
        for m in self.monsters:
            if m.dead or m.removed:
                continue
            # 碰撞椭圆轴对齐包围盒左边缘的屏幕坐标。
            if not (20 < m.hit_rect.left - cam < 920):
                continue
            dx = m.x - player.x
            dy = m._root_y() - player.root_y
            if _math.hypot(dx, dy) > 130:
                continue
            self.hit10_stage(player, "hit10-1", target=m)
            player.x, player.y = m.x, m.y
            player.last_hit = "hit10-1"
            player.timers = 40
            player.hit10_target = m
            return True
        return False

    def hit10_land(self, player: Player) -> None:
        if player.last_hit == "hit10-2":
            return
        self.hit10_stage(player, "hit10-2")
        player.last_hit = "hit10-2"

    def spawn_monster_bullet(self, monster: Monster, spec: dict,
                             x: float | None = None,
                             y: float | None = None) -> MonsterBullet:
        """根据数据驱动的动作配置（A.4）创建怪物弹丸。

        弹丸的 ``x/y`` 以怪物根节点的原点为基准，而 ``Monster.y`` 记录的是
        脚底线位置，因此这里统一减去当前动作的 ``colipse_bottom`` 换算成
        根节点坐标；``offset`` 也一律按该坐标系书写。
        """
        if x is None or y is None:
            offset = spec.get("offset") or [0, 0]
            root_y = monster._root_y()
            # 受击特效的 y 偏移随朝向变化（朝左时 -30，否则 +30），
            # 由配置项 offset_y_mul_facing 表达。
            if "y_offset" in spec:
                oy = float(spec["y_offset"])
            else:
                oy = float(offset[1]) if offset else 0.0
                if spec.get("offset_y_mul_facing"):
                    oy *= monster.facing
            if spec.get("target_offset"):
                tx, ty = spec["target_offset"]
                x = self.player.x + float(tx)
                if ty:
                    # y 相对目标点计算：在目标 y 的基础上叠加偏移。
                    y = self.player.y + float(ty)
                else:
                    y = root_y + oy
            else:
                facing = monster.facing
                # 攻击型怪物开火时，x 在 30..230 之间随机并取朝向一侧；
                # 蛋态没有开火子对象，因此改为前后 ±100、根节点上方
                # 100..250 之间随机。
                if "x_random_facing" in spec:
                    lo, hi = spec["x_random_facing"]
                    dx = random.uniform(float(lo), float(hi)) * facing
                elif "x_random" in spec:
                    lo, hi = spec["x_random"]
                    dx = random.uniform(float(lo), float(hi))
                else:
                    dx = facing * float(offset[0])
                x = monster.x + dx
                if "y_random" in spec:
                    lo, hi = spec["y_random"]
                    y = root_y + random.uniform(float(lo), float(hi))
                else:
                    y = root_y + oy
        # B19-F2b：标记 no_mirror 的子弹按世界坐标绘制、不参与朝向翻转。
        # 预警水圈是没有角色属性的普通显示对象，从不做水平镜像，所以恒居中；
        # 而后继的追踪弹带有角色属性，会随变换矩阵的水平缩放一起翻转
        # （这里保留镜像效果）。
        facing = 0 if spec.get("no_mirror") else monster.facing
        bullet = MonsterBullet(monster, spec, x, y, facing=facing,
                               hit_ns=self.new_hit_ns())
        self.monster_bullets.append(bullet)
        return bullet

    # ------------------------------------------------------------------ 死亡
    def on_monster_death(self, monster: Monster) -> None:
        # 消灭鲨魔王时，它召唤出的鲨鱼（类型 18）随之淡出移除，
        # 而不是靠自身 10 s 自爆；该移除流程只做透明度渐变和显示对象摘除，
        # 不结算经验与掉落。
        if monster.type == 19:
            for m in list(self.monsters):
                if m.type == 18 and m.summoner is monster and not m.dead:
                    m.despawn()
        # 蛋态死亡（类型 9）与最终 BOSS（类型 24）的死亡流程都不发放经验，
        # 因此这里跳过经验结算。
        if monster.type not in (9, 24):
            if self.player.gain_exp(monster.exp):
                self.add_message(f"升级！Lv.{self.player.level_no}", 2.2)
            # 经验结算只更新存档数据与界面文本，经验文本由界面刷新时读取；
            # 这里不会出现经验飘字，也绝不要在此处追加任何伤害数字。
        if monster.level_boss:
            self.boss_defeated = True
            self.add_message(f"{monster.name}被击败！", 2.0)
            self.shake = 0.35
            if self.level.id == "4-1":
                if monster.type == 22:
                    self._spawn_chain(23, 1500, 450)
                elif monster.type == 23:
                    self._spawn_chain(24, 1350, 300)
                elif monster.type == 24:
                    self._win_level()
            # 传送门由 _check_boss_destroy() 在死亡动画尾帧触发移除时才揭示
            # （约 20-25 帧之后），而不是在这个血量归零的通知时刻。
        if monster.type == 27:
            try:
                stage, sub = self.level.id.split("-")
                total = (int(stage) - 1) * 3 + int(sub) if int(stage) < 4 else 10
            except (ValueError, AttributeError):
                total = 10
            refresh_hou_dong(total)
        if monster.type == 24:
            # 最终 BOSS 的死亡结算：尚未持有时掉落金钢琢，并固定奖励 50 战功。
            owned = any((item or {}).get("fill") == "jgz"
                        for item in self.inventory + list(self.player.equipment.values()))
            if not owned:
                if len(self.inventory) < MAX_BAG:
                    self.loot_uid += 1
                    item = random_drop("jgz", self.player.hero, uid=self.loot_uid)
                    if item:
                        self.inventory.append(item)
                        self.add_message("获得 金钢琢", 2.0)
                else:
                    self.add_message("背包已满，无法获得 金刚琢", 2.0)
            self.gongxun += 50
            self.add_message("获得 战功 + 50", 2.0)
        # 光环、药品与装备由死亡动画尾段的 ``drop`` 指令经 ``drop_on_death``
        # 触发；若放在这里结算会提前最多 1.7 s（部分怪物的死亡动画长达 40 帧）。
        # BOSS 的漩涡结算同样由 ``boss_dead`` 指令调度。

    def _check_boss_destroy(self, monster: Monster) -> None:
        """在 BOSS 死亡动画尾帧揭示传送门。

        揭示时机对应 BOSS 死亡动画的最后一帧（或 ``boss_dead`` 指令）：
        ``Monster`` 已在 ``destroy`` 指令里置好 ``_death_destroyed``、在
        ``boss_dead`` 指令里置好 ``_boss_dead_fired``，因此这里用轮询判定，
        既不需要改动实体模块，也只比指令晚一帧。
        """
        if not monster.level_boss or self.level.portal is None:
            return
        if (getattr(monster, "_death_destroyed", False)
                or getattr(monster, "_boss_dead_fired", False)):
            self.portal_active = True        # 所有传送门都设为可见

    def drop_on_death(self, monster: Monster) -> None:
        """死亡掉落结算：先处理药品与装备，再处理光球。

        守卫判断位于药品与装备结算之后，因此没有攻击目标的死亡仍会
        掉落药水与装备，只是不会出现光球。
        """
        if getattr(monster, "_death_dropped", False):
            return
        monster._death_dropped = True
        self._roll_drop(monster)
        if monster.target is None:
            return
        self.auras.drop(monster)

    def sync_player_progress(self, player=None) -> None:
        """同步角色进度：等级、经验与生命/魔法值实时写入存档数据。"""
        if self.soul_data is None:
            return
        player = player or self.player
        data = self.soul_data
        pdata = data.get("player")
        if not isinstance(pdata, dict):
            pdata = {}
            data["player"] = pdata
        pdata["level"] = int(player.level_no)
        pdata["exp"] = int(player.exp)
        # 新一次挑战从满生命/满魔法开始（清档流程同样如此）；无双槽
        # （player.gxp）只在战斗内有效，从不写进存档。
        pdata["hp"] = int(player.max_hp)
        pdata["max_hp"] = int(player.max_hp)
        pdata["mp"] = int(player.max_mp)
        pdata["max_mp"] = int(player.max_mp)

    def apply_aura(self, aura) -> None:
        """吸收光球：按类型增加灵魂、生命/魔法值或无双槽。"""
        target = aura.target
        if target is None:
            return
        if aura.kind == "soul":
            # 灵魂光球的数量同时计入灵魂总数与隐藏分数；背包界面读取的是
            # 实时存档字典，因此在拾取时立即累加。
            self.soul_gain += aura.power
            if self.soul_data is not None:
                self.soul_data["soul"] = (int(self.soul_data.get("soul", 0))
                                          + int(aura.power))
        elif aura.kind == "hp":
            if not target.dead:
                target.hp = min(target.max_hp, target.hp + aura.power)
        elif aura.kind == "mp":
            if not target.dead:
                target.mp = min(target.max_mp, target.mp + aura.power)
        elif aura.kind == "ws" and target.gxp + 5 <= 100:
            target.gxp += 5

    def delay(self, seconds: float, callback) -> None:
        """供 BOSS 死亡漩涡使用的小型定时队列（12 帧 = 0.5 s）。"""
        self._delayed.append({"t": float(seconds), "fn": callback})

    def spawn_boss_dead(self, boss: Monster) -> None:
        """BOSS 死亡特效：先播放 12 帧漩涡动画，随后进行 BOSS 死亡结算。"""
        self.add_effect("boss_dead", boss.x, boss.y - boss.h / 2,
                        facing=-1, ttl=12 / 24)
        if self._boss_settle_pending:
            return
        self._boss_settle_pending = True
        # 保留一份快照：尸体计时器不能在第 12 帧之前移除发起漩涡的 BOSS
        # （尸体本身只存在到第 25 帧）。
        self._boss_settle_trigger = boss
        self._boss_settle_snapshot = [m for m in self.monsters if m.is_boss]
        self.delay(12 / 24, self._settle_bosses_at_vortex)

    def _settle_bosses_at_vortex(self) -> None:
        """漩涡第 12 帧：对场上 BOSS 执行掉落结算并移除。

        结算会遍历场上全部 BOSS，但由死亡连段召唤出来的 BOSS 除外——
        它们只由自己的漩涡收尾，不会被上一个 BOSS 的第 12 帧带走，
        这样 4-1 的连续召唤才能依次登场。
        """
        self._boss_settle_pending = False
        trigger = self._boss_settle_trigger
        self._boss_settle_trigger = None
        bosses: dict[int, Monster] = {
            id(m): m for m in self._boss_settle_snapshot}
        for monster in self.monsters:
            if monster.is_boss:
                bosses[id(monster)] = monster
        self._boss_settle_snapshot = []
        for boss in bosses.values():
            if boss is not trigger and boss.chain_spawned:
                continue
            # 即使尸体计时器已经移除了本体，第 12 帧仍要在它最后的位置执行掉落结算。
            self.drop_on_death(boss)
            boss.removed = True

    def _spawn_chain(self, enemy_type: int, x: float, y: float) -> None:
        # D-15：召唤函数的 y 是 **根节点** 坐标（例如 (24, 1350, 300) 与
        # (23, 1500, 450)），而 Monster.y 记录的是脚底线 ⇒ 统一加上
        # colipse_bottom 换算（与 spawn_monster 的 T-6/B-21 换算一致）。
        # **不做地面夹取**：最终 BOSS 的移动是空实现，脚底 520.978 低于
        # 4-1 地面 506.6，这是有意保留的结果。
        feet_y = float(y) + self._spawn_colipse_bottom(enemy_type)
        m = Monster(enemy_type, max(80.0, min(x, self.level.width - 80.0)),
                    feet_y, self.assets, self.level,
                    player_level=self.player.level_no)
        m.hit_ns = self.new_hit_ns()
        m.y = feet_y
        m.see = max(m.see, abs(self.player.x - m.x) + 60)
        m.level_boss = True
        m.chain_spawned = True
        self.monsters.append(m)
        self.boss = m
        self.boss_defeated = False
        self.boss_bar.on_spawn(m)
        self.add_message(f"{m.name}出现了！", 1.6)
        self.shake = 0.3

    def _win_level(self) -> None:
        self.finished = True
        self.won = True
        # 只有切换到关卡地图时才播放开场音乐；普通关卡通关时先停掉战斗
        # 背景音乐，再进入地图曲目。4-1 是例外：通关后仍让第 4 关背景音乐
        # 继续播放到结算演出结束，直到切到关卡地图才换成开场音乐。
        if self.level.id != "4-1":
            self.assets.stop_music()

    def _roll_medicine(self, monster: Monster, rng=None) -> None:
        """药品掉落判定：每次死亡最多掉落一瓶药水。

        判定树按固定顺序逐步执行，以保证随机数调用次数与边界方向稳定：

          ① 第一次随机数 >= 0.5 → 进入生命药分支；
          ② 紧接着的第二次随机数同时用于 0.1 与 0.05 两个阈值；
          ③ 落在 ≤0.05 区间时，再用一次 50/50 随机选择小生命药或大生命药；
          ④ 否则用第二次随机数 <= 0.1 判定小魔法药。

        ``kind`` 只有一个槽位，因此同一次死亡不会同时掉落生命药与魔法药。
        """
        rng = self.rng if rng is None else rng
        kind = None
        if rng.random() >= 0.5:              # ① 50%：生命药分支
            r = rng.random()                 # ② 两个阈值共用同一个随机数
            if r <= 0.1:
                if r <= 0.05:
                    kind = ("small_hp" if rng.random() >= 0.5
                            else "big_hp")   # ③ 各占 50%
                else:
                    kind = "small_hp"        # 0.05 < r <= 0.1：小生命药
        elif rng.random() <= 0.1:            # ④ 另一半的 50% × 10%：小魔法药
            kind = "small_mp"
        if kind is None:
            return
        rel, _amount, _number_kind = MEDICINE[kind]
        try:
            icon_h = self.assets.image(rel).get_height()
        except (KeyError, FileNotFoundError):
            icon_h = 40                      # 资源缺失时的兜底高度
        self.drops.append(Drop(monster.x,
                               monster.y - icon_h + DROP_DRAW_CENTER_DY,
                               {}, medicine=kind))

    def _roll_drop(self, monster: Monster) -> None:
        # 先判定药品，再判定装备：两个相互独立的随机决策。
        self._roll_medicine(monster)
        falls = list(monster.fallList or [])
        chance = float(monster.probability or 0.0)
        # 该 BOSS 的专属旗帜在尚未持有时必定掉落，已持有时改用按等级
        # 区分的通用掉落表。
        if monster.type == 26:
            owned = any((item or {}).get("fill") == "dhqf"
                        for item in self.inventory + list(self.player.equipment.values()))
            if owned:
                high = self.player.level_no > 20
                falls = ["qxsh", "jhcz", "ryjgb"] if high else [
                    "zqj", "qld", "bhz", "xwj", "qlp"]
                chance = 0.1 if high else 0.2
            else:
                falls = ["dhqf"]
                chance = 1.0
        if not falls or self.rng.random() >= chance:
            return
        fill = self.rng.choice(falls)
        self.loot_uid += 1
        item = random_drop(fill, self.player.hero, uid=self.loot_uid)
        if item:
            # 掉落物横向限制在可见区域内（世界坐标 x 50..890）。
            dx = max(self.camera_x + 50, min(self.camera_x + 890, monster.x))
            self.drops.append(Drop(dx, monster.y - 40, item))
            self.add_message(f"掉落：{item['name']}", 1.4)

    def _pickup_drops(self) -> None:
        for d in list(self.drops):
            if abs(d.x - self.player.x) <= 54 and abs(d.y - self.player.y) <= 100:
                if d.medicine:
                    self.drops.remove(d)
                    _rel, amount, number_kind = MEDICINE[d.medicine]
                    if d.medicine in ("small_hp", "big_hp"):
                        self.player.hp = min(self.player.max_hp,
                                             self.player.hp + amount)
                        self.add_message(f"回复 {amount} 生命", 1.0)
                    else:
                        self.player.mp = min(self.player.max_mp,
                                             self.player.mp + amount)
                        self.add_message(f"回复 {amount} 魔法", 1.0)
                    # 回复飘字显示的是药水标称数值（100/200/100），
                    # 而不是被上限截断后的实际变化量。
                    self.add_damage_number(self.player.x,
                                           self.player.y - self.player.h - 10,
                                           amount, number_kind)
                    continue
                if len(self.inventory) >= MAX_BAG:
                    if self.time - self._bag_full_msg_t > 1.5:
                        self.add_message("背包已满", 1.0)
                        self._bag_full_msg_t = self.time
                    continue
                self.drops.remove(d)
                self.inventory.append(d.item)
                # 这里保留一份本局战斗内的拾取记录，供工具与测试使用；
                # 存档中持有的是同一个物品实例。
                self.loot.append(d.item)
                self.add_message(f"拾取：{d.item['name']}", 1.2)

    # ------------------------------------------------------------------ 战斗
    def _shoot_down_bullets(self, rect: pygame.Rect | None) -> bool:
        """普攻顺带击落子弹的分支。

        判定条件：可被击落的子弹包围盒与英雄 ``body.stick`` 包围盒相交，
        与英雄当前释放的是哪一招无关；命中后先把子弹置为失效并做 1 s
        透明度淡出，淡出结束后才真正移除。
        """
        if rect is None or not self.monster_bullets:
            return False
        hit_any = False
        for b in self.monster_bullets:
            if getattr(b, "dead", False) or getattr(b, "disabled", False):
                continue
            if not getattr(b, "can_be_hit", False):
                continue
            box_of = getattr(b, "rect", None)
            if callable(box_of):
                b_rect = box_of()
            else:
                # 兼容只有位置的旧/桩对象（tools 探针、tests stub）。
                b_rect = pygame.Rect(round(getattr(b, "x", 0.0)),
                                     round(getattr(b, "y", 0.0)), 1, 1)
            if not rect.colliderect(b_rect):
                continue
            shot = getattr(b, "shot_down", None)
            if callable(shot):
                newly = bool(shot())
            else:
                newly = not getattr(b, "dead", False)
                b.dead = True
            if newly:
                hit_any = True
                self.add_effect("monster_hurt2", b.x, b.y)
        return hit_any

    def player_attack_hit(self, player: Player, base_damage: int,
                          hit: Hit | None = None, attack_id: int = 0,
                          aoe: float = 0.0, base_power: float = 0.0,
                          rect: pygame.Rect | None = None) -> None:
        if rect is None:
            # T15 B-2：用逐帧武器框（英雄 body.stick）与受击椭圆相交判定，
            # 而不是固定 96×108 矩形；没有逐帧数据时才回退到估算矩形。
            rect = player.stick_rect_now or player.attack_rect(aoe)
        hit_any = False
        for m in self.monsters:
            if m.dead or m.removed:
                continue
            if rect.colliderect(m.hit_rect):
                kbx, kby = hit.kb if hit is not None else (0, 0)
                if kbx:
                    # 击退方向：hit9 是唯一按目标相对方向计算的动作，
                    # 其余直接命中都沿用英雄朝向。
                    if player.frame_action.name == "hit9":
                        kbx *= 1 if player.x < m.x else -1
                    else:
                        kbx *= player.facing
                dmg = m.take_damage(base_damage, hit.kind if hit else "physics",
                                    attack_id, kbx=kbx, kby=kby,
                                    stage=hit.stage if hit else 0,
                                    hit_max=hit.hit_max if hit else 1,
                                    source_facing=player.facing,
                                    source_x=player.x,
                                    src_ns=player.hit_ns)
                if dmg is None:
                    continue
                hit_any = True
                # 每命中一只怪物连击数才 +1（按怪物计，不按挥击计）。
                self.combo.on_hit(self.wall_time)
                self.add_damage_number(m.x, m.y - 60, dmg, self.damage_kind(dmg))
                self.add_effect("monster_hurt2" if dmg >= player.atk_max else "monster_hurt1",
                                m.x, m.y - m.h / 2)
                self.assets.play(player.hit_sound, 0.35)
                player.add_gxp(dmg, base_power or base_damage)
        # 特定怪物的投射物可以被击落。判据是"子弹包围盒 × 英雄 body.stick
        # 包围盒"，与招数无关；逐帧的 _shoot_down_bullets() 还负责远程/法术招
        # （spawn_player_bullet 路径），这里保留一次同样的判定，保证直接调用
        # player_attack_hit 的路径（近战帧、hit10 结算、工具探针）也能打掉子弹。
        if self._shoot_down_bullets(rect):
            hit_any = True
        if hit_any:
            # 打掉子弹仍会带来轻微的命中震屏，但不影响连击计数
            # （击落子弹的分支不经过连击系统）。
            self.shake = max(self.shake, 0.06)

    # ------------------------------------------------------------------ 更新
    def update(self, dt: float, keys, events) -> None:
        """累计真实时间，并以固定 24 Hz 步长推进逻辑。

        模拟固定按 24 帧/秒推进，所有以帧为单位的数值（``timers``、
        ``attack_id`` 窗口、重力 1.5 px/帧、跳跃 -20 px/帧）才能保持精确，
        而不受实际帧率影响。
        """
        self.wall_time += dt
        if self.finished:
            self.combo.update(self.wall_time)
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
        # 淡入淡出与飘字按真实秒数推进（走过渡时间轴），不跟随 24 Hz 逻辑
        # 时钟，并且在 freeze_timer 期间仍继续运行。
        self.combo.update(self.wall_time)

    def _step(self, dt: float, keys, events) -> None:
        if self.finished:
            return
        self.time += dt
        self.shake = max(0.0, self.shake - dt)
        if self.freeze_timer > 0:
            # 冻结约 1000 毫秒：HUD、英雄、怪物、子弹全部停住，
            # 但挂在全局状态上的特效仍会继续播放。
            self.freeze_timer = max(0.0, self.freeze_timer - dt)
            if self.freeze_timer < 1e-9:
                self.freeze_timer = 0.0
            if self.gxp_effect is not None:
                self.gxp_effect.update(dt)
                if self.gxp_effect.done:
                    self.gxp_effect = None
            return

        # 全局逻辑在怪物/英雄更新之前推进，且只在未暂停时执行。
        # 连击计数每 20 帧重置一次的处理就写在这里。
        self.combo.logic_tick()
        # 怪物基类的每帧逻辑：Boss 血条读取上一帧（24 Hz）结束时的剩余血量，
        # 时机在本帧怪物/英雄更新之前；全局只有一份 Boss 血量槽，
        # 所有 Boss 共用这份数据。
        self.boss_bar.update(self.boss)

        for e in events:
            if e.type == pygame.KEYUP:
                key = e.key
                p = self.player
                # 英雄松键处理：S/J/K/W 任意一个松开都会恢复可接下一招。
                if key in (pygame.K_s, pygame.K_j, pygame.K_k, pygame.K_w):
                    p.can_next = True
                if key == pygame.K_j:
                    # 松键时只播放身体动作，真正的判定帧要到下一帧才执行。
                    p.request_tang_release()
                if key in (pygame.K_a, pygame.K_d):
                    p.double_count = 0
                    p.running = False
                continue
            if e.type != pygame.KEYDOWN:
                continue
            key = e.key
            p = self.player
            if getattr(p, "action_locked", False):
                continue
            if key == pygame.K_k:
                support = getattr(p, "support_wall", None)
                if keys[pygame.K_s] and support is not None and support.kind == "oneway":
                    # 组合键 S+K 触发下落：把身体向下推 10 像素，
                    # 使其不再与单向平台的顶边相交。
                    if p.drop_timer <= 0:
                        p.drop_timer = 0.30
                        p.y += 10.0
                        p.on_ground = False
                        p.jumps = 1
                        p.can_next = False
                        p.state = "jump"
                        p.anim.play(p._clip("jump1"), loop=False, restart=True)
                else:
                    p.jump()
            elif key == pygame.K_j:
                if p.hero == "wukong" and keys[pygame.K_w]:
                    if p.learned(0):
                        msg = self._cast(p, 0)
                        if not msg and p.state not in ("attack", "skill"):
                            p.normal_attack()
                else:
                    p.normal_attack()
            elif key == pygame.K_SPACE:
                msg = p.activate_gxp()
                if msg:
                    self.add_message(msg, 1.0)
                    self.gxp_effect = GxpEffect(self.assets, p.hero, owner=p)
                    self.freeze_timer = 1.0
                    # 变身与暂停在同一个按键帧内完成；
                    # 冻结前不要再执行一次常规的英雄/怪物更新。
                    return
            elif key == pygame.K_a:
                p.tap_direction(-1, self.time)
            elif key == pygame.K_d:
                p.tap_direction(1, self.time)
            elif key == pygame.K_s:
                # 只有 S+K 组合才能从单向平台向下穿过；
                # 单独按 S 不做任何事。
                pass
            elif key == pygame.K_w or key == pygame.K_UP:
                # 只有单独按 W/↑ 才算有效输入，其他动作键不能同时按下。
                if not self._try_dungeon() and self._portal_key_only(keys):
                    self.try_portal()
            else:
                mapping = {
                    "wukong": {pygame.K_u: 2, pygame.K_i: 3, pygame.K_o: 4},
                    "tangseng": {pygame.K_u: 1, pygame.K_i: 2,
                                 pygame.K_o: 3, pygame.K_l: 4},
                }[p.hero]
                if key in mapping:
                    self._cast(p, mapping[key])

        # 唐僧长按 J 的蓄力由 data/actions.json 里的判定帧事件驱动，
        # 不使用自动施法定时器。

        # 每帧更新顺序：怪物、英雄、镜头、场景交互，顺序不可调换。
        for m in list(self.monsters):
            m.update(dt, self.player, self)
            # Boss 死亡动画播到最后一帧时，
            # 会点亮所有已登记的传送门。
            if m.level_boss:
                self._check_boss_destroy(m)
        self.monsters = [m for m in self.monsters if not m.removed]
        for b in list(self.monster_bullets):
            b.update(dt, self)
        self.monster_bullets = [b for b in self.monster_bullets if not b.dead]
        self.auras.update(dt)
        self.player.update(dt, keys, self)
        # 怪物受击分支里"打掉可击落子弹"的处理每帧都要对英雄的武器判定区
        # 检测一次（远程与法术攻击同样覆盖，见 _shoot_down_bullets）。
        self._shoot_down_bullets(self.player.stick_rect_now)
        if self.player.dead and not self.player._dead_fx:
            # 英雄死亡：在 (x, y-10) 处播放 deadmb 特效，并立即隐藏英雄不再参与更新。
            self.player._dead_fx = True
            self.player.hidden = True
            self.add_effect("deadmb", self.player.x, self.player.y - 10,
                            self.player.facing, ttl=4.0)
        # 英雄碰撞椭圆与副本入口区域一重叠就自动进入 1-2 副本，
        # 不需要按键触发。
        if not self.finished and getattr(self.level, "dungeon", None):
            self._try_dungeon()
            if self.finished:
                return

        pending = None
        if self.active_stop is None and self.stop_index < len(self.stops):
            pending = self.stops[self.stop_index]
        triggered = self.camera.update(dt, self.player, self.active_stop, pending)
        if triggered is not None:
            self.activate_stop(triggered)
        # 关卡内可移动范围：英雄碰撞椭圆的左右边缘保持在画面 x [20,920] 内。
        # 镜头锁定后，仅凭这个范围就形成战斗区域的"空气墙"，
        # 无需再添加额外的阻挡墙。
        half = self.player.w / 2.0
        self.player.x = max(self.camera.x + 20.0 + half,
                            min(self.player.x, self.camera.x + 920.0 - half))
        self.player.x = max(12.0, min(self.player.x, self.level.width - 20.0))

        for t in self.thorns:
            t.update(dt, self)
        for w in self.water_strips:
            w.update(dt)

        for sp in list(self.spawners):
            sp.update(dt, self)
        self.spawners = [s for s in self.spawners if not s.done]

        for p in list(self.projectiles):
            p.update(dt, self)
        self.projectiles = [p for p in self.projectiles if not p.dead]

        for f in self.fx:
            f["t"] += dt
        self.fx = [f for f in self.fx if f["t"] < f["ttl"]]
        for cure in list(self.pending_cures):
            if cure["left"] <= 0:
                self.pending_cures.remove(cure)
                tang.cure_role(self, cure["caster"], cure["x"], cure["y"])
            else:
                cure["left"] -= 1
        for item in list(self._delayed):
            item["t"] -= dt
            if item["t"] <= 0:
                self._delayed.remove(item)
                item["fn"]()
        if self.gxp_effect is not None:
            self.gxp_effect.update(dt)
            if self.gxp_effect.done:
                self.gxp_effect = None

        for d in list(self.drops):
            d.update(dt, self.level.ground_y, self.level.surface_at)
            if d.ttl <= 0:
                self.drops.remove(d)
        self._pickup_drops()

        # 过场动画与游戏逻辑共用同一个固定 24 Hz 时钟，
        # 因此暂停世界时动画标签也一并暂停。它们必须在清场之前推进，
        # 这样本帧新建的标签会先保持在第 1 帧，直到下一帧才继续
        # （新标签在进入帧事件里创建，第 1 帧会先渲染一次，
        # 之后时间轴才前进）。
        for g in list(self.gogos):
            g.update(dt)
        self.gogos = [g for g in self.gogos if g.alive]

        if self.active_stop is not None:
            if not self.spawners and self.alive_count() == 0:
                self.clear_stop()

        for n in self.numbers:
            n.update(dt)
        self.numbers = [n for n in self.numbers if n.ttl > 0]

        for f in self.floating:
            f.update(dt)
        self.floating = [f for f in self.floating if f.alive]

        for i, (text, ttl) in enumerate(self.messages):
            self.messages[i] = (text, ttl - dt)
        self.messages = [(t, ttl) for t, ttl in self.messages if ttl > 0]

        if self.player.dead and self.player.death_timer <= 0:
            self.finished = True
            self.won = False

        # 通关成功：先对全局状态与场景做 1 秒的透明度过渡，再进入过关结算。
        # 淡出期间 finished 保持 False，
        # 且过关结算只触发一次（由 portal_clear_done 保证）。
        if self.portal_entered and not self.finished and not self.portal_clear_done:
            self.portal_clear_timer = max(0.0, self.portal_clear_timer - dt)
            # 24 次 1/24 秒累加会留下约 3.7e-16 的浮点残差，
            # 所以一秒过渡在剩余时间低于 1 微秒时就视为结束。
            if self.portal_clear_timer <= 1e-6:
                self.portal_clear_done = True
                self._win_level()

    def _cast(self, player: Player, index: int, force: bool = False) -> None:
        if force and player.state in ("attack", "skill"):
            player.state = "idle"
            player.attack = None
            player.anim.play(player._clip("wait"), loop=True, restart=True)
        msg = player.cast_active(index)
        if getattr(player, "hit4_detonate_requested", False):
            self.detonate_hit4(player)
            msg = "水魔爆·引爆"
        if msg:
            self.add_message(msg, 0.7)
        return msg

    def _try_dungeon(self) -> bool:
        door = getattr(self.level, "dungeon", None)
        if not door:
            return False
        p = self.player
        if abs(p.x - door["x"]) > 600 or abs(p.y - door["y"]) > 200:
            return False
        # 副本入口判定：用英雄碰撞椭圆与 500x50 的入口矩形做相交检测。
        fb = pygame.Rect(round(door["x"]), round(door["y"]),
                         round(door.get("w", 500)), round(door.get("h", 50)))
        body = pygame.Rect(round(p.x - p.w / 2), round(p.y - p.h),
                           round(p.w), round(p.h))
        if not body.colliderect(fb):
            return False
        self.transition_target = door.get("target", "9-1")
        self.finished = True
        self.won = True
        self.add_message("进入水晶宫……", 1.0)
        self.assets.stop_music()
        return True

    def portal_door_rect(self) -> pygame.Rect:
        """传送门矩形在世界坐标中的位置与尺寸（186x165）。"""
        p = self.level.portal
        if not p:
            return pygame.Rect(0, 0, 0, 0)
        return pygame.Rect(round(float(p["x"]) + DOOR_LEFT),
                           round(float(p["y"]) + DOOR_TOP), DOOR_W, DOOR_H)

    @staticmethod
    def _portal_key_only(keys) -> bool:
        for code in PORTAL_ACTION_KEYS:
            try:
                if keys[code]:
                    return False
            except (KeyError, IndexError):
                pass
        return True

    def try_portal(self) -> None:
        """检测可见的传送门是否与英雄碰撞椭圆相交。"""
        if (not self.portal_active or self.finished or self.portal_entered
                or not self.level.portal):
            return
        # 186x165 的门矩形与 45x90 的英雄身体做矩形相交判定。
        if not self.portal_door_rect().colliderect(self.player.body):
            return
        # 进门后播放一秒透明度淡出，然后进入过关结算。
        self.portal_entered = True
        self.portal_clear_timer = PORTAL_FADE_SECONDS
        self.assets.stop_music()

    def actor_draw_sequence(self):
        """绘制顺序：先画英雄，再按出场顺序画怪物。

        编号 21 的怪物在插入时被放在英雄之前，因此绘制在英雄后面；
        其余怪物都追加在英雄之后。
        """
        behind = [m for m in self.monsters if m.type == 21]
        front = [m for m in self.monsters if m.type != 21]
        return behind, self.player, front

    # ------------------------------------------------------------------ 绘制
    def draw(self, surface: pygame.Surface) -> None:
        # 远景地面：一张 1440x690 的图片按视差公式平移，
        # 不做平铺重复绘制。
        far = self.assets.image(self.level.far_background)
        span = max(1.0, self.level.width - WIDTH)
        parallax = max(0.0, (far.get_width() - WIDTH) / span)
        surface.blit(far, (round(-self.camera_x * parallax), 0))

        # 背景画布：整张 5238x775，自身原点在画布坐标 (-215,-137)，
        # 镜头为 0 时对应的世界绘制点为 (-245,-72)。
        bg = self.assets.image(self.level.background)
        bg_x = round(-self.camera_x + self.level.background_x_offset)
        bg_y = round(self.level.background_y)
        bb = getattr(self.level, "background_bbox", None)
        if bb:
            area = pygame.Rect(round(bb[0]), round(bb[1]),
                               round(bb[2]), round(bb[3]))
            surface.blit(bg, (bg_x + area.x, bg_y + area.y), area=area)
        else:
            surface.blit(bg, (bg_x, bg_y))

        # 场景元素按图层深度排序绘制：地面、平台/雕像/瀑布等装饰、
        # 水面、尖刺。
        items: list[tuple[int, str, object]] = []
        for d in self.deco_items:
            items.append((int(d.get("depth", 0)), "deco", d))
        for w in self.water_strips:
            items.append((w.depth, "water", w))
        for t in self.thorns:
            items.append((t.depth, "thorn", t))
        # 传送门不总在最上层：它在各关卡的深度分别为 25、256、465、501，
        # 会压住部分装饰，而不是盖住所有装饰。
        if self.portal_active and self.level.portal is not None:
            items.append((PORTAL_DEPTH.get(self.level.id, 501), "portal", None))
        for _depth, kind, obj in sorted(items, key=lambda it: it[0]):
            if kind == "deco":
                surface.blit(obj["img"], (round(obj["x"] - self.camera_x),
                                          round(obj["y"])))
            elif kind == "portal":
                self._draw_portal_door(surface)
            else:
                obj.draw(surface, self.camera_x)

        if getattr(self.level, "dungeon", None):
            self._draw_dungeon(surface)

        # 掉落物在角色之前绘制，因此显示在角色后方。
        for d in self.drops:
            self._draw_drop(surface, d)

        if self.boss is not None and self.boss.type == 24 and not self.boss.dead:
            if self._boss24_bg_anim is None:
                bg_clip = self.assets.fx("boss24_bg")
                if bg_clip:
                    self._boss24_bg_anim = Animation(bg_clip, loop=True)
            if self._boss24_bg_anim is not None:
                # 背景贴图的位置跟随 Boss 本体：纵坐标取本体基准高度（300），
                # 横坐标取本体 x。背景内容包围盒为 (-443.5,-222)-(443.5,222)，
                # 所以锚点采用内容中心 220，而不是图片底边
                # （早期实现用 442 再加地面偏移 4，
                # 导致头盔位置偏高了 11.4 像素）。
                self._boss24_bg_anim.draw(
                    surface, self.boss.x - self.camera_x,
                    self.boss._root_y(), facing=-1, alpha=210)

        behind, hero, front = self.actor_draw_sequence()
        for m in behind:
            m.draw(surface, self.camera_x)
        hero.draw(surface, self.camera_x)
        for m in front:
            m.draw(surface, self.camera_x)

        for b in self.monster_bullets:
            b.draw(surface, self.camera_x)
        self.auras.draw(surface, self.camera_x)

        if self.player.miss_timer > 0:
            try:
                miss = self.assets.image("ui/miss.png")
                surface.blit(miss, miss.get_rect(center=(
                    round(self.player.x - self.camera_x),
                    round(self.player.y + self.player.draw_offset
                          - self.player.h - 26))))
            except (KeyError, FileNotFoundError):
                pass

        for p in self.projectiles:
            p.draw(surface, self.camera_x)

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
            surface.blit(img, (round(f["x"] - self.camera_x - ax),
                               round(f["y"] - ay)))

        ui.draw_damage_numbers(surface, self.assets, self.numbers, self.camera_x)
        for f in self.floating:
            font = self.assets.font(18, bold=True)
            surface.blit(font.render(f.text, True, f.color),
                         (round(f.x - self.camera_x - 8), round(f.y)))

        self.hud.draw(surface, self.player)
        ui.draw_world_overlay(surface, self.assets, self)
        self.combo.draw(surface, self.assets, self.wall_time)
        # 界面绘制顺序：角色信息、血条、Boss 血条、各类覆盖层，最后是变身特效。
        # 因此特效会盖在 HUD 之上。
        if self.gxp_effect is not None:
            self.gxp_effect.draw(surface)
        # 通关淡出：全局状态与场景在 1 秒内透明度由 1 变为 0，
        # 放在最后绘制，所以会同时盖住 HUD 和游戏画面。
        ui.draw_exit_fade(surface, self.portal_fade_progress)

    def _draw_drop(self, surface: pygame.Surface, drop: Drop) -> None:
        if drop.medicine:
            rel = MEDICINE[drop.medicine][0]
        else:
            rel = drop_icon(drop.item)
        try:
            img = self.assets.image(rel)
        except (KeyError, FileNotFoundError):
            img = None
        sx = round(drop.x - self.camera_x)
        sy = round(drop.y)
        if img is not None:
            surface.blit(img, img.get_rect(center=(sx, sy - DROP_DRAW_CENTER_DY)))
        else:
            pygame.draw.rect(surface, (230, 200, 120), (sx - 8, sy - 30, 16, 16))

    def _draw_dungeon(self, surface: pygame.Surface) -> None:
        """1-2 副本入口是不可见的触发区域。

        关卡数据里只有一个红色调试占位块，其世界坐标 y 为 590..640，
        完全落在 590 像素高的画面下方，此外还有一个不可见的触发标记。
        早期实现曾在屏幕内 y-60 处自造脉动光环和"副本入口"标签，
        这些都已移除；自动进入判定见 :meth:`_try_dungeon`。
        """
        return

    def _draw_portal_door(self, surface: pygame.Surface) -> None:
        """绘制传送门美术资源，锚点沿用素材自带的注册点。"""
        p = self.level.portal
        if not p:
            return
        try:
            img = self.assets.image("ui/portal_door.png")
        except (KeyError, FileNotFoundError):
            return
        surface.blit(img, (round(float(p["x"]) - self.camera_x + DOOR_LEFT),
                           round(float(p["y"]) + DOOR_TOP)))
