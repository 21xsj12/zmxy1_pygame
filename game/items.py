"""装备数据与属性掷算。

``data/items.json`` 是装备定义表；加载时在这里统一做一次规整，
省得每个调用方各写一遍：

* ``quality`` 保留显示用的空格（``"粗 糙"``），``quality_key``
  则是去掉空格后的规范键（``"粗糙"``）。
* ``value`` 按品质取固定值（10/20/40/80/160/320）；``hjsl`` 这一项
  记价为 0，这里按 320 修正。
* 三个活动装备的 ``roll_param`` 为 10，其余为 1。它表示掷算参数，
  **不是**装备等级，也不能拿 ``showid`` 顶替。
* ``showid`` 只用于纸娃娃的外观帧。
"""
from __future__ import annotations

import json
import math
import random
import re

from .constants import DATA_DIR

ITEMS_PATH = DATA_DIR / "items.json"
ITEMS: list[dict] = json.loads(ITEMS_PATH.read_text(encoding="utf-8"))

SLOT_ORDER = ["zbfj", "zbwq", "zbsp", "zbfb"]
MAX_BAG = 25
SLOT_NAMES = {"zbfj": "防具", "zbwq": "武器", "zbsp": "饰 品", "zbfb": "法 宝"}
QUALITY_ORDER = ["粗糙", "普通", "优秀", "精良", "史诗", "传说"]
QUALITY_COLORS = {
    "粗糙": (204, 204, 204),
    "普通": (255, 255, 255),
    "优秀": (0, 255, 0),
    "精良": (0, 0, 255),
    "史诗": (102, 0, 153),
    "传说": (255, 102, 0),
}
QUALITY_VALUE = {
    "粗糙": 10, "普通": 20, "优秀": 40,
    "精良": 80, "史诗": 160, "传说": 320,
}
EVENT_FILL_COST = {"tndbthl": 800, "tndblg": 800, "zcld": 400}
EVENT_FILLS = set(EVENT_FILL_COST)


def quality_key(value: str | None) -> str:
    """返回品质的规范键（数据里带空格，如 ``"粗 糙"``）。"""
    return str(value or "").replace(" ", "").strip()


def quality_color(item: dict | None) -> tuple[int, int, int]:
    if not item:
        return QUALITY_COLORS["粗糙"]
    return QUALITY_COLORS.get(quality_key(item.get("quality")), (200, 200, 200))


def item_value(item: dict | None) -> int:
    """返回出售价（单位为魂）：数据里带有效 value 就用它，否则按品质取值。"""
    if not item:
        return 0
    value = item.get("value")
    if value is not None and int(value) > 0:
        return int(value)
    return QUALITY_VALUE.get(quality_key(item.get("quality")), 10)


def item_gongxun(item: dict | None) -> int:
    if not item:
        return 0
    value = item.get("gongxun")
    if value is not None and int(value) > 0:
        return int(value)
    return EVENT_FILL_COST.get(item.get("fill", ""), 0)


def _normalize_definition(it: dict) -> dict:
    key = quality_key(it.get("quality"))
    slot = it.get("slot", "")
    it["quality_key"] = key
    it["value"] = QUALITY_VALUE.get(key, 10)
    it["user"] = it.get("class", "")
    it["etype"] = SLOT_NAMES.get(slot, it.get("slot_name", ""))
    # 保留数据里的 0 价差异供文档与工具查看；运行时只用 value。
    it["value_original"] = 0 if it.get("fill") == "hjsl" else it["value"]
    it["gongxun"] = EVENT_FILL_COST.get(it.get("fill", ""), 0)
    it["slot_name"] = it["etype"]
    it["roll_param"] = 10 if it.get("fill") in EVENT_FILLS else 1
    it.setdefault("instruction", "")
    it.setdefault("is_strengthen", False)
    it.setdefault("icon", f"ui/items/{it.get('fill', '')}.png")
    it.setdefault("drop_icon", f"ui/drops/{it.get('fill', '')}.png")
    return it


for _it in ITEMS:
    _normalize_definition(_it)

BY_ID = {it["id"]: it for it in ITEMS}
BY_FILL: dict[str, list[dict]] = {}
for _it in ITEMS:
    BY_FILL.setdefault(_it["fill"], []).append(_it)
# 外观编号（即 showid）：纸娃娃的图层帧号。旧存档只记录了 fill，
# 因此这里保留一份 fill -> showid 的兜底表。
SHOWID_BY_FILL: dict[str, int] = {}
for _it in ITEMS:
    SHOWID_BY_FILL.setdefault(_it["fill"], int(_it.get("showid", 1)))


def appearance_id(item: dict | None) -> int:
    """返回某件已装备物品在纸娃娃上使用的外观帧号。"""
    if not item:
        return 1
    show = item.get("showid")
    if show is None:
        show = SHOWID_BY_FILL.get(item.get("fill", ""), 1)
    try:
        return max(1, int(show))
    except (TypeError, ValueError):
        return 1



def _js_round(x: float) -> int:
    return int(math.floor(x + 0.5))


def eval_stat(expr: str, param1: int = 1) -> float:
    text = (expr or "0").strip()
    if not text:
        return 0.0
    text = text.replace("Math.round", "js_round").replace("Math.random()", "random.random()")
    text = re.sub(r"Number\(\((.*?)\)\.toFixed\(2\)\)", r"js_round((\1) * 100) / 100", text)
    text = text.replace("Number(", "float(")
    try:
        return float(eval(text, {"__builtins__": {}}, {
            "js_round": _js_round, "random": random, "param1": param1,
            "float": float, "abs": abs, "min": min, "max": max,
        }))
    except Exception:
        return 0.0


def roll_item(item_id: str, param1: int | None = None, uid: int = 0) -> dict | None:
    base = BY_ID.get(item_id)
    if base is None:
        return None
    p = int(param1 if param1 is not None else base.get("roll_param", 1))
    return {
        "uid": uid,
        "id": base["id"],
        "name": base["name"],
        "fill": base["fill"],
        "slot": base["slot"],
        "class": base["class"],
        "user": base.get("user", base["class"]),
        "quality": base["quality"],
        "quality_key": base["quality_key"],
        "color": base["color"],
        "showid": int(base.get("showid", 1)),
        "level": int(base.get("showid", 1)),     # 旧字段：仅为兼容旧存档保留，算属性时从不使用
        "roll_param": p,
        "value": int(base.get("value", 0)),
        "value_original": int(base.get("value_original", base.get("value", 0))),
        "gongxun": int(base.get("gongxun", 0)),
        "etype": base.get("etype", ""),
        "instruction": base.get("instruction", ""),
        "is_strengthen": False,
        "stats": {
            "hp": int(eval_stat(base["hp"], p)),
            "mp": int(eval_stat(base["mp"], p)),
            "atk": int(eval_stat(base["atk"], p)),
            "def": int(eval_stat(base["def"], p)),
            "crit": eval_stat(base["crit"], p),
            "miss": eval_stat(base["miss"], p),
            "hp_regen": int(eval_stat(base["hp_regen"], p)),
            "mp_regen": int(eval_stat(base["mp_regen"], p)),
        },
    }


def bonuses(equipment: dict[str, dict | None]) -> dict:
    total = {"hp": 0, "mp": 0, "atk": 0, "def": 0, "crit": 0.0,
             "miss": 0.0, "hp_regen": 0, "mp_regen": 0}
    for item in equipment.values():
        if not item:
            continue
        for k in total:
            total[k] += item.get("stats", {}).get(k, 0)
    return total


def can_equip(item: dict, hero: str) -> bool:
    cls = item.get("class", "")
    if not cls:
        return True
    return (cls == "战士" and hero == "wukong") or (cls == "法师" and hero == "tangseng")


def random_drop(fill: str, hero: str = "", param1: int | None = None,
                uid: int = 0) -> dict | None:
    """掷出一件掉落装备。

    掉落**不按职业过滤**：悟空一路也可能捡到法师长袍，之后再卖掉。
    ``hero`` 参数只为保持接口兼容而保留，不能用来筛选装备表。
    """
    opts = BY_FILL.get(fill, [])
    if not opts:
        return None
    base = random.choice(opts)
    return roll_item(base["id"], base.get("roll_param", 1), uid)



def refresh_hou_dong(total_stage: int) -> None:
    """按当前总关卡重算三件活动装备的掷算参数。

    这三件活动传说装备的属性是带 ``param1`` 的表达式，按当前关卡重新生成
    等价于修改它们的掷算参数，强度随关卡推进而提高。
    """
    stage = max(1, int(total_stage))
    for item in ITEMS:
        if item.get("fill") in ("tndblg", "tndbthl", "zcld"):
            item["roll_param"] = stage


def sell_price(item: dict) -> int:
    """出售价：按品质取 10/20/40/80/160/320。"""
    return item_value(item)


def buy_price(item: dict) -> int:
    """商店与界面展示用的购买价：按出售价的 4 倍计，最低 20（背包逻辑不参与计算）。"""
    return max(20, item_value(item) * 4)


def exchange_catalog() -> list[dict]:
    """功勋兑换页固定的三件装备及其兑换价格。"""
    out = []
    for fill, cost in (("tndbthl", 800), ("tndblg", 800), ("zcld", 400)):
        opts = BY_FILL.get(fill, [])
        if not opts:
            continue
        base = opts[0]
        out.append({
            "fill": fill,
            "name": base["name"],
            "cost": cost,
            "quality": base["quality"],
            "quality_key": base["quality_key"],
            "icon": item_icon(base),
        })
    return out


def roll_exchange(fill: str, uid: int = 0) -> dict | None:
    opts = BY_FILL.get(fill, [])
    if not opts:
        return None
    return roll_item(opts[0]["id"], 10, uid)


def item_icon(item: dict) -> str:
    return item.get("icon") or f"ui/items/{item.get('fill', '')}.png"


def drop_icon(item: dict) -> str:
    return item.get("drop_icon") or f"ui/drops/{item.get('fill', '')}.png"


def shop_price(item: dict) -> int:
    return max(20, sell_price(item) * 4)


SHOP_IDS = [
    "ptxzf_warrior", "ptxzg_warrior", "ptjs_mage", "ptsmz_mage",
    "yxxzf_warrior", "yxxzg_warrior", "yxjs_mage", "yxsmz_mage",
    "dslj_any", "tsgl_any",
]


def starter_items(hero: str) -> tuple[list[dict], dict]:
    ids = ["ccxzf_warrior", "ccxzg_warrior"] if hero == "wukong" else ["ccjs_mage", "ccsmz_mage"]
    inventory = []
    equipment = {}
    for i, item_id in enumerate(ids, 1):
        item = roll_item(item_id, uid=i)
        if item:
            if item["slot"] == "zbfj":
                inventory.append(item)
            else:
                equipment[item["slot"]] = item
    return inventory, equipment


def normalize_saved_item(item: dict | None) -> dict | None:
    """为旧存档补齐后来新增的字段。

    已掷出的属性值一律不重算：旧存档保留当初的数值。
    这里只补显示、序列化与价格换算用的元数据。
    """
    if not isinstance(item, dict):
        return item
    fill = str(item.get("fill", ""))
    base = BY_ID.get(item.get("id"))
    if base is None:
        opts = BY_FILL.get(fill)
        base = opts[0] if opts else None
    if base is not None:
        item.setdefault("quality", base["quality"])
        item.setdefault("color", base["color"])
        item.setdefault("showid", base.get("showid", 1))
        item.setdefault("etype", base.get("etype", ""))
        item.setdefault("value", base.get("value", 0))
        item.setdefault("gongxun", base.get("gongxun", 0))
        item.setdefault("roll_param", base.get("roll_param", 1))
        item.setdefault("instruction", base.get("instruction", ""))
        item.setdefault("icon", item_icon(base))
    key = quality_key(item.get("quality"))
    item.setdefault("quality_key", key)
    item.setdefault("user", item.get("class", ""))
    if int(item.get("value", 0) or 0) <= 0:
        item["value"] = QUALITY_VALUE.get(key, 10)
    item.setdefault("value", QUALITY_VALUE.get(key, 10))
    item.setdefault("gongxun", EVENT_FILL_COST.get(fill, 0))
    item.setdefault("etype", SLOT_NAMES.get(item.get("slot", ""), ""))
    item.setdefault("roll_param", 10 if fill in EVENT_FILLS else 1)
    item.setdefault("instruction", "")
    item.setdefault("is_strengthen", False)
    item.setdefault("stats", {"hp": 0, "mp": 0, "atk": 0, "def": 0,
                              "crit": 0.0, "miss": 0.0,
                              "hp_regen": 0, "mp_regen": 0})
    return item

