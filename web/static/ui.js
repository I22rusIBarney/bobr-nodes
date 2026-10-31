/* Общие помощники Mini App: тема Telegram, API, элементы iOS, навигация. */
(function () {
  const tg = (window.Telegram && window.Telegram.WebApp && window.Telegram.WebApp.initData !== undefined) ? window.Telegram.WebApp : null;
  const at = v => { try { return !!tg && tg.isVersionAtLeast(v); } catch (e) { return false; } };

  // ── Тема: графитовая дизайн-система Bobr Nodes; Telegram красим в цвет фона ──
  const BG = '#0D1016';
  function applyTheme() {
    const root = document.documentElement;
    root.dataset.scheme = 'dark';
    root.classList.add('premium');
    if (!tg) return;
    try { if (at('6.1')) { tg.setHeaderColor(BG); tg.setBackgroundColor(BG); } } catch (e) {}
    try { if (at('7.10')) tg.setBottomBarColor(BG); } catch (e) {}
  }

  const UI = {
    tg,
    init() {
      try { tg && tg.ready(); tg && tg.expand(); } catch (e) {}
      // запрет масштабирования щипком и двойным тапом (iOS игнорирует user-scalable=no)
      ['gesturestart', 'gesturechange', 'gestureend'].forEach(t => document.addEventListener(t, e => e.preventDefault(), { passive: false }));
      document.addEventListener('touchmove', e => { if (e.touches && e.touches.length > 1) e.preventDefault(); }, { passive: false });
      // двойной тап отключён через CSS touch-action: pan-y — без перехвата касаний, чтобы не терять быстрые нажатия
      applyTheme();
      try { tg && tg.onEvent('themeChanged', applyTheme); } catch (e) {}
      try { if (at('7.7')) tg.disableVerticalSwipes(); } catch (e) {}
      // на телефонах — во весь экран; отступы под кнопки Telegram задают CSS-переменные --tg-*-safe-area-inset-*
      try {
        if (tg && at('8.0') && ['ios', 'android'].includes(tg.platform) && !tg.isFullscreen) tg.requestFullscreen();
        if (tg) tg.onEvent('fullscreenChanged', () => document.documentElement.classList.toggle('tg-fullscreen', !!tg.isFullscreen));
        if (tg && tg.isFullscreen) document.documentElement.classList.add('tg-fullscreen');
        // отступы под кнопки Telegram: берём из WebApp сами — CSS-переменные приходят не во всех версиях клиента
        const insets = () => {
          const st = document.documentElement.style, sa = tg.safeAreaInset || {}, ca = tg.contentSafeAreaInset || {};
          let top = ca.top || 0;
          if (tg.isFullscreen && !top && ['ios', 'android'].includes(tg.platform)) top = 48;  // клиент не сообщил — высота строки «Закрыть»
          if (sa.top) st.setProperty('--tg-safe-area-inset-top', sa.top + 'px');
          if (sa.bottom) st.setProperty('--tg-safe-area-inset-bottom', sa.bottom + 'px');
          st.setProperty('--tg-content-safe-area-inset-top', top + 'px');
        };
        if (tg) { insets(); ['safeAreaChanged', 'contentSafeAreaChanged', 'fullscreenChanged'].forEach(e => tg.onEvent(e, insets)); }
      } catch (e) {}
    },
    initData() { return tg ? tg.initData : ''; },

    api(base) {
      return async function (path, method = 'GET', body) {
        let r;
        // запрос не должен висеть вечно: иначе экран навсегда остаётся «загружается»
        const ctl = window.AbortController ? new AbortController() : null;
        const timer = ctl ? setTimeout(() => ctl.abort(), method === 'GET' ? 45000 : 120000) : 0;
        try {
          r = await fetch(base + path, {
            method, headers: { 'Content-Type': 'application/json', 'X-Init-Data': UI.initData() },
            body: body === undefined ? undefined : JSON.stringify(body), signal: ctl ? ctl.signal : undefined,
          });
        } catch (e) {
          const err = new Error(e && e.name === 'AbortError' ? 'Сервер бота не ответил вовремя. Попробуйте ещё раз.' : 'Нет соединения с сервером. Проверьте интернет и попробуйте снова.');
          err.status = 0; throw err;
        } finally { clearTimeout(timer); }
        let j = null; try { j = await r.json(); } catch (e) {}
        if (!r.ok) { const err = new Error(UI.errText(j, r.status)); err.status = r.status; throw err; }
        return j;
      };
    },
    errText(j, status) {
      const d = j && (j.detail !== undefined ? j.detail : j.error);
      if (typeof d === 'string' && d) return d;
      if (Array.isArray(d)) return d.map(x => (x && (x.msg || x.message)) || String(x)).join('; ');
      if (d && typeof d === 'object') return d.msg || d.message || JSON.stringify(d);
      if (status === 403) return 'Нет доступа. Откройте приложение из Telegram.';
      if (status >= 500) return 'Сервер не смог выполнить запрос. Попробуйте ещё раз через минуту.';
      return 'Ошибка ' + status;
    },

    // ── элементы ──
    h(tag, props, ...kids) {
      const n = document.createElement(tag);
      for (const [k, v] of Object.entries(props || {})) {
        if (v === undefined || v === null || v === false) continue;
        if (k === 'class') n.className = v;
        else if (k === 'style') n.style.cssText = v;
        else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
        else if (k in n && typeof v !== 'string') n[k] = v;
        else n.setAttribute(k, v === true ? '' : v);
      }
      for (const k of kids.flat(Infinity)) if (k !== null && k !== undefined && k !== false) n.append(k.nodeType ? k : String(k));
      return n;
    },
    icon(name, cls) { const s = document.createElement('span'); s.style.display = 'contents'; s.innerHTML = ICONS[name] || ''; const svg = s.firstElementChild; if (svg && cls) svg.setAttribute('class', cls); return svg || s; },
    rub(v, frac) { return new Intl.NumberFormat('ru-RU', { maximumFractionDigits: frac === undefined ? (Number.isInteger(+v) ? 0 : 2) : frac }).format(+v || 0) + ' ₽'; },
    date(iso) { if (!iso) return '—'; const d = new Date(iso); return d.toLocaleDateString('ru-RU', { day: 'numeric', month: 'long' }) + ', ' + d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' }); },
    flag(code) { const c = String(code || '').toUpperCase(); return /^[A-Z]{2}$/.test(c) ? String.fromCodePoint(...[...c].map(ch => 0x1F1E6 + ch.charCodeAt(0) - 65)) : '🌍'; },
    plural(n, one, few, many) { const a = Math.abs(n) % 100, b = a % 10; return a > 10 && a < 20 ? many : b > 1 && b < 5 ? few : b === 1 ? one : many; },

    /* Плавное изменение числа (цены, баланса): from → to за ms, формат fmt. */
    tween(el, from, to, fmt, ms = 260) {
      cancelAnimationFrame(el._tw);
      if (from === to || from == null || matchMedia('(prefers-reduced-motion: reduce)').matches) { el.textContent = fmt(to); return; }
      const t0 = performance.now();
      const stepFn = t => {
        const k = Math.min(1, (t - t0) / ms), e = 1 - Math.pow(1 - k, 3);
        el.textContent = fmt(k < 1 ? Math.round(from + (to - from) * e) : to);
        if (k < 1) el._tw = requestAnimationFrame(stepFn);
      };
      el._tw = requestAnimationFrame(stepFn);
    },
    bump(el) { el.classList.remove('bump'); void el.offsetWidth; el.classList.add('bump'); },
    /* Секция: подпись + карточка; footer — пояснение под карточкой. */
    section(label, body, footer, cls) {
      return UI.h('section', { class: 'section' + (cls ? ' ' + cls : '') }, label ? UI.h('div', { class: 'group-h' }, label) : null,
        body, footer ? UI.h('div', { class: 'group-f' }, footer) : null);
    },

    // ── строки списков ──
    group(header, rows, footer) {
      return UI.h('section', { class: 'group' }, header ? UI.h('div', { class: 'group-h' }, header) : null,
        UI.h('div', { class: 'list' }, rows), footer ? UI.h('div', { class: 'group-f' }, footer) : null);
    },
    /* Строка списка. status: [текст, 'ok'|'bad'|'warn'|'run', подпись] — точка и слово справа (без текста — одна точка); mono — detail машинным шрифтом;
       copy — значение, которое копируется кнопкой справа (IP, пароль, ключ). Иконки без цветных подложек. */
    row({ title, sub, detail, icon, color, flag, lead: lead0, onClick, chevron, cls, right, label, status, mono, copy, copyLabel, tags }) {
      const tap = !!onClick;
      const lead = lead0 ? lead0 : icon ? UI.tile(icon, color)
        : flag ? UI.h('span', { class: 'flag-tile', 'aria-hidden': 'true' }, flag) : null;
      // status без текста — только точка (успешно, на связи); третий элемент — подпись для экранного диктора
      let dot = null;
      if (status && !status[0]) dot = UI.h('span', { class: 'st dot ' + (status[1] || ''), role: 'img', 'aria-label': status[2] || '', title: status[2] || '' });
      else if (status) right = UI.h('span', { class: 'st ' + (status[1] || '') + (status[3] ? ' ' + status[3] : ''), 'aria-label': status[2] || null }, status[0]);
      if (copy) right = UI.h('button', { class: 'icon-btn', type: 'button', 'aria-label': 'Скопировать ' + (copyLabel || title),
        onclick: e => { e.stopPropagation(); UI.copy(copy, copyLabel || title); } }, UI.icon('copy'));
      const n = UI.h(tap ? 'button' : 'div', {
        class: 'row' + (tap ? ' tap' : '') + (lead ? ' has-icon' : '') + (cls ? ' ' + cls : ''), onclick: onClick, type: tap ? 'button' : undefined, 'aria-label': label,
      }, lead,
        UI.h('div', { class: 'grow' }, UI.h('div', { class: 't' }, title), sub ? UI.h('div', { class: 's' }, sub) : null,
          tags && tags.length ? UI.h('div', { class: 'tags' }, tags.map(t => UI.h('span', { class: 'tag ' + (t[1] || '') }, t[0]))) : null),
        dot,
        detail !== undefined && detail !== null ? UI.h('span', { class: 'd' + (mono ? ' mono' : ' num') }, detail) : null,
        right || null,
        (chevron !== undefined ? chevron : tap && !/accent|destructive/.test(cls || '')) ? UI.icon('chevron', 'chev') : null);
      return n;
    },
    /* Раскрывающийся блок: заголовок, сводка текущего значения и содержимое по нажатию. key — запомнить состояние. */
    fold({ title, summary, open, key, body }) {
      const store = UI._folds || (UI._folds = {});
      let isOpen = key && key in store ? store[key] : !!open;
      const chev = UI.icon('chevron', 'chev');
      const btn = UI.h('button', { class: 'fold', type: 'button', 'aria-expanded': String(isOpen) },
        UI.h('div', { class: 'grow' }, UI.h('div', { class: 't' }, title), summary ? UI.h('div', { class: 's' }, summary) : null), chev);
      const box = UI.h('div', { class: 'fold-body', hidden: !isOpen });
      const fill = () => { if (isOpen && !box.childElementCount) box.append(...[].concat(typeof body === 'function' ? body() : body).filter(Boolean)); };
      btn.onclick = () => { isOpen = !isOpen; if (key) store[key] = isOpen; btn.setAttribute('aria-expanded', String(isOpen)); box.hidden = !isOpen; fill(); UI.haptic('select'); };
      fill();
      return [btn, box];
    },
    toggle(checked, onChange, label) {
      const i = UI.h('input', { type: 'checkbox', role: 'switch', 'aria-label': label });
      i.checked = !!checked;
      i.addEventListener('change', () => { UI.haptic('select'); onChange(i.checked, i); });
      return UI.h('label', { class: 'switch' }, i, UI.h('span'));
    },
    segmented(options, value, onChange, cls) {
      const wrap = UI.h('div', { class: 'segmented' + (cls ? ' ' + cls : ''), role: 'group' });
      options.forEach(o => {
        const b = UI.h('button', { type: 'button', 'aria-pressed': String(o.value === value) }, o.label);
        b.onclick = () => { if (o.value === value) return; value = o.value; [...wrap.children].forEach((c, i) => c.setAttribute('aria-pressed', String(options[i].value === value))); UI.haptic('select'); onChange(o.value); };
        wrap.append(b);
      });
      requestAnimationFrame(() => UI.scrollHint(wrap));
      return wrap;
    },

    chips(options, value, onChange) {
      const wrap = UI.h('div', { class: 'chips', role: 'group' });
      options.forEach(o => {
        const b = UI.h('button', { type: 'button', 'aria-pressed': String(o.value === value), title: String(o.label).length > 28 ? o.label : null },
          o.flag ? UI.h('span', { class: 'f', 'aria-hidden': 'true' }, o.flag) : null, UI.h('span', { class: 'l' }, o.label));
        b.onclick = () => { if (o.value === value) return; value = o.value; UI.haptic('select'); onChange(o.value); };
        wrap.append(b);
      });
      requestAnimationFrame(() => { const on = wrap.querySelector('[aria-pressed="true"]'); if (on && on.offsetLeft + on.offsetWidth > wrap.clientWidth) wrap.scrollLeft = on.offsetLeft - 16; UI.scrollHint(wrap); });
      return wrap;
    },
    /* Горизонтальная лента, которая не помещается: тающий край, пока не докрутили до конца. */
    scrollHint(el) {
      const upd = () => { const more = el.scrollWidth > el.clientWidth + 2; el.classList.toggle('more', more); el.classList.toggle('end', more && el.scrollLeft + el.clientWidth >= el.scrollWidth - 2); };
      upd(); el.addEventListener('scroll', upd, { passive: true });
    },
    /* Блок «не загрузилось»: что случилось и «Повторить». */
    failed(message, retry, what) {
      return UI.h('div', { class: 'failed', role: 'alert' }, UI.h('p', {}, UI.h('b', {}, what || 'Не удалось загрузить'), message),
        retry ? UI.h('button', { class: 'btn secondary', type: 'button', onclick: retry }, 'Повторить') : null);
    },

    /* Цветная плитка раздела: иконка на мягкой подложке своего цвета (blue, violet, teal, amber, green, pink, gray). */
    tile(name, color, cls) {
      return UI.h('span', { class: 'icon-tile' + (cls ? ' ' + cls : ''), 'data-c': color || null, style: color ? `--c:var(--ic-${color})` : null, 'aria-hidden': 'true' }, UI.icon(name));
    },
    /* Стопка отдельных карточек — для групп, нод, пользователей. */
    stack(rows) { return UI.h('div', { class: 'stack' }, rows); },
    /* Фильтры со счётчиками: options [{value, label, n, tone}] */
    fchips(options, value, onChange) {
      const wrap = UI.h('div', { class: 'fchips', role: 'group' });
      options.forEach(o => wrap.append(UI.h('button', { type: 'button', 'aria-pressed': String(o.value === value),
        onclick: () => { if (o.value === value) return; UI.haptic('select'); onChange(o.value); } },
        o.label, o.n != null ? UI.h('span', { class: 'c' + (o.tone ? ' ' + o.tone : '') }, String(o.n)) : null)));
      requestAnimationFrame(() => UI.scrollHint(wrap));
      return wrap;
    },
    /* Поиск: значение живёт снаружи (get/set), перерисовка списка — onInput. */
    search(value, placeholder, onInput) {
      const i = UI.h('input', { type: 'search', placeholder, 'aria-label': placeholder, autocomplete: 'off', autocapitalize: 'off', spellcheck: false, enterkeyhint: 'search' });
      i.value = value || '';
      const x = UI.h('button', { class: 'x', type: 'button', 'aria-label': 'Очистить', hidden: !i.value }, UI.icon('close'));
      i.addEventListener('input', () => { x.hidden = !i.value; onInput(i.value); });
      x.onclick = () => { i.value = ''; x.hidden = true; onInput(''); i.focus(); };
      return UI.h('label', { class: 'search' }, UI.icon('search'), i, x);
    },
    /* График за период: series [{color, pts:[[t, v]]}], max — верх шкалы. Первая серия — с заливкой. */
    chart(series, { t0, t1, max = 100, limit } = {}) {
      const NS = 'http://www.w3.org/2000/svg', W = 300, H = 96;
      const el = (tag, attrs) => { const n = document.createElementNS(NS, tag); for (const k in attrs) n.setAttribute(k, attrs[k]); return n; };
      const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: 'none', 'aria-hidden': 'true' });
      const span = Math.max(1, t1 - t0);
      const X = t => (W * (t - t0) / span), Y = v => H - 4 - (H - 8) * Math.max(0, Math.min(max, v)) / max;
      [0.25, 0.5, 0.75].forEach(f => svg.append(el('line', { x1: 0, x2: W, y1: (H * f).toFixed(1), y2: (H * f).toFixed(1), stroke: 'rgba(255,255,255,.05)', 'stroke-width': 1, 'vector-effect': 'non-scaling-stroke' })));
      if (limit != null) svg.append(el('line', { x1: 0, x2: W, y1: Y(limit).toFixed(1), y2: Y(limit).toFixed(1), stroke: 'rgba(240,93,94,.45)', 'stroke-dasharray': '3 4', 'stroke-width': 1, 'vector-effect': 'non-scaling-stroke' }));
      series.forEach((s, k) => {
        const pts = s.pts.filter(p => p[1] != null && p[0] >= t0).map(p => [X(p[0]), Y(p[1])]);
        if (pts.length < 2) return;
        // сглаживание: кривая через середины отрезков
        let d = `M${pts[0][0].toFixed(1)},${pts[0][1].toFixed(1)}`;
        for (let i = 1; i < pts.length; i++) { const [x0, y0] = pts[i - 1], [x1, y1] = pts[i], mx = (x0 + x1) / 2; d += ` C${mx.toFixed(1)},${y0.toFixed(1)} ${mx.toFixed(1)},${y1.toFixed(1)} ${x1.toFixed(1)},${y1.toFixed(1)}`; }
        if (k === 0) {
          const id = 'g' + Math.random().toString(36).slice(2, 8), grad = el('linearGradient', { id, x1: 0, y1: 0, x2: 0, y2: 1 });
          grad.append(el('stop', { offset: '0', 'stop-color': s.color, 'stop-opacity': '.32' }), el('stop', { offset: '1', 'stop-color': s.color, 'stop-opacity': '0' }));
          const defs = el('defs', {}); defs.append(grad); svg.append(defs);
          svg.append(el('path', { d: d + ` L${pts[pts.length - 1][0].toFixed(1)},${H} L${pts[0][0].toFixed(1)},${H} Z`, fill: `url(#${id})` }));
        }
        svg.append(el('path', { d, fill: 'none', stroke: s.color, 'stroke-width': k === 0 ? 2 : 1.6, 'stroke-linejoin': 'round', 'stroke-linecap': 'round', 'vector-effect': 'non-scaling-stroke', opacity: k === 0 ? 1 : .9 }));
      });
      return UI.h('div', { class: 'chart' }, svg);
    },

    /* Аватар: фото из Telegram, иначе инициалы на цветном круге. src — функция, возвращающая Promise<url|null>. */
    avatar(user, size, src) {
      const name = (user && (user.first_name || user.username)) || '?';
      const letters = name.replace(/^@/, '').split(/\s+/).filter(Boolean).slice(0, 2).map(w => w[0].toUpperCase()).join('') || '?';
      const colors = ['#FF6B6B', '#F7A23B', '#34C759', '#30B0C7', '#007AFF', '#5856D6', '#AF52DE', '#FF2D55'];
      const id = Number(user && (user.id || user.tg_id)) || 0;
      const n = UI.h('span', { class: 'avatar', 'aria-hidden': 'true', style: `width:${size}px;height:${size}px;font-size:${Math.round(size * 0.4)}px;--ava-bg:${colors[id % colors.length]}` }, letters);
      if (src) Promise.resolve(src()).then(url => { if (!url) return; const img = new Image(); img.alt = ''; img.onload = () => n.replaceChildren(img); img.src = url; }).catch(() => {});
      return n;
    },
    async blobUrl(path) {
      const r = await fetch(path, { headers: { 'X-Init-Data': UI.initData() } });
      if (!r.ok) return null;
      return URL.createObjectURL(await r.blob());
    },

    // ── обратная связь ──
    haptic(kind) {
      try {
        if (!tg || !at('6.1')) return;
        if (kind === 'select') tg.HapticFeedback.selectionChanged();
        else if (kind === 'success' || kind === 'error' || kind === 'warning') tg.HapticFeedback.notificationOccurred(kind);
        else tg.HapticFeedback.impactOccurred(kind || 'light');
      } catch (e) {}
    },
    toast(text, isErr) {
      let t = document.querySelector('.toast');
      if (!t) { t = UI.h('div', { class: 'toast', role: 'status', 'aria-live': 'polite' }); document.body.append(t); }
      t.className = 'toast' + (isErr ? ' err' : '');
      t.replaceChildren(UI.icon(isErr ? 'alert' : 'checkCircle'), UI.h('span', {}, text));
      requestAnimationFrame(() => t.classList.add('show'));
      clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('show'), 2200);
      UI.haptic(isErr ? 'error' : 'success');
    },
    alert(msg) { return new Promise(r => { try { if (at('6.2')) return tg.showAlert(msg, () => r()); } catch (e) {} UI.dialog({ message: msg, buttons: [{ id: 'ok', text: 'Понятно' }], cancel: false }).then(() => r()); }); },
    /* Свой диалог поверх страницы — когда системный Telegram недоступен или не вмещает кнопки.
       В отличие от window.confirm не показывает адрес сайта и подписывает кнопки по смыслу. */
    dialog({ title, message, buttons, cancel = true }) {
      return new Promise(res => {
        const scrim = UI.h('div', { class: 'scrim dlg-scrim' });
        let done = false;
        const close = id => {
          if (done) return; done = true;
          box.classList.remove('show'); scrim.classList.remove('show');
          setTimeout(() => { box.remove(); scrim.remove(); }, 250);
          UI.nav._sheetClose = prevClose; UI.nav.syncBack();
          res(id);
        };
        const btns = buttons.map(b => UI.h('button', { class: 'btn ' + (b.type === 'destructive' ? 'destructive' : 'tinted'), type: 'button', onclick: () => close(b.id) }, b.text));
        if (cancel) btns.push(UI.h('button', { class: 'btn plain', type: 'button', onclick: () => close(null) }, 'Отмена'));
        const box = UI.h('div', { class: 'dlg', role: 'alertdialog', 'aria-modal': 'true', 'aria-label': title || message },
          title ? UI.h('b', { class: 'dlg-t' }, title) : null, message ? UI.h('div', { class: 'dlg-m' }, message) : null,
          UI.h('div', { class: 'dlg-b' }, btns));
        scrim.onclick = () => close(null);
        const prevClose = UI.nav._sheetClose;
        UI.nav._sheetClose = () => close(null); UI.nav.syncBack();
        document.body.append(scrim, box);
        requestAnimationFrame(() => { scrim.classList.add('show'); box.classList.add('show'); });
      });
    },
    /* Диалог действия. buttons: [{id, text, type:'default'|'destructive'}]; кнопка «Отмена» добавляется сама. */
    ask({ title, message, buttons }) {
      return new Promise(res => {
        try {
          // системный попап Telegram: не больше 3 кнопок (с «Отменой»), заголовок до 64 и текст до 256 символов
          if (at('6.2') && buttons.length <= 2 && (title || '').length <= 64 && (message || '').length <= 256 && buttons.every(b => b.text.length <= 64)) {
            return tg.showPopup({ title: title || '', message, buttons: [...buttons.map(b => ({ id: b.id, type: b.type || 'default', text: b.text })), { type: 'cancel' }] }, id => res(id || null));
          }
        } catch (e) {}
        UI.dialog({ title, message, buttons }).then(res);
      });
    },
    async run(fn, okText) {
      try { const r = await fn(); if (okText) UI.toast(okText); return r; }
      catch (e) { UI.haptic('error'); await UI.alert(e.message || String(e)); return undefined; }
    },
    busy(btn, fn) { return async (ev) => { if (btn.disabled) return; btn.disabled = true; try { await fn(ev); } finally { btn.disabled = false; } }; },
    copy(text, what) {
      const done = () => UI.toast((what || 'Текст') + ' скопирован');
      if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done, () => fallback());
      else fallback();
      function fallback() { const ta = UI.h('textarea', { style: 'position:fixed;opacity:0' }); ta.value = text; document.body.append(ta); ta.select(); try { document.execCommand('copy'); done(); } catch (e) {} ta.remove(); }
    },
    openLink(url) { if (!/^https:\/\//i.test(String(url || ''))) return; try { if (tg) return tg.openLink(url); } catch (e) {} window.open(url, '_blank'); },

    /* Переписка: messages [{author, text, created}], mine — какие авторы «свои» (справа). */
    chat(messages, mine, labels) {
      const box = UI.h('div', { class: 'chat' });
      let day = '';
      messages.forEach(m => {
        const d = new Date(m.created), dd = d.toLocaleDateString('ru-RU', { day: 'numeric', month: 'long' });
        if (dd !== day) { day = dd; box.append(UI.h('div', { class: 'day-sep' }, dd)); }
        const t = d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
        if (m.author === 'system') return box.append(UI.h('div', { class: 'msg sys' }, UI.h('div', { class: 'bubble' }, m.text + ' · ' + t)));
        if (m.author === 'note') return box.append(UI.h('div', { class: 'msg note' }, UI.h('div', { class: 'bubble' }, UI.h('div', { class: 'who' }, (labels && labels.note) || 'Заметка'), m.text), UI.h('time', {}, t)));
        const cls = mine.includes(m.author) ? 'mine' : 'theirs';
        box.append(UI.h('div', { class: 'msg ' + cls }, UI.h('div', { class: 'bubble' }, m.text), UI.h('time', {}, (labels && labels[m.author] ? labels[m.author] + ' · ' : '') + t + (m.source === 'bot' ? ' · из чата' : ''))));
      });
      return box;
    },
    /* Поле ввода внизу экрана. onSend(text, extra) → Promise; extra — состояние доп. кнопки (например, «заметка»). */
    composer(placeholder, onSend, modeLabel) {
      const ta = UI.h('textarea', { rows: 1, placeholder, 'aria-label': placeholder, maxlength: 4000 });
      const send = UI.h('button', { class: 'send', type: 'button', 'aria-label': 'Отправить', disabled: true }, UI.icon('send'));
      let mode = false;
      const modeBtn = modeLabel ? UI.h('button', { class: 'mode', type: 'button', 'aria-pressed': 'false', onclick: () => { mode = !mode; modeBtn.classList.toggle('on', mode); modeBtn.setAttribute('aria-pressed', String(mode)); ta.placeholder = mode ? 'Заметка для себя' : placeholder; UI.haptic('select'); } }, modeLabel) : null;
      const fit = () => { ta.style.height = 'auto'; ta.style.height = Math.min(ta.scrollHeight, 140) + 'px'; send.disabled = !ta.value.trim(); };
      ta.addEventListener('input', fit);
      send.onclick = UI.busy(send, async () => { const text = ta.value.trim(); if (!text) return; const ok = await onSend(text, mode); if (ok !== false) { ta.value = ''; fit(); } });
      return UI.h('div', { class: 'composer' }, modeBtn, ta, send);
    },

    // ── нижний лист ──
    sheet(title, content) {
      const scrim = UI.h('div', { class: 'scrim' });
      const close = () => { sh.classList.remove('show'); scrim.classList.remove('show'); setTimeout(() => { sh.remove(); scrim.remove(); }, 350); UI.nav._sheetClose = null; UI.nav.syncBack(); };
      const sh = UI.h('div', { class: 'sheet', role: 'dialog', 'aria-modal': 'true', 'aria-label': title },
        UI.h('div', { class: 'grabber' }),
        UI.h('div', { class: 'sheet-h' }, UI.h('b', {}, title), UI.h('button', { class: 'btn plain', style: 'width:auto;min-height:0;padding:4px 0', onclick: close }, 'Готово')),
        typeof content === 'function' ? content(close) : content);
      scrim.onclick = close;
      document.body.append(scrim, sh);
      requestAnimationFrame(() => { scrim.classList.add('show'); sh.classList.add('show'); });
      UI.nav._sheetClose = close; UI.nav.syncBack();
      return close;
    },

    // ── навигация со стеком и системной кнопкой «Назад» ──
    /* Разделы внизу (tabs: [{key, label, icon, root}]) — у каждого свой стек экранов; повторное нажатие раздела — к его началу. */
    nav: {
      stack: [], root: null, _sheetClose: null, _onRoot: null, tabs: null, tab: null, stacks: {}, bar: null, _scroll: {},
      mount(rootEl, renderRoot, tabs) {
        this.root = rootEl; this.stack = [];
        if (tabs && tabs.length) {
          this.tabs = tabs; this.tab = tabs[0].key; this._onRoot = tabs[0].root;
          this.bar = UI.h('nav', { class: 'tabbar', 'aria-label': 'Разделы' }, tabs.map(t => UI.h('button', { type: 'button', 'data-k': t.key, onclick: () => this.setTab(t.key) },
            UI.h('span', { class: 'tb-ic' }, UI.icon(t.icon)), t.label)));
          document.body.append(this.bar);
        } else this._onRoot = renderRoot;
        this.render(false);
        try { if (tg && at('6.1')) tg.BackButton.onClick(() => this.back()); } catch (e) {}
      },
      setTab(key) {
        if (!this.tabs) return;
        if (this._sheetClose) this._sheetClose();
        if (key === this.tab) { if (this.stack.length) { this.stack = []; this.render(true); } else window.scrollTo({ top: 0, behavior: 'smooth' }); return; }
        this.stacks[this.tab] = this.stack; this._scroll[this.tab] = window.scrollY;
        this.tab = key; this.stack = this.stacks[key] || [];
        this._onRoot = this.tabs.find(t => t.key === key).root;
        UI.haptic('select');
        this.render('tab');
        window.scrollTo(0, this._scroll[key] || 0);
      },
      pip(key, on, cls) { const b = this.bar && this.bar.querySelector(`[data-k="${key}"]`); if (!b) return; let p = b.querySelector('.pip'); if (on && !p) b.append(p = UI.h('span', { class: 'pip' })); if (!on && p) p.remove(); if (p) p.className = 'pip' + (cls ? ' ' + cls : ''); },
      push(render) { this.stack.push(render); this.render(false); window.scrollTo(0, 0); UI.haptic('light'); },
      back() { if (this._sheetClose) return this._sheetClose(); if (!this.stack.length) return; this.stack.pop(); this.render(true); },
      reset() { this.stack = []; this.render(false); },
      refresh() { this.render(null); },
      render(isPop) {
        const fn = this.stack.length ? this.stack[this.stack.length - 1] : this._onRoot;
        let node;
        // экран упал — показываем, что случилось, а не молча остаёмся на прежнем
        try { node = fn(); } catch (e) {
          console.error(e);
          node = UI.h('div', {}, UI.h('h1', { class: 'large-title' }, 'Экран не открылся'),
            UI.failed(String((e && e.message) || e) + (e && e.stack ? '\n' + String(e.stack).split('\n').slice(0, 2).join('\n') : ''),
              this.stack.length ? () => this.back() : () => this.refresh(), 'Ошибка интерфейса'));
        }
        const v = UI.h('div', { class: isPop === null ? '' : 'view' + (isPop === 'tab' ? ' tab' : isPop ? ' pop' : '') }, node);
        this.root.replaceChildren(v);
        document.body.classList.toggle('has-composer', !!v.querySelector('.composer'));
        if (this.bar) this.bar.querySelectorAll('button').forEach(b => b.setAttribute('aria-current', b.dataset.k === this.tab ? 'page' : 'false'));
        this.syncBack();
        document.dispatchEvent(new CustomEvent('navchange', { detail: { depth: this.stack.length, tab: this.tab } }));
      },
      syncBack() { try { if (tg && at('6.1')) (this.stack.length || this._sheetClose) ? tg.BackButton.show() : tg.BackButton.hide(); } catch (e) {} },
      backLink(text) { // для браузера вне Telegram, где нет системной кнопки
        if (tg && at('6.1')) return document.createTextNode('');  // в Telegram — системная кнопка «Назад»
        return UI.h('div', { class: 'nav' }, UI.h('button', { class: 'back', type: 'button', onclick: () => UI.nav.back() }, UI.icon('back'), text || 'Назад'));
      },
    },
  };

  const S = (d, extra = '') => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" ${extra}>${d}</svg>`;
  const ICONS = {
    chevron: '<svg viewBox="0 0 8 13" aria-hidden="true"><path d="M1.5 1.5 6.5 6.5l-5 5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    back: '<svg viewBox="0 0 12 20" width="12" height="20" aria-hidden="true"><path d="M10 2 2 10l8 8" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    server: S('<rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 7.5h.01M7 16.5h.01"/>'),
    plus: S('<path d="M12 5v14M5 12h14"/>'),
    plusCircle: S('<circle cx="12" cy="12" r="9"/><path d="M12 8v8M8 12h8"/>'),
    wallet: S('<path d="M3 7a2 2 0 0 1 2-2h12v4"/><rect x="3" y="7" width="18" height="13" rx="2"/><path d="M16.5 13.5h.01"/>'),
    gear: S('<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>'),
    search: S('<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>'),
    grip: S('<path d="M5 9h14M5 15h14"/>'),
    copy: S('<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1"/>'),
    eye: S('<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>'),
    eyeOff: S('<path d="M17.9 17.9A10.4 10.4 0 0 1 12 19c-6.4 0-10-7-10-7a18 18 0 0 1 4.1-5M9.9 5.2A9.6 9.6 0 0 1 12 5c6.4 0 10 7 10 7a18 18 0 0 1-2.2 3.2M3 3l18 18"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/>'),
    power: S('<path d="M12 3v8"/><path d="M6.3 6.3a8 8 0 1 0 11.4 0"/>'),
    restart: S('<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/>'),
    stop: S('<rect x="6" y="6" width="12" height="12" rx="2"/>'),
    terminal: S('<rect x="3" y="4" width="18" height="16" rx="2"/><path d="m7 9 3 3-3 3M13 15h4"/>'),
    globe: S('<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>'),
    key: S('<circle cx="8" cy="15" r="4"/><path d="m10.8 12.2 8.2-8.2M17 6l2 2M15 8l2 2"/>'),
    disc: S('<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="2.5"/>'),
    trash: S('<path d="M4 7h16M10 11v6M14 11v6M5 7l1 12a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2l1-12M9 7V4h6v3"/>'),
    renew: S('<path d="M3 12a9 9 0 0 1 15.5-6.2L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-15.5 6.2L3 16"/><path d="M3 21v-5h5"/>'),
    receipt: S('<path d="M6 3h12v18l-3-2-3 2-3-2-3 2z"/><path d="M9 8h6M9 12h6"/>'),
    chat: S('<path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z"/>'),
    checkCircle: S('<circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/>'),
    check: S('<path d="m5 12.5 4.5 4.5L19 7.5"/>', 'stroke-width="2.4"'),
    alert: S('<circle cx="12" cy="12" r="9"/><path d="M12 7.5v5.5M12 16.5h.01"/>'),
    users: S('<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0"/><path d="M16 4.6a3.5 3.5 0 0 1 0 6.8M18.5 20a6.5 6.5 0 0 0-3-5.5"/>'),
    card: S('<rect x="2.5" y="5" width="19" height="14" rx="2"/><path d="M2.5 10h19M6.5 15h3"/>'),
    tag: S('<path d="M20.6 13.4 13.4 20.6a2 2 0 0 1-2.8 0L3 13V3h10l7.6 7.6a2 2 0 0 1 0 2.8z"/><path d="M7.5 7.5h.01"/>'),
    store: S('<path d="M4 9h16l-1-5H5zM4 9v11h16V9"/><path d="M9 20v-6h6v6"/>'),
    text: S('<path d="M5 6h14M5 12h14M5 18h9"/>'),
    person: S('<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>'),
    doc: S('<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6M8 13h8M8 17h6"/>'),
    shield: S('<path d="M12 3 4 6v6c0 5 3.4 8.4 8 9 4.6-.6 8-4 8-9V6z"/><path d="m9 12 2 2 4-4"/>'),
    gift: S('<rect x="3" y="8" width="18" height="4" rx="1"/><path d="M12 8v13M19 12v9H5v-9M7.5 8a2.5 2.5 0 1 1 0-5C10 3 12 8 12 8s2-5 4.5-5a2.5 2.5 0 1 1 0 5"/>'),
    clock: S('<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>'),
    send: S('<path d="M12 19V5M5 12l7-7 7 7"/>', 'stroke-width="2.4"'),
    lifebuoy: S('<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="3.5"/><path d="m5.6 5.6 3.9 3.9M14.5 14.5l3.9 3.9M18.4 5.6l-3.9 3.9M9.5 14.5l-3.9 3.9"/>'),
    cpu: S('<rect x="6" y="6" width="12" height="12" rx="2"/><rect x="9.5" y="9.5" width="5" height="5" rx="1"/><path d="M9 3v3M15 3v3M9 18v3M15 18v3M3 9h3M3 15h3M18 9h3M18 15h3"/>'),
    ram: S('<rect x="3" y="7" width="18" height="10" rx="2"/><path d="M7 7v10M11 7v10M15 7v10M6 17v3M18 17v3"/>'),
    drive: S('<rect x="3" y="5" width="18" height="14" rx="3"/><path d="M7 15h.01M11 15h6M3 11h18"/>'),
    pin: S('<path d="M12 21s-7-6.2-7-11.5a7 7 0 0 1 14 0C19 14.8 12 21 12 21z"/><circle cx="12" cy="9.5" r="2.5"/>'),
    bolt: S('<path d="M13 3 5 13.5h6L10 21l8-10.5h-6z"/>'),
    arrowDown: S('<path d="M12 5v14M6 13l6 6 6-6"/>'),
    sparkle: S('<path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z"/><path d="M19 16l.7 1.8 1.8.7-1.8.7L19 21l-.7-1.8-1.8-.7 1.8-.7z"/>'),
    share: S('<path d="M21 4 3 11l7 2.5L12.5 21z"/><path d="m21 4-11 9.5"/>'),
    link: S('<path d="M10 14a4.5 4.5 0 0 0 6.4 0l3.2-3.2a4.5 4.5 0 0 0-6.4-6.4L12 5.6"/><path d="M14 10a4.5 4.5 0 0 0-6.4 0l-3.2 3.2a4.5 4.5 0 0 0 6.4 6.4l1.2-1.2"/>'),
    info: S('<circle cx="12" cy="12" r="9"/><path d="M12 11v5.5M12 7.5h.01"/>'),
    bell: S('<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/>'),
    megaphone: S('<path d="M3 10.5v3a1 1 0 0 0 1 1h2.5L12 18V6L6.5 9.5H4a1 1 0 0 0-1 1z"/><path d="M15.5 9a3.5 3.5 0 0 1 0 6M18.5 6a7.5 7.5 0 0 1 0 12"/>'),
    stack: S('<path d="m12 3 9 5-9 5-9-5z"/><path d="m3 13 9 5 9-5"/>'),
    up: S('<path d="M12 19V5M6 11l6-6 6 6"/>'),
    down: S('<path d="M12 5v14M6 13l6 6 6-6"/>'),
    close: S('<path d="M6 6l12 12M18 6 6 18"/>'),
    menu: S('<path d="M4 7h16M4 12h16M4 17h16"/>'),
    arrowLeft: S('<path d="M19 12H5M11 6l-6 6 6 6"/>'),
    home: S('<path d="M4 10.5 12 4l8 6.5V19a1 1 0 0 1-1 1h-4.5v-6h-5v6H5a1 1 0 0 1-1-1z"/>'),
    servers: S('<rect x="3.5" y="4" width="17" height="6.5" rx="2"/><rect x="3.5" y="13.5" width="17" height="6.5" rx="2"/><path d="M7.5 7.25h.01M7.5 16.75h.01M11 7.25h5M11 16.75h5"/>'),
    tools: S('<path d="M14.7 6.3a4 4 0 0 0-5.4 5.2L4 16.8V20h3.2l5.3-5.3a4 4 0 0 0 5.2-5.4l-2.6 2.6-2.4-.6-.6-2.4z"/>'),
    sliders: S('<path d="M4 7h10M18 7h2M4 17h4M12 17h8"/><circle cx="16" cy="7" r="2"/><circle cx="10" cy="17" r="2"/>'),
    more: S('<circle cx="5.5" cy="12" r="1.2" fill="currentColor"/><circle cx="12" cy="12" r="1.2" fill="currentColor"/><circle cx="18.5" cy="12" r="1.2" fill="currentColor"/>'),
    moreV: S('<circle cx="12" cy="5.5" r="1.2" fill="currentColor"/><circle cx="12" cy="12" r="1.2" fill="currentColor"/><circle cx="12" cy="18.5" r="1.2" fill="currentColor"/>'),
    activity: S('<path d="M3 12h4l3-8 4 16 3-8h4"/>'),
    sun: S('<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M4.6 4.6l1.4 1.4M18 18l1.4 1.4M2.5 12h2M19.5 12h2M4.6 19.4 6 18M18 6l1.4-1.4"/>'),
    code: S('<path d="m9 8-4 4 4 4M15 8l4 4-4 4"/>'),
    palette: S('<path d="M12 3a9 9 0 1 0 0 18c1.1 0 1.7-.8 1.7-1.7 0-.5-.2-.9-.5-1.2-.3-.3-.5-.7-.5-1.2 0-.9.8-1.7 1.7-1.7H16a5 5 0 0 0 5-5c0-4-4-7.2-9-7.2z"/><circle cx="7.5" cy="11" r="1" fill="currentColor"/><circle cx="10.5" cy="7.5" r="1" fill="currentColor"/><circle cx="15" cy="7.5" r="1" fill="currentColor"/>'),
    userPlus: S('<circle cx="10" cy="8" r="3.5"/><path d="M3.5 20a6.5 6.5 0 0 1 13 0M19 8v6M16 11h6"/>'),
    cloud: S('<path d="M7 18.5h10.5a4 4 0 0 0 .6-7.95A6 6 0 0 0 6.6 9.1 4.75 4.75 0 0 0 7 18.5z"/>'),
    upload: S('<path d="M12 16V4M7 9l5-5 5 5M4 16v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3"/>'),
    swap: S('<path d="M7 4 3 8l4 4M3 8h13M17 12l4 4-4 4M21 16H8"/>'),
    list: S('<path d="M9 6h11M9 12h11M9 18h11M4.5 6h.01M4.5 12h.01M4.5 18h.01"/>'),
    play: S('<path d="M7 5v14l11-7z" fill="currentColor" stroke="none"/>'),
    paste: S('<rect x="6" y="4" width="12" height="17" rx="2"/><path d="M9 4h6v3H9zM9 12h6M9 16h4"/>'),
    zap: S('<path d="M13 3 5 13.5h6L10 21l8-10.5h-6z"/>'),
    layers: S('<path d="m12 3 9 5-9 5-9-5z"/><path d="m3 13 9 5 9-5"/>'),
    route: S('<circle cx="6" cy="18" r="2.5"/><circle cx="18" cy="6" r="2.5"/><path d="M8.5 18H16a3 3 0 0 0 0-6H8a3 3 0 0 1 0-6h7.5"/>'),
    chart: S('<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>'),
    calendar: S('<rect x="3.5" y="5" width="17" height="15" rx="2"/><path d="M3.5 10h17M8 3v4M16 3v4"/>'),
    flag: S('<path d="M5 21V4M5 4h12l-2 4 2 4H5"/>'),
  };

  // необработанные ошибки — коротким сообщением: иначе кнопка «молча не нажимается»
  let lastErr = 0;
  const report = msg => { if (Date.now() - lastErr < 3000) return; lastErr = Date.now(); try { UI.toast('Ошибка: ' + String(msg).slice(0, 140), true); } catch (e) {} };
  window.addEventListener('error', e => report(e.message || 'сбой скрипта'));
  // касание пришлось не на интерфейс (что-то прозрачное поверх) — назвать перехватчика, чтобы найти и убрать
  document.addEventListener('click', e => {
    const t = e.target;
    if (!(t instanceof Element) || t === document.body || t === document.documentElement || t.closest('#root, .tabbar, .sheet, .dlg, .scrim, .toast, .composer')) return;
    report('касание перехватил ' + t.tagName.toLowerCase() + (t.id ? '#' + t.id : '') + (t.className && typeof t.className === 'string' ? '.' + t.className.trim().split(/\s+/).join('.') : ''));
  }, true);
  window.addEventListener('unhandledrejection', e => report((e.reason && e.reason.message) || e.reason || 'сбой'));

  window.UI = UI;
})();
