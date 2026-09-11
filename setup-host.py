"""Configure a portable Linux NAS install; personal settings stay outside Git."""
import argparse
import ipaddress
import json
import os
import secrets
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / 'install.local.json'
OVERRIDE = ROOT / 'compose.override.yaml'


def defaults():
    return {
        'uid': int(os.getenv('SUDO_UID', os.getuid() or 65532)),
        'gid': int(os.getenv('SUDO_GID', os.getgid() or 65532)),
        'bind_address': '0.0.0.0', 'port': 8787, 'timezone': 'UTC',
        'download_dirs': [str(ROOT / 'downloads')],
    }


def validate(cfg):
    for field in ('uid', 'gid', 'port'):
        if type(cfg.get(field)) is not int:
            raise ValueError(f'{field} 必须是整数')
    if cfg['uid'] <= 0 or cfg['gid'] <= 0:
        raise ValueError('请配置非 root 的 UID 和 GID')
    if not 1 <= cfg['port'] <= 65535:
        raise ValueError('端口必须在 1–65535 之间')
    ipaddress.IPv4Address(cfg['bind_address'])
    try:
        ZoneInfo(cfg['timezone'])
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError('时区无效，请使用 UTC 或有效的 IANA 时区')
    dirs = cfg.get('download_dirs')
    if not isinstance(dirs, list) or not dirs:
        raise ValueError('至少配置一个下载目录')
    normalized = []
    for raw in dirs:
        if not isinstance(raw, str) or not raw or any(c in raw for c in ':$\n\r'):
            raise ValueError('路径不能为空，且不能包含冒号、美元符号或换行')
        p = Path(raw).expanduser()
        if not p.is_absolute():
            raise ValueError('下载目录必须使用绝对路径')
        p = p.resolve()
        # Keep the same container path without masking its runtime or secrets.
        forbidden = [Path('/app'), Path('/data'), Path('/run'), Path('/proc'),
                     Path('/sys'), Path('/dev'), Path('/etc'), Path('/usr'),
                     Path('/bin'), Path('/sbin'), Path('/lib'), Path('/lib64'),
                     ROOT / 'data', ROOT / 'secrets']
        if any(p == base or p in base.parents or base in p.parents for base in forbidden):
            raise ValueError('下载目录不能覆盖系统目录或包含应用配置、密钥')
        if p == Path('/tmp') or p == ROOT or p in ROOT.parents:
            raise ValueError('下载目录不能包含整个项目目录或覆盖临时目录')
        if str(p) not in normalized:
            normalized.append(str(p))
    return dict(cfg, download_dirs=normalized)


def write_private(path, data):
    if path.is_symlink():
        raise ValueError(f'配置文件不能是符号链接：{path.name}')
    tmp = path.with_name(path.name + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w') as output:
            json.dump(data, output, ensure_ascii=False, indent=2)
            output.write('\n')
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def render_override(cfg):
    return {'services': {'web': {
        'user': f'{cfg["uid"]}:{cfg["gid"]}',
        'ports': [{'target': 8080, 'published': str(cfg['port']),
                   'host_ip': cfg['bind_address'], 'protocol': 'tcp'}],
        'environment': {'DOWNLOAD_ROOTS': ':'.join(cfg['download_dirs']), 'TZ': cfg['timezone']},
        'volumes': [{'type': 'bind', 'source': path, 'target': path,
                     'bind': {'create_host_path': False}} for path in cfg['download_dirs']],
    }}}


def configure(force=False):
    cfg = json.loads(CONFIG.read_text()) if CONFIG.exists() else defaults()
    if force or not CONFIG.exists():
        print('安装设置仅保存在本机，不会提交到 Git。')
        print('现有下载目录必须已挂载。默认在项目内新建 downloads 文件夹。')
        raw = input('下载目录（多个用 | 分隔） [' + ' | '.join(cfg['download_dirs']) + ']: ').strip()
        if raw:
            cfg['download_dirs'] = [p.strip() for p in raw.split('|')]
        for key, label in [('bind_address', '监听 IPv4（0.0.0.0 表示全部网卡）'),
                           ('port', '网页端口'), ('uid', '容器运行 UID'),
                           ('gid', '容器运行 GID'), ('timezone', '时区')]:
            value = input(f'{label} [{cfg[key]}]: ').strip()
            if value:
                cfg[key] = int(value) if key in ('port', 'uid', 'gid') else value
    cfg = validate(cfg)
    write_private(CONFIG, cfg)
    # JSON is valid YAML and handles spaces in paths without manual quoting.
    write_private(OVERRIDE, render_override(cfg))
    print('已生成 install.local.json 和 compose.override.yaml。')


def ensure_owned(path, uid, gid, mode):
    if path.is_symlink():
        raise ValueError(f'应用数据目录不能是符号链接：{path}')
    path.mkdir(parents=True, exist_ok=True, mode=mode)
    if os.geteuid() == 0:
        os.chown(path, uid, gid)
    elif path.stat().st_uid != uid or path.stat().st_gid != gid:
        raise PermissionError('目录运行身份不匹配，请以 sudo 运行准备步骤')
    os.chmod(path, mode)


def prepare():
    cfg = validate(json.loads(CONFIG.read_text()))
    for path in (ROOT / 'data', ROOT / 'secrets'):
        ensure_owned(path, cfg['uid'], cfg['gid'], 0o700)
    for raw in cfg['download_dirs']:
        path = Path(raw)
        if not path.exists():
            if path != ROOT / 'downloads':
                raise ValueError(f'下载目录不存在，请先挂载存储并创建目录：{path}')
            ensure_owned(path, cfg['uid'], cfg['gid'], 0o750)
        if not path.is_dir():
            raise ValueError(f'下载路径不是目录：{path}')
        # Existing media folders keep their ownership and permission bits.
    token = ROOT / 'secrets/access_token'
    if token.is_symlink():
        raise ValueError('访问密钥文件不能是符号链接')
    if not token.exists():
        fd = os.open(token, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as output:
            output.write(secrets.token_urlsafe(32) + '\n')
    if os.geteuid() == 0:
        os.chown(token, cfg['uid'], cfg['gid'])
    os.chmod(token, 0o600)


def show_url():
    cfg = json.loads(CONFIG.read_text())
    host = cfg['bind_address'] if cfg['bind_address'] != '0.0.0.0' else '<NAS-IP>'
    print(f'服务地址：http://{host}:{cfg["port"]}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['configure', 'prepare', 'url'])
    parser.add_argument('--reconfigure', action='store_true')
    args = parser.parse_args()
    try:
        if args.action == 'configure':
            configure(args.reconfigure)
        elif args.action == 'prepare':
            prepare()
        else:
            show_url()
    except (ValueError, OSError, EOFError) as exc:
        raise SystemExit(str(exc))
