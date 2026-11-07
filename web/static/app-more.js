'use strict';
/* Новый интерфейс: мастер «Добавить ноды», группы, задачи, настройки. Общие помощники — в app-core.js и app.js. */

const periodPrice = (kop, months, pct) => (months <= 1 ? kop : Math.ceil(kop * months * (100 - Math.max(0, pct)) / 100 / 100) * 100);  // как в Bobr Cloud
const busyBtn = async (e, fn) => { const b = e.currentTarget; b.classList.add('busy'); b.disabled = true; try { await fn(); } finally { b.classList.remove('busy'); b.disabled = false; } };
const fld = (label, input, hint, err) => h('div', { class: 'fld' }, h('label', null, label), input, err ? h('div', { class: 'err', role: 'alert' }, err) : hint ? h('div', { class: 'hint' }, hint) : null);

/* ═════════ Мастер «Добавить ноды» ═════════ */
function openDeploy(pid) {
  if (!groups().length) return modal({ title: 'Сначала подключите панель', icon: ['layers', 'run'], body: 'Ноды живут в группах: группа — это панель Remnawave со своим доменом.',
    actions: [{ l: 'Отмена' }, { l: 'Подключить панель', cls: 'primary', fn: c => { c(); newGroup(); } }] });
  if (D.st.issues && D.st.issues.length) return modal({ title: 'Нужна настройка', icon: ['sliders', 'warn'], body: 'Чтобы ставить ноды, сначала: ' + D.st.issues.join(', ') + '.',
    actions: [{ l: 'Отмена' }, { l: 'Открыть настройки', cls: 'primary', fn: c => { c(); go('settings/connections'); } }] });
  const p = parse();
  UI.wiz = { step: 0, pid: pid || (p.name === 'group' && +p.arg) || groups()[0].id, src: 'buy', locs: [], offer: {}, count: {}, period: '1m', sel: new Set(),
    domains: {}, err: '', extras: { psi: false, warp: false, xh: false }, port: '', ids: null };
  wizLoad(); go('deploy');
}
function wizLoad() {
  const w = UI.wiz, pid = w.pid;
  Object.assign(w, { cat: null, catErr: '', zones: null, zone: '', own: null, ownErr: '', locs: [], offer: {}, count: {}, sel: new Set(), domains: {} });
  api('catalog?pid=' + pid).then(r => { if (w.pid === pid) { w.cat = r; w.period = (r.periods[0] || {}).code || '1m'; } }).catch(e => { w.catErr = e.message; }).then(() => render(true));
  api(`profiles/${pid}/zones`).then(r => { if (w.pid === pid) { w.zones = r; w.zone = r.default || ''; } }).catch(e => { w.zones = { zones: [], error: e.message }; }).then(() => render(true));
}
function wizLoadOwn() {
  const w = UI.wiz;
  if (w.own || w.ownLoading) return;
  w.ownLoading = true;
  api(`profiles/${w.pid}/servers`).then(r => { w.own = r.servers || []; }).catch(e => { w.ownErr = e.message; w.own = []; }).then(() => { w.ownLoading = false; render(true); });
}
function closeWizard() {
  const w = UI.wiz, leave = () => { UI.wiz = null; closeOverlay(); };
  if (w && !w.ids && (w.locs.length || w.sel.size || w.step > 0)) confirmAct({ title: 'Закрыть мастер?', text: 'Выбор не сохранится. Ничего не куплено.', ok: 'Закрыть', cls: 'danger solid', fn: async () => leave() });
  else leave();
}
const L = code => ((UI.wiz.cat && UI.wiz.cat.locations) || []).find(l => l.code === code) || { code, name: code, flag: '', offers: [] };
function wizItems(w) {  // [{loc, n}] — сколько нод в какой локации
  if (w.src === 'own') { const by = {}; (w.own || []).filter(s => w.sel.has(s.id)).forEach(s => { by[s.location] = (by[s.location] || 0) + 1; }); return Object.entries(by).map(([loc, n]) => ({ loc, n })); }
  return w.locs.map(loc => ({ loc, n: w.count[loc] || 1 }));
}
function wizCost(w) {
  if (w.src === 'own' || !w.cat) return 0;
  const per = w.cat.periods.find(p => p.code === w.period) || { months: 1, pct: 0 };
  return w.locs.reduce((s, loc) => { const o = L(loc).offers.find(x => x.idx === w.offer[loc]); return s + (o ? periodPrice(o.price_kop, per.months, per.pct) * (w.count[loc] || 1) : 0); }, 0);
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
  const w = UI.wiz, own = w.src === 'own';
  const STEPS = own ? ['Куда', 'Имена', 'Проверка'] : ['Куда', 'Тариф', 'Имена', 'Проверка'];
  const kind = own ? ['where', 'names', 'review'][w.step] : ['where', 'tariff', 'names', 'review'][w.step];
  const items = wizItems(w), total = items.reduce((s, x) => s + x.n, 0), cost = wizCost(w);
  const bal = w.cat && w.cat.balance_kop;
  const ok = {
    where: own ? w.sel.size > 0 : w.locs.length > 0,
    tariff: w.locs.every(l => w.offer[l] != null),
    names: items.every(({ loc, n }) => (w.domains[loc] || []).filter(Boolean).length === n) && !w.namesBusy,
    review: own || bal == null || bal >= cost,
  }[kind];
  const why = { where: own ? 'Выберите серверы' : 'Выберите хотя бы одну страну', tariff: 'Выберите тариф для каждой страны', names: 'Нужно имя для каждой ноды', review: 'Не хватает денег на балансе' }[kind];
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
          w.step++;
        } catch (x) { w.err = x.message; }
      });
    } else w.step++;
    render();
  };
  const body = w.ids ? wizProgress(w) : !w.cat && !w.catErr ? h('div', { class: 'sk', style: 'height:240px' })
    : w.catErr ? empty('alert', 'Bobr Cloud не ответил', w.catErr, btn('Повторить', () => { wizLoad(); render(); }, 'primary', 'retry'))
    : { where: wizWhere, tariff: wizTariff, names: wizNamesStep, review: wizReview }[kind](w, items, cost);
  return h('div', { class: 'wizard', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Добавить ноды' },
    h('div', { class: 'wiz-top' },
      h('h2', null, w.ids ? 'Установка' : 'Добавить ноды'),
      w.ids ? null : h('div', { class: 'steps', 'aria-label': `Шаг ${w.step + 1} из ${STEPS.length}` }, STEPS.map((s, i) => [i ? h('span', { class: 'sep' }) : null,
        h('span', { class: 'stp' + (i === w.step ? ' on' : i < w.step ? ' done' : '') }, h('i', null, i < w.step ? '✓' : i + 1), h('span', null, s))])),
      h('span', { style: 'flex:1' }),
      h('button', { type: 'button', class: 'icon-btn', onclick: closeWizard, 'aria-label': 'Закрыть' }, ic('x'))),
    h('div', { class: 'wiz-body', 'data-keep': 'wiz' + w.step }, h('div', { class: 'wiz-in' }, body,
      w.err ? h('div', { class: 'notice', role: 'alert', style: 'margin-top:16px' }, h('span', { class: 'i' }, ic('alert')), h('div', { class: 'grow' }, h('b', null, 'Не получилось'), w.err)) : null)),
    w.ids || !w.cat ? null : h('div', { class: 'wiz-foot' },
      h('div', { class: 'sum' }, cost ? ['Итого ', h('b', null, rubK(cost)), ' · ', pl(total, 'нода', 'ноды', 'нод')] : total ? pl(total, 'нода', 'ноды', 'нод') : h('span', { class: 'faint' }, why)),
      h('span', { class: 'sp' }),
      w.step ? btn('Назад', () => { w.step--; w.err = ''; render(); }) : null,
      kind !== 'review' ? btn('Далее', next, 'primary', null, { disabled: !ok, title: ok ? null : why })
        : btn(own ? 'Установить' : `Купить и установить · ${rubK(cost)}`, e => busyBtn(e, () => wizSubmit(w, items)), 'primary', null, { disabled: !ok })));
}
function wizWhere(w) {
  const own = w.src === 'own', p = G(w.pid);
  if (own) wizLoadOwn();
  const minPrice = l => Math.min(...l.offers.map(o => o.price_kop));
  return [h('h3', null, 'Куда ставим'), h('p', null, own ? 'Отметьте серверы — бот поставит на них ноды.' : 'Выберите группу и страны. Тариф для каждой страны — на следующем шаге.'),
    groups().length > 1 ? h('div', { class: 'block' }, h('h4', null, 'Группа'), h('div', { class: 'chips' }, groups().map(g => h('button', { type: 'button', class: 'chip', 'aria-pressed': String(w.pid === g.id),
      onclick: () => { if (w.pid !== g.id) { w.pid = g.id; wizLoad(); render(); } } }, g.name)))) : null,
    h('div', { class: 'block' }, h('h4', null, 'Серверы'), h('div', { class: 'choice' },
      opt(!own, 'Купить в Bobr Cloud', `Бот купит серверы (${w.cat.buyer}), поставит ноды, пропишет DNS и добавит в панель`, () => { w.src = 'buy'; render(); }),
      opt(own, 'Уже купленные', 'Свободные серверы в Bobr Cloud — без новой покупки', () => { w.src = 'own'; render(); }))),
    own ? h('div', { class: 'block' }, h('h4', null, 'Свободные серверы'),
      w.own == null ? h('div', { class: 'sk', style: 'height:120px' }) : w.ownErr ? h('div', { class: 'err' }, w.ownErr)
        : !w.own.length ? empty('server', 'Серверов нет', 'В Bobr Cloud у покупателя этой группы нет купленных серверов.')
        : h('div', { class: 'list' }, w.own.map(s => h('label', { class: 'switch', style: s.ready ? '' : 'opacity:.55;cursor:default' },
          h('input', { type: 'checkbox', class: 'cb', disabled: !s.ready, checked: w.sel.has(s.id), onchange: e => { e.target.checked ? w.sel.add(s.id) : w.sel.delete(s.id); render(); } }),
          h('span', { class: 'grow' }, h('b', null, `${s.flag || ''} ${s.name || s.ip}`), h('span', null, `${s.loc_name} · ${s.cpu} vCPU, ${s.ram} ГБ, ${s.disk} ГБ · ${s.ip}` + (s.ready ? '' : ` · ${s.why}`))))))) : null,
    !own ? h('div', { class: 'block' }, h('h4', null, 'Страны'), !w.cat.locations.length ? h('div', { class: 'muted' }, 'В Bobr Cloud сейчас нет доступных локаций.')
      : h('div', { class: 'chips' }, w.cat.locations.map(l => h('button', { type: 'button', class: 'chip', 'aria-pressed': String(w.locs.includes(l.code)),
        onclick: () => { w.locs = w.locs.includes(l.code) ? w.locs.filter(x => x !== l.code) : [...w.locs, l.code]; render(); } },
        l.flag ? h('span', { class: 'flag' }, l.flag) : null, l.name, l.offers.length ? h('span', { class: 's' }, 'от ' + rubK(minPrice(l))) : null)))) : null,
    p.script ? h('div', { class: 'muted' }, `Скрипт установки группы: ${p.script}.`) : null];
}
function opt(on, t, s, onclick, extra) {
  return h('button', { type: 'button', class: 'opt', 'aria-pressed': String(on), onclick }, h('span', { class: 'ck' }, ic('check')), h('b', null, t), h('span', { class: 's' }, s), extra || null);
}
function wizTariff(w) {
  const per = w.cat.periods;
  return [h('h3', null, 'Тариф и количество'), h('p', null, 'Тариф выбирается явно для каждой страны. ОС — ' + w.cat.os_label + '.'),
    w.locs.map(code => { const l = L(code);
      return h('div', { class: 'loc-card' },
        h('div', { class: 'h' }, l.flag ? h('span', { class: 'flag' }, l.flag) : null, l.name,
          h('span', { style: 'margin-left:auto;display:flex;align-items:center;gap:10px;font-weight:500' }, h('span', { class: 'muted hide-m' }, 'Количество'),
            h('span', { class: 'stepper' }, h('button', { type: 'button', 'aria-label': 'Меньше', onclick: () => { w.count[code] = Math.max(1, (w.count[code] || 1) - 1); w.domains = {}; render(); } }, '−'),
              h('span', { class: 'num', 'aria-live': 'polite' }, w.count[code] || 1),
              h('button', { type: 'button', 'aria-label': 'Больше', onclick: () => { w.count[code] = Math.min(20, (w.count[code] || 1) + 1); w.domains = {}; render(); } }, '+')))),
        h('div', { class: 'choice' }, l.offers.map(o => opt(w.offer[code] === o.idx, `${o.cpu} vCPU · ${o.ram} ГБ`, `диск ${o.disk} ГБ` + (o.speed ? ` · ${o.speed}` : ''),
          () => { w.offer[code] = o.idx; render(); }, h('span', { class: 'price' }, rubK(o.price_kop), h('span', { class: 'faint', style: 'font:400 13px var(--font)' }, ' / мес'))))));
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
    h('details', { class: 'more-opts', open: UI.advOpen || null, ontoggle: e => { UI.advOpen = e.target.open; } },
      h('summary', null, 'Дополнительно', h('span', { class: 'faint', style: 'font-weight:400' }, ' — дополнения и порт, у большинства не меняются')),
      h('div', { class: 'list', style: 'margin:12px 0' },
        switchRow('Psiphon', 'Запасной выход, когда сайты блокируют IP хостера', w.extras.psi, v => { w.extras.psi = v; }),
        switchRow('WARP', 'Выход через Cloudflare для сервисов, которые не любят IP дата-центров', w.extras.warp, v => { w.extras.warp = v; }),
        p.script ? null : switchRow('Запасной XHTTP', 'Второй способ подключения, если основной режут', w.extras.xh, v => { w.extras.xh = v; })),
      fld('Порт для клиентов', h('input', { class: 'inp mono', 'data-k': 'wport', style: 'max-width:160px', inputmode: 'numeric', placeholder: '443', value: w.port, oninput: e => { w.port = e.target.value.trim(); } }),
        'Пусто — 443, как у обычного HTTPS.'))];
}
function switchRow(title, text, checked, onchange) {
  return h('label', { class: 'switch' }, h('span', { class: 'grow' }, h('b', null, title), text ? h('span', null, text) : null), toggle(checked, onchange, title));
}
function wizReview(w, items, cost) {
  const own = w.src === 'own', per = (w.cat.periods.find(p => p.code === w.period) || { months: 1, pct: 0 }), bal = w.cat.balance_kop;
  const row = (l, v, cls) => h('div', { class: 'li' }, h('div', { class: 'grow', style: 'min-width:0' }, l), h('span', { class: 'num ' + (cls || '') }, v));
  const ex = [w.extras.psi && 'Psiphon', w.extras.warp && 'WARP', w.extras.xh && 'XHTTP'].filter(Boolean);
  return [h('h3', null, 'Проверьте и запустите'), h('p', null, own ? 'Покупки нет: нода ставится поверх системы сервера.' : `Деньги спишутся с баланса: ${w.cat.buyer}. Если что-то пойдёт не так, повтор не купит сервер второй раз.`),
    h('div', { class: 'list', style: 'margin-bottom:16px' },
      row(h('span', null, 'Группа ', h('b', null, G(w.pid).name)), ''),
      items.map(({ loc, n }) => { const o = !own && L(loc).offers.find(x => x.idx === w.offer[loc]);
        return row(h('span', null, L(loc).flag ? h('span', { class: 'flag' }, L(loc).flag) : null, ` ${L(loc).name} × ${n}` + (o ? ` · ${o.cpu} vCPU, ${o.ram} ГБ · ${per.months} мес` : '')),
          o ? rubK(periodPrice(o.price_kop, per.months, per.pct) * n) : 'свой сервер', o ? '' : 'faint'); }),
      own ? null : row(h('b', null, 'Итого'), h('b', null, rubK(cost)))),
    h('div', { class: 'list', style: 'margin-bottom:16px' },
      row('Адреса', items.flatMap(({ loc }) => w.domains[loc]).join(', '), 'mono'),
      row('Дополнения', ex.join(', ') || 'нет'), row('Порт', w.port || '443')),
    !own && bal != null ? (bal < cost ? h('div', { class: 'notice' }, h('span', { class: 'i' }, ic('wallet')), h('div', { class: 'grow' }, h('b', null, `Не хватает ${rubK(cost - bal)}`),
      `На балансе ${rubK(bal)}. Пополнить можно в Bobr Cloud или в классическом интерфейсе.`), btn('Пополнить', () => classic(''), 's', 'ext'))
      : h('div', { class: 'muted' }, `На балансе ${rubK(bal)}, после покупки останется ${rubK(bal - cost)}.`)) : null];
}
async function wizSubmit(w, items) {
  const own = w.src === 'own';
  const payload = { pid: w.pid, period: w.period, port: w.port || null,
    items: items.map(({ loc }) => ({ location: loc, domains: w.domains[loc],
      ...(own ? { servers: (w.own || []).filter(s => w.sel.has(s.id) && s.location === loc).map(s => s.id) } : { idx: w.offer[loc] }) })),
    psiphon: w.extras.psi ? { enabled: true, region: '', domains: '' } : undefined, warp: w.extras.warp ? { enabled: true, domains: '' } : undefined,
    xhttp: w.extras.xh ? { enabled: true, mode: 'both' } : undefined };
  try { const r = await api('deploy', 'POST', payload); w.ids = r.ids; w.err = ''; toast(`Запущено: ${pl(r.ids.length, 'установка', 'установки', 'установок')}`); refreshSoon(); }
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
  const TABS = [['nodes', `Ноды · ${ns.length}`], ['conn', 'Подключение'], ['mon', 'Мониторинг'], ['auto', 'Автоматизация'], ['rent', 'Аренда']];
  const f = { status: 'all', q: '', cc: null, group: 'all', sort: UI.nf.sort };
  const content = { nodes: () => [D.nerr[pid] ? nodesErrors() : null, ns.length ? [nodesTable(filtered(ns, f), false), nodeCards(filtered(ns, f)), bulkBar()]
      : D.nodes[pid] ? empty('server', 'В группе нет нод', 'Добавьте первые — выберите страны и тариф.', btn('Добавить ноды', () => openDeploy(pid), 'primary', 'plus')) : skeleton()],
    conn: () => groupConn(g), mon: () => groupMon(g), auto: () => groupAuto(g), rent: () => groupRent(g) }[tab]();
  return [h('button', { type: 'button', class: 'back', onclick: () => go('groups') }, ic('chevL'), 'Группы'),
    pageH(g.name, `${(g.panel_url || '').replace(/^https?:\/\//, '')} · ${pl(ns.length, 'нода', 'ноды', 'нод')}` + (g.created_by_label ? ` · ${g.created_by_label}` : ''),
      [btn('Ноды в эту группу', () => openDeploy(pid), '', 'plus')]),
    h('div', { class: 'tabs page-tabs', role: 'tablist' }, TABS.map(([v, l]) => h('button', { type: 'button', role: 'tab', 'aria-selected': String(v === tab), onclick: () => { UI.tabs['g' + pid] = v; render(); } }, l))),
    content];
}
function loadFields() { if (!D.fields) { D.fields = { loading: true }; api('fields').then(r => { D.fields = r; }).catch(e => { D.fields = { error: e.message }; }).then(() => render(true)); } return D.fields; }
/* форма группы: поля и подсказки — с сервера (/api/fields), как в классическом интерфейсе */
function profileForm(F, val, locked, hints, required, keys) {
  return keys.map(k => { const f = F.profile[k]; if (!f) return null;
    const inp = h('input', { class: 'inp' + (['name', 'email'].includes(k) ? '' : ' mono'), 'data-k': 'pf-' + k, type: f.secret ? 'password' : 'text', autocomplete: 'off',
      disabled: locked.includes(k), value: val[k] ?? '', placeholder: f.secret && hints[k] ? `сохранён ${hints[k]} · введите новый, чтобы заменить` : '', oninput: e => { val[k] = e.target.value; } });
    return fld(f.label + (required.includes(k) ? '' : ' (необязательно)'), inp, f.help); });
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
    try { const r = await api('profiles/' + g.id, 'PUT', Object.fromEntries(MAIN_KEYS.concat(MORE_KEYS).filter(k => k in st.val).map(k => [k, st.val[k]]))); st.check = r.check; toast(r.check.ok ? 'Сохранено, панель отвечает' : 'Сохранено, но панель не ответила', r.check.message, r.check.ok ? 'ok' : 'bad'); delete GD[g.id].prof; refreshSoon(); }
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
    h('div', { class: 'list', style: 'margin-bottom:16px' },
      dangerRow('code', 'Скрипт установки, плагины нод, перенос хостов', 'Пока в классическом интерфейсе', btn('Открыть', () => classic('group:' + g.id), 's', 'ext'))),
    p.can_delete ? [h('div', { class: 'section-h' }, h('h2', null, 'Опасные действия')),
      h('div', { class: 'list' }, dangerRow('trash', 'Удалить группу', 'Бот забудет панель и правила группы. Ноды останутся в панели, серверы — в Bobr Cloud', btn('Удалить', () => confirmTyped({
        title: `Удалить группу «${g.name}»?`, word: g.name, ok: 'Удалить группу',
        text: 'Бот перестанет управлять нодами этой группы и следить за ними. Ноды в панели и серверы в Bobr Cloud не удаляются.',
        fn: async () => { await api('profiles/' + g.id, 'DELETE'); toast(`Группа «${g.name}» удалена`); go('groups'); refreshSoon(); } }), 's danger')))] : null);
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
      await loadAll(); go('group/' + r.id);
    } catch (x) { err.textContent = x.message; b.classList.remove('busy'); b.disabled = false; }
  } }] });
}
function groupMon(g) {
  if (!D.mon) return empty('radar', 'Мониторинг выключен', 'На этом боте нет проверки из России.');
  const m = (D.mon.groups || []).find(x => x.id === g.id);
  if (!m) return h('div', { class: 'sk', style: 'height:200px' });
  const st = GD['m' + g.id] || (GD['m' + g.id] = { steps: [...m.mon_steps], mode: m.mon_mode, quiet: m.mon_quiet, sub: '' });
  const save = e => busyBtn(e, async () => {
    try {
      await api(`profiles/${g.id}/monitor`, 'POST', { mon_mode: st.mode, mon_steps: st.steps, mon_quiet: st.quiet, ...(st.sub ? { mon_sub: st.sub } : {}) });
      toast('Мониторинг группы сохранён'); delete GD['m' + g.id]; D.mon = await api('monitor'); render();
    } catch (x) { toast('Не получилось', x.message, 'bad'); }
  });
  const ALL = Object.keys(D.mon.steps);
  return h('div', { style: 'max-width:720px' },
    h('div', { class: 'sheet form', style: 'padding:16px;margin-bottom:16px' },
      fld('Подписка для проверки', h('input', { class: 'inp mono', type: 'password', autocomplete: 'off', 'data-k': 'msub', value: st.sub, oninput: e => { st.sub = e.target.value.trim(); },
        placeholder: m.enabled ? `сохранена ${m.sub_hint || ''} · введите новую, чтобы заменить` : 'https://… — ссылка подписки любого клиента группы' }),
        m.enabled ? 'Чекеры в России подключаются по ней к каждой ноде. «-» — выключить проверку группы.' : 'Без подписки бот не проверяет ноды группы из России.'),
      fld('Что делать, если нода не проходит', seg(Object.entries(D.mon.modes).map(([v, l]) => ({ v, l })), st.mode, v => { st.mode = v; render(); }, 'Режим'))),
    h('div', { class: 'section-h' }, h('h2', null, 'Шаги починки'), h('span', { class: 'faint', style: 'font-size:13px' }, 'по порядку')),
    h('div', { class: 'list steps-list', style: 'margin-bottom:16px' }, ALL.map(k => h('label', { class: 'switch' },
      h('span', { class: 'num-i' }, st.steps.includes(k) ? st.steps.indexOf(k) + 1 : '–'), h('span', { class: 'grow' }, h('b', null, D.mon.steps[k][0].toUpperCase() + D.mon.steps[k].slice(1))),
      toggle(st.steps.includes(k), v => { st.steps = v ? [...st.steps, k].sort((a, b) => ALL.indexOf(a) - ALL.indexOf(b)) : st.steps.filter(x => x !== k); render(); }, D.mon.steps[k])))),
    h('div', { class: 'list', style: 'margin-bottom:16px' }, switchRow('Без уведомлений', 'Проверять и чинить молча — итог будет в колокольчике', st.quiet, v => { st.quiet = v; })),
    h('div', { class: 'chips' }, btn('Сохранить', save, 'primary'), btn('Чекеры', () => go('settings/checkers'), '', 'radar'), btn('Какие ноды докупать', () => classic('monitor'), 'ghost', 'ext')));
}
const WD = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс'];
function groupAuto(g) {
  const st = GD['a' + g.id];
  if (!st) { GD['a' + g.id] = { loading: true }; api(`profiles/${g.id}/auto`).then(r => { GD['a' + g.id] = { a: r }; }).catch(e => { GD['a' + g.id] = { err: e.message }; }).then(() => render(true)); }
  if (!st || st.loading) return h('div', { class: 'sk', style: 'height:240px' });
  if (st.err) return empty('alert', 'Не загрузилось', st.err, btn('Повторить', () => { delete GD['a' + g.id]; render(); }, 'primary'));
  const a = st.a;
  const save = async patch => { try { const r = await api(`profiles/${g.id}/auto`, 'POST', patch); Object.assign(a, r); toast('Сохранено'); } catch (x) { toast('Не получилось', x.message, 'bad'); } render(); };
  return h('div', { style: 'max-width:720px' },
    h('div', { class: 'list', style: 'margin-bottom:12px' },
      switchRow('Автообновление нод', `По одной, с проверкой и откатом, ${a.weekday == null ? 'каждый день' : 'по ' + WD[a.weekday]} в ${a.hour}:00`, a.update, v => save({ update: v })),
      switchRow('Следить за нагрузкой', 'Сообщать, когда процессор, память или канал ноды на пределе', a.load, v => save({ load: v })),
      switchRow('Автопродление аренды', 'Для новых серверов группы — за 3 дня до конца, если хватает баланса', !!a.renew, v => save({ renew: v }))),
    a.update ? h('div', { class: 'sheet form', style: 'padding:16px;margin-bottom:12px' },
      fld('Когда обновлять', h('div', { class: 'chips' },
        h('select', { class: 'sel', 'aria-label': 'День', value: a.weekday == null ? '' : String(a.weekday), onchange: e => save({ weekday: e.target.value === '' ? null : +e.target.value }) },
          h('option', { value: '' }, 'каждый день'), WD.map((d, i) => h('option', { value: String(i) }, d))),
        h('select', { class: 'sel', 'aria-label': 'Час', value: String(a.hour), onchange: e => save({ hour: +e.target.value }) }, Array.from({ length: 24 }, (_, i) => h('option', { value: String(i) }, `${i}:00`)))),
        a.last && a.last.date ? `Последний раз: ${a.last.date}` + (a.last.tag ? ` → ${a.last.tag}` : '') : 'Время — по часовому поясу из утренней сводки.')) : null,
    h('div', { class: 'list' }, dangerRow('zap', 'Автомасштабирование и прогноз баланса', 'Докупать ноды при нагрузке, предупреждать о деньгах — пока в классическом интерфейсе',
      btn('Открыть', () => classic('group:' + g.id), 's', 'ext'))));
}
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
  { id: 'connections', l: 'Подключения', i: 'globe', s: 'Bobr Cloud, Cloudflare, Gemini', admin: 1 },
  { id: 'notifications', l: 'Уведомления', i: 'bell', s: 'Что присылать в Telegram' },
  { id: 'checkers', l: 'Чекеры в России', i: 'radar', s: 'Кто проверяет ноды' },
  { id: 'users', l: 'Пользователи', i: 'users', s: 'Приглашения и доступ', admin: 1 },
  { id: 'bot', l: 'Сервер бота', i: 'server', s: 'Обновление и резервные копии', admin: 1 },
  { id: 'look', l: 'Интерфейс и тема', i: 'contrast', s: 'Светлая, тёмная, классический вид' },
  { id: 'classic', l: 'Ещё в классическом', i: 'ext', s: 'Скрипты, плагины, мосты, переезд' },
].filter(s => !s.admin || isAdmin());
function vSettings(sec) {
  const list = SETTINGS(), mobile = innerWidth < 720;
  if (sec && !list.some(s => s.id === sec)) sec = '';
  const cur = sec || (mobile ? '' : list[0].id);
  const nav = h('div', { class: 'list set-nav' }, list.map(s => h('button', { type: 'button', class: 'li', 'aria-current': s.id === cur ? 'page' : null, onclick: () => go('settings/' + s.id) },
    h('div', { class: 'ic-tile' }, ic(s.i)), h('div', { class: 'grow' }, h('div', { class: 't' }, s.l), h('div', { class: 's' }, s.s)), h('span', { class: 'chev show-m' }, ic('chevR')))));
  if (!cur) return [pageH('Настройки'), nav];
  const S_ = list.find(s => s.id === cur);
  const content = { connections: setConnections, notifications: setNotif, checkers: setCheckers, users: setUsers, bot: setBot, look: () => vLook().slice(1), classic: setClassic }[cur]();
  return mobile ? [h('button', { type: 'button', class: 'back', onclick: () => go('settings') }, ic('chevL'), 'Настройки'), pageH(S_.l), content]
    : [pageH('Настройки'), h('div', { class: 'set-grid' }, nav, h('div', { style: 'min-width:0' }, h('h2', { class: 'set-h' }, S_.l), content))];
}
function setConnections() {
  const st = GD.set || (GD.set = { val: {}, res: {} }), S = D.st.settings || {};
  const groupsOf = [['Bobr Cloud', 'bcloud', ['bcloud_url', 'bcloud_token', 'bcloud_tg_id']], ['Cloudflare', 'cloudflare', ['cf_token']],
    ['Gemini — разбор своих скриптов', 'gemini', ['gemini_key', 'gemini_model', 'gemini_proxy']], ['Установка', null, ['parallel', 'ssh_pubkey']]];
  const input = k => { const f = S[k]; if (!f) return null;
    return fld(f.label, h('input', { class: 'inp' + (k === 'parallel' ? '' : ' mono'), 'data-k': 'st-' + k, type: f.secret ? 'password' : 'text', autocomplete: 'off',
      value: st.val[k] ?? (f.secret ? '' : f.value || ''), placeholder: f.secret && f.set ? `сохранён ${f.hint} · введите новый, чтобы заменить` : '', oninput: e => { st.val[k] = e.target.value; } }), f.help); };
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
    U.users.length ? h('div', { class: 'list' }, U.users.map(u => h('div', { class: 'li', style: 'flex-wrap:wrap' }, h('span', { class: 'avatar' }, (u.first_name || u.label || '?')[0]),
      h('div', { class: 'grow', style: 'flex:1 1 200px;min-width:0' }, h('div', { class: 't' }, u.label + (u.first_name && !u.label.includes(u.first_name) ? ` · ${u.first_name}` : '')),
        h('div', { class: 's' }, (u.groups.length ? u.groups.map(g => g.name).join(', ') : 'групп нет') + (u.blocked ? ' · заблокирован' : ''))),
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
function setClassic() {
  const row = (i, t, s, open) => h('button', { type: 'button', class: 'li', onclick: () => classic(open) }, h('div', { class: 'ic-tile' }, ic(i)),
    h('div', { class: 'grow' }, h('div', { class: 't' }, t), h('div', { class: 's', style: 'white-space:normal' }, s)), h('span', { class: 'chev' }, ic('ext')));
  return [h('p', { class: 'muted', style: 'margin:0 0 14px' }, 'Эти разделы пока есть только в классическом интерфейсе — откроется сразу нужный.'),
    h('div', { class: 'list' },
      row('radar', 'Какие ноды докупать при блокировке', 'Тарифы и запасной вариант для шага «новая нода»', 'monitor'),
      row('route', 'Вход через Россию', 'Мост через российский сервер — на карточке ноды', 'servers'),
      isAdmin() ? row('terminal', 'Мои скрипты, плагины нод, переезд бота, журнал действий', 'Вкладка «Настройки» классического интерфейса', 'settings') : null,
      row('wallet', 'Подписка на бота', 'Оплата доступа для приглашённых', ''))];
}
