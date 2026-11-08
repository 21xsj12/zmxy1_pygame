"""英雄技能定义、已学习状态处理与购买交易。

技能表按序号排列：主动技能依次消耗 100/200/500/1000/2000 点灵魂，
被动技能提供可累加的固定加成。

界面本身位于 :mod:`game.skill_page`；本模块不依赖 pygame，
这样购买与升级规则就可以在没有显示设备的环境下单独测试。
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from .constants import DATA_DIR

SKILLS = json.loads((DATA_DIR / "skills.json").read_text(encoding="utf-8"))

ACTIVE_COUNT = 5
PASSIVE_COUNT = 5
PASSIVE_MAX = 5
PASSIVE_COST_STEP = 5000

# 界面显示用的按键提示：1P 使用字母键布局，2P 使用数字键布局；
# 数据表里只存 1P 的按键文本，2P 显示时按同一序号换算成数字键。
ACTIVE_KEYS = {
    "wukong": ["↑J", "→→J", "U", "I", "O"],
    "tangseng": ["长按J", "U", "I", "O", "L"],
}
P2_KEYS = {
    "wukong": ["↑1", "→→1", "4", "5", "6"],
    "tangseng": ["长按1", "4", "5", "6", "3"],
}


@dataclass(frozen=True)
class PurchaseResult:
    """一次主动技能购买或被动技能升级的结果。"""

    ok: bool
    cost: int = 0
    reason: str = ""       # 失败原因：learned / poor / max / bad_index
    level: int = 0         # 被动技能升级后的等级


def default_state(hero: str) -> dict:
    return {
        "active": [False] * ACTIVE_COUNT,
        "passive": [0] * PASSIVE_COUNT,
    }


def _as_bool(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _as_level(value) -> int:
    try:
        level = int(value)
    except (TypeError, ValueError):
        level = 0
    return max(0, min(PASSIVE_MAX, level))


def normalize(hero: str, state: dict | None) -> dict:
    """返回一份全新的 5 格状态，并兼容旧的或被手动改过的存档。"""
    base = default_state(hero)
    if not isinstance(state, dict):
        return base
    active = list(state.get("active") or [])[:ACTIVE_COUNT]
    passive = list(state.get("passive") or [])[:PASSIVE_COUNT]
    active += [False] * (ACTIVE_COUNT - len(active))
    passive += [0] * (PASSIVE_COUNT - len(passive))
    return {
        "active": [_as_bool(x) for x in active],
        "passive": [_as_level(x) for x in passive],
    }


def active_skill(hero: str, index: int) -> dict:
    return SKILLS[hero]["active"][index]


def active_cost(hero: str, index: int) -> int:
    return int(SKILLS[hero]["active"][index]["cost"])


def active_key(hero: str, index: int, p2: bool = False) -> str:
    """界面显示的按键文案；2P 使用数字键布局。"""
    table = P2_KEYS if p2 else ACTIVE_KEYS
    try:
        return table[hero][index]
    except (KeyError, IndexError):
        return str(SKILLS[hero]["active"][index].get("key", ""))


def passive_name(hero: str, index: int) -> str:
    return str(SKILLS[hero]["passive"][index]["name"])


def passive_cost(level: int) -> int:
    """把被动技能从 ``level`` 升到 ``level+1`` 所需的费用。

    计算公式为“目标等级 × 5000”；调用方会把等级限制在 0..4 的有效区间内。
    满级时本函数仍会算出理论值 30000，因此界面层应改用
    :func:`passive_next_cost` 取实际费用。
    """
    return PASSIVE_COST_STEP * (level + 1)


def passive_next_cost(level: int) -> int:
    """升到下一级所需的费用；已经满级时返回 0。"""
    if level >= PASSIVE_MAX:
        return 0
    return passive_cost(level)


def passive_effect_text(index: int, level: int) -> str:
    """某一等级对应的效果描述文本。

    尚未学习（等级 0）或等级超过 5 时显示 ``----``；
    当前效果为：生命与魔法上限各加 level*100+100，暴击率加 level+1 个百分点，
    每秒回复量加 level+1。
    """
    if not 0 <= index < PASSIVE_COUNT or level <= 0 or level > PASSIVE_MAX:
        return "----"
    if index == 0:
        return f"生命上限增加 {level * 100 + 100}"
    if index == 1:
        return f"魔法上限增加 {level * 100 + 100}"
    if index == 2:
        return f"暴击率增加 {level + 1} %"
    if index == 3:
        return f"每秒回血增加 {level + 1}"
    return f"每秒回魔增加 {level + 1}"


def try_buy_active(hero: str, state: dict, soul: int, index: int
                   ) -> PurchaseResult:
    """把一次主动技能购买应用到 ``state`` 上；灵魂扣减与存档由调用方负责。

    本实现额外补上了“已经学过”的检查，因此重复触发的事件处理器
    不会把同一项技能扣两次费。
    """
    if not 0 <= index < ACTIVE_COUNT:
        return PurchaseResult(False, reason="bad_index")
    if state["active"][index]:
        return PurchaseResult(False, reason="learned")
    cost = active_cost(hero, index)
    if soul < cost:
        return PurchaseResult(False, cost=cost, reason="poor")
    state["active"][index] = True
    return PurchaseResult(True, cost=cost)


def try_upgrade_passive(hero: str, state: dict, soul: int, index: int
                        ) -> PurchaseResult:
    """把一次被动技能升级应用到 ``state`` 上；灵魂扣减与存档由调用方负责。"""
    if not 0 <= index < PASSIVE_COUNT:
        return PurchaseResult(False, reason="bad_index")
    level = int(state["passive"][index])
    if level >= PASSIVE_MAX:
        return PurchaseResult(False, reason="max", level=level)
    cost = passive_cost(level)
    if soul < cost:
        return PurchaseResult(False, cost=cost, reason="poor", level=level)
    state["passive"][index] = level + 1
    return PurchaseResult(True, cost=cost, level=level + 1)


def passive_bonus(hero: str, state: dict) -> dict:
    """被动技能提供的固定累加加成。

    各属性的计算方式：
      hp     -> 100*n + 100
      mp     -> 100*n + 100
      crit   -> 0.01*n + 0.01
      regen  -> n + 1

    返回值是当前总量（不是单级增量）：等级 5 时共增加 600 点生命、
    600 点魔法、6% 暴击率以及每秒 6 点回复。
    """
    levels = normalize(hero, state)["passive"]
    n_hp, n_mp, n_crit, n_hp_r, n_mp_r = levels
    return {
        "hp": (100 * n_hp + 100) if n_hp else 0,
        "mp": (100 * n_mp + 100) if n_mp else 0,
        "crit": (0.01 * n_crit + 0.01) if n_crit else 0.0,
        "hp_regen": (n_hp_r + 1) if n_hp_r else 0,
        "mp_regen": (n_mp_r + 1) if n_mp_r else 0,
    }
