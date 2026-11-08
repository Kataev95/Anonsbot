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
FAILED_ATTEMPTS = {}
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
    if chat.get("type") == "private":
        return chat, me, False
    if chat.get("type") not in ("group", "supergroup"):
        raise ValueError("Нужен личный чат, группа или супергруппа")
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
    private = os.getenv("PRIVATE_TEST_CHAT_ID", "").strip()
    if private:
        if not private.isdigit():
            raise ValueError("PRIVATE_TEST_CHAT_ID должен быть положительным числовым Telegram ID")
        LOG.info("🧪 Включён тест личного чата: групповая рассылка временно отключена.")
        return [private]
    raw = os.getenv("CHAT_IDS", "").strip() or os.getenv("CHAT_ID", "").strip()
    ids = list(dict.fromkeys(part.strip() for part in raw.replace(";", ",").split(",") if part.strip()))
    if not ids:
        raise ValueError("Укажите CHAT_IDS=-100111,-100222 или CHAT_ID=-100111")
    return ids


def parse_allowed_chats():
    raw = os.getenv("CHAT_IDS", "").strip() or os.getenv("CHAT_ID", "").strip()
    if not raw:
        return set()
    return {v.strip() for v in raw.replace(";", ",").split(",") if v.strip()}


def handle_command(token, msg, username, state, path, allowed):
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    user = msg.get("from") or {}
    body = (msg.get("text") or "").strip()
    if not chat_id or not body or not user or user.get("is_bot"):
        return
    command = body.split(maxsplit=1)[0]
    action = command.split("@", 1)[0].lower()
    recipient = command.split("@", 1)[1].lower() if "@" in command else ""
    if action not in ("/start", "/anons") or (recipient and recipient != username.lower()):
        return
    kind = chat.get("type")
    if kind not in ("private", "group", "supergroup"):
        return
    chat_str = str(chat_id)
    if kind != "private" and allowed and chat_str not in allowed:
        LOG.warning("%s: команда из чата, которого нет в CHAT_IDS", chat_str)
        return
    if kind != "private":
        member = api(token, "getChatMember", {"chat_id": chat_id, "user_id": user["id"]})
        if member.get("status") not in ("creator", "administrator"):
            LOG.info("%s: команда пользователя без админских прав; игнорируем", chat_str)
            return
    version = os.getenv("WIDGET_VERSION", "2").strip() or "2"
    # Не повторять отклонённый Telegram запрос при серии /start.
    last_failed = FAILED_ATTEMPTS.get(chat_str, 0)
    if time.monotonic() - last_failed < 120:
        LOG.info("%s: повторная команда пропущена (120 секунд после ошибки).", chat_str)
        return
    record = state["chats"].get(chat_str)
    if record and str(record.get("version", "1")) == version:
        LOG.info("%s: сообщение версии %s уже существует; без повторного сообщения.", chat_str, version)
        return
    try:
        launch = datetime.fromisoformat(state["launch_at"])
        html = make_html(launch)
        beta = os.getenv("WIDGET_API_BETA", "0").strip() != "0"
        response = api(token, "sendWidget", {
            "chat_id": chat_id,
            "html": html,
            "width": 0,
            "height": 375,
            "caption": MARKER + "\n🐾 Питомцы готовятся к возвращению!",
        }, beta=beta)
    except RuntimeError as exc:
        LOG.error("%s: sendWidget отклонён: %s", chat_str, exc)
        FAILED_ATTEMPTS[chat_str] = time.monotonic()
        # Не засоряем групповые чаты диагностическими сообщениями.
        # Ошибка видна только в журналах хостинга.
        return
    message_id = response["message_id"]
    state["chats"][chat_str] = {
        "message_id": message_id, "pinned": False, "version": version
    }
    save_state(path, state)
    LOG.info("%s: ✅ HTML-виджет опубликован, message_id=%s", chat_str, message_id)
    if kind in ("group", "supergroup"):
        try:
            own = api(token, "getMe")
            member = api(token, "getChatMember", {
                "chat_id": chat_id, "user_id": own["id"]
            })
            can_pin = member.get("status") == "creator" or (
                member.get("status") == "administrator" and member.get("can_pin_messages", False)
            )
            if can_pin:
                api(token, "pinChatMessage", {
                    "chat_id": chat_id, "message_id": message_id,
                    "disable_notification": False
                })
                state["chats"][chat_str]["pinned"] = True
                save_state(path, state)
                LOG.info("%s: ✅ закреплено", chat_str)
            else:
                LOG.info("%s: опубликовано без закрепления", chat_str)
        except RuntimeError as exc:
            LOG.warning("%s: виджет опубликован, закрепление не удалось: %s", chat_str, exc)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise ValueError("Укажите BOT_TOKEN")
    me = api(token, "getMe")
    username = me.get("username") or ""
    if not username:
        raise ValueError("Не удалось определить username бота")
    path = state_file()
    state = read_state(path) or {}
    if "chats" not in state:
        old = state
        state = {"chats": {}}
        if old.get("chat_id") and old.get("message_id"):
            state["chats"][str(old["chat_id"])] = {
                "message_id": old["message_id"], "pinned": old.get("pinned", False)
            }
        if old.get("launch_at"):
            state["launch_at"] = old["launch_at"]
    if not state.get("launch_at"):
        state["launch_at"] = target_time().isoformat()
        save_state(path, state)
    allowed = parse_allowed_chats()
    if os.getenv("PRIVATE_TEST_CHAT_ID", "").strip():
        LOG.warning("PRIVATE_TEST_CHAT_ID больше не запускает авторассылку. Используйте /start в ЛС.")
    LOG.info("🤖 @%s запущен. Ожидаю /start@%s или /anons@%s в группе.", username, username, username)
    LOG.info("Отправки при старте нет. Ограничение CHAT_IDS: %s", ", ".join(sorted(allowed)) or "не задано")
    offset = None
    while True:
        try:
            updates = api(token, "getUpdates", {
                "timeout": 20,
                "offset": offset,
                "allowed_updates": ["message"],
            }) or []
            for update in updates:
                offset = update["update_id"] + 1
                msg = update.get("message")
                if msg:
                    try:
                        handle_command(token, msg, username, state, path, allowed)
                    except (RuntimeError, ValueError, OSError) as exc:
                        LOG.error("Ошибка обработки команды: %s", exc)
        except (RuntimeError, ValueError, OSError) as exc:
            LOG.error("Ошибка опроса Telegram: %s; повтор через 5 секунд", exc)
            time.sleep(5)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, OSError) as exc:
        LOG.error("Ошибка запуска: %s", exc)
        raise SystemExit(1)
