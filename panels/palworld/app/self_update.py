from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import os
import time

import docker


KST = timezone(timedelta(hours=9), name="KST")
TARGET_CONTAINER = os.getenv("TARGET_CONTAINER", "palworld-panel")
PROXY_CONTAINER = os.getenv("PROXY_CONTAINER", "palworld-panel-proxy")
TARGET_IMAGE = os.getenv("TARGET_IMAGE", "ghcr.io/kortechtim/palworld-panel:latest")
STATUS_FILE = Path(os.getenv("PANEL_UPDATE_STATUS_FILE", "/update-data/panel-update-status.json"))
START_DELAY_SECONDS = max(1, int(os.getenv("PANEL_UPDATE_DELAY_SECONDS", "2")))


def write_status(status: str, message: str, **details) -> None:
    payload = {
        "status": status,
        "message": message,
        "updated_at": datetime.now(KST).isoformat(timespec="seconds"),
        **details,
    }
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = STATUS_FILE.with_suffix(".tmp")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_path.replace(STATUS_FILE)


def container_run_options(container) -> dict:
    attrs = container.attrs
    config = attrs.get("Config") or {}
    host_config = attrs.get("HostConfig") or {}
    network_mode = str(host_config.get("NetworkMode") or "").strip()
    environment = [
        value
        for value in (config.get("Env") or [])
        if not str(value).startswith("PANEL_VERSION=")
    ]
    options = {
        "detach": True,
        "name": TARGET_CONTAINER,
        "environment": environment,
        "labels": config.get("Labels") or {},
        "tty": bool(config.get("Tty")),
        "stdin_open": bool(config.get("OpenStdin")),
        "read_only": bool(host_config.get("ReadonlyRootfs")),
    }

    if config.get("Cmd"):
        options["command"] = config["Cmd"]

    if config.get("Entrypoint"):
        options["entrypoint"] = config["Entrypoint"]

    if config.get("WorkingDir"):
        options["working_dir"] = config["WorkingDir"]

    if config.get("User"):
        options["user"] = config["User"]

    if host_config.get("Binds"):
        options["volumes"] = host_config["Binds"]

    restart_policy = host_config.get("RestartPolicy") or {}

    if restart_policy.get("Name"):
        options["restart_policy"] = restart_policy

    if network_mode and network_mode != "default":
        options["network"] = network_mode

    return options


def wait_until_running(container, timeout_seconds: int = 20) -> None:
    deadline = time.monotonic() + timeout_seconds

    while time.monotonic() < deadline:
        container.reload()

        if container.status == "running":
            return

        if container.status in {"dead", "exited", "removing"}:
            logs = container.logs(tail=30).decode("utf-8", errors="replace")
            raise RuntimeError(f"새 패널 컨테이너가 시작되지 않았습니다: {logs[-1200:]}")

        time.sleep(0.5)

    raise RuntimeError("새 패널 컨테이너가 제한 시간 안에 실행 상태가 되지 않았습니다.")


def remove_container_if_present(client, name: str) -> None:
    try:
        container = client.containers.get(name)
    except docker.errors.NotFound:
        return

    container.remove(force=True)


def restart_proxy_if_present(client) -> None:
    try:
        proxy = client.containers.get(PROXY_CONTAINER)
    except docker.errors.NotFound:
        return

    proxy.reload()

    if proxy.status == "running":
        proxy.restart(timeout=10)
    else:
        proxy.start()


def main() -> None:
    time.sleep(START_DELAY_SECONDS)
    client = docker.from_env()
    target = client.containers.get(TARGET_CONTAINER)
    target.reload()
    old_image_id = target.image.id
    options = container_run_options(target)
    latest_image = client.images.get(TARGET_IMAGE)
    latest_image_id = latest_image.id

    write_status(
        "restarting",
        "기존 TechTim 구동기 컨테이너를 중지하고 최신 이미지로 교체하고 있습니다.",
        progress=92,
        current_image_id=old_image_id,
        latest_image_id=latest_image_id,
    )
    target.stop(timeout=15)
    target.remove(force=True)

    try:
        replacement = client.containers.run(TARGET_IMAGE, **options)
        wait_until_running(replacement)
        write_status(
            "restarting",
            "새 TechTim 구동기 컨테이너가 시작되었습니다. 연결을 마무리하고 있습니다.",
            progress=97,
            current_image_id=old_image_id,
            latest_image_id=latest_image_id,
        )
        restart_proxy_if_present(client)
        write_status(
            "completed",
            "TechTim 구동기 업데이트가 완료되었습니다.",
            progress=100,
            current_image_id=latest_image_id,
            latest_image_id=latest_image_id,
        )
    except Exception as update_error:
        remove_container_if_present(client, TARGET_CONTAINER)

        try:
            rollback = client.containers.run(old_image_id, **options)
            wait_until_running(rollback)
            restart_proxy_if_present(client)
            write_status(
                "failed",
                f"업데이트에 실패하여 기존 TechTim 구동기로 복구했습니다: {update_error}",
                progress=100,
                current_image_id=old_image_id,
                latest_image_id=latest_image_id,
                rollback="completed",
            )
        except Exception as rollback_error:
            write_status(
                "failed",
                f"업데이트와 기존 버전 복구에 모두 실패했습니다: {update_error}; 복구 오류: {rollback_error}",
                progress=97,
                current_image_id=old_image_id,
                latest_image_id=latest_image_id,
                rollback="failed",
            )
            raise


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        try:
            existing = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = {}

        if existing.get("status") != "failed":
            write_status(
                "failed",
                f"TechTim 구동기 교체 작업에 실패했습니다: {error}",
                progress=int(existing.get("progress") or 0),
            )

        raise
