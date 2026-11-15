"""The Pi's timesyncd config exists twice: written by prius-deploy-mfd on every
deploy and baked into a fresh image (cyberpunk-prius-gateway
raspberry_pi_config/zero13-image/timesyncd-prius.conf). They must be identical."""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "POCO/rootfs/usr/local/sbin/prius-deploy-mfd"
IMAGE = ROOT.parent / "cyberpunk-prius-gateway/raspberry_pi_config/zero13-image/timesyncd-prius.conf"


def _deploy_copy() -> str:
    s = DEPLOY.read_text()
    return s.split("cat > /tmp/prius-time.conf <<'X'\n", 1)[1].split("\nX\n", 1)[0] + "\n"


def test_deploy_config_content():
    conf = _deploy_copy()
    for line in ("NTP=192.168.100.1", "FallbackNTP=", "PollIntervalMaxSec=64", "SaveIntervalSec=60"):
        assert line + "\n" in conf


@pytest.mark.skipif(not IMAGE.exists(), reason="gateway repo not checked out next to this one")
def test_image_copy_identical():
    assert IMAGE.read_text() == _deploy_copy()
