'use strict';
/* Раздел «Страж»: подозрительные подписчики по группам, сводка по дням и карточка подписчика — всё, чтобы решить,
   жулик клиент или нет: признаки, устройства, IP подключений с сетями, запросы подписки, трафик, связанные аккаунты. */

const GU = { list: null, grp: {}, card: {}, q: {}, found: {}, lvl: {} };
const gbs = b => b == null ? '—' : (b / 2 ** 30).toFixed(b >= 10 * 2 ** 30 ? 0 : 1).replace('.', ',') + ' ГБ';
const tsAgo = t => t ? ago(t * 1000) : '—';
const STATUS_RU = { ACTIVE: 'активен', DISABLED: 'отключён', LIMITED: 'лимит трафика', EXPIRED: 'истёк' };
const lvlChip = l => h('span', { class: 'st ' + (l === 'crit' ? 'bad' : l === 'warn' ? 'warn' : 'ok') }, l === 'crit' ? 'критично' : l === 'warn' ? 'внимание' : 'чисто');
const netTag = n => n ? h('span', { class: 's', style: 'color:' + (n.own ? 'var(--muted)' : n.hosting ? 'var(--air-red)' : 'inherit') },
  n.own ? 'своя нода или вход' : (n.holder || 'сеть неизвестна') + (n.hosting ? ' · хостинг' : n.relay ? ' · WARP или iCloud Private Relay' : '')) : null;

/* «Проверить сейчас»: проверка идёт в фоне (большая панель — минуты), раздел раз в 3 секунды спрашивает, как дела */
const GW = {};
function guardWatch(pid, apply) {
  if (GW[pid]) return;
  GW[pid] = setTimeout(async () => {
    let r = null;
    try { r = await api(`profiles/${pid}/guard`); } catch (x) { /* сеть моргнула — спросим ещё раз */ }
    delete GW[pid];
    if (r) {
      apply(r); GU.grp[pid] = { a: r, at: Date.now() };
      if (!r.running) {
        GU.list = null; Object.keys(GU.card).filter(k => k.startsWith(pid + '/')).forEach(k => delete GU.card[k]);
        const l = r.last || {};
        l.error ? toast('Проверка не прошла', l.error, 'bad') : toast('Проверено', `${(l.found || []).length} подозрительных · подписчиков ${l.users ?? '—'}`);
      }
    }
    render(true);
    if (!r || r.running) guardWatch(pid, apply);
  }, 3000);
}
async function guardStart(pid, apply) {
  try { const r = await api(`profiles/${pid}/guard/check`, 'POST', {}); apply(r); GU.grp[pid] = { a: r, at: Date.now() }; toast('Проверка началась', 'Большая панель проверяется несколько минут — можно уйти со страницы.'); }
  catch (x) { return toast('Не получилось', x.message, 'bad'); }
  render(); guardWatch(pid, apply);
}
const guardRunning = run => h('span', { class: 'chips', style: 'align-items:center;gap:8px' }, h('span', { class: 'spin', 'aria-hidden': 'true' }),
  h('span', { class: 'faint' }, `${run.stage || 'Проверяю'} · ${since(run.started * 1000)}`));

function guardLoad(force) {
  if (!force && GU.list && (GU.list.loading || Date.now() - GU.list.at < 60e3)) return;
  GU.list = { ...(GU.list || {}), loading: true, at: Date.now() };
  api('guard').then(r => { GU.list = { ...r, at: Date.now() }; }).catch(e => { GU.list = { err: e.message, at: Date.now() }; }).then(() => render(true));
}
function guardGroup(pid, force) {
  const st = GU.grp[pid];
  if (!force && st && (st.loading || Date.now() - st.at < 60e3)) return st;
  GU.grp[pid] = { ...(st || {}), loading: true, at: Date.now() };
  api(`profiles/${pid}/guard`).then(r => { GU.grp[pid] = { a: r, at: Date.now() }; }).catch(e => { GU.grp[pid] = { err: e.message, at: Date.now() }; }).then(() => render(true));
  return GU.grp[pid];
}
function guardAct(pid, f, what, after) {
  const T = { revoke: ['Перевыпустить подписку?', 'Старая ссылка и ключи перестанут работать — пользователю нужно будет заново добавить подписку из бота продаж.', 'Перевыпустить', 'primary'],
    disable: ['Отключить пользователя?', 'Пользователь будет отключён в панели Remnawave. Включить обратно можно в самой панели.', 'Отключить', 'danger solid'],
    ok: ['Всё в порядке?', 'Бот больше не будет писать об этом пользователе — только если появится что-то критичное.', 'Да, всё в порядке', 'primary'] }[what];
  confirmAct({ title: T[0], text: T[1], ok: T[2], cls: T[3], icon: ['shield', what === 'disable' ? 'bad' : what === 'ok' ? 'run' : 'warn'],
    fn: async () => { const r = await api(`profiles/${pid}/guard/users/${encodeURIComponent(f.key)}/${what}`, 'POST', {}); toast(r.msg); if (what === 'ok') f.ok = true; after && after(); render(); } });
}
const actBtns = (pid, f, after) => h('div', { class: 'chips' },
  btn('Перевыпустить', () => guardAct(pid, f, 'revoke', after), 's', 'refresh'), btn('Отключить', () => guardAct(pid, f, 'disable', after), 's danger'),
  f.ok ? null : btn('Всё в порядке', () => guardAct(pid, f, 'ok', after), 's'));

/* столбики по дням: подозрительные (критично + внимание) */
function dailyBars(daily) {
  const days = Object.keys(daily || {}).sort().slice(-14);
  if (days.length < 2) return null;
  const max = Math.max(1, ...days.map(d => (daily[d].crit || 0) + (daily[d].warn || 0)));
  return h('div', { style: 'display:flex;align-items:flex-end;gap:3px;height:54px;margin-top:10px', 'aria-label': 'Подозрительные по дням' },
    days.map(d => { const x = daily[d], c = x.crit || 0, w = x.warn || 0;
      return h('div', { title: `${d}: критично ${c}, внимание ${w}` + (x.revoke ? `, перевыпущено ${x.revoke}` : '') + (x.disable ? `, отключено ${x.disable}` : ''),
        style: 'flex:1;display:flex;flex-direction:column-reverse;min-width:6px;height:100%' },
        h('div', { style: `height:${c / max * 100}%;background:var(--air-red);border-radius:2px 2px 0 0` }),
        h('div', { style: `height:${w / max * 100}%;background:var(--warn);opacity:.8` })); }));
}

function vGuard(arg) {
  const [pidS, key] = (arg || '').split('/');
  if (pidS && key) return guardCard(+pidS, key);
  if (pidS) return guardGroupView(+pidS);
  guardLoad();
  const L = GU.list;
  const head = pageH('Страж подписок', 'Кто раздаёт или перепродаёт подписку: серверы вместо приложений, IP подключений больше, чем устройств, хостинги, обход бана, всплески трафика.');
  if (!L || (L.loading && !L.groups)) return [head, h('div', { class: 'sk', style: 'height:240px' })];
  if (L.err) return [head, empty('alert', 'Не загрузилось', L.err, btn('Повторить', () => guardLoad(true), 'primary'))];
  if (!L.groups.length) return [head, empty('layers', 'Групп нет', 'Страж смотрит подписчиков в панелях ваших групп.')];
  return [head, h('div', { class: 'list' }, L.groups.map(g => h('button', { type: 'button', class: 'li', onclick: () => go('guard/' + g.pid), style: 'align-items:flex-start' },
    h('div', { class: 'ic-tile' }, ic('shield')),
    h('div', { class: 'grow', style: 'min-width:0' },
      h('div', { class: 't' }, g.name),
      h('div', { class: 's' }, g.running ? `проверяю: ${g.running.stage || ''}` : !g.on ? 'выключен — включите в группе' : g.error ? 'панель не ответила: ' + g.error
        : g.at ? `проверено ${tsAgo(g.at)} · подписчиков ${g.users ?? '—'} · устройств ${g.devices ?? '—'}` + (g.ips != null ? ` · IP сейчас ${g.ips}` : '') : 'первая проверка — в течение часа'),
      g.on ? dailyBars(g.daily) : null),
    g.crit ? h('span', { class: 'st bad' }, `критично ${g.crit}`) : null, g.warn ? h('span', { class: 'st warn' }, `внимание ${g.warn}`) : null,
    g.on && g.at && !g.crit && !g.warn ? h('span', { class: 'st ok' }, 'чисто') : null,
    h('span', { class: 'chev' }, ic('chevR')))))];
}

/* итог дня от нейросети: последний разбор — сверху, раньшие — свёрнуто */
const ACT_ICON = { disable: '🔴', revoke: '🟠', message: '🟠', watch: '🟡', none: '⚪' };
function nightBlock(pid, a) {
  const nights = a.nights || [];
  if (!a.admin && !nights.length) return null;
  const run = e => busyBtn(e, async () => {
    try { const r = await api(`profiles/${pid}/guard/night`, 'POST', {}); GU.grp[pid] = { a: r, at: Date.now() }; toast('Нейросеть разбирает день', 'Итог придёт в Telegram и появится здесь примерно через минуту.'); setTimeout(() => guardGroup(pid, true), 60e3); }
    catch (x) { toast('Не получилось', x.message, 'bad'); } render(); });
  const n = nights[0], day = d => d.split('-').slice(1).reverse().join('.');
  const users = n ? n.users : [];
  return h('section', { class: 'section', style: 'margin-bottom:18px' },
    h('div', { class: 'section-h' }, h('h2', null, 'Итог дня от нейросети'), n ? h('span', { class: 'faint' }, `за ${day(n.day)} · ${ago(n.at * 1000)}`) : null),
    h('div', { class: 'sheet', style: 'padding:14px 16px' },
      !n ? h('p', { style: 'margin:0 0 10px' }, a.night ? 'Первый итог придёт в ближайшую полночь по времени группы.' : 'Включите «Итог дня от нейросети» в настройках стража группы — или сделайте разбор сейчас.') : null,
      n && n.summary ? h('p', { style: 'margin:0 0 10px' }, n.summary) : null,
      n && !users.length ? h('p', { class: 'faint', style: 'margin:0 0 10px' }, 'За сутки подозрительных не было.') : null,
      users.length ? h('div', { class: 'list', style: 'margin-bottom:10px' }, users.map(u => h('button', { type: 'button', class: 'li', style: 'align-items:flex-start', onclick: () => go(`guard/${pid}/${encodeURIComponent(u.key)}`) },
        h('span', { 'aria-hidden': 'true' }, ACT_ICON[u.action] || '⚪'),
        h('div', { class: 'grow', style: 'min-width:0' },
          h('div', { class: 't' }, u.name + (u.handle ? ` · ${u.handle}` : '')),
          h('div', { class: 's' }, `${(VERDICT[u.verdict] || VERDICT.unclear)[0]} · ${u.confidence}% · совет: ${AI_ACT[u.action] || u.action}`),
          h('div', { class: 's' }, u.why)),
        h('span', { class: 'chev' }, ic('chevR'))))) : null,
      a.admin ? h('div', { class: 'chips' }, a.night_running ? h('span', { class: 'faint' }, 'Нейросеть разбирает…') : btn(n ? 'Разобрать сейчас ещё раз' : 'Сделать итог сейчас', run, 's', 'chart')) : null,
      h('div', { class: 'hint', style: 'margin-top:6px' }, 'Нейросеть не видит имён, TG ID, HWID и полных IP и может ошибаться: решение за вами.')));
}

function guardGroupView(pid) {
  const g = groups().find(p => p.id === pid);
  const back = h('button', { type: 'button', class: 'back', onclick: () => go('guard') }, ic('chevL'), 'Страж');
  if (!g) return [back, empty('layers', 'Группа не найдена', 'Возможно, её удалили или у вас нет к ней доступа.')];
  const st = guardGroup(pid);
  guardLoad();  // сводка по дням — из общего списка
  if (!st.a) return [back, pageH(g.name), st.err ? empty('alert', 'Не загрузилось', st.err, btn('Повторить', () => guardGroup(pid, true), 'primary')) : h('div', { class: 'sk', style: 'height:300px' })];
  const a = st.a, last = a.last || {}, lvl = GU.lvl[pid] || 'all', q = GU.q[pid] || '';
  const check = () => guardStart(pid, r => { st.a = r; });
  if (a.running) guardWatch(pid, r => { st.a = r; });
  const found = (last.found || []).filter(f => lvl === 'all' || f.level === lvl);
  const search = async v => { GU.q[pid] = v; if (v.trim().length < 2) { GU.found[pid] = null; return render(); }
    try { GU.found[pid] = (await api(`profiles/${pid}/guard/users?q=${encodeURIComponent(v.trim())}`)).users; } catch (x) { GU.found[pid] = []; } render(); };
  const stat = (k, l, cls) => h('div', { class: 'stat', style: 'cursor:default' }, h('span', { class: 'k ' + (cls || '') }, k), h('span', { class: 'l' }, l));
  const crit = (last.found || []).filter(f => f.level === 'crit').length, warn = (last.found || []).filter(f => f.level === 'warn').length;
  return [back,
    pageH(g.name, last.at ? `Последняя проверка ${tsAgo(last.at)}` : 'Ещё не проверяли', [a.running ? guardRunning(a.running) : btn('Проверить сейчас', check, '', 'radar'), btn('Настройки', () => { UI.tabs['g' + pid] = 'guard'; go('group/' + pid); }, '', 'sliders')]),
    !a.on ? h('div', { class: 'notice', style: 'background:var(--warn-soft);margin-bottom:14px' }, h('span', { class: 'i', style: 'color:var(--warn)' }, ic('alert')),
      h('div', null, h('b', null, 'Страж в этой группе выключен'), 'Сам по расписанию не проверяет и не пишет. Включите в настройках группы — или проверьте разово кнопкой выше.')) : null,
    last.error ? h('div', { class: 'err', role: 'alert', style: 'margin-bottom:10px' }, 'Панель не ответила: ' + last.error) : null,
    last.conn_err ? h('div', { class: 'hint', style: 'margin-bottom:10px' }, 'IP подключений ноды не отдали (нужна панель 2.7+): ' + last.conn_err) : null,
    last.dev_err ? h('div', { class: 'hint', style: 'margin-bottom:10px' }, 'Список устройств панель не отдала: ' + last.dev_err) : null,
    h('div', { class: 'sheet', style: 'padding:14px 16px;margin-bottom:18px' },
      h('div', { class: 'stats' }, stat(last.users ?? '—', 'подписчиков'), stat(last.devices ?? '—', 'устройств'), stat(last.online ?? '—', 'подключены сейчас'),
        stat(last.ips ?? '—', 'IP подключений'), stat(crit, 'критично', crit ? 'bad' : ''), stat(warn, 'внимание', warn ? 'warn' : '')),
      dailyBars((GU.list && (GU.list.groups || []).find(x => x.pid === pid) || {}).daily)),
    nightBlock(pid, a),
    h('div', { class: 'chips', style: 'margin-bottom:10px;align-items:center' },
      seg([{ v: 'all', l: 'Все' }, { v: 'crit', l: 'Критично' }, { v: 'warn', l: 'Внимание' }], lvl, v => { GU.lvl[pid] = v; render(); }, 'Уровень'),
      h('input', { class: 'inp', type: 'search', placeholder: 'Найти подписчика: имя или TG ID', value: q, style: 'flex:1;min-width:180px', oninput: e => search(e.target.value) })),
    GU.found[pid] ? h('div', { class: 'list', style: 'margin-bottom:16px' }, GU.found[pid].length ? GU.found[pid].map(u => h('button', { type: 'button', class: 'li', onclick: () => go(`guard/${pid}/${encodeURIComponent(u.key)}`) },
      h('div', { class: 'grow' }, h('div', { class: 't' }, u.name), h('div', { class: 's' }, (STATUS_RU[u.status] || u.status || '') + (u.handle ? ` · ${u.handle}` : '') + (u.tg ? ` · TG ${u.tg}` : '') + (u.checked === false ? ' · не проверяется: системная или не по шаблону' : ''))), h('span', { class: 'chev' }, ic('chevR'))))
      : h('div', { class: 'li' }, h('span', { class: 'faint' }, 'Никого не нашлось в последней проверке'))) : null,
    found.length ? h('div', { class: 'list' }, found.map(f => h('button', { type: 'button', class: 'li', style: 'align-items:flex-start', onclick: () => go(`guard/${pid}/${encodeURIComponent(f.key)}`) },
      lvlChip(f.level),
      h('div', { class: 'grow', style: 'min-width:0' },
        h('div', { class: 't' }, f.name + (f.ok ? ' · «всё в порядке»' : '')),
        h('div', { class: 's' }, `баллов ${f.score} · устройств ${f.devices}` + (f.limit ? ` из ${f.limit}` : '') + (f.ips ? ` · IP сейчас ${f.ips}` : '') + (f.handle ? ` · ${f.handle}` : '')),
        f.reasons.slice(0, 3).map(r => h('div', { class: 's' }, '• ' + r))),
      h('span', { class: 'chev' }, ic('chevR')))))
      : last.at ? empty('shield', lvl === 'all' ? 'Подозрительных нет' : 'Таких нет', 'Бот напишет, если кто-то начнёт раздавать или перепродавать подписку.') : null];
}

const VERDICT = { honest: ['честный клиент', 'ok'], family: ['семья или друзья', 'ok'], sharing: ['раздаёт подписку', 'warn'], reselling: ['перепродаёт подписку', 'bad'],
  relay: ['сервер-посредник', 'bad'], evasion: ['обход бана', 'bad'], unclear: ['неясно', ''] };
const AI_ACT = { none: 'ничего не делать', watch: 'понаблюдать', message: 'написать клиенту', revoke: 'перевыпустить подписку', disable: 'отключить' };
function aiBlock(pid, key, d) {
  const ask = (e, force) => busyBtn(e, async () => {
    try { d.ai = await api(`profiles/${pid}/guard/users/${encodeURIComponent(key)}/ai`, 'POST', { force: !!force }); } catch (x) { toast('Нейросеть не ответила', x.message, 'bad'); }
    render(); });
  const a = d.ai;
  if (!a) return h('div', { class: 'sheet', style: 'padding:14px 16px' },
    h('p', { style: 'margin:0 0 10px' }, 'Gemini посмотрит на всё, что есть в карточке, и скажет, похоже ли это на семью, раздачу, перепродажу или посредника. Имя, TG ID, HWID и полные IP ему не отправляются.'),
    btn('Разобрать', e => ask(e), 'primary', 'chart'));
  const V = VERDICT[a.verdict] || VERDICT.unclear, list = (t, xs) => xs && xs.length ? [h('div', { class: 's', style: 'margin-top:10px' }, h('b', null, t)), xs.map(x => h('div', { class: 's' }, '• ' + x))] : null;
  return h('div', { class: 'sheet', style: 'padding:14px 16px' },
    h('div', { class: 'chips', style: 'align-items:center' }, h('span', { class: 'st ' + V[1] }, V[0]), h('span', { class: 'faint' }, `уверенность ${a.confidence}% · совет: ${AI_ACT[a.action] || a.action}` + (a.night ? ' · из итога дня' : ''))),
    a.summary ? h('p', { style: 'margin:10px 0 0' }, a.summary) : null,
    list('На чём основан вывод', a.evidence), list('В пользу клиента', a.doubts), list('Проверьте руками', a.check),
    h('div', { style: 'margin-top:12px' }, btn('Разобрать заново', e => ask(e, true), 's', 'refresh')),
    h('div', { class: 'hint', style: 'margin-top:6px' }, 'Нейросеть может ошибаться: решение — за вами.'));
}
function guardCard(pid, key) {
  const back = h('button', { type: 'button', class: 'back', onclick: () => go('guard/' + pid) }, ic('chevL'), (groups().find(p => p.id === pid) || {}).name || 'Группа');
  const ck = pid + '/' + key, st = GU.card[ck];
  if (!st || (!st.loading && Date.now() - st.at > 60e3)) {
    GU.card[ck] = { ...(st || {}), loading: true, at: Date.now() };
    api(`profiles/${pid}/guard/users/${encodeURIComponent(key)}`).then(r => { GU.card[ck] = { d: r, at: Date.now() }; }).catch(e => { GU.card[ck] = { err: e.message, at: Date.now() }; }).then(() => render(true));
  }
  const S = GU.card[ck];
  if (!S.d) return [back, S.err ? empty('alert', 'Не загрузилось', S.err) : h('div', { class: 'sk', style: 'height:420px' })];
  const d = S.d, refresh = () => { delete GU.card[ck]; GU.grp[pid] = null; GU.list = null; };
  const sec = (title, sub, ...kids) => h('section', { class: 'section', style: 'margin-top:18px' }, h('div', { class: 'section-h' }, h('h2', null, title), sub ? h('span', { class: 'faint' }, sub) : null), ...kids);
  const row = (main, sub, right, extra) => h('div', { class: 'li', style: 'align-items:flex-start' + (extra || '') },
    h('div', { class: 'grow', style: 'min-width:0' }, h('div', { class: 't', style: 'overflow-wrap:anywhere' }, main), sub), right || null);
  const maxT = Math.max(1, ...d.traffic.map(t => t.bytes));
  return [back,
    pageH(d.name, [STATUS_RU[d.status] || d.status, d.handle || '', d.tg ? `TG ${d.tg}` : '', `проверено ${tsAgo(d.at)}`].filter(Boolean).join(' · '), [lvlChip(d.level)]),
    h('div', { class: 'sheet', style: 'padding:14px 16px' },
      h('div', { class: 'stats' },
        ...[[d.score, 'баллов риска', d.level === 'crit' ? 'bad' : d.level === 'warn' ? 'warn' : ''], [d.devices + (d.limit ? ` / ${d.limit}` : ''), 'устройств / лимит'],
          [d.nets ?? '—', 'сетей одновременно', d.limit && d.nets > d.limit ? 'warn' : ''], [d.ips, 'IP одновременно'],
          ...(d.nets_checks ? [[d.nets_checks, 'проверок за сутки сверх лимита', d.nets_checks >= 3 ? 'bad' : 'warn']] : []), [gbs(d.yesterday), 'трафик за вчера'], [gbs(d.usual), 'обычно в сутки'], [gbs(d.used), 'всего'],
          [d.week_devices ?? '—', 'устройств за 7 дней', d.limit && d.week_devices >= d.limit + 3 ? 'warn' : ''],
          [`${d.sub_nets ?? '—'} / ${(d.sub_ips || []).length}`, 'сетей / IP ссылки за сутки'],
          [d.active_hours ?? '—', 'часов с трафиком из 24', d.active_hours >= 22 ? 'warn' : ''],
          ...(d.torrents ? [[d.torrents, 'торренты за неделю', 'warn']] : [])]
          .map(([k, l, c]) => h('div', { class: 'stat', style: 'cursor:default' }, h('span', { class: 'k ' + (c || '') }, String(k)), h('span', { class: 'l' }, l)))),
      h('div', { style: 'margin-top:12px' }, actBtns(pid, d, refresh)),
      d.ok ? h('div', { class: 'hint', style: 'margin-top:8px' }, 'Отмечен «всё в порядке»: бот напишет только о новом критичном признаке.') : null),
    sec('Почему подозрительно', d.reasons.length ? `${d.reasons.length}` : '',
      d.reasons.length ? h('div', { class: 'list' }, d.reasons.map(r => row(r.text, null, h('span', { class: 'st ' + (r.crit ? 'bad' : r.w >= 50 ? 'warn' : '') }, r.crit ? 'критично' : `+${r.w}`))))
        : h('div', { class: 'sheet', style: 'padding:14px 16px' }, 'Признаков нет: устройства обычные, IP подключений не больше устройств, трафик в норме.')),
    isAdmin() ? sec('Разбор нейросетью', d.ai ? `${ago(d.ai.at * 1000)}` : '', aiBlock(pid, key, d)) : null,
    sec('Устройства', `${d.device_list.length}`,
      d.device_list.length ? h('div', { class: 'list' }, d.device_list.map(x => row(
        [x.platform, x.os, x.model].filter(Boolean).join(' · ') || 'платформа неизвестна',
        h('div', null, h('div', { class: 's mono', style: 'overflow-wrap:anywhere' }, (x.ua || 'без User-Agent') + ' · ' + x.hwid),
          h('div', { class: 's' }, (x.ip ? `IP ${x.ip}` : 'IP неизвестен') + (x.created ? ` · добавлено ${ago(x.created * 1000)}` : '')), x.net ? netTag(x.net) : null),
        x.server ? h('span', { class: 'st bad' }, 'сервер') : null))) : h('div', { class: 'sheet', style: 'padding:14px 16px' }, 'Устройств нет — подписку ещё не добавляли или HWID не присылают.')),
    sec('IP подключений на нодах', d.conn_err ? 'ноды не отдали' : `${d.conn_list.length}`,
      h('div', { class: 'hint', style: 'margin-bottom:8px' }, 'Много IP — не признак: телефон меняет адрес от вышки к вышке. Важно, из скольких разных сетей (операторов) подключён одновременно — одно устройство не бывает в двух сетях сразу.'),
      d.conn_list.length ? h('div', { class: 'list' }, d.conn_list.map(c => row(c.ip,
        h('div', null, netTag(c.net), h('div', { class: 's' }, (c.nodes.length ? 'ноды: ' + c.nodes.join(', ') : '') + (c.last ? ` · виден ${ago(c.last * 1000)}` : ''))),
        h('span', { class: 'st ' + (c.live ? 'ok' : '') }, c.live ? 'сейчас' : 'раньше'))))
        : h('div', { class: 'sheet', style: 'padding:14px 16px' }, d.conn_err ? 'Ноды не отдали IP подключений (нужна панель 2.7+): ' + d.conn_err : 'Сейчас не подключён.')),
    (d.device_history || []).some(x => !x.current) ? sec('История устройств', `за 30 дней · ${d.device_history.length}`,
      h('div', { class: 'list' }, d.device_history.map(x => row(h('span', { class: 'mono' }, x.hwid),
        h('div', { class: 's' }, `впервые ${ago(x.first * 1000)}` + (x.current ? '' : ` · пропало ${ago(x.last * 1000)}`)),
        h('span', { class: 'st ' + (x.current ? 'ok' : '') }, x.current ? 'сейчас' : 'удалено'))))) : null,
    (d.sub_ips || []).length ? sec('С каких IP забирали ссылку подписки', `за сутки · ${d.sub_ips.length}`,
      h('div', { class: 'list' }, d.sub_ips.map(x => row(x.ip, h('div', null, netTag(x.net), h('div', { class: 's' }, ago(x.at * 1000))))))) : null,
    d.hourly ? sec('Трафик по часам', `за сутки · с трафиком ${d.active_hours} ч из 24`,
      h('div', { class: 'sheet', style: 'padding:14px 16px' }, (() => { const mx = Math.max(1, ...d.hourly);
        return h('div', { style: 'display:flex;align-items:flex-end;gap:2px;height:60px', 'aria-label': 'Трафик по часам' },
          d.hourly.map((b, i) => h('div', { title: `${24 - i} ч назад: ${gbs(b)}`, style: `flex:1;height:${Math.max(2, b / mx * 100)}%;border-radius:2px;background:${b >= 50 * 2 ** 20 ? 'var(--accent, var(--warn))' : 'var(--sheet-2)'}` }))); })(),
        h('div', { class: 'hint', style: 'margin-top:6px' }, 'У людей ночью трафик почти пропадает. Ровный круглые сутки — так качает сервер или раздача.'))) : null,
    (d.torrent_list || []).length ? sec('Торренты', `за неделю · ${d.torrent_list.length}`,
      h('div', { class: 'sheet', style: 'padding:14px 16px' }, 'Torrent Blocker срабатывал: ' + d.torrent_list.slice(0, 8).map(t => ago(t * 1000)).join(', '))) : null,
    sec('Запросы подписки', d.subs.length ? 'последние' : '',
      d.subs.length ? h('div', { class: 'list' }, d.subs.map(r => row(r.ua || 'без User-Agent',
        h('div', null, h('div', { class: 's' }, `${r.at ? ago(r.at * 1000) : ''}` + (r.ip ? ` · IP ${r.ip}` : '') + (r.rule ? ` · правило «${r.rule}»` : '')), r.net ? netTag(r.net) : null),
        h('span', { class: 'st ' + (r.blocked ? 'bad' : 'ok') }, r.blocked ? 'отказано' : (r.type || 'выдано').toLowerCase()))))
        : h('div', { class: 'sheet', style: 'padding:14px 16px' }, 'Панель не отдала историю запросов или их не было.')),
    d.traffic.length ? sec('Трафик по дням', '',
      h('div', { class: 'sheet', style: 'padding:14px 16px' }, d.traffic.map(t => h('div', { style: 'display:flex;align-items:center;gap:10px;margin:4px 0' },
        h('span', { class: 'mono s', style: 'width:84px' }, t.day.slice(5).split('-').reverse().join('.')),
        h('div', { style: 'flex:1;background:var(--sheet-2);border-radius:4px;height:10px' }, h('div', { style: `width:${t.bytes / maxT * 100}%;height:100%;border-radius:4px;background:var(--accent, var(--warn))` })),
        h('span', { class: 'num', style: 'width:70px;text-align:right' }, gbs(t.bytes)))))) : null,
    sec('Связанные аккаунты', d.links.length ? `${d.links.length}` : '',
      d.links.length ? h('div', { class: 'list' }, d.links.map(l => h('button', { type: 'button', class: 'li', onclick: () => go(`guard/${pid}/${encodeURIComponent(l.key)}`) },
        h('div', { class: 'grow' }, h('div', { class: 't' }, l.name), h('div', { class: 's' }, 'общее: ' + l.via.join(', '))),
        h('span', { class: 'st ' + (l.status === 'DISABLED' ? 'bad' : '') }, STATUS_RU[l.status] || l.status), h('span', { class: 'chev' }, ic('chevR')))))
        : h('div', { class: 'sheet', style: 'padding:14px 16px' }, 'Общих устройств и IP с другими подписчиками нет.'))];
}
