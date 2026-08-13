"""Servidor de autorizacion OAuth 2.1 respaldado por SQLite.

El protocolo (endpoints, PKCE, metadatos, registro dinamico) lo implementa el
SDK de MCP; aqui solo va la persistencia y la decision de quien es el usuario.

Se guarda el hash de codigos y tokens, nunca el valor: son credenciales, y quien
consiga una copia de la base no debe poder suplantar a nadie con ella.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import secrets
import time

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from . import store
from .config import settings

log = logging.getLogger(__name__)

VIDA_CODIGO = 300           # 5 min, lo que tarda una persona en identificarse
VIDA_ACCESO = 3600          # 1 h; el refresh se encarga de renovarlo
VIDA_REFRESCO = 30 * 86400  # 30 dias sin usarlo y toca volver a autorizar
VIDA_PENDIENTE = 600        # margen para completar el formulario de login


def _hash(valor: str) -> str:
    return hashlib.sha256(valor.encode()).hexdigest()


def _ahora() -> float:
    return time.time()


class ProveedorGarmin(OAuthAuthorizationServerProvider):
    """Implementa el contrato del SDK contra nuestra base."""

    # ------------------------------------------------------------- clientes
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with store.conn() as c:
            fila = c.execute(
                "SELECT info FROM oauth_clients WHERE client_id=?", (client_id,)
            ).fetchone()
        return OAuthClientInformationFull.model_validate_json(fila["info"]) if fila else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        # Registro dinamico: claude.ai y ChatGPT se dan de alta solos, sin que
        # haya que crearles credenciales a mano.
        with store.conn() as c:
            c.execute(
                "INSERT INTO oauth_clients(client_id, info, created_at) VALUES(?,?,?) "
                "ON CONFLICT(client_id) DO UPDATE SET info=excluded.info",
                (
                    client_info.client_id,
                    client_info.model_dump_json(exclude_none=True),
                    dt.datetime.now(dt.timezone.utc).isoformat(),
                ),
            )
        log.info("Cliente OAuth registrado: %s (%s)", client_info.client_id, client_info.client_name)

    # ---------------------------------------------------------- autorizacion
    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Aparca la peticion y manda al usuario a identificarse.

        El SDK no sabe quien es la persona: eso lo resuelve nuestra pagina de
        login, que al terminar llama a `completar_autorizacion`.
        """
        pendiente = secrets.token_urlsafe(24)
        with store.conn() as c:
            c.execute(
                "INSERT INTO oauth_pending(pending_id, client_id, params, expires_at) "
                "VALUES(?,?,?,?)",
                (
                    pendiente,
                    client.client_id,
                    params.model_dump_json(),
                    _ahora() + VIDA_PENDIENTE,
                ),
            )
        return f"{settings.public_url}/login?req={pendiente}"

    def leer_pendiente(self, pending_id: str) -> tuple[str, AuthorizationParams] | None:
        with store.conn() as c:
            fila = c.execute(
                "SELECT * FROM oauth_pending WHERE pending_id=?", (pending_id,)
            ).fetchone()
        if not fila or fila["expires_at"] < _ahora():
            return None
        return fila["client_id"], AuthorizationParams.model_validate_json(fila["params"])

    def completar_autorizacion(self, pending_id: str, user_id: str) -> str:
        """Emite el codigo ya con el usuario identificado y devuelve el destino."""
        datos = self.leer_pendiente(pending_id)
        if not datos:
            raise ValueError("La solicitud de autorizacion ha caducado. Vuelve a empezar.")
        client_id, params = datos

        codigo = secrets.token_urlsafe(32)  # >160 bits, como pide la RFC 6749
        registro = AuthorizationCode(
            code=codigo,
            scopes=params.scopes or [],
            expires_at=_ahora() + VIDA_CODIGO,
            client_id=client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject=user_id,
        )
        with store.conn() as c:
            c.execute(
                "INSERT INTO oauth_codes(code_hash, payload, expires_at) VALUES(?,?,?)",
                (_hash(codigo), registro.model_dump_json(), registro.expires_at),
            )
            c.execute("DELETE FROM oauth_pending WHERE pending_id=?", (pending_id,))
        return construct_redirect_uri(str(params.redirect_uri), code=codigo, state=params.state)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        with store.conn() as c:
            fila = c.execute(
                "SELECT payload FROM oauth_codes WHERE code_hash=?", (_hash(authorization_code),)
            ).fetchone()
        if not fila:
            return None
        codigo = AuthorizationCode.model_validate_json(fila["payload"])
        if codigo.client_id != client.client_id or codigo.expires_at < _ahora():
            return None
        return codigo

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        # De un solo uso: se borra antes de emitir nada, para que un codigo
        # interceptado no pueda canjearse dos veces.
        with store.conn() as c:
            borradas = c.execute(
                "DELETE FROM oauth_codes WHERE code_hash=?", (_hash(authorization_code.code),)
            ).rowcount
        if borradas != 1:
            raise ValueError("El codigo de autorizacion ya se habia canjeado.")
        return self._emitir(
            client_id=client.client_id,
            user_id=authorization_code.subject or "",
            scopes=authorization_code.scopes,
            resource=authorization_code.resource,
        )

    # ---------------------------------------------------------------- tokens
    def _emitir(
        self, client_id: str, user_id: str, scopes: list[str], resource: str | None
    ) -> OAuthToken:
        acceso = secrets.token_urlsafe(32)
        refresco = secrets.token_urlsafe(32)
        caduca = _ahora() + VIDA_ACCESO

        token_acceso = AccessToken(
            token=acceso,
            client_id=client_id,
            scopes=scopes,
            expires_at=int(caduca),
            resource=resource,
            subject=user_id,
        )
        token_refresco = RefreshToken(
            token=refresco,
            client_id=client_id,
            scopes=scopes,
            expires_at=int(_ahora() + VIDA_REFRESCO),
            subject=user_id,
        )
        with store.conn() as c:
            c.execute(
                "INSERT INTO oauth_tokens(token_hash, kind, payload, user_id, expires_at) "
                "VALUES(?,?,?,?,?)",
                (_hash(acceso), "access", token_acceso.model_dump_json(), user_id, caduca),
            )
            c.execute(
                "INSERT INTO oauth_tokens(token_hash, kind, payload, user_id, expires_at) "
                "VALUES(?,?,?,?,?)",
                (
                    _hash(refresco),
                    "refresh",
                    token_refresco.model_dump_json(),
                    user_id,
                    token_refresco.expires_at,
                ),
            )
        return OAuthToken(
            access_token=acceso,
            token_type="Bearer",
            expires_in=VIDA_ACCESO,
            refresh_token=refresco,
            scope=" ".join(scopes) if scopes else None,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        with store.conn() as c:
            fila = c.execute(
                "SELECT payload, expires_at FROM oauth_tokens WHERE token_hash=? AND kind='access'",
                (_hash(token),),
            ).fetchone()
        if not fila or (fila["expires_at"] and fila["expires_at"] < _ahora()):
            return None
        return AccessToken.model_validate_json(fila["payload"])

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        with store.conn() as c:
            fila = c.execute(
                "SELECT payload, expires_at FROM oauth_tokens WHERE token_hash=? AND kind='refresh'",
                (_hash(refresh_token),),
            ).fetchone()
        if not fila or (fila["expires_at"] and fila["expires_at"] < _ahora()):
            return None
        token = RefreshToken.model_validate_json(fila["payload"])
        return token if token.client_id == client.client_id else None

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        # Rotacion: la OAuth 2.1 la exige para clientes publicos, y ademas
        # permite detectar un refresh robado cuando se intenta reutilizar.
        with store.conn() as c:
            c.execute("DELETE FROM oauth_tokens WHERE token_hash=?", (_hash(refresh_token.token),))
        return self._emitir(
            client_id=client.client_id,
            user_id=refresh_token.subject or "",
            scopes=scopes or refresh_token.scopes,
            resource=None,
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        with store.conn() as c:
            c.execute("DELETE FROM oauth_tokens WHERE token_hash=?", (_hash(token.token),))

    # ------------------------------------------------------------ mantenimiento
    def purgar_caducados(self) -> int:
        ahora = _ahora()
        with store.conn() as c:
            n = c.execute("DELETE FROM oauth_tokens WHERE expires_at < ?", (ahora,)).rowcount
            n += c.execute("DELETE FROM oauth_codes WHERE expires_at < ?", (ahora,)).rowcount
            n += c.execute("DELETE FROM oauth_pending WHERE expires_at < ?", (ahora,)).rowcount
        return n


proveedor = ProveedorGarmin()
