"""Cifrado de los tokens de Garmin en reposo.

Un token de refresco de Garmin da acceso persistente a la cuenta durante casi un
año. Con un solo usuario vivian en disco en claro y era asumible; guardando los
de terceros, una copia de la base de datos no puede bastar para suplantarlos.

Se usa Fernet (AES-128-CBC + HMAC), que viene con `cryptography`, ya presente
como dependencia de garminconnect.
"""
from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from .config import settings


class ErrorDeCifrado(RuntimeError):
    pass


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    if not settings.encryption_key:
        raise ErrorDeCifrado(
            "Falta GB_ENCRYPTION_KEY. Genera una con:\n"
            "  python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        )
    try:
        return Fernet(settings.encryption_key.encode())
    except Exception as exc:
        raise ErrorDeCifrado(
            "GB_ENCRYPTION_KEY no es una clave Fernet valida (32 bytes en base64 url-safe)."
        ) from exc


def cifrar(texto: str) -> str:
    return _fernet().encrypt(texto.encode()).decode()


def descifrar(blob: str) -> str:
    """Descifra, distinguiendo 'clave cambiada' de 'dato corrupto'.

    Si la clave se pierde o se rota sin migrar, los tokens dejan de poder
    descifrarse y toca rehacer el login de Garmin. Conviene que el error lo diga
    en vez de parecer un fallo de sesion cualquiera.
    """
    try:
        return _fernet().decrypt(blob.encode()).decode()
    except InvalidToken as exc:
        raise ErrorDeCifrado(
            "No se pudo descifrar el token: la GB_ENCRYPTION_KEY no es la que "
            "cifro este dato. El usuario tendra que volver a vincular la cuenta."
        ) from exc
