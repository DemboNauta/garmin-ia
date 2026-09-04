// Genera las capturas del README: levanta el panel de verdad contra datos
// inventados y lo fotografia con Chrome headless.
//
//   node docs/capturas.js
//
// Existe porque un panel de salud enseña el cuerpo de alguien: las capturas se
// hacen con este molde y no con la cuenta de nadie. Al cambiar la interfaz, se
// vuelve a lanzar y las cuatro imagenes se rehacen.
//
// Las respuestas de /panel/api salen de aqui, no del servidor. En otro sistema
// operativo, apunta CHROME al navegador: CHROME=/usr/bin/chromium node docs/capturas.js
const http = require('http');
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');

const RAIZ = path.join(__dirname, '..');
const STATIC = path.join(RAIZ, 'app', 'static');
const SALIDA = path.join(__dirname, 'capturas');
const CHROME = process.env.CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const PUERTO = 8777;

// --------------------------------------------------------------- azar estable
let semilla = 20260904;
function rnd() { semilla = (semilla * 1103515245 + 12345) % 2147483648; return semilla / 2147483648; }
const entre = (a, b) => a + rnd() * (b - a);
const ent = (a, b) => Math.round(entre(a, b));
const dec = (a, b, d = 1) => +entre(a, b).toFixed(d);

const HOY = new Date('2026-09-04T20:00:00');
const iso = (d) => d.toISOString().slice(0, 10);
const hace = (n) => { const d = new Date(HOY); d.setDate(d.getDate() - n); return d; };

// ------------------------------------------------------------------- metricas
function dia(n) {
  const fecha = iso(hace(n));
  const readiness = ent(38, 88);
  return {
    date: fecha,
    steps: ent(4200, 13500),
    active_kcal: ent(320, 780),
    bmr_kcal: 1780,
    total_kcal: 0,
    intensity_minutes: ent(12, 95),
    floors_climbed: null,
    resting_hr: ent(48, 58),
    max_hr: ent(112, 168),
    avg_hr: null,
    hrv_last_night: ent(44, 78),
    hrv_status: 'BALANCED',
    vo2max: dec(42, 48, 2),
    sleep_hours: dec(5.1, 8.2, 2),
    sleep_score: ent(58, 92),
    deep_sleep_hours: dec(0.7, 2.1, 2),
    rem_sleep_hours: dec(0.6, 1.9, 2),
    awake_hours: dec(0.05, 0.4, 2),
    body_battery_high: ent(62, 98),
    body_battery_low: ent(8, 34),
    avg_stress: ent(22, 46),
    training_readiness: readiness,
    training_status: {
      status: 'maintaining', feedback: 'MAINTAINING_1', code: 3,
      since: iso(hace(n + 12)), paused: false,
      load_acute: ent(180, 420), load_chronic: ent(210, 380), acwr_status: 'OPTIMAL',
    },
  };
}
const dias30 = [];
for (let n = 29; n >= 0; n--) { const d = dia(n); d.total_kcal = d.bmr_kcal + d.active_kcal; dias30.push(d); }
const ultimo = dias30[dias30.length - 1];

// ------------------------------------------------------------------- sesiones
const NOMBRES_FUERZA = ['Fuerza - Pierna', 'Fuerza - Empuje', 'Fuerza - Tirón', 'Fuerza - Brazos'];
const sesiones = [];
for (let n = 0; n < 30; n++) {
  const esFuerza = n % 3 === 1;
  const fecha = hace(n);
  const hora = esFuerza ? '19:05' : '21:20';
  if (n % 7 === 3) continue;
  sesiones.push({
    id: 20260000 + n,
    name: esFuerza ? NOMBRES_FUERZA[Math.floor(n / 3) % 4] : 'Caminata',
    type: esFuerza ? 'strength_training' : 'walking',
    start: `${iso(fecha)} ${hora}:00`,
    duration_min: esFuerza ? dec(38, 68) : dec(28, 62),
    distance_km: esFuerza ? null : dec(2.6, 6.4, 2),
    avg_hr: esFuerza ? ent(105, 138) : ent(96, 124),
    max_hr: esFuerza ? ent(148, 172) : ent(118, 152),
    kcal: esFuerza ? ent(240, 560) : ent(180, 420),
    te_aerobic: dec(0.8, 3.1),
    te_anaerobic: esFuerza ? dec(0.4, 2.8) : dec(0, 0.6),
  });
}

// --------------------------------------------------------------------- cuerpo
const medidas = [];
for (let n = 0; n < 88; n++) {
  const peso = +(78.4 + n * 0.031 + entre(-0.45, 0.45)).toFixed(2);
  const grasa = +(19.2 + n * 0.012 + entre(-0.3, 0.3)).toFixed(2);
  medidas.push({
    date: iso(hace(n)), at: `${iso(hace(n))}T07:41:12+02:00`,
    weight_kg: peso, body_fat_pct: grasa,
    muscle_mass_kg: +(peso * (1 - grasa / 100) * 0.86).toFixed(2),
    fat_mass_kg: +(peso * grasa / 100).toFixed(2),
    fat_free_kg: +(peso * (1 - grasa / 100)).toFixed(2),
    water_pct: dec(53, 57, 1), bone_mass_kg: dec(3.1, 3.4, 2),
    protein_pct: dec(16.4, 17.6, 2), visceral_fat: ent(7, 9),
    subcutaneous_fat_pct: dec(15, 18, 1), bmr_kcal: ent(1740, 1830),
    metabolic_age: ent(24, 29), bmi: +(peso / (1.78 * 1.78)).toFixed(1),
    muscle_pct: dec(45, 49, 2), heart_rate: 0,
  });
}
const tendencia = (campo, signo) => ({
  change: +(signo * entre(0.4, 1.6)).toFixed(2), since: medidas[medidas.length - 1].date,
  avg_7d: +medidas[0][campo], avg_prev_7d: +(medidas[0][campo] - signo * 0.18).toFixed(2),
  change_per_week: +(signo * entre(0.06, 0.24)).toFixed(2),
});
const cuerpo = {
  days: 90,
  scale: {
    linked: true, provider: 'feelfit', days: 90, count: medidas.length,
    measurements: medidas, latest: medidas[0],
    weight_trend: tendencia('weight_kg', -1),
    body_fat_trend: tendencia('body_fat_pct', -1),
    muscle_mass_trend: tendencia('muscle_mass_kg', 1),
  },
  garmin: {
    days: 90,
    entries: medidas.filter((_, i) => i % 6 === 0).map((m) => ({ date: m.date, weight_kg: m.weight_kg })),
    latest: { date: medidas[0].date, weight_kg: medidas[0].weight_kg },
    average_kg: 78.9, change_kg: -1.2, since: medidas[medidas.length - 1].date,
  },
};

// ------------------------------------------------------------------- musculos
const REGIONES = {
  hombros: 'Hombros', pecho: 'Pecho', biceps: 'Bíceps', triceps: 'Tríceps',
  antebrazos: 'Antebrazos', abdomen: 'Abdomen', oblicuos: 'Oblicuos',
  trapecios: 'Trapecios', espalda: 'Espalda', lumbar: 'Lumbar', gluteos: 'Glúteos',
  cuadriceps: 'Cuádriceps', isquios: 'Isquios', gemelos: 'Gemelos',
};
const totales = {}; const diario = [];
for (const s of sesiones.filter((s) => s.name.startsWith('Fuerza'))) {
  const m = {};
  for (const r of Object.keys(REGIONES)) { if (rnd() > 0.45) { const v = dec(2, 9); m[r] = v; totales[r] = +((totales[r] || 0) + v).toFixed(1); } }
  diario.push({ date: s.start.slice(0, 10), sets: ent(9, 18), unknown_sets: ent(0, 3), muscles: m });
}
const musculos = {
  garmin_linked: true, days: 30, from: iso(hace(29)), to: iso(hace(0)),
  sessions: diario.length, unknown_sets: 4, muscles: totales, daily: diario,
  regions: REGIONES,
  categories: { SQUAT: 14, BENCH_PRESS: 12, ROW: 11, DEADLIFT: 9, SHOULDER_PRESS: 8, CURL: 7, TRICEPS_EXTENSION: 6 },
};

// ------------------------------------------------------------------- insights
const insights = [
  {
    insight_id: 'ins_ejemplo_1', created_at: `${iso(hace(0))}T09:12:00+02:00`,
    kind: 'daily_read', label: 'Lectura del día',
    title: 'Pierna con más carga que la semana pasada y sin deriva cardíaca',
    body: 'Sesión de 52 minutos con 14 series de trabajo y 6.900 kg de volumen, frente a los 4.100 de la semana anterior. LAS SUBIDAS SON REALES: sentadilla goblet 4x12 a 16 kg donde antes eran 3x10, y el peso muerto rumano pasa de 1.200 a 1.600 kg de volumen. EL PULSO se reparte 13% en Z1, 55% en Z2 y 27% en Z3, con 3,4 minutos por encima de 159. LOS DESCANSOS FUNCIONARON: los valles entre series se quedan en 120-135 durante toda la hora y los diez últimos minutos van más bajos que el arranque, así que no hay deriva. PARA LA PRÓXIMA: el goblet a 16 kg se queda corto para 12 repeticiones, toca probar 20 kg bajando a 8-10.',
    duracion_min: 52, series_trabajo: 14, volumen_total_kg: 6900, volumen_previo_kg: 4100,
    fc_media: 133, fc_maxima: 169, kcal: 558, z1_pct: 13, z2_pct: 55, z3_pct: 27, z4_pct: 5,
  },
  {
    insight_id: 'ins_ejemplo_2', created_at: `${iso(hace(2))}T21:40:00+02:00`,
    kind: 'daily_read', label: 'Lectura del día',
    title: 'Semana de mucha caminata y poca fuerza: el peso se mueve, el músculo no',
    body: 'Seis caminatas y una sola sesión de fuerza. El gasto activo se sostiene por encima de 500 kcal casi todos los días, pero el volumen de fuerza cae a un tercio del de la semana anterior. EL PESO baja 0,4 kg y la grasa 0,2 puntos, así que la pérdida no es solo agua. EL RIESGO está en el músculo: con una sesión semanal se mantiene, no se construye. RECOMENDACIÓN: recuperar dos sesiones de fuerza antes de bajar más las calorías.',
    kcal_activas_media: 512, sesiones_fuerza: 1, caminatas: 6, peso_delta_kg: -0.4, grasa_delta_pct: -0.2,
  },
  {
    insight_id: 'ins_ejemplo_3', created_at: `${iso(hace(5))}T08:05:00+02:00`,
    kind: 'sleep', label: 'Sueño',
    title: 'El sueño profundo mejora desde que las cenas son más temprano',
    body: 'La media de sueño profundo pasa de 1,1 h a 1,6 h en dos semanas, y el despertar nocturno baja de 0,3 h a 0,1 h. Coincide con adelantar la cena. LA HRV acompaña: de 58 ms a 65 ms de media. NO HAY MAGIA: las noches con entrenamiento después de las 21:00 siguen dando el peor sueño profundo de la semana.',
    profundo_h: 1.6, profundo_previo_h: 1.1, hrv_ms: 65, hrv_previo_ms: 58,
  },
];

// --------------------------------------------------------------------- estado
const estado = {
  user: { email: 'persona@ejemplo.com', is_admin: false, created_at: iso(hace(120)) },
  garmin: { linked: true },
  scale: { linked: true, provider: 'feelfit', account: 'persona@ejemplo.com', linked_at: `${iso(hace(60))}T10:00:00+02:00` },
  strava: { available: true, linked: true, athlete: 'Persona de Ejemplo', athlete_id: 123456, linked_at: `${iso(hace(30))}T10:00:00+02:00` },
  mcp_url: 'https://tu-dominio.example/mcp',
  timezone: 'Europe/Madrid',
  profile: {
    material: 'Mancuernas ajustables hasta 24 kg, banco, banda larga',
    dias_entreno: 4, lesiones: 'Hombro derecho: nada por encima de la cabeza con carga',
    objetivo: 'Perder grasa manteniendo la masa muscular',
  },
};

const DATOS = {
  '/panel/api/estado': estado,
  '/panel/api/hoy': { garmin_linked: true, date: ultimo.date, stale: false, today: ultimo.date, day: ultimo },
  '/panel/api/metricas': { garmin_linked: true, from: dias30[0].date, to: ultimo.date, days: dias30, errors: [] },
  '/panel/api/sesiones': { garmin_linked: true, sessions: sesiones },
  '/panel/api/cuerpo': cuerpo,
  '/panel/api/musculos': musculos,
  '/panel/api/insights': { insights },
};

// ---------------------------------------------------------------------- panel
const PESTANAS = [['hoy', 'Hoy'], ['tendencias', 'Tendencias'], ['sesiones', 'Sesiones'],
                  ['cuerpo', 'Cuerpo'], ['analisis', 'Análisis'], ['ajustes', 'Ajustes']];
function panelHtml() {
  const botones = PESTANAS.map(([c, t], i) =>
    `<button class='pestana ${i === 0 ? 'activa' : ''}' data-vista='${c}' role=tab aria-selected='${i === 0}'>${t}</button>`).join('');
  const secciones = PESTANAS.map(([c], i) =>
    `<section class='vista ${i === 0 ? 'activa' : ''}' id='vista-${c}' role=tabpanel><div class=esqueleto></div></section>`).join('');
  return `<!doctype html><html lang=es><meta charset=utf-8>
<meta name=viewport content='width=device-width,initial-scale=1'>
<meta name=color-scheme content=dark><title>Panel · Garmin Bridge</title>
<link rel=stylesheet href='/static/estilo.css'><link rel=stylesheet href='/static/panel.css'>
<style>*,*::before,*::after{transition:none!important;animation:none!important}.vista.activa{opacity:1!important;transform:none!important}</style>
<body class=panel><header class=barra>
<a class=marca href='/panel'><span class=punto></span>Garmin Bridge</a>
<nav class='pestanas nav-principal' role=tablist>${botones}</nav>
<div class=barra-fin><button id=sincronizar class=fantasma>Sincronizar</button>
<form method=post action='/salir'><button class=fantasma>Salir</button></form></div></header>
<main id=contenido>${secciones}</main>
<div id=aviso-flotante class=flotante hidden></div>
<script src='/static/panel.js' defer></script>`;
}

const TIPOS = { '.css': 'text/css', '.js': 'text/javascript', '.png': 'image/png',
                '.webmanifest': 'application/manifest+json' };

const servidor = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://localhost');
  const ruta = url.pathname;
  if (ruta.startsWith('/static/')) {
    const f = path.join(STATIC, ruta.slice(8));
    if (fs.existsSync(f)) {
      res.writeHead(200, { 'Content-Type': TIPOS[path.extname(f)] || 'application/octet-stream' });
      return res.end(fs.readFileSync(f));
    }
    res.writeHead(404); return res.end('no');
  }
  if (DATOS[ruta]) {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    return res.end(JSON.stringify(DATOS[ruta]));
  }
  if (ruta === '/panel' || ruta === '/') {
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
    return res.end(panelHtml());
  }
  res.writeHead(404, { 'Content-Type': 'application/json' });
  res.end('{}');
});

const CAPTURAS = [
  ['hoy', 1440, 1500], ['tendencias', 1440, 1500],
  ['sesiones', 1440, 1400], ['cuerpo', 1440, 1700],
];

// Las capturas van en asincrono a proposito: con execFileSync el bucle de node
// se queda bloqueado y el servidor no puede responderle a su propio Chrome.
function capturar(vista, w, h) {
  return new Promise((resolve, reject) => {
    const destino = path.join(SALIDA, `panel-${vista}.png`);
    const hijo = spawn(CHROME, [
      '--headless=new', '--disable-gpu', '--no-sandbox', '--no-first-run',
      '--user-data-dir=' + path.join(__dirname, 'perfil-' + vista), '--hide-scrollbars',
      `--window-size=${w},${h}`, '--force-device-scale-factor=1',
      '--virtual-time-budget=9000', `--screenshot=${destino}`,
      `http://127.0.0.1:${PUERTO}/panel#${vista}`,
    ], { stdio: 'ignore' });
    const corte = setTimeout(() => { hijo.kill(); reject(new Error('chrome no termino: ' + vista)); }, 60000);
    hijo.on('exit', () => {
      clearTimeout(corte);
      const kb = Math.round(fs.statSync(destino).size / 1024);
      console.log(`${path.basename(destino)}  ${w}x${h}  ${kb} KB`);
      resolve();
    });
  });
}

servidor.listen(PUERTO, '127.0.0.1', async () => {
  fs.mkdirSync(SALIDA, { recursive: true });
  for (const [vista, w, h] of CAPTURAS) await capturar(vista, w, h);
  servidor.close();
});
