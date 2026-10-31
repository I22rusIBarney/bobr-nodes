"""Обновление нод (bot/upgrade.py): выбор версии и проверка тега."""
import pytest

from bot import upgrade as up


def test_pick_target_same_major_as_panel():
    tags = ['2.3.1', '2.2.0', '1.9.9', '3.0.0']
    assert up.pick_target(tags, '2.1.4') == '2.3.1'
    assert up.pick_target(tags, '') == '3.0.0'
    assert up.pick_target(tags, '4.0.0') == 'latest'
    assert up.pick_target([], '2.1.4') == 'latest'


@pytest.mark.parametrize('tag', ['2.3.1', 'latest', 'dev', '2.3.1-rc1'])
def test_tag_ok(tag):
    assert up.check_tag(tag) == tag


@pytest.mark.parametrize('tag', ['rm -rf /', '2.3;id', '$(id)', '-x', 'a' * 70])
def test_tag_bad(tag):
    with pytest.raises(ValueError):
        up.check_tag(tag)


def test_commands_marked():
    assert up.MARK in up.upgrade_cmd('2.3.1') and '"$REPO:"2.3.1;' in up.upgrade_cmd('2.3.1')
    assert 'BNODES_UPGRADE_CHECK' in up.check_cmd() and 'BNODES_UPGRADE_ROLLBACK' in up.rollback_cmd()
