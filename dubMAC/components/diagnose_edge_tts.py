"""独立诊断 Edge TTS：网络层 + edge_tts 库 + 本仓库 probe。"""
import asyncio
import socket
import ssl
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

HOST = 'speech.platform.bing.com'
VOICE = 'zh-CN-XiaoxiaoNeural'


def test_dns():
    print('=== 1. DNS ===')
    try:
        infos = socket.getaddrinfo(HOST, 443, type=socket.SOCK_STREAM)
        addrs = sorted({i[4][0] for i in infos})
        print(f'  OK  {HOST} -> {", ".join(addrs[:6])}')
        return True
    except Exception as e:
        print(f'  FAIL  {e}')
        return False


def test_tcp():
    print('=== 2. TCP 443 ===')
    try:
        infos = socket.getaddrinfo(HOST, 443, type=socket.SOCK_STREAM)
        for fam, _, _, _, addr in infos[:4]:
            ip = addr[0]
            s = socket.socket(fam, socket.SOCK_STREAM)
            s.settimeout(12)
            t0 = time.time()
            try:
                s.connect((ip, 443))
                print(f'  OK  connect {ip}:443 ({time.time()-t0:.2f}s)')
                s.close()
                return True
            except OSError as e:
                print(f'  FAIL  {ip}:443  {e}')
            finally:
                try:
                    s.close()
                except OSError:
                    pass
        return False
    except Exception as e:
        print(f'  FAIL  {e}')
        return False


def test_tls_v4() -> bool:
    print('=== 3b. IPv4 TLS ===')
    ctx = ssl.create_default_context()
    try:
        infos = socket.getaddrinfo(HOST, 443, family=socket.AF_INET, type=socket.SOCK_STREAM)
        ip = infos[0][4][0]
        with socket.create_connection((ip, 443), timeout=12) as raw:
            with ctx.wrap_socket(raw, server_hostname=HOST) as ss:
                print(f'  OK  {ip} TLS {ss.version()}')
                return True
    except Exception as e:
        print(f'  FAIL  {e}')
        return False


def test_tls_v6() -> bool:
    print('=== 3a. IPv6 TLS ===')
    ctx = ssl.create_default_context()
    try:
        infos = socket.getaddrinfo(HOST, 443, family=socket.AF_INET6, type=socket.SOCK_STREAM)
        ip = infos[0][4][0]
        with socket.create_connection((ip, 443), timeout=12) as raw:
            with ctx.wrap_socket(raw, server_hostname=HOST) as ss:
                print(f'  OK  {ip} TLS {ss.version()}')
                return True
    except Exception as e:
        print(f'  FAIL  {e}')
        return False


def test_tls() -> tuple[bool, bool]:
    v6 = test_tls_v6()
    v4 = test_tls_v4()
    return v6, v4


async def test_edge_tts_direct():
    print('=== 4. edge_tts.Communicate().save()（默认 aiohttp，可能走 IPv6）===')
    import edge_tts
    print(f'  edge_tts 版本: {getattr(edge_tts, "__version__", "?")}')
    tmp = tempfile.mktemp(suffix='.mp3')
    try:
        comm = edge_tts.Communicate('连通性测试', VOICE, rate='+0%')
        t0 = time.time()
        await asyncio.wait_for(comm.save(tmp), timeout=30)
        size = Path(tmp).stat().st_size if Path(tmp).exists() else 0
        print(f'  OK  合成成功  {size} bytes  ({time.time()-t0:.2f}s)')
        return True
    except Exception as e:
        print(f'  FAIL  {type(e).__name__}: {e}')
    finally:
        Path(tmp).unlink(missing_ok=True)

    print('=== 4b. edge_communicate()（项目默认，Windows 强制 IPv4）===')
    from edge_tts_scheduler import edge_communicate, edge_ipv4_only_enabled
    print(f'  edge_ipv4_only={edge_ipv4_only_enabled()}')
    tmp = tempfile.mktemp(suffix='.mp3')
    try:
        comm = edge_communicate('连通性测试', VOICE, rate='+0%')
        t0 = time.time()
        await asyncio.wait_for(comm.save(tmp), timeout=30)
        size = Path(tmp).stat().st_size if Path(tmp).exists() else 0
        print(f'  OK  合成成功  {size} bytes  ({time.time()-t0:.2f}s)')
        return True
    except Exception as e:
        print(f'  FAIL  {type(e).__name__}: {e}')
        return False
    finally:
        Path(tmp).unlink(missing_ok=True)


async def test_repo_probe():
    print('=== 5. 项目 probe_edge_tts() ===')
    from edge_tts_scheduler import probe_edge_tts
    ok, err = await probe_edge_tts(VOICE, '+0%', timeout_sec=20)
    if ok:
        print('  OK  probe 通过')
    else:
        print(f'  FAIL  {err}')
    return ok


async def test_edge_cli():
    print('=== 6. edge-tts 命令行 ===')
    import subprocess
    tmp = Path(tempfile.gettempdir()) / '_edge_cli_test.mp3'
    cmd = [
        sys.executable, '-m', 'edge_tts',
        '--voice', VOICE,
        '--text', '命令行测试',
        '--write-media', str(tmp),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
        if r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 500:
            print(f'  OK  {tmp.stat().st_size} bytes')
            tmp.unlink(missing_ok=True)
            return True
        print(f'  FAIL  code={r.returncode}')
        if r.stderr:
            print(f'        stderr: {r.stderr.strip()[:200]}')
        if r.stdout:
            print(f'        stdout: {r.stdout.strip()[:200]}')
        return False
    except subprocess.TimeoutExpired:
        print('  FAIL  超时 45s')
        return False


async def main():
    print(f'目标: https://{HOST}/  语音: {VOICE}\n')
    r1 = test_dns()
    r2 = test_tcp()
    r3v6, r3v4 = test_tls()
    r4 = await test_edge_tts_direct()
    r5 = await test_repo_probe()
    r6 = await test_edge_cli()
    print('\n=== 汇总 ===')
    labels = [
        ('DNS', r1), ('TCP 443', r2), ('IPv4 TLS', r3v4),
        ('edge_tts IPv4(项目)', r4 or r5), ('项目 probe', r5), ('edge-tts CLI', r6),
    ]
    for name, ok in labels:
        print(f'  {"OK" if ok else "FAIL":4}  {name}')
    if not r3v6 and r3v4:
        print('\n结论: IPv6 到 Bing 语音 TLS 失败、IPv4 正常 → 不是「网站宕机」，'
              '是 aiohttp 默认走 IPv6 导致假「网络不可达」。'
              '请保持 tubeget.config.json 中 edge_ipv4_only=true 后重跑配音。')
    elif not r2:
        print('\n结论: TCP 连不上 → 本机网络/防火墙/DNS/代理问题，不是 Edge 全球宕机。')
    elif r5 or r4:
        print('\n结论: Edge TTS 当前可用（IPv4）；请重跑 测试本地配音.bat。')


if __name__ == '__main__':
    asyncio.run(main())
