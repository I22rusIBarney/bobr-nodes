'use strict';
/* Новый интерфейс: мастер «Добавить ноды», группы, задачи, настройки. Общие помощники — в app-core.js и app.js. */

const periodPrice = (kop, months, pct) => (months <= 1 ? kop : Math.ceil(kop * months * (100 - Math.max(0, pct)) / 100 / 100) * 100);  // как в Bobr Cloud
const busyBtn = async (e, fn) => { const b = e.currentTarget; b.classList.add('busy'); b.disabled = true; try { await fn(); } finally { b.classList.remove('busy'); b.disabled = false; } };
const fld = (label, input, hint, err) => h('div', { class: 'fld' }, h('label', null, label), input, err ? h('div', { class: 'err', role: 'alert' }, err) : hint ? h('div', { class: 'hint' }, hint) : null);

/* ═════════ Мастер «Добавить ноды» ═════════ */
function openDeploy(pid) {
  if (!groups().length) return modal({ title: 'Сначала подключите панель', icon: ['layers', 'run'], body: 'Ноды живут в группах: группа — это панель Remnawave со своим доменом.',
    actions: [{ l: 'Отмена' }, { l: 'Подключить панель', cls: 'primary', fn: c => { c(); newGroup(); } }] });
  // без Bobr Cloud можно поставить ноду на свой сервер; без Cloudflare и группы — нельзя (DNS и панель)
  const iss = D.st.issues || [], noBc = iss.some(x => /Bobr Cloud/.test(x)), rest = iss.filter(x => !/Bobr Cloud/.test(x));
  if (rest.length) return modal({ title: 'Нужна настройка', icon: ['sliders', 'warn'], body: 'Чтобы ставить ноды, сначала: ' + iss.join(', ') + '.',
    actions: [{ l: 'Отмена' }, { l: 'Открыть настройки', cls: 'primary', fn: c => { c(); go('settings/connections'); } }] });
  const p = parse();
  UI.wiz = { step: 0, pid: pid || (p.name === 'group' && +p.arg) || groups()[0].id, src: noBc ? 'ssh' : 'buy', locs: [], offer: {}, count: {}, period: '1m', sel: new Set(),
    domains: {}, err: '', extras: { psi: false, warp: false, xh: false, hy: false }, port: '', ids: null,
    ssh: { ip: '', cc: '', other: '', hoster: '', user: 'root', port: '22', auth: 'password', password: '', key_text: '' } };
  wizLoad(); go('deploy');
}
function wizLoad() {
  const w = UI.wiz, pid = w.pid;
  Object.assign(w, { cat: null, catErr: '', zones: null, zone: '', own: null, ownErr: '', locs: [], offer: {}, spec: {}, cq: {}, count: {}, sel: new Set(), domains: {} });
  api('catalog?pid=' + pid).then(r => { if (w.pid === pid) { w.cat = r; w.period = (r.periods[0] || {}).code || '1m'; } }).catch(e => { w.catErr = e.message; }).then(() => render(true));
  api(`profiles/${pid}/zones`).then(r => { if (w.pid === pid) { w.zones = r; w.zone = r.default || ''; } }).catch(e => { w.zones = { zones: [], error: e.message }; }).then(() => render(true));
  // сквады панели: обязательны — без них пользователи не увидят новые ноды; по умолчанию — сквады группы
  w.meta = null; w.squads = new Set();
  api(`profiles/${pid}/panel`).then(r => { if (w.pid === pid) { w.meta = r; w.squads = new Set(r.default_squads || []); } }).catch(e => { if (w.pid === pid) w.meta = { error: e.message, squads: [] }; }).then(() => render(true));
}
function wizLoadOwn() {
  const w = UI.wiz;
  if (w.own || w.ownLoading) return;
  w.ownLoading = true;
  api(`profiles/${w.pid}/servers`).then(r => { w.own = r.servers || []; }).catch(e => { w.ownErr = e.message; w.own = []; }).then(() => { w.ownLoading = false; render(true); });
}
function wizBack() {  // кнопка «Назад» Telegram в мастере: шаг назад, с первого шага — закрыть
  const w = UI.wiz;
  if (w && !w.ids && w.step > 0) { w.step--; w.err = ''; render(); } else closeWizard();
}
function closeWizard() {
  const w = UI.wiz, leave = () => { UI.wiz = null; closeOverlay(); };
  if (w && !w.ids && (w.locs.length || w.sel.size || w.step > 0)) confirmAct({ title: 'Закрыть мастер?', text: 'Выбор не сохранится. Ничего не куплено.', ok: 'Закрыть', cls: 'danger solid', fn: async () => leave() });
  else leave();
}
const L = code => ((UI.wiz.cat && UI.wiz.cat.locations) || []).find(l => l.code === code) || { code, name: code, flag: '', offers: [] };
const sshCC = w => (w.ssh.cc === 'other' ? w.ssh.other : w.ssh.cc).trim().toLowerCase();
const sshReady = w => /^\d{1,3}(\.\d{1,3}){3}$/.test(w.ssh.ip.trim()) && /^[a-z]{2}$/.test(sshCC(w)) && !!(w.ssh.auth === 'key' ? w.ssh.key_text.trim() : w.ssh.password);
function wizItems(w) {  // [{loc, n}] — сколько нод в какой локации
  if (w.src === 'ssh') return sshCC(w) ? [{ loc: sshCC(w), n: 1 }] : [];
  if (w.src === 'own') { const by = {}; (w.own || []).filter(s => w.sel.has(s.id)).forEach(s => { by[s.location] = (by[s.location] || 0) + 1; }); return Object.entries(by).map(([loc, n]) => ({ loc, n })); }
  return w.locs.map(loc => ({ loc, n: w.count[loc] || 1 }));
}
// тариф страны: готовый или своя конфигурация (цена — из Bobr Cloud, когда посчитана)
const wizOffer = (w, loc) => w.offer[loc] === 'custom' ? ((w.cq || {})[loc] || {}).o || null : L(loc).offers.find(x => x.idx === w.offer[loc]);
function wizQuote(w, loc) {  // пересчитать цену своей конфигурации, если она поменялась
  const sp = w.spec[loc], k = [w.pid, loc, sp.cpu, sp.ram, sp.disk].join('/'), cq = w.cq || (w.cq = {});
  if (cq[loc] && cq[loc].k === k) return;
  cq[loc] = { k, loading: true };
  clearTimeout((w.cqt || (w.cqt = {}))[loc]);
  w.cqt[loc] = setTimeout(() => api(`quote?pid=${w.pid}&location=${loc}&cpu=${sp.cpu}&ram=${sp.ram}&disk=${sp.disk}`)
    .then(o => { if (cq[loc].k === k) cq[loc] = { k, o }; }).catch(e => { if (cq[loc].k === k) cq[loc] = { k, err: e.message }; }).then(() => render(true)), 400);
}
function wizCost(w) {
  if (w.src !== 'buy' || !w.cat) return 0;
  const per = w.cat.periods.find(p => p.code === w.period) || { months: 1, pct: 0 };
  return w.locs.reduce((s, loc) => { const o = wizOffer(w, loc); return s + (o ? periodPrice(o.price_kop, per.months, per.pct) * (w.count[loc] || 1) : 0); }, 0);
}
async function wizNames(w) {  // автоимена для каждой локации, кроме уже введённых
  const items = wizItems(w), taken = [];
  for (const { loc, n } of items) {
    const have = (w.domains[loc] || []).filter(Boolean);
    if (have.length === n) { taken.push(...have); continue; }
    const r = await api('domains/auto', 'POST', { pid: w.pid, location: loc, count: n, base: w.zone || undefined, exclude: taken });
    w.domains[loc] = r.domains; taken.push(...r.domains);
  }
  for (const k of Object.keys(w.domains)) if (!items.some(i => i.loc === k)) delete w.domains[k];
}
function wizard() {
  const w = UI.wiz, own = w.src !== 'buy';  // уже купленный или свой сервер: без тарифа и оплаты
  const STEPS = own ? ['Куда', 'Имена', 'Проверка'] : ['Куда', 'Тариф', 'Имена', 'Проверка'];
  const kind = own ? ['where', 'names', 'review'][w.step] : ['where', 'tariff', 'names', 'review'][w.step];
  const items = wizItems(w), total = items.reduce((s, x) => s + x.n, 0), cost = wizCost(w);
  const bal = w.cat && w.cat.balance_kop;
  const ok = {
    where: w.src === 'ssh' ? sshReady(w) : own ? w.sel.size > 0 : w.locs.length > 0,
    tariff: w.locs.every(l => w.offer[l] != null && (w.offer[l] !== 'custom' || !!wizOffer(w, l))),
    names: items.every(({ loc, n }) => (w.domains[loc] || []).filter(Boolean).length === n) && !w.namesBusy && w.squads && w.squads.size > 0 && !wizHostsBad(w, items),
    review: own || bal == null || bal >= cost,
  }[kind];
  const why = { where: w.src === 'ssh' ? 'Укажите IP, страну и пароль или ключ' : own ? 'Выберите серверы' : 'Выберите хотя бы одну страну', tariff: 'Выберите тариф для каждой страны', names: wizHostsBad(w, items) || (w.squads && w.squads.size ? 'Нужно имя для каждой ноды' : 'Выберите хотя бы один сквад'), review: 'Не хватает денег на балансе' }[kind];
  const next = async e => {
    w.err = '';
    if (kind === 'tariff' || (kind === 'where' && own)) {  // дальше — имена: подбираем автоматически
      await busyBtn(e, async () => { try { await wizNames(w); w.step++; } catch (x) { w.err = x.message; } });
    } else if (kind === 'names') {
      await busyBtn(e, async () => {
        try {
          const all = items.flatMap(({ loc }) => w.domains[loc]);
          const r = await api('domains/check', 'POST', { pid: w.pid, domains: all, count: all.length });
          let k = 0; items.forEach(({ loc, n }) => { w.domains[loc] = r.domains.slice(k, k + n); k += n; });
          w.dnsWarn = r.warnings || [];
          w.step++;
        } catch (x) { w.err = x.message; }
      });
    } else w.step++;
    render();
  };
  const body = w.ids ? wizProgress(w) : w.src !== 'ssh' && !w.cat && !w.catErr ? h('div', { class: 'sk', style: 'height:240px' })
    : w.src !== 'ssh' && w.catErr ? empty('alert', 'Bobr Cloud не ответил', w.catErr, h('div', { class: 'chips' }, btn('Повторить', () => { wizLoad(); render(); }, 'primary', 'retry'),
      btn('Поставить на свой сервер', () => { w.src = 'ssh'; render(); }, '', 'server')))
    : { where: wizWhere, tariff: wizTariff, names: wizNamesStep, review: wizReview }[kind](w, items, cost);
  return h('div', { class: 'wizard', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Добавить ноды' },
    h('div', { class: 'wiz-top' },
      h('h2', null, w.ids ? 'Установка' : 'Добавить ноды'),
      w.ids ? null : h('div', { class: 'steps', 'aria-label': `Шаг ${w.step + 1} из ${STEPS.length}` }, STEPS.map((s, i) => [i ? h('span', { class: 'sep' }) : null,
        h('span', { class: 'stp' + (i === w.step ? ' on' : i < w.step ? ' done' : '') }, h('i', null, i < w.step ? '✓' : i + 1), h('span', null, s))])),
      h('span', { style: 'flex:1' }),
      h('button', { type: 'button', class: 'icon-btn x-close', onclick: closeWizard, 'aria-label': 'Закрыть' }, ic('x'))),
    h('div', { class: 'wiz-body', 'data-keep': 'wiz' + w.step }, h('div', { class: 'wiz-in' }, body,
      w.err ? h('div', { class: 'notice', role: 'alert', style: 'margin-top:16px' }, h('span', { class: 'i' }, ic('alert')), h('div', { class: 'grow' }, h('b', null, 'Не получилось'), w.err)) : null)),
    w.ids || (!w.cat && w.src !== 'ssh') ? null : h('div', { class: 'wiz-foot' },
      h('div', { class: 'sum' }, cost ? ['Итого ', h('b', null, rubK(cost)), ' · ', pl(total, 'нода', 'ноды', 'нод')] : total ? pl(total, 'нода', 'ноды', 'нод') : h('span', { class: 'faint' }, why)),
      h('span', { class: 'sp' }),
      w.step ? btn('Назад', () => { w.step--; w.err = ''; render(); }) : null,
      kind !== 'review' ? btn('Далее', next, 'primary', null, { disabled: !ok, title: ok ? null : why })
        : btn(own ? 'Установить' : `Купить и установить · ${rubK(cost)}`, e => busyBtn(e, () => wizSubmit(w, items)), 'primary', null, { disabled: !ok })));
}
function wizWhere(w) {
  const own = w.src === 'own', p = G(w.pid), cat = w.cat || { locations: [], buyer: '' };
  if (own) wizLoadOwn();
  const minPrice = l => Math.min(...l.offers.map(o => o.price_kop));
  return [h('h3', null, 'Куда ставим'), h('p', null, w.src === 'ssh' ? 'Сервер у любого хостера: бот зайдёт по SSH, поставит ноду, пропишет DNS и добавит в панель.'
      : own ? 'Отметьте серверы — бот поставит на них ноды.' : 'Выберите группу и страны. Тариф для каждой страны — на следующем шаге.'),
    groups().length > 1 ? h('div', { class: 'block' }, h('h4', null, 'Группа'), h('div', { class: 'chips' }, groups().map(g => h('button', { type: 'button', class: 'chip', 'aria-pressed': String(w.pid === g.id),
      onclick: () => { if (w.pid !== g.id) { w.pid = g.id; wizLoad(); render(); } } }, g.name)))) : null,
    h('div', { class: 'block' }, h('h4', null, 'Серверы'), h('div', { class: 'choice' },
      w.cat ? opt(w.src === 'buy', 'Купить в Bobr Cloud', `Бот купит серверы (${cat.buyer}), поставит ноды, пропишет DNS и добавит в панель`, () => { w.src = 'buy'; render(); }) : null,
      w.cat ? opt(own, 'Уже купленные', 'Свободные серверы в Bobr Cloud — без новой покупки', () => { w.src = 'own'; render(); }) : null,
      opt(w.src === 'ssh', 'Свой сервер', 'Купили у другого хостера — укажите IP и доступ по SSH', () => { w.src = 'ssh'; render(); }))),
    w.src === 'ssh' ? wizSsh(w, cat) : null,
    own ? h('div', { class: 'block' }, h('h4', null, 'Свободные серверы'),
      w.own == null ? h('div', { class: 'sk', style: 'height:120px' }) : w.ownErr ? h('div', { class: 'err' }, w.ownErr)
        : !w.own.length ? empty('server', 'Серверов нет', 'В Bobr Cloud у покупателя этой группы нет купленных серверов.')
        : h('div', { class: 'list' }, w.own.map(s => h('label', { class: 'switch', style: s.ready ? '' : 'opacity:.55;cursor:default' },
          h('input', { type: 'checkbox', class: 'cb', disabled: !s.ready, checked: w.sel.has(s.id), onchange: e => { e.target.checked ? w.sel.add(s.id) : w.sel.delete(s.id); render(); } }),
          h('span', { class: 'grow' }, h('b', null, `${s.flag || ''} ${s.name || s.ip}`), h('span', null, `${s.loc_name} · ${s.cpu} vCPU, ${s.ram} ГБ, ${s.disk} ГБ · ${s.ip}` + (s.ready ? (s.stale ? ` · система переустановлена — прежняя нода ${s.stale.domain || s.stale.ip} уйдёт из панели` : '') : ` · ${s.why}`))))))) : null,
    w.src === 'buy' ? h('div', { class: 'block' }, h('h4', null, 'Страны'), !w.cat.locations.length ? h('div', { class: 'muted' }, 'В Bobr Cloud сейчас нет доступных локаций.')
      : h('div', { class: 'chips' }, w.cat.locations.map(l => h('button', { type: 'button', class: 'chip', 'aria-pressed': String(w.locs.includes(l.code)),
        onclick: () => { w.locs = w.locs.includes(l.code) ? w.locs.filter(x => x !== l.code) : [...w.locs, l.code]; render(); } },
        l.flag ? h('span', { class: 'flag' }, l.flag) : null, l.name, l.offers.length ? h('span', { class: 's' }, 'от ' + rubK(minPrice(l))) : null)))) : null,
    p.script ? h('div', { class: 'muted' }, `Скрипт установки группы: ${p.script}.`) : null];
}
/* свой сервер: IP, страна (как в Bobr Cloud или любой код), хостер и доступ по SSH */
function wizSsh(w, cat) {
  const x = w.ssh, set = (k, rerender) => e => { x[k] = e.target.value; if (rerender) render(); };
  const inp = (k, ph, extra) => h('input', { class: 'inp' + (extra && extra.mono ? ' mono' : ''), 'data-k': 'ssh-' + k, value: x[k], placeholder: ph, oninput: set(k, true), ...(extra || {}), mono: null });
  return h('div', { class: 'block' }, h('h4', null, 'Сервер'), h('div', { class: 'sheet form', style: 'padding:16px' },
    fld('IP сервера', inp('ip', '203.0.113.10', { mono: true, inputmode: 'decimal' }), 'Публичный IPv4. Систему лучше свежую: Ubuntu или Debian.'),
    fld('Страна', h('select', { class: 'sel', style: 'width:100%', 'aria-label': 'Страна', onchange: set('cc', true) },
      h('option', { value: '', selected: !x.cc || null }, 'Выберите страну'),
      cat.locations.map(l => h('option', { value: l.code, selected: x.cc === l.code || null }, `${l.flag || ''} ${l.name}`)),
      h('option', { value: 'other', selected: x.cc === 'other' || null }, 'Другая — ввести код')),
      x.cc === 'other' ? inp('other', 'Код страны, например DE', { maxlength: 2, style: 'margin-top:8px;max-width:160px;text-transform:uppercase' }) : 'Для флага, имени хоста и статистики по странам.'),
    fld('Хостер', inp('hoster', 'например, Aeza'), 'Необязательно: подпись для статистики блокировок по хостерам.'),
    h('div', { style: 'display:grid;grid-template-columns:1fr 110px;gap:12px' }, fld('Пользователь', inp('user', 'root')), fld('SSH-порт', inp('port', '22', { mono: true, inputmode: 'numeric' }))),
    fld('Вход', seg([{ v: 'password', l: 'Пароль' }, { v: 'key', l: 'Ключ' }], x.auth, v => { x.auth = v; render(); }, 'Вход')),
    x.auth === 'key' ? fld('Закрытый ключ', h('textarea', { class: 'inp mono', rows: 4, 'data-k': 'ssh-key', placeholder: '-----BEGIN OPENSSH PRIVATE KEY-----', oninput: set('key_text', true) }, x.key_text))
      : fld('Пароль', h('input', { class: 'inp', type: 'password', 'data-k': 'ssh-pw', autocomplete: 'off', value: x.password, oninput: set('password', true) }),
        x.user && x.user !== 'root' ? 'Пользователь не root — бот будет работать через sudo.' : null)));
}
function opt(on, t, s, onclick, extra) {
  return h('button', { type: 'button', class: 'opt', 'aria-pressed': String(on), onclick }, h('span', { class: 'ck' }, ic('check')), h('b', null, t), h('span', { class: 's' }, s), extra || null);
}
function wizCustomOpt(w, code, conf) {
  const q = (w.cq || {})[code], on = w.offer[code] === 'custom';
  return opt(on, 'Своя конфигурация', 'vCPU, память и диск — сколько нужно', () => {
    w.offer[code] = 'custom'; w.spec[code] = w.spec[code] || { cpu: conf.cpu.min, ram: conf.ram.min, disk: conf.disk.min }; render(); },
  on && q && q.o ? h('span', { class: 'price' }, rubK(q.o.price_kop), h('span', { class: 'faint', style: 'font:400 13px var(--font)' }, ' / мес')) : null);
}
function wizCustomForm(w, code, conf) {
  const sp = w.spec[code];
  wizQuote(w, code);
  const q = w.cq[code];
  return h('div', { class: 'form', style: 'margin-top:12px' },
    [['cpu', 'vCPU'], ['ram', 'Память, ГБ'], ['disk', 'Диск, ГБ']].map(([k, t]) => fld(t,
      h('input', { class: 'inp mono', type: 'number', inputmode: 'numeric', min: conf[k].min, max: conf[k].max, step: conf[k].step || 1, value: String(sp[k]), style: 'max-width:140px',
        onchange: e => { const v = Math.round(+e.target.value); sp[k] = Math.min(conf[k].max, Math.max(conf[k].min, isFinite(v) ? v : conf[k].min)); render(); } }),
      `от ${conf[k].min} до ${conf[k].max}` + ((conf[k].step || 1) > 1 ? `, шаг ${conf[k].step}` : ''))),
    h('div', { class: 'muted', style: 'font-size:13px' }, q.loading ? 'Считаю цену в Bobr Cloud…' : q.err ? 'Цена: ' + q.err
      : q.o ? `${rubK(q.o.price_kop)} в месяц за сервер` + (q.o.speed ? ' · ' + q.o.speed : '') : ''));
}
function wizTariff(w) {
  const per = w.cat.periods, conf = w.cat.configurator;
  w.spec = w.spec || {};
  return [h('h3', null, 'Тариф и количество'), h('p', null, 'Тариф выбирается явно для каждой страны. ОС — ' + w.cat.os_label + '.'),
    w.locs.map(code => { const l = L(code);
      return h('div', { class: 'loc-card' },
        h('div', { class: 'h' }, l.flag ? h('span', { class: 'flag' }, l.flag) : null, l.name,
          h('span', { style: 'margin-left:auto;display:flex;align-items:center;gap:10px;font-weight:500' }, h('span', { class: 'muted hide-m' }, 'Количество'),
            h('span', { class: 'stepper' }, h('button', { type: 'button', 'aria-label': 'Меньше', onclick: () => { w.count[code] = Math.max(1, (w.count[code] || 1) - 1); w.domains = {}; render(); } }, '−'),
              h('span', { class: 'num', 'aria-live': 'polite' }, w.count[code] || 1),
              h('button', { type: 'button', 'aria-label': 'Больше', onclick: () => { w.count[code] = Math.min(20, (w.count[code] || 1) + 1); w.domains = {}; render(); } }, '+')))),
        h('div', { class: 'choice' }, l.offers.map(o => opt(w.offer[code] === o.idx, `${o.cpu} vCPU · ${o.ram} ГБ`, `диск ${o.disk} ГБ` + (o.speed ? ` · ${o.speed}` : ''),
          () => { w.offer[code] = o.idx; render(); }, h('span', { class: 'price' }, rubK(o.price_kop), h('span', { class: 'faint', style: 'font:400 13px var(--font)' }, ' / мес'))))
          .concat(conf ? [wizCustomOpt(w, code, conf)] : [])),
        conf && w.offer[code] === 'custom' ? wizCustomForm(w, code, conf) : null,
        locAdvice(code) ? h('div', { style: 'margin-top:12px' }, adviceNote(locAdvice(code))) : null);
    }),
    per.length > 1 ? h('div', { class: 'block', style: 'margin-top:20px' }, h('h4', null, 'Срок аренды'),
      seg(per.map(p => ({ v: p.code, l: `${p.months} мес${p.pct ? ' · −' + p.pct + '%' : ''}` })), w.period, v => { w.period = v; render(); }, 'Срок')) : null];
}
function wizNamesStep(w, items) {
  const p = G(w.pid), z = w.zones || { zones: [] };
  const regen = async e => busyBtn(e, async () => { w.domains = {}; w.err = ''; try { await wizNames(w); } catch (x) { w.err = x.message; } render(); });
  return [h('h3', null, 'Имена'), h('p', null, 'Каждая нода получит свой адрес. Бот подобрал свободные по шаблону группы — можно поправить.'),
    z.zones.length > 1 ? h('div', { class: 'block' }, fld('Домен', h('select', { class: 'sel', value: w.zone, style: 'max-width:320px', onchange: e => { w.zone = e.target.value; w.domains = {}; regen({ currentTarget: e.target }); } },
      z.zones.map(x => h('option', { value: x }, x))), z.error || null)) : z.error ? h('div', { class: 'hint', style: 'margin-bottom:12px' }, z.error) : null,
    h('div', { class: 'sheet', style: 'padding:6px 0;margin-bottom:12px' }, items.flatMap(({ loc, n }) => Array.from({ length: n }, (_, i) =>
      h('div', { class: 'name-row' }, L(loc).flag ? h('span', { class: 'flag' }, L(loc).flag) : null,
        h('input', { class: 'name-in', style: 'flex:1;min-width:0;max-width:340px', 'data-k': `dn-${loc}-${i}`, spellcheck: 'false', 'aria-label': `Адрес ноды ${i + 1}, ${L(loc).name}`,
          value: (w.domains[loc] || [])[i] || '', oninput: e => { (w.domains[loc] = w.domains[loc] || [])[i] = e.target.value.trim().toLowerCase(); } }))))),
    h('div', { class: 'chips', style: 'margin-bottom:22px' }, btn('Подобрать заново', regen, 's', 'refresh')),
    h('div', { class: 'block' }, h('h4', null, 'Сквады'), !w.meta ? sk(60) : w.meta.error ? h('div', { class: 'notice' }, h('span', { class: 'i' }, ic('alert')), h('div', { class: 'grow' }, h('b', null, 'Панель не ответила'), w.meta.error), btn('Повторить', () => { wizLoad(); render(); }, 's'))
      : !w.meta.squads.length ? h('div', { class: 'err' }, 'В панели нет сквадов — создайте сквад в Remnawave, иначе пользователи не увидят новые ноды.')
      : [h('div', { class: 'chips' }, w.meta.squads.map(s => h('button', { type: 'button', class: 'chip', 'aria-pressed': String(w.squads.has(s.uuid)), onclick: () => { w.squads.has(s.uuid) ? w.squads.delete(s.uuid) : w.squads.add(s.uuid); render(); } }, s.name))),
        h('div', { class: 'hint', style: 'margin-top:8px' }, w.squads.size ? 'Пользователи этих сквадов увидят новые ноды.' : 'Выберите хотя бы один сквад — без него ноды не будут готовы.')]),
    wizHosts(w, items),
    h('details', { class: 'more-opts', open: UI.advOpen || null, ontoggle: e => { UI.advOpen = e.target.open; } },
      h('summary', null, 'Дополнительно', h('span', { class: 'faint', style: 'font-weight:400' }, ' — дополнения и порт, у большинства не меняются')),
      h('div', { class: 'list', style: 'margin:12px 0' },
        switchRow('Psiphon', 'Запасной выход, когда сайты блокируют IP хостера', w.extras.psi, v => { w.extras.psi = v; }),
        switchRow('WARP', 'Выход через Cloudflare для сервисов, которые не любят IP дата-центров', w.extras.warp, v => { w.extras.warp = v; }),
        p.script ? null : switchRow('Запасной XHTTP', 'Второй способ подключения напрямую, если основной режут', w.extras.xh, v => { w.extras.xh = v; }),
        p.script ? null : switchRow('Hysteria2', 'Второй протокол на UDP 443. Делит IP с VLESS: если его распознают и забанят IP, отвалятся оба', w.extras.hy, v => { w.extras.hy = v; }),
        switchRow('Уникальные настройки', 'У каждой ноды свой порт, TLS-отпечаток, маска и параметры XHTTP — без общего шаблона', w.stealth, v => { w.stealth = v; render(); })),
      w.stealth ? null : fld('Порт для клиентов', h('input', { class: 'inp mono', 'data-k': 'wport', style: 'max-width:160px', inputmode: 'numeric', placeholder: '443', value: w.port, oninput: e => { w.port = e.target.value.trim(); } }),
        'Пусто — 443, как у обычного HTTPS.'))];
}
/* хосты в панели: обычные, общий балансировщик по тегу (NORMAL) или балансировщик страны (скрытые хосты с тегом
   страны и, по желанию, NORMAL; шаблон Balancer_<тег> и видимый хост бот создаст, если их ещё нет). Выбор — на группу. */
const HKEY = pid => 'bn:hosts:' + pid;
const TAG_RE = /^[A-Z0-9_:]{1,36}$/;
const tagList = v => String(v || '').toUpperCase().split(/[\s,]+/).filter(Boolean);
function wizHostsState(w) {
  if (w.hosts && w.hosts.pid === w.pid) return w.hosts;
  let saved = {}; try { saved = JSON.parse(localStorage.getItem(HKEY(w.pid)) || '{}'); } catch (e) { /* без хранилища */ }
  // extra — теги вдобавок к тегу страны («Ещё теги»); раньше был переключатель NORMAL
  const extra = saved.extra != null ? saved.extra : saved.normal === false ? '' : 'NORMAL';
  return (w.hosts = { pid: w.pid, mode: saved.mode || 'simple', tags: saved.tags || 'NORMAL', extra, ctags: saved.ctags || {} });
}
function wizHostsSave(H) { try { localStorage.setItem(HKEY(H.pid), JSON.stringify({ mode: H.mode, tags: H.tags, extra: H.extra, ctags: H.ctags })); } catch (e) { /* без хранилища */ } }
const ctagOf = (H, loc) => (H.ctags[loc] || loc).toUpperCase();
function wizHostsBad(w, items) {  // неверные теги — текст ошибки или ''
  const H = wizHostsState(w);
  const all = H.mode === 'normal' ? tagList(H.tags) : H.mode === 'balancer' ? [...items.map(i => ctagOf(H, i.loc)), ...tagList(H.extra)] : [];
  const bad = all.filter(t => !TAG_RE.test(t));
  return bad.length ? 'Теги: латиница, цифры, подчёркивание — ' + bad.join(', ') : H.mode === 'normal' && !all.length ? 'Укажите хотя бы один тег' : '';
}
function wizHosts(w, items) {
  const H = wizHostsState(w), set = p => { Object.assign(H, p); wizHostsSave(H); render(); };
  const bad = wizHostsBad(w, items);
  const tagIn = (val, ph, on, k, wide) => h('input', { class: 'inp mono', 'data-k': k, value: val, placeholder: ph, spellcheck: 'false', style: `max-width:${wide ? 320 : 160}px;text-transform:uppercase`,
    oninput: e => { on(e.target.value.toUpperCase()); wizHostsSave(H); } });
  return h('div', { class: 'block' }, h('h4', null, 'Хосты в панели'),
    h('div', { class: 'seg-row', style: 'margin-bottom:10px' }, seg([{ v: 'simple', l: 'Обычные' }, { v: 'normal', l: 'Общий тег' }, { v: 'balancer', l: 'По стране' }], H.mode, v => set({ mode: v }), 'Хосты')),
    H.mode === 'normal' ? fld('Теги хостов', tagIn(H.tags, 'NORMAL', v => { H.tags = v; }, 'wtags', true),
      'Через запятую. Видимые хосты с этими тегами подхватят ваши балансировщики (например, авто-выбор по NORMAL).')
    : H.mode === 'balancer' ? [
      fld(items.length > 1 ? 'Тег балансировщика каждой страны' : 'Тег балансировщика страны', h('div', { style: 'display:grid;gap:8px' }, items.map(({ loc }) =>
        h('div', { style: 'display:flex;align-items:center;gap:10px' }, L(loc).flag ? h('span', { class: 'flag' }, L(loc).flag) : null, h('span', { style: 'min-width:90px' }, L(loc).name || loc.toUpperCase()),
          tagIn(H.ctags[loc] || loc.toUpperCase(), loc.toUpperCase(), v => { H.ctags[loc] = v.trim(); }, 'wct-' + loc)))),
        'Обычно — код страны. Если балансировщика с этим тегом в панели ещё нет, бот создаст шаблон и видимый хост страны.'),
      fld('Ещё теги', tagIn(H.extra, 'NORMAL', v => { H.extra = v; }, 'wextra', true), 'Через запятую, можно пусто. Например NORMAL — чтобы нода попала и в общий авто-выбор.'),
      h('div', { class: 'hint' }, 'Хосты нод — скрытые, клиенты видят хост-балансировщик страны.')]
    : h('div', { class: 'hint' }, 'Видимые хосты без тегов — по одному на ноду.'),
    bad ? h('div', { class: 'err', role: 'alert', style: 'margin-top:8px' }, bad) : null);
}
function wizHostsPayload(w, items) {
  const H = wizHostsState(w);
  if (H.mode === 'normal') return { host_mode: 'normal', tags: tagList(H.tags).join(',') };
  if (H.mode === 'balancer') return { host_mode: 'balancer', tags: tagList(H.extra).join(','),
    balancer_tags: Object.fromEntries(items.map(i => [i.loc, ctagOf(H, i.loc)])), balancer_tag: items.length === 1 ? ctagOf(H, items[0].loc) : '' };
  return { host_mode: 'simple' };
}
function switchRow(title, text, checked, onchange) {
  return h('label', { class: 'switch' }, h('span', { class: 'grow' }, h('b', null, title), text ? h('span', null, text) : null), toggle(checked, onchange, title));
}
function wizReview(w, items, cost) {
  const own = w.src !== 'buy', cat = w.cat || { periods: [] }, per = (cat.periods.find(p => p.code === w.period) || { months: 1, pct: 0 }), bal = cat.balance_kop;
  const row = (l, v, cls) => h('div', { class: 'li' }, h('div', { class: 'grow', style: 'min-width:0' }, l), h('span', { class: 'num ' + (cls || '') }, v));
  const ex = [w.extras.psi && 'Psiphon', w.extras.warp && 'WARP', w.extras.xh && 'XHTTP', w.extras.hy && 'Hysteria2'].filter(Boolean);
  return [h('h3', null, 'Проверьте и запустите'), h('p', null, w.src === 'ssh' ? `Покупки нет: бот зайдёт на ${w.ssh.ip.trim()} по SSH и поставит ноду поверх системы сервера.`
      : own ? 'Покупки нет: нода ставится поверх системы сервера.' : `Деньги спишутся с баланса: ${w.cat.buyer}. Если что-то пойдёт не так, повтор не купит сервер второй раз.`),
    h('div', { class: 'list', style: 'margin-bottom:16px' },
      row(h('span', null, 'Группа ', h('b', null, G(w.pid).name)), ''),
      items.map(({ loc, n }) => { const o = !own && wizOffer(w, loc);
        return row(h('span', null, L(loc).flag ? h('span', { class: 'flag' }, L(loc).flag) : null, ` ${L(loc).name} × ${n}` + (o ? ` · ${o.cpu} vCPU, ${o.ram} ГБ · ${per.months} мес` : '')),
          o ? rubK(periodPrice(o.price_kop, per.months, per.pct) * n) : 'свой сервер', o ? '' : 'faint'); }),
      own ? null : row(h('b', null, 'Итого'), h('b', null, rubK(cost)))),
    h('div', { class: 'list', style: 'margin-bottom:16px' },
      row('Адреса', items.flatMap(({ loc }) => w.domains[loc]).join(', '), 'mono'),
      row('Сквады', (w.meta && w.meta.squads || []).filter(s => w.squads.has(s.uuid)).map(s => s.name).join(', ') || '—'),
      row('Хосты', (H => H.mode === 'normal' ? 'теги ' + tagList(H.tags).join(', ') : H.mode === 'balancer'
        ? 'скрытые, теги ' + [...new Set([...items.map(i => ctagOf(H, i.loc)), ...tagList(H.extra)])].join(', ') : 'обычные')(wizHostsState(w))),
      row('Дополнения', ex.join(', ') || 'нет'), row('Порт', w.stealth ? 'свой у каждой ноды' : w.port || '443')),
    (w.dnsWarn || []).length ? h('div', { class: 'notice', style: 'background:var(--warn-soft);margin-bottom:16px' }, h('span', { class: 'i', style: 'color:var(--warn)' }, ic('alert')),
      h('div', { class: 'grow' }, h('b', null, 'Проверьте имена'), w.dnsWarn.map(x => h('div', null, x)))) : null,
    !own && bal != null ? (bal < cost ? h('div', { class: 'notice' }, h('span', { class: 'i' }, ic('wallet')), h('div', { class: 'grow' }, h('b', null, `Не хватает ${rubK(cost - bal)}`),
      `На балансе ${rubK(bal)}.`), btn('Пополнить', topUp, 's primary', 'wallet'))
      : h('div', { class: 'muted' }, `На балансе ${rubK(bal)}, после покупки останется ${rubK(bal - cost)}.`)) : null];
}
async function wizSubmit(w, items) {
  if (w.src === 'ssh') return wizSubmitSsh(w, items);
  const own = w.src === 'own';
  const payload = { pid: w.pid, period: w.period, port: w.port || null, squads: [...w.squads],
    items: items.map(({ loc }) => ({ location: loc, domains: w.domains[loc],
      ...(own ? { servers: (w.own || []).filter(s => w.sel.has(s.id) && s.location === loc).map(s => s.id) } : w.offer[loc] === 'custom' ? { idx: null, spec: { ...w.spec[loc] } } : { idx: w.offer[loc] }) })),
    psiphon: w.extras.psi ? { enabled: true, region: '', domains: '' } : undefined, warp: w.extras.warp ? { enabled: true, domains: '' } : undefined,
    xhttp: w.extras.xh ? { enabled: true, mode: 'direct' } : undefined, hy2: w.extras.hy ? { enabled: true } : undefined, stealth: !!w.stealth,
    ...wizHostsPayload(w, items) };
  try { const r = await api('deploy', 'POST', payload); w.ids = r.ids; w.err = ''; toast(`Запущено: ${pl(r.ids.length, 'установка', 'установки', 'установок')}`); refreshSoon(); }
  catch (e) { w.err = e.message; }
  render();
}
async function wizSubmitSsh(w, items) {
  const x = w.ssh, loc = items[0].loc;
  const payload = { pid: w.pid, ip: x.ip.trim(), country: loc, hoster: x.hoster.trim(), user: x.user.trim() || 'root', port: x.port || 22,
    ...(x.auth === 'key' ? { key: 'own', key_text: x.key_text } : { key: '', password: x.password }),
    domain: (w.domains[loc] || [])[0], squads: [...w.squads], stealth: !!w.stealth, ...(w.stealth ? {} : { port_client: w.port || null }),
    psiphon: w.extras.psi ? { enabled: true, region: '', domains: '' } : undefined, warp: w.extras.warp ? { enabled: true, domains: '' } : undefined,
    xhttp: w.extras.xh ? { enabled: true, mode: 'direct' } : undefined, hy2: w.extras.hy ? { enabled: true } : undefined, ...wizHostsPayload(w, items) };
  try { const r = await api('deploy/own', 'POST', payload); w.ids = r.ids; w.err = ''; x.password = x.key_text = ''; toast('Установка запущена', r.domain); refreshSoon(); }
  catch (e) { w.err = e.message; }
  render();
}
function wizProgress(w) {
  const js = w.ids.map(id => D.jobs.find(j => j.id === id) || { id, status: 'queued', domain: '…', stage: '' });
  setTimeout(() => { if (UI.wiz === w && parse().name === 'deploy') api('jobs').then(r => { D.jobs = r.jobs || D.jobs; render(true); }).catch(() => {}); }, 4000);
  const done = js.filter(j => j.status === 'done').length, fail = js.filter(j => j.status === 'failed').length, finished = done + fail === js.length;
  return [h('h3', null, finished ? (fail ? `Готово ${done} из ${js.length}` : `Все ${pl(js.length, 'нода', 'ноды', 'нод')} установлены`) : `Ставим ${pl(js.length, 'ноду', 'ноды', 'нод')}`),
    h('p', null, finished ? (fail ? 'Часть установок остановилась — откройте задачу, там причина и «Повторить».' : 'Ноды добавлены в панель. Проверка из России — через пару минут.')
      : 'Можно закрыть окно — установка продолжится, ход виден в «Задачах», итог придёт в Telegram.'),
    h('div', { class: 'list', style: 'margin-bottom:20px' }, js.map(taskRow)),
    h('div', { class: 'chips' }, btn(finished ? 'К нодам' : 'Свернуть', () => { UI.wiz = null; UI.nf = { ...UI.nf, status: 'all', group: String(w.pid), q: '', cc: null }; go('nodes'); }, finished ? 'primary' : ''),
      finished ? btn('Добавить ещё', () => openDeploy(w.pid)) : null)];
}

/* ═════════ Группы ═════════ */
function vGroups() {
  const gs = groups();
  return [pageH('Группы', 'Группа — это панель Remnawave со своим доменом, нодами и правилами мониторинга.', [btn('Новая группа', newGroup, 'primary', 'plus')]),
    gs.length ? h('div', { class: 'list' }, gs.map(groupRow))
      : empty('layers', 'Групп пока нет', 'Подключите панель Remnawave: бот проверит адрес и токен и начнёт ставить в неё ноды.', btn('Подключить панель', newGroup, 'primary', 'plus'))];
}
const GD = {};  // подробности группы: профиль, автоматизация
function vGroup(arg) {
  const pid = +arg, g = groups().find(p => p.id === pid);
  if (!g) return [pageH('Группа не найдена'), empty('layers', 'Такой группы нет', 'Возможно, её удалили или у вас нет к ней доступа.', btn('К группам', () => go('groups'), 'primary'))];
  const tab = UI.tabs['g' + pid] || 'nodes', ns = allNodes().filter(n => n.pid === pid);
  const TABS = [['nodes', `Ноды · ${ns.length}`], ['conn', 'Подключение'], ['mon', 'Проверка и починка'], ['auto', 'Автоматизация'], ['rent', 'Аренда'], ['plugins', 'Плагины'], ['clean', 'Уборка']];
  const f = { status: 'all', q: '', cc: null, group: 'all', sort: UI.nf.sort };
  const content = { nodes: () => [D.nerr[pid] ? nodesErrors() : null, ns.length ? [nodesTable(filtered(ns, f), false), nodeCards(filtered(ns, f)), bulkBar()]
      : D.nodes[pid] ? empty('server', 'В группе нет нод', 'Добавьте первые — выберите страны и тариф.', btn('Добавить ноды', () => openDeploy(pid), 'primary', 'plus')) : skeleton()],
    conn: () => groupConn(g), mon: () => groupMon(g), auto: () => groupAuto(g), rent: () => groupRent(g), plugins: () => groupPlugins(g), clean: () => groupCleanup(g) }[tab]();
  D.prel = D.prel || {};
  if (!D.prel[pid]) { D.prel[pid] = { loading: true }; api(`profiles/${pid}/panel-release`).then(r => { D.prel[pid] = r; }, () => { D.prel[pid] = {}; }).then(() => render(true)); }
  const PR = D.prel[pid];
  return [h('button', { type: 'button', class: 'back', onclick: () => go('groups') }, ic('chevL'), 'Группы'),
    pageH(g.name, `${(g.panel_url || '').replace(/^https?:\/\//, '')}` + (PR.current ? ` · панель ${PR.current}` : '') + ` · ${pl(ns.length, 'нода', 'ноды', 'нод')}`
      + (ns.some(n => n.online != null) ? ` · ${ns.reduce((s, n) => s + (n.online || 0), 0)} онлайн` : '') + (g.created_by_label ? ` · ${g.created_by_label}` : ''),
      [PR.behind ? btn(`Есть панель ${PR.latest}`, () => panelRelease(pid), 's', 'up', { style: 'color:var(--warn);border-color:var(--warn)' }) : null,
        btn('Ноды в эту группу', () => openDeploy(pid), '', 'plus')]),
    h('div', { class: 'tabs page-tabs', role: 'tablist' }, TABS.map(([v, l]) => h('button', { type: 'button', role: 'tab', 'aria-selected': String(v === tab), onclick: () => { UI.tabs['g' + pid] = v; render(); } }, l))),
    content];
}
function loadFields() { if (!D.fields) { D.fields = { loading: true }; api('fields').then(r => { D.fields = r; }).catch(e => { D.fields = { error: e.message }; }).then(() => render(true)); } return D.fields; }
/* форма группы: поля и подсказки — с сервера (/api/fields), как в классическом интерфейсе */
function profileForm(F, val, locked, hints, required, keys) {
  return keys.map(k => { const f = F.profile[k]; if (!f) return null;
    const inp = h('input', { class: 'inp' + (['name', 'email'].includes(k) ? '' : ' mono'), 'data-k': 'pf-' + k, type: f.secret ? 'password' : 'text', autocomplete: 'off',
      disabled: locked.includes(k), value: val[k] ?? '', placeholder: f.secret && hints[k] ? `сохранён ${hints[k]} · введите новый, чтобы заменить` : '', oninput: e => { val[k] = e.target.value; } });
    return fld(f.label + (required.includes(k) ? '' : ' (необязательно)'), inp, rich(f.help)); });
}
const MAIN_KEYS = ['name', 'panel_url', 'panel_token', 'panel_cookie', 'outgoing_ips', 'email', 'base_domain'];
const MORE_KEYS = ['name_pattern', 'squads', 'node_port', 'cf_token', 'owner'];
function groupConn(g) {
  const F = loadFields(), st = GD[g.id] || (GD[g.id] = {});
  if (!st.prof && !st.loading) { st.loading = true; api('profiles/' + g.id).then(r => { st.prof = r; st.val = { ...r }; MAIN_KEYS.concat(MORE_KEYS).forEach(k => { if (F.profile && F.profile[k] && F.profile[k].secret) st.val[k] = ''; }); })
    .catch(e => { st.err = e.message; }).then(() => { st.loading = false; render(true); }); }
  if (st.err) return empty('alert', 'Не загрузилось', st.err, btn('Повторить', () => { delete GD[g.id]; render(); }, 'primary'));
  if (!st.prof || !F.profile) return h('div', { class: 'sk', style: 'height:300px' });
  const p = st.prof, locked = p.locked || [];
  const save = e => busyBtn(e, async () => {
    try { const r = await api('profiles/' + g.id, 'PUT', { ...Object.fromEntries(MAIN_KEYS.concat(MORE_KEYS).filter(k => k in st.val).map(k => [k, st.val[k]])), ...(st.script !== undefined ? { script_id: st.script } : {}) }); st.check = r.check; toast(r.check.ok ? 'Сохранено, панель отвечает' : 'Сохранено, но панель не ответила', r.check.message, r.check.ok ? 'ok' : 'bad'); delete GD[g.id].prof; refreshSoon(); }
    catch (x) { st.check = { ok: false, message: x.message }; }
    render();
  });
  const check = e => busyBtn(e, async () => { try { st.check = await api(`profiles/${g.id}/check`, 'POST'); } catch (x) { st.check = { ok: false, message: x.message }; } render(); });
  return h('div', { style: 'max-width:680px' },
    h('div', { class: 'sheet form', style: 'padding:18px;margin-bottom:16px' }, profileForm(F, st.val, locked, p.hints || {}, F.required, MAIN_KEYS),
      h('details', { class: 'more-opts' }, h('summary', null, 'Ещё настройки', h('span', { class: 'faint', style: 'font-weight:400' }, ' — шаблон имён, сквады, Cloudflare, клиент Bobr Cloud')),
        h('div', { class: 'form', style: 'margin-top:12px' }, profileForm(F, st.val, locked, p.hints || {}, F.required, MORE_KEYS))),
      st.check ? h('div', { class: 'notice' + (st.check.ok ? ' ok' : ''), role: 'status', style: 'margin:0' }, h('span', { class: 'i' }, ic(st.check.ok ? 'check' : 'alert')),
        h('div', { class: 'grow' }, h('b', null, st.check.ok ? 'Панель отвечает' : 'Не получилось'), st.check.message)) : null,
      h('div', { class: 'chips' }, btn('Проверить подключение', check, '', 'refresh'), btn('Сохранить', save, 'primary'))),
    groupScript(g, st),
    p.can_delete ? [h('div', { class: 'section-h' }, h('h2', null, 'Опасные действия')),
      h('div', { class: 'list' }, dangerRow('trash', 'Удалить группу', 'Бот забудет панель и правила группы. Ноды останутся в панели, серверы — в Bobr Cloud', btn('Удалить', () => confirmTyped({
        title: `Удалить группу «${g.name}»?`, word: g.name, ok: 'Удалить группу',
        text: 'Бот перестанет управлять нодами этой группы и следить за ними. Ноды в панели и серверы в Bobr Cloud не удаляются.',
        fn: async () => { await api('profiles/' + g.id, 'DELETE'); toast(`Группа «${g.name}» удалена`); go('groups'); refreshSoon(); } }), 's danger')))] : null);
}
function groupScript(g, st) {  // чем бот ставит ноды в этой группе: встроенный установщик или утверждённый свой скрипт
  if (!D.scrList) { D.scrList = { loading: true }; api('scripts').then(r => { D.scrList = r.scripts || []; }).catch(() => { D.scrList = []; }).then(() => render(true)); }
  const list = Array.isArray(D.scrList) ? D.scrList.filter(s => s.status === 'approved') : [];
  if (!list.length) return null;
  const cur = st.script !== undefined ? st.script : (st.prof.script_id || null);
  const chosen = list.find(s => s.id === cur);
  return h('div', { class: 'sheet form', style: 'padding:16px;margin-bottom:16px' }, fld('Скрипт установки', h('select', { class: 'sel', value: String(cur || ''), style: 'max-width:420px',
      onchange: e => { st.script = e.target.value ? +e.target.value : null; render(); } }, h('option', { value: '' }, 'Встроенный установщик'), list.map(s => h('option', { value: String(s.id) }, s.title))),
    chosen && chosen.profile_mode && chosen.profile_mode !== 'file' ? 'Этот скрипт не создаёт профиль Xray сам — профиль и inbound выберите в классическом интерфейсе (карточка группы).' : 'Сохраните, чтобы применить. Уже поставленные ноды не меняются.'),
    chosen && chosen.profile_mode && chosen.profile_mode !== 'file' ? h('div', null, btn('Открыть в классическом', () => classic('group:' + g.id), 's', 'ext')) : null);
}
function newGroup() {
  const F = loadFields();
  const val = {}, body = h('div', { class: 'form' });
  const draw = () => body.replaceChildren(...(!F.profile ? [h('div', { class: 'sk', style: 'height:200px' })] : [
    h('div', null, 'Бот проверит адрес и токен панели. Остальное можно поменять потом.'),
    ...profileForm(F, val, [], {}, F.required, MAIN_KEYS).filter(Boolean),
    h('details', { class: 'more-opts' }, h('summary', null, 'Ещё настройки'), h('div', { class: 'form', style: 'margin-top:12px' }, profileForm(F, val, isAdmin() ? [] : ['owner'], {}, F.required, MORE_KEYS))),
    err]));
  const err = h('div', { class: 'err', role: 'alert' });
  if (!F.profile) { const t = setInterval(() => { if (D.fields && D.fields.profile) { clearInterval(t); Object.assign(F, D.fields); draw(); } }, 300); }
  draw();
  modal({ title: 'Подключить панель', icon: ['layers', 'run'], body, actions: [{ l: 'Отмена' }, { l: 'Проверить и создать', cls: 'primary', fn: async (c, b) => {
    b.classList.add('busy'); b.disabled = true; err.textContent = '';
    try {
      const r = await api('profiles', 'POST', val); c();
      toast(r.check.ok ? 'Группа создана, панель отвечает' : 'Группа создана, но панель не ответила', r.check.message, r.check.ok ? 'ok' : 'bad', { l: 'Добавить ноды', fn: () => openDeploy(r.id) });
      await loadAll(true); go('group/' + r.id);
    } catch (x) { err.textContent = x.message; b.classList.remove('busy'); b.disabled = false; }
  } }] });
}
const WD = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс'];
function groupRent(g) {
  const rs = D.rent[g.id];
  if (rs == null) return h('div', { class: 'sk', style: 'height:200px' });
  if (!rs.length) return empty('wallet', 'Арендованных серверов нет', 'Здесь будут серверы, которые бот купил в Bobr Cloud для этой группы.');
  return h('div', { class: 'list' }, [...rs].sort((a, b) => a.days_left - b.days_left).map(s => {
    const n = allNodes().find(x => x.pid === g.id && x.rent && x.rent.id === s.id) || { pid: g.id, name: s.name, rent: s };
    const soon = s.risk || (s.days_left <= 3 && !s.auto_renew);
    return h('div', { class: 'li', style: 'flex-wrap:wrap' }, h('span', { class: 'flag' }, s.flag || ''),
      h('div', { class: 'grow', style: 'flex:1 1 200px;min-width:0' }, h('div', { class: 't' }, s.name || s.domain), h('div', { class: 's', style: 'white-space:normal' },
        `${s.domain || s.ip} · ${rubK(s.price_kop)} за 30 дней` + (s.risk ? ' · ' + s.risk : ''))),
      h('span', { class: soon ? 'tag warn' : 'num muted' }, 'ещё ' + pl(Math.max(0, Math.round(s.days_left)), 'день', 'дня', 'дней')),
      h('label', { style: 'display:flex;align-items:center;gap:8px;cursor:pointer' }, toggle(s.auto_renew, (v, el) => setAutoRenew(n, v, el), 'Автопродление ' + s.name), h('span', { class: 'muted hide-m' }, 'авто')),
      btn('Продлить', () => renew(n), 's'));
  }));
}

/* ═════════ Задачи ═════════ */
function vActivity() {
  const f = UI.af || 'all', js = D.jobs;
  const list = js.filter(j => f === 'all' || (f === 'run' ? !['done', 'failed'].includes(j.status) : j.status === f));
  const n = s => js.filter(j => s === 'run' ? !['done', 'failed'].includes(j.status) : j.status === s).length;
  return [pageH('Задачи', 'Установки, обновления, смена IP и починка — последние 60'),
    h('div', { class: 'toolbar' }, seg([{ v: 'all', l: 'Все', n: js.length }, { v: 'run', l: 'Идут', n: n('run') }, { v: 'failed', l: 'Ошибки', n: n('failed') }, { v: 'done', l: 'Готово', n: n('done') }],
      f, v => { UI.af = v; render(); }, 'Фильтр')),
    list.length ? h('div', { class: 'list' }, list.map(taskRow)) : empty('activity', f === 'failed' ? 'Ошибок нет' : 'Пусто', f === 'failed' ? 'Так и должно быть.' : 'Здесь появятся установки и обновления.')];
}

/* ═════════ Настройки ═════════ */
const SETTINGS = () => [
  { g: 'Основное', id: 'connections', l: 'Подключения', i: 'globe', s: 'Bobr Cloud, Cloudflare, Gemini', admin: 1 },
  { g: 'Основное', id: 'notifications', l: 'Уведомления', i: 'bell', s: 'Что присылать в Telegram' },
  { g: 'Основное', id: 'summary', l: 'Утренняя сводка', i: 'clock', s: 'Итог по всем группам раз в день' },
  { g: 'Основное', id: 'checkers', l: 'Чекеры в России', i: 'radar', s: 'Кто проверяет ноды' },
  { g: 'Основное', id: 'look', l: 'Интерфейс и тема', i: 'contrast', s: 'Светлая, тёмная, классический вид' },
  { g: 'Пользователи и деньги', id: 'users', l: 'Пользователи', i: 'users', s: 'Приглашения и доступ', admin: 1 },
  { g: 'Пользователи и деньги', id: 'tariff', l: 'Подписка для пользователей', i: 'wallet', s: 'Цена, период, бесплатный срок', admin: 1 },
  { g: 'Пользователи и деньги', id: 'reports', l: 'Отзывы и ошибки', i: 'alert', s: 'Обращения пользователей и сбои задач', admin: 1 },
  { g: 'Основное', id: 'subscription', l: 'Подписка', i: 'wallet', s: 'Срок доступа и автопродление', member: 1, billing: 1 },
  { g: 'Основное', id: 'problem', l: 'Сообщить о проблеме', i: 'alert', s: 'Администратор ответит здесь же', member: 1 },
  { g: 'Сервер бота', id: 'bot', l: 'Обновление', i: 'up', s: 'Версия, автообновление', admin: 1 },
  { g: 'Сервер бота', id: 'backup', l: 'Резервные копии', i: 'box', s: 'Пароль, расписание, восстановление', admin: 1 },
  { g: 'Сервер бота', id: 'health', l: 'Нагрузка и защита', i: 'shield', s: 'Ресурсы VPS, входы по SSH', admin: 1 },
  { g: 'Сервер бота', id: 'audit', l: 'Журнал действий', i: 'activity', s: 'Кто, когда и что поменял', admin: 1 },
  { g: 'Инструменты', id: 'scripts', l: 'Мои скрипты', i: 'code', s: 'Свои установщики с GitHub', admin: 1 },
  { g: 'Инструменты', id: 'transfer', l: 'Перенос хостов', i: 'layers', s: 'Между панелями или из файла' },
  { g: 'Инструменты', id: 'routing', l: 'Роутинг хостов', i: 'layers', s: 'Новые Xray JSON для хостов: файлы или архив' },
  { g: 'Инструменты', id: 'migrate', l: 'Переезд бота', i: 'server', s: 'На другой сервер со всеми данными', admin: 1 },
].filter(s => (!s.admin || isAdmin()) && (!s.member || !isAdmin()) && (!s.billing || (D.st.billing && D.st.billing.on)));
function vSettings(sec) {
  const list = SETTINGS(), mobile = innerWidth < 720;
  if (sec && !list.some(s => s.id === sec)) sec = '';
  const cur = sec || (mobile ? '' : list[0].id);
  const item = s => h('button', { type: 'button', class: 'li', 'aria-current': s.id === cur ? 'page' : null, onclick: () => go('settings/' + s.id) },
    h('div', { class: 'ic-tile' }, ic(s.i)), h('div', { class: 'grow' }, h('div', { class: 't' }, s.l), h('div', { class: 's' }, s.s)), h('span', { class: 'chev show-m' }, ic('chevR')));
  const nav = h('div', { class: 'set-nav-wrap' }, [...new Set(list.map(s => s.g))].map(g => [h('div', { class: 'set-g' }, g),
    h('div', { class: 'list set-nav', style: 'margin-bottom:14px' }, list.filter(s => s.g === g).map(item))]));
  if (!cur) return [pageH('Настройки'), nav];
  const S_ = list.find(s => s.id === cur);
  const content = { health: setHealth, scripts: setScripts, reports: setReports, audit: setAudit, tariff: setTariff, subscription: setSubscription, problem: setProblem,
    connections: setConnections, notifications: setNotif, summary: setSummary, checkers: setCheckers, users: setUsers, bot: setBot, look: () => vLook().slice(1), transfer: setTransfer, routing: setRouting, migrate: setMigrate, backup: setBackup }[cur]();
  return mobile ? [h('button', { type: 'button', class: 'back', onclick: () => go('settings') }, ic('chevL'), 'Настройки'), pageH(S_.l), content]
    : [pageH('Настройки'), h('div', { class: 'set-grid' }, nav, h('div', { style: 'min-width:0' }, h('h2', { class: 'set-h' }, S_.l), content))];
}
function setConnections() {
  const st = GD.set || (GD.set = { val: {}, res: {} }), S = D.st.settings || {};
  const groupsOf = [['Bobr Cloud', 'bcloud', ['bcloud_url', 'bcloud_token', 'bcloud_tg_id']], ['Cloudflare', 'cloudflare', ['cf_token']],
    ['Gemini — разбор своих скриптов', 'gemini', ['gemini_key', 'gemini_model', 'gemini_proxy']],
    ['GitHub — обновления бота, версии Xray и панели', 'github', ['github_token']], ['Установка', null, ['parallel', 'ssh_pubkey']]];
  const input = k => { const f = S[k]; if (!f) return null;
    return fld(f.label, h('input', { class: 'inp' + (k === 'parallel' ? '' : ' mono'), 'data-k': 'st-' + k, type: f.secret ? 'password' : 'text', autocomplete: 'off',
      value: st.val[k] ?? (f.secret ? '' : f.value || ''), placeholder: f.secret && f.set ? `сохранён ${f.hint} · введите новый, чтобы заменить` : '', oninput: e => { st.val[k] = e.target.value; } }), rich(f.help)); };
  const save = e => busyBtn(e, async () => {
    try { const r = await api('settings', 'POST', { values: st.val }); D.st.settings = r.settings; D.st.issues = r.issues; st.val = {}; toast('Сохранено'); }
    catch (x) { toast('Не получилось', x.message, 'bad'); }
    render();
  });
  return h('div', { style: 'max-width:680px' }, D.st.issues && D.st.issues.length ? h('div', { class: 'notice', style: 'background:var(--warn-soft)' }, h('span', { class: 'i', style: 'color:var(--warn)' }, ic('sliders')),
    h('div', { class: 'grow' }, h('b', null, 'Чтобы начать'), D.st.issues.join(', '))) : null,
    groupsOf.map(([title, chk, keys]) => h('div', { class: 'sheet form', style: 'padding:16px;margin-bottom:14px' }, h('b', { style: 'font-size:15px' }, title), keys.map(input),
      chk ? h('div', { class: 'chips', style: 'align-items:center' }, btn('Проверить', e => busyBtn(e, async () => {
        try { st.res[chk] = await api('check/' + chk, 'POST'); } catch (x) { st.res[chk] = { ok: false, message: x.message }; } render(); }), 's', 'refresh'),
        st.res[chk] ? h('span', { class: st.res[chk].ok ? 'ru ok' : 'ru bad', style: 'font-weight:500' }, ic(st.res[chk].ok ? 'check' : 'alert'), st.res[chk].message) : null) : null)),
    h('div', { class: 'chips' }, btn('Сохранить', save, 'primary')));
}
const NMODE = [{ v: 'tg', l: 'Telegram' }, { v: 'bell', l: 'Колокольчик' }, { v: 'off', l: 'Выкл.' }];
function setNotif() {
  if (!D.np) { D.np = { loading: true }; api('notify/prefs?tz=' + -new Date().getTimezoneOffset()).then(r => { D.np = r; }).catch(e => { D.np = { error: e.message }; }).then(() => render(true)); }
  const P = D.np;
  if (P.loading) return h('div', { class: 'sk', style: 'height:300px' });
  if (P.error) return empty('alert', 'Не загрузилось', P.error, btn('Повторить', () => { D.np = null; render(); }, 'primary'));
  const save = async patch => { try { D.np = await api('notify/prefs', 'POST', patch); toast('Сохранено'); } catch (x) { toast('Не получилось', x.message, 'bad'); } render(); };
  return h('div', { style: 'max-width:720px' },
    h('div', { class: 'list', style: 'margin-bottom:18px' }, switchRow(`Тихие часы ${P.quiet.from}:00–${P.quiet.to}:00`, 'Ночью некритичное — только в колокольчик', P.quiet.on, v => save({ quiet: { on: v } }))),
    P.categories.map(c => [h('div', { class: 'section-h' }, h('h2', null, c.title)),
      h('div', { class: 'list', style: 'margin-bottom:18px' }, P.catalog.filter(t => t.cat === c.id).map(t => h('div', { class: 'li', style: 'flex-wrap:wrap' },
        h('div', { class: 'grow', style: 'min-width:180px' }, h('div', { class: 't', style: 'white-space:normal;font-weight:500' }, t.title)),
        seg(NMODE, P.types[t.type], v => save({ types: { [t.type]: v } }), t.title))))]));
}
function setUsers() {
  if (!D.users) { D.users = { loading: true }; api('users').then(r => { D.users = r; }).catch(e => { D.users = { error: e.message }; }).then(() => render(true)); }
  const U = D.users;
  if (U.loading) return h('div', { class: 'sk', style: 'height:300px' });
  if (U.error) return empty('alert', 'Не загрузилось', U.error, btn('Повторить', () => { D.users = null; render(); }, 'primary'));
  const reload = () => { D.users = null; render(); };
  const invites = U.invites.filter(i => i.state === 'active');
  return h('div', { style: 'max-width:720px' },
    h('div', { class: 'chips', style: 'margin-bottom:16px' }, btn('Пригласить', invite, 'primary', 'plus')),
    invites.length ? [h('div', { class: 'section-h' }, h('h2', null, 'Приглашения')), h('div', { class: 'list', style: 'margin-bottom:18px' }, invites.map(i => h('div', { class: 'li', style: 'flex-wrap:wrap' },
      h('div', { class: 'grow', style: 'flex:1 1 220px;min-width:0' }, h('div', { class: 't' }, i.note || 'Без заметки'), h('div', { class: 's' }, `использовано ${i.used.length} из ${i.max_uses}` + (i.expires_at ? ` · до ${new Date(i.expires_at * 1000).toLocaleDateString('ru-RU')}` : ''))),
      btn('Скопировать', () => copy(i.link, 'Ссылка'), 's', 'copy'),
      btn('Отозвать', () => confirmAct({ title: 'Отозвать приглашение?', text: 'Ссылка перестанет работать. Кто уже вошёл по ней — останется.', ok: 'Отозвать', cls: 'danger solid', icon: ['users', 'warn'],
        fn: async () => { await api('invites/' + i.id, 'DELETE'); toast('Приглашение отозвано'); reload(); } }), 's ghost'))))] : null,
    h('div', { class: 'section-h' }, h('h2', null, 'Пользователи')),
    U.users.length ? h('div', { class: 'list' }, U.users.map(u => h('div', { class: 'li', style: 'flex-wrap:wrap' }, avatar(u, 40),
      h('div', { class: 'grow', style: 'flex:1 1 200px;min-width:0' }, h('div', { class: 't' }, u.label + (u.first_name && !u.label.includes(u.first_name) ? ` · ${u.first_name}` : '')),
        h('div', { class: 's' }, (u.groups.length ? u.groups.map(g => g.name).join(', ') : 'групп нет') + (u.blocked ? ' · заблокирован' : ''))),
      btn('Доступ', () => userAccess(u), 's ghost'),
      btn(u.blocked ? 'Разблокировать' : 'Заблокировать', () => confirmAct({ title: u.blocked ? `Разблокировать ${u.label}?` : `Заблокировать ${u.label}?`, icon: ['users', 'warn'],
        text: u.blocked ? 'Пользователь снова сможет открыть бота.' : 'Пользователь не сможет открыть бота, пока вы не разблокируете. Его группы и ноды продолжат работать.',
        ok: u.blocked ? 'Разблокировать' : 'Заблокировать', cls: u.blocked ? 'primary' : 'danger solid',
        fn: async () => { await api(`users/${u.tg_id}/block`, 'POST', { blocked: !u.blocked }); reload(); } }), 's ghost'),
      btn('Убрать', () => confirmTyped({ title: `Убрать ${u.label}?`, word: String(u.tg_id), ok: 'Убрать доступ',
        text: 'Пользователь потеряет доступ к боту. Группы, которые он создал, останутся — их видит администратор.',
        fn: async () => { await api('users/' + u.tg_id, 'DELETE'); toast(`${u.label}: доступ убран`); reload(); } }), 's danger'))))
      : empty('users', 'Пользователей нет', 'Пригласите человека — он сможет ставить ноды в свои группы со своего баланса.'));
}
function invite() {
  const v = { note: '', uses: 1, days: 7 }, err = h('div', { class: 'err', role: 'alert' });
  modal({ title: 'Пригласить пользователя', icon: ['users', 'run'], body: h('div', { class: 'form' },
    h('div', null, 'Получится ссылка на бота. Пользователь увидит только свои группы и будет платить со своего баланса Bobr Cloud.'),
    fld('Заметка для себя', h('input', { class: 'inp', placeholder: 'Например, Вася', oninput: e => { v.note = e.target.value; } })),
    h('div', { class: 'row2' }, fld('Сколько человек', h('input', { class: 'inp', inputmode: 'numeric', value: '1', oninput: e => { v.uses = +e.target.value || 1; } })),
      fld('Действует, дней', h('input', { class: 'inp', inputmode: 'numeric', value: '7', oninput: e => { v.days = +e.target.value || 0; } }), '0 — без срока')), err),
    actions: [{ l: 'Отмена' }, { l: 'Создать ссылку', cls: 'primary', fn: async (c, b) => {
      b.classList.add('busy'); err.textContent = '';
      try {
        const r = await api('invites', 'POST', v); c(); D.users = null; render();
        modal({ title: 'Ссылка готова', icon: ['check', 'run'], body: h('div', { class: 'form' }, h('div', null, 'Отправьте её человеку.'), h('div', { class: 'log', style: 'user-select:all' }, r.link)),
          actions: [{ l: 'Скопировать', cls: 'primary', fn: cc => { copy(r.link, 'Ссылка'); cc(); } }] });
      } catch (x) { err.textContent = x.message; b.classList.remove('busy'); }
    } }] });
}

/* что изменится при обновлении панели группы: разбор релизов Remnawave от её версии */
async function panelRelease(pid) {
  const body = h('div', null, h('div', { class: 'sk', style: 'height:160px' }));
  modal({ title: `Панель Remnawave ${(D.prel[pid] || {}).latest || ''}`, icon: ['up', 'run'], body });
  let r; try { r = await api(`profiles/${pid}/panel-release?report=1`); } catch (e) { body.replaceChildren(h('div', { class: 'err' }, e.message)); return; }
  const x = r.report, rel = r.rel || {};
  if (!x) { body.replaceChildren(h('div', null, r.error || 'Панель уже на последней версии.')); return; }
  const RISK = { none: ['ok', 'не затронет'], low: ['warn', 'вряд ли затронет'], high: ['bad', 'может сломать'] };
  const line = (label, risk, why) => h('div', { class: 'li' }, h('div', { class: 'grow', style: 'min-width:0' }, h('div', { class: 't', style: 'white-space:normal' }, label),
    why ? h('div', { class: 's', style: 'white-space:normal' }, why) : null), h('span', { class: 'st ' + RISK[risk][0], style: 'flex:none' }, RISK[risk][1]));
  body.replaceChildren(h('div', null,  // h() раскрывает вложенные списки и пропускает пустое
    h('p', { class: 'muted', style: 'margin:0 0 10px' }, `У группы ${r.current} → новая ${r.latest}` + ((rel.span || []).length > 1 ? ` · изменения за ${rel.span.length} релиза` : '')),
    x.summary ? h('p', { style: 'margin:0 0 12px' }, x.summary) : null,
    x.changes.length ? [h('b', null, 'Что поменялось' + (x.plain ? ' (из заметок релиза, на английском)' : '')),
      h('ul', { style: 'margin:6px 0 12px;padding-left:20px' }, x.changes.map(c => h('li', { style: 'margin:3px 0' }, c)))] : null,
    h('div', { class: 'list', style: 'margin-bottom:12px' },
      line('Работа этого бота', x.bot_risk, x.bot_why),
      x.api_risk ? line('Интеграции по API (боты продажи подписок и т.п.)', x.api_risk, x.api_why) : null,
      x.plain ? null : h('div', { class: 'li' }, h('div', { class: 'grow', style: 'min-width:0' }, h('div', { class: 't' }, 'Ноды'),
        x.node_why ? h('div', { class: 's', style: 'white-space:normal' }, x.node_why) : null), h('span', { class: 'st ' + (x.node_update ? 'warn' : 'ok'), style: 'flex:none' }, x.node_update ? 'обновить' : 'не обязательно'))),
    x.actions.length ? [h('b', null, 'Что сделать'), h('ol', { style: 'margin:6px 0 12px;padding-left:20px' }, x.actions.map(a => h('li', { style: 'margin:3px 0' }, a)))] : null,
    x.plain ? h('div', { class: 'muted', style: 'margin-bottom:10px' }, 'Подробный разбор на русском появится, когда администратор подключит Gemini.') : null,
    rel.url ? h('a', { href: rel.url, target: '_blank', rel: 'noopener' }, 'Заметки релиза на GitHub') : null));
}
