# TechTim Minecraft Java Server Panel

`itzg/minecraft-server`를 이용해 Minecraft Java Edition 서버를 관리하는 GCP VM용 Web UI입니다.

## 주요 기능

- `admin/admin` 최초 로그인 후 새 비밀번호 설정
- Vanilla, Paper, Purpur, Spigot, Forge, NeoForge, Fabric, Quilt 지원
- Java 8, 11, 16, 17, 21, 25 런타임 선택
- Forge, NeoForge, Fabric 서버팩 URL 자동 설치
- 서버 설치, 시작, 중지, 삭제, 상태 및 실시간 로그
- 게임 컨테이너와 전체 서버 데이터를 초기화한 뒤 엔진 설치부터 다시 시작
- 서버 실행 중 설정/파일 쓰기 잠금
- `/data` 전체 서버 디렉터리 탐색기
- 한국표준시 기준 매일 예약 재시작 및 최근 실행 결과 확인
- CPU, 메모리, 디스크, 네트워크 리소스 5초 주기 모니터링
- Docker 이미지 Pull 진행 로그, 웹패널 업데이트 진행률 및 VM 재부팅 후 자동 복구

게임 데이터는 호스트의 `/opt/techtim/minecraft/data/server`에 유지되고 게임 컨테이너의 `/data`에 마운트됩니다.

예약 재시작 설정은 호스트의 `/opt/techtim/minecraft/data/restart-schedule.json`에 저장됩니다. 예약 시각에 서버가 실행 중일 때만 월드 저장 후 게임 컨테이너를 재시작하며, 서버가 꺼져 있거나 백업·복원이 진행 중이면 해당 회차를 건너뜁니다.

## Local Docker Build

```bash
docker build -t minecraft-panel panels/minecraft
docker run --rm -p 8080:8080 \
  -e DATA_DIR=/data \
  -e HOST_DATA_DIR=/tmp/minecraft-panel-data \
  -v /tmp/minecraft-panel-data:/data \
  -v /var/run/docker.sock:/var/run/docker.sock \
  minecraft-panel
```

초기 계정은 `admin / admin`입니다. Minecraft EULA는 서버 시작 버튼을 누를 때 공식 약관을 확인한 뒤 동의해야 합니다.

## GCP 배포

Vercel 프로젝트에 다음 환경 변수를 먼저 등록합니다.

```text
INSTALL_CODE_MINECRAFT=MC-2026-GCP-AABB22112211
```

VM 생성 예시:

```bash
gcloud compute instances create minecraft \
  --zone=asia-northeast3-a \
  --machine-type=n2d-highmem-4 \
  --image-family=ubuntu-2204-lts \
  --image-project=ubuntu-os-cloud \
  --boot-disk-size=50GB \
  --boot-disk-type=pd-ssd \
  --boot-disk-device-name=minecraft \
  --metadata=startup-script-url=https://raw.githubusercontent.com/KorTechTim/TechTimInstallScripts/refs/heads/main/minecraft/minecraft-webui-install.sh,install-code=MC-2026-GCP-AABB22112211
```

GCP 방화벽 예시:

```bash
gcloud compute firewall-rules create techtim-allow-all-ingress \
  --network=default \
  --direction=INGRESS \
  --priority=1000 \
  --action=ALLOW \
  --rules=tcp,udp,icmp \
  --source-ranges=0.0.0.0/0
```

패널은 `http://VM_EXTERNAL_IP:8080`, Minecraft Java 서버는 `VM_EXTERNAL_IP:25565`로 접속합니다.
