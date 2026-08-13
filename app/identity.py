"""Quien esta haciendo la peticion.

Las herramientas MCP no reciben al usuario como parametro —seria absurdo y
falsificable, porque el modelo elegiria a quien consultar—, asi que la identidad
viaja por contexto: la fija el middleware al validar las credenciales y la leen
las herramientas.

Hoy solo hay un emisor de identidad (el bearer del dueño). Cuando entre OAuth,
lo unico que cambia es quien llama a `fijar_usuario`: las herramientas no se
tocan.
"""
from __future__ import annotations

from contextvars import ContextVar

from .config import settings

_usuario: ContextVar[str | None] = ContextVar("usuario_actual", default=None)


class SinIdentidad(RuntimeError):
    """No hay usuario en contexto. Es un fallo de programacion, no del cliente."""


def fijar_usuario(user_id: str):
    """Fija el usuario de esta peticion. Devuelve el testigo para restaurarlo."""
    return _usuario.set(user_id)


def restaurar(testigo) -> None:
    _usuario.reset(testigo)


def usuario_actual() -> str:
    """El usuario de la peticion en curso.

    Falla en vez de caer al dueño por defecto: un fallo silencioso aqui
    significaria servirle a alguien los datos de salud de otro.
    """
    uid = _usuario.get()
    if uid is None:
        raise SinIdentidad(
            "No hay usuario en contexto: la peticion no paso por el middleware "
            "de autenticacion."
        )
    return uid


def dueño() -> str:
    """El dueño de la instalacion, para tareas de fondo sin peticion asociada."""
    return settings.owner_user_id
