# Garmin Bridge

Backend que lee tus datos de Garmin Connect y los expone por **MCP**, para que un
modelo pueda analizar tus métricas y escribir entrenamientos estructurados en tu cuenta.

Los datos salen de **Garmin Connect**, no del dispositivo: funciona con el Cirqa y con
cualquier otro Garmin que vincules después (Fénix, Forerunner, Edge…) sin tocar código.

## Arquitectura

```
Cirqa ──BLE──> App Garmin Connect ──> Garmin Cloud
                                          │
                            garminconnect (no oficial)
                                          │
                              ┌───────────▼───────────┐
                              │   Garmin Bridge (VPS) │
                              │  FastAPI + SQLite     │
                              │  sync horario         │
                              └───┬───────────────┬───┘
                                  │ /mcp          │ REST
                              modelo LLM      lo que quieras
```

## Instalación

```bash
git clone <repo> && cd garmin-bridge
cp .env.example .env && nano .env        # credenciales + GB_API_TOKEN
docker compose build
docker compose run --rm api python -m app.login   # una vez, resuelve el MFA
docker compose up -d
curl localhost:8000/health
```

Detrás de un Caddy o Nginx con TLS: el endpoint MCP es `https://tu-dominio/mcp`,
con cabecera `Authorization: Bearer <GB_API_TOKEN>`.

## Herramientas MCP

| Herramienta | Para qué |
|---|---|
| `get_devices` | Qué Garmin hay vinculados y su última sincronización |
| `get_today` | Foto de hoy: sueño, HRV, Body Battery, readiness |
| `get_metrics(days)` | Serie diaria de las ~15 métricas accionables |
| `get_activities(days)` | Sesiones registradas con FC media, ritmo y training effect |
| `create_workout(spec, fecha)` | Crea y programa un entrenamiento estructurado |
| `list_workouts` | Entrenamientos ya guardados en Connect |

## Avisos

- **La librería es no oficial.** Garmin puede romperla en cualquier actualización.
  El acoplamiento está aislado en `garmin_client.py` y `workouts.py`: si un día
  quieres migrar a la API oficial (Health + Training API), solo se tocan esos dos.
- **No hagas login en bucle.** El sync por defecto es cada hora con backfill de 7 días;
  bajarlo mucho es la forma más rápida de que te bloqueen la cuenta.
- **Guarda el `.env` fuera del repo.** Contiene tu contraseña real de Garmin.
- **El Cirqa no tiene pantalla**: los entrenamientos creados se ven en la app Garmin
  Connect, no en la muñeca. El día que añadas un reloj se sincronizarán a él solos.
- `create_workout` usa un endpoint interno de Connect (`workout-service`) y es la
  parte menos estable del proyecto. Pruébalo con un entrenamiento tonto primero.
