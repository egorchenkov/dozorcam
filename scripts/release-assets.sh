#!/bin/sh
# Файлы GitHub-релиза, которые читают install.sh и `dozorcam update`:
# compose.yml, env.example (= .env.example), CHANGELOG.md, install.sh, обёртка dozorcam и SHA256SUMS.
# Без точки в начале: GitHub переименовывает ассет «.env.example» в «default.env.example».
# Тот же набор собирает CI-джоб install-smoke, так что установщик проверяется на
# том, что потом уезжает в релиз.
#
#   scripts/release-assets.sh <каталог>
set -eu
out=${1:?usage: scripts/release-assets.sh OUTDIR}
root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
mkdir -p "$out"
cp "$root/compose.yml" "$root/CHANGELOG.md" "$out/"
cp "$root/.env.example" "$out/env.example"
cp "$root/scripts/install.sh" "$root/scripts/dozorcam" "$out/"
chmod 0755 "$out/install.sh" "$out/dozorcam"
(cd "$out" && sha256sum compose.yml env.example CHANGELOG.md install.sh dozorcam >SHA256SUMS)
