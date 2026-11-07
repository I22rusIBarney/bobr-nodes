'use strict';
/* Новый интерфейс: настройки ноды и SSH, чекеры в России, пополнение баланса, обновление бота и резервные копии. */

/* ═════════ Настройки ноды ═════════ */
const EXTRAS = [['psiphon', 'Psiphon', 'Запасной выход, когда сайты блокируют IP хостера'], ['warp', 'WARP', 'Выход через Cloudflare для сервисов, которые не любят IP дата-центров'],
  ['xhttp', 'Запасной XHTTP', 'Второй способ подключения через CDN, если основной режут']];
function nodeSettings(n, det) {
  const d = det.d;
  if (!d) return det.err ? empty('alert', 'Не загрузилось', det.err) : h('div', { class: 'sk', style: 'height:280px' });
  const st = UI.ns && UI.ns.id === n.id ? UI.ns : (UI.ns = { id: n.id, add: {}, port: '', mask: null, ssh: { user: d.ssh.user, port: String(d.ssh.port), key: d.ssh.key, password: '', key_text: '', sudo_password: '' } });
  const has = new Set(d.features || []), adds = Object.keys(st.add).filter(k => st.add[k]);
  const changes = adds.length + (st.port ? 1 : 0) + (st.mask != null ? 1 : 0);
  const apply = () => confirmAct({ title: `Применить к ${n.name}?`, icon: ['sliders', 'run'], ok: 'Применить',
    text: h('div', null, h('ul', { style: 'margin:0 0 10px;padding-left:18px;color:var(--ink)' },
      adds.map(k => h('li', null, 'Добавить ' + EXTRAS.find(x => x[0] === k)[1])), st.port ? h('li', null, `Порт для клиентов: ${st.port}`) : null,
      st.mask != null ? h('li', null, 'Final Mask: ' + (st.mask ? 'включить' : 'выключить')) : null),
      'Бот зайдёт на сервер по SSH и перезапустит ноду — клиенты переподключатся за несколько секунд.'),
    fn: async () => {
      const r = await api(`profiles/${n.pid}/nodes/${n.uuid}/apply`, 'POST', { port: st.port || undefined, final_mask: st.mask == null ? undefined : st.mask,
        psiphon: st.add.psiphon ? { enabled: true, region: '', domains: '' } : undefined, warp: st.add.warp ? { enabled: true, domains: '' } : undefined,
        xhttp: st.add.xhttp ? { enabled: true, mode: 'both' } : undefined });
      UI.ns = null; jobToast(`${n.name}: настройки применяются`, r.id); refreshSoon();
    } });
  const hasFM = (d.hosts || []).some(x => x.final_mask);
  return [
    h('div', { class: 'section-h' }, h('h2', null, 'Дополнения')),
    h('div', { class: 'list', style: 'margin-bottom:20px' }, EXTRAS.map(([k, t, s]) => has.has(k)
      ? h('div', { class: 'switch' }, h('span', { class: 'grow' }, h('b', null, t), h('span', null, s)), h('span', { class: 'st ok' }, 'стоит'))
      : switchRow(t, s, !!st.add[k], v => { st.add[k] = v; render(); }))),
    h('div', { class: 'section-h' }, h('h2', null, 'Подключение')),
    h('div', { class: 'sheet form', style: 'padding:16px;margin-bottom:12px' },
      fld('Порт для клиентов', h('input', { class: 'inp mono', 'data-k': 'nport', inputmode: 'numeric', style: 'max-width:160px', placeholder: String(d.inbound_port || 443), value: st.port,
        oninput: e => { st.port = e.target.value.trim(); render(); } }), `Сейчас ${d.inbound_port || 443}. Пусто — не менять, 443 — вернуть стандартный.`),
      fld('Final Mask', seg([{ v: null, l: 'Не менять' }, { v: true, l: 'Включить' }, { v: false, l: 'Выключить' }], st.mask, v => { st.mask = v; render(); }, 'Final Mask'),
        `Сейчас ${hasFM ? 'включён' : 'выключен'}. Маскирует начало соединения — помогает, когда провайдер режет по шаблону.`)),
    h('div', { class: 'apply-bar', style: 'margin-bottom:24px' }, h('span', { class: 'muted' }, changes ? pl(changes, 'изменение', 'изменения', 'изменений') : 'Изменений нет'),
      h('span', { class: 'sp' }), changes ? btn('Сбросить', () => { UI.ns = null; render(); }, 's') : null, btn('Применить', apply, 's primary', null, { disabled: !changes })),
    nodeSsh(n, d, st.ssh),
    h('div', { class: 'list', style: 'margin-top:20px' }, dangerRow('route', 'Вход через Россию', 'Пока в классическом интерфейсе', btn('Открыть', () => classic(`node:${n.pid}:${n.uuid}`), 's', 'ext'))),
  ];
}
function nodeSsh(n, d, s) {
  const KEYS = [{ v: '', l: 'Без ключа' }, { v: 'own', l: 'Свой ключ' }, ...(isAdmin() || d.ssh.key === 'bot' ? [{ v: 'bot', l: 'Ключ бота' }] : [])];
  const save = e => busyBtn(e, async () => {
    try {
      const r = await api(`profiles/${n.pid}/nodes/${n.uuid}/ssh`, 'POST', { ...s, ip: d.ip || n.address });
      toast(r.ok ? 'Доступ сохранён' : 'Сохранено, но вход не удался', r.message, r.ok ? 'ok' : 'bad');
      s.password = s.key_text = s.sudo_password = ''; D.detail[n.id] = null; render();
    } catch (x) { toast('Не получилось', x.message, 'bad'); }
  });
  const changed = !!(s.password || s.key_text || s.sudo_password) || s.key !== d.ssh.key || s.user !== d.ssh.user || s.port !== String(d.ssh.port);
  return [h('div', { class: 'section-h' }, h('h2', null, 'Доступ SSH'), h('span', { class: 'faint', style: 'font-size:13px' }, d.ssh.summary)),
    d.ssh.source === 'bot' ? h('p', { class: 'muted', style: 'margin:-4px 0 12px' }, 'Сервер купил бот — он заходит паролем из покупки. Менять доступ нужно, только если вы сменили пароль сами.') : null,
    h('div', { class: 'sheet form', style: 'padding:16px' },
      h('div', { class: 'row2' },
        fld('Пользователь', h('input', { class: 'inp mono', 'data-k': 'su', value: s.user, oninput: e => { s.user = e.target.value.trim(); render(); } })),
        fld('Порт', h('input', { class: 'inp mono', 'data-k': 'sp', inputmode: 'numeric', value: s.port, oninput: e => { s.port = e.target.value.trim(); render(); } }))),
      fld('Пароль', h('input', { class: 'inp', type: 'password', autocomplete: 'off', 'data-k': 'spw', value: s.password, placeholder: d.ssh.has_password ? 'сохранён · введите новый, чтобы заменить' : '', oninput: e => { s.password = e.target.value; render(); } }),
        d.ssh.has_password ? '«-» — убрать сохранённый пароль.' : null),
      fld('Ключ', seg(KEYS, s.key, v => { s.key = v; render(); }, 'Ключ'), s.key === 'bot' ? 'Бот заходит своим ключом. В группах приглашённых пользователей — только на серверы, которые бот сам купил или поставил.' : null),
      s.key === 'own' ? fld('Закрытый ключ', h('textarea', { class: 'inp mono', 'data-k': 'skey', rows: '4', placeholder: d.ssh.has_key ? 'сохранён · вставьте новый, чтобы заменить' : '-----BEGIN OPENSSH PRIVATE KEY-----',
        oninput: e => { s.key_text = e.target.value; render(); } }, s.key_text)) : null,
      s.user !== 'root' ? fld('Пароль sudo', h('input', { class: 'inp', type: 'password', autocomplete: 'off', 'data-k': 'ssudo', value: s.sudo_password, placeholder: d.ssh.has_sudo_password ? 'сохранён' : 'если sudo спрашивает пароль',
        oninput: e => { s.sudo_password = e.target.value; render(); } })) : null,
      h('div', { class: 'chips' }, btn('Сохранить и проверить вход', save, 'primary', null, { disabled: !changed, title: changed ? null : 'Нет изменений' }), btn('Скопировать команду', () => copy(`ssh -p ${s.port || 22} ${s.user}@${d.ip || n.address}`, 'Команда'), '', 'terminal')))];
}

/* ═════════ Чекеры в России ═════════ */
const CK_ST = { running: ['ok', 'работает'], idle: ['ok', 'ждёт подписку'], installing: ['run', 'ставится'], new: ['run', 'ставится'], failed: ['bad', 'ошибка'] };
function setCheckers() {
  if (!D.mon) return empty('radar', 'Мониторинг выключен', 'На этом боте нет проверки из России.');
  const cs = D.mon.checkers || [], R = D.mon.regions || {};
  if (cs.some(c => ['installing', 'new'].includes(c.status))) setTimeout(() => { if (parse().arg === 'checkers') api('monitor').then(r => { D.mon = r; render(true); }).catch(() => {}); }, 6000);
  return h('div', { style: 'max-width:760px' },
    h('p', { class: 'muted', style: 'margin:0 0 14px' }, 'Чекеры — серверы в России. Они проверяют, открываются ли ноды оттуда; бот решает по большинству. Хватит двух-трёх у разных провайдеров.'),
    h('div', { class: 'chips', style: 'margin-bottom:14px' }, btn('Добавить чекер', addChecker, 'primary', 'plus'),
      btn('Проверить сейчас', e => busyBtn(e, async () => { try { await api('monitor/check', 'POST'); toast('Проверка запущена', 'Итог — через 1–2 минуты'); } catch (x) { toast('Не получилось', x.message, 'bad'); } }), '', 'refresh')),
    cs.length ? h('div', { class: 'list' }, cs.map(c => { const [cls, word] = CK_ST[c.status] || ['off', c.status || '—'];
      return h('div', { class: 'li', style: 'flex-wrap:wrap' }, h('div', { class: 'ic-tile' }, ic('radar')),
        h('div', { class: 'grow', style: 'flex:1 1 220px;min-width:0' }, h('div', { class: 't' }, c.name),
          h('div', { class: 's', style: 'white-space:normal' }, [R[c.region], c.ip, c.access, c.by ? 'добавил ' + c.by : ''].filter(Boolean).join(' · ')
            + (c.poll_error ? ' · ' + c.poll_error : c.polled_at ? ` · ответил ${ago(c.polled_at * 1000)}` : '') + (c.status === 'failed' && c.error ? ' · ' + c.error : ''))),
        h('span', { class: 'st ' + (c.poll_error ? 'warn' : cls) }, c.poll_error ? 'не отвечает' : word),
        c.mine ? [btn('Переустановить', () => confirmAct({ title: `Переустановить «${c.name}»?`, icon: ['radar', 'run'], ok: 'Переустановить',
          text: 'Бот зайдёт на сервер и поставит проверку заново. Пара минут.', fn: async () => { await api(`checkers/${c.id}/install`, 'POST', {}); toast('Переустановка началась'); D.mon = await api('monitor'); render(); } }), 's ghost'),
          btn('Убрать', () => confirmAct({ title: `Убрать чекер «${c.name}»?`, icon: ['trash', 'bad'], ok: 'Убрать', cls: 'danger solid',
            text: 'Бот перестанет проверять ноды с этого сервера и остановит на нём проверку. Сам сервер останется.',
            fn: async () => { await api('checkers/' + c.id, 'DELETE'); toast('Чекер убран'); D.mon = await api('monitor'); render(); } }), 's danger')] : null);
    })) : empty('radar', 'Чекеров нет', 'Без них бот не узнает, что ноду заблокировали в России.'));
}
function addChecker() {
  const v = { name: '', ip: '', user: 'root', port: '22', password: '', key: '' }, err = h('div', { class: 'err', role: 'alert' });
  modal({ title: 'Добавить чекер', icon: ['radar', 'run'], body: h('div', { class: 'form' },
    h('div', null, 'Подойдёт любой VPS в России с Ubuntu или Debian. Бот сам поставит проверку.'),
    fld('Название', h('input', { class: 'inp', placeholder: 'Например, Москва МТС', oninput: e => { v.name = e.target.value; } }), 'По названию города бот угадает регион.'),
    h('div', { class: 'row2' }, fld('IP-адрес', h('input', { class: 'inp mono', inputmode: 'decimal', oninput: e => { v.ip = e.target.value.trim(); } })),
      fld('SSH-порт', h('input', { class: 'inp mono', inputmode: 'numeric', value: '22', oninput: e => { v.port = e.target.value.trim(); } }))),
    fld('Пароль root', h('input', { class: 'inp', type: 'password', autocomplete: 'off', oninput: e => { v.password = e.target.value; } }), 'Нужен, чтобы поставить проверку.'), err),
    actions: [{ l: 'Отмена' }, { l: 'Добавить и поставить', cls: 'primary', fn: async (c, b) => {
      b.classList.add('busy'); err.textContent = '';
      try { await api('checkers', 'POST', v); c(); toast('Чекер добавлен', 'Установка займёт пару минут'); D.mon = await api('monitor'); render(); }
      catch (x) { err.textContent = x.message; b.classList.remove('busy'); }
    } }] });
}

/* ═════════ Пополнение баланса Bobr Cloud ═════════ */
function topUp() {
  const W = D.wallet;
  if (!W) return toast('Bobr Cloud не ответил', 'Попробуйте позже', 'bad');
  const v = { amount: Math.max(W.min_rub || 100, 500), provider: (W.providers[0] || {}).code }, err = h('div', { class: 'err', role: 'alert' });
  const sums = [500, 1000, 3000, 5000].filter(x => x >= (W.min_rub || 0) && x <= (W.max_rub || 1e9));
  const body = h('div', { class: 'form' });
  const draw = () => body.replaceChildren(h('div', null, `На балансе ${rubK(W.balance_kop)} (${W.account}). С него оплачиваются серверы, продление и смена IP.`),
    h('div', { class: 'chips' }, sums.map(x => h('button', { type: 'button', class: 'chip', 'aria-pressed': String(v.amount === x), onclick: () => { v.amount = x; draw(); } }, rub(x)))),
    fld('Своя сумма, ₽', h('input', { class: 'inp mono', inputmode: 'numeric', value: String(v.amount), style: 'max-width:180px', oninput: e => { v.amount = parseInt(e.target.value, 10) || 0; } }), `От ${W.min_rub} до ${W.max_rub} ₽`),
    W.providers.length > 1 ? fld('Способ', seg(W.providers.map(p => ({ v: p.code, l: p.label })), v.provider, x => { v.provider = x; draw(); }, 'Способ оплаты')) : '', err);
  draw();
  modal({ title: 'Пополнить баланс', icon: ['wallet', 'run'], body, actions: [{ l: 'Отмена' }, { l: 'Перейти к оплате', cls: 'primary', fn: async (c, b) => {
    b.classList.add('busy'); err.textContent = '';
    try {
      const r = await api('wallet/topup', 'POST', { amount_rub: v.amount, provider: v.provider });
      c();
      if (r.url) { try { tg && tg.openLink ? tg.openLink(r.url) : window.open(r.url, '_blank', 'noopener'); } catch (_) { location.href = r.url; } }
      toast('Откройте оплату', 'После оплаты баланс обновится сам', 'info');
      setTimeout(() => api('wallet').then(x => { D.wallet = x; render(true); }).catch(() => {}), 30000);
    } catch (x) { err.textContent = x.message; b.classList.remove('busy'); }
  } }] });
}

/* ═════════ Сервер бота: обновление и резервные копии ═════════ */
function setBot() {
  if (!D.su) { D.su = { loading: true }; Promise.all([api('selfupdate'), api('selfupdate/check').catch(() => null), api('backup').catch(e => ({ error: e.message }))])
    .then(([c, rem, bk]) => { D.su = { c, rem, bk }; }).catch(e => { D.su = { error: e.message }; }).then(() => render(true)); }
  const S = D.su;
  if (S.loading) return h('div', { class: 'sk', style: 'height:300px' });
  if (S.error) return empty('alert', 'Не загрузилось', S.error, btn('Повторить', () => { D.su = null; render(); }, 'primary'));
  const { c, rem, bk } = S, latest = rem && rem.state === 'latest';
  const saveUpd = async p => { try { Object.assign(c, await api('selfupdate', 'POST', p)); toast('Сохранено'); } catch (x) { toast('Не получилось', x.message, 'bad'); } render(); };
  const next = (c.changelog || [])[0];
  return h('div', { style: 'max-width:720px' },
    h('div', { class: 'list', style: 'margin-bottom:12px' },
      h('div', { class: 'li', style: 'flex-wrap:wrap' }, h('div', { class: 'ic-tile' }, ic('up')),
        h('div', { class: 'grow', style: 'flex:1 1 220px' }, h('div', { class: 't' }, `Bobr Nodes ${c.version.number}`),
          h('div', { class: 's', style: 'white-space:normal' }, !rem ? 'GitHub не ответил' : latest ? 'Последняя версия' : rem.state === 'ready' ? (rem.version && rem.version !== c.version.number ? `Есть ${rem.version}` : 'Есть обновление')
            : rem.state === 'testing' ? 'Новая версия проходит тесты' : rem.state === 'failed' ? 'Новая версия не прошла тесты' : 'Не удалось проверить')),
        rem && rem.state === 'ready' ? btn('Обновить сейчас', () => confirmAct({ title: 'Обновить бота сейчас?', icon: ['up', 'run'], ok: 'Обновить',
          text: 'Бот перезапустится примерно на 10 секунд, начатые задачи доделает до обновления. Если новая версия не запустится, бот сам вернёт прежнюю.',
          fn: async () => { const r = await api('selfupdate/now', 'POST'); toast('Обновление запланировано', r.message); } }), 's primary')
          : btn('Проверить', e => busyBtn(e, async () => { try { S.rem = await api('selfupdate/check?force=1'); } catch (x) { toast('Не получилось', x.message, 'bad'); } render(); }), 's', 'refresh')),
      switchRow('Обновляться автоматически', `Ночью в ${c.hour}:00 по вашему времени`, c.on, v => saveUpd({ on: v }))),
    next ? h('details', { class: 'more-opts', style: 'margin-bottom:22px' }, h('summary', null, `Что нового в ${next.version}`),
      h('ul', { style: 'margin:8px 0 0;padding-left:18px' }, next.items.map(i => h('li', { style: 'margin-bottom:6px' }, i.text)))) : null,
    h('div', { class: 'section-h' }, h('h2', null, 'Резервные копии')),
    bk.error ? h('div', { class: 'muted' }, bk.error)
      : [h('div', { class: 'list', style: 'margin-bottom:12px' },
          switchRow('Копия каждую ночь', `В ${bk.hour}:00, хранится ${bk.keep} последних` + (bk.last_at ? ` · последняя ${ago(bk.last_at * 1000)}` : ''), bk.on,
            async v => { try { S.bk = await api('backup', 'POST', { on: v }); toast('Сохранено'); } catch (x) { toast('Не получилось', x.message, 'bad'); } render(); })),
        bk.last_error ? h('div', { class: 'notice' }, h('span', { class: 'i' }, ic('alert')), h('div', { class: 'grow' }, h('b', null, 'Последняя копия не удалась'), bk.last_error)) : null,
        h('div', { class: 'chips' }, btn('Сделать копию сейчас', e => busyBtn(e, async () => {
          try { S.bk = await api('backup/now', 'POST'); toast('Копия создана', 'Файл отправлен вам в Telegram'); } catch (x) { toast('Не получилось', x.message, 'bad'); } render(); }), '', 'box'),
          btn('Восстановление и ключ шифрования', () => classic('settings'), 'ghost', 'ext'))]);
}
