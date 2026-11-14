import re
import socket
import json
import time
import datetime as dt
import unicodedata
import urllib.request
import textwrap
import threading
from urllib.parse import urlencode

try:
    from zoneinfo import ZoneInfo   # na Windowsie wymaga: pip install tzdata
except ImportError:                 # pragma: no cover
    ZoneInfo = None

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL_NAME = "qwen2.5-coder:14b"  # mozesz zmienic np. na "gemma2:9b"
HOST = "127.0.0.1"
PORT = 8080

BOT_NAME = "Wendy"       # etykieta przed odpowiedzia
SCREEN_WIDTH = 37        # Atari ma 38 kolumn; 37 chroni przed pustymi liniami przy zawijaniu
CHARS_PER_SEC = 100      # tempo wysylania do Atari (0 = bez ograniczenia); ~dla 1200 baud
CHUNK = 16               # maks. ile znakow naraz (przy wolniejszym tempie porcje sa mniejsze)
MIN_CPS = 10             # najwolniejsze tempo dostepne z /typespeed
MAX_CPS = 100            # najszybsze tempo ("fast") - tyle Atari jeszcze bez problemu przyjmuje
SPEED_PRESETS = {"slow": 25, "normal": 50, "fast": 100}
KEEP_BLANK_LINES = False # True = zostaw puste linie miedzy akapitami; False = usun (oszczedza ekran)
MAX_HISTORY = 20         # ile ostatnich wiadomosci trzymac w kontekscie
OLLAMA_TIMEOUT = 300     # pierwsze zaladowanie duzego modelu potrafi trwac
BANNER_DELAY = 1.5       # pauza po polaczeniu, zanim wyslemy powitanie
DEBUG = False            # loguj surowe bajty od Atari (hex)

TIMEZONE = "Europe/Warsaw"        # strefa czasowa do "what time is it?"
DEFAULT_CITY = "Lodz, Poland"     # gdy ktos pyta o pogode bez podania miejsca
API_TIMEOUT = 8                   # sekundy na zapytanie do Open-Meteo

IDLE_MINUTES = 20        # po tylu minutach ciszy usera bot moze sam sie odezwac (0 = wylaczone)
MAX_UNANSWERED = 1       # ile wiadomosci z inicjatywy pod rzad bez odpowiedzi usera (0 = bez limitu)
INITIATIVE_BEEPS = 1     # ile piknieć przy wiadomosci z inicjatywy
IDLE_PROMPT = (
    "[Automatic note from the chat system - the user did not write this] "
    "The user has gone quiet for a while ({minutes} min). You are Wendy and you enjoy keeping "
    "the conversation going. Pick up the thread of your chat: ask a follow-up question about "
    "something they said, share a related thought, or tease them lightly. Write one or two "
    "sentences addressed directly to the user. Do not mention this note. "
    "Reply with only the word SKIP only if the conversation has clearly come to a natural end "
    "(for example the user said goodbye)."
)

SYSTEM_PROMPT = (
    "You are Wendy Jenkins, a highly creative, and immersive companion. "
    "This is a private, safe space for creative expression.  "
    "The user's name is {user}. Address the user directly as you, or by name now and then. "
    # Terminal Atari ma maly ekran i jest wolny - krotkie odpowiedzi to podstawa:
    "The user talks to you through a tiny 38-column retro terminal. "
    "Keep replies short (2-4 sentences), plain text only: no markdown, no emoji, no lists."
)

STATE_FILE = "atari_bridge_state.json"   # tu zapamietujemy imie usera miedzy uruchomieniami
DEFAULT_USER = "Felix"
user_name = DEFAULT_USER

chat_history = []   # tylko tury user/assistant; system budujemy swiezy przy kazdym pytaniu

EOT = b"\x04"  # sygnal "koniec wypowiedzi bota" - terminal na Atari rysuje wtedy prompt
EOL_RE = re.compile(rb"[\x9b\r\n]")  # ATASCII EOL, CR albo LF konczy linie
NOTICE = b"\x05"  # "uwaga, powiadomienie": Atari wymazuje wtedy linie z promptem i tym, co user wpisal
BELL = b"\xfd"    # ATASCII 253 = buzzer
THINK = b"\x06"   # "dostalem Twoja wiadomosc, mysle": Atari zaczyna wtedy animacje kropek
BEEPS = 2          # ile razy pikac przy przypomnieniu
BEEP_GAP = 0.35    # przerwa miedzy pikniciami (sek.)

# Przypomnienia: lista (kiedy, tekst). Zyja tak dlugo, jak dziala mostek.
reminders = []
state_lock = threading.Lock()   # chroni liste reminders
io_lock = threading.Lock()      # tylko jeden watek naraz pisze do gniazda (cala "tura" bota = jedna blokada)
active_conn = None              # polaczenie, do ktorego wolno dostarczac przypomnienia
last_activity = time.monotonic()  # kiedy ostatnio "cos sie dzialo" (user pisal, bot odpowiedzial)
unanswered = 0                  # ile wiadomosci z inicjatywy bota czeka bez reakcji usera

PL_MAP = str.maketrans("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ", "acelnoszzACELNOSZZ")
TYPO_MAP = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201e": '"', "\u201d": '"',
    "\u2013": "-", "\u2014": "-", "\u2026": "...", "\u00a0": " ",
})

# --------------------------------------------------------------- imie usera --

NICK_RE = re.compile(r"/nick\s+(.+)", re.I)


def load_state():
    global user_name, CHARS_PER_SEC
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    user_name = data.get("user_name") or DEFAULT_USER
    cps = data.get("chars_per_sec")
    if isinstance(cps, int):
        CHARS_PER_SEC = max(MIN_CPS, min(MAX_CPS, cps))


def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"user_name": user_name, "chars_per_sec": CHARS_PER_SEC}, f)
    except OSError as e:
        print(f"[state] nie moge zapisac: {e}")


def set_user_name(raw):
    """Czysci nick z Atari (max 12 znakow, bez dziwnych znakow) i zapisuje go na dysk."""
    global user_name
    name = re.sub(r"[^A-Za-z0-9 _\-]", "", raw).strip()[:12].strip()
    if not name:
        return
    user_name = name
    save_state()
    print(f"[nick] user = {user_name}")


SPEED_RE = re.compile(r"/typespeed(?:\s+(\S+))?", re.I)


def speed_label():
    names = [n for n, v in SPEED_PRESETS.items() if v == CHARS_PER_SEC]
    return f"{CHARS_PER_SEC} chars/s" + (f" ({names[0]})" if names else "")


def handle_typespeed(arg):
    """/typespeed [slow|normal|fast|10-100]. Zwraca tekst odpowiedzi."""
    global CHARS_PER_SEC
    usage = "Use /typespeed slow, normal, fast or a number 10-100."
    if arg is None:
        return f"Typing speed: {speed_label()}.\n{usage}"
    word = arg.lower()
    if word in SPEED_PRESETS:
        cps = SPEED_PRESETS[word]
    elif word.isdigit():
        cps = max(MIN_CPS, min(MAX_CPS, int(word)))
    else:
        return usage
    CHARS_PER_SEC = cps
    save_state()
    return f"Typing speed set to {speed_label()}."


# ---------------------------------------------------------------- zywe dane --

def now_local():
    if ZoneInfo is not None:
        try:
            return dt.datetime.now(ZoneInfo(TIMEZONE))
        except Exception:
            pass
    return dt.datetime.now().astimezone()  # brak tzdata -> czas systemowy


def time_context():
    n = now_local()
    return (f"Current local date and time: {n:%A, %d %B %Y, %H:%M} "
            f"(timezone {TIMEZONE}). Use it whenever asked about the time or date.")


WMO = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "rime fog", 51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "freezing drizzle", 57: "heavy freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "heavy freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light rain showers", 81: "rain showers", 82: "violent rain showers",
    85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with heavy hail",
}

WEATHER_RE = re.compile(
    r"\b(weather|forecast|temperature)\b"
    r"|\b(?:is|will|does|isn't|going to)\s+it\s+(?:be\s+)?"
    r"(?:rain|raining|snow|snowing|sunny|cold|hot|warm|windy)",
    re.I)
PREP_RE = re.compile(r"\b(?:in|for|at|near)\s+", re.I)
CUT_RE = re.compile(
    r"\s+(?:in|on|at|for|during|around|tomorrow|today|tonight|now|right|currently|"
    r"later|this|please|and|or)\b|[?!.;]", re.I)


TIME_WORDS = {"tomorrow", "today", "tonight", "now", "later", "this", "the", "a", "my", "right",
              "currently", "please", "weekend", "morning", "evening", "afternoon", "week"}


def extract_place(text):
    """Zwraca miejsce z pytania o pogode albo DEFAULT_CITY."""
    for m in PREP_RE.finditer(text):
        cand = CUT_RE.split(text[m.end():], 1)[0].strip(" ,'-")
        if cand.split(" ")[0].lower() in TIME_WORDS:
            continue  # "for tomorrow in Paris" -> pomin "tomorrow", bierz kolejne "in"
        if 2 <= len(cand) <= 40 and re.fullmatch(r"[A-Za-z][A-Za-z .,'\-]*", cand):
            return cand
    return DEFAULT_CITY


def http_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "atari-ollama-bridge"})
    with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def geocode(query):
    city, _, rest = query.partition(",")
    rest = rest.strip().lower()
    url = "https://geocoding-api.open-meteo.com/v1/search?" + urlencode(
        {"name": city.strip(), "count": 10, "language": "en", "format": "json"})
    results = http_json(url).get("results") or []
    if rest:  # "Lodz, Poland" -> dopasuj kraj / wojewodztwo
        for r in results:
            fields = (r.get("country", ""), r.get("country_code", ""), r.get("admin1", ""))
            if any(f.lower().startswith(rest) for f in fields if f):
                return r
    return results[0] if results else None


def weather_context(place):
    try:
        g = geocode(place)
        if not g:
            return (f"Live weather lookup: no place called '{place}' was found. "
                    "Tell the user you could not find that place.")
        q = {
            "latitude": g["latitude"], "longitude": g["longitude"],
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,"
                       "precipitation,weather_code,wind_speed_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum",
            "forecast_days": 2, "timezone": "auto",
        }
        d = http_json("https://api.open-meteo.com/v1/forecast?" + urlencode(q))
        c, day = d["current"], d["daily"]
        name = ", ".join(x for x in (g.get("name"), g.get("admin1"), g.get("country")) if x)

        def desc(i):
            return (f"{WMO.get(day['weather_code'][i], 'unknown conditions')}, "
                    f"high {day['temperature_2m_max'][i]}C, low {day['temperature_2m_min'][i]}C, "
                    f"precipitation {day['precipitation_sum'][i]} mm")

        return (
            f"Live weather data for {name}, fetched just now (metric units). "
            f"NOW: {c['temperature_2m']}C (feels like {c['apparent_temperature']}C), "
            f"{WMO.get(c['weather_code'], 'unknown conditions')}, humidity "
            f"{c['relative_humidity_2m']}%, wind {c['wind_speed_10m']} km/h. "
            f"TODAY: {desc(0)}. TOMORROW: {desc(1)}. "
            "Answer from this data only; do not invent other numbers.")
    except Exception as e:
        print(f"[weather] blad: {e}")
        return ("Live weather lookup failed just now. Tell the user you could not "
                "get the weather data right now and do not guess numbers.")


def live_context(user_msg):
    parts = [time_context()]
    if WEATHER_RE.search(user_msg):
        place = extract_place(user_msg)
        print(f"[weather] szukam: {place}")
        parts.append(weather_context(place))
    return "\n".join(parts)


# ------------------------------------------------------------------- tekst ---

def to_atari_text(text, width=SCREEN_WIDTH):
    clean = text.translate(PL_MAP).translate(TYPO_MAP)
    clean = unicodedata.normalize("NFKD", clean)
    clean = clean.encode("ascii", "ignore").decode("ascii")
    clean = clean.replace("**", "").replace("`", "").replace("*", "")

    lines = []
    for paragraph in clean.splitlines():
        paragraph = " ".join(paragraph.split())   # zwija wielokrotne spacje i tabulatory
        if not paragraph:
            if KEEP_BLANK_LINES and lines and lines[-1] != "":
                lines.append("")                  # max jedna pusta linia z rzedu
        else:
            lines.extend(textwrap.wrap(paragraph, width=width))
    while lines and lines[-1] == "":
        lines.pop()
    return ("\x9b".join(lines) + "\x9b").encode("latin1")


def inverse(data):
    """Odwrocone znaki ATASCII = kod + 128 (spacji nie ruszamy, bo wyszedlby blok)."""
    return bytes(c | 0x80 if c != 0x20 else c for c in data)


def send_paced(conn, data):
    """Wysyla dane malymi porcjami, zeby 850/BASIC nie dostal wiecej niz zdazy odczytac."""
    if CHARS_PER_SEC <= 0:
        conn.sendall(data)
        return
    chunk = max(1, min(CHUNK, round(CHARS_PER_SEC / 6)))  # ~6 porcji na sekunde, rowne tempo
    for i in range(0, len(data), chunk):
        conn.sendall(data[i:i + chunk])
        time.sleep(chunk / CHARS_PER_SEC)


# ------------------------------------------------------------------ Ollama ---

def build_system(live):
    return (SYSTEM_PROMPT.replace("{user}", user_name)
            + "\n\n[LIVE DATA - trust it, never claim you lack real-time access]\n" + live)


def ollama_chat(system, messages):
    payload = {
        "model": MODEL_NAME,
        "messages": [{"role": "system", "content": system}] + messages,
        "stream": False,
        "options": {"num_ctx": 8192},
    }
    req = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))["message"]["content"]


def ask_ollama(user_msg):
    global chat_history
    if user_msg.strip().lower() == "/reset":
        chat_history = []
        return "Pamiec wyczyszczona! Zaczynamy od nowa."

    chat_history.append({"role": "user", "content": user_msg})
    chat_history = chat_history[-MAX_HISTORY:]
    try:
        reply = ollama_chat(build_system(live_context(user_msg)), chat_history)
        chat_history.append({"role": "assistant", "content": reply})
        return reply
    except Exception as e:
        chat_history.pop()  # nie zostawiaj wiszacej wiadomosci usera
        return f"BLAD OLLAMA: {e}"


# -------------------------------------------------------------- przypomnienia --

REMIND_CMD_RE = re.compile(r"/remind\b", re.I)
REMIND_ARGS_RE = re.compile(r"(\d{1,2}:\d{2}|\d+)\s+(.+)", re.S)


def pending_text():
    with state_lock:
        items = list(reminders)
    if not items:
        return "No reminders pending."
    return "Pending:\n" + "\n".join(f"{d:%a %H:%M} {t}" for d, t in items)


def handle_remind(cmd):
    """/remind 10 text  |  /remind 17:15 text  |  /remind  |  /remind clear"""
    arg = cmd[len("/remind"):].strip()
    if arg.lower() == "clear":
        with state_lock:
            n = len(reminders)
            reminders.clear()
        return f"Cleared {n} reminder(s)."
    m = REMIND_ARGS_RE.fullmatch(arg)
    if not m:
        return "Use: /remind 10 call Alice (minutes) or /remind 17:15 call Alice. " \
               "/remind clear removes all.\n" + pending_text()
    when, text = m.group(1), m.group(2).strip()
    now = now_local()
    if ":" in when:
        h, mi = (int(x) for x in when.split(":"))
        if h > 23 or mi > 59:
            return "That time does not exist. Use HH:MM, 24-hour clock."
        due = now.replace(hour=h, minute=mi, second=0, microsecond=0)
        if due <= now:
            due += dt.timedelta(days=1)
    else:
        mins = int(when)
        if not 1 <= mins <= 1440:
            return "Give me 1 to 1440 minutes."
        due = now + dt.timedelta(minutes=mins)
    with state_lock:
        reminders.append((due, text))
        reminders.sort(key=lambda r: r[0])
    day = " tomorrow" if due.date() != now.date() else ""
    return f"OK, I will remind you at {due:%H:%M}{day}: {text}"


def send_notice(conn, label, text, beeps):
    """Powiadomienie spoza kolejnosci: znacznik, piknieca, tekst. Wolac POD io_lock."""
    data = to_atari_text(f"{label} {text}")
    n = len(label)
    conn.sendall(NOTICE)
    for _ in range(beeps):
        conn.sendall(BELL)
        time.sleep(BEEP_GAP)
    send_paced(conn, inverse(data[:n]) + data[n:] + EOT)


def deliver_reminder(conn, text):
    """Zwraca True, jesli poszlo do Atari."""
    try:
        with io_lock:  # czeka, az bot skonczy swoja ture
            send_notice(conn, "REMINDER:", text, BEEPS)
        return True
    except OSError:
        return False  # polaczenie padlo - przypomnienie zostaje do nastepnego razu


def reminder_loop():
    while True:
        time.sleep(1)
        conn = active_conn
        if conn is None:
            continue
        now = now_local()
        with state_lock:
            due = [r for r in reminders if r[0] <= now]
        for r in due:
            print(f"[remind] {r[1]}")
            if deliver_reminder(conn, r[1]):
                touch_activity()
                with state_lock:
                    if r in reminders:
                        reminders.remove(r)


# ------------------------------------------------------ inicjatywa bota (cisza) --

SKIP_RE = re.compile(r"\W*skip\b", re.I)


def touch_activity(user=False):
    """Zeruje licznik ciszy. user=True oznacza, ze to user sie odezwal (zeruje tez limit inicjatyw)."""
    global last_activity, unanswered
    last_activity = time.monotonic()
    if user:
        unanswered = 0


def initiative_loop():
    global chat_history, last_activity, unanswered
    while True:
        time.sleep(1)
        conn = active_conn
        if conn is None or IDLE_MINUTES <= 0:
            continue
        if time.monotonic() - last_activity < IDLE_MINUTES * 60:
            continue
        with io_lock:  # ta sama blokada co tura usera: historia ruszana tylko pod nia
            if active_conn is not conn:
                continue
            now = time.monotonic()
            if now - last_activity < IDLE_MINUTES * 60:
                continue  # user zdazyl sie odezwac, gdy czekalismy na blokade
            minutes = round((now - last_activity) / 60)
            last_activity = stamp = now  # licznik zerujemy zawsze: SKIP tez konczy "runde"
            if not chat_history or (MAX_UNANSWERED and unanswered >= MAX_UNANSWERED):
                continue  # nie ma do czego nawiazac albo bot juz sie odezwal i nikt nie odpowiedzial
            note = {"role": "user", "content": IDLE_PROMPT.format(minutes=minutes)}
            try:
                reply = ollama_chat(build_system(time_context()), chat_history + [note]).strip()
            except Exception as e:
                print(f"[initiative] blad Ollamy: {e}")
                continue
            if last_activity != stamp:
                print("[initiative] user sie odezwal w trakcie generowania - odrzucam")
                continue
            if not reply or SKIP_RE.match(reply):
                print(f"[initiative] cisza {minutes} min -> SKIP (model: {reply[:60]!r})")
                continue
            print(f"{BOT_NAME} (z inicjatywy) > {reply}\n")
            try:
                send_notice(conn, f"{BOT_NAME}:", reply, INITIATIVE_BEEPS)
            except OSError:
                continue  # polaczenie padlo - nic nie zapisujemy do historii
            chat_history.append({"role": "assistant", "content": reply})
            chat_history = chat_history[-MAX_HISTORY:]
            unanswered += 1
            last_activity = time.monotonic()


# ------------------------------------------------------------------ serwer ---

def send_bot(conn, text):
    label = f"{BOT_NAME}:"
    data = to_atari_text(f"{label} {text}")
    n = len(label)
    send_paced(conn, b"\x9b" + inverse(data[:n]) + data[n:] + EOT)


def handle_client(conn):
    global active_conn
    time.sleep(BANNER_DELAY)
    with io_lock:
        send_paced(conn, to_atari_text(
            f"=== {BOT_NAME} ONLINE (ATARI BRIDGE) ===\n"
            "Write /reset to clear the memory.\n"
            "Write /remind to set a reminder.\n") + EOT)
    touch_activity(user=True)
    active_conn = conn  # od teraz wolno dostarczac przypomnienia i inicjatywy bota
    buffer = b""
    while True:
        chunk = conn.recv(1024)
        if not chunk:
            print("[-] Altirra zamknela polaczenie (recv=0)")
            break
        if DEBUG:
            print(f"[rx] {chunk.hex(' ')}  {chunk!r}")
        buffer += chunk
        while True:
            m = EOL_RE.search(buffer)
            if not m:
                break
            line, buffer = buffer[:m.start()], buffer[m.end():]
            user_text = bytes(b for b in line if 32 <= b <= 126).decode("ascii").strip()
            if not user_text:
                continue
            print(f"ATARI > {user_text}")
            touch_activity(user=True)
            m = NICK_RE.fullmatch(user_text)
            if m:  # Atari juz samo potwierdzilo na ekranie - tu tylko zapamietujemy, bez odpowiedzi
                set_user_name(m.group(1))
                continue
            m = SPEED_RE.fullmatch(user_text)
            if m:
                with io_lock:  # zmiana tempa i odpowiedz pod blokada, wiec odpowiedz idzie juz nowym tempem
                    reply = handle_typespeed(m.group(1))
                    print(f"{BOT_NAME} > {reply}\n")
                    send_bot(conn, reply)
                continue
            if REMIND_CMD_RE.match(user_text):
                reply = handle_remind(user_text)
                print(f"{BOT_NAME} > {reply}\n")
                with io_lock:
                    send_bot(conn, reply)
                continue
            with io_lock:  # cala tura (kropki + generowanie + odpowiedz) bez wtracania przypomnien
                conn.sendall(THINK)  # znak zycia na czas generowania (Atari animuje kropki)
                reply = ask_ollama(user_text)
                print(f"{BOT_NAME} > {reply}\n")
                send_bot(conn, reply)
                touch_activity()


def start_server():
    global active_conn
    load_state()
    threading.Thread(target=reminder_loop, daemon=True).start()
    threading.Thread(target=initiative_loop, daemon=True).start()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((HOST, PORT))
        s.listen(1)
        print(f"[*] Mostek Atari-Ollama nasluchuje na {HOST}:{PORT} (Model: {MODEL_NAME}, user: {user_name})")
        while True:
            conn, addr = s.accept()
            print(f"[+] Polaczono z Altirra: {addr}")
            with conn:
                try:
                    handle_client(conn)
                except ConnectionError as e:  # Reset/Aborted/BrokenPipe (Windows: WinError 10053)
                    print(f"[-] Polaczenie zerwane przez Altirre: {e}")
                finally:
                    active_conn = None
            print("[-] Rozlaczono, czekam na nowe polaczenie...")


if __name__ == "__main__":
    start_server()