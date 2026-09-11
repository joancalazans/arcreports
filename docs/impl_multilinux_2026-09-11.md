# Install.sh multi-distro — ArcReports

Data: 2026-09-11

## Distribuições suportadas

- RHEL, Rocky Linux e AlmaLinux 9 ou superior.
- Fedora 38 ou superior.
- Ubuntu 22.04 ou superior, incluindo Ubuntu 24.04.
- Debian 11 e 12.

A distribuição é identificada por `/etc/os-release`. Versões abaixo dos mínimos e IDs não reconhecidos encerram a instalação com uma mensagem que relaciona as plataformas suportadas.

## Diferenças por família

- Família RHEL: usa `dnf` e instala `python3.9`, `python3-pip`, `mariadb-server`, `mariadb`, `nginx`, `curl` e `git` conforme a necessidade.
- Família Debian: executa `apt-get update -qq` antes das instalações e usa `apt-get` para `python3.9`, `python3-pip`, `python3.9-venv`, `python3-venv`, `mariadb-server`, `nginx`, `curl` e `git`.
- O serviço MariaDB usa inicialmente o nome `mariadb`; em Debian/Ubuntu, o instalador usa `mysql` como fallback quando a unidade `mariadb` não é encontrada.
- O firewall não é modificado. Quando `firewalld` ou `ufw` está ativo, o resumo apresenta somente o comando que o administrador pode executar manualmente para liberar HTTP.

## SELinux e AppArmor

Na família RHEL, o instalador verifica se o SELinux está habilitado. Quando ativo, tenta registrar a porta TCP 8000 como `http_port_t`, habilitar `httpd_can_network_connect` e aplicar `httpd_sys_content_t` ao diretório do ArcReports. Falhas nessas ações geram avisos com orientação para configuração manual.

Na família Debian, nenhuma alteração de SELinux é feita. A presença de `aa-status` gera apenas um aviso para monitorar os perfis AppArmor caso o `mysqldump` falhe.

## Nginx por família

- Família RHEL: grava `/etc/nginx/conf.d/arcreports.conf`, sem link simbólico.
- Família Debian: grava `/etc/nginx/sites-available/arcreports`, cria o link `/etc/nginx/sites-enabled/arcreports` e remove o link/configuração padrão habilitado em `/etc/nginx/sites-enabled/default`.

O fluxo existente de backup, teste com `nginx -t` e recarga do serviço foi preservado.

## Python por família

- Família RHEL: prefere `python3.9` e aceita `python3` quando sua versão já é 3.9 ou superior. O comando selecionado é usado para criar o ambiente virtual.
- Família Debian: exige `python3.9`, instala os pacotes de `venv` e confirma `python3.9 --version` antes de prosseguir.
- Ubuntu: se `python3.9` não estiver disponível nos repositórios configurados, instala `software-properties-common`, habilita `ppa:deadsnakes/ppa`, atualiza os índices e instala Python 3.9 com suporte a `venv`. Isso contempla o Ubuntu 24.04, cujo Python padrão é mais recente.

## Resultado

- `bash -n install.sh`: **ok** (código de saída 0, sem erros de sintaxe).
- O instalador não foi executado.
- Nenhum arquivo em `app/` foi alterado.
