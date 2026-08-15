"""Lectura y correccion de sesiones ya registradas.

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
from zoneinfo import ZoneInfo

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

# Unidades del resumen de una sesion: Garmin lo guarda todo en metros y
# segundos, y quien habla con el modelo cuenta kilometros y minutos.
METROS_POR_KILOMETRO = 1000.0
SEGUNDOS_POR_MINUTO = 60.0


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


# El detalle de una sesion viene como una tabla: `metricDescriptors` dice que
# columna es cada cosa y `activityDetailMetrics` son las filas, una por segundo.
_CLAVE_FC = "directHeartRate"
_CLAVES_TIEMPO = ("sumElapsedDuration", "sumDuration", "sumMovingDuration")


def _columnas(detalle: dict) -> dict[str, int]:
    return {
        d["key"]: d["metricsIndex"]
        for d in (detalle.get("metricDescriptors") or [])
        if d.get("key") is not None and d.get("metricsIndex") is not None
    }


def curva_fc(detalle: dict, zonas: list[dict] | None = None, puntos: int = 40) -> dict:
    """Detalle de una sesion -> curva de pulso legible, con su reparto por zonas.

    Garmin graba a un punto por segundo: una caminata de media hora son casi dos
    mil muestras, que ni caben en una respuesta ni se leen. Se agrupan en tramos
    y de cada uno se da la media y el pico, porque con solo la media un intervalo
    duro de treinta segundos desaparece dentro del tramo.

    El reparto por zonas viene ya calculado por Garmin (`hrTimeInZones`), asi que
    no se recalcula aqui: sus zonas dependen del perfil de cada persona.
    """
    columnas = _columnas(detalle)
    fc = columnas.get(_CLAVE_FC)
    if fc is None:
        return {"points": [], "note": "Esta sesion no tiene pulso grabado."}
    reloj = next((columnas[k] for k in _CLAVES_TIEMPO if k in columnas), None)

    muestras: list[tuple[float, float]] = []
    for i, fila in enumerate(detalle.get("activityDetailMetrics") or []):
        valores = fila.get("metrics") or []
        pulso = valores[fc] if fc < len(valores) else None
        if not pulso:  # sin dato y sin pulso cero: la pulsera se despego
            continue
        segundos = valores[reloj] if reloj is not None and reloj < len(valores) else i
        muestras.append((float(segundos or 0), float(pulso)))

    if not muestras:
        return {"points": [], "note": "Esta sesion no tiene pulso grabado."}

    puntos = max(5, min(int(puntos), 200))
    tamano = max(1, -(-len(muestras) // puntos))  # division hacia arriba
    curva = []
    for inicio in range(0, len(muestras), tamano):
        tramo = muestras[inicio:inicio + tamano]
        pulsos = [p for _, p in tramo]
        curva.append({
            "min": round(tramo[0][0] / 60, 1),
            "bpm": round(sum(pulsos) / len(pulsos)),
            "peak": round(max(pulsos)),
        })

    todos = [p for _, p in muestras]
    salida = {
        "samples": len(muestras),
        "avg_hr": round(sum(todos) / len(todos)),
        "max_hr": round(max(todos)),
        "min_hr": round(min(todos)),
        "points": curva,
    }
    if zonas:
        salida["zones"] = _zonas(zonas)
    return salida


def _zonas(zonas: list[dict]) -> list[dict]:
    """Minutos en cada zona de pulso, con su porcentaje sobre el total."""
    total = sum(z.get("secsInZone") or 0 for z in zonas)
    return [
        {
            "zone": z.get("zoneNumber"),
            "from_bpm": z.get("zoneLowBoundary"),
            "minutes": round((z.get("secsInZone") or 0) / 60, 1),
            "pct": round((z.get("secsInZone") or 0) / total * 100) if total else 0,
        }
        for z in zonas
    ]


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


def a_gmt(inicio_local: str, zona: str) -> str:
    """Hora local de una sesion -> la misma en UTC, que Garmin guarda aparte.

    Las dos tienen que moverse juntas: el listado ordena por una y el detalle
    pinta la otra, asi que cambiar solo la local deja la sesion a dos horas
    distintas segun donde se mire.
    """
    momento = dt.datetime.fromisoformat(inicio_local).replace(tzinfo=ZoneInfo(zona))
    return momento.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000")


def payload_resumen(
    activity_id: int | str,
    resumen_actual: dict,
    zona: str,
    distancia_km: float | None = None,
    duracion_min: float | None = None,
    calorias: float | None = None,
    desnivel_m: float | None = None,
    inicio_local: str | None = None,
) -> dict:
    """Totales corregidos -> cuerpo del PUT de la actividad.

    Solo viaja lo que se pide cambiar: es el mismo endpoint que la libreria usa
    para el nombre o el deporte mandando dos campos, asi que admite cuerpos
    parciales y el resto de la sesion se queda como estaba.

    `resumen_actual` es el summaryDTO tal y como lo tiene Garmin, y hace falta
    para el ritmo: al corregir solo la distancia de un paseo, la velocidad media
    hay que rehacerla con la duracion que ya habia.
    """
    negativos = {
        "distance_km": distancia_km,
        "duration_min": duracion_min,
        "calories": calorias,
    }
    for campo, valor in negativos.items():
        if valor is not None and valor < 0:
            raise ValueError(f"{campo} no puede ser negativo (se recibio {valor}).")

    campos: dict[str, Any] = {}
    if distancia_km is not None:
        campos["distance"] = round(distancia_km * METROS_POR_KILOMETRO, 2)
    if duracion_min is not None:
        segundos = round(duracion_min * SEGUNDOS_POR_MINUTO, 1)
        # Garmin guarda tres duraciones. La de movimiento es la que usa para el
        # ritmo, asi que corregir solo `duration` deja la sesion diciendo un
        # tiempo y un ritmo que no cuadran entre si.
        campos["duration"] = segundos
        campos["elapsedDuration"] = segundos
        campos["movingDuration"] = segundos
    if calorias is not None:
        campos["calories"] = float(calorias)
    if desnivel_m is not None:
        campos["elevationGain"] = float(desnivel_m)
    if inicio_local is not None:
        campos["startTimeLocal"] = inicio_local
        campos["startTimeGMT"] = a_gmt(inicio_local, zona)

    # La velocidad media es un campo guardado, no algo que se derive al leer.
    # SIN VERIFICAR si Garmin la rehace por su cuenta al recibir una distancia
    # nueva; mandarla no estorba, porque es el mismo numero que saldria.
    if distancia_km is not None or duracion_min is not None:
        metros = campos.get("distance", (resumen_actual or {}).get("distance"))
        segundos = campos.get("duration", (resumen_actual or {}).get("duration"))
        if metros and segundos:
            velocidad = round(metros / segundos, 4)
            campos["averageSpeed"] = velocidad
            campos["averageMovingSpeed"] = velocidad

    cuerpo: dict[str, Any] = {"activityId": int(activity_id), "summaryDTO": campos}
    if calorias is not None:
        # Las sesiones dadas de alta a mano nacen con autoCalcCalories, y con el
        # puesto Garmin vuelve a estimarlas con el perfil y se come la cifra.
        cuerpo["metadataDTO"] = {"autoCalcCalories": False}
    return cuerpo


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
        "duration": duracion_min * SEGUNDOS_POR_MINUTO,
    }
    if distancia_km is not None:
        resumen["distance"] = distancia_km * METROS_POR_KILOMETRO
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
