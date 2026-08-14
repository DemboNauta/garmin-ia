/* Panel de Garmin Bridge.
 *
 * Sin dependencias ni compilacion, a proposito: el servidor es Python puro y
 * mete un build de node en el despliegue solo por pintar seis pantallas no
 * compensa. Todo lo que hay aqui es una funcion por vista que pide su JSON y
 * devuelve HTML.
 *
 * Regla que no se salta nunca: todo lo que venga del servidor pasa por esc()
 * antes de tocar innerHTML. Los nombres de actividad los escribe la persona en
 * Garmin y los analisis los escribe un modelo, asi que ninguno de los dos es
 * texto de confianza. */
"use strict";

const API = "/panel/api";
const APP = { estado: null, vista: null, pintadas: new Set() };
const cache = new Map();

/* --------------------------------------------------------------- utilidades */
function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

async function pedir(ruta, opciones) {
  const r = await fetch(ruta, { credentials: "same-origin", ...opciones });
  if (r.status === 401) {
    location.href = "/entrar?destino=/panel";
    throw new Error("sesión caducada");
  }
  const cuerpo = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(cuerpo.detail || `Error ${r.status}`);
  return cuerpo;
}

/* Una llamada por URL mientras dure la visita. El boton de sincronizar es lo
   unico que la vacia: sin eso, cambiar de pestaña y volver repetiria consultas
   que tardan segundos porque van contra la nube de Garmin. */
function pedirCache(ruta) {
  if (!cache.has(ruta)) {
    cache.set(ruta, pedir(ruta).catch((e) => { cache.delete(ruta); throw e; }));
  }
  return cache.get(ruta);
}

function avisar(texto, malo) {
  const caja = document.getElementById("aviso-flotante");
  caja.textContent = texto;
  caja.className = "flotante" + (malo ? " malo" : "");
  caja.hidden = false;
  clearTimeout(avisar.reloj);
  avisar.reloj = setTimeout(() => { caja.hidden = true; }, 4000);
}

/* ------------------------------------------------------------------ formato */
const NUM = (v, dec = 0) =>
  v == null || Number.isNaN(v) ? "—" : v.toLocaleString("es-ES", {
    minimumFractionDigits: dec, maximumFractionDigits: dec,
  });

const MESES = ["ene", "feb", "mar", "abr", "may", "jun",
               "jul", "ago", "sep", "oct", "nov", "dic"];

function fecha(iso) {
  if (!iso) return "—";
  const d = new Date(iso.slice(0, 10) + "T00:00:00");
  return `${d.getDate()} ${MESES[d.getMonth()]}`;
}

function fechaLarga(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleDateString("es-ES", {
    weekday: "long", day: "numeric", month: "long",
  });
}

function haceCuanto(iso) {
  const minutos = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (minutos < 60) return `hace ${Math.max(1, minutos)} min`;
  if (minutos < 1440) return `hace ${Math.round(minutos / 60)} h`;
  const dias = Math.round(minutos / 1440);
  return dias === 1 ? "ayer" : `hace ${dias} días`;
}

/* La escala de recuperacion de WHOOP: verde por encima de dos tercios, ambar
   en la banda media, rojo abajo. Es lo unico que hace falta leer de lejos. */
function colorRecuperacion(v) {
  if (v == null) return "var(--apagado)";
  if (v >= 67) return "var(--verde)";
  if (v >= 34) return "var(--amarillo)";
  return "var(--rojo)";
}

const media = (xs) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null);

function mediaDe(dias, campo, excluir) {
  const xs = dias
    .filter((d) => d.date !== excluir)
    .map((d) => d[campo])
    .filter((v) => typeof v === "number");
  return media(xs);
}

/* ---------------------------------------------------------------- anillos */
const RADIO = 50;
const VUELTA = 2 * Math.PI * RADIO;

function anillo({ valor, max = 100, color, nombre, nota, unidad, decimales = 0 }) {
  const fraccion = valor == null ? 0 : Math.max(0, Math.min(1, valor / max));
  const hueco = VUELTA * (1 - fraccion);
  return `
    <div class=anillo>
      <svg viewBox="0 0 120 120" role=img aria-label="${esc(nombre)}: ${NUM(valor, decimales)}">
        <circle class=pista cx=60 cy=60 r=${RADIO} fill=none stroke-width=9></circle>
        <circle class=arco cx=60 cy=60 r=${RADIO} fill=none stroke-width=9
                style="stroke:${color}" stroke-dasharray="${VUELTA.toFixed(1)}"
                stroke-dashoffset="${VUELTA.toFixed(1)}" data-hueco="${hueco.toFixed(1)}"></circle>
        <text class=valor x=60 y="${unidad ? 58 : 64}" text-anchor=middle
              dominant-baseline=middle>${NUM(valor, decimales)}</text>
        ${unidad ? `<text class=unidad x=60 y=78 text-anchor=middle>${esc(unidad)}</text>` : ""}
      </svg>
      <div class="nombre rotulo">${esc(nombre)}</div>
      <div class=nota>${esc(nota || "")}</div>
    </div>`;
}

function animarAnillos(raiz) {
  requestAnimationFrame(() => {
    raiz.querySelectorAll(".arco[data-hueco]").forEach((arco) => {
      arco.style.strokeDashoffset = arco.dataset.hueco;
    });
  });
}

/* --------------------------------------------------------------- graficas */
/* Barras de una metrica a lo largo del periodo. El SVG se estira en horizontal
   (preserveAspectRatio=none) para ocupar el ancho que haya: son rectangulos,
   asi que deformarlos no se nota, y a cambio no hay que medir el contenedor.
   Por eso mismo aqui dentro no va ni una letra: el texto si se deformaria. */
function barras(dias, campo, color, opciones = {}) {
  const alto = 64;
  const paso = 10;
  const valores = dias.map((d) => (typeof d[campo] === "number" ? d[campo] : null));
  const presentes = valores.filter((v) => v != null);
  if (!presentes.length) return `<p class=nota-vacia>Sin datos en el periodo.</p>`;

  const tope = Math.max(...presentes);
  // El suelo no siempre es cero: en FC en reposo o en HRV, empezar en cero
  // aplasta las barras contra el techo y no se ve la variacion, que es justo
  // lo unico que interesa mirar.
  const suelo = opciones.desdeCero ? 0 : Math.min(...presentes) * 0.92;
  const rango = tope - suelo || 1;
  const promedio = media(presentes);
  const yMedia = alto - ((promedio - suelo) / rango) * alto;

  const rects = valores.map((v, i) => {
    if (v == null) return "";
    const h = Math.max(2, ((v - suelo) / rango) * alto);
    const col = typeof color === "function" ? color(v) : color;
    return `<rect class=barra-dia x="${i * paso}" y="${(alto - h).toFixed(1)}" `
         + `width="${paso - 3.5}" height="${h.toFixed(1)}" style="fill:${col}">`
         + `<title>${esc(fecha(dias[i].date))}: ${NUM(v, opciones.decimales || 0)}</title></rect>`;
  }).join("");

  return `<svg viewBox="0 0 ${dias.length * paso} ${alto}" preserveAspectRatio=none
               style="height:${alto}px">
    <line class=guia x1=0 x2="${dias.length * paso}" y1="${yMedia.toFixed(1)}"
          y2="${yMedia.toFixed(1)}" vector-effect=non-scaling-stroke></line>
    ${rects}</svg>`;
}

function linea(puntos, color, opciones = {}) {
  const alto = 90;
  const paso = 10;
  const valores = puntos.map((p) => p.valor);
  if (valores.length < 2) return `<p class=nota-vacia>Hacen falta al menos dos medidas.</p>`;
  const tope = Math.max(...valores);
  const suelo = Math.min(...valores);
  const rango = tope - suelo || 1;
  const margen = alto * 0.12;
  const y = (v) => alto - margen - ((v - suelo) / rango) * (alto - margen * 2);
  const d = puntos.map((p, i) => `${i === 0 ? "M" : "L"}${i * paso},${y(p.valor).toFixed(1)}`).join(" ");
  const ultimo = puntos.length - 1;
  return `<svg viewBox="0 0 ${ultimo * paso} ${alto}" preserveAspectRatio=none
               style="height:${alto}px">
    <path d="${d}" fill=none style="stroke:${color}" stroke-width=2
          stroke-linejoin=round vector-effect=non-scaling-stroke></path>
    <circle cx="${ultimo * paso}" cy="${y(puntos[ultimo].valor).toFixed(1)}" r=3
            style="fill:${color}" vector-effect=non-scaling-stroke></circle>
  </svg>
  <div class=grafica-pie><span>${esc(fecha(puntos[0].fecha))}</span>
  <span>${NUM(suelo, opciones.decimales ?? 1)}–${NUM(tope, opciones.decimales ?? 1)}
  ${esc(opciones.unidad || "")}</span>
  <span>${esc(fecha(puntos[ultimo].fecha))}</span></div>`;
}

/* --------------------------------------------------------------- metricas */
/* La comparacion con la media llega despues que el numero: pedirla obliga a
   bajar treinta dias de Garmin y eso son segundos, mientras que el valor de hoy
   ya esta. Por eso la baldosa se pinta con el hueco del delta vacio y se
   rellena luego con aplicarBases(). */
function metrica({ nombre, valor, unidad, decimales = 0, campo, mejorArriba = true }) {
  const comparable = campo && typeof valor === "number";
  const datos = comparable
    ? ` data-campo="${campo}" data-valor="${valor}" data-dec="${decimales}"`
      + ` data-arriba="${mejorArriba ? 1 : 0}"`
    : "";
  return `<div class=metrica${datos}><b>${esc(nombre)}</b>
    <strong>${NUM(valor, decimales)}${unidad ? `<small>${esc(unidad)}</small>` : ""}</strong>
    ${comparable ? `<span class=delta></span>` : ""}</div>`;
}

/* `mejorArriba` decide el color y no es cosmetico: en HRV subir es buena señal
   y en frecuencia en reposo es mala, asi que sin distinguirlo el verde acabaria
   felicitando por lo contrario. */
function aplicarBases(seccion, dias, excluir) {
  seccion.querySelectorAll(".metrica[data-campo]").forEach((baldosa) => {
    const hueco = baldosa.querySelector(".delta");
    if (!hueco) return;
    const base = mediaDe(dias, baldosa.dataset.campo, excluir);
    if (base == null || base === 0) return;
    const dec = Number(baldosa.dataset.dec);
    const dif = Number(baldosa.dataset.valor) - base;
    if (Math.abs(dif) < Math.max(0.05, Math.abs(base) * 0.015)) {
      hueco.className = "delta";
      hueco.textContent = "en tu media";
      return;
    }
    const bien = (baldosa.dataset.arriba === "1") === (dif > 0);
    hueco.className = `delta ${bien ? "bien" : "mal"}`;
    hueco.textContent = `${dif > 0 ? "▲" : "▼"} ${NUM(Math.abs(dif), dec)} vs. tu media`;
  });
}

/* Rellena los dias que faltan del rango pedido. La API solo devuelve los que
   tienen datos, y sin los huecos un mes con diez dias medidos se dibujaria como
   diez barras anchisimas en vez de enseñar lo que de verdad hay. */
function rellenar(dias, desde, hasta) {
  if (!desde || !hasta) return dias;
  const porFecha = new Map(dias.map((d) => [d.date, d]));
  const salida = [];
  const cursor = new Date(`${desde}T00:00:00`);
  const fin = new Date(`${hasta}T00:00:00`);
  while (cursor <= fin) {
    const iso = `${cursor.getFullYear()}-${String(cursor.getMonth() + 1).padStart(2, "0")}`
              + `-${String(cursor.getDate()).padStart(2, "0")}`;
    salida.push(porFecha.get(iso) || { date: iso });
    cursor.setDate(cursor.getDate() + 1);
  }
  return salida;
}

function sinGarmin(nota) {
  return `<div class=vacio><b>Garmin no está vinculado</b>
    <p>${esc(nota || "Conecta tu cuenta de Garmin Connect y aquí aparecerán tus métricas.")}</p>
    <a class=boton href="/vincular-garmin">Vincular Garmin</a></div>`;
}

/* ====================================================================== HOY */
async function pintarHoy(seccion) {
  const hoy = await pedirCache(`${API}/hoy`);

  if (hoy.garmin_linked === false) { seccion.innerHTML = sinGarmin(hoy.note); return; }
  if (!hoy.day) {
    seccion.innerHTML = `<div class=vacio><b>Todavía no hay datos de hoy</b>
      <p>Garmin aún no ha recibido nada del día. Sincroniza el reloj desde su app
      y vuelve dentro de un rato.</p></div>`;
    return;
  }

  const d = hoy.day;
  const estado = d.training_status || {};

  seccion.innerHTML = `
    <div class=encabezado>
      <div>
        <h1>${esc(saludo())}</h1>
        <div class=fecha>${esc(fechaLarga(d.date))}</div>
      </div>
      ${hoy.stale ? `<span class=desfasado>● Último día con datos: ${esc(fecha(d.date))}</span>` : ""}
    </div>

    <div class=anillos>
      ${anillo({
        valor: d.training_readiness, nombre: "Recuperación",
        color: colorRecuperacion(d.training_readiness),
        nota: estado.trainingStatus ? String(estado.trainingStatus).toLowerCase() : "readiness de Garmin",
      })}
      ${anillo({
        valor: d.sleep_score ?? (d.sleep_hours != null ? Math.round(d.sleep_hours / 8 * 100) : null),
        nombre: "Sueño", color: "var(--azul)",
        nota: d.sleep_hours != null ? `${NUM(d.sleep_hours, 1)} h dormidas` : "sin registro",
      })}
      ${anillo({
        valor: d.body_battery_high, nombre: "Body Battery", color: "var(--violeta)",
        nota: d.body_battery_low != null ? `mínimo ${NUM(d.body_battery_low)}` : "",
      })}
    </div>

    <div class=titulo-seccion>Lo que dice tu asistente</div>
    <div id=destacados><div class=esqueleto style="height:120px"></div></div>

    <div class=titulo-seccion>Recuperación</div>
    <div class=metricas>
      ${metrica({ nombre: "HRV nocturna", valor: d.hrv_last_night, unidad: "ms",
                  campo: "hrv_last_night" })}
      ${metrica({ nombre: "FC en reposo", valor: d.resting_hr, unidad: "ppm",
                  campo: "resting_hr", mejorArriba: false })}
      ${metrica({ nombre: "Sueño", valor: d.sleep_hours, unidad: "h", decimales: 1,
                  campo: "sleep_hours" })}
      ${metrica({ nombre: "Sueño profundo", valor: d.deep_sleep_hours, unidad: "h",
                  decimales: 1, campo: "deep_sleep_hours" })}
      ${metrica({ nombre: "REM", valor: d.rem_sleep_hours, unidad: "h", decimales: 1,
                  campo: "rem_sleep_hours" })}
      ${metrica({ nombre: "Estrés medio", valor: d.avg_stress,
                  campo: "avg_stress", mejorArriba: false })}
    </div>

    <div class=titulo-seccion>Carga del día</div>
    <div class=metricas>
      ${metrica({ nombre: "Pasos", valor: d.steps, campo: "steps" })}
      ${metrica({ nombre: "Minutos de intensidad", valor: d.intensity_minutes,
                  campo: "intensity_minutes" })}
      ${metrica({ nombre: "Calorías totales", valor: d.total_kcal, unidad: "kcal",
                  campo: "total_kcal" })}
      ${metrica({ nombre: "Calorías activas", valor: d.active_kcal, unidad: "kcal",
                  campo: "active_kcal" })}
      ${metrica({ nombre: "FC máxima", valor: d.max_hr, unidad: "ppm" })}
      ${metrica({ nombre: "VO₂ máx.", valor: d.vo2max, decimales: 1 })}
    </div>
    ${estado.trainingStatus ? `<p class=aviso>Estado de entrenamiento según Garmin:
      <b>${esc(String(estado.trainingStatus).toLowerCase())}</b>.
      ${estado.trainingStatusFeedbackPhrase
        ? esc(String(estado.trainingStatusFeedbackPhrase).replaceAll("_", " ").toLowerCase()) : ""}</p>` : ""}
  `;
  animarAnillos(seccion);

  // Las dos piezas que faltan van por su cuenta y no bloquean lo de arriba: los
  // analisis salen de la base local (instantaneos) y las medias obligan a bajar
  // treinta dias de Garmin, que es lo unico lento de esta pantalla.
  pintarDestacados(seccion);
  pedirCache(`${API}/metricas?dias=30`)
    .then((m) => aplicarBases(seccion, m.days || [], d.date))
    .catch(() => {});
}

async function pintarDestacados(seccion) {
  const caja = seccion.querySelector("#destacados");
  if (!caja) return;
  try {
    const { insights: lista } = await pedirCache(`${API}/insights?limite=3`);
    caja.innerHTML = lista.length
      ? lista.map((i) => tarjetaInsight(i, { resumido: true })).join("")
        + `<button class=fantasma data-ir=analisis>Ver todos los análisis</button>`
      : `<div class="tarjeta-panel">
          <p style="margin:0;color:var(--tenue);font-size:.92rem">Tu asistente todavía
          no ha dejado nada por escrito. Pídeselo en la conversación —«mira cómo he
          dormido esta semana y déjamelo apuntado»— y aparecerá aquí.</p></div>`;
    const ir = caja.querySelector("[data-ir]");
    if (ir) ir.addEventListener("click", () => mostrar("analisis"));
  } catch (e) {
    caja.innerHTML = `<p class=nota-vacia>No se pudieron cargar los análisis: ${esc(e.message)}</p>`;
  }
}

function saludo() {
  const h = new Date().getHours();
  if (h < 6) return "Buenas noches";
  if (h < 14) return "Buenos días";
  if (h < 21) return "Buenas tardes";
  return "Buenas noches";
}

/* =============================================================== TENDENCIAS */
const SERIES = [
  { campo: "training_readiness", nombre: "Recuperación", color: colorRecuperacion, desdeCero: true },
  { campo: "sleep_hours", nombre: "Sueño", unidad: "h", decimales: 1, color: "var(--azul)", desdeCero: true },
  { campo: "hrv_last_night", nombre: "HRV nocturna", unidad: "ms", color: "var(--violeta)" },
  { campo: "resting_hr", nombre: "FC en reposo", unidad: "ppm", color: "var(--rojo)" },
  { campo: "body_battery_high", nombre: "Body Battery máx.", color: "var(--amarillo)", desdeCero: true },
  { campo: "avg_stress", nombre: "Estrés medio", color: "var(--amarillo)", desdeCero: true },
  { campo: "steps", nombre: "Pasos", color: "var(--verde)", desdeCero: true },
  { campo: "total_kcal", nombre: "Calorías totales", unidad: "kcal", color: "var(--verde)", desdeCero: true },
  { campo: "intensity_minutes", nombre: "Minutos de intensidad", unidad: "min", color: "var(--azul)", desdeCero: true },
];

async function pintarTendencias(seccion, dias = 30) {
  seccion.innerHTML = `<div class=esqueleto></div>`;
  const datos = await pedirCache(`${API}/metricas?dias=${dias}`);
  if (datos.garmin_linked === false) { seccion.innerHTML = sinGarmin(datos.note); return; }

  const serie = rellenar(datos.days || [], datos.from, datos.to);
  const opciones = [7, 30, 90].map((n) =>
    `<button class="pestana ${n === dias ? "activa" : ""}" data-dias="${n}">${n} días</button>`
  ).join("");

  const graficas = SERIES.map((s) => {
    const presentes = serie.map((d) => d[s.campo]).filter((v) => typeof v === "number");
    const ultimo = [...serie].reverse().find((d) => typeof d[s.campo] === "number");
    return `<div class="grafica tarjeta-panel">
      <div class=grafica-cabeza>
        <div><div class=rotulo>${esc(s.nombre)}</div>
          <div class=ahora>${NUM(ultimo ? ultimo[s.campo] : null, s.decimales || 0)}
          <small>${esc(s.unidad || "")}</small></div></div>
        <div class=media>media ${NUM(media(presentes), s.decimales || 0)}
          ${esc(s.unidad || "")} · ${presentes.length}/${serie.length} días</div>
      </div>
      ${barras(serie, s.campo, s.color, { decimales: s.decimales, desdeCero: s.desdeCero })}
    </div>`;
  }).join("");

  seccion.innerHTML = `
    <div class=encabezado><h1>Tendencias</h1>
      <div class=pestanas id=rango-tendencias>${opciones}</div></div>
    ${serie.length ? graficas : `<div class=vacio><b>Sin datos en el periodo</b>
      <p>Sincroniza el reloj o pulsa «Sincronizar» arriba.</p></div>`}`;

  seccion.querySelectorAll("[data-dias]").forEach((b) =>
    b.addEventListener("click", () => pintarTendencias(seccion, Number(b.dataset.dias))));
}

/* ================================================================= SESIONES */
const TIPOS = {
  strength_training: ["Fuerza", "fuerza"],
  indoor_cardio: ["Cardio", ""],
  cardio_training: ["Cardio", ""],
  running: ["Carrera", ""],
  treadmill_running: ["Cinta", ""],
  walking: ["Caminata", "andar"],
  hiking: ["Senderismo", "andar"],
  cycling: ["Bici", ""],
  indoor_cycling: ["Bici indoor", ""],
  yoga: ["Yoga", ""],
  elliptical: ["Elíptica", ""],
  // Lo que graba el reloj cuando se le da al crono sin elegir deporte. Sale a
  // menudo, asi que merece nombre propio en vez de "stop watch".
  stop_watch: ["Cronómetro", ""],
  other: ["Otro", ""],
};

async function pintarSesiones(seccion, dias = 30) {
  seccion.innerHTML = `<div class=esqueleto></div>`;
  const datos = await pedirCache(`${API}/sesiones?dias=${dias}`);
  if (datos.garmin_linked === false) { seccion.innerHTML = sinGarmin(datos.note); return; }

  const sesiones = datos.sessions || [];
  const total = sesiones.reduce((a, s) => a + (s.duration_min || 0), 0);
  const opciones = [7, 30, 90].map((n) =>
    `<button class="pestana ${n === dias ? "activa" : ""}" data-dias="${n}">${n} días</button>`
  ).join("");

  const filas = sesiones.map((s) => {
    const [etiqueta, clase] = TIPOS[s.type] || [(s.type || "").replaceAll("_", " "), ""];
    const d = new Date(s.start);
    const cifra = (v, u, dec = 0) => v
      ? `<div><b>${NUM(v, dec)}</b><span>${esc(u)}</span></div>` : "";
    return `<div class=sesion>
      <div class=dia><span class=num>${d.getDate()}</span>
        <span class=mes>${MESES[d.getMonth()]}</span></div>
      <div>
        <div class=titulo>${esc(s.name || "Sesión")}
          ${etiqueta ? `<span class="tipo ${clase}">${esc(etiqueta)}</span>` : ""}</div>
        <div class=detalle>${d.toTimeString().slice(0, 5)} ·
          ${NUM(s.duration_min, 0)} min${s.distance_km ? ` · ${NUM(s.distance_km, 2)} km` : ""}
          ${s.te_aerobic ? ` · efecto aeróbico ${NUM(s.te_aerobic, 1)}` : ""}</div>
      </div>
      <div class=cifras>${cifra(s.avg_hr, "FC media")}${cifra(s.max_hr, "FC máx")}
        ${cifra(s.kcal, "kcal")}</div>
    </div>`;
  }).join("");

  seccion.innerHTML = `
    <div class=encabezado>
      <div><h1>Sesiones</h1>
        <div class=fecha>${sesiones.length} sesiones · ${NUM(Math.round(total))} min en total</div></div>
      <div class=pestanas id=rango-sesiones>${opciones}</div></div>
    ${sesiones.length ? `<div class=lista>${filas}</div>`
      : `<div class=vacio><b>Ninguna sesión en el periodo</b>
         <p>Aquí aparece lo que grabe tu Garmin, y también lo que el asistente
         dé de alta a mano por ti.</p></div>`}`;

  seccion.querySelectorAll("[data-dias]").forEach((b) =>
    b.addEventListener("click", () => pintarSesiones(seccion, Number(b.dataset.dias))));
}

/* =================================================================== CUERPO */
/* El mapa muscular. Dos figuras esquematicas (frente y espalda) dibujadas a
   mano en SVG: cada region es una forma con data-region, y el color lo pone
   pintarMusculos segun las series del periodo. No es anatomia, es un mapa de
   calor con forma de persona: lo que importa es ver de un vistazo que llevas
   dos semanas sin tocar pierna. */
const SILUETA = `
  <circle cx=60 cy=15 r=9.5 class=silueta></circle>
  <rect x=54 y=24 width=12 height=9 rx=4 class=silueta></rect>
  <rect x=38 y=31 width=44 height=80 rx=15 class=silueta></rect>
  <rect x=25 y=40 width=13 height=68 rx=6.5 class=silueta></rect>
  <rect x=82 y=40 width=13 height=68 rx=6.5 class=silueta></rect>
  <rect x=42 y=108 width=17 height=92 rx=8 class=silueta></rect>
  <rect x=61 y=108 width=17 height=92 rx=8 class=silueta></rect>`;

function _el(region, forma) {
  return forma.replace(
    /^<(ellipse|rect|path|polygon)/,
    `<$1 class=musculo data-region="${region}"`,
  );
}

const CUERPO_FRENTE = [
  ["hombros",    "<ellipse cx=35 cy=44 rx=8 ry=6.5></ellipse>"],
  ["hombros",    "<ellipse cx=85 cy=44 rx=8 ry=6.5></ellipse>"],
  ["pecho",      "<ellipse cx=51 cy=57 rx=9.5 ry=8></ellipse>"],
  ["pecho",      "<ellipse cx=69 cy=57 rx=9.5 ry=8></ellipse>"],
  ["biceps",     "<ellipse cx=31.5 cy=64 rx=5.5 ry=10></ellipse>"],
  ["biceps",     "<ellipse cx=88.5 cy=64 rx=5.5 ry=10></ellipse>"],
  ["antebrazos", "<ellipse cx=30 cy=92 rx=4.5 ry=13></ellipse>"],
  ["antebrazos", "<ellipse cx=90 cy=92 rx=4.5 ry=13></ellipse>"],
  ["abdomen",    "<rect x=53 y=68 width=14 height=35 rx=6></rect>"],
  ["oblicuos",   "<ellipse cx=46 cy=85 rx=4 ry=14></ellipse>"],
  ["oblicuos",   "<ellipse cx=74 cy=85 rx=4 ry=14></ellipse>"],
  ["cuadriceps", "<ellipse cx=50.5 cy=140 rx=8 ry=25></ellipse>"],
  ["cuadriceps", "<ellipse cx=69.5 cy=140 rx=8 ry=25></ellipse>"],
];

const CUERPO_ESPALDA = [
  ["trapecios",  "<path d='M60,31 L44,47 Q60,55 76,47 Z'></path>"],
  ["hombros",    "<ellipse cx=35 cy=44 rx=8 ry=6.5></ellipse>"],
  ["hombros",    "<ellipse cx=85 cy=44 rx=8 ry=6.5></ellipse>"],
  ["espalda",    "<ellipse cx=51 cy=68 rx=8.5 ry=16></ellipse>"],
  ["espalda",    "<ellipse cx=69 cy=68 rx=8.5 ry=16></ellipse>"],
  ["lumbar",     "<rect x=53 y=86 width=14 height=17 rx=5></rect>"],
  ["triceps",    "<ellipse cx=31.5 cy=66 rx=5.5 ry=10.5></ellipse>"],
  ["triceps",    "<ellipse cx=88.5 cy=66 rx=5.5 ry=10.5></ellipse>"],
  ["antebrazos", "<ellipse cx=30 cy=92 rx=4.5 ry=13></ellipse>"],
  ["antebrazos", "<ellipse cx=90 cy=92 rx=4.5 ry=13></ellipse>"],
  ["gluteos",    "<ellipse cx=51.5 cy=113 rx=8.5 ry=9.5></ellipse>"],
  ["gluteos",    "<ellipse cx=68.5 cy=113 rx=8.5 ry=9.5></ellipse>"],
  ["isquios",    "<ellipse cx=50.5 cy=146 rx=7.5 ry=22></ellipse>"],
  ["isquios",    "<ellipse cx=69.5 cy=146 rx=7.5 ry=22></ellipse>"],
  ["gemelos",    "<ellipse cx=50 cy=182 rx=6 ry=13></ellipse>"],
  ["gemelos",    "<ellipse cx=70 cy=182 rx=6 ry=13></ellipse>"],
];

function figura(formas, titulo, valores, etiquetas, tope) {
  const cuerpo = formas.map(([region, forma]) => {
    const series = valores[region] || 0;
    // Escala con suelo: una region tocada una vez tiene que distinguirse de
    // una sin tocar, aunque el maximo sea alto.
    const t = tope && series ? Math.sqrt(series / tope) : 0;
    const relleno = series
      ? `color-mix(in srgb, var(--verde) ${Math.round(18 + 72 * t)}%, var(--carbon-alto))`
      : "rgba(255,255,255,.055)";
    return _el(region, forma).replace(
      ">",
      `${t > 0.6 ? ' data-fuerte=1' : ''} style="fill:${relleno}">` +
      `<title>${esc(etiquetas[region] || region)}: ` +
      `${NUM(series, series % 1 ? 1 : 0)} series</title>`,
    );
  }).join("");
  return `<div class=cuerpo-figura>
    <svg viewBox="0 0 120 205" role=img aria-label="${esc(titulo)}">${SILUETA}${cuerpo}</svg>
    <div class=rotulo>${esc(titulo)}</div></div>`;
}

/* La lista minimalista de al lado: nombre, una barra de un pelo y el numero.
   Es la misma informacion que el cuerpo, en forma legible y ordenada; pasar
   por encima de una fila enciende su region en las figuras, y al reves. */
function listaMusculos(valores, etiquetas, tope) {
  const filas = Object.entries(valores)
    .filter(([, v]) => v > 0)
    .sort((a, b) => b[1] - a[1])
    .map(([region, v]) => `
      <div class=musculo-fila data-region="${esc(region)}" tabindex=0>
        <span class=nombre>${esc(etiquetas[region] || region)}</span>
        <span class=pista-mini><span class=lleno-mini
          style="width:${Math.round(100 * v / tope)}%"></span></span>
        <span class=cifra>${NUM(v, v % 1 ? 1 : 0)}</span>
      </div>`).join("");
  return filas || `<p class=nota-vacia>Solo hay series sin identificar en esta selección.</p>`;
}

// Que dia (o periodo) esta mirando el mapa. Vive fuera de la funcion para que
// cambiar de pestaña y volver no resetee la seleccion.
const MAPA = { dias: 30, dia: null };

async function pintarMusculos(seccion) {
  const caja = seccion.querySelector("#mapa-muscular");
  if (!caja) return;
  let datos;
  try {
    datos = await pedirCache(`${API}/musculos?dias=${MAPA.dias}`);
  } catch (e) {
    caja.innerHTML = `<p class=nota-vacia>No se pudo cargar el mapa muscular: ${esc(e.message)}</p>`;
    return;
  }
  if (datos.garmin_linked === false) { caja.innerHTML = ""; return; }

  const porDia = datos.daily || [];
  // Si el dia elegido ya no esta en el periodo nuevo, se vuelve al total.
  if (MAPA.dia && !porDia.some((d) => d.date === MAPA.dia)) MAPA.dia = null;
  const seleccion = MAPA.dia ? porDia.find((d) => d.date === MAPA.dia) : null;
  const valores = seleccion ? seleccion.muscles : (datos.muscles || {});
  const sinId = seleccion ? seleccion.unknown_sets : (datos.unknown_sets || 0);
  const etiquetas = datos.regions || {};
  const tope = Math.max(0, ...Object.values(valores));

  const periodos = [7, 30, 90].map((n) =>
    `<button class="pestana ${n === MAPA.dias && !MAPA.dia ? "activa" : ""}"
       data-mapa-dias="${n}">${n} días</button>`).join("");
  const pildorasDia = porDia.map((d) =>
    `<button class="dia-pildora ${d.date === MAPA.dia ? "activa" : ""}"
       data-mapa-dia="${d.date}">${esc(fecha(d.date))}
       <small>${d.sets + d.unknown_sets}</small></button>`).join("");

  const contenido = (!tope && !sinId)
    ? `<p class=nota-vacia>Sin series de fuerza clasificadas en los últimos
       ${datos.days} días.</p>`
    : `<div class=mapa-rejilla>
        <div class=cuerpos>
          ${figura(CUERPO_FRENTE, "Frente", valores, etiquetas, tope)}
          ${figura(CUERPO_ESPALDA, "Espalda", valores, etiquetas, tope)}
        </div>
        <div class=musculo-lista>
          <div class=rotulo>${seleccion
            ? `Series del ${esc(fecha(seleccion.date))}`
            : `Series · últimos ${datos.days} días`}</div>
          ${listaMusculos(valores, etiquetas, tope)}
        </div>
      </div>
      ${sinId ? `<p class=aviso>${seleccion ? "Ese día hay" : "Además hay"}
        <b>${sinId} series sin identificar</b>: el reloj no supo qué ejercicio
        eran. Cuéntaselo a tu asistente y las repartirá en el mapa.</p>` : ""}`;

  caja.innerHTML = `
    <div class=mapa-cabecera>
      <div class="pestanas periodo-mapa">${periodos}</div>
      ${pildorasDia ? `<div class=dias-fuerza>${pildorasDia}</div>` : ""}
    </div>
    ${contenido}`;

  caja.querySelectorAll("[data-mapa-dias]").forEach((b) =>
    b.addEventListener("click", () => {
      MAPA.dias = Number(b.dataset.mapaDias);
      MAPA.dia = null;
      pintarMusculos(seccion);
    }));
  caja.querySelectorAll("[data-mapa-dia]").forEach((b) =>
    b.addEventListener("click", () => {
      // Tocar el dia ya elegido lo deselecciona y vuelve al periodo entero.
      MAPA.dia = MAPA.dia === b.dataset.mapaDia ? null : b.dataset.mapaDia;
      pintarMusculos(seccion);
    }));

  // Fila <-> region: el mismo dato en dos sitios se subraya a la vez.
  const enlazar = (region, encendido) => {
    caja.querySelectorAll(`[data-region="${region}"]`).forEach((el) =>
      el.classList.toggle("destacado", encendido));
  };
  caja.querySelectorAll(".musculo-fila, .musculo").forEach((el) => {
    const region = el.dataset.region;
    el.addEventListener("mouseenter", () => enlazar(region, true));
    el.addEventListener("mouseleave", () => enlazar(region, false));
  });
}

const COMPOSICION = [
  { campo: "body_fat_pct", nombre: "Grasa corporal", unidad: "%", decimales: 1 },
  { campo: "muscle_mass_kg", nombre: "Masa muscular", unidad: "kg", decimales: 2 },
  { campo: "water_pct", nombre: "Agua", unidad: "%", decimales: 1 },
  { campo: "bone_mass_kg", nombre: "Masa ósea", unidad: "kg", decimales: 2 },
  { campo: "visceral_fat", nombre: "Grasa visceral", decimales: 1 },
  { campo: "bmi", nombre: "IMC", decimales: 1 },
  { campo: "bmr_kcal", nombre: "Metabolismo basal", unidad: "kcal" },
  { campo: "metabolic_age", nombre: "Edad metabólica", unidad: "años" },
];

function tendenciaSemanal(t, nombre, unidad, mejorArriba) {
  if (!t || t.change_per_week == null) return "";
  const bien = mejorArriba ? t.change_per_week > 0 : t.change_per_week < 0;
  const signo = t.change_per_week > 0 ? "+" : "−";
  return `<span class="chip"><b>${esc(nombre)}</b> <span class="${bien ? "delta bien" : "delta mal"}">
    ${signo}${NUM(Math.abs(t.change_per_week), 2)} ${esc(unidad)}/semana</span></span>`;
}

async function pintarCuerpo(seccion, dias = 90) {
  seccion.innerHTML = `<div class=esqueleto></div>`;
  const datos = await pedirCache(`${API}/cuerpo?dias=${dias}`);
  const bascula = datos.scale || {};
  const garmin = datos.garmin || {};

  let bloque;
  if (bascula.linked === false) {
    bloque = `<div class=vacio><b>No tienes báscula vinculada</b>
      <p>Garmin solo conoce el peso que tecleas a mano. Con una báscula de
      bioimpedancia se mide todos los días la grasa, el músculo y el agua: es lo
      único que distingue adelgazar de perder lo que estabas construyendo.</p>
      <a class=boton href="/vincular-bascula">Vincular báscula</a></div>`;
  } else if (bascula.expired || bascula.error) {
    bloque = `<div class=vacio><b>La báscula no responde</b>
      <p>${esc(bascula.note || bascula.error)}</p>
      <a class=boton href="/vincular-bascula">Volver a vincular</a></div>`;
  } else if (!bascula.latest) {
    bloque = `<div class=vacio><b>Sin pesajes en el periodo</b>
      <p>${esc(bascula.note || "Comprueba que la app de la báscula ha sincronizado.")}</p></div>`;
  } else {
    const u = bascula.latest;
    const serie = (bascula.measurements || [])
      .filter((m) => typeof m.weight_kg === "number")
      .map((m) => ({ fecha: m.date, valor: m.weight_kg }))
      .reverse();
    bloque = `
      <div class="tarjeta-panel grafica">
        <div class=grafica-cabeza>
          <div><div class=rotulo>Peso</div>
            <div class=ahora>${NUM(u.weight_kg, 2)} <small>kg</small></div></div>
          <div class=media>${esc(fecha(u.date))} · ${bascula.count} pesajes</div>
        </div>
        ${linea(serie, "var(--verde)", { unidad: "kg", decimales: 1 })}
        <div class=chips style="margin-top:1rem">
          ${tendenciaSemanal(bascula.weight_trend, "Peso", "kg", false)}
          ${tendenciaSemanal(bascula.body_fat_trend, "Grasa", "%", false)}
          ${tendenciaSemanal(bascula.muscle_mass_trend, "Músculo", "kg", true)}
        </div>
      </div>
      <div class=titulo-seccion>Composición del último pesaje</div>
      <div class=metricas>
        ${COMPOSICION.filter((c) => u[c.campo] != null).map((c) => metrica({
          nombre: c.nombre, valor: u[c.campo], unidad: c.unidad, decimales: c.decimales || 0,
        })).join("")}
      </div>`;
  }

  const pesajesGarmin = (garmin.entries || []).length;
  seccion.innerHTML = `
    <div class=encabezado><div><h1>Cuerpo</h1>
      <div class=fecha>Últimos ${datos.days} días</div></div></div>
    ${bloque}
    <div class=titulo-seccion>Músculos entrenados · últimos 30 días</div>
    <div class=tarjeta-panel id=mapa-muscular>
      <div class=esqueleto style="height:180px"></div>
    </div>
    <div class=titulo-seccion>Peso en Garmin</div>
    <div class=tarjeta-panel>
      ${pesajesGarmin ? `<div class=metricas style="border:0">
        ${metrica({ nombre: "Último", valor: garmin.latest?.weight_kg, unidad: "kg", decimales: 2 })}
        ${metrica({ nombre: "Media del periodo", valor: garmin.average_kg, unidad: "kg", decimales: 2 })}
        ${metrica({ nombre: "Cambio", valor: garmin.change_kg, unidad: "kg", decimales: 2 })}
        ${metrica({ nombre: "Pesajes", valor: pesajesGarmin })}
      </div>` : `<p class=nota-vacia>${esc(garmin.note || garmin.error
        || "Sin pesajes registrados en Garmin durante el periodo.")}</p>`}
      <p class=aviso>Son los pesajes de Garmin Connect: los que tecleas a mano o
      apunta el asistente. La báscula inteligente va por su cuenta y mide más cosas.</p>
    </div>`;

  // El mapa muscular va por su cuenta para no retrasar el peso, que ya esta.
  pintarMusculos(seccion);
}

/* ================================================================= ANÁLISIS */
/* La misma tarjeta sirve en Hoy y en la pestaña entera. En Hoy va `resumido`:
   recorta el cuerpo a unas lineas y quita el boton de borrar, porque esa
   pantalla es un vistazo y no el sitio donde se gestiona nada. */
function tarjetaInsight(i, { resumido = false } = {}) {
  const chips = Object.entries(i.metrics || {}).map(([k, v]) =>
    `<span class=chip><b>${esc(k)}</b> ${esc(v)}</span>`).join("");
  return `<article class="insight ${esc(i.kind)}${resumido ? " resumido" : ""}"
                   data-id="${esc(i.insight_id)}">
    ${resumido ? "" : `<button class=borrar title="Quitar del panel">Borrar</button>`}
    <div class=meta><span class=clase>${esc(i.label || i.kind)}</span>
      <span class=cuando>${esc(haceCuanto(i.created_at))}
      ${i.period_days ? ` · mirando ${i.period_days} días` : ""}</span></div>
    <h3>${esc(i.title)}</h3>
    <p>${esc(i.body)}</p>
    ${chips ? `<div class=chips>${chips}</div>` : ""}
  </article>`;
}

async function pintarAnalisis(seccion) {
  seccion.innerHTML = `<div class=esqueleto></div>`;
  const datos = await pedirCache(`${API}/insights?limite=50`);
  const lista = datos.insights || [];
  const tarjetas = lista.map((i) => tarjetaInsight(i)).join("");

  seccion.innerHTML = `
    <div class=encabezado><div><h1>Análisis</h1>
      <div class=fecha>Lo que tu asistente ha concluido de tus datos</div></div></div>
    ${lista.length ? tarjetas : `<div class=vacio><b>Todavía no hay análisis</b>
      <p>Esto no se rellena solo: lo escribe tu asistente cuando llega a una
      conclusión que merece releerse. Pídeselo en la conversación —«mira cómo he
      dormido esta semana y déjamelo apuntado»— y aparecerá aquí, con los números
      en los que se apoyó.</p></div>`}`;

  seccion.querySelectorAll(".borrar").forEach((b) => b.addEventListener("click", async () => {
    const tarjeta = b.closest(".insight");
    try {
      await pedir(`${API}/insights/${encodeURIComponent(tarjeta.dataset.id)}`, { method: "DELETE" });
      // Se tiran todas las listas cacheadas, no solo esta: el destacado de Hoy
      // se pide con otro limite y seguiria enseñando lo que se acaba de borrar.
      [...cache.keys()].filter((k) => k.includes("/insights")).forEach((k) => cache.delete(k));
      APP.pintadas.delete("hoy");
      tarjeta.remove();
      avisar("Análisis borrado.");
    } catch (e) { avisar(e.message, true); }
  }));
}

/* ================================================================== AJUSTES */
const REPARTOS = {
  full_body: "Todo el cuerpo en cada sesión",
  torso_pierna: "Torso / pierna",
  empuje_tiron_pierna: "Empuje / tirón / pierna",
  por_grupos: "Un grupo muscular por día",
  sin_preferencia: "Sin preferencia",
};
const CAMPOS_PERFIL = [
  ["equipment", "Material"], ["split", "Reparto"], ["days_per_week", "Días por semana"],
  ["session_minutes", "Minutos por sesión"], ["activities", "Actividades habituales"],
  ["goals", "Objetivos"], ["limitations", "Limitaciones"], ["notes", "Notas"],
];

function pintarAjustes(seccion) {
  const e = APP.estado;
  const garmin = e.garmin.linked;
  const b = e.scale;

  const perfil = CAMPOS_PERFIL
    .filter(([k]) => e.profile[k] != null)
    .map(([k, nombre]) => {
      let v = e.profile[k];
      if (k === "split") v = REPARTOS[v] || v;
      if (Array.isArray(v)) v = v.join(", ");
      return `<div class=dato><b>${esc(nombre)}</b><span>${esc(v)}</span></div>`;
    }).join("");

  seccion.innerHTML = `
    <div class=encabezado><div><h1>Ajustes</h1>
      <div class=fecha>${esc(e.user.email || "")} · cuenta desde ${esc(e.user.created_at)}</div></div></div>

    <div class=titulo-seccion>Conexiones</div>
    <div class=conexion>
      <span class="luz ${garmin ? "on" : "off"}"></span>
      <div class=info><b>Garmin Connect</b>
        <span>${garmin ? "Vinculado. Tus métricas se bajan solas."
          : "Sin vincular: el panel está vacío hasta que lo conectes."}</span></div>
      <div class=acciones><a class=fantasma href="/vincular-garmin">
        ${garmin ? "Volver a vincular" : "Vincular"}</a></div>
    </div>

    <div class=conexion>
      <span class="luz ${b.linked ? "on" : ""}"></span>
      <div class=info><b>Báscula inteligente</b>
        <span>${b.linked
          ? `${esc(b.name)} · ${esc(b.account || "")} · desde ${esc(b.linked_at || "")}`
          : "Sin vincular. Con báscula verás grasa y músculo, no solo el peso."}</span></div>
      <div class=acciones>
        <a class=fantasma href="/vincular-bascula">${b.linked ? "Cambiar" : "Vincular"}</a>
        ${b.linked ? `<form method=post action="/desvincular-bascula">
          <button class=fantasma type=submit>Desvincular</button></form>` : ""}
      </div>
    </div>

    <div class=titulo-seccion>Conector para tu asistente</div>
    <div class=tarjeta-panel>
      <p style="color:var(--tenue);font-size:.9rem">Añádelo como conector personalizado
      en Claude o ChatGPT. Te pedirá iniciar sesión aquí una vez.</p>
      <div class=copiable><span class=url id=url-mcp>${esc(e.mcp_url)}</span>
        <button class=fantasma id=copiar>Copiar</button></div>
    </div>

    <div class=titulo-seccion>Perfil de entrenamiento</div>
    <div class=tarjeta-panel>
      ${perfil ? `<div class=rejilla style="margin:0">${perfil}</div>`
        : `<p class=nota-vacia>Vacío todavía.</p>`}
      <p class=aviso>Esto no se edita aquí a propósito: lo mantiene tu asistente
      cuando le cuentas algo estable («me he comprado una bici», «me duele el
      hombro»). Así no hay dos versiones que se contradigan.</p>
    </div>

    ${e.user.is_admin ? `<div class=titulo-seccion>Administración</div>
      <div class=conexion><span class="luz on"></span>
      <div class=info><b>Panel de administración</b>
        <span>Invitaciones y usuarios dados de alta.</span></div>
      <div class=acciones><a class=fantasma href="/admin">Abrir</a></div></div>` : ""}`;

  seccion.querySelector("#copiar").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(e.mcp_url);
      avisar("Dirección copiada.");
    } catch { avisar("No se pudo copiar; selecciónala a mano.", true); }
  });
}

/* =================================================================== arranque */
const VISTAS = {
  hoy: pintarHoy,
  tendencias: pintarTendencias,
  sesiones: pintarSesiones,
  cuerpo: pintarCuerpo,
  analisis: pintarAnalisis,
  ajustes: pintarAjustes,
};

async function mostrar(nombre, forzar) {
  APP.vista = nombre;
  document.querySelectorAll(".pestana[data-vista]").forEach((b) => {
    const activa = b.dataset.vista === nombre;
    b.classList.toggle("activa", activa);
    b.setAttribute("aria-selected", activa ? "true" : "false");
  });
  document.querySelectorAll(".vista").forEach((v) =>
    v.classList.toggle("activa", v.id === `vista-${nombre}`));
  history.replaceState(null, "", `#${nombre}`);

  const seccion = document.getElementById(`vista-${nombre}`);
  if (APP.pintadas.has(nombre) && !forzar) return;
  try {
    await VISTAS[nombre](seccion);
    APP.pintadas.add(nombre);
  } catch (err) {
    seccion.innerHTML = `<div class=vacio><b>No se ha podido cargar</b>
      <p>${esc(err.message)}</p></div>`;
  }
}

async function arrancar() {
  document.querySelectorAll(".pestana[data-vista]").forEach((b) =>
    b.addEventListener("click", () => mostrar(b.dataset.vista)));

  document.getElementById("sincronizar").addEventListener("click", async (ev) => {
    const boton = ev.currentTarget;
    boton.disabled = true;
    const antes = boton.textContent;
    boton.textContent = "Sincronizando…";
    try {
      const r = await pedir(`${API}/sync?dias=7`, { method: "POST" });
      cache.clear();
      APP.pintadas.clear();
      await mostrar(APP.vista, true);
      avisar(`${r.synced_days} días actualizados`
        + (r.errors?.length ? ` · ${r.errors.length} con error` : ""));
    } catch (e) {
      avisar(e.message, true);
    } finally {
      boton.disabled = false;
      boton.textContent = antes;
    }
  });

  try {
    APP.estado = await pedir(`${API}/estado`);
  } catch (e) {
    document.getElementById("contenido").innerHTML =
      `<div class=vacio><b>No se ha podido cargar el panel</b><p>${esc(e.message)}</p></div>`;
    return;
  }

  const inicial = location.hash.slice(1);
  mostrar(VISTAS[inicial] ? inicial : "hoy");
}

arrancar();
