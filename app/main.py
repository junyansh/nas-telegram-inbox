import asyncio
import contextlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse, unquote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from telethon import TelegramClient, errors
from python_socks import ProxyType

DATA = Path(os.getenv('DATA_DIR', '/data'))
ROOTS = [Path(p).resolve() for p in os.getenv('DOWNLOAD_ROOTS', '/downloads').split(':')]
TOKEN_FILE = Path(os.getenv('ACCESS_TOKEN_FILE', '/run/secrets/access_token'))
db = None
client = None
client_lock = asyncio.Lock()
phone_state = None
last_error = ''


def setting():
    row = db.execute('SELECT value FROM settings WHERE id=1').fetchone()
    return json.loads(row[0]) if row else {'directory': str(ROOTS[0]), 'api_id': 0, 'api_hash': '', 'proxy': ''}


def parse_link(link):
    u = urlparse(link.strip())
    if u.scheme not in ('https', 'http') or u.hostname not in ('t.me', 'telegram.me') or u.username or u.password or u.port:
        raise ValueError('请使用 https://t.me/频道/消息编号 或 https://t.me/c/频道编号/消息编号')
    parts = u.path.strip('/').split('/')
    if parts[0] == 's':
        parts = parts[1:]
    if parts and parts[0] == 'c':
        if len(parts) not in (3, 4) or not all(p.isdigit() and int(p) > 0 for p in parts[1:]):
            raise ValueError('私有消息链接格式不正确')
        entity = int('-100' + parts[1])
    else:
        if len(parts) not in (2, 3) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{3,}', parts[0]) or not all(p.isdigit() and int(p) > 0 for p in parts[1:]):
            raise ValueError('请复制具体消息链接；不支持邀请链接或频道首页')
        entity = parts[0].lower()
    return entity, int(parts[-1])


def safe_dir(raw, create=False):
    p = Path(raw).resolve()
    if not any(p == root or root in p.parents for root in ROOTS):
        raise ValueError('目录必须位于已挂载的 NAS 下载目录内')
    if create:
        p.mkdir(parents=True, exist_ok=True)
    if not p.is_dir():
        raise ValueError('目录不存在，请先创建目录')
    return p


def update_job(job_id, **values):
    db.execute('UPDATE jobs SET ' + ','.join(f'{k}=?' for k in values) + ' WHERE id=?', [*values.values(), job_id])
    db.commit()


def error_text(exc):
    if isinstance(exc, errors.FloodWaitError):
        return f'Telegram 请求限流，需要等待 {exc.seconds} 秒'
    if isinstance(exc, (asyncio.TimeoutError, ConnectionError, OSError)):
        return f'连接或存储操作失败：{type(exc).__name__}；请检查网络、代理、空间和目录权限'
    if isinstance(exc, errors.RPCError):
        return f'Telegram 返回 {type(exc).__name__}；请检查验证码、账号登录状态及频道访问权限'
    if isinstance(exc, ValueError):
        return str(exc)
    return f'操作失败：{type(exc).__name__}'


def proxy_value(raw):
    if not raw:
        return None
    u = urlparse(raw)
    kinds = {'socks5': ProxyType.SOCKS5, 'http': ProxyType.HTTP}
    if u.scheme not in kinds or not u.hostname or not u.port:
        raise ValueError('代理格式为 socks5://主机:端口 或 http://主机:端口')
    return (kinds[u.scheme], u.hostname, u.port, True,
            unquote(u.username) if u.username else None, unquote(u.password) if u.password else None)


async def get_client():
    global client
    async with client_lock:
        cfg = setting()
        if not cfg.get('api_id') or not cfg.get('api_hash'):
            raise ValueError('请先在设置中保存 Telegram API ID 和 API Hash')
        if client is None:
            client = TelegramClient(str(DATA / 'telegram'), cfg['api_id'], cfg['api_hash'],
                                    proxy=proxy_value(cfg.get('proxy', '')), device_model='fnOS Video Inbox',
                                    connection_retries=2, request_retries=2, flood_sleep_threshold=0)
        if not client.is_connected():
            await asyncio.wait_for(client.connect(), 25)
        return client


async def download_job(job, tg):
    entity, mid = parse_link(job['link'])
    directory = safe_dir(job['directory'])
    if isinstance(entity, int):
        try:
            peer = await tg.get_input_entity(entity)
        except ValueError:
            # Populate channel access hashes for private channels the account has joined.
            async for dialog in tg.iter_dialogs():
                if dialog.id == entity:
                    break
            peer = await tg.get_input_entity(entity)
    else:
        peer = await tg.get_input_entity(entity)
    msg = await tg.get_messages(peer, ids=mid)
    if not msg or not msg.document or not (msg.video or (msg.document.mime_type or '').startswith('video/')):
        raise ValueError('这条消息没有可下载的视频；请复制视频所在的那条消息链接')
    size = msg.file.size or 0
    if shutil.disk_usage(directory).free < size + 128 * 1024 * 1024:
        raise ValueError('目标磁盘剩余空间不足（预留 128 MB）')
    name = msg.file.name or ('video' + (msg.file.ext or '.mp4'))
    name = re.sub(r'[^\w.\-\u4e00-\u9fff]', '_', name)[:100]
    final = directory / f'{entity}_{mid}_{job["id"][:8]}_{name}'
    partial = directory / f'.{job["id"]}.part'
    if final.exists():
        if final.is_file() and not final.is_symlink() and final.stat().st_size == size:
            update_job(job['id'], state='done', progress=100, filename=str(final), error='')
            return
        raise ValueError('目标文件已存在但大小不匹配，请检查后重试')
    last_tick = 0

    async def progress(current, total):
        nonlocal last_tick
        if time.monotonic() - last_tick > 1 or current == total:
            update_job(job['id'], progress=round(current * 100 / max(total, 1), 1))
            last_tick = time.monotonic()

    # O_NOFOLLOW refuses a pre-existing symlink, O_EXCL protects existing files.
    if partial.exists() and not partial.is_symlink():
        partial.unlink()
    try:
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
        with os.fdopen(fd, 'wb') as output:
            await tg.download_media(msg, file=output, progress_callback=progress)
            output.flush()
            os.fsync(output.fileno())
        if partial.stat().st_size != size:
            raise ValueError('下载文件大小校验失败，请重试')
        os.link(partial, final)  # atomic publication without overwriting another file
        partial.unlink()
        update_job(job['id'], state='done', progress=100, filename=str(final), error='')
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


async def worker():
    global last_error
    while True:
        try:
            cooldown = db.execute("SELECT COALESCE(MAX(retry_at),0) FROM jobs WHERE state='queued'").fetchone()[0]
            if cooldown > time.time():
                await asyncio.sleep(min(5, cooldown - time.time()))
                continue
            job = db.execute("SELECT * FROM jobs WHERE state='queued' AND retry_at<=? ORDER BY created LIMIT 1", (time.time(),)).fetchone()
            if job:
                tg = await get_client()
                if await asyncio.wait_for(tg.is_user_authorized(), 20):
                    claimed = db.execute("UPDATE jobs SET state='downloading', error='' WHERE id=? AND state='queued'", (job['id'],)).rowcount
                    db.commit()
                    if not claimed:  # The user may cancel while authorization is in flight.
                        continue
                    try:
                        await download_job(job, tg)
                    except errors.FloodWaitError as e:
                        update_job(job['id'], state='queued', retry_at=time.time() + e.seconds + 2, error=error_text(e))
                    except Exception as e:
                        update_job(job['id'], state='failed', error=error_text(e))
                    last_error = ''
        except Exception as e:
            last_error = error_text(e)
        await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(app):
    global db, client
    os.umask(0o077)
    DATA.mkdir(parents=True, exist_ok=True)
    if len(TOKEN_FILE.read_text().strip()) < 32:
        raise RuntimeError('Access token must contain at least 32 characters')
    app.state.token = TOKEN_FILE.read_text().strip()
    db = sqlite3.connect(DATA / 'inbox.db')
    db.row_factory = sqlite3.Row
    db.executescript('''
      PRAGMA journal_mode=WAL;
      CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, value TEXT);
      CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY, link TEXT NOT NULL, directory TEXT NOT NULL,
        state TEXT NOT NULL, progress REAL DEFAULT 0, error TEXT DEFAULT '',
        filename TEXT DEFAULT '', created REAL NOT NULL, retry_at REAL DEFAULT 0);
      UPDATE jobs SET state='queued', progress=0 WHERE state='downloading';
    ''')
    task = asyncio.create_task(worker())
    yield
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    if client:
        await client.disconnect()
        client = None
    db.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware('http')
async def auth(request: Request, call_next):
    # Route using the ASGI path, never a URL reconstructed from the Host header.
    # Only these read-only endpoints are public; all other paths fail closed.
    public = (request.scope.get('path') in ('/', '/healthz')
              and request.scope.get('method') in ('GET', 'HEAD'))
    if not public:
        authorization = request.headers.get('Authorization', '')
        token = authorization[7:] if authorization.startswith('Bearer ') else ''
        if not hmac.compare_digest(token.encode(), request.app.state.token.encode()):
            return JSONResponse({'detail': '请输入正确的服务访问密钥'}, status_code=401)
        try:
            length = int(request.headers.get('content-length', '0'))
        except ValueError:
            return JSONResponse({'detail': 'Content-Length 格式不正确'}, status_code=400)
        if length < 0:
            return JSONResponse({'detail': 'Content-Length 格式不正确'}, status_code=400)
        if length > 65536:
            return JSONResponse({'detail': '请求内容过长'}, status_code=413)
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'no-referrer'
    return response


@app.exception_handler(ValueError)
async def bad_value(request, exc):
    return JSONResponse({'detail': str(exc)}, status_code=400)


@app.exception_handler(errors.RPCError)
async def telegram_error(request, exc):
    return JSONResponse({'detail': error_text(exc)}, status_code=400)


@app.exception_handler(asyncio.TimeoutError)
async def timeout_error(request, exc):
    return JSONResponse({'detail': 'Telegram 连接超时，请检查网络或设置代理'}, status_code=504)


@app.exception_handler(OSError)
async def storage_error(request, exc):
    return JSONResponse({'detail': error_text(exc)}, status_code=400)


@app.get('/')
async def index():
    return FileResponse(Path(__file__).with_name('index.html'))


@app.get('/healthz')
async def health():
    db.execute('SELECT 1').fetchone()
    return {'ok': True}


@app.get('/api/status')
async def status():
    cfg = setting()
    authorized = False
    if client and client.is_connected():
        authorized = await asyncio.wait_for(client.is_user_authorized(), 10)
    return {'authorized': authorized, 'configured': bool(cfg.get('api_id')), 'error': last_error,
            'directory': cfg['directory'], 'roots': [str(p) for p in ROOTS],
            'api_id': cfg.get('api_id', 0), 'has_api_hash': bool(cfg.get('api_hash')), 'has_proxy': bool(cfg.get('proxy'))}


class Settings(BaseModel):
    directory: str = Field(max_length=1024)
    api_id: int = Field(default=0, ge=0)
    api_hash: str = Field(default='', max_length=64)
    proxy: str | None = Field(default=None, max_length=1024)


@app.post('/api/settings')
async def save_settings(body: Settings):
    global client, phone_state
    old = setting()
    folder = safe_dir(body.directory, create=True)
    probe = folder / f'.write-test-{secrets.token_hex(8)}'
    try:
        probe.write_text('')
    finally:
        probe.unlink(missing_ok=True)
    cfg = dict(old, directory=str(folder))
    if body.api_id:
        cfg['api_id'] = body.api_id
    if body.api_hash:
        if not re.fullmatch(r'[a-fA-F0-9]{32}', body.api_hash):
            raise ValueError('API Hash 应为 32 位十六进制字符')
        cfg['api_hash'] = body.api_hash
    if body.proxy is not None:
        proxy_value(body.proxy)
        cfg['proxy'] = body.proxy
    changed = any(old.get(k) != cfg.get(k) for k in ('api_id', 'api_hash', 'proxy'))
    if changed:
        if db.execute("SELECT 1 FROM jobs WHERE state IN ('queued','downloading')").fetchone():
            raise ValueError('队列中仍有任务；请完成任务后再修改账号或代理配置')
        async with client_lock:
            if client:
                await client.disconnect()
                client = None
            phone_state = None
    db.execute('INSERT OR REPLACE INTO settings VALUES(1,?)', (json.dumps(cfg),))
    db.commit()
    return {'ok': True}


@app.get('/api/folders')
async def folders(path: str = ''):
    folder = safe_dir(path or str(ROOTS[0]))
    children = []
    for p in sorted(folder.iterdir()):
        if not p.name.startswith('.') and p.is_dir() and not p.is_symlink():
            children.append(str(p))
        if len(children) >= 500:
            break
    return {'path': str(folder), 'children': children,
            'parent': str(folder.parent) if folder not in ROOTS else None,
            'free_gb': round(shutil.disk_usage(folder).free / 1024 ** 3, 1)}


class Phone(BaseModel):
    phone: str = Field(pattern=r'^\+[0-9]{7,15}$')


@app.post('/api/telegram/code')
async def send_code(body: Phone):
    global phone_state
    tg = await get_client()
    if await tg.is_user_authorized():
        return {'authorized': True}
    code = await asyncio.wait_for(tg.send_code_request(body.phone), 40)
    phone_state = (body.phone, code.phone_code_hash)
    return {'sent': True}


class Login(BaseModel):
    code: str = Field(default='', max_length=16)
    password: str = Field(default='', max_length=512)


@app.post('/api/telegram/login')
async def telegram_login(body: Login):
    tg = await get_client()
    try:
        if body.password:
            await asyncio.wait_for(tg.sign_in(password=body.password), 40)
        else:
            if not phone_state:
                raise ValueError('请先发送验证码')
            await asyncio.wait_for(tg.sign_in(phone_state[0], code=body.code, phone_code_hash=phone_state[1]), 40)
    except errors.SessionPasswordNeededError:
        return {'need_password': True}
    return {'authorized': True}


@app.post('/api/telegram/connect')
async def reconnect():
    tg = await get_client()
    return {'authorized': await asyncio.wait_for(tg.is_user_authorized(), 20)}


class Links(BaseModel):
    links: str = Field(min_length=1, max_length=20000)


@app.post('/api/jobs')
async def add_jobs(body: Links):
    tg = await get_client()
    if not await tg.is_user_authorized():
        raise ValueError('请先登录 Telegram')
    links = [s.strip() for s in body.links.splitlines() if s.strip()]
    if len(links) > 100:
        raise ValueError('一次最多提交 100 条消息链接')
    parsed = [(link, parse_link(link)) for link in links]  # validate whole batch before inserting
    if db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','downloading')").fetchone()[0] + len(parsed) > 500:
        raise ValueError('队列已满，请等待已有任务完成')
    folder = str(safe_dir(setting()['directory']))
    added = skipped = 0
    for link, (entity, mid) in parsed:
        canonical = f'https://t.me/c/{str(entity)[4:]}/{mid}' if isinstance(entity, int) else f'https://t.me/{entity}/{mid}'
        if db.execute("SELECT 1 FROM jobs WHERE link=? AND directory=? AND state IN ('queued','downloading','done')", (canonical, folder)).fetchone():
            skipped += 1
            continue
        db.execute('INSERT INTO jobs(id,link,directory,state,created) VALUES(?,?,?,?,?)',
                   (secrets.token_hex(16), canonical, folder, 'queued', time.time()))
        added += 1
    db.commit()
    return {'added': added, 'skipped': skipped}


@app.get('/api/jobs')
async def jobs():
    return [dict(r) for r in db.execute('SELECT * FROM jobs ORDER BY created DESC LIMIT 200')]


@app.post('/api/jobs/{job_id}/retry')
async def retry(job_id: str):
    row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, '任务不存在')
    if row['state'] != 'failed':
        raise ValueError('仅失败任务可以重试')
    safe_dir(row['directory'])
    update_job(job_id, state='queued', progress=0, error='', retry_at=0)
    return {'ok': True}


@app.post('/api/jobs/{job_id}/cancel')
async def cancel(job_id: str):
    row = db.execute('SELECT state FROM jobs WHERE id=?', (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, '任务不存在')
    if row['state'] != 'queued':
        raise ValueError('仅排队任务可以取消')
    update_job(job_id, state='cancelled', error='用户取消')
    return {'ok': True}
