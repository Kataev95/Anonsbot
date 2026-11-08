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
    if member.get("status") in ("left", "kicked"):
        raise RuntimeError("Бот не состоит в группе")
    can_pin = member.get("status") == "creator" or (
        member.get("status") == "administrator" and member.get("can_pin_messages", False)
    )
    return chat, me, can_pin


def is_our_pin(chat, me):
    pinned = chat.get("pinned_message") or {}
    return (pinned.get("from") or {}).get("id") == me["id"] and MARKER in pinned.get("caption", "")


def parse_chat_ids():
    raw = os.getenv("CHAT_IDS", "").strip() or os.getenv("CHAT_ID", "").strip()
    ids = list(dict.fromkeys(part.strip() for part in raw.replace(";", ",").split(",") if part.strip()))
    if not ids:
        raise ValueError("Укажите CHAT_IDS=-100111,-100222 или CHAT_ID=-100111")
    return ids


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise ValueError("Укажите BOT_TOKEN")
    chat_ids = parse_chat_ids()
    state_path = state_file()
    saved = read_state(state_path) or {}
    if "chats" not in saved:
        old = saved
        saved = {"chats": {}}
        if old.get("chat_id") and old.get("message_id"):
            saved["chats"][str(old["chat_id"])] = {
                "message_id": old["message_id"], "pinned": old.get("pinned", False)
            }
            if old.get("launch_at"):
                saved["launch_at"] = old["launch_at"]

    if saved.get("launch_at"):
        launch = datetime.fromisoformat(saved["launch_at"])
    else:
        launch = target_time()
        saved["launch_at"] = launch.isoformat()
        save_state(state_path, saved)
    html = make_html(launch)
    beta = os.getenv("WIDGET_API_BETA", "1").strip() != "0"
    if beta:
        LOG.warning(
            "Используется beta Bot API. Обычный токен может работать в getMe, "
            "но возвращать 401 на beta/test/sendWidget, если нет доступа к бете."
        )

    for chat_id in chat_ids:
        key = int(chat_id) if chat_id.lstrip("-").isdigit() else chat_id
        try:
            chat, me, can_pin = check_chat(token, key)
            record = saved["chats"].get(chat_id)
            if record:
                LOG.info("%s: уже отправлено сообщение %s, дубликат не создаём.", chat_id, record["message_id"])
                if record.get("pinned") or not can_pin:
                    if not can_pin:
                        LOG.info("%s: права на закрепление нет, сообщение уже опубликовано.", chat_id)
                    continue
                message_id = record["message_id"]
            elif is_our_pin(chat, me):
                message_id = chat["pinned_message"]["message_id"]
                saved["chats"][chat_id] = {"message_id": message_id, "pinned": True}
                save_state(state_path, saved)
                LOG.info("%s: анонс уже закреплён.", chat_id)
                continue
            else:
                LOG.info("%s: отправляем HTML-виджет...", chat_id)
                msg = api(token, "sendWidget", {
                    "chat_id": key, "html": html, "width": 0, "height": 375,
                    "caption": MARKER + "\n🐾 Возвращение питомцев уже близко!",
                    "disable_notification": False,
                }, beta=beta)
                message_id = msg["message_id"]
                saved["chats"][chat_id] = {"message_id": message_id, "pinned": False}
                save_state(state_path, saved)
                LOG.info("%s: отправлено сообщение %s", chat_id, message_id)
            if can_pin:
                try:
                    api(token, "pinChatMessage", {
                        "chat_id": key, "message_id": message_id, "disable_notification": False
                    })
                    saved["chats"][chat_id]["pinned"] = True
                    save_state(state_path, saved)
                    LOG.info("%s: ✅ анонс закреплён!", chat_id)
                except RuntimeError as exc:
                    LOG.warning("%s: сообщение отправлено, закрепить не удалось: %s", chat_id, exc)
            else:
                LOG.info("%s: ✅ анонс отправлен без закрепления (нет права закреплять).", chat_id)
        except (RuntimeError, ValueError, OSError) as exc:
            LOG.error("%s: ошибка: %s", chat_id, exc)
            if beta and "sendWidget: HTTP 401" in str(exc):
                LOG.error(
                    "Обычный getMe уже прошёл: токен действителен для стандартного Bot API. "
                    "Но beta-эндпоинт отклонил его. Это не ошибка CHAT_ID и не прав "
                    "администратора. Проверьте доступ к beta Bot API или попробуйте "
                    "WIDGET_API_BETA=0 только если sendWidget уже опубликован в "
                    "основном API."
                )
            # Остальные чаты не должны зависеть от ошибки в одном чате.

    LOG.info("Обработка %d чатов завершена. Процесс остаётся активным.", len(chat_ids))
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, OSError) as exc:
        LOG.error("Не удалось запустить анонс: %s", exc)
        raise SystemExit(1)
