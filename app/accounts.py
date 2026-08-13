"""Cuentas de usuario: contraseñas e invitaciones.

El servicio es cerrado. No hay registro abierto: alguien tiene que emitir una
invitacion, y cada invitacion sirve una sola vez. Con datos de salud de por
medio, un formulario de alta publico seria una irresponsabilidad.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import secrets
import uuid

from . import store

# Parametros de scrypt. n=2**15 tarda del orden de 100 ms, que es un buen
# equilibrio: imperceptible al iniciar sesion y caro de multiplicar por millones
# si alguien se lleva la base.
#
# maxmem hay que subirlo a mano: estos parametros necesitan 128*n*r = 32 MiB
# exactos, que es justo el tope por defecto de OpenSSL, y revienta por un pelo.
_SCRYPT = {"n": 2**15, "r": 8, "p": 1, "dklen": 32, "maxmem": 68 * 1024 * 1024}
_DIAS_INVITACION = 7


class ErrorDeCuenta(RuntimeError):
    pass


# ------------------------------------------------------------------ contraseñas
def hashear_password(password: str) -> str:
    sal = secrets.token_bytes(16)
    clave = hashlib.scrypt(password.encode(), salt=sal, **_SCRYPT)
    return f"scrypt${base64.b64encode(sal).decode()}${base64.b64encode(clave).decode()}"


def verificar_password(password: str, guardado: str | None) -> bool:
    if not guardado or not guardado.startswith("scrypt$"):
        return False
    try:
        _, sal_b64, clave_b64 = guardado.split("$")
        sal = base64.b64decode(sal_b64)
        esperado = base64.b64decode(clave_b64)
    except Exception:
        return False
    calculado = hashlib.scrypt(password.encode(), salt=sal, **_SCRYPT)
    # compare_digest para no filtrar cuanto coincide por tiempo de respuesta.
    return hmac.compare_digest(calculado, esperado)


def _validar_password(password: str) -> None:
    if len(password) < 12:
        raise ErrorDeCuenta("La contraseña necesita al menos 12 caracteres.")


# ----------------------------------------------------------------- invitaciones
def _hash_codigo(codigo: str) -> str:
    """SHA-256 basta: el codigo lo generamos nosotros con 256 bits de entropia,
    asi que no hay nada que adivinar por fuerza bruta."""
    return hashlib.sha256(codigo.encode()).hexdigest()


def crear_invitacion(email: str | None = None) -> str:
    """Emite una invitacion y devuelve el codigo EN CLARO, la unica vez que se ve."""
    codigo = secrets.token_urlsafe(32)
    ahora = dt.datetime.now(dt.timezone.utc)
    with store.conn() as c:
        c.execute(
            "INSERT INTO invites(code_hash, email, created_at, expires_at) VALUES(?,?,?,?)",
            (
                _hash_codigo(codigo),
                email,
                ahora.isoformat(),
                (ahora + dt.timedelta(days=_DIAS_INVITACION)).isoformat(),
            ),
        )
    return codigo


def invitacion_valida(codigo: str) -> dict | None:
    with store.conn() as c:
        fila = c.execute(
            "SELECT * FROM invites WHERE code_hash=?", (_hash_codigo(codigo),)
        ).fetchone()
    if not fila or fila["used_at"]:
        return None
    if dt.datetime.fromisoformat(fila["expires_at"]) < dt.datetime.now(dt.timezone.utc):
        return None
    return dict(fila)


def listar_invitaciones() -> list[dict]:
    with store.conn() as c:
        filas = c.execute(
            "SELECT email, created_at, expires_at, used_at FROM invites ORDER BY created_at DESC"
        ).fetchall()
    return [dict(f) for f in filas]


# --------------------------------------------------------------------- usuarios
def registrar_con_invitacion(codigo: str, email: str, password: str) -> str:
    """Canjea la invitacion y crea la cuenta. Devuelve el user_id."""
    _validar_password(password)
    email = email.strip().lower()
    if not email or "@" not in email:
        raise ErrorDeCuenta("Email no valido.")
    if buscar_por_email(email):
        raise ErrorDeCuenta("Ya existe una cuenta con ese email.")

    user_id = f"u_{uuid.uuid4().hex[:16]}"
    ahora = dt.datetime.now(dt.timezone.utc).isoformat()
    with store.conn() as c:
        # El UPDATE condicionado a used_at IS NULL evita que dos altas
        # simultaneas gasten la misma invitacion.
        cur = c.execute(
            "UPDATE invites SET used_at=? WHERE code_hash=? AND used_at IS NULL",
            (ahora, _hash_codigo(codigo)),
        )
        if cur.rowcount != 1:
            raise ErrorDeCuenta("Invitacion invalida o ya usada.")
        c.execute(
            "INSERT INTO users(user_id, email, created_at, password_hash) VALUES(?,?,?,?)",
            (user_id, email, ahora, hashear_password(password)),
        )
    return user_id


def buscar_por_email(email: str) -> dict | None:
    with store.conn() as c:
        fila = c.execute(
            "SELECT * FROM users WHERE email=?", (email.strip().lower(),)
        ).fetchone()
    return dict(fila) if fila else None


def autenticar(email: str, password: str) -> str | None:
    """Devuelve el user_id si las credenciales son correctas."""
    usuario = buscar_por_email(email)
    if not usuario:
        # Se hashea igualmente para que un email inexistente tarde lo mismo que
        # uno real y no se pueda enumerar quien tiene cuenta.
        hashear_password(password)
        return None
    if not verificar_password(password, usuario.get("password_hash")):
        return None
    return usuario["user_id"]


def fijar_password(user_id: str, password: str) -> None:
    _validar_password(password)
    with store.conn() as c:
        c.execute(
            "UPDATE users SET password_hash=? WHERE user_id=?",
            (hashear_password(password), user_id),
        )
