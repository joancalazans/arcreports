#!/bin/bash
# ArcReports — Script de instalação híbrida
# Compatível com RHEL 9.x. Uso: bash install.sh [--prepare|--verify|--help]
# Executar como ia-dev. Não instala pacotes do SO nem altera Linux/Nginx/systemd.
# O operador prepara a infraestrutura e ativa o serviço pelas instruções em docs/.
set -euo pipefail
umask 077
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'
ok()   { printf "${GREEN}✓${NC} %s\n" "$1"; }
warn() { printf "${YELLOW}⚠${NC} %s\n" "$1"; }
fail() { printf "${RED}✗${NC} %s\n" "$1" >&2; exit 1; }

INSTALL_DIR="/opt/sites/glpi-portal"
SERVICE_USER="ia-dev"
DB_NAME="reports"
DB_APP_USER="glpi_portal"
DB_ADMIN_USER="portal_db_admin"
DB_USER_ADMIN="portal_db_user"
export INSTALL_DIR DB_NAME DB_APP_USER DB_ADMIN_USER DB_USER_ADMIN

check_version() {
    local name="$1" min="$2" found="$3" version
    # mysql anuncia Ver 15.1 (cliente); comparar Distrib 10.x (MariaDB).
    if [[ "$name" == MariaDB ]]; then
        [[ "$found" == *MariaDB* ]] || fail "Esperado MariaDB; encontrado: $found"
        [[ "$found" =~ Distrib[[:space:]]+([0-9]+\.[0-9]+(\.[0-9]+)?) ]] || fail "Versão MariaDB não reconhecida: $found"
    else
        [[ "$found" =~ ([0-9]+\.[0-9]+(\.[0-9]+)?) ]] || fail "Versão $name não reconhecida: $found"
    fi
    version="${BASH_REMATCH[1]}"
    python3 - "$min" "$version" <<'PY' || fail "$name $version insuficiente; mínimo $min. Solicite a preparação ao operador."
import sys
def parts(value):
    values = tuple(int(p) for p in value.split('.'))
    return values + (0,) * (3 - len(values))
sys.exit(0 if parts(sys.argv[2]) >= parts(sys.argv[1]) else 1)
PY
    ok "$name $version (mínimo $min)"
}

usage() {
    cat <<'HELP'
ArcReports v0.1.0 — instalação híbrida, sem execução privilegiada
  bash install.sh             Prepara venv, configurações e banco local.
  bash install.sh --prepare   Mesmo comportamento padrão.
  bash install.sh --verify    Verifica HTTP após ativação manual do serviço.

Pré-requisitos preparados pelo operador:
  ia-dev; diretório /opt/sites/glpi-portal gravável por ia-dev; código revisado
  copiado/clonado nesse diretório; Python >=3.9, MariaDB >=10.5, Nginx >=1.18;
  mysql, mysqldump, curl e venv disponíveis; MariaDB local em execução.
  Em atualização: backup externo, serviço parado e código da versão já copiado
  preservando .env, local.env, ldap.env, logs/, snapshots/ e static/uploads/.

Banco novo: fornecer INSTALL_DB_CNF=/caminho/privado/bootstrap.cnf, modo 0600,
com seção [client], user e password de uma conta DBA não root já provisionada
no MariaDB LOCAL. Pode conter host=localhost, port=3306 ou socket local.
A conta precisa criar banco/contas e conceder os grants documentados.
Sem esse arquivo, usa LOCAL_DB_ADMIN_USER/PASS de um .env existente.
Não solicita a senha de uma quarta conta e não configura autenticação do servidor.

Offline: PIP_NO_INDEX=1 PIP_FIND_LINKS=/caminho/wheels bash install.sh
O diretório de wheels deve conter todas as dependências para Python/plataforma.

O script não clona uma URL implicitamente, não faz git pull, não altera contas
Linux nem copia arquivos para /etc. Exporta as etapas operacionais em docs/.
HELP
}

verify_http() {
    local code
    command -v curl >/dev/null || fail "curl ausente."
    # /health não existe no core v0.1.0; não tratar um 404 como sucesso.
    code=$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
        --connect-timeout 3 --max-time 15 http://127.0.0.1:8000/health) || fail "Serviço HTTP indisponível."
    if [[ "$code" == 404 ]]; then
        warn "/health ausente na v0.1.0; verificando /login (não é diagnóstico completo do banco)."
        code=$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
            --connect-timeout 3 --max-time 15 http://127.0.0.1:8000/login) || fail "Falha ao acessar /login."
    fi
    [[ "$code" == 200 ]] || fail "Verificação HTTP retornou $code."
    ok "Resposta HTTP 200. Confirme login, conector, relatório e snapshot pela interface."
    printf '\n==================================\n ArcReports instalado com sucesso\n==================================\n'
    printf ' URL: http://SEU_IP (ou hostname configurado no Nginx)\n Usuário: ADMIN_USERNAME configurado\n Logs: %s/logs/\n Snapshots: %s/snapshots/\n==================================\n' "$INSTALL_DIR" "$INSTALL_DIR"
}

main() {
    case "${1:---prepare}" in
        --help|-h) usage; return ;;
        --prepare|--verify) ;;
        *) usage; fail "Opção desconhecida." ;;
    esac
    [[ $# -le 1 ]] || fail "Argumentos excedentes."
    [[ "$EUID" -ne 0 ]] || fail "Nunca executar como root. Use a sessão de ia-dev."
    [[ "$(id -un)" == "$SERVICE_USER" ]] || fail "Execute como $SERVICE_USER."
    [[ "${1:---prepare}" != --verify ]] || { verify_http; return; }

    # 1–3. Infraestrutura e código são preparados pelo operador, sem alterar SO.
    [[ -d "$INSTALL_DIR" && -w "$INSTALL_DIR" ]] || fail "Operador deve criar $INSTALL_DIR para $SERVICE_USER."
    cd "$INSTALL_DIR"
    [[ "$(readlink -f "${BASH_SOURCE[0]}")" == "$INSTALL_DIR/install.sh" ]] || fail "Copie o código revisado para $INSTALL_DIR antes de instalar."
    for file in requirements.txt app/config.py app/models.py alembic.ini .env.example local.env.example ldap.env.example docs/nginx.example.conf docs/arcreports.service.example; do
        [[ -r "$file" ]] || fail "Arquivo necessário ausente: $file"
    done
    for tool in python3 mysql mysqldump nginx curl systemctl; do
        command -v "$tool" >/dev/null || fail "Dependência ausente: $tool. Solicite ao operador."
    done
    check_version "Python" "3.9" "$(python3 --version 2>&1)"
    check_version "MariaDB" "10.5" "$(mysql --version 2>&1)"
    check_version "Nginx" "1.18" "$(nginx -v 2>&1)"
    # Evita instalar dependências/migrar com o serviço existente em execução.
    if systemctl is-active --quiet arcreports; then
        fail "Serviço ativo: operador deve fazer backup e parar arcreports antes da preparação."
    fi
    if systemctl cat arcreports >/dev/null 2>&1; then
        warn "Serviço existente: preservar customizações e atualizar a unidade pelo procedimento em docs/."
    fi

    # 4–5. Não usa pip install .: o empacotamento atual não substitui requirements.
    [[ -x venv/bin/python ]] || python3 -m venv venv
    check_version "Python do venv" "3.9" "$(venv/bin/python --version 2>&1)"
    source venv/bin/activate
    python -m pip install -r requirements.txt
    python -m pip check
    mkdir -p logs snapshots static/uploads/appearance

    # 6–9. Prompts, preservação de segredos, grants locais e baseline.
    # Python lê dotenv como dados; nunca source/eval de arquivos de credenciais.
    python - <<'PY'
import configparser
import getpass
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import tempfile
from datetime import datetime

import pymysql
from dotenv import dotenv_values

BASE = Path(os.environ['INSTALL_DIR'])
os.chdir(BASE)

def stop(message):
    raise SystemExit('ERRO: ' + message)

def read_env(path):
    if path.is_symlink():
        stop('Não usar symlink para arquivo de credenciais: ' + path.name)
    return dict(dotenv_values(path, interpolate=False)) if path.exists() else {}

def ask(label, secret=False):
    value = getpass.getpass(label + ': ') if secret else input(label + ': ').strip()
    if not value or any(c in value for c in ('\n', '\r', '\x00', '${')):
        stop('Valor vazio, multilinha, NUL ou interpolação ${...} não permitido.')
    if secret and (len(value) < 16 or value == 'SENHA_FORTE_AQUI'):
        stop('Use senha própria com pelo menos 16 caracteres.')
    return value

def quoted(value):
    return "'" + value.replace('\\', '\\\\').replace("'", "\\'") + "'"

def write_env(path, template, values):
    # Preserva comentários e campos desconhecidos; troca somente campos pedidos.
    content = template
    for key, value in values.items():
        line = key + '=' + quoted(value)
        pattern = r'(?m)^' + re.escape(key) + r'=.*$'
        content = re.sub(pattern, lambda m: line, content) if re.search(pattern, content) else content + '\n' + line + '\n'
    if path.exists():
        backup = path.with_name(path.name + '.bak.' + datetime.now().strftime('%Y%m%d%H%M%S%f'))
        with backup.open('x', encoding='utf-8') as out:
            out.write(path.read_text(encoding='utf-8'))
        backup.chmod(0o600)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=BASE, delete=False) as out:
        out.write(content)
        temp = Path(out.name)
    temp.chmod(0o600)
    temp.replace(path)

path = BASE / '.env'
existing = path.exists()
values = read_env(path)
if existing:
    key = values.get('SECRET_KEY') or values.get('APP_SECRET_KEY') or ''
    if len(key) < 32 or key == 'GERAR_COM_COMANDO_ACIMA':
        stop('.env existente sem chave válida. Recupere a chave original; não será gerada outra.')
    # Pergunta obrigatória antes de escrever; recusar preserva arquivo integralmente.
    update = input('.env já existe. Completar chaves ausentes, preservando TODOS os valores e SECRET_KEY? [s/N] ').lower() == 's'
    if update:
        defaults = read_env(BASE / '.env.example')
        defaults.pop('SECRET_KEY', None)
        missing = {k: v or '' for k, v in defaults.items() if k not in values}
        for k in ('LOCAL_DB_PASS', 'LOCAL_DB_ADMIN_PASS', 'LOCAL_DB_USER_ADMIN_PASS'):
            if k in missing:
                missing[k] = ask(k, True)
        write_env(path, path.read_text(encoding='utf-8'), missing)
        values.update(missing)
else:
    values = read_env(BASE / '.env.example')
    for k, label in (('LOCAL_DB_PASS', 'Senha do glpi_portal'), ('LOCAL_DB_ADMIN_PASS', 'Senha do portal_db_admin'), ('LOCAL_DB_USER_ADMIN_PASS', 'Senha do portal_db_user')):
        values[k] = ask(label, True)
    values['SECRET_KEY'] = secrets.token_hex(32)
    write_env(path, (BASE / '.env.example').read_text(encoding='utf-8'), values)

for k, expected in {'LOCAL_DB_HOST': 'localhost', 'LOCAL_DB_PORT': '3306', 'LOCAL_DB_NAME': 'reports', 'LOCAL_DB_USER': 'glpi_portal', 'LOCAL_DB_ADMIN_USER': 'portal_db_admin', 'LOCAL_DB_USER_ADMIN_USER': 'portal_db_user'}.items():
    if values.get(k) != expected:
        stop(k + ' difere do perfil fixo local suportado pelo instalador; preservar e revisar manualmente.')
for k, v in values.items():
    if v and '${' in v:
        stop('Interpolação em ' + k + ': revisar manualmente para não mudar credenciais.')
for k in ('LOCAL_DB_PASS', 'LOCAL_DB_ADMIN_PASS', 'LOCAL_DB_USER_ADMIN_PASS'):
    if not values.get(k) or values[k] == 'SENHA_FORTE_AQUI':
        stop('Preencha a credencial ' + k + '.')
# Impede overrides herdados de redirecionarem Alembic/app para outra origem.
for k in values:
    os.environ[k] = values[k] or ''

local_path = BASE / 'local.env'
local = read_env(local_path)
if not local_path.exists():
    local = {'ADMIN_USERNAME': ask('ADMIN_USERNAME'), 'ADMIN_PASSWORD': ask('ADMIN_PASSWORD', True)}
    if local['ADMIN_USERNAME'].lower() == 'scheduler':
        stop('scheduler é um nome reservado.')
    write_env(local_path, (BASE / 'local.env.example').read_text(encoding='utf-8'), local)
if not local.get('ADMIN_USERNAME') or not local.get('ADMIN_PASSWORD') or local.get('ADMIN_PASSWORD') == 'SENHA_FORTE_AQUI':
    stop('local.env existente incompleto; corrigir manualmente sem redefinir contas existentes.')
ldap_path = BASE / 'ldap.env'
read_env(ldap_path)
if not ldap_path.exists():
    with ldap_path.open('x', encoding='utf-8') as out:
        out.write((BASE / 'ldap.env.example').read_text(encoding='utf-8'))
for private in (path, local_path, ldap_path):
    private.chmod(0o600)

# Credencial DBA pré-provisionada; nunca autenticar como root.
cnf = os.environ.get('INSTALL_DB_CNF')
params = dict(host='localhost', port=3306, user=values['LOCAL_DB_ADMIN_USER'], password=values['LOCAL_DB_ADMIN_PASS'])
if cnf:
    cnf_path = Path(cnf)
    if not cnf_path.is_file() or cnf_path.is_symlink() or cnf_path.stat().st_uid != os.getuid() or stat.S_IMODE(cnf_path.stat().st_mode) != 0o600:
        stop('INSTALL_DB_CNF deve pertencer a ia-dev, ser regular sem symlink e ter modo 0600.')
    parser = configparser.RawConfigParser()
    parser.read(cnf_path)
    section = parser['client']
    params = dict(host=section.get('host', 'localhost'), port=int(section.get('port', '3306')), user=section['user'], password=section['password'])
    if section.get('socket'):
        params['unix_socket'] = section['socket']
if params['user'].lower() == 'root' or params['host'] not in ('localhost', '127.0.0.1') or params['port'] != 3306:
    stop('Bootstrap exige DBA não root no MariaDB local, porta 3306.')
try:
    conn = pymysql.connect(**params, charset='utf8mb4', autocommit=True, connect_timeout=5)
    with conn.cursor() as cur:
        cur.execute('SELECT CURRENT_USER(), VERSION()')
        account, version = cur.fetchone()
        if account.split('@', 1)[0].lower() == 'root':
            stop('Conta efetiva root não permitida.')
        numbers = re.match(r'(\d+)\.(\d+)', version)
        if 'MariaDB' not in version or not numbers or tuple(map(int, numbers.groups())) < (10, 5):
            stop('Servidor precisa ser MariaDB >=10.5; versão do cliente não basta.')
        cur.execute('SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=%s', ('reports',))
        if cur.fetchone() is None:
            cur.execute('CREATE DATABASE `reports` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci')
        # CREATE USER IF NOT EXISTS preserva senhas existentes; testar antes de grants.
        accounts = [('glpi_portal', values['LOCAL_DB_PASS']), ('portal_db_admin', values['LOCAL_DB_ADMIN_PASS']), ('portal_db_user', values['LOCAL_DB_USER_ADMIN_PASS'])]
        for user, password in accounts:
            cur.execute('CREATE USER IF NOT EXISTS %s@%s IDENTIFIED BY %s', (user, 'localhost', password))
            test = pymysql.connect(host='localhost', port=3306, user=user, password=password, connect_timeout=5)
            test.close()
        # Os grants são aditivos: nunca revoga privilégios preexistentes.
        cur.execute("GRANT SELECT,INSERT,UPDATE,DELETE,CREATE,DROP,INDEX,ALTER ON `reports`.* TO 'glpi_portal'@'localhost'")
        cur.execute("GRANT ALL PRIVILEGES ON *.* TO 'portal_db_admin'@'localhost' WITH GRANT OPTION")
        cur.execute("GRANT CREATE USER ON *.* TO 'portal_db_user'@'localhost'")
        cur.execute("GRANT SELECT ON `reports`.* TO 'portal_db_user'@'localhost' WITH GRANT OPTION")
    conn.close()
except pymysql.MySQLError as exc:
    stop('Falha no provisionamento local (código %s). Verifique INSTALL_DB_CNF, permissões e senhas existentes; não houve redefinição de senhas.' % exc.args[0])

# Baseline histórico NÃO cria tabelas. Não importar app.main nem iniciar scheduler.
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.migration import MigrationContext
from alembic.autogenerate import compare_metadata
from sqlalchemy import inspect
from app.models import Base
from app.database import local_engine
cfg = Config(str(BASE / 'alembic.ini'))
heads = tuple(ScriptDirectory.from_config(cfg).get_heads())
with local_engine.connect() as connection:
    tables = set(inspect(connection).get_table_names())
    revisions = tuple(MigrationContext.configure(connection).get_current_heads())
if not tables:
    if heads != ('74d249fd4a23',):
        stop('Instalação limpa de outra versão exige procedimento de bootstrap revisado.')
    Base.metadata.create_all(local_engine)
if not revisions:
    if heads != ('74d249fd4a23',):
        stop('Banco sem versão: não aplicar stamp head em uma revisão futura.')
    with local_engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={
            'compare_type': True,
            'include_object': lambda obj, name, kind, reflected, compare_to: kind != 'table' or name in Base.metadata.tables,
        })
        differences = compare_metadata(context, Base.metadata)
    if differences:
        stop('Schema diverge do ORM. Revisar workflow Alembic antes de registrar baseline; nenhum stamp executado.')
    # Equivale a alembic stamp head, SOMENTE após validar o schema da baseline.
    command.stamp(cfg, 'head')
elif set(revisions) != set(heads):
    stop('Migrações pendentes. Operador deve revisar e aplicar alembic upgrade head após backup; depois repetir install.sh. Nunca usar stamp para atualizar.')
command.check(cfg)
print('Configurações preservadas, contas locais validadas e schema alinhado.')
PY

    # 10–12. Apenas exporta instruções; nenhuma cópia para /etc ou systemctl mutante.
    cat > docs/install_operator_steps.txt <<'OPS'
ArcReports — etapas de infraestrutura MANUAIS (não executadas pelo instalador)
1. Preparar ia-dev e /opt/sites/glpi-portal com escrita somente para a conta do serviço.
2. Disponibilizar código revisado; em atualização, preservar segredos, uploads e backups.
3. Antes de atualizar, guardar dump externo de reports/réplicas, segredos e versão do código;
   parar o serviço pela operação autorizada. Depois executar bash install.sh como ia-dev.
4. Revisar docs/nginx.example.conf: hostname, TLS e virtual hosts existentes.
   Copiar para /etc/nginx/conf.d/arcreports.conf somente se não houver vhost do portal.
   Se houver (inclusive glpi-portal.conf), atualizar esse arquivo preservando customizações;
   não manter dois vhosts equivalentes. Validar com nginx -t antes de reload autorizado.
5. Revisar docs/arcreports.service.example. Se arcreports.service já existir,
   fazer backup e atualizar preservando customizações; caso contrário, instalar em
   /etc/systemd/system/arcreports.service. Não duplicar o processo/worker.
   A configuração .env é lida pelo aplicativo via python-dotenv.
6. Pelo procedimento autorizado da infraestrutura:
   systemctl daemon-reload
   systemctl enable arcreports
   systemctl start arcreports
   Validar/recarregar Nginx conforme o procedimento local.
7. Como ia-dev: bash install.sh --verify
   /health ausente na v0.1.0: o script verifica /login quando recebe 404.
8. Validar login, permissões, origem SELECT, execução, PDF/Excel e criação de snapshot.
O instalador não altera Linux, Nginx, systemd, firewall, SELinux ou autenticação MariaDB.
Não considerar preparação concluída como serviço instalado e validado.
OPS
    ok "Preparação concluída. Etapas manuais: $INSTALL_DIR/docs/install_operator_steps.txt"
    warn "Serviço não iniciado por este script. Após ativação pelo operador, execute bash install.sh --verify."
}

# Permite carregar somente funções para validar versões sem executar instalação.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
