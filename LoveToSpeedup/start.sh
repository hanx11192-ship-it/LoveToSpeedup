#!/bin/bash
set -e

echo "== 构建并启动 ajiasu-gateway =="
if docker compose version >/dev/null 2>&1; then
  docker compose up -d --build
else
  docker-compose up -d --build
fi

sleep 3
echo ""
echo "== 部署完成 =="
echo "Web 面板(SSH转发):  http://127.0.0.1:5702"
echo "zhuque 内 API:     http://ajiasu-gateway:8000"
echo "zhuque 内代理:     http://ajiasu-gateway:10801"
