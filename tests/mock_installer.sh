#!/bin/bash
# Имитация Nuvrion Vision Installer для тестов: те же вопросы (настоящие ask_yes и runtime.py collect из репозитория установщика).
# $1 — IP (для имени каталога), $2 — ok | unknown
NV_SRC="${NV_SRC:?путь к src установщика}"
eval "$(sed -n '/^ask_yes()/,/^}/p' "$NV_SRC/installer.sh")"
BOLD=$'\e[1m'; YELLOW=$'\e[93m'; RESET=$'\e[0m'
ask_yes 'Установить на машину Nuvrion Vision?' || exit 9
ask_yes 'Разрешить для текущей установки обновление APT и системы, а также установку обязательных пакетов, Docker и nginx?' || exit 9
ask_yes 'Удалить перечисленные ненужные зависимости?' || true
out="${TMPDIR:-/tmp}/nvmock-$1-$$"
python3 "$NV_SRC/runtime.py" collect --output "$out" < /dev/tty || exit 8
[[ $2 == unknown ]] && { ask_yes 'Совсем новый вопрос из будущей версии?'; exit 0; }
ask_yes 'Установить и включить Nuvrion Traffic Control?' || true
exit 2
