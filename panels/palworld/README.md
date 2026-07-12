# TechTim Palworld Server Panel

Palworld Dedicated Server를 GCP VM에서 관리하기 위한 TechTim Web UI 패널입니다.

## 기능

- FastAPI 기반 Web UI
- admin/admin 최초 로그인 및 비밀번호 변경 강제
- Pocketpair 공식 Palworld 1.0 Docker 이미지 기반 엔진 설치
- 최초 설치 후 `엔진 설치` 버튼을 `서버 업데이트`로 전환하고 공식 latest 이미지 Pull
- PalWorldSettings.ini 조회/저장
- 서버 시작/중지/재시작/상태/로그 조회
- 매일 지정한 KST 시각에 실행 중인 게임 서버 컨테이너 자동 재시작
- 설치 로그와 서버 로그 자동 갱신 및 자동 스크롤
- Pal/Saved 서버 디렉토리 탐색기와 파일/폴더 구조 업로드

게임 서버 런타임 이미지는 Pocketpair 공식 최신 태그를 사용합니다.

```text
ghcr.io/pocketpairjp/palserver:latest
```

설정과 세이브는 호스트의
`/opt/techtim/palworld/data/server/Pal/Saved`에 유지되며, 공식 컨테이너의
`/pal/Package/Pal/Saved`에 마운트됩니다.

자동 재시작 예약은 `/opt/techtim/palworld/data/restart-schedule.json`에
저장됩니다. 예약 시각에 게임 서버가 중지 상태라면 서버를 자동으로
시작하지 않고 해당 실행을 건너뜁니다. 예약 설정은 게임 서버가 중지된
상태에서만 변경할 수 있습니다.

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
