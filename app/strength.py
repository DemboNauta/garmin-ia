"""Historial de cargas: como ha ido subiendo el peso en cada ejercicio.

Sale entero de `summarizedExerciseSets`, que Garmin ya manda dentro de cada
actividad del listado y que por tanto esta en la cache: el historial no cuesta
ni una llamada mas, igual que el mapa muscular de `muscles.py`.

Cada bloque de ese resumen trae, por ejercicio y sesion, las series, las
repeticiones, el peso maximo y el volumen total, en gramos y milisegundos.

Se agrupa por CATEGORIA y no por variante a proposito. La variante la adivina el
reloj mirando el movimiento, asi que las mismas mancuernas salen un dia como
'Dumbbell Bench Press' y otro como 'Dumbbell Incline Bench Press'; agrupando por
variante, la progresion se parte en trozos de una sesion cada uno. La variante
que detecto se devuelve igualmente en cada fila, que para eso es informacion.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from . import store, sync, workouts
from .activities import GRAMOS_POR_KILO

MILISEGUNDOS_POR_SEGUNDO = 1000.0


def _kilos(gramos: Any) -> float | None:
    """Gramos de Garmin -> kilos. El 0 es 'sin carga', no un peso de cero."""
    if not gramos:
        return None
    return round(gramos / GRAMOS_POR_KILO, 1)


def resolver_categoria(termino: str) -> tuple[str, str | None]:
    """Lo que pida el modelo -> (categoria, variante) de Garmin.

    Acepta las tres formas con las que se puede referir a un ejercicio: la
    categoria cruda que sale de este mismo historial ('BENCH_PRESS'), el nombre
    visible del catalogo ('Barbell Bench Press') y el enum de la variante. Si no
    es ninguna, `resolver_ejercicio` ya falla con los candidatos parecidos.
    """
    clave = termino.strip().upper().replace(" ", "_")
    if clave in workouts.categorias():
        return clave, None
    entrada = workouts.resolver_ejercicio(termino)
    return entrada["category"], entrada.get("exercise")


def _bloques(actividad: dict) -> list[dict]:
    return actividad.get("summarizedExerciseSets") or []


def _fila(actividad: dict, bloque: dict) -> dict:
    series = bloque.get("sets") or 0
    reps = bloque.get("reps") or 0
    fila = {
        "date": (actividad.get("startTimeLocal") or "")[:10],
        "activity_id": actividad.get("activityId"),
        "variant": workouts.nombre_visible(bloque.get("category"), bloque.get("subCategory"))
        or bloque.get("subCategory"),
        "sets": series,
        "reps": reps,
        "reps_per_set": round(reps / series, 1) if series else None,
        "max_weight_kg": _kilos(bloque.get("maxWeight")),
        # El volumen es la suma de peso por repeticion: sube tanto añadiendo
        # kilos como haciendo mas series, y es lo que distingue una sesion floja
        # de una buena cuando el peso maximo es el mismo.
        "volume_kg": _kilos(bloque.get("volume")),
        "minutes": round((bloque.get("duration") or 0) / MILISEGUNDOS_POR_SEGUNDO / 60, 1) or None,
    }
    return {k: v for k, v in fila.items() if v is not None}


def historial(user_id: str, termino: str, start: dt.date, end: dt.date) -> dict:
    """Sesiones en las que aparece un ejercicio, de la mas reciente a la mas vieja."""
    categoria, variante = resolver_categoria(termino)
    sync.asegurar_actividades(user_id, start, end)

    sesiones = [
        _fila(actividad, bloque)
        for actividad in store.get_activities(user_id, start, end)
        for bloque in _bloques(actividad)
        if bloque.get("category") == categoria and (bloque.get("sets") or bloque.get("reps"))
    ]
    sesiones.sort(key=lambda s: (s.get("date") or "", s.get("activity_id") or 0), reverse=True)

    con_peso = [s for s in sesiones if s.get("max_weight_kg")]
    mejor = max(con_peso, key=lambda s: s["max_weight_kg"], default=None)
    salida = {
        "exercise": workouts.nombre_visible(categoria, variante) or categoria,
        "category": categoria,
        "from": start.isoformat(),
        "to": end.isoformat(),
        # Sesiones, no filas: una misma sesion puede traer dos bloques de la
        # categoria si el reloj creyo ver dos variantes distintas.
        "sessions": len({s.get("activity_id") for s in sesiones}),
        "history": sesiones,
    }
    if mejor:
        salida["best_weight_kg"] = mejor["max_weight_kg"]
        salida["best_weight_date"] = mejor["date"]
        salida["last_weight_kg"] = con_peso[0]["max_weight_kg"]
    return salida


def catalogo_entrenado(user_id: str, start: dt.date, end: dt.date) -> dict:
    """Que ejercicios se han hecho en el periodo, para saber por cual preguntar."""
    sync.asegurar_actividades(user_id, start, end)
    por_categoria: dict[str, dict] = {}

    for actividad in store.get_activities(user_id, start, end):
        dia = (actividad.get("startTimeLocal") or "")[:10]
        for bloque in _bloques(actividad):
            categoria = bloque.get("category")
            if not categoria or categoria == "UNKNOWN" or not (bloque.get("sets") or 0):
                continue
            entrada = por_categoria.setdefault(
                categoria,
                {"category": categoria, "sets": 0, "last": "", "max_weight_kg": None, "_ids": set()},
            )
            entrada["_ids"].add(actividad.get("activityId"))
            entrada["sets"] += bloque.get("sets") or 0
            entrada["last"] = max(entrada["last"], dia)
            peso = _kilos(bloque.get("maxWeight"))
            if peso and peso > (entrada["max_weight_kg"] or 0):
                entrada["max_weight_kg"] = peso

    catalogo = [
        {**{k: v for k, v in e.items() if k != "_ids"}, "sessions": len(e["_ids"])}
        for e in por_categoria.values()
    ]
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "exercises": sorted(catalogo, key=lambda e: (e["sessions"], e["sets"]), reverse=True),
    }
