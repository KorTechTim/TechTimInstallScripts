# TechTim Palworld Server Panel

Palworld Dedicated Server를 GCP VM에서 관리하기 위한 TechTim Web UI 패널입니다.

## 기능

- FastAPI 기반 Web UI
- admin/admin 최초 로그인 및 비밀번호 변경 강제
- Pocketpair 공식 Palworld 1.0 Docker 이미지 기반 엔진 설치
- 최초 설치 후 `엔진 설치` 버튼을 `서버 업데이트`로 전환하고 공식 latest 이미지 Pull
- 상단 톱니바퀴 메뉴에서 TechTim 웹패널 컨테이너 자체 업데이트 및 실패 시 자동 복구
- PalWorldSettings.ini 조회/저장
- Community Server 공개 ON/OFF 토글 및 `-publiclobby` 실행 인수 지원
- 서버 시작/중지/재시작/상태/로그 조회
- 매일 지정한 KST 시각에 실행 중인 게임 서버 컨테이너 자동 재시작
- 설치 로그와 서버 로그 자동 갱신 및 자동 스크롤
- Pal/Saved 서버 디렉토리 탐색기, 파일/폴더 구조 업로드, 선택 폴더 ZIP 다운로드

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

## Community Server 공개

메인 서버 설정의 `RCON 사용` 오른쪽에 있는 `공개 OFF/ON` 토글로
Community Server 공개 모드를 선택할 수 있습니다. 활성화하면 Palworld 서버
컨테이너를 새로 만들 때 `-publiclobby` 실행 인수가 추가됩니다. Xbox 및 PS5에서
서버 이름을 검색해 접속하려면 Community Server 공개 모드가 필요합니다.

공개 설정은 `PalWorldSettings.ini`가 아닌 다음 별도 파일에 저장됩니다.

```text
/opt/techtim/palworld/data/server-launch-settings.json
```

기존 설치 환경에서 파일이 없거나 손상된 경우 기본값은 `OFF`이며 기존 INI와
세이브에는 영향을 주지 않습니다. 설정을 적용하려면 게임 서버를 중지하고 토글을
변경한 뒤 `설정 저장`을 누른 다음 서버를 다시 시작해야 합니다. 게임 접속에는
기존과 동일하게 UDP 8211 포트와 GCP VPC/호스트 방화벽 정책을 사용합니다.

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
