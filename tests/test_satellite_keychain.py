"""Tests for the satellite keychain (docs/SATELLITES.md §1-2)."""

import json
import os
import stat

import pytest

from cyberpunk_computer.backend import satellite_keychain as kc_mod
from cyberpunk_computer.backend.satellite_keychain import (
    Keychain,
    KeychainError,
    b64d,
    b64e,
    canonical_json,
    fingerprint,
)

pytest.importorskip("cryptography")


@pytest.fixture
def kc(tmp_path):
    k = Keychain(str(tmp_path / "keys"))
    k.init_car_key()
    return k


def test_fingerprint_rule():
    fp = fingerprint(b"\x01" * 32)
    assert len(fp) == 26 and fp == fp.lower() and "=" not in fp
    assert fingerprint(b"\x01" * 32) == fp
    assert fingerprint(b"\x02" * 32) != fp


def test_canonical_json_is_sorted_compact_utf8():
    assert canonical_json({"b": 1, "a": "ż"}) == '{"a":"ż","b":1}'.encode("utf-8")


def test_init_car_key_files_and_modes(tmp_path):
    k = Keychain(str(tmp_path / "k"))
    assert not k.has_car_key() and k.car_fp is None
    fp, created = k.init_car_key()
    assert created and len(fp) == 26
    assert stat.S_IMODE(os.stat(k.car_key_path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(k.car_pub_path).st_mode) == 0o644
    assert stat.S_IMODE(os.stat(k.path).st_mode) == 0o600
    assert json.load(open(k.path))["car_fp"] == fp
    fp2, created2 = k.init_car_key()
    assert fp2 == fp and not created2


def test_new_satellite_blob_and_cert(kc):
    with kc.transaction():
        blob = kc.new_satellite("garage", "ble", caps=["relay"])
    assert blob["sat_id"] == 300 and blob["transport"] == "ble"
    assert fingerprint(b64d(blob["pubkey"])) == blob["fp"]
    assert kc_mod.public_from_private(b64d(blob["privkey"])) == b64d(blob["pubkey"])
    assert kc_mod.verify_cert(kc.car_pub, blob["cert"]) is None
    assert b64d(blob["car_pub"]) == kc.car_pub
    # persisted, reloadable
    k2 = Keychain(kc.directory)
    assert k2.get(300)["fp"] == blob["fp"] and k2.get(300)["auth"] == "ed25519"


def test_cert_tamper_and_expiry_detected(kc):
    priv, pub = kc_mod.generate_keypair()
    body = kc_mod.cert_body(301, "x", "ble", b64e(pub), [], 1000, None)
    cert = kc_mod.sign_cert(kc.car_priv, body)
    assert kc_mod.verify_cert(kc.car_pub, cert) is None
    bad = dict(cert, name="y")
    assert kc_mod.verify_cert(kc.car_pub, bad) == "cert signature"
    assert kc_mod.verify_cert(kc.car_pub, dict(cert, transport="wifi")) is not None
    expired = kc_mod.sign_cert(kc.car_priv, dict(body, expires=2000))
    assert kc_mod.verify_cert(kc.car_pub, expired, now=3000) == "cert expired"
    other_priv, other_pub = kc_mod.generate_keypair()
    assert kc_mod.verify_cert(other_pub, cert) == "cert signature"


def test_free_id_allocation_per_range(kc):
    with kc.transaction():
        a = kc.new_satellite("a", "wifi")["sat_id"]
        b = kc.new_satellite("b", "wifi")["sat_id"]
        c = kc.new_satellite("c", "ble", sat_id=305)["sat_id"]
        d = kc.new_satellite("d", "ble")["sat_id"]
    assert (a, b, c, d) == (400, 401, 305, 300)
    with pytest.raises(KeychainError):
        kc.new_satellite("e", "wifi", sat_id=300)   # wrong range
    with pytest.raises(KeychainError):
        kc.new_satellite("a", "wifi")               # name taken
    with pytest.raises(KeychainError):
        kc.new_satellite("f", "wifi", sat_id=400)   # id taken


def test_range_full(kc, monkeypatch):
    monkeypatch.setitem(kc_mod.ID_RANGES, "wifi", (400, 401))
    kc.new_satellite("a", "wifi")
    kc.new_satellite("b", "wifi")
    with pytest.raises(KeychainError):
        kc.free_id("wifi")


def test_revoke_and_forget(kc):
    with kc.transaction():
        blob = kc.new_satellite("garage", "wifi")
        kc.add_rs485(106, "drl")
    with kc.transaction():
        with pytest.raises(KeychainError, match="nothing to revoke"):
            kc.revoke("drl")
    with kc.transaction():
        sid, fp = kc.revoke("garage")
    assert sid == 400 and fp == blob["fp"] and kc.is_revoked(fp)
    assert kc.get(400) is None
    # re-enrolling a revoked key is refused
    with pytest.raises(KeychainError, match="revoked"):
        kc.enrol_pubkey("again", "wifi", blob["pubkey"])
    with kc.transaction():
        kc.forget(106)
    assert kc.get(106) is None
    with pytest.raises(KeychainError):
        kc.forget(106)


def test_reload_if_changed_sees_other_writer(kc):
    other = Keychain(kc.directory)
    assert not kc.reload_if_changed()
    with other.transaction():
        other.add_rs485(107, "light")
    assert kc.reload_if_changed()
    assert kc.get(107)["name"] == "light"


def test_dir_env_and_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv(kc_mod.ENV_KEYCHAIN_DIR, str(tmp_path / "envdir"))
    assert Keychain().directory == str(tmp_path / "envdir")
    monkeypatch.chdir(tmp_path)
    blocked = tmp_path / "ro"
    blocked.mkdir()
    blocked.chmod(0o500)
    try:
        if os.access(str(blocked), os.W_OK):
            pytest.skip("running as root")
        k = Keychain(str(blocked / "sub"))
        assert k.directory == str(tmp_path / "satellites")
    finally:
        blocked.chmod(0o700)


def test_accept_cert_renames_on_name_clash(kc):
    with kc.transaction():
        kc.new_satellite("garage", "wifi")
    priv, pub = kc_mod.generate_keypair()
    cert = kc_mod.sign_cert(kc.car_priv, kc_mod.cert_body(410, "garage", "wifi", b64e(pub)))
    sid, entry = kc.accept_cert(cert)
    assert sid == 410 and entry["name"] == "garage-410"


def test_ble_role_beacon_needs_address_and_lands_in_blob(kc):
    with kc.transaction():
        with pytest.raises(KeychainError, match="needs its BLE address"):
            kc.new_satellite("door", "ble", ble_role="beacon")
        with pytest.raises(KeychainError, match="BLE satellites only"):
            kc.new_satellite("door", "wifi", ble_role="beacon", address="AA:BB:CC:DD:EE:B1")
        with pytest.raises(KeychainError, match="ble_role must be"):
            kc.new_satellite("door", "ble", ble_role="mesh", address="AA:BB:CC:DD:EE:B1")
        with pytest.raises(KeychainError, match="bad BLE address"):
            kc.new_satellite("door", "ble", ble_role="beacon", address="nope")
        blob = kc.new_satellite("door", "ble", ble_role="beacon", address="aa-bb-cc-dd-ee-b1")
        plain = kc.new_satellite("garage", "ble")
    entry = Keychain(kc.directory).get(blob["sat_id"])
    assert entry["ble_role"] == "beacon" and entry["address"] == "AA:BB:CC:DD:EE:B1"
    assert blob["ble"]["role"] == "beacon" and blob["ble"]["address"] == "AA:BB:CC:DD:EE:B1"
    assert "ble_role" not in kc.get(plain["sat_id"]) and plain["ble"]["role"] == "scanner"
    assert kc_mod.verify_cert(kc.car_pub, blob["cert"]) is None   # cert fields unchanged


def test_satkey_cli_ble_role(kc, capsys):
    import importlib.machinery
    import importlib.util
    from pathlib import Path
    script = Path(__file__).resolve().parents[1] / "POCO/rootfs/usr/local/sbin/prius-satkey"
    loader = importlib.machinery.SourceFileLoader("prius_satkey", str(script))
    spec = importlib.util.spec_from_loader("prius_satkey", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    pub = b64e(b"\x07" * 32)
    assert mod.main(["--dir", kc.directory, "sign", "door", "--pubkey", pub, "--transport", "ble",
                     "--ble-role", "beacon", "--address", "AA:BB:CC:DD:EE:B1"]) == 0
    blob = json.loads(capsys.readouterr().out)
    assert blob["ble"] == {"service": "580e04f7-d4c0-458b-b2e0-4ebb01a960c8",
                           "car_addr": "02:00:E8:51:02:58", "role": "beacon",
                           "address": "AA:BB:CC:DD:EE:B1"}
    with pytest.raises(SystemExit, match="needs its BLE address"):
        mod.main(["--dir", kc.directory, "sign", "door2", "--pubkey", b64e(b"\x08" * 32),
                  "--transport", "ble", "--ble-role", "beacon"])
    mod.main(["--dir", kc.directory, "list"])
    assert "[beacon AA:BB:CC:DD:EE:B1]" in capsys.readouterr().out
