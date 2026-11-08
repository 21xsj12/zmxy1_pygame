"""关卡数据结构与加载器。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .constants import DATA_DIR


@dataclass(frozen=True)
class Wall:
    x: float
    y: float
    w: float
    h: float
    kind: str = "solid"

    @property
    def right(self) -> float:
        return self.x + self.w

    @property
    def bottom(self) -> float:
        return self.y + self.h


@dataclass(frozen=True)
class Stop:
    idx: int
    x: float
    y: float
    between_rand_l: int = 1150
    # 停顿点素材宽度为 70 像素；若某关卡的停顿标记做了缩放，
    # 可通过 width 字段单独配置。
    width: float = 70.0


@dataclass(frozen=True)
class SpawnSpec:
    x: float
    y: float
    enemy_type: int
    total: int
    interval: float
    delay: float
    stop_idx: int
    random: bool = False


@dataclass
class Level:
    id: str
    name: str
    width: float
    ground_y: float
    player_start: tuple[float, float]
    background: str
    background_y: float
    far_background: str
    foreground: str
    foreground_x: float
    foreground_y: float
    music: str
    camera_lock_offset: float
    stops: list[Stop]
    spawns: list[SpawnSpec]
    boss: dict
    portal: dict
    background_x_offset: float = 0.0
    ground: str = ""
    walls: list[Wall] = field(default_factory=list)
    slopes: list[dict] = field(default_factory=list)
    terrain: list[dict] = field(default_factory=list)
    deco: list[dict] = field(default_factory=list)
    thorns: list[dict] = field(default_factory=list)
    water: list[dict] = field(default_factory=list)
    boss_enemy_type: int | None = None
    dungeon: dict | None = None
    # 仅作为地面绘制元数据：草皮绘制顶部减去碰撞顶部得到该差值，它不会被
    # 叠加到角色的绘制位置上；角色原点仍落在碰撞线上，草皮因此可能盖住
    # 脚部（详见文档 Plan C 第 8 节）。
    ground_art_delta: float = 0.0
    background_bbox: list[float] | None = None
    turnbacks: list[dict] = field(default_factory=list)
    no_continue: list[dict] = field(default_factory=list)
    disappear_points: list[dict] = field(default_factory=list)

    @property
    def max_camera_x(self) -> float:
        # 镜头右边界取最后一处停顿点的 x 坐标减去 870，即停在终点前的
        # 固定留白处；关卡宽度的 width 字段不参与该计算，按宽度减 940 的
        # 写法是遗留分支，不要再沿用。
        if not self.stops:
            return 0.0
        return max(0.0, max(s.x for s in self.stops) - 870.0)

    def surface_at(self, x: float, y: float, prev_y: float | None = None,
                   half_width: float = 8.0) -> float | None:
        """返回下落物能落到的最高局部表面。

        ``y``/``prev_y`` 是掉落物当前/上一帧的底部坐标。
        该查询有意不依赖角色物理：它服务于没有水平速度的怪物掉落。处理情况包括：
          * 抬高的地形（kind 为 ``terrain`` 的墙体），其可见实体一直延伸到主地面；
          * 普通实心平台的顶面（包括单帧内穿过顶面的情况）；
          * 单向平台只在从上方落下时才生效；
          * 斜坡顶边，包括初始位置已在斜坡内部的下落。

        当 ``x`` 处没有任何地面/平台时返回 ``None``（例如 1-2 的水坑），
        掉落物会继续下落，直到落到更下方的桥面/地面。
        """
        left = x - half_width
        right = x + half_width

        def overlaps(wx: float, ww: float) -> bool:
            return right >= wx and left <= wx + ww

        best: float | None = None

        for wall in self.walls:
            if not overlaps(wall.x, wall.w):
                continue
            bottom = wall.y + wall.h
            if wall.kind == "oneway":
                # 只有从上方落下时才会停在这块平台上；已经处在平台下方的
                # 掉落物不能被向上吸穿过平台。
                if prev_y is not None and prev_y <= wall.y + 2 and y >= wall.y:
                    best = wall.y if best is None else min(best, wall.y)
                continue
            if wall.kind == "terrain":
                # 抬高的地形是一整块可见实体，一直延伸到主地面；掉落物若
                # 已落在地形顶面之下，说明它嵌进了石壁内部，需要抬到顶面。
                if y >= wall.y:
                    best = wall.y if best is None else min(best, wall.y)
                continue
            # 普通实心平台：掉落物穿过顶面或已经位于其包围盒内部时，停在顶面。
            if wall.y <= y <= bottom or (
                    prev_y is not None and prev_y <= wall.y + 2 and y >= wall.y):
                best = wall.y if best is None else min(best, wall.y)

        # 地形列表作为兜底：部分较早生成的关卡 JSON 没有把抬高平台同时
        # 写进 ``walls``，这里补上这类数据。
        for t in self.terrain:
            if overlaps(float(t["x"]), float(t["w"])) and y >= float(t["y"]):
                top = float(t["y"])
                best = top if best is None else min(best, top)

        for s in self.slopes:
            x0, x1 = float(s["x0"]), float(s["x1"])
            if x1 < x0:
                x0, x1 = x1, x0
            if right < x0 or left > x1 or x1 == x0:
                continue
            sx = min(max(x, x0), x1)
            t = (sx - x0) / (x1 - x0)
            surface = float(s["y0"]) + (float(s["y1"]) - float(s["y0"])) * t
            # 斜坡下方没有可站立的空间；掉落物若已经位于斜面内部，直接抬到
            # 对应的坡面高度。
            if y >= surface:
                best = surface if best is None else min(best, surface)

        return best


def load_level(level_id: str = "1-1", path: Path | None = None) -> Level:
    path = path or (DATA_DIR / "levels" / f"{level_id}.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    return Level(
        id=data["id"],
        name=data["name"],
        width=float(data["width"]),
        ground_y=float(data["ground_y"]),
        player_start=(float(data["player_start"]["x"]), float(data["player_start"]["y"])),
        background=data["background"],
        background_x_offset=float(data.get("background_x_offset", 0)),
        background_y=float(data.get("background_y", 0)),
        far_background=data["far_background"],
        foreground=data.get("foreground", ""),
        foreground_x=float(data.get("foreground_x", 0)),
        foreground_y=float(data.get("foreground_y", 65)),
        ground=data.get("ground", ""),
        music=data.get("music", "bg1"),
        camera_lock_offset=float(data.get("camera_lock_offset", 800)),
        stops=[Stop(**s) for s in data["stops"]],
        spawns=[SpawnSpec(**s) for s in data["spawns"]],
        boss=data["boss"],
        portal=data["portal"],
        walls=[Wall(x=w["x"], y=w["y"], w=w["w"], h=w["h"],
                    kind=w.get("kind", w.get("type", "solid"))) for w in data["walls"]],
        slopes=data.get("slopes", []),
        terrain=data.get("terrain", []),
        deco=data.get("deco", []),
        thorns=data.get("thorns", []),
        water=data.get("water", []),
        boss_enemy_type=data.get("boss_enemy_type"),
        dungeon=data.get("dungeon"),
        ground_art_delta=float(data.get("ground_art_delta",
                                        data.get("actor_draw_offset", 0.0))),
        background_bbox=data.get("background_bbox"),
        turnbacks=data.get("turnbacks", []),
        no_continue=data.get("no_continue", []),
        disappear_points=data.get("disappear_points", []),
    )
