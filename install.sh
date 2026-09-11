#!/bin/bash
# ArcReports — Instalador híbrido
# Verifica, instala e configura tudo
# Compatível com RHEL 9.x / Rocky / Alma
# Uso: sudo bash install.sh
set -euo pipefail

umask 077
INSTALL_DIR="/opt/sites/glpi-portal"
SERVICE_USER="ia-dev"
DB_NAME="reports"
DB_APP_USER="glpi_portal"
DB_ADMIN_USER="portal_db_admin"
DB_USER_ADMIN="portal_db_user"
INSTALL_MODE=""
PYTHON_BIN=""
DATABASE_IS_NEW=0
MARIADB_INSTALLED_NOW=0
NGINX_INSTALLED_NOW=0
DB_ROOT_CNF=""
DB_VALUES_FILE=""

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { printf "${GREEN}✓${NC} %s\n" "$1"; }
warn() { printf "${YELLOW}⚠${NC} %s\n" "$1"; }
fail() { printf "${RED}✗${NC} %s\n" "$1" >&2; exit 1; }

on_error() {
    local status=$?
    printf "${RED}✗${NC} Falha na linha %s (comando: %s).\n" "$1" "$2" >&2
    printf '  Corrija a causa indicada e execute novamente; as etapas concluídas são idempotentes.\n' >&2
    exit "$status"
}

cleanup() {
    [[ -z "$DB_ROOT_CNF" || ! -f "$DB_ROOT_CNF" ]] || rm -f -- "$DB_ROOT_CNF"
    [[ -z "$DB_VALUES_FILE" || ! -f "$DB_VALUES_FILE" ]] || rm -f -- "$DB_VALUES_FILE"
}
trap 'on_error "$LINENO" "$BASH_COMMAND"' ERR
trap cleanup EXIT

# --- Funções de versão e instalação ---
version_ge() {
    [[ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1)" == "$2" ]]
}

extract_version() {
    if [[ "$1" =~ ([0-9]+\.[0-9]+(\.[0-9]+)?) ]]; then
        printf '%s\n' "${BASH_REMATCH[1]}"
    else
        return 1
    fi
}

extract_mariadb_version() {
    [[ "$1" == *MariaDB* ]] || return 1
    if [[ "$1" =~ Distrib[[:space:]]+([0-9]+\.[0-9]+(\.[0-9]+)?) ]]; then
        printf '%s\n' "${BASH_REMATCH[1]}"
    elif [[ "$1" =~ ([0-9]+\.[0-9]+(\.[0-9]+)?)-MariaDB ]]; then
        printf '%s\n' "${BASH_REMATCH[1]}"
    else
        return 1
    fi
}

dnf_install() {
    command -v dnf >/dev/null 2>&1 || fail "dnf ausente. Este instalador requer RHEL 9.x, Rocky Linux ou AlmaLinux."
    dnf install -y "$@"
}

check_or_install_python() {
    local output="" version="" install_needed=0
    if command -v python3 >/dev/null 2>&1; then
        output="$(python3 --version 2>&1)"
        version="$(extract_version "$output" || true)"
        [[ -n "$version" ]] && version_ge "$version" 3.9 || install_needed=1
    else
        install_needed=1
    fi
    if (( install_needed )); then
        warn "Python ${version:-não encontrado} — instalando Python 3.9+."
        dnf_install python39 python3-pip
    fi
    if command -v python3.9 >/dev/null 2>&1; then
        PYTHON_BIN="$(command -v python3.9)"
    else
        PYTHON_BIN="$(command -v python3 || true)"
    fi
    [[ -n "$PYTHON_BIN" ]] || fail "Python não ficou disponível após dnf."
    output="$($PYTHON_BIN --version 2>&1)"
    version="$(extract_version "$output" || true)"
    [[ -n "$version" ]] && version_ge "$version" 3.9 || fail "Python 3.9+ indisponível (detectado: $output)."
    ok "Python $version encontrado"
}

secure_new_mariadb() {
    # Equivalente não interativo ao mysql_secure_installation. Altera somente
    # contas internas do MariaDB local e o schema de teste, nunca o banco GLPI.
    "$1" --protocol=socket --user=root <<'SQL'
DELETE FROM mysql.global_priv WHERE User='';
DELETE FROM mysql.global_priv WHERE User='root' AND Host NOT IN ('localhost', '127.0.0.1', '::1');
DROP DATABASE IF EXISTS test;
DELETE FROM mysql.db WHERE Db='test' OR Db LIKE 'test\\_%';
FLUSH PRIVILEGES;
SQL
    ok "mysql_secure_installation aplicado de forma não interativa"
}

check_or_install_mariadb() {
    local output="" version="" install_needed=0 mysql_client
    if command -v mysql >/dev/null 2>&1; then
        output="$(mysql --version 2>&1)"
        version="$(extract_mariadb_version "$output" || true)"
        [[ -n "$version" ]] && version_ge "$version" 10.5 || install_needed=1
    else
        install_needed=1
    fi
    if (( install_needed )); then
        warn "MariaDB ${version:-não encontrado} — instalando MariaDB 10.5+."
        dnf_install mariadb-server mariadb
        MARIADB_INSTALLED_NOW=1
    fi
    mysql_client="$(command -v mysql || true)"
    [[ -n "$mysql_client" ]] || fail "mysql não ficou disponível após dnf."
    output="$($mysql_client --version 2>&1)"
    version="$(extract_mariadb_version "$output" || true)"
    [[ -n "$version" ]] && version_ge "$version" 10.5 || fail "MariaDB 10.5+ indisponível (detectado: $output)."
    systemctl enable --now mariadb
    systemctl is-active --quiet mariadb || fail "MariaDB não iniciou. Consulte: journalctl -u mariadb"
    if (( MARIADB_INSTALLED_NOW )); then
        secure_new_mariadb "$mysql_client"
        ok "MariaDB $version instalado com sucesso"
    else
        ok "MariaDB $version encontrado e serviço ativo"
    fi
}

check_or_install_nginx() {
    local output="" version="" install_needed=0
    if command -v nginx >/dev/null 2>&1; then
        output="$(nginx -v 2>&1)"
        version="$(extract_version "$output" || true)"
        [[ -n "$version" ]] && version_ge "$version" 1.18 || install_needed=1
    else
        install_needed=1
    fi
    if (( install_needed )); then
        warn "Nginx ${version:-não encontrado} — instalando Nginx 1.18+."
        dnf_install nginx
        NGINX_INSTALLED_NOW=1
    fi
    command -v nginx >/dev/null 2>&1 || fail "nginx não ficou disponível após dnf."
    output="$(nginx -v 2>&1)"
    version="$(extract_version "$output" || true)"
    [[ -n "$version" ]] && version_ge "$version" 1.18 || fail "Nginx 1.18+ indisponível (detectado: $output)."
    systemctl enable --now nginx
    systemctl is-active --quiet nginx || fail "Nginx não iniciou. Consulte: journalctl -u nginx"
    (( NGINX_INSTALLED_NOW )) && ok "Nginx $version instalado com sucesso" || ok "Nginx $version encontrado e serviço ativo"
}

check_or_install_git() {
    if command -v git >/dev/null 2>&1; then
        ok "Git encontrado ($(git --version))"
    else
        dnf_install git
        command -v git >/dev/null 2>&1 || fail "Git não ficou disponível após dnf."
        ok "Git instalado com sucesso ($(git --version))"
    fi
}

check_or_install_curl() {
    if command -v curl >/dev/null 2>&1; then
        ok "curl encontrado"
    else
        dnf_install curl
        command -v curl >/dev/null 2>&1 || fail "curl não ficou disponível após dnf."
        ok "curl instalado com sucesso"
    fi
}

check_or_install_mysqldump() {
    if command -v mysqldump >/dev/null 2>&1; then
        ok "mysqldump encontrado"
    else
        warn "mysqldump ausente — instalando o cliente MariaDB."
        dnf_install mariadb
        command -v mysqldump >/dev/null 2>&1 || fail "mysqldump não ficou disponível com o pacote mariadb."
        ok "mysqldump instalado com sucesso"
    fi
}

# --- Verificar se é root ---
check_root() {
    [[ "$EUID" -eq 0 ]] || fail "Execute como root: sudo bash install.sh"
    ok "Root confirmado; operações da aplicação usarão runuser -u $SERVICE_USER"
}

detect_install_mode() {
    if [[ -n "${ARCREPORTS_INSTALL_MODE:-}" ]]; then
        case "${ARCREPORTS_INSTALL_MODE,,}" in
            new|nova) INSTALL_MODE="new" ;;
            update|atualizacao|atualização) INSTALL_MODE="update" ;;
            *) fail "ARCREPORTS_INSTALL_MODE deve ser new ou update." ;;
        esac
    elif [[ -f "$INSTALL_DIR/.env" || -d "$INSTALL_DIR/venv" || -f /etc/systemd/system/arcreports.service ]]; then
        printf 'Instalação existente detectada. [N]ova instalação ou [U]pdate (recomendado)? '
        read -r answer
        case "${answer,,}" in
            n|nova|new) INSTALL_MODE="new" ;;
            ''|u|update|atualizacao|atualização) INSTALL_MODE="update" ;;
            *) fail "Opção inválida; informe N ou U." ;;
        esac
    else
        INSTALL_MODE="new"
    fi
    if [[ "$INSTALL_MODE" == update ]]; then
        ok "Modo update: .env, local.env, ldap.env, logs/, snapshots/ e uploads/ serão preservados"
    else
        ok "Modo nova instalação"
    fi
    export INSTALL_MODE
}

# --- Criar usuário e diretório ---
create_service_user() {
    if id "$SERVICE_USER" >/dev/null 2>&1; then
        ok "Usuário $SERVICE_USER já existe"
    else
        useradd -m -s /bin/bash "$SERVICE_USER"
        ok "Usuário $SERVICE_USER criado"
    fi
}

create_install_dir() {
    if [[ -d "$INSTALL_DIR" ]]; then
        ok "Diretório $INSTALL_DIR já existe"
    else
        mkdir -p "$INSTALL_DIR"
        chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
        ok "Diretório $INSTALL_DIR criado"
    fi
    for file in requirements.txt .env.example local.env.example ldap.env.example alembic.ini docs/nginx.example.conf docs/arcreports.service.example; do
        [[ -r "$INSTALL_DIR/$file" ]] || fail "Arquivo necessário ausente: $INSTALL_DIR/$file. Copie o pacote ArcReports completo."
    done
    runuser -u "$SERVICE_USER" -- test -w "$INSTALL_DIR" || fail "$SERVICE_USER não possui escrita em $INSTALL_DIR. Corrija proprietário/permissões."
}

# --- Configurar venv Python ---
setup_venv() {
    if [[ -x "$INSTALL_DIR/venv/bin/python" ]]; then
        ok "Ambiente virtual existente será reutilizado"
    else
        runuser -u "$SERVICE_USER" -- "$PYTHON_BIN" -m venv "$INSTALL_DIR/venv"
        ok "Ambiente virtual criado"
    fi
    local version
    version="$(extract_version "$("$INSTALL_DIR/venv/bin/python" --version 2>&1)" || true)"
    [[ -n "$version" ]] && version_ge "$version" 3.9 || fail "O venv usa Python ${version:-desconhecido}; recrie-o com Python 3.9+."
    [[ -z "${PIP_FIND_LINKS:-}" ]] || ok "pip usará o repositório offline: $PIP_FIND_LINKS"
    runuser -u "$SERVICE_USER" -- env PIP_FIND_LINKS="${PIP_FIND_LINKS:-}" PIP_NO_INDEX="${PIP_NO_INDEX:-0}" \
        "$INSTALL_DIR/venv/bin/python" -m pip install -r "$INSTALL_DIR/requirements.txt"
    runuser -u "$SERVICE_USER" -- "$INSTALL_DIR/venv/bin/python" -m pip check
    ok "Dependências Python instaladas e validadas"
}

# --- Gerar .env ---
setup_env() {
    runuser -u "$SERVICE_USER" -- env INSTALL_DIR="$INSTALL_DIR" INSTALL_MODE="$INSTALL_MODE" "$INSTALL_DIR/venv/bin/python" <<'PY'
import getpass, os, re, secrets
from pathlib import Path
from dotenv import dotenv_values

base = Path(os.environ['INSTALL_DIR']); target = base / '.env'
if target.is_symlink(): raise SystemExit('ERRO: .env não pode ser link simbólico')

def quote(value):
    if any(c in value for c in '\n\r\0'): raise SystemExit('ERRO: valor multilinha/NUL não permitido')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'

def secret(label):
    one = getpass.getpass(label + ': '); two = getpass.getpass('Confirme ' + label + ': ')
    if one != two or len(one) < 16: raise SystemExit('ERRO: senhas devem coincidir e ter pelo menos 16 caracteres')
    return one

old_key = str(dotenv_values(target, interpolate=False).get('SECRET_KEY') or '') if target.exists() else ''
if target.exists() and (not old_key or old_key == 'GERAR_COM_COMANDO_ACIMA'):
    raise SystemExit('ERRO: restaure a SECRET_KEY original no .env existente antes de continuar')
if target.exists() and os.environ['INSTALL_MODE'] == 'update':
    print('✓ .env preservado integralmente no update, incluindo SECRET_KEY'); raise SystemExit
overwrite = False
if target.exists():
    overwrite = input('.env já existe. Sobrescrever preservando a SECRET_KEY? [s/N] ').strip().lower() == 's'
    if not overwrite: print('⚠ .env existente mantido'); raise SystemExit
text = (base / '.env.example').read_text(encoding='utf-8')
values = {'LOCAL_DB_PASS': secret('Senha de glpi_portal'),
          'LOCAL_DB_ADMIN_PASS': secret('Senha de portal_db_admin'),
          'LOCAL_DB_USER_ADMIN_PASS': secret('Senha de portal_db_user'),
          'SECRET_KEY': old_key or secrets.token_urlsafe(48)}
for key, value in values.items():
    line = key + '=' + quote(value); pattern = re.compile(rf'(?m)^{re.escape(key)}=.*$')
    text = pattern.sub(lambda _: line, text) if pattern.search(text) else text.rstrip() + '\n' + line + '\n'
temporary = base / '.env.installing'; temporary.write_text(text, encoding='utf-8'); temporary.chmod(0o600); temporary.replace(target)
target.chmod(0o600)
print('✓ .env atualizado; SECRET_KEY preservada' if overwrite else '✓ .env criado; SECRET_KEY gerada uma vez')
PY
}

# --- Gerar local.env ---
setup_local_env() {
    runuser -u "$SERVICE_USER" -- env INSTALL_DIR="$INSTALL_DIR" "$PYTHON_BIN" <<'PY'
import getpass, os, re
from pathlib import Path
base = Path(os.environ['INSTALL_DIR']); target = base / 'local.env'
if target.is_symlink(): raise SystemExit('ERRO: local.env não pode ser link simbólico')
if target.exists():
    print('✓ local.env já existe — mantendo')
else:
    user = input('ADMIN_USERNAME [admin_local]: ').strip() or 'admin_local'
    if not re.fullmatch(r'[A-Za-z0-9_.-]{3,64}', user) or user.lower() == 'scheduler': raise SystemExit('ERRO: ADMIN_USERNAME inválido')
    password = getpass.getpass('ADMIN_PASSWORD: '); confirmation = getpass.getpass('Confirme ADMIN_PASSWORD: ')
    if password != confirmation or len(password) < 16: raise SystemExit('ERRO: senhas devem coincidir e ter pelo menos 16 caracteres')
    text = (base / 'local.env.example').read_text(encoding='utf-8')
    for key, value in {'ADMIN_USERNAME': user, 'ADMIN_PASSWORD': password}.items():
        quoted = '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'
        text = re.sub(rf'(?m)^{key}=.*$', lambda _: key + '=' + quoted, text)
    target.write_text(text, encoding='utf-8'); target.chmod(0o600); print('✓ local.env criado')
ldap = base / 'ldap.env'
if ldap.is_symlink(): raise SystemExit('ERRO: ldap.env não pode ser link simbólico')
if ldap.exists(): print('✓ ldap.env já existe — mantendo')
else:
    ldap.write_text((base / 'ldap.env.example').read_text(encoding='utf-8'), encoding='utf-8'); ldap.chmod(0o600); print('✓ ldap.env criado')
for name in ('logs', 'snapshots', 'static/uploads', 'static/uploads/appearance'): (base / name).mkdir(parents=True, exist_ok=True)
PY
}

create_db_root_cnf() {
    local password escaped_password
    printf 'Senha root do MariaDB (Enter para unix_socket): '; read -r -s password; printf '\n'
    DB_ROOT_CNF="$(mktemp /run/arcreports-db-root.XXXXXX.cnf)"; chmod 600 "$DB_ROOT_CNF"
    escaped_password="${password//\\/\\\\}"; escaped_password="${escaped_password//\"/\\\"}"
    { printf '[client]\nuser=root\nprotocol=socket\n'; [[ -z "$password" ]] || printf 'password="%s"\n' "$escaped_password"; } > "$DB_ROOT_CNF"
}

# --- Configurar MariaDB local (nunca o banco GLPI) ---
setup_mariadb() {
    create_db_root_cnf
    local exists
    exists="$(mysql --defaults-extra-file="$DB_ROOT_CNF" --batch --skip-column-names --execute="SELECT COUNT(*) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME='${DB_NAME}'")" || \
        fail "Falha ao autenticar no MariaDB local. Confira a senha root ou unix_socket."
    [[ "$exists" != 0 ]] || DATABASE_IS_NEW=1
    DB_VALUES_FILE="$(mktemp /tmp/arcreports-db-values.XXXXXX)"
    chmod 600 "$DB_VALUES_FILE"
    runuser -u "$SERVICE_USER" -- env INSTALL_DIR="$INSTALL_DIR" "$INSTALL_DIR/venv/bin/python" <<'PY' > "$DB_VALUES_FILE"
import base64, os
from pathlib import Path
from dotenv import dotenv_values
values = dotenv_values(Path(os.environ['INSTALL_DIR']) / '.env', interpolate=False)
for key in ('LOCAL_DB_PASS', 'LOCAL_DB_ADMIN_PASS', 'LOCAL_DB_USER_ADMIN_PASS'):
    value = str(values.get(key) or '')
    if len(value) < 16 or any(c in value for c in '\n\r\0'): raise SystemExit('ERRO: credencial inválida em .env: ' + key)
    print(base64.b64encode(value.encode()).decode())
PY
    local -a db_values; mapfile -t db_values < "$DB_VALUES_FILE"; rm -f -- "$DB_VALUES_FILE"; DB_VALUES_FILE=""
    [[ ${#db_values[@]} -eq 3 ]] || fail "Não foi possível carregar as credenciais do .env."
    local app_pass admin_pass user_admin_pass
    app_pass="$(printf '%s' "${db_values[0]}" | base64 --decode)"
    admin_pass="$(printf '%s' "${db_values[1]}" | base64 --decode)"
    user_admin_pass="$(printf '%s' "${db_values[2]}" | base64 --decode)"
    app_pass="${app_pass//\\/\\\\}"; admin_pass="${admin_pass//\\/\\\\}"; user_admin_pass="${user_admin_pass//\\/\\\\}"
    app_pass="${app_pass//\'/\'\'}"; admin_pass="${admin_pass//\'/\'\'}"; user_admin_pass="${user_admin_pass//\'/\'\'}"
    mysql --defaults-extra-file="$DB_ROOT_CNF" <<SQL
CREATE DATABASE IF NOT EXISTS \`${DB_NAME}\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS '${DB_APP_USER}'@'localhost' IDENTIFIED BY '${app_pass}';
CREATE USER IF NOT EXISTS '${DB_ADMIN_USER}'@'localhost' IDENTIFIED BY '${admin_pass}';
CREATE USER IF NOT EXISTS '${DB_USER_ADMIN}'@'localhost' IDENTIFIED BY '${user_admin_pass}';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, INDEX, ALTER ON \`${DB_NAME}\`.* TO '${DB_APP_USER}'@'localhost';
GRANT ALL PRIVILEGES ON *.* TO '${DB_ADMIN_USER}'@'localhost' WITH GRANT OPTION;
GRANT CREATE USER ON *.* TO '${DB_USER_ADMIN}'@'localhost';
GRANT SELECT ON \`${DB_NAME}\`.* TO '${DB_USER_ADMIN}'@'localhost' WITH GRANT OPTION;
FLUSH PRIVILEGES;
SQL
    (( DATABASE_IS_NEW )) && ok "Banco local $DB_NAME e usuários criados" || ok "Banco local $DB_NAME já existia; usuários e grants conferidos"
}

# --- Executar Alembic baseline ---
setup_alembic() {
    if (( DATABASE_IS_NEW )); then
        runuser -u "$SERVICE_USER" -- bash -c 'cd "$1" && venv/bin/alembic stamp head' _ "$INSTALL_DIR"
        ok "Alembic baseline registrado no banco novo"
    else
        warn "Banco existente — alembic stamp head não executado"
    fi
}

# --- Configurar Nginx ---
setup_nginx() {
    local source="$INSTALL_DIR/docs/nginx.example.conf" target="/etc/nginx/conf.d/arcreports.conf" replace=0 answer
    if [[ ! -f "$target" ]]; then
        replace=1
    elif [[ "$INSTALL_MODE" == update ]]; then
        warn "Nginx já configurado — mantendo $target"
    else
        printf 'Configuração Nginx existente. Sobrescrever após backup? [s/N] '; read -r answer
        [[ "${answer,,}" == s ]] && replace=1 || warn "Nginx já configurado — mantendo"
    fi
    if (( replace )); then
        [[ ! -f "$target" ]] || cp -a "$target" "${target}.bak.$(date +%Y%m%d%H%M%S)"
        install -o root -g root -m 0644 "$source" "$target"; ok "Configuração Nginx instalada"
    fi
    nginx -t || fail "nginx -t falhou. Revise $target antes de recarregar."
    systemctl reload nginx; ok "Nginx validado e recarregado"
}

# --- Configurar systemd ---
setup_systemd() {
    local source="$INSTALL_DIR/docs/arcreports.service.example" target="/etc/systemd/system/arcreports.service"
    if [[ -f "$target" ]]; then warn "Unidade systemd já existe — mantendo $target"
    else install -o root -g root -m 0644 "$source" "$target"; ok "Unidade systemd instalada"; fi
    systemctl daemon-reload; systemctl enable arcreports; systemctl restart arcreports
    systemctl is-active --quiet arcreports || fail "ArcReports não iniciou. Consulte: journalctl -u arcreports"
    ok "Serviço ArcReports habilitado e ativo"
}

# --- Verificar instalação ---
verify_install() {
    local attempt code=000
    for attempt in {1..10}; do
        code="$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' --connect-timeout 3 --max-time 10 http://127.0.0.1:8000/login || true)"
        [[ "$code" == 200 ]] && break
        sleep 2
    done
    [[ "$code" == 200 ]] || fail "Verificação /login retornou HTTP $code. Consulte logs/ e journalctl -u arcreports."
    ok "ArcReports respondeu HTTP 200 em /login"
}

# --- Resumo final ---
print_summary() {
    echo "=================================="
    echo " ArcReports instalado com sucesso"
    echo "=================================="
    echo " URL: http://SEU_IP"
    echo " Usuário: ADMIN_USERNAME de local.env"
    echo " Logs: $INSTALL_DIR/logs/"
    echo " Snapshots: $INSTALL_DIR/snapshots/"
    echo "=================================="
}

# --- Fluxo principal ---
main() {
    check_root
    detect_install_mode
    check_or_install_python
    check_or_install_mariadb
    check_or_install_nginx
    check_or_install_git
    check_or_install_curl
    check_or_install_mysqldump
    create_service_user
    create_install_dir
    setup_venv
    setup_env
    setup_local_env
    setup_mariadb
    setup_alembic
    setup_nginx
    setup_systemd
    verify_install
    print_summary
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
