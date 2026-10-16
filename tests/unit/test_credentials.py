"""Credenciales por entorno: clave de Best Buy y firma de Walmart (spec del rastreo §2.1, §4.5)."""

from __future__ import annotations

import base64
from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from discountz.__main__ import main
from discountz.config import Settings
from discountz.domain.errors import CredentialUnavailable
from discountz.infra.credentials import EnvCredentials
from discountz.sources.base import NoCredentials

NOW = datetime(2026, 10, 16, 12, 0, 0, 123_000, tzinfo=UTC)
BASE_ENV = {
    "DISCOUNTZ_DATABASE_URL": "postgresql://u:p@localhost/db",
    "DISCOUNTZ_TELEGRAM_BOT_TOKEN": "123:abc",
    "DISCOUNTZ_TELEGRAM_CHAT_ID": "1",
    "DISCOUNTZ_TELEGRAM_OWNER_ID": "2",
}
WALMART = (
    "DISCOUNTZ_WALMART_CONSUMER_ID",
    "DISCOUNTZ_WALMART_KEY_VERSION",
    "DISCOUNTZ_WALMART_PRIVATE_KEY",
    "DISCOUNTZ_WALMART_PUBLISHER_ID",
)


@pytest.fixture(scope="module")
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def pem(key: rsa.RSAPrivateKey) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def settings(monkeypatch: pytest.MonkeyPatch, **extra: str) -> Settings:
    for k in (*WALMART, "DISCOUNTZ_BESTBUY_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    for k, v in {**BASE_ENV, **extra}.items():
        monkeypatch.setenv(k, v)
    return Settings()  # type: ignore[call-arg]


def test_walmart_signature_verifies(
    monkeypatch: pytest.MonkeyPatch, rsa_key: rsa.RSAPrivateKey
) -> None:
    s = settings(
        monkeypatch,
        DISCOUNTZ_WALMART_CONSUMER_ID="c0ffee",
        DISCOUNTZ_WALMART_KEY_VERSION="1",
        DISCOUNTZ_WALMART_PRIVATE_KEY=pem(rsa_key),
        DISCOUNTZ_WALMART_PUBLISHER_ID="pub9",
    )
    headers = EnvCredentials(s).signed_headers("walmart", NOW)
    ts = str(int(NOW.timestamp() * 1000))
    assert headers["WM_CONSUMER.ID"] == "c0ffee"
    assert headers["WM_CONSUMER.INTIMESTAMP"] == ts
    assert headers["WM_SEC.KEY_VERSION"] == "1"
    signature = base64.b64decode(headers["WM_SEC.AUTH_SIGNATURE"])
    rsa_key.public_key().verify(
        signature, f"c0ffee\n{ts}\n1\n".encode(), padding.PKCS1v15(), hashes.SHA256()
    )


def test_walmart_key_accepts_escaped_newlines(
    monkeypatch: pytest.MonkeyPatch, rsa_key: rsa.RSAPrivateKey
) -> None:
    """Los secretos de Actions suelen llegar en una línea con `\\n` literales."""
    s = settings(
        monkeypatch,
        DISCOUNTZ_WALMART_CONSUMER_ID="c0ffee",
        DISCOUNTZ_WALMART_KEY_VERSION="1",
        DISCOUNTZ_WALMART_PRIVATE_KEY=pem(rsa_key).replace("\n", "\\n"),
        DISCOUNTZ_WALMART_PUBLISHER_ID="pub9",
    )
    assert "WM_SEC.AUTH_SIGNATURE" in EnvCredentials(s).signed_headers("walmart", NOW)


@pytest.mark.parametrize("count", [1, 2, 3])
def test_walmart_requires_all_four_or_none(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], count: int
) -> None:
    settings(monkeypatch)
    for k in WALMART[:count]:
        monkeypatch.setenv(k, "x")
    assert main(["check-config"]) != 0
    assert "walmart" in capsys.readouterr().err


def test_walmart_missing_raises_credential_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    creds = EnvCredentials(settings(monkeypatch))
    with pytest.raises(CredentialUnavailable):
        creds.signed_headers("walmart", NOW)
    with pytest.raises(CredentialUnavailable):
        creds.signed_headers("otro", NOW)


def test_bestbuy_missing_raises_credential_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(CredentialUnavailable):
        EnvCredentials(settings(monkeypatch)).bearer("bestbuy")
    with pytest.raises(CredentialUnavailable):
        NoCredentials().signed_headers("walmart", NOW)


def test_bestbuy_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    creds = EnvCredentials(settings(monkeypatch, DISCOUNTZ_BESTBUY_API_KEY="k3y"))
    assert creds.bearer("bestbuy").get_secret_value() == "k3y"
    with pytest.raises(CredentialUnavailable):
        creds.bearer("walmart")


def test_repr_hides_secrets(monkeypatch: pytest.MonkeyPatch, rsa_key: rsa.RSAPrivateKey) -> None:
    key = pem(rsa_key)
    s = settings(
        monkeypatch,
        DISCOUNTZ_BESTBUY_API_KEY="sup3rsecret",
        DISCOUNTZ_WALMART_CONSUMER_ID="c0ffee",
        DISCOUNTZ_WALMART_KEY_VERSION="1",
        DISCOUNTZ_WALMART_PRIVATE_KEY=key,
        DISCOUNTZ_WALMART_PUBLISHER_ID="pub9",
    )
    for text in (repr(s), repr(EnvCredentials(s)), str(EnvCredentials(s))):
        assert "sup3rsecret" not in text
        assert "PRIVATE KEY" not in text


def test_empty_env_values_mean_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Actions pasa los secretos no definidos como cadena vacía."""
    s = settings(monkeypatch, DISCOUNTZ_BESTBUY_API_KEY="", **dict.fromkeys(WALMART, ""))
    assert s.bestbuy_api_key is None
    assert s.walmart_private_key is None
    with pytest.raises(CredentialUnavailable):
        EnvCredentials(s).bearer("bestbuy")
