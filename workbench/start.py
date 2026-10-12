"""Start/reuse this project's loopback server and open its canonical URL."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
import webbrowser
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
SERVICE = "sheet-to-audio-workbench"
DEFAULT_PORT = 8765


def server_identity(port: int) -> dict | None:
    try:
        with urlopen(f"http://127.0.0.1:{port}/api/health", timeout=0.8) as response:
            data = json.load(response)
        return data if isinstance(data, dict) else None
    except (URLError, TimeoutError, OSError, ValueError):
        return None


def matching_server(data: dict | None, root: Path = ROOT, *, bundle=None, config=None) -> bool:
    return bool(data and data.get("service") == SERVICE
                and isinstance(data.get("workspace"), str)
                and Path(data["workspace"]).resolve() == root.resolve()
                and (data.get("baseBundle", data.get("bundle")) == str(Path(bundle).resolve()) if bundle else data.get("bundle") is None)
                and (data.get("config") == str(Path(config).resolve()) if config else True))


def port_occupied(port: int) -> bool:
    with socket.socket() as connection:
        connection.settimeout(0.5)
        return connection.connect_ex(("127.0.0.1", port)) == 0


def ensure_server(port: int, root: Path = ROOT, timeout: float = 15, *, bundle=None, config=None, generated=None) -> dict:
    if not bundle or not config:
        raise ValueError("bundle 与 config 必须显式提供")
    identity = server_identity(port)
    if matching_server(identity, root, bundle=bundle, config=config):
        return identity
    if port_occupied(port):
        raise RuntimeError(f"端口 {port} 已被其他服务或旧版工作台占用；未停止任何进程。"
                           f"请关闭旧服务后重试，或用 choir-assistant serve --project PROJECT --port {port + 1}。")
    python = Path(sys.executable).resolve()
    output = Path(generated).resolve() if generated else Path(config).resolve().parent / "preview"
    if output.is_relative_to(ROOT):
        raise ValueError("Generated files must stay outside the installed plugin")
    output.mkdir(parents=True, exist_ok=True)
    log_path = output / "server-launch.log"
    command = [str(python), str(root / "workbench/server.py"), "--port", str(port)]
    if bundle:
        command.extend(["--bundle", str(Path(bundle).resolve()), "--config", str(Path(config).resolve())])
    if generated:
        command.extend(["--generated", str(Path(generated).resolve())])
    with log_path.open("ab") as log:
        process = subprocess.Popen(command,
                                   cwd=root, stdin=subprocess.DEVNULL, stdout=log,
                                   stderr=subprocess.STDOUT, start_new_session=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        identity = server_identity(port)
        if matching_server(identity, root, bundle=bundle, config=config):
            return identity
        if identity or (process.poll() is not None):
            raise RuntimeError(f"工作台未启动成功，请查看 {log_path}")
        time.sleep(0.2)
    raise RuntimeError(f"工作台未在 {timeout:g} 秒内就绪，请查看 {log_path}。"
                       "服务可能仍在启动，重试会先检查已有服务。")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-open", action="store_true", help="只启动/检查服务，不打开浏览器")
    parser.add_argument("--bundle", required=True, help="打开已验证的独立 workflow 资产包")
    parser.add_argument("--config", required=True, help="独立曲谱项目配置，与 --bundle 同时使用")
    parser.add_argument("--generated", help="独立预览的音频和校准输出目录")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port 应在 1–65535")
    if bool(args.bundle) != bool(args.config):
        parser.error("--bundle 与 --config 需要同时提供")
    if args.generated and not args.bundle:
        parser.error("--generated 仅用于独立预览")
    try:
        identity = ensure_server(args.port, timeout=45 if args.bundle else 15,
                                 bundle=args.bundle, config=args.config, generated=args.generated)
        url = f"http://127.0.0.1:{args.port}/"
        print(f"工作台已就绪：{url}（revision {identity.get('revision', 'unknown')}）")
        if not args.no_open:
            webbrowser.open(url)
        return 0
    except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
