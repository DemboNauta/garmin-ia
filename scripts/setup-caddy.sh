#!/usr/bin/env bash
# Publica garmin-ia en Caddy SIN reescribir el Caddyfile compartido.
#
# El fichero lo comparten todas las apps del servidor, asi que este script solo
# toca su propio bloque, delimitado por marcadores, y valida antes de recargar.
#
#   ./setup-caddy.sh garmin.tu-dominio.com [puerto]
#
# Para un dominio detras de Cloudflare en modo Flexible, pasa el dominio con el
# prefijo http:// incluido, o Caddy intentara sacar un certificado que sobra.
set -euo pipefail

DOMINIO="${1:?uso: setup-caddy.sh <dominio> [puerto]}"
PUERTO="${2:-8003}"
CADDYFILE=/etc/caddy/Caddyfile

cp "$CADDYFILE" "${CADDYFILE}.bak.$(date +%s)"

# Fuera el bloque anterior, si lo hubiera, para que no se dupliquen.
sed -i '/# >>> GARMIN BEGIN <<</,/# >>> GARMIN END <<</d' "$CADDYFILE"

cat >> "$CADDYFILE" <<EOF

# >>> GARMIN BEGIN <<<
${DOMINIO} {
	encode gzip

	# El bearer viaja en cada peticion: sin HSTS un downgrade a HTTP lo expone.
	header {
		Strict-Transport-Security "max-age=31536000; includeSubDomains"
		X-Content-Type-Options "nosniff"
		Referrer-Policy "no-referrer"
		-Server
	}

	# flush_interval -1 es obligatorio: el endpoint MCP va por streaming HTTP
	# y con el buffer por defecto Caddy trocea las respuestas.
	reverse_proxy 127.0.0.1:${PUERTO} {
		flush_interval -1
	}
}
# >>> GARMIN END <<<
EOF

# Si la configuracion no valida, mejor enterarse aqui que dejar a Caddy caido
# y llevarse por delante las otras cinco apps del servidor.
caddy validate --config "$CADDYFILE"
systemctl reload caddy

echo "Publicado ${DOMINIO} -> localhost:${PUERTO}"
