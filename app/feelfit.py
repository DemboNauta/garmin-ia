"""Cliente de la nube de FeelFit (basculas QN Cloud, feelfit.qnclouds.com).

API no oficial: la misma que habla la app de Android. No esta documentada en
ningun sitio; lo que hay aqui sale de mirar dos integraciones que ya la habian
descifrado (la de Home Assistant de Sanji78 y el mcp-feelfit de
tecnologicachile) y de comprobar contra el servidor que sigue respondiendo asi.
Puede romperse sin aviso, como el resto de capas no oficiales del proyecto.

Tres rarezas que conviene saber antes de tocar esto:

  - La contraseña no viaja en claro sino cifrada con RSA contra una clave
    publica que la propia app lleva dentro. No es un secreto nuestro: cualquiera
    la saca del APK, y sin ella el servidor no acepta el login.
  - Todo responde HTTP 200, tambien los errores. El fallo de verdad va en el
    campo `code` del JSON ("20101" = la cuenta no existe), asi que mirar el
    codigo HTTP no sirve de nada.
  - El servidor identifica al cliente por los parametros de la query, no por
    cabeceras: sin `app_id=Feelfit` y compañia contesta como si no existiera el
    endpoint.
"""
from __future__ import annotations

import base64
import datetime as dt
import logging
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import load_pem_public_key

from .config import settings

log = logging.getLogger(__name__)

# Lo que se le enseña al usuario al elegir marca en el formulario.
NOMBRE = "FeelFit"
DESCRIPCION = "Basculas que se manejan con la app FeelFit (nube QN Cloud)"

BASE = "https://feelfit.qnclouds.com/api/v4"
ESPERA = 20  # segundos

CLAVE_PUBLICA = b"""-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQC+25I2upukpfQ7rIaaTZtVE744
u2zV+HaagrUhDOTq8fMVf9yFQvEZh2/HKxFudUxP0dXUa8F6X4XmWumHdQnum3zm
Jr04fz2b2WCcN0ta/rbF2nYAnMVAk2OJVZAMudOiMWhcxV1nNJiKgTNNr13de0EQ
IiOL2CUBzu+HmIfUbQIDAQAB
-----END PUBLIC KEY-----"""

# Identificacion del cliente. Son los valores de una version concreta de la app
# de Android: si algun dia el servidor empieza a rechazar por version vieja, es
# `app_revision` lo que hay que subir.
CLIENTE = {
    "app_revision": "4.16.0",
    "html_version": "14.16.0",
    "cellphone_type": "samsung SM-T510",
    "system_type": "11_30",
    "app_id": "Feelfit",
    "platform": "android",
    "area_code": "ES",
    "locale": "es",
}

CABECERAS = {
    "Accept-Encoding": "gzip",
    "Connection": "Keep-Alive",
    "User-Agent": "okhttp/4.9.1",
}

CODIGOS_OK = ("200", "0")


class ErrorFeelFit(RuntimeError):
    """La nube de FeelFit contesto, pero con un error."""


class SesionCaducada(ErrorFeelFit):
    """El token ya no vale: hay que volver a vincular con email y contraseña."""


@dataclass(frozen=True)
class Sesion:
    """Lo unico que se guarda de una cuenta de FeelFit.

    La contraseña NO esta aqui a proposito, igual que con Garmin: se usa una vez
    para conseguir el token y se olvida. El token dura unos seis meses; cuando
    caduque habra que volver a pasar por el formulario, que es barato, en vez de
    tener guardada una contraseña ajena para poder renovarlo solos.
    """

    token: str
    expira_en: float          # epoch en segundos
    user_id: str              # el identificador de la persona dentro de FeelFit
    cuenta: str               # su email, solo para poder enseñarlo

    @property
    def viva(self) -> bool:
        return bool(self.token) and time.time() < self.expira_en


# ------------------------------------------------------------------ transporte
def _parametros(extra: dict[str, Any] | None = None) -> str:
    params = {**CLIENTE, "zone": settings.timezone}
    if extra:
        params.update({k: str(v) for k, v in extra.items()})
    return urllib.parse.urlencode(params)


def _url(path: str, extra: dict[str, Any] | None = None) -> str:
    return f"{BASE}{path}?{_parametros(extra)}"


def _datos(respuesta: httpx.Response) -> dict:
    """Saca el `data` de una respuesta, convirtiendo el `code` en excepcion."""
    if respuesta.status_code == 401:
        raise SesionCaducada("FeelFit ya no acepta el token guardado.")
    respuesta.raise_for_status()
    cuerpo = respuesta.json()
    codigo = str(cuerpo.get("code", ""))
    if codigo not in CODIGOS_OK:
        mensaje = cuerpo.get("msg") or f"error {codigo}"
        # 20105/20106 y similares son de sesion; el resto son de peticion.
        if codigo.startswith("2010") and "token" in mensaje.lower():
            raise SesionCaducada(mensaje)
        raise ErrorFeelFit(mensaje)
    return cuerpo.get("data") or {}


def _cifrar_password(password: str) -> str:
    clave = load_pem_public_key(CLAVE_PUBLICA)
    return base64.b64encode(clave.encrypt(password.encode(), padding.PKCS1v15())).decode()


# --------------------------------------------------------------------- lectura
def iniciar_sesion(email: str, password: str) -> Sesion:
    """Cambia email y contraseña por un token. Es lo unico que pide la clave."""
    cabeceras = {
        **CABECERAS,
        "Authorization": "Bearer",
        "Content-Type": "application/json;charset=UTF-8",
    }
    cuerpo = {"email": email.strip(), "password": _cifrar_password(password)}
    with httpx.Client(timeout=ESPERA) as cliente:
        datos = _datos(cliente.post(_url("/users/sign_in"), headers=cabeceras, json=cuerpo))

    token = (datos.get("token_info") or {}).get("token")
    if not token:
        raise ErrorFeelFit("FeelFit no devolvio ningun token para esta cuenta.")
    usuario = datos.get("user_info") or {}
    return Sesion(
        token=token,
        expira_en=time.time() + float((datos["token_info"].get("remaining_time") or 0)),
        user_id=str(usuario.get("user_id") or ""),
        cuenta=email.strip(),
    )


def _get(sesion: Sesion, path: str, extra: dict[str, Any] | None = None) -> dict:
    if not sesion.viva:
        raise SesionCaducada("El acceso a FeelFit ha caducado.")
    cabeceras = {**CABECERAS, "Authorization": f"Bearer {sesion.token}"}
    with httpx.Client(timeout=ESPERA) as cliente:
        return _datos(cliente.get(_url(path, extra), headers=cabeceras))


# De los nombres de FeelFit a los nuestros, con los decimales que tienen sentido
# para cada cosa (0 = numero entero). Lo que no este aqui se descarta: la
# respuesta trae ademas identificadores internos y banderas que no dicen nada.
_CAMPOS: dict[str, tuple[str, int]] = {
    "weight": ("weight_kg", 2),
    "bodyfat": ("body_fat_pct", 1),
    "sinew": ("muscle_mass_kg", 2),          # masa muscular esqueletica
    "muscle": ("muscle_pct", 1),
    "water": ("water_pct", 1),
    "protein": ("protein_pct", 1),
    "subfat": ("subcutaneous_fat_pct", 1),
    "visfat": ("visceral_fat", 1),           # indice, no kilos ni por ciento
    "bone": ("bone_mass_kg", 2),
    "fat_free_weight": ("fat_free_kg", 2),
    "body_fat_mass": ("fat_mass_kg", 2),
    "bmi": ("bmi", 1),
    "bmr": ("bmr_kcal", 0),
    "bodyage": ("metabolic_age", 0),
    "heart_rate": ("heart_rate", 0),
}


def _numero(valor: Any, decimales: int) -> float | int | None:
    """FeelFit devuelve los numeros unas veces como float y otras como texto."""
    try:
        n = float(valor)
    except (TypeError, ValueError):
        return None
    return int(round(n)) if decimales == 0 else round(n, decimales)


def _medida(cruda: dict) -> dict:
    """Un pesaje de FeelFit -> la forma neutra que usa el resto del backend.

    El peso viene ya en kilos (Garmin, en cambio, lo da en gramos). La marca de
    tiempo es epoch, asi que se pasa a la zona del usuario para que el dia sea
    el suyo y no el del servidor.
    """
    salida: dict[str, Any] = {}
    marca = _numero(cruda.get("time_stamp"), 0)
    if marca:
        cuando = dt.datetime.fromtimestamp(marca, ZoneInfo(settings.timezone))
        salida["date"] = cuando.date().isoformat()
        salida["at"] = cuando.isoformat()
    for origen, (destino, decimales) in _CAMPOS.items():
        valor = _numero(cruda.get(origen), decimales)
        if valor is not None:
            salida[destino] = valor
    return salida


def medidas(sesion: Sesion) -> list[dict]:
    """Historico de pesajes, del mas reciente al mas antiguo.

    Se pide entero (`last_updated_at=0`) en vez de incremental: son unos cientos
    de entradas incluso con años de uso, y asi no hay que guardar marcas de
    sincronizacion que se puedan desfasar.
    """
    datos = _get(
        sesion,
        "/measurements/list_measurement",
        {"user_id": sesion.user_id, "last_updated_at": 0, "last_measurement_id": 0},
    )
    leidas = [_medida(m) for m in datos.get("measurements") or []]
    return sorted(
        (m for m in leidas if m.get("date")),
        key=lambda m: m.get("at", ""),
        reverse=True,
    )


def dispositivos(sesion: Sesion) -> list[dict]:
    """Basculas dadas de alta en la cuenta. Solo para poder enseñar cual es."""
    datos = _get(sesion, "/device_binds/list_device_bind")
    modelos = {m.get("scale_name"): m for m in datos.get("device_models") or []}
    salida = []
    for d in datos.get("device_binds") or []:
        modelo = modelos.get(d.get("scale_name")) or {}
        marca = (modelo.get("brand_info") or {}).get("brand_name")
        salida.append(
            {
                "model": modelo.get("internal_model") or d.get("scale_name"),
                "brand": marca,
                "mac": d.get("mac"),
            }
        )
    return salida
