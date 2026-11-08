"""Новые релизы панели: когда сообщать, разбор без нейросети и с ней, текст уведомления."""
import asyncio

from bot import autopilot as ap
from bot import panelwatch as pw
from bot import scripts as sc
from bot import upgrade as up
from bot.store import Store

R = [{'tag': '3.5.0', 'name': '3.5.0', 'body': '## What\'s Changed\n* feat: new hosts API\n* BREAKING: renamed field `isHidden`\n**Full Changelog**: x',
      'url': 'https://github.com/remnawave/panel/releases/tag/3.5.0', 'date': '2026-11-08'},
     {'tag': '3.4.7', 'name': '3.4.7', 'body': '* fix', 'url': '', 'date': '2026-11-01'}]


def test_newer_and_plain():
    assert pw.newer(R, '', ['3.4.7'])['tag'] == '3.5.0'
    assert pw.newer(R, '3.5.0', ['3.4.7']) is None            # уже разбирали
    assert pw.newer(R, '', ['3.5.0']) is None                  # панели уже на нём
    assert pw.newer(R, '', []) is None                         # ничего не знаем — только запомнить
    ex = pw.plain(R[0])
    assert ex['bot_risk'] == 'high' and ex['changes'][:2] == ['feat: new hosts API', 'BREAKING: renamed field isHidden']
    t = pw.text(R[0], ex, ['3.4.7'])
    assert '3.5.0' in t and 'у вас 3.4.7' in t and 'Может сломать' in t and '<script' not in pw.text({**R[0], 'tag': '<script>'}, ex, [])
    assert 'GET /api/nodes' in pw.api_paths() and 'PATCH /api/hosts' in pw.api_paths()


def test_check_panel_release(tmp_path, monkeypatch):
    store = Store(tmp_path / 'db.sqlite')
    store.save_profile({'name': 'g', 'panel_url': 'https://p', 'panel_token': 't'})
    pilot = ap.Autopilot(store, None, None, None)
    sent = []

    async def emit(type_, text, prof=None, *a, **k):
        sent.append((type_, text))
    pilot.notify.emit = emit
    monkeypatch.setattr(pw, 'releases', lambda: R)
    monkeypatch.setattr(pw, 'backend_notes', lambda tag: '')  # нет заметок backend — берём заметки panel
    monkeypatch.setattr(up, 'panel_version', lambda panel: '3.4.7')

    async def ask(self, system, turns):
        assert 'GET /api/nodes' in turns[0]['text'] and 'isHidden' in turns[0]['text']
        return {'summary': 'Новый API хостов.', 'changes': ['Поле isHidden переименовано'], 'bot_risk': 'high',
                'bot_why': 'бот читает isHidden у хостов', 'node_update': True, 'node_why': 'новая ветка', 'actions': ['Подождать обновления бота']}
    monkeypatch.setattr(sc.Gemini, 'ask', ask)
    store.set('gemini_key', 'k')
    monkeypatch.setattr(sc.Gemini, 'ensure_model', lambda self: 'm')
    ex = asyncio.run(pilot.check_panel_release())
    assert ex['bot_risk'] == 'high' and sent[0][0] == 'panel.release' and 'бот читает isHidden' in sent[0][1]
    assert asyncio.run(pilot.check_panel_release()) is None and len(sent) == 1      # второй раз — тишина


BACKEND = """🌊 Remnawave Backend v3.4.8
<p align="center"><img src="x"></p>
## 3.4.7...3.4.8
[compare changes](https://github.com/remnawave/backend/compare/3.4.7...3.4.8)
### 🚀 Enhancements
- Add hosts bulk API ([abc1234](https://github.com/remnawave/backend/commit/abc1234))
### 🩹 Fixes
- Restrict certificates file reads ([51a32181](https://github.com/remnawave/backend/commit/51a32181))
### 🏡 Chore
- Update dependencies ([397459c8](https://x))
- Bump version to 3.4.8 ([4e989707](https://x))
### ⚠️ Breaking Changes
- Removed `GET /api/hosts/get-all`
Latest Docker image:
- `remnawave/backend:latest`
"""


def test_bullets_and_gather(monkeypatch):
    b = pw.bullets(BACKEND)
    assert b == ['Новое: Add hosts bulk API', 'Исправлено: Restrict certificates file reads', '⚠️ Ломающее: Removed GET /api/hosts/get-all']
    assert pw.plain({'body': BACKEND})['changes'][0].startswith('⚠️') and pw.plain({'body': BACKEND})['bot_risk'] == 'high'
    monkeypatch.setattr(pw, 'backend_notes', lambda tag: {'3.5.0': BACKEND}.get(tag, ''))
    g = pw.gather(R, '3.4.6')
    assert g['tag'] == '3.5.0' and g['span'] == ['3.5.0', '3.4.7'] and 'Add hosts bulk API' in g['body'] and '* fix' in g['body']
