"""Panel LUT loading and composition in the framebuffer equalizer."""
from cyberpunk_computer.core import framebuffer as fb


def _tables(tmp_path, data):
    p = tmp_path / "lut.bin"
    p.write_bytes(data)
    return str(p)


def test_off_by_default(monkeypatch):
    monkeypatch.delenv("MFD_LUT_FILE", raising=False)
    assert fb._load_panel_lut() is None


def test_fitted_lut_is_valid():
    from pathlib import Path
    lut = Path(__file__).resolve().parent.parent / "tools/calib/mfd_lut.bin"
    assert len(lut.read_bytes()) == 768


def test_lut_file_loaded_and_applied(tmp_path, monkeypatch):
    r = bytes(255 - i for i in range(256))
    g = bytes(i // 2 for i in range(256))
    b = bytes(range(256))
    monkeypatch.setenv("MFD_LUT_FILE", _tables(tmp_path, r + g + b))
    for k in ("MFD_GAIN_R", "MFD_GAIN_G", "MFD_GAIN_B"):
        monkeypatch.delenv(k, raising=False)
    out = fb.FramebufferOutput()
    assert out._lut_r == r and out._lut_g == g
    assert out._lut_b is None  # identity table stays passthrough
    px = bytes([10, 100, 200, 255])  # B G R A
    assert bytes(out._equalize_bgra(px)) == bytes([10, 50, 55, 255])


def test_gain_then_panel(tmp_path, monkeypatch):
    panel = bytes(min(255, i + 1) for i in range(256))
    monkeypatch.setenv("MFD_LUT_FILE", _tables(tmp_path, panel * 3))
    monkeypatch.setenv("MFD_GAIN_R", "0.5")
    out = fb.FramebufferOutput()
    assert out._lut_r[200] == panel[100]


def test_disabled_and_invalid(tmp_path, monkeypatch):
    monkeypatch.setenv("MFD_LUT_FILE", "none")
    assert fb._load_panel_lut() is None
    monkeypatch.setenv("MFD_LUT_FILE", _tables(tmp_path, b"\x00" * 10))
    assert fb._load_panel_lut() is None
