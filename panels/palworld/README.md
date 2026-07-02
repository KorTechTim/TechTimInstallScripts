# TechTim Palworld Server Panel

Palworld Dedicated Server를 GCP VM에서 관리하기 위한 TechTim Web UI 패널입니다.

## 기능

- FastAPI 기반 Web UI
- admin/admin 최초 로그인 및 비밀번호 변경 강제
- SteamCMD anonymous 기반 Palworld 엔진 설치
- PalWorldSettings.ini 조회/저장
- 서버 시작/중지/재시작/상태/로그 조회
- 설치 로그와 서버 로그 자동 갱신 및 자동 스크롤
- Pal/Saved/SaveGames ZIP 다운로드/업로드

## Local Docker Build

```bash
docker build -t palworld-panel .
docker run --rm -p 8080:8080 \
  -e DATA_DIR=/data \
  -e HOST_DATA_DIR=/tmp/palworld-panel-data \
  -v /tmp/palworld-panel-data:/data \
  -v /var/run/docker.sock:/var/run/docker.sock \
  palworld-panel
```

초기 계정:

```text
admin / admin
```
