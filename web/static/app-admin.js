'use strict';
/* Новый интерфейс: свои скрипты, отчёты и обращения, журнал действий, нагрузка и защита сервера бота,
   подписка (тариф администратора, подписка и пауза пользователя), доступ пользователя. */

const dt = (ts, withTime) => ts ? new Date(ts * 1000).toLocaleString('ru-RU', withTime ? { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' } : { day: 'numeric', month: 'long', year: 'numeric' }).replace(/ г\.$/, '') : '—';
const gb = b => b == null ? '—' : b >= 1e9 ? (b / 1e9).toFixed(1).replace('.', ',') + ' ГБ' : Math.round(b / 1e6) + ' МБ';
const durH = s => !s ? '—' : s >= 86400 ? pl(Math.floor(s / 86400), 'день', 'дня', 'дней') : s >= 3600 ? Math.floor(s / 3600) + ' ч' : Math.max(1, Math.floor(s / 60)) + ' мин';
const loader = (slot, path, then) => {  // D[slot]: {loading} → данные или {error}
  if (!D[slot]) { D[slot] = { loading: true }; api(path).then(r => { D[slot] = then ? then(r) : r; }).catch(e => { D[slot] = { error: e.message }; }).then(() => render(true)); }
  return D[slot];
};
const failBox = (slot, msg) => empty('alert', 'Не загрузилось', msg, btn('Повторить', () => { D[slot] = null; render(); }, 'primary'));
const sk = (ht = 240) => h('div', { class: 'sk', style: `height:${ht}px` });
const kvRows = rows => h('div', { class: 'kv', style: 'margin-bottom:16px' }, rows.filter(Boolean).map(([k, v]) => [h('div', { class: 'k' }, k), h('div', { class: 'v' }, v), h('div')]));

/* ═════════ Свои скрипты ═════════ */
function setScripts() {
  const S = loader('scr', 'scripts');
  if (S.loading) return sk();
  if (S.error) return failBox('scr', S.error);
  const v = { url: '' }, err = h('div', { class: 'err', role: 'alert' });
  const create = async path => {
    const r = await api('scripts', 'POST', { url: v.url, path });
    if (r.choose) {
      const close = modal({ title: 'Какой файл ставит ноду?', icon: ['code', 'run'], body: h('div', null, h('p', { style: 'margin:0 0 10px' }, 'Файлы .sh на коммите ' + r.sha.slice(0, 12) + '.'),
        h('div', { class: 'list', style: 'background:var(--sheet-2)' }, r.choose.map(c => h('button', { type: 'button', class: 'li', onclick: async () => { close(); try { await create(c.path); } catch (x) { toast('Не получилось', x.message, 'bad'); } } },
          h('div', { class: 'grow' }, h('div', { class: 't mono' }, c.path)), h('span', { class: 'faint' }, Math.round(c.size / 1024) + ' КБ'))))) });
      return;
    }
    D.scr = null; toast('Скрипт загружен', 'Gemini разбирает его'); go('script/' + r.id); UI.autoAnalyze = String(r.id);
  };
  const SST = { approved: ['ok', 'утверждён'], draft: ['warn', 'черновик'] };
  return h('div', { style: 'max-width:760px' },
    h('p', { class: 'muted', style: 'margin:0 0 14px' }, 'Скрипты установки с GitHub. Gemini разбирает, вы утверждаете рецепт. Бот закрепляет коммит и SHA-256 файла — на ноды идёт ровно разобранная версия.'),
    !S.gemini ? h('div', { class: 'notice', style: 'background:var(--warn-soft)' }, h('span', { class: 'i', style: 'color:var(--warn)' }, ic('info')), h('div', { class: 'grow' }, h('b', null, 'Нужен ключ Gemini'), 'Задайте его в «Подключениях».'),
      btn('Открыть', () => go('settings/connections'), 's')) : null,
    h('div', { class: 'sheet form', style: 'padding:16px;margin-bottom:20px' }, fld('Ссылка на репозиторий или файл', h('input', { class: 'inp mono', type: 'url', placeholder: 'https://github.com/автор/репозиторий', oninput: e => { v.url = e.target.value.trim(); } })), err,
      h('div', null, btn('Добавить', e => busyBtn(e, async () => { err.textContent = ''; try { await create(); } catch (x) { err.textContent = x.message; } }), 'primary', 'plus'))),
    h('div', { class: 'list' }, h('div', { class: 'li' }, h('div', { class: 'ic-tile' }, ic('box')), h('div', { class: 'grow' }, h('div', { class: 't' }, 'Встроенный установщик'), h('div', { class: 's' }, 'VLESS TLS Vision')), h('span', { class: 'tag' }, 'по умолчанию')),
      (S.scripts || []).map(s => h('button', { type: 'button', class: 'li', onclick: () => go('script/' + s.id) }, h('div', { class: 'ic-tile' }, ic('code')),
        h('div', { class: 'grow' }, h('div', { class: 't' }, s.title), h('div', { class: 's mono' }, `${s.repo}/${s.path} @${s.sha.slice(0, 7)}` + (s.used_by.length ? ' · ' + s.used_by.join(', ') : ''))),
        h('span', { class: 'st ' + (SST[s.status] || ['off'])[0] }, (SST[s.status] || [0, s.status])[1]), h('span', { class: 'chev' }, ic('chevR'))))));
}
function vScript(id) {
  const slot = 'sv' + id, s = loader(slot, 'scripts/' + id);
  const back = h('button', { type: 'button', class: 'back', onclick: () => go('settings/scripts') }, ic('chevL'), 'Мои скрипты');
  if (s.loading) return [back, sk(300)];
  if (s.error && !s.id) return [back, failBox(slot, s.error)];
  const busy = UI['scrBusy' + id];
  const run = async (fn, okMsg) => { UI['scrBusy' + id] = true; render(); try { D[slot] = await fn(); D.scr = null; if (okMsg) toast(okMsg); } catch (x) { toast('Не получилось', x.message, 'bad'); } UI['scrBusy' + id] = false; render(); };
  const analyze = () => run(() => api(`scripts/${id}/analyze`, 'POST'));
  if (UI.autoAnalyze === id && !s.analysis && !busy) { UI.autoAnalyze = null; setTimeout(analyze); }
  const a = s.analysis, d = s.describe, chat = { text: '' };
  const SST = { approved: ['ok', 'утверждён'], draft: ['warn', 'черновик'] };
  return [back, pageH(s.title, `${s.repo}/${s.path} @${s.sha.slice(0, 12)}`),
    h('div', { style: 'max-width:820px' },
      kvRows([['Статус', h('span', { class: 'st ' + (SST[s.status] || ['off'])[0] }, (SST[s.status] || [0, s.status])[1])], ['SHA-256', h('span', { class: 'mono' }, s.sha256.slice(0, 16) + '…')],
        ['GitHub', h('a', { href: s.github, target: '_blank', rel: 'noopener', onclick: e => { if (tg && tg.openLink) { e.preventDefault(); tg.openLink(s.github); } } }, 'эта версия файла')]]),
      busy ? h('div', { class: 'notice info' }, h('span', { class: 'i' }, ic('refresh')), h('div', null, h('b', null, 'Gemini читает скрипт'), 'Обычно 20–60 секунд.')) : null,
      !a && !busy ? btn('Разобрать с Gemini', analyze, 'primary', 'zap') : null,
      a ? [...section('Что делает скрипт', h('div', { class: 'sheet', style: 'padding:16px;margin-bottom:16px' }, a.summary || '—')),
        a.recommendations.length ? section('Рекомендации', h('ul', { class: 'sheet', style: 'padding:14px 16px 14px 34px;margin:0 0 16px' }, a.recommendations.map(t => h('li', null, t)))) : null,
        a.risks.length ? section('Риски', h('ul', { class: 'sheet', style: 'padding:14px 16px 14px 34px;margin:0 0 16px' }, a.risks.map(t => h('li', null, t)))) : null,
        a.recipe_error ? h('div', { class: 'notice' }, h('span', { class: 'i' }, ic('alert')), h('div', null, h('b', null, 'Рецепт не прошёл проверку'), a.recipe_error + '. Напишите Gemini ниже или поправьте вручную.')) : null] : null,
      d ? [...section('Как бот будет ставить', kvRows([['Запуск', h('span', { class: 'mono' }, d.run)],
          ...Object.entries(d.env || {}).map(([k, x]) => ['Переменная ' + k, h('span', { class: 'mono' }, x)]),
          ...d.prompts.map(p => ['На «' + p.match + '»', '→ ' + (p.answer.includes('{secret_key}') ? '‹SECRET_KEY›' : p.answer) + (p.why ? ' — ' + p.why : '')]),
          d.done_when ? ['Готово, когда', h('span', { class: 'mono' }, d.done_when)] : null,
          ...d.post_commands.map(c => ['После установки', h('span', { class: 'mono' }, c)]),
          d.check_command ? ['Проверка', h('span', { class: 'mono' }, d.check_command)] : null,
          ['Профиль Xray', d.profile.mode === 'file' ? `скрипт пишет ${d.profile.path} — бот создаст профиль в панели` : 'уже есть в панели — выберите его в настройках группы']])),
        h('p', { class: 'muted', style: 'margin:-6px 0 14px;font-size:13px' }, 'На вопрос скрипта, которого нет в списке, бот остановится. Команды «после установки» выполняются под root — проверьте их.'),
        h('div', { class: 'chips', style: 'margin-bottom:22px' },
          s.status !== 'approved' ? btn('Утвердить', () => run(() => api(`scripts/${id}/approve`, 'POST'), 'Утверждён — можно выбрать в группе'), 'primary', 'check') : null,
          btn('Править рецепт', () => { const ta = h('textarea', { class: 'inp mono', rows: '16', spellcheck: 'false' }, JSON.stringify(s.recipe, null, 2)); const err = h('div', { class: 'err' });
            modal({ title: 'Рецепт (JSON)', icon: ['code', 'run'], body: h('div', { class: 'form' }, ta, err), actions: [{ l: 'Отмена' }, { l: 'Сохранить', cls: 'primary', fn: async (c, b) => {
              b.classList.add('busy'); try { D[slot] = await api(`scripts/${id}/recipe`, 'PUT', { recipe: ta.value }); c(); toast('Сохранено — утвердите снова'); render(); } catch (x) { err.textContent = x.message; b.classList.remove('busy'); } } }] }); }, '', 'code'))] : null,
      s.chat.length ? [...section('Переписка с Gemini'), h('div', { class: 'list', style: 'margin-bottom:12px' }, s.chat.map(m => h('div', { class: 'li', style: 'align-items:flex-start' },
        h('span', { class: 'avatar', style: 'width:28px;height:28px;font-size:12px' }, m.role === 'user' ? 'Я' : 'G'), h('div', { class: 'grow', style: 'white-space:pre-wrap' }, m.role === 'user' ? m.text : (m.show || '')))))] : null,
      a ? h('div', { class: 'sheet form', style: 'padding:16px;margin-bottom:22px' }, h('textarea', { class: 'inp', rows: '3', 'data-k': 'gchat', placeholder: 'Ответьте Gemini или попросите изменить рецепт', oninput: e => { chat.text = e.target.value; } }),
        h('div', null, btn('Отправить', () => chat.text.trim() && run(() => api(`scripts/${id}/chat`, 'POST', { text: chat.text })), 's primary', null, { disabled: busy }))) : null,
      h('div', { class: 'list' },
        a ? dangerRow('refresh', 'Разобрать заново', 'Переписка и рецепт заменятся новым разбором', btn('Разобрать', () => confirmAct({ title: 'Разобрать заново?', icon: ['zap', 'run'], ok: 'Разобрать', text: 'Переписка и рецепт будут заменены.', fn: async () => analyze() }), 's')) : null,
        dangerRow('trash', 'Удалить скрипт', 'Ноды, уже поставленные им, останутся', btn('Удалить', () => confirmAct({ title: 'Удалить скрипт?', icon: ['trash', 'bad'], ok: 'Удалить', cls: 'danger solid', text: 'Ноды, уже поставленные им, останутся.',
          fn: async () => { await api('scripts/' + id, 'DELETE'); D.scr = null; D[slot] = null; toast('Удалён'); go('settings/scripts'); } }), 's danger'))))];
}

/* ═════════ Отчёты и обращения ═════════ */
const REP_KIND = { auto: ['bad', 'ошибка'], survey: ['warn', 'опрос'], user: ['run', 'сообщение'] };
const repHead = x => x.kind === 'auto' ? `${x.domain || 'задача #' + x.job_id}: ${(x.error || '').slice(0, 90)}` : x.kind === 'survey' ? `Не работает: ${x.domain}` : (x.text || '').replace(/^\[[^\]]+\]\s*/, '').slice(0, 100);
function setReports() {
  const f = UI.repF || 'open', slot = 'rep' + f, R = loader(slot, 'reports?status=' + f);
  if (R.loading) return sk();
  if (R.error) return failBox(slot, R.error);
  const S = R.stats, total = S.done + S.failed, pct = (a, b) => b ? Math.round(a * 100 / b) + '%' : '—';
  return h('div', { style: 'max-width:820px' },
    h('div', { class: 'sheet fleet', style: 'margin-bottom:16px' }, h('div', { class: 'stats' },
      h('div', { class: 'stat' }, h('span', { class: 'k' }, pct(S.done, total)), h('span', { class: 'l' }, 'задач успешно за 30 дней')),
      h('div', { class: 'stat' }, h('span', { class: 'k bad' }, S.failed), h('span', { class: 'l' }, 'ошибок')),
      h('div', { class: 'stat' }, h('span', { class: 'k' }, S.surveys ? pct(S.good, S.surveys) : '—'), h('span', { class: 'l' }, 'довольны по опросу')))),
    S.kinds.length ? h('div', { class: 'list', style: 'margin-bottom:16px' }, S.kinds.map(k => h('div', { class: 'li' }, h('div', { class: 'grow' }, h('div', { class: 't' }, k.title),
      h('div', { class: 's' }, `успешно ${k.done} · ошибок ${k.failed}` + (k.good + k.bad ? ` · опрос: хорошо ${k.good}, плохо ${k.bad}` : ''))),
      k.failed && k.failed / (k.done + k.failed) > 0.2 ? h('span', { class: 'st bad' }, 'часто падает') : null))) : null,
    h('div', { class: 'toolbar' }, seg([{ v: 'open', l: 'Открытые', n: S.open }, { v: '', l: 'Все' }], f, v => { UI.repF = v; D['rep' + v] = null; render(); }, 'Фильтр')),
    R.reports.length ? h('div', { class: 'list' }, R.reports.map(x => h('button', { type: 'button', class: 'li', onclick: () => go('report/' + x.id) },
      h('span', { class: 'avatar' }, (x.who || '?')[0]),
      h('div', { class: 'grow' }, h('div', { class: 't', style: 'white-space:normal' }, repHead(x)), h('div', { class: 's' }, [x.who, x.group, dt(x.ts, true), x.count > 1 ? `повторялось ${x.count} раз` : ''].filter(Boolean).join(' · '))),
      h('span', { class: 'st ' + (x.status === 'done' ? 'ok' : REP_KIND[x.kind][0]) }, x.status === 'done' ? 'решено' : REP_KIND[x.kind][1]), h('span', { class: 'chev' }, ic('chevR')))))
      : empty('check', f === 'open' ? 'Открытых отчётов нет' : 'Отчётов пока нет', 'Ошибки задач и ответы «есть проблема» появятся здесь сами.'));
}
function vReport(id) {
  const slot = 'rd' + id, x = loader(slot, 'reports/' + id);
  const back = h('button', { type: 'button', class: 'back', onclick: () => go('settings/reports') }, ic('chevL'), 'Отчёты');
  if (x.loading) return [back, sk()];
  if (x.error && !x.id) return [back, failBox(slot, x.error)];
  const reply = { text: '' };
  const act = done => async e => busyBtn(e, async () => {
    try { D[slot] = await api(`reports/${id}/resolve`, 'POST', { reply: reply.text, done }); D.repopen = D.rep = null; toast(done ? 'Отмечено решённым' : 'Отправлено'); } catch (y) { toast('Не получилось', y.message, 'bad'); } render(); });
  return [back, pageH('Отчёт #' + id, `${REP_KIND[x.kind][1]} · ${dt(x.ts, true)}`),
    h('div', { style: 'max-width:820px' },
      kvRows([['От кого', x.who], x.group ? ['Группа', x.group] : null, x.domain ? [x.kind === 'survey' ? 'Ноды' : 'Нода', h('span', { class: 'mono' }, x.domain)] : null,
        x.job_id ? ['Задача', h('a', { href: '#/task/' + x.job_id }, '#' + x.job_id + (x.stage ? ', этап «' + x.stage + '»' : ''))] : null]),
      x.error ? h('div', { class: 'notice' }, h('span', { class: 'i' }, ic('alert')), h('div', null, h('b', null, 'Ошибка' + (x.count > 1 ? ` (повторялась ${x.count} раз)` : '')), x.error)) : null,
      x.text ? [...section('От пользователя'), h('div', { class: 'log', style: 'margin-bottom:16px' }, x.text)] : null,
      x.replies.length ? [...section('Ответы'), h('div', { class: 'list', style: 'margin-bottom:16px' }, x.replies.map(r => h('div', { class: 'li' }, h('div', { class: 'grow' }, h('div', { class: 't', style: 'white-space:normal;font-weight:500' }, r.text), h('div', { class: 's' }, dt(r.at, true))))))] : null,
      x.log ? [...section('Журнал задачи'), h('div', { class: 'log', style: 'margin-bottom:16px' }, x.log)] : null,
      h('div', { class: 'sheet form', style: 'padding:16px' }, fld('Ответ', h('textarea', { class: 'inp', rows: '3', 'data-k': 'rreply', placeholder: 'Придёт пользователю в Telegram', oninput: e => { reply.text = e.target.value; } })),
        h('div', { class: 'chips' }, btn(x.status === 'done' ? 'Ответить' : 'Решено' + (x.uid === D.st.me_id ? '' : ' — сообщить'), act(true), 'primary'), btn(x.status === 'done' ? 'Открыть снова' : 'Только ответить', act(false)))))];
}
function setProblem() {
  const M = loader('myrep', 'my/reports', r => r.reports);
  const v = { text: '', pid: groups().length === 1 ? groups()[0].id : null };
  return h('div', { style: 'max-width:680px' },
    h('div', { class: 'sheet form', style: 'padding:16px;margin-bottom:20px' },
      fld('Что случилось?', h('textarea', { class: 'inp', rows: '5', placeholder: 'Какая группа или нода, что вы делали, что видите', oninput: e => { v.text = e.target.value; } }),
        'Ошибки установок и обновлений администратор получает сам — о них писать не нужно.'),
      groups().length > 1 ? fld('Группа', h('select', { class: 'sel', onchange: e => { v.pid = e.target.value ? +e.target.value : null; } }, h('option', { value: '' }, 'Не важно'), groups().map(p => h('option', { value: String(p.id) }, p.name)))) : null,
      h('div', null, btn('Отправить', e => busyBtn(e, async () => { try { const r = await api('reports', 'POST', { text: v.text, pid: v.pid }); toast('Отправлено', r.message); D.myrep = null; render(); } catch (x) { toast('Не получилось', x.message, 'bad'); } }), 'primary'))),
    Array.isArray(M) && M.length ? [...section('Мои обращения'), h('div', { class: 'list' }, M.map(x => h('div', { class: 'li' },
      h('div', { class: 'grow' }, h('div', { class: 't', style: 'white-space:normal' }, repHead(x)),
        h('div', { class: 's', style: 'white-space:normal' }, `#${x.id} · ${dt(x.ts, true)}` + (x.replies.length ? ' · ответ: ' + x.replies[x.replies.length - 1].text.slice(0, 140) : ''))),
      h('span', { class: 'st ' + (x.status === 'done' ? 'ok' : 'warn') }, x.status === 'done' ? 'решено' : 'у администратора'))))] : null);
}

/* ═════════ Журнал действий ═════════ */
function setAudit() {
  const F = UI.audF || (UI.audF = { uid: null, q: '' });
  const qs = before => 'audit?' + [F.uid != null ? 'uid=' + F.uid : '', F.q ? 'q=' + encodeURIComponent(F.q) : '', before ? 'before=' + before : ''].filter(Boolean).join('&');
  const A = loader('aud', qs());
  let t = 0;
  const people = ((A && A.people) || []).filter(p => p.id !== D.st.me_id);  // себя — кнопкой «Я»
  const head = h('div', { class: 'toolbar' },
    seg([{ v: null, l: 'Все' }, { v: D.st.me_id, l: 'Я' }, { v: 0, l: 'Бот' }, ...people.slice(0, 6).map(p => ({ v: p.id, l: p.name }))], F.uid, v => { F.uid = v; D.aud = null; render(); }, 'Кто'),
    h('label', { class: 'field-s' }, ic('search'), h('input', { 'data-k': 'audq', placeholder: 'Нода, домен, пользователь', value: F.q, oninput: e => { clearTimeout(t); t = setTimeout(() => { F.q = e.target.value.trim(); D.aud = null; render(); }, 400); } })));
  if (A.loading) return [head, sk()];
  if (A.error) return [head, failBox('aud', A.error)];
  if (!A.items.length) return [head, empty('activity', 'Пусто', F.uid != null || F.q ? 'По этому фильтру действий нет.' : 'Здесь появятся действия в панели и в боте.')];
  const byDay = new Map();
  A.items.forEach(x => { const d = new Date(x.ts * 1000).toLocaleDateString('ru-RU', { day: 'numeric', month: 'long' }); if (!byDay.has(d)) byDay.set(d, []); byDay.get(d).push(x); });
  return [head, ...[...byDay].map(([d, items]) => [h('div', { class: 'section-h' }, h('h2', null, d)), h('div', { class: 'list', style: 'margin-bottom:18px' }, items.map(x => h('div', { class: 'li' },
    x.uid ? h('span', { class: 'avatar' }, (x.who || '?')[0]) : h('div', { class: 'ic-tile' }, ic('server')),
    h('div', { class: 'grow' }, h('div', { class: 't', style: 'white-space:normal;font-weight:500' }, x.label),
      h('div', { class: 's', style: 'white-space:normal' }, [new Date(x.ts * 1000).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' }), x.who, x.group, x.detail].filter(Boolean).join(' · '))))))]),
    A.more ? btn('Показать раньше', e => busyBtn(e, async () => { try { const r = await api(qs(A.items[A.items.length - 1].id)); A.items = A.items.concat(r.items); A.more = r.more; render(); } catch (x) { toast('Не получилось', x.message, 'bad'); } })) : null];
}

/* ═════════ Нагрузка и защита сервера бота ═════════ */
function setHealth() {
  const Hs = loader('host', 'host');
  const sec = D.st.security || {}, hd = sec.hardened;
  const host = Hs.loading ? sk(200) : Hs.error ? failBox('host', Hs.error) : (() => {
    const n = Hs.now || {}, hist = (Hs.history || []).filter(p => p.cpu != null || p.mem != null);
    const w = 480, ht = 120, t0 = hist.length ? hist[0].t : 0, t1 = hist.length ? hist[hist.length - 1].t : 1;
    const path = k => hist.filter(p => p[k] != null).map((p, i) => (i ? 'L' : 'M') + (((p.t - t0) / Math.max(1, t1 - t0)) * w).toFixed(1) + ' ' + (ht - (p[k] / 100) * ht).toFixed(1)).join('');
    return [h('div', { class: 'notice' + (Hs.level === 'ok' ? ' ok' : ''), style: Hs.level === 'warn' ? 'background:var(--warn-soft)' : '' },
      h('span', { class: 'i' }, ic(Hs.level === 'ok' ? 'check' : 'alert')), h('div', null, h('b', null, Hs.level === 'ok' ? 'VPS подходит' : Hs.level === 'warn' ? 'Сервер на пределе' : 'Серверу не хватает ресурсов'),
        Hs.level === 'ok' ? `Свободно ${gb(n.mem_avail)} памяти и ${gb(n.disk_free)} на диске.` : Hs.why.join('; ') + '. Возьмите VPS помощнее или уберите с него лишнее.')),
      hist.length > 2 ? h('div', { class: 'chart', style: 'margin-bottom:16px' }, h('div', { class: 'chart-h' }, h('b', null, 'За сутки'),
        h('span', { class: 'legend' }, h('span', null, h('i', { style: 'background:var(--air-blue)' }), 'процессор'), h('span', null, h('i', { style: 'background:var(--ink-3)' }), 'память')),
        h('span', { class: 'faint', style: 'margin-left:auto;font-size:12.5px' }, 'шкала 0–100 %')),
        h('div', { html: `<svg viewBox="0 0 ${w} ${ht}" preserveAspectRatio="none" aria-hidden="true"><g class="grid">${[0.5, 60, 119.5].map(y => `<line x1="0" x2="${w}" y1="${y}" y2="${y}" vector-effect="non-scaling-stroke"/>`).join('')}</g>
          <path d="${path('cpu')}" fill="none" stroke="var(--air-blue)" stroke-width="1.6" vector-effect="non-scaling-stroke"/><path d="${path('mem')}" fill="none" stroke="var(--ink-3)" stroke-width="1.6" vector-effect="non-scaling-stroke"/></svg>` })) : null,
      kvRows([['Процессор', `${n.cpu ?? '—'}% · ядер ${n.cores || '—'}` + (n.load ? ' · очередь ' + n.load.map(x => x.toFixed(2)).join(' / ') : '')],
        ['Память', `${n.mem ?? '—'}% · свободно ${gb(n.mem_avail)} из ${gb(n.mem_total)}` + (n.swap ? ` · подкачка ${gb(n.swap)}` : '')],
        ['Диск', `${n.disk ?? '—'}% · свободно ${gb(n.disk_free)} из ${gb(n.disk_total)}`],
        ['Бот', `память ${gb(n.bot_rss)} · процессор ${n.bot_cpu != null ? n.bot_cpu + '%' : '—'} · база ${gb(n.db)}, журналы ${gb(n.logs)}`],
        ['Работает', durH(Hs.bot_uptime) + (n.uptime ? ` · сервер ${durH(n.uptime)}` : '')]])];
  })();
  return h('div', { style: 'max-width:760px' }, ...section('Нагрузка сервера'), host,
    ...section('Защита'),
    !hd ? h('div', { class: 'notice', style: 'background:var(--warn-soft)' }, h('span', { class: 'i', style: 'color:var(--warn)' }, ic('shield')),
      h('div', null, h('b', null, 'Защита сервера не настроена'), 'Выполните на сервере ', h('span', { class: 'mono' }, 'bnodes harden'), ' — файрвол, fail2ban, усиленный SSH, автообновления и сообщения о входах.'))
      : kvRows([['Файрвол и fail2ban', h('span', { class: 'st ok' }, 'включены')], ['Вход по паролю', h('span', { class: 'st ' + (hd.password_login ? 'warn' : 'ok') }, hd.password_login ? 'разрешён' : 'выключен')],
        ['SSH-порт', h('span', { class: 'mono' }, hd.ssh_port)], ['Заблокировано за сутки', String(sec.bans_24h || 0)]]),
    ...section('Входы по SSH'),
    (sec.logins || []).length ? h('div', { class: 'list' }, sec.logins.map(x => h('div', { class: 'li' }, h('div', { class: 'grow' }, h('div', { class: 't mono' }, x.ip), h('div', { class: 's' }, `${x.user} · ${dt(x.ts, true)}`)),
      x.new ? h('span', { class: 'st warn' }, 'новый IP') : null))) : h('div', { class: 'muted' }, hd ? 'Входов пока не было.' : 'Появятся после bnodes harden.'));
}

/* ═════════ Подписка ═════════ */
const rubsK = kop => Math.round((kop || 0) / 100) + ' ₽';
function setTariff() {
  const T = loader('tariff', 'billing/settings');
  if (T.loading) return sk();
  if (T.error) return failBox('tariff', T.error);
  const save = async p => { try { D.tariff = await api('billing/settings', 'POST', p); toast('Сохранено'); } catch (x) { toast('Не получилось', x.message, 'bad'); } render(); };
  const S = T.stats;
  return h('div', { style: 'max-width:680px' },
    h('div', { class: 'list', style: 'margin-bottom:16px' }, switchRow('Платный доступ', T.on ? 'пользователи платят после бесплатного срока' : 'выключен — все пользуются бесплатно', T.on, v => save({ on: v }))),
    h('div', { class: 'sheet form', style: 'padding:16px;margin-bottom:16px' }, h('div', { class: 'row2' },
      fld('Цена, ₽', numIn(T.price_rub, x => save({ price_rub: x }))), fld('Период, дней', numIn(T.period_days, x => save({ period_days: Math.max(1, x) }))),
      fld('Напоминать за, дней', numIn(T.remind_days, x => save({ remind_days: Math.max(1, x) }))), fld('Бесплатно новым, дней', numIn(T.trial_days, x => save({ trial_days: x })), '0 — бессрочно')),
      h('div', { class: 'hint' }, 'Скидку и «бесплатно навсегда» ставьте у пользователя. Оплата — с баланса Bobr Cloud пользователя.')),
    T.on ? h('div', { class: 'sheet fleet' }, h('div', { class: 'stats' }, h('div', { class: 'stat' }, h('span', { class: 'k' }, S.active), h('span', { class: 'l' }, 'платят')),
      h('div', { class: 'stat' }, h('span', { class: 'k warn' }, S.paused), h('span', { class: 'l' }, 'на паузе')), h('div', { class: 'stat' }, h('span', { class: 'k' }, rubsK(S.month_kop)), h('span', { class: 'l' }, 'за 30 дней')))) : null);
}
async function billPay() {
  try { const r = await api('billing/pay', 'POST', {}); toast('Продлено', r.message); D.sub = null; await loadAll(); } catch (x) { toast('Не получилось', x.message, 'bad'); }
}
function setSubscription() {
  const B = loader('sub', 'billing');
  if (B.loading) return sk();
  if (B.error) return failBox('sub', B.error);
  return h('div', { style: 'max-width:680px' },
    kvRows([['Доступ', B.state === 'paused' ? h('span', { class: 'st bad' }, 'на паузе') : 'до ' + dt(B.until) + (B.days_left != null ? ` · осталось ${pl(B.days_left, 'день', 'дня', 'дней')}` : '')],
      ['Стоимость', `${rubsK(B.price_kop)} за ${B.period_days} дн.` + (B.discount ? ` · скидка ${B.discount}%` : '')],
      ['Баланс Bobr Cloud', B.balance_kop != null ? rubsK(B.balance_kop) : '—']]),
    h('div', { class: 'list', style: 'margin-bottom:16px' }, switchRow('Автопродление', 'В день окончания — с баланса Bobr Cloud. Не хватит денег — доступ встанет на паузу, ноды продолжат работать', B.autorenew,
      async v => { try { D.sub = { ...B, ...(await api('billing/autorenew', 'POST', { value: v })) }; toast('Сохранено'); } catch (x) { toast('Не получилось', x.message, 'bad'); } render(); })),
    h('div', { class: 'chips', style: 'margin-bottom:20px' }, btn(`Продлить сейчас за ${rubsK(B.price_kop)}`, e => busyBtn(e, billPay), 'primary'), btn('Пополнить баланс', topUp, '', 'wallet')),
    B.payments.length ? [...section('Оплаты'), h('div', { class: 'list' }, B.payments.map(p => h('div', { class: 'li' }, h('div', { class: 'grow' }, h('div', { class: 't' }, rubsK(p.kop)),
      h('div', { class: 's' }, `${dt(p.at)} · ${p.days} дн. · ${p.how === 'auto' ? 'автопродление' : 'вручную'}`)))))] : null);
}
function paywall() {
  const B = D.st.billing || {};
  return [pageH('Доступ на паузе'),
    h('div', { style: 'max-width:640px' },
      h('div', { class: 'notice', style: 'background:var(--warn-soft)' }, h('span', { class: 'i', style: 'color:var(--warn)' }, ic('clock')),
        h('div', null, h('b', null, 'Срок закончился ' + dt(B.until)), 'Ваши ноды работают как прежде, но бот ими сейчас не управляет: нет мониторинга, починки и уведомлений. Продлите — всё вернётся сразу.')),
      kvRows([['Подписка', `${rubsK(B.price_kop)} за ${B.period_days} дн.` + (B.discount ? ` · скидка ${B.discount}%` : '')], ['Оплата', 'с баланса Bobr Cloud — того же, с которого покупаются серверы']]),
      h('div', { class: 'chips' }, btn(`Продлить за ${rubsK(B.price_kop)}`, e => busyBtn(e, billPay), 'primary'), btn('Пополнить баланс', () => { if (!D.wallet) api('wallet').then(w => { D.wallet = w; topUp(); }).catch(x => toast('Bobr Cloud не ответил', x.message, 'bad')); else topUp(); }, '', 'wallet')))];
}
/* доступ пользователя: подписка, скидка, бесплатно — у администратора в «Пользователях» */
function userAccess(u) {
  const B = u.billing || {};
  const act = async (body, msg) => { try { await api(`users/${u.tg_id}/access`, 'POST', body); toast(msg || 'Сохранено'); D.users = null; close(); render(); } catch (x) { toast('Не получилось', x.message, 'bad'); } };
  const st = !B.on ? 'платный режим выключен' : B.free ? 'бесплатно навсегда' : !B.until ? 'бессрочно' : B.state === 'paused' ? 'на паузе с ' + dt(B.until) : 'до ' + dt(B.until);
  const close = modal({ title: 'Доступ: ' + u.label, icon: ['users', 'run'], body: h('div', { class: 'form' },
    kvRows([['Сейчас', st], B.on && !B.free ? ['Цена для него', `${rubsK(B.price_kop)} / ${B.period_days} дн.`] : null]),
    fld('Продлить бесплатно', h('div', { class: 'chips' }, [7, 30, 90].map(d => btn('+' + d + ' дн.', () => act({ add_days: d }, `+${d} дн.`), 's')), btn('Бессрочно', () => act({ until: null }, 'Бессрочно'), 's'))),
    fld('Скидка, %', numIn(B.discount || 0, x => act({ discount: Math.min(100, x) }))),
    h('div', { class: 'list', style: 'background:var(--sheet-2)' }, switchRow('Бесплатно навсегда', null, !!B.free, v => act({ free: v }, v ? 'Бесплатно' : 'По тарифу'))),
    (B.payments || []).length ? fld('Оплаты', h('div', { class: 'muted' }, B.payments.slice(0, 5).map(p => `${rubsK(p.kop)} · ${dt(p.at)}`).join('; '))) : null) });
}
