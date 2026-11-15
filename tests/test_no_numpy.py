"""アプリが numpy を使わないこと (設計書 §31.9 段階 ⑤、2026-10-30)。

numpy はアプリの依存から外した (.exe を小さくするため)。値は packed.Packed、保存は npz.py。numpy を使って
よいのはテストと確かめのツール (tools/) と、Packed を numpy の配列にする Packed.__array__ の中だけ。
"""

import ast
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from ts_smartcut.autocut import npz
from ts_smartcut.autocut.packed import Packed

PACKAGE = Path(__file__).resolve().parents[1] / "ts_smartcut"


def _numpy_imports(tree: ast.AST):
    """numpy を読み込む文と、それを含む関数の名前。"""
    found = []

    def visit(node, function):
        for child in ast.iter_child_nodes(node):
            name = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else function
            if isinstance(child, ast.Import) and any(a.name.split(".")[0] == "numpy" for a in child.names):
                found.append((child.lineno, function))
            elif isinstance(child, ast.ImportFrom) and (child.module or "").split(".")[0] == "numpy":
                found.append((child.lineno, function))
            visit(child, name)

    visit(tree, None)
    return found


def test_the_app_does_not_import_numpy():
    found = []
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(PACKAGE).as_posix()
        for line, function in _numpy_imports(ast.parse(path.read_text(encoding="utf-8"))):
            if (rel, function) != ("autocut/packed.py", "__array__"):
                found.append(f"{rel}:{line}")
    assert found == []


def test_starting_the_app_modules_does_not_load_numpy():
    code = ("import sys; import ts_smartcut.main, ts_smartcut.gui.main_window, ts_smartcut.autocut.model, "
            "ts_smartcut.autocut.features, ts_smartcut.autocut.lgd_export, ts_smartcut.autocut.tuning; "
            "print('numpy' in sys.modules)")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                            cwd=PACKAGE.parent, check=True)
    assert result.stdout.strip() == "False"


@pytest.mark.parametrize("dtype, shape", [
    (np.uint8, (7,)), (np.bool_, (4, 3, 5)), (np.int16, (2, 3)), (np.float16, (4, 6, 9)), (np.float32, (0,)),
    (np.float64, (3, 2)), (np.int64, ()),
])
def test_npz_reads_numpy_files_and_numpy_reads_ours(dtype, shape):
    """記録 DB の .npz: numpy で書いた記録をそのまま読み、ここで書いた記録は numpy でも読める (DB の形は変えない)。"""
    import io

    rng = np.random.default_rng(0)
    values = (rng.random(shape) * 100).astype(dtype) if shape else dtype(42)
    buf = io.BytesIO()
    np.savez_compressed(buf, a=values, meta=np.frombuffer(b'{"x": 1}', np.uint8))
    loaded = npz.load(buf.getvalue())
    assert loaded["a"].shape == np.shape(values) and np.asarray(loaded["a"]).tobytes() == np.asarray(values).tobytes()
    assert npz.text(loaded["meta"]) == '{"x": 1}'
    with np.load(io.BytesIO(npz.save(loaded)), allow_pickle=False) as again:
        assert again["a"].dtype == np.asarray(values).dtype and again["a"].shape == np.shape(values)
        assert again["a"].tobytes() == np.asarray(values).tobytes()


def test_npz_reads_only_the_names_asked_for():
    blob = npz.save({"a": Packed.from_values([1, 2], "u1"), "b": Packed.from_values([1.5], "f4")})
    assert list(npz.load(blob, names=("b",))) == ["b"]


def test_packed_rounds_float16_like_numpy_and_indexes_like_numpy():
    values = [0.1, 65519.0, 70000.0, 1e-8, -2.5, 6.1e-5]
    assert Packed.from_values(values, "f2").data == np.array(values).astype(np.float16).tobytes()
    grid = np.arange(24, dtype=np.int16).reshape(2, 3, 4)
    p = Packed(grid.tobytes(), "i2", grid.shape)
    assert p[1].tolist() == grid[1].tolist() and p[1, 2, 3] == grid[1, 2, 3] and p[-1, 0, 0] == grid[-1, 0, 0]
    assert p[0:2].tolist() == grid[0:2].tolist() and p.tolist() == grid.tolist()
    assert p.astype("f4").tolist() == grid.astype(np.float32).tolist()
    assert np.array_equal(np.asarray(p), grid)
    with pytest.raises(ValueError):
        Packed(b"\0" * 5, "i2", (3,))
