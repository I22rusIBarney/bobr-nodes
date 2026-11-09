'use strict';
/* Новый интерфейс: вход через Россию (мост) — вкладка карточки ноды. Те же запросы, что у классического. */

const RL_ST = { new: ['run', 'создаётся'], buying: ['run', 'покупаю сервер'], installing: ['run', 'ставлю'], ok: ['ok', 'работает'], warn: ['warn', 'нода не отвечает'], failed: ['bad', 'ошибка'] };
const RL_MODE = { egames: 'роутинг, как в eGames', vision: 'роутинг, Vision TLS', xray: 'роутинг, REALITY', haproxy: 'проброс (HAProxy)' };
const MASK_DEF = JSON.stringify({ tcp: [{ type: 'fragment', settings: { length: '10-20', packets: 'tlshello', interval: '10-20' } }] });
// обновление во время установки: прежние данные остаются на экране, пока грузятся свежие (без мигания заглушкой)
const reloadDetail = n => { const c = D.detail[n.id]; D.detail[n.id] = c && c.d ? { d: c.d, at: 0 } : null; render(true); };
// один таймер на ноду: каждая перерисовка ставила свой, и обновления множились
const RL_POLL = {};
const pollDetail = (n, ms, still) => { if (RL_POLL[n.id]) return; RL_POLL[n.id] = setTimeout(() => { delete RL_POLL[n.id]; if (still() && !(D.detail[n.id] || {}).loading) reloadDetail(n); }, ms); };

function nodeBridge(n, det) {
  const d = det.d;
  if (!d) return det.err ? empty('alert', 'Не загрузилось', det.err) : sk(240);
  const rid = UI.relay && UI.relay.node === n.id ? UI.relay.id : null;
  if (rid === 'new') return relayAdd(n, d);
  const x = rid && (d.relays || []).find(r => r.id === rid);
  if (x) return relayDetail(n, d, x);
  const rs = d.relays || [];
  if (rs.some(r => ['new', 'buying', 'installing'].includes(r.status))) pollDetail(n, 4000, () => parse().name === 'node' && UI.tabs.node === 'bridge');
  return [h('p', { class: 'muted', style: 'margin:0 0 14px' }, 'Клиенты подключаются к серверу входа в России, а в интернет выходят с этой ноды. Если заблокируют вход — меняется только вход, нода остаётся.'),
    rs.length ? h('div', { class: 'list', style: 'margin-bottom:14px' }, rs.map(r => { const [cls, w] = RL_ST[r.status] || ['off', r.status];
      return h('button', { type: 'button', class: 'li', onclick: () => { UI.relay = { node: n.id, id: r.id }; render(); } }, h('div', { class: 'ic-tile' }, ic('route')),
        h('div', { class: 'grow' }, h('div', { class: 't' }, r.remark || r.address), h('div', { class: 's' }, `${r.address}:${r.listen} → ${r.target}:${r.target_port} · ${RL_MODE[r.mode] || r.mode}`)),
        h('span', { class: 'st ' + cls }, w), h('span', { class: 'chev' }, ic('chevR')));
    })) : empty('route', 'Мостов нет', 'Поставьте вход на российском сервере — бот купит его или возьмёт ваш, настроит и добавит хост в панель.'),
    btn('Новый вход', () => { UI.relay = { node: n.id, id: 'new' }; UI.rlf = null; render(); }, 'primary', 'plus')];
}

function relayAdd(n, d) {
  const R = UI.rlf || (UI.rlf = { exitEg: false, mask: true, port: '443', custom: false, src: 'buy', loc: null, idx: null, sid: null, convert: false, ip: '', remark: '',
    ssh: { user: 'root', port: '22', password: '', key: '', key_text: '', sudo_password: '' } });
  const backBtn = h('button', { type: 'button', class: 'back', onclick: () => { UI.relay = null; render(); } }, ic('chevL'), 'Мосты');
  let pick = null, conv = null, body = null;
  if (R.src === 'buy') {
    const C = UI.rlCat && UI.rlCat.pid === n.pid ? UI.rlCat : null;
    if (!C) { UI.rlCat = { pid: n.pid, loading: true }; api('catalog?pid=' + n.pid).then(c => { UI.rlCat = { pid: n.pid, c };
      const cc = (n.country || '').toLowerCase(), L = c.locations.find(l => l.code === cc) || c.locations[0];
      if (L && !R.loc) { R.loc = L.code; R.idx = [...L.offers].sort((a, b) => a.price_kop - b.price_kop)[0].idx; } }).catch(e => { UI.rlCat = { pid: n.pid, err: e.message }; }).then(() => render(true)); }
    if (!C || C.loading) body = sk(180);
    else if (C.err) body = empty('alert', 'Bobr Cloud не ответил', C.err, btn('Повторить', () => { UI.rlCat = null; render(); }, 'primary'));
    else { const L = C.c.locations.find(l => l.code === R.loc);
      pick = L && L.offers.find(o => o.idx === R.idx);
      body = [fld('Где', h('div', { class: 'chips' }, C.c.locations.map(l => h('button', { type: 'button', class: 'chip', 'aria-pressed': String(R.loc === l.code),
          onclick: () => { R.loc = l.code; R.idx = [...l.offers].sort((a, b) => a.price_kop - b.price_kop)[0].idx; render(); } }, l.flag ? h('span', { class: 'flag' }, l.flag) : null, l.name))),
        (n.country && R.loc === n.country.toLowerCase() ? 'Та же страна, что у ноды, — задержка почти не вырастет. ' : '') + 'Входу хватит самого маленького тарифа: он только пересылает трафик.'),
        L ? fld('Тариф', h('div', { class: 'choice' }, [...L.offers].sort((a, b) => a.price_kop - b.price_kop).map(o => opt(o.idx === R.idx, `${o.cpu} vCPU · ${o.ram} ГБ`, `диск ${o.disk} ГБ` + (o.speed ? ' · ' + o.speed : ''),
          () => { R.idx = o.idx; render(); }, h('span', { class: 'price' }, rubK(o.price_kop), h('span', { class: 'faint', style: 'font:400 13px var(--font)' }, ' / мес'))))),
          C.c.balance_kop != null ? `На балансе ${rubK(C.c.balance_kop)} (${C.c.buyer}).` : null) : null];
    }
  } else if (R.src === 'server') {
    const Sv = UI.rlSrv && UI.rlSrv.pid === n.pid ? UI.rlSrv : null;
    if (!Sv) { UI.rlSrv = { pid: n.pid, loading: true }; api(`profiles/${n.pid}/servers`).then(r => { UI.rlSrv = { pid: n.pid, s: r.servers }; }).catch(e => { UI.rlSrv = { pid: n.pid, err: e.message }; }).then(() => render(true)); }
    if (!Sv || Sv.loading) body = sk(160);
    else if (Sv.err) body = empty('alert', 'Не загрузилось', Sv.err);
    else {
      const self = x => x.node && [x.node.domain, x.node.ip].includes(n.address);
      const ok = Sv.s.filter(x => x.ready || (x.relay && x.status === 'active')), cv = Sv.s.filter(x => x.node && !x.relay && x.status === 'active' && !self(x));
      conv = R.convert ? cv.find(x => x.id === R.sid) : null;
      const pickRow = (x, sub, isConv) => h('button', { type: 'button', class: 'li', onclick: () => { R.sid = x.id; R.convert = isConv; render(); } },
        h('span', { class: 'cb-m' + (R.sid === x.id ? ' on' : ''), style: 'border-radius:50%' }, R.sid === x.id ? ic('check') : null),
        h('div', { class: 'grow' }, h('div', { class: 't' }, `${x.flag || ''} ${x.name || x.ip}`), h('div', { class: 's', style: 'white-space:normal' }, sub)));
      body = [fld('Свободные серверы', ok.length ? h('div', { class: 'list', style: 'background:var(--sheet-2)' }, ok.map(x => pickRow(x, [x.ip, `${x.cpu} vCPU, ${x.ram} ГБ`, x.relay ? 'уже вход для другой ноды' : '',
            x.stale ? `система переустановлена — прежняя нода ${x.stale.domain || x.stale.ip} уйдёт из панели` : ''].filter(Boolean).join(' · '), false)))
          : h('div', { class: 'muted' }, cv.length ? 'Свободных нет — можно переделать сервер с нодой (ниже).' : 'Свободных серверов нет — купите новый.'), 'Пароль root бот возьмёт из Bobr Cloud.'),
        cv.length ? fld('С нодой — переделать в мост', h('div', { class: 'list', style: 'background:var(--sheet-2)' }, cv.map(x => pickRow(x, `${x.ip} · нода ${x.node.domain || x.ip} будет убрана`, true))),
          'Бот остановит ноду, уберёт её хосты из панели и A-запись, затем поставит мост. Пользователи этой ноды переключатся на другие ноды подписки.') : null];
    }
  } else body = [fld('IP сервера', h('input', { class: 'inp mono', 'data-k': 'rlip', value: R.ip, placeholder: '203.0.113.10', oninput: e => { R.ip = e.target.value.trim(); } })),
    h('div', { class: 'row2' }, fld('Пользователь', h('input', { class: 'inp mono', value: R.ssh.user, oninput: e => { R.ssh.user = e.target.value.trim(); } })),
      fld('SSH-порт', h('input', { class: 'inp mono', inputmode: 'numeric', value: R.ssh.port, oninput: e => { R.ssh.port = e.target.value.trim(); } }))),
    fld('Пароль', h('input', { class: 'inp', type: 'password', autocomplete: 'off', 'data-k': 'rlpw', value: R.ssh.password, oninput: e => { R.ssh.password = e.target.value; } }), 'Нужен root или пользователь с sudo — бот ставит Docker, сертификат и ноду.')];
  const ready = R.src === 'buy' ? !!pick : R.src === 'server' ? !!R.sid : !!(R.ip && (R.ssh.password || R.ssh.key));
  const submit = async () => {
    const body2 = { mode: 'egames', exit_egames: R.exitEg, final_mask: R.mask, final_mask_json: R.mask ? MASK_DEF : undefined, listen: R.custom ? R.port : '443', remark: R.remark,
      target_port: String(d.relay_port || 443), source: R.src, background: true,
      ...(R.src === 'buy' ? { location: R.loc, idx: R.idx } : R.src === 'server' ? { server_id: R.sid, convert: !!conv } : { ip: R.ip, ...R.ssh }) };
    const r = await api(`profiles/${n.pid}/nodes/${n.uuid}/relays`, 'POST', body2);
    UI.rlf = null; UI.relay = { node: n.id, id: r.id }; reloadDetail(n);
    toast(r.pending ? 'Мост ставится' : r.ok ? 'Вход работает' : 'Мост поставлен с замечаниями', r.pending ? 'Ход — здесь же, обычно 3–8 минут' : r.message, r.ok || r.pending ? 'ok' : 'bad');
  };
  const go = () => R.src === 'buy' ? confirmAct({ title: 'Купить сервер под вход?', icon: ['wallet', 'run'], ok: `Купить за ${rubK(pick.price_kop)}/мес`,
      text: `${rubK(pick.price_kop)} в месяц спишется с баланса Bobr Cloud. Бот поставит на сервер мост к ноде ${n.name}.`, fn: submit })
    : conv ? confirmAct({ title: `Убрать ноду ${conv.node.domain || conv.ip}?`, icon: ['trash', 'bad'], ok: 'Убрать и сделать мостом', cls: 'danger solid',
      text: `Нода на этом сервере перестанет работать и пропадёт из панели и подписок. Сервер станет входом для ${n.name}.`, fn: submit })
    : confirmAct({ title: 'Поставить мост?', icon: ['route', 'run'], ok: 'Поставить', text: `Бот зайдёт на ${R.ip} по SSH и поставит вход к ноде ${n.name}.`, fn: submit });
  return [backBtn, h('h3', { style: 'margin:6px 0 4px' }, 'Новый вход'),
    h('p', { class: 'muted', style: 'margin:0 0 14px' }, 'На входе — нода с REALITY «под себя»: свой домен на вашем Cloudflare, сертификат и сайт-заглушка. До выхода — Shadowsocks, российские сайты открываются со входа напрямую.'),
    h('div', { class: 'list', style: 'margin-bottom:14px' },
      switchRow('Переустановить и выход', `${n.name} — тоже нодой как в eGames (бот поставит выход и сам, если он не на связи)`, R.exitEg, v => { R.exitEg = v; }),
      switchRow('Final Mask', 'Фрагментация ClientHello на хосте входа — помогает, когда ТСПУ режет тяжёлые соединения', R.mask, v => { R.mask = v; })),
    h('div', { class: 'sheet form', style: 'padding:16px;margin-bottom:14px' },
      fld('Порт входа', seg([{ v: false, l: 'Стандартный 443' }, { v: true, l: 'Свой порт' }], R.custom, v => { R.custom = v; if (v && R.port === '443') R.port = '8443'; render(); }, 'Порт'),
        R.custom ? 'Облачный фаервол откройте сами — ufw бот откроет.' : 'Клиенты подключаются к 443 — как к обычному сайту.'),
      R.custom ? fld('Порт', h('input', { class: 'inp mono', inputmode: 'numeric', style: 'max-width:140px', value: R.port, oninput: e => { R.port = e.target.value.replace(/\D/g, ''); } })) : null,
      fld('Сервер входа', seg([{ v: 'buy', l: 'Купить' }, { v: 'server', l: 'Уже купленный' }, { v: 'ssh', l: 'Свой сервер' }], R.src, v => { R.src = v; R.sid = null; R.convert = false; render(); }, 'Сервер входа'))),
    h('div', { class: 'sheet form', style: 'padding:16px;margin-bottom:14px' }, body),
    h('div', { class: 'sheet form', style: 'padding:16px;margin-bottom:14px' }, fld('Название хоста в подписке', h('input', { class: 'inp', 'data-k': 'rlrem', value: R.remark, placeholder: n.name + ' ⇄', oninput: e => { R.remark = e.target.value; } }), 'Необязательно.')),
    btn(R.src === 'buy' ? (pick ? `Купить за ${rubK(pick.price_kop)}/мес и поставить` : 'Выберите тариф') : conv ? 'Убрать ноду и поставить мост' : 'Поставить мост', go, conv ? 'danger solid' : 'primary', null, { disabled: !ready })];
}

function relayDetail(n, d, x) {
  const running = ['new', 'buying', 'installing'].includes(x.status), [cls, w] = RL_ST[x.status] || ['off', x.status];
  if (running) pollDetail(n, 3500, () => UI.relay && UI.relay.id === x.id && parse().name === 'node');
  const act = (fn, ok) => async e => busyBtn(e, async () => { try { const r = await fn(); if (ok || (r && r.message)) toast(ok || r.message); reloadDetail(n); } catch (y) { toast('Не получилось', y.message, 'bad'); } });
  const shared = (d.relays || []).filter(o => o.id !== x.id && o.ip && o.ip === x.ip).length;
  const sshAsk = exit => { const v = { pw: '', forget: true }, err = h('div', { class: 'err', role: 'alert' });
    modal({ title: 'Новый пароль SSH', icon: ['key', 'warn'], body: h('div', { class: 'form' }, h('div', null, (exit ? 'Сервер выхода' : 'Сервер входа') + ' не пустил со старым паролем. Бот проверит вход с новым и продолжит установку с того же места.'),
      fld(exit ? `Пароль root ${n.name}` : `Пароль root ${x.ip}`, h('input', { class: 'inp', type: 'password', autocomplete: 'new-password', oninput: e => { v.pw = e.target.value; } })),
      h('div', { class: 'list', style: 'background:var(--sheet-2)' }, switchRow('Сервер переустанавливали', 'забыть его прежний ключ SSH', true, on => { v.forget = on; })), err),
      actions: [{ l: 'Отмена' }, { l: 'Проверить и продолжить', cls: 'primary', fn: async (c, b) => {
        if (!v.pw.trim()) { err.textContent = 'Введите пароль'; return; }
        b.classList.add('busy'); err.textContent = '';
        try {
          let r;
          if (exit) {
            if (v.forget) await api(`profiles/${n.pid}/nodes/${n.uuid}/forget-hostkey`, 'POST', { ip: n.address }).catch(() => null);
            r = await api(`profiles/${n.pid}/nodes/${n.uuid}/ssh`, 'POST', { user: 'root', port: (d.ssh && d.ssh.port) || 22, password: v.pw, ip: n.address });
            if (r.ok) await api(`relays/${x.id}/apply`, 'POST', { background: true });
          } else r = await api(`relays/${x.id}/ssh-retry`, 'POST', { password: v.pw, forget: v.forget });
          if (!r.ok) { err.textContent = r.message; b.classList.remove('busy'); return; }
          c(); toast('Пароль подошёл', 'Продолжаю установку'); reloadDetail(n);
        } catch (y) { err.textContent = y.message; b.classList.remove('busy'); }
      } }] }); };
  const check = e => busyBtn(e, async () => {
    let r; try { r = await api(`relays/${x.id}/check`, 'POST'); } catch (y) { return toast('Не получилось', y.message, 'bad'); }
    const P = { ok: ['ok', 'проходит'], cert: ['ok', 'проходит'], stall: ['bad', 'нет ответа — режут'], fail: ['bad', 'сброс'] };
    const pr = (t, s, q) => { const st = !q ? ['off', 'нет данных'] : q.error ? ['bad', 'ошибка'] : q.tcp !== 'ok' && q.tcp !== '?' ? ['bad', 'порт закрыт'] : P[q.tls] || ['off', q.tls];
      return h('div', { class: 'li' }, h('div', { class: 'grow' }, h('div', { class: 't' }, t), h('div', { class: 's', style: 'white-space:normal' }, q && q.error ? q.error : s)), h('span', { class: 'st ' + st[0] }, st[1])); };
    const ent = `${r.entry}:${r.listen}`, tgt = `${r.target}:${r.target_port}`;
    modal({ title: 'Проверка моста', icon: ['shield', 'run'], body: h('div', null,
      h('div', { class: 'notice' + (/работает/.test(r.verdict[0]) ? ' ok' : ''), style: 'margin-bottom:12px' }, h('span', { class: 'i' }, ic(/работает/.test(r.verdict[0]) ? 'check' : 'alert')), h('div', null, r.verdict.map(t => h('div', null, t)))),
      h('div', { class: 'list', style: 'background:var(--sheet-2)' }, pr('Вход → нода', `с сервера входа на ${tgt}`, r.from_entry), pr('Через вход из-за рубежа', `с сервера бота на ${ent}`, r.from_bot),
        r.checkers.map(c => [pr(`${c.name}: вход`, `из России на ${ent}`, c.error ? { error: c.error } : c.entry), pr(`${c.name}: нода напрямую`, `из России на ${tgt}`, c.error ? null : c.node)])),
      r.sni ? h('p', { class: 'muted', style: 'margin:10px 0 0;font-size:13px' }, `TLS — с SNI ${r.sni}, как у клиента.`) : null) });
  });
  const remove = () => {
    const del = async (force, srv) => { const qs = [force ? 'force=1' : '', srv ? 'server=1' : ''].filter(Boolean).join('&'); return api(`relays/${x.id}` + (qs ? '?' + qs : ''), 'DELETE').catch(e => ({ ok: false, message: e.message })); };
    const finish = async srv => { let r = await del(false, srv);
      if (!r.ok) return confirmAct({ title: 'Сервер недоступен', icon: ['alert', 'warn'], ok: 'Убрать из бота и панели', cls: 'danger solid', text: r.message + ' Убрать вход только из бота и панели? Проброс на сервере останется.',
        fn: async () => { r = await del(true, srv); if (!r.ok) throw new Error(r.message); toast(r.message || 'Вход убран'); UI.relay = null; reloadDetail(n); } });
      toast(r.message || 'Вход убран'); UI.relay = null; reloadDetail(n); };
    if (x.server_id && !shared) modal({ title: 'Убрать вход?', icon: ['trash', 'bad'], body: 'Бот уберёт проброс и хосты из панели. Сервер входа больше ничему не нужен — удалить его в Bobr Cloud, чтобы не платить?',
      actions: [{ l: 'Отмена' }, { l: 'Только убрать', fn: c => { c(); finish(false); } }, { l: 'Убрать и удалить сервер', cls: 'danger solid', fn: c => { c(); finish(true); } }] });
    else confirmAct({ title: 'Убрать вход?', icon: ['trash', 'bad'], ok: 'Убрать', cls: 'danger solid', text: 'Бот уберёт проброс с сервера (другие его входы останутся) и хосты из панели.', fn: async () => finish(false) });
  };
  const order = (x.stages || []).map(s => s.code), cur = x.status === 'buying' ? Math.max(0, order.indexOf('wait')) : order.indexOf(x.stage);
  const sshBad = x.status === 'failed' && /SSH не принял пароль|SSH спрашивает пароль/.test(x.error || ''), keyBad = /ключ SSH|host key|REMOTE HOST IDENTIFICATION/i.test(x.error || '');
  return [h('button', { type: 'button', class: 'back', onclick: () => { UI.relay = null; render(); } }, ic('chevL'), 'Мосты'),
    h('h3', { style: 'margin:6px 0 12px;display:flex;align-items:center;gap:10px;flex-wrap:wrap' }, x.remark || x.address, h('span', { class: 'st ' + cls }, w)),
    x.status === 'buying' ? h('div', { class: 'notice info' }, h('span', { class: 'i' }, ic('clock')), h('div', null, h('b', null, 'Сервер создаётся в Bobr Cloud'), 'Как только получит IP, бот поставит вход и добавит хосты — обычно 2–4 минуты. Окно можно закрыть.')) : null,
    x.error ? h('div', { class: 'notice' }, h('span', { class: 'i' }, ic('alert')), h('div', { class: 'grow' }, h('b', null, 'Ошибка'), x.error),
      sshBad ? btn('Ввести новый пароль', () => sshAsk(/^выход/.test(x.error)), 's primary') : null,
      keyBad ? btn('Забыть ключ и повторить', () => confirmAct({ title: 'Забыть ключ сервера?', icon: ['key', 'warn'], ok: 'Забыть и повторить', text: 'Нужно, если сервер переустанавливали. Если нет — смена ключа может означать подмену сервера.',
        fn: async () => { await api(`relays/${x.id}/forget-hostkey`, 'POST'); await api(`relays/${x.id}/apply`, 'POST', { background: true }); reloadDetail(n); } }), 's') : null) : null,
    (running || x.status === 'failed') && order.length ? h('div', { class: 'list', style: 'margin-bottom:14px' }, x.stages.map((s, i) => {
      const k = cur < 0 ? 'wait' : i < cur ? 'done' : i === cur ? (x.status === 'failed' ? 'fail' : 'now') : 'wait';
      return h('div', { class: 'li' }, h('span', { class: 'step-dot ' + k }, k === 'done' ? ic('check') : k === 'fail' ? ic('x') : i + 1), h('div', { class: 'grow' }, h('div', { class: 't', style: k === 'wait' ? 'color:var(--ink-3);font-weight:500' : '' }, s.title)));
    })) : x.bad.length ? h('div', { class: 'notice', style: 'background:var(--warn-soft)' }, h('span', { class: 'i', style: 'color:var(--warn)' }, ic('alert')), h('div', null, h('b', null, 'Мост стоит, но'), x.bad.join('; '))) : null,
    h('div', { class: 'kv', style: 'margin-bottom:14px' }, [
      ['Вид', RL_MODE[x.mode] || x.mode], x.mode === 'xray' ? ['SNI', h('span', { class: 'mono' }, x.sni)] : null,
      x.domain ? ['Домен входа', h('span', { class: 'mono' }, x.domain), copyBtn(x.domain, 'Домен')] : null,
      x.server_id ? ['Сервер', [x.flag, x.spec || 'Bobr Cloud #' + x.server_id].filter(Boolean).join(' ') + (x.price_kop ? ` · ${rubK(x.price_kop)}/мес` : '')] : null,
      x.ip ? ['Клиенты подключаются', h('span', { class: 'mono' }, `${x.address}:${x.listen}`), copyBtn(x.address, 'Адрес')] : null,
      ['Трафик идёт на', h('span', { class: 'mono' }, `${x.target}:${x.target_port}`)],
      ['xray', 'vision', 'egames'].includes(x.mode) && ['ok', 'warn'].includes(x.status) ? ['Связь с выходом', x.hop === 'ss' ? `Shadowsocks :${x.hop_port || ''}` : 'VLESS'] : null,
      ['Хостов в панели', String(x.hosts)], ['Вход по SSH', x.access]].filter(Boolean).flatMap(([k, v, a]) => [h('div', { class: 'k' }, k), h('div', { class: 'v' }, v), h('div', null, a || null)])),
    !running ? h('div', { class: 'chips', style: 'margin-bottom:14px' },
      x.ip ? btn('Проверить мост', check, 'primary', 'shield') : null,
      btn(x.status === 'failed' ? 'Повторить установку' : 'Переустановить', act(() => api(`relays/${x.id}/apply`, 'POST', { background: true }), 'Ставлю — до пары минут'), '', 'refresh'),
      ['xray', 'vision', 'egames'].includes(x.mode) && ['ok', 'warn'].includes(x.status) ? btn(x.hop === 'ss' ? 'Связь: на VLESS' : 'Связь: на Shadowsocks', () => confirmAct({ title: 'Сменить связь входа с выходом?', icon: ['route', 'run'], ok: 'Сменить',
        text: 'Shadowsocks — отдельный вход только для моста, мимо общего inbound клиентов; российские сайты — со входа напрямую. VLESS — вход ходит на выход как обычный клиент. Xray выхода перезапустится на пару секунд, подписку обновлять не нужно.',
        fn: async () => { await api(`relays/${x.id}/hop`, 'POST', { hop: x.hop === 'ss' ? 'vless' : 'ss' }); toast('Связь изменена'); reloadDetail(n); } }), '') : null) : null,
    x.ip && !running ? h('div', { class: 'list', style: 'margin-bottom:14px' },
      x.mode === 'egames' ? dangerRow('refresh', 'Переустановить выход как в eGames', `${n.name} — для чистого сервера выхода. Клиентам ноды нужно обновить подписку`,
        btn('Переустановить', () => confirmAct({ title: 'Переустановить выход?', icon: ['refresh', 'warn'], ok: 'Переустановить', text: `На сервер ${n.name} встанет нода как в eGames: сертификат, заглушка, свой профиль; прежние хосты этой ноды выключатся.`,
          fn: async () => { await api(`relays/${x.id}/exit-egames`, 'POST'); reloadDetail(n); } }), 's'))
        : dangerRow('shield', 'Переделать как в eGames', shared ? 'На сервере есть другие входы — сначала уберите их' : 'REALITY «под себя» на своём домене, до выхода — Shadowsocks. Клиентам нужно обновить подписку',
          shared ? null : btn('Переделать', () => confirmAct({ title: 'Переделать мост?', icon: ['shield', 'run'], ok: 'Переделать', text: 'Бот уберёт прежний мост из панели и с сервера, даст входу имя на вашем домене, выпустит сертификат и поставит ноду с REALITY. Сервер и оплата не меняются.',
            fn: async () => { await api(`relays/${x.id}/to-xray`, 'POST', { mode: 'egames', exit_egames: false }); reloadDetail(n); } }), 's'))) : null,
    (x.log || []).length ? [h('div', { class: 'section-h' }, h('h2', null, running ? 'Журнал' : 'Последняя установка')), h('div', { class: 'log', style: 'margin-bottom:14px' }, x.log.slice(-16).join('\n'))] : null,
    h('div', { class: 'list' },
      dangerRow('key', 'Забыть ключ сервера', 'Только если систему на сервере переустанавливали', btn('Забыть', () => confirmAct({ title: 'Забыть ключ сервера?', icon: ['key', 'warn'], ok: 'Забыть',
        text: 'Нужно, только если систему на сервере переустанавливали. Иначе смена ключа — признак подмены сервера.', fn: async () => { const r = await api(`relays/${x.id}/forget-hostkey`, 'POST'); toast(r.message || 'Готово'); } }), 's')),
      dangerRow('trash', 'Убрать вход', 'Проброс с сервера и хосты из панели', btn('Убрать', remove, 's danger')))];
}
