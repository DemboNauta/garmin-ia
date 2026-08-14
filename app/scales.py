"""Basculas inteligentes: vinculacion, lectura y progreso.

Garmin ya da el peso, pero solo el que alguien teclea a mano o le manda una
bascula de la propia marca. Una bascula de bioimpedancia mide todos los dias y
añade lo que de verdad dice si el entrenamiento va bien: grasa, musculo y agua.
Sin eso, perder dos kilos y perder dos kilos de musculo son el mismo numero.

Este modulo es la parte que NO depende de la marca: guarda con quien esta
vinculado cada usuario, normaliza los pesajes y saca la tendencia. Hablar con la
nube de cada fabricante es cosa de los modulos de proveedor (`app/feelfit.py`).
Hoy solo hay FeelFit; añadir otra marca es escribir su modulo y una linea en
PROVEEDORES. El contrato que tiene que cumplir es corto:

    NOMBRE, DESCRIPCION   como se llama y que se enseña al elegir marca
    Sesion                dataclass serializable con lo que haga falta guardar
    SesionCaducada        excepcion cuando el permiso guardado deja de valer
    iniciar_sesion(email, password) -> Sesion
    medidas(sesion)       -> pesajes ya en la forma neutra de aqui
    dispositivos(sesion)  -> las basculas de la cuenta, para poder enseñarlas
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
from typing import Any

from . import crypto, feelfit, store, sync

log = logging.getLogger(__name__)

# Las marcas soportadas. La clave es lo que viaja en el formulario y lo que
# queda guardado en la base, asi que no se cambia a la ligera.
PROVEEDORES = {
    "feelfit": feelfit,
}


class ErrorDeBascula(RuntimeError):
    pass


class SinBascula(ErrorDeBascula):
    """El usuario no tiene ninguna bascula vinculada todavia."""


class SesionCaducada(ErrorDeBascula):
    """El permiso guardado ya no vale: hay que volver a pasar por el formulario."""


def catalogo() -> list[dict]:
    """Marcas que se pueden elegir, para pintar el formulario."""
    return [
        {"id": clave, "name": modulo.NOMBRE, "description": modulo.DESCRIPCION}
        for clave, modulo in PROVEEDORES.items()
    ]


def _modulo(proveedor: str):
    modulo = PROVEEDORES.get(proveedor)
    if modulo is None:
        conocidos = ", ".join(PROVEEDORES) or "ninguna"
        raise ErrorDeBascula(f"Marca de bascula desconocida: '{proveedor}'. Hay: {conocidos}.")
    return modulo


# ----------------------------------------------------------------- vinculacion
def vincular(user_id: str, proveedor: str, email: str, password: str) -> dict:
    """Cambia las credenciales por un token y lo guarda cifrado.

    La contraseña de la bascula no se guarda en ningun momento, igual que la de
    Garmin: se usa aqui y se olvida.
    """
    modulo = _modulo(proveedor)
    sesion = modulo.iniciar_sesion(email, password)
    store.guardar_bascula(
        user_id,
        proveedor,
        sesion.cuenta,
        crypto.cifrar(json.dumps(dataclasses.asdict(sesion))),
    )
    log.info("Bascula %s vinculada para %s", proveedor, user_id)

    # Los dispositivos son un adorno: si la llamada falla, la vinculacion sigue
    # siendo buena y no tiene sentido tumbarla por no poder decir el modelo.
    try:
        aparatos = modulo.dispositivos(sesion)
    except Exception as exc:  # noqa: BLE001
        log.info("No se pudieron leer los dispositivos de %s (%s)", proveedor, exc)
        aparatos = []
    return {"provider": proveedor, "name": modulo.NOMBRE, "account": sesion.cuenta,
            "devices": aparatos}


def desvincular(user_id: str) -> None:
    store.borrar_bascula(user_id)


def estado(user_id: str) -> dict | None:
    """Que bascula tiene vinculada, sin tocar la red. None si no tiene."""
    fila = store.leer_bascula(user_id)
    if not fila:
        return None
    modulo = PROVEEDORES.get(fila["provider"])
    return {
        "provider": fila["provider"],
        "name": modulo.NOMBRE if modulo else fila["provider"],
        "account": fila["account"],
        "linked_at": fila["linked_at"][:10],
    }


def _sesion(user_id: str):
    """La sesion guardada del usuario, ya descifrada, con su modulo."""
    fila = store.leer_bascula(user_id)
    if not fila:
        raise SinBascula(
            "No hay ninguna bascula vinculada a esta cuenta. Se vincula desde la "
            "web del servicio, no desde aqui: la contraseña de la bascula no "
            "puede pasar por la conversacion."
        )
    modulo = _modulo(fila["provider"])
    datos = json.loads(crypto.descifrar(fila["session_enc"]))
    return modulo, modulo.Sesion(**datos)


# --------------------------------------------------------------------- lectura
def pesajes(user_id: str, dias: int) -> list[dict]:
    """Pesajes de los ultimos N dias, del mas reciente al mas antiguo.

    No se cachean, al reves que las metricas de Garmin: es una sola llamada, la
    nube del fabricante devuelve el historico entero de golpe y una bascula se
    pisa como mucho una vez al dia. Guardar una copia solo añadiria una politica
    de frescura mas que mantener.
    """
    modulo, sesion = _sesion(user_id)
    desde = (sync.today() - dt.timedelta(days=dias - 1)).isoformat()
    try:
        medidas = modulo.medidas(sesion)
    except modulo.SesionCaducada as exc:
        raise SesionCaducada(
            f"El acceso a {modulo.NOMBRE} ha caducado. Hay que volver a vincular "
            "la bascula desde la web del servicio."
        ) from exc
    return [m for m in medidas if m.get("date", "") >= desde]


def _media(valores: list[float]) -> float | None:
    return round(sum(valores) / len(valores), 2) if valores else None


def _serie(medidas: list[dict], campo: str) -> list[tuple[str, float]]:
    """Los valores de un campo con su fecha, saltandose los pesajes que no lo
    traen: la bioimpedancia falla a veces y deja un pesaje solo con el peso."""
    return [(m["date"], m[campo]) for m in medidas if m.get(campo) is not None]


def _evolucion(medidas: list[dict], campo: str) -> dict:
    """Cuanto se ha movido un campo: total del periodo y semana contra semana.

    El total de punta a punta engaña, porque el peso oscila un kilo largo entre
    la mañana y la noche y entre un dia y otro. Por eso va tambien la media de
    los ultimos siete dias contra la de los siete anteriores, que es la forma
    honesta de ver si la tendencia sube o baja.
    """
    serie = _serie(medidas, campo)
    if len(serie) < 2:
        return {}
    fuera: dict[str, Any] = {
        "change": round(serie[0][1] - serie[-1][1], 2),
        "since": serie[-1][0],
    }
    hoy = dt.date.fromisoformat(serie[0][0])
    if (hoy - dt.date.fromisoformat(serie[-1][0])).days < 7:
        # Menos de una semana de datos: la media de siete dias seria la de todo
        # lo que hay, y llamarla asi haria pensar en una ventana que no existe.
        return fuera
    ultima = [v for f, v in serie if (hoy - dt.date.fromisoformat(f)).days < 7]
    anterior = [v for f, v in serie if 7 <= (hoy - dt.date.fromisoformat(f)).days < 14]
    media_ultima, media_anterior = _media(ultima), _media(anterior)
    if media_ultima is not None:
        fuera["avg_7d"] = media_ultima
    if media_ultima is not None and media_anterior is not None:
        fuera["avg_prev_7d"] = media_anterior
        fuera["change_per_week"] = round(media_ultima - media_anterior, 2)
    return fuera


def resumen(user_id: str, dias: int) -> dict:
    """Lo que necesita el modelo: el ultimo pesaje, la serie y la tendencia."""
    bascula = estado(user_id)
    medidas = pesajes(user_id, dias)
    fuera: dict[str, Any] = {
        "provider": (bascula or {}).get("provider"),
        "days": dias,
        "measurements": medidas,
    }
    if not medidas:
        fuera["note"] = (
            f"La bascula esta vinculada pero no hay pesajes en los ultimos {dias} "
            "dias. Comprueba que la app de la bascula ha sincronizado."
        )
        return fuera

    fuera["latest"] = medidas[0]
    fuera["count"] = len(medidas)
    for campo, nombre in (("weight_kg", "weight"), ("body_fat_pct", "body_fat"),
                          ("muscle_mass_kg", "muscle_mass")):
        evolucion = _evolucion(medidas, campo)
        if evolucion:
            fuera[f"{nombre}_trend"] = evolucion
    return fuera
