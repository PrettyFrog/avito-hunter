"""T-406: Leaf helpers for summary card keyboard building and message sending."""

from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from hunter.notify.telegram_router_parts.originalproductstore_01 import (
    _safe_edit_text,
    _safe_reply,
)


def build_summary_card_keyboard(free_budget: int, backfill_on: bool) -> InlineKeyboardMarkup:
    keyboard_buttons = [
        [
            InlineKeyboardButton(text="✅ Запустить", callback_data="add_ok:live"),
            InlineKeyboardButton(text="👁 Shadow на 24ч", callback_data="add_ok:shadow"),
        ]
    ]

    if free_budget >= 15:
        btn_txt = "📦 Архив: Вкл" if backfill_on else "📦 Архив: Выкл"
        keyboard_buttons.append([InlineKeyboardButton(text=btn_txt, callback_data="add_toggle_archive")])

    keyboard_buttons.append([
        InlineKeyboardButton(text="✏ Изменить", callback_data="add_edit"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="add_cancel"),
    ])

    return InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)


async def send_summary_card(
    target_msg: Any, text: str, kb: InlineKeyboardMarkup
) -> None:
    if hasattr(target_msg, "edit_text"):
        await _safe_edit_text(target_msg, text, reply_markup=kb, parse_mode="Markdown")
    else:
        await _safe_reply(target_msg, text, reply_markup=kb, parse_mode="Markdown")


def _format_summary_card_text(payload: dict[str, Any]) -> str:
    p = payload["product"]
    name, price = p["name"], p["max_price"]
    policy = p.get("match", {}).get("policy", "strict")
    aliases_str = ", ".join(p.get("match", {}).get("aliases", [])[:4]) or "нет"
    reject_str = ", ".join(p.get("match", {}).get("reject", [])[:4]) or "нет"
    hits_count = payload.get("hits_count", 0)
    price_ok_count = payload.get("price_ok_count", 0)
    passing_count = payload.get("passing_count", 0)
    accepted_examples = payload.get("accepted_examples", [])
    rejected_examples = payload.get("rejected_examples", [])
    close_tokens = payload.get("close_tokens", [])
    budget_line = payload.get("budget_line", "")
    budget_warning = payload.get("budget_warning", "")

    backfill_on = p.get("coverage", {}).get("backfill", False)
    archive_status = "включён" if backfill_on else "выключен"
    rec_text = "Рекомендация: **запустить (live)**" if policy == "strict" else "Рекомендация: **сначала shadow на 24ч**"

    lines = [
        f"🔍 **Предпросмотр товара «{name}»:**\n",
        f"• **ID:** `{p['id']}`",
        f"• **Потолок:** {price:,} ₽ (+20% near-miss: {int(price*1.2):,} ₽)",
        f"• **Политика:** `{policy}` (пресет: `{p.get('preset', 'generic')}`)",
        f"• **Алиасы:** {aliases_str}",
        f"• **Отсевы (reject):** {reject_str}",
        f"• **Архивный поиск:** {archive_status}",
        f"• **Выдача поиска:** всего {hits_count} лотов | ≤ цены: {price_ok_count} | прошло фильтры: {passing_count}",
    ]
    if close_tokens:
        tok_str = ", ".join(f"`{tok}` ({cnt})" for tok, cnt in close_tokens[:5])
        lines.append(f"• **Близкие токены (reject):** {tok_str}")
    if accepted_examples:
        lines.append("\n**Примеры подходящих лотов:**")
        for item in accepted_examples[:3]:
            pr_str = f"{item['price']:,} ₽" if item.get("price") else "цена не указана"
            title_clean = item["title"].replace("[", "").replace("]", "")
            url = item.get("url") or "https://www.avito.ru"
            lines.append(f"  [+] {pr_str} — [{title_clean}]({url})")
    if rejected_examples:
        lines.append("\n**Примеры отклонённых лотов:**")
        for item in rejected_examples[:3]:
            pr_str = f"{item['price']:,} ₽" if item.get("price") else "цена не указана"
            title_clean = item["title"].replace("[", "").replace("]", "")
            url = item.get("url") or "https://www.avito.ru"
            lines.append(f"  [-] {pr_str} — [{title_clean}]({url})\n      (причина: {item['reason']})")
    if budget_line:
        lines.append(f"\n📊 {budget_line}")
    if budget_warning:
        lines.append(f"\n{budget_warning}")
    lines.append(f"\n💡 {rec_text}")
    return "\n".join(lines)
