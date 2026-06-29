#!/bin/bash
# 더블클릭으로 사진 정리기 웹앱 실행. (Finder 에서 더블클릭)
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
  echo "처음 실행: 가상환경/의존성 설치 중…"
  python3 -m venv .venv && .venv/bin/pip install --quiet Pillow pillow-heif
fi
exec .venv/bin/python app.py
