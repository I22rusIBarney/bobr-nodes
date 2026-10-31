"""Telegram-чат: меню с кнопкой веб-админки, быстрый мастер развёртывания, задачи и их ход.
Настройки и группы — в веб-админке (bot/web.py)."""
from __future__ import annotations

import html

from aiogram import BaseMiddleware, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo

from . import services as svc
from .access import INVITE_PREFIX, Access, uname
from .bcloud import BcloudError
from .runner import STATUS_ICON, Runner, job_text
from .store import ACTIVE, Store

router = Router()


# ───────────────────────── доступ ─────────────────────────

def invite_token(text: str | None) -> str:
    """Токен из «/start inv_…» — так Telegram передаёт ссылку-приглашение t.me/<бот>?start=inv_…"""
    parts = (text or '').split()
    if len(parts) == 2 and parts[0].split('@')[0] == '/start' and parts[1].startswith(INVITE_PREFIX):
        return parts[1][len(INVITE_PREFIX):][:64]
    return ''


class AdminOnly(BaseMiddleware):
    """Доступ к боту: администраторы (ADMIN_IDS) и приглашённые пользователи. Ссылка-приглашение открывает доступ.
    В обработчики передаются role ('admin' | 'member'), uid и username."""
    def __init__(self, admins: set[int], access: Access | None = None):
        self.admins, self.access = set(admins), access

    async def __call__(self, handler, event, data):
        user = data.get('event_from_user')
        uid = user.id if user else 0
        role = 'admin' if uid in self.admins else (self.access.role(uid) if self.access and uid else None)
        if role is None and self.access and isinstance(event, Message) and invite_token(event.text):
            good, text = self.access.redeem(invite_token(event.text), user)
            if not good:
                await event.answer('❌ ' + text)
                return None
            role = 'member'
            await event.answer('✅ ' + text + ' Группы, которые администратор завёл для вас, уже здесь; '
                               'свою панель можно добавить в «Панели управления».')
            bot = data.get('bot')
            who = html.escape(f'@{user.username}' if user.username else (user.full_name or str(uid)))
            for a in sorted(self.admins):
                try:
                    await bot.send_message(a, f'👤 По приглашению вошёл {who} (<code>{uid}</code>).', parse_mode='HTML')
                except Exception:  # noqa: BLE001 — сообщение админу не обязательно
                    pass
        if role is None:
            if isinstance(event, Message):
                await event.answer('Этот бот работает по приглашениям. Попросите ссылку у администратора.')
            elif isinstance(event, CallbackQuery):
                await event.answer('Нет доступа', show_alert=True)
            return None
        if role == 'member':
            self.access.touch(user)
        data.update(role=role, uid=uid, username=uname(user) if user else '')
        return await handler(event, data)


def my_profiles(store: Store, access: Access | None, role: str, uid: int, username: str = '') -> list[dict]:
    if role == 'admin' or access is None:
        return store.profiles()
    return access.profiles_for(uid, username)


def my_profile(store: Store, access: Access | None, role: str, uid: int, username: str, pid) -> dict | None:
    p = store.profile(int(pid or 0))
    if not p or role == 'admin' or access is None:
        return p
    return p if access.can(p, uid, username) else None


def my_job(store: Store, access: Access | None, role: str, uid: int, username: str, jid: int) -> dict | None:
    j = store.job(jid)
    if not j or role == 'admin' or access is None:
        return j
    return j if my_profile(store, access, role, uid, username, j['profile_id']) else None


# ───────────────────────── помощники ─────────────────────────

def kb(*rows) -> InlineKeyboardMarkup:
    out = []
    for row in rows:
        if not row:
            continue
        line = []
        for item in row:
            if isinstance(item, InlineKeyboardButton):
                line.append(item)
            else:
                line.append(InlineKeyboardButton(text=item[0], callback_data=item[1]))
        out.append(line)
    return InlineKeyboardMarkup(inline_keyboard=out)


BACK = [('« Меню', 'm:main')]


def admin_button(public_url: str, text: str = '⚙️ Панель управления', path: str = '/admin'):
    if not public_url.startswith('https://'):
        return None
    return InlineKeyboardButton(text=text, web_app=WebAppInfo(url=public_url.rstrip('/') + path))


def job_kb(job: dict) -> InlineKeyboardMarkup:
    row = [('📄 Журнал', f'j:{job["id"]}:log')]
    if job['status'] == 'failed':
        row.append(('🔁 Повторить', f'j:{job["id"]}:retry'))
    if job['status'] in ('done', 'failed'):
        row.append(('🙈 Скрыть', f'j:{job["id"]}:hide'))
    return kb(row)


def rub(kop: int) -> str:
    return f'{kop / 100:,.0f}'.replace(',', ' ') + ' ₽'


async def show(target: Message | CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    """В колбэке — правим текущее сообщение, на сообщение пользователя — отвечаем новым."""
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup, parse_mode='HTML', disable_web_page_preview=True)
        except Exception:
            await target.message.answer(text, reply_markup=markup, parse_mode='HTML', disable_web_page_preview=True)
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup, parse_mode='HTML', disable_web_page_preview=True)


async def delete_quietly(m: Message) -> None:
    try:
        await m.delete()
    except Exception:
        pass


class Deploy(StatesGroup):
    count = State()
    domains = State()
    spec = State()


# ───────────────────────── меню ─────────────────────────

def ready_issues(store: Store, profiles: list[dict] | None = None, role: str = 'admin') -> list[str]:
    ps = store.profiles() if profiles is None else profiles
    out = []
    if not all(store.get(k) for k in ('bcloud_url', 'bcloud_token', 'bcloud_tg_id')):
        out.append('подключите Bobr Cloud' if role == 'admin' else 'администратор ещё не подключил Bobr Cloud')
    if role == 'admin' and not store.get('cf_token') and not any(p.get('cf_token') for p in ps):
        out.append('добавьте токен Cloudflare')
    if not ps:
        out.append('создайте группу')
    return out


def main_text(store: Store, profiles: list[dict] | None = None, role: str = 'admin') -> str:
    ps = store.profiles() if profiles is None else profiles
    issues = ready_issues(store, ps, role)
    lines = ['<b>🦫 Bobr Nodes</b>', 'Покупка серверов в Bobr Cloud и установка нод Remnawave.', '']
    if issues:
        lines.append('Чтобы начать, в панели управления: ' + ', '.join(issues) + '.')
    else:
        lines.append(f'Групп: {len(ps)}')
    ids = {p['id'] for p in ps}
    active = sum(1 for j in store.active_jobs() if j['profile_id'] in ids)
    if active:
        lines.append(f'В работе задач: {active}')
    return '\n'.join(lines)


def main_kb(store: Store, public_url: str) -> InlineKeyboardMarkup:
    adm = admin_button(public_url)
    return kb([('🚀 Развернуть ноды', 'm:deploy')], [('📋 Задачи', 'm:jobs')], [adm] if adm else None)


def menu_text(store: Store, access, role: str, uid: int, username: str) -> str:
    return main_text(store, my_profiles(store, access, role, uid, username), role)


@router.message(CommandStart())
@router.message(Command('menu'))
async def start(m: Message, state: FSMContext, store: Store, public_url: str, access: Access | None = None,
                role: str = 'admin', uid: int = 0, username: str = ''):
    await state.clear()
    await show(m, menu_text(store, access, role, uid, username), main_kb(store, public_url))


@router.callback_query(F.data == 'm:main')
async def main_cb(c: CallbackQuery, state: FSMContext, store: Store, public_url: str, access: Access | None = None,
                  role: str = 'admin', uid: int = 0, username: str = ''):
    await state.clear()
    await show(c, menu_text(store, access, role, uid, username), main_kb(store, public_url))


@router.message(Command('cancel'))
async def cancel(m: Message, state: FSMContext, store: Store, public_url: str, access: Access | None = None,
                 role: str = 'admin', uid: int = 0, username: str = ''):
    await state.clear()
    await show(m, 'Отменено.\n\n' + menu_text(store, access, role, uid, username), main_kb(store, public_url))


# ───────────────────────── развёртывание ─────────────────────────

async def wizard(state: FSMContext) -> dict:
    return (await state.get_data()).get('w') or {}


@router.callback_query(F.data == 'm:deploy')
async def deploy_start(c: CallbackQuery, state: FSMContext, store: Store, public_url: str, access: Access | None = None,
                       role: str = 'admin', uid: int = 0, username: str = ''):
    await state.clear()
    ps = my_profiles(store, access, role, uid, username)
    issues = ready_issues(store, ps, role)
    if issues:
        adm = admin_button(public_url)
        return await show(c, 'Сначала в панели управления: ' + ', '.join(issues) + '.', kb([adm] if adm else None, BACK))
    if len(ps) == 1:
        return await deploy_profile(c, state, store, ps[0]['id'])
    await show(c, '<b>🚀 Куда ставим ноды?</b>\n\nВыберите группу:',
               kb(*[[(f'🗂 {p["name"]}', f'd:p:{p["id"]}')] for p in ps], BACK))


@router.callback_query(F.data.startswith('d:p:'))
async def deploy_profile_cb(c: CallbackQuery, state: FSMContext, store: Store, access: Access | None = None,
                            role: str = 'admin', uid: int = 0, username: str = ''):
    if not my_profile(store, access, role, uid, username, c.data[4:]):
        return await c.answer('Группа не найдена', show_alert=True)
    await deploy_profile(c, state, store, int(c.data[4:]))


async def deploy_profile(c: CallbackQuery, state: FSMContext, store: Store, pid: int):
    try:
        cat = await svc.load_catalog(store, store.profile(pid))
    except BcloudError as e:
        return await show(c, f'❌ {html.escape(str(e))}', kb(BACK))
    await state.update_data(w={'pid': pid, 'cat': cat})
    btns = []
    for loc in cat['locations']:
        cheapest = min(o['price_kop'] for o in loc['offers'])
        btns.append([(f'{loc["flag"]} {loc["name"]} · от {rub(cheapest)}', f'd:l:{loc["code"]}')])
    p = store.profile(pid)
    await show(c, f'<b>🚀 {html.escape(p["name"])}</b>\n\nЛокация для новых серверов — или поставьте ноды на уже купленные:',
               kb(*btns, [('📦 Уже купленные серверы', 'd:own')], [('« Отмена', 'm:main')]))


@router.callback_query(F.data.startswith('d:l:'))
async def deploy_loc(c: CallbackQuery, state: FSMContext):
    await state.set_state(None)
    w = await wizard(state)
    if not w:
        return await c.answer('Мастер устарел — начните заново', show_alert=True)
    loc = next(x for x in w['cat']['locations'] if x['code'] == c.data[4:])
    w['loc'] = loc['code']
    await state.update_data(w=w)
    btns = [[(f'{svc.spec_text(o)} — {rub(o["price_kop"])}/мес', f'd:o:{o["idx"]}')] for o in loc['offers']]
    if w['cat'].get('configurator'):
        btns.append([('🛠 Своя конфигурация', 'd:cu')])
    await show(c, f'{loc["flag"]} <b>{html.escape(loc["name"])}</b>\n\nКонфигурация сервера:',
               kb(*btns, [('« Локации', f'd:p:{w["pid"]}')]))


def spec_ranges(conf: dict) -> str:
    def one(k, label):
        r = conf[k]
        return f'{label} {r["min"]}–{r["max"]}' + (f' (шаг {r["step"]})' if r['step'] > 1 else '')
    return ', '.join((one('cpu', 'vCPU'), one('ram', 'память'), one('disk', 'диск')))


@router.callback_query(F.data == 'd:cu')
async def deploy_custom(c: CallbackQuery, state: FSMContext):
    w = await wizard(state)
    if not w.get('loc'):
        return await c.answer('Мастер устарел — начните заново', show_alert=True)
    await state.set_state(Deploy.spec)
    await show(c, '<b>🛠 Своя конфигурация</b>\n\nПришлите через пробел: vCPU, память (ГБ) и диск (ГБ), например <code>2 4 40</code>.\n'
               f'Можно: {spec_ranges(w["cat"]["configurator"])}.\n\n/cancel — отмена',
               kb([('« Готовые тарифы', f'd:l:{w["loc"]}')]))


@router.message(Deploy.spec)
async def deploy_custom_msg(m: Message, state: FSMContext, store: Store):
    w = await wizard(state)
    nums = (m.text or '').replace(',', ' ').split()
    try:
        if len(nums) != 3:
            raise ValueError('нужно три числа: vCPU, память и диск, например 2 4 40')
        offer = await svc.custom_offer(store, store.profile(w['pid']), w['cat'], w['loc'], dict(zip(('cpu', 'ram', 'disk'), nums)))
    except (ValueError, BcloudError) as e:
        return await m.answer(f'❌ {html.escape(str(e))}\nПришлите ещё раз или /cancel', parse_mode='HTML')
    await state.set_state(None)
    w.update(idx=None, custom=offer)
    await state.update_data(w=w)
    await m.answer(f'🛠 {svc.spec_text(offer)} — <b>{rub(offer["price_kop"])}/мес</b>', parse_mode='HTML')
    await choose_period(m, state, w)


@router.callback_query(F.data.startswith('d:o:'))
async def deploy_offer(c: CallbackQuery, state: FSMContext):
    w = await wizard(state)
    w['idx'] = int(c.data[4:])
    w.pop('custom', None)
    await state.update_data(w=w)
    await choose_period(c, state, w)


async def choose_period(c, state: FSMContext, w: dict):
    periods = w['cat']['periods']
    if len(periods) <= 1:
        return await deploy_period(c, state, '1m')
    btns = [(svc.PERIOD_LABEL.get(p['code'], p['code']) + (f' −{p["pct"]}%' if p['pct'] else ''), f'd:t:{p["code"]}')
            for p in periods]
    await show(c, 'Срок аренды:', kb(btns, [('« Назад', f'd:l:{w["loc"]}')]))


@router.callback_query(F.data.startswith('d:t:'))
async def deploy_period_cb(c: CallbackQuery, state: FSMContext):
    await deploy_period(c, state, c.data[4:])


async def deploy_period(c: CallbackQuery, state: FSMContext, period: str):
    w = await wizard(state)
    w['period'] = period
    await state.update_data(w=w)
    await show(c, 'Сколько нод поставить?',
               kb([(str(n), f'd:c:{n}') for n in (1, 2, 3, 5, 10)], [('Другое число', 'd:c:?')],
                  [('« Назад', f'd:l:{w["loc"]}')]))


@router.callback_query(F.data.startswith('d:c:'))
async def deploy_count_cb(c: CallbackQuery, state: FSMContext, store: Store):
    if c.data == 'd:c:?':
        await state.set_state(Deploy.count)
        return await show(c, 'Пришлите число от 1 до 50. /cancel — отмена')
    await deploy_count(c, state, store, int(c.data[4:]))


@router.message(Deploy.count)
async def deploy_count_msg(m: Message, state: FSMContext, store: Store):
    t = (m.text or '').strip()
    if not t.isdigit() or not 1 <= int(t) <= 50:
        return await m.answer('Нужно число от 1 до 50')
    await state.set_state(None)
    await deploy_count(m, state, store, int(t))


async def deploy_count(target, state: FSMContext, store: Store, n: int):
    from . import naming
    w = await wizard(state)
    w['count'] = n
    await state.update_data(w=w)
    p = store.profile(w['pid'])
    rows = []
    if p.get('base_domain'):
        sample = f'{naming.render(p.get("name_pattern") or naming.DEFAULT_PATTERN, naming.cc_of(w["loc"]), 1)}.{p["base_domain"]}'
        rows.append([(f'🤖 Автоматически ({sample}…)', 'd:dom:auto')])
    rows.append([('✍️ Ввести вручную', 'd:dom:manual')])
    hint = '' if p.get('base_domain') else '\n\nДля автоимён задайте «Домен для автоимён» в настройках группы (панель управления).'
    await show(target, f'Домены для {n} нод:{hint}', kb(*rows, [('« Отмена', 'm:main')]))


@router.callback_query(F.data == 'd:dom:auto')
async def deploy_auto(c: CallbackQuery, state: FSMContext, store: Store):
    w = await wizard(state)
    await c.answer('Подбираю свободные имена…')
    try:
        if w.get('own'):  # серверы бывают в разных локациях — имена по локации каждого, в порядке выбора
            locs, by, taken = w['own_locs'], {}, set()
            for loc in dict.fromkeys(locs):
                names = await svc.auto_domains(store, store.profile(w['pid']), loc, locs.count(loc), exclude=taken)
                taken |= set(names)
                by[loc] = iter(names)
            w['domains'] = [next(by[loc]) for loc in locs]
        else:
            w['domains'] = await svc.auto_domains(store, store.profile(w['pid']), w['loc'], w['count'])
    except ValueError as e:
        return await show(c, f'❌ {html.escape(str(e))}', kb([('✍️ Ввести вручную', 'd:dom:manual')], BACK))
    await state.update_data(w=w)
    await confirm(c, state, store)


@router.callback_query(F.data == 'd:dom:manual')
async def deploy_manual(c: CallbackQuery, state: FSMContext):
    w = await wizard(state)
    await state.set_state(Deploy.domains)
    await show(c, f'Пришлите {w["count"]} домен(а/ов), по одному в строке. Зоны должны быть в Cloudflare.\n\n/cancel — отмена')


@router.message(Deploy.domains)
async def deploy_manual_msg(m: Message, state: FSMContext, store: Store):
    w = await wizard(state)
    try:
        w['domains'] = await svc.check_domains(store, store.profile(w['pid']), m.text or '', w['count'])
    except ValueError as e:
        return await m.answer('❌ ' + html.escape(str(e)) + '\nПришлите список ещё раз или /cancel', parse_mode='HTML')
    await state.update_data(w=w)
    await state.set_state(None)
    await confirm(m, state, store)


async def confirm(target, state: FSMContext, store: Store):
    from .runner import bcloud_from, buyer_of
    w = await wizard(state)
    if w.get('own'):
        return await own_confirm(target, w, store)
    p = store.profile(w['pid'])
    q = svc.quote(w['cat'], w['loc'], w['idx'], w['period'], w['count'], w.get('custom'))
    try:
        bal = await bcloud_from(store, buyer_of(store, p)).balance()
        bal_line = f'Баланс {html.escape(p.get("owner_label") or "bcloud")}: {rub(bal)}' + ('' if bal >= q['total_kop'] else ' — ⚠️ <b>не хватает</b>')
    except BcloudError as e:
        bal, bal_line = None, f'Баланс: ❓ {html.escape(str(e))}'
    loc, offer = q['loc'], q['offer']
    lines = ['<b>Проверьте заказ</b>', '',
             f'🗂 {html.escape(p["name"])}' + (f' · для {html.escape(p["owner_label"])}' if p.get('owner_label') else ''),
             f'{loc["flag"]} {html.escape(loc["name"])} · {svc.spec_text(offer)}' + (' (своя)' if w.get('custom') else ''),
             f'💿 {html.escape(w["cat"]["os_label"])} · {svc.PERIOD_LABEL.get(w["period"], w["period"])}',
             f'🛡 Traffic Control: {"да" if p.get("traffic_control", True) else "нет"}', '',
             *[f'• <code>{html.escape(d)}</code>' for d in w['domains']], '',
             f'Итого: {rub(q["one_kop"])} × {w["count"]} = <b>{rub(q["total_kop"])}</b>', bal_line]
    rows = [[('🚀 Купить и установить', 'd:go')]] if bal is None or bal >= q['total_kop'] else []
    await show(target, '\n'.join(lines), kb(*rows, [('« Отмена', 'm:main')]))


@router.callback_query(F.data == 'd:go')
async def deploy_go(c: CallbackQuery, state: FSMContext, store: Store, runner: Runner):
    w = await wizard(state)
    if not w.get('domains'):
        return await c.answer('Мастер устарел — начните заново', show_alert=True)
    await state.clear()
    await c.message.edit_text(f'🚀 Запускаю {len(w["domains"])} задач(и). Ход каждой — отдельным сообщением ниже, '
                              f'все задачи — в «📋 Задачи».', reply_markup=kb(BACK))
    await c.answer()
    if w.get('own'):
        await own_create(c, store, runner, w)
        return
    await svc.create_jobs(store, runner, c.message.chat.id, w['pid'], w['cat'], w['loc'], w['idx'], w['period'],
                          w['domains'], custom=w.get('custom'))


# ───────────────────────── уже купленные серверы ─────────────────────────

def srv_title(x: dict) -> str:
    return f'{x.get("flag") or ""} {x.get("name") or x.get("ip") or "#" + str(x["id"])}'.strip()


async def own_list(c: CallbackQuery, state: FSMContext, store: Store, refresh: bool = True):
    w = await wizard(state)
    p = store.profile(w.get('pid') or 0)
    if not p:
        return await c.answer('Мастер устарел — начните заново', show_alert=True)
    if refresh or 'own_list' not in w:
        try:
            servers = await svc.own_servers(store, p)
        except BcloudError as e:
            return await show(c, f'❌ {html.escape(str(e))}', kb([('« Назад', f'd:p:{w["pid"]}')]))
        w['own_list'] = [{k: v for k, v in x.items() if k != 'root_password'} for x in servers]
    w.update(own=True, sel=[x for x in w.get('sel', []) if any(y['id'] == x and y['ready'] for y in w['own_list'])])
    await state.update_data(w=w)
    ready = [x for x in w['own_list'] if x['ready']]
    busy = [x for x in w['own_list'] if not x['ready']]
    who = html.escape(p.get('owner_label') or 'ваш аккаунт')
    lines = [f'<b>📦 Уже купленные серверы</b> · {who}', '',
             'Серверы этого аккаунта в Bobr Cloud, где бы они ни были куплены. Нода ставится поверх системы — '
             'на сервере не должно быть своего сайта или Xray на портах 80 и 443.']
    if busy:
        lines += ['', 'Недоступны:', *[f'• {html.escape(srv_title(x))} — {html.escape(x["why"])}' for x in busy[:15]]]
    if not ready:
        lines += ['', 'Свободных серверов нет.']
    btns = [[(('✅ ' if x['id'] in w['sel'] else '▫️ ') + f'{srv_title(x)} · {x["ip"]} · {x["cpu"]}/{x["ram"]}/{x["disk"]}',
              f'd:os:{x["id"]}')] for x in ready[:40]]
    nav = [('« Назад', f'd:p:{w["pid"]}')] + ([(f'Дальше · {len(w["sel"])} »', 'd:own:go')] if w['sel'] else [])
    await show(c, '\n'.join(lines), kb(*btns, [('🔄 Обновить', 'd:own')], nav))


@router.callback_query(F.data == 'd:own')
async def own_start(c: CallbackQuery, state: FSMContext, store: Store):
    await state.set_state(None)
    await own_list(c, state, store)


@router.callback_query(F.data.regexp(r'^d:os:\d+$'))
async def own_toggle(c: CallbackQuery, state: FSMContext, store: Store):
    w = await wizard(state)
    if not w.get('own'):
        return await c.answer('Мастер устарел — начните заново', show_alert=True)
    sid = int(c.data[5:])
    w['sel'] = [x for x in w['sel'] if x != sid] if sid in w['sel'] else w['sel'] + [sid]
    await state.update_data(w=w)
    await own_list(c, state, store, refresh=False)


@router.callback_query(F.data == 'd:own:go')
async def own_go(c: CallbackQuery, state: FSMContext, store: Store):
    w = await wizard(state)
    if not w.get('own') or not w.get('sel'):
        return await c.answer('Выберите серверы', show_alert=True)
    by_id = {x['id']: x for x in w['own_list']}
    w.update(count=len(w['sel']), own_locs=[by_id[x]['location'] for x in w['sel']], loc=by_id[w['sel'][0]]['location'])
    await state.update_data(w=w)
    p = store.profile(w['pid'])
    rows = []
    if p.get('base_domain'):
        rows.append([('🤖 Автоматически', 'd:dom:auto')])
    rows.append([('✍️ Ввести вручную', 'd:dom:manual')])
    order = '\n'.join(f'{i}. {html.escape(srv_title(by_id[x]))} · <code>{by_id[x]["ip"]}</code>' for i, x in enumerate(w['sel'], 1))
    await show(c, f'Домены для {len(w["sel"])} нод — по порядку серверов:\n\n{order}', kb(*rows, [('« Серверы', 'd:own')]))


async def own_confirm(target, w: dict, store: Store):
    p = store.profile(w['pid'])
    by_id = {x['id']: x for x in w['own_list']}
    lines = ['<b>Проверьте установку</b>', '',
             f'🗂 {html.escape(p["name"])}' + (f' · серверы {html.escape(p["owner_label"])}' if p.get('owner_label') else ''),
             f'🛡 Traffic Control: {"да" if p.get("traffic_control", True) else "нет"}', '',
             *[f'• {html.escape(srv_title(by_id[sid]))} ({by_id[sid]["ip"]}) → <code>{html.escape(d)}</code>'
               for sid, d in zip(w['sel'], w['domains'])],
             '', 'Покупки нет: ставим на уже оплаченные серверы.']
    await show(target, '\n'.join(lines), kb([('🚀 Установить', 'd:go')], [('« Отмена', 'm:main')]))


async def own_create(c: CallbackQuery, store: Store, runner: Runner, w: dict) -> None:
    """Задачи на выбранные серверы: свежие данные из Bobr Cloud, по локациям, домены — в порядке выбора."""
    p = store.profile(w['pid'])
    try:
        fresh = {x['id']: x for x in await svc.own_servers(store, p)}
    except BcloudError as e:
        await c.message.answer(f'❌ {html.escape(str(e))}', parse_mode='HTML')
        return
    groups: dict[str, tuple[list, list]] = {}
    for sid, dom in zip(w['sel'], w['domains']):
        x = fresh.get(sid)
        if not x or not x['ready']:
            await c.message.answer(f'❌ Сервер #{sid}: ' + html.escape(x['why'] if x else 'нет в Bobr Cloud') + ' — задачи не созданы',
                                   parse_mode='HTML')
            return
        g = groups.setdefault(x['location'], ([], []))
        g[0].append(x)
        g[1].append(dom)
    for loc, (servers, doms) in groups.items():
        await svc.create_jobs(store, runner, c.message.chat.id, w['pid'], w['cat'], loc, None, '1m', doms, servers=servers)


# ───────────────────────── мониторинг ─────────────────────────

@router.callback_query(F.data.regexp(r'^mon:[fri]:[0-9a-f]{8}$'))
async def monitor_button(c: CallbackQuery, store: Store, monitor=None, access: Access | None = None,
                         role: str = 'admin', uid: int = 0, username: str = ''):
    if monitor is None:
        return await c.answer('Мониторинг выключен', show_alert=True)
    found = monitor.find(c.data[6:])
    if found and not my_profile(store, access, role, uid, username, found[0]['id']):
        return await c.answer('Нет доступа', show_alert=True)
    what = {'f': 'fix', 'r': 'replace', 'i': 'ignore'}[c.data[4]]
    msg = await monitor.action(c.data[6:], what)
    try:
        await c.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        pass
    await c.answer(msg, show_alert=True)


# ───────────────────────── задачи ─────────────────────────

@router.callback_query(F.data == 'm:jobs')
async def jobs(c: CallbackQuery, state: FSMContext, store: Store, access: Access | None = None,
               role: str = 'admin', uid: int = 0, username: str = ''):
    await state.clear()
    if role == 'admin' or access is None:
        js = store.jobs(15)
    else:
        ids = {p['id'] for p in my_profiles(store, access, role, uid, username)}
        js = [j for j in store.jobs(500) if j['profile_id'] in ids][:15]
    if not js:
        return await show(c, '<b>📋 Задачи</b>\n\nПока пусто.', kb(BACK))
    btns = [[(f'{STATUS_ICON.get(j["status"], "•")} #{j["id"]} {j["data"]["domain"]}', f'j:{j["id"]}')] for j in js]
    active = sum(1 for j in js if j['status'] in ACTIVE)
    await show(c, f'<b>📋 Задачи</b>\n\nПоследние {len(js)}' + (f', в работе: {active}' if active else ''),
               kb(*btns, [('🔄 Обновить', 'm:jobs')], BACK))


@router.callback_query(F.data.regexp(r'^j:\d+$'))
async def job_view(c: CallbackQuery, store: Store, runner: Runner, access: Access | None = None,
                   role: str = 'admin', uid: int = 0, username: str = ''):
    j = my_job(store, access, role, uid, username, int(c.data[2:]))
    if not j:
        return await c.answer('Задача не найдена', show_alert=True)
    p = store.profile(j['profile_id']) or {}
    m = await c.message.answer(job_text(j, p.get('name', ''), runner.last.get(j['id'], '')),
                               reply_markup=job_kb(j), parse_mode='HTML')
    store.update_job(j['id'], msg_id=m.message_id, chat_id=m.chat.id)  # дальше обновляется это сообщение
    await c.answer()


@router.callback_query(F.data.regexp(r'^j:\d+:log$'))
async def job_log(c: CallbackQuery, store: Store, runner: Runner, access: Access | None = None,
                  role: str = 'admin', uid: int = 0, username: str = ''):
    jid = int(c.data.split(':')[1])
    if not my_job(store, access, role, uid, username, jid):
        return await c.answer('Задача не найдена', show_alert=True)
    path = runner.log_path(jid)
    if not path.exists() or path.stat().st_size == 0:
        return await c.answer('Журнала пока нет — установка ещё не начиналась', show_alert=True)
    await c.message.answer_document(FSInputFile(path, filename=f'job-{jid}.log'))
    await c.answer()


@router.callback_query(F.data.regexp(r'^j:\d+:retry$'))
async def job_retry(c: CallbackQuery, store: Store, runner: Runner, access: Access | None = None,
                    role: str = 'admin', uid: int = 0, username: str = ''):
    jid = int(c.data.split(':')[1])
    j = my_job(store, access, role, uid, username, jid)
    if not j or j['status'] != 'failed':
        return await c.answer('Повторить можно только задачу с ошибкой', show_alert=True)
    store.update_job(jid, status='queued', error='')
    runner.start(jid)
    rebuy = not j['data'].get('server_id') and j['data'].get('kind') != 'node'
    await c.answer('Запущено снова' + (' — будет новая покупка' if rebuy else ''))


@router.callback_query(F.data.regexp(r'^j:\d+:hide$'))
async def job_hide(c: CallbackQuery, store: Store, access: Access | None = None,
                   role: str = 'admin', uid: int = 0, username: str = ''):
    jid = int(c.data.split(':')[1])
    if not my_job(store, access, role, uid, username, jid):
        return await c.answer('Задача не найдена', show_alert=True)
    store.update_job(jid, hidden=1)
    await delete_quietly(c.message)
    await c.answer('Скрыто из списка')
