"""Administracion desde la linea de comandos.

    python -m app.admin invitar [email]     emite una invitacion
    python -m app.admin usuarios            lista las cuentas
    python -m app.admin invitaciones        lista las invitaciones
    python -m app.admin borrar <user_id>    borra una cuenta y todos sus datos

Deliberadamente no hay endpoints HTTP de administracion: emitir invitaciones
requiere acceso al servidor, que es la barrera que mantiene cerrado el servicio.
"""
from __future__ import annotations

import sys

from . import accounts, store
from .config import settings


def _invitar(email: str | None) -> int:
    store.init()
    codigo = accounts.crear_invitacion(email)
    print("Invitacion creada. Mandale este enlace a la persona:\n")
    print(f"  {settings.public_url}/invite/{codigo}\n")
    print("Caduca en 7 dias y solo sirve una vez.")
    print("El codigo no se puede volver a consultar: se guarda hasheado.")
    return 0


def _usuarios() -> int:
    store.init()
    filas = store.listar_usuarios()
    if not filas:
        print("No hay usuarios todavia.")
        return 0
    print(f"{'user_id':22} {'email':32} {'garmin':7} creado")
    for u in filas:
        garmin = "si" if u["garmin_vinculado"] else "NO"
        print(f"{u['user_id']:22} {str(u['email'] or '-'):32} {garmin:7} {u['created_at'][:19]}")
    return 0


def _invitaciones() -> int:
    store.init()
    filas = accounts.listar_invitaciones()
    if not filas:
        print("No hay invitaciones.")
        return 0
    print(f"{'email':32} {'estado':10} caduca")
    for i in filas:
        estado = "usada" if i["used_at"] else "pendiente"
        print(f"{str(i['email'] or '-'):32} {estado:10} {i['expires_at'][:19]}")
    return 0


def _borrar(user_id: str) -> int:
    store.init()
    if not store.existe_usuario(user_id):
        print(f"No existe el usuario '{user_id}'.")
        return 1
    store.borrar_usuario(user_id)
    print(f"Usuario '{user_id}' y todos sus datos borrados.")
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    orden, *resto = argv
    if orden == "invitar":
        return _invitar(resto[0] if resto else None)
    if orden == "usuarios":
        return _usuarios()
    if orden == "invitaciones":
        return _invitaciones()
    if orden == "borrar":
        if not resto:
            print("Falta el user_id.")
            return 1
        return _borrar(resto[0])
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
