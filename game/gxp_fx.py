"""全屏无双特效：24 帧，总时长 1 秒。

每一帧都是预先烘焙好的 920x590 RGBA 图像，由 ``tools/build_gxp_assets.py``
从美术素材生成，存放在：

    assets/fx/wushuang/wukong/01.png .. 24.png
    assets/fx/wushuang/tangseng/01.png .. 24.png

这层特效必须画在 HUD 之上：它带 81% 黑色遮罩，需要盖住左下角的角色信息
面板，所以绘制顺序要排在面板之后。
"""
from __future__ import annotations

from .assets import Assets

STAGE_W = 940
STAGE_H = 590
FPS = 24.0
FRAMES = 24


class GxpEffect:
    def __init__(self, assets: Assets, hero: str, owner=None):
        self.assets = assets
        self.hero = "tangseng" if hero == "tangseng" else "wukong"
        self.owner = owner
        self.t = 0.0
        self.done = False
        self.frames = []
        base = f"fx/wushuang/{self.hero}"
        for i in range(1, FRAMES + 1):
            try:
                self.frames.append(assets.image(f"{base}/{i:02d}.png"))
            except (KeyError, FileNotFoundError):
                # 素材缺失时退化为空帧列表，让特效安静地什么都不画，不抛异常。
                self.frames = []
                break

    def update(self, dt: float) -> None:
        if self.done:
            return
        self.t += float(dt)
        # 用 > 而不是 >=：t == 1.0 时第 24 帧仍然可见，超出总时长后
        # 才置位 done，由下一步逻辑移除特效。
        if self.t > FRAMES / FPS:
            self.done = True

    def draw(self, surface) -> None:
        if not self.frames or self.done:
            return
        idx = min(FRAMES - 1, int(self.t * FPS))
        surface.blit(self.frames[idx], (0, 0))
