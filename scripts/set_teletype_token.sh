#!/usr/bin/env bash
set -euo pipefail

project_dir="${1:-/opt/itvibe-content-editor}"
env_file="${project_dir}/.env"

if [[ ! -f "${env_file}" ]]; then
  echo "Не найден ${env_file}" >&2
  exit 1
fi

read -r -s -p "Вставьте токен аккаунта Teletype: " teletype_token
echo
if [[ -z "${teletype_token}" || "${teletype_token}" == *$'\n'* ]]; then
  echo "Токен не может быть пустым." >&2
  exit 1
fi

umask 077
temp_file="$(mktemp "${project_dir}/.env.teletype.XXXXXX")"
cleanup() {
  rm -f "${temp_file}"
  unset teletype_token
}
trap cleanup EXIT

while IFS= read -r line || [[ -n "${line}" ]]; do
  case "${line}" in
    TELETYPE_ACCESS_TOKEN=*|TELETYPE_CLIENT_VERSION=*) ;;
    *) printf '%s\n' "${line}" >> "${temp_file}" ;;
  esac
done < "${env_file}"
printf 'TELETYPE_ACCESS_TOKEN=%s\n' "${teletype_token}" >> "${temp_file}"
printf 'TELETYPE_CLIENT_VERSION=4.41.26\n' >> "${temp_file}"
chmod 600 "${temp_file}"
mv "${temp_file}" "${env_file}"
trap - EXIT
unset teletype_token

echo "Токен Teletype сохранён без вывода на экран."
echo "Перезапустите сервис: docker compose up -d --no-deps --force-recreate editor"
