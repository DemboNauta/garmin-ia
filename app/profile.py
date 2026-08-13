"""Perfil de entrenamiento: lo que el modelo necesita saber y Garmin no cuenta.

Garmin sabe como duermes y cuanto te recuperas, pero no que tienes en casa para
entrenar, cuantos dias puedes, ni que te reventaste el hombro hace dos años. Sin
eso el modelo propone a ciegas o pregunta lo mismo en cada conversacion.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Literal

from pydantic import BaseModel, Field

from . import store

Reparto = Literal[
    "full_body",              # todo el cuerpo en cada sesion
    "torso_pierna",           # upper/lower
    "empuje_tiron_pierna",    # push/pull/legs
    "por_grupos",             # un grupo muscular por dia
    "sin_preferencia",
]


class Perfil(BaseModel):
    """Preferencias de entrenamiento. Todo es opcional."""

    equipment: list[str] | None = Field(
        default=None,
        description=(
            "Material disponible, en terminos llanos: 'mancuernas', 'banco', "
            "'barra', 'gomas', 'gimnasio completo'. Condiciona que ejercicios "
            "se pueden prescribir."
        ),
    )
    split: Reparto | None = Field(
        default=None, description="Como prefiere repartir el trabajo de fuerza."
    )
    days_per_week: int | None = Field(
        default=None, ge=1, le=7, description="Dias de entrenamiento por semana."
    )
    session_minutes: int | None = Field(
        default=None, ge=10, le=300, description="Duracion habitual de una sesion."
    )
    activities: list[str] | None = Field(
        default=None,
        description=(
            "Actividades habituales fuera de la fuerza, con su frecuencia y "
            "detalle si se sabe: 'paseo nocturno 30-50 min casi a diario', "
            "'bici los sabados'. Sirven como trabajo aerobico o vuelta a la calma."
        ),
    )
    goals: str | None = Field(
        default=None, description="Que busca: fuerza, perder grasa, una carrera, salud general."
    )
    limitations: str | None = Field(
        default=None,
        description="Lesiones, molestias o restricciones que hay que respetar al prescribir.",
    )
    notes: str | None = Field(
        default=None, description="Cualquier otra cosa util que no encaje arriba."
    )


def leer(user_id: str) -> dict:
    with store.conn() as c:
        fila = c.execute(
            "SELECT payload FROM user_profiles WHERE user_id=?", (user_id,)
        ).fetchone()
    return json.loads(fila["payload"]) if fila else {}


def actualizar(user_id: str, cambios: Perfil) -> dict:
    """Mezcla los campos enviados sobre lo que ya hubiera.

    Parcial a proposito, al reves que update_workout: un perfil se va
    completando a lo largo de muchas conversaciones, y si cada llamada
    reemplazara el conjunto, mencionar el material borraria las lesiones.

    Un campo enviado explicitamente como null si borra: es la forma de retirar
    algo que ya no aplica.
    """
    actual = leer(user_id)
    # model_fields_set distingue "no lo ha mencionado" de "lo ha puesto a null".
    for campo in cambios.model_fields_set:
        valor = getattr(cambios, campo)
        if valor is None:
            actual.pop(campo, None)
        else:
            actual[campo] = valor

    with store.conn() as c:
        c.execute(
            "INSERT INTO user_profiles(user_id, payload, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET payload=excluded.payload, "
            "updated_at=excluded.updated_at",
            (
                user_id,
                json.dumps(actual, ensure_ascii=False),
                dt.datetime.now(dt.timezone.utc).isoformat(),
            ),
        )
    return actual
