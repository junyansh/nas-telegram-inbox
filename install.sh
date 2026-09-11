#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ $(uname -s) != Linux ]]; then
  echo '请在运行 Docker 的 Linux NAS / 服务器上安装。'
  exit 1
fi
for tool in python3 docker; do
  command -v "$tool" >/dev/null || { echo "缺少依赖：$tool"; exit 1; }
done
docker compose version >/dev/null
python3 setup-host.py configure "$@"
admin=()
if [[ $(id -u) != 0 ]] && command -v sudo >/dev/null; then
  echo '准备数据目录需要 sudo 时，请在终端输入本机密码。'
  sudo -v
  admin=(sudo)
fi
"${admin[@]}" python3 setup-host.py prepare
docker_cmd=(docker)
if ! docker info >/dev/null 2>&1; then
  docker_cmd=("${admin[@]}" docker)
fi
"${docker_cmd[@]}" compose config --quiet
"${docker_cmd[@]}" compose build --pull
# Validate real mounts as the configured service user before starting.
"${docker_cmd[@]}" compose run --rm --no-deps web python -c '
import os, pathlib, secrets
for root in os.environ["DOWNLOAD_ROOTS"].split(":") + ["/data"]:
    p = pathlib.Path(root) / (".write-check-" + secrets.token_hex(8))
    p.write_text("ok")
    p.unlink()
print("存储目录写入检查通过")
'
"${docker_cmd[@]}" compose up -d --wait --wait-timeout 90
python3 setup-host.py url
echo '请将以下服务访问密钥填入网页：'
"${admin[@]}" cat secrets/access_token
echo '首次使用：网页设置 API ID/Hash → 登录 Telegram → 设置保存位置 → 粘贴消息链接。'
