"""Paginas web: alta por invitacion, inicio de sesion y vinculacion de cuentas.

Por que hay HTML en un servidor MCP: las credenciales de Garmin o de la bascula
NO pueden pedirse con una herramienta MCP. Si lo hicieran, la contraseña del
usuario pasaria por el contexto del modelo y quedaria en el historial de la
conversacion. Aqui van directas del navegador al servidor.

Aqui vive tambien el armazon comun (la cookie de sesion y la plantilla de
pagina) del que tira el panel en `panel.py`. La dependencia va en ese sentido y
no al reves: estas paginas no saben nada del panel salvo su direccion.
"""
from __future__ import annotations

import html
import logging
import time

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import accounts, garmin_client, scales, store, strava
from .config import settings
from .oauth_provider import proveedor

log = logging.getLogger(__name__)
router = APIRouter()

COOKIE = "gb_sesion"

_MARCA = "<div class=marca><span class=punto></span>Garmin Bridge</div>"


def _cabeza(titulo: str, *hojas: str) -> str:
    enlaces = "".join(f"<link rel=stylesheet href='/static/{h}'>" for h in ("estilo.css", *hojas))
    return (
        f"<!doctype html><html lang=es><meta charset=utf-8>"
        f"<meta name=viewport content='width=device-width,initial-scale=1,viewport-fit=cover'>"
        f"<meta name=color-scheme content=dark>"
        # El manifest es lo que convierte "añadir a pantalla de inicio" en una
        # app de verdad: icono propio, sin barra del navegador y arrancando en
        # /panel. El scope va explicito porque el fichero vive en /static/ y su
        # ambito por defecto seria solo ese directorio.
        f"<link rel=manifest href='/static/manifest.webmanifest'>"
        f"<meta name=theme-color content='#08080a'>"
        f"<link rel=icon type=image/png href='/static/icono-192.png'>"
        f"<link rel=apple-touch-icon href='/static/icono-180.png'>"
        f"<title>{html.escape(titulo)}</title>{enlaces}"
    )


def _pagina(titulo: str, cuerpo: str, codigo: int = 200, ancho: bool = False) -> HTMLResponse:
    return HTMLResponse(
        f"{_cabeza(titulo)}<body class=formulario>"
        f"<main class='{'ancho' if ancho else ''}'>"
        f"<div class=tarjeta>{_MARCA}{cuerpo}</div></main>",
        status_code=codigo,
    )


def _aviso(texto: str, clase: str = "error") -> str:
    return f"<div class={clase}>{html.escape(texto)}</div>"


def pagina_panel() -> HTMLResponse:
    """Armazon del panel. Lo rellena `/static/panel.js` llamando a la API.

    Las secciones van vacias a proposito: pintar aqui la primera pantalla
    obligaria a esperar a Garmin antes del primer byte, y con un dia sin
    cachear eso son varios segundos de pagina en blanco.
    """
    pestañas = [
        ("hoy", "Hoy"),
        ("tendencias", "Tendencias"),
        ("sesiones", "Sesiones"),
        ("cuerpo", "Cuerpo"),
        ("analisis", "Análisis"),
        ("ajustes", "Ajustes"),
    ]
    primera = pestañas[0][0]
    botones = "".join(
        f"<button class='pestana {'activa' if clave == primera else ''}' "
        f"data-vista='{clave}' role=tab "
        f"aria-selected='{'true' if clave == primera else 'false'}'>{texto}</button>"
        for clave, texto in pestañas
    )
    secciones = "".join(
        f"<section class='vista {'activa' if clave == primera else ''}' "
        f"id='vista-{clave}' role=tabpanel><div class=esqueleto></div></section>"
        for clave, _ in pestañas
    )
    return HTMLResponse(
        f"{_cabeza('Panel · Garmin Bridge', 'panel.css')}<body class=panel>"
        f"<header class=barra>"
        f"<a class=marca href='/panel'><span class=punto></span>Garmin Bridge</a>"
        f"<nav class='pestanas nav-principal' role=tablist>{botones}</nav>"
        f"<div class=barra-fin>"
        f"<button id=sincronizar class=fantasma title='Volver a bajar los últimos días'>"
        f"Sincronizar</button>"
        f"<form method=post action='/salir'><button class=fantasma>Salir</button></form>"
        f"</div></header>"
        f"<main id=contenido>{secciones}</main>"
        f"<div id=aviso-flotante class=flotante hidden></div>"
        f"<script src='/static/panel.js' defer></script>"
    )


# ----------------------------------------------------------------- sesion web
def poner_cookie(respuesta, token: str) -> None:
    respuesta.set_cookie(
        COOKIE,
        token,
        httponly=True,                                   # fuera del alcance de JS
        secure=settings.public_url.startswith("https"),  # solo por TLS
        # Lax y no Strict, y no es un descuido. Con Strict, Chrome no manda la
        # cookie cuando la navegacion la inicia algo que no es el navegador: al
        # abrir el panel desde el acceso directo de la pantalla de inicio pedia
        # iniciar sesion siempre, porque el lanzador de Android cuenta como
        # origen externo. Lax solo afloja las navegaciones GET de primer nivel;
        # todo lo que cambia estado aqui (login, salir, desvincular, sync,
        # borrar analisis) es POST o DELETE, y ahi Lax sigue sin mandarla.
        samesite="lax",
        max_age=30 * 24 * 3600,
        path="/",
    )


def _usuario(request: Request) -> str | None:
    return accounts.usuario_de_sesion(request.cookies.get(COOKIE))


def _a_entrar(destino: str) -> RedirectResponse:
    return RedirectResponse(f"/entrar?destino={destino}", status_code=303)


@router.get("/entrar", response_class=HTMLResponse)
def form_entrar(request: Request, destino: str = "/panel"):
    if _usuario(request):
        return RedirectResponse(_seguro(destino), status_code=303)
    return _pagina_entrar(destino)


def _pagina_entrar(destino: str, error: str = "") -> HTMLResponse:
    return _pagina(
        "Entrar",
        f"<h1>Entra en tu panel</h1>"
        f"<p class=sub>Tus métricas, lo que ha analizado tu asistente y tus "
        f"conexiones.</p>"
        f"{_aviso(error) if error else ''}"
        f"<form method=post>"
        f"<input type=hidden name=destino value='{html.escape(_seguro(destino))}'>"
        f"<label for=email>Email</label>"
        f"<input id=email name=email type=email required autocomplete=username>"
        f"<label for=password>Contraseña</label>"
        f"<input id=password name=password type=password required "
        f"autocomplete=current-password>"
        f"<button type=submit>Entrar</button></form>"
        f"<p class=aviso>¿Todavía no tienes cuenta? El servicio es cerrado: hace "
        f"falta una invitación de quien lo administra.</p>",
    )


def _seguro(destino: str) -> str:
    """Solo se admite volver a una ruta de este sitio.

    Sin esto, `/entrar?destino=https://otro.sitio` convierte el login en un
    redirector abierto, que es la pieza que le falta a media suplantacion.
    """
    return destino if destino.startswith("/") and not destino.startswith("//") else "/panel"


@router.post("/entrar")
def entrar(email: str = Form(...), password: str = Form(...), destino: str = Form("/panel")):
    user_id = accounts.autenticar(email, password)
    if not user_id:
        # Mismo mensaje para email inexistente y contraseña mala: si no, se
        # podria averiguar quien tiene cuenta probando emails.
        return _pagina_entrar(destino, "Email o contraseña incorrectos.")
    respuesta = RedirectResponse(_seguro(destino), status_code=303)
    poner_cookie(respuesta, accounts.crear_sesion(user_id))
    return respuesta


@router.post("/salir")
def salir(request: Request):
    accounts.cerrar_sesion(request.cookies.get(COOKIE))
    respuesta = RedirectResponse("/", status_code=303)
    respuesta.delete_cookie(COOKIE, path="/")
    return respuesta


# ------------------------------------------------------------------- portada
@router.get("/", response_class=HTMLResponse)
def portada(request: Request) -> HTMLResponse:
    if _usuario(request):
        return RedirectResponse("/panel", status_code=303)
    url_mcp = f"{settings.public_url}/mcp"
    return _pagina(
        "Garmin Bridge",
        f"<h1>Tus datos de Garmin, dentro de tu asistente</h1>"
        f"<p class=sub>Conecta Garmin Connect con Claude o ChatGPT. El asistente "
        f"lee tu sueño, tu HRV y tu recuperación, puede escribir entrenamientos "
        f"estructurados en tu cuenta, y deja su análisis por escrito en tu panel.</p>"
        f"<div class=rejilla>"
        f"<div class=dato><b>Lee</b><span>Sueño, HRV, frecuencia en reposo, "
        f"Body Battery, readiness y actividades</span></div>"
        f"<div class=dato><b>Escribe</b><span>Entrenamientos de fuerza y cardio, "
        f"con ejercicio y grupo muscular, programados en tu calendario</span></div>"
        f"</div>"
        f"<h2>Cómo conectarlo</h2>"
        f"<ol>"
        f"<li><b>Consigue una invitación.</b> El servicio es cerrado: necesitas "
        f"un enlace de quien lo administra.</li>"
        f"<li><b>Crea tu cuenta y vincula Garmin.</b> Se hace en este sitio, "
        f"desde tu navegador.</li>"
        f"<li><b>Añade el conector</b> en tu asistente con esta dirección:"
        f"<span class=url>{html.escape(url_mcp)}</span></li>"
        f"<li><b>Autoriza.</b> Se abrirá una ventana para que inicies sesión aquí. "
        f"Eso es todo.</li>"
        f"</ol>"
        f"<a class=boton href='/entrar'>Entrar en mi panel</a>"
        f"<p class=aviso>En Claude: <code>Configuración → Conectores → Añadir "
        f"conector personalizado</code>. En ChatGPT, en los ajustes de conectores. "
        f"Tu contraseña de Garmin se usa una sola vez para obtener un permiso de "
        f"acceso y no se guarda; el permiso queda cifrado en el servidor.</p>",
        ancho=True,
    )


# ---------------------------------------------------------------------- admin
@router.get("/admin", response_class=HTMLResponse)
def panel_admin(request: Request, nuevo: str = "") -> HTMLResponse:
    # Ser administrador no abre una sesion aparte: es la misma cookie con una
    # comprobacion mas, asi que quien no lo sea vuelve a su panel de siempre.
    if not accounts.admin_de_sesion(request.cookies.get(COOKIE)):
        # Quien no ha entrado, al login; quien ha entrado pero no es admin, a su
        # panel: decirle "no tienes permiso" solo confirmaria que esto existe.
        return _a_entrar("/admin") if not _usuario(request) else \
            RedirectResponse("/panel", status_code=303)

    recien = ""
    if nuevo:
        enlace = f"{settings.public_url}/invite/{nuevo}"
        recien = (
            f"<div class=ok><b>Invitación creada.</b> Cópiala ahora: no se puede "
            f"volver a ver.</div><span class=url>{html.escape(enlace)}</span>"
        )

    filas = []
    for u in store.listar_usuarios():
        marca = " · admin" if u["is_admin"] else ""
        garmin = "Garmin vinculado" if u["garmin_vinculado"] else "sin vincular"
        bascula = f" · báscula {u['bascula']}" if u["bascula"] else ""
        filas.append(
            f"<div class=dato><b>{html.escape(u['email'] or u['user_id'])}{marca}</b>"
            f"<span>{garmin}{html.escape(bascula)} · desde {u['created_at'][:10]}</span></div>"
        )

    pendientes = [i for i in accounts.listar_invitaciones() if not i["used_at"]]
    lista_pendientes = "".join(
        f"<div class=dato><b>{html.escape(i['email'] or 'sin email')}</b>"
        f"<span>pendiente · caduca {i['expires_at'][:10]}</span></div>"
        for i in pendientes
    ) or "<p class=aviso>No hay invitaciones pendientes.</p>"

    return _pagina(
        "Administración",
        f"<h1>Administración</h1>"
        f"<p class=sub>Invita a quien quieras y controla quién tiene acceso.</p>"
        f"{recien}"
        f"<form method=post action='/admin/invitar'>"
        f"<label for=email>Invitar a (email, opcional: solo para acordarte)</label>"
        f"<input id=email name=email type=email placeholder='amigo@ejemplo.com'>"
        f"<button type=submit>Crear invitación</button></form>"
        f"<h2>Usuarios</h2><div class=rejilla>{''.join(filas)}</div>"
        f"<h2>Invitaciones pendientes</h2><div class=rejilla>{lista_pendientes}</div>"
        f"<a class=boton href='/panel'>Volver a mi panel</a>",
        ancho=True,
    )


@router.post("/admin/invitar")
def admin_invitar(request: Request, email: str = Form(default="")):
    if not accounts.admin_de_sesion(request.cookies.get(COOKIE)):
        return RedirectResponse("/admin", status_code=303)
    codigo = accounts.crear_invitacion(email.strip() or None)
    # El codigo viaja una vez en la URL para poder enseñarlo; despues solo queda
    # su hash en la base y no hay forma de recuperarlo.
    return RedirectResponse(f"/admin?nuevo={codigo}", status_code=303)


# ------------------------------------------------------------------ invitacion
@router.get("/invite/{codigo}", response_class=HTMLResponse)
def form_invitacion(codigo: str, error: str = "") -> HTMLResponse:
    if not accounts.invitacion_valida(codigo):
        return _pagina(
            "Invitación no válida",
            "<h1>Esta invitación no sirve</h1><p class=sub>Puede que haya caducado "
            "o que ya se haya usado. Pídele otra a quien te invitó.</p>",
            codigo=404,
        )
    return _pagina(
        "Crear cuenta",
        f"<h1>Crea tu cuenta</h1>"
        f"<p class=sub>Para conectar tus datos de Garmin con tu asistente.</p>"
        f"{_aviso(error) if error else ''}"
        f"<form method=post>"
        f"<label for=email>Email</label>"
        f"<input id=email name=email type=email required autocomplete=username>"
        f"<label for=password>Contraseña (mínimo 12 caracteres)</label>"
        f"<input id=password name=password type=password required minlength=12 "
        f"autocomplete=new-password>"
        f"<button type=submit>Crear cuenta</button></form>"
        f"<p class=aviso>Esta contraseña es solo para este servicio. "
        f"La de Garmin se pide después, y no se guarda.</p>",
    )


@router.post("/invite/{codigo}", response_class=HTMLResponse)
def crear_cuenta(codigo: str, email: str = Form(...), password: str = Form(...)):
    try:
        user_id = accounts.registrar_con_invitacion(codigo, email, password)
    except accounts.ErrorDeCuenta as exc:
        return form_invitacion(codigo, error=str(exc))
    log.info("Cuenta creada: %s", user_id)
    # Entra ya: acaba de demostrar quien es poniendo la contraseña, y sin sesion
    # la siguiente pantalla no sabria de quien es la cuenta de Garmin que vincula.
    respuesta = RedirectResponse("/vincular-garmin", status_code=303)
    poner_cookie(respuesta, accounts.crear_sesion(user_id))
    return respuesta


# ------------------------------------------------------------- login de OAuth
@router.get("/login", response_class=HTMLResponse)
def form_login(req: str = "", error: str = "") -> HTMLResponse:
    if not req or not proveedor.leer_pendiente(req):
        return _pagina(
            "Solicitud caducada",
            "<h1>La solicitud ha caducado</h1><p class=sub>Vuelve a intentar la "
            "conexión desde tu asistente.</p>",
            codigo=400,
        )
    # Aunque haya sesion abierta se piden las credenciales otra vez: esto no es
    # entrar en el sitio, es autorizar a un tercero a leer datos de salud, y
    # conviene que sea un acto explicito y no un clic distraido.
    return _pagina(
        "Iniciar sesión",
        f"<h1>Conectar con Garmin Bridge</h1>"
        f"<p class=sub>Tu asistente quiere acceder a tus métricas y crear "
        f"entrenamientos en tu cuenta de Garmin.</p>"
        f"{_aviso(error) if error else ''}"
        f"<form method=post>"
        f"<input type=hidden name=req value='{html.escape(req)}'>"
        f"<label for=email>Email</label>"
        f"<input id=email name=email type=email required autocomplete=username>"
        f"<label for=password>Contraseña</label>"
        f"<input id=password name=password type=password required "
        f"autocomplete=current-password>"
        f"<button type=submit>Autorizar</button></form>",
    )


@router.post("/login")
def hacer_login(req: str = Form(...), email: str = Form(...), password: str = Form(...)):
    user_id = accounts.autenticar(email, password)
    if not user_id:
        return form_login(req, error="Email o contraseña incorrectos.")
    try:
        destino = proveedor.completar_autorizacion(req, user_id)
    except ValueError as exc:
        return form_login(req, error=str(exc))
    return RedirectResponse(destino, status_code=303)


# -------------------------------------------------------- vinculacion de Garmin
@router.get("/vincular-garmin", response_class=HTMLResponse)
def form_garmin(request: Request, error: str = "", mfa: str = ""):
    user_id = _usuario(request)
    if not user_id:
        return _a_entrar("/vincular-garmin")
    if mfa:
        return _pagina(
            "Código de verificación",
            f"<h1>Código de Garmin</h1>"
            f"<p class=sub>Garmin ha enviado un código a tu email o teléfono.</p>"
            f"{_aviso(error) if error else ''}"
            f"<form method=post action='/vincular-garmin/mfa'>"
            f"<input type=hidden name=mfa_id value='{html.escape(mfa)}'>"
            f"<label for=codigo>Código</label>"
            f"<input id=codigo name=codigo required inputmode=numeric autocomplete=one-time-code>"
            f"<button type=submit>Confirmar</button></form>",
        )
    ya = store.leer_tokens_garmin(user_id) is not None
    return _pagina(
        "Vincular Garmin",
        f"<h1>Conecta tu cuenta de Garmin</h1>"
        f"<p class=sub>Se usa una sola vez para obtener un permiso de acceso. "
        f"Tu contraseña no se guarda en ningún momento.</p>"
        f"{'<div class=ok>Ya tienes Garmin vinculado. Si vuelves a hacerlo, se sustituye el permiso anterior.</div>' if ya else ''}"
        f"{_aviso(error) if error else ''}"
        f"<form method=post>"
        f"<label for=email>Email de Garmin Connect</label>"
        f"<input id=email name=email type=email required autocomplete=off>"
        f"<label for=password>Contraseña de Garmin</label>"
        f"<input id=password name=password type=password required autocomplete=off>"
        f"<button type=submit>Vincular</button></form>"
        f"<p class=aviso>Garmin limita los intentos por IP. Si falla, espera un "
        f"rato antes de reintentar en vez de insistir.</p>",
    )


@router.post("/vincular-garmin", response_class=HTMLResponse)
def vincular(request: Request, email: str = Form(...), password: str = Form(...)):
    from garminconnect import Garmin

    user_id = _usuario(request)
    if not user_id:
        return _a_entrar("/vincular-garmin")

    api = Garmin(email, password, return_on_mfa=True)
    try:
        resultado = api.login()
    except Exception as exc:
        return form_garmin(request, error=f"Garmin rechazó el acceso: {exc}"[:200])

    if isinstance(resultado, tuple) and resultado and resultado[0] == "needs_mfa":
        import secrets as _s

        mfa_id = _s.token_urlsafe(16)
        _MFA_PENDIENTE[mfa_id] = (user_id, api, resultado[1])
        return RedirectResponse(f"/vincular-garmin?mfa={mfa_id}", status_code=303)

    return _guardar_vinculo(user_id, api)


@router.post("/vincular-garmin/mfa", response_class=HTMLResponse)
def vincular_mfa(request: Request, mfa_id: str = Form(...), codigo: str = Form(...)):
    user_id = _usuario(request)
    if not user_id:
        return _a_entrar("/vincular-garmin")
    pendiente = _MFA_PENDIENTE.get(mfa_id)
    # El identificador no basta: tiene que ser de quien abrio la verificacion,
    # o valdria para colgarle a otro la cuenta de Garmin de uno.
    if not pendiente or pendiente[0] != user_id:
        return form_garmin(request, error="La verificación ha caducado. Empieza de nuevo.")
    _, api, estado = pendiente
    try:
        api.resume_login(estado, codigo.strip())
    except Exception as exc:
        return form_garmin(request, error=f"Código incorrecto: {exc}"[:150], mfa=mfa_id)
    _MFA_PENDIENTE.pop(mfa_id, None)
    return _guardar_vinculo(user_id, api)


# El estado intermedio del MFA solo vive en memoria y unos minutos: no merece
# una tabla, y menos con la contraseña de Garmin dentro del objeto de sesion.
_MFA_PENDIENTE: dict = {}


def _guardar_vinculo(user_id: str, api) -> HTMLResponse:
    try:
        garmin_client.para_usuario(user_id).guardar_sesion(api)
    except Exception as exc:
        log.exception("Fallo guardando la sesion de Garmin de %s", user_id)
        return _pagina(
            "No se pudo guardar",
            f"<h1>No se pudo guardar la sesión</h1>{_aviso(str(exc)[:150])}"
            f"<a class=boton href='/vincular-garmin'>Volver a intentarlo</a>",
        )
    garmin_client.olvidar(user_id)
    log.info("Garmin vinculado para %s", user_id)
    return _pagina(
        "Listo",
        "<h1>Garmin conectado</h1>"
        "<p class=sub>Ya puedes ver tus métricas en el panel y añadir el conector "
        "en tu asistente.</p>"
        "<div class=ok>Vinculado correctamente.</div>"
        "<a class=boton href='/panel'>Ir a mi panel</a>"
        "<p class=aviso>¿Tienes una báscula inteligente? "
        "<a href='/vincular-bascula'>Vincúlala también</a> y el asistente verá tu "
        "grasa y tu masa muscular, no solo el peso.</p>",
    )


# ------------------------------------------------------------ vinculacion de bascula
@router.get("/vincular-bascula", response_class=HTMLResponse)
def form_bascula(request: Request, error: str = ""):
    user_id = _usuario(request)
    if not user_id:
        return _a_entrar("/vincular-bascula")

    marcas = "".join(
        f"<option value='{html.escape(p['id'])}'>{html.escape(p['name'])} — "
        f"{html.escape(p['description'])}</option>"
        for p in scales.catalogo()
    )
    ya = scales.estado(user_id)
    aviso_actual = (
        f"<div class=ok>Ahora mismo tienes vinculada {html.escape(ya['name'])} "
        f"({html.escape(ya['account'] or '')}). Si vuelves a vincular, la sustituye.</div>"
        if ya else ""
    )
    return _pagina(
        "Vincular báscula",
        f"<h1>Conecta tu báscula</h1>"
        f"<p class=sub>Las mismas credenciales con las que entras en la app de la "
        f"báscula. Se usan una sola vez para obtener un permiso de acceso: la "
        f"contraseña no se guarda.</p>"
        f"{aviso_actual}"
        f"{_aviso(error) if error else ''}"
        f"<form method=post>"
        f"<label for=provider>Marca</label>"
        f"<select id=provider name=provider required>{marcas}</select>"
        f"<label for=email>Email de la cuenta</label>"
        f"<input id=email name=email type=email required autocomplete=off>"
        f"<label for=password>Contraseña</label>"
        f"<input id=password name=password type=password required autocomplete=off>"
        f"<button type=submit>Vincular báscula</button></form>"
        f"<p class=aviso>Los pesajes se leen de la nube del fabricante, así que "
        f"tienen que haberse sincronizado antes desde su app.</p>",
    )


@router.post("/vincular-bascula", response_class=HTMLResponse)
def vincular_bascula(
    request: Request,
    provider: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
):
    user_id = _usuario(request)
    if not user_id:
        return _a_entrar("/vincular-bascula")
    try:
        vinculo = scales.vincular(user_id, provider, email, password)
    except Exception as exc:  # noqa: BLE001
        # El mensaje del fabricante ya es legible ("Account does not exist..."),
        # asi que se enseña tal cual en vez de taparlo con uno generico.
        log.info("Fallo vinculando bascula %s de %s: %s", provider, user_id, exc)
        return form_bascula(request, error=f"No se pudo vincular: {exc}"[:200])

    aparatos = ", ".join(
        " ".join(x for x in (d.get("brand"), d.get("model")) if x) for d in vinculo["devices"]
    )
    return _pagina(
        "Listo",
        f"<h1>Báscula conectada</h1>"
        f"<p class=sub>Tu asistente ya puede ver tu peso y tu composición corporal, "
        f"y seguir cómo evolucionan.</p>"
        f"<div class=ok>{html.escape(vinculo['name'])} vinculada"
        f"{' · ' + html.escape(aparatos) if aparatos else ''}.</div>"
        f"<a class=boton href='/panel'>Ir a mi panel</a>",
    )


@router.post("/desvincular-bascula")
def desvincular_bascula(request: Request):
    user_id = _usuario(request)
    if user_id:
        scales.desvincular(user_id)
        log.info("Bascula desvinculada para %s", user_id)
    return RedirectResponse("/panel", status_code=303)


# ------------------------------------------------------------- vinculacion de Strava
# El estado del OAuth solo vive en memoria y unos minutos, igual que el MFA de
# Garmin: es un numero de un solo uso para que /strava/callback sepa que la
# vuelta viene de una ida que empezo aqui y de que usuario, no un dato que
# merezca sobrevivir a un reinicio.
_STRAVA_STATE: dict[str, tuple[str, float]] = {}
_STRAVA_STATE_TTL = 600  # segundos


@router.get("/vincular-strava", response_class=HTMLResponse)
def form_strava(request: Request, error: str = ""):
    user_id = _usuario(request)
    if not user_id:
        return _a_entrar("/vincular-strava")
    if not strava.habilitado():
        return _pagina(
            "Strava no disponible",
            "<h1>Strava no está configurado</h1><p class=sub>Quien administra "
            "este servidor todavía no ha dado de alta una app de Strava.</p>"
            "<a class=boton href='/panel'>Volver a mi panel</a>",
        )

    ya = strava.estado(user_id)
    aviso_actual = (
        f"<div class=ok>Ahora mismo está vinculada la cuenta de "
        f"{html.escape(ya['athlete'] or 'Strava')}. Si vuelves a conectar, la sustituye.</div>"
        if ya else ""
    )

    import secrets as _s
    codigo_estado = _s.token_urlsafe(24)
    _STRAVA_STATE[codigo_estado] = (user_id, time.time() + _STRAVA_STATE_TTL)

    return _pagina(
        "Conectar Strava",
        f"<h1>Lleva a Strava las series que ya corregiste</h1>"
        f"<p class=sub>Si tu reloj detecta mal una sesión de fuerza (una serie "
        f"donde hiciste veinticinco), Garmin la exporta a Strava tal cual antes "
        f"de que la corrijas. Con esto, tu asistente puede ocultar ahí la "
        f"versión mal detectada y subir la que ya arreglaste en Garmin.</p>"
        f"{aviso_actual}"
        f"{_aviso(error) if error else ''}"
        f"<a class=boton href='{html.escape(strava.url_autorizacion(codigo_estado))}'>"
        f"Conectar con Strava</a>"
        f"<p class=aviso>Se abre en strava.com: tu contraseña de Strava no pasa "
        f"por aquí en ningún momento, solo el permiso que autorices allí.</p>",
    )


@router.get("/strava/callback", response_class=HTMLResponse)
def strava_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    user_id = _usuario(request)
    if not user_id:
        return _a_entrar("/vincular-strava")

    pendiente = _STRAVA_STATE.pop(state, None) if state else None
    if error:
        return form_strava(request, error="Strava no concedió el permiso." if error == "access_denied"
                            else f"Strava devolvió un error: {error}"[:150])
    # El estado tiene que ser uno que se emitio para ESTE usuario: sin la
    # comprobacion, alguien podria colgarle a otro su propia cuenta de Strava
    # simplemente adivinando o reutilizando un enlace de autorizacion ajeno.
    if not pendiente or pendiente[0] != user_id or pendiente[1] < time.time():
        return form_strava(request, error="El enlace de autorización ha caducado. Empieza de nuevo.")
    if not code:
        return form_strava(request, error="Strava no devolvió ningún código de autorización.")

    try:
        vinculo = strava.vincular(user_id, code)
    except strava.ErrorDeStrava as exc:
        log.info("Fallo vinculando Strava de %s: %s", user_id, exc)
        return form_strava(request, error=f"No se pudo vincular: {exc}"[:200])

    log.info("Strava vinculado para %s (atleta %s)", user_id, vinculo.get("athlete_id"))
    return _pagina(
        "Listo",
        f"<h1>Strava conectado</h1>"
        f"<div class=ok>{html.escape(vinculo['athlete'] or 'Cuenta')} vinculada.</div>"
        f"<p class=sub>Tu asistente ya puede corregir en Strava las sesiones de "
        f"fuerza que arregles en Garmin.</p>"
        f"<a class=boton href='/panel'>Ir a mi panel</a>",
    )


@router.post("/desvincular-strava")
def desvincular_strava(request: Request):
    user_id = _usuario(request)
    if user_id:
        strava.desvincular(user_id)
        log.info("Strava desvinculado para %s", user_id)
    return RedirectResponse("/panel", status_code=303)
