"""Покупка сервера не повторяется, если прошлая попытка прошла без ответа (таймаут, перезапуск бота)."""
import asyncio

from bot.bcloud import BcloudUnavailable
from bot.runner import Runner
from bot.store import Store


class Stop(Exception):
    pass


class FakeBC:
    def __init__(self, lose_answer=True):
        self.lose_answer, self.buys, self.list = lose_answer, 0, []

    async def buy(self, location, os_, idx, period, password, custom=None):
        self.buys += 1
        srv = {'id': 500 + self.buys, 'status': 'provisioning', 'location': location, 'root_password': None}
        self.list.append(srv)
        if self.lose_answer:
            self.lose_answer = False
            raise BcloudUnavailable('Bobr Cloud недоступен: ReadTimeout')  # купил, а ответ не дошёл
        return srv

    async def servers(self):
        return self.list

    async def server(self, sid):
        raise Stop(sid)  # дальше покупки в этом тесте не идём


def job(store, password='pw-of-this-job'):
    pid = store.save_profile({'name': 'G', 'owner': '1'})
    return store.add_job('b', pid, 1, {'location': 'nl', 'os': 'ubuntu', 'idx': 0, 'period': '1m', 'password': password,
                                       'buyer': 1, 'domain': 'nl1.example.com'})


def run(runner, jid):
    try:
        asyncio.run(runner._run(jid))
    except Stop:
        pass


def test_retry_after_lost_answer_takes_the_bought_server(tmp_path):
    store = Store(tmp_path / 'bot.db')
    bc = FakeBC()
    runner = Runner(store, None, tmp_path / 'logs', bcloud_factory=lambda store, buyer: bc)
    runner.render = lambda *a, **k: asyncio.sleep(0)
    jid = job(store)
    run(runner, jid)
    assert store.job(jid)['status'] == 'failed' and bc.buys == 1
    bc.list[0]['root_password'] = 'pw-of-this-job'  # Bobr Cloud уже знает пароль сервера
    store.update_job(jid, status='queued')  # «Повторить»
    run(runner, jid)
    assert bc.buys == 1, 'второй сервер не покупаем'
    assert store.job(jid)['data']['server_id'] == 501


def test_foreign_server_with_other_password_is_not_taken(tmp_path):
    store = Store(tmp_path / 'bot.db')
    bc = FakeBC()
    runner = Runner(store, None, tmp_path / 'logs', bcloud_factory=lambda store, buyer: bc)
    runner.render = lambda *a, **k: asyncio.sleep(0)
    jid = job(store)
    run(runner, jid)
    bc.list[0]['root_password'] = 'someone-elses-password'  # сервер купили не мы — тот же регион, другой пароль
    store.update_job(jid, status='queued')
    run(runner, jid)
    assert bc.buys == 2 and store.job(jid)['data']['server_id'] == 502
