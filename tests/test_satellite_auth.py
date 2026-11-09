"""Tests for the satellite auth handshake (docs/SATELLITES.md §3)."""

import pytest

pytest.importorskip("cryptography")

from cyberpunk_computer.backend import satellite_keychain as kc_mod
from cyberpunk_computer.backend.satellite_auth import (
    FAILED,
    HELD,
    OK,
    PENDING,
    LinkAuth,
    SatelliteAuthClient,
    SatelliteAuthError,
)
from cyberpunk_computer.backend.satellite_keychain import Keychain, b64e


@pytest.fixture
def kc(tmp_path):
    k = Keychain(str(tmp_path / "keys"))
    k.init_car_key()
    return k


def unenrolled_blob(kc, transport="wifi", with_cert=False, sat_id=420):
    priv, pub = kc_mod.generate_keypair()
    blob = {"privkey": b64e(priv), "pubkey": b64e(pub), "fp": kc_mod.fingerprint(pub),
            "transport": transport, "car_pub": b64e(kc.car_pub), "cert": None}
    if with_cert:
        blob["cert"] = kc_mod.sign_cert(kc.car_priv, kc_mod.cert_body(
            sat_id, "certsat", transport, blob["pubkey"]))
    return blob


def run(auth, client, pairing=False, now=0.0):
    reply = client.respond(auth.challenge())
    return auth.handle(reply, now=now, pairing_open=pairing)


def test_enrolled_satellite_mutual_auth(kc):
    with kc.transaction():
        blob = kc.new_satellite("garage", "wifi")
    auth = LinkAuth(kc, "wifi", "10.42.0.5:8788", now=0.0)
    client = SatelliteAuthClient(blob)
    res = run(auth, client)
    assert res.state == OK and res.sat_id == 400 and res.reply["a"] == "auth_ok"
    assert client.check_result(res.reply) == 400 and client.authenticated


def test_valid_cert_from_unknown_satellite_is_trusted(kc):
    blob = unenrolled_blob(kc, with_cert=True, sat_id=421)
    auth = LinkAuth(kc, "wifi", "a", now=0.0)
    res = run(auth, SatelliteAuthClient(blob))
    assert res.state == OK and res.cert_sat_id == 421 and res.cert is not None


def test_cert_for_other_transport_refused(kc):
    blob = unenrolled_blob(kc, transport="ble", with_cert=True, sat_id=310)
    blob["transport"] = "wifi"   # satellite lies about the link it is on
    auth = LinkAuth(kc, "wifi", "a", now=0.0)
    res = run(auth, SatelliteAuthClient(blob))
    assert res.state == FAILED


def test_unknown_refused_without_pairing_held_with_pairing(kc):
    blob = unenrolled_blob(kc)
    res = run(LinkAuth(kc, "wifi", "a", now=0.0), SatelliteAuthClient(blob))
    assert res.state == FAILED and res.reply == {"a": "auth_fail", "why": "unknown device"}

    auth = LinkAuth(kc, "wifi", "a", now=0.0)
    client = SatelliteAuthClient(blob)
    res = run(auth, client, pairing=True)
    assert res.state == HELD and res.reply is None and res.pubkey == blob["pubkey"]
    assert not auth.expired(100.0)         # held links do not time out here
    # operator enrols -> auth_ok on the same session verifies on the satellite
    with kc.transaction():
        sid, entry = kc.enrol_pubkey("newsat", "wifi", res.pubkey)
    msg = auth.auth_ok(sid, entry["cert"])
    assert client.check_result(msg) == sid and client.cert == entry["cert"]


def test_revoked_refused_even_with_cert(kc):
    blob = unenrolled_blob(kc, with_cert=True)
    with kc.transaction():
        kc.revoke(blob["fp"])
    res = run(LinkAuth(kc, "wifi", "a", now=0.0), SatelliteAuthClient(blob))
    assert res.state == FAILED and res.why == "revoked"


def test_bad_signature_and_fp_mismatch(kc):
    with kc.transaction():
        blob = kc.new_satellite("garage", "wifi")
    auth = LinkAuth(kc, "wifi", "a", now=0.0)
    reply = SatelliteAuthClient(blob).respond(auth.challenge())
    tampered = dict(reply, nonce=b64e(b"\x00" * 32))
    assert auth.handle(tampered, now=0.0).why == "bad signature"

    auth = LinkAuth(kc, "wifi", "a", now=0.0)
    reply = SatelliteAuthClient(blob).respond(auth.challenge())
    assert auth.handle(dict(reply, fp="a" * 26), now=0.0).why == "fp does not match pubkey"


def test_replayed_reply_fails_on_new_challenge(kc):
    with kc.transaction():
        blob = kc.new_satellite("garage", "wifi")
    first = LinkAuth(kc, "wifi", "a", now=0.0)
    reply = SatelliteAuthClient(blob).respond(first.challenge())
    second = LinkAuth(kc, "wifi", "a", now=0.0)
    assert second.handle(reply, now=0.0).state == FAILED


def test_wrong_transport_for_enrolled_key(kc):
    with kc.transaction():
        blob = kc.new_satellite("garage", "ble")
    blob["transport"] = "wifi"
    res = run(LinkAuth(kc, "wifi", "a", now=0.0), SatelliteAuthClient(blob))
    assert res.state == FAILED


def test_timeout_and_non_auth_ignored(kc):
    auth = LinkAuth(kc, "wifi", "a", now=0.0, timeout_s=10)
    assert auth.handle({"a": "ping"}, now=1.0).state == PENDING
    assert not auth.expired(9.9) and auth.expired(10.0)
    with kc.transaction():
        blob = kc.new_satellite("garage", "wifi")
    reply = SatelliteAuthClient(blob).respond(auth.challenge())
    assert auth.handle(reply, now=11.0).why == "timeout"


def test_client_rejects_foreign_car_and_forged_ok(kc, tmp_path):
    with kc.transaction():
        blob = kc.new_satellite("garage", "wifi")
    other = Keychain(str(tmp_path / "other"))
    other.init_car_key()
    client = SatelliteAuthClient(blob)
    with pytest.raises(SatelliteAuthError):
        client.respond(LinkAuth(other, "wifi", "a").challenge())
    # forged auth_ok signed by a different car key
    auth = LinkAuth(kc, "wifi", "a", now=0.0)
    reply = client.respond(auth.challenge())
    assert auth.handle(reply, now=0.0).state == OK
    fake = LinkAuth(other, "wifi", "a")
    fake.car_nonce, fake.sat_nonce, fake.fp = auth.car_nonce, auth.sat_nonce, auth.fp
    with pytest.raises(SatelliteAuthError):
        client.check_result(fake.auth_ok(400))
    with pytest.raises(SatelliteAuthError):
        client.check_result({"a": "auth_fail", "why": "x"})


def test_no_car_key_means_no_challenge(tmp_path):
    k = Keychain(str(tmp_path / "empty"))
    assert LinkAuth(k, "wifi", "a").challenge() is None
