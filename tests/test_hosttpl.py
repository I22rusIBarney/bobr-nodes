"""Новый роутинг для хостов: файлы и архивы, сопоставление с хостами, замена шаблонов с откатом."""
import io
import json
import zipfile

import pytest

from bot import hosttpl as ht
from bot.engine import StepError


def cfg(tag, addr, rules=1):
    return {'outbounds': [{'tag': tag, 'protocol': 'vless', 'settings': {'vnext': [{'address': addr, 'port': 443}]},
                           'streamSettings': {'realitySettings': {'serverName': addr}}},
                          {'tag': 'direct', 'protocol': 'freedom'}],
            'routing': {'rules': [{'outboundTag': 'direct'}] * rules}}


class Cp866Info(zipfile.ZipInfo):
    """Как у архиваторов Windows: имя байтами cp866, без флага UTF-8."""
    def _encodeFilenameFlags(self):
        return self.filename.encode('cp866'), self.flag_bits


def zipped(files: dict, utf8_flag=True) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        for name, data in files.items():
            info = zipfile.ZipInfo(name) if utf8_flag else Cp866Info(name)
            z.writestr(info, data)
    return buf.getvalue()


def test_zip_with_russian_and_superscript_names_sorted():
    data = zipped({'mob/3² b.json': json.dumps(cfg('m3b', 'b')), 'mob/1 a.json': json.dumps(cfg('m1', 'a')),
                   'mob/3¹ c.json': json.dumps(cfg('m3a', 'c')), '__MACOSX/mob/._1 a.json': 'x', 'mob/readme.txt': 'hi'})
    files = sorted(ht.read_files('3.zip', data), key=lambda f: ht.sort_key(f['name']))
    assert [f['name'] for f in files] == ['1 a.json', '3¹ c.json', '3² b.json']
    ru = ht.read_files('r.zip', zipped({'Обход 6.json': json.dumps(cfg('m', 'x'))}, utf8_flag=False))
    assert ru[0]['name'] == 'Обход 6.json'


def test_bad_inputs_are_explained():
    with pytest.raises(StepError, match='не JSON'):
        ht.read_files('a.json', b'{oops')
    with pytest.raises(StepError, match='нет outbounds'):
        ht.read_files('a.json', b'{"routing": {}}')
    with pytest.raises(StepError, match='Перенос хостов'):
        ht.read_files('a.json', json.dumps({'kind': 'bobr-nodes/hosts', 'outbounds': [1]}).encode())
    with pytest.raises(StepError, match='нет .json'):
        ht.read_files('a.zip', zipped({'x.txt': 'y'}))


def test_suggest_by_main_server():
    hosts = [{'uuid': 'h6', 'xrayJsonTemplateUuid': 't6'}, {'uuid': 'h7', 'xrayJsonTemplateUuid': 't7'}, {'uuid': 'h8'}]
    tpls = {'t6': {'templateJson': cfg('m1-main', 'wrab.online')}, 't7': {'templateJson': cfg('m2-main', '100gramm.pro')}}
    files = [{'json': cfg('m2-main', '100gramm.pro', 5)}, {'json': cfg('m1-main', 'wrab.online', 9)}, {'json': cfg('m9', 'new.example')}]
    assert ht.suggest(files, hosts, tpls) == ['h7', 'h6', None]


class Panel:
    def __init__(self):
        self.hosts = [{'uuid': 'h6', 'remark': 'Обход 6', 'xrayJsonTemplateUuid': 't6'},
                      {'uuid': 'h7', 'remark': 'Обход 7', 'xrayJsonTemplateUuid': 't7'},
                      {'uuid': 'h8', 'remark': 'Обход 8', 'xrayJsonTemplateUuid': 't6'},
                      {'uuid': 'h9', 'remark': 'Без шаблона'}]
        self.tpls = {'t6': {'uuid': 't6', 'name': 'Arenda6', 'templateType': 'XRAY_JSON', 'templateJson': {**cfg('m1', 'a'), 'remnawave': {'injectHosts': [1]}}},
                     't7': {'uuid': 't7', 'name': 'Arenda7', 'templateType': 'XRAY_JSON', 'templateJson': cfg('m2', 'b')}}
        self.patches = []

    def call(self, method, path, body=None):
        if method == 'GET':
            return self.hosts
        self.patches.append(body)
        self.tpls[body['uuid']]['templateJson'] = body['templateJson']
        return self.tpls[body['uuid']]

    def templates(self):
        return list(self.tpls.values())


def test_apply_keeps_panel_settings_and_undo():
    p, log = Panel(), []
    res, backup = ht.apply(p, [('h6', '1.json', cfg('m1', 'a', 7)), ('h7', '2.json', cfg('m2', 'b'))], log.append)
    assert res == {'updated': 1, 'same': 1}                                   # второй уже такой же — не трогаем
    assert p.tpls['t6']['templateJson']['remnawave'] == {'injectHosts': [1]}    # служебное панели сохранилось
    assert len(p.tpls['t6']['templateJson']['routing']['rules']) == 7
    assert [b['name'] for b in backup] == ['Arenda6']
    ht.restore(p, backup, log.append)
    assert len(p.tpls['t6']['templateJson']['routing']['rules']) == 1


def test_apply_checks_everything_before_changing():
    p = Panel()
    with pytest.raises(StepError, match='один шаблон'):          # h6 и h8 — общий шаблон: второй конфиг перетёр бы первый
        ht.apply(p, [('h6', '1.json', cfg('x', 'x')), ('h8', '2.json', cfg('y', 'y'))], print)
    with pytest.raises(StepError, match='нет Xray JSON-шаблона'):
        ht.apply(p, [('h7', '1.json', cfg('x', 'x')), ('h9', '2.json', cfg('y', 'y'))], print)
    assert not p.patches                                          # ни одной правки в панели


def test_upload_body_read_whole_across_chunks():
    """Архив приходит по сети кусками: читаем до конца, иначе zip без оглавления — «архив повреждён»."""
    import asyncio
    from unittest import mock

    from aiohttp import streams

    from bot.web import read_body

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for i in range(30):
            z.writestr(f'{i}.json', json.dumps({'outbounds': [{'protocol': 'vless', 'tag': 'proxy', 'pad': 'x' * 4000 + str(i)}]}))
    raw = buf.getvalue()

    async def go(limit):
        proto = mock.Mock(_reading_paused=False)
        sr = streams.StreamReader(proto, 2 ** 16, loop=asyncio.get_running_loop())
        async def net():  # куски по 1000 байт приходят со временем, как из сети
            for k in range(0, len(raw), 1000):
                sr.feed_data(raw[k:k + 1000])
                await asyncio.sleep(0)
            sr.feed_eof()
        t = asyncio.create_task(net())
        await asyncio.sleep(0)
        data = await read_body(mock.Mock(content=sr), limit)
        t.cancel()
        return data
    data = asyncio.run(go(10 ** 7))
    assert data == raw and len(ht.read_files('a.zip', data)) == 30
    assert len(asyncio.run(go(5000))) == 5001  # превышение лимита видно по лишнему байту


def test_country_balancer_tags_from_wizard():
    """Мастер: «По стране» с NORMAL (tags не передан) и без него (tags='') — теги хостов ноды."""
    from bot import services as svc
    with_n = svc.host_options('pl', 'balancer', None, 'PL')
    without = svc.host_options('pl', 'balancer', '', 'PL')
    multi = svc.host_options('de', 'balancer', None, '')
    assert with_n['tags'] == ['PL', 'NORMAL'] and with_n['hidden'] and with_n['balancer']['tag'] == 'PL'
    assert without['tags'] == ['PL'] and multi['tags'] == ['DE', 'NORMAL']
    assert svc.host_options('pl', 'normal', 'NORMAL')['tags'] == ['NORMAL']


def test_country_balancer_free_tags():
    from bot import services as svc
    h = svc.host_options('pl', 'balancer', 'NORMAL,EU, pl', 'PL_WAW')
    assert h['tags'][0] == 'PL_WAW' and set(h['tags']) == {'PL_WAW', 'NORMAL', 'EU', 'PL'} and len(h['tags']) == len(set(h['tags']))
    assert svc.host_options('pl', 'balancer', 'NORMAL, PL_WAW', 'PL_WAW')['tags'] == ['PL_WAW', 'NORMAL']   # без дублей
    import pytest
    with pytest.raises(ValueError):
        svc.host_options('pl', 'balancer', 'норм', 'PL')
