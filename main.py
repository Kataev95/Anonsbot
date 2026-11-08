"""IRISKA 3.0: один HTML-виджет с обратным отсчётом и автозакреплением."""
import json
import logging
import os
import time
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

LOG = logging.getLogger("anonsbot")
ROOT = Path(__file__).resolve().parent
MARKER = "🍬 IRISKA 3.0 • АНОНС"


def api(token, method, params=None, *, beta=False):
    prefix = "https://api.telegram.org/beta/bot" if beta else "https://api.telegram.org/bot"
    url = prefix + token + ("/test/" if beta else "/") + method
    request = Request(
        url,
        data=json.dumps(params or {}, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            data = json.load(response)
    except HTTPError as exc:
        info = exc.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"{method}: HTTP {exc.code} — {info}") from exc
    except URLError as exc:
        raise RuntimeError(f"{method}: сеть недоступна — {exc.reason}") from exc
    if not data.get("ok"):
        raise RuntimeError(f"{method}: {data.get('description', 'ошибка API')}")
    return data.get("result")


def state_file():
    directory = Path(os.getenv("STATE_DIR", "."))
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "announcement_state.json"


def read_state(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None


def save_state(path, value):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def target_time():
    raw = os.getenv("LAUNCH_AT", "").strip()
    if raw:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            raise ValueError("LAUNCH_AT должен содержать часовой пояс (+03:00)")
        if dt <= datetime.now(timezone.utc):
            raise ValueError("LAUNCH_AT уже прошёл")
        return dt
    return datetime.now(timezone.utc) + timedelta(days=10)


def make_html(launch):
    template = (ROOT / "widget.html").read_text(encoding="utf-8")
    html = template.replace("__TARGET_MS__", str(int(launch.timestamp() * 1000)))
    raw = html.encode("utf-8")
    packed = zlib.compress(raw, 9)
    LOG.info("Размер виджета: %s байт, zlib %s байт", len(raw), len(packed))
    if len(raw) > 32768 or len(packed) > 4070:
        raise ValueError("HTML-виджет превышает лимиты Telegram")
    return html


def check_chat(token, chat_id):
    me = api(token, "getMe")
    chat = api(token, "getChat", {"chat_id": chat_id})
    if chat.get("type") not in ("group", "supergroup"):
        raise ValueError("CHAT_ID должен указывать на группу/супергруппу")
    member = api(token, "getChatMember", {"chat_id": chat_id, "user_id": me["id"]})
    if member.get("status") not in ("administrator", "creator"):
        raise RuntimeError("Бот должен быть администратором группы")
    if member.get("status") == "administrator" and not member.get("can_pin_messages", False):
        raise RuntimeError("У бота нет разрешения закреплять сообщения")
    return chat, me


def is_our_pin(chat, me):
    pinned = chat.get("pinned_message") or {}
    return (pinned.get("from") or {}).get("id") == me["id"] and MARKER in pinned.get("caption", "")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    token = os.getenv("BOT_TOKEN", "").strip()
    chat_id = os.getenv("CHAT_ID", "").strip()
    if not token or not chat_id:
        raise ValueError("Укажите BOT_TOKEN и CHAT_ID в переменных хостинга")
    chat_key = int(chat_id) if chat_id.lstrip("-").isdigit() else chat_id
    state_path = state_file()
    saved = read_state(state_path)
    chat, me = check_chat(token, chat_key)

    if saved and str(saved.get("chat_id")) == str(chat_id):
        LOG.info("Запись уже есть: message_id=%s. Повторная отправка запрещена.", saved.get("message_id"))
        if not saved.get("pinned", False):
            try:
                api(token, "pinChatMessage", {"chat_id": chat_key, "message_id": saved["message_id"], "disable_notification": False})
                saved["pinned"] = True
                save_state(state_path, saved)
                LOG.info("✅ Повторное закрепление выполнено.")
            except RuntimeError as exc:
                LOG.warning("Закрепить пока не удалось: %s", exc)
    elif is_our_pin(chat, me):
        LOG.info("Наш анонс уже закреплён — дубль не публикуем.")
        saved = {"chat_id": chat_id, "message_id": chat["pinned_message"]["message_id"], "pinned": True}
        save_state(state_path, saved)
    else:
        launch = target_time()
        html = make_html(launch)
        beta = os.getenv("WIDGET_API_BETA", "1").strip() != "0"
        LOG.info("Отправляем HTML-виджет в %s (%s API)...", chat_id, "beta" if beta else "regular")
        msg = api(token, "sendWidget", {
            "chat_id": chat_key,
            "html": html,
            "width": 0,
            "height": 375,
            "caption": MARKER + "\n🐾 Возвращение питомцев через 10 дней!",
            "disable_notification": False,
        }, beta=beta)
        saved = {"chat_id": chat_id, "message_id": msg["message_id"], "launch_at": launch.isoformat(), "pinned": False}
        save_state(state_path, saved)  # обязательно ДО попытки закрепления
        LOG.info("Виджет отправлен: message_id=%s", msg["message_id"])
        try:
            api(token, "pinChatMessage", {
                "chat_id": chat_key,
                "message_id": msg["message_id"],
                "disable_notification": False,
            })
            saved["pinned"] = True
            save_state(state_path, saved)
            LOG.info("✅ Анонс закреплён!")
        except RuntimeError as exc:
            LOG.warning("Сообщение отправлено, но не закреплено: %s", exc)
            LOG.warning("Исправьте права и перезапустите контейнер. Не удаляйте STATE_DIR.")

    # Хостингам с политикой restart=always нужен живой процесс.
    # Постоянный опрос для таймера не нужен: анимация и таймер работают на клиенте.
    LOG.info("Анонс опубликован. Процесс оставлен активным без запросов к Telegram.")
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, OSError) as exc:
        LOG.error("Не удалось запустить анонс: %s", exc)
        raise SystemExit(1)
