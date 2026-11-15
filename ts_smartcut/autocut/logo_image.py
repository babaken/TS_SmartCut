"""局のロゴ画像(.lgd)の材料 (設計書 §31.9「局のロゴの書き出し」)。

.lgd の書き出しは A9 の後だが、材料は特徴の計算の読み込みの時にしか取れない(録画は移される)
ので、集める部分だけを先に入れた(利用者の指示 2026-10-13)。

半透明のロゴの画素は「見える値 = (1 − 不透明度) × 背景 + 不透明度 × ロゴの色」。ロゴの枠の
すぐ外側(輪)がほぼ平らなフレームで、輪の平均を背景として、画素ごとに直線を当てはめると、
傾きから不透明度、切片からロゴの色が求まる(logoscan と同じ考え方)。直線の当てはめに要る
合計(フレーム数・背景の和・背景の 2 乗の和・見える値の和・背景×見える値の和)を録画ごとに
DB に残す。合計なので、録画どうしを足して局の値にできる。

値は元の解像度の Y・Cb・Cr(放送の 16〜235 / 16〜240 のまま、YUV 4:2:0)。位置はフレーム全体の
座標(偶数にそろえる)。ロゴの位置は、その録画のロゴの型(logo.py、縮小した四隅)のマスクから求める。

ロゴの範囲と合計は Rust の部品(native/src/post.rs の logo_boxes・logo_image_samples。決まりの値もそちら。
2026-10-28 までは numpy)、合計からロゴを求める solve も Rust (native/src/logo.rs の logo_solve。2026-10-30 まで
は numpy)。ここは形と保存 (値は packed.Packed、保存は npz.py)。.lgd の書き出しは lgd_export.py。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from ts_smartcut.autocut import npz
from ts_smartcut.autocut.logo import LogoTemplate
from ts_smartcut.autocut.native import native_module
from ts_smartcut.autocut.packed import Packed, as_packed

logger = logging.getLogger(__name__)

# 保存形式の版。集め方を変えたら上げる
SAMPLES_VERSION = 1
# 1本の録画で一時ファイルに残すキーフレームの最大数(残した区間から偏りなく選ぶ。1920×1080 で約 390MB)
MAX_PICTURES = 500
# 型のマスクの外接矩形の周りに足す余白(元の解像度の画素)。ロゴの縁の外の薄い部分も入れる。
# 範囲の決め方(余白・長い直線を除く・一番大きな塊)は Rust の部品(post.rs の logo_boxes)
PAD = 8


@dataclass
class CornerGeometry:
    """元の解像度での四隅の位置。モザイク(四隅を 2×2 に並べた画像)の大きさは (2*width, 2*height)。"""

    frame_width: int
    frame_height: int
    width: int  # 隅1つの幅(偶数)
    height: int

    def origin(self, corner: int) -> Tuple[int, int]:
        """隅の左上のフレームでの座標 (x, y)。CORNER_NAMES の順(左上・右上・左下・右下)。"""
        x = 0 if corner % 2 == 0 else self.frame_width - self.width
        y = 0 if corner < 2 else self.frame_height - self.height
        return x, y

    @property
    def mosaic_bytes(self) -> int:
        return 2 * self.width * 2 * self.height * 3 // 2

    def filter(self) -> str:
        """ffmpeg の -vf: 四隅を切り出して 2×2 に並べ、yuv420p にする。"""
        w, h = self.width, self.height
        crops = []
        for corner, label in enumerate("pqrs"):
            x, y = self.origin(corner)
            crops.append(f"[{'abcd'[corner]}]crop={w}:{h}:{x}:{y}[{label}]")
        return ("format=yuv420p,split=4[a][b][c][d];" + ";".join(crops)
                + ";[p][q]hstack[t];[r][s]hstack[u];[t][u]vstack")


def corner_geometry(frame_width: int, frame_height: int, corner_size: Tuple[int, int],
                    scale: int) -> Optional[CornerGeometry]:
    """特徴の四隅(縮小後の corner_size、縮小の割合 scale)と同じ範囲の元の解像度の四隅。
    縮小の割合で割り切れない大きさの映像は None(位置が型とずれる)。"""
    cw, ch = corner_size
    if frame_width % scale or frame_height % scale or cw <= 0 or ch <= 0:
        return None
    width, height = cw * scale, ch * scale
    if width % 2 or height % 2:
        return None
    return CornerGeometry(frame_width, frame_height, width, height)


def logo_boxes(template: LogoTemplate, geometry: CornerGeometry) -> List[Tuple[int, int, int, int, int]]:
    """型のマスクから、ロゴのある隅ごとの範囲 (隅, x0, y0, x1, y1)。隅の中の座標、元の解像度、
    余白込み、偶数にそろえる。番組の画面の枠などの長い直線を除き、一番大きな塊だけを囲む
    (CS-D の録画で、型が四隅の枠の線を拾い、範囲が隅全体になって材料が取れなかった。2026-10-13)。"""
    h, w = template.gx.shape[1], template.gy.shape[2]
    return [tuple(b) for b in native_module().logo_boxes(
        h, w, template.mask_x.astype("b1").data, template.mask_y.astype("b1").data, geometry.width,
        geometry.height)]


@dataclass
class CornerSamples:
    """1つの隅のロゴの範囲の合計。box はフレームの座標 (x, y, 幅, 高さ)、偶数。
    n・sum_b・sum_bb は面(Y・Cb・Cr)ごとの値、sum_v・sum_bv は画素ごと(Cb・Cr は縦横 1/2)。"""

    corner: int
    box: Tuple[int, int, int, int]
    n: int
    sum_b: List[float]  # 面ごと (3 つ)
    sum_bb: List[float]
    sum_v: List[Packed]  # float64 [Y (h, w), Cb (h/2, w/2), Cr]
    sum_bv: List[Packed]

    def __post_init__(self):
        self.sum_b, self.sum_bb = [float(v) for v in self.sum_b], [float(v) for v in self.sum_bb]
        self.sum_v = [as_packed(v, "f8") for v in self.sum_v]
        self.sum_bv = [as_packed(v, "f8") for v in self.sum_bv]


@dataclass
class LogoSamples:
    """録画1本のロゴ画像の材料。"""

    frame_size: Tuple[int, int]
    pictures: int  # 使ったキーフレームの数(背景が平らでないものも含む)
    corners: List[CornerSamples]
    version: int = SAMPLES_VERSION

    def to_blob(self) -> bytes:
        meta = {
            "version": self.version,
            "frame_size": list(self.frame_size),
            "pictures": self.pictures,
            "corners": [{"corner": c.corner, "box": list(c.box), "n": c.n} for c in self.corners],
        }
        arrays = {"meta": npz.from_text(json.dumps(meta))}
        for i, c in enumerate(self.corners):
            arrays[f"c{i}_sum_b"] = Packed.from_values(c.sum_b, "f8")
            arrays[f"c{i}_sum_bb"] = Packed.from_values(c.sum_bb, "f8")
            for p in range(3):
                arrays[f"c{i}_sum_v{p}"] = c.sum_v[p].astype("f8")
                arrays[f"c{i}_sum_bv{p}"] = c.sum_bv[p].astype("f8")
        return npz.save(arrays)

    @classmethod
    def from_blob(cls, blob: bytes) -> "LogoSamples":
        data = npz.load(blob)
        meta = json.loads(npz.text(data["meta"]))
        corners = [
            CornerSamples(
                corner=c["corner"], box=tuple(c["box"]), n=c["n"],
                sum_b=data[f"c{i}_sum_b"].astype("f8").flat(), sum_bb=data[f"c{i}_sum_bb"].astype("f8").flat(),
                sum_v=[data[f"c{i}_sum_v{p}"].astype("f8") for p in range(3)],
                sum_bv=[data[f"c{i}_sum_bv{p}"].astype("f8") for p in range(3)],
            )
            for i, c in enumerate(meta["corners"])
        ]
        return cls(tuple(meta["frame_size"]), meta["pictures"], corners, meta["version"])


def accumulate(spool: Path, count: int, geometry: CornerGeometry,
               boxes: Sequence[Tuple[int, int, int, int, int]]) -> List[CornerSamples]:
    """一時ファイル(モザイク (count, mosaic_bytes))のフレームから、隅ごとの合計を作る。範囲の外側の輪(背景)の
    Y の標準偏差が小さいフレームだけを使う。輪が狭すぎる・平らなフレームが無い隅は無い。"""
    if count == 0 or not boxes:
        return []
    raw = native_module().logo_image_samples(str(spool), int(count), geometry.frame_width, geometry.frame_height,
                                             geometry.width, geometry.height, [tuple(b) for b in boxes])
    results = []
    for corner, box, n, sum_b, sum_bb, sum_v, sum_bv in raw:
        _x, _y, w, h = box
        shapes = [(h, w), (h // 2, w // 2), (h // 2, w // 2)]
        results.append(CornerSamples(
            corner, tuple(box), n, [float(v) for v in sum_b], [float(v) for v in sum_bb],
            [Packed(v, "f8", shapes[p]) for p, v in enumerate(sum_v)],
            [Packed(v, "f8", shapes[p]) for p, v in enumerate(sum_bv)]))
    for corner, x0, y0, x1, y1 in boxes:
        if not any(r.corner == corner for r in results):
            logger.info("No logo image samples for corner %d (box %s): too few background pixels around it or "
                        "no frame with a flat background", corner, (x0, y0, x1, y1))
    return results


def build_samples(spool: Path, count: int, pictures: int, geometry: CornerGeometry,
                  template: Optional[LogoTemplate]) -> Optional[LogoSamples]:
    """録画のロゴの型のある隅で合計を作る。ロゴが無い・平らな背景のフレームが無ければ None。"""
    if template is None or template.frames == 0 or not any(template.has_logo):
        return None
    corners = accumulate(spool, count, geometry, logo_boxes(template, geometry))
    if not corners:
        return None
    return LogoSamples((geometry.frame_width, geometry.frame_height), pictures, corners)


# ---- 合計からロゴを求める(.lgd の書き出し・確かめ用) ----------------------

@dataclass
class SolvedLogo:
    """フレームの座標 box の範囲の、面ごとの不透明度(0〜1)とロゴの色、使ったフレーム数(画素ごと)。"""

    box: Tuple[int, int, int, int]
    alpha: List[Packed]  # float64 [Y (h, w), Cb, Cr]
    color: List[Packed]
    frames: List[Packed]


def solve_inputs(samples: Sequence[CornerSamples]) -> list:
    """Rust の部品 (logo_solve・logo_sample_checks) に渡す形の合計。"""
    return [(tuple(int(v) for v in s.box), int(s.n), [float(v) for v in s.sum_b], [float(v) for v in s.sum_bb],
             [f8_bytes(v) for v in s.sum_v], [f8_bytes(v) for v in s.sum_bv])
            for s in samples]


def solve(samples: Sequence[CornerSamples]) -> Optional[SolvedLogo]:
    """1つの隅の合計(複数の録画をまとめてよい)から不透明度と色を求める。範囲が録画ごとに違う時は
    合わせた範囲で、画素ごとにその画素を含む録画の合計を足す。"""
    if not samples:
        return None
    raw = native_module().logo_solve(solve_inputs(samples))
    if raw is None:
        return None
    box, alpha, color, frames = raw
    _x, _y, w, h = box
    shapes = [(h, w), (h // 2, w // 2), (h // 2, w // 2)]

    def planes(values):
        return [Packed(v, "f8", shapes[p]) for p, v in enumerate(values)]

    return SolvedLogo(tuple(box), planes(alpha), planes(color), planes(frames))


def f8_bytes(values) -> bytes:
    """float64 のバイト列 (Packed のほか、テストの numpy の float64 の配列も受け取る)。"""
    if isinstance(values, Packed):
        return values.astype("f8").data
    return memoryview(values).cast("B").tobytes()
