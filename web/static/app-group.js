'use strict';
/* Новый интерфейс: группа целиком — автоматизация, проверка и починка, плагины нод, уборка; утренняя сводка. */

const tzNow = () => -new Date().getTimezoneOffset();
const numIn = (val, onset, attrs = {}) => h('input', { class: 'inp mono', inputmode: 'numeric', style: 'max-width:140px', value: String(val ?? ''), ...attrs,
  onchange: e => { const v = parseInt(e.target.value.replace(/\D/g, ''), 10); onset(Number.isFinite(v) ? v : 0); } });
const hourSel = (val, onset, label) => h('select', { class: 'sel', 'aria-label': label || 'Час', value: String(val), onchange: e => onset(+e.target.value) },
  Array.from({ length: 24 }, (_, i) => h('option', { value: String(i) }, `${i}:00`)));
const area = (val, rows, oninput, ph) => h('textarea', { class: 'inp mono', rows: String(rows), spellcheck: 'false', placeholder: ph || '', oninput: e => oninput(e.target.value) }, val);
const section = (title, ...kids) => [h('div', { class: 'section-h' }, h('h2', null, title)), ...kids];

/* ═════════ Автоматизация ═════════ */
function groupAuto(g) {
  const key = 'a' + g.id, st = GD[key];
  if (!st) { GD[key] = { loading: true }; api(`profiles/${g.id}/auto`).then(r => { GD[key] = { a: r }; }).catch(e => { GD[key] = { err: e.message }; }).then(() => render(true)); }
  if (!st || st.loading) return h('div', { class: 'sk', style: 'height:300px' });
  if (st.err) return empty('alert', 'Не загрузилось', st.err, btn('Повторить', () => { delete GD[key]; render(); }, 'primary'));
  const a = st.a;
  const save = async patch => { try { Object.assign(a, await api(`profiles/${g.id}/auto`, 'POST', { ...patch, tz: tzNow() })); toast('Сохранено'); } catch (x) { toast('Не получилось', x.message, 'bad'); } render(); };
  const last = a.last || {};
  const lastText = last.date ? `Последний запуск ${last.date}` + (last.tag ? ` → ${last.tag}: обновлено ${(last.done || []).length}` + ((last.failed || []).length ? `, ошибок ${last.failed.length}` : '') : ': всё уже было свежим') : '';
  const sheet = (...kids) => h('div', { class: 'sheet form', style: 'padding:16px;margin-bottom:22px' }, ...kids);
  return h('div', { style: 'max-width:760px' },
    section('Обновление нод',
      h('div', { class: 'list', style: 'margin-bottom:10px' }, switchRow('Автообновление', 'По одной, с проверкой. Новая версия не вышла на связь — бот вернёт прежнюю и остановится', a.update, v => save({ update: v }))),
      a.update ? sheet(fld('Когда', h('div', { class: 'chips' },
        h('select', { class: 'sel', 'aria-label': 'День', value: a.weekday == null ? '' : String(a.weekday), onchange: e => save({ weekday: e.target.value === '' ? null : +e.target.value }) },
          h('option', { value: '' }, 'каждый день'), (a.weekdays || WD).map((d, i) => h('option', { value: String(i) }, d))),
        hourSel(a.hour, x => save({ hour: x }))), lastText || 'Время — по часовому поясу этого устройства.')) : h('div', { style: 'margin-bottom:22px' })),
    section('Нагрузка и онлайн',
      h('div', { class: 'list', style: 'margin-bottom:10px' }, switchRow('Следить за нагрузкой', 'Процессор, память, диск, трафик раз в 15 минут. Нода упирается ~45 минут — бот напишет и посоветует тариф или ещё ноду', a.load, v => save({ load: v }))),
      sheet(fld('Онлайн на ноду', numIn(a.online_max, x => save({ online_max: x }), { placeholder: '0' }),
        a.online_max ? 'Онлайн держится от 90% предела ~15 минут — бот напишет заранее.' : '0 — не следить. Поставьте чуть выше обычного пика ноды.'),
        (a.online || []).length ? h('div', { class: 'list', style: 'background:var(--sheet-2)' }, a.online.slice(0, 6).map(x => h('div', { class: 'li' },
          flag(x.country) ? h('span', { class: 'flag' }, flag(x.country)) : null, h('div', { class: 'grow' }, h('div', { class: 't' }, x.name), h('div', { class: 's' }, `сейчас ${x.now ?? '—'} · пик за сутки ${x.peak}`)),
          a.online_max && x.peak >= a.online_max * 0.9 ? h('span', { class: 'st ' + (x.peak >= a.online_max ? 'bad' : 'warn') }, x.peak >= a.online_max ? 'полна' : 'почти полна') : null))) : null)),
    section('Деньги',
      sheet(fld('Автопродление серверов', seg([{ v: null, l: 'Не трогать' }, { v: true, l: 'Включать всем' }], a.renew === true ? true : null, x => save({ renew: x }), 'Автопродление'),
          'Раз в 6 часов бот проверяет аренду и пишет, если сервер кончится через 3 дня и сам не продлится.'),
        fld('Предупредить, если денег хватит меньше чем на', seg([{ v: 0, l: 'выкл.' }, { v: 3, l: '3 дня' }, { v: 7, l: 'неделю' }, { v: 14, l: '2 недели' }], a.bal_days, x => save({ bal_days: x }), 'Прогноз баланса')))),
    section('Автомасштабирование',
      sheet(fld('Когда нода перегружена', seg([{ v: 'off', l: 'Только сообщить' }, { v: 'ask', l: 'Предлагать докупить' }], a.scale, x => save({ scale: x }), 'Автомасштабирование'),
          a.scale === 'off' ? 'Бот может предложить соседа в той же локации — нагрузка разделится.'
            : 'Только для нод, купленных ботом, не чаще раза в сутки на ноду. Предложение появится на главной: домен, тариф и сквады выбираете вы, без подтверждения бот ничего не покупает.'),
        a.scale !== 'off' ? h('div', { class: 'row2' },
          fld('Лимит докупленных нод', numIn(a.scale_max, x => save({ scale_max: Math.max(1, x) }))),
          fld('Не тратить ниже, ₽', numIn(a.scale_floor, x => save({ scale_floor: x })), 'останется на балансе')) : null)),
    h('div', { class: 'list' }, dangerRow('clock', 'Утренняя сводка', 'Итог по всем группам раз в день', btn('Настроить', () => go('settings/summary'), 's'))));
}

/* ═════════ Проверка и починка ═════════ */
function groupMon(g) {
  if (!D.mon) return empty('radar', 'Мониторинг выключен', 'На этом боте нет проверки из России.');
  const m = (D.mon.groups || []).find(x => x.id === g.id);
  if (!m) return h('div', { class: 'sk', style: 'height:200px' });
  const ALL = Object.keys(D.mon.steps), key = 'm' + g.id;
  const S = GD[key] || (GD[key] = { sub: '', mode: m.mon_mode, on: new Set(m.mon_steps), order: [...m.mon_steps, ...ALL.filter(k => !m.mon_steps.includes(k))],
    script: m.mon_script, port: String(m.mon_port || 8443), old: m.mon_old, max: String(m.mon_max_buys), buy: (m.mon_buy || []).map(x => ({ ...x })), last: m.mon_buy_last, skip: m.mon_skip || '' });
  const mv = (arr, i, j) => { [arr[i], arr[j]] = [arr[j], arr[i]]; render(); };
  const arrows = (arr, i, removable) => h('span', { class: 'chips', style: 'gap:4px;flex-wrap:nowrap' },
    btn('', () => mv(arr, i, i - 1), 's', 'up', { disabled: i === 0, 'aria-label': 'Выше', style: 'padding:0 8px' }),
    btn('', () => mv(arr, i, i + 1), 's', 'chevD', { disabled: i === arr.length - 1, 'aria-label': 'Ниже', style: 'padding:0 8px' }),
    removable ? btn('', () => { arr.splice(i, 1); render(); }, 's', 'x', { disabled: arr.length === 1, 'aria-label': 'Убрать', style: 'padding:0 8px' }) : null);
  const BL = c => c.type === 'same' ? 'Как у старой ноды' : `${c.cpu} vCPU · ${c.ram} ГБ · ${c.disk} ГБ` + (c.type === 'custom' ? ' — своя' : ' — тариф');
  const addBuy = c => { if (S.buy.some(x => JSON.stringify(x) === JSON.stringify(c))) return toast('Такой вариант уже есть', null, 'info'); if (S.buy.length >= 8) return toast('Не больше 8 вариантов', null, 'bad'); S.buy.push(c); render(); };
  const pickPreset = async e => busyBtn(e, async () => {
    let c; try { c = await api('catalog?pid=' + g.id); } catch (x) { return toast('Bobr Cloud не ответил', x.message, 'bad'); }
    const specs = {};
    c.locations.forEach(l => l.offers.forEach(o => { const k = `${o.cpu}/${o.ram}/${o.disk}`; const s = specs[k] || (specs[k] = { cpu: o.cpu, ram: o.ram, disk: o.disk, min: o.price_kop, max: o.price_kop, n: 0 }); s.min = Math.min(s.min, o.price_kop); s.max = Math.max(s.max, o.price_kop); s.n++; }));
    const close = modal({ title: 'Готовый тариф', icon: ['server', 'run'], body: h('div', null, h('p', { style: 'margin:0 0 10px' }, 'Ищется в локации старой ноды по vCPU, памяти и диску. Нет там такого — следующий вариант.'),
      h('div', { class: 'list', style: 'background:var(--sheet-2)' }, Object.values(specs).sort((a, b) => a.min - b.min).map(s => h('button', { type: 'button', class: 'li', onclick: () => { close(); addBuy({ type: 'preset', cpu: s.cpu, ram: s.ram, disk: s.disk }); } },
        h('div', { class: 'grow' }, h('div', { class: 't' }, `${s.cpu} vCPU · ${s.ram} ГБ · ${s.disk} ГБ`), h('div', { class: 's' }, `локаций: ${s.n}`)),
        h('span', { class: 'num' }, s.min === s.max ? rubK(s.min) : `${rubK(s.min)}–${rubK(s.max)}`))))) });
  });
  const pickCustom = async e => busyBtn(e, async () => {
    let c; try { c = await api('catalog?pid=' + g.id); } catch (x) { return toast('Bobr Cloud не ответил', x.message, 'bad'); }
    const conf = c.configurator;
    if (!conf) return toast('Конструктора нет', 'В Bobr Cloud доступны только готовые тарифы', 'info');
    const sp = { cpu: conf.cpu.min, ram: conf.ram.min, disk: conf.disk.min };
    modal({ title: 'Своя конфигурация', icon: ['sliders', 'run'], body: h('div', { class: 'form' }, [['cpu', 'vCPU'], ['ram', 'Память, ГБ'], ['disk', 'Диск, ГБ']].map(([k, t]) =>
      fld(t, h('input', { class: 'inp mono', type: 'number', min: conf[k].min, max: conf[k].max, step: conf[k].step || 1, value: String(sp[k]), style: 'max-width:140px', oninput: x => { sp[k] = +x.target.value; } }), `от ${conf[k].min} до ${conf[k].max}`))),
      actions: [{ l: 'Отмена' }, { l: 'Добавить', cls: 'primary', fn: cl => { cl(); addBuy({ type: 'custom', ...sp }); } }] });
  });
  const save = e => busyBtn(e, async () => {
    try {
      const r = await api(`profiles/${g.id}/monitor`, 'POST', { mon_sub: S.sub, mon_mode: S.mode, mon_steps: S.order.filter(k => S.on.has(k)), mon_script: S.script, mon_port: S.port,
        mon_old: S.old, mon_max_buys: S.max, mon_buy: S.buy, mon_buy_last: S.last, mon_skip: S.skip });
      toast('Сохранено', r.resync ? 'Записываю подписку на чекеры — результаты примерно через минуту' : null); delete GD[key]; D.mon = await api('monitor'); render();
    } catch (x) { toast('Не получилось', x.message, 'bad'); }
  });
  const notify = async (patch, msg) => { try { await api(`profiles/${g.id}/monitor/notify`, 'POST', patch); D.mon = await api('monitor'); toast(msg); } catch (x) { toast('Не получилось', x.message, 'bad'); } render(); };
  const muted = (m.mon_mute || []).length;
  let n = 0;
  const MODE_SUB = { watch: 'только отметка в интерфейсе', notify: 'сообщение в чат', ask: 'сообщение с кнопками «Починить», «Новая нода», «Не трогать»', auto: 'шаги ниже по порядку, без вопросов' };
  return h('div', { style: 'max-width:760px' },
    h('div', { class: 'list', style: 'margin-bottom:22px' },
      switchRow('Сообщения в Telegram', m.mon_quiet ? 'выключены для всей группы' : muted ? `без уведомлений: ${pl(muted, 'нода', 'ноды', 'нод')}` : 'когда нода упала, ожила и что сделала починка',
        !m.mon_quiet, v => notify({ quiet: !v }, v ? 'Уведомления включены' : 'Уведомления выключены')),
      muted ? dangerRow('bell', 'Заглушённые ноды', `${muted}: проверяются и чинятся молча`, btn('Включить всем', () => notify({ clear: true }, 'Уведомления нод включены'), 's')) : null),
    section('Подписка для проверки', h('div', { class: 'sheet form', style: 'padding:16px;margin-bottom:22px' },
      fld('Ссылка подписки', h('input', { class: 'inp mono', type: 'password', autocomplete: 'off', 'data-k': 'msub', value: S.sub, oninput: e => { S.sub = e.target.value.trim(); },
        placeholder: m.sub_hint ? `сохранена ${m.sub_hint} · введите новую, чтобы заменить («-» — убрать)` : 'https://…' }),
        'Отдельный пользователь панели со всеми сквадами нод. Бот запишет его подписку на все чекеры.'))),
    section('Когда нода недоступна', h('div', { class: 'list', role: 'radiogroup', style: 'margin-bottom:22px' }, Object.entries(D.mon.modes).map(([k, t]) =>
      h('button', { type: 'button', class: 'li', role: 'radio', 'aria-checked': String(S.mode === k), onclick: () => { S.mode = k; render(); } },
        h('span', { class: 'cb-m' + (S.mode === k ? ' on' : ''), style: 'border-radius:50%' }, S.mode === k ? ic('check') : null),
        h('div', { class: 'grow' }, h('div', { class: 't' }, t[0].toUpperCase() + t.slice(1)), h('div', { class: 's', style: 'white-space:normal' }, MODE_SUB[k] || '')))))),
    h('div', { class: 'section-h' }, h('h2', null, 'Шаги починки'), h('span', { class: 'faint', style: 'font-size:13px' }, `${S.on.size} из ${S.order.length} · сверху вниз`)),
    h('div', { class: 'list', style: 'margin-bottom:8px' }, S.order.map((k, i) => { const on = S.on.has(k);
      return [h('div', { class: 'li', style: 'flex-wrap:wrap' }, toggle(on, v => { v ? S.on.add(k) : S.on.delete(k); render(); }, D.mon.steps[k]),
        h('div', { class: 'grow', style: 'flex:1 1 160px' }, h('div', { class: 't', style: on ? '' : 'color:var(--ink-3)' }, (on ? (++n) + '. ' : '') + D.mon.steps[k][0].toUpperCase() + D.mon.steps[k].slice(1))),
        arrows(S.order, i)),
      on && k === 'script' ? h('div', { class: 'li' }, fld('Чем переустановить', h('select', { class: 'sel', value: String(S.script ?? ''), onchange: e => { S.script = e.target.value ? +e.target.value : null; } },
        h('option', { value: '' }, 'Встроенный установщик'), (D.mon.scripts || []).map(x => h('option', { value: String(x.id) }, x.title))))) : null,
      on && k === 'mask' ? h('div', { class: 'li' }, fld('Порт для клиентов', h('input', { class: 'inp mono', inputmode: 'numeric', style: 'max-width:140px', value: S.port, oninput: e => { S.port = e.target.value.replace(/\D/g, ''); } }))) : null];
    })),
    h('p', { class: 'muted', style: 'margin:0 0 22px;font-size:13px' }, 'После каждого шага бот ждёт две проверки (~10 мин): ожила — готово, нет — следующий. Смена IP — только для нод, купленных ботом.'),
    section('Конфигурация новой ноды',
      h('div', { class: 'list', style: 'margin-bottom:10px' }, S.buy.map((c, i) => h('div', { class: 'li' }, h('div', { class: 'grow' }, h('div', { class: 't' }, `${i + 1}. ${BL(c)}`)), arrows(S.buy, i, true)))),
      h('div', { class: 'chips', style: 'margin-bottom:12px' }, S.buy.some(x => x.type === 'same') ? null : btn('Как у старой', () => addBuy({ type: 'same' }), 's', 'plus'),
        btn('Готовый тариф', pickPreset, 's', 'plus'), btn('Своя конфигурация', pickCustom, 's', 'plus')),
      h('div', { class: 'sheet form', style: 'padding:16px;margin-bottom:22px' },
        fld('Если не подошёл ни один', seg(Object.entries(D.mon.buy_last).map(([v, l]) => ({ v, l })), S.last, v => { S.last = v; render(); }, 'Запасной вариант')),
        fld('Старый сервер после замены', seg(Object.entries(D.mon.old).map(([v, l]) => ({ v, l })), S.old, v => { S.old = v; render(); }, 'Старый сервер'),
          'Удалённый в Bobr Cloud сервер не вернуть.'),
        fld('Покупок за сутки, не больше', h('input', { class: 'inp mono', inputmode: 'numeric', style: 'max-width:140px', value: S.max, oninput: e => { S.max = e.target.value.replace(/\D/g, ''); } }), `Сегодня куплено: ${m.buys_today}.`))),
    section('Не проверять', h('div', { class: 'sheet form', style: 'padding:16px;margin-bottom:22px' },
      fld('Группы подписки (балансировщики)', h('input', { class: 'inp', 'data-k': 'mskip', value: S.skip, placeholder: 'АВТО-ВЫБОР, NORMAL', oninput: e => { S.skip = e.target.value; } }),
        'Через запятую, достаточно части названия: общий балансировщик повторяет ноды, которые и так проверяются.'))),
    h('div', { class: 'chips' }, btn('Сохранить', save, 'primary'), btn('Сбросить', () => { delete GD[key]; render(); }), btn('Чекеры', () => go('settings/checkers'), 'ghost', 'radar')));
}

/* ═════════ Плагины нод ═════════ */
const PDUR = [{ v: 3600, l: '1 ч' }, { v: 6 * 3600, l: '6 ч' }, { v: 86400, l: 'сутки' }, { v: 7 * 86400, l: 'неделя' }];
function groupPlugins(g) {
  const key = 'p' + g.id, st = GD[key];
  const load = () => { GD[key] = { loading: true }; api(`profiles/${g.id}/plugins`).then(r => { GD[key] = { p: r }; }).catch(e => { GD[key] = { err: e.message }; }).then(() => render(true)); };
  if (!st) load();
  if (!st || st.loading) return h('div', { class: 'sk', style: 'height:300px' });
  if (st.err) return empty('alert', 'Плагины не загрузились', st.err, btn('Повторить', load, 'primary'));
  const P = st.p, F = st.f || (st.f = { ignore: (P.torrent.ignore_ips || []).join('\n'), ig: P.ingress.manual.join('\n'), egIps: P.egress.manual_ips.join('\n'), egPorts: P.egress.manual_ports.join('\n') });
  const done = r => { GD[key] = { p: r }; render(); };
  const save = async (patch, msg) => { try { done(await api(`profiles/${g.id}/plugins`, 'POST', patch)); toast(msg); } catch (x) { toast('Не получилось', x.message, 'bad'); render(); } };
  const preset = async (kind, id, on) => { try { done(await api(`profiles/${g.id}/plugins/preset`, 'POST', { kind, id, on })); toast(on ? 'Пресет добавлен' : 'Пресет убран'); } catch (x) { toast('Не получилось', x.message, 'bad'); render(); } };
  const all = P.total > 0 && P.bound === P.total, T = P.torrent, I = P.ingress, E = P.egress;
  const sheet = (...kids) => h('div', { class: 'sheet form', style: 'padding:16px;margin-bottom:10px' }, ...kids);
  return h('div', { style: 'max-width:760px' },
    h('div', { class: 'list', style: 'margin-bottom:22px' }, dangerRow('layers', P.exists ? `Нод под плагином: ${P.bound} из ${P.total}` : 'Плагин ещё не создан',
      P.exists ? `${P.name} · новые ноды бот привязывает сам` : 'Создастся при первом включении функции',
      P.exists && !all ? btn('Привязать ко всем', () => P.foreign.length
        ? modal({ title: 'У части нод другой плагин', icon: ['layers', 'warn'], body: `${P.foreign.slice(0, 5).join(', ')}${P.foreign.length > 5 ? '…' : ''} — у ноды может быть только один плагин. Заменить на этот?`,
          actions: [{ l: 'Только свободные', fn: c => { c(); save({ bind: false }, 'Привязано'); } }, { l: 'Заменить', cls: 'primary', fn: c => { c(); save({ bind: true }, 'Привязано'); } }] })
        : save({ bind: false }, 'Привязано'), 's primary') : P.exists ? h('span', { class: 'st ok' }, 'все') : null)),
    section('Блокировка торрентов',
      h('div', { class: 'list', style: 'margin-bottom:10px' }, switchRow('Torrent Blocker', 'IP пользователя банится после первого торрент-пакета (nftables на ноде)', T.on, v => save({ torrent: { on: v } }, v ? 'Включено' : 'Выключено'))),
      T.on ? sheet(fld('Срок бана', seg(PDUR.some(x => x.v === T.duration) ? PDUR : [...PDUR, { v: T.duration, l: Math.round(T.duration / 60) + ' мин' }], T.duration, x => save({ torrent: { duration: x } }), 'Срок бана')),
        fld('Не банить эти IP (по одному в строке)', area(F.ignore, 3, x => { F.ignore = x; })),
        h('div', { class: 'chips' }, btn('Сохранить исключения', () => save({ torrent: { ignore_ips: F.ignore } }, 'Сохранено'), 's'), btn('Срабатывания', () => torrentStats(g), 's', 'activity'))) : null,
      h('div', { style: 'margin-bottom:12px' })),
    section('Фильтр входящих',
      h('div', { class: 'list', style: 'margin-bottom:10px' }, switchRow('Ingress Filter', I.count ? `в списке ${I.count} подсетей — сканеры не получают ответа от ноды` : 'список пуст — включите пресет', I.on, v => save({ ingress: { on: v } }, v ? 'Включено' : 'Выключено')),
        I.presets.map(x => switchRow(x.title, x.on ? `${x.sub} · ${x.count} подсетей` : x.sub, x.on, v => preset('ingress', x.id, v)))),
      sheet(fld('Свои адреса и подсети IPv4', area(F.ig, 3, x => { F.ig = x; })),
        h('div', { class: 'chips' }, btn('Сохранить свои адреса', () => save({ ingress: { manual: F.ig } }, 'Сохранено'), 's'),
          I.presets.some(x => x.on) ? btn('Обновить пресеты', async e => busyBtn(e, async () => { for (const x of I.presets.filter(y => y.on)) await preset('ingress', x.id, true); }), 's', 'refresh') : null)),
      h('div', { style: 'margin-bottom:12px' })),
    section('Фильтр исходящих',
      h('div', { class: 'list', style: 'margin-bottom:10px' }, switchRow('Egress Filter', 'Куда серверу ноды нельзя подключаться: взломанный аккаунт не уйдёт в частные сети и не начнёт рассылать спам', E.on, v => save({ egress: { on: v } }, v ? 'Включено' : 'Выключено')),
        E.presets.map(x => switchRow(x.title, x.sub, x.on, v => (v && x.id === 'private')
          ? confirmAct({ title: 'Закрыть частные сети?', icon: ['shield', 'warn'], ok: 'Закрыть', text: 'Если у серверов нод внутренняя сеть провайдера или DNS в этих диапазонах, ноды потеряют связь. На обычных VPS это безопасно.', fn: async () => preset('egress', x.id, true) })
          : preset('egress', x.id, v)))),
      sheet(fld('Свои адреса и подсети (IPv4 / IPv6)', area(F.egIps, 2, x => { F.egIps = x; })), fld('Свои порты', area(F.egPorts, 1, x => { F.egPorts = x; })),
        h('div', { class: 'chips' }, btn('Сохранить свои списки', () => save({ egress: { manual_ips: F.egIps, manual_ports: F.egPorts } }, 'Сохранено'), 's')))));
}
async function torrentStats(g) {
  let S; try { S = await api(`profiles/${g.id}/plugins/stats`); } catch (x) { return toast('Не загрузилось', x.message, 'bad'); }
  const close = modal({ title: 'Срабатывания торрент-блока', icon: ['activity', 'run'], body: h('div', null,
    h('p', { style: 'margin:0 0 12px' }, `За сутки ${S.day}, всего ${S.total}, пользователей ${S.users}. Нажмите на запись, чтобы снять бан раньше срока.`),
    S.recent.length ? h('div', { class: 'list', style: 'background:var(--sheet-2)' }, S.recent.slice(0, 30).map(x => h('button', { type: 'button', class: 'li', disabled: !x.ip,
      onclick: () => confirmAct({ title: 'Разблокировать?', icon: ['shield', 'run'], ok: 'Разблокировать', text: `${x.ip} снова сможет подключаться ко всем нодам.`,
        fn: async () => { await api(`profiles/${g.id}/plugins/unblock`, 'POST', { ip: x.ip }); toast('Разблокирован', x.ip); close(); } }) },
      h('div', { class: 'grow' }, h('div', { class: 't mono' }, x.user || '—'), h('div', { class: 's' }, [x.ip, x.node, x.at ? new Date(x.at).toLocaleString('ru-RU', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) : ''].filter(Boolean).join(' · '))))))
      : h('div', { class: 'muted' }, 'Срабатываний пока не было.')) });
}

/* ═════════ Уборка ═════════ */
function groupCleanup(g) {
  const key = 'c' + g.id, st = GD[key];
  const load = () => { GD[key] = { loading: true }; api(`profiles/${g.id}/cleanup`).then(r => { GD[key] = { c: r }; }).catch(e => { GD[key] = { err: e.message }; }).then(() => render(true)); };
  if (!st) load();
  if (!st || st.loading) return h('div', { class: 'sk', style: 'height:200px' });
  if (st.err) return empty('alert', 'Не удалось проверить', st.err, btn('Повторить', load, 'primary'));
  const C = st.c;
  const delServer = x => confirmTyped({ title: `Удалить ${x.name || x.ip}?`, word: x.name || x.ip, ok: 'Удалить сервер',
    text: 'Сервер удалится в Bobr Cloud вместе с нодой, хостами и A-записью. Отменить нельзя.',
    fn: async () => { await api(`profiles/${g.id}/cleanup/servers/${x.id}`, 'POST', {}); toast('Удалено'); load(); refreshSoon(); } });
  const delNode = x => confirmTyped({ title: `Убрать ${x.name} из панели?`, word: x.name, ok: 'Убрать',
    text: 'Нода, её хосты и A-запись. Сам сервер бот не трогает — он куплен не через бота.',
    fn: async () => { await api(`profiles/${g.id}/nodes/${x.uuid}`, 'DELETE'); toast('Убрано'); load(); refreshSoon(); } });
  if (!C.servers.length && !C.dead.length) return empty('check', 'Убирать нечего', 'Все оплаченные серверы в панели, мёртвых нод нет. Бот проверяет это каждые 6 часов и напишет, если что-то найдётся.');
  return h('div', { style: 'max-width:760px' },
    C.servers.length ? section('Оплачиваются впустую', h('p', { class: 'muted', style: 'margin:0 0 10px' }, `${rubK(C.waste_kop)} в месяц за серверы, которых нет в панели.`),
      h('div', { class: 'list', style: 'margin-bottom:22px' }, C.servers.map(x => h('div', { class: 'li', style: 'flex-wrap:wrap' }, h('span', { class: 'flag' }, x.flag || ''),
        h('div', { class: 'grow', style: 'flex:1 1 200px' }, h('div', { class: 't' }, x.name || x.ip), h('div', { class: 's', style: 'white-space:normal' }, `${x.why} · ${rubK(x.price_kop)}/мес`)),
        btn('Удалить', () => delServer(x), 's danger'))))) : null,
    C.dead.length ? section('Ноды без связи', h('p', { class: 'muted', style: 'margin:0 0 10px' }, 'Купленные ботом удаляются вместе с сервером, остальные — только из панели.'),
      h('div', { class: 'list' }, C.dead.map(x => h('div', { class: 'li', style: 'flex-wrap:wrap' },
        h('div', { class: 'grow', style: 'flex:1 1 200px' }, h('div', { class: 't' }, x.name), h('div', { class: 's' }, `${x.address} · нет связи ${pl(x.days, 'день', 'дня', 'дней')}`)),
        btn(x.server_id ? 'Удалить' : 'Убрать', () => x.server_id ? delServer({ id: x.server_id, name: x.name }) : delNode(x), 's danger'))))) : null);
}

/* ═════════ Утренняя сводка ═════════ */
function setSummary() {
  if (!D.sum) { D.sum = { loading: true }; api('summary').then(r => { D.sum = r; }).catch(e => { D.sum = { error: e.message }; }).then(() => render(true)); }
  const S = D.sum;
  if (S.loading) return h('div', { class: 'sk', style: 'height:160px' });
  if (S.error) return empty('alert', 'Не загрузилось', S.error, btn('Повторить', () => { D.sum = null; render(); }, 'primary'));
  const save = async p => { try { D.sum = await api('summary', 'POST', { ...p, tz: tzNow() }); toast('Сохранено'); } catch (x) { toast('Не получилось', x.message, 'bad'); } render(); };
  return h('div', { style: 'max-width:640px' },
    h('p', { class: 'muted', style: 'margin:0 0 14px' }, 'По каждой группе: сколько нод на связи и онлайн, что недоступно из России и что бот чинил, обновления за сутки, нагрузка, старые версии и аренда, которая скоро кончится. В чате — команда /summary.'),
    h('div', { class: 'list', style: 'margin-bottom:12px' }, switchRow('Присылать каждый день', S.on ? `в ${S.hour}:00 по вашему времени` : 'выключено', S.on, v => save({ on: v }))),
    S.on ? h('div', { class: 'sheet form', style: 'padding:16px;margin-bottom:12px' }, fld('Время', hourSel(S.hour, x => save({ hour: x })))) : null,
    btn('Прислать сейчас', e => busyBtn(e, async () => { try { const r = await api('summary/now', 'POST', {}); toast('Отправлено', r.message); } catch (x) { toast('Не получилось', x.message, 'bad'); } }), '', 'bell'));
}
