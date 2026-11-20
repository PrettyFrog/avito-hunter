"""Fake Avito server for local test stand and simulated runs (aiohttp, <= 400 lines).

Serves realistic search HTML and item cards matching Avito format.
Supports scriptable scenarios: new item, price change, disappeared item,
empty page, 429 rate limit, 403 block, captcha/challenge page, delayed response.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import html
import json
import time
from typing import Any

from aiohttp import web


@dataclass
class FakeAvitoItem:
    id: str
    title: str
    price: int
    url: str
    location: str = "Москва"
    seller_name: str = "Частное лицо"
    seller_type: str = "private"
    published_at: str = ""

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "id": int(self.id) if self.id.isdigit() else self.id,
            "title": self.title,
            "urlPath": self.url,
            "priceDetailed": {"value": self.price, "postfix": "₽"},
            "location": {"name": self.location},
            "seller": {"name": self.seller_name, "type": self.seller_type},
        }

    def to_html_block(self) -> str:
        safe_title = html.escape(self.title)
        safe_url = html.escape(self.url)
        return (
            f'<div data-marker="item" data-item-id="{self.id}">'
            f'  <a data-marker="item-title" href="{safe_url}">{safe_title}</a>'
            f'  <meta itemprop="price" content="{self.price}">'
            f'  <span data-marker="item-price">{self.price:,} ₽</span>'
            f'  <div data-marker="item-location">{html.escape(self.location)}</div>'
            f'</div>'
        )


class FakeAvitoStand:
    """Mock Avito server serving realistic HTML and scriptable failure modes."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self.host = host
        self.port = port
        self.app = web.Application()
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self.actual_port: int = 0

        self.mode: str = "ok"  # "ok", "empty", "429", "403", "challenge", "corrupt"
        self.delay_sec: float = 0.0
        self.items: dict[str, FakeAvitoItem] = {}
        self.request_history: list[dict[str, Any]] = []

        self._setup_routes()

    def _setup_routes(self) -> None:
        self.app.router.add_route("GET", "/{tail:.*}", self._handle_get)
        self.app.router.add_route("HEAD", "/{tail:.*}", self._handle_get)

    def add_item(
        self,
        id: str | int,
        title: str,
        price: int,
        url: str | None = None,
        location: str = "Москва",
        seller_name: str = "Частное лицо",
    ) -> FakeAvitoItem:
        str_id = str(id)
        url_id = str_id
        if url_id.isdigit() and len(url_id) < 9:
            url_id = f"400{url_id.zfill(7)}"
        elif not url_id.isdigit():
            import hashlib

            num = int(hashlib.md5(url_id.encode()).hexdigest()[:8], 16) % 900000000 + 1000000000
            url_id = str(num)
        full_url = url or f"/moskva/audio_i_video/item_{url_id}"
        item = FakeAvitoItem(
            id=str_id,
            title=title,
            price=price,
            url=full_url,
            location=location,
            seller_name=seller_name,
            published_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        self.items[str_id] = item
        return item

    def update_price(self, id: str | int, new_price: int) -> None:
        str_id = str(id)
        if str_id in self.items:
            self.items[str_id].price = new_price

    def remove_item(self, id: str | int) -> None:
        self.items.pop(str(id), None)

    def clear_items(self) -> None:
        self.items.clear()

    def set_mode(self, mode: str) -> None:
        self.mode = mode

    def set_delay(self, seconds: float) -> None:
        self.delay_sec = max(0.0, seconds)

    def _render_search_html(self, query_path: str) -> str:
        items_list = list(self.items.values())
        json_items = [it.to_json_dict() for it in items_list]
        state_payload = {
            "catalog": {"items": json_items, "total": len(json_items)},
            "current": {"location": "Москва", "query": query_path},
        }
        json_blob = json.dumps(state_payload, ensure_ascii=False)
        html_blocks = "\n".join(it.to_html_block() for it in items_list)
        return (
            "<!DOCTYPE html>\n"
            "<html lang=\"ru\">\n"
            "<head><meta charset=\"utf-8\"><title>Авито — Поиск</title></head>\n"
            "<body>\n"
            f'<script type="application/json" id="mfe-state">{json_blob}</script>\n'
            f'<div data-marker="catalog-serp">\n{html_blocks}\n</div>\n'
            "</body></html>"
        )

    def _render_empty_html(self) -> str:
        state_payload = {"catalog": {"items": [], "total": 0}}
        json_blob = json.dumps(state_payload, ensure_ascii=False)
        return (
            "<!DOCTYPE html>\n"
            "<html><head><meta charset=\"utf-8\"></head><body>\n"
            f'<script type="application/json" id="mfe-state">{json_blob}</script>\n'
            '<div data-marker="empty-page">Ничего не найдено</div>\n'
            "</body></html>"
        )

    def _render_challenge_html(self) -> str:
        return (
            "<!DOCTYPE html>\n"
            "<html><head><meta charset=\"utf-8\"><title>Проверка безопасности</title></head>\n"
            "<body>\n"
            '<div class="firewall/captcha">\n'
            '  <h1>Проверка безопасности</h1>\n'
            '  <div data-marker="captcha-box">Доступ ограничен: проблема с IP</div>\n'
            '</div>\n'
            "</body></html>"
        )

    def _render_blocked_html(self) -> str:
        return (
            "<!DOCTYPE html>\n"
            "<html><head><meta charset=\"utf-8\"><title>Доступ ограничен</title></head>\n"
            "<body>\n"
            '<div class="security-firewall-block">\n'
            '  <h1>Доступ с вашего IP временно ограничен</h1>\n'
            '</div>\n'
            "</body></html>"
        )

    async def _handle_get(self, request: web.Request) -> web.Response:
        self.request_history.append({
            "path": request.path,
            "query": dict(request.query),
            "headers": dict(request.headers),
            "time": time.time(),
        })

        if self.delay_sec > 0:
            await asyncio.sleep(self.delay_sec)

        if self.mode == "429":
            return web.Response(
                status=429,
                text="Too Many Requests",
                content_type="text/plain",
                headers={"Retry-After": "30"},
            )
        if self.mode == "403":
            return web.Response(
                status=403,
                text=self._render_blocked_html(),
                content_type="text/html",
            )
        if self.mode == "challenge":
            return web.Response(
                status=403,
                text=self._render_challenge_html(),
                content_type="text/html",
            )
        if self.mode == "corrupt":
            return web.Response(
                status=200,
                text="<html><body><div>Truncated response data...",
                content_type="text/html",
            )
        if self.mode == "empty":
            return web.Response(
                status=200,
                text=self._render_empty_html(),
                content_type="text/html",
            )

        html_content = self._render_search_html(request.path)
        return web.Response(status=200, text=html_content, content_type="text/html")

    async def start(self) -> str:
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()
        sockets = self._site._server.sockets
        self.actual_port = sockets[0].getsockname()[1] if sockets else self.port
        return f"http://{self.host}:{self.actual_port}"

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
