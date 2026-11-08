"""应用主循环与场景切换。"""
from __future__ import annotations

import os
import sys

import pygame

from .arena import ArenaWorld
from . import ui
from .assets import Assets
from .backpack import Backpack
from .constants import (ASSETS_DIR, FPS, HEIGHT, MAIN_LEVELS, TITLE, WIDTH)
from .cover_scene import CoverScene
from .entities import hero_stats
from .hero_select import HeroSelectScene
from .items import MAX_BAG
from .level import load_level
from .loading import LoadingView, load_assets_with_loading
from .map_scene import MapScene
from .menu_data import (HERO_SLOT, ROLE_TO_HERO, SLOT_DOUBLE,
                        SLOT_TANGSENG, SLOT_WUKONG)
from .narration import NARRATION, Narration
from .save import (default_player, delete_save, frontier_from_cleared, list_saves,
                   load_save, load_settings, new_save, update_player_from_save,
                   write_save, write_settings)
from .setmenu import SetMenu
from .skill_page import SkillPage
from .skills import active_cost, default_state, normalize, passive_cost
from .world import World


# 帮助界面返回按钮的命中区域：按素材在界面上的实际摆放尺寸与位置量得
# 矩形约为 (842,10)-(934,51)，这里直接沿用精确值，以保证点击反馈与看到的一致；
# 早期实现用过的 824,0,118,60 只是刻意放宽的兜底区域，容易把周边元素也误判为点击。
HELP_BACK_RECT = pygame.Rect(842, 10, 92, 41)
ABOUT_BACK_RECT = pygame.Rect(806, 506, 134, 76)


class GameApp:
    def __init__(self, headless: bool = False, audio: bool = True, warp: float | None = None,
                 start_level: int | None = None, level_id: str = "1-1",
                 fullscreen: bool = False, show_loading: bool | None = None,
                 loading_stats: bool = False):
        self.headless = headless
        self.start_level = start_level
        self.level_id = level_id
        self.warp = warp
        pygame.init()
        flags = 0 if headless else pygame.SCALED
        if fullscreen:
            flags |= pygame.FULLSCREEN
        self.screen = pygame.display.set_mode((WIDTH, HEIGHT), flags)
        pygame.display.set_caption(TITLE)
        self.clock = pygame.time.Clock()
        if show_loading is None:
            # 无窗口的工具与测试不需要启动加载画面；真正显示窗口时才需要，
            # 加载进度条也只在窗口模式下出现。
            show_loading = not headless
        self.loading: LoadingView | None = None
        if show_loading:
            self.loading = LoadingView.try_create(ASSETS_DIR)
        if self.loading is not None:
            self.assets = load_assets_with_loading(
                self.screen, self.loading, audio=audio,
                print_stats=loading_stats,
            )
        else:
            if show_loading:
                self._draw_boot_loading()
            self.assets = Assets(audio=audio)
        self.settings = load_settings()
        self.assets.muted = bool(self.settings.get("muted", False))
        self.assets.music_volume = float(self.settings.get("music", 0.35))
        self.assets.sfx_volume = float(self.settings.get("sfx", 0.6))
        self.assets.play_music(self.assets.music_volume, "begin")
        self.settings_sel = 0
        self.state = "title"
        self.title_t = 0.0
        self.menu_sel = 0
        self.title_msg = ""
        self.title_msg_t = 0.0
        self.air_mode = bool(self.settings.get("air_mode", False))
        self.battle: World | None = None
        self.arena: ArenaWorld | None = None
        self.paused = False
        self.pause_sel = 0
        self.setmenu = SetMenu(self.assets, lambda: self.assets.muted)
        self.result_won = False
        self.result_sel = 0
        # 仅用鼠标操作时不显示键盘焦点框；
        # 只有用 WASD 或方向键导航后才出现。
        self.result_keyboard_focus = False
        # 场景切换后的第一帧要吃掉鼠标点击（失败面板/按钮不穿透，T15 A-11）
        self._pointer_locked = False
        self.map_sel = 0
        self.map_msg = ""
        self.map_scene: MapScene | None = None
        self.save_cd = 0.0
        # 游戏人数与模式标记：``play_num`` / ``game_mode`` 为 1 表示单人，2 表示双人。
        self.play_num = 1
        self.game_mode = 1
        self.slot = 1
        self.slot_sel = 0
        self.hero_sel = 0
        self.inv_sel = 0
        self.inv_msg = ""
        self.skill_sel = 0
        self.skill_tab = 0
        self.skill_msg = ""
        self.skill_page: SkillPage | None = None
        self.help_back = "title"
        self.help_back_hover = False
        self.about_back_hover = False
        self.menu_back = "map"
        self.save_data = load_save(slot=self.slot)
        self.has_save = self.save_data is not None
        self.backpack = Backpack(self)
        self.opening_page = 0
        self.opening: Narration | None = None
        self.ending: Narration | None = None
        # 仅为兼容会打印该字段的工具与探测脚本而保留；
        # 当前流程没有“结局画面停留等待按键”的第二个阶段，
        # 结局播完最后一帧就直接回到地图。
        self.ending_done = False
        self.demo_t = 0.0
        self.demo_next_attack = 0.4
        self.demo_next_jump = 2.2
        self.demo_w_until = 0.0
        # 开场剧情的已播放标记只在本次运行期间有效：本次已经播过开场后，
        # 之后再选择“新游戏”会直接进入选关地图。
        self.opening_seen_session = False
        # 双人模式的封面与选角界面已经完整（需要依次选择两名角色），
        # 但双人战役本身尚未实现，暂不在本版本范围内。
        self.double_campaign_ready = False
        self.last_double_pick: list[str] = []
        self.cover = CoverScene(self)
        self.hero_select: HeroSelectScene | None = None

    # ------------------------------------------------------------------ 场景
    def _draw_boot_loading(self) -> None:
        """加载画面占位提示：在资源加载期间显示。"""
        self.screen.fill((8, 26, 44))
        try:
            font = pygame.font.Font(r"C:\Windows\Fonts\msyh.ttc", 26)
        except Exception:
            font = pygame.font.Font(None, 26)
        img = font.render("游戏加载中…", True, (225, 240, 180))
        self.screen.blit(img, img.get_rect(center=(WIDTH // 2, HEIGHT // 2)))
        pygame.display.flip()

    def start_battle(self, saved_player: dict | None = None, level_id: str | None = None):
        if level_id:
            self.level_id = level_id
        # 场景切换后的第一帧鼠标点击要被新场景吃掉（失败面板/地图按钮不穿透）。
        self._pointer_locked = True
        if saved_player is None:
            saved_player = (update_player_from_save(self.save_data)
                            if self.save_data is not None
                            else default_player(self.start_level))
        level = load_level(self.level_id)
        inventory = (self.save_data.get("inventory")
                     if self.save_data is not None else None)
        if not isinstance(inventory, list):
            inventory = []
            if self.save_data is not None:
                self.save_data["inventory"] = inventory
        self.battle = World(self.assets, level, saved_player, inventory=inventory,
                            soul_data=self.save_data)
        if self.warp is not None:
            self.battle.player.x = self.warp
            self.battle.player.y = level.ground_y
            idx = 0
            while idx < len(self.battle.stops) and self.battle.stops[idx].x < self.warp:
                idx += 1
            self.battle.stop_index = idx
            self.battle.camera.reset(
                max(0.0, min(self.warp - WIDTH / 2, level.max_camera_x)))
        self.state = "battle"
        self._exit_map()
        self.skill_page = None
        self.paused = False
        self.pause_sel = 0
        self.setmenu.close()
        self.assets.play_music(self.assets.music_volume, level.music)

    def save_clear(self) -> None:
        assert self.battle is not None
        p = self.battle.player
        data = self.save_data or new_save(self.slot)
        lid = self.battle.level.id
        data["hero"] = p.hero
        data["level_id"] = lid
        data["cleared"] = True
        # 先把当前进度边界读出来，再把这次通关记录写入 ``cleared_levels``：
        # 只有通关当前边界关卡才会推进进度。
        cur_stage, cur_level = self.progress
        cleared = list(data.get("cleared_levels", []))
        if lid not in cleared:
            cleared.append(lid)
        data["cleared_levels"] = cleared
        # 通关结算的进度推进规则：只有通关当前边界关卡才会
        # 推进大关与小关进度；重打已通关的旧关卡不会改变任何进度。
        # 第 4 大关最高只到 4-1。
        if lid in MAIN_LEVELS:
            try:
                stage, level = (int(x) for x in lid.split("-"))
            except ValueError:
                stage = level = 0
            if stage == cur_stage:
                if cur_stage <= 3 and level == cur_level:
                    if level == 3:
                        cur_stage, cur_level = cur_stage + 1, 1
                    elif level < 3:
                        cur_level += 1
                elif cur_stage == 4:
                    cur_level = 1
        data["cur_stage"] = cur_stage
        data["cur_level"] = cur_level
        # 拾取时已经把物品追加到实时背包列表（容量也在那里检查过）；
        # 这里保留同一列表引用，不能悄悄丢弃超出容量的物品。
        data["inventory"] = list(getattr(self.battle, "inventory",
                                        data.get("inventory", [])))[:MAX_BAG]
        # 吸收魂时已经把魂值记到实时存档字典里
        # （读取的是实时背包对象里的魂值字段），
        # 只有本场战斗没有拿到这个实时字典时才在这里补加 soul_gain。
        soul_gain = int(getattr(self.battle, "soul_gain", 0))
        live_soul = getattr(self.battle, "soul_data", None)
        if live_soul is None or live_soul is not data:
            data["soul"] = int(data.get("soul", 0)) + soul_gain
        data["gongxun"] = int(data.get("gongxun", 0)) + int(self.battle.gongxun)
        self.battle.soul_gain = 0
        self.battle.gongxun = 0
        data["player"] = {
            "hero": p.hero,
            "equipment": p.equipment,
            "skills": p.skills,
            "level": p.level_no,
            "exp": p.exp,
            "hp": p.max_hp,
            "max_hp": p.max_hp,
            "mp": p.max_mp,
            "max_mp": p.max_mp,
        }
        write_save(data, slot=self.slot)
        self.save_data = data
        self.has_save = True

    def _bank_battle_currency(self) -> None:
        """结算已结束战斗中获得经验、等级、魂值与功勋，并写回存档。

        经验与等级在战斗结束时就已经同步到当前玩家数据上，因此
        关卡失败或退回选关地图也会保留经验；只有无双槽是本场战斗内的
        临时数据，不在此持久化。
        """
        if self.battle is None:
            return
        sync = getattr(self.battle, "sync_player_progress", None)
        if sync is not None:
            sync()
        data = self.save_data or new_save(self.slot)
        soul = int(getattr(self.battle, "soul_gain", 0))
        merit = int(getattr(self.battle, "gongxun", 0))
        live_soul = getattr(self.battle, "soul_data", None)
        if live_soul is None or live_soul is not data:
            data["soul"] = int(data.get("soul", 0)) + soul
        data["gongxun"] = int(data.get("gongxun", 0)) + merit
        self.battle.soul_gain = 0
        self.battle.gongxun = 0
        self.save_data = data
        write_save(data, slot=self.slot)

    def _player_snapshot(self) -> dict:
        p = self.battle.player
        return {
            "hero": p.hero, "level": p.level_no, "exp": p.exp,
            "hp": p.hp, "mp": p.mp, "equipment": p.equipment,
            "skills": p.skills,
        }

    def finish_battle(self) -> None:
        assert self.battle is not None
        battle = self.battle
        if battle.transition_target:
            # 通过传送门切到下一关时不会走通关存档流程；因此必须
            # 先结算本关获得的魂值与功勋，否则本次吸收的
            # 所有魂珠都会在切关时丢失。
            self._bank_battle_currency()
            snapshot = self._player_snapshot()
            self.start_battle(snapshot, level_id=battle.transition_target)
            return
        self.result_won = battle.won
        self.result_sel = 0
        self.result_keyboard_focus = False
        if self.result_won:
            self.save_clear()
            if battle.level.id == "4-1":
                self.save_data["game_cleared"] = True
                write_save(self.save_data, slot=self.slot)
                self.start_ending()
                return
            if (getattr(battle, "portal_entered", False)
                    and getattr(battle, "portal_clear_done", False)):
                # 普通传送门通关会保存进度并直接切换到选关地图；
                # 因此只有失败，
                # 以及非传送门（强制结算）通关的情况，
                # 才会留在结算面板。
                self.to_map()
                return
        else:
            # 失败时保留已经获得的经验、等级、魂值与功勋；
            # 经验与等级在获取时便已同步到玩家数据，故失败不会丢失。
            # 无双槽（player.gxp）故意不持久化。
            self._bank_battle_currency()
        self.state = "result"

    def start_arena(self) -> None:
        self._exit_map()
        self.arena = ArenaWorld(self.assets)
        self.battle = None
        self.skill_page = None
        self.state = "arena"
        self.assets.play_music(self.assets.music_volume, "bg1")

    def to_title(self) -> None:
        if self.battle is not None:
            # 中途离开关卡会保留经验、等级与魂值；无双槽仅在战斗内有效。
            self._bank_battle_currency()
        self.state = "title"
        self.battle = None
        self.arena = None
        self.skill_page = None
        self._exit_map()
        self.paused = False
        self.setmenu.close()
        self.title_msg = ""
        self.hero_select = None
        self.cover.to_main()
        self.assets.play_music(self.assets.music_volume, "begin")

    def result_button_rects(self) -> tuple[pygame.Rect, pygame.Rect]:
        """失败面板两个按钮的矩形（绘制高亮与鼠标命中共用同一份数据）。

        矩形与 ``ui/fail.png`` 中已烘焙的橙色文字对齐
        （重新挑战 x=310..424，返回地图 x=470..588，y=398..422）。
        """
        return ui.RESULT_BUTTON_RECTS[0], ui.RESULT_BUTTON_RECTS[1]

    def hit_test_result(self, pos: tuple[int, int]) -> int | None:
        """返回命中的按钮索引（0 = 重新挑战，1 = 返回地图）。"""
        for i, rect in enumerate(self.result_button_rects()):
            if rect.collidepoint(pos):
                return i
        return None

    def to_map(self) -> None:
        # 失败面板的“返回地图”点击不得穿透到选关地图的关卡节点。
        self._pointer_locked = True
        if self.battle is not None:
            # 从战斗进入选关地图（通过暂停菜单或失败面板）时，
            # 必须保留本次挑战中已经获得的经验与等级。
            self._bank_battle_currency()
        self._exit_map()
        self.state = "map"
        self.battle = None
        self.arena = None
        self.skill_page = None
        self.paused = False
        self.setmenu.close()
        stage, level = self.progress
        current = f"{stage}-{level}"
        self.map_sel = (ui.MAP_SELECT_ORDER.index(current)
                        if current in ui.MAP_SELECT_ORDER else 0)
        self.map_scene = MapScene(self)
        self.assets.play_music(self.assets.music_volume, "begin")

    def _ensure_map_scene(self) -> MapScene:
        if self.map_scene is None:
            if not 0 <= self.map_sel < len(ui.MAP_SELECT_ORDER):
                self.map_sel = 0
            self.map_scene = MapScene(self)
        return self.map_scene

    def _exit_map(self) -> None:
        if self.map_scene is not None:
            self.map_scene.exit()
            self.map_scene = None

    # ------------------------------------------------------- 封面/选角流程
    def slot_exists(self, slot: int) -> bool:
        return load_save(slot=int(slot)) is not None

    def has_save_for_mode(self, mode: int) -> bool:
        """判断指定模式是否有可继续的存档：单人看任意角色槽位，双人看 2 号槽位。"""
        if int(mode) == 2:
            return self.slot_exists(SLOT_DOUBLE)
        return self.slot_exists(SLOT_TANGSENG) or self.slot_exists(SLOT_WUKONG)

    def toast(self, text: str, ttl: float = 1.8) -> None:
        self._msg(text, ttl)

    def to_cadpa(self) -> None:
        # 适龄提示是全屏静态公告；本实现没有对应图形面板，
        # 因此直接用文字提示展示公告文案。
        self.toast("适龄提示：12+  CADPA", 2.2)

    def _player_payload(self, hero: str, level: int | None = None) -> dict:
        lv = max(1, int(level if level is not None else (self.start_level or 1)))
        stats = hero_stats(hero, lv)
        return {
            "hero": hero,
            "equipment": {},
            "skills": default_state(hero),
            "level": lv,
            "exp": 0,
            "hp": stats["max_hp"],
            "max_hp": stats["max_hp"],
            "mp": stats["max_mp"],
            "max_mp": stats["max_mp"],
        }

    def _new_save_for_hero(self, hero: str, slot: int, level: int | None = None) -> dict:
        data = new_save(slot)
        data["slot"] = int(slot)
        data["hero"] = hero
        data["player"] = self._player_payload(hero, level)
        data["inventory"] = []
        return data

    def start_hero_select(self, play_num: int = 1, game_mode: int = 1) -> None:
        """进入角色选择流程：保存人数与模式，切换到选角场景。"""
        self.play_num = int(play_num)
        self.game_mode = int(game_mode)
        self.hero_select = HeroSelectScene(self, self.play_num, self.game_mode)
        self.state = "heroes"
        self.battle = None
        self.arena = None
        self._exit_map()
        # 菜单与选角界面继续播放菜单背景音乐（播放接口会避免重新开始
        # 同一首“begin”）；因此这里不要调用 stop_music。

    def to_heroes(self) -> None:
        """兼容旧调用方的别名方法。"""
        self.start_hero_select(self.play_num, self.game_mode)

    def finish_hero_select(self, roles: list[int], play_num: int,
                           game_mode: int) -> None:
        """选角完成后的收尾：创建新存档并进入开场剧情或选关地图。"""
        if not roles:
            return
        had_save = self.has_save_for_mode(play_num)
        self.play_num = int(play_num)
        self.game_mode = int(game_mode)
        if self.play_num == 2:
            r1 = int(roles[0])
            r2 = int(roles[1]) if len(roles) > 1 else r1
            h1, h2 = ROLE_TO_HERO[r1], ROLE_TO_HERO[r2]
            if not self.double_campaign_ready:
                # 双人战役（两名英雄同场）尚未实现；
                # 这里仍然保留双人选角流程，但要明确告知玩家，
                # 而不是悄悄按单人战斗开局。
                self.last_double_pick = [h1, h2]
                self.hero_select = None
                self.to_title()
                self.toast("双人战役暂未开放：已按原版完成双人选角", 2.8)
                return
            slot = SLOT_DOUBLE
            data = self._new_save_for_hero(h1, slot)
            data["hero2"] = h2
            data["player2"] = self._player_payload(h2, data["player"].get("level"))
            data["role2"] = r2
        else:
            role = int(roles[0])
            hero = ROLE_TO_HERO[role]
            slot = HERO_SLOT[hero]
            data = self._new_save_for_hero(hero, slot)
        self.slot = slot
        self.save_data = data
        # 选角流程本身不写存档；
        # 存档由通关结算或地图上的保存操作写入。
        self.has_save = False
        self.hero_select = None
        if had_save or self.opening_seen_session:
            self.to_map()
        else:
            self.to_opening()

    def continue_from_slot(self, slot: int, play_num: int = 1) -> bool:
        """从指定槽位读取存档并继续游戏；成功时进入选关地图。"""
        if int(play_num) == 2 and not self.double_campaign_ready:
            self.toast("双人战役暂未开放")
            return False
        data = load_save(slot=int(slot))
        if data is None:
            self.toast("没有找到该存档")
            return False
        self.slot = int(slot)
        self.save_data = data
        self.has_save = True
        self.play_num = int(play_num)
        self.game_mode = 1 if self.play_num == 1 else 2
        self.hero_select = None
        self.to_map()
        return True

    def to_slots(self) -> None:
        self.state = "slots"
        self.slot_sel = max(0, self.slot - 1)
        self.assets.stop_music()

    def to_help(self, back: str = "title") -> None:
        self.help_back = back
        self.help_back_hover = False
        if back == "pause":
            # 帮助界面只是覆盖在已暂停的设置菜单之上。
            self.paused = True
        self.state = "help"

    def to_about(self) -> None:
        self.about_back_hover = False
        self.state = "about"

    def to_settings(self) -> None:
        self.state = "settings"
        self.settings_sel = 0

    def to_inventory(self, back: str = "map") -> None:
        if back == "map":
            self._exit_map()
        self.state = "inventory"
        self.menu_back = back
        self.inv_sel = 0
        self.inv_msg = ""
        self.backpack.open(back)

    def _narration(self, key: str) -> Narration | None:
        """根据 data/narration.json 构建剧情播放器。

        素材缺失或损坏时回退到 ui.draw_opening / ui.draw_ending
        的静态卡片绘制，避免场景切换直接崩溃。
        """
        spec = NARRATION.get(key)
        if not spec:
            return None
        try:
            return Narration(spec, ASSETS_DIR)
        except (KeyError, OSError, pygame.error, ValueError):
            return None

    def to_opening(self) -> None:
        self.state = "opening"
        self.opening_page = 0
        # 开场剧情加入后即标记为已播放；初始化数据时不会重置该标记，
        # 因此它只在本次运行期间有效。
        self.opening_seen_session = True
        # 开场演出：插画在 49 帧内淡入，
        # 剧情文字以每帧 3.4 像素滚动。
        self.opening = self._narration("opening")
        # 开场剧情不主动切换音乐：封面场景的“begin”曲目继续播放，
        # 直到进入地图或战斗时才切换。

    def start_ending(self) -> None:
        self.state = "ending"
        self.ending_done = False
        # 结局演出：胜利画面加总长 1205 帧的制作者名单时间线。
        # 结局不主动切换音乐：4-1 的“bg4”曲目持续播放，
        # 直到调用 to_map() 切换到选关地图的“begin”曲目。
        self.ending = self._narration("ending")

    def to_skill_shop(self, back: str = "map") -> None:
        if back == "map":
            self._exit_map()
        if self.save_data is None:
            self.to_map()
            return
        self.menu_back = back
        self.skill_sel = 0
        self.skill_tab = 0
        self.skill_msg = ""
        self.skill_page = SkillPage(self, back)
        self.state = "skills"

    # ------------------------------------------------------------------ 存档
    @property
    def progress(self) -> tuple[int, int]:
        """当前进度边界：大关与小关（均从 1 开始计数）。"""
        data = self.save_data or {}
        try:
            stage = int(data.get("cur_stage", 0))
            level = int(data.get("cur_level", 0))
        except (TypeError, ValueError):
            stage, level = 0, 0
        if 1 <= stage <= 4 and 1 <= level <= 3:
            # 若已通关集合比记录的进度边界更靠前，则自动修正进度
            # （兼容早期工具或手工修改过的存档）。
            derived = frontier_from_cleared(data.get("cleared_levels", []))
            return max((stage, level), derived)
        return frontier_from_cleared(data.get("cleared_levels", []))

    def is_level_unlocked(self, level_id: str) -> bool:
        if level_id == "arena":
            return self.play_num == 2
        if level_id not in MAIN_LEVELS:
            return False
        try:
            stage, level = (int(x) for x in level_id.split("-"))
        except ValueError:
            return False
        cur_stage, cur_level = self.progress
        return stage < cur_stage or (stage == cur_stage and level <= cur_level)

    def new_game_for_hero(self, hero: str, slot: int | None = None) -> None:
        self.slot = slot or HERO_SLOT[hero]
        data = new_save(self.slot)
        data["hero"] = hero
        level = max(1, int(self.start_level or 1))
        stats = hero_stats(hero, level)
        # 新建角色时背包与技能状态都为空、技能学习标记全为 0，
        # 因此不要发放初始装备。
        data["inventory"] = []
        data["player"] = {
            "hero": hero, "equipment": {},
            "skills": default_state(hero),
            "level": level, "exp": 0,
            "hp": stats["max_hp"], "max_hp": stats["max_hp"],
            "mp": stats["max_mp"], "max_mp": stats["max_mp"],
        }
        write_save(data, slot=self.slot)
        self.save_data = data
        self.has_save = True

    def open_hero(self, hero: str) -> None:
        slot = HERO_SLOT[hero]
        data = load_save(slot=slot)
        if data is None:
            self.new_game_for_hero(hero, slot)
            self.to_opening()
            return
        self.slot = slot
        self.save_data = data
        self.has_save = True
        if not data.get("opening_seen", False):
            self.to_opening()
        else:
            self.to_map()

    # ------------------------------------------------------------------ 输入
    def _msg(self, text: str, ttl: float = 1.6) -> None:
        self.title_msg = text
        self.title_msg_t = ttl

    def handle_title_event(self, e: pygame.event.Event) -> bool:
        if e.type != pygame.KEYDOWN:
            return False
        if e.key in (pygame.K_UP, pygame.K_w):
            self.menu_sel = (self.menu_sel - 1) % len(ui.MENU_ROWS)
        elif e.key in (pygame.K_DOWN, pygame.K_s):
            self.menu_sel = (self.menu_sel + 1) % len(ui.MENU_ROWS)
        elif e.key in (pygame.K_RETURN, pygame.K_j, pygame.K_SPACE):
            choice = self.menu_sel
            if choice == 0:
                self.to_heroes()
            elif choice == 1:
                self._msg("双人模式暂未开放")
            elif choice == 2:
                self._msg("成就房间暂未开放")
            elif choice == 3:
                self._msg("游戏商城暂未开放")
            elif choice == 4:
                self.to_help("title")
            elif choice == 5:
                self._msg("游戏推荐暂未开放")
            elif choice == 6:
                self.to_about()
            else:
                self._quit()
        elif e.key == pygame.K_c:
            self.to_slots()
        elif e.key == pygame.K_F1:
            self.to_settings()
        elif e.key == pygame.K_ESCAPE:
            return False
        return True

    def handle_slots_event(self, e: pygame.event.Event) -> bool:
        if e.type != pygame.KEYDOWN:
            return False
        if e.key in (pygame.K_UP, pygame.K_w):
            self.slot_sel = (self.slot_sel - 1) % 3
        elif e.key in (pygame.K_DOWN, pygame.K_s):
            self.slot_sel = (self.slot_sel + 1) % 3
        elif e.key in (pygame.K_1, pygame.K_2, pygame.K_3):
            self.slot_sel = e.key - pygame.K_1
        elif e.key in (pygame.K_RETURN, pygame.K_j):
            slot = self.slot_sel + 1
            if slot == 3:
                self._msg("双人战役暂未开放，可在地图进入竞技场", 2.0)
                return True
            data = load_save(slot=slot)
            if data is None:
                hero = "tangseng" if slot == 1 else "wukong"
                self.new_game_for_hero(hero, slot)
                self.to_opening()
                return True
            self.slot = slot
            self.save_data = data
            self.has_save = True
            self.to_map()
        elif e.key in (pygame.K_DELETE, pygame.K_BACKSPACE):
            delete_save(self.slot_sel + 1)
            if self.slot == self.slot_sel + 1:
                self.save_data = None
                self.has_save = False
        elif e.key == pygame.K_ESCAPE:
            self.to_title()
        return True

    def handle_hero_event(self, e: pygame.event.Event) -> bool:
        if e.type != pygame.KEYDOWN:
            return False
        if e.key in (pygame.K_LEFT, pygame.K_a, pygame.K_RIGHT, pygame.K_d):
            self.hero_sel = 1 - self.hero_sel
        elif e.key in (pygame.K_RETURN, pygame.K_j):
            self.open_hero("wukong" if self.hero_sel == 0 else "tangseng")
        elif e.key == pygame.K_ESCAPE:
            self.to_title()
        return True

    def handle_inventory_event(self, e: pygame.event.Event) -> bool:
        return self.backpack.handle_event(e)

    def handle_opening_event(self, e: pygame.event.Event) -> bool:
        if e.type == pygame.MOUSEBUTTONDOWN and e.button == 1:
            # 开场动画本身带一个真实的跳过按钮；本实现同样接受点击该按钮，
            # 回车/退出键也仍然可以跳过。
            if self.opening is not None and ui.OPENING_SKIP_RECT.collidepoint(e.pos):
                self.opening.skip()
                self.opening = None
                self.to_map()
                return True
            return False
        if e.type != pygame.KEYDOWN:
            return False
        # 跳过开场：停止开场音乐，发出“开场结束”事件并移除自身。
        # 此过程不写存档；收到开场结束事件后进入地图。
        if self.opening is not None:
            self.opening.skip()
        self.opening = None
        self.to_map()
        return True

    def handle_help_event(self, e: pygame.event.Event) -> bool:
        if e.type == pygame.KEYDOWN and e.key in (pygame.K_ESCAPE, pygame.K_RETURN):
            self.close_help()
        return True

    def close_help(self) -> None:
        """帮助界面的返回按钮 / Esc：关闭覆盖层并回到打开它的界面。"""
        self.help_back_hover = False
        self._set_hand_cursor(False)
        if self.help_back == "pause":
            self.state = "battle"
            self.paused = True
            # 帮助界面叠在暂停菜单之上；若调用方（旧工具/测试）直接设置了
            # paused 标志，这里也把暂停面板重新打开，保持一致。
            if not self.setmenu.active:
                self.setmenu.open()
        elif self.help_back == "map":
            self.to_map()
        else:
            self.to_title()

    def handle_about_event(self, e: pygame.event.Event) -> bool:
        if e.type == pygame.KEYDOWN and e.key in (pygame.K_ESCAPE, pygame.K_RETURN):
            self.close_about()
        return True

    def close_about(self) -> None:
        self.about_back_hover = False
        self._set_hand_cursor(False)
        self.to_title()

    def handle_settings_event(self, e: pygame.event.Event) -> bool:
        if e.type != pygame.KEYDOWN:
            return False
        if e.key == pygame.K_ESCAPE:
            self.to_title()
            return True
        if e.key in (pygame.K_UP, pygame.K_w):
            self.settings_sel = (self.settings_sel - 1) % 4
        elif e.key in (pygame.K_DOWN, pygame.K_s):
            self.settings_sel = (self.settings_sel + 1) % 4
        elif e.key in (pygame.K_LEFT, pygame.K_RIGHT, pygame.K_a, pygame.K_d):
            delta = -0.05 if e.key in (pygame.K_LEFT, pygame.K_a) else 0.05
            if self.settings_sel == 1:
                self.settings["music"] = max(0.0, min(1.0, float(self.settings["music"]) + delta))
                self.assets.music_volume = self.settings["music"]
                self.assets.play_music(self.assets.music_volume, self.assets.current_music)
            elif self.settings_sel == 2:
                self.settings["sfx"] = max(0.0, min(1.0, float(self.settings["sfx"]) + delta))
                self.assets.sfx_volume = self.settings["sfx"]
            elif self.settings_sel == 0:
                self.settings["muted"] = not self.settings["muted"]
                self.assets.set_muted(self.settings["muted"])
            write_settings(self.settings)
        elif e.key in (pygame.K_RETURN, pygame.K_j):
            if self.settings_sel == 0:
                self.settings["muted"] = not self.settings["muted"]
                self.assets.set_muted(self.settings["muted"])
                write_settings(self.settings)
            elif self.settings_sel == 3:
                self.to_title()
        return True

    def handle_skill_event(self, e: pygame.event.Event) -> bool:
        """技能页的键盘处理部分；鼠标事件由 handle_mouse 分发。"""
        if self.skill_page is None:
            return False
        return self.skill_page.handle_key(e)

    def handle_map_event(self, e: pygame.event.Event) -> bool:
        """兼容入口；真正的逻辑在 MapScene 中实现。"""
        return self._ensure_map_scene().handle_event(e)

    # ---------------------------------------------------------- 暂停/暂停菜单
    def open_pause(self) -> None:
        """战斗界面的设置按钮打开暂停菜单；它是覆盖层，不是场景切换。"""
        if self.state != "battle" or self.setmenu.active:
            return
        self.setmenu.open()
        self.paused = True
        self.pause_sel = 0
        self._set_hand_cursor(False)

    def close_pause(self) -> None:
        self.setmenu.close()
        self.paused = False
        self._set_hand_cursor(False)

    def handle_setmenu_action(self, action: str) -> None:
        if action == "close":
            self.close_pause()
        elif action == "sound":
            # 暂停菜单里的声音开关只切换全局静音状态 0/1，不写入 settings.json。
            self.assets.set_muted(not self.assets.muted)
        elif action == "map":
            self.close_pause()
            self.to_map()
        elif action == "menu":
            self.close_pause()
            self.to_title()
        elif action == "help":
            self.to_help("pause")

    def handle_pause_event(self, e: pygame.event.Event) -> bool:
        """兼容入口；真正的菜单逻辑由 game.setmenu 承载。"""
        if not self.setmenu.active:
            if e.type == pygame.KEYDOWN and e.key == pygame.K_ESCAPE:
                self.paused = False
                return True
            return False
        action = self.setmenu.handle_key(e)
        if action is not None:
            self.handle_setmenu_action(action)
        return e.type == pygame.KEYDOWN

    def handle_result_event(self, e: pygame.event.Event) -> bool:
        if e.type != pygame.KEYDOWN:
            return False
        if self.result_won:
            if e.key in (pygame.K_RETURN, pygame.K_ESCAPE):
                self.to_map()
            return True
        if e.key in (pygame.K_LEFT, pygame.K_RIGHT, pygame.K_UP,
                     pygame.K_DOWN, pygame.K_a, pygame.K_d,
                     pygame.K_w, pygame.K_s):
            self.result_keyboard_focus = True
        if e.key in (pygame.K_LEFT, pygame.K_RIGHT, pygame.K_a, pygame.K_d):
            self.result_sel = 1 - self.result_sel
        elif e.key == pygame.K_r:
            self.start_battle(update_player_from_save(self.save_data),
                              level_id=self.level_id)
        elif e.key == pygame.K_RETURN:
            if self.result_sel == 0:
                # T15 A-11：“重新挑战”固定重开当前关，不复用上一次的 level_id
                self.start_battle(update_player_from_save(self.save_data),
                                  level_id=self.level_id)
            else:
                self.to_map()
        elif e.key == pygame.K_ESCAPE:
            self.to_map()
        return True

    def handle_ending_event(self, e: pygame.event.Event) -> bool:
        """结束动画播完后进入选关地图。

        结束画面没有跳过按钮：播放约 50 秒（1205 帧）后会自动
        发出“关卡完成”事件。本实现出于玩家操作便利保留任意键跳过，
        但跳过后仍然回到地图，不会回到标题界面。
        """
        if e.type == pygame.KEYDOWN:
            if self.ending is not None:
                self.ending.skip()
            self.ending_done = True
            self.ending = None
            self.to_map()
        return True

    # ------------------------------------------------------------------ 更新
    def update(self, dt: float, keys, events) -> None:
        if self.state != "battle":
            self.title_msg_t = max(0.0, self.title_msg_t - dt)
            if self.title_msg_t <= 0:
                self.title_msg = ""
        if self.state == "title":
            self.title_t += dt
            for e in events:
                if e.type == pygame.QUIT:
                    self._quit()
                self.cover.handle_key(e)
                if self.state != "title":
                    break
            return

        if self.state == "slots":
            for e in events:
                if e.type == pygame.QUIT:
                    self._quit()
                self.handle_slots_event(e)
            return

        if self.state == "heroes":
            if self.hero_select is None:
                self.to_title()
                return
            for e in events:
                if e.type == pygame.QUIT:
                    self._quit()
                if self.hero_select is None:
                    break
                self.hero_select.handle_key(e)
            return

        if self.state == "help":
            for e in events:
                if e.type == pygame.QUIT:
                    self._quit()
                self.handle_help_event(e)
            return

        if self.state == "about":
            for e in events:
                if e.type == pygame.QUIT:
                    self._quit()
                self.handle_about_event(e)
            return

        if self.state == "settings":
            for e in events:
                if e.type == pygame.QUIT:
                    self._quit()
                self.handle_settings_event(e)
            return

        if self.state == "opening":
            if self.opening is not None:
                self.opening.update(dt)
                if self.opening.finished:
                    # 开场播放结束会发出“开场结束”事件，且从不写存档；
                    # 本次会话的已看标记在 to_opening() 里就已设置。
                    self.opening = None
                    self.to_map()
                    return
            for e in events:
                if e.type == pygame.QUIT:
                    self._quit()
                self.handle_opening_event(e)
                if self.state != "opening":
                    break
            return

        if self.state == "skills":
            if self.skill_page is not None:
                self.skill_page.update(dt)
            for e in events:
                if e.type == pygame.QUIT:
                    self._quit()
                self.handle_skill_event(e)
                if self.state != "skills":
                    break
            return

        if self.state == "inventory":
            self.backpack.update(dt)
            for e in events:
                if e.type == pygame.QUIT:
                    self._quit()
                self.handle_inventory_event(e)
            return

        if self.state == "map":
            scene = self._ensure_map_scene()
            scene.update(dt)
            for e in events:
                if e.type == pygame.QUIT:
                    self._quit()
                if self._pointer_locked and e.type in (pygame.MOUSEBUTTONDOWN,
                                                       pygame.MOUSEBUTTONUP):
                    # 失败面板/结算面板的那一下点击不穿透到选关节点（T15 A-11）
                    continue
                scene.handle_event(e)
                if self.state != "map":
                    break
            self._pointer_locked = False
            return

        if self.state == "arena":
            for e in events:
                if e.type == pygame.QUIT:
                    self._quit()
                if e.type == pygame.KEYDOWN and e.key == pygame.K_ESCAPE:
                    self.to_map()
                elif self.arena and self.arena.finished and e.type == pygame.KEYDOWN and e.key in (pygame.K_RETURN, pygame.K_j):
                    self.to_map()
            if self.arena:
                self.arena.update(dt, keys, [e for e in events if e.type != pygame.QUIT])
            return

        if self.state == "battle":
            for e in events:
                if e.type == pygame.QUIT:
                    self._quit()
                if self.setmenu.active:
                    action = self.setmenu.handle_key(e) if e.type == pygame.KEYDOWN else None
                    if action is not None:
                        self.handle_setmenu_action(action)
                        if self.state != "battle":
                            break
                    continue
                if self.paused:
                    if e.type == pygame.KEYDOWN and e.key == pygame.K_ESCAPE:
                        self.paused = False
                    continue
                if e.type == pygame.KEYDOWN and e.key == pygame.K_ESCAPE:
                    self.open_pause()          # 本实现新增的便捷入口：按 Esc 打开暂停菜单
                elif e.type == pygame.KEYDOWN and e.key == pygame.K_m:
                    self.assets.toggle_mute()
                elif e.type == pygame.KEYDOWN and e.key == pygame.K_c:
                    self.to_inventory("battle")
                elif e.type == pygame.KEYDOWN and e.key == pygame.K_v:
                    self.to_skill_shop("battle")
            if not self.paused and self.battle:
                self.battle.update(dt, keys, [e for e in events if e.type != pygame.QUIT])
                if self.battle.finished:
                    self.finish_battle()
            return

        if self.state == "ending":
            if self.ending is not None:
                self.ending.update(dt)
                if self.ending.finished:
                    # 结束画面播到最后一帧会自动走完：先销毁结束画面，
                    # 再进入关卡完成流程并切到选关地图。
                    # 这个切换不需要玩家按键。
                    self.ending_done = True
                    self.ending = None
                    self.to_map()
                    return
            for e in events:
                if e.type == pygame.QUIT:
                    self._quit()
                self.handle_ending_event(e)
                if self.state != "ending":
                    break
            return

        if self.state == "result":
            for e in events:
                if e.type == pygame.QUIT:
                    self._quit()
                self.handle_result_event(e)
            return

    def _quit(self) -> None:
        if self.battle is not None:
            # 关卡中途关闭窗口时，已获得的经验和魂魄仍要结算保留。
            self._bank_battle_currency()
        pygame.quit()
        sys.exit(0)

    def _set_hand_cursor(self, hand: bool) -> None:
        try:
            pygame.mouse.set_cursor(
                pygame.SYSTEM_CURSOR_HAND if hand else pygame.SYSTEM_CURSOR_ARROW)
        except Exception:
            pass

    # ------------------------------------------------------------------ 鼠标
    def handle_mouse(self, event: pygame.event.Event) -> None:
        """为全屏面板提供鼠标操作支持。

        ``inventory`` 交给 :class:`Backpack` 处理，它同时需要鼠标移动和
        点击事件；其他界面只关心点击。
        """
        if self.state == "skills":
            if self.skill_page is not None:
                self.skill_page.handle_mouse(event)
            return
        if self.state == "inventory":
            self.backpack.handle_mouse(event)
            return
        if self.state == "title":
            self.cover.handle_mouse(event)
            return
        if self.state == "heroes":
            if self.hero_select is not None:
                self.hero_select.handle_mouse(event)
            return
        if self.state == "help":
            if event.type == pygame.MOUSEMOTION:
                self.help_back_hover = HELP_BACK_RECT.collidepoint(event.pos)
                self._set_hand_cursor(self.help_back_hover)
            elif (event.type == pygame.MOUSEBUTTONDOWN
                  and getattr(event, "button", 1) == 1
                  and HELP_BACK_RECT.collidepoint(event.pos)):
                self.close_help()
            return
        if self.state == "about":
            if event.type == pygame.MOUSEMOTION:
                self.about_back_hover = ABOUT_BACK_RECT.collidepoint(event.pos)
                self._set_hand_cursor(self.about_back_hover)
            elif (event.type == pygame.MOUSEBUTTONDOWN
                  and getattr(event, "button", 1) == 1
                  and ABOUT_BACK_RECT.collidepoint(event.pos)):
                self.close_about()
            return
        if (self.state == "battle" and self.setmenu.active):
            action = self.setmenu.handle(event)
            if event.type == pygame.MOUSEMOTION:
                self._set_hand_cursor(self.setmenu.hit_test(event.pos) is not None)
            if action is not None:
                self.handle_setmenu_action(action)
            return
        if (self.state == "battle" and not self.paused
                and event.type == pygame.MOUSEMOTION):
            if self.battle:
                self.battle.hud.set_mouse_pos(event.pos)
            hover = self.battle.hud.hit_test(event.pos) if self.battle else None
            self._set_hand_cursor(hover is not None)
            return
        if (self.state == "result" and not self.result_won
                and event.type == pygame.MOUSEMOTION):
            # 纯鼠标操作时隐藏键盘焦点框；下一次按 WASD/方向键时会重新出现
            # （见 handle_result_event）。
            self.result_keyboard_focus = False
            return
        if event.type != pygame.MOUSEBUTTONDOWN:
            return
        if getattr(event, "button", 1) != 1:
            return
        pos = event.pos
        x, y = pos
        if self.state == "slots":
            for i in range(3):
                ry = 120 + i * 125
                if 180 <= x <= 760 and ry <= y <= ry + 100:
                    self.slot_sel = i
                    self.handle_slots_event(pygame.event.Event(
                        pygame.KEYDOWN, key=pygame.K_RETURN))
                    return
        elif self.state == "opening":
            self.handle_opening_event(pygame.event.Event(
                pygame.KEYDOWN, key=pygame.K_RETURN))
        elif self.state == "ending":
            self.handle_ending_event(pygame.event.Event(
                pygame.KEYDOWN, key=pygame.K_RETURN))
        elif self.state == "battle" and self.paused:
            # 兼容旧工具的兜底路径：它们会直接设置 ``paused``；
            # 真正的暂停输入走上面的暂停菜单。
            if pygame.Rect(578, 86, 40, 40).collidepoint(pos):
                self.close_pause()
                return
        elif self.state == "battle":
            if self.battle:
                self.battle.hud.set_mouse_pos(pos)
            action = self.battle.hud.hit_test(pos) if self.battle else None
            if action == "study":
                self.to_skill_shop("battle")
                return
            if action == "bag":
                self.to_inventory("battle")
                return
            if action == "set":
                # 战斗界面的设置按钮会打开暂停菜单并冻结战斗；
                # 它是覆盖层，不是场景切换。
                self.open_pause()
                return
        elif self.state == "result" and not self.result_won:
            # T15 A-11：命中区与绘制同源（result_button_rects），且点击被面板消费。
            self.result_keyboard_focus = False
            index = self.hit_test_result(pos)
            if index is None:
                return
            self.result_sel = index
            self.handle_result_event(pygame.event.Event(
                pygame.KEYDOWN, key=pygame.K_RETURN))
            self._pointer_locked = True
        elif self.state == "settings":
            for i in range(4):
                ry = 140 + i * 66
                if 230 <= x <= 710 and ry <= y <= ry + 46:
                    self.settings_sel = i
                    self.handle_settings_event(pygame.event.Event(
                        pygame.KEYDOWN, key=pygame.K_RETURN))
                    return

    def _stats_view(self) -> dict:
        data = self.save_data or {}
        p = self.battle.player if self.battle else None
        if p is not None:
            return {
                "hp": p.max_hp, "mp": p.max_mp, "atk": f"{p.atk_min}-{p.atk_max}",
                "def": p.defense, "crit": f"{int(p.crit * 100)}%",
                "miss": f"{int(p.miss * 100)}%", "hp_regen": p.hp_regen,
                "mp_regen": p.mp_regen, "level": p.level_no,
                "exp": p.exp, "exp_need": p.exp_need,
                "gongxun": int(data.get("gongxun", 0)),
            }
        from .items import bonuses
        from .skills import passive_bonus
        hero = data.get("hero", "wukong")
        pdata = data.get("player", {})
        level = int(pdata.get("level", 1))
        skills = normalize(hero, pdata.get("skills"))
        stats = hero_stats(hero, level)
        pb = passive_bonus(hero, skills)
        eq = bonuses(pdata.get("equipment", {}))
        return {
            "hp": int(stats["max_hp"]) + pb["hp"] + eq["hp"],
            "mp": int(stats["max_mp"]) + pb["mp"] + eq["mp"],
            "atk": f"{int(stats['atk_min']) + eq['atk']}-{int(stats['atk_max']) + eq['atk']}",
            "def": int(stats["defense"]) + eq["def"],
            "crit": f"{int((eq['crit'] + pb['crit']) * 100)}%",
            "miss": f"{int(eq['miss'] * 100)}%",
            "hp_regen": int(eq["hp_regen"]) + pb["hp_regen"],
            "mp_regen": int(eq["mp_regen"]) + pb["mp_regen"],
            "level": level, "exp": int(pdata.get("exp", 0)),
            "exp_need": stats["exp_need"], "gongxun": int(data.get("gongxun", 0)),
        }

    # ------------------------------------------------------------------ 绘制
    def draw(self) -> None:
        self.screen.fill((0, 0, 0))
        if self.state == "title":
            self.cover.draw(self.screen)
            if self.title_msg:
                ui.draw_text(self.screen, self.assets.font(16, bold=True), self.title_msg,
                             (WIDTH // 2, 520), (245, 210, 120), anchor="center")
        elif self.state == "slots":
            ui.draw_slots(self.screen, self.assets, self.slot_sel, list_saves())
            if self.title_msg:
                ui.draw_text(self.screen, self.assets.font(15, bold=True), self.title_msg,
                             (WIDTH // 2, 520), (245, 210, 120), anchor="center")
        elif self.state == "heroes":
            if self.hero_select is not None:
                self.hero_select.draw(self.screen)
        elif self.state == "opening":
            if self.opening is not None:
                self.opening.draw(self.screen)
                ui.draw_opening_skip_button(self.screen, self.assets)
            else:
                ui.draw_opening(self.screen, self.assets,
                                (self.save_data or {}).get("hero", "wukong"),
                                self.opening_page)
        elif self.state == "settings":
            ui.draw_settings(self.screen, self.assets, self.settings_sel, self.settings)
        elif self.state == "help":
            ui.draw_help(self.screen, self.assets, self.help_back_hover)
        elif self.state == "about":
            ui.draw_about(self.screen, self.assets, self.about_back_hover)
        elif self.state == "inventory":
            if self.battle is not None and self.menu_back == "battle":
                self.battle.draw(self.screen)
            self.backpack.draw(self.screen)
        elif self.state == "skills":
            if self.skill_page is not None:
                self.skill_page.draw(self.screen)
            else:
                # 旧存档或状态切换不完整时的兜底：直接内联绘制技能商店。
                data = self.save_data or {}
                hero = data.get("hero", "wukong")
                ui.draw_skill_shop(self.screen, self.assets, hero,
                                   normalize(hero, data.get("player", {}).get("skills")),
                                   int(data.get("soul", 0)), self.skill_sel,
                                   self.skill_msg, self.skill_tab)
        elif self.state == "map":
            self._ensure_map_scene().draw(self.screen)
        elif self.state == "battle":
            if self.battle:
                self.battle.draw(self.screen)
            if self.setmenu.active:
                self.setmenu.draw(self.screen)
        elif self.state == "arena":
            if self.arena:
                self.arena.draw(self.screen)
                ui.draw_arena_hud(self.screen, self.assets, self.arena)
        elif self.state == "result":
            if self.battle:
                self.battle.draw(self.screen)
            ui.draw_result(self.screen, self.assets, self.result_won,
                           self.battle.player if self.battle else None,
                           self.title_t, self.result_sel,
                           self.result_keyboard_focus)
        elif self.state == "ending":
            if self.ending is not None:
                self.ending.draw(self.screen)
            else:
                # 仅在结局素材缺失时使用的兜底：退化为纯代码绘制结局画面。
                ui.draw_ending(self.screen, self.assets,
                               (self.save_data or {}).get("hero", "wukong"))

    # ------------------------------------------------------------------ 主循环
    def run(self) -> None:
        accumulator = 0.0
        step = 1.0 / FPS
        while True:
            frame_dt = min(self.clock.tick(FPS) / 1000.0, 0.10)
            accumulator += frame_dt
            pending_events = pygame.event.get()
            for event in pending_events:
                if event.type == pygame.KEYDOWN and event.key == pygame.K_F11:
                    try:
                        pygame.display.toggle_fullscreen()
                    except pygame.error:
                        pass
                elif event.type in (pygame.MOUSEBUTTONDOWN, pygame.MOUSEBUTTONUP,
                                    pygame.MOUSEMOTION):
                    self.handle_mouse(event)
            keys = pygame.key.get_pressed()
            steps = 0
            while accumulator >= step and steps < 5:
                self.update(step, keys, pending_events)
                pending_events = []
                accumulator -= step
                steps += 1
            if steps == 5:
                accumulator = 0.0
            self.draw()
            pygame.display.flip()

    def run_headless(self, seconds: float, screenshot: str | None = None,
                     battle: bool = False, demo: bool = False) -> None:
        if battle:
            self.start_battle(update_player_from_save(self.save_data) if self.has_save else None)
        frames = int(seconds * FPS)
        self.demo_t = 0.0
        self.demo_next_attack = 0.4
        self.demo_next_jump = 2.2
        for i in range(frames):
            t = i / FPS
            events = []
            state = {k: False for k in range(512)}
            if self.state == "battle" and demo:
                state[pygame.K_d] = t > 0.2
                if t >= self.demo_next_attack:
                    events.append(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_j))
                    self.demo_next_attack += 1.05
                if t >= self.demo_next_jump:
                    events.append(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_k))
                    self.demo_next_jump += 3.2
                if 4.0 <= t <= 4.08:
                    state[pygame.K_w] = True
                    if abs(t - 4.0) < 1 / FPS:
                        events.append(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_j))
            self.update(1 / FPS, state, events)
            self.draw()
            pygame.display.flip()
        if screenshot:
            os.makedirs(os.path.dirname(os.path.abspath(screenshot)), exist_ok=True)
            pygame.image.save(self.screen, screenshot)
