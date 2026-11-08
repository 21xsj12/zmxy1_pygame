"""视口（相机）控制器：负责关卡横向滚动，并让主角在屏幕内保持合适位置。

本实现不把主角固定在屏幕正中。只有当主角走出屏幕上的死区时，关卡才会滚动：

* 右侧死区：屏幕 x >= 940 * 2 / 3
* 左侧死区：屏幕 x <= 940 / 5

滚动过程中，关卡层按主角的行走/奔跑速度平移（不是用弹簧插值去追位置），
远景层则按下面的比例跟着移动::

    delta_bg = delta_level * (bg_width - 940) / (level_width - 940)

这样两层会在同一时刻到达各自的右边界。

停止点沿用一套状态机式处理：

* 待触发的停止点，其屏幕 x 进入 ``[900, 980]`` 即视为触发；
* 该停止点对应的波次还存活期间，相机被钉在触发时的位置；
* 非最终波次结束后，相机会自动向右推进，直到主角回到屏幕
  x <= 450（或者下一个停止点已经接近屏幕右缘）；
* 最后一个停止点之后，相机最多还能推进到 ``最后一个停止点的 x - 870``，
  这是最终停止点允许的推进上限。

本实现只有一名玩家，所以不再区分两条角色路径，统一成一套判断。
碰撞盒左边界用 ``player.w`` 计算，因为判定要看角色碰撞椭圆的左缘。
"""
from __future__ import annotations

from .constants import FPS, WIDTH

RIGHT_DEAD_ZONE = WIDTH * 2.0 / 3.0
LEFT_DEAD_ZONE = WIDTH / 5.0

STOP_TRIGGER_LEFT = 900.0
STOP_TRIGGER_RIGHT = 980.0
FINAL_STOP_OFFSET = 870.0

AUTO_CAMERA_BASE = 10.0 * FPS       # 自动推进的基础速度：每帧额外加 10 px
AUTO_CAMERA_MAX = 30.0 * FPS        # 自动推进的速度上限：每帧 30 px
AUTO_CAMERA_STOP_SCREEN_X = 450.0
AUTO_CAMERA_NEXT_STOP_X = 1000.0

UNLIMITED_BACKWARD = -99.0


class ViewCamera:
    """相机的 x 表示视口左边缘在世界坐标系中的位置。"""

    def __init__(self, level):
        self.level = level
        self.x = 0.0
        self.lock_x = 0.0
        self.locked = False

        # 停止点激活期间相机还能向左移动的剩余像素数；-99 表示还没遇到过停止点。
        # 每次真正向左移动后都要从额度里扣减，扣到 0 就不能再回退。
        self.can_backward = 0.0

        # 是否处于波次结束后的自动推进阶段。
        self.auto = False

        stops = getattr(level, "stops", None) or []
        # 最后一个停止点的数据坐标 x；停止点数组由物理世界按 x 升序维护，
        # 所以直接取最大值即可。
        self.last_point_x = (max(float(s.x) for s in stops) if stops
                             else float(level.width))
        # 最终上限 = 最后一个停止点的 x - 870，且不允许为负。
        self.final_x = max(0.0, self.last_point_x - FINAL_STOP_OFFSET)
        # 关卡数据自带左侧触发位置（默认 900）；触发带宽固定为 80 px，
        # 右沿由左沿加 80 得到。
        self.trigger_left = float(getattr(level, "camera_lock_offset",
                                          STOP_TRIGGER_LEFT))
        self.trigger_right = self.trigger_left + (STOP_TRIGGER_RIGHT
                                                   - STOP_TRIGGER_LEFT)

    # ------------------------------------------------------------------ 接口
    def reset(self, x: float = 0.0) -> None:
        """把相机重置到 ``x`` 处（--warp 传送和读档加载都会用到）。"""
        self.x = max(0.0, min(float(x), self.level.max_camera_x))
        self.lock_x = self.x
        self.locked = False
        self.can_backward = UNLIMITED_BACKWARD
        self.auto = False

    def update(self, dt: float, player, active_stop, pending_stop):
        """推进相机，并返回本帧应当触发的停止点。

        ``player`` 需要提供 ``x``、``w`` 和 ``camera_speed``（带符号，单位 px/s）。
        当前波次仍在进行时，``pending_stop`` 必须为 ``None``。
        """
        dt = max(0.0, float(dt))
        speed = float(getattr(player, "camera_speed", 0.0))
        # 命中判定取角色碰撞椭圆包围盒的左缘，所以这里减去半个宽度。
        screen_x = player.x - player.w / 2.0 - self.x
        triggered = None

        if active_stop is not None:
            self.locked = True

        if screen_x >= RIGHT_DEAD_ZONE:
            if active_stop is not None:
                # 相机被钉在触发时的位置：主角先向左走会把相机拉回去，
                # 再向右走又能推回锁定位置，但不会越过该位置。
                if speed > 0:
                    self._move_right(speed * dt, self.lock_x)
            elif pending_stop is not None:
                # 先按速度推进（速度为负时按 0 处理），再判断停止点的屏幕位置，
                # 命中触发带就锁定，否则清空回退额度。
                # 有待触发停止点时，相机不得越过其触发窗口的下沿。
                # 触发带 [900, 980] 只有 80 px，单帧位移却可能达到 45 px，
                # 相机速度又等于角色速度；一旦整帧跨过触发带，停止点就会
                # 永久丢失（索引不前进、相机也不锁定）。所以先限位再判触发带，
                # 保证相机推进到停止点屏幕 x = trigger_left 时必定锁定。
                # 自动推进阶段不触发停止点，因此该阶段不做这层限位。
                limit = self.level.max_camera_x
                if not self.auto:
                    limit = min(limit, pending_stop.x - self.trigger_left)
                self._move_right(max(speed, 0.0) * dt, limit)
                stop_screen = pending_stop.x - self.x
                if (not self.auto
                        and self.trigger_left <= stop_screen <= self.trigger_right):
                    self.lock_x = self.x
                    self.locked = True
                    # 回退额度 = 停止点参数减去视口宽度，即触发点左侧还剩多少可回退空间。
                    self.can_backward = self._backward_budget(pending_stop)
                    triggered = pending_stop
                else:
                    self.can_backward = UNLIMITED_BACKWARD
            else:
                # 已经没有停止点：相机可推进到最后一个停止点的 x - 870 处。
                if speed > 0:
                    self._move_right(speed * dt, self.final_x)

        elif screen_x <= LEFT_DEAD_ZONE:
            # 只有角色确实向左走、且仍有回退额度时，才允许相机左移。
            if speed < 0 and (self.can_backward > 0
                              or self.can_backward == UNLIMITED_BACKWARD):
                self._move_left(-speed * dt)

        # 自动推进：一波清完之后继续向右滚动，直到主角回到屏幕 x <= 450，
        # 或者下一个停止点已经接近屏幕右缘。
        if self.auto and active_stop is None:
            if pending_stop is None or pending_stop.x - self.x <= AUTO_CAMERA_NEXT_STOP_X:
                self.auto = False
            elif player.x - self.x > AUTO_CAMERA_STOP_SCREEN_X:
                auto_speed = min(abs(speed) + AUTO_CAMERA_BASE, AUTO_CAMERA_MAX)
                self._move_right(auto_speed * dt, self.level.max_camera_x)
            else:
                self.auto = False

        self.x = max(0.0, min(self.x, self.level.max_camera_x))
        return triggered

    # -------------------------------------------------------------- 辅助方法
    def _backward_budget(self, stop) -> float:
        """按停止点携带的回退窗口参数换算相机还能向左回退的像素数。"""
        between = float(stop.between_rand_l)
        level_id = str(getattr(self.level, "id", ""))
        if level_id.startswith("1-"):
            between = 1450.0
        elif level_id in ("4-1", "9-1"):
            between = 940.0
        return between - WIDTH

    def _move_right(self, amount: float, limit: float) -> None:
        if amount <= 0:
            return
        target = min(self.x + amount, max(self.x, limit))
        if target > self.x:
            self.x = target

    def _move_left(self, amount: float) -> None:
        if amount <= 0:
            return
        target = max(0.0, self.x - amount)
        if self.can_backward > 0:
            target = max(target, self.x - self.can_backward)
        moved = self.x - target
        self.x = target
        if self.can_backward > 0:
            self.can_backward = max(0.0, self.can_backward - moved)
