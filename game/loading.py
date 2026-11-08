# -*- coding: utf-8 -*-
"""Pygame 版主模块加载界面的加载条实现。

背景是一张 940x590 的径向渐变图，颜色从 #006699 过渡到 #000000；
进度条用 100 帧的遮罩图集按百分比查表取帧；
文字由“游戏加载中...”固定文案、2..8 的数字和“/8”后缀三部分组成。

计数范围是 2..8 而不是 1..7；每个加载步骤各自维护自己的百分比。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import pygame

from .assets import AssetBundle, Assets

STAGE_W, STAGE_H = 940, 590
BAR_DEST = (286, 348)
BAR_CELL_W, BAR_CELL_H = 360, 34
LABEL_POS = (306, 326)
DIGIT_POS = (452, 318)
SLASH_POS = (486, 329)


def bar_frame(percent: int) -> int:
    """把百分比夹取到 1..100 后返回，作为进度条图集的帧号。"""
    return max(1, min(100, int(percent)))


@dataclass(frozen=True)
class LoadingStep:
    key: str
    file_index: int              # 取值范围 2..8，决定加载界面显示的数字（也用作统计编号）
    clips: tuple[str, ...] = ()
    images: tuple[str, ...] = ()
    audio: bool = False
    fx_defs: bool = False


@dataclass
class LoadingState:
    steps: list[LoadingStep]
    index: int = 0
    percent: int = 0

    @property
    def step(self) -> LoadingStep:
        return self.steps[self.index]

    @property
    def done(self) -> bool:
        return self.index >= len(self.steps)

    def set_progress(self, done: int, total: int) -> None:
        self.percent = 0 if total <= 0 else max(0, min(100, int(done * 100 / total)))


def build_loading_bundles(assets: Assets, audio: bool = True) -> list[LoadingStep]:
    """把七个加载步骤映射到本项目已经提取好的素材分组。

    这是工程上的划分，而不是现成的数据：这里只按七个步骤来组织加载。保留
    七步是为了沿用 2..8 的计数，并让每一步结束时进度条都从零重新涨满。
    """
    role_clips: list[str] = []
    m1_clips: list[str] = []
    m2_clips: list[str] = []
    m3_clips: list[str] = []
    other_clips: list[str] = []
    for name in assets.manifest["clips"]:
        if name.startswith(("wukong/", "tangseng/")):
            role_clips.append(name)
        elif name.startswith("monster/"):
            try:
                mid = int(name.split("/")[1])
            except (IndexError, ValueError):
                other_clips.append(name)
                continue
            if mid <= 11:
                m1_clips.append(name)
            elif mid <= 22:
                m2_clips.append(name)
            else:
                m3_clips.append(name)
        else:
            other_clips.append(name)

    images: dict[int, list[str]] = {2: [], 3: [], 4: [], 5: [], 6: [], 7: []}
    for folder in ("menu", "ui", "bg", "fg"):
        base = assets.root / folder
        if not base.exists():
            continue
        for p in sorted(base.rglob("*.png")):
            rel = p.relative_to(assets.root).as_posix()
            if rel.startswith("ui/loading/"):
                continue
            if p.parent.name.endswith("_cur"):
                continue
            if rel.startswith("ui/players/"):
                images[2].append(rel)
            elif rel.startswith(("ui/items/", "ui/backpack_level/", "ui/skills/",
                                 "ui/drops/", "ui/digits/", "ui/combo/")):
                images[7].append(rel)
            else:
                images[6].append(rel)

    return [
        LoadingStep("role", 2, tuple(role_clips), tuple(images[2])),
        LoadingStep("monster1", 3, tuple(m1_clips), tuple(images[3])),
        LoadingStep("monster2", 4, tuple(m2_clips), tuple(images[4])),
        LoadingStep("monster3", 5, tuple(m3_clips), tuple(images[5])),
        LoadingStep("othermat", 6, tuple(other_clips), tuple(images[6]), fx_defs=True),
        LoadingStep("backpack", 7, (), tuple(images[7])),
        LoadingStep("music", 8, (), (), audio=audio),
    ]


def _as_bundle(step: LoadingStep) -> AssetBundle:
    return AssetBundle(
        key=step.key,
        file_index=step.file_index,
        clips=step.clips,
        images=step.images,
        audio=step.audio,
        fx_defs=step.fx_defs,
    )


class LoadingView:
    """940x590 的加载界面视图，负责背景、进度条和文字层的绘制。"""

    def __init__(self, assets_root: Path):
        base = assets_root / "ui" / "loading"
        self.bg = pygame.image.load(str(base / "bg_940x590.png")).convert()
        self.bar = pygame.image.load(str(base / "bar_atlas_100.png")).convert_alpha()
        self.label = pygame.image.load(str(base / "text_label.png")).convert_alpha()
        self.slash = pygame.image.load(str(base / "text_slash8.png")).convert_alpha()
        self.digits = {
            n: pygame.image.load(str(base / "digits" / f"{n}.png")).convert_alpha()
            for n in range(2, 9)
        }

    @classmethod
    def try_create(cls, assets_root: Path) -> "LoadingView | None":
        try:
            return cls(assets_root)
        except (OSError, pygame.error):
            return None

    def draw(self, screen: pygame.Surface, state: LoadingState) -> None:
        screen.blit(self.bg, (0, 0))
        frame = bar_frame(state.percent)
        screen.blit(
            self.bar,
            BAR_DEST,
            pygame.Rect(0, (frame - 1) * BAR_CELL_H, BAR_CELL_W, BAR_CELL_H),
        )
        screen.blit(self.label, LABEL_POS)
        screen.blit(self.digits[state.step.file_index], DIGIT_POS)
        screen.blit(self.slash, SLASH_POS)


def load_assets_with_loading(
    screen: pygame.Surface,
    view: LoadingView,
    *,
    audio: bool = True,
    assets: Assets | None = None,
    steps: list[LoadingStep] | None = None,
    min_seconds: float = 0.0,
    print_stats: bool = False,
) -> Assets:
    """在显示加载界面的同时载入素材，进度按真实工作量推进。

    每个加载步骤对应一个素材分组，百分比表示该步骤内部的字节进度，所以
    每进入下一步进度条都会清零重来。进度完全由真实的文件读写和图片解码
    耗时驱动，没有睡眠或固定时长的假动画。绘制开销很小，并按时间戳把帧率
    限制在 24 fps，不会阻塞加载流程。
    """
    if assets is None:
        assets = Assets(defer=True)
    if steps is None:
        steps = build_loading_bundles(assets, audio=audio)
    state = LoadingState(steps)
    started = time.monotonic()
    last_draw = 0.0
    assets.load_stats = []

    def draw(force: bool = False) -> None:
        nonlocal last_draw
        now = time.monotonic()
        if not force and now - last_draw < 1.0 / 24.0:
            return
        view.draw(screen, state)
        pygame.display.flip()
        last_draw = now

    draw(force=True)
    for i, step in enumerate(steps):
        state.index = i
        state.percent = 0
        draw(force=True)

        def on_progress(done: int, total: int) -> None:
            state.set_progress(done, total)
            pygame.event.pump()
            if pygame.event.peek(pygame.QUIT):
                raise SystemExit
            draw()

        step_started = time.monotonic()
        step_bytes = assets.load_step(_as_bundle(step), on_progress)
        step_seconds = time.monotonic() - step_started
        # 最后一个文件载完后，load_step() 才会报告真实的 100；这里立刻再绘制
        # 一次，让该步骤完成的状态可见，随后下一步会把进度清零重来。
        if state.percent >= 100:
            draw(force=True)
        assets.load_stats.append({
            "key": step.key,
            "file_index": step.file_index,
            "bytes": step_bytes,
            "seconds": step_seconds,
        })

    # 请求了音频，但最后一步没有携带音频时，在这里补做初始化。
    if audio and not any(step.audio for step in steps):
        assets._init_audio()

    # 可选的调试停留时长；默认为 0，避免人为拖慢加载速度。
    if min_seconds > 0:
        deadline = started + min_seconds
        while time.monotonic() < deadline:
            pygame.event.pump()
            if pygame.event.peek(pygame.QUIT):
                raise SystemExit
            draw(force=True)

    if print_stats:
        total = sum(s["seconds"] for s in assets.load_stats)
        for s in assets.load_stats:
            print("[loading] %-9s %d/8 bytes=%9d time=%6.3fs" % (
                s["key"], s["file_index"], s["bytes"], s["seconds"]))
        print("[loading] total actual asset time: %.3fs" % total)
    return assets
