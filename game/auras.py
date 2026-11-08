"""怪物死亡后掉落的灵魂粒子拾取物。

掉落与吸收流程如下：

* 怪物死亡时一次生成 2-4 个红色灵魂球，另外绿、蓝、白三种颜色各自
  独立掷一次 12% 概率，命中后按 3 / 2 / 1 个的档位生成。
* 灵魂球先原地停留 20 帧，再用 1 秒向上飘 30-50 px（缓出曲线），随后
  以 4-6 px/帧追踪击杀者，每帧加速 2 px/帧、速度上限 20 px/帧，
  与目标距离不超过 10 px 时被吸收。
* 吸收红色灵魂球会把其威力值（怪物的 gxp）加到玩家的灵魂值（soul）
  与隐藏的 myscore；绿色灵魂球回复生命，蓝色灵魂球回复法力，
  白色灵魂球给无双槽加 5 点。

所有运动都按 24 帧/秒的固定帧步进推进：动画播放与位移共用同一节拍，
因此帧率变化不会让飘浮和追踪的节奏脱节。每帧最多补算 8 个步进，避免
卡顿后一次性追帧过多；粒子存活上限为停留与追踪阶段累计 2400 帧。
"""
from __future__ import annotations

import math
import random
from typing import TYPE_CHECKING

from .assets import Animation

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .world import World

_GLOBAL_RNG = random.Random()

# 时间参数统一按 24 帧/秒换算：上升缓动与逐帧推进共用同一采样节拍，
# 因此固定 24 Hz 的帧步进即可让飘浮和追踪的时序完全对齐。
FPS = 24
TICK = 1.0 / FPS
IDLE_TICKS = 20
RISE_TICKS = 24
TTL_TICKS = 2400
PICKUP_RADIUS = 10.0
SPEED_CAP = 20.0
SPEED_ACCEL = 2.0
RISE_MIN, RISE_MAX = 30.0, 50.0
SPEED_MIN, SPEED_MAX = 4.0, 6.0
RED_SPREAD = 5.0
GREEN_SPREAD = 20.0
BLUE_SPREAD = 30.0
WHITE_SPREAD = 40.0
COLOR_PROBS = (0.04, 0.08, 0.12)  # 依次对应 3 / 2 / 1 个的概率，其余情况不掉落

KIND_CLIP = {
    "soul": "aura_soul",   # 红色灵魂球 -> 灵魂（soul/myscore）
    "hp": "aura_hp",       # 绿色灵魂球 -> 红色贴图，回复生命
    "mp": "aura_mp",       # 蓝色灵魂球 -> 回复法力
    "ws": "aura_ws",       # 白色灵魂球 -> 金色贴图，无双 +5
}


class AuraParticle:
    """单个灵魂粒子：停留 -> 上升 -> 追踪 -> 吸收。"""

    def __init__(self, assets, x: float, y: float, kind: str, power: int,
                 target, rng=None):
        clip = assets.fx(KIND_CLIP[kind])
        self.anim = Animation(clip, loop=True) if clip is not None else None
        self.x = float(x)
        self.y = float(y)
        self.y0 = float(y)
        self.kind = kind
        self.power = int(power)
        self.target = target
        self.phase = "idle"          # 阶段：idle（停留）/ rise（上升）/ home（追踪）
        self.idle_left = IDLE_TICKS
        self.rise_tick = 0
        self.active_ticks = 0        # 存活计时只在停留和追踪阶段累加
        self.rise_up = (rng or _GLOBAL_RNG).uniform(RISE_MIN, RISE_MAX)
        self.speed = (rng or _GLOBAL_RNG).uniform(SPEED_MIN, SPEED_MAX)
        self.dead = False

    # ------------------------------------------------------------------ 逻辑
    def update_tick(self, apply_reward) -> None:
        """推进一个 1/24 秒的帧；奖励结算由调用方传入的回调完成。"""
        if self.dead:
            return
        if self.anim is not None:
            self.anim.update(TICK)
        if self.phase == "idle":
            self.idle_left -= 1
            if not self._count_ttl():
                self._finish(apply_reward)
                return
            if self.idle_left <= 0:
                self.phase = "rise"
        elif self.phase == "rise":
            self.rise_tick += 1
            t = self.rise_tick / RISE_TICKS
            self.y = self.y0 - self.rise_up * (1.0 - (1.0 - t) ** 2)
            if self.rise_tick >= RISE_TICKS:
                self.phase = "home"
        else:
            if not self._count_ttl():
                self._finish(apply_reward)
                return
            target = self.target
            tx, ty = self._target_point(target)
            dx = tx - self.x
            dy = ty - self.y
            dist = math.hypot(dx, dy)
            if dist < 1e-6:              # 距离为 0 时直接结束，避免除零
                self._finish(apply_reward)
                return
            # 追踪顺序：先按当前速度移动，再加速，最后用新距离判断是否吸收。
            self.x += dx / dist * self.speed
            self.y += dy / dist * self.speed
            self.speed = min(SPEED_CAP, self.speed + SPEED_ACCEL)
            dx = tx - self.x
            dy = ty - self.y
            if math.hypot(dx, dy) <= PICKUP_RADIUS:
                self._finish(apply_reward)

    @staticmethod
    def _target_point(target) -> tuple[float, float]:
        """吸收判定点取英雄 root 原点的 x/y，而不是脚底位置。

        本作 Player.y 记录的是脚底线，因此这里改用 root 坐标（减
        draw_offset 约 45 px）；直接用脚底线会让灵魂吸到脚下。
        """
        if hasattr(target, "root_y"):
            return float(target.x), float(target.root_y)
        offset = getattr(target, "draw_offset", None)
        dy = float(offset()) if callable(offset) else float(offset or 0.0)
        return float(target.x), float(target.y) - dy

    def _count_ttl(self) -> bool:
        self.active_ticks += 1
        return self.active_ticks <= TTL_TICKS

    def _finish(self, apply_reward) -> None:
        if self.dead:
            return
        self.dead = True
        apply_reward(self)

    # ------------------------------------------------------------------- 绘制
    def draw(self, surface, camera_x: float) -> None:
        if self.anim is None:
            return
        img = self.anim.frame
        surface.blit(img, (round(self.x - camera_x) - img.get_width() // 2,
                           round(self.y) - img.get_height() // 2))


class AuraSystem:
    """管理所有存活的灵魂粒子，并按 24 Hz 固定步长推进。"""

    def __init__(self, world: "World"):
        self.world = world
        self.items: list[AuraParticle] = []
        self._acc = 0.0

    # ---------------------------------------------------------------- 生成
    def drop(self, monster) -> int:
        """掉落入口：2-4 个灵魂球，再加各颜色的独立概率判定。"""
        target = monster.target
        if target is None:
            return 0
        rng = self.world.rng
        spawned = 0
        # 灵魂球以怪物碰撞椭圆的中心为基准点，而 Monster.y 记录的是脚底与
        # 地面的接触点，两者相差约一个椭圆半高。这里先减去 colipse_bottom，
        # 让光球出现在尸体周围，而不是陷进草地里
        # （普通怪的 colipse_bottom 实测约为 30.451）。
        root_y = float(monster.y) - float(
            getattr(monster, "colipse_bottom", 0.0) or 0.0)
        for _ in range(2 + int(rng.random() * 3)):        # 数量为 2、3 或 4 个
            self.spawn(
                monster.x + (rng.random() - 0.5) * RED_SPREAD * 2,
                root_y + (rng.random() - 0.5) * RED_SPREAD * 2,
                "soul", monster.gxp, target)
            spawned += 1
        for kind, spread in (("hp", GREEN_SPREAD), ("mp", BLUE_SPREAD),
                             ("ws", WHITE_SPREAD)):
            r = rng.random()                              # 每种颜色单独掷一次
            p3, p2, p1 = COLOR_PROBS
            n = 3 if r < p3 else 2 if r < p2 else 1 if r < p1 else 0
            for _ in range(n):
                self.spawn(
                    monster.x + (rng.random() - 0.5) * spread * 2,
                    root_y + (rng.random() - 0.5) * spread * 2,
                    kind, self._power(kind, target), target)
                spawned += 1
        return spawned

    @staticmethod
    def _power(kind: str, target) -> int:
        if kind == "hp":
            return max(10, int(target.max_hp / 20))
        if kind == "mp":
            return max(10, int(target.max_mp / 20))
        return 5                                           # 白色灵魂球的固定威力值

    def spawn(self, x: float, y: float, kind: str, power: int,
              target) -> AuraParticle:
        particle = AuraParticle(self.world.assets, x, y, kind, power, target,
                                self.world.rng)
        self.items.append(particle)
        return particle

    # ----------------------------------------------------------------- 更新
    def update(self, dt: float) -> None:
        self._acc += dt
        ticks = 0
        while self._acc >= TICK and ticks < 8:
            self._acc -= TICK
            for particle in self.items:
                particle.update_tick(self.world.apply_aura)
            self.items = [p for p in self.items if not p.dead]
            ticks += 1

    def clear(self) -> None:
        self.items.clear()
        self._acc = 0.0

    def draw(self, surface, camera_x: float) -> None:
        for particle in self.items:
            particle.draw(surface, camera_x)
