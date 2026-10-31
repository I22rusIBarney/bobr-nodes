"""Bobr Nodes: покупка серверов в bcloud → Nuvrion Vision → регистрация нод в Remnawave.
Telegram-бот + веб-админка (Mini App) в одном процессе."""
import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, MenuButtonWebApp, WebAppInfo
from aiohttp import web

from bot.access import Access
from bot.handlers import AdminOnly, router
from bot.monitor import Monitor
from bot.runner import Runner
from bot.security import Watch
from bot.store import Store
from bot.web import build_web

# Значения, которые установщик может передать через .env при первом запуске (дальше правятся в админке)
SEED = {'BCLOUD_URL': 'bcloud_url', 'BCLOUD_TOKEN': 'bcloud_token', 'BCLOUD_TG_ID': 'bcloud_tg_id', 'CF_TOKEN': 'cf_token'}


def load_env(path='.env'):
    if os.path.exists(path):
        for line in open(path, encoding='utf-8'):
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))


async def main():
    load_env()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    token = os.environ.get('BOT_TOKEN', '')
    admins = {int(x) for x in os.environ.get('ADMIN_IDS', '').replace(' ', '').split(',') if x}
    if not token or not admins:
        raise SystemExit('Заполните BOT_TOKEN и ADMIN_IDS в .env (bnodes setup)')
    public_url = os.environ.get('PUBLIC_URL', '').rstrip('/')
    data_dir = os.environ.get('DATA_DIR', 'data')
    os.makedirs(data_dir, mode=0o700, exist_ok=True)

    store = Store(f'{data_dir}/bot.db')
    for env_key, key in SEED.items():
        # из .env берём, только если в админке ещё пусто или установщик выдал новый токен bcloud
        v = os.environ.get(env_key, '')
        if v and (not store.get(key) or (key == 'bcloud_token' and store.get('seed_' + key) != v)):
            store.set(key, v)
            store.set('seed_' + key, v)
    if not store.get('bcloud_tg_id'):
        store.set('bcloud_tg_id', str(sorted(admins)[0]))

    bot = Bot(token, default=DefaultBotProperties(parse_mode='HTML'))
    me = await bot.get_me()
    dp = Dispatcher(storage=MemoryStorage())
    access = Access(store, admins)
    mw = AdminOnly(admins, access)
    dp.message.outer_middleware(mw)
    dp.callback_query.outer_middleware(mw)
    dp.include_router(router)
    runner = Runner(store, bot, f'{data_dir}/logs')
    monitor = Monitor(store, runner, bot, admins, access=access)
    dp['store'], dp['runner'], dp['public_url'], dp['monitor'] = store, runner, public_url, monitor
    dp['access'] = access

    await bot.set_my_commands([BotCommand(command='start', description='Меню'),
                               BotCommand(command='cancel', description='Отменить ввод')])
    if public_url.startswith('https://'):
        try:
            await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text='Панель', web_app=WebAppInfo(url=public_url + '/admin')))
        except Exception as e:  # кнопка меню — удобство, не обязательна
            logging.warning('menu button: %s', e)

    watch = Watch(store, bot, admins, data_dir)
    watch_task = asyncio.create_task(watch.run())  # входы по SSH и блокировки fail2ban на сервере бота
    sync_task = asyncio.create_task(runner.sync_loop())  # серверы, удалённые в bcloud, — убрать из панели
    mon_task = asyncio.create_task(monitor.run())  # доступность нод из России: чекеры, сообщения, починка
    app = build_web(store, runner, token, admins, public_url, me.username, security=watch, data_dir=data_dir, monitor=monitor,
                    access=access)
    web_runner = web.AppRunner(app, access_log=None)
    await web_runner.setup()
    await web.TCPSite(web_runner, os.environ.get('WEB_HOST', '0.0.0.0'), int(os.environ.get('WEB_PORT', '8080'))).start()
    logging.info('веб-админка: %s/admin', public_url or 'http://localhost:8080')

    resumed = await runner.resume_all()
    if resumed:
        logging.info('продолжаю незавершённые задачи: %s', resumed)
    try:
        await dp.start_polling(bot)
    finally:
        watch_task.cancel()
        sync_task.cancel()
        mon_task.cancel()


if __name__ == '__main__':
    asyncio.run(main())
