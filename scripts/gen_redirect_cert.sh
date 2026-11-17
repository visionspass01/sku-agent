#!/usr/bin/env bash
# 生成「https → http 跳转」用的自签证书（部署时执行一次，产物不进仓库）。
#
# 用途：nginx.conf 的 443 server 只做 301 跳转，用于兜底「浏览器默认以 https 打开 IP」的情况。
# 要求：openssl 1.1.1+（`-addext` 需要）。证书含 IP SAN，浏览器点「继续访问」后即跳转。
#
# 用法：
#   bash scripts/gen_redirect_cert.sh                 # 输出到 docker/certs/
#   REDIRECT_CERT_IP=1.2.3.4 bash scripts/gen_redirect_cert.sh
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
DIR=${1:-"$ROOT/docker/certs"}
CRT="$DIR/redirect-selfsigned.pem"
KEY="$DIR/redirect-selfsigned.key"

mkdir -p "$DIR"

if [ -s "$CRT" ] && [ -s "$KEY" ]; then
    echo "[skip] 已存在：$CRT / $KEY"
    exit 0
fi

IP=${REDIRECT_CERT_IP:-}
if [ -z "$IP" ]; then
    IP=$(hostname -I 2>/dev/null | awk '{print $1}')
fi
[ -n "$IP" ] || IP=127.0.0.1

openssl req -x509 -nodes -newkey rsa:2048 -days 3650 \
    -subj "/CN=${IP}" \
    -addext "subjectAltName=IP:${IP},DNS:localhost" \
    -keyout "$KEY" -out "$CRT" 2>/dev/null

chmod 600 "$KEY"
chmod 644 "$CRT"
echo "[ok] 自签证书已生成（CN=${IP}）"
echo "     $CRT"
echo "     $KEY"
