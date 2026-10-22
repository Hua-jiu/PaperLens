"""Loopback-only HTTP API and static WebUI; no frontend build step required."""

from __future__ import annotations

import json
import mimetypes
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .application import APIError, Application, MAX_UPLOAD_BYTES, normalize_color, safe_error
from .ai_service import ConfigurationError, DryRunError
from .pdf_engine import DEFAULT_FORMULA_SCALE, get_thumbnail, render_figure, render_page

STATIC_ROOT = Path(__file__).resolve().parent.parent / "static"
MAX_JSON_BYTES = 512 * 1024


class ReaderServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, application: Application):
        self.application = application
        super().__init__(address, ReaderHandler)


class ReaderHandler(BaseHTTPRequestHandler):
    server_version = "PaperLens/1.0"

    @property
    def app(self) -> Application:
        return self.server.application

    def log_message(self, format, *args):
        # Neither local paths, provider credentials nor document selections go to logs.
        return

    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request()

    def do_PUT(self):
        self.handle_request()

    def do_PATCH(self):
        self.handle_request()

    def do_DELETE(self):
        self.handle_request()

    def do_OPTIONS(self):
        self.respond({"error": "跨站请求不受支持"}, 403)

    def handle_request(self):
        try:
            self.check_origin()
            parsed = urlsplit(self.path)
            path = unquote(parsed.path)
            query = parse_qs(parsed.query)
            if path == "/api/health" and self.command == "GET":
                return self.respond({"ok": True, "app": "paperlens", "dry_run": self.app.dryrun})
            if path == "/api/settings":
                if self.command == "GET":
                    return self.respond(self.app.store.settings(public=True))
                if self.command == "PUT":
                    return self.respond(self.app.save_settings(self.read_json()))
            if path in ("/api/settings/test", "/api/settings/models") and self.command == "POST":
                payload = self.read_json()
                provider_id = payload.get("provider_id") or payload.get("id")
                old = next((p for p in self.app.store.settings()["providers"] if p["id"] == provider_id), {})
                provider = {"name": payload.get("name", old.get("name", "供应商")),
                            "base_url": payload.get("base_url") or old.get("base_url", ""),
                            "api_key": "" if payload.get("clear_key") else payload.get("api_key") or old.get("api_key", ""),
                            "default_model": payload.get("default_model", old.get("default_model", ""))}
                try:
                    if path == "/api/settings/models":
                        return self.respond({"models": self.app.service().list_models(provider)})
                    result = self.app.service().test_connection(provider, str(payload.get("model", "")))
                    return self.respond({"ok": True, "message": result})
                except Exception as exc:
                    status = 400 if path == "/api/settings/test" or isinstance(exc, (ConfigurationError, DryRunError)) else 502
                    raise APIError(safe_error(exc), status) from exc
            if path == "/api/papers":
                if self.command == "GET":
                    return self.respond({"papers": self.app.store.list_papers(), "dry_run": self.app.dryrun})
                if self.command == "POST":
                    filename = query.get("filename", ["paper.pdf"])[0]
                    return self.respond(self.app.upload(filename, self.read_body(MAX_UPLOAD_BYTES)), 201)
            if path == "/api/papers/import-path" and self.command == "POST":
                return self.respond(self.app.import_path(str(self.read_json().get("path", ""))), 201)
            match = re.fullmatch(r"/api/papers/([a-f0-9]{32})(?:/(.*))?", path)
            if match:
                return self.paper_route(match.group(1), match.group(2) or "")
            if self.command == "GET" and path in ("/", "/index.html", "/app.js", "/style.css"):
                filename = "index.html" if path == "/" else path[1:]
                content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
                return self.respond_bytes((STATIC_ROOT / filename).read_bytes(), content_type + "; charset=utf-8")
            if path == "/favicon.ico":
                return self.respond_bytes(b"", "image/x-icon", 204)
            raise APIError("页面或接口不存在", 404)
        except APIError as exc:
            self.respond({"error": str(exc)}, exc.status)
        except KeyError as exc:
            self.respond({"error": str(exc).strip("'")}, 404)
        except (ValueError, TypeError, StopIteration, json.JSONDecodeError):
            self.respond({"error": "请求参数格式不正确"}, 400)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self.respond({"error": "操作未完成，请稍后重试"}, 500)

    def paper_route(self, paper_id: str, action: str):
        record = self.app.detail(paper_id) if not action and self.command == "GET" else self.app.store.get(paper_id)
        if not action:
            if self.command == "GET":
                return self.respond(record)
            if self.command == "DELETE":
                self.app.delete(paper_id)
                return self.respond({"ok": True})
        if self.command == "GET":
            if action == "pdf":
                return self.respond_bytes(self.app.store.source(paper_id), "application/pdf")
            if action == "thumbnail":
                return self.respond_bytes(get_thumbnail(self.app.store.source(paper_id)), "image/png")
            page_match = re.fullmatch(r"pages/(\d+)", action)
            if page_match:
                page = int(page_match.group(1))
                if page < 1 or page > record["paper"]["page_count"]:
                    raise APIError("页码超出范围", 404)
                return self.respond_bytes(render_page(self.app.store.source(paper_id), page), "image/png")
            figure_match = re.fullmatch(r"figures/([\w-]+)", action)
            if figure_match:
                figure = next((f for f in record["document"]["figures"] if f["id"] == figure_match.group(1)), None)
                if not figure:
                    raise APIError("图表不存在", 404)
                return self.respond_bytes(render_figure(self.app.store.source(paper_id), figure), "image/png")
            formula_match = re.fullmatch(r"formulas/([\w-]+)", action)
            if formula_match:
                block = next((b for page in record["document"]["pages"] for b in page.get("blocks", [])
                              if b.get("id") == formula_match.group(1) and b.get("display_math")), None)
                if not block:
                    raise APIError("公式不存在", 404)
                x0, y0, x1, y1 = block["bbox"]
                crop = {"page": block["page"], "bbox": [x0 - 3, y0 - 3, x1 + 3, y1 + 3]}
                return self.respond_bytes(render_figure(self.app.store.source(paper_id), crop, DEFAULT_FORMULA_SCALE), "image/png")
        if action == "reading" and self.command == "PUT":
            payload = self.read_json()
            reading = {
                "page": max(1, min(int(payload.get("page", 1)), max(1, record["paper"]["page_count"]))),
                "block_id": str(payload.get("block_id", ""))[:100],
                "scroll": max(0, min(float(payload.get("scroll", 0)), 1e8)),
                "font_size": max(14, min(int(payload.get("font_size", 18)), 30)),
            }
            self.app.store.update(paper_id, lambda r: r["reading"].update(reading))
            return self.respond({"ok": True})
        if action == "translate" and self.command == "POST":
            self.read_json()
            return self.respond(self.app.start_translation(paper_id))
        if action == "reprocess" and self.command == "POST":
            self.read_json()
            if record["paper"]["status"] == "error":
                self.app.submit(paper_id, "parse", self.app.parse,
                                on_queued=lambda: self.app.update_paper(paper_id, status="processing", progress=0))
            else:
                if not any(self.app.module_configured(name) for name in ("summary", "translation", "reflow")):
                    raise APIError("请先在设置中配置 AI 模型", 503)
                if self.app.dryrun:
                    raise APIError("当前为 dryrun 验证模式，AI 网络请求已停用", 503)
                self.app.submit(paper_id, "ai", self.app.enrich,
                                on_queued=lambda: self.app.update_paper(paper_id, ai_status="processing", stage="等待 AI 处理", ai_error=""))
            return self.respond({"ok": True})
        if action == "selection" and self.command == "POST":
            payload = self.read_json()
            text = str(payload.get("text", "")).strip()
            if not text or len(text) > 15000:
                raise APIError("请选择 1 至 15000 字符的文字")
            action_name = payload.get("action")
            if action_name not in ("translate", "explain"):
                raise APIError("不支持的划词操作")
            self.app.require_module("translation" if action_name == "translate" else "chat")
            try:
                if action_name == "translate":
                    result = self.app.service().translate_text(text)
                else:
                    result = self.app.service().explain(record["document"], text)
            except Exception as exc:
                raise APIError(safe_error(exc), 502) from exc
            return self.respond({"text": result})
        if action == "chat" and self.command == "POST":
            payload = self.read_json()
            message = str(payload.get("message", "")).strip()
            if not message or len(message) > 15000:
                raise APIError("问题不能为空且不能超过 15000 字符")
            self.app.require_module("chat")
            try:
                answer = self.app.service().chat(record["document"], record["chat"][-16:], message, str(payload.get("selected_text", ""))[:15000])
            except Exception as exc:
                raise APIError(safe_error(exc), 502) from exc
            reply = {"role": "assistant", "content": answer}
            self.app.store.update(paper_id, lambda r: r["chat"].extend([{"role": "user", "content": message}, reply]))
            return self.respond({"message": reply})
        if action == "highlights" and self.command == "POST":
            return self.respond(self.app.add_highlight(paper_id, self.read_json()), 201)
        highlight_match = re.fullmatch(r"highlights/([a-f0-9]{32})", action)
        if highlight_match and self.command in ("PATCH", "DELETE"):
            highlight_id = highlight_match.group(1)
            if not any(h["id"] == highlight_id for h in record["highlights"]):
                raise APIError("笔记不存在", 404)
            if self.command == "DELETE":
                self.app.store.update(paper_id, lambda r: r.update(highlights=[h for h in r["highlights"] if h["id"] != highlight_id]))
                return self.respond({"ok": True})
            payload = self.read_json()
            def edit(current):
                for item in current["highlights"]:
                    if item["id"] == highlight_id:
                        if "note" in payload:
                            item["note"] = str(payload["note"])[:10000]
                        if "color" in payload:
                            item["color"] = normalize_color(payload["color"])
            updated = self.app.store.update(paper_id, edit)
            return self.respond({"highlight": next(h for h in updated["highlights"] if h["id"] == highlight_id)})
        raise APIError("接口或操作不存在", 404)

    def check_origin(self):
        host = urlsplit("http://" + self.headers.get("Host", "")).hostname
        if host not in ("localhost", "127.0.0.1", "::1"):
            raise APIError("仅允许本机访问", 403)
        origin = self.headers.get("Origin")
        if origin and origin != "http://" + self.headers.get("Host", ""):
            raise APIError("不接受其他站点发起的请求", 403)
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise APIError("不接受跨站请求", 403)

    def read_body(self, maximum: int) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise APIError("请求长度无效") from exc
        if length < 0 or length > maximum:
            raise APIError("上传内容超过大小限制", 413)
        self.connection.settimeout(30)
        data = self.rfile.read(length)
        if len(data) != length:
            raise APIError("上传中断，请重试")
        self.connection.settimeout(180)
        return data

    def read_json(self) -> dict:
        body = self.read_body(MAX_JSON_BYTES)
        result = json.loads(body) if body else {}
        if not isinstance(result, dict):
            raise APIError("请求内容必须为 JSON 对象")
        return result

    def respond(self, payload: dict, status: int = 200):
        self.respond_bytes(json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status)

    def respond_bytes(self, body: bytes, content_type: str, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; frame-src 'self' blob:; object-src 'self'; connect-src 'self'; base-uri 'self'; frame-ancestors 'self'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


def create_server(host: str, port: int, data_dir: Path, dryrun: bool = False) -> ReaderServer:
    if host not in ("127.0.0.1", "localhost"):
        raise ValueError("此版本只支持本机 loopback 地址")
    app = Application(data_dir=data_dir, dryrun=dryrun)
    try:
        return ReaderServer((host, port), app)
    except Exception:
        app.close()
        raise
