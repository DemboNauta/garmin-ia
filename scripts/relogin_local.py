r"""Rehace el login de Garmin DESDE ESTA MAQUINA, no desde el VPS.

Garmin bloquea por IP el flujo de login de los datacenter: la via movil
devuelve 429 y el portal un 403 de Cloudflare. Desde una IP domestica normal si
pasa. Ojo a la asimetria, porque es lo que hace que esto funcione: lo bloqueado
es solo el login inicial. Las llamadas con un token ya valido salen del VPS sin
problema, asi que basta con obtener el permiso aqui y llevarlo alli.

Ejecutalo desde el prompt de Claude Code con `!`, para que pueda pedirte el
codigo del MFA por teclado:

    ! .venv\Scripts\python.exe scripts\relogin_local.py

Deja los tokens EN CLARO en `tokens_nuevos.json` (ignorado por git). Para
llevarlos al servidor:

    scp tokens_nuevos.json root@<vps>:/root/garmin-ia/

y alli, recifrarlos con la clave de aquel lado. No vale copiar el blob cifrado
de la base local: `GB_ENCRYPTION_KEY` es distinta en cada entorno.

    from app import crypto, store
    store.guardar_tokens_garmin("edgar", crypto.cifrar(open("tokens_nuevos.json").read()))

Despues, borra el fichero en claro en los dos lados y recupera los dias que se
perdieran con `POST /sync?days=N` contra el propio servicio, no con un proceso
aparte, que serian dos escritores sobre la misma sqlite.

Antes de nada, si el servidor lleva tiempo fallando, apaga `GB_SYNC_ENABLED` en
su `.env`: el sync reintenta el login en cada ciclo sin backoff, y son esos
reintentos los que convierten un token caducado en una IP con rate limit.
"""
from __future__ import annotations

import io
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from garminconnect import Garmin

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SALIDA = os.path.join(RAIZ, "tokens_nuevos.json")


def _del_env(clave: str) -> str:
    texto = io.open(os.path.join(RAIZ, ".env"), encoding="utf-8").read()
    m = re.search(rf"^{clave}=(.*)$", texto, re.M)
    return m.group(1).strip().strip('"').strip("'") if m else ""


def main() -> int:
    email = _del_env("GB_GARMIN_EMAIL")
    password = _del_env("GB_GARMIN_PASSWORD")
    if not email or not password:
        print("Faltan GB_GARMIN_EMAIL / GB_GARMIN_PASSWORD en .env")
        return 1

    print(f"Entrando en Garmin como {email} desde esta IP...")
    api = Garmin(email, password, return_on_mfa=True)
    try:
        resultado = api.login()
    except Exception as exc:
        print(f"\nGarmin rechazo el acceso: {exc}")
        print("Si es un 429 o un 403, esta IP tambien esta limitada: espera y reintenta.")
        return 2

    if isinstance(resultado, tuple) and resultado and resultado[0] == "needs_mfa":
        codigo = input("Codigo MFA que te ha mandado Garmin: ").strip()
        try:
            api.resume_login(resultado[1], codigo)
        except Exception as exc:
            print(f"\nCodigo rechazado: {exc}")
            return 3

    io.open(SALIDA, "w", encoding="utf-8").write(api.client.dumps())
    print(f"\nLogin correcto. Tokens escritos en {os.path.normpath(SALIDA)}")
    print("Subelos al VPS y recifralos alli; luego borra el fichero en claro.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
