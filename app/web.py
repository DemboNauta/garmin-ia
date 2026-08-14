"""Paginas web: alta por invitacion, inicio de sesion y vinculacion de cuentas.

Por que hay HTML en un servidor MCP: las credenciales de Garmin o de la bascula
NO pueden pedirse con una herramienta MCP. Si lo hicieran, la contraseña del
usuario pasaria por el contexto del modelo y quedaria en el historial de la
conversacion. Aqui van directas del navegador al servidor.
"""
from __future__ import annotations

import html
import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import accounts, garmin_client, scales, store
from .config import settings
from .oauth_provider import proveedor

log = logging.getLogger(__name__)
router = APIRouter()

_ESTILO = """
:root {
  color-scheme: light dark;
  --fondo: #fafafa; --papel: #fff; --borde: #e7e7ec; --texto: #17171b;
  --suave: #6c6c78; --acento: #4f46e5; --acento-fuerte: #4338ca;
  --codigo-fondo: #f4f4f6;
}
@media (prefers-color-scheme: dark) {
  :root { --fondo: #0f0f12; --papel: #17171b; --borde: #26262e; --texto: #ededf2;
          --suave: #9a9aa8; --acento: #7c74ff; --acento-fuerte: #938cff;
          --codigo-fondo: #101014; }
}
* { box-sizing: border-box; }
body { font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
       margin: 0; min-height: 100vh; padding: 2rem 1.25rem; background: var(--fondo);
       color: var(--texto); line-height: 1.55; -webkit-font-smoothing: antialiased;
       display: flex; flex-direction: column; align-items: center; }
main { width: 100%; max-width: 27rem; }
main.ancho { max-width: 44rem; }
.tarjeta { background: var(--papel); border: 1px solid var(--borde); border-radius: 16px;
           padding: 2rem; box-shadow: 0 1px 2px rgba(0,0,0,.04); }
.marca { display: flex; align-items: center; gap: .6rem; font-weight: 650;
         letter-spacing: -.01em; margin-bottom: 1.75rem; font-size: .95rem; }
.punto { width: 1.6rem; height: 1.6rem; border-radius: 7px; flex: none;
         background: linear-gradient(135deg, var(--acento), #8b5cf6); }
h1 { font-size: 1.4rem; line-height: 1.25; margin: 0 0 .5rem; letter-spacing: -.02em; }
h2 { font-size: 1rem; margin: 2rem 0 .6rem; letter-spacing: -.01em; }
p { margin: 0 0 1rem; }
p.sub { color: var(--suave); font-size: .93rem; margin-bottom: 1.75rem; }
label { display: block; font-size: .8rem; font-weight: 600; margin: 1.1rem 0 .4rem; }
input, select { width: 100%; padding: .7rem .8rem; font-size: 1rem; border-radius: 9px;
        border: 1px solid var(--borde); background: var(--fondo); color: var(--texto);
        transition: border-color .15s; }
input:focus, select:focus { outline: none; border-color: var(--acento);
              box-shadow: 0 0 0 3px color-mix(in srgb, var(--acento) 18%, transparent); }
button { width: 100%; margin-top: 1.6rem; padding: .75rem; font-size: .95rem;
         font-weight: 600; color: #fff; background: var(--acento); border: 0;
         border-radius: 9px; cursor: pointer; transition: background .15s; }
button:hover { background: var(--acento-fuerte); }
.error, .ok { padding: .75rem .9rem; font-size: .87rem; border-radius: 9px;
              margin-bottom: 1.25rem; }
.error { background: color-mix(in srgb, #ef4444 12%, transparent); color: #dc2626; }
.ok { background: color-mix(in srgb, #22c55e 14%, transparent); color: #16a34a; }
.aviso { font-size: .8rem; color: var(--suave); margin: 1.5rem 0 0;
         border-top: 1px solid var(--borde); padding-top: 1.1rem; }
ol { padding-left: 1.1rem; margin: 0 0 1rem; }
ol li { margin-bottom: .9rem; }
ol li::marker { color: var(--suave); font-variant-numeric: tabular-nums; }
code, .url { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: .86em;
             background: var(--codigo-fondo); border: 1px solid var(--borde);
             padding: .12em .4em; border-radius: 6px; }
.url { display: block; padding: .7rem .85rem; margin: .6rem 0 0; word-break: break-all;
       font-size: .85rem; }
.rejilla { display: grid; gap: .8rem; grid-template-columns: repeat(auto-fit, minmax(11rem, 1fr));
           margin: 1.25rem 0 0; }
.dato { border: 1px solid var(--borde); border-radius: 11px; padding: .85rem .95rem;
        background: var(--fondo); }
.dato b { display: block; font-size: .82rem; margin-bottom: .15rem; }
.dato span { font-size: .8rem; color: var(--suave); }
footer { margin: 2rem 0 0; font-size: .78rem; color: var(--suave); text-align: center; }
"""

_MARCA = "<div class=marca><span class=punto></span>Garmin Bridge</div>"


def _pagina(titulo: str, cuerpo: str, codigo: int = 200, ancho: bool = False) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><html lang=es><meta charset=utf-8>"
        f"<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(titulo)}</title><style>{_ESTILO}</style>"
        f"<body><main class='{'ancho' if ancho else ''}'>"
        f"<div class=tarjeta>{_MARCA}{cuerpo}</div></main>",
        status_code=codigo,
    )


def _aviso(texto: str, clase: str = "error") -> str:
    return f"<div class={clase}>{html.escape(texto)}</div>"


@router.get("/", response_class=HTMLResponse)
def portada() -> HTMLResponse:
    url_mcp = f"{settings.public_url}/mcp"
    return _pagina(
        "Garmin Bridge",
        f"<h1>Tus datos de Garmin, dentro de tu asistente</h1>"
        f"<p class=sub>Conecta Garmin Connect con Claude o ChatGPT. El asistente "
        f"lee tu sueño, tu HRV y tu recuperación, y puede escribir entrenamientos "
        f"estructurados directamente en tu cuenta.</p>"
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
        f"<p class=aviso>En Claude: <code>Configuración → Conectores → Añadir "
        f"conector personalizado</code>. En ChatGPT, en los ajustes de conectores. "
        f"Tu contraseña de Garmin se usa una sola vez para obtener un permiso de "
        f"acceso y no se guarda; el permiso queda cifrado en el servidor.</p>",
        ancho=True,
    )


# ---------------------------------------------------------------------- admin
_COOKIE = "gb_admin"


def _cookie_segura(respuesta, token: str) -> None:
    respuesta.set_cookie(
        _COOKIE,
        token,
        httponly=True,                                  # fuera del alcance de JS
        secure=settings.public_url.startswith("https"),  # solo por TLS
        samesite="strict",                              # corta el CSRF entre sitios
        max_age=12 * 3600,
        path="/",
    )


@router.get("/admin", response_class=HTMLResponse)
def panel(request: Request, error: str = "", nuevo: str = "") -> HTMLResponse:
    user_id = accounts.usuario_de_sesion(request.cookies.get(_COOKIE))
    if not user_id:
        return _pagina(
            "Administración",
            f"<h1>Panel de administración</h1>"
            f"<p class=sub>Entra con tu cuenta.</p>"
            f"{_aviso(error) if error else ''}"
            f"<form method=post action='/admin/entrar'>"
            f"<label for=email>Email</label>"
            f"<input id=email name=email type=email required autocomplete=username>"
            f"<label for=password>Contraseña</label>"
            f"<input id=password name=password type=password required "
            f"autocomplete=current-password>"
            f"<button type=submit>Entrar</button></form>",
        )

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
        f"<form method=post action='/admin/salir'>"
        f"<button type=submit style='background:transparent;color:var(--suave);"
        f"border:1px solid var(--borde)'>Cerrar sesión</button></form>",
        ancho=True,
    )


@router.post("/admin/entrar")
def admin_entrar(request: Request, email: str = Form(...), password: str = Form(...)):
    user_id = accounts.autenticar(email, password)
    if not user_id or not store.es_admin(user_id):
        # Mismo mensaje si la contraseña falla o si no es administrador: no hay
        # que confirmarle a nadie que ha acertado con una credencial.
        return panel(request, error="Credenciales incorrectas o sin permiso.")
    respuesta = RedirectResponse("/admin", status_code=303)
    _cookie_segura(respuesta, accounts.crear_sesion_admin(user_id))
    return respuesta


@router.post("/admin/invitar")
def admin_invitar(request: Request, email: str = Form(default="")):
    if not accounts.usuario_de_sesion(request.cookies.get(_COOKIE)):
        return RedirectResponse("/admin", status_code=303)
    codigo = accounts.crear_invitacion(email.strip() or None)
    # El codigo viaja una vez en la URL para poder enseñarlo; despues solo queda
    # su hash en la base y no hay forma de recuperarlo.
    return RedirectResponse(f"/admin?nuevo={codigo}", status_code=303)


@router.post("/admin/salir")
def admin_salir(request: Request):
    accounts.cerrar_sesion(request.cookies.get(_COOKIE))
    respuesta = RedirectResponse("/admin", status_code=303)
    respuesta.delete_cookie(_COOKIE, path="/")
    return respuesta


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
def crear_cuenta(codigo: str, email: str = Form(...), password: str = Form(...)) -> HTMLResponse:
    try:
        user_id = accounts.registrar_con_invitacion(codigo, email, password)
    except accounts.ErrorDeCuenta as exc:
        return form_invitacion(codigo, error=str(exc))
    log.info("Cuenta creada: %s", user_id)
    return RedirectResponse(f"/vincular-garmin?u={user_id}", status_code=303)


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
        # Mismo mensaje para email inexistente y contraseña mala: si no, se
        # podria averiguar quien tiene cuenta probando emails.
        return form_login(req, error="Email o contraseña incorrectos.")
    try:
        destino = proveedor.completar_autorizacion(req, user_id)
    except ValueError as exc:
        return form_login(req, error=str(exc))
    return RedirectResponse(destino, status_code=303)


# -------------------------------------------------------- vinculacion de Garmin
@router.get("/vincular-garmin", response_class=HTMLResponse)
def form_garmin(u: str = "", error: str = "", mfa: str = "") -> HTMLResponse:
    if not u or not store.existe_usuario(u):
        return _pagina("Usuario desconocido", "<h1>Usuario desconocido</h1>", codigo=404)
    if mfa:
        return _pagina(
            "Código de verificación",
            f"<h1>Código de Garmin</h1>"
            f"<p class=sub>Garmin ha enviado un código a tu email o teléfono.</p>"
            f"{_aviso(error) if error else ''}"
            f"<form method=post action='/vincular-garmin/mfa'>"
            f"<input type=hidden name=u value='{html.escape(u)}'>"
            f"<input type=hidden name=mfa_id value='{html.escape(mfa)}'>"
            f"<label for=codigo>Código</label>"
            f"<input id=codigo name=codigo required inputmode=numeric autocomplete=one-time-code>"
            f"<button type=submit>Confirmar</button></form>",
        )
    return _pagina(
        "Vincular Garmin",
        f"<h1>Conecta tu cuenta de Garmin</h1>"
        f"<p class=sub>Se usa una sola vez para obtener un permiso de acceso. "
        f"Tu contraseña no se guarda en ningún momento.</p>"
        f"{_aviso(error) if error else ''}"
        f"<form method=post>"
        f"<input type=hidden name=u value='{html.escape(u)}'>"
        f"<label for=email>Email de Garmin Connect</label>"
        f"<input id=email name=email type=email required autocomplete=off>"
        f"<label for=password>Contraseña de Garmin</label>"
        f"<input id=password name=password type=password required autocomplete=off>"
        f"<button type=submit>Vincular</button></form>"
        f"<p class=aviso>Garmin limita los intentos por IP. Si falla, espera un "
        f"rato antes de reintentar en vez de insistir.</p>",
    )


@router.post("/vincular-garmin", response_class=HTMLResponse)
def vincular(u: str = Form(...), email: str = Form(...), password: str = Form(...)):
    from garminconnect import Garmin

    if not store.existe_usuario(u):
        return _pagina("Usuario desconocido", "<h1>Usuario desconocido</h1>", codigo=404)

    api = Garmin(email, password, return_on_mfa=True)
    try:
        resultado = api.login()
    except Exception as exc:
        return form_garmin(u, error=f"Garmin rechazó el acceso: {exc}"[:200])

    if isinstance(resultado, tuple) and resultado and resultado[0] == "needs_mfa":
        import secrets as _s

        mfa_id = _s.token_urlsafe(16)
        _MFA_PENDIENTE[mfa_id] = (u, api, resultado[1])
        return RedirectResponse(f"/vincular-garmin?u={u}&mfa={mfa_id}", status_code=303)

    return _guardar_vinculo(u, api)


@router.post("/vincular-garmin/mfa", response_class=HTMLResponse)
def vincular_mfa(u: str = Form(...), mfa_id: str = Form(...), codigo: str = Form(...)):
    pendiente = _MFA_PENDIENTE.get(mfa_id)
    if not pendiente or pendiente[0] != u:
        return form_garmin(u, error="La verificación ha caducado. Empieza de nuevo.")
    _, api, estado = pendiente
    try:
        api.resume_login(estado, codigo.strip())
    except Exception as exc:
        return form_garmin(u, error=f"Código incorrecto: {exc}"[:150], mfa=mfa_id)
    _MFA_PENDIENTE.pop(mfa_id, None)
    return _guardar_vinculo(u, api)


# El estado intermedio del MFA solo vive en memoria y unos minutos: no merece
# una tabla, y menos con la contraseña de Garmin dentro del objeto de sesion.
_MFA_PENDIENTE: dict = {}


def _guardar_vinculo(user_id: str, api) -> HTMLResponse:
    try:
        garmin_client.para_usuario(user_id).guardar_sesion(api)
    except Exception as exc:
        log.exception("Fallo guardando la sesion de Garmin de %s", user_id)
        return form_garmin(user_id, error=f"No se pudo guardar la sesión: {exc}"[:150])
    garmin_client.olvidar(user_id)
    log.info("Garmin vinculado para %s", user_id)
    return _pagina(
        "Listo",
        "<h1>Garmin conectado</h1>"
        "<p class=sub>Ya puedes volver a tu asistente y añadir el conector. "
        "Tus métricas estarán disponibles ahí.</p>"
        f"<div class=ok>Vinculado correctamente.</div>"
        f"<p class=aviso>¿Tienes una báscula inteligente? "
        f"<a href='/vincular-bascula?u={html.escape(user_id)}'>Vincúlala también</a> "
        f"y el asistente verá tu grasa y tu masa muscular, no solo el peso.</p>",
    )


# ------------------------------------------------------------ vinculacion de bascula
@router.get("/vincular-bascula", response_class=HTMLResponse)
def form_bascula(u: str = "", error: str = "") -> HTMLResponse:
    if not u or not store.existe_usuario(u):
        return _pagina("Usuario desconocido", "<h1>Usuario desconocido</h1>", codigo=404)

    marcas = "".join(
        f"<option value='{html.escape(p['id'])}'>{html.escape(p['name'])} — "
        f"{html.escape(p['description'])}</option>"
        for p in scales.catalogo()
    )
    ya = scales.estado(u)
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
        f"<input type=hidden name=u value='{html.escape(u)}'>"
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
    u: str = Form(...),
    provider: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
):
    if not store.existe_usuario(u):
        return _pagina("Usuario desconocido", "<h1>Usuario desconocido</h1>", codigo=404)
    try:
        vinculo = scales.vincular(u, provider, email, password)
    except Exception as exc:  # noqa: BLE001
        # El mensaje del fabricante ya es legible ("Account does not exist..."),
        # asi que se enseña tal cual en vez de taparlo con uno generico.
        log.info("Fallo vinculando bascula %s de %s: %s", provider, u, exc)
        return form_bascula(u, error=f"No se pudo vincular: {exc}"[:200])

    aparatos = ", ".join(
        " ".join(x for x in (d.get("brand"), d.get("model")) if x) for d in vinculo["devices"]
    )
    return _pagina(
        "Listo",
        f"<h1>Báscula conectada</h1>"
        f"<p class=sub>Tu asistente ya puede ver tu peso y tu composición corporal, "
        f"y seguir cómo evolucionan.</p>"
        f"<div class=ok>{html.escape(vinculo['name'])} vinculada"
        f"{' · ' + html.escape(aparatos) if aparatos else ''}.</div>",
    )
