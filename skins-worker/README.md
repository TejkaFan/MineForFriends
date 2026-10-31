# Сервер скинов MineForFriends

Скины хранятся в публичном репозитории **TejkaFan/MineForFriends-skins**: мод CustomSkinLoader
у всех игроков читает их прямо оттуда — на любом сервере и в одиночной игре, без Radmin и без ПК хоста.

Загружает скины `worker.js` — бесплатный Cloudflare Worker. Он проверяет файл и токен ника и
коммитит скин в репозиторий своим токеном GitHub. Токен лежит только в настройках Worker,
в лаунчер он не попадает.

## Настройка (один раз)

### 1. Репозиторий для скинов

На GitHub: **New repository** → имя `MineForFriends-skins`, **Public**, галочка **Add a README file** → **Create repository**.

### 2. Токен GitHub для Worker

GitHub → аватар → **Settings** → **Developer settings** → **Personal access tokens** → **Fine-grained tokens** → **Generate new token**:

- Token name: `MineForFriends skins`
- Expiration: максимальный срок (за пару дней до конца GitHub пришлёт письмо — тогда сделать новый и заменить в Worker)
- Repository access: **Only select repositories** → `MineForFriends-skins`
- Permissions → Repository permissions → **Contents: Read and write** и **Pull requests: Read and write**
  (второе — для сборок друзей; у уже созданного токена его можно добавить: открыть токен → **Edit**)

**Generate token** и скопировать его (GitHub покажет его один раз). Никому его не отправляй — он нужен только в шаге 3.

### 3. Worker в Cloudflare

1. Зарегистрироваться на <https://dash.cloudflare.com/sign-up> (бесплатно).
2. **Workers & Pages** → **Create** → **Create Worker** → имя `mineforfriends-skins` → **Deploy**.
3. **Edit code** → удалить всё в `worker.js` → вставить содержимое этого `worker.js` → **Deploy**.
4. Вернуться к Worker → **Settings** → **Variables and Secrets** → **Add**:
   - Type **Text**, имя `SKINS_REPO`, значение `TejkaFan/MineForFriends-skins`
   - Type **Secret**, имя `GITHUB_TOKEN`, значение — токен из шага 2
   
   → **Deploy**.
5. Открыть адрес Worker вида `https://mineforfriends-skins.<твой-поддомен>.workers.dev` —
   должно быть `{"ok": true, "message": "MineForFriends: загрузка скинов работает"}`.

Этот адрес (он не секретный) вписывается в `SKINS_UPLOAD` в `launcher.py` с `/upload` на конце.

### 4. Отчёты об ошибках

Тот же Worker принимает отчёты о сбоях лаунчера (`POST /report`) и складывает их задачами (Issues)
в **закрытый** репозиторий — там пути, ники и логи игроков, поэтому он не должен быть публичным.

1. На GitHub: **New repository** → имя `MineForFriends-reports`, **Private**, галочка **Add a README file** → **Create repository**.
2. Токен из шага 2: GitHub → **Settings** → **Developer settings** → **Fine-grained tokens** → токен `MineForFriends skins` → **Edit**:
   - Repository access: добавить `MineForFriends-reports` (к `MineForFriends-skins`)
   - Permissions → Repository permissions → **Issues: Read and write**
   
   → **Update**. Сам токен не меняется, в Worker ничего заменять не нужно.
3. В Worker: **Edit code** → вставить свежий `worker.js` → **Deploy**. Переменная `REPORTS_REPO`
   не обязательна: по умолчанию `TejkaFan/MineForFriends-reports`.

Отчёты видны во вкладке **Issues** репозитория. Одна и та же ошибка не плодит задачи: повторы
приходят комментариями к открытой задаче. Закрыл задачу — следующий такой же сбой откроет новую.

### 5. Защита от спама (KV)

Worker сам ограничивает частоту запросов: с одного IP — не больше 6 загрузок скинов/плащей за
10 минут и 30 в сутки, 10 отчётов в час и 40 в сутки, 5 сборок в сутки; со всех вместе — 300 загрузок, 200 отчётов
и 40 сборок в сутки. Одна и та же ошибка дописывается в задачу не больше 50 раз. Сверх лимита — ответ 429,
до GitHub запрос не доходит.

Без KV счётчики живут только в памяти Worker и сбрасываются при его перезапуске. Чтобы они
были надёжными:

1. Cloudflare → **Storage & Databases** → **KV** → **Create** → имя `mineforfriends-limits` → **Create**.
2. Worker `mineforfriends-skins` → **Settings** → **Bindings** → **Add** → **KV namespace** →
   Variable name `LIMITS`, namespace `mineforfriends-limits` → **Deploy**.

Бесплатно KV даёт 1000 записей в сутки — для своих с запасом. Если записи кончатся, Worker
будет отвечать «слишком много запросов» до следующих суток, а не пускать спам.

### 6. Сборки друзей

Игрок жмёт «Поделиться» у своей сборки — лаунчер находит её моды на Modrinth и упаковывает конфиги
(до 4 МБ). Worker создаёт ветку `recipe/…` и открывает **Pull Request** в репозитории скинов:
в описании автор, версия, список модов и размер конфигов. В `main` ничего не попадает, пока ты не решишь:

- **Merge pull request** — сборка появляется у всех в «Сборках друзей»;
- **Close pull request** — отклонить (ветку потом можно удалить кнопкой **Delete branch**).

Файлы модов в репозиторий не кладутся — друзья скачивают их с `cdn.modrinth.com` и сверяют хэш.
Обновить сборку может только её автор (с того же ника и компьютера) — тоже через новую заявку.

## Обслуживание

- **Сбросить ник** (игрок потерял токен — переустановил Windows, сменил ПК): удалить в репозитории файл `owners/<ник в нижнем регистре>.json`.
- **Удалить скин**: удалить `skins/<Ник>.json` (вместе с плащом).
- **Убрать только плащ**: в `skins/<Ник>.json` удалить поле `"cape"`.
- Новый скин виден в игре через несколько минут: GitHub кэширует файлы примерно на 5 минут.
- **Удалить сборку друзей**: удалить папку `recipes/<id>/`.
- Бесплатного тарифа Cloudflare (100 000 запросов в день) хватает с огромным запасом.
