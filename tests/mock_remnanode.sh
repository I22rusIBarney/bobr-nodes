#!/bin/bash
# Имитация стороннего скрипта установки ноды в духе DigneZzZ/remnanode.sh:
#   @ install [--force] [--secret-key=KEY] [--port=PORT] [--xray]
# Без флагов задаёт вопросы; в конце, как оригинал, бесконечно показывает логи (docker compose logs -f).
# Переменная MOCK_LOG — файл, куда пишутся полученные параметры (для проверки в тесте); MOCK_EXTRA=ask — лишний вопрос.
FORCE=""; KEY=""; PORT=""; XRAY="no"
[[ $1 == @ ]] && shift
[[ $1 == install ]] || { echo "unknown command $1"; exit 3; }
shift
for a in "$@"; do
  case "$a" in
    --force|-f) FORCE=1 ;;
    --secret-key=*) KEY="${a#*=}" ;;
    --port=*) PORT="${a#*=}" ;;
    --xray) XRAY=yes ;;
    *) echo "Unknown option: $a"; exit 2 ;;
  esac
done
if [[ -z $KEY ]]; then
  echo "Please paste the content of the SECRET_KEY from Remnawave-Panel, press ENTER on a new line when finished: "
  while IFS= read -r line; do [[ -z $line ]] && break; KEY="$KEY$line"; done
fi
[[ -n $KEY ]] || { echo "SECRET_KEY is required!"; exit 1; }
if [[ -z $PORT ]]; then
  if [[ -n $FORCE ]]; then PORT=3000; else read -p "Enter the NODE_PORT (default 3000): " -r PORT; PORT=${PORT:-3000}; fi
fi
if [[ ${MOCK_EXTRA:-} == ask ]]; then read -p "Do you want to remove old data files too? (y/n): " -r x; fi
printf 'key=%s\nport=%s\nxray=%s\n' "$KEY" "$PORT" "$XRAY" > "${MOCK_LOG:-/dev/null}"
echo "Starting remnanode ... done"
while true; do echo "remnanode  | [Xray] started"; sleep 1; done
