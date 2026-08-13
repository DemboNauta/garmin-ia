"""Login interactivo. Se ejecuta UNA vez en el VPS:

    docker compose run --rm api python -m app.login

Guarda los tokens OAuth en el volumen; a partir de ahi el servicio arranca solo.
Los tokens de refresco de Garmin duran alrededor de un año.
"""
from __future__ import annotations

import getpass
import sys

from garminconnect import Garmin
from garminconnect.client import token_file_path

from .config import settings


def main() -> int:
    email = settings.garmin_email or input("Email de Garmin Connect: ").strip()
    password = settings.garmin_password or getpass.getpass("Contraseña: ")

    # prompt_mfa deja que la libreria pida el codigo en mitad del login: mas
    # simple y mas robusto que el par return_on_mfa/resume_login, porque cada
    # estrategia de la cadena resuelve su propio MFA.
    api = Garmin(email, password, prompt_mfa=lambda: input("Codigo MFA: ").strip())

    # login(tokenstore) reutiliza tokens si los hay y los vuelca al terminar.
    api.login(settings.tokenstore)

    # El volcado interno va envuelto en un suppress(Exception), asi que
    # comprobamos a mano que el fichero existe de verdad.
    token_file = token_file_path(settings.tokenstore)
    if not token_file.exists():
        api.client.dump(settings.tokenstore)
    print(f"Tokens guardados en {token_file}")
    print("Dispositivos detectados:")
    for d in api.get_devices():
        print(f"  - {d.get('productDisplayName')} (ultima sync: {d.get('lastSyncTime')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
