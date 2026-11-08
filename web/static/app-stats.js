'use strict';
/* Раздел «Статистика»: где и почему блокируют ноды (по истории блокировок), разбор нейросетью и сколько тратит бот.
   На «Обзоре» — короткая карточка. Советы по локации при покупке берутся отсюда же. Обновляем раз в 10 минут. */

const STATS_TTL = 600e3;
function loadStats(force) {
  const S = D.stats, days = UI.statsDays || 90;
  if (!force && S && S.days === days && (S.loading || Date.now() - S.at < STATS_TTL)) return;
  D.stats = { ...(S || {}), loading: true, at: Date.now(), days };
  Promise.all([api('stats/blocks?days=' + days).catch(() => null), api('stats/costs?days=30').catch(() => null)])
    .then(([blocks, costs]) => { D.stats = { blocks, costs, at: Date.now(), days }; render(true); });
}
/* совет для покупки в локации code: { here, best } или null */
function locAdvice(code) {
  loadStats();
  return code && D.stats && D.stats.blocks && (D.stats.blocks.advice || {})[code] || null;
}
const dec = x => String(x).replace('.', ',');
const rate = r => r.rate == null ? 'мало данных' : r.rate === 0 ? 'не блокировали' : `${dec(r.rate)} в мес. на ноду`;
function adviceNote(a) {
  if (!a) return null;
  const x = a.here, b = a.best;
  return h('div', { class: 'notice', style: 'background:var(--warn-soft);margin:0' }, h('span', { class: 'i', style: 'color:var(--warn)' }, ic('alert')),
    h('div', null, h('b', null, `${x.flag || ''} ${x.label}: ` + (x.subnet >= 2 ? 'режут подсети хостера' : 'ноды блокируют часто')),
      `За ${D.stats.blocks.days} дней: ${rate(x)}` + (x.life_days != null ? `, до блокировки нода живёт около ${dec(x.life_days)} дн` : '') +
      `. Для сравнения, ${b.flag || ''} ${b.label}: ${rate(b)}` + (b.life_days != null ? `, около ${dec(b.life_days)} дн` : '') + '.'
      + (x.subnet ? ` Здесь ${pl(x.subnet, 'раз', 'раза', 'раз')} резали всю подсеть /24 — похоже на бан хостера.` : '')));
}

function statsCards() {
  loadStats();
  const S = D.stats || {};
  const B = S.blocks, C = S.costs;
  const by = UI.statsBy || 'locations';
  const steps = (D.mon && D.mon.steps) || {};
  const fixName = k => k === 'self' ? 'прошло само' : k === 'replace' ? 'новая нода' : k === 'removed' ? 'нода убрана' : steps[k] || k;
  const rows = B ? B[by].filter(r => r.nodes || r.blocks) : [];
  const best = rows.filter(r => r.rate != null && r.months >= 1).sort((a, b) => a.rate - b.rate)[0];
  const blocksCard = h('section', { class: 'section' },
    h('div', { class: 'section-h', style: 'flex-wrap:wrap;row-gap:8px' }, h('h2', null, 'Где блокируют реже'), h('span', { class: 'faint', style: 'font-size:13px' }, (B ? B.days : UI.statsDays || 90) + ' дней'),
      h('select', { class: 'sel', 'aria-label': 'Разрез', style: 'margin-left:auto;max-width:200px', onchange: e => { UI.statsBy = e.target.value; render(); } },
        STATS_BY.map(([k, l]) => h('option', { value: k, selected: by === k || null }, l)))),
    !B ? h('div', { class: 'sk', style: 'height:160px' })
      : !rows.length ? h('div', { class: 'sheet', style: 'padding:16px' }, h('div', { class: 'muted' }, by === 'locations' || by === 'configs'
        ? 'Пока нечего сравнивать: бот начнёт записывать блокировки нод, купленных через него, с этой версии.'
        : 'Пока нет данных в этом разрезе: сети нод бот узнаёт в фоне (обновите через минуту), настройки и соседей записывает с новых блокировок.'))
      : h('div', { class: 'list' }, rows.slice(0, 12).map(r => {
        const top = Object.entries(r.fixes).sort((a, b) => b[1] - a[1])[0];
        return h('div', { class: 'li' },
          by === 'locations' ? h('span', { class: 'flag', style: 'font-size:20px;width:28px;text-align:center' }, r.flag || '🏳') : h('div', { class: 'ic-tile' }, ic('box')),
          h('div', { class: 'grow', style: 'min-width:0' }, h('div', { class: 't', style: 'white-space:normal' }, r.label || 'не указано', r === best && rows.length > 1 ? h('span', { class: 'st ok', style: 'margin-left:8px' }, 'реже всех') : null),
            h('div', { class: 's', style: 'white-space:normal' }, [pl(r.nodes, 'нода', 'ноды', 'нод'), r.blocks ? pl(r.blocks, 'блокировка', 'блокировки', 'блокировок') : 'без блокировок',
              r.life_days != null ? `живёт ~${dec(r.life_days)} дн` : '', r.hours != null ? `блокировка ~${dec(r.hours)} ч` : '', top ? 'чаще помогало: ' + fixName(top[0]) : '',
              r.outages ? pl(r.outages, 'падение сервера', 'падения сервера', 'падений сервера') : ''].filter(Boolean).join(' · '))),
          r.rate ? h('div', { style: 'text-align:right;flex:none' }, h('b', { class: 'num', style: 'font-size:17px;' + (best && r.rate >= Math.max(best.rate * 2, best.rate + 0.5) ? 'color:var(--warn)' : '') }, dec(r.rate)),
            h('div', { class: 's', style: 'font-size:12px;color:var(--ink-3)' }, 'в мес. на ноду')) : h('span', { class: 's', style: 'flex:none;font-size:13px;color:var(--ink-3)' }, rate(r)));
      })));
  const rent = groups().flatMap(p => D.rent[p.id] || []);
  const monthly = rent.filter(x => x.auto_renew !== false).reduce((s, x) => s + (x.price_kop || 0), 0);
  const costCard = h('section', { class: 'section' },
    h('div', { class: 'section-h' }, h('h2', null, 'Расходы'), h('span', { class: 'faint', style: 'font-size:13px' }, '30 дней')),
    !C ? h('div', { class: 'sk', style: 'height:160px' }) : h('div', { class: 'list' },
      h('div', { class: 'li' }, h('div', { class: 'ic-tile' }, ic('server')), h('div', { class: 'grow' }, h('div', { class: 't' }, 'Аренда в месяц'),
        h('div', { class: 's' }, rent.length ? `${pl(rent.filter(x => x.auto_renew !== false).length, 'сервер', 'сервера', 'серверов')} с автопродлением` : 'серверы бота в Bobr Cloud')), h('b', { class: 'num' }, rubK(monthly))),
      C.kinds.map(k => h('div', { class: 'li' }, h('div', { class: 'ic-tile' }, ic(k.kind === 'reip' ? 'globe' : k.kind === 'replace' ? 'refresh' : 'box')),
        h('div', { class: 'grow' }, h('div', { class: 't' }, k.label), h('div', { class: 's' }, pl(k.n, 'раз', 'раза', 'раз'))), h('span', { class: 'num' }, rubK(k.kop)))),
      h('div', { class: 'li' }, h('div', { class: 'grow' }, h('b', null, 'Разово за 30 дней'), h('div', { class: 's', style: 'white-space:normal' }, 'по задачам бота: покупки, замены, докупка и смены IP')),
        h('b', { class: 'num' }, rubK(C.total_kop)))),
    C && (C.hosters || []).length ? [h('div', { class: 'section-h', style: 'margin-top:16px' }, h('h2', { style: 'font-size:15px' }, 'Во что обошлись блокировки'),
      h('span', { class: 'faint', style: 'font-size:13px' }, 'замены и смены IP по хостерам')),
      h('div', { class: 'list' }, C.hosters.map(x => h('div', { class: 'li' }, h('div', { class: 'grow', style: 'min-width:0' }, h('div', { class: 't', style: 'white-space:normal' }, x.label),
        h('div', { class: 's' }, pl(x.n, 'раз', 'раза', 'раз'))), h('span', { class: 'num' }, rubK(x.kop)))))] : null);
  return { blocksCard, costCard, B };
}
/* «Обзор»: итоги одной карточкой и вход в раздел */
function statsSection() {
  const { B } = statsCards(), o = B && B.overall;
  return h('section', { class: 'section' }, h('div', { class: 'section-h' }, h('h2', null, 'Блокировки'), btn('Статистика', () => go('stats'), 'ghost s more')),
    !B ? h('div', { class: 'sk', style: 'height:80px' }) : h('button', { type: 'button', class: 'sheet', style: 'display:block;width:100%;text-align:left;padding:16px;cursor:pointer;border:0;color:inherit;font:inherit', onclick: () => go('stats') },
      h('div', null, B.total ? `${pl(B.total, 'блокировка', 'блокировки', 'блокировок')} за ${B.days} дней` + (o && o.rate != null ? ` · ${dec(o.rate)} в мес. на ноду` : '') : `За ${B.days} дней блокировок не было`),
      h('div', { class: 'muted', style: 'margin-top:4px' }, B.ai ? 'Нейросеть: ' + B.ai.summary.slice(0, 160) + (B.ai.summary.length > 160 ? '…' : '') : 'Хостеры, подсети, проверки соседей и разбор нейросетью — в «Статистике».')));
}
/* раздел «Статистика» */
function vStats() {
  const { blocksCard, costCard, B } = statsCards(), o = B && B.overall, days = UI.statsDays || 90;
  const tile = (v, l, warn) => h('div', { class: 'sheet', style: 'padding:14px 16px;min-width:0' }, h('b', { class: 'num', style: 'font-size:22px;display:block;' + (warn ? 'color:var(--warn)' : '') }, v), h('div', { class: 'muted', style: 'font-size:13px' }, l));
  const top = B && (B.hosters || []).filter(r => r.rate != null && r.blocks).sort((a, b) => b.rate - a.rate)[0];
  return [
    pageH('Статистика', 'Где и почему блокируют ноды, разбор нейросетью и расходы'),
    h('div', { class: 'toolbar' }, seg([[30, '30 дн'], [90, '90 дн'], [180, '180 дн'], [365, 'Год']].map(([v, l]) => ({ v, l })), days,
      v => { UI.statsDays = v; loadStats(true); render(); }, 'Период')),
    !B ? h('div', { class: 'sk', style: 'height:90px;margin-bottom:16px' }) : h('div', { style: 'display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:20px' },
      tile(o ? o.nodes : 0, 'нод под наблюдением'), tile(B.total, 'блокировок', B.total > 0),
      tile(o && o.rate != null ? dec(o.rate) : '—', 'в мес. на ноду'), tile(o && o.life_days != null ? dec(o.life_days) + ' дн' : '—', 'живёт до блокировки'),
      top ? tile(top.label.length > 18 ? top.label.slice(0, 17) + '…' : top.label, `чаще всех: ${dec(top.rate)} в мес.`, true) : null),
    B ? aiCard(B) : null,
    h('div', { class: 'grid2' }, blocksCard, B ? banSignals(B) || h('div') : h('div')),
    B ? recentBlocks(B) : null,
    costCard,
  ];
}
/* последние блокировки со снимком ноды */
function recentBlocks(B) {
  const rs = B.recent || [];
  if (!rs.length) return null;
  const steps = (D.mon && D.mon.steps) || {};
  const fixName = k => !k ? '' : k === 'self' ? 'прошло само' : k === 'replace' ? 'новая нода' : k === 'removed' ? 'нода убрана' : steps[k] || k;
  const dur = b => { const s = (b.end || Date.now() / 1000) - b.start; return s < 3600 ? Math.max(1, Math.round(s / 60)) + ' мин' : s < 86400 ? Math.round(s / 3600) + ' ч' : Math.round(s / 86400) + ' дн'; };
  return h('section', { class: 'section' }, h('div', { class: 'section-h' }, h('h2', null, 'Последние блокировки')),
    h('div', { class: 'list' }, rs.slice(0, UI.recentAll ? 40 : 10).map(b => h('div', { class: 'li' },
      h('div', { class: 'grow', style: 'min-width:0' }, h('div', { class: 't', style: 'white-space:normal' }, b.name && b.flag && !b.name.includes(b.flag) ? b.flag + ' ' + b.name : b.name || '—'),
        h('div', { class: 's', style: 'white-space:normal' }, [new Date(b.start * 1000).toLocaleString('ru-RU', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }),
          b.hoster || b.holder || '', b.net24 || '', b.port ? 'порт ' + b.port : '', b.stealth ? 'уникальные настройки' : '',
          b.users != null ? `${b.users} онлайн` : '', b.neigh ? NEIGH[b.neigh] : '', fixName(b.fix) ? 'помогло: ' + fixName(b.fix) : ''].filter(Boolean).join(' · '))),
      h('span', { class: 'st ' + (b.end ? '' : 'bad'), style: 'flex:none' }, b.end ? dur(b) : 'идёт ' + dur(b))))),
    rs.length > 10 ? h('div', { class: 'chips', style: 'margin-top:10px' }, btn(UI.recentAll ? 'Свернуть' : `Показать все (${rs.length})`, () => { UI.recentAll = !UI.recentAll; render(); }, 's ghost')) : null);
}
const STATS_BY = [['locations', 'Локации'], ['hosters', 'Хостеры'], ['asns', 'Сети (ASN)'], ['nets', 'Подсети /24'], ['configs', 'Тарифы'],
  ['stealth', 'Уникальные настройки'], ['ports', 'Порты'], ['fps', 'TLS-отпечатки']];
const NEIGH = { subnet: 'режут и соседей — бан подсети или хостера', single: 'соседи открываются — узнали именно ноду',
  mixed: 'часть соседей тоже недоступна', unknown: 'живых соседей мало — вывода нет' };
/* сигналы из истории: пачки блокировок в одной сети за сутки и итоги проверок соседей по /24 */
function banSignals(B) {
  const cl = B.clusters || [], nb = Object.entries(B.neighbors || {});
  if (!cl.length && !nb.length) return null;
  return h('section', { class: 'section' }, h('div', { class: 'section-h' }, h('h2', null, 'Почему блокируют'), h('span', { class: 'faint', style: 'font-size:13px' }, B.days + ' дней')),
    h('div', { class: 'list' },
      nb.length ? h('div', { class: 'li' }, h('div', { class: 'ic-tile' }, ic('radar')), h('div', { class: 'grow', style: 'min-width:0' }, h('div', { class: 't' }, 'Проверка соседей по /24 из России'),
        h('div', { class: 's', style: 'white-space:normal' }, nb.sort((a, b) => b[1] - a[1]).map(([k, n]) => `${n} — ${NEIGH[k] || k}`).join(' · ')))) : null,
      cl.slice(0, 6).map(c => h('div', { class: 'li' }, h('div', { class: 'ic-tile' }, ic('alert')), h('div', { class: 'grow', style: 'min-width:0' },
        h('div', { class: 't', style: 'white-space:normal' }, `${pl(c.n, 'нода', 'ноды', 'нод')} за сутки в ${c.key}`),
        h('div', { class: 's', style: 'white-space:normal' }, new Date(c.start * 1000).toLocaleDateString('ru-RU') + ' · ' + c.nodes.join(', ')))))));
}
/* разбор нейросетью: последний отчёт и вопрос своими словами (только администратор — ключ Gemini его) */
function aiCard(B) {
  const r = UI.aiAns || B.ai, CONF = { high: ['bad', 'уверенно'], medium: ['warn', 'вероятно'], low: ['', 'слабый сигнал'] };
  const ask = e => busyBtn(e, async () => {
    try { UI.aiAns = await api('stats/ask', 'POST', { question: UI.aiQ || '', days: B.days }); render(); }
    catch (x) { toast('Нейросеть не ответила', x.message, 'bad'); }
  });
  if (!isAdmin() && !r) return null;
  return h('section', { class: 'section' }, h('div', { class: 'section-h' }, h('h2', null, 'Разбор нейросетью'),
      r ? h('span', { class: 'faint', style: 'font-size:13px' }, ago(r.ts * 1000) + ' · по ' + pl(r.blocks, 'блокировке', 'блокировкам', 'блокировкам')) : null),
    h('div', { class: 'sheet', style: 'padding:16px;display:grid;gap:12px' },
      r ? [h('div', null, r.summary),
        r.findings.length ? h('div', { class: 'list' }, r.findings.map(f => h('div', { class: 'li' }, h('div', { class: 'grow', style: 'min-width:0' },
          h('div', { class: 't', style: 'white-space:normal' }, f.factor), h('div', { class: 's', style: 'white-space:normal' }, f.evidence)),
          h('span', { class: 'st ' + CONF[f.confidence][0], style: 'flex:none' }, CONF[f.confidence][1])))) : null,
        r.actions.length ? h('div', null, h('b', null, 'Что сделать'), h('ul', { style: 'margin:6px 0 0;padding-left:20px' }, r.actions.map(a => h('li', null, a)))) : null,
        r.need_data.length ? h('div', { class: 'muted' }, 'Для уверенного вывода не хватает: ' + r.need_data.join('; ')) : null]
        : h('div', { class: 'muted' }, 'Нейросеть посмотрит частоту блокировок по хостерам, подсетям, настройкам и нагрузке, пачки банов и проверки соседей — и скажет, что сильнее всего связано с блокировками. Раз в неделю бот делает такой разбор сам и присылает итог.'),
      isAdmin() ? (B.ai_ready ? [h('textarea', { class: 'inp', rows: 2, placeholder: 'Свой вопрос (необязательно): например, стоит ли уходить от этого хостера?', value: UI.aiQ || '',
          oninput: e => { UI.aiQ = e.target.value; } }),
        h('div', { class: 'chips' }, btn(r ? 'Разобрать заново' : 'Разобрать', ask, 's primary', 'refresh'))]
        : h('div', { class: 'muted' }, 'Нужен ключ Gemini: «Настройки» → «Подключения».')) : null));
}
/* страница ноды: сеть, хостер и проверка соседей по /24 */
function nodeNet(n, d) {
  const x = d && d.net;
  if (!x || !d.ip) return null;
  const saveHoster = async v => {
    try { const r = await api(`profiles/${n.pid}/nodes/${n.uuid}/hoster`, 'POST', { hoster: v }); x.hoster = r.hoster; toast('Сохранено', r.hoster ? 'Хостер: ' + r.hoster : 'Хостер — по владельцу сети'); }
    catch (e) { toast('Не сохранилось', e.message, 'bad'); }
  };
  const probe = e => busyBtn(e, async () => {
    try { x.neigh = await api(`profiles/${n.pid}/nodes/${n.uuid}/neighbors`, 'POST'); render(); }
    catch (er) { toast('Проверка не удалась', er.message, 'bad'); }
  });
  const nb = x.neigh;
  return [
    kv('Сеть', h('span', { style: 'display:grid;gap:2px;min-width:0' }, h('span', null, x.asn || 'неизвестно', h('span', { class: 'faint' }, ' · ' + (x.net24 || x.prefix))),
      x.holder ? h('span', { class: 'faint', style: 'font-size:13px;overflow:hidden;text-overflow:ellipsis' }, x.holder) : null)),
    kv('Хостер', h('input', { class: 'inp', style: 'width:100%;max-width:220px', placeholder: x.holder ? x.holder.split(' ')[0] : 'например, Aeza', value: x.hoster || '', 'aria-label': 'Хостер',
      onchange: e => saveHoster(e.target.value.trim()) })),
    kv('Соседи', h('span', { style: 'display:grid;gap:8px;justify-items:start;min-width:0' },
      h('span', { style: 'white-space:normal' }, nb ? `${NEIGH[nb.kind] || nb.kind}` + (nb.checked ? ` (недоступны ${nb.blocked} из ${nb.checked})` : '') + ' · ' + ago(nb.ts * 1000)
        : h('span', { class: 'faint' }, 'по /24 не проверялись')),
      btn('Проверить соседей', probe, 's', 'radar'))),
  ];
}

/* ── раздел «Хосты»: какие хосты панели проверять из России отдельно (по умолчанию — без нод, например обход белых
   списков) и как они проходят. Хосты нод видны и на карточках нод; их и балансировщики можно включить вручную. ── */
const nodesTabs = () => h('div', { style: 'margin-bottom:12px' }, seg([{ v: 'nodes', l: 'Ноды' }, { v: 'hosts', l: 'Хосты' }], UI.nodesTab || 'nodes',
  v => { UI.nodesTab = v; render(); }, 'Раздел'));
const HKIND = { loose: 'без ноды', node: 'вход ноды', balancer: 'балансировщик', disabled: 'выключен в панели', '': '' };
function loadHosts(pid, force) {
  D.hw = D.hw || {};
  const x = D.hw[pid];
  if (!force && x && (x.loading || Date.now() - x.at < 60e3)) return;
  D.hw[pid] = { ...(x || {}), loading: true, at: Date.now() };
  api(`profiles/${pid}/hosts/watch`).then(r => { D.hw[pid] = { ...r, at: Date.now() }; }, e => { D.hw[pid] = { err: e.message, at: Date.now() }; }).then(() => render(true));
}
function hostState(x) {
  const m = x.mon;
  if (!m || !m.data) return { cls: '', t: m ? 'чекеры его не видят' : 'ещё не проверялся' };
  const ms = Object.values(m.by || {}).map(v => v[2]).filter(Boolean);
  const best = ms.length ? ` · ${Math.min(...ms)} мс` : '';
  if (m.status === 'down' || m.fail * 2 > m.data) return { cls: 'bad', t: `не проходит у ${m.fail} из ${m.data} чекеров` };
  if (m.fail) return { cls: 'warn', t: `не проходит у ${m.fail} из ${m.data}${best}` };
  return { cls: 'ok', t: `проходит у ${m.data} из ${m.data}${best}` };
}
function vHosts() {
  const gs = groups(), q = (UI.hq || '').toLowerCase();
  gs.forEach(p => loadHosts(p.id));
  const setWatch = async (p, x, on, el) => {
    el.disabled = true;
    try { await api(`profiles/${p.id}/hosts/watch`, 'POST', { uuid: x.uuid, on }); x.watched = on; if (!on) x.mon = null;
      toast(on ? 'Хост проверяется' : 'Хост больше не проверяется', on ? 'Первые результаты — после следующего опроса чекеров (до 5 минут)' : ''); render(); }
    catch (e) { toast('Не сохранилось', e.message, 'bad'); el.checked = !on; }  // вернуть переключатель как было
    el.disabled = false;
  };
  const row = (p, x) => {
    const s = x.watched ? hostState(x) : null;
    return h('div', { class: 'li' },
      h('div', { class: 'grow', style: 'min-width:0' }, h('div', { class: 't', style: 'white-space:normal' }, x.remark || x.address),
        h('div', { class: 's', style: 'white-space:normal' }, [x.address + (x.port ? ':' + x.port : ''), HKIND[x.kind], x.hidden ? 'скрытый' : '',
          s && x.mon && x.mon.checked_at ? 'проверен ' + ago(x.mon.checked_at * 1000) : ''].filter(Boolean).join(' · ')),
        s ? h('div', { class: 'ru ' + s.cls, style: 'margin-top:4px;white-space:normal;' + (s.cls === 'warn' ? 'color:var(--warn)' : s.cls ? '' : 'color:var(--ink-3)') }, s.t) : null),
      x.kind === 'disabled' ? null : toggle(x.watched, (on, el) => setWatch(p, x, on, el), 'Проверять ' + (x.remark || x.address)));
  };
  return [
    pageH('Ноды', 'Хосты панели: какие проверять из России отдельно'),
    nodesTabs(),
    h('div', { class: 'toolbar' }, h('label', { class: 'field-s' }, ic('search'), h('span', { class: 'sr' }, 'Поиск'),
      h('input', { 'data-k': 'hq', placeholder: 'Название или адрес хоста', value: UI.hq || '', oninput: e => { UI.hq = e.target.value; render(); } }))),
    h('div', { class: 'muted', style: 'margin-bottom:16px' }, 'Включённые хосты чекеры проверяют отдельно, и бот сообщит, если хост перестанет проходить. По умолчанию это хосты без нод (например, обход белых списков). Входы нод видны и на карточках нод; балансировщики и их можно включить здесь.'),
    gs.map(p => {
      const x = (D.hw || {})[p.id];
      if (!x || (!x.hosts && !x.err)) return h('section', { class: 'section' }, h('div', { class: 'sk', style: 'height:120px' }));
      if (x.err) return h('div', { class: 'notice' }, h('span', { class: 'i' }, ic('alert')), h('div', { class: 'grow' }, h('b', null, p.name), x.err), btn('Повторить', () => loadHosts(p.id, true), 's'));
      const hs = x.hosts.filter(y => !q || (y.remark + ' ' + y.address).toLowerCase().includes(q));
      const on = hs.filter(y => y.watched), off = hs.filter(y => !y.watched);
      return h('section', { class: 'section' },
        h('div', { class: 'section-h' }, h('h2', null, p.name), h('span', { class: 'faint', style: 'font-size:13px' }, `${on.length} из ${x.hosts.length} проверяются`)),
        !x.enabled ? h('div', { class: 'notice', style: 'margin-bottom:10px' }, h('span', { class: 'i' }, ic('radar')), h('div', { class: 'grow' }, 'У группы не включён мониторинг — без подписки чекеры хосты не проверят.')) : null,
        x.enabled && !x.known ? h('div', { class: 'muted', style: 'margin-bottom:10px' }, 'Бот разберёт, какие хосты без нод, при следующем опросе чекеров — до 5 минут.') : null,
        on.length ? h('div', { class: 'list', style: 'margin-bottom:10px' }, on.map(y => row(p, y))) : h('div', { class: 'muted', style: 'margin-bottom:10px' }, 'Отдельно не проверяется ни один хост.'),
        off.length ? h('details', { class: 'more-opts', open: q ? true : null }, h('summary', null, `Не проверяются отдельно — ${off.length}`),
          h('div', { class: 'list', style: 'margin-top:10px' }, off.map(y => row(p, y)))) : null);
    }),
  ];
}
