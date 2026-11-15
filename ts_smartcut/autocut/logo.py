"""ロゴの型とフレームごとの一致度 (設計書 §31.4、自動カット計画 A4 の案A)。

局のロゴは本編の間ずっと画面の隅の同じ位置に出て、CM の間は消えることが多い。
放送のロゴは半透明のことが多く(CS-A・BS-A)、明るさそのものは背景で変わるが、
ロゴの縁の「明るさが変わる向き」は毎フレーム同じになる。そこで、

- 型: 録画の「残した区間」のフレームで、四隅の画素ごとに縁の向き(横・縦の隣の画素
  より明るいか暗いか)を数え、3割以上のフレームで同じ向きの画素をマスクにする。
  背景の縁はフレームごとに向きが違い、数分だけ映る静止画も割合が低いので残らない。
- 一致度: マスクの画素で、フレームの縁の向きが型と合う割合(0〜1)。

型はその録画自身の残した区間から作る(局ごとの型が無くても、録画ごとに一致度を
計算できる。利用者の決定 2026-10-08)。特徴の計算の1回の読み込みの間に、四隅を
1/8 に縮小して一時ファイルに書いておき、読み終わって型ができてから一致度を計算する
(50分の番組で約 540MB。計算が終わると消す)。

縁の向きを数える・型を作る・一致度の計算は Rust の部品(native/src/post.rs。2026-10-28 までは numpy)、
局の型のまとめ方と型どうしの比べ方も Rust (native/src/logo.rs。2026-10-30 までは numpy)。
ここは型の形(LogoTemplate。値は packed.Packed)と保存の形(npz.py。numpy の .npz と同じ)、局の型を DB で
作り直す流れ。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from ts_smartcut.autocut import npz
from ts_smartcut.autocut.native import native_module
from ts_smartcut.autocut.packed import Packed, as_packed

# 四隅(縮小後の映像の 1/4 の大きさ)を、さらに 1/2 にして型と一致度を計算する
LOGO_DIVISOR = 2
# マスク: 残した区間のフレームのうち、この割合以上で同じ向きの縁が出ている画素(局の型をまとめる時も使う)。
# 平均にすると、数分だけ映る静止画(エンドカード等)の強い縁が型を占めた(CS-A、2026-10-08)。
# 0.5 では、明るい場面の多い番組で半透明のロゴの縁が埋もれ、型が小さくなる・ロゴなしに
# なった(GR-A・BS-B、2026-10-09)。実際のカット 20 本で、0.3 でもロゴの無い隅・
# CS-A でマスクになる画素は 0 だった。縁とみなす明るさの差(2)とあわせて Rust の部品(post.rs)にもある
CONSISTENCY = 0.3
# ロゴがある隅とみなす、マスクの最小の画素数
MIN_MASK_PIXELS = 8


@dataclass
class LogoTemplate:
    """録画1本から作ったロゴの型(四隅ごと)。gx・gy は残した区間のフレームで縁の向きが
    + だった割合 − − だった割合(−1〜1)。マスクの外は使わない。"""

    gx: Packed  # float32 (4, h, w-1)
    gy: Packed  # float32 (4, h-1, w)
    mask_x: Packed  # bool、|gx| >= CONSISTENCY
    mask_y: Packed
    strength: List[float]  # 隅ごと (4 つ、float32 の値) マスクの部分の |g| の平均(CONSISTENCY〜1)。マスクが無ければ 0
    frames: int  # 型に使ったフレーム数

    def __post_init__(self):
        self.gx, self.gy = as_packed(self.gx, "f4"), as_packed(self.gy, "f4")
        self.mask_x, self.mask_y = as_packed(self.mask_x, "b1"), as_packed(self.mask_y, "b1")
        self.strength = [float(v) for v in self.strength]

    @property
    def has_logo(self) -> List[bool]:
        """隅ごとに、ロゴがあるとみなせるか (4 つ)。"""
        return [self.mask_x[i].count_nonzero() + self.mask_y[i].count_nonzero() >= MIN_MASK_PIXELS for i in range(4)]


def template_from_native(raw, shape: Tuple[int, int], frames: int) -> "LogoTemplate":
    """Rust の型 (gx, gy, mask_x, mask_y, strength) を LogoTemplate にする。shape は型の (高さ, 幅)。"""
    h, w = shape
    gx, gy, mask_x, mask_y, strength = raw
    return LogoTemplate(
        Packed(gx, "f4", (4, h, max(w - 1, 0))), Packed(gy, "f4", (4, max(h - 1, 0), w)),
        Packed(mask_x, "b1", (4, h, max(w - 1, 0))), Packed(mask_y, "b1", (4, max(h - 1, 0), w)),
        [float(v) for v in strength], int(frames))


def _arrays(template: "LogoTemplate"):
    """Rust に渡す型の値 (gx・gy は float32、マスクは uint8 のバイト列)。"""
    return (template.gx.astype("f4").data, template.gy.astype("f4").data,
            template.mask_x.astype("b1").data, template.mask_y.astype("b1").data)


def _shape(template: "LogoTemplate") -> Tuple[int, int]:
    """型の (高さ, 幅)。"""
    return template.gx.shape[1], template.gy.shape[2]


def template_from_sums(sum_x: bytes, sum_y: bytes, shape: Tuple[int, int], count: int) -> LogoTemplate:
    """縁の向きを数えた合計 (int64 のバイト列。特徴の計算の読み込みが返す) とフレーム数から型。"""
    return template_from_native(native_module().logo_template(sum_x, sum_y, shape[0], shape[1], int(count)), shape, count)


def _corner_bytes(corners) -> Tuple[bytes, Tuple[int, ...]]:
    """縮小した四隅 (k, 4, 高さ, 幅) の uint8 のバイト列と形。Packed のほか、テストの numpy の配列も受け取る
    (numpy は読み込まず、配列の astype を呼ぶだけ。値は uint16 → uint8 の順で丸める)。"""
    if isinstance(corners, Packed):
        return corners.astype("u1").data, corners.shape
    values = corners.astype("uint16").astype("uint8")
    return values.tobytes(), tuple(values.shape)


def template_from_corners(corners) -> LogoTemplate:
    """縮小した四隅 (k, 4, 高さ, 幅。0〜255) を 1/2 にし、全部のフレームの縁の向きから型を作る (確かめ用)。"""
    data, (k, _, big_h, big_w) = _corner_bytes(corners)
    sum_x, sum_y = native_module().logo_sums_from_corners(data, k, big_h, big_w)
    return template_from_sums(sum_x, sum_y, (big_h // LOGO_DIVISOR, big_w // LOGO_DIVISOR), k)


def scores_from_corners(corners, template: LogoTemplate) -> Packed:
    """縮小した四隅 (k, 4, 高さ, 幅) のフレームごと・隅ごとの一致度 (k, 4)、0〜255 の uint8。ロゴが無い隅は 0。
    マスクの画素ごとに、フレームの縁の向きが型と同じなら +、逆なら − として、型の割合の重みで平均する
    (相関(差の大きさで重み付け)にすると、ロゴに重なった背景の強い縁で値が大きく揺れる)。"""
    data, (k, _, big_h, big_w) = _corner_bytes(corners)
    out = native_module().logo_scores_from_corners(data, k, big_h, big_w, *_arrays(template))
    return Packed(out, "u1", (k, 4))


def score_file(path: Path, count: int, shape: Tuple[int, int], template: LogoTemplate) -> bytes:
    """一時ファイル(uint8 の縮小した四隅 (count, 4, h, w))の各フレームの一致度を、
    0〜255 の uint8 (count, 4) のバイト列で返す。"""
    h, w = shape
    return native_module().logo_scores_from_file(str(path), int(count), h, w, *_arrays(template))


# ---- 局ごとの型 ------------------------------------------------------------
# 局(original_network_id と service_id)ごとに、その局の録画ごとの型をまとめる。録画を
# 開いてカットする前(残した区間が無い時)の一致度は、局の型で計算する(A6 の提案)。
# まとめ方は、録画ごとの「同じ向きの縁が出ていた割合」をフレーム数の重みで平均する
# (その局の録画の残した区間をすべて合わせて数えたのと同じ)。

# 局の型を作るのに必要な録画の数(1本だけだと、その番組だけの静止画等が型に入る)
MIN_STATION_RECORDINGS = 2
# 局の型のマスクは、使った録画のこの割合以上で(録画ごとの型の)マスクだった画素だけ。
# 割合の平均だけだと、2本のうち1本の番組だけに出ていた表示(番組のマーク等)も 0.5 になり、しきい値を超える。
# まとめる計算は Rust の部品(native/src/logo.rs の logo_combine。この値もそちら)
STATION_AGREEMENT = 2 / 3


def usable_for_station(templates: List[Optional[LogoTemplate]]) -> List[int]:
    """局の型に使える録画の番号: 型があり、最後(新しい録画)の型と大きさ(解像度)が同じもの。"""
    valid = [i for i, t in enumerate(templates) if t is not None and t.frames > 0]
    if not valid:
        return []
    last = templates[valid[-1]]
    shape = (last.gx.shape, last.gy.shape)
    return [i for i in valid if (templates[i].gx.shape, templates[i].gy.shape) == shape]


def combine_templates(templates: List[Optional[LogoTemplate]]) -> Optional[LogoTemplate]:
    """録画ごとの型をまとめて局の型にする(usable_for_station の型だけを使う)。使える型が
    MIN_STATION_RECORDINGS 本に満たなければ None。"""
    usable = [templates[i] for i in usable_for_station(templates)]
    if len(usable) < MIN_STATION_RECORDINGS:
        return None
    frames = sum(t.frames for t in usable)
    shape = _shape(usable[-1])
    parts = [_arrays(t)[:2] + (int(t.frames),) for t in usable]
    return template_from_native(native_module().logo_combine(shape[0], shape[1], parts), shape, frames)


def logo_agreement(imported: LogoTemplate, reference: LogoTemplate) -> Optional[float]:
    """型 (取り込んだロゴ・録画の型) のマスクの縁で、比べる型 (局の型など) と縁の向きが合う割合 (0〜1)。
    大きさが違う・比べる型にロゴが無い時は None。比べる型のロゴが別の隅にしか無ければ 0。
    取り込んだロゴが古いか (lgd_import) と、局のロゴが変わったか (logo_change) に使う。"""
    if (imported.gx.shape, imported.gy.shape) != (reference.gx.shape, reference.gy.shape):
        return None
    return native_module().logo_agreement(*_shape(imported), _arrays(imported), *_shape(reference), _arrays(reference))


def two_way_agreement(a: Optional[LogoTemplate], b: Optional[LogoTemplate]) -> Optional[float]:
    """2 つの型の合い方: どちらから見た一致 (logo_agreement) の大きい方 (どちらも測れなければ None)。"""
    if a is None or b is None or (a.gx.shape, a.gy.shape) != (b.gx.shape, b.gy.shape):
        return None
    return native_module().logo_two_way_agreement(*_shape(a), _arrays(a), *_shape(b), _arrays(b))


def template_to_blob(template: LogoTemplate) -> bytes:
    """型の保存の形 (.npz。gx・gy は float16)。"""
    return npz.save({
        "gx": template.gx.astype("f2"), "gy": template.gy.astype("f2"),
        "mask_x": template.mask_x.astype("b1"), "mask_y": template.mask_y.astype("b1"),
        "strength": Packed.from_values(template.strength, "f4"),
        "frames": Packed.from_values([template.frames], "i8", ()),
    })


def template_from_blob(blob: bytes) -> LogoTemplate:
    data = npz.load(blob)
    return LogoTemplate(
        gx=data["gx"].astype("f4"), gy=data["gy"].astype("f4"),
        mask_x=data["mask_x"].astype("b1"), mask_y=data["mask_y"].astype("b1"),
        strength=data["strength"].astype("f4").flat(), frames=int(data["frames"].item()),
    )


def recording_template_from_features(blob: bytes, include_station: bool = False) -> Optional[LogoTemplate]:
    """features の BLOB から録画の型だけを読む(他の配列は読まない)。一致度を局の型で
    計算した特徴(カットする前)は、include_station でなければ None。"""
    data = npz.load(blob, names=("meta", "logo_gx", "logo_gy", "logo_mask_x", "logo_mask_y"))
    if "logo_gx" not in data:
        return None
    meta = json.loads(npz.text(data["meta"]))
    if meta.get("logo_source") == "station" and not include_station:
        return None
    return LogoTemplate(
        gx=data["logo_gx"].astype("f4"), gy=data["logo_gy"].astype("f4"),
        mask_x=data["logo_mask_x"].astype("b1"), mask_y=data["logo_mask_y"].astype("b1"),
        strength=Packed.from_values(meta.get("logo_strength", [0.0] * 4), "f4").flat(),
        frames=int(meta.get("logo_frames", 0)),
    )


def update_station_logo(db, recording_id: int) -> Optional[LogoTemplate]:
    """録画(の特徴)を保存した後に、その局の型を作り直して DB に保存する。局が分からない
    (番組情報が読めなかった)録画は何もしない。作れない(録画が足りない)時は局の型を消す。"""
    recording = db.get_recording(recording_id)
    if recording is None or recording.get("service_id") is None:
        return None
    return rebuild_station_logo(db, recording.get("original_network_id"), recording["service_id"])


def rebuild_station_logo(db, original_network_id: Optional[int], service_id: int) -> Optional[LogoTemplate]:
    """局の今の型を作り直す。ロゴが変わって作り直しを始めた局(start_station_logo_generation)は、
    その起点より後の録画だけを使う。"""
    onid, sid = original_network_id, service_id
    current = db.get_station_logo(onid, sid)
    since = current.get("generation_start") if current else None
    rows = db.get_station_feature_blobs(onid, sid, since=since)
    templates = [recording_template_from_features(row["data"]) for row in rows]
    used = usable_for_station(templates)
    station = combine_templates(templates)
    if station is None:
        db.save_station_logo(onid, sid, None, 0, None, None)
        return None
    db.save_station_logo(
        onid, sid, template_to_blob(station), len(used), rows[used[0]]["recorded_at"], rows[used[-1]]["recorded_at"],
    )
    return station


def restart_station_logo(db, original_network_id: Optional[int], service_id: int, since: str) -> Optional[LogoTemplate]:
    """ロゴが変わった時(A9、利用者が「はい」を選んだ時): 今の型を期間付きの古い型として残し、
    since(録画の日時)以降の録画だけで局の型を作り直す。録画が足りなければ型は無いまま
    (以後、録画を保存するたびに update_station_logo が作り直す)。

    残す古い型は、起点より前の録画だけで作り直してから残す (ロゴが変わった後の録画も入った今の型は、
    新旧が混ざってロゴが消えていることがある。A9、2026-10-15)。"""
    onid, sid = original_network_id, service_id
    current = db.get_station_logo(onid, sid)
    generation = current.get("generation_start") if current else None
    rows = [row for row in db.get_station_feature_blobs(onid, sid, since=generation) if row["recorded_at"] < since]
    templates = [recording_template_from_features(row["data"]) for row in rows]
    old = combine_templates(templates)
    if old is not None:
        used = usable_for_station(templates)
        db.save_station_logo(onid, sid, template_to_blob(old), len(used),
                             rows[used[0]]["recorded_at"], rows[used[-1]]["recorded_at"])
    db.start_station_logo_generation(original_network_id, service_id, since)
    return rebuild_station_logo(db, original_network_id, service_id)


def load_station_logo(
    db, original_network_id: Optional[int], service_id: int, recorded_at: Optional[str] = None
) -> Optional[LogoTemplate]:
    """局の型(無ければ None)。録画を開いて提案を出す時(A6)に compute_features に渡す。
    recorded_at を渡すと、ロゴが変わる前の録画には、その期間の古い型を使う。"""
    row = db.get_station_logo(original_network_id, service_id, recorded_at)
    if row is None or row.get("logo_data") is None:
        return None
    return template_from_blob(row["logo_data"])

