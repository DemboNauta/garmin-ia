"""Peso corporal: los pesajes de Garmin y el alta de uno nuevo.

Garmin lo guarda todo en gramos (89500.0 son 89,5 kg), asi que aqui se pasa a
kilos una sola vez y el resto del backend ya no se entera.

Casi nadie tiene bascula que sincronice: lo normal es apuntarlo a mano cada
pocos dias, asi que la serie es corta y con huecos. Por eso el peso no va en la
foto diaria, donde saldria nulo casi siempre, sino en su propia herramienta.
"""
from __future__ import annotations

import datetime as dt
from typing import Any
from zoneinfo import ZoneInfo

from .config import settings

GRAMOS_POR_KILO = 1000.0

# Hora a la que se apunta un pesaje del que solo se sabe el dia. La bascula se
# pisa al levantarse, antes de desayunar, que es cuando el dato es comparable.
HORA_POR_DEFECTO = dt.time(8, 0)


def _kilos(gramos: Any) -> float | None:
    return round(gramos / GRAMOS_POR_KILO, 2) if isinstance(gramos, (int, float)) else None


def leer_pesajes(datos: dict) -> list[dict]:
    """Respuesta de Garmin -> pesajes en kilos, del mas reciente al mas antiguo."""
    salida = []
    for p in (datos or {}).get("dateWeightList") or []:
        entrada = {
            "date": p.get("calendarDate"),
            "weight_kg": _kilos(p.get("weight")),
            "body_fat_pct": p.get("bodyFat"),
            "muscle_mass_kg": _kilos(p.get("muscleMass")),
            "bmi": round(p["bmi"], 1) if p.get("bmi") else None,
        }
        salida.append({k: v for k, v in entrada.items() if v is not None})
    return sorted(salida, key=lambda e: e.get("date") or "", reverse=True)


def resumen(datos: dict, dias: int) -> dict:
    """Los pesajes del periodo, con lo unico que hay que mirar: el ultimo y
    cuanto se ha movido desde el primero."""
    pesajes = leer_pesajes(datos)
    fuera: dict[str, Any] = {"days": dias, "entries": pesajes}
    if not pesajes:
        fuera["note"] = (
            f"Sin pesajes en los ultimos {dias} dias. Se apuntan con log_weight, "
            "o desde la app de Garmin."
        )
        return fuera

    fuera["latest"] = pesajes[0]
    media = _kilos(((datos or {}).get("totalAverage") or {}).get("weight"))
    if media is not None:
        fuera["average_kg"] = media
    if len(pesajes) > 1:
        primero, ultimo = pesajes[-1].get("weight_kg"), pesajes[0].get("weight_kg")
        if primero is not None and ultimo is not None:
            fuera["change_kg"] = round(ultimo - primero, 2)
            fuera["since"] = pesajes[-1].get("date")
    return fuera


def momento(cuando: str | None) -> str:
    """Cuando se hizo el pesaje, en hora local y con su desfase explicito.

    El desfase no es un detalle: el servidor va en UTC, y sin el la libreria
    tomaria su hora para calcular el GMT y un pesaje de primera hora podria
    acabar contado en el dia anterior.
    """
    zona = ZoneInfo(settings.timezone)
    if not cuando:
        return dt.datetime.now(zona).isoformat()
    texto = cuando.strip().replace(" ", "T")
    try:
        instante = dt.datetime.fromisoformat(texto)
    except ValueError as exc:
        raise ValueError(
            f"'{cuando}' no es una fecha valida. Usa 'YYYY-MM-DD' o "
            "'YYYY-MM-DDTHH:MM' en hora local."
        ) from exc
    if len(texto) == 10:  # solo la fecha: se asume el pesaje de la mañana
        instante = dt.datetime.combine(instante.date(), HORA_POR_DEFECTO)
    if instante.tzinfo is None:
        instante = instante.replace(tzinfo=zona)
    return instante.isoformat()
