'use strict';
/* Новый интерфейс: перенос хостов между панелями и переезд бота на другой сервер. */

/* ═════════ Перенос хостов ═════════ */
// метаданные панели группы (хосты, сквады) — в T.srcMeta / T.dstMeta; T.src / T.dst — выбранные группы
function txMeta(T, pid, key) {
  if (!pid || pid === 'json' || T[key + 'Pid'] === pid) return;
  T[key + 'Pid'] = pid; T[key] = null;
  api('profiles/' + pid + '/panel').then(m => { if (T[key + 'Pid'] === pid) { T[key] = m; if (key === 'dstMeta') T.squads = new Set(m.default_squads); render(true); } })
    .catch(e => { if (T[key + 'Pid'] === pid) { T[key] = { error: e.message, hosts: [], squads: [] }; render(true); } });
}
function setTransfer() {
  const gs = groups();
  if (!gs.length) return empty('layers', 'Групп нет', 'Перенос работает между панелями ваших групп.');
  const T = UI.tx || (UI.tx = { src: gs[0].id, dst: (gs[1] || gs[0]).id, hosts: new Set(), squads: new Set(), mode: 'copy', json: '', tname: '', filter: '' });
  txMeta(T, T.src, 'srcMeta'); txMeta(T, T.dst, 'dstMeta');
  let parsed = null; try { parsed = JSON.parse(T.json); } catch (e) { /* ещё не JSON */ }
  const kind = parsed && parsed.kind === 'bobr-nodes/hosts' ? 'bundle' : parsed && typeof parsed === 'object' ? 'template' : null;
  const sheet = (...k) => h('div', { class: 'sheet form', style: 'padding:16px;margin-bottom:16px' }, ...k);
  let srcBox;
  if (T.src === 'json') srcBox = sheet(
    fld('JSON', h('textarea', { class: 'inp mono', rows: '8', 'data-k': 'txjson', spellcheck: 'false', placeholder: 'Набор хостов из Bobr Nodes («Скачать набор JSON») или Xray JSON-шаблон', oninput: e => { T.json = e.target.value; } }, T.json),
      kind === 'bundle' ? `Набор: хостов ${(parsed.hosts || []).length}, профилей ${(parsed.profiles || []).length}, шаблонов ${(parsed.templates || []).length}.`
        : kind === 'template' ? 'Это Xray JSON — бот создаст или обновит шаблон с этим именем.' : 'Набор переносит хосты вместе с их профилями и Xray JSON-шаблонами.'),
    h('div', null, btn('Разобрать', () => render(), 's')),
    kind === 'template' ? fld('Имя шаблона', h('input', { class: 'inp', value: T.tname, placeholder: 'например Arenda5', oninput: e => { T.tname = e.target.value; } })) : null);
  else {
    const M = T.srcMeta;
    if (!M) srcBox = sk(200);
    else if (M.error) srcBox = h('div', { class: 'notice' }, h('span', { class: 'i' }, ic('alert')), h('div', null, h('b', null, 'Панель недоступна'), M.error));
    else {
      const q = T.filter.trim().toLowerCase(), items = M.hosts.filter(x => !q || [x.remark, x.address, x.profile, x.template].join(' ').toLowerCase().includes(q));
      srcBox = [h('div', { class: 'toolbar' }, h('label', { class: 'field-s' }, ic('search'), h('input', { 'data-k': 'txq', placeholder: 'Название, адрес, профиль, шаблон', value: T.filter, oninput: e => { T.filter = e.target.value; render(); } })),
          h('span', { class: 'muted' }, T.hosts.size ? `выбрано ${T.hosts.size} из ${M.hosts.length}` : pl(M.hosts.length, 'хост', 'хоста', 'хостов')),
          btn(items.every(x => T.hosts.has(x.uuid)) && items.length ? 'Снять все' : 'Выбрать все', () => { const all = items.every(x => T.hosts.has(x.uuid)); items.forEach(x => all ? T.hosts.delete(x.uuid) : T.hosts.add(x.uuid)); render(); }, 's')),
        h('div', { class: 'list', style: 'max-height:50vh;overflow:auto;margin-bottom:10px' }, items.length ? items.map(x => h('label', { class: 'switch' },
          h('input', { type: 'checkbox', class: 'cb', checked: T.hosts.has(x.uuid), onchange: e => { e.target.checked ? T.hosts.add(x.uuid) : T.hosts.delete(x.uuid); render(); } }),
          h('span', { class: 'grow' }, h('b', null, x.remark || x.address), h('span', null, [`${x.address}:${x.port}`, x.profile, x.template ? 'шаблон ' + x.template : '', x.balancer ? 'балансировщик' : ''].filter(Boolean).join(' · ')))))
          : h('div', { class: 'empty' }, 'Ничего не нашлось')),
        h('div', { class: 'chips', style: 'margin-bottom:16px' }, btn('Скачать набор JSON', e => busyBtn(e, async () => {
          if (!T.hosts.size) return toast('Выберите хосты', null, 'info');
          try { T.exported = JSON.stringify(await api('hosts/export', 'POST', { pid: T.src, hosts: [...T.hosts] }), null, 2); render(); } catch (x) { toast('Не получилось', x.message, 'bad'); }
        }), 's', 'box')),
        T.exported ? sheet(fld('Набор JSON', h('textarea', { class: 'inp mono', rows: '6', readonly: true }, T.exported), 'Его можно вставить в любом Bobr Nodes: «Откуда → Вставить JSON».'),
          h('div', null, btn('Скопировать', () => copy(T.exported, 'Набор'), 's', 'copy'))) : null];
    }
  }
  const dm = T.dstMeta, sq = dm && !dm.error ? dm.squads : [];
  const go = e => busyBtn(e, async () => {
    const body = { pid: T.dst, squads: [...T.squads], template_mode: T.mode };
    if (T.src === 'json') {
      if (!parsed) return toast('Это не JSON', null, 'bad');
      if (kind === 'bundle') body.bundle = parsed; else { body.template_json = parsed; body.template_name = T.tname; }
    } else { if (!T.hosts.size) return toast('Выберите хосты', null, 'info'); body.from_pid = T.src; body.hosts = [...T.hosts]; }
    try { T.result = await api('hosts/import', 'POST', body); T.dstMetaPid = null; haptic(T.result.ok ? 'ok' : 'bad'); } catch (x) { T.result = { ok: false, error: x.message, log: [] }; }
    render();
  });
  const r = T.result, dstName = (groups().find(p => p.id === T.dst) || {}).name || '';
  return h('div', { style: 'max-width:820px' },
    h('p', { class: 'muted', style: 'margin:0 0 14px' }, 'Хосты переносятся вместе с профилями и Xray JSON-шаблонами. Хост с тем же названием, адресом и портом обновляется, а не дублируется.'),
    fld('Откуда', h('div', { class: 'chips', style: 'margin-bottom:12px' }, [...gs.map(p => ({ v: p.id, l: p.name })), { v: 'json', l: 'Вставить JSON' }].map(o =>
      h('button', { type: 'button', class: 'chip', 'aria-pressed': String(T.src === o.v), onclick: () => { T.src = o.v; T.hosts = new Set(); T.result = null; T.exported = null; render(); } }, o.l)))),
    srcBox,
    fld('Куда', h('div', { class: 'chips', style: 'margin-bottom:12px' }, gs.map(p => h('button', { type: 'button', class: 'chip', 'aria-pressed': String(T.dst === p.id), onclick: () => { T.dst = p.id; T.result = null; render(); } }, p.name)))),
    sheet(fld('Сквады', !dm ? sk(60) : dm.error ? h('div', { class: 'err' }, 'Панель недоступна: ' + dm.error) : sq.length ? h('div', { class: 'chips' }, sq.map(s =>
        h('button', { type: 'button', class: 'chip', 'aria-pressed': String(T.squads.has(s.uuid)), onclick: () => { T.squads.has(s.uuid) ? T.squads.delete(s.uuid) : T.squads.add(s.uuid); render(); } }, s.name))) : h('div', { class: 'muted' }, 'В панели нет сквадов'),
        'Inbound перенесённых хостов добавится в отмеченные сквады — иначе пользователи хосты не увидят.'),
      fld('Если шаблон уже есть', seg([{ v: 'copy', l: 'Создать копию' }, { v: 'update', l: 'Обновить его' }], T.mode, v => { T.mode = v; render(); }, 'Шаблон'),
        '«Обновить» меняет роутинг у всех хостов с этим шаблоном. Профили не перезаписываются: другой конфиг — копия с новыми тегами.')),
    btn('Перенести', go, 'primary'),
    r ? h('div', { style: 'margin-top:16px' }, h('div', { class: 'notice' + (r.ok ? ' ok' : '') }, h('span', { class: 'i' }, ic(r.ok ? 'check' : 'alert')),
        h('div', null, h('b', null, r.ok ? `Перенесено в «${dstName}»` : 'Не перенесено'),
          r.ok ? `Хостов создано: ${r.created}, обновлено: ${r.updated}. Профилей: ${r.profiles}, шаблонов: ${r.templates}.` : r.error + (r.log.length ? ' Ниже — что успело выполниться.' : ' В панели ничего не изменилось.'))),
      r.log.length ? h('div', { class: 'log' }, r.log.join('\n')) : null) : null);
}

/* ═════════ Переезд бота ═════════ */
function setMigrate() {
  const M = loader('mig', 'migrate');
  if (M.loading) return sk();
  if (M.error && !M.status) return failBox('mig', M.error);
  const F = UI.migf || (UI.migf = { ip: '', port: '22', password: '', key_text: '', showKey: false, bcloud_url: '', dns: true });
  if (['running', 'done'].includes(M.status) || (M.status === 'failed' && M.log.length)) {
    if (M.status === 'running') setTimeout(() => { if (parse().arg === 'migrate') { D.mig = null; render(true); } }, 2000);
    const cur = M.stages.findIndex(x => x.code === M.stage);
    return h('div', { style: 'max-width:720px' },
      h('div', { class: 'list', style: 'margin-bottom:14px' }, M.stages.map((x, i) => { const k = M.status === 'done' || i < cur ? 'done' : i === cur ? (M.status === 'failed' ? 'fail' : 'now') : 'wait';
        return h('div', { class: 'li' }, h('span', { class: 'step-dot ' + k }, k === 'done' ? ic('check') : k === 'fail' ? ic('x') : i + 1), h('div', { class: 'grow' }, h('div', { class: 't', style: k === 'wait' ? 'color:var(--ink-3);font-weight:500' : '' }, x.title)),
          k === 'now' ? h('span', { class: 'st run' }, 'идёт') : null); })),
      M.status === 'done' ? h('div', { class: 'notice ok' }, h('span', { class: 'i' }, ic('check')), h('div', null, h('b', null, 'Готово'),
        `Бот работает на ${M.target.ip}. Через минуту закройте и откройте приложение заново — оно откроется уже с нового сервера. Старый сервер остановлен, данные на нём остались: удалите его, когда убедитесь, что всё работает.`)) : null,
      M.status === 'failed' ? [h('div', { class: 'notice' }, h('span', { class: 'i' }, ic('alert')), h('div', null, h('b', null, 'Переезд не удался'), M.error + '. Бот остался на этом сервере и работает как раньше.')),
        btn('Попробовать снова', () => { D.mig = { ...M, status: 'idle', log: [] }; render(); })] : null,
      h('div', { class: 'section-h', style: 'margin-top:16px' }, h('h2', null, 'Журнал')), h('div', { class: 'log' }, M.log.slice(-14).join('\n')));
  }
  const body = () => ({ ip: F.ip, port: Number(F.port) || 22, password: F.password, key_text: F.showKey ? F.key_text : '', bcloud_url: F.bcloud_url, dns: F.dns });
  const checked = M.facts && M.facts.OS && M.target && M.target.ip === F.ip;
  const ready = M.status === 'ready' && M.target && M.target.ip === F.ip && !M.bad.length;
  const f = M.facts || {};
  return h('div', { style: 'max-width:720px' },
    h('p', { class: 'muted', style: 'margin:0 0 14px' }, 'Переносится всё: база (группы, ноды, задачи, пользователи, история), SSH-ключи бота, журналы, оформление и настройки. Ноды и чекеры трогать не нужно — ключ бота тот же.'),
    h('div', { class: 'sheet form', style: 'padding:16px;margin-bottom:14px' },
      h('div', { class: 'row2' }, fld('IP нового сервера', h('input', { class: 'inp mono', 'data-k': 'migip', value: F.ip, placeholder: '203.0.113.10', oninput: e => { F.ip = e.target.value.trim(); } })),
        fld('SSH-порт', h('input', { class: 'inp mono', inputmode: 'numeric', value: F.port, oninput: e => { F.port = e.target.value.trim(); } }))),
      fld('Пароль root', h('input', { class: 'inp', type: 'password', autocomplete: 'new-password', 'data-k': 'migpw', value: F.password, oninput: e => { F.password = e.target.value; } }),
        'Чистый VPS с Ubuntu или Debian. Пароль нужен один раз — бот добавит свой ключ, в базу пароль не сохраняется.'),
      h('div', { class: 'list', style: 'background:var(--sheet-2)' }, switchRow('Вход по SSH-ключу', 'если на сервере вход только по ключу', F.showKey, v => { F.showKey = v; render(); })),
      F.showKey ? fld('Закрытый ключ', h('textarea', { class: 'inp mono', rows: '4', 'data-k': 'migkey', oninput: e => { F.key_text = e.target.value; } }, F.key_text)) : null,
      M.bcloud_internal ? fld('Публичный адрес Bobr Cloud', h('input', { class: 'inp mono', value: F.bcloud_url, placeholder: 'https://cloud.example.com', oninput: e => { F.bcloud_url = e.target.value.trim(); } }),
        'Сейчас бот на одном сервере с Bobr Cloud и ходит к нему напрямую. С нового сервера — по этому адресу.') : null,
      M.cf ? h('div', { class: 'list', style: 'background:var(--sheet-2)' }, switchRow(`Переключить ${M.domain || 'домен'} на новый IP`, 'A-запись в Cloudflare', F.dns, v => { F.dns = v; }))
        : h('div', { class: 'hint' }, `Cloudflare не подключён — A-запись ${M.domain || 'домена'} поменяете сами после переезда.`)),
    checked ? h('div', { class: 'notice' + (M.bad.length ? '' : ' ok') }, h('span', { class: 'i' }, ic(M.bad.length ? 'alert' : 'check')),
      h('div', null, h('b', null, M.bad.length ? 'Сюда переехать нельзя' : 'Сервер подходит'),
        [`${f.OS} ${f.VER || ''}`.trim(), f.MEM ? `${Math.round(f.MEM / 102.4) / 10} ГБ памяти` : '', f.DISK ? `${f.DISK} ГБ свободно` : '', f.DOCKER ? 'Docker есть' : 'Docker поставлю'].filter(Boolean).join(' · '),
        [...M.bad, ...M.warn].map(x => h('div', { style: 'margin-top:4px' }, '· ' + x)))) : null,
    M.busy_jobs ? h('p', { class: 'muted' }, `Идут задачи (${M.busy_jobs}) — переезд станет доступен, когда они закончатся.`) : null,
    h('div', { class: 'chips' },
      btn('Проверить сервер', e => busyBtn(e, async () => { try { D.mig = await api('migrate/check', 'POST', body()); } catch (x) { toast('Не получилось', x.message, 'bad'); } render(); }), '', 'shield'),
      btn('Переехать', () => confirmAct({ title: `Переехать на ${F.ip}?`, icon: ['server', 'warn'], ok: 'Переехать',
        text: 'Бот остановится на 2–5 минут: база скопируется, бот запустится на новом сервере' + (M.cf && F.dns ? ', домен переключится на новый IP' : '') + '. Если что-то пойдёт не так, всё вернётся как было.',
        fn: async () => { D.mig = await api('migrate/start', 'POST', body()); F.password = ''; F.key_text = ''; render(); } }), 'primary', null, { disabled: !ready || M.busy_jobs > 0 })));
}

/* ═════════ Резервные копии ═════════ */
function setBackup() {
  const B = loader('bk', 'backup');
  if (B.loading) return sk();
  if (B.error && B.on === undefined) return failBox('bk', B.error);
  const save = async p => { try { D.bk = await api('backup', 'POST', { ...p, tz: tzNow() }); D.su = null; toast('Сохранено'); } catch (x) { toast('Не получилось', x.message, 'bad'); } render(); };
  const key = UI.bkKey;
  return h('div', { style: 'max-width:720px' },
    h('p', { class: 'muted', style: 'margin:0 0 14px' }, 'Зашифрованная копия базы — каждый день вам в Telegram. Восстанавливается всё: группы, ноды, пользователи и подписки, задачи, SSH-ключи, настройки.'),
    ...section('Пароль восстановления'),
    key ? h('div', { class: 'sheet', style: 'padding:16px;margin-bottom:14px' }, h('div', { class: 'mono', style: 'font-size:20px;font-weight:600;letter-spacing:.04em;word-break:break-all;margin-bottom:12px' }, key),
        h('div', { class: 'chips' }, btn('Скопировать', () => copy(key, 'Пароль'), 's', 'copy'),
          B.key_saved ? btn('Скрыть', () => { UI.bkKey = null; render(); }, 's') : btn('Я сохранил', async () => { await save({ key_saved: true }); UI.bkKey = null; render(); }, 's primary')))
      : h('div', { class: 'list', style: 'margin-bottom:14px' }, dangerRow('key', B.key_saved ? 'Сохранён у вас' : 'Ещё не сохранён', B.key_saved ? 'Показать снова можно в любой момент' : 'Без него копию не открыть — сохраните в менеджер паролей',
        btn('Показать', e => busyBtn(e, async () => { try { UI.bkKey = (await api('backup/key')).key; } catch (x) { toast('Не получилось', x.message, 'bad'); } render(); }), 's ' + (B.key_saved ? '' : 'primary')))),
    h('p', { class: 'muted', style: 'margin:-6px 0 20px;font-size:13px' }, 'В Telegram пароль не отправляется: тот, у кого окажется файл копии, без пароля её не откроет.'),
    ...section('Расписание'),
    h('div', { class: 'list', style: 'margin-bottom:10px' }, switchRow('Каждый день', 'Файл придёт без звука', B.on, v => save({ on: v }))),
    B.on ? h('div', { class: 'sheet form', style: 'padding:16px;margin-bottom:10px' }, h('div', { class: 'row2' }, fld('Время', hourSel(B.hour, x => save({ hour: x }))),
      fld('Хранить на сервере, копий', numIn(B.keep, x => save({ keep: Math.min(30, Math.max(1, x)) }))))) : null,
    h('p', { class: 'muted', style: 'margin:0 0 12px;font-size:13px' }, B.last_at ? `Последняя копия — ${dt(B.last_at, true)}` + (B.last_size ? `, ${gb(B.last_size)}` : '') + '.' : 'Копий ещё не было.'),
    B.last_error ? h('div', { class: 'notice' }, h('span', { class: 'i' }, ic('alert')), h('div', null, h('b', null, 'Последняя копия не удалась'), B.last_error)) : null,
    h('div', { class: 'chips', style: 'margin-bottom:22px' }, btn('Сделать копию сейчас', e => busyBtn(e, async () => {
      try { D.bk = await api('backup/now', 'POST'); const r = D.bk.result; toast(r && r.ok ? 'Копия отправлена вам в Telegram' : (r && r.error) || 'Готово', null, r && r.ok === false ? 'bad' : 'ok'); } catch (x) { toast('Не получилось', x.message, 'bad'); } render(); }), 'primary', 'box')),
    ...section('Как восстановить'),
    h('ol', { class: 'sheet', style: 'padding:14px 16px 14px 34px;margin:0 0 22px;line-height:1.6' },
      h('li', null, 'Поставьте бота на новый сервер обычной установкой — тот же токен бота и ваш Telegram ID.'),
      h('li', null, 'Перешлите боту файл последней копии (.bnb) из этого чата.'),
      h('li', null, 'Пришлите пароль — бот покажет, что в копии, и после «Восстановить» перезапустится с ней.')),
    h('div', { class: 'list' }, dangerRow('refresh', 'Новый пароль', 'Новые копии — под новым паролем, старые открываются только старым', btn('Сменить', () => confirmAct({ title: 'Сменить пароль?', icon: ['key', 'warn'], ok: 'Сменить', cls: 'danger solid',
      text: 'Новые копии будут под новым паролем. Старые копии откроются только старым — сохраните оба, пока старые нужны.',
      fn: async () => { const r = await api('backup/key', 'POST'); UI.bkKey = r.key; D.bk = r; render(); } }), 's danger'))));
}

/* ручная смена IP: нода куплена не через бота — новый адрес указывают сами */
function manualIp(n, d) {
  const v = { ip: '' }, err = h('div', { class: 'err', role: 'alert' });
  modal({ title: `Новый IP для ${n.name}`, icon: ['globe', 'run'], body: h('div', { class: 'form' },
    h('div', null, 'Укажите адрес, который выдал хостер. Бот обновит DNS-запись и адрес в панели и проверит вход по SSH.'),
    fld('Новый IP', h('input', { class: 'inp mono', inputmode: 'decimal', placeholder: '203.0.113.10', oninput: e => { v.ip = e.target.value.trim(); } }), d && d.ip ? `Сейчас ${d.ip}` : null), err),
    actions: [{ l: 'Отмена' }, { l: 'Сменить', cls: 'primary', fn: async (c, b) => {
      b.classList.add('busy'); err.textContent = '';
      try { const r = await api(`profiles/${n.pid}/nodes/${n.uuid}/ip`, 'POST', { mode: 'manual', ip: v.ip }); c(); jobToast(`${n.name}: смена IP началась`, r.id); refreshSoon(); }
      catch (x) { err.textContent = x.message; b.classList.remove('busy'); }
    } }] });
}
