# Despliegue en VPS

Guía para poner Garmin Bridge en un servidor con TLS y sin dejar nada abierto.
Asume Debian 12 o Ubuntu 22.04+.

## 1. Preparar el servidor

```bash
adduser garmin && usermod -aG sudo garmin   # no trabajes como root
```

Cortafuegos: solo SSH y HTTP/S. **El puerto 8000 no se abre nunca** — se llega a
él por el proxy, desde dentro de la máquina.

```bash
ufw default deny incoming && ufw allow OpenSSH && ufw allow 80 && ufw allow 443 && ufw enable
```

SSH solo con clave, que es la puerta por la que entran de verdad. En
`/etc/ssh/sshd_config`:

```
PasswordAuthentication no
PermitRootLogin no
```

Docker:

```bash
curl -fsSL https://get.docker.com | sh && usermod -aG docker garmin
```

## 2. Clonar y configurar

```bash
git clone https://github.com/DemboNauta/garmin-ia.git && cd garmin-ia
cp .env.example .env
```

Genera el token y mételo en `.env` (`GB_API_TOKEN`):

```bash
openssl rand -hex 32
```

Deja `GB_GARMIN_EMAIL` y `GB_GARMIN_PASSWORD` **vacíos**: el login del paso 4 los
pide por teclado y así tu contraseña de Garmin no queda escrita en disco. Solo
rellénalos si necesitas que el servicio pueda re-loguearse solo dentro de un año,
cuando caduquen los tokens.

El `.env` solo lo lee su dueño:

```bash
chmod 600 .env
```

## 3. Permisos del volumen

El contenedor corre como UID 10001 sin privilegios, así que el directorio de
datos del host tiene que pertenecerle o no podrá escribir los tokens ni la base:

```bash
mkdir -p data && sudo chown -R 10001:10001 data && chmod 700 data
```

## 4. Primer login (una sola vez)

```bash
docker compose build
docker compose run --rm api python -m app.login
```

Pide email, contraseña y, si la cuenta lo tiene activado, el código MFA. Guarda
los tokens en `data/garmin_tokens/`, que la librería escribe con permisos 0600.

Si sale `429: IP rate limited by Garmin`, **no lo reintentes en bucle**: el
bloqueo es por IP y machacarlo lo alarga. Espera y vuelve a probar. La librería
encadena cinco estrategias, así que es normal ver fallar las primeras y que entre
por una posterior.

## 5. Arrancar

```bash
docker compose up -d
curl localhost:8000/health
```

## 6. TLS

```bash
sudo apt install caddy
sudo cp Caddyfile /etc/caddy/Caddyfile   # cambia el dominio primero
sudo systemctl reload caddy
```

Caddy pide y renueva el certificado solo. El dominio debe apuntar ya al VPS.

## 7. Comprobar que está bien cerrado

```bash
curl https://garmin.tu-dominio.com/health                    # 200, publico
curl -o /dev/null -w '%{http_code}\n' https://garmin.tu-dominio.com/metrics   # 401
curl -o /dev/null -w '%{http_code}\n' -X POST https://garmin.tu-dominio.com/mcp/  # 401
```

Los dos últimos **tienen que dar 401**. Si el de `/mcp/` responde otra cosa, el
middleware no está activo: para y revísalo antes de seguir.

Desde fuera del VPS, el 8000 debe estar mudo:

```bash
nc -zv TU_IP 8000    # connection refused / timeout
```

Y que los datos fluyan de verdad, no solo que el servicio responda:

```bash
curl -X POST 'https://garmin.tu-dominio.com/sync?days=2' -H "Authorization: Bearer TU_TOKEN"
```

Tiene que devolver `"errors": []` y un `synced_days` mayor que cero. Si trae
errores de sesión, el login del paso 4 no cuajó: repítelo. El servicio ya no
cachea días vacíos ni los cuenta como sincronizados, así que un `synced_days: 0`
significa exactamente eso y no se queda pegado en la base de datos.

## 8. Conectar el cliente MCP

```bash
claude mcp add --transport http garmin https://garmin.tu-dominio.com/mcp/ \
  --header "Authorization: Bearer TU_TOKEN"
```

## Mantenimiento

- **Copia `data/garmin_tokens/`.** Sin ese fichero toca rehacer el login con MFA.
  Es material sensible: da acceso persistente a la cuenta, guárdalo cifrado.
- **Rotar el token de API**: cambia `GB_API_TOKEN`, `docker compose up -d` y
  actualiza los clientes. No requiere tocar nada de Garmin.
- **No bajes `GB_SYNC_INTERVAL_MINUTES`.** Una hora con backfill de 7 días ya va
  sobrado; apretarlo es la vía rápida a que Garmin bloquee la cuenta.
- **Actualizar**: `git pull && docker compose build && docker compose up -d`.
  La librería es no oficial y Garmin la rompe cada pocos meses.
