#!/usr/bin/env bash
# Teste negativo das regras do .gitleaks.toml (job gitleaks do CI; roda tambem
# na maquina local, com o gitleaks do PATH): a URL do PostgreSQL (postgresql://,
# postgres:// e com driver) com a senha que o make deploy gera reprova num
# manifesto, a URL do AMQP (amqp:// e amqps://) reprova em qualquer arquivo, a
# forma dos manifestos ($(POSTGRES_APP_PASSWORD)) passa, e senha apagada no
# commit seguinte ainda reprova no historico. Sem ele, uma regra que nao casa
# mais nada passaria verde.
set -euo pipefail

raiz=$(cd "$(dirname "$0")/../.." && pwd)
dir=$(mktemp -d "${TMPDIR:-/tmp}/testa-gitleaks.XXXXXX")
trap 'rm -rf "$dir"' EXIT
mkdir -p "$dir/k8s" "$dir/scripts" "$dir/tests/unitarios/compartilhado/mensageria"

# postgres <senha> [esquema] e amqp <senha> [esquema]: as linhas que os manifestos montam.
postgres() { printf 'value: %s://os_app:%s@os-postgres.pytstop-os.svc.cluster.local:5432/os\n' "${2:-postgresql}" "$1"; }
amqp() { printf 'RABBITMQ_URL: "%s://os:%s@rabbitmq.pytstop-plataforma.svc.cluster.local:5672/%%2F"\n' "${2:-amqp}" "$1"; }
postgres "$(openssl rand -hex 24)" > "$dir/k8s/postgres-senha-gerada.yaml"
postgres "$(openssl rand -hex 24)" postgres > "$dir/k8s/postgres-curto-senha-gerada.yaml"
postgres "$(openssl rand -hex 24)" postgresql+psycopg2 > "$dir/k8s/postgres-driver-senha-gerada.yaml"
amqp "$(openssl rand -hex 24)" > "$dir/k8s/amqp-senha-gerada.yaml"
amqp "$(openssl rand -hex 24)" amqps > "$dir/k8s/amqps-senha-gerada.yaml"
# A regra do AMQP vale em qualquer arquivo; a do PostgreSQL, so em k8s/ (os testes
# do servico montam URLs de banco com senha de mentira).
amqp "$(openssl rand -hex 24)" > "$dir/scripts/amqp-senha-gerada.sh"
# shellcheck disable=SC2016 # o $(...) e literal: a expansao e do kubelet
postgres '$(POSTGRES_APP_PASSWORD)' > "$dir/k8s/postgres-expansao-do-pod.yaml"
postgres 's3nh4-de-mentira' > "$dir/tests/postgres-de-mentira.py"
{ amqp pytstop-os-demo-2026 | sed 's/$/  # gitleaks:allow/'; } > "$dir/compose-com-marcador.yml"
# A senha de mentira do teste do boot dos processos passa so naquele arquivo, e
# so ela: a gerada no mesmo arquivo, e a de mentira em outro, reprovam.
teste="$dir/tests/unitarios/compartilhado/mensageria/test_processo.py"
amqp outra-senha > "$teste"
amqp "$(openssl rand -hex 24)" >> "$teste"
amqp outra-senha > "$dir/tests/outra-senha.py"

(cd "$dir" && gitleaks dir . --config "$raiz/.gitleaks.toml" --no-banner --redact --exit-code 0 \
  --report-format json --report-path "$dir/achados.json")
achados=$(jq -r '[.[] | .RuleID + " " + .File] | sort | join(", ")' "$dir/achados.json")
esperado="pytstop-url-amqp-com-senha k8s/amqp-senha-gerada.yaml, pytstop-url-amqp-com-senha k8s/amqps-senha-gerada.yaml, pytstop-url-amqp-com-senha scripts/amqp-senha-gerada.sh, pytstop-url-amqp-com-senha tests/outra-senha.py, pytstop-url-amqp-com-senha tests/unitarios/compartilhado/mensageria/test_processo.py, pytstop-url-postgres-com-senha k8s/postgres-curto-senha-gerada.yaml, pytstop-url-postgres-com-senha k8s/postgres-driver-senha-gerada.yaml, pytstop-url-postgres-com-senha k8s/postgres-senha-gerada.yaml"
if [ "$achados" != "$esperado" ]; then
  echo "gitleaks found [$achados], expected [$esperado]" >&2
  exit 1
fi
# Historico: senha commitada e apagada no commit seguinte continua reprovando
# com o mesmo escopo do CI (--log-opts="HEAD").
repo="$dir/historico"
git init -q "$repo"
git -C "$repo" -c user.name=teste -c user.email=teste@example.invalid commit -q --allow-empty -m inicio
mkdir -p "$repo/k8s"
postgres "$(openssl rand -hex 24)" > "$repo/k8s/vazou.yaml"
git -C "$repo" add k8s/vazou.yaml
git -C "$repo" -c user.name=teste -c user.email=teste@example.invalid commit -q -m vaza
git -C "$repo" rm -q k8s/vazou.yaml
git -C "$repo" -c user.name=teste -c user.email=teste@example.invalid commit -q -m apaga
if gitleaks git "$repo" --log-opts="HEAD" --config "$raiz/.gitleaks.toml" --no-banner --redact >/dev/null 2>&1; then
  echo "gitleaks passed a password committed and deleted in the next commit" >&2
  exit 1
fi
echo "gitleaks rules hold: generated passwords flagged (postgresql, postgres, driver and amqp, amqps), the pod expansion, the marked demo password and the test password allowed only where the config says, deleted password still flagged in history"
