"""Correccion de sesiones ya registradas.

El reloj graba lo que puede. Una pulsera sin pantalla no sabe que ejercicio
estas haciendo ni con cuanto peso: guarda la serie como UNKNOWN, sin carga y a
veces con las repeticiones mal contadas. Lo que falta solo lo puede poner
quien entreno, contandolo despues.

Aqui se traduce entre el DTO de exerciseSets de Garmin y una forma que el
modelo pueda leer, corregir y devolver tal cual, igual que se hace con los
entrenamientos en `workouts.py`.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Literal

from pydantic import BaseModel, Field

from . import workouts

# Los dos tipos de serie que distingue Garmin. El descanso tambien es una serie
# y ocupa su sitio en la lista: si se omite, las siguientes se desplazan.
TIPOS_SERIE: dict[str, str] = {"active": "ACTIVE", "rest": "REST"}
_POR_TIPO = {v: k for k, v in TIPOS_SERIE.items()}

# Garmin guarda el peso de las series en gramos, igual que en los pasos de un
# entrenamiento (ver WEIGHT_UNIT_KG en workouts.py). SIN VERIFICAR contra una
# serie real: el reloj no graba carga nunca, asi que no hay ningun ejemplo con
# peso que leer. Si al corregir una serie la app muestra el peso multiplicado o
# dividido por mil, el arreglo es esta constante y nada mas.
GRAMOS_POR_KILO = 1000.0

# Formato de fecha del DTO: ISO con una decima de segundo y en GMT.
_FORMATO = "%Y-%m-%dT%H:%M:%S.%f"


class Serie(BaseModel):
    """Una serie de una sesion de fuerza ya registrada."""

    kind: Literal["active", "rest"] = Field(
        default="active",
        description="'active' es una serie de trabajo; 'rest', el descanso entre series.",
    )
    exercise: str | None = Field(
        default=None,
        description=(
            "Nombre EXACTO del catalogo de Garmin, que esta solo en ingles: "
            "buscalo con find_exercises. De el sale el grupo muscular con el que "
            "se clasifica la sesion, que es lo que el reloj no supo detectar. "
            "No lo pongas en los descansos."
        ),
    )
    reps: int | None = Field(default=None, ge=0, description="Repeticiones de la serie.")
    weight_kg: float | None = Field(
        default=None, ge=0,
        description="Carga en kilos. El reloj nunca la sabe: si no se dice, no hay progresion.",
    )
    seconds: float | None = Field(
        default=None, ge=0,
        description=(
            "Duracion en segundos. Si no se manda, se conserva la que midio el "
            "reloj en esa posicion, que es el dato que si tiene bueno."
        ),
    )


def leer_series(payload: dict) -> list[dict]:
    """DTO de Garmin -> series con la MISMA forma que acepta update_activity_sets.

    Simetrico a proposito: lo que devuelve get_activity se puede corregir y
    reenviar sin traducir nada. `detected` sale solo cuando el reloj registro
    algo que no llega a ejercicio del catalogo, que es justo lo que hay que
    corregir; UNKNOWN significa que no reconocio nada.
    """
    salida: list[dict] = []
    for i, s in enumerate(payload.get("exerciseSets") or []):
        ejercicios = s.get("exercises") or []
        # La lista viene ordenada por probabilidad: la primera es la apuesta
        # del reloj y las demas son alternativas que descarto.
        mejor = ejercicios[0] if ejercicios else {}
        categoria = mejor.get("category")
        nombre = workouts.nombre_visible(categoria, mejor.get("name"))
        peso = s.get("weight")
        serie = {
            "index": i,
            "kind": _POR_TIPO.get(s.get("setType"), "active"),
            "exercise": nombre,
            "detected": categoria if nombre is None and categoria else None,
            "reps": s.get("repetitionCount"),
            "weight_kg": round(peso / GRAMOS_POR_KILO, 2) if peso else None,
            "seconds": round(s["duration"], 1) if s.get("duration") is not None else None,
        }
        salida.append({k: v for k, v in serie.items() if v is not None})
    return salida


def _instante(texto: str | None) -> dt.datetime | None:
    if not texto:
        return None
    try:
        return dt.datetime.strptime(texto, _FORMATO)
    except ValueError:
        return None


def _texto(instante: dt.datetime) -> str:
    return instante.strftime(_FORMATO)[:-5]  # una decima, como lo devuelve Garmin


def a_payload(
    activity_id: int | str,
    series: list[dict],
    originales: list[dict],
    inicio_gmt: str | None,
    con_variante: bool = True,
) -> dict:
    """Series corregidas -> cuerpo del PUT de exerciseSets.

    Los tiempos no los toca el modelo: cada serie hereda el `startTime` de la
    que ocupaba su lugar, y las que se añaden al final se encadenan sumando
    duraciones. El reloj mide bien cuando pasaron las cosas; lo que no sabe es
    que se estaba haciendo.

    Con `con_variante=False` se manda solo la categoria (el grupo muscular) y no
    la variante concreta: es la red de seguridad para los ejercicios cuyo enum
    Garmin no acepta en este endpoint.
    """
    previos = originales or []
    reloj = _instante(inicio_gmt)
    salida: list[dict] = []

    for i, serie in enumerate(series):
        original = previos[i] if i < len(previos) else {}
        duracion = serie.get("seconds")
        if duracion is None:
            duracion = original.get("duration")

        arranque = _instante(original.get("startTime")) or reloj
        if arranque is None and salida:
            arranque = _instante(salida[-1]["startTime"])

        ejercicios: list[dict[str, Any]] = []
        if serie.get("exercise"):
            entrada = workouts.resolver_ejercicio(serie["exercise"])
            ejercicios = [{
                "category": entrada["category"],
                "name": entrada["exercise"] if con_variante else None,
                "probability": 100.0,
            }]

        peso = serie.get("weight_kg")
        salida.append({
            "exercises": ejercicios,
            "setType": TIPOS_SERIE[serie.get("kind", "active")],
            "repetitionCount": serie.get("reps"),
            "weight": float(peso) * GRAMOS_POR_KILO if peso is not None else None,
            "duration": float(duracion) if duracion is not None else None,
            "startTime": _texto(arranque) if arranque else None,
            "messageIndex": i,
            "wktStepIndex": original.get("wktStepIndex"),
        })

        # Reloj para la siguiente: lo que dure esta serie a partir de su inicio.
        if arranque is not None:
            reloj = arranque + dt.timedelta(seconds=float(duracion or 0))

    return {"activityId": int(activity_id), "exerciseSets": salida}


def sin_ejercicio(series: list[dict]) -> int:
    """Series de trabajo que se guardarian sin ejercicio identificado."""
    return sum(1 for s in series if s.get("kind", "active") == "active" and not s.get("exercise"))


def resolver_tipo(clave: str, tipos: list[dict]) -> dict:
    """Clave de tipo de actividad -> su entrada completa (typeId y padre).

    Garmin necesita los tres identificadores para cambiar el tipo, y solo acepta
    claves de su lista; falla con candidatos en vez de inventarse una.
    """
    clave = clave.strip().lower()
    for t in tipos:
        if t.get("typeKey") == clave:
            return t
    parecidos = [t["typeKey"] for t in tipos if clave in (t.get("typeKey") or "")]
    if len(parecidos) == 1:
        return next(t for t in tipos if t["typeKey"] == parecidos[0])
    pista = f" Parecidos: {', '.join(parecidos[:10])}." if parecidos else ""
    raise ValueError(
        f"'{clave}' no es un tipo de actividad de Garmin.{pista} "
        "Las claves habituales son running, walking, strength_training, cycling, "
        "indoor_cycling, hiking, cardio_training, yoga, elliptical, other."
    )


def payload_manual(
    nombre: str,
    tipo: dict,
    inicio_local: str,
    zona: str,
    duracion_min: float,
    distancia_km: float | None = None,
) -> dict:
    """Cuerpo para dar de alta a mano una sesion que el reloj no llego a grabar.

    Nace privada y con las calorias calculadas por Garmin a partir del perfil,
    que es lo unico razonable cuando no hay pulsometro que las midiera.
    """
    resumen: dict[str, Any] = {
        "startTimeLocal": inicio_local,
        "duration": duracion_min * 60,
    }
    if distancia_km is not None:
        resumen["distance"] = distancia_km * 1000
    return {
        "activityName": nombre[:80],
        "activityTypeDTO": {
            "typeId": tipo["typeId"],
            "typeKey": tipo["typeKey"],
            "parentTypeId": tipo.get("parentTypeId"),
        },
        "accessControlRuleDTO": {"typeId": 2, "typeKey": "private"},
        "timeZoneUnitDTO": {"unitKey": zona},
        "metadataDTO": {"autoCalcCalories": True},
        "summaryDTO": resumen,
    }


def normalizar_inicio(texto: str) -> str:
    """Fecha de inicio en el formato que traga el alta manual.

    Acepta lo que suele escribir el modelo ('2026-08-13 21:30', con o sin
    segundos) y devuelve el ISO con milesimas que espera Garmin.
    """
    limpio = texto.strip().replace(" ", "T")
    try:
        momento = dt.datetime.fromisoformat(limpio)
    except ValueError as exc:
        raise ValueError(
            f"'{texto}' no es una fecha valida. Usa 'YYYY-MM-DDTHH:MM:SS' en hora local."
        ) from exc
    return momento.strftime("%Y-%m-%dT%H:%M:%S.000")
