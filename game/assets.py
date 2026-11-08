"""资源加载：精灵动画片段、图片、字体与音频。"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import pygame

from .constants import ASSETS_DIR, ANIM_FPS
from .paperdoll import PaperDoll

_GLYPH_CACHE: dict[tuple[int, str], bool] = {}
_NOTDEF_CACHE: dict[int, tuple[tuple[int, int], bytes]] = {}

# 所有背景音乐共用同一条循环通道：同一时刻只播一首，切歌前先停掉当前曲目。
# 本实现把同一批 mp3 同时以 ``mixer.music`` 和 ``Sound`` 对象两种方式加载，
# 因此停止背景音乐时也要停掉以 Sound 对象播放的曲目，否则旧曲目会残留。
# 可用的曲目名为 begin、over 与 bg1..bg4。
BGM_SOUND_NAMES = ("begin", "over", "bg1", "bg2", "bg3", "bg4")


def glyph_supported(font: pygame.font.Font, ch: str) -> bool:
    """判断 ``font`` 是否真正含有 ``ch`` 对应的字形。

    pygame 没有 cmap 接口，因此每个字符只需渲染一次，把渲染结果与字体的
    缺字方框（用私用区码位渲染）比较即可；比较结果会缓存起来复用。
    """
    key = (id(font), ch)
    if key in _GLYPH_CACHE:
        return _GLYPH_CACHE[key]
    fid = id(font)
    if fid not in _NOTDEF_CACHE:
        ref = font.render("\ue000", True, (255, 255, 255))
        _NOTDEF_CACHE[fid] = (ref.get_size(), pygame.image.tostring(ref, "RGBA"))
    ref_size, ref_data = _NOTDEF_CACHE[fid]
    surf = font.render(ch, True, (255, 255, 255))
    ok = surf.get_size() != ref_size or pygame.image.tostring(surf, "RGBA") != ref_data
    _GLYPH_CACHE[key] = ok
    return ok


@dataclass
class Clip:
    name: str
    fps: float
    loop: bool
    width: int
    height: int
    anchor_x: float
    anchor_y: float
    frames: list[pygame.Surface]
    # 根节点到碰撞椭圆底部的距离。怪物会设置该值，渲染器据此把根节点
    # 放在 ``foot_y - colipse_bottom`` 的位置，让脚底贴地对齐。
    colipse_bottom: float = 0.0

    @property
    def duration(self) -> float:
        return len(self.frames) / max(self.fps, 1.0)


@dataclass(frozen=True)
class AssetBundle:
    """资源按七个分组依次加载，这是其中一个分组及其文件清单。"""

    key: str
    file_index: int                 # 2..8，与加载界面上显示的文件序号一致
    clips: tuple[str, ...] = ()
    images: tuple[str, ...] = ()
    audio: bool = False
    fx_defs: bool = False


class Animation:
    """单个动画片段的时间轴播放器，沿用每秒 24 帧的节奏。"""

    def __init__(self, clip: Clip, loop: bool | None = None):
        self.clip = clip
        self.loop = clip.loop if loop is None else loop
        self.time = 0.0
        self.finished = False

    def play(self, clip: Clip | None = None, loop: bool | None = None, restart: bool = False) -> None:
        if clip is not None and clip is not self.clip:
            self.clip = clip
            self.time = 0.0
            self.finished = False
            if loop is not None:
                self.loop = loop
        elif restart:
            self.time = 0.0
            self.finished = False
        if loop is not None:
            self.loop = loop

    def update(self, dt: float) -> None:
        if self.finished:
            return
        self.time += dt
        if self.time >= self.clip.duration:
            if self.loop:
                self.time %= self.clip.duration
            else:
                self.time = max(0.0, self.clip.duration - 1e-6)
                self.finished = True

    @property
    def frame_index(self) -> int:
        idx = int(self.time * self.clip.fps)
        return min(max(idx, 0), len(self.clip.frames) - 1)

    @property
    def frame(self) -> pygame.Surface:
        return self.clip.frames[self.frame_index]

    def draw(self, surface: pygame.Surface, x: float, y: float, facing: int = 1,
             alpha: int = 255, flash: tuple[int, int, int] | None = None) -> pygame.Rect:
        """以片段中脚部/身体的锚点把当前帧画到 (x, y)，返回屏幕矩形。"""
        img = self.frame
        ax = self.clip.anchor_x
        # 素材里的角色图形本身朝左。角色朝右时先水平翻转再绘制，
        # 否则左右移动的方向看起来会相反。
        if facing > 0:
            img = pygame.transform.flip(img, True, False)
            ax = self.clip.width - ax
        if alpha < 255:
            img = img.copy()
            img.set_alpha(alpha)
        if flash is not None:
            img = img.copy()
            overlay = pygame.Surface(img.get_size(), pygame.SRCALPHA)
            # 不改动 alpha 通道，让透明像素保持透明。
            overlay.fill((*flash, 0))
            img.blit(overlay, (0, 0), special_flags=pygame.BLEND_RGBA_ADD)
        rect = img.get_rect()
        rect.topleft = (round(x - ax), round(y - self.clip.anchor_y))
        surface.blit(img, rect)
        return rect


class Assets:
    def __init__(self, root: Path = ASSETS_DIR, audio: bool = True, defer: bool = False):
        self.root = root
        self.manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        self.clips: dict[str, Clip] = {}
        self.fx_clips: dict[str, Clip] = {}
        self.fx_defs: dict[str, dict] = {}
        self.images: dict[str, pygame.Surface] = {}
        self.fonts: dict[tuple[int, bool, bool], pygame.font.Font] = {}
        self.sounds: dict[str, pygame.mixer.Sound] = {}
        self.audio_ok = False
        self.muted = False
        self.music_volume = 0.35
        self.sfx_volume = 0.6
        self.current_music = "bg1"
        self.load_stats: list[dict] = []
        self.paperdoll = PaperDoll(root)
        if not defer:
            self.load_all(audio)

    def load_all(self, audio: bool = True) -> None:
        """一次性加载全部资源（兼容旧调用方式、测试与 --no-loading 参数）。"""
        self._load_clips()
        self._load_images()
        self._load_fx_defs()
        if audio:
            self._init_audio()

    def load_step(self, bundle: AssetBundle, on_progress=None) -> int:
        """加载一个资源分组，并报告按字节统计的进度。

        返回该分组实际的字节总数，调用方可据此记录真实的加载速度。
        进度按 ``已加载字节/总字节`` 计算；这里没有网络下载过程，因此把
        一个分组内所有文件的字节数累加，作为与之一致的进度口径。
        """
        total = 0
        clip_data: dict[str, dict] = {}
        for name in bundle.clips:
            data = self.manifest["clips"][name]
            clip_data[name] = data
            total += sum(os.path.getsize(self.root / rel) for rel in data["frames"])
        for rel in bundle.images:
            total += os.path.getsize(self.root / rel)
        fx_path = self.root / "fx" / "manifest.json"
        if bundle.fx_defs and fx_path.exists():
            total += fx_path.stat().st_size
        audio_files = self._audio_files() if bundle.audio else []
        total += sum(p.stat().st_size for _, p in audio_files)

        done = 0

        def report(extra: int = 0) -> None:
            nonlocal done
            done += extra
            if on_progress is not None:
                on_progress(done, total)

        for name in bundle.clips:
            data = clip_data[name]
            if name in self.clips:
                # 幂等处理：重复调用时也照常计入字节数，让调用方的总进度走满。
                for rel in data["frames"]:
                    report(os.path.getsize(self.root / rel))
                continue
            self.clips[name] = self._clip_from(name, data, on_frame=report)
        for rel in bundle.images:
            if rel not in self.images:
                self.images[rel] = pygame.image.load(str(self.root / rel)).convert_alpha()
            report(os.path.getsize(self.root / rel))
        if bundle.fx_defs:
            self._load_fx_defs()
            if fx_path.exists():
                report(fx_path.stat().st_size)
        if bundle.audio:
            self._init_audio(on_file=report)
        if on_progress is not None:
            # 即使某个可选音频文件或混音器初始化失败，也强制把进度补到 100%。
            on_progress(max(total, 1), max(total, 1))
        return total

    # ------------------------------------------------------------- 动画片段
    def _clip_from(self, name: str, data: dict, on_frame=None) -> Clip:
        frames = []
        for rel in data["frames"]:
            img = pygame.image.load(str(self.root / rel)).convert_alpha()
            frames.append(img)
            if on_frame is not None:
                on_frame(os.path.getsize(self.root / rel))
        return Clip(
            name=name,
            fps=float(data.get("fps", ANIM_FPS)),
            loop=bool(data.get("loop", False)),
            width=int(data["width"]),
            height=int(data["height"]),
            anchor_x=float(data["anchor_x"]),
            anchor_y=float(data["anchor_y"]),
            frames=frames,
            colipse_bottom=float(data.get("colipse_bottom", 0.0)),
        )

    def _load_clips(self) -> None:
        for name, data in self.manifest["clips"].items():
            self.clips[name] = self._clip_from(name, data)

    def _load_fx_defs(self) -> None:
        path = self.root / "fx" / "manifest.json"
        if not path.exists():
            return
        try:
            self.fx_defs = json.loads(path.read_text(encoding="utf-8"))["clips"]
        except (OSError, ValueError, KeyError):
            self.fx_defs = {}

    def fx(self, name: str) -> Clip | None:
        """按需从资源清单里加载一个特效动画片段。"""
        key = name if name.startswith("fx/") else f"fx/{name}"
        if key in self.fx_clips:
            return self.fx_clips[key]
        data = self.fx_defs.get(key)
        if data is None:
            return None
        clip = self._clip_from(key, data)
        self.fx_clips[key] = clip
        return clip

    # ------------------------------------------------------------- 图片
    def _load_images(self) -> None:
        for folder in ("ui", "bg", "fg", "menu"):
            base = self.root / folder
            if not base.exists():
                continue
            for p in base.rglob("*.png"):
                rel = p.relative_to(self.root).as_posix()
                # 加载进度界面会自行管理这批贴图，启动时不要重复加载。
                if rel.startswith("ui/loading/"):
                    continue
                # 角色信息面板用到的 445 张小切片由 HUD 按需加载；
                # 预加载只会拖慢每次启动，没有任何收益。
                if rel.startswith("ui/hud/"):
                    continue
                # 关卡选择界面的当前进度动画每个为 19 或 48 帧，
                # 地图场景只按需加载正在使用的那一个。
                if p.parent.name.endswith("_cur"):
                    continue
                self.images[rel] = pygame.image.load(str(p)).convert_alpha()

    def font_for(self, text: str, size: int, bold: bool = False) -> pygame.font.Font:
        """当内置字体覆盖 ``text`` 中的全部字符时返回它，否则退回中文字体。

        内置的方正粗圆子集共收录 371 个字形（见文档 4.10）；pygame 对缺字会
        返回默认的 ``(0, 9, 0, 14, 10)`` 度量元组。
        """
        original = self.font(size, bold)
        for ch in str(text):
            if ch.isspace():
                continue
            if not glyph_supported(original, ch):
                return self.font(size, bold, system=True)
        return original

    def image(self, rel: str) -> pygame.Surface:
        key = rel.replace("\\", "/")
        try:
            return self.images[key]
        except KeyError:
            surf = pygame.image.load(str(self.root / key)).convert_alpha()
            self.images[key] = surf
            return surf

    # ------------------------------------------------------------- 字体
    def font(self, size: int, bold: bool = False, system: bool = False) -> pygame.font.Font:
        """界面字体优先使用内置的方正粗圆简体子集。

        该子集共 371 个字形，正常排版优先使用（见文档 4.10）；只有落在子集
        之外的字形才退回系统中文字体（``system=True``）。
        """
        key = (size, bold, system)
        if key in self.fonts:
            return self.fonts[key]
        candidates = []
        if not system:
            candidates.append(str(self.root / "fonts" / "game.ttf"))
        if bold:
            candidates.append(r"C:\Windows\Fonts\msyhbd.ttc")
        candidates += [
            r"C:\Windows\Fonts\msyh.ttc",
            r"C:\Windows\Fonts\simhei.ttf",
        ]
        for path in candidates:
            if os.path.exists(path):
                try:
                    f = pygame.font.Font(path, size)
                    if bold:
                        f.set_bold(True)
                    self.fonts[key] = f
                    return f
                except Exception:
                    continue
        f = pygame.font.Font(None, size)
        self.fonts[key] = f
        return f

    # ------------------------------------------------------------- 音频
    def _audio_files(self) -> list[tuple[str, Path]]:
        base = self.root / "audio"
        files: list[tuple[str, Path]] = []
        for p in sorted((base / "sfx").glob("*.mp3")):
            files.append(("sfx", p))
        # 音频清单把 begin/over 归为背景音乐，但它们在游戏里也会作为
        # 单次音效播放，因此这两个文件同样要作为音效对象加载。
        for p in sorted((base / "bgm").glob("*.mp3")):
            files.append(("bgm", p))
        return files

    def _init_audio(self, on_file=None) -> None:
        try:
            if not pygame.mixer.get_init():
                pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=512)
            for kind, p in self._audio_files():
                if kind == "sfx":
                    self.sounds[p.stem] = pygame.mixer.Sound(str(p))
                else:
                    self.sounds.setdefault(p.stem, pygame.mixer.Sound(str(p)))
                if on_file is not None:
                    on_file(p.stat().st_size)
            self.audio_ok = True
        except Exception:
            self.audio_ok = False

    def play(self, name: str, volume: float = 0.6) -> None:
        if not self.audio_ok or self.muted:
            return
        snd = self.sounds.get(name)
        if snd:
            snd.set_volume(volume * self.sfx_volume)
            snd.play()

    def play_music(self, volume: float = 0.35, name: str = "bg1") -> None:
        old = self.current_music
        self.music_volume = max(0.0, min(1.0, volume))
        self.current_music = name
        if not self.audio_ok or self.muted:
            return
        try:
            if name != old:
                # 切歌时先停掉唯一的循环通道，再加载并启动新曲目，
                # 避免两首背景音乐同时叠加播放。
                # ``stop_music`` 会顺带清空 ``current_music``，这里要重新写回。
                self.stop_music()
                self.current_music = name
                path = self.root / "audio" / "bgm" / f"{name}.mp3"
                if path.exists():
                    pygame.mixer.music.load(str(path))
            pygame.mixer.music.set_volume(self.music_volume)
            if not pygame.mixer.music.get_busy():
                pygame.mixer.music.play(-1)
        except Exception:
            pass

    def stop_music(self) -> None:
        """停止唯一的背景音乐通道，并清空当前曲目记录。

        所有背景音乐都复用同一条共享循环通道，因此停止操作会静音任意一首
        正在播放的背景音乐。pygame 把它拆成 ``mixer.music`` 与由相同 mp3
        复制出的 Sound 实例两部分，二者都必须停止。重置 ``current_music``
        可让下一次 ``play_music`` 重新加载曲目，而不是重播已经过期的曲目。
        """
        if self.audio_ok:
            try:
                pygame.mixer.music.stop()
            except Exception:
                pass
            for name in BGM_SOUND_NAMES:
                snd = self.sounds.get(name)
                if snd is not None:
                    try:
                        snd.stop()
                    except Exception:
                        pass
        self.current_music = ""

    def set_muted(self, muted: bool) -> None:
        """全局静音，对应游戏里的音量控制开关。

        静音需要把背景音乐音量置 0，并立即停掉当前正在播放的音效，
        因为 pygame 用两套接口分别管理它们。取消静音时恢复背景音乐音量；
        若游戏是在静音状态下启动的，此时再补上被推迟的播放。
        """
        self.muted = bool(muted)
        if not self.audio_ok:
            return
        try:
            if self.muted:
                pygame.mixer.music.set_volume(0.0)
                pygame.mixer.stop()
            else:
                pygame.mixer.music.set_volume(self.music_volume)
                # 如果游戏启动时就处于静音状态，play_music() 会在加载
                # 背景音乐之前直接返回；这里补做加载与播放，但已经在
                # 播放的曲目不要重新开始。
                if not pygame.mixer.music.get_busy():
                    path = self.root / "audio" / "bgm" / f"{self.current_music}.mp3"
                    if path.exists():
                        pygame.mixer.music.load(str(path))
                        pygame.mixer.music.play(-1)
        except Exception:
            pass

    def toggle_mute(self) -> None:
        self.set_muted(not self.muted)
