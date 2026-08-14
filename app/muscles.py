"""Del historial de fuerza al mapa muscular del panel.

Garmin clasifica cada serie por categoria de ejercicio (BENCH_PRESS, CURL,
SQUAT...), no por musculo. Para pintar un cuerpo con lo entrenado hace falta
traducir: cada categoria reparte sus series entre las regiones que trabaja,
con peso 1.0 el musculo principal y menos los secundarios, que es la
convencion habitual de las apps de fuerza.

Todo sale de `summarizedExerciseSets`, que ya viene en las actividades
cacheadas: el mapa no cuesta ni una llamada extra a Garmin.

Las series UNKNOWN no se reparten a ningun musculo, pero SI se cuentan y se
devuelven aparte: son sesiones que el reloj no supo clasificar y que el
asistente puede arreglar con update_activity_sets. Ocultarlas haria parecer
que no se entreno lo que en realidad esta sin etiquetar.
"""
from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict

from . import store, sync

# Las regiones que pinta el panel, con su etiqueta y en que cara del cuerpo
# van. "ambas" = hombros y antebrazos, que se ven por delante y por detras.
REGIONES: dict[str, dict] = {
    "hombros":    {"label": "Hombros",     "cara": "ambas"},
    "pecho":      {"label": "Pecho",       "cara": "frente"},
    "biceps":     {"label": "Bíceps",      "cara": "frente"},
    "triceps":    {"label": "Tríceps",     "cara": "espalda"},
    "antebrazos": {"label": "Antebrazos",  "cara": "ambas"},
    "abdomen":    {"label": "Abdomen",     "cara": "frente"},
    "oblicuos":   {"label": "Oblicuos",    "cara": "frente"},
    "trapecios":  {"label": "Trapecios",   "cara": "espalda"},
    "espalda":    {"label": "Espalda",     "cara": "espalda"},
    "lumbar":     {"label": "Lumbar",      "cara": "espalda"},
    "gluteos":    {"label": "Glúteos",     "cara": "espalda"},
    "cuadriceps": {"label": "Cuádriceps",  "cara": "frente"},
    "isquios":    {"label": "Isquios",     "cara": "espalda"},
    "gemelos":    {"label": "Gemelos",     "cara": "espalda"},
}

# Categoria de Garmin -> (region, peso). Peso 1.0 para el musculo que da
# nombre al ejercicio y fracciones para los que ayudan: una serie de press de
# banca cuenta entera como pecho y media como triceps.
CATEGORIA_A_REGIONES: dict[str, list[tuple[str, float]]] = {
    "BENCH_PRESS":        [("pecho", 1.0), ("triceps", 0.5), ("hombros", 0.3)],
    "FLYE":               [("pecho", 1.0)],
    "PUSH_UP":            [("pecho", 1.0), ("triceps", 0.5)],
    "SHOULDER_PRESS":     [("hombros", 1.0), ("triceps", 0.5)],
    "LATERAL_RAISE":      [("hombros", 1.0)],
    "SHOULDER_STABILITY": [("hombros", 1.0)],
    "SHRUG":              [("trapecios", 1.0)],
    "CURL":               [("biceps", 1.0), ("antebrazos", 0.3)],
    "TRICEPS_EXTENSION":  [("triceps", 1.0)],
    "ROW":                [("espalda", 1.0), ("biceps", 0.5)],
    "PULL_UP":            [("espalda", 1.0), ("biceps", 0.5)],
    "DEADLIFT":           [("lumbar", 1.0), ("isquios", 0.7), ("gluteos", 0.7)],
    "HYPEREXTENSION":     [("lumbar", 1.0), ("gluteos", 0.3)],
    "SQUAT":              [("cuadriceps", 1.0), ("gluteos", 0.7)],
    "LUNGE":              [("cuadriceps", 1.0), ("gluteos", 0.7)],
    "LEG_CURL":           [("isquios", 1.0)],
    "LEG_RAISE":          [("abdomen", 1.0)],
    "CALF_RAISE":         [("gemelos", 1.0)],
    "HIP_RAISE":          [("gluteos", 1.0), ("isquios", 0.3)],
    "HIP_STABILITY":      [("gluteos", 0.7)],
    "HIP_SWING":          [("gluteos", 0.7)],
    "SIT_UP":             [("abdomen", 1.0)],
    "CRUNCH":             [("abdomen", 1.0)],
    "PLANK":              [("abdomen", 1.0), ("oblicuos", 0.5)],
    "CORE":               [("abdomen", 1.0), ("oblicuos", 0.5)],
    "CHOP":               [("oblicuos", 1.0)],
    "CARRY":              [("antebrazos", 1.0), ("trapecios", 0.5)],
}


def resumen(user_id: str, start: dt.date, end: dt.date) -> dict:
    """Series por region en el rango, a partir de la cache de actividades.

    Devuelve tambien las series UNKNOWN y las de categorias sin mapear, para
    que el panel pueda decir "hay N series sin identificar" en vez de fingir
    que ese trabajo no existio.
    """
    sync.asegurar_actividades(user_id, start, end)
    por_region: dict[str, float] = defaultdict(float)
    por_categoria: dict[str, int] = defaultdict(int)
    desconocidas = 0
    sesiones = 0

    for actividad in store.get_activities(user_id, start, end):
        bloques = actividad.get("summarizedExerciseSets") or []
        if not bloques:
            continue
        sesiones += 1
        for bloque in bloques:
            series = bloque.get("sets") or 0
            if not series:
                continue
            categoria = bloque.get("category") or "UNKNOWN"
            if categoria == "UNKNOWN":
                desconocidas += series
                continue
            por_categoria[categoria] += series
            for region, peso in CATEGORIA_A_REGIONES.get(categoria, []):
                por_region[region] += series * peso

    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "sessions": sesiones,
        "unknown_sets": desconocidas,
        "muscles": {r: round(v, 1) for r, v in por_region.items()},
        "regions": {r: info["label"] for r, info in REGIONES.items()},
        "categories": dict(sorted(por_categoria.items(), key=lambda kv: -kv[1])),
    }
