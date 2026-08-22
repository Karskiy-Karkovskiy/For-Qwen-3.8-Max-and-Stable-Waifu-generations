import asyncio
import base64
import json
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from telethon import TelegramClient

# ================= НАСТРОЙКИ =================
CHANNEL = 'StableWaifuArt'   # лента артов; если нужен другой чат — замените username
OWNER   = 'Karskiy-Karkovskiy'
REPO    = 'For-Qwen-3.8-Max-and-Stable-Waifu-generations'
BRANCH  = 'main'
DAYS    = 3    # сколько ПОЛНЫХ дней (не считая сегодня) брать
CHUNK   = 30   # постов в одном пакете = один коммит на GitHub
# =============================================

HOME = os.path.expanduser('~')
TRANSIT = os.path.join(HOME, 'art_transit')
STATE_FILE = os.path.join(HOME, 'artchat_state.json')
os.makedirs(TRANSIT, exist_ok=True)

GH  = f'https://api.github.com/repos/{OWNER}/{REPO}'
RAW = f'https://raw.githubusercontent.com/{OWNER}/{REPO}/{BRANCH}'

SEED_RE = re.compile(r'(?:seed|сид)\s*[:=\-]?\s*(\d{1,15})', re.IGNORECASE)
MIME_EXT = {'image/jpeg': '.jpg', 'image/png': '.png',
            'image/webp': '.webp', 'image/gif': '.gif'}

with open(os.path.join(HOME, 'tg_api.json'), encoding='utf-8') as f:
    tg = json.load(f)
API_ID, API_HASH = tg['api_id'], tg['api_hash']

def get_token():
    with open(os.path.join(HOME, '.git-credentials')) as f:
        for line in f:
            if 'github.com' in line:
                return urlparse(line.strip()).password
    raise SystemExit('Не найден токен в ~/.git-credentials')

TOKEN = get_token()

def gh(method, path, data=None):
    body = json.dumps(data).encode() if data is not None else None
    req = Request(GH + path, data=body, method=method)
    req.add_header('Authorization', 'Bearer ' + TOKEN)
    req.add_header('Accept', 'application/vnd.github+json')
    req.add_header('Content-Type', 'application/json')
    with urlopen(req) as r:
        return json.loads(r.read().decode())

def get_ext(msg):
    if msg.photo:
        return '.jpg'
    if msg.document:
        return MIME_EXT.get(getattr(msg.document, 'mime_type', '') or '')
    return None

client = TelegramClient('artchat_session', API_ID, API_HASH)

async def main():
    print('📱 Подключение к Telegram...')
    await client.start()
    entity = await client.get_entity(CHANNEL)
    print(f'✅ Канал: {entity.title}')

    last_id = 0
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding='utf-8') as f:
            last_id = json.load(f).get('last_id', 0)

    # окно: DAYS полных календарных дней ДО сегодня (по местному времени)
    today_start = datetime.now().astimezone().replace(
        hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    cutoff = today_start - timedelta(days=DAYS)

    # ---- читаем только новые посты, сегодняшние пропускаем, альбомы склеиваем ----
    print('⏳ Чтение новых постов...')
    groups, cur = [], []
    async for msg in client.iter_messages(entity):
        if msg.date < cutoff or msg.id <= last_id:
            break
        if msg.date >= today_start:
            continue
        if cur and cur[0].grouped_id and msg.grouped_id == cur[0].grouped_id:
            cur.append(msg)
        else:
            if cur:
                groups.append(cur)
            cur = [msg]
    if cur:
        groups.append(cur)
    groups.reverse()  # старые -> новые

    if not groups:
        print('Новых постов нет. Выход.')
        return
    print(f'📦 Новых постов: {len(groups)}. Пакеты по {CHUNK}.')

    for start in range(0, len(groups), CHUNK):
        chunk = groups[start:start + CHUNK]
        print(f'--- Пакет {start // CHUNK + 1}: посты {start + 1}..{start + len(chunk)} ---')

        entries, files, chunk_max = [], [], last_id
        for grp in chunk:
            chunk_max = max(chunk_max, max(m.id for m in grp))
            text = next((m.message for m in grp if m.message and m.message.strip()), '')
            m_seed = SEED_RE.search(text) if text else None
            seed = int(m_seed.group(1)) if m_seed else None
            images, media = [], [m for m in grp if get_ext(m)]
            for k, m in enumerate(media, 1):
                base = f'seed{seed}_post{grp[0].id}' if seed is not None else f'post{grp[0].id}'
                if len(media) > 1:
                    base += f'_{k}'
                fname = base + get_ext(m)
                path = os.path.join(TRANSIT, fname)
                if os.path.exists(path):
                    print(f'  ⏭ уже скачано: {fname}')
                else:
                    await client.download_media(m, file=path)
                    print(f'  📷 {fname}')
                images.append(fname)
                files.append(fname)
            if text or images:
                entries.append({
                    'id': grp[0].id,
                    'date': grp[0].date.isoformat(),
                    'views': next((m.views for m in grp if m.views), None),
                    'seed': seed,
                    'text': text,
                    'images': images,
                })

        # ---- отправляем пакет на GitHub одним коммитом ----
        base_sha  = gh('GET', f'/git/ref/heads/{BRANCH}')['object']['sha']
        base_tree = gh('GET', f'/git/commits/{base_sha}')['tree']['sha']
        tree = []
        for fname in files:
            with open(os.path.join(TRANSIT, fname), 'rb') as f:
                b64 = base64.b64encode(f.read()).decode()
            blob = gh('POST', '/git/blobs', {'content': b64, 'encoding': 'base64'})
            tree.append({'path': f'ArtChat/Arts/{fname}', 'mode': '100644',
                         'type': 'blob', 'sha': blob['sha']})

        old = []
        try:
            with urlopen(f'{RAW}/ArtChat/ArtPromts.json') as r:
                old = json.loads(r.read().decode())
        except Exception:
            old = []
        blob = gh('POST', '/git/blobs', {
            'content': base64.b64encode(
                json.dumps(old + entries, ensure_ascii=False, indent=2).encode()
            ).decode(),
            'encoding': 'base64'})
        tree.append({'path': 'ArtChat/ArtPromts.json', 'mode': '100644',
                     'type': 'blob', 'sha': blob['sha']})

        new_tree = gh('POST', '/git/trees', {'base_tree': base_tree, 'tree': tree})
        commit = gh('POST', '/git/commits', {
            'message': f'ArtChat sync: +{len(files)} arts',
            'tree': new_tree['sha'], 'parents': [base_sha]})
        gh('PATCH', f'/git/refs/heads/{BRANCH}', {'sha': commit['sha']})
        print(f'✅ Пакет на GitHub: коммит {commit["sha"][:7]}')

        # ---- очищаем телефон и запоминаем прогресс ----
        for fname in files:
            os.remove(os.path.join(TRANSIT, fname))
        last_id = chunk_max
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump({'last_id': last_id}, f)
        print('🧽 Телефон очищен. Можно прерваться (Ctrl+C) без потерь.')

    print('🎉 Всё готово!')

if __name__ == '__main__':
    with client:
        client.loop.run_until_complete(main())
