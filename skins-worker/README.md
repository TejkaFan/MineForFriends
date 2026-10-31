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
- Permissions → Repository permissions → **Contents: Read and write**

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

## Обслуживание

- **Сбросить ник** (игрок потерял токен — переустановил Windows, сменил ПК): удалить в репозитории файл `owners/<ник в нижнем регистре>.json`.
- **Удалить скин**: удалить `skins/<Ник>.json`.
- Новый скин виден в игре через несколько минут: GitHub кэширует файлы примерно на 5 минут.
- Бесплатного тарифа Cloudflare (100 000 запросов в день) хватает с огромным запасом.
