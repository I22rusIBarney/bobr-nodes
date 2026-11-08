'use strict';
/* Карта проверок из России на странице ноды: те же данные мониторинга (чекеры, итог по ноде), та же карта округов
   (/static/ru-map.json), что в классическом интерфейсе — в новом оформлении, с подробностями по округу. */

const RU_ORDER = ['c', 'nw', 'v', 's', 'nc', 'u', 'sib', 'fe'];
const RU_SHORT = { c: 'Центр', nw: 'Северо-Запад', v: 'Поволжье', s: 'Юг', nc: 'Кавказ', u: 'Урал', sib: 'Сибирь', fe: 'Дальний Восток' };
let RU_GEO = null;
const ruGeo = () => RU_GEO || (RU_GEO = fetch('/static/ru-map.json').then(r => (r.ok ? r.json() : null)).catch(() => null));

/* итог одного чекера по ноде: by[id] = [прошло, всего, задержка мс] */
function ckVerdict(n, c) {
  const r = ((n.mon && n.mon.by) || {})[String(c.id)];
  if (r) return r[0] ? { tone: r[0] < r[1] ? 'warn' : 'ok', text: (r[1] > 1 ? `проходит ${r[0]} из ${r[1]}` : 'проходит'), ms: r[2] || null }
    : { tone: 'bad', text: r[1] > 1 ? `не проходит ни один из ${r[1]}` : 'не проходит' };
  if (!['running', 'idle'].includes(c.status)) return { tone: '', text: 'чекер не запущен' };
  if (c.poll_error) return { tone: '', text: 'чекер не отвечает' };
  return { tone: '', text: 'нет данных — нода не в подписке этого чекера' };
}
function ckPoint(geo, c) {  // точка чекера: город из названия, иначе центр округа
  const nm = (c.name || '').toLowerCase();
  for (const [words, xy] of Object.entries(geo.cities)) if (words.split(' ').some(w => nm.includes(w))) return xy;
  const d = geo.districts[c.region];
  return d ? [d.lx, d.ly] : null;
}

function nodeChecks(n) {
  if (!D.mon) return null;
  const cks = D.mon.checkers || [];
  const every = D.mon.every || 300, at = n.mon && n.mon.checked_at;
  const stale = at && Date.now() / 1000 - at > every * 3;  // три пропущенных опроса — данные уже не текущие
  const per = {}, verdicts = cks.map(c => ({ c, v: ckVerdict(n, c) }));
  verdicts.forEach(({ c, v }) => { if (c.region && v.tone) (per[c.region] = per[c.region] || []).push(v.tone); });
  const cells = {};
  for (const [k, t] of Object.entries(per)) cells[k] = t.includes('bad') && !t.includes('ok') ? 'bad' : t.includes('bad') || t.includes('warn') ? 'warn' : 'ok';
  const sel = UI.mapRegion && UI.mapRegion.node === n.id ? UI.mapRegion.r : null;
  const box = h('div', { class: 'ru-map', role: 'img', 'aria-label': 'Доступность ноды из округов России' }, h('div', { class: 'sk', style: 'aspect-ratio:1000/552' }));
  ruGeo().then(geo => {
    if (!geo) { box.replaceChildren(h('div', { class: 'muted', style: 'padding:12px' }, 'Карта не загрузилась — ниже список чекеров.')); return; }
    const fills = RU_ORDER.map(k => `<path d="${geo.districts[k].d}" data-r="${k}" class="rm-d ${cells[k] || ''}${sel === k ? ' sel' : ''}"><title>${esc(RU_SHORT[k])}</title></path>`).join('');
    const labels = RU_ORDER.map(k => { const d = geo.districts[k]; return `<text x="${d.lx}" y="${d.ly}" class="rm-l ${cells[k] ? 'on' : ''}" text-anchor="middle">${esc(RU_SHORT[k])}</text>`; }).join('');
    const seen = {};
    const dots = verdicts.map(({ c, v }) => { const xy = ckPoint(geo, c); if (!xy) return '';
      const key = xy.join(','), i = seen[key] = (seen[key] || 0) + 1, x = xy[0] + (i - 1) * 26, y = xy[1];
      return `<g class="rm-p ${v.tone}" data-r="${esc(c.region || '')}"><circle cx="${x}" cy="${y}" r="22" class="halo"/><circle cx="${x}" cy="${y}" r="10"/>${v.ms ? `<text x="${x + 18}" y="${y - 16}" class="rm-n">${v.ms} мс</text>` : ''}</g>`; }).join('');
    box.innerHTML = `<svg viewBox="0 0 ${geo.w} ${geo.h}" xmlns="http://www.w3.org/2000/svg">${fills}<path d="${geo.borders}" class="rm-b"/>${labels}${dots}</svg>`;
  });
  box.addEventListener('click', e => { const t = e.target.closest('[data-r]'); if (!t || !t.dataset.r) return; UI.mapRegion = { node: n.id, r: sel === t.dataset.r ? null : t.dataset.r }; render(); });
  const inSel = sel ? verdicts.filter(({ c }) => c.region === sel) : [];
  const shown = sel ? inSel : verdicts;
  const okN = verdicts.filter(x => x.v.tone === 'ok').length, badN = verdicts.filter(x => x.v.tone === 'bad').length, warnN = verdicts.filter(x => x.v.tone === 'warn').length;
  const check = e => busyBtn(e, async () => {
    try { await api('monitor/check', 'POST'); toast('Проверяю из России', 'Новые результаты — через минуту-две'); [30000, 75000].forEach(t => setTimeout(() => api('monitor').then(r => { D.mon = r; render(true); }).catch(() => {}), t)); }
    catch (x) { toast('Не получилось', x.message, 'bad'); }
  });
  return [h('div', { class: 'section-h', style: 'margin-top:4px' }, h('h2', null, 'Доступность из России'),
      h('span', { class: 'faint', style: 'font-size:13px' }, !cks.length ? 'чекеров нет' : at ? 'проверено ' + ago(at * 1000) : 'ещё не проверялась'),
      stale ? h('span', { class: 'st warn' }, 'данные устарели') : null),
    !cks.length ? h('div', { class: 'list', style: 'margin-bottom:22px' }, dangerRow('radar', 'Нет чекеров в России', 'Без них бот не узнает, что ноду заблокировали', btn('Добавить', () => { closeOverlay(); go('settings/checkers'); }, 's primary')))
    : [h('div', { class: 'chart', style: 'margin-bottom:10px;padding:10px 12px' }, box,
        h('div', { class: 'rm-legend' }, h('span', null, h('i', { class: 'ok' }), 'проходит'), h('span', null, h('i', { class: 'warn' }), 'частично'),
          h('span', null, h('i', { class: 'bad' }), 'не проходит'), h('span', null, h('i'), 'нет данных'),
          h('span', { class: 'faint', style: 'margin-left:auto' }, `${okN} ок · ${warnN} частично · ${badN} нет`))),
      sel ? h('div', { class: 'chips', style: 'margin-bottom:8px;align-items:center' }, h('b', null, RU_SHORT[sel] + ':'), inSel.length ? null : h('span', { class: 'muted' }, 'в этом округе нет чекеров'),
        btn('Все округа', () => { UI.mapRegion = null; render(); }, 's ghost')) : null,
      h('div', { class: 'list', style: 'margin-bottom:10px' }, shown.sort((a, b) => RU_ORDER.indexOf(a.c.region) - RU_ORDER.indexOf(b.c.region)).map(({ c, v }) => h('div', { class: 'li' },
        h('span', { class: 'rm-dot ' + v.tone }),
        h('div', { class: 'grow' }, h('div', { class: 't' }, c.name), h('div', { class: 's' }, [(D.mon.regions || {})[c.region] || 'регион не указан', c.polled_at ? 'чекер ответил ' + ago(c.polled_at * 1000) : ''].filter(Boolean).join(' · '))),
        h('span', { class: 'ru ' + (v.tone === 'ok' ? 'ok' : v.tone === 'bad' ? 'bad' : ''), style: v.tone === 'warn' ? 'color:var(--warn)' : v.tone ? '' : 'color:var(--ink-3)' }, v.text + (v.ms ? ` · ${v.ms} мс` : ''))))),
      h('div', { class: 'chips', style: 'margin-bottom:22px' }, btn('Проверить сейчас', check, 's', 'refresh'),
        cks.some(c => !c.region) ? h('span', { class: 'faint', style: 'font-size:13px' }, 'У части чекеров не указан регион — их нет на карте.') : null)]];
}
