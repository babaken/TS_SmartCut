import hashlib

import build_release


def test_sha256_file_uses_lf_on_every_os(tmp_path):
    """The .sha256 must check with `sha256sum -c` on Linux, also when written on Windows."""
    target = tmp_path / "TS_SmartCut-0.0.0-setup.exe"
    target.write_bytes(b"release file")

    sha_path = build_release.write_sha256(target)

    expected = f"{hashlib.sha256(b'release file').hexdigest()}  {target.name}\n".encode("ascii")
    assert sha_path.name == target.name + ".sha256"
    assert sha_path.read_bytes() == expected
