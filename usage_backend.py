#!/usr/bin/env python3
import json
import os
import sys
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

STORE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "usage_backend.json")
# store: { group: { device: { day: { type: count } } } }
store = {}
_lock = threading.Lock()

TOKEN = os.environ.get("USAGE_TOKEN") or ""   # 空=不鉴权


def load():
    global store
    if os.path.exists(STORE_FILE):
        try:
            with open(STORE_FILE, "r", encoding="utf-8") as f:
                store = json.load(f)
        except Exception:
            store = {}


def save():
    try:
        tmp = STORE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False)
        os.replace(tmp, STORE_FILE)
    except Exception as e:
        sys.stderr.write(f"save failed: {e}\n")


def add_report(body):
    group = str(body.get("group", "")).strip()
    device = str(body.get("deviceId", "")).strip()
    inc = body.get("increments") or {}
    if not group or not device:
        return False
    with _lock:
        g = store.setdefault(group, {})
        d = g.setdefault(device, {})
        for day, types in inc.items():
            if not isinstance(types, dict):
                continue
            dd = d.setdefault(day, {})
            for t, c in types.items():
                try:
                    dd[t] = dd.get(t, 0) + int(c)
                except (TypeError, ValueError):
                    continue
        save()
    return True


def summarize(group, device, quota):
    today = date.today().strftime("%Y-%m-%d")
    ym = today[:7]
    total_today, total_month = {}, {}
    other_today, other_month = {}, {}
    with _lock:
        for dev, days in store.get(group, {}).items():
            is_self = (dev == device)
            for day, types in days.items():
                if not day.startswith(ym):
                    continue
                for t, c in types.items():
                    total_month[t] = total_month.get(t, 0) + c
                    if day == today:
                        total_today[t] = total_today.get(t, 0) + c
                if not is_self:
                    for t, c in types.items():
                        other_month[t] = other_month.get(t, 0) + c
                        if day == today:
                            other_today[t] = other_today.get(t, 0) + c
    total_month_sum = sum(total_month.values())
    remaining = max(0, quota - total_month_sum) if quota > 0 else 0
    return {
        "otherToday": other_today,
        "otherMonth": other_month,
        "totalToday": total_today,
        "totalMonth": total_month,
        "quota": quota,
        "remaining": remaining,
    }


def check_token(headers, query):
    if not TOKEN:
        return True
    h = headers.get("X-Token", "")
    if h and h == TOKEN:
        return True
    q = query.get("token", [""])[0]
    if q and q == TOKEN:
        return True
    return False


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self, code, html):
        data = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if not self.path.startswith("/report"):
            self._send(404, {"ok": False, "error": "not found"})
            return
        qs = parse_qs(urlparse(self.path).query)
        if not check_token(self.headers, qs):
            self._send(403, {"ok": False, "error": "forbidden: token required"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length > 0 else b"{}"
            body = json.loads(raw.decode("utf-8"))
        except Exception as e:
            self._send(400, {"ok": False, "error": str(e)})
            return
        ok = add_report(body)
        self._send(200, {"ok": ok})

    def do_GET(self):
        if self.path.startswith("/summary"):
            qs = parse_qs(urlparse(self.path).query)
            if not check_token(self.headers, qs):
                self._send(403, {"error": "forbidden: token required"})
                return
            group = (qs.get("group") or [""])[0]
            device = (qs.get("device") or [""])[0]
            try:
                quota = int((qs.get("quota") or ["0"])[0])
            except ValueError:
                quota = 0
            self._send(200, summarize(group, device, quota))
            return
        if self.path == "/" or self.path.startswith("/status"):
            with _lock:
                groups = len(store)
                devices = sum(len(v) for v in store.values())
            self._send_html(200,
                "<!doctype html><meta charset=utf-8><title>GPS 用量后端</title>"
                "<h2>GPS 用量后端运行中</h2>"
                f"<p>配额组数: <b>{groups}</b> · 设备数: <b>{devices}</b></p>"
                f"<p>鉴权: {'已开启 (USAGE_TOKEN)' if TOKEN else '未开启（建议公网部署时设置）'}</p>"
                "<p>接口: POST /report · GET /summary</p>")
            return
        self._send(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        pass


def main():
    host = "0.0.0.0"
    port = int(os.environ.get("PORT", "8000"))
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--port" and i + 1 < len(args):
            port = int(args[i + 1])
        elif a == "--host" and i + 1 < len(args):
            host = args[i + 1]
        elif a == "--token" and i + 1 < len(args):
            global TOKEN
            TOKEN = args[i + 1]
    load()
    print(f"用量后端已启动: http://{host}:{port}  数据文件: {STORE_FILE}")
    print(f"鉴权: {'已开启' if TOKEN else '未开启'}")
    srv = ThreadingHTTPServer((host, port), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
