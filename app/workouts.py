"""Constructor de entrenamientos estructurados.

Traduce una descripcion simple (la que genera el modelo) al JSON que espera
workout-service de Garmin Connect. Los ids son los internos de Garmin: si
Garmin los cambia, hay que tocar estas tablas y nada mas.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from garminconnect import exercises as _catalogo
from pydantic import BaseModel, Discriminator, Field, Tag

SPORTS: dict[str, tuple[int, str]] = {
    "running": (1, "running"),
    "cycling": (2, "cycling"),
    "swimming": (4, "swimming"),
    "strength": (5, "strength_training"),
    "cardio": (6, "cardio_training"),
    "walking": (9, "walking"),
    "hiit": (10, "hiit"),
}

STEP_TYPES: dict[str, tuple[int, str]] = {
    "warmup": (1, "warmup"),
    "cooldown": (2, "cooldown"),
    "interval": (3, "interval"),
    "recovery": (4, "recovery"),
    "rest": (5, "rest"),
    "repeat": (6, "repeat"),
    "other": (7, "other"),
}

END_CONDITIONS: dict[str, tuple[int, str]] = {
    "lap_button": (1, "lap.button"),
    "time": (2, "time"),          # segundos
    "distance": (3, "distance"),  # metros
    "iterations": (7, "iterations"),
    "reps": (10, "reps"),         # repeticiones (fuerza)
}

# Garmin guarda el peso en GRAMOS pero etiquetado como kilogramo.
WEIGHT_UNIT_KG: dict[str, Any] = {"unitId": 8, "unitKey": "kilogram", "factor": 1000.0}

TARGETS: dict[str, tuple[int, str]] = {
    "none": (1, "no.target"),
    "pace": (6, "pace.zone"),       # m/s
    "hr_zone": (4, "heart.rate.zone"),
    "power": (2, "power.zone"),
    "cadence": (3, "cadence"),
}

Step = dict[str, Any]


class EjercicioDesconocido(ValueError):
    """El nombre no esta en el catalogo de Garmin."""


def buscar_ejercicios(termino: str) -> list[dict[str, str]]:
    """Ejercicios cuyo nombre contiene `termino`. El catalogo esta en ingles."""
    return _catalogo.find(termino)


def categorias() -> list[str]:
    """Las 47 categorias de Garmin: son los grupos musculares / patrones."""
    return list(_catalogo.CATEGORIES)


# Indice inverso: de los enums que devuelve Garmin al nombre visible.
_POR_ENUM: dict[tuple[str, str], str] = {
    (e["category"], e["exercise"]): e["name"] for e in _catalogo.EXERCISES
}


def nombre_visible(category: str | None, exercise: str | None) -> str | None:
    """(categoria, variante) de Garmin -> nombre del catalogo.

    Lo que se lee de un entrenamiento tiene que poder reenviarse tal cual al
    editarlo, y Garmin devuelve enums (BARBELL_BACK_SQUAT) donde el catalogo
    usa nombres visibles (Barbell Back Squat).
    """
    if not category:
        return None
    return _POR_ENUM.get((category, exercise or ""))


def resolver_ejercicio(nombre: str) -> dict[str, str]:
    """Nombre visible -> entrada del catalogo (categoria + variante).

    Falla en vez de tragarse el nombre: si el paso se manda sin `category`,
    Garmin lo guarda como ejercicio generico y se pierde el grupo muscular,
    que es justo el dato que interesa conservar.
    """
    entrada = _catalogo.resolve(nombre)
    if entrada:
        return entrada
    # Se acepta tambien el enum crudo, porque es lo que sale de get_workout y
    # sin esto el ciclo leer -> editar -> guardar se romperia.
    por_enum = [e for e in _catalogo.EXERCISES if e["exercise"] == nombre]
    if len(por_enum) == 1:
        return por_enum[0]
    candidatos = por_enum or buscar_ejercicios(nombre)
    if len(candidatos) == 1:  # coincidencia parcial inequivoca
        return candidatos[0]
    if candidatos:
        nombres = ", ".join(e["name"] for e in candidatos[:8])
        raise EjercicioDesconocido(
            f"'{nombre}' no es exacto. Candidatos: {nombres}"
        )
    raise EjercicioDesconocido(
        f"'{nombre}' no esta en el catalogo de Garmin (esta en ingles). "
        "Usa la herramienta find_exercises para dar con el nombre exacto."
    )


def step(
    kind: Literal["warmup", "interval", "recovery", "rest", "cooldown", "other"],
    *,
    seconds: int | None = None,
    meters: int | None = None,
    reps: int | None = None,
    exercise: str | None = None,
    weight_kg: float | None = None,
    hr_zone: int | None = None,
    target_low: float | None = None,
    target_high: float | None = None,
    note: str | None = None,
) -> Step:
    """Un paso suelto.

    Usa `seconds`, `meters` o `reps`; si no, termina con boton de vuelta.
    En fuerza, `exercise` es el nombre visible del catalogo (ver
    `buscar_ejercicios`): de el salen la categoria (grupo muscular) y la
    variante que Garmin necesita para no guardarlo como ejercicio suelto.
    """
    if reps is not None:
        cond, value = END_CONDITIONS["reps"], float(reps)
    elif seconds is not None:
        cond, value = END_CONDITIONS["time"], float(seconds)
    elif meters is not None:
        cond, value = END_CONDITIONS["distance"], float(meters)
    else:
        cond, value = END_CONDITIONS["lap_button"], None

    if hr_zone is not None:
        target, zone = TARGETS["hr_zone"], hr_zone
    elif target_low is not None:
        target, zone = TARGETS["pace"], None
    else:
        target, zone = TARGETS["none"], None

    paso: Step = {
        "_kind": kind,
        "type": "ExecutableStepDTO",
        "stepType": {"stepTypeId": STEP_TYPES[kind][0], "stepTypeKey": STEP_TYPES[kind][1]},
        "endCondition": {"conditionTypeId": cond[0], "conditionTypeKey": cond[1]},
        "endConditionValue": value,
        "targetType": {"workoutTargetTypeId": target[0], "workoutTargetTypeKey": target[1]},
        "targetValueOne": target_low,
        "targetValueTwo": target_high,
        "zoneNumber": zone,
        "description": note,
    }

    if exercise is not None:
        entrada = resolver_ejercicio(exercise)
        paso["category"] = entrada["category"]      # el grupo muscular
        paso["exerciseName"] = entrada["exercise"]  # la variante concreta

    if weight_kg is not None:
        paso["weightValue"] = float(weight_kg) * 1000.0  # Garmin lo quiere en gramos
        paso["weightUnit"] = dict(WEIGHT_UNIT_KG)

    return paso


def repeat(times: int, steps: list[Step]) -> Step:
    """Bloque repetido N veces."""
    return {
        "_kind": "repeat",
        "type": "RepeatGroupDTO",
        "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat"},
        "numberOfIterations": times,
        "smartRepeat": False,
        "endCondition": {"conditionTypeId": 7, "conditionTypeKey": "iterations"},
        "endConditionValue": float(times),
        "workoutSteps": steps,
    }


def _number(steps: list[Step], counter: list[int]) -> list[Step]:
    out = []
    for s in steps:
        s = {k: v for k, v in s.items() if k != "_kind"}
        counter[0] += 1
        s["stepOrder"] = counter[0]
        if "workoutSteps" in s:
            s["workoutSteps"] = _number(s["workoutSteps"], counter)
        out.append(s)
    return out


def build(name: str, sport: str, steps: list[Step], description: str | None = None) -> dict:
    sport_id, sport_key = SPORTS[sport]
    sport_dto = {"sportTypeId": sport_id, "sportTypeKey": sport_key}
    return {
        "workoutName": name[:80],
        "description": description,
        "sportType": sport_dto,
        "workoutSegments": [
            {
                "segmentOrder": 1,
                "sportType": sport_dto,
                "workoutSteps": _number(steps, [0]),
            }
        ],
    }


Deporte = Literal["running", "cycling", "swimming", "strength", "cardio", "walking", "hiit"]
TipoPaso = Literal["warmup", "interval", "recovery", "rest", "cooldown", "other"]


class Paso(BaseModel):
    """Un paso suelto del entrenamiento."""

    kind: TipoPaso = Field(
        default="interval",
        description="Papel del paso. Los descansos entre series van como 'rest'.",
    )
    exercise: str | None = Field(
        default=None,
        description=(
            "OBLIGATORIO en ejercicios de fuerza. Nombre EXACTO del catalogo de "
            "Garmin, que esta solo en ingles: busca antes con find_exercises "
            "(p.ej. 'Barbell Bench Press'). De el sale el grupo muscular; sin "
            "esto la sesion queda sin clasificar y describirlo en 'note' no "
            "sirve de nada. No lo pongas en descansos ni en calentamientos."
        ),
    )
    reps: int | None = Field(default=None, ge=1, description="Repeticiones. Usalo en fuerza.")
    seconds: int | None = Field(default=None, ge=1, description="Duracion en segundos.")
    meters: int | None = Field(default=None, ge=1, description="Distancia en metros.")
    weight_kg: float | None = Field(
        default=None, ge=0,
        description="Carga en kilos. Sin esto no hay seguimiento de progresion de peso.",
    )
    hr_zone: int | None = Field(default=None, ge=1, le=5, description="Zona de frecuencia cardiaca.")
    target_low: float | None = Field(default=None, description="Ritmo objetivo minimo, en m/s.")
    target_high: float | None = Field(default=None, description="Ritmo objetivo maximo, en m/s.")
    note: str | None = Field(
        default=None,
        description="Texto libre para matices (RIR, tecnica). NO uses esto para el ejercicio.",
    )


class Bloque(BaseModel):
    """Serie de pasos repetida N veces."""

    repeat: int = Field(ge=1, description="Cuantas vueltas se dan al bloque.")
    steps: list[Elemento] = Field(description="Pasos que se repiten, en orden.")


def _discriminar(valor: Any) -> str:
    """Bloque si trae 'repeat', si no un paso suelto.

    Sin esto la union prueba las dos ramas y un fallo tonto en un paso sale
    sepultado bajo errores de 'Bloque' que no vienen a cuento, que es justo lo
    que despista a un modelo cuando intenta corregirse.
    """
    if isinstance(valor, dict):
        return "bloque" if "repeat" in valor else "paso"
    return "bloque" if isinstance(valor, Bloque) else "paso"


Elemento = Annotated[
    Union[Annotated[Bloque, Tag("bloque")], Annotated[Paso, Tag("paso")]],
    Discriminator(_discriminar),
]


class EntrenoSpec(BaseModel):
    """Entrenamiento estructurado listo para Garmin Connect."""

    name: str = Field(max_length=80, description="Nombre visible en Garmin Connect.")
    sport: Deporte = Field(default="running", description="Deporte del entrenamiento.")
    description: str | None = Field(default=None, description="Descripcion general de la sesion.")
    steps: list[Elemento] = Field(
        description="Pasos en orden. Un elemento con 'repeat' agrupa y repite los suyos.",
    )


Bloque.model_rebuild()
EntrenoSpec.model_rebuild()


def pasos_sin_ejercicio(spec: dict) -> list[str]:
    """Pasos de trabajo que se guardarian sin ejercicio identificado.

    No bloquea (hay sesiones legitimas sin ejercicio concreto, tipo AMRAP), pero
    quien llame se entera de que va a perder el grupo muscular.
    """
    avisos: list[str] = []

    def recorrer(items: list[dict]) -> None:
        for it in items:
            if "repeat" in it:
                recorrer(it.get("steps") or [])
            elif it.get("kind") in ("interval", "other") and not it.get("exercise"):
                avisos.append(it.get("note") or f"paso '{it.get('kind')}' sin ejercicio")

    if spec.get("sport") == "strength":
        recorrer(spec.get("steps") or [])
    return avisos


def from_spec(spec: dict) -> dict:
    """Convierte un dict declarativo (lo que produce el modelo) en payload Garmin.

    Ejemplo:
    {"name": "Z2 60'", "sport": "running", "steps": [
        {"kind": "warmup", "seconds": 600, "hr_zone": 1},
        {"repeat": 4, "steps": [
            {"kind": "interval", "meters": 800, "hr_zone": 4},
            {"kind": "recovery", "seconds": 120, "hr_zone": 2}]},
        {"kind": "cooldown", "seconds": 600, "hr_zone": 1}]}
    """
    def parse(items: list[dict]) -> list[Step]:
        out: list[Step] = []
        for it in items:
            if "repeat" in it:
                out.append(repeat(int(it["repeat"]), parse(it["steps"])))
            else:
                out.append(step(it.pop("kind", "other"), **it))
        return out

    return build(
        spec["name"],
        spec.get("sport", "running"),
        parse(list(spec.get("steps", []))),
        spec.get("description"),
    )
