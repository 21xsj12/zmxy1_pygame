"""数据驱动的旁白播放器，负责开场与结局两段剧情演出。

这两段演出都不是逐帧位图，而是由容器精灵分层组合而成：

* 开场：单帧容器，内含一张 940x590 的插画（49 帧内把透明度从 0 淡入
  到 256），以及一个 286 帧的子容器；该子容器里只放一张预渲染的剧情图
  （297x677），以每帧 3.4 像素的速度向上滚动。
* 结局：单帧容器，内含 940x590 的胜利画面（不做淡入），以及一个 1205
  帧的子容器；该子容器放三张预渲染文字图（304x24、325x559、304x1253），
  分别在第 1 / 85 / 317 帧进入，并以每帧 3.05 / 2.05 / 2.25 像素的
  速度向上滚动。

本实现保留上述六张关键图，用整体透明度、裁剪过的文字窗口和逐帧贴图
重建整条时间线；由帧号换算出的参数统一存放在 data/narration.json 中，
调整数值即可改变节奏，不必改动绘制代码。

绘制顺序固定为：清屏、贴背景、按窗口裁剪、贴已经出现的文字图。
"""
from __future__ import annotations

import json
from pathlib import Path

import pygame

from .constants import DATA_DIR

NARRATION_PATH = DATA_DIR / "narration.json"


def load_specs(path: str | Path | None = None) -> dict:
    """读取 ``data/narration.json``；读不到或内容非法时返回 ``{}``。"""
    source = Path(path) if path is not None else NARRATION_PATH
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


# 模块导入时只读取一次；GameApp 复用同一批字典对象，
# 这样测试才能断言 app.opening.spec is NARRATION["opening"]。
NARRATION = load_specs()


class Narration:
    """用一张主图加若干张文字图播放一条旁白时间线。"""

    def __init__(self, spec: dict, assets_dir: str | Path):
        self.spec = spec
        self.assets_dir = Path(assets_dir)
        self.t = 0.0
        self.duration = float(spec["duration"])
        self.fade_in = float(spec.get("fade_in", 0.0) or 0.0)
        self.bg = self._load(str(spec["bg"]))
        self._texts: list[tuple[pygame.Surface, dict]] = [
            (self._load(str(item["image"])), item)
            for item in spec.get("texts", [])
        ]
        self.window = pygame.Rect(*(int(v) for v in spec["window"]))
        self.finished = False

    def _load(self, rel: str) -> pygame.Surface:
        return pygame.image.load(str(self.assets_dir / rel)).convert_alpha()

    @property
    def bg_alpha(self) -> int:
        """当前背景的整体透明度（0..255）；开场会淡入，结局不淡入。"""
        if self.fade_in <= 0:
            return 255
        return int(round(255 * max(0.0, min(1.0, self.t / self.fade_in))))

    def update(self, dt: float) -> None:
        if self.finished:
            return
        self.t += max(0.0, float(dt))
        if self.t >= self.duration:
            self.t = self.duration
            self.finished = True

    def skip(self) -> None:
        """快进到时间线末尾，供“跳过演出”入口使用。"""
        self.t = self.duration
        self.finished = True

    def text_layout(self, t: float | None = None) -> list[dict]:
        """当前已出现的文字图，附带各自未经裁剪的屏幕矩形。"""
        now = self.t if t is None else float(t)
        layout: list[dict] = []
        for image, item in self._texts:
            t0 = float(item.get("t0", 0.0) or 0.0)
            if now < t0:
                continue
            x = float(item["x"])
            y = float(item["y0"]) - float(item["vy"]) * (now - t0)
            layout.append({
                "image": image,
                "spec": item,
                "x": x,
                "y": y,
                "rect": pygame.Rect(round(x), round(y),
                                    image.get_width(), image.get_height()),
            })
        return layout

    def draw(self, surface: pygame.Surface) -> None:
        surface.fill((0, 0, 0))
        alpha = self.bg_alpha
        # 这里必须始终传入数值型整体透明度：pygame-ce 在 set_alpha(None) 时
        # 会让随后的 blit 忽略位图自带的逐像素 alpha，结果把开场背景那圈
        # 柔和的雾状边缘裁成硬邦邦的矩形。传 255 既保留逐像素 alpha，
        # 又能清掉上一次淡入留下的整体透明度。
        self.bg.set_alpha(alpha)
        surface.blit(self.bg, (0, 0))
        surface.set_clip(self.window)
        for entry in self.text_layout():
            rect = entry["rect"]
            if rect.bottom < self.window.top or rect.top > self.window.bottom:
                continue
            surface.blit(entry["image"], (rect.x, rect.y))
        surface.set_clip(None)
