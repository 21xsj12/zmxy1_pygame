"""项目级常量与路径定义。"""
from __future__ import annotations

import itertools
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ASSETS_DIR = PROJECT_ROOT / "assets"
DATA_DIR = PROJECT_ROOT / "data"
SAVE_DIR = PROJECT_ROOT / "saves"

WIDTH = 940
HEIGHT = 590
FPS = 60

TITLE = "造梦西游1 · 七魔王篇"

# 动画逻辑帧率：固定每秒 24 帧的节奏。
ANIM_FPS = 24

# 物理量由每秒 24 帧的“每帧”数值换算得到。
GRAVITY = 864.0          # 每帧 1.5 像素，乘以 24² 换算成每秒值
JUMP_SPEED = -480.0      # 每帧 -20 像素，乘以 24
WALK_SPEED = 144.0       # 每帧 6 像素，乘以 24
RUN_SPEED = 240.0        # 每帧 10 像素，乘以 24
MAX_FALL = 980.0

COLOR_BG = (24, 32, 38)
COLOR_WHITE = (245, 245, 235)
COLOR_BLACK = (12, 12, 12)
COLOR_HP = (210, 40, 40)
COLOR_MP = (40, 120, 220)
COLOR_EXP = (235, 170, 30)
COLOR_GOLD = (240, 200, 80)
COLOR_UI_BG = (18, 18, 18, 210)

# 角色成长从 1 级起步；调试时可用 ``--level`` 覆盖起始等级。
# 早期里程碑曾把起点设为 5 级，只为让 1-1 的 Boss 更容易打过。
START_LEVEL = 1

LEVEL_ORDER = ["1-1", "1-2", "1-3", "2-1", "2-2", "2-3",
               "3-1", "3-2", "3-3", "4-1", "9-1", "vs"]
# 关卡名取自地图节点上烘焙的文字，这才是游戏内实际显示的名称；
# 文字设定稿中的地名（如白骨山）与美术画面不一致，
# 因此以画面上的文字为准。
LEVEL_NAMES = {
    "1-1": "花果山", "1-2": "水帘洞", "1-3": "桃花源",
    "2-1": "蝙蝠窟", "2-2": "七星洞", "2-3": "迦楼罗之穴",
    "3-1": "浅岸", "3-2": "东海", "3-3": "鲨之漩涡",
    "4-1": "牛魔谷", "9-1": "水晶宫", "vs": "竞技场",
}
# 地图上展示的主线关卡（9-1 是 1-2 的洞窟关卡，vs 是竞技场）。
MAIN_LEVELS = [lid for lid in LEVEL_ORDER if lid not in ("9-1", "vs")]

# ---------------------------------------------------------------- 命中标识
# 命中身份由“攻击者实例 + 攻击计数器 + 段号”共同确定，用于区分每一次攻击。
# 只靠攻击计数器不够：不同攻击者的计数可能相同，因此还要带上攻击者实例的
# 命名空间。本实现用 ``(src_ns, attack_id, stage)`` 三元组表达：``src_ns`` 是
# 攻击者**实例**的命名空间（战斗内单调递增、永不复用），``attack_id`` 是该
# 攻击者的攻击计数器，``stage`` 是同一次攻击内的段号（内部概念，默认 0）。
# 这样即使两个实例的攻击计数相同，也不会被误判为同一次命中。
HitKey = tuple[int, int, int]

# 不在 World 里构造的实体（测试直接 new）用这个兜底计数器，起点避开
# World/ArenaWorld 的 100+ 区间，保证同场对象互不撞号。
_HIT_NS_FALLBACK = itertools.count(1_000_000)


def next_hit_ns() -> int:
    """分配一个兜底的攻击实例命名空间（World 内请用 ``world.new_hit_ns()``）。"""
    return next(_HIT_NS_FALLBACK)
