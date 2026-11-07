"""Synthetic edge-case dialogues for the CQA judge ("unit tests for the judge").

    python eval/synthetic/build.py          # -> eval/synthetic/records.json + eval/synthetic/expected.jsonl

Every dialogue is written by hand (m3-robustness) to sit exactly on one rule of the organizers' checklist
(data/raw/Checklist.txt, pages 1-3). The expected answers are fixed BY CONSTRUCTION here, next to the dialogue, with the
rule they test; they are never produced by the model we evaluate.

Format of records.json = DEV format of the organizers' API: [{conversation_id, channel, language, transcript:[{timestamp,
speaker, text}], audio_url}]. Roles: client / operator / bot / system. Chats use MM:SS, calls ISO 8601 with a timezone.

expected.jsonl, one line per dialogue:
  conversation_id, language, channel,
  target   - criteria the dialogue was built to test (the edge),
  expected - {criterion_id: answer} for every criterion we are sure about ("yes"/"no"/"na"/number; C8 "yes" = violation),
  accept   - optional {criterion_id: [alternative answers]} where the PDF leaves one adjacent step defensible,
  reading  - optional {criterion_id: reading} when the answer depends on a team reading (guidance_v1.json variants),
  rule     - the checklist line the edge is built on, why - short justification.
All names, numbers and accounts are fictional.
"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent

# ----------------------------------------------------------------- helpers
BOT_HELLO = {
    "RU": "Здравствуйте! Я AIM, AI-ассистент Freedom Broker. Отвечу на вопросы о продуктах и услугах или передам вопрос специалистам.",
    "KZ": "Сәлеметсіз бе! Мен AIM, Freedom Broker компаниясының жасанды интеллект көмекшісімін. Сұрағыңызды қолдау қызметіне жолдай аламын.",
    "EN": "Hello! I'm AIM, the Freedom Broker AI assistant. I can answer questions or pass your question to our support team.",
}
ESCALATE = {"RU": "Эскалация на оператора.", "KZ": "Чат операторға жіберілді.", "EN": "Chat transferred to an agent."}
CLOSED = {"RU": "Оператор закрыл чат.", "KZ": "Оператор чатты жапты.", "EN": "Agent closed the chat."}
LEFT = {"RU": "Клиент покинул чат.", "KZ": "Клиентпен байланыс үзілді.", "EN": "Connection to customer lost."}
VERIFIED = {"RU": "Личность клиента подтверждена.", "KZ": "Клиенттің жеке басы расталды.", "EN": "Customer identity verified."}

CRITERIA = ["C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8"]
RECORDS: list[dict] = []
EXPECTED: list[dict] = []


def _mmss(sec: int) -> str:
    return f"{sec // 60:02d}:{sec % 60:02d}"


def _sec(ts: str) -> int:
    m, s = ts.split(":")
    return int(m) * 60 + int(s)


def chat(lang: str, first: str, turns: list, *, bot_extra: list[str] | None = None) -> list[dict]:
    """Standard DEV-like opening (client -> bot hello -> [bot extra] -> escalation), then turns.
    A turn is (speaker, text) or (speaker, text, "MM:SS"); without a time the clock advances 25 s."""
    out = [{"timestamp": "00:00", "speaker": "client", "text": first},
           {"timestamp": "00:02", "speaker": "bot", "text": BOT_HELLO[lang]}]
    t = 2
    for b in bot_extra or []:
        if isinstance(b, tuple):
            sp, text = b
        else:
            sp, text = "bot", b
        t += 8
        out.append({"timestamp": _mmss(t), "speaker": sp, "text": text})
    t += 4
    out.append({"timestamp": _mmss(t), "speaker": "system", "text": ESCALATE[lang]})
    t += 40
    for turn in turns:
        sp, text = turn[0], turn[1]
        if len(turn) > 2:
            t = _sec(turn[2])
        out.append({"timestamp": _mmss(t), "speaker": sp, "text": text})
        t += 25
    return out


def call(start: str, turns: list) -> list[dict]:
    """Voice call: turns are (offset_seconds, speaker, text); ISO 8601 timestamps with +00:00."""
    from datetime import datetime, timedelta

    t0 = datetime.fromisoformat(start)
    def iso7(dt: datetime) -> str:  # DEV calls use 7 fractional digits: 2026-11-03T15:49:13.1900670+00:00
        s = dt.isoformat(timespec="microseconds")
        return s[:26] + "0" + s[26:]

    return [{"timestamp": iso7(t0 + timedelta(seconds=off, milliseconds=137 * (i % 7))), "speaker": sp, "text": text}
            for i, (off, sp, text) in enumerate(turns)]


def case(cid: str, lang: str, transcript: list[dict], *, target: list[str], expected: dict, rule: str, why: str,
         accept: dict | None = None, reading: dict | None = None, channel: str = "agent chat", audio_url: str | None = None) -> None:
    assert set(expected) <= set(CRITERIA), cid
    assert set(target) <= set(expected), cid
    RECORDS.append({"conversation_id": cid, "channel": channel, "language": lang, "transcript": transcript, "audio_url": audio_url})
    exp = {c: expected.get(c) for c in CRITERIA}
    EXPECTED.append({"conversation_id": cid, "language": lang, "channel": channel, "target": target, "expected": exp,
                     "accept": accept or {}, "reading": reading or {}, "rule": rule, "why": why})


# =================================================================== C1 — greeting in client's language + name in 1st reply
case("syn-c1-01-ru", "RU", chat("RU", "Где найти отчёт брокера за прошлый год?", [
    ("operator", "Сәлеметсіз бе, менің атым Дана. Отчёт брокера за год находится в веб-версии: «Кабинет» → «Отчёты» → «Годовой»."),
    ("client", "Нашёл, спасибо."),
    ("operator", "Пожалуйста! Если появятся вопросы — пишите. Хорошего дня!"),
]), target=["C1"], expected={"C1": "no", "C3": "na", "C4": 20, "C5": "yes", "C7": "yes", "C8": "no"},
    rule="C1: «поздоровался на языке клиента и представился в первом ответе»",
    why="Клиент пишет по-русски, оператор поздоровался по-казахски; имя есть, но язык приветствия не совпадает → Нет.")

case("syn-c1-02-kz", "KZ", chat("KZ", "Брокерлік шотты қалай толтырамын?", [
    ("operator", "Здравствуйте, меня зовут Ерлан. Қосымшада «Толтыру» батырмасын басып, картаны таңдаңыз."),
    ("client", "Рақмет, түсіндім."),
    ("operator", "Оқасы жоқ. Басқа сұрақ болса, жазыңыз. Сау болыңыз!"),
]), target=["C1"], expected={"C1": "no", "C3": "na", "C5": "yes", "C7": "yes", "C8": "no"},
    rule="C1: приветствие на языке клиента",
    why="Клиент пишет по-казахски, приветствие и представление оператора на русском (дальше — казахский) → Нет.")

case("syn-c1-03-en", "EN", chat("EN", "How do I change the email on my account?", [
    ("operator", "Здравствуйте! Меня зовут Анна. Go to Profile → Settings → Email, enter the new address and confirm the code we send you."),
    ("client", "Done, thanks."),
    ("operator", "You're welcome! Anything else I can help with? Have a nice day!"),
]), target=["C1"], expected={"C1": "no", "C5": "yes", "C7": "yes", "C8": "no"},
    rule="C1: приветствие на языке клиента",
    why="Клиент пишет по-английски, оператор здоровается и представляется по-русски → Нет.")

case("syn-c1-04-ru", "RU", chat("RU", "Как поменять тариф?", [
    ("operator", "Здравствуйте! Сейчас подскажу."),
    ("operator", "Меня зовут Олег. Тариф меняется в приложении: «Профиль» → «Тариф» → «Сменить». Новый тариф начнёт действовать с 1-го числа следующего месяца."),
    ("client", "Понял, спасибо."),
    ("operator", "Рад помочь! До свидания!"),
]), target=["C1"], expected={"C1": "no", "C3": "na", "C5": "yes", "C7": "yes", "C8": "no"},
    rule="C1: «представился в первом ответе»",
    why="Первый ответ — «Здравствуйте! Сейчас подскажу.» без имени; имя прозвучало только во второй реплике → Нет.")

case("syn-c1-05-en", "EN", chat("EN", "Where can I see my dividends?", [
    ("operator", "You can find them under Portfolio → History → Dividends."),
    ("client", "Got it."),
    ("operator", "Glad to help. Have a great day!"),
]), target=["C1"], expected={"C1": "no", "C5": "yes", "C8": "no"},
    rule="C1: оценивается оператор; приветствие бота — контекст",
    why="Поздоровался и представился только бот AIM; первый ответ оператора сразу с инструкцией, без приветствия и имени → Нет.")

case("syn-c1-06-kz", "KZ", chat("KZ", "«Кабинет» бөлімінде тарифімді қайдан көремін?", [
    ("operator", "Сәлеметсіз бе! Менің атым Әсел, Freedom Broker қолдау қызметі. Веб-нұсқада «Кабинет» бөлімін ашыңыз, тариф аты-жөніңіздің астында жазылған."),
    ("client", "Таптым, рақмет!"),
    ("operator", "Оқасы жоқ. Тағы сұрақ болса, жазыңыз. Сау болыңыз!"),
]), target=["C1"], expected={"C1": "yes", "C3": "na", "C4": 20, "C5": "yes", "C6": 10, "C7": "yes", "C8": "no"},
    rule="C1 (позитивный контроль): русские названия разделов не нарушают «язык клиента»",
    why="Приветствие и имя на казахском в первом ответе; «Кабинет» — название раздела интерфейса → Да.")

case("syn-c1-07-ru", "RU", chat("RU", "Подскажите, как скачать справку о брокерском счёте?", [
    ("operator", "Freedom Broker, меня зовут Ирина, добрый день! Справка заказывается в приложении: «Документы» → «Заказать справку», готова в течение рабочего дня."),
    ("client", "Отлично, спасибо!"),
    ("operator", "Пожалуйста! Хорошего дня!"),
]), target=["C1"], expected={"C1": "yes", "C3": "na", "C5": "yes", "C7": "yes", "C8": "no"},
    rule="C1 (позитивный контроль): порядок «компания, имя, приветствие» допустим",
    why="В первом ответе есть и приветствие («добрый день») на русском, и имя → Да.")

# =================================================================== C2 — needs: use known details, ask the missing ones
case("syn-c2-01-ru", "RU", chat("RU", "Не могу войти в приложение на Android, пишет «Сессия истекла».", [
    ("operator", "Здравствуйте! Меня зовут Мария. Подскажите, пожалуйста, на каком устройстве вы входите и какую ошибку видите?"),
    ("client", "Я же написал: Android, «Сессия истекла»."),
    ("operator", "Извините. Выйдите из приложения, очистите кэш в настройках Android и войдите заново — сессия обновится."),
    ("client", "Получилось."),
    ("operator", "Отлично! Если что-то ещё понадобится — пишите. До свидания!"),
]), target=["C2"], expected={"C1": "yes", "C2": "no", "C3": "na", "C5": "yes", "C7": "yes", "C8": "no"},
    accept={"C6": [7, 4, 10]},
    rule="C2: «учёл известные детали» — переспрос уже сообщённого = Нет",
    why="Клиент в первом сообщении указал устройство и текст ошибки; оператор переспросил оба → Нет.")

case("syn-c2-02-en", "EN", chat("EN", "I can't sell my shares, the Sell button is grey.", [
    ("operator", "Hello, I'm Daniel from support. Which security is it, and are you using the mobile app or the web terminal?"),
    ("client", "Tesla, in the mobile app."),
    ("operator", "Thanks. US exchanges are closed right now, so the Sell button for Tesla is inactive. It becomes active at 19:30 Astana time when the market opens, or you can place a limit order now in the web terminal."),
    ("client", "Ah, makes sense. Thanks!"),
    ("operator", "You're welcome! Anything else? Have a good evening!"),
]), target=["C2"], expected={"C1": "yes", "C2": "yes", "C3": "na", "C4": 20, "C5": "yes", "C7": "yes", "C8": "no"},
    rule="C2: «уточнил недостающее до ответа»",
    why="Для ответа не хватало бумаги и платформы; оператор уточнил их до ответа и учёл в ответе → Да.")

case("syn-c2-03-kz", "KZ", chat("KZ", "«Freedom Basic» тарифінде АҚШ акцияларын сатып алу комиссиясы қанша?", [
    ("operator", "Сәлеметсіз бе! Менің атым Нұржан. «Freedom Basic» тарифінде АҚШ акцияларын сатып алу комиссиясы мәміле сомасының 0,1% құрайды."),
    ("client", "Түсінікті, рақмет."),
    ("operator", "Оқасы жоқ! Сау болыңыз!"),
]), target=["C2"], expected={"C1": "yes", "C2": "na", "C3": "na", "C5": "yes", "C7": "yes", "C8": "no"},
    reading={"C2": "default"},
    rule="C2 N/A: «данных для ответа хватало с самого начала»; C3 N/A: общий вопрос о тарифах",
    why="Клиент сразу назвал тариф и рынок; уточнять было нечего → C2 N/A. Тарифный вопрос → C3 N/A.")

case("syn-c2-04-ru", "RU", chat("RU", "Не приходит отчёт брокера на почту.", [
    ("operator", "Здравствуйте! Меня зовут Тимур. Ежемесячный отчёт формируется до 5-го числа и приходит на почту автоматически, подождите, пожалуйста."),
    ("client", "Мне нужен годовой для налоговой, а не ежемесячный. Сегодня уже 20-е."),
    ("operator", "Годовой отчёт можно заказать в «Кабинете» → «Отчёты» → «Годовой», он придёт на почту в течение часа."),
    ("client", "Хорошо."),
    ("operator", "Хорошего дня!"),
], bot_extra=["За какой период нужен отчёт?", ("client", "Соедините с оператором.")]),
    target=["C2"], expected={"C1": "yes", "C2": "no", "C3": "na", "C8": "no"},
    rule="C2: ответ вслепую, когда не хватало детали (период), и ответ не подошёл = Нет",
    why="Бот спросил период, клиент не ответил; оператор не уточнил и ответил про ежемесячный отчёт, клиенту нужен годовой → Нет.")

case("syn-c2-05-en", "EN", chat("EN", "How do I change the phone number linked to my account?", [
    ("operator", "Hi, I'm Laura. To change your email, open Profile → Settings → Email and confirm the new address."),
    ("client", "I asked about the phone number, not email."),
    ("operator", "Sorry about that. The phone number is changed only at a branch office with your ID. Have a nice day!"),
]), target=["C2"], expected={"C1": "yes", "C2": "no", "C3": "na", "C8": "no"},
    accept={"C4": [15, 20, 10]},
    rule="C2: отвечает на другой вопрос = Нет",
    why="Клиент спросил про телефон, оператор ответил про email (не учёл известную деталь запроса) → Нет.")

case("syn-c2-06-ru", "RU", chat("RU", "Какой минимальный срок для ИИС, чтобы получить налоговый вычет?", [
    ("operator", "Здравствуйте! Меня зовут Вера. Чтобы получить вычет по ИИС, счёт должен быть открыт не менее 3 лет."),
    ("client", "Спасибо, понятно."),
    ("operator", "Пожалуйста! Обращайтесь. До свидания!"),
]), target=["C2"], expected={"C1": "yes", "C2": "na", "C3": "na", "C5": "yes", "C7": "yes", "C8": "no"},
    reading={"C2": "default"},
    rule="C2 N/A: данных хватало с начала",
    why="Общий вопрос, все данные для ответа есть в первом сообщении, уточнять нечего → N/A.")

# =================================================================== C3 — identification before disclosure / protected action
case("syn-c3-01-ru", "RU", chat("RU", "Почему у меня заблокирован вывод денег?", [
    ("operator", "Здравствуйте! Меня зовут Айгерим. Вижу на вашем счёте ограничение на вывод по требованию комплаенс-службы от 3 ноября."),
    ("client", "И что делать?"),
    ("system", "Личность клиента подтверждена."),
    ("operator", "Нужно загрузить подтверждение источника средств в разделе «Документы». После проверки ограничение снимут в течение 2 рабочих дней."),
    ("client", "Понял, спасибо."),
    ("operator", "Пожалуйста! Если будут вопросы — пишите. До свидания!"),
]), target=["C3"], expected={"C1": "yes", "C3": "no", "C5": "yes", "C7": "yes", "C8": "no"},
    rule="C3: «проверка личности подтверждена ДО раскрытия данных»",
    why="Оператор раскрыл ограничение на счёте до системного «Личность клиента подтверждена.» → Нет (подтверждение пришло после раскрытия).")

case("syn-c3-02-en", "EN", chat("EN", "What is the commission for buying US stocks on the standard plan?", [
    ("operator", "Hello, I'm Kevin. On the standard plan the commission for US stocks is 0.1% of the trade amount, minimum 1.5 USD."),
    ("client", "Thanks!"),
    ("operator", "You're welcome! Have a nice day!"),
]), target=["C3"], expected={"C1": "yes", "C2": "na", "C3": "na", "C4": 20, "C5": "yes", "C7": "yes", "C8": "no"},
    rule="Памятка: «Общий вопрос о тарифах не требует идентификации» → C3 N/A",
    why="Вопрос о тарифе без данных конкретного счёта → N/A.")

case("syn-c3-03-kz", "KZ", chat("KZ", "Кеше ақша шығаруға өтінім бердім, әлі келмеді. Статусы қандай?", [
    ("operator", "Сәлеметсіз бе! Менің атым Дәурен. Тексеру үшін қосымшадағы сілтеме арқылы жеке басыңызды растаңызшы."),
    ("system", "Клиенттің жеке басы расталды."),
    ("operator", "Рақмет. Өтініміңіз орындалды, ақша бүгін 18:00-ге дейін картаңызға түседі."),
    ("client", "Жақсы, рақмет!"),
    ("operator", "Оқасы жоқ! Тағы сұрақ болса, жазыңыз. Сау болыңыз!"),
]), target=["C3"], expected={"C1": "yes", "C3": "yes", "C4": 20, "C5": "yes", "C7": "yes", "C8": "no"},
    rule="C3: подтверждение системным сообщением до раскрытия статуса",
    why="Системное «Клиенттің жеке басы расталды.» пришло до сообщения статуса выплаты → Да.")

case("syn-c3-04-ru", "RU", chat("RU", "Поменяйте, пожалуйста, мой номер телефона на +7 701 000 00 00.", [
    ("operator", "Здравствуйте! Меня зовут Саule. Готово, номер в профиле изменён на +7 701 000 00 00."),
    ("client", "Спасибо!"),
    ("operator", "Пожалуйста! Хорошего дня!"),
]), target=["C3"], expected={"C1": "yes", "C3": "no", "C5": "yes", "C7": "yes"},
    rule="Памятка: «требовалась проверка личности перед защищённым действием и она не подтверждена → Нет»",
    why="Изменение номера телефона — защищённое действие; подтверждения личности в чате нет → Нет.")

case("syn-c3-05-en", "EN", chat("EN", "Where can I download my broker report?", [
    ("operator", "Hi, I'm Sara from support. In the web version open Cabinet → Reports, choose the period and press Download."),
    ("client", "Found it, thank you."),
    ("operator", "Great! Anything else I can help with? Bye!"),
]), target=["C3"], expected={"C1": "yes", "C3": "na", "C4": 20, "C5": "yes", "C7": "yes", "C8": "no"},
    rule="C3 N/A: навигационный вопрос не требует идентификации",
    why="Где найти отчёт — без раскрытия данных счёта и без действий по нему → N/A.")

case("syn-c3-06-ru", "RU", chat("RU", "Подскажите, какой у меня сейчас остаток на счёте?", [
    ("operator", "Здравствуйте! Меня зовут Камила. Для этого нужно подтвердить личность — пройдите, пожалуйста, проверку по ссылке в чате."),
    ("system", "Личность клиента подтверждена."),
    ("operator", "Спасибо! Свободный остаток на вашем счёте — 152 300 ₸."),
    ("client", "Спасибо."),
    ("operator", "Пожалуйста! Хорошего вечера!"),
]), target=["C3"], expected={"C1": "yes", "C3": "yes", "C5": "yes", "C7": "yes", "C8": "no"},
    rule="C3: остаток счёта раскрыт после подтверждения",
    why="Остаток назван после системного «Личность клиента подтверждена.» → Да.")

# =================================================================== C4 — consultation scale 0/5/10/15/20, N/A
case("syn-c4-01-ru", "RU", chat("RU", "Как вывести деньги на карту? И главное — сколько за это берут комиссию?", [
    ("operator", "Здравствуйте! Меня зовут Асель. В приложении откройте «Вывод», выберите карту, введите сумму и подтвердите кодом из SMS."),
    ("client", "А комиссия?"),
    ("operator", "Хорошего дня!"),
    ("system", CLOSED["RU"]),
]), target=["C4"], expected={"C1": "yes", "C3": "na", "C4": 10, "C7": "no", "C8": "no"},
    rule="C4 = 10: «Часть запроса закрыта, существенный вопрос остался открытым»",
    why="Закрыт вопрос «как вывести», главный вопрос о комиссии (повторён клиентом) остался без ответа → 10.")

case("syn-c4-02-en", "EN", chat("EN", "How do I get my 2025 broker report for the tax office? Also, roughly how long does it take to generate?", [
    ("operator", "Hello, I'm Mark. Open Cabinet → Reports → Annual, select 2025 and press Order. The report is signed electronically and accepted by the tax office."),
    ("client", "Great, ordering now. Thanks!"),
    ("operator", "You're welcome! Have a nice day!"),
]), target=["C4"], expected={"C1": "yes", "C3": "na", "C4": 15, "C5": "yes", "C7": "yes", "C8": "no"},
    rule="C4 = 15: «Основной вопрос решён, пропущена второстепенная деталь»",
    why="Как получить отчёт — ответ полный; второстепенный вопрос о сроке формирования пропущен → 15.")

case("syn-c4-03-kz", "KZ", chat("KZ", "Сәлеметсіз бе", [
    ("operator", "Сәлеметсіз бе! Менің атым Жанар. Сізге қалай көмектесе аламын?"),
    ("system", LEFT["KZ"]),
]), target=["C4", "C7"], expected={"C1": "yes", "C3": "na", "C4": "na", "C5": "yes", "C7": "na", "C8": "no"},
    rule="C4 N/A: «чат закончился до того, как клиент сформулировал запрос»; C7 N/A: «чат оборвался»",
    why="Клиент только поздоровался, связь прервалась до формулировки запроса → C4 N/A, C7 N/A (C3 — запроса нет).")

case("syn-c4-04-ru", "RU", chat("RU", "Как подать поручение на участие в IPO?", [
    ("operator", "Здравствуйте! Меня зовут Данияр. Всё есть в приложении, посмотрите там."),
    ("client", "А где именно?"),
    ("operator", "В приложении, в разделах. Хорошего дня!"),
]), target=["C4"], expected={"C1": "yes", "C3": "na", "C4": 5, "C8": "no"},
    accept={"C4": [0]},
    rule="C4 = 5: «Есть общая подсказка, но клиенту всё ещё непонятно, как действовать»",
    why="Общая отсылка «всё есть в приложении» без конкретики; клиент переспрашивает и не получает пути → 5.")

case("syn-c4-05-en", "EN", chat("EN", "My limit order was not executed although the price touched my level. Why?", [
    ("operator", "Hi, I'm Alex. I don't know, nothing can be done about it."),
    ("client", "That's it?"),
    ("operator", "Yes. Bye."),
]), target=["C4"], expected={"C1": "yes", "C4": 0, "C5": "no", "C8": "no"},
    rule="C4 = 0: «Ответа по существу нет»",
    why="Отказ «не знаю, ничего не сделать» без объяснения и следующего шага → 0.")

case("syn-c4-06-ru", "RU", chat("RU", "Мне пришло списание 5 000 ₸ «сервисный сбор», я не понимаю за что.", [
    ("operator", "Здравствуйте! Меня зовут Алия. Для проверки списания подтвердите, пожалуйста, личность по ссылке в чате."),
    ("system", VERIFIED["RU"]),
    ("operator", "Спасибо! Такие списания проверяет бэк-офис. Я передала запрос, ответ придёт на вашу почту в течение 2 рабочих дней. Номер обращения — 48213."),
    ("client", "Хорошо, буду ждать."),
    ("operator", "Спасибо за ожидание и понимание! Если будут вопросы — пишите. До свидания!"),
]), target=["C4"], expected={"C1": "yes", "C3": "yes", "C4": 20, "C5": "yes", "C7": "yes", "C8": "no"},
    rule="C4 = 20: «дан корректный следующий шаг»",
    why="Решить в чате нельзя; оператор передал в бэк-офис, назвал срок, канал ответа и номер обращения → 20.")

case("syn-c4-07-ru", "RU", chat("RU", "Как пополнить счёт через Kaspi?", [
    ("operator", "Здравствуйте! Меня зовут Руслан. В приложении Kaspi откройте «Платежи» → «Инвестиции» → «Freedom Broker», введите номер договора и сумму. Деньги зачисляются в течение 15 минут."),
    ("system", LEFT["RU"]),
]), target=["C4", "C7"], expected={"C1": "yes", "C3": "na", "C4": 20, "C5": "yes", "C7": "na", "C8": "no"},
    rule="C4: обрыв после ответа не делает C4 неприменимым; C7 N/A: «чат оборвался до возможности его завершить»",
    why="Ответ полный → C4 20; клиент ушёл сразу после ответа, у оператора не было возможности попрощаться → C7 N/A.")

# =================================================================== C5 — empathy
case("syn-c5-01-ru", "RU", chat("RU", "Я уже третий раз пишу! Деньги неделю не приходят на карту, мне это надоело.", [
    ("operator", "Здравствуйте, меня зовут Игорь. Подтвердите личность по ссылке, пожалуйста."),
    ("system", VERIFIED["RU"]),
    ("operator", "Вывод в статусе «Отправлен». Срок зачисления — до 5 рабочих дней."),
    ("client", "Уже прошло 7! Сколько можно ждать?"),
    ("operator", "Срок зачисления — до 5 рабочих дней. До свидания."),
]), target=["C5"], expected={"C1": "yes", "C3": "yes", "C5": "no", "C8": "no"},
    rule="C5: «если клиент выразил недовольство… отреагировал на конкретную проблему»",
    why="Клиент дважды выразил недовольство («надоело», «сколько можно ждать»); оператор не признал проблему и повторил шаблон → Нет.")

case("syn-c5-02-en", "EN", chat("EN", "I'm really nervous, my stop-loss didn't trigger and the price keeps falling!", [
    ("operator", "Hello, I'm Emma. I understand how stressful this is — let's sort it out right now. Please confirm your identity via the link in the chat."),
    ("system", VERIFIED["EN"]),
    ("operator", "Thank you. Your stop-loss was set for the main session, and the drop happened in pre-market, so it didn't trigger. It is still active and will work once the main session opens at 19:30 Astana time. If you prefer, you can sell now with a limit order in pre-market."),
    ("client", "Ok, I'll wait for the session. Thanks for explaining."),
    ("operator", "You're welcome. I'm sorry for the worry this caused. Is there anything else? Take care!"),
]), target=["C5"], expected={"C1": "yes", "C3": "yes", "C4": 20, "C5": "yes", "C7": "yes", "C8": "no"},
    rule="C5: реакция на конкретное неудобство",
    why="Оператор признал волнение клиента, объяснил причину и предложил варианты → Да.")

case("syn-c5-03-kz", "KZ", chat("KZ", "Облигацияны қалай сатып аламын? Таба алмадым.", [
    ("operator", "Сәлеметсіз бе, менің атым Бауыржан. Нұсқаулықты оқысаңыз, сұрамас едіңіз. «Нарық» → «Облигациялар» бөліміне кіріңіз."),
    ("client", "Жақсы..."),
    ("operator", "Сау болыңыз."),
]), target=["C5"], expected={"C1": "yes", "C3": "na", "C5": "no", "C8": "no"},
    rule="C5: сарказм/пренебрежение = Нет",
    why="«Нұсқаулықты оқысаңыз, сұрамас едіңіз» (прочитали бы инструкцию — не спрашивали бы) — пренебрежительный тон → Нет; это упрёк, а не оскорбление → C8 Нет.")

case("syn-c5-04-ru", "RU", chat("RU", "Где посмотреть дату выплаты купона по облигации?", [
    ("operator", "Здравствуйте, меня зовут Арман. Вы что, читать не умеете? Там же всё написано, тупой вопрос."),
    ("client", "Можно повежливее?"),
    ("operator", "Карточка облигации, раздел «Купоны»."),
]), target=["C5", "C8"], expected={"C1": "yes", "C3": "na", "C5": "no", "C8": "yes"},
    rule="C5: оскорбление — всегда Нет; C8: «оскорбление клиента»",
    why="«Вы что, читать не умеете? … тупой вопрос» — оскорбление клиента → C5 Нет, C8 Да.")

case("syn-c5-05-en", "EN", chat("EN", "Can I open a second account in euros?", [
    ("operator", "Hello, I'm Nina. Yes — in the app go to Accounts → Open new account and choose EUR. It's free and opens instantly."),
    ("client", "Perfect, thanks."),
    ("operator", "My pleasure! Anything else? Have a lovely day!"),
]), target=["C5"], expected={"C1": "yes", "C3": "na", "C4": 20, "C5": "yes", "C6": 10, "C7": "yes", "C8": "no"},
    rule="C5: без недовольства клиента для «Да» достаточно вежливости",
    why="Клиент не жаловался, оператор вежлив → Да.")

case("syn-c5-06-ru", "RU", chat("RU", "Хочу отменить поручение № 77120 на покупку Apple.", [
    ("operator", "Здравствуйте! Меня зовут Лаура. Назовите, пожалуйста, номер поручения."),
    ("client", "Вы серьёзно? Я же писал выше: 77120."),
    ("operator", "Простите, пожалуйста, вы действительно его указали. Подтвердите личность по ссылке в чате, и я сразу отменю поручение."),
    ("system", VERIFIED["RU"]),
    ("operator", "Поручение № 77120 отменено, деньги разблокированы на счёте."),
    ("client", "Спасибо."),
    ("operator", "Пожалуйста! Ещё раз извините за неудобство. Хорошего дня!"),
], bot_extra=["Уточните номер поручения, пожалуйста.", ("client", "77120. Соедините с оператором.")]),
    target=["C5", "C2"], expected={"C1": "yes", "C2": "no", "C3": "yes", "C4": 20, "C5": "yes", "C7": "yes", "C8": "no"},
    rule="C5: отреагировал на выраженное недовольство; C2: переспрос уже сообщённого (в т.ч. боту) = Нет",
    why="Оператор переспросил номер, уже сообщённый клиентом и боту → C2 Нет; на «Вы серьёзно?» извинился и решил → C5 Да.")

# =================================================================== C6 — clarity, order, warning about waiting
case("syn-c6-01-ru", "RU", chat("RU", "Почему мне начислили налог на дивиденды 15%, а не 10%?", [
    ("operator", "Здравствуйте! Меня зовут Гульнара. Подтвердите личность по ссылке, пожалуйста.", "01:20"),
    ("system", VERIFIED["RU"], "01:40"),
    ("operator", "Проверила в системе: у вас не загружена форма W-8BEN, поэтому применена ставка 15%. Загрузите форму в разделе «Документы», и следующие дивиденды будут облагаться по ставке 10%.", "07:55"),
    ("client", "Понятно, загружу.", "08:20"),
    ("operator", "Спасибо! Если будут вопросы — пишите. До свидания!", "08:40"),
]), target=["C6"], expected={"C1": "yes", "C3": "yes", "C4": 20, "C5": "yes", "C6": 7, "C7": "yes", "C8": "no"},
    rule="C6: «Если было явное ожидание, оператор предупредил» — ожидание без предупреждения не выше 7",
    why="Оператор ушёл проверять (≈6 минут между репликами, «Проверила в системе»), не предупредив и не поблагодарив за ожидание → 7.")

case("syn-c6-02-en", "EN", chat("EN", "Why was 5 USD charged from my account yesterday?", [
    ("operator", "Hello, I'm Leo. Please verify your identity via the link in the chat.", "01:20"),
    ("system", VERIFIED["EN"], "01:45"),
    ("operator", "Thank you! Let me check the transaction history, it will take about 3 minutes, please stay in the chat.", "02:00"),
    ("operator", "Thanks for waiting! The 5 USD is the monthly market data subscription for NASDAQ Level 2. You can turn it off in Settings → Subscriptions.", "05:10"),
    ("client", "Oh, I forgot about it. Thanks!", "05:30"),
    ("operator", "You're welcome! Anything else I can help with? Have a good day!", "05:50"),
]), target=["C6"], expected={"C1": "yes", "C3": "yes", "C4": 20, "C5": "yes", "C6": 10, "C7": "yes", "C8": "no"},
    rule="C6 = 10: ясно, предупредил об ожидании",
    why="Оператор предупредил об ожидании, назвал срок и поблагодарил после → 10.")

case("syn-c6-03-kz", "KZ", chat("KZ", "Шоттан ақшаны қалай шығарамын?", [
    ("operator", "Сәлеметсіз бе! Менің атым Ғалым. Ол жерде бөлімде батырма бар, соны басасыз да, әрі қарай сол жақтан."),
    ("client", "Түсінбедім. Қай бөлім, қай батырма?"),
    ("operator", "Кешіріңіз. Қосымшада «Шығару» бөлімін ашыңыз, картаны таңдап, соманы енгізіп, SMS-кодпен растаңыз."),
    ("client", "Енді түсінікті, рақмет."),
    ("operator", "Оқасы жоқ! Сау болыңыз!"),
]), target=["C6"], expected={"C1": "yes", "C3": "na", "C5": "yes", "C6": 4, "C7": "yes", "C8": "no"},
    accept={"C6": [7]},
    rule="C6 = 4: «Клиенту приходится переспрашивать из-за формулировок»",
    why="Первый ответ невнятный («ол жерде бөлімде батырма бар…»), клиент вынужден переспросить → 4.")

case("syn-c6-04-ru", "RU", chat("RU", "Сколько стоит обслуживание счёта в месяц?", [
    ("operator", "Здравствуйте, меня зовут Нурлан. Обслуживание бесплатное."),
    ("client", "Точно бесплатное?"),
    ("operator", "Нет, 1 000 ₸ в месяц. Хотя по вашему тарифу может быть и 0. Или это про депозитарий. Карту можно заказать в приложении."),
    ("client", "Так сколько в итоге? И при чём тут карта?"),
    ("operator", "Ну зависит. В целом бесплатно, но платно. Всего доброго."),
]), target=["C6"], expected={"C1": "yes", "C3": "na", "C6": 0, "C8": "no"},
    accept={"C6": [4]},
    rule="C6 = 0: «Из-за бессвязных или противоречивых реплик ход диалога непонятен»",
    why="Оператор противоречит себе («бесплатное» → «1 000 ₸» → «бесплатно, но платно»), вставляет нерелевантное про карту; клиент не понимает итог → 0 (4 — допустимая соседняя ступень).")

case("syn-c6-05-en", "EN", chat("EN", "How long does a transfer from Freedom Bank to my brokerage account take?", [
    ("operator", "Hello, I'm Chris. Your card delivery is scheduled for Friday."),
    ("operator", "Sorry, that message was meant for another chat. A transfer from Freedom Bank to your brokerage account is instant, usually within 1 minute."),
    ("client", "Ok, thanks."),
    ("operator", "You're welcome! Have a nice day!"),
]), target=["C6"], expected={"C1": "yes", "C3": "na", "C4": 20, "C6": 7, "C7": "yes", "C8": "no"},
    accept={"C6": [4]},
    rule="C6 = 7: «Один заметный сбой, но клиент может понять ответ и продолжить»",
    why="Одна реплика не по адресу, оператор сам исправился, клиенту не пришлось переспрашивать → 7.")

case("syn-c6-06-ru", "RU", chat("RU", "Какая минимальная сумма для покупки облигаций и есть ли комиссия?", [
    ("operator", "Здравствуйте! Меня зовут Жанна. Отвечу по порядку. 1) Минимальная сумма — стоимость одной облигации, обычно около 1 000 ₸. 2) Комиссия по вашему вопросу — по тарифу, для облигаций KASE она 0,05% от суммы сделки."),
    ("client", "Всё ясно, спасибо!"),
    ("operator", "Пожалуйста! Если будут ещё вопросы — пишите. До свидания!"),
]), target=["C6"], expected={"C1": "yes", "C3": "na", "C4": 20, "C5": "yes", "C6": 10, "C7": "yes", "C8": "no"},
    rule="C6 = 10 (позитивный контроль): ясно и по порядку",
    why="Оба вопроса закрыты по порядку, без сбоев → 10.")

# =================================================================== C7 — closing
case("syn-c7-01-ru", "RU", chat("RU", "Где поменять пароль?", [
    ("operator", "Здравствуйте! Меня зовут Олжас. «Профиль» → «Безопасность» → «Сменить пароль»."),
    ("client", "Спасибо, сделал."),
    ("operator", "Хорошо."),
    ("system", CLOSED["RU"]),
]), target=["C7"], expected={"C1": "yes", "C3": "na", "C4": 20, "C7": "no", "C8": "no"},
    rule="C7: «закрыл чат без прощания» = Нет (пример обоснования из памятки)",
    why="После решённого вопроса оператор ответил «Хорошо.» и закрыл чат без прощания и без предложения помощи → Нет.")

case("syn-c7-02-en", "EN", chat("EN", "I need help with a margin call on my account, what should I do?", [
    ("operator", "Hello, I'm Victor. Please verify your identity via the link so I can look at your positions."),
    ("system", VERIFIED["EN"]),
    ("operator", "Thank you. Your margin level is 112%. You can either deposit funds or close part of the positions. Which option do you prefer?"),
    ("system", LEFT["EN"]),
]), target=["C7"], expected={"C1": "yes", "C3": "yes", "C7": "na", "C8": "no"},
    rule="C7 N/A: «чат оборвался до возможности его завершить»",
    why="Связь оборвалась, пока оператор ждал ответа клиента на вопрос; завершить было невозможно → N/A.")

case("syn-c7-03-kz", "KZ", chat("KZ", "Акцияларды басқа брокерге қалай ауыстырамын?", [
    ("operator", "Сәлеметсіз бе! Менің атым Айдана. Ол үшін қосымшада «Құжаттар» → «Бағалы қағаздарды аудару» өтінімін толтырыңыз, 3 жұмыс күні ішінде орындалады."),
    ("client", "Түсінікті, рақмет."),
    ("operator", "Қорытындылайын: өтінімді «Құжаттар» бөлімінде толтырасыз, аудару 3 жұмыс күні. Тағы сұрақтарыңыз болса, жазыңыз. Сау болыңыз!"),
]), target=["C7"], expected={"C1": "yes", "C3": "na", "C4": 20, "C5": "yes", "C7": "yes", "C8": "no"},
    rule="C7 = Да: «подвёл итог или предложил дальнейшую помощь и попрощался»",
    why="Итог, предложение помощи и прощание → Да.")

case("syn-c7-04-ru", "RU", chat("RU", "Мне не пришли дивиденды по Halyk Bank.", [
    ("operator", "Здравствуйте! Меня зовут Ержан. Подтвердите личность по ссылке, пожалуйста."),
    ("system", VERIFIED["RU"]),
    ("operator", "Передал ваш вопрос в депозитарий."),
    ("client", "А мне ответят здесь или на почту? И когда?"),
    ("operator", "До свидания."),
    ("system", CLOSED["RU"]),
]), target=["C7"], expected={"C1": "yes", "C3": "yes", "C7": "no", "C8": "no"},
    accept={"C4": [10, 5]},
    rule="C7 = Нет: попрощался, когда вопрос клиента ещё открыт",
    why="На вопрос «где и когда ответят» оператор ответил «До свидания.» и закрыл чат → Нет.")

case("syn-c7-05-en", "EN", chat("EN", "How do I enable two-factor authentication?", [
    ("operator", "Hi, I'm Olivia. Go to Profile → Security → Two-factor authentication and follow the steps."),
    ("client", "Done. And can I use Google Authenticator instead of SMS?"),
    ("system", CLOSED["EN"]),
]), target=["C7"], expected={"C1": "yes", "C3": "na", "C7": "no", "C8": "no"},
    accept={"C4": [10]},
    rule="C7 = Нет: последняя реплика клиента без ответа, оператор закрыл чат",
    why="Клиент задал новый вопрос, оператор закрыл чат без ответа и прощания (закрыл сам — не обрыв) → Нет.")

case("syn-c7-06-kz", "KZ", chat("KZ", "Депозитарлық комиссия қашан алынады?", [
    ("operator", "Сәлеметсіз бе! Менің атым Әлия. Депозитарлық комиссия әр айдың соңғы жұмыс күні алынады."),
    ("client", "Рақмет!"),
    ("operator", "Оқасы жоқ! Тағы көмек керек болса, жазыңыз. Сау болыңыз!"),
]), target=["C7"], expected={"C1": "yes", "C2": "na", "C3": "na", "C5": "yes", "C7": "yes", "C8": "no"},
    rule="C7 = Да: предложил помощь и попрощался",
    why="«Тағы көмек керек болса, жазыңыз. Сау болыңыз!» → Да.")

# =================================================================== C8 — gross errors (yes = violation)
case("syn-c8-01-ru", "RU", chat("RU", "Хочу начать инвестировать, но не знаю, с чего начать.", [
    ("operator", "Здравствуйте! Меня зовут Бекзат. Могу помочь лично: переведите мне на карту сумму, которую готовы вложить, я сам буду управлять и приносить доход 5% в месяц."),
    ("client", "Нет, спасибо, я лучше сам через приложение."),
    ("operator", "Как хотите. В приложении есть раздел «Обучение» с подборками для новичков. Хорошего дня!"),
]), target=["C8"], expected={"C1": "yes", "C8": "yes"},
    rule="Памятка: «предлагает перевести деньги ему лично… Достаточно самого предложения. Согласие клиента… не требуется»",
    why="Предложение перевести деньги оператору лично для управления; клиент отказался — флаг всё равно ставится → Да.")

case("syn-c8-02-en", "EN", chat("EN", "Which stocks would you recommend for a long-term portfolio?", [
    ("operator", "Hello, I'm Ryan. I can't recommend specific stocks in this chat. But give me your personal phone number and I'll consult you privately for a small fee — much better picks than the app."),
    ("client", "Hmm, I'll think about it."),
    ("operator", "Sure. The app also has a Model Portfolios section. Have a nice day!"),
]), target=["C8"], expected={"C1": "yes", "C8": "yes"},
    rule="Памятка: «просит личный номер клиента, чтобы оказать платную консультацию в частном порядке»",
    why="Запрос личного номера ради платной частной консультации; согласие не требуется → Да.")

case("syn-c8-03-kz", "KZ", chat("KZ", "Неге менің өтінімім әлі орындалмады? Үш рет жаздым!", [
    ("operator", "Сәлеметсіз бе, менің атым Серік. Сіз ақымақсыз ба? Биржа жабық, түнде ешкім сатып алмайды."),
    ("client", "Неге дөрекісіз?"),
    ("operator", "Биржа ертең 11:00-де ашылады."),
]), target=["C8", "C5"], expected={"C1": "yes", "C5": "no", "C8": "yes"},
    rule="C8: «оскорбление клиента»; C5: оскорбление — всегда Нет",
    why="«Сіз ақымақсыз ба?» (вы что, дурак?) — оскорбление клиента → C8 Да, C5 Нет.")

case("syn-c8-04-ru", "RU", chat("RU", "Мой брат тоже открывал у вас счёт. Можно узнать, сколько у него там денег? Хочу сделать ему подарок.", [
    ("operator", "Здравствуйте! Меня зовут Мадина. Да, вижу: Ерлан Сейткалиев, на его счёте сейчас около 2,4 млн ₸."),
    ("client", "Ого, спасибо!"),
    ("operator", "Пожалуйста! Хорошего дня!"),
]), target=["C8"], expected={"C1": "yes", "C8": "yes"},
    rule="C8: «раскрытие конфиденциальной информации»",
    why="Оператор раскрыл ФИО и баланс счёта третьего лица → Да.")

case("syn-c8-05-en", "EN", chat("EN", "Do you have any personal advisory service?", [
    ("operator", "Hello, I'm Grace. Yes — Freedom Broker has the Premium Advisory plan for 30 USD per month: a personal adviser from the company and weekly portfolio reviews. You can subscribe in the app under Services."),
    ("client", "Sounds good, thanks."),
    ("operator", "You're welcome! Anything else? Have a nice day!"),
]), target=["C8"], expected={"C1": "yes", "C3": "na", "C5": "yes", "C7": "yes", "C8": "no"},
    rule="C8 (негативный контроль): платная услуга компании — не «в обход компании»",
    why="Предложена официальная услуга компании через приложение, не частная → Нет.")

case("syn-c8-06-ru", "RU", chat("RU", "Можно, чтобы со мной связался персональный менеджер?", [
    ("operator", "Здравствуйте! Меня зовут Ольга. Да, я оставила заявку: ваш персональный менеджер Freedom Broker позвонит вам сегодня до 18:00 по номеру из профиля."),
    ("client", "Отлично, спасибо!"),
    ("operator", "Пожалуйста! Хорошего дня!"),
]), target=["C8"], expected={"C1": "yes", "C4": 20, "C5": "yes", "C7": "yes", "C8": "no"},
    rule="C8 (негативный контроль): звонок менеджера компании по номеру из профиля — не частная консультация",
    why="Связь через официального менеджера компании, без оплаты лично оператору → Нет.")

case("syn-c8-07-kz", "KZ", chat("KZ", "Опциондармен сауда жасауды үйреткіңіз келе ме?", [
    ("operator", "Сәлеметсіз бе! Менің атым Ерболат. Чатта толық айта алмаймын, бірақ менің жеке нөмірім +7 705 111 22 33, WhatsApp-қа жазыңыз — ақылы түрде жеке өзім үйретемін."),
    ("client", "Ойланып көрейін."),
    ("operator", "Жақсы. Сау болыңыз!"),
]), target=["C8"], expected={"C1": "yes", "C8": "yes"},
    rule="C8: предложение платной консультации в частном порядке (в обход компании)",
    why="Оператор даёт личный номер и предлагает платное частное обучение → Да.")

case("syn-c8-08-ru", "RU", chat("RU", "Сколько у меня сейчас денег на брокерском счёте?", [
    ("operator", "Здравствуйте! Меня зовут Тимур. На вашем счёте 845 000 ₸ и 1 200 USD."),
    ("client", "Спасибо."),
    ("operator", "Пожалуйста! Хорошего дня!"),
]), target=["C3", "C8"], expected={"C1": "yes", "C3": "no", "C5": "yes", "C7": "yes", "C8": "no"},
    reading={"C8": "explicit_triggers_only"},
    rule="C3: раскрытие данных счёта без идентификации = Нет; трактовка команды: это C3, а не C8",
    why="Баланс самого клиента раскрыт без подтверждения личности → C3 Нет; по трактовке команды (C8 только явные триггеры) → C8 Нет.")

# =================================================================== context: bot / system, long calls with pauses
case("syn-ctx-01-ru", "RU", chat("RU", "Хочу закрыть брокерский счёт. Что для этого нужно?", [
    ("operator", "Здравствуйте! Меня зовут Динара. Вижу, вы уже ответили боту, что счёт пустой и выводить ничего не нужно. Тогда подайте заявление на закрытие в приложении: «Документы» → «Закрытие счёта». Счёт закроют в течение 3 рабочих дней."),
    ("client", "Отлично, так и сделаю."),
    ("operator", "Хорошо! Если появятся вопросы — пишите. До свидания!"),
], bot_extra=["Есть ли на счёте деньги или ценные бумаги?", ("client", "Нет, счёт пустой, выводить ничего не надо."),
              "Для закрытия счёта нужно подать заявление. Соединить вас с оператором?", ("client", "Да.")]),
    target=["C2", "C1"], expected={"C1": "yes", "C2": "yes", "C3": "na", "C4": 20, "C5": "yes", "C7": "yes", "C8": "no"},
    accept={"C2": ["na"]}, reading={"C2": "default (bot_yes → yes)"},
    rule="Бот и система — контекст: детали, сообщённые боту, — известные детали (C2); приветствие бота не засчитывается оператору (C1)",
    why="Оператор опёрся на ответ клиента боту и не переспросил → C2 Да (N/A допустим по прочтению default). Оператор сам поздоровался и представился → C1 Да.")

case("syn-ctx-02-en", "EN", chat("EN", "How do I reset my trading password?", [
    ("operator", "As the assistant said — Profile → Security → Reset trading password."),
    ("client", "Ok."),
    ("operator", "Bye."),
], bot_extra=["Hello, I'm AIM. To reset the trading password open Profile → Security → Reset trading password.", ("client", "Agent please.")]),
    target=["C1", "C7"], expected={"C1": "no", "C3": "na", "C7": "yes", "C8": "no"},
    accept={"C7": ["no"], "C5": ["yes", "no"]},
    rule="C1: приветствие и представление бота оператору не засчитываются",
    why="Бот поздоровался и представился, оператор — нет → C1 Нет. «Bye.» после решённого вопроса — прощание, но без итога/помощи (C7 спорно).")

CALL_URL = "https://example.invalid/synthetic-call.mp3"
case("syn-call-01-ru", "RU", call("2026-11-03T10:00:00+00:00", [
    (0, "system", "Запись и транскрибирование запущены."),
    (3, "bot", "Здравствуйте, добро пожаловать во Фридом брокер. Для выбора русского языка нажмите 2."),
    (9, "client", "2"),
    (14, "system", "Клиент переведен от агента на представителя службы поддержки"),
    (20, "system", "Агент присоединился к разговору с клиентом."),
    (22, "operator", "Фридом брокер, здравствуйте, меня зовут [Имя скрыто]."),
    (24, "operator", "Чем могу помочь?"),
    (30, "client", "Здравствуйте. У меня заблокировали счёт, не могу купить акции."),
    (36, "operator", "Понимаю, неприятная ситуация, сейчас разберёмся. Для проверки назовите, пожалуйста, ваше ФИО и ИИН."),
    (48, "client", "Нурланов Арман Серикович, ИИН девяносто ноль один ноль один, три ноль ноль, один два три."),
    (60, "operator", "Спасибо, данные совпадают, личность подтверждена."),
    (64, "operator", "Мне нужно уточнить причину у бэк-офиса, это займёт около двух минут. Оставайтесь, пожалуйста, на линии."),
    (70, "client", "Хорошо, жду."),
    (72, "system", "Клиент находится на удержании."),
    (80, "operator", "Коллеги, по Нурланову ограничение откуда? А, понял, документы на проверке, спасибо."),
    (205, "system", "Клиент больше не находится на удержании."),
    (208, "operator", "Спасибо за ожидание."),
    (211, "operator", "Ограничение поставлено, потому что не пройдена ежегодная проверка документов."),
    (216, "operator", "Загрузите, пожалуйста, удостоверение личности в приложении в разделе документы. После проверки, обычно в течение одного рабочего дня, ограничение снимут, и я вам отправлю СМС."),
    (232, "client", "Э-э, понял. А сейчас вообще ничего нельзя купить?"),
    (238, "operator", "До снятия ограничения покупки недоступны, но продать бумаги и вывести деньги вы можете."),
    (246, "client", "Понял, спасибо."),
    (249, "system", "Запись и транскрибирование приостановлены."),
    (262, "system", "Запись и транскрибирование возобновлены."),
    (265, "operator", "Подытожу: загружаете удостоверение, в течение рабочего дня ограничение снимут, придёт СМС. Могу ещё чем-то помочь?"),
    (271, "client", "Нет, всё, спасибо."),
    (274, "operator", "Спасибо за звонок, всего доброго, до свидания."),
    (277, "system", "Клиент завершил разговор."),
]), channel="voice call", audio_url=CALL_URL,
    target=["C3", "C6", "C5"], expected={"C1": "yes", "C3": "yes", "C4": 20, "C5": "yes", "C6": 10, "C7": "yes", "C8": "no"},
    rule="Звонок: сверка ФИО+ИИН = идентификация; удержание с предупреждением и благодарностью = 10; реплики на удержании клиент не слышал",
    why="Идентификация до раскрытия причины → C3 Да; предупредил об удержании и поблагодарил → C6 10; разговор с коллегами на удержании не оценивается; итог, помощь, прощание → C7 Да.")

case("syn-call-02-ru", "RU", call("2026-11-04T07:30:00+00:00", [
    (0, "system", "Запись и транскрибирование запущены."),
    (2, "bot", "Здравствуйте, добро пожаловать во Фридом брокер. Ожидайте ответа специалиста."),
    (20, "system", "Агент присоединился к разговору с клиентом."),
    (23, "operator", "Фридом брокер, здравствуйте, меня зовут [Имя скрыто], слушаю вас."),
    (29, "client", "Здравствуйте, подскажите, какая комиссия за вывод долларов на карту другого банка?"),
    (35, "operator", "Так."),
    (37, "system", "Клиент находится на удержании."),
    (240, "system", "Клиент больше не находится на удержании."),
    (242, "operator", "Комиссия за вывод долларов на карту другого банка — ноль целых пять десятых процента, минимум пять долларов."),
    (250, "client", "Понял. А в тенге если выводить?"),
    (254, "operator", "В тенге на карту любого казахстанского банка вывод бесплатный, зачисление в течение…"),
    (258, "system", "Клиент завершил разговор."),
]), channel="voice call", audio_url=CALL_URL,
    target=["C6", "C7"], expected={"C1": "yes", "C2": "na", "C3": "na", "C5": "yes", "C6": 7, "C7": "na", "C8": "no"},
    accept={"C6": [4]},
    rule="C6: удержание без предупреждения — не выше 7; C7 N/A: клиент положил трубку до прощания",
    why="«Так.» и удержание ~3,5 минуты без предупреждения и без благодарности → 7. Клиент прервал звонок, пока оператор ещё отвечал → C7 N/A.")


def main() -> None:
    ids = [r["conversation_id"] for r in RECORDS]
    assert len(ids) == len(set(ids)), "duplicate ids"
    (HERE / "records.json").write_text(json.dumps(RECORDS, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    with (HERE / "expected.jsonl").open("w", encoding="utf-8") as fh:
        for e in EXPECTED:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    from collections import Counter

    tgt = Counter(c for e in EXPECTED for c in e["target"])
    print(f"{len(RECORDS)} dialogues; targets per criterion: {dict(sorted(tgt.items()))}; "
          f"languages: {dict(Counter(r['language'] for r in RECORDS))}")


if __name__ == "__main__":
    main()
