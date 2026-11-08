"""本地 JSON 存档文件：三个存档位（唐僧 / 悟空 / 双人）。"""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from pathlib import Path

from .constants import START_LEVEL, SAVE_DIR
from .items import normalize_saved_item

SAVE_SLOTS = 3
SAVE_PATH = SAVE_DIR / "slot1.json"
SETTINGS_PATH = SAVE_DIR / "settings.json"
SAVE_VERSION = 1
DEFAULT_SETTINGS = {"muted": False, "music": 0.35, "sfx": 0.6}

# 进度记录的是“最前沿”的关卡（当前大关与当前小关），而不是“所有已通关的关卡”。
# 把它一起存进存档，重玩旧关卡时才不会让进度高亮倒退。
# 旧存档里只有已通关列表时，交给 frontier_from_cleared 换算出前沿位置。
MAIN_IDS = ["1-1", "1-2", "1-3", "2-1", "2-2", "2-3",
            "3-1", "3-2", "3-3", "4-1"]


def frontier_from_cleared(cleared) -> tuple[int, int]:
    """把旧存档里的已通关关卡列表换算成进度前沿（大关、小关）。"""
    done = set(cleared or ())
    for lid in MAIN_IDS:
        if lid not in done:
            stage, level = lid.split("-")
            return int(stage), int(level)
    return 4, 1


def slot_path(slot: int = 1) -> Path:
    return SAVE_DIR / f"slot{max(1, min(SAVE_SLOTS, int(slot)))}.json"


def default_player(level: int | None = None) -> dict:
    level = max(1, int(level if level is not None else START_LEVEL))
    return {
        "hero": "wukong",
        "equipment": {},
        "skills": {"active": [False] * 5,
                   "passive": [0, 0, 0, 0, 0]},
        "level": level,
        "exp": 0,
        "hp": 80 + 50 * (level - 1),
        "max_hp": 80 + 50 * (level - 1),
        "mp": 50 + 20 * (level - 1),
        "max_mp": 50 + 20 * (level - 1),
    }


def new_save(slot: int = 1) -> dict:
    return {
        "version": SAVE_VERSION,
        "slot": int(slot),
        "hero": None,
        "soul": 0,
        "myscore": 0,
        "gongxun": 2000,
        "next_uid": 1,
        "level_id": "1-1",
        "cleared": False,
        "cleared_levels": [],
        "cur_stage": 1,
        "cur_level": 1,
        "opening_seen": False,
        "game_cleared": False,
        "saved_at": None,
        "player": default_player(),
        "inventory": [],
    }


def load_save(path: Path | None = None, slot: int = 1) -> dict | None:
    path = path or slot_path(slot)
    for candidate in (path, path.with_name(path.name + ".bak")):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("version") != SAVE_VERSION:
            continue
        player = data.setdefault("player", default_player())
        data.setdefault("level_id", "1-1")
        data.setdefault("cleared_levels", [])
        data.setdefault("inventory", [])
        data.setdefault("soul", 0)
        data.setdefault("myscore", 0)
        data.setdefault("gongxun", 2000)
        data.setdefault("next_uid", 1)
        data.setdefault("opening_seen", True)
        data.setdefault("game_cleared", False)
        try:
            stage = int(data.get("cur_stage", 0))
            level = int(data.get("cur_level", 0))
        except (TypeError, ValueError):
            stage, level = 0, 0
        if not (1 <= stage <= 4 and 1 <= level <= 3):
            stage, level = frontier_from_cleared(data.get("cleared_levels", []))
            data["cur_stage"], data["cur_level"] = stage, level
        for key, value in default_player().items():
            player.setdefault(key, value)
        if not isinstance(data.get("inventory"), list):
            data["inventory"] = []
        for item in data.get("inventory", []):
            normalize_saved_item(item)
        equipment = player.get("equipment")
        if not isinstance(equipment, dict):
            equipment = {}
            player["equipment"] = equipment
        for key, item in list(equipment.items()):
            if item is not None:
                equipment[key] = normalize_saved_item(item)
        return data
    return None


def write_save(data: dict, path: Path | None = None, slot: int = 1) -> None:
    path = path or slot_path(slot)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = dict(data)
    data["version"] = SAVE_VERSION
    data["saved_at"] = datetime.now().isoformat(timespec="seconds")
    tmp = path.with_name(path.name + ".tmp")
    try:
        if path.exists():
            shutil.copy2(path, path.with_name(path.name + ".bak"))
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
            fh.flush()
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def update_player_from_save(data: dict | None) -> dict:
    if not data:
        return default_player()
    p = dict(default_player())
    p.update(data.get("player", {}))
    return p


def list_saves() -> dict[int, dict | None]:
    return {slot: load_save(slot=slot) for slot in range(1, SAVE_SLOTS + 1)}


def delete_save(slot: int) -> None:
    p = slot_path(slot)
    for candidate in (p, p.with_name(p.name + ".bak")):
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass


def load_settings() -> dict:
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    out = dict(DEFAULT_SETTINGS)
    out.update(data)
    return out


def write_settings(data: dict) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
