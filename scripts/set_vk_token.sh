#!/usr/bin/env bash
set -euo pipefail

project_dir="${1:-/opt/itvibe-content-editor}"
env_file="${project_dir}/.env"

if [[ ! -f "${env_file}" ]]; then
  echo "Не найден ${env_file}" >&2
  exit 1
fi

read -r -p "ID сообщества VK (только цифры, без минуса): " vk_group_id
if [[ ! "${vk_group_id}" =~ ^[1-9][0-9]*$ ]]; then
  echo "ID сообщества должен быть положительным числом." >&2
  exit 1
fi

read -r -s -p "Вставьте ключ доступа сообщества VK: " vk_token
echo
if [[ -z "${vk_token}" || "${vk_token}" == *$'\n'* ]]; then
  echo "Ключ не может быть пустым." >&2
  exit 1
fi

umask 077
temp_file="$(mktemp "${project_dir}/.env.vk.XXXXXX")"
cleanup() {
  rm -f "${temp_file}"
  unset vk_token
}
trap cleanup EXIT

while IFS= read -r line || [[ -n "${line}" ]]; do
  case "${line}" in
    VK_COMMUNITY_ACCESS_TOKEN=*|VK_COMMUNITY_GROUP_ID=*|VK_API_VERSION=*) ;;
    *) printf '%s\n' "${line}" >> "${temp_file}" ;;
  esac
done < "${env_file}"
printf 'VK_COMMUNITY_ACCESS_TOKEN=%s\n' "${vk_token}" >> "${temp_file}"
printf 'VK_COMMUNITY_GROUP_ID=%s\n' "${vk_group_id}" >> "${temp_file}"
printf 'VK_API_VERSION=5.199\n' >> "${temp_file}"
chmod 600 "${temp_file}"
mv "${temp_file}" "${env_file}"
trap - EXIT
unset vk_token

echo "Ключ VK сохранён без вывода на экран."
echo "Перезапустите сервис: docker compose up -d --no-deps --force-recreate editor"
