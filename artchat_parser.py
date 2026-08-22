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
DAYS    = 3   # глубина первого запуска в днях
# =============================================

HOME = os.path.expanduser('~')
TRANSIT = os.path.join(HOME, 'art_transit')      # временная папка-перевалка
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
    await client.start()   # при первом запуске попросит телефон и код
    entity = await client.get_entity(CHANNEL)
    print(f'✅ Канал: {entity.title}')

    last_id = 0
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding='utf-8') as f:
            last_id = json.load(f).get('last_id', 0)
    cutoff = datetime.now(timezone.utc) - timedelta(days=DAYS)

    # ---- читаем только новые посты (свежие -> старые), альбомы склеиваем ----
    print('⏳ Чтение новых постов...')
    groups, cur, max_id = [], [], last_id
    async for msg in client.iter_messages(entity):
        if msg.date < cutoff or msg.id <= last_id:
            break
        max_id = max(max_id, msg.id)
        if cur and cur[0].grouped_id and msg.grouped_id == cur[0].grouped_id:
            cur.append(msg)
        else:
            if cur:
                groups.append(cur)
            cur = [msg]
    if cur:
        groups.append(cur)

    if not groups:
        print('Новых постов нет. Выход.')
        return

    # ---- скачиваем медиа во временную папку ----
    entries, files = [], []
    for grp in groups:
        text = next((m.message for m in grp if m.message and m.message.strip()), '')
        m_seed = SEED_RE.search(text) if text else None
        seed = int(m_seed.group(1)) if m_seed else None
        images, media = [], [m for m in grp if get_ext(m)]
        for k, m in enumerate(media, 1):
            base = f'seed{seed}_post{grp[0].id}' if seed is not None else f'post{grp[0].id}'
            if len(media) > 1:
                base += f'_{k}'
            fname = base + get_ext(m)
            await client.download_media(m, file=os.path.join(TRANSIT, fname))
            images.append(fname)
            files.append(fname)
            print(f'  📷 {fname}')
        if text or images:
            entries.append({
                'id': grp[0].id,
                'date': grp[0].date.isoformat(),
                'views': next((m.views for m in grp if m.views), None),
                'seed': seed,
                'text': text,
                'images': images,
            })

    # ---- загружаем на GitHub одним коммитом ----
    print(f'📤 Отправка {len(files)} файлов на GitHub...')
    base_sha  = gh('GET', f'/git/ref/heads/{BRANCH}')['object']['sha']
    base_tree = gh('GET', f'/git/commits/{base_sha}')['tree']['sha']

    tree = []
    for fname in files:
        with open(os.path.join(TRANSIT, fname), 'rb') as f:
            b64 = base64.b64encode(f.read()).decode()
        blob = gh('POST', '/git/blobs', {'content': b64, 'encoding': 'base64'})
        tree.append({'path': f'ArtChat/Arts/{fname}', 'mode': '100644',
                     'type': 'blob', 'sha': blob['sha']})

    # дописываем новые промты в существующий ArtPromts.json
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
    print(f'✅ Коммит на GitHub: {commit["sha"][:7]}')

    # ---- очищаем телефон ----
    for fname in files:
        os.remove(os.path.join(TRANSIT, fname))
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump({'last_id': max_id}, f)
    print('🧽 Временные файлы удалены с устройства. Готово!')

if __name__ == '__main__':
    with client:
        client.loop.run_until_complete(main())
