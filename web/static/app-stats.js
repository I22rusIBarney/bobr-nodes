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
      h('div', { class: 'seg', role: 'tablist', style: 'margin-left:auto' }, [['locations', 'Локации'], ['configs', 'Установка']].map(([k, l]) =>
        h('button', { type: 'button', role: 'tab', 'aria-selected': String(by === k), onclick: () => { UI.statsBy = k; render(); } }, l)))),
    !B ? h('div', { class: 'sk', style: 'height:160px' })
      : !rows.length ? h('div', { class: 'sheet', style: 'padding:16px' }, h('div', { class: 'muted' }, 'Пока нечего сравнивать: бот начнёт записывать блокировки нод, купленных через него, с этой версии.'))
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
  return h('div', { class: 'grid2' }, blocksCard, costCard);
}
