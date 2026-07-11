# TechTim Palworld Server Panel

Palworld Dedicated Server를 GCP VM에서 관리하기 위한 TechTim Web UI 패널입니다.

## 기능

- FastAPI 기반 Web UI
- admin/admin 최초 로그인 및 비밀번호 변경 강제
- Pocketpair 공식 Palworld 1.0 Docker 이미지 기반 엔진 설치
- PalWorldSettings.ini 조회/저장
- 서버 시작/중지/재시작/상태/로그 조회
- 설치 로그와 서버 로그 자동 갱신 및 자동 스크롤
- Pal/Saved 서버 디렉토리 탐색기

게임 서버 런타임 이미지는 다음 공식 버전으로 고정합니다.

```text
ghcr.io/pocketpairjp/palserver:v1.0.0.100427
```

설정과 세이브는 호스트의
`/opt/techtim/palworld/data/server/Pal/Saved`에 유지되며, 공식 컨테이너의
`/pal/Package/Pal/Saved`에 마운트됩니다.

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
