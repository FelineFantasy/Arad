import asyncio
import json
import os
import platform
import signal
import subprocess
import sys
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

import aiohttp
import psutil
from loguru import logger

# Конфигурация
API_BASE_URL = "http://localhost:8000"
DEVICE_ID = "super_device_id"  # Можно сделать уникальным
ADMIN_TOKEN = "secret_token"  # Только для регистрации

# Настройка логирования
logger.remove()
logger.add(
    sys.stdout,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
    level="INFO",
)
logger.add("device_client.log", rotation="10 MB", retention="7 days", level="DEBUG")


class Platform(str, Enum):
    WINDOWS = "windows"
    LINUX = "linux"
    MACOS = "macos"
    ANDROID = "android"
    IOS = "ios"


class DeviceClient:
    def __init__(self, base_url: str = API_BASE_URL, device_id: str = None):
        self.base_url = base_url.rstrip("/")
        self.device_id = device_id or self._generate_device_id()
        self.device_token = None
        self.session: Optional[aiohttp.ClientSession] = None
        self.running = False
        self.heartbeat_interval = 30  # секунд
        self.polling_interval = 5  # секунд
        self.system_info = self._collect_system_info()
        logger.info(f"Device client initialized: {self.device_id}")

    def _generate_device_id(self) -> str:
        """Генерация уникального ID устройства"""
        hostname = platform.node()
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        return f"{hostname}-{timestamp}"

    def _collect_system_info(self) -> Dict[str, Any]:
        """Сбор информации о системе"""
        try:
            return {
                "device_id": self.device_id,
                "name": platform.node(),
                "platform": self._detect_platform(),
                "arch": platform.machine(),
                "version": platform.version(),
                "processor": platform.processor(),
                "ram_gb": round(psutil.virtual_memory().total / (1024**3), 2),
                "cpu_count": psutil.cpu_count(),
                "python_version": platform.python_version(),
                "hostname": platform.node(),
            }
        except Exception as e:
            logger.error(f"Error collecting system info: {e}")
            return {
                "device_id": self.device_id,
                "name": "Unknown Device",
                "platform": self._detect_platform(),
                "arch": platform.machine(),
                "version": "Unknown",
            }

    def _detect_platform(self) -> str:
        """Определение платформы"""
        system = platform.system().lower()
        if system == "windows":
            return Platform.WINDOWS
        elif system == "linux":
            # Проверяем, не Android ли это
            if "android" in platform.platform().lower():
                return Platform.ANDROID
            return Platform.LINUX
        elif system == "darwin":
            return Platform.MACOS
        else:
            return "unknown"

    async def _create_session(self):
        """Создание aiohttp сессии"""
        if not self.session:
            timeout = aiohttp.ClientTimeout(total=30)
            self.session = aiohttp.ClientSession(
                base_url=self.base_url,
                timeout=timeout,
                headers={"User-Agent": f"DeviceClient/{self.device_id}"},
            )
            logger.debug("HTTP session created")

    async def _close_session(self):
        """Закрытие сессии"""
        if self.session:
            await self.session.close()
            self.session = None
            logger.debug("HTTP session closed")

    async def register_device(self) -> bool:
        """Регистрация устройства на сервере"""
        try:
            await self._create_session()

            # Проверяем, не зарегистрировано ли уже устройство
            async with self.session.get(f"/devices/{self.device_id}/heartbeat") as resp:
                if resp.status == 200:
                    logger.info(f"Device {self.device_id} is already registered")
                    return True

            # Регистрируем новое устройство
            registration_data = {
                "device_id": self.system_info["device_id"],
                "name": self.system_info["name"],
                "platform": self.system_info["platform"],
                "arch": self.system_info["arch"],
                "version": self.system_info["version"],
                "ip_address": await self._get_ip_address(),
            }

            logger.info(f"Registering device: {registration_data}")

            async with self.session.post(
                "/devices/register", json=registration_data
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self.device_token = data.get("device_token")
                    logger.success(
                        f"Device registered successfully. Token: {self.device_token[:10]}..."
                    )

                    # Сохраняем токен в файл для последующего использования
                    self._save_device_token()
                    return True
                else:
                    error = await resp.text()
                    logger.error(f"Registration failed: {error}")
                    return False

        except aiohttp.ClientError as e:
            logger.error(f"Network error during registration: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error during registration: {e}")
            return False

    async def _get_ip_address(self) -> str:
        """Получение IP адреса"""
        try:
            # Простой способ получить внешний IP (можно заменить на другой метод)
            async with aiohttp.ClientSession() as temp_session:
                async with temp_session.get("https://api.ipify.org") as resp:
                    return await resp.text()
        except:
            return "unknown"

    def _save_device_token(self):
        """Сохранение токена устройства в файл"""
        if self.device_token:
            token_data = {
                "device_id": self.device_id,
                "token": self.device_token,
                "saved_at": datetime.now().isoformat(),
            }
            try:
                with open(f"{self.device_id}.token", "w") as f:
                    json.dump(token_data, f)
                logger.info(f"Token saved to {self.device_id}.token")
            except Exception as e:
                logger.warning(f"Could not save token: {e}")

    def _load_device_token(self) -> bool:
        """Загрузка токена из файла"""
        try:
            token_file = f"{self.device_id}.token"
            if os.path.exists(token_file):
                with open(token_file, "r") as f:
                    token_data = json.load(f)
                self.device_token = token_data.get("token")
                logger.info("Token loaded from file")
                return True
        except Exception as e:
            logger.warning(f"Could not load token: {e}")
        return False

    async def _make_request(
        self, method: str, endpoint: str, **kwargs
    ) -> Optional[Dict]:
        """Универсальный метод для HTTP запросов"""
        try:
            headers = kwargs.get("headers", {})
            if self.device_token and "/admin/" not in endpoint:
                headers["Authorization"] = f"Bearer {self.device_token}"
            kwargs["headers"] = headers

            async with getattr(self.session, method)(endpoint, **kwargs) as resp:
                if resp.status == 200:
                    return await resp.json()
                elif resp.status == 401:
                    logger.error("Authentication failed. Token may be invalid.")
                    return None
                else:
                    logger.warning(f"Request failed with status {resp.status}")
                    return None
        except aiohttp.ClientError as e:
            logger.error(f"Network error: {e}")
            return None
        except Exception as e:
            logger.error(f"Request error: {e}")
            return None

    async def send_heartbeat(self) -> bool:
        """Отправка heartbeat на сервер"""
        try:
            result = await self._make_request(
                "post", f"/devices/{self.device_id}/heartbeat"
            )
            if result:
                logger.debug(f"Heartbeat sent: {result}")
                return True
            return False
        except Exception as e:
            logger.error(f"Heartbeat error: {e}")
            return False

    async def get_pending_commands(self) -> List[Dict]:
        """Получение pending команд с сервера"""
        try:
            result = await self._make_request(
                "get", f"/devices/{self.device_id}/commands"
            )
            if result and result.get("status") == "success":
                commands = result.get("commands", [])
                logger.info(f"Received {len(commands)} pending commands")
                return commands
            return []
        except Exception as e:
            logger.error(f"Error getting commands: {e}")
            return []

    async def execute_command(self, command_data: Dict) -> Dict:
        """Выполнение команды на устройстве"""
        command_id = command_data.get("command_id", "unknown")
        command = command_data.get("command", "")
        timeout = command_data.get("timeout", 30)

        logger.info(f"Executing command {command_id}: {command[:50]}...")

        try:
            # Безопасное выполнение команды
            if command_data.get("async_exec", True):
                # Асинхронное выполнение с таймаутом
                process = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    shell=True,
                )

                try:
                    stdout, stderr = await asyncio.wait_for(
                        process.communicate(), timeout=timeout
                    )
                    exit_code = process.returncode
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
                    stdout = b""
                    stderr = f"Command timed out after {timeout} seconds".encode()
                    exit_code = -1
            else:
                # Синхронное выполнение
                result = subprocess.run(
                    command, shell=True, capture_output=True, text=True, timeout=timeout
                )
                stdout = result.stdout.encode() if result.stdout else b""
                stderr = result.stderr.encode() if result.stderr else b""
                exit_code = result.returncode

            output = stdout.decode("utf-8", errors="replace")
            error = stderr.decode("utf-8", errors="replace")

            # Формируем результат
            execution_result = {
                "command_id": command_id,
                "command": command,
                "output": f"STDOUT:\n{output}\n\nSTDERR:\n{error}",
                "exit_code": exit_code,
                "executed_at": datetime.now().isoformat(),
                "success": exit_code == 0,
            }

            logger.info(f"Command {command_id} executed with exit code {exit_code}")

            return execution_result

        except subprocess.TimeoutExpired:
            logger.error(f"Command {command_id} timed out")
            return {
                "command_id": command_id,
                "output": f"Command timed out after {timeout} seconds",
                "exit_code": -1,
                "success": False,
            }
        except Exception as e:
            logger.error(f"Error executing command {command_id}: {e}")
            return {
                "command_id": command_id,
                "output": f"Execution error: {str(e)}",
                "exit_code": 1,
                "success": False,
            }

    async def report_command_execution(self, execution_result: Dict) -> bool:
        """Отправка результата выполнения команды на сервер"""
        try:
            data = {
                "command_id": execution_result["command_id"],
                "output": execution_result["output"],
                "exit_code": execution_result["exit_code"],
            }

            result = await self._make_request(
                "post", f"/devices/{self.device_id}/execute", json=data
            )

            if result and result.get("status") == "success":
                logger.info(
                    f"Command {execution_result['command_id']} execution reported"
                )
                return True
            else:
                logger.warning(f"Failed to report command execution")
                return False

        except Exception as e:
            logger.error(f"Error reporting execution: {e}")
            return False

    async def process_commands(self):
        """Обработка всех pending команд"""
        commands = await self.get_pending_commands()

        for command_data in commands:
            if command_data.get("status") == "pending":
                # Выполняем команду
                execution_result = await self.execute_command(command_data)

                # Отправляем результат
                await self.report_command_execution(execution_result)

                # Небольшая пауза между командами
                await asyncio.sleep(0.5)

    async def _heartbeat_loop(self):
        """Цикл отправки heartbeat"""
        while self.running:
            try:
                await self.send_heartbeat()
            except Exception as e:
                logger.error(f"Heartbeat loop error: {e}")

            await asyncio.sleep(self.heartbeat_interval)

    async def _polling_loop(self):
        """Основной цикл опроса сервера"""
        while self.running:
            try:
                # Проверяем и выполняем команды
                await self.process_commands()
            except Exception as e:
                logger.error(f"Polling loop error: {e}")

            await asyncio.sleep(self.polling_interval)

    async def start(self):
        """Запуск клиента"""
        logger.info(f"Starting device client: {self.device_id}")

        # Пытаемся загрузить сохраненный токен
        if not self.device_token:
            self._load_device_token()

        # Регистрируемся, если нужно
        if not self.device_token:
            if not await self.register_device():
                logger.error("Failed to register device. Exiting.")
                return

        # Создаем сессию
        await self._create_session()

        # Запускаем основные циклы
        self.running = True

        # Запускаем heartbeat и polling в отдельных задачах
        heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        polling_task = asyncio.create_task(self._polling_loop())

        # Обработка graceful shutdown
        try:
            # Ждем завершения задач (они будут работать бесконечно)
            await asyncio.gather(heartbeat_task, polling_task)
        except asyncio.CancelledError:
            logger.info("Client shutdown requested")
        finally:
            self.running = False
            await self._close_session()
            logger.info("Device client stopped")

    async def stop(self):
        """Остановка клиента"""
        logger.info("Stopping device client...")
        self.running = False


# Graceful shutdown обработчик
async def shutdown(signal_name: str, client: DeviceClient):
    """Обработка shutdown сигналов"""
    logger.info(f"Received {signal_name}. Shutting down...")
    await client.stop()


async def main():
    """Основная функция"""
    # Создаем клиент
    client = DeviceClient()

    # Настройка обработчиков сигналов
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(
            sig, lambda s=sig: asyncio.create_task(shutdown(s.name, client))
        )

    try:
        # Запускаем клиент
        await client.start()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
    finally:
        await client.stop()


if __name__ == "__main__":
    # Запуск асинхронного клиента
    asyncio.run(main())
