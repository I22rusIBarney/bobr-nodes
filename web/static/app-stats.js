'use strict';
/* Статистика на «Обзоре»: где и как поставленные ноды блокируют реже (по истории блокировок) и сколько тратит бот.
   Советы по локации при покупке берутся отсюда же. Данные меняются медленно — обновляем раз в 10 минут. */

const STATS_TTL = 600e3;
function loadStats(force) {
  const S = D.stats;
  if (!force && S && (S.loading || Date.now() - S.at < STATS_TTL)) return;
  D.stats = { ...(S || {}), loading: true, at: Date.now() };
  Promise.all([api('stats/blocks?days=90').catch(() => null), api('stats/costs?days=30').catch(() => null)])
    .then(([blocks, costs]) => { D.stats = { blocks, costs, at: Date.now() }; render(true); });
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
    h('div', null, h('b', null, `${x.flag || ''} ${x.label}: ноды блокируют часто`),
      `За ${D.stats.blocks.days} дней: ${rate(x)}` + (x.life_days != null ? `, до блокировки нода живёт около ${dec(x.life_days)} дн` : '') +
      `. Для сравнения, ${b.flag || ''} ${b.label}: ${rate(b)}` + (b.life_days != null ? `, около ${dec(b.life_days)} дн` : '') + '.'));
}

function statsSection() {
  loadStats();
  const S = D.stats || {};
  const B = S.blocks, C = S.costs;
  const by = UI.statsBy || 'locations';
  const steps = (D.mon && D.mon.steps) || {};
  const fixName = k => k === 'self' ? 'прошло само' : k === 'replace' ? 'новая нода' : k === 'removed' ? 'нода убрана' : steps[k] || k;
  const rows = B ? B[by].filter(r => r.nodes || r.blocks) : [];
  const best = rows.filter(r => r.rate != null && r.months >= 1).sort((a, b) => a.rate - b.rate)[0];
  const blocksCard = h('section', { class: 'section' },
    h('div', { class: 'section-h', style: 'flex-wrap:wrap;row-gap:8px' }, h('h2', null, 'Где блокируют реже'), h('span', { class: 'faint', style: 'font-size:13px' }, '90 дней'),
      h('select', { class: 'sel', 'aria-label': 'Разрез', style: 'margin-left:auto;max-width:200px', onchange: e => { UI.statsBy = e.target.value; render(); } },
        STATS_BY.map(([k, l]) => h('option', { value: k, selected: by === k || null }, l)))),
    !B ? h('div', { class: 'sk', style: 'height:160px' })
      : !rows.length ? h('div', { class: 'sheet', style: 'padding:16px' }, h('div', { class: 'muted' }, by === 'locations' || by === 'configs'
        ? 'Пока нечего сравнивать: бот начнёт записывать блокировки нод, купленных через него, с этой версии.'
        : 'Пока нет данных в этом разрезе: сети нод бот узнаёт в фоне (обновите через минуту), настройки и соседей записывает с новых блокировок.'))
      : h('div', { class: 'list' }, rows.slice(0, 8).map(r => {
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
        h('b', { class: 'num' }, rubK(C.total_kop)))));
  return [h('div', { class: 'grid2' }, blocksCard, costCard), B ? banSignals(B) : null, B ? aiCard(B) : null];
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
    kv('Сеть', h('span', { style: 'white-space:normal' }, x.asn ? `${x.asn} ${x.holder}` : 'неизвестно', h('span', { class: 'faint' }, ' · ' + (x.prefix || x.net24)))),
    kv('Хостер', h('input', { class: 'inp', style: 'max-width:220px', placeholder: x.holder || 'например, Aeza', value: x.hoster || '', 'aria-label': 'Хостер',
      onchange: e => saveHoster(e.target.value.trim()) })),
    kv('Соседи по /24', h('span', { style: 'white-space:normal' }, nb ? `${NEIGH[nb.kind] || nb.kind}` + (nb.checked ? ` (недоступны ${nb.blocked} из ${nb.checked})` : '') + ' · ' + ago(nb.ts * 1000)
      : h('span', { class: 'faint' }, 'не проверялись')), btn('Проверить', probe, 's ghost', 'radar')),
  ];
}
