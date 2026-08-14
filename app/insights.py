"""Lo que el asistente concluye, guardado.

El resto del proyecto es fontaneria: entrega datos limpios y ejecuta ordenes,
y pensar es cosa del modelo. El problema es que ese pensamiento vive en una
conversacion y se va con ella: al mes siguiente nadie recuerda por que aquella
semana tocaba bajar el volumen, ni si la subida de peso ya se habia visto
venir.

Aqui se queda por escrito. No es un dato de Garmin ni se sincroniza con nada:
es texto que escribe el asistente cuando llega a una conclusion, con los
numeros en los que se apoyo, para que el usuario pueda releerlo en el panel y
para que el propio modelo recupere el hilo en la siguiente conversacion.

Que NO va aqui: preferencias estables de entrenamiento (eso es `profile.py`,
que se lee entero antes de prescribir) ni el diario de lo que se hizo (eso son
las actividades de Garmin). Esto es la lectura, no el hecho.
"""
from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field

from . import store

# Los cuatro tipos existen para que el panel pueda agruparlos y para obligar a
# distinguir una observacion de un plan: mezclados, el feed se vuelve ilegible.
Tipo = Literal["lectura", "plan", "progreso", "aviso"]

_ETIQUETAS: dict[str, str] = {
    "lectura": "Lectura del dia",
    "plan": "Plan",
    "progreso": "Progreso",
    "aviso": "Aviso",
}

# Tope de lo que se guarda por usuario. Un insight es texto corto y se escriben
# unos pocos por semana, pero sin limite una conversacion enganchada podria
# llenar la base; al pasarse se tiran los mas viejos, que es lo que nadie relee.
_MAXIMO_POR_USUARIO = 500


class Insight(BaseModel):
    """Una conclusion del asistente, tal y como se guarda."""

    kind: Tipo = Field(
        description=(
            "'lectura' para como esta hoy y por que; 'plan' para la sesion o la "
            "semana que se propone; 'progreso' para una revision de tendencia "
            "(peso, carga, sueño); 'aviso' para algo que hay que vigilar."
        )
    )
    title: str = Field(
        max_length=120,
        description=(
            "Una linea que se entienda sola, porque es lo unico que se ve en la "
            "tarjeta del panel. Concreta: 'HRV tres dias por debajo de tu media', "
            "no 'Analisis de recuperacion'."
        ),
    )
    body: str = Field(
        max_length=4000,
        description=(
            "El razonamiento, en prosa y dirigido al usuario. Los saltos de "
            "linea se respetan. Explica en que te apoyas: dentro de un mes esto "
            "se lee sin la conversacion que lo genero."
        ),
    )
    metrics: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Los numeros que sostienen la conclusion, para poder comprobarla "
            "despues: {'hrv': 38, 'media_30d': 52, 'sueño_h': 5.8}. Se pintan "
            "como etiquetas junto al texto."
        ),
    )
    period_days: int | None = Field(
        default=None, ge=1, le=365,
        description="Ventana mirada para llegar a esto, si aplica.",
    )


def guardar(user_id: str, insight: Insight) -> dict:
    ahora = dt.datetime.now(dt.timezone.utc).isoformat()
    insight_id = f"i_{uuid.uuid4().hex[:16]}"
    payload = {
        k: v for k, v in
        (("metrics", insight.metrics), ("period_days", insight.period_days))
        if v is not None
    }
    with store.conn() as c:
        c.execute(
            "INSERT INTO insights(insight_id, user_id, created_at, kind, title, body, payload) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                insight_id,
                user_id,
                ahora,
                insight.kind,
                insight.title.strip(),
                insight.body.strip(),
                json.dumps(payload, ensure_ascii=False) if payload else None,
            ),
        )
        # El borrado va por fecha y no por rowid: es el orden con el que se lee.
        c.execute(
            "DELETE FROM insights WHERE user_id=? AND insight_id NOT IN ("
            "  SELECT insight_id FROM insights WHERE user_id=? "
            "  ORDER BY created_at DESC LIMIT ?)",
            (user_id, user_id, _MAXIMO_POR_USUARIO),
        )
    return {"insight_id": insight_id, "created_at": ahora, **insight.model_dump(exclude_none=True)}


def _fila(f) -> dict:
    payload = json.loads(f["payload"]) if f["payload"] else {}
    return {
        "insight_id": f["insight_id"],
        "created_at": f["created_at"],
        "kind": f["kind"],
        "label": _ETIQUETAS.get(f["kind"], f["kind"]),
        "title": f["title"],
        "body": f["body"],
        **payload,
    }


def listar(user_id: str, limite: int = 20, kind: str | None = None) -> list[dict]:
    """Los mas recientes primero, que es como se leen."""
    consulta = "SELECT * FROM insights WHERE user_id=?"
    parametros: list[Any] = [user_id]
    if kind:
        consulta += " AND kind=?"
        parametros.append(kind)
    consulta += " ORDER BY created_at DESC LIMIT ?"
    parametros.append(max(1, min(limite, 100)))
    with store.conn() as c:
        return [_fila(f) for f in c.execute(consulta, parametros).fetchall()]


def borrar(user_id: str, insight_id: str) -> bool:
    """Devuelve si habia algo que borrar. El user_id va en el WHERE aunque el id
    sea unico: sin el, un id filtrado bastaria para borrarle notas a otro."""
    with store.conn() as c:
        cur = c.execute(
            "DELETE FROM insights WHERE user_id=? AND insight_id=?", (user_id, insight_id)
        )
    return cur.rowcount > 0
