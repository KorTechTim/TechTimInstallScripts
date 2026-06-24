# TechTim Romestead Server Panel

Romestead Dedicated Server를 GCP VM에서 관리하기 위한 TechTim Web UI 패널입니다.

## 초기 기능

- FastAPI 기반 Web UI
- 8080 포트 제공
- Romestead 서버 관리 UI 기본 화면
- 추후 엔진 설치, 서버 시작/중지, 로그, 세이브 관리 기능 추가 예정

## Local Docker Build

```bash
docker build -t romestead-panel .
docker run --rm -p 8080:8080 romestead-panel
