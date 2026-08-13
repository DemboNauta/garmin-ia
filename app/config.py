from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="GB_", extra="ignore")

    # Credenciales de Garmin Connect. Solo se usan en el primer login:
    # a partir de ahi se trabaja con los tokens OAuth cacheados.
    garmin_email: str = ""
    garmin_password: str = ""

    # Directorio donde se guardan los tokens (persistir en volumen Docker).
    tokenstore: str = "/data/garmin_tokens"

    # SQLite de cache
    db_path: str = "/data/garmin.db"

    # Token bearer para proteger la API y el endpoint MCP.
    api_token: str = "cambiame"

    # Dominios publicos por los que se sirve el MCP, separados por comas.
    # El SDK trae proteccion anti DNS rebinding: como uvicorn escucha en
    # 127.0.0.1, FastMCP asume entorno local y responde 421 a cualquier Host
    # que no sea localhost. Detras de un proxy hay que declarar el de verdad.
    allowed_hosts: str = ""

    # Zona horaria para resolver "hoy"
    timezone: str = "Europe/Madrid"

    # Sincronizacion automatica
    sync_enabled: bool = True
    sync_interval_minutes: int = 60
    sync_backfill_days: int = 7


settings = Settings()
