'use strict';
/* Покупка только с согласия администратора: окно подтверждения новой ноды (замена при починке, докупка при нагрузке).
   Что можно — берём из конфигурации (локация, тариф старой ноды, её сквады); что важно — показываем и даём поменять;
   кнопка покупки активна, только когда выбраны домен, тариф и хотя бы один сквад. */

function buyConfirm({ title, intro, plan, onBuy, note }) {
  const P = plan;
  if (!P.available) return modal({ title, icon: ['alert', 'warn'], body: `Локации ${P.loc_name || ''} сейчас нет в Bobr Cloud — купить ноду там же нельзя.` });
  const sel = { zone: P.zone || (P.zones || [])[0] || '', idx: P.same ?? ((P.offers || [])[0] || {}).idx, squads: new Set(P.squads_default || []) };
  const body = h('div', { class: 'form' }), err = h('div', { class: 'err', role: 'alert' });
  let buyBtn;
  const draw = () => {
    const o = (P.offers || []).find(x => x.idx === sel.idx);
    const ready = sel.zone && o && sel.squads.size > 0 && P.buys_left !== 0;
    if (buyBtn) { buyBtn.disabled = !ready; buyBtn.textContent = o ? `Купить за ${rubK(o.price_kop)}/мес` : 'Выберите тариф'; }
    body.replaceChildren(
      intro ? h('div', null, intro) : null,
      kvRows([['Где', `${P.flag || ''} ${P.loc_name || '—'}`], P.buys_left != null ? ['Покупок сегодня осталось', P.buys_left ? String(P.buys_left) : h('span', { class: 'st bad' }, 'лимит')] : null]),
      adviceNote(locAdvice(P.loc)),
      fld('Домен для имени', (P.zones || []).length ? h('div', { class: 'chips' }, P.zones.map(z => h('button', { type: 'button', class: 'chip', 'aria-pressed': String(sel.zone === z), onclick: () => { sel.zone = z; draw(); } }, z)))
        : h('div', { class: 'err' }, 'Нет доменов Cloudflare — подключите Cloudflare для группы: новой ноде нужно имя.')),
      fld('Тариф', h('div', { class: 'list', style: 'background:var(--sheet-2)' }, (P.offers || []).map(x => h('button', { type: 'button', class: 'li', onclick: () => { sel.idx = x.idx; draw(); } },
        h('span', { class: 'cb-m' + (sel.idx === x.idx ? ' on' : ''), style: 'border-radius:50%' }, sel.idx === x.idx ? ic('check') : null),
        h('div', { class: 'grow' }, h('div', { class: 't' }, x.label), x.idx === P.same ? h('div', { class: 's' }, 'как у старой ноды') : null), h('span', { class: 'num' }, rubK(x.price_kop) + '/мес')))),
        P.same == null ? 'Старая нода куплена не через бота или на своей конфигурации — выберите тариф.' : null),
      fld('Сквады', (P.squads || []).length ? h('div', { class: 'chips' }, P.squads.map(s => h('button', { type: 'button', class: 'chip', 'aria-pressed': String(sel.squads.has(s.uuid)),
          onclick: () => { sel.squads.has(s.uuid) ? sel.squads.delete(s.uuid) : sel.squads.add(s.uuid); draw(); } }, s.name)))
        : h('div', { class: 'err' }, 'В панели нет сквадов — создайте сквад в Remnawave, иначе пользователи не увидят новую ноду.'),
        sel.squads.size ? 'Пользователи этих сквадов увидят новую ноду.' : h('span', { class: 'err' }, 'Выберите хотя бы один сквад — без него нода не будет готова.')),
      o ? h('div', { class: 'notice info', style: 'margin:0' }, h('span', { class: 'i' }, ic('info')), h('div', null, h('b', null, 'Итог'),
        `${P.flag || ''} ${P.loc_name}, ${o.label}, домен ${sel.zone || '—'}, сквады: ${[...sel.squads].map(u => (P.squads.find(s => s.uuid === u) || {}).name).join(', ') || '—'}. `
        + `${rubK(o.price_kop)} в месяц спишется с баланса Bobr Cloud.` + (note ? ' ' + note : ''))) : null,
      err);
  };
  draw();
  modal({ title, icon: ['wallet', 'warn'], body, actions: [{ l: 'Отмена' }, { l: 'Купить', cls: 'primary', ref: b => { buyBtn = b; draw(); }, fn: async (c, b) => {
    b.classList.add('busy'); b.disabled = true; err.textContent = '';
    try { await onBuy({ zone: sel.zone, idx: sel.idx, squads: [...sel.squads] }); c(); }
    catch (x) { err.textContent = x.message; b.classList.remove('busy'); b.disabled = false; }
  } }] });
}

/* замена ноды при починке: «Сразу новая нода» или подтверждение шага, на котором бот остановился */
async function replaceNode(n, confirmStep) {
  let P; try { P = await api('monitor/replace-plan?tok=' + encodeURIComponent(n.mon.tok)); } catch (x) { return toast('Не получилось', x.message, 'bad'); }
  const OLD = { panel: 'Когда новая нода заработает, старую бот уберёт из панели.', delete: 'Когда новая нода заработает, старую бот уберёт из панели и удалит её сервер.', keep: 'Старая нода останется как есть.' };
  buyConfirm({ title: `Новая нода вместо ${n.name}`, plan: P, note: OLD[P.old] || '',
    intro: confirmStep ? 'Бесплатные шаги починки не помогли. Бот готов купить новую ноду в той же локации — проверьте выбор.' : 'Бот купит новую ноду в той же локации и поставит её.',
    onBuy: async ch => { const r = await api('monitor/action', 'POST', { tok: n.mon.tok, action: confirmStep ? 'confirm' : 'replace', ...ch }); toast('Покупаю новую ноду', r.message); refreshSoon(); } });
}
/* платная смена IP, на которой починка остановилась */
async function confirmReip(n) {
  let price = null;
  try { const d = await api(`profiles/${n.pid}/nodes/${n.uuid}`); price = d.bcloud && d.bcloud.price_kop; } catch (x) { /* цена — не главное */ }
  confirmAct({ title: `Сменить IP у ${n.name}?`, icon: ['globe', 'warn'], ok: price != null ? `Сменить за ${rubK(price)}` : 'Сменить IP',
    text: 'Бесплатные шаги починки не помогли. Bobr Cloud выдаст серверу новый адрес, бот обновит DNS и панель и проверит ноду из России.' + (price != null ? ` Спишется ${rubK(price)}.` : ''),
    fn: async () => { const r = await api('monitor/action', 'POST', { tok: n.mon.tok, action: 'confirm' }); toast('Меняю IP', r.message); refreshSoon(); } });
}
/* докупка при нагрузке */
async function scaleNode(a) {
  let P; try { P = await api('scale/plan?tok=' + encodeURIComponent(a.tok)); } catch (x) { return toast('Не получилось', x.message, 'bad'); }
  buyConfirm({ title: `Ещё одна нода рядом с ${a.name}`, plan: P, intro: `${a.name} перегружена — новая нода в той же локации разделит нагрузку.`,
    onBuy: async ch => { const r = await api('scale/confirm', 'POST', { tok: a.tok, ...ch }); if (!r.ok) throw new Error(r.message); toast('Покупаю', r.message); D.scale = null; refreshSoon(); } });
}
