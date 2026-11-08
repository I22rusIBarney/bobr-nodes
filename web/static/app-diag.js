'use strict';
/* Диагностика ноды: почему недоступна и что делать. Факты собирает бот (/diagnose), здесь — диагноз, исправления по порядку
   (бесплатные раньше платных; платные — только через подтверждение) и что именно проверено. */

const DIAG_TTL = 120e3;  // результат свежий 2 минуты; при проблеме с нодой проверка запускается сама
function loadDiag(n, force) {
  const cur = D.diag[n.id];
  if (!force && cur && (cur.loading || Date.now() - cur.at < DIAG_TTL)) return;
  D.diag[n.id] = { ...(cur || {}), loading: true, at: Date.now() };
  api(`profiles/${n.pid}/nodes/${n.uuid}/diagnose`).then(r => { D.diag[n.id] = { r, at: Date.now() }; })
    .catch(e => { D.diag[n.id] = { err: e.message, at: Date.now() }; }).then(() => render(true));
}
function recheckLater(n) {  // после исправления — проверить снова, когда изменения успеют примениться
  D.diag[n.id] = null;
  [60000, 180000].forEach(t => setTimeout(() => { if (parse().name === 'node' && parse().arg === n.id) loadDiag(n, true); }, t));
}
const DIAG_TONE = { ok: 'ok', warn: 'warn', bad: '' };
function diagFix(n, f, det) {
  const after = () => recheckLater(n);
  return {
    fix: () => n.mon && n.mon.tok ? fixNode(n).then(after) : toast('Мониторинг группы выключен', 'Включите проверку из России в «Проверке и починке» группы — шаги починки берутся оттуда', 'info'),
    sync_ip: () => confirmAct({ title: 'Поправить DNS?', icon: ['globe', 'run'], ok: 'Поправить', text: 'Бот направит домен ноды на её сервер. Бесплатно. Если IP сервера уже сменили в Bobr Cloud — обновит и DNS, и панель задачей.',
      fn: async () => { const r = await api(`profiles/${n.pid}/nodes/${n.uuid}/fix-dns`, 'POST', {}); r.id ? jobToast(r.message, r.id) : toast('Готово', r.message); after(); } }),
    upgrade: () => confirmAct({ title: 'Переустановить контейнер ноды?', icon: ['refresh', 'run'], ok: 'Переустановить',
      text: `Бот по SSH поднимет ноду заново (версия ${n.version || n.target || 'последняя'}), проверит её и вернёт прежнюю, если новая не запустится. Клиенты переподключатся за несколько секунд. Бесплатно.`,
      fn: async () => { const r = await api(`profiles/${n.pid}/nodes/upgrade`, 'POST', { uuids: [n.uuid], tag: n.version || n.target || 'latest' });
        if (!(r.ids || []).length) throw new Error((r.skipped || []).map(s => s.why).join('; ') || 'не запустилось'); jobToast('Переустанавливаю ноду', r.ids[0]); after(); } }),
    reip: () => { const d = det && det.d; if (d && d.bcloud && d.bcloud.server_id) changeIp(n, d); else manualIp(n, d); after(); },
    replace: () => n.mon && n.mon.tok ? replaceNode(n, false) : toast('Мониторинг группы выключен', 'Новую ноду вместо этой предлагает мониторинг — включите его для группы', 'info'),
    hoster: () => modal({ title: 'Проверить сервер у хостера', icon: ['server', 'warn'], body: h('div', { class: 'form' },
      h('div', null, 'Сервер не отвечает даже из-за рубежа — это не блокировка. Что сделать:'),
      h('ol', { style: 'margin:0;padding-left:20px;line-height:1.6' },
        h('li', null, 'Откройте панель хостера (для серверов бота — Bobr Cloud → Серверы) и посмотрите статус: не закончилась ли аренда, не выключен ли сервер.'),
        h('li', null, 'Перезагрузите сервер. Через пару минут нажмите «Проверить» здесь.'),
        h('li', null, 'Если сервер не поднимается — «Новая нода»: бот купит замену после вашего подтверждения.'))) }),
  }[f.id];
}
function nodeDiag(n, det) {
  const problem = offline(n) || ruState(n) === 'bad' || waitsMe(n);
  const S = D.diag[n.id];
  if (problem && !S) loadDiag(n);
  const head = h('div', { class: 'section-h' }, h('h2', null, 'Диагностика'),
    S && S.r ? h('span', { class: 'faint', style: 'font-size:13px' }, 'проверено ' + ago(S.r.at * 1000)) : null,
    btn(S && S.loading ? 'Проверяю…' : S ? 'Проверить снова' : 'Проверить', () => loadDiag(n, true), 's ghost more', null, { disabled: S && S.loading }));
  if (!S) return [head, h('p', { class: 'muted', style: 'margin:0 0 20px' }, 'Бот проверит панель, Bobr Cloud, DNS, порт и сертификат из-за рубежа, SSH и итоги чекеров в России — и скажет, в чём дело.')];
  if (S.loading && !S.r) return [head, h('div', { class: 'sheet', style: 'padding:16px;margin-bottom:20px' }, h('div', { class: 'sk', style: 'height:16px;width:60%;margin-bottom:10px' }), h('div', { class: 'sk', style: 'height:12px;width:85%' }))];
  if (S.err) return [head, h('div', { class: 'notice', style: 'margin-bottom:20px' }, h('span', { class: 'i' }, ic('alert')), h('div', null, h('b', null, 'Не получилось проверить'), S.err))];
  const { verdict: v, facts: f } = S.r;
  const tick = (ok, label, detail) => h('div', { class: 'li' }, h('span', { class: 'rm-dot ' + (ok === true ? 'ok' : ok === false ? 'bad' : ok === 'warn' ? 'warn' : '') }),
    h('div', { class: 'grow' }, h('div', { class: 't', style: 'font-weight:500' }, label), detail ? h('div', { class: 's', style: 'white-space:normal' }, detail) : null));
  const ruAll = Object.values(f.russia || {}).flat();
  const proxied = (f.dns || []).length && (f.dns || []).every(ip => /^(104\.(1[6-9]|2[0-7])|172\.(6[4-9]|7[01])|162\.15[89]|188\.114\.9[6-9])\./.test(ip));
  return [head,
    h('div', { class: 'notice' + (v.severity === 'ok' ? ' ok' : ''), style: (v.severity === 'warn' ? 'background:var(--warn-soft);' : '') + 'margin-bottom:10px' },
      h('span', { class: 'i', style: v.severity === 'warn' ? 'color:var(--warn)' : '' }, ic(v.severity === 'ok' ? 'check' : 'alert')),
      h('div', { class: 'grow' }, h('b', null, v.title), v.text,
        v.fixes.length ? h('div', { class: 'chips', style: 'margin-top:10px' }, v.fixes.map((x, i) => {
          const fn = diagFix(n, x, det);
          return fn ? h('button', { type: 'button', class: 'btn s ' + (i === 0 ? 'primary' : ''), title: x.why, onclick: fn }, x.label, x.paid ? h('span', { class: 'tag', style: 'margin-left:6px;height:18px' }, 'платно') : null) : null;
        })) : null)),
    v.fixes.length ? h('p', { class: 'muted', style: 'margin:0 0 10px;font-size:13px' }, v.fixes.map((x, i) => `${i + 1}. ${x.label} — ${x.why}`).join('. ') + '.') : null,
    h('details', { class: 'more-opts', style: 'margin-bottom:20px' }, h('summary', null, 'Что проверено'),
      h('div', { class: 'list', style: 'margin-top:10px' },
        tick(f.panel, 'Панель видит ноду', f.panel ? null : 'Remnawave не связан с нодой'),
        f.bcloud !== undefined && f.bcloud !== null ? tick(f.bcloud === 'active', 'Сервер в Bobr Cloud', f.bcloud) : null,
        f.domain ? tick(proxied ? 'warn' : f.ip ? (f.dns || []).includes(f.ip) : (f.dns || []).length > 0, 'DNS', `${f.domain} → ${(f.dns || []).join(', ') || 'нет записи'}` + (proxied ? ' (через прокси Cloudflare)' : f.ip ? `, сервер ${f.ip}` : '')) : null,
        tick(f.tcp, `Порт ${f.port} из-за рубежа`, f.tcp == null ? 'не проверялся — нет публичного адреса' : f.tcp ? 'открывается с сервера бота' : 'не открывается с сервера бота'),
        f.tls ? tick(f.tls.tls === 'ok' ? true : f.tls.tls === 'cert' ? 'warn' : false, 'TLS', f.tls.tls === 'ok' ? `сертификат проверен${f.tls.days != null ? `, ещё ${f.tls.days} дн.` : ''}` : f.tls.tls === 'cert' ? 'рукопожатие есть, сертификат не свой (у REALITY так и должно быть)' : 'рукопожатия нет') : null,
        f.ssh !== null && f.ssh !== undefined ? tick(f.ssh, 'SSH', f.ssh ? 'сервер отвечает' : 'не отвечает') : null,
        tick(!ruAll.length ? null : ruAll.every(x => x === 'ok') ? true : ruAll.some(x => x === 'ok') ? 'warn' : false, 'Из России',
          !ruAll.length ? 'чекеры ещё не проверяли' : `${ruAll.filter(x => x === 'ok').length} из ${ruAll.length} чекеров пропускают` + (f.checked_at ? ` · ${ago(f.checked_at * 1000)}` : ''))))];
}
