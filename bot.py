#!/usr/bin/env python3
"""
TelegramComfyBot — Telegram-бот для управления ComfyUI i2v воркфлоу.

Команды:
  /start          — приветствие и список команд
  /status         — состояние рендера и ожидающих файлов
  /frames <N>     — задать количество кадров (без отправки медиа)
  /reset          — очистить ожидающие файлы (фото/видео/кадры)
  /cancel         — прервать текущий рендер
  /diag           — диагностика: ComfyUI, диск, пути
  /logs           — последние 40 строк лог-файла
  /help           — справка

Сценарий использования:
  1. Отправить фото (портрет человека)
  2. Отправить видео (референс движения)
  3. Опционально: отправить число (количество кадров) или /frames N
  4. Бот запустит воркфлоу и вернёт результат.
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


# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------

PROGRESS_NOTIFY_INTERVAL = 300   # секунды между промежуточными уведомлениями
MAX_VIDEO_SIZE_TG_MB = 49         # лимит Telegram на sendVideo (50 MB)
LOG_TAIL_LINES = 40               # сколько строк отдавать по /logs


# ---------------------------------------------------------------------------
# Пользовательские исключения
# ---------------------------------------------------------------------------

class BotError(Exception):
    """Общая ошибка бота — ловится в главном цикле."""


class BusyError(BotError):
    """ComfyUI занят — уже идёт рендер."""


# ---------------------------------------------------------------------------
# Основной класс бота
# ---------------------------------------------------------------------------

class TelegramComfyBot:

    # ------------------------------------------------------------------
    # Инициализация
    # ------------------------------------------------------------------

    def __init__(self, config_path: str) -> None:
        self.config = self._load_json(config_path)

        # Telegram
        self.token: str = self.config["telegram_bot_token"]
        self.allowed_chat_id: int = int(self.config["allowed_chat_id"])
        self.poll_timeout: int = int(self.config.get("poll_timeout_seconds", 25))
        self.poll_interval: int = int(self.config.get("status_poll_interval_seconds", 5))

        # Директории / пути
        self.base_url: str = self.config["comfyui_base_url"].rstrip("/")
        self.comfy_input_dir = Path(self.config["comfy_input_dir"]).expanduser()
        self.comfy_output_dir = Path(self.config["comfy_output_dir"]).expanduser()
        self.workflow_api_json = Path(self.config["workflow_api_json"]).expanduser()

        # Ноды воркфлоу
        self.photo_node_id: str = str(self.config["photo_node_id"])
        self.photo_input_field: str = self.config.get("photo_input_field", "image")
        self.video_node_id: str = str(self.config["video_node_id"])
        self.video_input_field: str = self.config.get("video_input_field", "video")
        self.save_node_id: Optional[str] = str(self.config.get("save_node_id", "")).strip() or None
        self.save_prefix_field: str = self.config.get("save_prefix_field", "filename_prefix")
        self.frames_node_id: Optional[str] = str(self.config.get("frames_node_id", "")).strip() or None
        self.frames_field: str = self.config.get("frames_field", "value")
        self.frames_default: int = int(self.config.get("frames_default", 150))

        # Расширения результатов
        self.result_extensions: Tuple[str, ...] = tuple(
            self.config.get("result_extensions", [".mp4", ".mov", ".webm", ".gif"])
        )

        # Минимальное свободное место
        self.min_free_disk_gb: float = float(self.config.get("min_free_disk_gb", 10))

        # Рабочие директории бота
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
    # Сериализация состояния
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

    # ------------------------------------------------------------------
    # Offset (для getUpdates)
    # ------------------------------------------------------------------

    def _read_offset(self) -> int:
        if self.offset_path.exists():
            try:
                return int(self.offset_path.read_text(encoding="utf-8").strip())
            except Exception:
                return 0
        return 0

    def _write_offset(self, value: int) -> None:
        self.offset_path.write_text(str(value), encoding="utf-8")

    # ------------------------------------------------------------------
    # Логирование
    # ------------------------------------------------------------------

    def _log(self, message: str) -> None:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {message}"
        print(line, flush=True)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def _log_tail(self, n: int = LOG_TAIL_LINES) -> str:
        """Вернуть последние n строк лог-файла."""
        if not self.log_path.exists():
            return "(лог пустой)"
        with open(self.log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        tail = lines[-n:]
        return "".join(tail).strip() or "(лог пустой)"

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

    def send_message(self, chat_id: int, text: str, parse_mode: str = "") -> None:
        params: Dict[str, Any] = {"chat_id": chat_id, "text": text}
        if parse_mode:
            params["parse_mode"] = parse_mode
        try:
            self.tg("sendMessage", params=params)
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
            # Если слишком большой или sendVideo не прошёл — отправляем как документ
            self.tg(
                "sendDocument",
                params={"chat_id": chat_id, "caption": caption},
                files={"document": f},
                timeout=600,
            )

    def download_telegram_file(self, file_id: str, dest: Path) -> Path:
        info = self.tg("getFile", params={"file_id": file_id})
        file_path = info["file_path"]
        url = f"https://api.telegram.org/file/bot{self.token}/{file_path}"
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
        """Возвращает (queue_running, queue_pending) — сколько задач в очереди."""
        try:
            data = self.comfy_get("/queue")
            running = len(data.get("queue_running", []))
            pending = len(data.get("queue_pending", []))
            return running, pending
        except Exception:
            return 0, 0

    # ------------------------------------------------------------------
    # Классификация ошибок
    # ------------------------------------------------------------------

    def classify_error(self, exc: Exception) -> Tuple[str, str]:
        """
        Возвращает (категория, совет).
        Категории:
          🟢 Решается удалённо     — ты сам можешь разобраться по SSH / TeamViewer
          🟡 Попроси родителей     — нужны простые действия у ноутбука
          🔴 Нужна ручная проверка — сложная или неизвестная ситуация
        """
        msg = str(exc).lower()
        full = repr(exc).lower()

        # --- Ошибки диска / места ---
        if any(x in msg for x in ["no space left", "disk full", "not enough space",
                                   "свободное место", "ниже лимита"]):
            return (
                "🟡 Попроси родителей",
                "На диске закончилось место. Попроси открыть папку ComfyUI/output "
                "и удалить старые видео. Или удали временные файлы в корзину.",
            )

        # --- ComfyUI не отвечает ---
        if any(x in msg for x in ["connection refused", "failed to establish",
                                   "max retries exceeded", "connect timeout",
                                   "remotedisconnected", "connectionerror"]):
            return (
                "🟢 Решается удалённо",
                "ComfyUI не запущен или завис. Подключись удалённо (TeamViewer / AnyDesk / SSH) "
                "и перезапусти ComfyUI. Проверь командой /diag.",
            )

        # --- VRAM / RAM ---
        if any(x in msg for x in ["cuda out of memory", "out of memory",
                                   "oom", "cudnn", "not enough memory"]):
            return (
                "🟢 Решается удалённо",
                "Не хватает видеопамяти (VRAM). Попробуй: \n"
                "• Уменьшить количество кадров (/frames)\n"
                "• Перезапустить ComfyUI (очистится VRAM)\n"
                "• Закрыть другие программы с GPU.",
            )

        # --- Ошибка воркфлоу / нод ---
        if any(x in msg for x in ["workflow", "node", "invalid prompt",
                                   "node id", "not found in workflow"]):
            return (
                "🟢 Решается удалённо",
                "Ошибка конфигурации воркфлоу. Проверь: \n"
                "• Правильность node_id в config.json\n"
                "• Актуальность i2v.json (экспортируй API JSON заново из ComfyUI).",
            )

        # --- Отсутствует файл ---
        if any(x in msg for x in ["no such file", "file not found",
                                   "filenotfounderror", "не найден"]):
            return (
                "🟢 Решается удалённо",
                "Файл воркфлоу или входной файл не найден. Проверь пути в config.json "
                "и что workflow_api_json существует.",
            )

        # --- Telegram API ---
        if any(x in msg for x in ["telegram api", "getfile", "sendvideo",
                                   "senddocument", "file too large"]):
            return (
                "🟢 Решается удалённо",
                "Проблема с Telegram API или отправкой файла. Обычно проходит само — "
                "попробуй повторить команду через минуту.",
            )

        # --- Питание / железо ---
        if any(x in msg for x in ["power", "overheat", "thermal", "shutdown",
                                   "battery", "hardware"]):
            return (
                "🟡 Попроси родителей",
                "Возможна проблема с питанием или перегревом ноутбука. Попроси родителей "
                "проверить, что ноутбук включён, вентиляция не перекрыта, зарядка подключена.",
            )

        # --- Неизвестная ---
        return (
            "🔴 Нужна ручная проверка",
            "Нестандартная ошибка. Что делать:\n"
            "1. Посмотри лог командой /logs\n"
            "2. Попроси родителей сфотографировать экран ноутбука и прислать скриншот\n"
            "3. Если ноутбук завис — можно попросить перезагрузить.",
        )

    # ------------------------------------------------------------------
    # Диск
    # ------------------------------------------------------------------

    def ensure_disk_space(self) -> None:
        usage = shutil.disk_usage(self.work_dir)
        free_gb = usage.free / (1024 ** 3)
        if free_gb < self.min_free_disk_gb:
            raise BotError(
                f"Свободное место на диске ниже лимита: {free_gb:.1f} GB "
                f"(минимум {self.min_free_disk_gb} GB)"
            )

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
    # Вспомогательные форматтеры
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
            lines.append(f"🎬 Идёт рендер: {job['job_id']}")
            lines.append(f"⏱ Длительность: {self.human_duration(elapsed)}")
            lines.append(f"🖼 Фото: {'✅' if job.get('person_image') else '❌'}")
            lines.append(f"📹 Видео: {'✅' if job.get('driving_video') else '❌'}")
            lines.append(f"🎞 Кадров: {job.get('frames', self.frames_default)}")
        else:
            lines.append("✅ Рендер не запущен.")

        if pending:
            lines.append("")
            lines.append("Ожидают запуска:")
            lines.append(f"  🖼 Фото: {'✅' if pending.get('photo_path') else '❌'}")
            lines.append(f"  📹 Видео: {'✅' if pending.get('video_path') else '❌'}")
            lines.append(f"  🎞 Кадров: {pending.get('frames', self.frames_default)}")
        return "\n".join(lines)

    def diag_text(self) -> str:
        lines: List[str] = ["🔧 Диагностика:"]

        # Пути
        lines.append(f"  workflow JSON: {'✅ OK' if self.workflow_api_json.exists() else '❌ НЕ НАЙДЕН'}")
        lines.append(f"  comfy input: {'✅ OK' if self.comfy_input_dir.exists() else '❌ НЕ НАЙДЕН'}")
        lines.append(f"  comfy output: {'✅ OK' if self.comfy_output_dir.exists() else '❌ НЕ НАЙДЕН'}")

        # Диск
        try:
            usage = shutil.disk_usage(self.work_dir)
            free_gb = usage.free / (1024 ** 3)
            total_gb = usage.total / (1024 ** 3)
            icon = "✅" if free_gb >= self.min_free_disk_gb else "⚠️"
            lines.append(f"  {icon} Диск: {free_gb:.1f} GB свободно / {total_gb:.0f} GB всего")
        except Exception as e:
            lines.append(f"  ❌ Диск: ошибка ({e})")

        # ComfyUI
        try:
            self.comfy_get("/history", timeout=10)
            running, pending = self.comfy_queue_size()
            lines.append(f"  ✅ ComfyUI: отвечает (очередь: {running} исполняется, {pending} ожидает)")
        except Exception as e:
            lines.append(f"  ❌ ComfyUI: не отвечает — {e}")

        # Текущий рендер
        job = self.running_job()
        if job:
            elapsed = time.time() - job["started_at"]
            lines.append(f"  🎬 Текущий рендер: {job['job_id']} ({self.human_duration(elapsed)})")
        else:
            lines.append("  ℹ️ Рендер: не запущен")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Обработка медиа из сообщения
    # ------------------------------------------------------------------

    def extract_media(
        self, message: Dict[str, Any]
    ) -> Tuple[Optional[Tuple[str, str]], Optional[Tuple[str, str]]]:
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
        """Попытаться прочитать число кадров из текста сообщения."""
        text = text.strip()
        m = re.fullmatch(r"\d+", text)
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
        "/start — приветствие\n"
        "/status — состояние рендера\n"
        "/frames <N> — задать кол-во кадров (например, /frames 120)\n"
        "/reset — очистить ожидающие файлы\n"
        "/cancel — прервать текущий рендер\n"
        "/diag — диагностика\n"
        "/logs — последние строки лога\n"
        "/help — эта справка\n\n"
        "Чтобы запустить рендер:\n"
        "1️⃣ Отправь фото (портрет)\n"
        "2️⃣ Отправь видео (референс движения)\n"
        "3️⃣ Опционально: число кадров (/frames N или просто цифра)\n"
        "Бот сам запустит рендер и пришлёт результат."
    )

    def handle_command(self, chat_id: int, text: str) -> None:
        parts = text.strip().split()
        cmd = parts[0].lower().split("@")[0]  # убираем @botname если есть

        if cmd == "/start":
            self.send_message(
                chat_id,
                "👋 Привет! Я управляю ComfyUI i2v воркфлоу.\n\n" + self.HELP_TEXT,
            )

        elif cmd == "/help":
            self.send_message(chat_id, self.HELP_TEXT)

        elif cmd == "/status":
            self.send_message(chat_id, self.status_text(chat_id))

        elif cmd == "/diag":
            self.send_message(chat_id, self.diag_text())

        elif cmd == "/logs":
            tail = self._log_tail(LOG_TAIL_LINES)
            # Telegram ограничивает 4096 символов
            if len(tail) > 3900:
                tail = "...(обрезано)...\n" + tail[-3800:]
            self.send_message(chat_id, f"📄 Лог (последние {LOG_TAIL_LINES} строк):\n\n{tail}")

        elif cmd == "/reset":
            self.clear_pending(chat_id)
            self.send_message(chat_id, "🗑 Ожидающие файлы и настройки очищены. Текущий рендер не тронут.")

        elif cmd == "/cancel":
            job = self.running_job()
            if not job:
                self.send_message(chat_id, "ℹ️ Сейчас нет активного рендера.")
                return
            self.comfy_interrupt()
            self.set_running_job(None)
            self.send_message(
                chat_id,
                f"⛔ Запрос на остановку отправлен. Рендер {job['job_id']} должен остановиться "
                "в течение нескольких секунд."
            )
            self._log(f"Cancel requested for job {job['job_id']}")

        elif cmd == "/frames":
            if len(parts) < 2:
                pending = self.pending_for_chat(chat_id)
                cur = pending.get("frames", self.frames_default)
                self.send_message(
                    chat_id,
                    f"Текущее количество кадров: {cur}\n"
                    f"Чтобы изменить: /frames <число>, например /frames 120"
                )
                return
            try:
                val = int(parts[1])
                if not (1 <= val <= 10000):
                    raise ValueError
            except ValueError:
                self.send_message(chat_id, "⚠️ Укажи целое число от 1 до 10000. Пример: /frames 150")
                return
            pending = self.pending_for_chat(chat_id)
            pending["frames"] = val
            self._save_state()
            self.send_message(chat_id, f"✅ Кол-во кадров установлено: {val}")
        elif cmd == "/sendtest":
            # Ищем любой последний видеофайл в output-папке
            found = []
            for root, _, files in os.walk(self.comfy_output_dir):
                for name in files:
                    if name.lower().endswith(self.result_extensions):
                        p = Path(root) / name
                        found.append(p)
            if not found:
                self.send_message(
                    chat_id,
                    f"❌ В папке {self.comfy_output_dir} нет ни одного видеофайла "
                    f"с расширениями {self.result_extensions}"
                )
                return
            found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            target = found[0]
            size_mb = target.stat().st_size / (1024 * 1024)
            self.send_message(
                chat_id,
                f"🔍 Найдено {len(found)} видеофайлов.\n"
                f"Отправляю последний:\n{target}\n"
                f"Размер: {size_mb:.1f} MB"
            )
            try:
                self.send_video(chat_id, target, caption=f"sendtest: {target.name}")
                self.send_message(chat_id, "✅ Отправка прошла успешно!")
            except Exception as e:
                title, hint = self.classify_error(e)
                self.send_message(
                    chat_id,
                    f"❌ Ошибка при отправке: {e}\n\nКатегория: {title}\nЧто делать: {hint}"
                )
        else:
            self.send_message(chat_id, f"❓ Неизвестная команда. {self.HELP_TEXT}")

    # ------------------------------------------------------------------
    # Сохранение медиа в pending
    # ------------------------------------------------------------------

    def save_pending_media(
        self,
        chat_id: int,
        photo_info: Optional[Tuple[str, str]],
        video_info: Optional[Tuple[str, str]],
    ) -> None:
        pending = self.pending_for_chat(chat_id)
        ts = int(time.time())
        if photo_info:
            file_id, ext = photo_info
            dest = self.download_dir / f"chat{chat_id}_{ts}_photo{ext}"
            self.download_telegram_file(file_id, dest)
            # Удалить старый файл если есть
            old = pending.get("photo_path")
            if old and Path(old).exists() and old != str(dest):
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
            if old and Path(old).exists() and old != str(dest):
                try:
                    Path(old).unlink()
                except Exception:
                    pass
            pending["video_path"] = str(dest)
        pending["updated_at"] = time.time()
        self._save_state()

    # ------------------------------------------------------------------
    # Патчинг воркфлоу
    # ------------------------------------------------------------------

    def patch_workflow(
        self, job_id: str, person_image_name: str, driving_video_name: str, frames: int
    ) -> Dict[str, Any]:
        with open(self.workflow_api_json, "r", encoding="utf-8") as f:
            workflow = json.load(f)

        def _check_node(node_id: str, label: str) -> None:
            if node_id not in workflow:
                raise BotError(
                    f"{label} node_id={node_id} не найден в workflow JSON. "
                    "Проверь config.json и актуальность i2v.json."
                )

        _check_node(self.photo_node_id, "photo_node_id")
        _check_node(self.video_node_id, "video_node_id")

        workflow[self.photo_node_id]["inputs"][self.photo_input_field] = person_image_name
        workflow[self.video_node_id]["inputs"][self.video_input_field] = driving_video_name

        # Кол-во кадров
        if self.frames_node_id:
            _check_node(self.frames_node_id, "frames_node_id")
            workflow[self.frames_node_id]["inputs"][self.frames_field] = frames

        # Префикс для сохранения
        prefix = f"tg_{job_id}"
        if self.save_node_id:
            _check_node(self.save_node_id, "save_node_id")
            workflow[self.save_node_id]["inputs"][self.save_prefix_field] = prefix

        return workflow

    # ------------------------------------------------------------------
    # Запуск рендера
    # ------------------------------------------------------------------

    def submit_job(self, chat_id: int) -> None:
        # Проверка: уже идёт рендер?
        job = self.running_job()
        if job:
            elapsed = time.time() - job["started_at"]
            raise BusyError(
                f"Уже идёт рендер {job['job_id']} (запущен {self.human_duration(elapsed)} назад). "
                "Дождись завершения или останови его командой /cancel."
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

        photo_src = Path(photo_path)
        video_src = Path(video_path)
        if not photo_src.exists():
            raise BotError(f"Файл фото не найден на диске: {photo_src}")
        if not video_src.exists():
            raise BotError(f"Файл видео не найден на диске: {video_src}")

        self.ensure_disk_space()

        frames = int(pending.get("frames", self.frames_default))
        job_id = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]

        # Копируем файлы в comfy/input
        comfy_photo_name = f"{job_id}_person{photo_src.suffix.lower()}"
        comfy_video_name = f"{job_id}_drive{video_src.suffix.lower()}"
        comfy_photo_path = self.comfy_input_dir / comfy_photo_name
        comfy_video_path = self.comfy_input_dir / comfy_video_name
        shutil.copy2(photo_src, comfy_photo_path)
        shutil.copy2(video_src, comfy_video_path)

        # Патчим и отправляем воркфлоу
        workflow = self.patch_workflow(job_id, comfy_photo_name, comfy_video_name, frames)
        client_id = str(uuid.uuid4())
        res = self.comfy_post("/prompt", {"prompt": workflow, "client_id": client_id})
        prompt_id = res.get("prompt_id")
        if not prompt_id:
            raise BotError(f"ComfyUI не вернул prompt_id. Ответ: {res}")

        job_data: Dict[str, Any] = {
            "job_id": job_id,
            "chat_id": chat_id,
            "prompt_id": prompt_id,
            "client_id": client_id,
            "started_at": time.time(),
            "person_image": str(comfy_photo_path),
            "driving_video": str(comfy_video_path),
            "prefix": f"tg_{job_id}",
            "frames": frames,
        }
        self.set_running_job(job_data)
        self.clear_pending(chat_id)
        self.last_progress_notify = time.time()

        self.send_message(
            chat_id,
            f"🚀 Рендер запущен!\n"
            f"  job_id: {job_id}\n"
            f"  Кадров: {frames}\n"
            f"Буду уведомлять о прогрессе. Проверяй /status."
        )
        self._log(f"Started job {job_id} prompt_id={prompt_id} frames={frames}")

    # ------------------------------------------------------------------
    # Поиск результата в истории ComfyUI
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

    def _find_newest_result_on_disk(self, prefix: str, started_at: float) -> Optional[Path]:
        candidates: List[Path] = []
        for root, _, files in os.walk(self.comfy_output_dir):
            for name in files:
                if not name.lower().endswith(self.result_extensions):
                    continue
                if prefix not in name:
                    continue
                p = Path(root) / name
                if p.stat().st_mtime >= started_at - 5:
                    candidates.append(p)
        if not candidates:
            return None
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
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

        # Промежуточное уведомление о прогрессе
        if now - self.last_progress_notify >= PROGRESS_NOTIFY_INTERVAL:
            elapsed = now - job["started_at"]
            self.send_message(
                chat_id,
                f"⏳ Рендер продолжается... {self.human_duration(elapsed)} прошло. "
                f"Используй /status для подробностей или /cancel для остановки."
            )
            self.last_progress_notify = now

        # Проверяем историю не чаще чем раз в poll_interval секунд
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
            return  # Ещё не завершено

        # Ищем результат: сначала в ответе истории, потом на диске
        result_path: Optional[Path] = None
        filename = self._find_video_in_history(item)
        if filename:
            result_path = self._find_newest_result_on_disk(
                Path(filename).stem, job["started_at"]
            )
        if not result_path:
            result_path = self._find_newest_result_on_disk(job["prefix"], job["started_at"])
        if not result_path:
            # Fallback: любой новый видеофайл появившийся после старта рендера
            result_path = self._find_newest_result_on_disk("", job["started_at"])

        # Проверяем на ошибку в статусе
        status = item.get("status", {})
        status_str = json.dumps(status, ensure_ascii=False)
        if any(x in status_str.lower() for x in ["error", "failed", "exception"]):
            raise BotError(f"ComfyUI вернул ошибку в статусе задачи: {status_str[:500]}")

        if result_path and result_path.exists():
            elapsed = time.time() - job["started_at"]
            size_mb = result_path.stat().st_size / (1024 * 1024)
            self.send_message(
                chat_id,
                f"✅ Рендер завершён за {self.human_duration(elapsed)}!\n"
                f"Размер файла: {size_mb:.1f} MB. Отправляю..."
            )
            self.send_video(chat_id, result_path, caption=f"job_id: {job['job_id']}")
            self._log(f"Finished job {job['job_id']} -> {result_path} ({size_mb:.1f} MB)")
            self.set_running_job(None)
        else:
            self._log(f"History present for {job['job_id']} but no result file found yet.")

    # ------------------------------------------------------------------
    # Обработка входящего сообщения
    # ------------------------------------------------------------------

    def handle_message(self, message: Dict[str, Any]) -> None:
        chat_id = int(message["chat"]["id"])
        if chat_id != self.allowed_chat_id:
            return

        text = message.get("text", "") or ""
        caption = message.get("caption", "") or ""
        full_text = (text + " " + caption).strip()

        # --- Команды ---
        if text.startswith("/"):
            self.handle_command(chat_id, text)
            return

        # --- Медиа ---
        photo_info, video_info = self.extract_media(message)

        # --- Если только текст без медиа ---
        if not photo_info and not video_info:
            # Может быть числом (кол-во кадров)?
            frames = self.extract_frames(full_text)
            if frames is not None:
                pending = self.pending_for_chat(chat_id)
                pending["frames"] = frames
                self._save_state()
                self.send_message(
                    chat_id,
                    f"🎞 Кол-во кадров установлено: {frames}\n"
                    "Теперь пришли фото и видео для запуска рендера."
                )
            else:
                self.send_message(
                    chat_id,
                    "ℹ️ Пришли фото и видео для запуска рендера.\n"
                    "Или используй /help для списка команд."
                )
            return

        # --- Уже идёт рендер — не принимаем новые файлы ---
        job = self.running_job()
        if job:
            elapsed = time.time() - job["started_at"]
            self.send_message(
                chat_id,
                f"⚠️ Сейчас идёт рендер {job['job_id']} "
                f"(запущен {self.human_duration(elapsed)} назад).\n"
                "Новые файлы не принимаю до завершения. "
                "Используй /status или /cancel."
            )
            return

        # --- Сохраняем медиа ---
        self.save_pending_media(chat_id, photo_info, video_info)

        # Попытаться прочитать кол-во кадров из подписи
        frames_from_caption = self.extract_frames(caption)
        if frames_from_caption is not None:
            pending = self.pending_for_chat(chat_id)
            pending["frames"] = frames_from_caption
            self._save_state()

        pending = self.pending_for_chat(chat_id)
        have_photo = bool(pending.get("photo_path"))
        have_video = bool(pending.get("video_path"))
        frames_set = pending.get("frames", self.frames_default)

        if have_photo and have_video:
            self.send_message(
                chat_id,
                f"✅ Фото и видео получены. Запускаю рендер...\n"
                f"Кадров: {frames_set} (изменить: /frames N)"
            )
            self.submit_job(chat_id)
        elif have_photo:
            self.send_message(
                chat_id,
                f"🖼 Фото получено. Теперь пришли видео-референс.\n"
                f"Кадров: {frames_set} (изменить: /frames N)"
            )
        elif have_video:
            self.send_message(
                chat_id,
                f"📹 Видео получено. Теперь пришли фото человека.\n"
                f"Кадров: {frames_set} (изменить: /frames N)"
            )

    # ------------------------------------------------------------------
    # Главный цикл
    # ------------------------------------------------------------------

    def run(self) -> None:
        offset = self._read_offset()
        self._log("Bot started")
        self.send_message(
            self.allowed_chat_id,
            "🤖 Бот запущен и готов к работе. Введи /help для справки."
        )

        while True:
            try:
                # Проверяем статус запущенного задания
                self.check_running_job()

                # Получаем обновления
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
                        chat_id = int(message["chat"]["id"])
                        if chat_id == self.allowed_chat_id:
                            self.send_message(chat_id, f"⚠️ {e}")
                        self._log(f"BusyError: {e}")
                    except Exception as e:
                        title, hint = self.classify_error(e)
                        chat_id = int(message["chat"]["id"])
                        if chat_id == self.allowed_chat_id:
                            self.send_message(
                                chat_id,
                                f"❌ Ошибка: {e}\n\n"
                                f"Категория: {title}\n"
                                f"Что делать: {hint}"
                            )
                        self._log(f"Message handling error [{title}]: {e}")
                        job = self.running_job()
                        if job:
                            self.set_running_job(None)

                time.sleep(1)

            except KeyboardInterrupt:
                self._log("Bot stopped by user (Ctrl+C)")
                break
            except Exception as e:
                title, hint = self.classify_error(e)
                self._log(f"Main loop error [{title}]: {e}")
                try:
                    self.send_message(
                        self.allowed_chat_id,
                        f"🔴 Ошибка в главном цикле: {e}\n\n"
                        f"Категория: {title}\n"
                        f"Что делать: {hint}"
                    )
                except Exception:
                    pass
                time.sleep(5)


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

def main() -> None:
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.json"
    if not Path(config_path).exists():
        print(f"Ошибка: файл конфигурации не найден: {config_path}", file=sys.stderr)
        sys.exit(1)
    bot = TelegramComfyBot(config_path)
    bot.run()


if __name__ == "__main__":
    main()
