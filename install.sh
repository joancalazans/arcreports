#!/bin/bash
# ArcReports — Instalador híbrido
# Suportado: RHEL 9.x, Rocky 9, AlmaLinux 9,
#            Fedora 38+, Ubuntu 22.04+,
#            Ubuntu 24.04, Debian 11, Debian 12
# Uso: sudo bash install.sh
# Documentação: README.md
set -euo pipefail

umask 077
INSTALL_DIR="/opt/sites/arcreports"
SERVICE_USER="ia-dev"
DB_NAME="reports"
DB_APP_USER="glpi_portal"
DB_ADMIN_USER="portal_db_admin"
DB_USER_ADMIN="portal_db_user"
INSTALL_MODE=""
FAMILY=""
PKG_MANAGER=""
PYTHON_BIN=""
SERVICE_MARIADB="mariadb"
NGINX_CONF_DIR=""
NGINX_CONF_AVAILABLE=""
NGINX_CONF_ENABLED=""
NGINX_CONF=""
SELINUX_ACTIVE=false
APT_UPDATED=0
DATABASE_IS_NEW=0
MARIADB_INSTALLED_NOW=0
NGINX_INSTALLED_NOW=0
DB_ROOT_CNF=""
DB_VALUES_FILE=""

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { printf "${GREEN}✓${NC} %s\n" "$1"; }
warn() { printf "${YELLOW}⚠${NC} %s\n" "$1"; }
fail() { printf "${RED}✗${NC} %s\n" "$1" >&2; exit 1; }

validate_password() {
    local pass="$1"
    local valid=true

    if [[ ${#pass} -lt 8 ]]; then
        warn "Senha deve ter pelo menos 8 caracteres."
        valid=false
    fi
    if ! [[ "$pass" =~ [A-Z] ]]; then
        warn "Senha deve ter pelo menos 1 letra maiúscula."
        valid=false
    fi
    if ! [[ "$pass" =~ [a-z] ]]; then
        warn "Senha deve ter pelo menos 1 letra minúscula."
        valid=false
    fi
    if ! [[ "$pass" =~ [0-9] ]]; then
        warn "Senha deve ter pelo menos 1 número."
        valid=false
    fi
    [[ "$valid" == true ]]
}

prompt_password() {
    local prompt="$1"
    local password confirmation

    while true; do
        printf "\n"
        printf "Requisitos: mínimo 8 caracteres,\n"
        printf "  1 maiúscula, 1 minúscula,\n"
        printf "  1 número\n"
        printf "  (ex: Arcdata1)\n"
        printf "%s" "$prompt"
        read -r -s password </dev/tty
        printf "\n"
        printf "Confirme a senha: "
        read -r -s confirmation </dev/tty
        printf "\n"
        if [[ "$password" != "$confirmation" ]]; then
            warn "Senhas não coincidem. Tente novamente."
            continue
        fi
        if validate_password "$password"; then
            REPLY="$password"
            return
        fi
    done
}

prompt_database_password() {
    local account="$1"
    local description="$2"

    printf "\n"
    printf "Senha para conta de banco '%s'\n" "$account"
    prompt_password "$description"
}

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

detect_distribution() {
    [[ -r /etc/os-release ]] || fail "Não foi possível ler /etc/os-release."
    # shellcheck disable=SC1091
    . /etc/os-release
    : "${ID:?ID ausente em /etc/os-release}"
    : "${VERSION_ID:?VERSION_ID ausente em /etc/os-release}"

    case "$ID" in
        rhel|rocky|almalinux)
            version_ge "$VERSION_ID" 9 || fail "$NAME $VERSION_ID não suportado. Requer versão 9 ou superior."
            FAMILY="rhel"
            PKG_MANAGER="dnf"
            ;;
        fedora)
            version_ge "$VERSION_ID" 38 || fail "$NAME $VERSION_ID não suportado. Requer Fedora 38 ou superior."
            FAMILY="rhel"
            PKG_MANAGER="dnf"
            ;;
        ubuntu)
            version_ge "$VERSION_ID" 22.04 || fail "$NAME $VERSION_ID não suportado. Requer Ubuntu 22.04 ou superior."
            FAMILY="debian"
            PKG_MANAGER="apt-get"
            ;;
        debian)
            version_ge "$VERSION_ID" 11 || fail "$NAME $VERSION_ID não suportado. Requer Debian 11 ou superior."
            FAMILY="debian"
            PKG_MANAGER="apt-get"
            ;;
        *)
            fail "Distribuição '$ID' não suportada.
Suportadas: RHEL 9.x, Rocky 9, AlmaLinux 9,
Fedora 38+, Ubuntu 22.04+, Debian 11/12."
            ;;
    esac

    if [[ "$FAMILY" == rhel ]]; then
        NGINX_CONF_DIR="/etc/nginx/conf.d"
        NGINX_CONF="$NGINX_CONF_DIR/arcreports.conf"
    else
        NGINX_CONF_AVAILABLE="/etc/nginx/sites-available"
        NGINX_CONF_ENABLED="/etc/nginx/sites-enabled"
        NGINX_CONF="$NGINX_CONF_AVAILABLE/arcreports"
    fi

    ok "Distribuição detectada: ${NAME:-$ID} $VERSION_ID"
    ok "Família: $FAMILY (gerenciador: $PKG_MANAGER)"
}

apt_update() {
    if (( ! APT_UPDATED )); then
        apt-get update -qq
        APT_UPDATED=1
    fi
}

package_install() {
    if [[ "$FAMILY" == rhel ]]; then
        command -v dnf >/dev/null 2>&1 || fail "dnf não está disponível nesta distribuição."
        dnf install -y "$@"
    else
        command -v apt-get >/dev/null 2>&1 || fail "apt-get não está disponível nesta distribuição."
        apt_update
        apt-get install -y "$@"
    fi
}

check_or_install_python() {
    local output="" version="" install_needed=0
    if [[ "$FAMILY" == debian ]]; then
        if ! command -v python3.9 >/dev/null 2>&1; then
            apt_update
            if [[ "$ID" == ubuntu ]] && ! apt-cache show python3.9 >/dev/null 2>&1; then
                warn "Python 3.9 ausente nos repositórios — habilitando deadsnakes PPA."
                package_install software-properties-common
                add-apt-repository ppa:deadsnakes/ppa -y
                APT_UPDATED=0
                apt_update
            fi
            package_install python3.9 python3-pip python3.9-venv python3-venv
        else
            package_install python3-pip python3.9-venv python3-venv
        fi
        python3.9 --version >/dev/null 2>&1 || fail "Python 3.9 não disponível."
        PYTHON_BIN="$(command -v python3.9)"
        ok "Python 3.9 instalado"
        return
    fi

    if command -v python3 >/dev/null 2>&1; then
        output="$(python3 --version 2>&1)"
        version="$(extract_version "$output" || true)"
        [[ -n "$version" ]] && version_ge "$version" 3.9 || install_needed=1
    else
        install_needed=1
    fi
    if (( install_needed )); then
        warn "Python ${version:-não encontrado} — instalando Python 3.9+."
        package_install python3.9 python3-pip
    fi
    if command -v python3.9 >/dev/null 2>&1; then
        PYTHON_BIN="$(command -v python3.9)"
    else
        PYTHON_BIN="$(command -v python3 || true)"
    fi
    [[ -n "$PYTHON_BIN" ]] || fail "Python não ficou disponível após $PKG_MANAGER."
    output="$($PYTHON_BIN --version 2>&1)"
    version="$(extract_version "$output" || true)"
    [[ -n "$version" ]] && version_ge "$version" 3.9 || fail "Python 3.9+ indisponível (detectado: $output)."
    ok "Python 3.9 instalado (comando: $PYTHON_BIN, versão: $version)"
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
        if [[ "$FAMILY" == rhel ]]; then
            package_install mariadb-server mariadb
        else
            package_install mariadb-server
        fi
        MARIADB_INSTALLED_NOW=1
    fi
    mysql_client="$(command -v mysql || true)"
    [[ -n "$mysql_client" ]] || fail "mysql não ficou disponível após $PKG_MANAGER."
    output="$($mysql_client --version 2>&1)"
    version="$(extract_mariadb_version "$output" || true)"
    [[ -n "$version" ]] && version_ge "$version" 10.5 || fail "MariaDB 10.5+ indisponível (detectado: $output)."
    SERVICE_MARIADB="mariadb"
    if [[ "$FAMILY" == debian ]]; then
        systemctl list-units --type=service | grep -q mariadb || SERVICE_MARIADB="mysql"
    fi
    systemctl enable --now "$SERVICE_MARIADB"
    systemctl is-active --quiet "$SERVICE_MARIADB" || fail "MariaDB não iniciou. Consulte: journalctl -u $SERVICE_MARIADB"
    if (( MARIADB_INSTALLED_NOW )); then
        secure_new_mariadb "$mysql_client"
        ok "MariaDB instalado e ativo (versão $version)"
    else
        ok "MariaDB instalado e ativo (versão $version já existente)"
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
        package_install nginx
        NGINX_INSTALLED_NOW=1
    fi
    command -v nginx >/dev/null 2>&1 || fail "nginx não ficou disponível após $PKG_MANAGER."
    output="$(nginx -v 2>&1)"
    version="$(extract_version "$output" || true)"
    [[ -n "$version" ]] && version_ge "$version" 1.18 || fail "Nginx 1.18+ indisponível (detectado: $output)."
    systemctl enable --now nginx
    systemctl is-active --quiet nginx || fail "Nginx não iniciou. Consulte: journalctl -u nginx"
    (( NGINX_INSTALLED_NOW )) && ok "Nginx instalado e ativo (versão $version)" || ok "Nginx instalado e ativo (versão $version já existente)"
}

check_or_install_git() {
    if command -v git >/dev/null 2>&1; then
        ok "Git encontrado ($(git --version))"
    else
        package_install git
        command -v git >/dev/null 2>&1 || fail "Git não ficou disponível após $PKG_MANAGER."
        ok "Git instalado com sucesso ($(git --version))"
    fi
}

check_or_install_curl() {
    if command -v curl >/dev/null 2>&1; then
        ok "curl encontrado"
    else
        package_install curl
        command -v curl >/dev/null 2>&1 || fail "curl não ficou disponível após $PKG_MANAGER."
        ok "curl instalado com sucesso"
    fi
}

check_or_install_mysqldump() {
    if command -v mysqldump >/dev/null 2>&1; then
        ok "mysqldump encontrado"
    else
        warn "mysqldump ausente — instalando o cliente MariaDB."
        if [[ "$FAMILY" == rhel ]]; then
            package_install mariadb
        else
            package_install mariadb-client
        fi
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
        read -r answer </dev/tty
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
    if ! id "$SERVICE_USER" &>/dev/null; then
        useradd -m -s /bin/bash "$SERVICE_USER" \
            || adduser --disabled-password --gecos "" "$SERVICE_USER"
        ok "Usuário $SERVICE_USER criado (sem senha de login direto)"
        ok "Para acessar: sudo su - $SERVICE_USER"
    else
        ok "Usuário $SERVICE_USER já existe"
    fi
}

create_install_dir() {
    mkdir -p /opt/sites
    mkdir -p "$INSTALL_DIR"
    chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
    ok "Diretório $INSTALL_DIR pronto"
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
    local overwrite_env=false local_db_pass local_db_admin_pass local_db_user_admin_pass answer

    if [[ -f "$INSTALL_DIR/.env" && "$INSTALL_MODE" != update ]]; then
        printf '.env já existe. Sobrescrever preservando a SECRET_KEY? [s/N] '
        read -r answer </dev/tty
        if [[ "${answer,,}" != s ]]; then
            warn ".env existente mantido"
            return
        fi
        overwrite_env=true
    fi

    if [[ ! -f "$INSTALL_DIR/.env" || "$INSTALL_MODE" != update ]]; then
        prompt_database_password "$DB_APP_USER" "Senha da conta de operação do portal: "
        local_db_pass="$REPLY"
        prompt_database_password "$DB_ADMIN_USER" "Senha da conta administrativa do banco: "
        local_db_admin_pass="$REPLY"
        prompt_database_password "$DB_USER_ADMIN" "Senha da conta de gestão de usuários: "
        local_db_user_admin_pass="$REPLY"
    fi

    runuser -u "$SERVICE_USER" -- env \
        INSTALL_DIR="$INSTALL_DIR" \
        INSTALL_MODE="$INSTALL_MODE" \
        OVERWRITE_ENV="$overwrite_env" \
        LOCAL_DB_PASS_INPUT="${local_db_pass:-}" \
        LOCAL_DB_ADMIN_PASS_INPUT="${local_db_admin_pass:-}" \
        LOCAL_DB_USER_ADMIN_PASS_INPUT="${local_db_user_admin_pass:-}" \
        "$INSTALL_DIR/venv/bin/python" <<'PY'
import os, re, secrets
from pathlib import Path
from dotenv import dotenv_values

base = Path(os.environ['INSTALL_DIR']); target = base / '.env'
if target.is_symlink(): raise SystemExit('ERRO: .env não pode ser link simbólico')

def quote(value):
    if any(c in value for c in '\n\r\0'): raise SystemExit('ERRO: valor multilinha/NUL não permitido')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'

old_key = str(dotenv_values(target, interpolate=False).get('SECRET_KEY') or '') if target.exists() else ''
if target.exists() and (not old_key or old_key == 'GERAR_COM_COMANDO_ACIMA'):
    raise SystemExit('ERRO: restaure a SECRET_KEY original no .env existente antes de continuar')
if target.exists() and os.environ['INSTALL_MODE'] == 'update':
    print('✓ .env preservado integralmente no update, incluindo SECRET_KEY'); raise SystemExit
overwrite = os.environ['OVERWRITE_ENV'] == 'true'
text = (base / '.env.example').read_text(encoding='utf-8')
values = {'LOCAL_DB_PASS': os.environ['LOCAL_DB_PASS_INPUT'],
          'LOCAL_DB_ADMIN_PASS': os.environ['LOCAL_DB_ADMIN_PASS_INPUT'],
          'LOCAL_DB_USER_ADMIN_PASS': os.environ['LOCAL_DB_USER_ADMIN_PASS_INPUT'],
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
    local admin_username admin_password

    if [[ ! -f "$INSTALL_DIR/local.env" ]]; then
        printf "\nUsuário administrador do portal [admin]: "
        read -r admin_username </dev/tty
        admin_username="${admin_username:-admin}"
        if ! [[ "$admin_username" =~ ^[A-Za-z0-9_.-]{3,64}$ ]] || [[ "${admin_username,,}" == scheduler ]]; then
            fail "Usuário administrador do portal inválido."
        fi
        prompt_password "Senha do administrador do portal: "
        admin_password="$REPLY"
    fi

    runuser -u "$SERVICE_USER" -- env \
        INSTALL_DIR="$INSTALL_DIR" \
        ADMIN_USERNAME_INPUT="${admin_username:-}" \
        ADMIN_PASSWORD_INPUT="${admin_password:-}" \
        "$PYTHON_BIN" <<'PY'
import os, re
from pathlib import Path
base = Path(os.environ['INSTALL_DIR']); target = base / 'local.env'
if target.is_symlink(): raise SystemExit('ERRO: local.env não pode ser link simbólico')
if target.exists():
    print('✓ local.env já existe — mantendo')
else:
    user = os.environ['ADMIN_USERNAME_INPUT']
    password = os.environ['ADMIN_PASSWORD_INPUT']
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
    printf 'Senha root do MariaDB (Enter para unix_socket): '; read -r -s password </dev/tty; printf '\n'
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
    if len(value) < 8 or any(c in value for c in '\n\r\0'): raise SystemExit('ERRO: credencial inválida em .env: ' + key)
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
    local source="$INSTALL_DIR/docs/nginx.example.conf" target="$NGINX_CONF" replace=0 answer
    if [[ ! -f "$target" ]]; then
        replace=1
    elif [[ "$INSTALL_MODE" == update ]]; then
        warn "Nginx já configurado — mantendo $target"
    else
        printf 'Configuração Nginx existente. Sobrescrever após backup? [s/N] '; read -r answer </dev/tty
        [[ "${answer,,}" == s ]] && replace=1 || warn "Nginx já configurado — mantendo"
    fi
    if (( replace )); then
        [[ ! -f "$target" ]] || cp -a "$target" "${target}.bak.$(date +%Y%m%d%H%M%S)"
        install -o root -g root -m 0644 "$source" "$target"; ok "Configuração Nginx instalada"
    fi
    if [[ "$FAMILY" == debian ]]; then
        ln -sf "$NGINX_CONF" "$NGINX_CONF_ENABLED/arcreports"
        rm -f /etc/nginx/sites-enabled/default
    fi
    nginx -t || fail "nginx -t falhou. Revise $target antes de recarregar."
    systemctl reload nginx; ok "Nginx validado e recarregado"
}

# --- Controles de segurança específicos da distribuição ---
configure_platform_security() {
    if [[ "$FAMILY" == rhel ]]; then
        if command -v getenforce >/dev/null 2>&1 && [[ "$(getenforce)" != Disabled ]]; then
            SELINUX_ACTIVE=true
            warn "SELinux ativo — configurando portas..."
            if command -v semanage >/dev/null 2>&1; then
                semanage port -a -t http_port_t -p tcp 8000 2>/dev/null || \
                    semanage port -m -t http_port_t -p tcp 8000 2>/dev/null || \
                    warn "SELinux: configure manualmente a porta 8000 se necessário."
            else
                warn "SELinux: semanage indisponível; configure manualmente a porta 8000 se necessário."
            fi
            setsebool -P httpd_can_network_connect 1 2>/dev/null || \
                warn "SELinux: httpd_can_network_connect não configurado automaticamente."
            chcon -R -t httpd_sys_content_t "$INSTALL_DIR" 2>/dev/null || \
                warn "SELinux: contexto do diretório não configurado automaticamente."
        fi
    elif command -v aa-status >/dev/null 2>&1; then
        warn "AppArmor detectado — monitorar mysqldump"
        warn "Se mysqldump falhar, verifique os perfis AppArmor ativos."
    fi
}

warn_firewall() {
    if [[ "$FAMILY" == rhel ]] && systemctl is-active --quiet firewalld 2>/dev/null; then
        warn "Firewall ativo — libere a porta 80 manualmente"
        warn "Para liberar HTTP: firewall-cmd --permanent --add-service=http; firewall-cmd --reload"
    elif [[ "$FAMILY" == debian ]] && command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
        warn "Firewall ativo — libere a porta 80 manualmente"
        warn "Para liberar HTTP: ufw allow http"
    fi
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
    local LOCAL_IP ADMIN_USERNAME

    # Obter IP da máquina automaticamente
    LOCAL_IP=$(hostname -I | awk '{print $1}')
    ADMIN_USERNAME=$(runuser -u "$SERVICE_USER" -- \
        "$INSTALL_DIR/venv/bin/python" -c \
        'import sys; from dotenv import dotenv_values; print(dotenv_values(sys.argv[1], interpolate=False).get("ADMIN_USERNAME", "admin"))' \
        "$INSTALL_DIR/local.env")

    printf "\n"
    printf "╔══════════════════════════════════════╗\n"
    printf "║   ArcReports instalado com sucesso!  ║\n"
    printf "╚══════════════════════════════════════╝\n"
    printf "\n"
    printf "  Acesso ao portal:\n"
    printf "  → http://localhost\n"
    printf "  → http://127.0.0.1\n"
    printf "  → http://%s\n" "$LOCAL_IP"
    printf "\n"
    printf "  Credenciais de acesso:\n"
    printf "  Usuário: %s\n" "$ADMIN_USERNAME"
    printf "  Senha:   (a que você definiu)\n"
    printf "\n"
    printf "  Diretório: %s\n" "$INSTALL_DIR"
    printf "  Logs:      %s/logs/\n" "$INSTALL_DIR"
    printf "  Snapshots: %s/snapshots/\n" "$INSTALL_DIR"
    printf "\n"
    printf "  Usuário do serviço: ia-dev\n"
    printf "  Acesso manual: sudo su - ia-dev\n"
    printf "\n"
    printf "  Para atualizar:\n"
    printf "  git config --global --add\n"
    printf "    safe.directory %s\n" "$INSTALL_DIR"
    printf "  cd %s && git pull\n" "$INSTALL_DIR"
    printf "  sudo bash install.sh\n"
    printf "\n"
    printf "  Serviço:\n"
    printf "  systemctl status arcreports\n"
    printf "  systemctl restart arcreports\n"
    printf "  journalctl -u arcreports -f\n"
    printf "\n"
}

# --- Fluxo principal ---
main() {
    check_root
    detect_distribution
    [[ "$FAMILY" != debian ]] || apt_update
    detect_install_mode
    check_or_install_python
    check_or_install_mariadb
    check_or_install_nginx
    check_or_install_git
    check_or_install_curl
    check_or_install_mysqldump
    create_service_user
    create_install_dir
    git config --global \
        --add safe.directory "$INSTALL_DIR" \
        2>/dev/null || true
    configure_platform_security
    setup_venv
    setup_env
    setup_local_env
    setup_mariadb
    setup_alembic
    setup_nginx
    setup_systemd
    verify_install
    warn_firewall
    print_summary
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
