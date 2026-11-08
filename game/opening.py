"""开场（片头制作名单）动画播放器，整段共 1205 帧。

帧图存放在开场动画专用的素材子目录中，按需逐帧读入，因此约 15 MB 的 PNG
不会同时全部占用内存；默认按 24 帧/秒推进，播放到最后一帧即停住。
"""
from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

import pygame

from .constants import HEIGHT, WIDTH


class OpeningPlayer:
    def __init__(self, folder: Path, fps: float = 24.0, cache_size: int = 16):
        self.folder = folder
        self.files = sorted(folder.glob("frame_*.png"))
        self.fps = fps
        self.cache_size = cache_size
        self.cache: OrderedDict[int, pygame.Surface] = OrderedDict()
        self.time = 0.0
        self.finished = False
        self._scaled: dict[int, pygame.Surface] = {}

    @property
    def duration(self) -> float:
        return len(self.files) / self.fps if self.files else 0.0

    @property
    def index(self) -> int:
        if not self.files:
            return 0
        return min(len(self.files) - 1, int(self.time * self.fps))

    def _load(self, index: int) -> pygame.Surface | None:
        if index in self.cache:
            self.cache.move_to_end(index)
            return self.cache[index]
        try:
            surf = pygame.image.load(str(self.files[index])).convert_alpha()
        except (IndexError, pygame.error):
            return None
        self.cache[index] = surf
        while len(self.cache) > self.cache_size:
            self.cache.popitem(last=False)
        return surf

    def update(self, dt: float) -> None:
        if not self.files:
            self.finished = True
            return
        self.time += dt
        if self.time >= self.duration:
            self.time = self.duration
            self.finished = True
        # 预载接下来几帧，避免逐帧播放时因临时读盘而卡顿。
        for i in range(self.index, min(len(self.files), self.index + 6)):
            self._load(i)

    def draw(self, surface: pygame.Surface) -> None:
        surface.fill((0, 0, 0))
        img = self._load(self.index)
        if img is None:
            return
        scale = HEIGHT / max(1, img.get_height())
        w = max(1, int(img.get_width() * scale))
        key = self.index
        scaled = self._scaled.get(key)
        if scaled is None:
            scaled = pygame.transform.smoothscale(img, (w, HEIGHT))
            if len(self._scaled) > 8:
                self._scaled.clear()
            self._scaled[key] = scaled
        surface.blit(scaled, ((WIDTH - w) // 2, 0))
