"""Credenciales leídas del entorno: clave de Best Buy, firma RSA de Walmart, eBay y Mercado Libre.

Nunca se registran ni se ponen en `repr`; los errores llevan solo el proveedor y un código.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from datetime import datetime

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from pydantic import SecretStr

from discountz.config import Settings
from discountz.domain.errors import CredentialUnavailable


class EnvCredentials:
    def __init__(self, settings: Settings) -> None:
        self._bestbuy = settings.bestbuy_api_key
        self._woot = settings.woot_api_key
        self._ebay: SecretStr | None = None
        if settings.ebay_app_id and settings.ebay_cert_id:
            pair = f"{settings.ebay_app_id}:{settings.ebay_cert_id.get_secret_value()}"
            self._ebay = SecretStr(base64.b64encode(pair.encode()).decode("ascii"))
        self._meli: tuple[str, SecretStr] | None = None
        if settings.meli_client_id and settings.meli_client_secret:
            self._meli = (settings.meli_client_id, settings.meli_client_secret)
        self._walmart: tuple[str, str, SecretStr] | None = None
        self._publisher = settings.walmart_publisher_id
        if (
            settings.walmart_consumer_id
            and settings.walmart_key_version
            and settings.walmart_private_key
        ):
            self._walmart = (
                settings.walmart_consumer_id,
                settings.walmart_key_version,
                settings.walmart_private_key,
            )

    def __repr__(self) -> str:
        return (
            f"EnvCredentials(bestbuy={self._bestbuy is not None}, "
            f"walmart={self._walmart is not None}, ebay={self._ebay is not None}, "
            f"woot={self._woot is not None}, mercadolibre={self._meli is not None})"
        )

    def bearer(self, provider: str) -> SecretStr:
        if provider == "bestbuy" and self._bestbuy is not None:
            return self._bestbuy
        if provider == "ebay" and self._ebay is not None:
            return self._ebay  # `Basic` del token de aplicación (base64 de app_id:cert_id)
        if provider == "woot" and self._woot is not None:
            return self._woot  # va en la cabecera `x-api-key`
        if provider == "mercadolibre" and self._meli is not None:
            return self._meli[1]  # `client_secret` del token de la app
        raise CredentialUnavailable(provider, "missing")

    def public_id(self, provider: str) -> str:
        if provider == "walmart" and self._walmart is not None and self._publisher:
            return self._publisher
        if provider == "mercadolibre" and self._meli is not None:
            return self._meli[0]  # `client_id` de la app
        raise CredentialUnavailable(provider, "missing")

    def signed_headers(self, provider: str, now: datetime) -> Mapping[str, str]:
        """Cabeceras de la API de afiliados de Walmart (firma PKCS#1 v1.5 + SHA-256)."""
        if provider != "walmart" or self._walmart is None:
            raise CredentialUnavailable(provider, "missing")
        consumer_id, key_version, secret = self._walmart
        ts = str(int(now.timestamp() * 1000))
        message = f"{consumer_id}\n{ts}\n{key_version}\n".encode()
        signature = _private_key(secret).sign(message, padding.PKCS1v15(), hashes.SHA256())
        return {
            "WM_CONSUMER.ID": consumer_id,
            "WM_CONSUMER.INTIMESTAMP": ts,
            "WM_SEC.KEY_VERSION": key_version,
            "WM_SEC.AUTH_SIGNATURE": base64.b64encode(signature).decode("ascii"),
        }


def _private_key(secret: SecretStr) -> rsa.RSAPrivateKey:
    """Acepta PEM con saltos reales o con `\\n` literales (secretos de una línea)."""
    text = secret.get_secret_value().replace("\\n", "\n").strip()
    try:
        key = serialization.load_pem_private_key(text.encode(), password=None)
    except (ValueError, TypeError):
        raise CredentialUnavailable("walmart", "bad_private_key") from None
    if not isinstance(key, rsa.RSAPrivateKey):
        raise CredentialUnavailable("walmart", "bad_private_key")
    return key
