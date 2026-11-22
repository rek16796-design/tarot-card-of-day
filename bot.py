"""Автопостинг в Telegram-канал: «Карта дня» каждый день в 10:25 МСК + очередь постов.

Команды:
  python bot.py preview [N]   показать, как будут выглядеть следующие N карт дня (ничего не отправляет)
  python bot.py check         проверить токен и доступ бота к каналу (ничего не публикует)
  python bot.py card-now      опубликовать карту дня прямо сейчас
  python bot.py queue         показать очередь постов из queue.json
  python bot.py [run]         работать постоянно (команда по умолчанию): карта дня в 10:25 МСК и посты из очереди по времени

Токен и канал берутся из файла .env рядом со скриптом (или из переменных окружения):
  BOT_TOKEN=...      токен от @BotFather
  CHANNEL=@username  или числовой id канала вида -100...
Только стандартная библиотека Python 3.8+.
"""
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
MSK = timezone(timedelta(hours=3))  # в Москве нет перехода на летнее время
POST_TIME = (10, 25)

# Папка для изменяемых данных. На bothost это /app/data: она не стирается при обновлении из GitHub.
DATA_DIR = Path(os.environ.get("DATA_DIR") or ("/app/data" if Path("/app/data").is_dir() else BASE))
# Сколько минут после назначенного времени пост ещё можно отправить (если бот перезапускался).
CATCHUP = timedelta(minutes=int(os.environ.get("CATCHUP_MINUTES", "15")))
DEFAULT_CHANNEL = "-1001812594867"  # канал «Магия карт» (id из ссылки t.me/c/1812594867/...)

DECK_FILE = BASE / "deck.json"
QUEUE_FILE = BASE / "queue.json"
STATE_FILE = DATA_DIR / "state.json"
IMAGES_DIR = BASE / "images"  # необязательно: images/<Название карты>.jpg|png

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]
EMOJI = ["🔮", "✨", "🌙", "🪄", "💫", "🌟", "🤍"]
FOOTER = ("👀Заказать индивидуальный расклад - @tarozzeli\n"
          "‼️Комплексные расклады по акции - @zzkomplex")


# ---------- настройки ----------

def load_env():
    for env_file in (BASE / ".env", DATA_DIR / ".env"):
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    token = os.environ.get("BOT_TOKEN") or os.environ.get("TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
    channel = os.environ.get("CHANNEL") or DEFAULT_CHANNEL
    if not token:
        sys.exit("Нет BOT_TOKEN. Задайте переменную окружения BOT_TOKEN или файл .env по образцу .env.example")
    return token, channel


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def write_json(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def log(msg):
    print(f"[{datetime.now(MSK):%Y-%m-%d %H:%M:%S} МСК] {msg}", flush=True)


# ---------- Telegram Bot API ----------

def api(token, method, fields=None, file_field=None, file_path=None):
    url = f"https://api.telegram.org/bot{token}/{method}"
    fields = fields or {}
    if file_path:
        boundary = uuid.uuid4().hex
        body = b""
        for k, v in fields.items():
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode()
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
                 f"filename=\"{Path(file_path).name}\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode()
        body += Path(file_path).read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    else:
        req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode())
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        data = json.loads(e.read().decode() or "{}")
    if not data.get("ok"):
        # токен в текст ошибки не попадает
        raise RuntimeError(f"Telegram {method}: {data.get('description', 'неизвестная ошибка')}")
    return data["result"]


def send_post(token, channel, text, image=None):
    if image and len(text) <= 1024:
        return api(token, "sendPhoto", {"chat_id": channel, "caption": text}, "photo", image)
    if image:  # подпись к фото ограничена 1024 знаками: фото отдельно, текст следом
        api(token, "sendPhoto", {"chat_id": channel}, "photo", image)
    return api(token, "sendMessage", {"chat_id": channel, "text": text,
                                       "disable_web_page_preview": "true"})


# ---------- карта дня ----------

def next_card(state, deck, consume=True):
    """Карты идут в случайном порядке без повторов, пока колода не закончится."""
    order = state.get("order") or []
    if not order:
        order = list(range(len(deck)))
        random.shuffle(order)
    idx = order[0]
    if consume:
        state["order"] = order[1:]
    else:
        state["order"] = order
    return deck[idx]


def format_card(card, day):
    emoji = EMOJI[day.toordinal() % len(EMOJI)]
    return (f"Карта дня {emoji} {day.day} {MONTHS[day.month - 1]}\n\n"
            f"✨ {card['card']} ✨\n\n"
            f"{card['text']}\n\n{FOOTER}")


def card_image(card):
    for ext in ("jpg", "jpeg", "png"):
        p = IMAGES_DIR / f"{card['card']}.{ext}"
        if p.exists():
            return p
    return None


def post_card(token, channel, day=None):
    day = day or datetime.now(MSK).date()
    deck = read_json(DECK_FILE, [])
    state = read_json(STATE_FILE, {})
    card = next_card(state, deck)
    send_post(token, channel, format_card(card, day), card_image(card))
    state["last_card_date"] = day.isoformat()
    write_json(STATE_FILE, state)
    log(f"Карта дня опубликована: {card['card']}")


# ---------- очередь постов ----------

def parse_at(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=MSK)


def item_key(item):
    return f"{item['at']}|{item['text'].splitlines()[0]}"


def process_queue(token, channel, now):
    """queue.json приходит из GitHub и не меняется; отметки об отправке живут в state.json (DATA_DIR)."""
    queue = read_json(QUEUE_FILE, [])
    state = read_json(STATE_FILE, {})
    sent = state.setdefault("sent", {})
    changed = False
    for item in queue:
        key = item_key(item)
        if key in sent or parse_at(item["at"]) > now:
            continue
        if now - parse_at(item["at"]) > CATCHUP:
            log(f"Пропущен (время прошло): {item['at']} {item['text'].splitlines()[0]!r}")
            sent[key] = "skipped"
        else:
            image = BASE / item["image"] if item.get("image") else None
            send_post(token, channel, item["text"], image)
            sent[key] = now.strftime("%Y-%m-%d %H:%M")
            log(f"Пост из очереди опубликован: {item['text'].splitlines()[0]!r}")
        changed = True
        write_json(STATE_FILE, state)  # сохраняем сразу после каждого поста
    return changed


def queue_status(item):
    return read_json(STATE_FILE, {}).get("sent", {}).get(item_key(item))


# ---------- основной цикл ----------

def run(token, channel):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log(f"Бот запущен. Карта дня каждый день в {POST_TIME[0]:02d}:{POST_TIME[1]:02d} МСК. "
        f"Канал: {channel}. Данные: {DATA_DIR}")
    while True:
        now = datetime.now(MSK)
        try:
            state = read_json(STATE_FILE, {})
            due = now.replace(hour=POST_TIME[0], minute=POST_TIME[1], second=0, microsecond=0)
            # если бот перезапускался в 10:25, пост уйдёт сразу после старта (в пределах CATCHUP)
            if due <= now < due + CATCHUP and state.get("last_card_date") != now.date().isoformat():
                post_card(token, channel, now.date())
            process_queue(token, channel, now)
        except Exception as e:  # сеть упала и т.п. — пробуем снова через минуту
            log(f"Ошибка: {e}")
        time.sleep(30)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"  # хостинг запускает файл без аргументов
    if cmd == "preview":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 3
        deck, state = read_json(DECK_FILE, []), read_json(STATE_FILE, {})
        state = json.loads(json.dumps(state))  # копия: превью не трогает state.json
        day = datetime.now(MSK).date()
        for i in range(n):
            print(format_card(next_card(state, deck), day + timedelta(days=i)), "\n" + "-" * 40)
        return
    if cmd == "queue":
        for item in read_json(QUEUE_FILE, []):
            print(f"{item['at']}  [{queue_status(item) or 'ждёт'}]  {item['text'].splitlines()[0]}")
        return
    if cmd not in ("check", "card-now", "run"):  # help и всё неизвестное
        print(__doc__)
        return
    token, channel = load_env()
    if cmd == "check":
        me = api(token, "getMe")
        chat = api(token, "getChat", {"chat_id": channel})
        member = api(token, "getChatMember", {"chat_id": channel, "user_id": me["id"]})
        ok = member.get("status") == "administrator" and member.get("can_post_messages", False)
        print(f"Бот: @{me['username']}\nКанал: {chat.get('title')}\n"
              f"Права на публикацию: {'есть' if ok else 'НЕТ — добавьте бота админом с правом публиковать'}")
    elif cmd == "card-now":
        post_card(token, channel)
    elif cmd == "run":
        run(token, channel)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    main()
