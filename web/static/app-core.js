'use strict';
/* Новый интерфейс Bobr Nodes: разметка, запросы к боту, окна, уведомления. Экраны — в app.js. */

const $ = (s, el = document) => el.querySelector(s);
function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  let value;
  if (props) for (const [k, v] of Object.entries(props)) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'html') el.innerHTML = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'value') value = v;
    else if (k === 'checked') el.checked = true;
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const c of kids.flat(Infinity)) if (c != null && c !== false && c !== true) el.append(c instanceof Node ? c : String(c));
  if (value !== undefined) el.value = value;
  return el;
}
/* подсказка с сервера: <code>…</code> — моноширинным, прочие теги отбрасываем (вставляем только как текст) */
function rich(s) {
  const out = h('span');
  String(s || '').split(/(<code>[\s\S]*?<\/code>)/g).forEach(p => { const m = p.match(/^<code>([\s\S]*)<\/code>$/); out.append(m ? h('span', { class: 'mono' }, m[1].replace(/<[^>]+>/g, '')) : p.replace(/<[^>]+>/g, '')); });
  return out;
}
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const ICONS = {
  home: '<path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V21h14V9.5"/><path d="M10 21v-6h4v6"/>',
  server: '<rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 7.5h.01M7 16.5h.01"/>',
  layers: '<path d="m12 3 9 5-9 5-9-5 9-5z"/><path d="m3 13 9 5 9-5"/>',
  activity: '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
  sliders: '<path d="M4 6h10M4 12h4M12 12h8M4 18h12"/><circle cx="16" cy="6" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
  bell: '<path d="M6 8a6 6 0 1 1 12 0c0 7 3 8 3 8H3s3-1 3-8"/><path d="M10 20a2 2 0 0 0 4 0"/>',
  x: '<path d="M6 6l12 12M18 6 6 18"/>',
  chevR: '<path d="m9 6 6 6-6 6"/>', chevL: '<path d="m15 6-6 6 6 6"/>', chevD: '<path d="m6 9 6 6 6-6"/>',
  check: '<path d="m5 12.5 4.5 4.5L19 7"/>',
  alert: '<path d="M12 3 2 20h20L12 3z"/><path d="M12 10v4M12 17h.01"/>',
  unplug: '<path d="M3 3l18 18"/><path d="M8.5 16.5a5 5 0 0 1 7 0"/><path d="M5 12.5a10 10 0 0 1 5.2-2.8"/><path d="M19 12.5a10 10 0 0 0-2.2-1.6"/><path d="M12 20h.01"/>',
  shieldX: '<path d="M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6l-8-3z"/><path d="m9.5 9.5 5 5m0-5-5 5"/>',
  shield: '<path d="M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6l-8-3z"/><path d="m8.5 12 2.5 2.5 4.5-5"/>',
  refresh: '<path d="M20 11a8 8 0 0 0-14.5-4.5L4 8"/><path d="M4 3v5h5"/><path d="M4 13a8 8 0 0 0 14.5 4.5L20 16"/><path d="M20 21v-5h-5"/>',
  up: '<circle cx="12" cy="12" r="9"/><path d="m8 12 4-4 4 4M12 8v8"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/>',
  wallet: '<path d="M3 7a2 2 0 0 1 2-2h13v4"/><rect x="3" y="7" width="18" height="13" rx="2"/><path d="M16 13.5h.01"/>',
  trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
  globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>',
  key: '<circle cx="8" cy="15" r="4"/><path d="m11 12 9-9M17 6l3 3M15 8l2 2"/>',
  zap: '<path d="M13 2 4 14h7l-1 8 9-12h-7l1-8z"/>',
  retry: '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/>',
  users: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0"/><path d="M16 4.5a3.5 3.5 0 0 1 0 7M18 14a6 6 0 0 1 3.5 6"/>',
  contrast: '<circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 0 0 18z" fill="currentColor"/>',
  grid: '<rect x="4" y="4" width="6" height="6" rx="1.5"/><rect x="14" y="4" width="6" height="6" rx="1.5"/><rect x="4" y="14" width="6" height="6" rx="1.5"/><rect x="14" y="14" width="6" height="6" rx="1.5"/>',
  radar: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="4"/><path d="M12 12 19 5"/>',
  route: '<circle cx="6" cy="18" r="2.5"/><circle cx="18" cy="6" r="2.5"/><path d="M8.5 18H15a3 3 0 0 0 0-6H9a3 3 0 0 1 0-6h6.5"/>',
  box: '<path d="M3 7l9-4 9 4v10l-9 4-9-4V7z"/><path d="M3 7l9 4 9-4M12 11v10"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>',
  ext: '<path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
  code: '<path d="m8 7-5 5 5 5M16 7l5 5-5 5"/>',
  terminal: '<rect x="2" y="3" width="20" height="18" rx="2"/><path d="m6 9 3 3-3 3M12 15h6"/>',
};
function ic(n) { const t = document.createElement('template'); t.innerHTML = `<svg class="i" viewBox="0 0 24 24" aria-hidden="true">${ICONS[n] || ''}</svg>`; return t.content.firstChild; }

/* ── форматирование ── */
const MIN = 60000;
const rub = n => Math.round(n).toLocaleString('ru-RU') + ' ₽';
const rubK = kop => rub((kop || 0) / 100);
function plural(n, a, b, c) { n = Math.abs(n) % 100; const d = n % 10; if (n > 10 && n < 20) return c; if (d > 1 && d < 5) return b; return d === 1 ? a : c; }
const pl = (n, a, b, c) => `${n} ${plural(n, a, b, c)}`;
function since(ts) {  // ts — миллисекунды
  const m = Math.max(0, Math.round((Date.now() - ts) / MIN));
  if (m < 1) return 'меньше минуты';
  if (m < 60) return m + ' мин';
  const hh = Math.round(m / 60);
  if (hh < 24) return hh + ' ч';
  return pl(Math.round(hh / 24), 'день', 'дня', 'дней');
}
const ago = ts => (Date.now() - ts < MIN ? 'только что' : since(ts) + ' назад');
const clock = (ts = Date.now()) => new Date(ts).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
const flag = cc => (/^[A-Z]{2}$/.test(cc || '') ? String.fromCodePoint(...[...cc].map(c => 0x1F1E6 + c.charCodeAt(0) - 65)) : '');

/* ── Telegram и запросы к боту ── */
const tg = (window.Telegram && window.Telegram.WebApp && window.Telegram.WebApp.initData !== undefined) ? window.Telegram.WebApp : null;
try { tg && tg.ready(); tg && tg.expand(); } catch (e) { /* старый клиент */ }
/* Telegram: безопасные области, высота окна, нативная кнопка «Назад».
   В полноэкранном режиме Telegram рисует поверх страницы строку состояния и свои кнопки — контент начинается ниже них. */
const tgAt = v => { try { return !!tg && tg.isVersionAtLeast(v); } catch (e) { return false; } };
const nativeBack = !!tg && tg.platform && tg.platform !== 'unknown' && tgAt('6.1');
if (nativeBack) document.documentElement.classList.add('tg-native');
function tgLayout() {
  if (!tg) return;
  const st = document.documentElement.style, sa = tg.safeAreaInset || {}, ca = tg.contentSafeAreaInset || {};
  let ctop = ca.top || 0;
  // старые клиенты не сообщают высоту полосы с кнопками Telegram — берём её стандартную высоту
  if (tg.isFullscreen && !ctop && ['ios', 'android'].includes(tg.platform)) ctop = 48;
  st.setProperty('--tg-safe-area-inset-top', (sa.top || 0) + 'px');
  st.setProperty('--tg-safe-area-inset-bottom', (sa.bottom || 0) + 'px');
  st.setProperty('--tg-content-safe-area-inset-top', ctop + 'px');
  st.setProperty('--tg-content-safe-area-inset-bottom', (ca.bottom || 0) + 'px');
  if (tg.viewportStableHeight) st.setProperty('--app-h', tg.viewportStableHeight + 'px');
  document.documentElement.classList.toggle('tg-fullscreen', !!tg.isFullscreen);
}
try {
  if (tg) {
    // на телефоне — во весь экран, как классический интерфейс; свайп вниз не закрывает приложение посреди прокрутки
    if (tgAt('8.0') && ['ios', 'android'].includes(tg.platform) && !tg.isFullscreen) tg.requestFullscreen();
    if (tgAt('7.7')) tg.disableVerticalSwipes();
    tgLayout();
    ['safeAreaChanged', 'contentSafeAreaChanged', 'fullscreenChanged', 'viewportChanged'].forEach(e => tg.onEvent(e, tgLayout));
  }
} catch (e) { /* старый клиент */ }
async function api(path, method = 'GET', body) {
  const ctl = new AbortController(), timer = setTimeout(() => ctl.abort(), method === 'GET' ? 45000 : 120000);
  let r;
  try {
    r = await fetch('/api/' + path, { method, signal: ctl.signal, body: body === undefined ? undefined : JSON.stringify(body),
      headers: { 'Content-Type': 'application/json', 'X-Init-Data': tg ? tg.initData : '' } });
  } catch (e) {
    const err = new Error(e && e.name === 'AbortError' ? 'Бот не ответил вовремя. Попробуйте ещё раз.' : 'Нет соединения с ботом. Проверьте интернет.');
    err.status = 0; throw err;
  } finally { clearTimeout(timer); }
  let j = null; try { j = await r.json(); } catch (e) { /* не JSON */ }
  if (!r.ok) { const err = new Error((j && (j.detail || j.error)) || `Ошибка ${r.status}`); err.status = r.status; throw err; }
  return j;
}
const haptic = k => { try { tg && tg.HapticFeedback && (k === 'ok' ? tg.HapticFeedback.notificationOccurred('success') : k === 'bad' ? tg.HapticFeedback.notificationOccurred('error') : tg.HapticFeedback.selectionChanged()); } catch (e) { /* нет вибро */ } };
/* классический интерфейс: разделы, которые ещё не перенесены, открываются там */
function classic(open) { location.href = '/admin?classic=1' + (open ? '&open=' + encodeURIComponent(open) : ''); }

/* ── аватарки: фото из Telegram (через бота) или инициалы; каждое фото грузим один раз за сессию ── */
const AVA = new Map();           // tg_id → objectURL | null (фото нет)
const avaQueue = [], AVA_PAR = 4; // не больше 4 загрузок одновременно — список не ждёт картинок
let avaBusy = 0, avaRedraw = 0;
function avaPump() {
  while (avaBusy < AVA_PAR && avaQueue.length) {
    const id = avaQueue.shift();
    avaBusy++;
    fetch('/api/avatar/' + id, { headers: { 'X-Init-Data': tg ? tg.initData : '' } })
      .then(r => (r.ok ? r.blob() : null)).then(b => { AVA.set(id, b && b.size ? URL.createObjectURL(b) : null); })
      .catch(() => { AVA.set(id, null); })
      .then(() => { avaBusy--; avaPump(); clearTimeout(avaRedraw); avaRedraw = setTimeout(() => window.render && render(true), 120); });  // одна перерисовка на пачку
  }
}
const AVA_BG = ['#3B5BDB', '#0C8599', '#2B8A3E', '#E67700', '#C2255C', '#7048E8', '#1971C2', '#5C940D'];
function avatar(user, size = 32) {
  const id = Number(user && (user.tg_id || user.id || user.uid)) || 0;
  const name = String((user && (user.first_name || user.who || user.label || user.username || user.name)) || '?').replace(/^@/, '');
  const letters = name.split(/\s+/).filter(Boolean).slice(0, 2).map(w => w[0].toUpperCase()).join('') || '?';
  const el = h('span', { class: 'avatar', 'aria-hidden': 'true', style: `width:${size}px;height:${size}px;font-size:${Math.round(size * 0.38)}px;background:${AVA_BG[id % AVA_BG.length]};color:#fff` }, letters);
  if (id > 0) {
    const url = AVA.get(id);
    if (url) el.replaceChildren(h('img', { src: url, alt: '', width: size, height: size, decoding: 'async' }));
    else if (!AVA.has(id)) { AVA.set(id, undefined); avaQueue.push(id); avaPump(); }  // undefined — уже в очереди
  }
  return el;
}

/* ── мелкие компоненты ── */
const btn = (label, onclick, cls = '', icon, extra = {}) => h('button', { type: 'button', class: 'btn ' + cls, onclick, ...extra }, icon ? ic(icon) : null, label);
function toggle(checked, onchange, label) {
  return h('span', { class: 'tg' }, h('input', { type: 'checkbox', role: 'switch', checked, 'aria-label': label, onchange: e => onchange(e.target.checked, e.target) }), h('i'));
}
function seg(options, value, onpick, label) {
  return h('div', { class: 'seg', role: 'group', 'aria-label': label },
    options.map(o => h('button', { type: 'button', 'aria-pressed': String(o.v === value), onclick: () => onpick(o.v) }, o.l, o.n != null ? h('span', { class: 'n' }, o.n) : null)));
}
function empty(icon, title, text, action) {
  return h('div', { class: 'empty sheet' }, h('div', { class: 'ic-tile' }, ic(icon)), h('b', null, title), h('div', null, text), action || null);
}
async function copy(text, what) {
  try { await navigator.clipboard.writeText(text); toast(`${what || 'Текст'} скопирован`, text); }
  catch (e) { toast('Не получилось скопировать', 'Выделите и скопируйте вручную: ' + text, 'bad'); }
}

/* ── уведомления ── */
function toast(title, text, kind = 'ok', action) {
  let host = $('.toasts'); if (!host) { host = h('div', { class: 'toasts', role: 'status', 'aria-live': 'polite' }); document.body.append(host); }
  const close = () => { el.classList.add('out'); setTimeout(() => el.remove(), 200); };
  const el = h('div', { class: 'toast' }, h('span', { class: kind }, ic(kind === 'bad' ? 'alert' : kind === 'info' ? 'info' : 'check')),
    h('div', { class: 't' }, h('b', null, title), text ? h('span', null, text) : null),
    action ? h('button', { type: 'button', style: 'opacity:1;font-weight:600;text-decoration:underline', onclick: () => { close(); action.fn(); } }, action.l) : null,
    h('button', { type: 'button', onclick: close, 'aria-label': 'Закрыть' }, ic('x')));
  host.append(el); haptic(kind);
  setTimeout(close, action ? 8000 : kind === 'bad' ? 7000 : 4200);
}

/* ── окна ── */
const layers = [];
function modal({ title, body, actions = [], icon }) {
  const prev = document.activeElement;
  const close = () => { wrap.remove(); const i = layers.indexOf(close); if (i >= 0) layers.splice(i, 1); syncBack(); prev && prev.focus && prev.focus(); };
  const box = h('div', { class: 'modal', role: 'dialog', 'aria-modal': 'true', 'aria-label': title },
    h('div', { class: 'modal-h' }, icon ? h('div', { class: 'attn-ic ' + icon[1] }, ic(icon[0])) : null, h('h3', { style: 'flex:1;margin-top:5px' }, title),
      h('button', { type: 'button', class: 'icon-btn x-close', style: 'width:32px;height:32px;border:0', onclick: close, 'aria-label': 'Закрыть' }, ic('x'))),
    h('div', { class: 'modal-b' }, body),
    actions.length ? h('div', { class: 'modal-f' }, actions.map(a => {
      const b = h('button', { type: 'button', class: 'btn ' + (a.cls || ''), disabled: a.disabled, onclick: () => a.fn ? a.fn(close, b) : close() }, a.l);
      a.ref && a.ref(b); return b;
    })) : null);
  const wrap = h('div', { class: 'modal-wrap', onmousedown: e => { if (e.target === wrap) close(); } }, box);
  document.body.append(wrap); layers.push(close); syncBack();
  requestAnimationFrame(() => { box.classList.add('show'); const f = box.querySelector('input, select, textarea, .modal-f .btn.primary, .modal-f .btn.solid'); f && f.focus(); });
  return close;
}
/* действие с подтверждением: кнопка крутится, пока бот отвечает; ошибка — в окне, а не молча */
function confirmAct({ title, text, ok, cls = 'primary', fn, icon }) {
  const err = h('div', { class: 'err', role: 'alert', style: 'margin-top:10px' });
  modal({ title, icon, body: h('div', null, text, err), actions: [{ l: 'Отмена' }, { l: ok, cls, fn: async (c, b) => {
    b.classList.add('busy'); b.disabled = true; err.textContent = '';
    try { await fn(); c(); } catch (e) { err.textContent = e.message; b.classList.remove('busy'); b.disabled = false; haptic('bad'); }
  } }] });
}
function confirmTyped({ title, text, word, ok, fn }) {
  let b;
  const err = h('div', { class: 'err', role: 'alert' });
  const inp = h('input', { class: 'inp mono', autocomplete: 'off', spellcheck: 'false', 'aria-label': `Введите ${word}`, oninput: e => { b.disabled = e.target.value.trim().toLowerCase() !== word.toLowerCase(); } });
  modal({ title, icon: ['trash', 'bad'], body: h('div', { class: 'form' }, h('div', null, text), h('div', { class: 'fld' }, h('label', null, 'Чтобы подтвердить, введите ', h('span', { class: 'mono' }, word)), inp), err),
    actions: [{ l: 'Отмена' }, { l: ok, cls: 'danger solid', disabled: true, ref: x => { b = x; }, fn: async (c, x) => {
      x.classList.add('busy'); x.disabled = true; err.textContent = '';
      try { await fn(); c(); } catch (e) { err.textContent = e.message; x.classList.remove('busy'); x.disabled = false; }
    } }] });
}

/* ── кнопка «Назад» Telegram: закрывает верхнее окно ── */
let backHandler = null;
function topClose() { if (layers.length) return layers[layers.length - 1]; return backHandler; }
function syncBack() {
  if (!nativeBack) return;
  try { topClose() ? tg.BackButton.show() : tg.BackButton.hide(); } catch (e) { /* старый клиент */ }
}
try { nativeBack && tg.BackButton.onClick(() => { const c = topClose(); c && c(); }); } catch (e) { /* старый клиент */ }
document.addEventListener('keydown', e => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); window.openPalette && openPalette(); return; }
  if (e.key === 'Escape') { const c = topClose(); c && !c.nav && c(); }  // Esc закрывает окна, но не уводит со страницы
});
window.addEventListener('scroll', () => { const t = $('.topbar'); t && t.classList.toggle('scrolled', window.scrollY > 4); }, { passive: true });

/* ── тема: как в Telegram или своя (запоминается на этом устройстве) ── */
const THEME_KEY = 'bn:app-theme';
let themePref = 'auto';
try { themePref = localStorage.getItem(THEME_KEY) || 'auto'; } catch (e) { /* без хранилища */ }
const mq = matchMedia('(prefers-color-scheme: dark)');
function applyTheme() {
  const dark = themePref === 'auto' ? (tg && tg.colorScheme ? tg.colorScheme === 'dark' : mq.matches) : themePref === 'dark';
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  try { if (tg && tg.isVersionAtLeast('6.1')) { const c = dark ? '#0E1421' : '#F3F4F7'; tg.setHeaderColor(c); tg.setBackgroundColor(c); } } catch (e) { /* старый клиент */ }
}
function setTheme(v) { themePref = v; try { localStorage.setItem(THEME_KEY, v); } catch (e) { /* без хранилища */ } applyTheme(); }
mq.addEventListener('change', applyTheme);
try { tg && tg.onEvent('themeChanged', applyTheme); } catch (e) { /* старый клиент */ }
applyTheme();
