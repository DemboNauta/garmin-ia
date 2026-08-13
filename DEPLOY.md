# Despliegue en VPS

Cómo se pone Garmin Bridge en producción **tal y como está montado el servidor
real**: Python en un venv, un servicio de systemd y Caddy delante haciendo de
proxy con TLS. Sin Docker.

> Los datos concretos de la máquina (IP, clave SSH, qué más corre ahí, qué puerto
> le toca a esta app) están en `CLAUDE.local.md`, que no se versiona. Léelo antes
> de tocar nada. Aquí solo está el procedimiento.

El repo también trae `Dockerfile` y `docker-compose.yml`, que funcionan en una
máquina dedicada, pero **no es así como está desplegado**. Ver el último
apartado.

## Antes de empezar

En el servidor: Ubuntu 24.04, Caddy ya instalado y sirviendo otros dominios,
`ufw` activo. Se entra como `root` con clave — es el único usuario con shell, así
que **no pongas `PermitRootLogin no`**: te quedarías fuera y solo se recupera por
la consola del proveedor. El puerto de la app no se abre en `ufw` nunca: se llega
a él por el proxy, desde dentro de la máquina.

En local: Windows con OpenSSH. El código se sube por `scp`; **en el servidor no
hay git y ningún proyecto tiene `.git`**, así que no intentes `git pull` allí.

Elige antes:

- **Un subdominio** con registro A al VPS (DNS en Dondominio). Si el dominio va
  por Cloudflare en modo Flexible, el bloque de Caddy necesita el prefijo
  `http://`; si el A es directo, no.
- **Un puerto libre** de loopback. Comprueba con `ss -tlnH` cuáles están cogidos.
  Los scripts de `scripts/` asumen **8003**; si eliges otro, cámbialo en los tres.

En el resto del documento: `PUERTO` es el que hayas elegido y `TU_DOMINIO` el
subdominio.

## 1. Subir el código

La primera vez, a mano — `scripts/deploy.ps1` sirve para actualizar, pero da por
hecho que el venv ya existe:

```powershell
$VPS  = "root@TU_IP"
$KEY  = "$env:USERPROFILE\.ssh\id_ed25519"
$ARGS = @("-i", $KEY)

ssh @ARGS $VPS "mkdir -p /root/garmin-ia/{app,logs,data,scripts}"

robocopy .\app "$env:TEMP\gb-app" /E /XD __pycache__ /XF "*.pyc" | Out-Null
scp @ARGS -r -q "$env:TEMP\gb-app\*" "${VPS}:/root/garmin-ia/app/"
scp @ARGS -q .\requirements.txt "${VPS}:/root/garmin-ia/"
scp @ARGS -q .\scripts\garmin-api.service .\scripts\setup-caddy.sh "${VPS}:/root/garmin-ia/scripts/"
Remove-Item "$env:TEMP\gb-app" -Recurse -Force
```

Nunca subas `.venv/`, `data/` ni `.env` en este paso: los dos últimos viven solo
en el servidor y una actualización no debe pisarlos.

## 2. venv y dependencias

```bash
cd /root/garmin-ia
apt-get install -y python3-venv python3-dev build-essential
python3 -m venv venv
venv/bin/pip install --upgrade pip
venv/bin/pip install -r requirements.txt
```

## 3. Configuración

```bash
cd /root/garmin-ia
openssl rand -hex 32     # el token de la API
nano .env
chmod 600 .env
```

Contenido mínimo:

```ini
GB_API_TOKEN=<los 64 hex del comando anterior>

# OBLIGATORIO fuera de Docker: por defecto config.py apunta a /data/, que es la
# raiz del sistema de ficheros. Sin estas dos lineas los tokens y la BD acaban
# en /data/ del host.
GB_TOKENSTORE=/root/garmin-ia/data/garmin_tokens
GB_DB_PATH=/root/garmin-ia/data/garmin.db

GB_TIMEZONE=Europe/Madrid
GB_SYNC_ENABLED=true
GB_SYNC_INTERVAL_MINUTES=60
GB_SYNC_BACKFILL_DAYS=7
```

`GB_GARMIN_EMAIL` y `GB_GARMIN_PASSWORD` se dejan **fuera**: el login del paso
siguiente los pide por teclado y así la contraseña de Garmin no queda escrita en
disco. Solo rellénalos si necesitas que el servicio pueda re-loguearse solo
dentro de un año, cuando caduquen los tokens.

El token se valida al arrancar: si sigue siendo `cambiame` o mide menos de 32
caracteres, el servicio **no arranca** (`_check_token_config` en `app/main.py`).

## 4. Primer login (una sola vez)

Necesita terminal interactiva para la contraseña y el MFA, así que `ssh -t`:

```bash
ssh -t root@TU_IP 'cd /root/garmin-ia && venv/bin/python -m app.login'
```

Pide email, contraseña y, si la cuenta lo tiene activado, el código MFA. Guarda
los tokens OAuth en `GB_TOKENSTORE`, que la librería escribe con permisos 0600.

Si sale `429: IP rate limited by Garmin`, **no lo reintentes en bucle**: el
bloqueo es por IP y machacarlo lo alarga. Espera y vuelve a probar. La librería
encadena cinco estrategias, así que es normal ver fallar las primeras y que entre
por una posterior.

## 5. Servicio systemd

La unit está en `scripts/garmin-api.service`:

```bash
cd /root/garmin-ia
cp scripts/garmin-api.service /etc/systemd/system/
```

**`--workers 1` no es negociable, aunque otros servicios del VPS usen 2.** Dos
motivos, los dos en `app/main.py`:

- El `lifespan` arranca un `BackgroundScheduler` de APScheduler. Con N workers
  hay N schedulers sincronizando contra Garmin en paralelo — la vía rápida a que
  te bloqueen la cuenta.
- El `session_manager` de MCP guarda las sesiones en memoria del proceso. Con
  varios workers las peticiones caen en procesos distintos y las sesiones se
  rompen.

No lleva `EnvironmentFile`: `pydantic-settings` ya lee el `.env` del
`WorkingDirectory`. Ponerlo además haría que systemd reinterprete las comillas
por su cuenta.

```bash
systemctl daemon-reload
systemctl enable --now garmin-api
systemctl status garmin-api
```

## 6. Caddy

⚠️ **`/etc/caddy/Caddyfile` es único y lo comparten todos los sitios del
servidor. Nunca lo sobrescribas** — copiar encima el `Caddyfile` de este repo
tumbaría el resto de dominios.

`scripts/setup-caddy.sh` hace lo correcto: copia de seguridad, borra solo su
propio bloque entre marcadores `# >>> GARMIN BEGIN/END <<<`, lo vuelve a añadir,
**valida** y recarga.

```bash
bash /root/garmin-ia/scripts/setup-caddy.sh TU_DOMINIO PUERTO
```

Si `caddy validate` falla, el script para ahí y no recarga: arregla antes de
insistir. Es idempotente, se puede repetir para cambiar dominio o puerto.

El `Caddyfile` de la raíz del repo es la plantilla de ese bloque, como
referencia. No es un fichero para instalar tal cual.

Caddy pide y renueva el certificado solo, siempre que el dominio ya apunte al
VPS. Si va detrás de Cloudflare en modo Flexible, pasa el dominio **con el
prefijo** `http://TU_DOMINIO` o Caddy intentará sacar un certificado que sobra.

## 7. Comprobar que está bien cerrado

```bash
curl -s localhost:PUERTO/health          # {"status":"ok",...}
```

```bash
curl https://TU_DOMINIO/health                                        # 200, publico
curl -o /dev/null -w '%{http_code}\n' https://TU_DOMINIO/metrics      # 401
curl -o /dev/null -w '%{http_code}\n' -X POST https://TU_DOMINIO/mcp/ # 401
```

Los dos últimos **tienen que dar 401**. Si el de `/mcp/` responde otra cosa, el
`BearerAuthMiddleware` no está activo: para y revísalo antes de seguir.

Desde fuera del VPS el puerto interno debe estar mudo — `ufw` no lo abre y
uvicorn escucha solo en loopback:

```bash
nc -zv TU_IP PUERTO    # connection refused / timeout
```

Y que los datos fluyan de verdad, no solo que el servicio responda:

```bash
curl -X POST 'https://TU_DOMINIO/sync?days=2' -H "Authorization: Bearer TU_TOKEN"
```

Tiene que devolver `"errors": []` y un `synced_days` mayor que cero. Si trae
errores de sesión, el login del paso 4 no cuajó: repítelo. El servicio ya no
cachea días vacíos ni los cuenta como sincronizados, así que un `synced_days: 0`
significa exactamente eso y no se queda pegado en la base de datos.

## 8. Conectar el cliente MCP

```bash
claude mcp add --transport http garmin https://TU_DOMINIO/mcp/ \
  --header "Authorization: Bearer TU_TOKEN"
```

## Actualizar

```powershell
$env:GARMIN_VPS = "root@TU_IP"
.\scripts\deploy.ps1
```

Sube `app/` y `requirements.txt`, instala dependencias, reinicia el servicio y
comprueba el `/health`. **No toca `.env` ni `data/`.** El host no está escrito en
el script a propósito: sale de `$env:GARMIN_VPS` o del parámetro `-Vps`.

## Operaciones

```bash
journalctl -u garmin-api -f            # logs del servicio
tail -f /root/garmin-ia/logs/api.err   # stderr de la app
systemctl restart garmin-api
systemctl reload caddy
```

## Mantenimiento

- **Copia `data/garmin_tokens/`.** Sin ese fichero toca rehacer el login con MFA.
  Es material sensible: da acceso persistente a la cuenta, guárdalo cifrado y
  fuera del VPS. El servidor **no tiene backup automático de ese directorio**.
- **Rotar el token de API**: cambia `GB_API_TOKEN` en `.env`,
  `systemctl restart garmin-api` y actualiza los clientes. No requiere tocar nada
  de Garmin.
- **No bajes `GB_SYNC_INTERVAL_MINUTES`.** Una hora con backfill de 7 días ya va
  sobrado; apretarlo es la vía rápida a que Garmin bloquee la cuenta.
- **Actualizar la librería**: `garminconnect` es no oficial y Garmin la rompe
  cada pocos meses. Al subirla de versión, repite el login antes de dar el
  despliegue por bueno.

## Alternativa: Docker

`Dockerfile` y `docker-compose.yml` siguen en el repo y sirven para una máquina
dedicada donde no compitas con otros servicios. **El servidor actual no tiene
Docker instalado** y su patrón es venv + systemd; instalarlo solo para esta app
saldría de la convención y añadiría reglas propias de iptables que se saltan
`ufw`. Si aun así vas por ahí, el compose ya ata el puerto a `127.0.0.1`
justamente para evitar eso, y el volumen `./data:/data` hace que los valores por
defecto de `GB_TOKENSTORE` y `GB_DB_PATH` sean correctos sin tocar el `.env`.
