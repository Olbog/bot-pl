# bot-pl

Telegram-бот для разговорной практики польского: исправляет явные ошибки, подсказывает польские слова вместо русских, отвечает текстом (с транскрипцией и переводом) и голосом.

Схема: Telegram ⇄ бот (Docker на сервере) → Cloudflare Worker (Варшава) → Gemini API. Голос: edge-tts + ffmpeg.

## Команды

- `/new` — новая тема
- `/itog` — новые слова и ошибки за текущий разговор
- `/help` — подсказка

## Первый запуск на сервере

```powershell
cd E:\Bots
git clone https://github.com/Olbog/bot-pl.git
cd bot-pl
copy .env.example .env
notepad .env        # вписать TELEGRAM_TOKEN и PROXY_TOKEN, ALLOWED_USER_IDS пока пустой
docker compose up -d --build
docker compose logs -f   # должно быть «Бот запущен», выход — Ctrl+C
```

Напиши боту что угодно — он ответит «Нет доступа. Твой Telegram ID: …». Впиши этот ID в `ALLOWED_USER_IDS` в `.env` и перезапусти:

```powershell
docker compose up -d
```

## Порядок обновления

1. Claude правит код в `D:\Castro_2\Job\Bot\bot-pl` и кладёт описание коммита в `COMMIT_MSG.txt`.
2. На ноуте: `.\ship.ps1` — коммит и push.
3. На сервере: `cd E:\Bots\bot-pl; .\deploy.ps1` — pull, пересборка, тесты, лог.

Если PowerShell пишет, что выполнение скриптов отключено:
`powershell -ExecutionPolicy Bypass -File .\deploy.ps1` (так же для ship.ps1).

Код запекается в образ, поэтому deploy.ps1 делает `--build`, простого restart недостаточно.

## Тесты

```powershell
docker compose exec bot pytest -q
```

## Данные

База SQLite — `data/bot.db` (в git не попадает). Таблицы: `messages` (история), `words` (новые слова), `corrections` (ошибки).
