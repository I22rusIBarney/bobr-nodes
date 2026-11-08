'use strict';
/* Новый интерфейс Bobr Nodes — экраны. Данные — те же запросы, что у классического интерфейса.
   Классический интерфейс остаётся рядом: в него можно вернуться в «Интерфейс и тема». */

const D = { st: null, err: null, loading: true, nodes: {}, nerr: {}, mon: null, jobs: [], rent: {}, wallet: null, detail: {}, job: {}, diag: {}, hist: {} };
const UI = { base: null, lastKey: '', ovKey: '', tabs: {}, nf: { status: 'all', group: 'all', q: '', sort: 'attn', cc: null }, sel: new Set(), selMode: false };
const isAdmin = () => D.st && D.st.role === 'admin';
const groups = () => (D.st ? D.st.profiles : []);
const G = pid => groups().find(p => p.id === pid) || { name: '—' };

/* ── загрузка ── */
let pollTimer = 0;
/* как часто что обновлять: статусы и задачи меняются часто, ноды — из кэша панели на сервере,
   баланс и аренда — внешний Bobr Cloud и меняются редко. Свёрнутое окно не опрашивает ничего. */
const EVERY = { nodes: 60e3, monitor: 60e3, scale: 120e3, wallet: 300e3, rent: 600e3 };
const AT = {};
const due = (k, force) => force || !AT[k] || Date.now() - AT[k] >= EVERY[k];
async function loadAll(force) {
  clearTimeout(pollTimer);
  if (document.hidden && !force) return;  // вернётся к окну — обновим сразу (visibilitychange)
  try { D.st = await api('state'); D.err = null; }
  catch (e) { D.err = e; D.loading = false; render(true); pollTimer = setTimeout(loadAll, 30000); return; }
  if (D.st.role === 'paused') { D.loading = false; render(true); return; }  // подписка на паузе — только экран оплаты
  try { localStorage.setItem('bn:ui', D.st.theme && D.st.theme.ui === 'classic' ? 'classic' : 'new'); } catch (e) {}  // /admin откроет сразу нужный
  if (D.st.theme) syncTheme(D.st.theme.mode);
  if (D.st.theme && D.st.theme.ui === 'classic') { location.replace('/admin?classic=1'); return; }  // классический выбрали на другом устройстве
  const first = D.loading, mark = k => { AT[k] = Date.now(); };
  await Promise.all([
    api('jobs').then(r => { D.jobs = r.jobs || []; }).catch(() => {}),
    ...(due('nodes', force || first) ? (mark('nodes'), groups().map(p => loadNodes(p.id))) : []),
    due('monitor', force || first) ? (mark('monitor'), api('monitor').then(r => { D.mon = r; }).catch(() => { D.mon = null; })) : null,
    due('wallet', force || first) ? (mark('wallet'), api('wallet').then(r => { D.wallet = r; }).catch(() => { D.wallet = null; })) : null,
    due('scale', force || first) ? (mark('scale'), api('scale/pending').then(r => { D.scale = r.pending || []; }).catch(() => { D.scale = []; })) : null,
  ]);
  D.loading = false; render(true);
  // аренда — из Bobr Cloud (по 2 запроса на группу): после первой отрисовки, раз в 10 минут или если её сбросили (продление)
  const rentFor = groups().filter(p => due('rent', first) || D.rent[p.id] == null);
  if (rentFor.length) { if (due('rent', first)) mark('rent'); Promise.all(rentFor.map(p => api(`profiles/${p.id}/rent`).then(r => { D.rent[p.id] = r.servers || []; }).catch(() => {}))).then(() => render(true)); }
  pollTimer = setTimeout(loadAll, activeJobs().length ? 6000 : 30000);
}
document.addEventListener('visibilitychange', () => { if (!document.hidden && D.st) loadAll(); });
async function loadNodes(pid, fresh) {
  try {
    const r = await api(`profiles/${pid}/nodes` + (fresh ? '?fresh=1' : ''));
    D.nodes[pid] = r; delete D.nerr[pid];
    if (r.stale && !fresh) loadNodes(pid, true).then(() => render(true));  // устаревший кэш показали сразу, свежий — следом
  } catch (e) { D.nerr[pid] = e.message; }
}
async function refreshSoon() { await new Promise(r => setTimeout(r, 1200)); loadAll(true); }  // после действия — всё сразу
const activeJobs = () => D.jobs.filter(j => !['done', 'failed'].includes(j.status));

/* ── ноды всех групп одной таблицей ── */
let memo = null;
function allNodes() {
  if (memo) return memo;
  const out = [];
  for (const p of groups()) {
    const L = D.nodes[p.id]; if (!L) continue;
    const mg = D.mon && (D.mon.groups || []).find(g => g.id === p.id);
    const rent = D.rent[p.id] || [];
    for (const n of L.nodes) {
      const host = n.domain || n.address;
      out.push({ ...n, id: p.id + '~' + n.uuid, pid: p.id, group: p.name, target: (L.versions || {}).target || '',
        mon: mg ? (mg.nodes || []).find(x => x.uuid === n.uuid && (x.kind || 'node') === 'node') || null : null,
        rent: rent.find(s => s.domain === host || s.domain === n.address || s.name === n.name) || null });
    }
  }
  return (memo = out);
}
const N = id => allNodes().find(n => n.id === id);
const ruState = n => { const s = n.mon && n.mon.status; return !s || !n.mon.total ? 'none' : s === 'ok' ? 'ok' : ['down', 'gave_up', 'confirm'].includes(s) ? 'bad' : ['repairing', 'replaced'].includes(s) ? 'run' : 'ign'; };
const waitsMe = n => !!(n.mon && n.mon.status === 'confirm' && n.mon.tok);  // починка остановилась перед платным шагом
const outdated = n => !!(n.version && n.target && n.version !== n.target && !n.disabled && !n.busy);
const rentSoon = n => !!(n.rent && (n.rent.risk || (n.rent.days_left <= 3 && !n.rent.auto_renew)));
const offline = n => !n.connected && !n.disabled;
const urgent = n => offline(n) || ruState(n) === 'bad' || rentSoon(n);
function conn(n) {
  if (n.disabled) return { cls: 'off', word: 'Выключена', short: 'ВЫКЛ' };
  if (n.busy) return { cls: 'run', word: 'Идёт задача', short: 'В РАБОТЕ' };
  if (!n.connected) return { cls: 'bad', word: 'Нет связи', short: 'НЕТ СВЯЗИ' };
  if (waitsMe(n)) return { cls: 'warn', word: 'Ждёт решения', short: 'РЕШЕНИЕ' };
  if (ruState(n) === 'bad') return { cls: 'warn', word: 'Не из России', short: 'БЛОК РФ' };
  if (ruState(n) === 'run') return { cls: 'run', word: 'Чинится', short: 'ЧИНИТСЯ' };
  return { cls: 'ok', word: 'На связи', short: 'НА СВЯЗИ' };
}
const stChip = n => { const c = conn(n); return h('span', { class: 'st ' + c.cls }, c.word); };

/* ── роутер и отрисовка ── */
const OVERLAYS = { node: 'nodes', task: 'activity', deploy: 'overview' };
const parse = () => { const [name = 'overview', ...rest] = location.hash.replace(/^#\/?/, '').split('/'); return { name: name || 'overview', arg: rest.join('/') ? decodeURIComponent(rest.join('/')) : '' }; };
const keyOf = r => r.name + (r.arg ? '/' + encodeURIComponent(r.arg) : '');
function go(path) { if (location.hash === '#/' + path) render(); else location.hash = '#/' + path; }
function closeOverlay() { go(UI.base ? keyOf(UI.base) : 'overview'); }
window.addEventListener('hashchange', () => render());

let pending = 0, pressed = false, deferred = false;
// фоновое обновление не должно перерисовать экран посреди нажатия — иначе нажатие теряется
document.addEventListener('pointerdown', () => { pressed = true; }, true);
const release = () => setTimeout(() => { pressed = false; if (deferred) { deferred = false; render(true); } });
document.addEventListener('pointerup', release, true);
document.addEventListener('pointercancel', release, true);
function render(soft) {
  if (soft && pressed) { deferred = true; return; }
  if (soft && document.activeElement && document.activeElement.tagName === 'SELECT') { clearTimeout(pending); pending = setTimeout(() => render(true), 600); return; }
  memo = null;
  const r = parse();
  if (!(r.name in OVERLAYS) && !VIEWS[r.name]) return go('overview');
  if (r.name in OVERLAYS) { if (!UI.base) UI.base = { name: OVERLAYS[r.name] }; } else UI.base = r;
  const a = document.activeElement, fk = a && a.dataset && a.dataset.k, sel = fk && a.selectionStart != null ? [a.selectionStart, a.selectionEnd] : null;
  const scrolls = {}; document.querySelectorAll('[data-keep]').forEach(e => { scrolls[e.dataset.keep] = e.scrollTop; });
  const key = keyOf(UI.base), changed = key !== UI.lastKey;
  UI.lastKey = key;
  $('#app').replaceChildren(shell(UI.base, changed));
  renderOverlay(r);
  if (fk) { const e = document.querySelector(`[data-k="${fk}"]`); if (e) { e.focus({ preventScroll: true }); if (sel && e.setSelectionRange) try { e.setSelectionRange(...sel); } catch (_) { /* не текст */ } } }
  document.querySelectorAll('[data-keep]').forEach(e => { if (scrolls[e.dataset.keep] != null) e.scrollTop = scrolls[e.dataset.keep]; });
  if (changed && !(r.name in OVERLAYS)) window.scrollTo(0, 0);
}
function parentOf(r) {
  const phone = innerWidth < 720;
  if (r.name === 'group') return 'groups';
  if (r.name === 'script') return 'settings/scripts';
  if (r.name === 'report') return 'settings/reports';
  if (r.name === 'settings' && r.arg) return phone ? 'settings' : null;
  if (phone && ['groups', 'settings'].includes(r.name)) return 'more';  // на телефоне группы и настройки открываются из «Ещё»
  return null;
}
function renderOverlay(r) {
  const host = $('#ov');
  const key = r.name in OVERLAYS ? keyOf(r) : '';
  const fresh = key && key.split('/')[0] !== UI.ovKey.split('/')[0];
  UI.ovKey = key;
  document.body.style.overflow = key ? 'hidden' : '';
  // нативная «Назад»: окно — закрыть, мастер — шаг назад, вложенная страница — к родителю; на разделах её нет
  const up = parentOf(UI.base);
  const relayBack = r.name === 'node' && UI.relay && UI.relay.node === r.arg && UI.tabs.node === 'bridge' ? () => { UI.relay = null; render(); } : null;  // мост → список мостов
  backHandler = key ? (r.name === 'deploy' ? wizBack : relayBack || closeOverlay) : up ? Object.assign(() => go(up), { nav: true }) : null; syncBack();
  if (!key || D.loading || D.err) return host.replaceChildren();
  let panel;
  if (r.name === 'deploy') {
    if (!UI.wiz) { setTimeout(closeOverlay); return host.replaceChildren(); }
    panel = wizard(); host.replaceChildren(panel);
    if (fresh) requestAnimationFrame(() => requestAnimationFrame(() => panel.classList.add('show'))); else panel.classList.add('show');
    return;
  }
  if (r.name === 'node') { const n = N(r.arg); if (!n) { if (Object.keys(D.nodes).length >= groups().length) setTimeout(closeOverlay); return host.replaceChildren(); } panel = drawerNode(n); }
  else panel = drawerTask(r.arg);
  host.replaceChildren(h('div', { class: 'scrim show', onclick: closeOverlay }), panel);
  if (fresh) requestAnimationFrame(() => requestAnimationFrame(() => panel.classList.add('show'))); else panel.classList.add('show');
}

/* ── оболочка ── */
const NAV = [
  { id: 'overview', l: 'Обзор', i: 'home' },
  { id: 'nodes', l: 'Ноды', i: 'server', cnt: () => allNodes().filter(urgent).length },
  { id: 'groups', l: 'Группы', i: 'layers' },
  { id: 'activity', l: 'Задачи', i: 'activity', cnt: () => D.jobs.filter(j => j.status === 'failed' && Date.now() / 1000 - j.updated_at < 3 * 86400).length },
  { id: 'stats', l: 'Статистика', i: 'chart' },
  { id: 'settings', l: 'Настройки', i: 'sliders' },
];
const SECTION_OF = { node: 'nodes', task: 'activity', group: 'groups', look: 'settings', script: 'settings', report: 'settings' };
function shell(base, changed) {
  const cur = SECTION_OF[base.name] || base.name;
  const navBtn = x => { const c = x.cnt && x.cnt();
    return h('button', { type: 'button', class: 'nav-a', 'aria-current': cur === x.id ? 'page' : null, onclick: () => go(x.id), title: x.l },
      ic(x.i), h('span', { class: 'lbl' }, x.l), c ? h('span', { class: 'cnt', 'aria-label': `требуют внимания: ${c}` }, c) : null); };
  const unread = D.st ? D.st.notif_unread : 0, name = (D.st && D.st.me) || (tg && tg.initDataUnsafe && tg.initDataUnsafe.user && tg.initDataUnsafe.user.first_name) || 'Вы';
  const bal = D.wallet && D.wallet.balance_kop != null ? D.wallet.balance_kop : null;
  return h('div', { class: 'app' },
    h('aside', { class: 'rail', 'aria-label': 'Разделы' },
      h('div', { class: 'brand' }, h('img', { src: '/static/logo.png', alt: '' }), h('span', null, 'Bobr Nodes')),
      NAV.map(navBtn),
      h('div', { class: 'spacer' }),
      h('button', { type: 'button', class: 'me nav-a', style: 'height:auto', onclick: () => go('settings/look'), title: 'Интерфейс и тема' },
        avatar({ tg_id: D.st && D.st.me_id, first_name: name }, 32),
        h('span', { class: 'who' }, h('b', null, name), h('span', null, 'интерфейс и тема')))),
    h('main', { class: 'main' },
      h('header', { class: 'topbar' + (window.scrollY > 4 ? ' scrolled' : '') },
        h('span', { class: 'title-m' }, h('img', { src: '/static/logo.png', alt: 'Bobr Nodes' })),
        h('button', { type: 'button', class: 'search-btn', onclick: () => openPalette(), 'aria-label': 'Поиск и команды' }, ic('search'), h('span', { class: 'lbl' }, 'Найти ноду или действие'), h('kbd', null, '⌘K')),
        h('span', { class: 'sp' }),
        bal != null ? h('button', { type: 'button', class: 'balance', onclick: topUp, title: 'Пополнить баланс Bobr Cloud · ' + (D.wallet.account || '') }, ic('wallet'), h('span', { class: 'num' }, rubK(bal))) : null,
        h('button', { type: 'button', class: 'icon-btn', id: 'bell', onclick: openBell, 'aria-label': `Уведомления${unread ? `, новых: ${unread}` : ''}` }, ic('bell'), unread ? h('span', { class: 'dot' }) : null),
        h('button', { type: 'button', class: 'btn primary hide-m', onclick: () => openDeploy() }, ic('plus'), 'Добавить ноды')),
      h('div', { class: 'page' + (changed ? ' view-in' : '') }, D.err ? errorView() : D.loading ? skeleton() : D.st.role === 'paused' ? paywall() : VIEWS[base.name](base.arg))),
    h('nav', { class: 'tabbar', 'aria-label': 'Разделы' },
      tabBtn(NAV[0], cur), tabBtn(NAV[1], cur),
      h('button', { type: 'button', onclick: () => openDeploy(), 'aria-label': 'Добавить ноды' }, h('span', { class: 'add' }, ic('plus'))),
      tabBtn(NAV[3], cur),
      tabBtn({ id: 'more', l: 'Ещё', i: 'grid' }, ['groups', 'settings', 'stats'].includes(cur) ? 'more' : cur)));
}
function tabBtn(x, cur) {
  const c = x.cnt && x.cnt();
  return h('button', { type: 'button', 'aria-current': cur === x.id ? 'page' : null, onclick: () => go(x.id) }, ic(x.i), x.l, c ? h('span', { class: 'cnt' }, c) : null);
}
function skeleton() {
  const bar = (w, ht = 14, mb = 10) => h('div', { class: 'sk', style: `width:${w};height:${ht}px;margin-bottom:${mb}px` });
  return h('div', { 'aria-busy': 'true', 'aria-label': 'Загрузка' }, bar('180px', 28, 8), bar('320px', 14, 24),
    h('div', { class: 'sheet', style: 'padding:16px' }, [0, 1, 2, 3, 4].map(i => h('div', { style: 'display:flex;gap:12px;align-items:center;margin-bottom:14px' },
      h('div', { class: 'sk', style: 'width:34px;height:34px;border-radius:9px' }), h('div', { style: 'flex:1' }, bar(`${40 + (i * 13) % 40}%`, 12, 6), bar(`${20 + (i * 7) % 25}%`, 10, 0))))));
}
function errorView() {
  const e = D.err;
  return [pageH(e.status === 403 ? 'Нет доступа' : 'Бот не ответил'),
    empty('alert', e.status === 403 ? 'Откройте бота из Telegram' : 'Не получилось загрузить данные', e.message,
      e.status === 403 ? null : btn('Повторить', () => { D.err = null; D.loading = true; render(); loadAll(); }, 'primary', 'retry'))];
}
function pageH(title, sub, acts) {
  return h('div', { class: 'page-h' }, h('div', { style: 'min-width:0' }, h('h1', null, title), sub ? h('p', null, sub) : null),
    acts && acts.length ? h('div', { class: 'act' }, acts) : null);
}
function nodesErrors() {
  return Object.entries(D.nerr).map(([pid, msg]) => h('div', { class: 'notice' }, h('span', { class: 'i' }, ic('unplug')),
    h('div', { class: 'grow' }, h('b', null, `Панель группы «${G(+pid).name}» не ответила`), msg),
    btn('Повторить', async e => { e.currentTarget.classList.add('busy'); await loadNodes(+pid, true); render(); }, 's')));
}

/* ── действия ── */
const jobToast = (title, id) => toast(title, 'Ход — в задаче', 'ok', id ? { l: 'Открыть', fn: () => go('task/' + id) } : null);
function fixNode(n) {
  return api('monitor/action', 'POST', { tok: n.mon.tok, action: 'fix' }).then(r => { toast(`${n.name}: починка запущена`, r.message); refreshSoon(); })
    .catch(e => toast('Не получилось', e.message, 'bad'));
}
function ignoreNode(n) {
  confirmAct({ title: `Не чинить ${n.name}?`, icon: ['shieldX', 'warn'], ok: 'Не чинить',
    text: 'Бот перестанет напоминать и чинить эту ноду, пока она снова не начнёт проходить из России.',
    fn: async () => { const r = await api('monitor/action', 'POST', { tok: n.mon.tok, action: 'ignore' }); toast('Готово', r.message); refreshSoon(); } });
}
function updateNodes(list) {
  list = list.filter(outdated);
  if (!list.length) return toast('Обновлять нечего', 'Выбранные ноды уже на нужной версии', 'info');
  const byPid = {}; list.forEach(n => { (byPid[n.pid] = byPid[n.pid] || []).push(n); });
  confirmAct({ title: `Обновить ${pl(list.length, 'ноду', 'ноды', 'нод')}?`, icon: ['up', 'run'], ok: 'Обновить',
    text: `До версии ${list[0].target}. Бот обновит по одной, проверит каждую и вернёт прежнюю версию, если новая не запустится. Клиенты переподключатся за несколько секунд.`,
    fn: async () => {
      let ids = [], skipped = [];
      for (const [pid, ns] of Object.entries(byPid)) {
        const r = await api(`profiles/${pid}/nodes/upgrade`, 'POST', { uuids: ns.map(n => n.uuid), tag: ns[0].target });
        ids = ids.concat(r.ids || []); skipped = skipped.concat(r.skipped || []);
      }
      toast(ids.length ? `Обновление запущено: ${ids.length}` : 'Ничего не запущено', skipped.length ? 'Пропущены: ' + skipped.map(s => `${s.name} — ${s.why}`).join('; ') : null, ids.length ? 'ok' : 'bad');
      UI.sel.clear(); refreshSoon();
    } });
}
function renew(n) {
  const r = n.rent;
  confirmAct({ title: `Продлить ${n.name}?`, icon: ['clock', 'warn'], ok: `Продлить за ${rubK(r.price_kop)}`,
    text: `Ещё 30 дней аренды. ${rubK(r.price_kop)} спишется с баланса Bobr Cloud` + (D.wallet && D.wallet.balance_kop != null ? `, сейчас там ${rubK(D.wallet.balance_kop)}.` : '.'),
    fn: async () => { const x = await api(`profiles/${n.pid}/rent/${r.id}/renew`, 'POST'); toast(`${n.name}: аренда продлена`, x.message); D.rent[n.pid] = null; refreshSoon(); } });
}
async function setAutoRenew(n, v, input) {
  try { await api(`profiles/${n.pid}/rent/${n.rent.id}/autorenew`, 'POST', { value: v }); n.rent.auto_renew = v; toast(v ? 'Автопродление включено' : 'Автопродление выключено', n.name); }
  catch (e) { input.checked = !v; toast('Не получилось', e.message, 'bad'); }
}
async function retryJob(j) {
  let full;
  try { full = await api('jobs/' + j.id); } catch (e) { return toast('Не получилось', e.message, 'bad'); }
  const rebuy = !full.server_id && (full.stages || []).some(s => s.code === 'buy');
  const run = async () => { const r = await api(`jobs/${j.id}/retry`, 'POST'); toast('Задача запущена снова', r.rebuy ? 'Сервер покупается заново' : null); D.job[j.id] = null; refreshSoon(); };
  if (rebuy) confirmAct({ title: 'Купить сервер заново?', icon: ['wallet', 'warn'], ok: 'Повторить с покупкой', cls: 'danger solid',
    text: 'В прошлый раз сервер так и не был создан, поэтому повтор купит его снова и спишет оплату с баланса Bobr Cloud. Bobr Cloud сам возвращает деньги за несозданные серверы.', fn: run });
  else run().catch(e => toast('Не получилось', e.message, 'bad'));
}

/* ── «Требует внимания» ── */
function attention() {
  const ns = allNodes(), out = [], st = D.st;
  if (st.issues && st.issues.length) out.push({ sev: 'warn', icon: 'sliders', title: 'Нужна настройка', text: st.issues.join(', '),
    acts: [{ l: 'Настроить', p: 1, fn: () => go(isAdmin() ? 'settings/connections' : 'groups') }] });
  if (st.host && st.host.level === 'bad') out.push({ sev: 'bad', icon: 'server', title: 'Серверу бота не хватает ресурсов', text: `Процессор ${st.host.cpu ?? '—'}%, память ${st.host.mem ?? '—'}%`,
    acts: [{ l: 'Подробнее', fn: () => go('settings/health') }] });
  for (const n of ns.filter(offline))
    out.push({ sev: 'bad', icon: 'unplug', title: `${n.name} не на связи`, text: `${n.group} · панель не видит ноду`,
      acts: [{ l: 'Открыть', p: 1, fn: () => go('node/' + n.id) }] });
  for (const n of ns.filter(waitsMe))
    out.push({ sev: 'bad', icon: 'wallet', title: `${n.name}: нужно ваше решение`,
      text: n.mon.step === 'replace' ? 'Бесплатные шаги починки не помогли. Купить новую ноду в той же локации? Домен, тариф и сквады — на ваш выбор.'
        : 'Бесплатные шаги починки не помогли. Сменить IP в Bobr Cloud? Это платно.',
      acts: [{ l: n.mon.step === 'replace' ? 'Выбрать и купить' : 'Сменить IP', p: 1, fn: () => n.mon.step === 'replace' ? replaceNode(n, true) : confirmReip(n) }, { l: 'Не трогать', fn: () => ignoreNode(n) }] });
  for (const n of ns.filter(n => ruState(n) === 'bad' && !waitsMe(n)))
    out.push({ sev: 'warn', icon: 'shieldX', title: `${n.name} не проходит из России`,
      text: `${n.mon.since ? since(n.mon.since * 1000) + ' · ' : ''}${n.mon.status === 'gave_up' ? 'автопочинка не помогла · ' : ''}${n.online != null ? `у ${n.online} пользователей может не быть связи` : n.group}. Платное — только после вашего подтверждения`,
      acts: [{ l: 'Починить', p: 1, fn: () => fixNode(n) }, { l: 'Новая нода', fn: () => replaceNode(n, false) }, { l: 'Не чинить', fn: () => ignoreNode(n) }] });
  for (const a of (D.scale || []))
    out.push({ sev: 'run', icon: 'plus', title: `${a.name} перегружена — докупить ноду рядом?`, text: `${a.group} · ${a.label}${a.price_kop ? ', ' + rubK(a.price_kop) + '/мес' : ''}. Без вашего подтверждения бот ничего не покупает`,
      acts: [{ l: 'Выбрать и докупить', p: 1, fn: () => scaleNode(a) }] });
  const failed = D.jobs.filter(j => j.status === 'failed' && Date.now() / 1000 - j.updated_at < 3 * 86400);
  if (failed.length > 3) out.push({ sev: 'bad', icon: 'alert', title: `${pl(failed.length, 'задача', 'задачи', 'задач')} с ошибкой за 3 дня`, text: failed.slice(0, 3).map(j => j.domain).join(', ') + '…',
    acts: [{ l: 'Показать', p: 1, fn: () => { UI.af = 'failed'; go('activity'); } }] });
  else for (const j of failed) out.push({ sev: 'bad', icon: 'alert', title: `${jobTitle(j)} — ошибка`, text: j.error || `остановилась на шаге «${j.stage}»`,
    acts: [{ l: 'Повторить', p: 1, fn: () => retryJob(j) }, { l: 'Подробнее', fn: () => go('task/' + j.id) }] });
  for (const n of ns.filter(rentSoon))
    out.push({ sev: 'warn', icon: 'clock', title: `Аренда ${n.name} кончится через ${pl(Math.max(0, Math.round(n.rent.days_left)), 'день', 'дня', 'дней')}`,
      text: n.rent.risk || `Автопродление выключено · ${rubK(n.rent.price_kop)} за 30 дней`, acts: [{ l: 'Продлить', p: 1, fn: () => renew(n) }, { l: 'Открыть', fn: () => go('node/' + n.id) }] });
  const old = ns.filter(outdated);
  if (old.length) out.push({ sev: 'run', icon: 'up', title: `${pl(old.length, 'нода', 'ноды', 'нод')} на старой версии`, text: `Есть ${old[0].target}`,
    acts: [{ l: 'Обновить все', p: 1, fn: () => updateNodes(old) }, { l: 'Показать', fn: () => { UI.nf = { ...UI.nf, status: 'old', cc: null, q: '', group: 'all' }; go('nodes'); } }] });
  return out;
}
function attnRow(a) {
  return h('div', { class: 'attn-item' }, h('div', { class: 'attn-ic ' + a.sev }, ic(a.icon)),
    h('div', { class: 'what' }, h('b', null, a.title), h('span', null, a.text)),
    h('div', { class: 'acts' }, a.acts.map(x => btn(x.l, x.fn, 's ' + (x.p ? 'primary' : '')))));
}
const jobTitle = j => (j.spec && j.spec.startsWith('обновление') ? 'Обновление ' : j.spec === 'смена IP' ? 'Смена IP ' : j.spec === 'дополнения на ноде' ? 'Дополнения ' : 'Установка ') + (j.loc_name && !/[А-я]/.test(j.loc_name) ? j.loc_name : j.domain);

/* ── Обзор ── */
function vOverview() {
  const ns = allNodes(), items = attention(), gs = groups();
  if (!gs.length) return [pageH('Добро пожаловать', 'Подключите панель Remnawave — бот начнёт ставить в неё ноды и следить за ними.'),
    empty('layers', 'Групп пока нет', 'Группа — это панель Remnawave со своим доменом и нодами.', btn('Подключить панель', newGroup, 'primary', 'plus'))];
  const cnt = f => ns.filter(f).length;
  const okN = cnt(n => n.connected && !n.disabled && ruState(n) !== 'bad'), offN = cnt(offline), blkN = cnt(n => ruState(n) === 'bad'), disN = cnt(n => n.disabled),
    online = ns.reduce((s, n) => s + (n.online || 0), 0);
  const byCc = {};
  for (const n of ns) { const c = byCc[n.country || '—'] || (byCc[n.country || '—'] = { n: 0, bad: 0 }); c.n++; if (urgent(n)) c.bad++; }
  const to = p => () => { UI.nf = { ...UI.nf, status: 'all', cc: null, q: '', group: 'all', ...p }; go('nodes'); };
  const stat = (k, l, p, cls) => h('button', { type: 'button', class: 'stat', onclick: to(p) }, h('span', { class: 'k ' + (cls || '') }, k), h('span', { class: 'l' }, l));
  const loadingNodes = gs.some(p => !D.nodes[p.id] && !D.nerr[p.id]);
  return [
    pageH('Обзор', `${pl(ns.length, 'нода', 'ноды', 'нод')} в ${pl(gs.length, 'группе', 'группах', 'группах')}` + (D.mon && D.mon.every ? ` · проверка из России раз в ${Math.round(D.mon.every / 60)} мин` : '')),
    nodesErrors(),
    h('section', { class: 'section', 'aria-labelledby': 'h-attn' },
      h('div', { class: 'section-h' }, h('h2', { id: 'h-attn' }, 'Требует внимания'), items.length ? h('span', { class: 'tag' }, items.length) : null),
      items.length ? h('div', { class: 'airmail' }, h('div', { class: 'inner' }, items.map(attnRow)))
        : h('div', { class: 'sheet all-clear' }, h('div', { class: 'attn-ic', style: 'background:var(--ok-soft);color:var(--ok)' }, ic('check')),
          h('div', null, h('b', null, loadingNodes ? 'Проверяю панели…' : 'Всё в порядке'), h('div', { class: 'muted' }, 'Ноды на связи и проходят из России. Если что-то случится, бот напишет в Telegram.')))),
    h('div', { class: 'grid2' },
      h('div', null,
        h('section', { class: 'section' },
          h('div', { class: 'section-h' }, h('h2', null, 'Парк'), btn('Все ноды', to({}), 'ghost s more')),
          h('div', { class: 'sheet fleet' },
            h('div', { class: 'stats' },
              stat(okN, 'в порядке', { status: 'ok' }), offN ? stat(offN, 'нет связи', { status: 'off' }, 'bad') : null, blkN ? stat(blkN, 'не из России', { status: 'blocked' }, 'warn') : null,
              disN ? stat(disN, 'выключено', {}, 'faint') : null,
              h('div', { class: 'stat', style: 'cursor:default' }, h('span', { class: 'k' }, online.toLocaleString('ru-RU')), h('span', { class: 'l' }, 'онлайн сейчас'))),
            h('div', { class: 'countries', role: 'list', 'aria-label': 'Страны' }, Object.entries(byCc).sort((a, b) => b[1].n - a[1].n).map(([cc, c]) =>
              h('button', { type: 'button', role: 'listitem', class: 'cty' + (c.bad ? ' has-bad' : ''), title: c.bad ? `${c.bad} требуют внимания` : '', onclick: to({ cc }) },
                flag(cc) ? h('span', { class: 'flag' }, flag(cc)) : null, cc, h('span', { class: 'n' }, c.bad ? `${c.bad}/${c.n}` : c.n)))))),
        h('section', { class: 'section' },
          h('div', { class: 'section-h' }, h('h2', null, 'Группы'), btn('Все группы', () => go('groups'), 'ghost s more')),
          h('div', { class: 'list' }, gs.map(groupRow)))),
      h('section', { class: 'section' },
        h('div', { class: 'section-h' }, h('h2', null, 'Недавно'), btn('Все задачи', () => go('activity'), 'ghost s more')),
        D.jobs.length ? h('div', { class: 'list' }, D.jobs.slice(0, 7).map(taskRow)) : empty('activity', 'Пока тихо', 'Здесь появятся установки, обновления и починки.'))),
    statsSection(),
  ];
}
function groupRow(p) {
  const ns = allNodes().filter(n => n.pid === p.id), bad = ns.filter(urgent).length;
  const who = p.kind === 'user' ? p.created_by_label : p.kind === 'shared' ? 'от администратора' : '';
  return h('button', { type: 'button', class: 'li', onclick: () => go('group/' + p.id) },
    h('div', { class: 'ic-tile' }, ic('layers')),
    h('div', { class: 'grow' }, h('div', { class: 't' }, p.name), h('div', { class: 's' }, `${(p.panel_url || '').replace(/^https?:\/\//, '')} · ${D.nodes[p.id] ? pl(ns.length, 'нода', 'ноды', 'нод') : 'загружаю…'}`)),
    who ? h('span', { class: 'tag hide-m' }, who) : null,
    D.nerr[p.id] ? h('span', { class: 'st bad' }, 'панель не отвечает') : ns.length ? h('span', { class: 'st ' + (bad ? 'bad' : 'ok') }, bad ? `${bad} с проблемой` : 'в порядке') : null,
    h('span', { class: 'chev' }, ic('chevR')));
}
const JOB_ST = { done: ['ok', 'готово'], failed: ['bad', 'ошибка'] };
function taskRow(j) {
  const [cls, word] = JOB_ST[j.status] || ['run', 'идёт'];
  const ts = (j.status === 'done' || j.status === 'failed' ? j.updated_at : j.created_at) * 1000;
  return h('button', { type: 'button', class: 'li', onclick: () => go('task/' + j.id) },
    h('div', { class: 'attn-ic ' + (cls === 'ok' ? '' : cls), style: cls === 'ok' ? 'background:var(--ok-soft);color:var(--ok)' : '' }, ic(j.spec && j.spec.startsWith('обновление') ? 'up' : j.spec === 'смена IP' ? 'globe' : 'box')),
    h('div', { class: 'grow' }, h('div', { class: 't' }, jobTitle(j)), h('div', { class: 's' }, `${ago(ts)} · ${j.profile}`)),
    h('span', { class: 'st ' + cls }, word), h('span', { class: 'chev' }, ic('chevR')));
}

/* ── Ноды ── */
const FILTERS = [
  { v: 'all', l: 'Все', f: () => true }, { v: 'attn', l: 'Внимание', f: urgent },
  { v: 'ok', l: 'На связи', f: n => n.connected && !n.disabled }, { v: 'off', l: 'Нет связи', f: offline },
  { v: 'blocked', l: 'Не из России', f: n => ruState(n) === 'bad' }, { v: 'old', l: 'Старая версия', f: outdated },
];
const SORTS = { attn: 'Сначала проблемные', name: 'По имени', load: 'По нагрузке', online: 'По онлайну' };
const rank = n => (offline(n) ? 0 : ruState(n) === 'bad' ? 1 : rentSoon(n) ? 2 : n.busy ? 3 : outdated(n) ? 4 : n.disabled ? 6 : 5);
function filtered(ns, f) {
  const q = f.q.trim().toLowerCase(), F = (FILTERS.find(x => x.v === f.status) || FILTERS[0]).f;
  return ns.filter(n => F(n) && (f.group === 'all' || String(n.pid) === f.group) && (!f.cc || (n.country || '—') === f.cc) &&
    (!q || [n.name, n.address, n.domain, n.country, n.group].some(s => (s || '').toLowerCase().includes(q))))
    .sort((a, b) => f.sort === 'name' ? a.name.localeCompare(b.name) : f.sort === 'load' ? ((b.load || {}).cpu ?? -1) - ((a.load || {}).cpu ?? -1)
      : f.sort === 'online' ? (b.online || 0) - (a.online || 0) : rank(a) - rank(b) || a.name.localeCompare(b.name));
}
function vNodes() {
  if (UI.nodesTab === 'hosts') return vHosts();
  const all = allNodes(), f = UI.nf, gs = groups();
  if (f.group !== 'all' && !gs.some(p => String(p.id) === f.group)) f.group = 'all';
  const add = btn('Добавить ноды', () => openDeploy(), 'primary', 'plus');
  const loadingNodes = gs.some(p => !D.nodes[p.id] && !D.nerr[p.id]);
  if (!all.length) return [pageH('Ноды'), nodesTabs(), nodesErrors(), loadingNodes ? skeleton() : empty('server', 'Нод пока нет', gs.length ? 'Выберите страны и тариф — бот купит серверы, поставит ноды и пропишет DNS.' : 'Сначала подключите панель Remnawave.', gs.length ? add : btn('Подключить панель', newGroup, 'primary', 'plus'))];
  const scoped = all.filter(n => (f.group === 'all' || String(n.pid) === f.group) && (!f.cc || (n.country || '—') === f.cc));
  const list = filtered(all, f);
  const set = p => { UI.nf = { ...UI.nf, ...p }; render(); };
  return [
    pageH('Ноды', `${pl(all.length, 'нода', 'ноды', 'нод')} во всех группах · строка открывает карточку`),
    nodesTabs(),
    nodesErrors(),
    h('div', { class: 'toolbar' },
      seg(FILTERS.map(x => ({ v: x.v, l: x.l, n: scoped.filter(x.f).length })).filter(x => x.v === 'all' || x.n || x.v === f.status), f.status, v => set({ status: v }), 'Состояние'),
      h('label', { class: 'field-s' }, ic('search'), h('span', { class: 'sr' }, 'Поиск'),
        h('input', { 'data-k': 'nq', placeholder: 'Имя, домен, страна', value: f.q, oninput: e => set({ q: e.target.value }) }),
        f.q ? h('button', { type: 'button', class: 'icon-btn', style: 'width:24px;height:24px;border:0', 'aria-label': 'Очистить', onclick: () => set({ q: '' }) }, ic('x')) : null),
      gs.length > 1 ? h('select', { class: 'sel', 'aria-label': 'Группа', value: f.group, onchange: e => set({ group: e.target.value }) },
        h('option', { value: 'all' }, 'Все группы'), gs.map(p => h('option', { value: String(p.id) }, p.name))) : null,
      h('select', { class: 'sel hide-m', 'aria-label': 'Сортировка', value: f.sort, onchange: e => set({ sort: e.target.value }) }, Object.entries(SORTS).map(([v, l]) => h('option', { value: v }, l))),
      h('button', { type: 'button', class: 'btn show-m', 'aria-pressed': String(UI.selMode), onclick: () => { UI.selMode = !UI.selMode; if (!UI.selMode) UI.sel.clear(); render(); } }, UI.selMode ? 'Готово' : 'Выбрать')),
    f.cc ? h('div', { style: 'margin:-2px 0 12px' }, h('button', { type: 'button', class: 'chip', 'aria-pressed': 'true', onclick: () => set({ cc: null }) },
      flag(f.cc) ? h('span', { class: 'flag' }, flag(f.cc)) : null, f.cc, ic('x'))) : null,
    list.length ? [nodesTable(list, gs.length > 1 && f.group === 'all'), nodeCards(list)]
      : empty('search', 'Ничего не нашлось', 'Попробуйте другой запрос или сбросьте фильтры.', btn('Сбросить фильтры', () => set({ status: 'all', q: '', cc: null, group: 'all' }))),
    bulkBar(),
  ];
}
function ruCell(n) {
  const s = ruState(n);
  if (!n.connected || n.disabled || s === 'none') return h('span', { class: 'faint', title: s === 'none' ? 'Чекеры ещё не проверяли эту ноду' : null }, '—');
  return s === 'bad' ? h('span', { class: 'ru bad' }, ic('shieldX'), 'не проходит') : s === 'run' ? h('span', { class: 'ru', style: 'color:var(--run)' }, ic('refresh'), 'чинится')
    : s === 'ign' ? h('span', { class: 'faint' }, 'не чиним') : h('span', { class: 'ru ok' }, ic('check'), 'проходит');
}
function loadBar(l) {
  const v = l && l.cpu;
  if (v == null) return h('span', { class: 'faint' }, '—');
  return h('span', { class: 'load' + (v >= 80 ? ' hot' : ''), title: `Процессор ${v}%` + (l.mem != null ? `, память ${l.mem}%` : '') }, h('span', { class: 'bar' }, h('i', { style: `width:${v}%` })), h('span', { class: 'num' }, v + '%'));
}
const verCell = n => !n.version ? h('span', { class: 'faint' }, '—') : outdated(n) ? h('span', { class: 'tag warn mono', title: `Есть ${n.target}` }, n.version) : h('span', { class: 'mono faint' }, n.version);
function toggleSel(id) { UI.sel.has(id) ? UI.sel.delete(id) : UI.sel.add(id); render(); }
function nodesTable(list, groupCol) {
  const allSel = list.every(n => UI.sel.has(n.id));
  return h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl' },
    h('thead', null, h('tr', null,
      h('th', { style: 'width:44px' }, h('input', { type: 'checkbox', class: 'cb', 'aria-label': 'Выбрать все', checked: allSel && list.length > 0,
        onchange: e => { list.forEach(n => e.target.checked ? UI.sel.add(n.id) : UI.sel.delete(n.id)); render(); } })),
      h('th', null, 'Нода'), groupCol ? h('th', { class: 'c-group' }, 'Группа') : null, h('th', null, 'Связь'), h('th', null, 'Из России'),
      h('th', { class: 'r' }, 'Онлайн'), h('th', null, 'Версия'), h('th', null, 'Нагрузка'))),
    h('tbody', null, list.map(n => h('tr', { class: UI.sel.has(n.id) ? 'sel' : '', tabindex: '0', onclick: () => go('node/' + n.id), onkeydown: e => { if (e.key === 'Enter') go('node/' + n.id); } },
      h('td', { onclick: e => e.stopPropagation() }, h('input', { type: 'checkbox', class: 'cb', 'aria-label': 'Выбрать ' + n.name, checked: UI.sel.has(n.id), onchange: () => toggleSel(n.id) })),
      h('td', null, h('div', { class: 'node-cell' }, h('span', { class: 'flag', title: n.country }, flag(n.country)), h('div', { style: 'min-width:0' }, h('b', null, n.name), h('span', null, n.domain || n.address)))),
      groupCol ? h('td', { class: 'muted c-group' }, n.group) : null,
      h('td', null, stChip(n)), h('td', null, ruCell(n)),
      h('td', { class: 'r num' }, n.online ?? '—'), h('td', null, verCell(n)), h('td', null, loadBar(n.load)))))));
}
function nodeCards(list) {
  return h('div', { class: 'cards' }, list.map(n => h('button', { type: 'button', class: 'node-card' + (UI.sel.has(n.id) ? ' sel' : ''), onclick: () => UI.selMode ? toggleSel(n.id) : go('node/' + n.id) },
    UI.selMode ? h('span', { class: 'cb-m' + (UI.sel.has(n.id) ? ' on' : '') }, UI.sel.has(n.id) ? ic('check') : null) : h('span', { class: 'flag' }, flag(n.country)),
    h('div', { class: 'grow' }, h('div', { class: 't' }, n.name, outdated(n) ? h('span', { class: 'tag warn mono' }, n.version) : null, rentSoon(n) ? h('span', { class: 'tag warn' }, 'аренда') : null),
      h('div', { class: 's' }, n.domain || n.address)),
    h('div', { class: 'side' }, stChip(n), h('span', { class: 'faint num', style: 'font-size:12.5px' }, n.connected && n.online != null ? `${n.online} онлайн` : '')))));
}
function bulkBar() {
  const list = allNodes().filter(n => UI.sel.has(n.id));
  if (!list.length) return null;
  return h('div', { class: 'bulk', role: 'toolbar', 'aria-label': 'Действия с выбранными' },
    h('b', null, `Выбрано: ${list.length}`),
    btn('Обновить', () => updateNodes(list), 's primary', 'up', { disabled: !list.some(outdated), title: list.some(outdated) ? null : 'Все выбранные уже на нужной версии' }),
    btn('Уникальные настройки', () => stealthNodes(list), 's', 'shield'),
    h('span', { class: 'sp' }),
    h('button', { type: 'button', class: 'btn s', 'aria-label': 'Снять выбор', onclick: () => { UI.sel.clear(); UI.selMode = false; render(); } }, ic('x')));
}

/* уникальные настройки на уже стоящих нодах: у каждой свои случайные порт, отпечаток и маска (задача на каждую ноду) */
function stealthNodes(list) {
  confirmAct({ title: `Уникальные настройки для ${pl(list.length, 'ноды', 'нод', 'нод')}?`, icon: ['shield', 'run'], ok: 'Применить',
    text: 'Каждая нода получит свой случайный порт для клиентов (примерно половина останется на 443), TLS-отпечаток и фрагментацию ClientHello. '
      + 'Порт 443 на нодах продолжит работать, так что клиенты со старой подпиской не отвалятся; новые параметры придут с обновлением подписки. Для смены порта нужен доступ по SSH.',
    fn: async () => {
      const by = {}; list.forEach(n => { (by[n.pid] = by[n.pid] || []).push(n.uuid); });
      let ids = 0; const skipped = [];
      for (const [pid, uuids] of Object.entries(by)) {
        const r = await api(`profiles/${pid}/nodes/apply-many`, 'POST', { uuids, stealth: true });
        ids += r.ids.length; skipped.push(...r.skipped.map(x => `${x.name}: ${x.why}`));
      }
      UI.sel.clear(); UI.selMode = false;
      toast(`Запущено: ${pl(ids, 'задача', 'задачи', 'задач')}`, skipped.length ? 'Пропущены — ' + skipped.join('; ') : 'Ход — в «Задачах»', skipped.length ? 'info' : 'ok');
      refreshSoon();
    } });
}

/* ── Карточка ноды ── */
function postmark(n) {
  const c = conn(n), id = 'pm' + n.uuid.replace(/\W/g, '');
  const ring = `BOBR NODES · ${n.name.toUpperCase()} · ${n.group.toUpperCase()} ·`.slice(0, 40);
  return `<svg class="postmark ${c.cls}" viewBox="0 0 100 100" role="img" aria-label="Состояние: ${esc(c.word)}, ${clock()}">
    <defs><path id="${id}" d="M50,50 m-37,0 a37,37 0 1,1 74,0 a37,37 0 1,1 -74,0"/></defs>
    <circle cx="50" cy="50" r="47" fill="none" stroke="currentColor" stroke-width="2.2"/>
    <circle cx="50" cy="50" r="29" fill="none" stroke="currentColor" stroke-width="1.1"/>
    <text font-size="8" textLength="226" lengthAdjust="spacing"><textPath href="#${id}">${esc(ring)}</textPath></text>
    <text x="50" y="48" text-anchor="middle" font-size="${c.short.length > 8 ? 7.4 : 8.4}">${c.short}</text>
    <text x="50" y="61" text-anchor="middle" font-size="10.5">${clock()}</text></svg>`;
}
function loadDetail(n) {
  if (D.detail[n.id] && (D.detail[n.id].loading || Date.now() - D.detail[n.id].at < 20000)) return;
  D.detail[n.id] = { loading: true, at: Date.now() };
  api(`profiles/${n.pid}/nodes/${n.uuid}`).then(r => { D.detail[n.id] = { d: r, at: Date.now() }; }).catch(e => { D.detail[n.id] = { err: e.message, at: Date.now() }; }).then(() => render(true));
}
function drawerNode(n) {
  loadDetail(n);
  const tab = UI.tabs.node || 'overview';
  const TABS = [['overview', 'Обзор'], ['more', 'Настройки'], ['bridge', 'Вход через Россию'], ['history', 'История']];
  const body = { overview: nodeOverview, more: nodeSettings, bridge: nodeBridge, history: nodeHistory }[tab](n, D.detail[n.id] || {});
  return h('aside', { class: 'drawer', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Нода ' + n.name },
    h('div', { class: 'drawer-h' }, h('div', { html: postmark(n) }),
      h('div', { style: 'min-width:0;flex:1' },
        h('h2', null, flag(n.country) ? h('span', { class: 'flag' }, flag(n.country)) : null, n.name),
        h('div', { class: 'mono', style: 'font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap' }, n.domain || n.address),
        h('div', { class: 'muted', style: 'font-size:13px;margin-top:2px' }, `${n.group}${n.profile ? ' · ' + n.profile : ''}`)),
      h('button', { type: 'button', class: 'icon-btn x', onclick: closeOverlay, 'aria-label': 'Закрыть' }, ic('x'))),
    h('div', { class: 'tabs', role: 'tablist' }, TABS.map(([v, l]) => h('button', { type: 'button', role: 'tab', 'aria-selected': String(v === tab), onclick: () => { UI.tabs.node = v; UI.relay = null; render(); } }, l))),
    h('div', { class: 'drawer-body', 'data-keep': 'node-' + n.id + tab + (UI.relay ? UI.relay.id : '') }, body));
}
const kv = (k, v, act) => [h('div', { class: 'k' }, k), h('div', { class: 'v' }, v), h('div', null, act || null)];
const copyBtn = (v, what) => h('button', { type: 'button', class: 'icon-btn', style: 'width:30px;height:30px;border:0', onclick: () => copy(v, what), 'aria-label': 'Скопировать ' + what }, ic('copy'));
function nodeOverview(n, det) {
  const d = det.d, c = conn(n), running = activeJobs().find(j => j.profile_id === n.pid && (j.domain === n.domain || j.domain === n.address));
  const r = n.rent;
  return [
    waitsMe(n) ? h('div', { class: 'notice' }, h('span', { class: 'i' }, ic('wallet')), h('div', { class: 'grow' }, h('b', null, 'Нужно ваше решение'),
        n.mon.step === 'replace' ? 'Бесплатные шаги не помогли. Бот готов купить новую ноду — выберите домен, тариф и сквады.' : 'Бесплатные шаги не помогли. Следующий шаг — платная смена IP.'),
      btn(n.mon.step === 'replace' ? 'Выбрать и купить' : 'Сменить IP', () => n.mon.step === 'replace' ? replaceNode(n, true) : confirmReip(n), 's primary')) : null,
    running ? h('div', { class: 'notice info' }, h('span', { class: 'i' }, ic('refresh')), h('div', { class: 'grow' }, h('b', null, jobTitle(running)), 'Идёт сейчас'), btn('Подробнее', () => go('task/' + running.id), 's')) : null,
    h('div', { class: 'chips', style: 'margin-bottom:16px' },
      outdated(n) ? btn(`Обновить до ${n.target}`, () => updateNodes([n]), 's primary', 'up') : null,
      btn('Проверить из России', async e => { const b = e.currentTarget; b.classList.add('busy');
        try { await api('monitor/check', 'POST'); toast('Проверка из России запущена', 'Результат появится через 1–2 минуты'); } catch (x) { toast('Не получилось', x.message, 'bad'); }
        b.classList.remove('busy'); }, 's', 'shield'),
      btn('Скопировать адрес', () => copy(n.domain || n.address, 'Адрес'), 's', 'copy')),
    nodeDiag(n, det),
    h('div', { class: 'kv', style: 'margin-bottom:16px' },
      kv('Состояние', h('span', { class: 'st ' + c.cls }, c.word)),
      kv('Адрес', h('span', { class: 'mono' }, n.address), copyBtn(n.address, 'Адрес')),
      d && d.ip ? kv('IP', h('span', { class: 'mono' }, d.ip), copyBtn(d.ip, 'IP')) : null,
      kv('Онлайн', n.online != null ? h('span', null, h('span', { class: 'num' }, n.online), ' ' + plural(n.online, 'пользователь', 'пользователя', 'пользователей')) : '—'),
      kv('Версия', h('span', null, verCell(n), d && d.xray ? h('span', { class: 'faint' }, ' · Xray ' + d.xray) : null)),
      d && d.inbound_port ? kv('Порт', h('span', { class: 'mono' }, d.inbound_port)) : null,
      d ? kv('Хосты', `${pl(d.hosts.length, 'хост', 'хоста', 'хостов')}` + (d.hosts.some(x => x.final_mask) ? ' · Final Mask' : '')) : null,
      nodeNet(n, d),
      r ? kv('Аренда', h('span', { style: 'display:grid;gap:8px' },
        h('span', { style: 'display:flex;align-items:center;gap:8px;flex-wrap:wrap' },
          h('span', { class: rentSoon(n) ? 'tag warn' : '' }, 'ещё ' + pl(Math.max(0, Math.round(r.days_left)), 'день', 'дня', 'дней')),
          h('span', { class: 'faint' }, rubK(r.price_kop) + ' за 30 дней'), btn('Продлить', () => renew(n), 's ghost')),
        h('label', { style: 'display:flex;align-items:center;gap:10px;cursor:pointer' }, toggle(r.auto_renew, (v, el) => setAutoRenew(n, v, el), 'Автопродление'), h('span', { class: 'muted' }, 'продлевать само')))) : null),
    det.loading && !d ? h('div', { class: 'sheet', style: 'padding:16px;margin-bottom:16px' }, h('div', { class: 'sk', style: 'height:120px' })) : null,
    det.err ? h('div', { class: 'notice' }, h('span', { class: 'i' }, ic('alert')), h('div', { class: 'grow' }, h('b', null, 'Подробности не загрузились'), det.err),
      btn('Повторить', () => { D.detail[n.id] = null; render(); }, 's')) : null,
    d ? loadChart(d.load) : null,
    nodeChecks(n),
    h('div', { class: 'section-h' }, h('h2', null, 'Опасные действия')),
    h('div', { class: 'list' },
      d && d.bcloud && d.bcloud.server_id ? dangerRow('globe', 'Сменить IP', d.bcloud.price_kop != null ? `Новый адрес от Bobr Cloud за ${rubK(d.bcloud.price_kop)}, DNS и панель бот обновит сам` : 'Новый адрес от Bobr Cloud, DNS и панель бот обновит сам',
        btn('Сменить IP', () => changeIp(n, d), 's')) : dangerRow('globe', 'Сменить IP', 'Нода куплена не через бота — новый адрес указываете вы, остальное бот сделает сам', btn('Указать IP', () => manualIp(n, d), 's')),
      dangerRow('trash', 'Убрать из панели', 'Нода, её хосты и DNS-запись. Сервер в Bobr Cloud не удаляется', btn('Убрать', () => removeNode(n), 's danger'))),
  ];
}
const dangerRow = (i, t, s, b) => h('div', { class: 'li' }, h('div', { class: 'ic-tile' }, ic(i)), h('div', { class: 'grow' }, h('div', { class: 't' }, t), h('div', { class: 's', style: 'white-space:normal' }, s)), b);
function loadChart(l) {
  const hist = ((l && l.hist) || []).filter(p => p.cpu != null || p.mem != null);
  if (hist.length < 3) return h('div', { class: 'chart', style: 'margin-bottom:16px' }, h('div', { class: 'chart-h' }, h('b', null, 'Нагрузка'),
    h('span', { class: 'faint', style: 'font-size:13px' }, l && l.cpu != null ? `сейчас: процессор ${l.cpu}%` + (l.mem != null ? `, память ${l.mem}%` : '') : 'данных пока нет — бот собирает их по ходу работы')));
  const w = 480, ht = 120, t0 = hist[0].t, t1 = hist[hist.length - 1].t || t0 + 1;
  const path = k => hist.filter(p => p[k] != null).map((p, i) => (i ? 'L' : 'M') + (((p.t - t0) / Math.max(1, t1 - t0)) * w).toFixed(1) + ' ' + (ht - (p[k] / 100) * ht).toFixed(1)).join('');
  return h('div', { class: 'chart', style: 'margin-bottom:16px' },
    h('div', { class: 'chart-h' }, h('b', null, 'Нагрузка'),
      h('span', { class: 'legend' }, h('span', null, h('i', { style: 'background:var(--air-blue)' }), 'процессор'), h('span', null, h('i', { style: 'background:var(--ink-3)' }), 'память')),
      h('span', { class: 'faint', style: 'margin-left:auto;font-size:12.5px' }, 'шкала 0–100 %')),
    h('div', { html: `<svg viewBox="0 0 ${w} ${ht}" preserveAspectRatio="none" aria-hidden="true"><g class="grid">${[0.5, 60, 119.5].map(y => `<line x1="0" x2="${w}" y1="${y}" y2="${y}" vector-effect="non-scaling-stroke"/>`).join('')}</g>
      <path d="${path('cpu')}" fill="none" stroke="var(--air-blue)" stroke-width="1.6" vector-effect="non-scaling-stroke"/>
      <path d="${path('mem')}" fill="none" stroke="var(--ink-3)" stroke-width="1.6" vector-effect="non-scaling-stroke"/></svg>` }),
    h('div', { class: 'axis-x' }, h('span', null, ago(t0 * 1000)), h('span', null, 'сейчас')));
}
function changeIp(n, d) {
  const price = d.bcloud.price_kop, bal = D.wallet && D.wallet.balance_kop;
  confirmAct({ title: `Сменить IP у ${n.name}?`, icon: ['globe', 'run'], ok: price != null ? `Сменить за ${rubK(price)}` : 'Сменить IP',
    text: h('div', null, 'Bobr Cloud выдаст серверу новый адрес, бот обновит DNS-запись и адрес в панели. Клиенты переподключатся за 1–2 минуты.',
      price != null ? h('div', { style: 'margin-top:10px' }, `Спишется ${rubK(price)}` + (bal != null ? `, на балансе ${rubK(bal)}.` : '.')) : null),
    fn: async () => { const r = await api(`profiles/${n.pid}/nodes/${n.uuid}/ip`, 'POST', { mode: 'bcloud' }); jobToast(`${n.name}: смена IP началась`, r.id); refreshSoon(); } });
}
function removeNode(n) {
  confirmTyped({ title: `Убрать ${n.name} из панели?`, word: n.name, ok: 'Убрать',
    text: 'Бот удалит ноду из панели вместе с её хостами и DNS-записью. Клиенты потеряют эту ноду. Сервер в Bobr Cloud останется — удалить его можно в Bobr Cloud.',
    fn: async () => { const r = await api(`profiles/${n.pid}/nodes/${n.uuid}`, 'DELETE'); toast(r.message || `${n.name} убрана`); closeOverlay(); refreshSoon(); } });
}
function nodeHistory(n) {  // события мониторинга, задачи и действия людей по этой ноде — одной лентой, по дням
  const key = n.id, H = D.hist[key];
  if (!H || (!H.loading && Date.now() - H.at > 60000)) {
    D.hist[key] = { ...(H || {}), loading: true, at: Date.now() };
    const q = encodeURIComponent(n.name);
    Promise.all([api('notify/feed?q=' + q).then(r => r.items || []).catch(() => []), isAdmin() ? api('audit?q=' + q).then(r => r.items || []).catch(() => []) : Promise.resolve([])])
      .then(([feed, aud]) => { D.hist[key] = { feed, aud, at: Date.now() }; render(true); });
  }
  const js = D.jobs.filter(j => j.profile_id === n.pid && (j.domain === n.domain || j.domain === n.address || j.loc_name === n.name));
  const own = t => new RegExp('(^|[^\\w.-])' + n.name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '([^\\w-]|$)').test(t || '');  // de5, но не de55
  const H2 = D.hist[key] || {};
  const seenEv = {}, ev = (H2.feed || []).filter(e => { if (!own(e.text)) return false; const k = e.type + '|' + e.text, prev = seenEv[k]; seenEv[k] = e.ts; return !(prev && Math.abs(prev - e.ts) < 300); }),  // одно событие дважды подряд (повтор опроса) — одной строкой
    au = (H2.aud || []).filter(a => own(a.label + ' ' + (a.detail || '')));
  const rows = [
    ...js.map(j => ({ ts: (j.status === 'done' || j.status === 'failed' ? j.updated_at : j.created_at), el: taskRow(j) })),
    ...ev.map(e => { const lines = String(e.text || '').split('\n'); const bad = /node\.(ru_down|down|offline)/.test(e.type) || e.crit, good = /ru_up|online|recover/.test(e.type);
      return { ts: e.ts, el: h('div', { class: 'li' }, h('span', { class: 'rm-dot ' + (bad ? 'bad' : good ? 'ok' : /degraded/.test(e.type) ? 'warn' : '') }),
        h('div', { class: 'grow' }, h('div', { class: 't', style: 'white-space:normal;font-weight:500' }, lines.slice(1).join(' ') || lines[0]), h('div', { class: 's' }, (e.title || '') + ' · ' + clock(e.ts * 1000)))) }; }),
    ...au.map(a => ({ ts: a.ts, el: h('div', { class: 'li' }, a.uid ? avatar({ uid: a.uid, who: a.who }, 28) : h('div', { class: 'ic-tile', style: 'width:28px;height:28px' }, ic('server')),
      h('div', { class: 'grow' }, h('div', { class: 't', style: 'white-space:normal;font-weight:500' }, a.label), h('div', { class: 's', style: 'white-space:normal' }, [a.who, a.detail, clock(a.ts * 1000)].filter(Boolean).join(' · ')))) })),
  ].sort((a, b) => b.ts - a.ts);
  if (!rows.length) return H2.loading ? sk(200) : empty('clock', 'Пока тихо', `По ${n.name} не было ни задач, ни событий мониторинга, ни изменений.`);
  const byDay = new Map();
  rows.forEach(r => { const d = new Date(r.ts * 1000).toLocaleDateString('ru-RU', { day: 'numeric', month: 'long' }); if (!byDay.has(d)) byDay.set(d, []); byDay.get(d).push(r.el); });
  const downs = ev.filter(e => /ru_down/.test(e.type)).length;
  return [downs ? h('p', { class: 'muted', style: 'margin:0 0 12px' }, `Блокировок из России в истории: ${downs}.` + (downs >= 3 ? ' Нода блокируется регулярно — возможно, стоит сменить локацию или конфигурацию.' : '')) : null,
    ...[...byDay].map(([d, els]) => [h('div', { class: 'section-h' }, h('h2', null, d)), h('div', { class: 'list', style: 'margin-bottom:16px' }, els)])];
}

/* ── Задача ── */
function drawerTask(id) {
  const st = D.job[id];
  if (!st || (!st.loading && Date.now() - st.at > 5000 && st.d && !['done', 'failed'].includes(st.d.status))) {
    D.job[id] = { ...(st || {}), loading: true, at: Date.now() };
    api('jobs/' + id).then(r => { D.job[id] = { d: r, at: Date.now() }; }).catch(e => { D.job[id] = { err: e.message, at: Date.now() }; }).then(() => render(true));
    if (st && st.d && !['done', 'failed'].includes(st.d.status)) setTimeout(() => render(true), 5000);
  }
  const j = st && st.d;
  const head = h('div', { class: 'drawer-h' }, h('div', { style: 'min-width:0;flex:1' },
    h('h2', null, j ? jobTitle(j) : 'Задача'),
    j ? h('div', { class: 'muted', style: 'font-size:13px' }, h('span', { class: 'st ' + (JOB_ST[j.status] || ['run'])[0], style: 'margin-right:8px' }, (JOB_ST[j.status] || [0, 'идёт'])[1]), `${j.profile} · начата ${ago(j.created_at * 1000)}`) : null),
    h('button', { type: 'button', class: 'icon-btn x', onclick: closeOverlay, 'aria-label': 'Закрыть' }, ic('x')));
  if (!j) return h('aside', { class: 'drawer', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Задача' }, head,
    h('div', { class: 'drawer-body' }, st && st.err ? empty('alert', 'Не загрузилось', st.err, btn('Повторить', () => { D.job[id] = null; render(); }, 'primary')) : h('div', { class: 'sk', style: 'height:200px' })));
  if (!['done', 'failed'].includes(j.status)) setTimeout(() => { if (parse().name === 'task') render(true); }, 5000);
  const cur = j.stages.findIndex(s => s.code === j.stage);
  return h('aside', { class: 'drawer', role: 'dialog', 'aria-modal': 'true', 'aria-label': jobTitle(j) }, head,
    h('div', { class: 'drawer-body', 'data-keep': 'task' + id },
      j.status === 'failed' ? h('div', { class: 'notice' }, h('span', { class: 'i' }, ic('alert')), h('div', { class: 'grow' }, h('b', null, 'Ошибка'), j.error), btn('Повторить', () => retryJob(j), 's primary', 'retry')) : null,
      h('div', { class: 'list', style: 'margin-bottom:16px' }, j.stages.map((s, i) => {
        const x = j.status === 'done' || i < cur ? 'done' : i === cur ? (j.status === 'failed' ? 'fail' : 'now') : 'wait';
        return h('div', { class: 'li' }, h('span', { class: 'step-dot ' + x }, x === 'done' ? ic('check') : x === 'fail' ? ic('x') : i + 1),
          h('div', { class: 'grow' }, h('div', { class: 't', style: x === 'wait' ? 'color:var(--ink-3);font-weight:500' : '' }, s.label),
            x === 'now' && j.last ? h('div', { class: 's mono', style: 'white-space:normal' }, String(j.last).replace(/^#{2,}\s*/, '')) : null),
          x === 'now' ? h('span', { class: 'st run' }, 'сейчас') : null);
      })),
      j.log ? [h('div', { class: 'section-h' }, h('h2', null, 'Журнал'), btn('Скопировать', () => copy(j.log, 'Журнал'), 'ghost s more')),
        h('div', { class: 'log', tabindex: '0' }, j.log.slice(-8000).replace(/^#{2,}\s*/gm, ''))] : null,
      h('div', { class: 'chips', style: 'margin-top:16px' },
        j.status === 'done' || j.status === 'failed' ? btn('Скрыть из списка', async () => { try { await api(`jobs/${j.id}/hide`, 'POST'); D.jobs = D.jobs.filter(x => x.id !== j.id); closeOverlay(); } catch (x) { toast('Не получилось', x.message, 'bad'); } }, 'ghost') : null)));
}

/* ── Интерфейс и тема ── */
function vLook() {
  return [pageH('Интерфейс и тема'),
    h('div', { class: 'sheet form', style: 'padding:16px;max-width:640px;margin-bottom:20px' },
      h('div', { class: 'fld' }, h('label', null, 'Тема'),
        seg([{ v: 'auto', l: 'Как в Telegram' }, { v: 'light', l: 'Светлая' }, { v: 'dark', l: 'Тёмная' }], themePref, v => { setTheme(v); render(); }, 'Тема'),
        h('div', { class: 'hint' }, 'Только для вас — на всех ваших устройствах.'))),
    isAdmin() && D.st.theme_global ? lookForAll() : null,
    h('div', { class: 'list', style: 'max-width:640px' },
      dangerRow('contrast', 'Классический интерфейс', 'Вернуться к прежнему виду. Переключиться обратно можно в его «Оформлении».',
        btn('Вернуть классический', async e => { const b = e.currentTarget; b.classList.add('busy');
          try { await api('theme', 'POST', { ui: 'classic' }); try { localStorage.setItem('bn:ui', 'classic'); } catch (e) {} location.replace('/admin?classic=1'); } catch (x) { b.classList.remove('busy'); toast('Не получилось', x.message, 'bad'); } }, 's')))];
}
function lookForAll() {  // администратор: оформление по умолчанию для всех пользователей бота
  const G0 = D.st.theme_global, set = G0.set || [];
  const cur = { mode: set.includes('mode') ? G0.mode : themePref, ui: set.includes('ui') ? G0.ui : 'new' };  // не задано — как сейчас у вас
  const S = UI.lookAll || (UI.lookAll = { ...cur });
  const MODE = { auto: 'как в Telegram', light: 'светлая', dark: 'тёмная' }, UIN = { new: 'новый', classic: 'классический' };
  const diff = ['mode', 'ui'].filter(k => S[k] !== cur[k]);
  const send = diff.length ? diff : ['mode', 'ui'];  // ничего не меняли — применить текущее ещё раз (сбросить личный выбор)
  const what = send.map(k => k === 'mode' ? `тема «${MODE[S.mode]}»` : `${UIN[S.ui]} интерфейс`).join(' и ');
  const apply = () => confirmAct({ title: 'Применить для всех?', icon: ['contrast', 'run'], ok: 'Применить',
    text: `${what[0].toUpperCase() + what.slice(1)} — у всех пользователей бота, включая вас. Личный выбор у них сбросится на этот; потом каждый сможет поменять у себя.`,
    fn: async () => {
      const r = await api('theme', 'POST', { scope: 'all', ...Object.fromEntries(send.map(k => [k, S[k]])) });
      D.st.theme_global = r.global; D.st.theme = { ...r, global: undefined }; UI.lookAll = null;
      syncTheme(r.mode); toast('Оформление для всех обновлено', 'Пользователи увидят его при следующем открытии');
      if (r.ui === 'classic') { try { localStorage.setItem('bn:ui', 'classic'); } catch (e) {} location.replace('/admin?classic=1'); } else render();
    } });
  return h('div', { class: 'sheet form', style: 'padding:16px;max-width:640px;margin-bottom:20px' },
    h('h3', { style: 'margin:0 0 4px' }, 'Для всех пользователей'),
    h('p', { class: 'muted', style: 'margin:0 0 12px' }, 'Что увидят все пользователи бота. Видно только администраторам.'),
    h('div', { class: 'fld' }, h('label', null, 'Тема'),
      seg([{ v: 'auto', l: 'Как в Telegram' }, { v: 'light', l: 'Светлая' }, { v: 'dark', l: 'Тёмная' }], S.mode, v => { S.mode = v; render(); }, 'Тема для всех')),
    h('div', { class: 'fld' }, h('label', null, 'Интерфейс'),
      seg([{ v: 'new', l: 'Новый' }, { v: 'classic', l: 'Классический' }], S.ui, v => { S.ui = v; render(); }, 'Интерфейс для всех'),
      set.includes('ui') && G0.ui === 'classic' ? h('div', { class: 'hint' }, 'Сейчас у всех по умолчанию классический.') : null),
    h('div', { class: 'chips', style: 'margin-top:4px' }, btn(diff.length ? 'Применить для всех' : 'Применить ещё раз для всех', apply, 's ' + (diff.length ? 'primary' : ''))));
}
function vMore() {
  const row = (i, t, s, fn) => h('button', { type: 'button', class: 'li', onclick: fn }, h('div', { class: 'ic-tile' }, ic(i)),
    h('div', { class: 'grow' }, h('div', { class: 't' }, t), h('div', { class: 's' }, s)), h('span', { class: 'chev' }, ic('chevR')));
  return [pageH('Ещё'), h('div', { class: 'list' },
    row('chart', 'Статистика', 'блокировки, хостеры, разбор нейросетью, расходы', () => go('stats')),
    row('layers', 'Группы', `${groups().length} · панели, мониторинг, автоматизация`, () => go('groups')),
    row('sliders', 'Настройки', 'подключения, уведомления, пользователи', () => go('settings')),
    row('contrast', 'Интерфейс и тема', 'светлая, тёмная, классический вид', () => go('settings/look')))];
}

/* ── Колокольчик ── */
function openBell() {
  if ($('.popover')) return;
  const b = $('#bell').getBoundingClientRect();
  const listEl = h('div', { style: 'max-height:60vh;overflow:auto' }, h('div', { class: 'empty' }, 'Загружаю…'));
  const fill = items => listEl.replaceChildren(...(items.length ? items.slice(0, 20).map(x => {
    const lines = String(x.text || '').split('\n');
    return h('div', { class: 'li' + (x.unread ? ' unread' : '') }, h('div', { class: 'attn-ic ' + (x.crit ? 'bad' : 'run') }, ic(x.crit ? 'alert' : 'bell')),
      h('div', { class: 'grow' }, h('div', { class: 't', style: 'white-space:normal;font-weight:500' }, lines[0]),
        h('div', { class: 's', style: 'white-space:normal' }, (lines.slice(1).join(' ').slice(0, 140) || x.title) + ' · ' + ago(x.ts * 1000))));
  }) : [h('div', { class: 'empty' }, 'Уведомлений нет')]));
  api('notify/feed').then(r => { D.feed = r; fill(r.items || []); }).catch(e => listEl.replaceChildren(h('div', { class: 'empty' }, e.message)));
  const pop = h('div', { class: 'popover', role: 'dialog', 'aria-label': 'Уведомления', style: innerWidth < 720 ? 'left:12px;right:12px;top:calc(var(--sa-top) + 64px);width:auto' : `top:${b.bottom + 8}px;right:${innerWidth - b.right}px` },
    h('div', { style: 'display:flex;align-items:center;padding:14px 16px;border-bottom:1px solid var(--rule)' }, h('b', { style: 'flex:1' }, 'Уведомления'),
      btn('Прочитать все', async () => { try { await api('notify/read', 'POST'); D.st.notif_unread = 0; close(); render(); } catch (e) { toast('Не получилось', e.message, 'bad'); } }, 's ghost')),
    listEl);
  const off = e => { if (!pop.contains(e.target) && !e.target.closest('#bell')) close(); };
  const close = () => { pop.remove(); document.removeEventListener('mousedown', off); const i = layers.indexOf(close); if (i >= 0) layers.splice(i, 1); syncBack(); };
  document.body.append(pop); layers.push(close); syncBack();
  setTimeout(() => document.addEventListener('mousedown', off));
}

/* ── Поиск и команды ⌘K ── */
function openPalette() {
  if ($('.palette')) return;
  const cmds = [
    ...NAV.map(x => ({ g: 'Разделы', l: x.l, i: x.i, fn: () => go(x.id) })),
    { g: 'Действия', l: 'Добавить ноды', i: 'plus', fn: () => openDeploy() },
    { g: 'Действия', l: 'Новая группа', i: 'layers', fn: () => newGroup() },
    { g: 'Действия', l: 'Обновить устаревшие ноды', i: 'up', fn: () => updateNodes(allNodes().filter(outdated)) },
    { g: 'Действия', l: 'Проверить ноды из России', i: 'shield', fn: () => api('monitor/check', 'POST').then(() => toast('Проверка запущена', 'Результат — через 1–2 минуты')).catch(e => toast('Не получилось', e.message, 'bad')) },
    { g: 'Действия', l: 'Интерфейс и тема', i: 'contrast', fn: () => go('settings/look') },
    ...groups().map(p => ({ g: 'Группы', l: p.name, s: (p.panel_url || '').replace(/^https?:\/\//, ''), i: 'layers', fn: () => go('group/' + p.id) })),
    ...allNodes().map(n => ({ g: 'Ноды', l: n.name, s: (n.domain || n.address) + ' · ' + n.group, flag: n.country, fn: () => go('node/' + n.id), q: [n.name, n.address, n.domain, n.country, n.group].join(' ') })),
  ];
  let q = '', idx = 0, shown = [];
  const list = h('div', { class: 'pal-list', role: 'listbox', id: 'pal-list' });
  const draw = () => {
    const s = q.trim().toLowerCase();
    shown = cmds.filter(c => !s ? c.g !== 'Ноды' : (c.q || c.l + ' ' + (c.s || '')).toLowerCase().includes(s));
    shown = [...shown.filter(c => c.g !== 'Ноды'), ...shown.filter(c => c.g === 'Ноды').slice(0, 8)];
    idx = Math.min(idx, Math.max(0, shown.length - 1));
    let g = '';
    list.replaceChildren(...(shown.length ? shown.flatMap((c, i) => [c.g !== g ? (g = c.g, h('div', { class: 'pal-g' }, c.g)) : null,
      h('button', { type: 'button', class: 'pal-i', role: 'option', id: 'pi' + i, 'aria-selected': String(i === idx), onmousemove: () => { if (idx !== i) { idx = i; draw(); } }, onclick: () => run(c) },
        c.flag ? h('span', { class: 'flag' }, flag(c.flag)) : ic(c.i || 'server'), c.l, c.s ? h('span', { class: 's' }, c.s) : null)]).filter(Boolean)
      : [h('div', { class: 'empty', style: 'padding:24px' }, 'Ничего не нашлось')]));
    const cur = list.querySelector('[aria-selected="true"]'); cur && cur.scrollIntoView({ block: 'nearest' });
    inp.setAttribute('aria-activedescendant', 'pi' + idx);
  };
  const run = c => { close(); c.fn(); };
  const inp = h('input', { placeholder: 'Нода или действие…', 'aria-label': 'Поиск', role: 'combobox', 'aria-controls': 'pal-list', 'aria-expanded': 'true',
    oninput: e => { q = e.target.value; idx = 0; draw(); },
    onkeydown: e => {
      if (e.key === 'ArrowDown') { idx = (idx + 1) % shown.length; draw(); e.preventDefault(); }
      else if (e.key === 'ArrowUp') { idx = (idx - 1 + shown.length) % shown.length; draw(); e.preventDefault(); }
      else if (e.key === 'Enter' && shown[idx]) run(shown[idx]);
    } });
  const pal = h('div', { class: 'palette', role: 'dialog', 'aria-label': 'Поиск и команды' }, inp, list);
  const scrim = h('div', { class: 'scrim show', style: 'z-index:59', onclick: () => close() });
  const close = () => { pal.remove(); scrim.remove(); const i = layers.indexOf(close); if (i >= 0) layers.splice(i, 1); syncBack(); };
  document.body.append(scrim, pal); layers.push(close); syncBack(); draw(); inp.focus();
}

const VIEWS = { overview: vOverview, nodes: vNodes, stats: vStats, groups: vGroups, group: vGroup, activity: vActivity, settings: vSettings, script: vScript, report: vReport,
  look: () => { setTimeout(() => go('settings/look')); return skeleton(); }, more: () => (innerWidth >= 720 ? (setTimeout(() => go('settings')), skeleton()) : vMore()) };
render();
loadAll();
