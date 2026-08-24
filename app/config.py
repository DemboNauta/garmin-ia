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

    # Clave Fernet con la que se cifran los tokens de Garmin en la base de datos.
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # Si se pierde, los usuarios tienen que volver a vincular su cuenta.
    encryption_key: str = ""

    # Identificador del dueño de la instalacion. Los datos que ya hubiera en la
    # base cuando esta era mono-usuario se le atribuyen a el al migrar.
    owner_user_id: str = "owner"

    # Dominios publicos por los que se sirve el MCP, separados por comas.
    # El SDK trae proteccion anti DNS rebinding: como uvicorn escucha en
    # 127.0.0.1, FastMCP asume entorno local y responde 421 a cualquier Host
    # que no sea localhost. Detras de un proxy hay que declarar el de verdad.
    allowed_hosts: str = ""

    # URL publica del servicio, tal cual la ve el navegador. OAuth la necesita
    # para los metadatos y las redirecciones, y tiene que ser HTTPS: por ahi
    # viajan codigos de autorizacion.
    public_url: str = "http://127.0.0.1:8000"

    # Zona horaria para resolver "hoy"
    timezone: str = "Europe/Madrid"

    # Sincronizacion automatica
    sync_enabled: bool = True
    sync_interval_minutes: int = 60
    sync_backfill_days: int = 7

    # OAuth de Strava (developers.strava.com/settings). Vacias, la vinculacion
    # se desactiva sola: el boton no aparece en vez de fallar a medias.
    # El "Authorization Callback Domain" que pide Strava al crear la app es el
    # host de GB_PUBLIC_URL, sin esquema ni ruta.
    strava_client_id: str = ""
    strava_client_secret: str = ""


settings = Settings()
