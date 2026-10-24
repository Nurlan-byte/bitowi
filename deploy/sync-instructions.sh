#!/bin/bash
# Копия инструкций с сервера на Windows — шаг каждой выкладки (после git pull и перезапуска бота).
# Главная копия — на сервере (~/bitowi_instructions); на Windows — зеркало: удалённое на сервере
# удаляется и здесь. Запуск из Git Bash на Windows:
#   deploy/sync-instructions.sh [user@host] [папка на Windows]
# Сначала копия в <папка>.new и сверка sha256 каждого файла; рабочая папка заменяется,
# только если все хеши совпали.
set -euo pipefail

HOST=${1:-nurlan@192.168.0.21}
DEST=${2:-/c/Dev/claude/bitowi_instructions}
readonly SRC='~/bitowi_instructions'
stage="$DEST.new"
old="$DEST.old"
# временная копия не остаётся ни при какой ошибке
trap 'rm -rf "$stage"' EXIT

die() {
    echo "sync-instructions: $1" >&2
    exit 1
}

# одинаковый вид на обеих сторонах: «хеш  ./путь», сортировка по байтам
hashes() {
    find . -type f | LC_ALL=C sort | tr '\n' '\0' | xargs -0 -r sha256sum | sed 's/ \*\./  ./'
}

rm -rf "$stage"
mkdir -p "$stage"
ssh -o BatchMode=yes "$HOST" "cd $SRC && tar -cf - ." | tar -xf - -C "$stage" || die "копирование не удалось"

remote=$(ssh -o BatchMode=yes "$HOST" "cd $SRC && find . -type f | LC_ALL=C sort | tr '\n' '\0' | xargs -0 -r sha256sum")
local_=$(cd "$stage" && hashes)
if [[ "$remote" != "$local_" ]]; then
    diff <(echo "$remote") <(echo "$local_") >&2 || true
    die "хеши не совпали — $DEST не изменена"
fi

rm -rf "$old"
[[ -d "$DEST" ]] && mv "$DEST" "$old"
mv "$stage" "$DEST"
rm -rf "$old"
echo "sync-instructions: $(echo "$local_" | grep -c .) файлов, хеши совпадают с сервером → $DEST"
