"""主菜单封面与选角界面的数据层。

坐标数据来自 ``docs/assets/menu/*.json``，由 ``analysis/export_menu_ui.py``
统一导出，没有手工微调；本文件不依赖 pygame，便于在无窗口环境下
做单元测试。所有数值都以数据文件为准，代码里不写死坐标，
避免界面调整时需要同时修改两处。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from .constants import ASSETS_DIR

MENU_DIR = ASSETS_DIR / "menu"

# 存档槽位与角色对应：1 为唐僧，2 为孙悟空，3 为双人模式。
SLOT_TANGSENG = 1
SLOT_WUKONG = 2
SLOT_DOUBLE = 3

# 选角界面的角色编号：1 代表孙悟空，2 代表唐僧。
ROLE_WUKONG = 1
ROLE_TANGSENG = 2

ROLE_TO_HERO = {ROLE_WUKONG: "wukong", ROLE_TANGSENG: "tangseng"}
HERO_TO_ROLE = {v: k for k, v in ROLE_TO_HERO.items()}
HERO_SLOT = {"tangseng": SLOT_TANGSENG, "wukong": SLOT_WUKONG}
HERO_LABEL = {"wukong": "孙悟空", "tangseng": "唐僧"}

# 主菜单元素的绘制层级顺序（从下到上），括号内为素材层序号。
# 各元素层号依次为：165,169,173,177,181,185,189,193,199,203,207,211,216,220
DRAW_ORDER = [
    "simpleGame", "doubleGame", "successRoom", "gameShop", "gameHelp",
    "gameRecommend", "aboutUs", "newGame", "continueGame", "backbtn",
    "tangseng", "wukong", "btnCadpa", "btnGoBack",
]
MAIN_ITEMS = ["simpleGame", "doubleGame", "successRoom", "gameShop",
              "gameHelp", "gameRecommend", "aboutUs"]

# 隐藏菜单时，七个主菜单条目整体移出画面范围。
MODE_HIDDEN = list(MAIN_ITEMS)


@dataclass
class MenuItem:
    name: str
    label: str
    button: str
    x: float
    y: float
    hit: tuple[float, float, float, float]
    states: dict[str, str] = field(default_factory=dict)
    dx: dict[str, float] = field(default_factory=dict)
    dy: dict[str, float] = field(default_factory=dict)

    def png(self, state: str) -> str:
        return "menu/" + self.states[state]

    def state_pos(self, state: str, x: float, y: float) -> tuple[int, int]:
        return (round(x + self.dx.get(state, 0.0)),
                round(y + self.dy.get(state, 0.0)))

    def hit_rect(self, x: float, y: float):
        hx, hy, hx2, hy2 = self.hit
        return (round(x + hx), round(y + hy),
                round(hx2 - hx), round(hy2 - hy))


@dataclass
class Card:
    slot: int
    key: str
    label: str
    role: int
    selectable: bool
    button: str | None
    sprite: str
    x: int
    y: int
    w: int
    h: int
    color_png: str
    gray_png: str
    gray_only: bool
    hit: tuple[float, float, float, float] | None
    button_x: float | None
    button_y: float | None

    @property
    def color_path(self) -> str:
        return "menu/" + self.color_png

    @property
    def gray_path(self) -> str:
        return "menu/" + self.gray_png

    def hit_rect(self):
        if self.hit is None:
            return None
        hx, hy, hw, hh = self.hit
        return (round(self.x + hx), round(self.y + hy),
                round(hw), round(hh))


@dataclass
class MenuData:
    stage_w: int
    stage_h: int
    background: str
    background_pos: tuple[int, int]
    version_text: dict
    items: dict[str, MenuItem]
    stage_fps: int = 24

    def item(self, name: str) -> MenuItem:
        return self.items[name]


@dataclass
class SelectData:
    stage_w: int
    stage_h: int
    cards: list[Card]
    tags: dict[str, dict]
    tag_anchor: dict


_menu_cache: MenuData | None = None
_select_cache: SelectData | None = None


def _read(name: str) -> dict:
    return json.loads((MENU_DIR / name).read_text(encoding="utf-8"))


def load_menu() -> MenuData:
    global _menu_cache
    if _menu_cache is not None:
        return _menu_cache
    raw = _read("menu_ui.json")
    items: dict[str, MenuItem] = {}
    for it in raw["items"]:
        states = {k: v["png"] for k, v in it["states"].items()}
        dx = {k: float(v["dx"]) for k, v in it["states"].items()}
        dy = {k: float(v["dy"]) for k, v in it["states"].items()}
        hit = tuple(float(v) for v in it.get("hit_bounds") or (0, 0, 0, 0))
        items[it["name"]] = MenuItem(
            name=it["name"], label=it["label"], button=str(it["button"]),
            x=float(it["x"]), y=float(it["y"]), hit=hit,
            states=states, dx=dx, dy=dy,
        )
    bg = raw["background"]
    _menu_cache = MenuData(
        stage_w=int(raw["stage"]["w"]), stage_h=int(raw["stage"]["h"]),
        background=bg["bitmap"], background_pos=(int(bg["x"]), int(bg["y"])),
        version_text=raw.get("version_text", {}), items=items,
        stage_fps=int(raw["stage"].get("fps", 24)),
    )
    return _menu_cache


def load_select() -> SelectData:
    global _select_cache
    if _select_cache is not None:
        return _select_cache
    raw = _read("select_role.json")
    cards: list[Card] = []
    for c in raw["cards"]:
        hit = c.get("hit")
        cards.append(Card(
            slot=int(c["slot"]), key=c["key"], label=c["label"],
            role=int(c["role"]), selectable=bool(c.get("selectable")),
            button=c.get("button"), sprite=str(c["sprite"]),
            x=int(c["x"]), y=int(c["y"]), w=int(c["w"]), h=int(c["h"]),
            color_png=c["color_png"], gray_png=c["gray_png"],
            gray_only=bool(c.get("gray_only")),
            hit=tuple(float(v) for v in hit) if hit else None,
            button_x=(float(c["button_x"]) if c.get("button_x") is not None else None),
            button_y=(float(c["button_y"]) if c.get("button_y") is not None else None),
        ))
    _select_cache = SelectData(
        stage_w=int(raw["stage"]["w"]), stage_h=int(raw["stage"]["h"]),
        cards=cards, tags=raw["tags"], tag_anchor=raw["tag_anchor"],
    )
    return _select_cache


def original_actions() -> dict[str, str]:
    """各按钮名对应的实际动作，并标明该按钮是否真的绑定了监听。"""
    return {
        "simpleGame": "mode_single",
        "doubleGame": "mode_double",
        "successRoom": "toast",
        "gameShop": "toast",
        "gameHelp": "help",
        "gameRecommend": "none",
        "aboutUs": "about",
        "newGame": "new_game",
        "continueGame": "continue",
        "backbtn": "back",
        "tangseng": "slot_tangseng",
        "wukong": "slot_wukong",
        "btnCadpa": "cadpa",
        "btnGoBack": "go_back",
    }
