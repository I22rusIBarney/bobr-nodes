#!/usr/bin/env bash
# Установщик Bobr Nodes. Запуск на VPS (Ubuntu/Debian) от root: sudo ./install.sh
# Спрашивает только самое нужное: токен бота, ID админа и домен панели. Остальное — в веб-админке.
# Если на этом сервере уже стоит bcloud, подключается к нему сам и работает за его HTTPS.
# После установки доступна команда bnodes (bnodes help — список команд).
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

C_B=$'\e[1m'; C_G=$'\e[32m'; C_Y=$'\e[33m'; C_R=$'\e[31m'; C_0=$'\e[0m'
say()  { printf '%s\n' "$*"; }
ok()   { printf '%s✔%s %s\n' "$C_G" "$C_0" "$*"; }
warn() { printf '%s!%s %s\n' "$C_Y" "$C_0" "$*"; }
die()  { printf '%s✖ %s%s\n' "$C_R" "$*" "$C_0" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Запустите от root: sudo ./install.sh"

compose() { docker compose "$@"; }
env_get() { [[ -f .env ]] && sed -n "s/^$1='\{0,1\}\([^']*\)'\{0,1\}$/\1/p" .env | tail -1; }
env_set() {
  if grep -q "^$1=" .env 2>/dev/null; then sed -i "s|^$1=.*|$1='$2'|" .env; else printf "%s='%s'\n" "$1" "$2" >> .env; fi
}

# ───────── Готовый образ из GitHub ─────────
# GitHub Actions собирает образ после прохождения тестов: ghcr.io/<владелец>/<репозиторий>:sha-<коммит>.
# Сервер только скачивает его — без сборки и pip на VPS. Нет образа — собираем на месте.
gh_repo() { git config --get remote.origin.url 2>/dev/null | sed -E 's#(\.git)?/*$##; s#^.*github\.com[:/]##'; }
pull_image() {  # $1 — sha коммита. 0 — образ скачан, 1 — собрать на сервере, 2 — тесты не прошли
  local sha="$1" repo img st waited=0
  [[ "${BNODES_BUILD:-}" == 1 ]] && return 1          # BNODES_BUILD=1 bnodes update — всегда собирать на месте
  repo="$(gh_repo)"; [[ -n "$repo" ]] || return 1
  img="ghcr.io/${repo,,}:sha-$sha"
  while :; do
    if docker pull -q "$img" >/dev/null 2>&1; then
      env_set BNODES_IMAGE "$img"
      docker images "ghcr.io/${repo,,}" --format '{{.Repository}}:{{.Tag}}' | grep -vx "$img" | xargs -r docker rmi >/dev/null 2>&1 || true
      return 0
    fi
    st="$(curl -fsS -m 10 "https://api.github.com/repos/$repo/actions/runs?head_sha=$sha&per_page=5" 2>/dev/null | tr -d ' \n' || true)"
    [[ "$st" == *'"head_sha"'* ]] || { [[ $waited -gt 0 ]] && warn "Не дождался образа — собираю на сервере"; return 1; }
    [[ "$st" == *'"conclusion":"failure"'* ]] && return 2
    if [[ "$st" != *'"status":"queued"'* && "$st" != *'"status":"in_progress"'* && "$st" != *'"status":"waiting"'* ]]; then
      warn "Готового образа для этой версии нет — собираю на месте"; return 1
    fi
    (( waited >= 600 )) && { warn "GitHub собирает дольше 10 минут — собираю на сервере"; return 1; }
    (( waited == 0 )) && say "Новая версия ещё проходит тесты на GitHub (обычно 2–3 минуты), жду…"
    sleep 20; waited=$((waited + 20))
  done
}
# запуск: готовый образ из GitHub или сборка на месте. $1 — к какой версии откатиться, если тесты не прошли
deploy() {
  local sha rc=1
  sha="$(git rev-parse HEAD 2>/dev/null || true)"
  if [[ -n "$sha" ]]; then pull_image "$sha" && rc=0 || rc=$?; fi
  if [[ $rc == 2 ]]; then
    if [[ -n "${1:-}" ]] && git reset -q --keep "$1"; then
      die "Тесты этой версии на GitHub не прошли — обновление отменено, бот продолжает работать на версии $1"
    fi
    die "Тесты этой версии на GitHub не прошли — запуск отменён"
  fi
  # версия для админки бота: коммит, дата, описание
  mkdir -p data && git log -1 --format='%H%x09%cd%x09%s' --date=format:'%d.%m.%Y %H:%M' > data/version.txt 2>/dev/null || true
  git config --get remote.origin.url > data/origin.txt 2>/dev/null || true   # откуда ставить при переезде
  if [[ $rc == 0 ]]; then
    ok "Готовый образ с GitHub скачан"
    compose up -d --remove-orphans
  else
    env_set BNODES_IMAGE "bobr-nodes:local"
    compose up -d --build --remove-orphans
  fi
}

install_cli() {
  local dir; dir="$(pwd)"
  cat > /usr/local/bin/bnodes <<EOF
#!/usr/bin/env bash
# Bobr Nodes — управление ботом. Создано установщиком из ${dir}
if [[ \$EUID -ne 0 ]]; then exec sudo "\$0" "\$@"; fi
cd "${dir}" || { echo "Папка бота ${dir} не найдена — запустите install.sh из новой папки"; exit 1; }
exec ./install.sh "\${@:-help}"
EOF
  chmod 755 /usr/local/bin/bnodes
  # автообновление: раз в час бот сам решает, пора ли (включается и настраивается в админке)
  if command -v systemctl >/dev/null && [[ -d /run/systemd/system ]]; then
    cat > /etc/systemd/system/bnodes-auto.service <<'EOF2'
[Unit]
Description=Bobr Nodes: автообновление
After=docker.service
[Service]
Type=oneshot
ExecStart=/usr/local/bin/bnodes _auto
EOF2
    cat > /etc/systemd/system/bnodes-auto.timer <<'EOF2'
[Unit]
Description=Bobr Nodes: проверка обновлений раз в час
[Timer]
OnCalendar=*-*-* *:07:00
RandomizedDelaySec=120
[Install]
WantedBy=timers.target
EOF2
    # «Обновить сейчас» в панели: бот кладёт data/update-now — systemd сразу запускает обновление
    cat > /etc/systemd/system/bnodes-update-now.path <<EOF2
[Unit]
Description=Bobr Nodes: обновление по кнопке из панели
[Path]
PathExists=${dir}/data/update-now
Unit=bnodes-auto.service
[Install]
WantedBy=multi-user.target
EOF2
    systemctl daemon-reload >/dev/null 2>&1 || true
    systemctl enable --now bnodes-auto.timer bnodes-update-now.path >/dev/null 2>&1 || true
  fi
}

# Папка bcloud на этом сервере (из команды bcloud) или пусто
bcloud_dir() {
  [[ -x /usr/local/bin/bcloud ]] || return 0
  sed -n 's/^cd "\([^"]*\)".*/\1/p' /usr/local/bin/bcloud | head -1
}
benv() { [[ -f "$1/.env" ]] && sed -n "s/^$2='\{0,1\}\([^']*\)'\{0,1\}$/\1/p" "$1/.env" | tail -1; }

port_busy() { ss -Hltn "( sport = :$1 )" 2>/dev/null | grep -q .; }

wait_ready() {
  local i
  for _ in $(seq 1 40); do
    if compose exec -T nodes python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=3)" >/dev/null 2>&1; then return 0; fi
    sleep 2
  done
  return 1
}

# ───────── защита сервера (как bcloud harden; если bcloud на этом же сервере — дополняем, не перетираем) ─────────
# sshd -T падает без /run/sshd (Ubuntu 24.04: SSH запускается по сокету, папка появляется при первом входе)
sshd_conf() { mkdir -p /run/sshd 2>/dev/null || true; sshd -T 2>/dev/null || true; }
ssh_port() { sshd_conf | awk '/^port /{print $2; exit}'; }

harden() {
  command -v apt-get >/dev/null || die "Поддерживаются Ubuntu и Debian"
  local port dir keys pw_off="no" a
  port="$(ssh_port)"; port="${port:-22}"
  dir="$(pwd)"
  keys=0; [[ -s /root/.ssh/authorized_keys ]] && keys=$(grep -cE '^(ssh-|ecdsa-|sk-)' /root/.ssh/authorized_keys || true)
  cat <<EOF
${C_B}Защита сервера Bobr Nodes${C_0}
  • Файрвол UFW: входящие закрыты, кроме SSH (порт ${port}), 80 и 443
  • fail2ban: блокировка IP после 5 неудачных входов по SSH (на час, повторно — дольше)
  • SSH: не больше 3 попыток за подключение, без пустых паролей и X11
  • Автоматические обновления безопасности
  • Защита сетевого стека (sysctl) и сообщения в бота о входах по SSH и подборе паролей
В базе бота — root-пароли нод и токены панелей, поэтому защита сервера особенно важна.
Текущую SSH-сессию не закрывайте, пока не проверите вход в новом окне.
EOF
  read -r -p "Продолжить? [y/N] " a </dev/tty || a=""; [[ "$a" =~ ^[yYдД] ]] || { say "Отменено"; return; }
  if (( keys > 0 )); then
    say; say "Найдено SSH-ключей у root: ${keys}. Можно запретить вход по паролю — это главная защита от взлома."
    say "Перед этим убедитесь, что входите именно по ключу, иначе потеряете доступ."
    read -r -p "Запретить вход по паролю? [y/N] " a </dev/tty || a=""; [[ "$a" =~ ^[yYдД] ]] && pw_off="yes"
  else
    warn "SSH-ключей у root нет — вход по паролю оставляю. Настройте ключ и запустите bnodes harden ещё раз."
  fi

  say "Устанавливаю пакеты…"
  apt-get update -qq >/dev/null
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ufw fail2ban unattended-upgrades >/dev/null || die "Не удалось установить пакеты"

  # 1. файрвол: существующие правила не сбрасываем, только добавляем нужные
  ufw default deny incoming >/dev/null
  ufw default allow outgoing >/dev/null
  ufw allow "${port}/tcp" comment 'SSH' >/dev/null
  local ports="$port" p
  for p in $(ss -tlnpH 2>/dev/null | awk '/sshd/{n=split($4,a,":"); print a[n]}' | sort -u); do
    if [[ "$p" =~ ^[0-9]+$ && "$p" != "$port" ]]; then ufw allow "${p}/tcp" comment 'SSH' >/dev/null; ports="${ports},${p}"; fi
  done
  ufw allow 80/tcp comment 'HTTP (Caddy)' >/dev/null
  ufw allow 443/tcp comment 'HTTPS (Caddy)' >/dev/null
  ufw --force enable >/dev/null
  ok "Файрвол включён: открыты ${ports} (SSH), 80, 443. Порт бота 8080 наружу не опубликован"

  # 2. хук событий: входы по SSH и блокировки → data/host-events.jsonl → бот
  mkdir -p data && chmod 700 data
  cat > /usr/local/sbin/bnodes-event <<EOF
#!/bin/sh
# Bobr Nodes: события сервера для бота (создано bnodes harden)
F="${dir}/data/host-events.jsonl"
clean() { printf '%s' "\$1" | tr -cd 'A-Za-z0-9._:@-' | cut -c1-64; }
case "\$1" in
  login) [ "\$PAM_TYPE" = "open_session" ] || exit 0
         printf '{"type":"login","user":"%s","ip":"%s","ts":%s}\n' "\$(clean "\$PAM_USER")" "\$(clean "\$PAM_RHOST")" "\$(date +%s)" >> "\$F" ;;
  ban)   printf '{"type":"ban","ip":"%s","jail":"%s","ts":%s}\n' "\$(clean "\$2")" "\$(clean "\$3")" "\$(date +%s)" >> "\$F" ;;
esac
exit 0
EOF
  chmod 755 /usr/local/sbin/bnodes-event
  grep -qs bnodes-event /etc/pam.d/sshd || echo "session optional pam_exec.so quiet /usr/local/sbin/bnodes-event login" >> /etc/pam.d/sshd

  # 3. fail2ban. Файл 00-… читается раньше bcloud.conf: если bcloud тоже защищает сервер, его [sshd] главнее,
  #    а наш отдельный jail только сообщает боту о блокировках (без второй блокировки)
  mkdir -p /etc/fail2ban/action.d /etc/fail2ban/jail.d
  cat > /etc/fail2ban/action.d/bnodes-notify.conf <<'EOF'
[Definition]
actionban = /usr/local/sbin/bnodes-event ban <ip> <name>
actionunban =
EOF
  cat > /etc/fail2ban/jail.d/00-bnodes.conf <<EOF
# Bobr Nodes (bnodes harden)
[DEFAULT]
bantime = 1h
bantime.increment = true
bantime.maxtime = 1w
findtime = 10m
maxretry = 5
ignoreip = 127.0.0.1/8 ::1

[sshd]
enabled = true
port = ${ports}
backend = systemd

[bnodes-notify]
enabled = true
filter = sshd
port = ${ports}
backend = systemd
action = bnodes-notify
EOF
  systemctl enable fail2ban >/dev/null 2>&1 || true
  systemctl restart fail2ban || die "fail2ban не запустился: journalctl -u fail2ban"
  ok "fail2ban следит за SSH"

  # 4. SSH: отдельный файл; в sshd_config.d действует первое значение (00-bcloud раньше 00-bnodes — не спорят)
  mkdir -p /etc/ssh/sshd_config.d
  {
    echo "# Bobr Nodes (bnodes harden). Удалите файл, чтобы вернуть прежние настройки."
    echo "MaxAuthTries 3"
    echo "LoginGraceTime 30"
    echo "PermitEmptyPasswords no"
    echo "X11Forwarding no"
    echo "ClientAliveInterval 300"
    echo "ClientAliveCountMax 2"
    if [[ "$pw_off" == "yes" ]]; then
      echo "PasswordAuthentication no"
      echo "KbdInteractiveAuthentication no"
      echo "PermitRootLogin prohibit-password"
    fi
  } > /etc/ssh/sshd_config.d/00-bnodes.conf
  mkdir -p /run/sshd 2>/dev/null || true
  if sshd -t 2>/dev/null; then
    systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
    ok "SSH усилен"; [[ "$pw_off" == "yes" ]] && ok "Вход по паролю отключён — только по ключу"
  else
    rm -f /etc/ssh/sshd_config.d/00-bnodes.conf
    warn "Проверка конфигурации SSH не прошла — SSH оставлен как был"
  fi
  local pw_now; pw_now="$(sshd_conf | awk '/^passwordauthentication /{print $2}')"

  # 5. автообновления безопасности и сетевой стек (rp_filter не трогаем, если тут стоит vps-warp: ему нужен loose)
  printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' > /etc/apt/apt.conf.d/20auto-upgrades
  {
    echo "# Bobr Nodes (bnodes harden)"
    echo "net.ipv4.tcp_syncookies = 1"
    if [[ ! -f /etc/sysctl.d/99-vps-warp.conf ]]; then
      echo "net.ipv4.conf.all.rp_filter = 1"
      echo "net.ipv4.conf.default.rp_filter = 1"
    fi
    echo "net.ipv4.conf.all.accept_redirects = 0"
    echo "net.ipv4.conf.default.accept_redirects = 0"
    echo "net.ipv6.conf.all.accept_redirects = 0"
    echo "net.ipv4.conf.all.send_redirects = 0"
    echo "net.ipv4.conf.all.accept_source_route = 0"
    echo "net.ipv4.icmp_echo_ignore_broadcasts = 1"
  } > /etc/sysctl.d/90-bnodes.conf
  sysctl --system >/dev/null 2>&1 || true
  ok "Автообновления безопасности включены"

  # 6. права на секреты
  chmod 600 .env 2>/dev/null || true
  chmod -R go-rwx data 2>/dev/null || true
  [[ -d /root/bnodes-backups ]] && chmod 700 /root/bnodes-backups

  printf '{"ts":%s,"ssh_port":%s,"password_login":%s}\n' "$(date +%s)" "$port" "$([[ "$pw_now" == no ]] && echo false || echo true)" > data/.hardened
  say
  ok "Готово. О входах по SSH и атаках бот напишет админам."
  warn "Не закрывайте эту сессию: откройте новое окно и проверьте, что входите на сервер."
}

security_status() {
  say "${C_B}Файрвол${C_0}"; ufw status 2>/dev/null | sed -n '1,12p' || warn "UFW не установлен — bnodes harden"
  say; say "${C_B}fail2ban (SSH)${C_0}"
  if command -v fail2ban-client >/dev/null; then fail2ban-client status sshd 2>/dev/null | grep -E 'Currently|Total' || warn "fail2ban не запущен"; else warn "fail2ban не установлен — bnodes harden"; fi
  say; say "${C_B}SSH${C_0}"
  sshd_conf | grep -E '^(port|passwordauthentication|permitrootlogin|maxauthtries) ' || true
  [[ "$(sshd_conf | awk '/^passwordauthentication /{print $2}')" == "yes" ]] && warn "Вход по паролю разрешён: настройте SSH-ключ и запустите bnodes harden"
  say; say "${C_B}Обновления безопасности${C_0}"
  grep -q 'Unattended-Upgrade "1"' /etc/apt/apt.conf.d/20auto-upgrades 2>/dev/null && ok "Автообновления включены" || warn "Автообновления выключены"
  say; say "${C_B}Бот${C_0}"
  docker ps --format '{{.Names}} {{.Ports}}' 2>/dev/null | grep -E '8080' | grep -q '0.0.0.0:8080' && warn "Порт 8080 бота опубликован наружу!" || ok "Порт бота наружу не открыт"
  [[ "$(stat -c %a .env 2>/dev/null)" == 600 ]] && ok ".env доступен только root" || warn ".env доступен не только root: chmod 600 .env"
  [[ "$(stat -c %a data 2>/dev/null)" == 700 ]] && ok "data/ доступна только root" || warn "data/ доступна не только root: chmod 700 data"
}

usage() {
  cat <<EOF
${C_B}bnodes${C_0} — управление Bobr Nodes

  bnodes update    обновить из GitHub и перезапустить
  bnodes restart   перезапустить бота
  bnodes logs      логи бота (выход — Ctrl+C)
  bnodes status    состояние и версия
  bnodes backup    резервная копия базы и настроек в /root/bnodes-backups
  (переезд на другой сервер — в панели: Настройки → «Переезд на другой сервер»)
  bnodes setup     повторная настройка (токен, админы, домен, bcloud)
  bnodes harden    защитить сервер: файрвол, fail2ban, SSH, автообновления
  bnodes security  состояние защиты: файрвол, блокировки, вход по паролю
  bnodes help      эта справка

Подключения, профили панелей и развёртывание — в веб-админке (кнопка «Панель» в боте).
EOF
}

case "${1:-install}" in
  update)
    [[ -d .git ]] || die "Это не git-клон — обновите файлы вручную"
    before="$(git rev-parse --short HEAD)"
    git fetch -q origin main && git merge -q --ff-only origin/main || die "Не удалось обновить из git (git status)"
    after="$(git rev-parse --short HEAD)"
    [[ "$before" == "$after" ]] && ok "Уже последняя версия ($after)" || ok "Код обновлён: $before → $after"
    exec ./install.sh _rebuild "$before" ;;
  _auto)  # таймер раз в час или кнопка «Обновить сейчас»: новая версия + готовый образ + бот говорит «пора» → обновиться
    [[ -d .git ]] || exit 0
    now=""; [[ -f data/update-now ]] && { now="--now"; rm -f data/update-now; }   # убрать сразу: иначе path-юнит сработает снова
    git fetch -q origin main 2>/dev/null || exit 0
    before="$(git rev-parse HEAD)"; after="$(git rev-parse origin/main)"
    [[ "$before" == "$after" ]] && exit 0
    git merge-base --is-ancestor HEAD origin/main || exit 0   # локальные правки — только вручную
    repo="$(gh_repo)"; [[ -n "$repo" ]] || exit 0
    docker pull -q "ghcr.io/${repo,,}:sha-$after" >/dev/null 2>&1 || exit 0   # тесты ещё идут или не прошли
    compose exec -T nodes python -m bot.selfupdate check $now >/dev/null 2>&1 || exit 0
    { printf '%s %s\n' "${before:0:7}" "${after:0:7}"; git log --no-merges --format='%s' "$before..$after" | head -12; } > data/update-note.txt
    git merge -q --ff-only origin/main || { rm -f data/update-note.txt; exit 1; }
    exec ./install.sh _rebuild "${before:0:7}" ;;
  restore)  # переезд: .env и data уже перенесены ботом со старого сервера — запустить без вопросов
    [[ -f .env && -f data/bot.db ]] || die "Нет .env или data/bot.db — это команда для переезда"
    command -v docker >/dev/null || die "Нет Docker"
    deploy; install_cli
    if wait_ready; then ok "Бот запущен"; echo RESTORE_OK; else die "Бот не запустился — посмотрите: bnodes logs"; fi
    exit 0 ;;
  _rebuild)
    deploy "${2:-}"; install_cli
    wait_ready && ok "Бот перезапущен" || warn "Бот не ответил — посмотрите bnodes logs"
    exit 0 ;;
  restart) compose restart nodes; ok "Бот перезапущен"; exit 0 ;;
  logs)    compose logs -f --tail=100 nodes; exit 0 ;;
  status)
    compose ps
    [[ -d .git ]] && say "Версия: $(git log -1 --format='%h от %cd' --date=format:'%d.%m.%Y %H:%M')"
    say "Панель: $(env_get PUBLIC_URL)/admin"
    exit 0 ;;
  backup)
    mkdir -p /root/bnodes-backups && chmod 700 /root/bnodes-backups
    f="/root/bnodes-backups/bnodes-$(date +%Y%m%d-%H%M%S).tar.gz"
    tar -czf "$f" data .env 2>/dev/null || die "Не удалось создать копию"
    chmod 600 "$f"
    ls -1t /root/bnodes-backups/bnodes-*.tar.gz | tail -n +11 | xargs -r rm -f
    ok "Копия: $f"; exit 0 ;;
  harden)   harden; exit 0 ;;
  security) security_status; exit 0 ;;
  help|-h|--help) usage; exit 0 ;;
  setup|install) ;;
  *) usage; die "Неизвестная команда: $1" ;;
esac

# ───────── значения из прошлой установки ─────────
declare -A V
if [[ -f .env ]]; then
  while IFS='=' read -r k v; do
    [[ -z "$k" || "$k" == \#* ]] && continue
    v="${v#\'}"; v="${v%\'}"; V["$k"]="$v"
  done < .env
  warn "Найден .env — Enter оставит текущее значение."
fi

ask() {
  local name="$1" q="$2" secret="${3:-}" cur="${V[$1]:-}" shown="" ans=""
  if [[ -n "$cur" ]]; then
    if [[ -n "$secret" ]]; then shown=" [оставить текущее]"; else shown=" [$cur]"; fi
  fi
  while true; do
    if [[ -n "$secret" ]]; then read -rs -p "$q$shown: " ans </dev/tty || true; echo
    else read -r -p "$q$shown: " ans </dev/tty || true; fi
    ans="${ans:-$cur}"
    [[ "$ans" == *"'"* ]] && { warn "Символ ' не поддерживается, повторите."; continue; }
    V["$name"]="$ans"; return
  done
}
yesno() { local a; read -r -p "$1 [Y/n]: " a </dev/tty || true; [[ -z "$a" || "$a" =~ ^[YyДд] ]]; }

say "${C_B}══ Установка Bobr Nodes ══${C_0}"

# ───────── Docker ─────────
if ! command -v docker >/dev/null 2>&1; then
  say "Устанавливаю Docker…"
  command -v curl >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq curl ca-certificates; }
  curl -fsSL https://get.docker.com | sh >/dev/null
fi
docker compose version >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq docker-compose-plugin; }
systemctl enable --now docker >/dev/null 2>&1 || true
command -v curl >/dev/null 2>&1 || apt-get install -y -qq curl >/dev/null
ok "Docker готов"

# ───────── Telegram ─────────
say; say "${C_B}1/2 Telegram${C_0}  (новый бот — у @BotFather, ваш ID — у @userinfobot)"
while true; do
  ask BOT_TOKEN "Токен бота" secret
  r=$(curl -fsS "https://api.telegram.org/bot${V[BOT_TOKEN]}/getMe" 2>/dev/null || true)
  if [[ "$r" == *'"ok":true'* ]]; then BOT_USER="$(sed -n 's/.*"username":"\([^"]*\)".*/\1/p' <<<"$r")"; ok "Бот: @$BOT_USER"; break; fi
  warn "Токен не подошёл, попробуйте ещё раз."; V[BOT_TOKEN]=""
done
BDIR="$(bcloud_dir)"
if [[ -n "$BDIR" && "${V[BOT_TOKEN]}" == "$(benv "$BDIR" BOT_TOKEN)" ]]; then
  die "Это токен бота bcloud. Создайте отдельного бота у @BotFather."
fi
[[ -z "${V[ADMIN_IDS]:-}" && -n "$BDIR" ]] && V[ADMIN_IDS]="$(benv "$BDIR" ADMIN_IDS)"
while true; do
  ask ADMIN_IDS "Telegram ID администратора (несколько — через запятую)"
  V[ADMIN_IDS]="${V[ADMIN_IDS]// /}"
  [[ "${V[ADMIN_IDS]}" =~ ^[0-9]+(,[0-9]+)*$ ]] && break
  warn "Нужны только цифры, через запятую."; V[ADMIN_IDS]=""
done

# ───────── bcloud на этом сервере ─────────
MODE=caddy; V[BCLOUD_URL]="${V[BCLOUD_URL]:-}"; V[BCLOUD_NET]=""
if [[ -n "$BDIR" && -f "$BDIR/.env" ]]; then
  say; ok "Найден bcloud: $BDIR"
  if ! bcloud help 2>/dev/null | grep -q "bcloud site"; then
    warn "Эта версия bcloud ещё не умеет подключать другие программы. Обновите: bcloud update — и запустите установку снова."
    yesno "Продолжить без подключения (bcloud настроите в админке)?" || exit 0
  elif yesno "Покупать серверы через этот bcloud и работать за его HTTPS?"; then
    tok="$(benv "$BDIR" SERVICE_TOKEN)"
    if [[ -z "$tok" ]]; then
      say "Включаю служебный API bcloud (бот bcloud перезапустится на пару секунд)…"
      bcloud api-token >/dev/null
      tok="$(benv "$BDIR" SERVICE_TOKEN)"
    fi
    [[ -n "$tok" ]] || die "Не удалось получить токен bcloud — выполните bcloud api-token и повторите"
    # аккаунт для покупок: первый общий админ (покупать через API можно только от имени ADMIN_IDS bcloud)
    badm=",$(benv "$BDIR" ADMIN_IDS | tr -d ' '),"; buyer=""
    IFS=',' read -ra ids <<<"${V[ADMIN_IDS]}"
    for id in "${ids[@]}"; do [[ "$badm" == *",$id,"* ]] && { buyer="$id"; break; }; done
    if [[ -z "$buyer" ]]; then
      buyer="$(cut -d, -f1 <<<"${badm:1}")"
      warn "Ни один из ваших ID не админ bcloud — покупки пойдут с баланса $buyer (поменяете в админке)."
    fi
    V[BCLOUD_URL]="http://bot:8080"; V[BCLOUD_TOKEN]="$tok"; V[BCLOUD_TG_ID]="$buyer"
    V[BCLOUD_NET]="$(bcloud site network 2>/dev/null | head -1)"
    [[ -n "${V[BCLOUD_NET]}" ]] || die "Не нашёл docker-сеть bcloud — он запущен? (bcloud status)"
    MODE=bcloud
    ok "bcloud подключён: покупки с баланса $buyer"
  fi
fi

# ───────── Домен панели ─────────
say; say "${C_B}2/2 Домен панели управления${C_0}  (отдельный поддомен, A-запись — на IP этого сервера)"
SERVER_IP=$(curl -fsS -4 https://api.ipify.org 2>/dev/null || curl -fsS -4 https://ifconfig.me 2>/dev/null || echo "")
[[ -n "$SERVER_IP" ]] && say "IP этого сервера: $SERVER_IP"
taken=""; [[ "$MODE" == bcloud ]] && { m="$(benv "$BDIR" PUBLIC_URL)"; m="${m#https://}"; taken="${m%%/*}"; }
while true; do
  ask DOMAIN "Домен (например nodes.example.com)"
  d="${V[DOMAIN]#https://}"; d="${d%%/*}"; d="${d,,}"; V[DOMAIN]="$d"
  [[ "$d" =~ ^[a-z0-9.-]+\.[a-z]{2,}$ ]] || { warn "Похоже на неверный домен."; V[DOMAIN]=""; continue; }
  [[ -n "$taken" && "$d" == "$taken" ]] && { warn "Это домен bcloud — нужен отдельный поддомен."; V[DOMAIN]=""; continue; }
  RES=$(getent ahostsv4 "$d" 2>/dev/null | awk '{print $1; exit}' || true)
  if [[ -n "$SERVER_IP" && "$RES" == "$SERVER_IP" ]]; then ok "DNS указывает на этот сервер"; break; fi
  warn "Домен сейчас указывает на «${RES:-ничего}», а не на $SERVER_IP. Без этого HTTPS не получится."
  yesno "Продолжить всё равно?" && break
  V[DOMAIN]=""
done

if [[ "$MODE" == caddy ]]; then
  for p in 80 443; do
    port_busy $p || continue
    # свой же Caddy из прошлой установки — не помеха
    owner="$(docker ps --filter "publish=$p" --format '{{.Label "com.docker.compose.project.working_dir"}}' 2>/dev/null | head -1)"
    [[ "$owner" == "$(pwd)" ]] && continue
    die "Порт $p занят другой программой. Освободите его или поставьте бота на сервер с bcloud."
  done
fi

# ───────── Файлы ─────────
umask 077
cat > .env <<EOF
BOT_TOKEN='${V[BOT_TOKEN]}'
ADMIN_IDS='${V[ADMIN_IDS]}'
DOMAIN='${V[DOMAIN]}'
PUBLIC_URL='https://${V[DOMAIN]}'
BCLOUD_URL='${V[BCLOUD_URL]:-}'
BCLOUD_TOKEN='${V[BCLOUD_TOKEN]:-}'
BCLOUD_TG_ID='${V[BCLOUD_TG_ID]:-}'
COMPOSE_PROFILES='$([[ "$MODE" == caddy ]] && echo caddy)'
BNODES_IMAGE='${V[BNODES_IMAGE]:-}'
EOF
chmod 600 .env
mkdir -p data && chmod 700 data
if [[ "$MODE" == bcloud ]]; then
  cat > docker-compose.override.yml <<EOF
# создано установщиком: бот в сети bcloud — Caddy bcloud проксирует панель, бот обращается к bcloud напрямую
services:
  nodes:
    networks: [default, bcloud]
networks:
  bcloud:
    external: true
    name: ${V[BCLOUD_NET]}
EOF
  rm -f Caddyfile
else
  rm -f docker-compose.override.yml
  printf '%s {\n\treverse_proxy nodes:8080\n}\n' "${V[DOMAIN]}" > Caddyfile
fi
ok "Конфигурация записана"

# ───────── Запуск ─────────
say "Запускаю…"
deploy
if [[ "$MODE" == bcloud ]]; then
  bcloud site add nodes "${V[DOMAIN]}" nodes:8080 >/dev/null
  ok "Панель подключена к HTTPS bcloud"
fi
install_cli
wait_ready || die "Бот не запустился — посмотрите: bnodes logs"
ok "Бот запущен"

say
say "${C_B}Готово!${C_0}"
say "  1. Откройте @${BOT_USER:-вашего бота} → /start → «Панель»."
if [[ "$MODE" == bcloud ]]; then
  say "  2. В панели: добавьте токен Cloudflare и профиль панели Remnawave — bcloud уже подключён."
else
  say "  2. В панели: подключите bcloud (адрес и токен — bcloud api-token на его сервере), Cloudflare и профиль Remnawave."
fi
say "  Сертификат HTTPS выпускается в первую минуту — если панель не открылась сразу, подождите."
say "  Управление: bnodes help"
if [[ ! -f data/.hardened ]]; then
  say
  warn "Сервер ещё не защищён, а в базе бота будут root-пароли нод и токены панелей."
  if yesno "Защитить сервер сейчас (файрвол, fail2ban, SSH, автообновления)?"; then harden; else say "Позже: bnodes harden"; fi
fi
