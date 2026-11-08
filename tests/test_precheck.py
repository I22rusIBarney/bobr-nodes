"""До покупки: имя, на котором установка упадёт уже после оплаты (CNAME, CAA без Let's Encrypt), — ошибка;
имя, которое сейчас ведёт на другой сервер, — предупреждение."""
import asyncio

import pytest

from bot import services as svc
from bot.store import Store


def test_caa_rules():
    le = {'name': 'ex.com', 'type': 'CAA', 'content': '0 issue "letsencrypt.org"'}
    other = {'name': 'ex.com', 'type': 'CAA', 'data': {'tag': 'issue', 'value': 'digicert.com'}}
    assert svc.caa_allows_le('a.ex.com', [])                       # CAA нет — можно всем
    assert svc.caa_allows_le('a.ex.com', [le, other])
    assert not svc.caa_allows_le('a.ex.com', [other])
    assert svc.caa_allows_le('a.ex.com', [other, {**le, 'name': 'a.ex.com'}])   # ближайший уровень главнее


def test_precheck_blocks_cname_and_caa_warns_on_live_a(tmp_path, monkeypatch):
    store = Store(tmp_path / 'db.sqlite')
    prof = store.profile(store.save_profile({'name': 'g'}))
    recs = {'c.ex.com': [{'type': 'CNAME', 'content': 'x.net'}], 'live.ex.com': [{'type': 'A', 'content': '198.51.100.7', 'comment': ''}]}
    caa: list = []

    class CF:
        def zone_for(self, d):
            return 'z'

        def records(self, d):
            return recs.get(d, [])

        def call(self, method, path):
            return caa
    monkeypatch.setattr(svc, 'cf_for', lambda store, prof: CF())
    warns: list = []
    assert asyncio.run(svc.check_domains(store, prof, ['live.ex.com', 'new.ex.com'], 2, warns)) == ['live.ex.com', 'new.ex.com']
    assert warns == ['live.ex.com сейчас ведёт на 198.51.100.7 — бот перенаправит его на новый сервер']
    with pytest.raises(ValueError, match='CNAME'):
        asyncio.run(svc.check_domains(store, prof, ['c.ex.com'], 1))
    caa.append({'name': 'ex.com', 'type': 'CAA', 'content': '0 issue "sectigo.com"'})
    with pytest.raises(ValueError, match="Let's Encrypt"):
        asyncio.run(svc.check_domains(store, prof, ['new.ex.com'], 1))
