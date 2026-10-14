#!/usr/bin/env bash
# A excecao do trivy (KSV-0109, k8s/trivy-ignore.rego) vale para o ConfigMap do
# papeis.sql inteiro, e nem ela nem o gitleaks pegam um PASSWORD '<hex>' escrito
# no script. Este script estreita a excecao ao que ela precisa: todo PASSWORD do
# script recebe uma variavel do psql (:'senha_...') que o proprio script le do
# ambiente com \getenv; valor literal, ou variavel que ninguem le, reprova.
# Roda no make manifests, na raiz do repositorio.
set -euo pipefail

arquivo="${1:-k8s/base/papeis.sql}"
# Sem os comentarios e numa linha so: o valor pode vir na linha seguinte.
codigo=$(sed 's/--.*$//' "$arquivo" | tr '\n' ' ')
# PASSWORD como palavra do SQL, ate o fim do comando: POSTGRES_OWNER_PASSWORD (o
# \getenv) nao conta.
senhas=$(printf '%s' "$codigo" | grep -oiE "(^|[^A-Za-z0-9_])password[[:space:]]+[^;]*" \
  | sed -E 's/^[^A-Za-z0-9_]//' || true)
if [ -z "$senhas" ]; then
  echo "$arquivo: nenhum PASSWORD encontrado" >&2
  exit 1
fi
literais=$(printf '%s\n' "$senhas" | grep -viE "^password[[:space:]]+:'[a-z_]+'[[:space:]]*$" || true)
if [ -n "$literais" ]; then
  echo "$arquivo: PASSWORD sem a variavel do psql (:'senha_...'):" >&2
  echo "$literais" >&2
  exit 1
fi
for variavel in $(printf '%s\n' "$senhas" | sed -E "s/^.*:'([a-z_]+)'[[:space:]]*$/\1/" | sort -u); do
  if ! grep -qE "^\\\\getenv[[:space:]]+${variavel}[[:space:]]+[A-Z_]+[[:space:]]*$" "$arquivo"; then
    echo "$arquivo: a variavel ${variavel} nao e lida com \\getenv" >&2
    exit 1
  fi
done
echo "$arquivo: todo PASSWORD vem de uma variavel lida do ambiente com \\getenv"
