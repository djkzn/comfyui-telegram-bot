#!/usr/bin/env python3
"""
TelegramComfyBot — управление ComfyUI i2v и txt2img воркфлоу через Telegram.

Команды:
  /start          — приветствие
  /help           — справка
  /img <промпт>   — генерация фото по тексту (txt2img)
  /status         — состояние рендера
  /frames <N>     — кол-во кадров для i2v
  /reset          — очистить ожидающие файлы i2v
  /cancel         — прервать текущий рендер
  /diag           — диагностика
  /logs           — последние строки лога
  /sendtest       — тест отправки последнего видео из output
"""

import json
import os
import re
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests


PROGRESS_NOTIFY_INTERVAL = 300
MAX_VIDEO_SIZE_TG_MB = 49
LOG_TAIL_LINES = 40


class BotError(Exception):
    pass


class BusyError(BotError):
    pass


class TelegramComfyBot:

    # ------------------------------------------------------------------
    # Инициализация
    # ------------------------------------------------------------------

    def __init__(self, config_path: str) -> None:
        self.config = self._load_json(config_path)

        self.token: str = self.config["telegram_bot_token"]
        self.allowed_chat_id: int = int(self.config["allowed_chat_id"])
        self.poll_timeout: int = int(self.config.get("poll_timeout_seconds", 25))
        self.poll_interval: int = int(self.config.get("status_poll_interval_seconds", 5))
        self.min_free_disk_gb: float = float(self.config.get("min_free_disk_gb", 10))

        self.base_url: str = self.config["comfyui_base_url"].rstrip("/")

        # --- i2v воркфлоу ---
        self.comfy_input_dir = Path(self.config["comfy_input_dir"]).expanduser()
        self.comfy_output_dir = Path(self.config["comfy_output_dir"]).expanduser()
        self.workflow_api_json = Path(self.config["workflow_api_json"]).expanduser()
        self.photo_node_id: str = str(self.config["photo_node_id"])
        self.photo_input_field: str = self.config.get("photo_input_field", "image")
        self.video_node_id: str = str(self.config["video_node_id"])
        self.video_input_field: str = self.config.get("video_input_field", "video")
        self.save_node_id: Optional[str] = str(self.config.get("save_node_id", "")).strip() or None
        self.save_prefix_field: str = self.config.get("save_prefix_field", "filename_prefix")
        self.frames_node_id: Optional[str] = str(self.config.get("frames_node_id", "")).strip() or None
        self.frames_field: str = self.config.get("frames_field", "value")
        self.frames_default: int = int(self.config.get("frames_default", 150))
        self.result_extensions: Tuple[str, ...] = tuple(
            self.config.get("result_extensions", [".mp4", ".mov", ".webm", ".gif"])
        )

        # --- txt2img воркфлоу ---
        self.txt2img_workflow_json = Path(self.config["txt2img_workflow_api_json"]).expanduser()
        self.txt2img_prompt_node_id: str = str(self.config.get("txt2img_prompt_node_id", "57:27"))
        self.txt2img_prompt_field: str = self.config.get("txt2img_prompt_field", "text")
        self.txt2img_save_node_id: Optional[str] = str(self.config.get("txt2img_save_node_id", "9")).strip() or None
        self.txt2img_save_prefix_field: str = self.config.get("txt2img_save_prefix_field", "filename_prefix")
        self.txt2img_result_extensions: Tuple[str, ...] = tuple(
            self.config.get("txt2img_result_extensions", [".png", ".jpg", ".jpeg", ".webp"])
        )

        # --- Рабочие директории ---
        self.work_dir = Path(self.config.get("work_dir", "./bot_data")).expanduser()
        self.download_dir = self.work_dir / "downloads"
        self.runtime_dir = self.work_dir / "runtime"
        self.state_path = self.runtime_dir / "state.json"
        self.log_path = self.runtime_dir / "bot.log"
        self.offset_path = self.runtime_dir / "offset.txt"

        for d in (self.download_dir, self.runtime_dir,
                  self.comfy_input_dir, self.comfy_output_dir):
            d.mkdir(parents=True, exist_ok=True)

        self.state: Dict[str, Any] = self._load_state()
        self.last_history_check: float = 0.0
        self.last_progress_notify: float = 0.0

    # ------------------------------------------------------------------
    # Состояние / лог / offset
    # ------------------------------------------------------------------

    def _load_json(self, path: str) -> Dict[str, Any]:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _load_state(self) -> Dict[str, Any]:
        if self.state_path.exists():
            try:
                with open(self.state_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"pending": {}, "running_job": None}

    def _save_state(self) -> None:
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2)

    def _read_offset(self) -> int:
        if self.offset_path.exists():
            try:
                return int(self.offset_path.read_text(encoding="utf-8").strip())
            except Exception:
                return 0
        return 0

    def _write_offset(self, value: int) -> None:
        self.offset_path.write_text(str(value), encoding="utf-8")

    def _log(self, message: str) -> None:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {message}"
        print(line, flush=True)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def _log_tail(self, n: int = LOG_TAIL_LINES) -> str:
        if not self.log_path.exists():
            return "(лог пустой)"
        with open(self.log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return "".join(lines[-n:]).strip() or "(лог пустой)"

    # ------------------------------------------------------------------
    # Telegram API
    # ------------------------------------------------------------------

    def tg(self, method: str, *, params=None, files=None, timeout: int = 120) -> Any:
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        try:
            r = requests.post(url, data=params, files=files, timeout=timeout)
            data = r.json()
        except Exception as e:
            raise BotError(f"Telegram API сетевая ошибка: {e}")
        if not data.get("ok"):
            raise BotError(f"Telegram API {method} вернул ошибку: {data}")
        return data["result"]

    def tg_get_updates(self, offset: int) -> List[Dict[str, Any]]:
        return self.tg(
            "getUpdates",
            params={"offset": offset, "timeout": self.poll_timeout},
            timeout=self.poll_timeout + 15,
        )

    def send_message(self, chat_id: int, text: str) -> None:
        try:
            self.tg("sendMessage", params={"chat_id": chat_id, "text": text})
        except Exception as e:
            self._log(f"send_message failed: {e}")

    def send_video(self, chat_id: int, path: Path, caption: str = "") -> None:
        size_mb = path.stat().st_size / (1024 * 1024)
        with open(path, "rb") as f:
            if size_mb <= MAX_VIDEO_SIZE_TG_MB:
                try:
                    self.tg(
                        "sendVideo",
                        params={"chat_id": chat_id, "caption": caption, "supports_streaming": "true"},
                        files={"video": f},
                        timeout=600,
                    )
                    return
                except Exception:
                    f.seek(0)
            self.tg(
                "sendDocument",
                params={"chat_id": chat_id, "caption": caption},
                files={"document": f},
                timeout=600,
            )

    def send_photo(self, chat_id: int, path: Path, caption: str = "") -> None:
        with open(path, "rb") as f:
            try:
                self.tg(
                    "sendPhoto",
                    params={"chat_id": chat_id, "caption": caption},
                    files={"photo": f},
                    timeout=120,
                )
            except Exception:
                f.seek(0)
                self.tg(
                    "sendDocument",
                    params={"chat_id": chat_id, "caption": caption},
                    files={"document": f},
                    timeout=120,
                )

    def download_telegram_file(self, file_id: str, dest: Path) -> Path:
        info = self.tg("getFile", params={"file_id": file_id})
        url = f"https://api.telegram.org/file/bot{self.token}/{info['file_path']}"
        r = requests.get(url, timeout=300)
        r.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(r.content)
        return dest

    # ------------------------------------------------------------------
    # ComfyUI API
    # ------------------------------------------------------------------

    def comfy_get(self, path: str, timeout: int = 60) -> Any:
        r = requests.get(f"{self.base_url}{path}", timeout=timeout)
        r.raise_for_status()
        return r.json()

    def comfy_post(self, path: str, payload: Dict[str, Any], timeout: int = 120) -> Any:
        r = requests.post(f"{self.base_url}{path}", json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json()

    def comfy_interrupt(self) -> None:
        try:
            requests.post(f"{self.base_url}/interrupt", timeout=15)
        except Exception as e:
            raise BotError(f"Не удалось отправить interrupt в ComfyUI: {e}")

    def comfy_queue_size(self) -> Tuple[int, int]:
        try:
            data = self.comfy_get("/queue")
            return len(data.get("queue_running", [])), len(data.get("queue_pending", []))
        except Exception:
            return 0, 0

    # ------------------------------------------------------------------
    # Классификация ошибок
    # ------------------------------------------------------------------

    def classify_error(self, exc: Exception) -> Tuple[str, str]:
        msg = str(exc).lower()
        if any(x in msg for x in ["no space left", "disk full", "not enough space", "ниже лимита"]):
            return ("🟡 Попроси родителей",
                    "На диске закончилось место. Удали старые файлы из ComfyUI/output.")
        if any(x in msg for x in ["connection refused", "failed to establish",
                                   "max retries exceeded", "connectionerror"]):
            return ("🟢 Решается удалённо",
                    "ComfyUI не запущен. Подключись удалённо и перезапусти ComfyUI.")
        if any(x in msg for x in ["cuda out of memory", "out of memory", "oom"]):
            return ("🟢 Решается удалённо",
                    "Не хватает VRAM. Уменьши кол-во кадров или перезапусти ComfyUI.")
        if any(x in msg for x in ["workflow", "node", "invalid prompt", "not found in workflow"]):
            return ("🟢 Решается удалённо",
                    "Ошибка конфигурации воркфлоу. Проверь node_id в config.json.")
        if any(x in msg for x in ["no such file", "filenotfounderror"]):
            return ("🟢 Решается удалённо",
                    "Файл не найден. Проверь пути в config.json.")
        if any(x in msg for x in ["telegram api", "sendvideo", "sendphoto"]):
            return ("🟢 Решается удалённо",
                    "Проблема с Telegram API. Попробуй повторить через минуту.")
        return ("🔴 Нужна ручная проверка",
                "Нестандартная ошибка. Смотри /logs, попроси родителей сфотографировать экран.")

    # ------------------------------------------------------------------
    # Диск
    # ------------------------------------------------------------------

    def ensure_disk_space(self) -> None:
        usage = shutil.disk_usage(self.work_dir)
        free_gb = usage.free / (1024 ** 3)
        if free_gb < self.min_free_disk_gb:
            raise BotError(f"Мало места на диске: {free_gb:.1f} GB (минимум {self.min_free_disk_gb} GB)")

    # ------------------------------------------------------------------
    # Состояние pending / running
    # ------------------------------------------------------------------

    def pending_for_chat(self, chat_id: int) -> Dict[str, Any]:
        return self.state["pending"].setdefault(str(chat_id), {})

    def clear_pending(self, chat_id: int) -> None:
        self.state["pending"][str(chat_id)] = {}
        self._save_state()

    def running_job(self) -> Optional[Dict[str, Any]]:
        return self.state.get("running_job")

    def set_running_job(self, job: Optional[Dict[str, Any]]) -> None:
        self.state["running_job"] = job
        self._save_state()

    # ------------------------------------------------------------------
    # Форматтеры
    # ------------------------------------------------------------------

    def human_duration(self, seconds: float) -> str:
        seconds = int(seconds)
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h}ч {m}м {s}с"
        if m:
            return f"{m}м {s}с"
        return f"{s}с"

    def _guess_ext(self, name: str, default: str) -> str:
        suf = Path(name).suffix.lower()
        return suf if suf else default

    # ------------------------------------------------------------------
    # Текстовые отчёты
    # ------------------------------------------------------------------

    def status_text(self, chat_id: int) -> str:
        job = self.running_job()
        pending = self.pending_for_chat(chat_id)
        lines: List[str] = []
        if job:
            elapsed = time.time() - job["started_at"]
            jtype = job.get("job_type", "i2v")
            icon = "🎬" if jtype == "i2v" else "🖼"
            label = "i2v рендер" if jtype == "i2v" else "txt2img генерация"
            lines.append(f"{icon} Идёт {label}: {job['job_id']}")
            lines.append(f"⏱ Длительность: {self.human_duration(elapsed)}")
            if jtype == "i2v":
                lines.append(f"🖼 Фото: {'✅' if job.get('person_image') else '❌'}")
                lines.append(f"📹 Видео: {'✅' if job.get('driving_video') else '❌'}")
                lines.append(f"🎞 Кадров: {job.get('frames', self.frames_default)}")
            else:
                prompt_preview = str(job.get("prompt", ""))[:80]
                lines.append(f"📝 Промпт: {prompt_preview}...")
        else:
            lines.append("✅ Рендер не запущен.")
        if pending:
            lines.append("")
            lines.append("Ожидают i2v запуска:")
            lines.append(f"  🖼 Фото: {'✅' if pending.get('photo_path') else '❌'}")
            lines.append(f"  📹 Видео: {'✅' if pending.get('video_path') else '❌'}")
            lines.append(f"  🎞 Кадров: {pending.get('frames', self.frames_default)}")
        return "\n".join(lines)
    def _get_temperatures(self) -> str:
        lines: List[str] = []

        # --- GPU через pynvml ---
        try:
            import pynvml
            pynvml.nvmlInit()
            for i in range(pynvml.nvmlDeviceGetCount()):
                handle = pynvml.nvmlDeviceGetHandleByIndex(i)
                name = pynvml.nvmlDeviceGetName(handle)
                temp = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
                mem_info = pynvml.nvmlDeviceGetMemoryInfo(handle)
                used_mb = mem_info.used // (1024 * 1024)
                total_mb = mem_info.total // (1024 * 1024)
                icon = "🟢" if temp < 70 else "🟡" if temp < 85 else "🔴"
                lines.append(f"  {icon} GPU {name}: {temp}°C  VRAM: {used_mb}/{total_mb} MB")
            pynvml.nvmlShutdown()
        except ImportError:
            lines.append("  ⚠️ GPU: pip install nvidia-ml-py3")
        except pynvml.NVMLError:
            # NVML не найден — пробуем nvidia-smi напрямую
            try:
                import subprocess
                r = subprocess.run(
                    ["nvidia-smi",
                     "--query-gpu=name,temperature.gpu,memory.used,memory.total",
                     "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=8
                )
                for line in r.stdout.strip().splitlines():
                    parts = [x.strip() for x in line.split(",")]
                    if len(parts) >= 4:
                        name, temp, mem_used, mem_total = parts[0], parts[1], parts[2], parts[3]
                        t = int(temp)
                        icon = "🟢" if t < 70 else "🟡" if t < 85 else "🔴"
                        lines.append(f"  {icon} GPU {name}: {t}°C  VRAM: {mem_used}/{mem_total} MB")
            except Exception as e2:
                lines.append(f"  ❌ GPU: nvidia-smi тоже не сработал: {e2}")
        except Exception as e:
            lines.append(f"  ❌ GPU: {e}")

        # --- CPU через PowerShell WMI (Windows only) ---
        try:
            import subprocess
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-WmiObject MSAcpi_ThermalZoneTemperature -Namespace root/wmi "
                 "| Select-Object -ExpandProperty CurrentTemperature"],
                capture_output=True, text=True, timeout=10
            )
            values = [v.strip() for v in r.stdout.strip().splitlines() if v.strip().isdigit()]
            if values:
                temps = [(int(v) - 2732) / 10.0 for v in values]
                avg = sum(temps) / len(temps)
                max_t = max(temps)
                icon = "🟢" if max_t < 70 else "🟡" if max_t < 85 else "🔴"
                lines.append(f"  {icon} CPU: avg {avg:.0f}°C  max {max_t:.0f}°C")
            else:
                # WMI пустой — пробуем OpenHardwareMonitor / LibreHardwareMonitor через WMI
                r2 = subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     "Get-WmiObject -Namespace root/LibreHardwareMonitor -Class Sensor "
                     "| Where-Object { $_.SensorType -eq 'Temperature' -and $_.Name -like '*CPU*' } "
                     "| Select-Object Name, Value"],
                    capture_output=True, text=True, timeout=10
                )
                out = r2.stdout.strip()
                if out:
                    for line in out.splitlines():
                        line = line.strip()
                        if line and "Name" not in line and "----" not in line:
                            lines.append(f"  🌡 CPU (OHM): {line}")
                else:
                    lines.append(
                        "  ⚠️ CPU: температура недоступна.\n"
                        "  Установи LibreHardwareMonitor и запусти его в фоне:\n"
                        "  https://github.com/LibreHardwareMonitor/LibreHardwareMonitor"
                    )
        except Exception as e:
            lines.append(f"  ❌ CPU: {e}")

        return "\n".join(lines) if lines else "  ❌ Данные недоступны"

    def diag_text(self) -> str:
        lines: List[str] = ["🔧 Диагностика:"]
        lines.append(f"  i2v workflow: {'✅' if self.workflow_api_json.exists() else '❌ НЕ НАЙДЕН'}")
        lines.append(f"  txt2img workflow: {'✅' if self.txt2img_workflow_json.exists() else '❌ НЕ НАЙДЕН'}")
        lines.append(f"  comfy input: {'✅' if self.comfy_input_dir.exists() else '❌ НЕ НАЙДЕН'}")
        lines.append(f"  comfy output: {'✅' if self.comfy_output_dir.exists() else '❌ НЕ НАЙДЕН'}")
        try:
            usage = shutil.disk_usage(self.work_dir)
            free_gb = usage.free / (1024 ** 3)
            total_gb = usage.total / (1024 ** 3)
            icon = "✅" if free_gb >= self.min_free_disk_gb else "⚠️"
            lines.append(f"  {icon} Диск: {free_gb:.1f} GB / {total_gb:.0f} GB")
        except Exception as e:
            lines.append(f"  ❌ Диск: {e}")
        try:
            self.comfy_get("/history", timeout=10)
            running, pending = self.comfy_queue_size()
            lines.append(f"  ✅ ComfyUI: отвечает (очередь: {running}+{pending})")
        except Exception as e:
            lines.append(f"  ❌ ComfyUI: не отвечает — {e}")
        job = self.running_job()
        if job:
            elapsed = time.time() - job["started_at"]
            lines.append(f"  🎬 Текущая задача: {job['job_id']} [{job.get('job_type','?')}] ({self.human_duration(elapsed)})")
        else:
            lines.append("  ℹ️ Задача: не запущена")
        lines.append("")
        lines.append("🌡 Температуры:")
        lines.append(self._get_temperatures())
        return "\n".join(lines)
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Медиа из сообщения
    # ------------------------------------------------------------------

    def extract_media(self, message: Dict[str, Any]) -> Tuple[Optional[Tuple[str, str]], Optional[Tuple[str, str]]]:
        photo: Optional[Tuple[str, str]] = None
        video: Optional[Tuple[str, str]] = None
        if message.get("photo"):
            largest = message["photo"][-1]
            photo = (largest["file_id"], ".jpg")
        if message.get("video"):
            v = message["video"]
            video = (v["file_id"], self._guess_ext(v.get("file_name", "video.mp4"), ".mp4"))
        doc = message.get("document")
        if doc:
            mime = doc.get("mime_type", "")
            name = doc.get("file_name", "")
            ext = self._guess_ext(name, "")
            video_exts = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".gif"}
            if mime.startswith("video/") or ext in video_exts or ext in self.result_extensions:
                video = (doc["file_id"], ext or ".mp4")
            elif mime.startswith("image/"):
                photo = (doc["file_id"], ext or ".jpg")
        return photo, video

    def extract_frames(self, text: str) -> Optional[int]:
        m = re.fullmatch(r"\d+", text.strip())
        if m:
            val = int(m.group())
            if 1 <= val <= 10000:
                return val
        return None

    # ------------------------------------------------------------------
    # Команды
    # ------------------------------------------------------------------

    HELP_TEXT = (
        "📋 Команды:\n"
        "/img <промпт> — 🖼 генерация фото по тексту\n"
        "/start — приветствие\n"
        "/status — состояние задачи\n"
        "/frames <N> — кол-во кадров для i2v\n"
        "/reset — очистить ожидающие i2v файлы\n"
        "/cancel — прервать задачу\n"
        "/diag — диагностика\n"
        "/logs — последние строки лога\n"
        "/sendtest — тест отправки последнего видео\n"
        "/help — эта справка\n\n"
        "Для i2v (фото→видео):\n"
        "1️⃣ Отправь фото (портрет)\n"
        "2️⃣ Отправь видео (референс движения)\n"
        "3️⃣ Опционально: /frames N или просто число\n\n"
        "Для txt2img:\n"
        "Отправь /img твой промпт на английском"
    )

    def handle_command(self, chat_id: int, text: str) -> None:
        parts = text.strip().split()
        cmd = parts[0].lower().split("@")[0]

        if cmd == "/start":
            self.send_message(chat_id, "👋 Привет!\n\n" + self.HELP_TEXT)

        elif cmd == "/help":
            self.send_message(chat_id, self.HELP_TEXT)

        elif cmd == "/img":
            if len(parts) < 2:
                self.send_message(chat_id,
                    "⚠️ Укажи промпт после команды.\n"
                    "Пример: /img a beautiful woman in a park, photorealistic")
                return
            prompt = " ".join(parts[1:]).strip()
            job = self.running_job()
            if job:
                elapsed = time.time() - job["started_at"]
                raise BusyError(
                    f"Уже выполняется задача {job['job_id']} [{job.get('job_type','?')}] "
                    f"({self.human_duration(elapsed)} назад). Дождись завершения или /cancel."
                )
            self.send_message(chat_id, f"🖼 Запускаю генерацию фото...\nПромпт: {prompt[:100]}")
            self.submit_txt2img_job(chat_id, prompt)

        elif cmd == "/status":
            self.send_message(chat_id, self.status_text(chat_id))

        elif cmd == "/diag":
            self.send_message(chat_id, self.diag_text())

        elif cmd == "/logs":
            tail = self._log_tail(LOG_TAIL_LINES)
            if len(tail) > 3900:
                tail = "...(обрезано)...\n" + tail[-3800:]
            self.send_message(chat_id, f"📄 Лог:\n\n{tail}")

        elif cmd == "/reset":
            self.clear_pending(chat_id)
            self.send_message(chat_id, "🗑 Ожидающие i2v файлы очищены.")

        elif cmd == "/cancel":
            job = self.running_job()
            if not job:
                self.send_message(chat_id, "ℹ️ Нет активной задачи.")
                return
            self.comfy_interrupt()
            self.set_running_job(None)
            self.send_message(chat_id, f"⛔ Задача {job['job_id']} остановлена.")
            self._log(f"Cancel: {job['job_id']}")

        elif cmd == "/frames":
            if len(parts) < 2:
                pending = self.pending_for_chat(chat_id)
                self.send_message(chat_id,
                    f"Текущих кадров: {pending.get('frames', self.frames_default)}\n"
                    "Изменить: /frames <N>")
                return
            try:
                val = int(parts[1])
                if not (1 <= val <= 10000):
                    raise ValueError
            except ValueError:
                self.send_message(chat_id, "⚠️ Укажи целое число от 1 до 10000.")
                return
            pending = self.pending_for_chat(chat_id)
            pending["frames"] = val
            self._save_state()
            self.send_message(chat_id, f"✅ Кадров установлено: {val}")

        elif cmd == "/sendtest":
            found: List[Path] = []
            for root, _, files in os.walk(self.comfy_output_dir):
                for name in files:
                    if name.lower().endswith(self.result_extensions):
                        found.append(Path(root) / name)
            if not found:
                self.send_message(chat_id,
                    f"❌ Нет видеофайлов в {self.comfy_output_dir}")
                return
            found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            target = found[0]
            size_mb = target.stat().st_size / (1024 * 1024)
            self.send_message(chat_id,
                f"🔍 Найдено {len(found)} файлов.\nОтправляю: {target.name} ({size_mb:.1f} MB)")
            try:
                self.send_video(chat_id, target, caption=f"sendtest: {target.name}")
                self.send_message(chat_id, "✅ Отправка успешна!")
            except Exception as e:
                title, hint = self.classify_error(e)
                self.send_message(chat_id, f"❌ Ошибка: {e}\n{title}\n{hint}")

        else:
            self.send_message(chat_id, f"❓ Неизвестная команда.\n{self.HELP_TEXT}")

    # ------------------------------------------------------------------
    # i2v — сохранение pending медиа
    # ------------------------------------------------------------------

    def save_pending_media(self, chat_id: int,
                           photo_info: Optional[Tuple[str, str]],
                           video_info: Optional[Tuple[str, str]]) -> None:
        pending = self.pending_for_chat(chat_id)
        ts = int(time.time())
        if photo_info:
            file_id, ext = photo_info
            dest = self.download_dir / f"chat{chat_id}_{ts}_photo{ext}"
            self.download_telegram_file(file_id, dest)
            old = pending.get("photo_path")
            if old and Path(old).exists():
                try:
                    Path(old).unlink()
                except Exception:
                    pass
            pending["photo_path"] = str(dest)
        if video_info:
            file_id, ext = video_info
            dest = self.download_dir / f"chat{chat_id}_{ts}_video{ext}"
            self.download_telegram_file(file_id, dest)
            old = pending.get("video_path")
            if old and Path(old).exists():
                try:
                    Path(old).unlink()
                except Exception:
                    pass
            pending["video_path"] = str(dest)
        pending["updated_at"] = time.time()
        self._save_state()

    # ------------------------------------------------------------------
    # i2v — патч воркфлоу и запуск
    # ------------------------------------------------------------------

    def patch_i2v_workflow(self, job_id: str, person_name: str,
                           video_name: str, frames: int) -> Dict[str, Any]:
        with open(self.workflow_api_json, "r", encoding="utf-8") as f:
            wf = json.load(f)

        def check(nid: str, label: str) -> None:
            if nid not in wf:
                raise BotError(f"{label} node_id={nid} не найден в i2v workflow.")

        check(self.photo_node_id, "photo_node_id")
        check(self.video_node_id, "video_node_id")
        wf[self.photo_node_id]["inputs"][self.photo_input_field] = person_name
        wf[self.video_node_id]["inputs"][self.video_input_field] = video_name
        if self.frames_node_id:
            check(self.frames_node_id, "frames_node_id")
            wf[self.frames_node_id]["inputs"][self.frames_field] = frames
        if self.save_node_id:
            check(self.save_node_id, "save_node_id")
            wf[self.save_node_id]["inputs"][self.save_prefix_field] = f"tg_{job_id}"
        return wf

    def submit_i2v_job(self, chat_id: int) -> None:
        if self.running_job():
            job = self.running_job()
            elapsed = time.time() - job["started_at"]
            raise BusyError(
                f"Уже идёт задача {job['job_id']} [{job.get('job_type','?')}] "
                f"({self.human_duration(elapsed)}). Дождись завершения или /cancel."
            )
        pending = self.pending_for_chat(chat_id)
        photo_path = pending.get("photo_path")
        video_path = pending.get("video_path")
        missing = []
        if not photo_path:
            missing.append("📷 фото")
        if not video_path:
            missing.append("🎬 видео")
        if missing:
            raise BotError(f"Не хватает: {', '.join(missing)}")
        photo_src, video_src = Path(photo_path), Path(video_path)
        if not photo_src.exists():
            raise BotError(f"Файл фото не найден: {photo_src}")
        if not video_src.exists():
            raise BotError(f"Файл видео не найден: {video_src}")
        self.ensure_disk_space()
        frames = int(pending.get("frames", self.frames_default))
        job_id = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
        comfy_photo = self.comfy_input_dir / f"{job_id}_person{photo_src.suffix.lower()}"
        comfy_video = self.comfy_input_dir / f"{job_id}_drive{video_src.suffix.lower()}"
        shutil.copy2(photo_src, comfy_photo)
        shutil.copy2(video_src, comfy_video)
        wf = self.patch_i2v_workflow(job_id, comfy_photo.name, comfy_video.name, frames)
        client_id = str(uuid.uuid4())
        res = self.comfy_post("/prompt", {"prompt": wf, "client_id": client_id})
        prompt_id = res.get("prompt_id")
        if not prompt_id:
            raise BotError(f"ComfyUI не вернул prompt_id: {res}")
        job_data: Dict[str, Any] = {
            "job_type": "i2v",
            "job_id": job_id,
            "chat_id": chat_id,
            "prompt_id": prompt_id,
            "client_id": client_id,
            "started_at": time.time(),
            "person_image": str(comfy_photo),
            "driving_video": str(comfy_video),
            "prefix": f"tg_{job_id}",
            "frames": frames,
        }
        self.set_running_job(job_data)
        self.clear_pending(chat_id)
        self.last_progress_notify = time.time()
        self.send_message(chat_id,
            f"🚀 i2v рендер запущен!\n  job_id: {job_id}\n  Кадров: {frames}")
        self._log(f"i2v started: {job_id} prompt_id={prompt_id} frames={frames}")

    # ------------------------------------------------------------------
    # txt2img — патч воркфлоу и запуск
    # ------------------------------------------------------------------

    def patch_txt2img_workflow(self, job_id: str, prompt: str) -> Dict[str, Any]:
        with open(self.txt2img_workflow_json, "r", encoding="utf-8") as f:
            wf = json.load(f)

        def check(nid: str, label: str) -> None:
            if nid not in wf:
                raise BotError(f"{label} node_id={nid} не найден в txt2img workflow.")

        check(self.txt2img_prompt_node_id, "txt2img_prompt_node_id")
        wf[self.txt2img_prompt_node_id]["inputs"][self.txt2img_prompt_field] = prompt

        if self.txt2img_save_node_id:
            check(self.txt2img_save_node_id, "txt2img_save_node_id")
            wf[self.txt2img_save_node_id]["inputs"][self.txt2img_save_prefix_field] = f"tg_img_{job_id}"

        return wf

    def submit_txt2img_job(self, chat_id: int, prompt: str) -> None:
        if self.running_job():
            job = self.running_job()
            elapsed = time.time() - job["started_at"]
            raise BusyError(
                f"Уже идёт задача {job['job_id']} [{job.get('job_type','?')}] "
                f"({self.human_duration(elapsed)}). Дождись или /cancel."
            )
        self.ensure_disk_space()
        job_id = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
        wf = self.patch_txt2img_workflow(job_id, prompt)
        client_id = str(uuid.uuid4())
        res = self.comfy_post("/prompt", {"prompt": wf, "client_id": client_id})
        prompt_id = res.get("prompt_id")
        if not prompt_id:
            raise BotError(f"ComfyUI не вернул prompt_id: {res}")
        job_data: Dict[str, Any] = {
            "job_type": "txt2img",
            "job_id": job_id,
            "chat_id": chat_id,
            "prompt_id": prompt_id,
            "client_id": client_id,
            "started_at": time.time(),
            "prefix": f"tg_img_{job_id}",
            "prompt": prompt,
        }
        self.set_running_job(job_data)
        self.last_progress_notify = time.time()
        self.send_message(chat_id, f"✅ Генерация запущена! job_id: {job_id}")
        self._log(f"txt2img started: {job_id} prompt_id={prompt_id}")

    # ------------------------------------------------------------------
    # Поиск результата на диске
    # ------------------------------------------------------------------

    def _find_video_in_history(self, item: Any) -> Optional[str]:
        if isinstance(item, dict):
            for k, v in item.items():
                if k == "filename" and isinstance(v, str):
                    if v.lower().endswith(self.result_extensions):
                        return v
                found = self._find_video_in_history(v)
                if found:
                    return found
        elif isinstance(item, list):
            for v in item:
                found = self._find_video_in_history(v)
                if found:
                    return found
        return None

    def _find_image_in_history(self, item: Any) -> Optional[str]:
        if isinstance(item, dict):
            for k, v in item.items():
                if k == "filename" and isinstance(v, str):
                    if v.lower().endswith(self.txt2img_result_extensions):
                        return v
                found = self._find_image_in_history(v)
                if found:
                    return found
        elif isinstance(item, list):
            for v in item:
                found = self._find_image_in_history(v)
                if found:
                    return found
        return None

    def _find_newest_on_disk(self, extensions: Tuple[str, ...],
                              prefix: str, started_at: float) -> Optional[Path]:
        candidates: List[Path] = []
        all_found: List[str] = []
        for root, _, files in os.walk(self.comfy_output_dir):
            for name in files:
                if not name.lower().endswith(extensions):
                    continue
                p = Path(root) / name
                mtime = p.stat().st_mtime
                pm = prefix in name if prefix else True
                tm = mtime >= started_at - 5
                all_found.append(f"  {p} [prefix={pm} time={tm}]")
                if pm and tm:
                    candidates.append(p)
        if all_found:
            self._log(f"[DEBUG] search prefix={prefix!r}:\n" + "\n".join(all_found))
        else:
            self._log(f"[DEBUG] Нет файлов {extensions} в {self.comfy_output_dir}")
        if not candidates:
            self._log("[DEBUG] Нет кандидатов после фильтра.")
            return None
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        self._log(f"[DEBUG] Лучший кандидат: {candidates[0]}")
        return candidates[0]

    # ------------------------------------------------------------------
    # Проверка статуса запущенного задания
    # ------------------------------------------------------------------

    def check_running_job(self) -> None:
        job = self.running_job()
        if not job:
            return
        now = time.time()
        chat_id = int(job["chat_id"])
        job_type = job.get("job_type", "i2v")

        # Промежуточное уведомление
        if now - self.last_progress_notify >= PROGRESS_NOTIFY_INTERVAL:
            elapsed = now - job["started_at"]
            self.send_message(chat_id,
                f"⏳ {'Рендер' if job_type == 'i2v' else 'Генерация'} продолжается... "
                f"{self.human_duration(elapsed)} прошло.")
            self.last_progress_notify = now

        if now - self.last_history_check < self.poll_interval:
            return
        self.last_history_check = now

        prompt_id = job["prompt_id"]
        try:
            history = self.comfy_get(f"/history/{prompt_id}")
        except Exception as e:
            self._log(f"History check failed for {job['job_id']}: {e}")
            return

        item = history.get(prompt_id)
        if not item:
            return

        # Проверка на ошибку
        status_str = json.dumps(item.get("status", {}), ensure_ascii=False)
        if any(x in status_str.lower() for x in ["error", "failed", "exception"]):
            raise BotError(f"ComfyUI ошибка в статусе задачи: {status_str[:400]}")

        result_path: Optional[Path] = None

        if job_type == "i2v":
            filename = self._find_video_in_history(item)
            if filename:
                result_path = self._find_newest_on_disk(
                    self.result_extensions, Path(filename).stem, job["started_at"])
            if not result_path:
                result_path = self._find_newest_on_disk(
                    self.result_extensions, job["prefix"], job["started_at"])
            if not result_path:
                # fallback: любой новый видеофайл
                result_path = self._find_newest_on_disk(
                    self.result_extensions, "", job["started_at"])

            if result_path and result_path.exists():
                elapsed = time.time() - job["started_at"]
                size_mb = result_path.stat().st_size / (1024 * 1024)
                self.send_message(chat_id,
                    f"✅ Рендер завершён за {self.human_duration(elapsed)}! "
                    f"({size_mb:.1f} MB) Отправляю...")
                self.send_video(chat_id, result_path, caption=f"job: {job['job_id']}")
                self._log(f"i2v done: {job['job_id']} -> {result_path}")
                self.set_running_job(None)
            else:
                self._log(f"i2v history ready but file not found yet: {job['job_id']}")

        else:  # txt2img
            filename = self._find_image_in_history(item)
            if filename:
                result_path = self._find_newest_on_disk(
                    self.txt2img_result_extensions, Path(filename).stem, job["started_at"])
            if not result_path:
                result_path = self._find_newest_on_disk(
                    self.txt2img_result_extensions, job["prefix"], job["started_at"])
            if not result_path:
                # fallback: любое новое изображение
                result_path = self._find_newest_on_disk(
                    self.txt2img_result_extensions, "", job["started_at"])

            if result_path and result_path.exists():
                elapsed = time.time() - job["started_at"]
                self.send_message(chat_id,
                    f"✅ Генерация завершена за {self.human_duration(elapsed)}! Отправляю...")
                self.send_photo(chat_id, result_path, caption=f"job: {job['job_id']}")
                self._log(f"txt2img done: {job['job_id']} -> {result_path}")
                self.set_running_job(None)
            else:
                self._log(f"txt2img history ready but file not found yet: {job['job_id']}")

    # ------------------------------------------------------------------
    # Обработка входящего сообщения
    # ------------------------------------------------------------------

    def handle_message(self, message: Dict[str, Any]) -> None:
        chat_id = int(message["chat"]["id"])
        if chat_id != self.allowed_chat_id:
            return

        text = (message.get("text", "") or "").strip()
        caption = (message.get("caption", "") or "").strip()

        # --- Команды ---
        if text.startswith("/"):
            self.handle_command(chat_id, text)
            return

        # --- Медиа (i2v) ---
        photo_info, video_info = self.extract_media(message)
        if photo_info or video_info:
            job = self.running_job()
            if job:
                elapsed = time.time() - job["started_at"]
                self.send_message(chat_id,
                    f"⚠️ Идёт задача {job['job_id']} [{job.get('job_type','?')}] "
                    f"({self.human_duration(elapsed)}). Новые файлы не принимаю. /cancel")
                return
            self.save_pending_media(chat_id, photo_info, video_info)
            frames_from_caption = self.extract_frames(caption)
            if frames_from_caption:
                pending = self.pending_for_chat(chat_id)
                pending["frames"] = frames_from_caption
                self._save_state()
            pending = self.pending_for_chat(chat_id)
            have_photo = bool(pending.get("photo_path"))
            have_video = bool(pending.get("video_path"))
            frames_set = pending.get("frames", self.frames_default)
            if have_photo and have_video:
                self.send_message(chat_id,
                    f"✅ Фото и видео получены. Запускаю i2v...\nКадров: {frames_set}")
                self.submit_i2v_job(chat_id)
            elif have_photo:
                self.send_message(chat_id,
                    f"🖼 Фото получено. Пришли видео-референс.\nКадров: {frames_set}")
            elif have_video:
                self.send_message(chat_id,
                    f"📹 Видео получено. Пришли фото человека.\nКадров: {frames_set}")
            return

        # --- Просто число → кол-во кадров ---
        if text:
            frames = self.extract_frames(text)
            if frames is not None:
                pending = self.pending_for_chat(chat_id)
                pending["frames"] = frames
                self._save_state()
                self.send_message(chat_id, f"🎞 Кадров установлено: {frames}")
                return

        # --- Текст без медиа → подсказка ---
        self.send_message(chat_id,
            "ℹ️ Пришли фото + видео для i2v рендера.\n"
            "Для генерации фото по тексту используй /img <промпт>.\n"
            "/help — все команды.")

    # ------------------------------------------------------------------
    # Главный цикл
    # ------------------------------------------------------------------

    def run(self) -> None:
        offset = self._read_offset()
        self._log("Bot started")
        self.send_message(self.allowed_chat_id, "🤖 Бот запущен. /help — справка.")
        while True:
            try:
                self.check_running_job()
                updates = self.tg_get_updates(offset)
                for upd in updates:
                    offset = upd["update_id"] + 1
                    self._write_offset(offset)
                    message = upd.get("message") or upd.get("edited_message")
                    if not message:
                        continue
                    try:
                        self.handle_message(message)
                    except BusyError as e:
                        c = int(message["chat"]["id"])
                        if c == self.allowed_chat_id:
                            self.send_message(c, f"⚠️ {e}")
                        self._log(f"BusyError: {e}")
                    except Exception as e:
                        title, hint = self.classify_error(e)
                        c = int(message["chat"]["id"])
                        if c == self.allowed_chat_id:
                            self.send_message(c,
                                f"❌ Ошибка: {e}\n\n{title}\n{hint}")
                        self._log(f"Error [{title}]: {e}")
                        if self.running_job():
                            self.set_running_job(None)
                time.sleep(1)
            except KeyboardInterrupt:
                self._log("Bot stopped")
                break
            except Exception as e:
                title, hint = self.classify_error(e)
                self._log(f"Main loop error [{title}]: {e}")
                try:
                    self.send_message(self.allowed_chat_id,
                        f"🔴 Ошибка главного цикла: {e}\n\n{title}\n{hint}")
                except Exception:
                    pass
                time.sleep(5)


def main() -> None:
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.json"
    if not Path(config_path).exists():
        print(f"Ошибка: {config_path} не найден", file=sys.stderr)
        sys.exit(1)
    TelegramComfyBot(config_path).run()


if __name__ == "__main__":
    main()
