import asyncio
import json
import logging
import secrets
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import BackgroundTasks, Body, Depends, FastAPI, HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Device Command Manager API",
    description="API для управления удаленными устройствами и выполнения команд",
    version="1.0.0",
)

# Security
security = HTTPBearer()
ADMIN_TOKEN = "secret_token"  # Основной токен администратора

# Хранилище данных в памяти
command_pool = {}
registered_devices = {}
device_tokens = {}  # device_id -> token
command_history = {}
active_sessions = {}


# Модели данных
class Platform(str, Enum):
    WINDOWS = "windows"
    LINUX = "linux"
    MACOS = "macos"
    ANDROID = "android"
    IOS = "ios"


class DeviceStatus(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    BUSY = "busy"


class DeviceInfo(BaseModel):
    device_id: str = Field(
        ..., min_length=3, max_length=100, description="Уникальный ID устройства"
    )
    name: Optional[str] = Field(None, max_length=100, description="Название устройства")
    platform: Platform = Field(..., description="Платформа устройства")
    arch: str = Field(..., description="Архитектура процессора")
    version: Optional[str] = Field(None, description="Версия ОС/прошивки")
    ip_address: Optional[str] = Field(None, description="IP адрес устройства")


class CommandRequest(BaseModel):
    command: str = Field(
        ..., min_length=1, max_length=1000, description="Команда для выполнения"
    )
    timeout: Optional[int] = Field(
        30, ge=1, le=300, description="Таймаут выполнения в секундах"
    )
    async_exec: Optional[bool] = Field(True, description="Асинхронное выполнение")


class CommandExecution(BaseModel):
    command_id: str
    command: str
    status: str  # pending, executing, completed, failed
    output: Optional[str]
    created_at: datetime
    executed_at: Optional[datetime]
    device_id: str


class DeviceResponse(BaseModel):
    device_id: str
    name: Optional[str]
    platform: Platform
    arch: str
    status: DeviceStatus
    last_seen: datetime
    registered_at: datetime
    pending_commands: int


# Вспомогательные функции
def generate_device_token(device_id: str) -> str:
    """Генерация токена для устройства"""
    token = secrets.token_urlsafe(32)
    device_tokens[device_id] = token
    return token


def verify_admin_token(credentials: HTTPAuthorizationCredentials = Security(security)):
    """Верификация токена администратора"""
    if credentials.credentials != ADMIN_TOKEN:
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing admin token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return True


def verify_device_token(
    credentials: HTTPAuthorizationCredentials = Security(security),
) -> Optional[str]:
    """Проверка токена устройства и возврат device_id"""
    token = credentials.credentials

    # Находим device_id по токену
    for device_id, stored_token in device_tokens.items():
        if stored_token == token:
            return device_id

    raise HTTPException(
        status_code=401,
        detail="Invalid or missing device token",
        headers={"WWW-Authenticate": "Bearer"},
    )


def update_device_status(device_id: str):
    """Обновление статуса устройства"""
    active_sessions[device_id] = datetime.now()
    if device_id in registered_devices:
        registered_devices[device_id]["last_seen"] = datetime.now()


def cleanup_old_sessions():
    """Очистка неактивных сессий"""
    cutoff = datetime.now() - timedelta(minutes=5)
    to_remove = []
    for device_id, last_seen in active_sessions.items():
        if last_seen < cutoff:
            to_remove.append(device_id)

    for device_id in to_remove:
        del active_sessions[device_id]


# Роуты API


@app.post(
    "/devices/register",
    response_model=Dict[str, Any],
    summary="Регистрация нового устройства",
    tags=["Devices"],
)
async def register_device(device_info: DeviceInfo, background_tasks: BackgroundTasks):
    """
    Регистрация нового устройства в системе.
    """
    if device_info.device_id in registered_devices:
        raise HTTPException(status_code=400, detail="Device already registered")

    # Генерация токена для устройства
    device_token = generate_device_token(device_info.device_id)

    # Сохранение информации об устройстве
    registered_devices[device_info.device_id] = {
        **device_info.dict(),
        "registered_at": datetime.now(),
        "last_seen": datetime.now(),
    }

    # Инициализация пула команд
    command_pool[device_info.device_id] = []
    command_history[device_info.device_id] = []

    # Создание активной сессии
    active_sessions[device_info.device_id] = datetime.now()

    logger.info(f"Device registered: {device_info.device_id}")

    return {
        "status": "success",
        "device_id": device_info.device_id,
        "device_token": device_token,
        "message": "Device registered successfully",
    }


@app.post("/devices/{device_id}/heartbeat", tags=["Devices"])
async def device_heartbeat(
    device_id: str, authenticated_device_id: str = Depends(verify_device_token)
):
    """
    Отправка heartbeat от устройства для поддержания сессии.
    """
    # Проверяем, что device_id из пути совпадает с аутентифицированным
    if device_id != authenticated_device_id:
        raise HTTPException(status_code=403, detail="Device ID mismatch")

    update_device_status(device_id)
    return {"status": "success", "message": "Heartbeat received"}


@app.get(
    "/devices",
    response_model=Dict[str, Any],
    summary="Получить список всех устройств",
    tags=["Admin"],
)
async def get_all_devices(
    token: str = Depends(verify_admin_token), status: Optional[DeviceStatus] = None
):
    """
    Получить список всех зарегистрированных устройств.
    Требуется токен администратора.
    """
    cleanup_old_sessions()

    devices = []
    for device_id, info in registered_devices.items():
        device_status = (
            DeviceStatus.ONLINE
            if device_id in active_sessions
            else DeviceStatus.OFFLINE
        )
        if status and device_status != status:
            continue

        devices.append(
            DeviceResponse(
                device_id=device_id,
                name=info.get("name"),
                platform=info["platform"],
                arch=info["arch"],
                status=device_status,
                last_seen=info["last_seen"],
                registered_at=info["registered_at"],
                pending_commands=len(
                    [
                        cmd
                        for cmd in command_pool.get(device_id, [])
                        if cmd.get("status") == "pending"
                    ]
                ),
            )
        )

    return {
        "status": "success",
        "devices": devices,
        "total": len(devices),
        "online": len([d for d in devices if d.status == DeviceStatus.ONLINE]),
        "offline": len([d for d in devices if d.status == DeviceStatus.OFFLINE]),
    }


@app.get(
    "/devices/{device_id}/commands",
    response_model=Dict[str, Any],
    summary="Получить команды для устройства",
    tags=["Devices"],
)
async def get_device_commands(
    device_id: str,
    authenticated_device_id: str = Depends(verify_device_token),
    limit: int = 10,
    pending_only: bool = True,
):
    """
    Получить команды для выполнения на устройстве.
    Возвращает как pending, так и выполненные команды.
    """
    # Проверяем, что device_id из пути совпадает с аутентифицированным
    if device_id != authenticated_device_id:
        raise HTTPException(status_code=403, detail="Device ID mismatch")

    update_device_status(device_id)

    if device_id not in command_pool:
        raise HTTPException(status_code=404, detail="Device not found")

    commands = command_pool[device_id]

    if pending_only:
        commands = [cmd for cmd in commands if cmd.get("status") == "pending"]

    return {
        "status": "success",
        "device_id": device_id,
        "commands": commands[:limit],
        "total_pending": len(
            [cmd for cmd in command_pool[device_id] if cmd.get("status") == "pending"]
        ),
    }


@app.post(
    "/commands",
    response_model=Dict[str, Any],
    summary="Добавить команду для устройства",
    tags=["Admin"],
)
async def add_command(
    command_req: CommandRequest,
    device_id: Optional[str] = None,
    token: str = Depends(verify_admin_token),
):
    """
    Добавить команду для выполнения.
    Если device_id не указан, команда добавляется всем устройствам.
    """
    command_id = secrets.token_urlsafe(8)

    if device_id:
        # Команда для конкретного устройства
        if device_id not in registered_devices:
            raise HTTPException(status_code=404, detail="Device not found")

        devices = [device_id]
        message = f"Command added to device {device_id}"
    else:
        # Команда для всех устройств
        devices = list(registered_devices.keys())
        message = "Command added to all devices"

    for dev_id in devices:
        command_entry = {
            "command_id": command_id,
            "command": command_req.command,
            "status": "pending",
            "created_at": datetime.now(),
            "device_id": dev_id,
            "timeout": command_req.timeout,
            "async_exec": command_req.async_exec,
        }

        command_pool[dev_id].append(command_entry)

        # Сохраняем в историю
        command_history[dev_id].append(command_entry.copy())

    logger.info(f"Command added: {command_id} for devices: {devices}")

    return {
        "status": "success",
        "command_id": command_id,
        "message": message,
        "target_devices": devices if device_id else "all",
        "total_devices_affected": len(devices),
    }


@app.post(
    "/devices/{device_id}/execute",
    response_model=Dict[str, Any],
    summary="Сообщить о выполнении команды",
    tags=["Devices"],
)
async def command_executed(
    device_id: str,
    authenticated_device_id: str = Depends(verify_device_token),
    command_id: str = Body(...),
    output: str = Body(...),
    exit_code: int = Body(0),
):
    """
    Сообщить о выполнении команды устройством.
    """
    # Проверяем, что device_id из пути совпадает с аутентифицированным
    if device_id != authenticated_device_id:
        raise HTTPException(status_code=403, detail="Device ID mismatch")

    update_device_status(device_id)

    if device_id not in command_pool:
        raise HTTPException(status_code=404, detail="Device not found")

    # Находим команду в пуле
    command_found = False
    for cmd in command_pool[device_id]:
        if cmd.get("command_id") == command_id and cmd.get("status") == "pending":
            cmd["status"] = "completed" if exit_code == 0 else "failed"
            cmd["output"] = output
            cmd["executed_at"] = datetime.now()
            cmd["exit_code"] = exit_code
            command_found = True
            break

    if not command_found:
        raise HTTPException(
            status_code=404, detail="Command not found or already executed"
        )

    # Обновляем историю
    for hist_cmd in command_history[device_id]:
        if hist_cmd.get("command_id") == command_id:
            hist_cmd.update(cmd)
            break

    logger.info(f"Command executed: {command_id} on device {device_id}")

    return {
        "status": "success",
        "message": "Command execution reported",
        "command_id": command_id,
        "device_id": device_id,
    }


@app.get(
    "/commands/history",
    response_model=Dict[str, Any],
    summary="Получить историю команд",
    tags=["Admin"],
)
async def get_command_history(
    token: str = Depends(verify_admin_token),
    device_id: Optional[str] = None,
    limit: int = 50,
    days: int = 7,
):
    """
    Получить историю выполненных команд.
    Можно фильтровать по устройству и периоду времени.
    """
    cleanup_old_sessions()
    cutoff = datetime.now() - timedelta(days=days)
    all_history = []

    if device_id:
        devices = [device_id] if device_id in command_history else []
    else:
        devices = command_history.keys()

    for dev_id in devices:
        for cmd in command_history.get(dev_id, []):
            created_at = cmd.get("created_at")
            if isinstance(created_at, str):
                try:
                    created_at = datetime.fromisoformat(created_at)
                except:
                    created_at = None

            if created_at and created_at > cutoff:
                all_history.append(
                    {
                        **cmd,
                        "device_id": dev_id,
                        "device_name": registered_devices.get(dev_id, {}).get(
                            "name", dev_id
                        ),
                    }
                )

    # Сортировка по времени создания
    all_history.sort(key=lambda x: x.get("created_at") or datetime.min, reverse=True)

    return {
        "status": "success",
        "history": all_history[:limit],
        "total": len(all_history),
        "from_date": cutoff,
    }


@app.delete("/devices/{device_id}", summary="Удалить устройство", tags=["Admin"])
async def delete_device(device_id: str, token: str = Depends(verify_admin_token)):
    """
    Удалить устройство из системы.
    """
    if device_id not in registered_devices:
        raise HTTPException(status_code=404, detail="Device not found")

    # Очистка данных устройства
    del registered_devices[device_id]
    if device_id in command_pool:
        del command_pool[device_id]
    if device_id in command_history:
        del command_history[device_id]

    if device_id in device_tokens:
        del device_tokens[device_id]

    if device_id in active_sessions:
        del active_sessions[device_id]

    logger.info(f"Device deleted: {device_id}")

    return {"status": "success", "message": f"Device {device_id} deleted successfully"}


@app.get("/health", summary="Проверка здоровья API", tags=["System"])
async def health_check():
    """
    Проверка работоспособности API.
    """
    cleanup_old_sessions()

    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "devices_registered": len(registered_devices),
        "devices_online": len(active_sessions),
        "total_pending_commands": sum(
            len([cmd for cmd in cmd_list if cmd.get("status") == "pending"])
            for cmd_list in command_pool.values()
        ),
    }


@app.get("/", summary="Информация о API", tags=["System"])
async def root():
    """
    Основная информация о API.
    """
    return {
        "name": "Device Command Manager API",
        "version": "1.0.0",
        "documentation": "/docs",
        "description": "API для управления удаленными устройствами",
    }


# Middleware для логирования запросов
@app.middleware("http")
async def log_requests(request, call_next):
    logger.info(f"Incoming request: {request.method} {request.url.path}")
    response = await call_next(request)
    logger.info(f"Response status: {response.status_code}")
    return response


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
