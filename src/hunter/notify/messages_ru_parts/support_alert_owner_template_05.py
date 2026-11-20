"""Сообщения бота: поддержка, рассылка, удаление профиля и админ-панель (срез 05)."""

from __future__ import annotations

from hunter.notify.messages_ru_parts.faq_items_03 import (
    DISCLAIMER_DELAY_POSSIBLE,
    DISCLAIMER_NOT_AFFILIATED,
    DISCLAIMER_OWNER_SEES,
    DISCLAIMER_SEARCH_LINKS_ONLY,
)

# --- Поддержка /support и /tickets (M5) ---
SUPPORT_ALERT_OWNER_TEMPLATE = (
    "📩 <b>Новое обращение в поддержку #{ticket_id}</b>\nОт: {user_repr}\n\n{text}"
)
SUPPORT_REPLY_PROMPT_TEMPLATE = "Введите текст ответа на обращение #{ticket_id}:"
SUPPORT_REPLY_SUCCESS_TEMPLATE = (
    "✅ Ответ на обращение #{ticket_id} успешно доставлен пользователю."
)
SUPPORT_DELIVERY_FAILED_BLOCKED = "⚠️ Не удалось доставить ответ: пользователь заблокировал бота."
SUPPORT_DELIVERY_FAILED_DELETED = "⚠️ Не удалось доставить ответ: пользователь удалил свои данные."
SUPPORT_USER_REPLY_TEMPLATE = (
    "📩 <b>Ответ службы поддержки на обращение #{ticket_id}:</b>\n\n{text}"
)
SUPPORT_TICKETS_EMPTY = "Открытых обращений нет."
SUPPORT_TICKETS_HEADER = "📋 <b>Открытые обращения в поддержку:</b>\n"
SUPPORT_BTN_REPLY = "Ответить"

# --- Рассылка /broadcast (M5) ---
BROADCAST_PROMPT = "Введите текст рассылки для пользователей (без разметки):"
BROADCAST_PREVIEW_TEMPLATE = (
    "📢 <b>Предпросмотр рассылки:</b>\n\n"
    "{text}\n\n"
    "👥 Получателей: всего {total}, семья {family}, друзья {friend}\n"
    "Выберите аудиторию для отправки:"
)
BROADCAST_BTN_ALL = "Всем ({count})"
BROADCAST_BTN_FAMILY = "Только семье ({count})"
BROADCAST_BTN_FRIEND = "Только друзьям ({count})"
BROADCAST_BTN_CANCEL = "Отмена"
BROADCAST_CANCELLED = "Рассылка отменена."
BROADCAST_RESULT_TEMPLATE = (
    "✅ <b>Рассылка завершена:</b>\nДоставлено: {delivered}\nНе доставлено: {failed}"
)

# --- Жизненный цикл и удаление /delete_me (M5) ---
DELETE_ME_CONFIRM = (
    "⚠️ <b>Удаление аккаунта и данных</b>\n\n"
    "Это действие безвозвратно удалит все ваши товары, найденные объявления, историю и настройки.\n"
    "Продолжить?"
)
DELETE_ME_BTN_CONFIRM = "Да, удалить всё"
DELETE_ME_BTN_CANCEL = "Отмена"
DELETE_ME_CANCELLED = "Удаление отменено."
DELETE_ME_SUCCESS = "Ваш аккаунт и все связанные данные успешно удалены."
DELETE_ME_OWNER_FORBIDDEN = "Владелец бота не может удалить свой аккаунт."
DELETE_ME_OWNER_ALERT_TEMPLATE = "ℹ️ Пользователь {user_repr} удалил свои данные."

# --- Админ-панель /admin (M5) ---
ADMIN_MENU_TITLE = "⚙️ <b>Панель управления (Владелец):</b>"
ADMIN_BTN_USERS = "👥 Пользователи"
ADMIN_BTN_INVITES = "🎫 Приглашения"
ADMIN_BTN_BROADCAST = "📢 Рассылка"
ADMIN_BTN_STATS = "📊 Статистика"
ADMIN_BTN_SYSTEM = "🖥 Состояние системы"
ADMIN_BTN_ERRORS = "⚠️ Ошибки сети (24ч)"
ADMIN_BTN_BACK = "◀️ Назад"

ADMIN_CONFIRM_EXPIRED = "Срок действия подтверждения истёк (2 минуты). Повторите действие."
ADMIN_USER_DETAIL_TEMPLATE = (
    "👤 <b>Пользователь:</b> {name} ({user_repr})\n"
    "ID: {id}\n"
    "Статус: {status_ru}\n"
    "Тариф: {tier_ru}\n"
    "Товаров: {products_count} (макс. {max_products})\n"
    "Интервал опроса: от {min_interval} мин\n"
    "Лимит находок: {max_alerts}/час"
)
ADMIN_BTN_U_PAUSE = "⏸ Пауза всех товаров"
ADMIN_BTN_U_RESUME = "▶️ Снять паузу товаров"
ADMIN_BTN_U_BLOCK = "🚫 Заблокировать"
ADMIN_BTN_U_UNBLOCK = "✅ Разблокировать"
ADMIN_BTN_U_TIER = "🏷 Сменить тариф"
ADMIN_BTN_U_DELETE = "🗑 Удалить пользователя"

ADMIN_BLOCK_CONFIRM_TEMPLATE = (
    "⚠️ Заблокировать пользователя {name} (ID: {id})?\n"
    "Его товары будут поставлены на паузу, а доступ к боту закрыт.\n"
    "Подтверждение действует 2 минуты."
)
ADMIN_DELETE_CONFIRM_TEMPLATE = (
    "⚠️ <b>Внимание!</b> Удалить пользователя {name} (ID: {id})?\n"
    "Все его товары, история и настройки будут безвозвратно стёрты!\n"
    "Подтверждение действует 2 минуты."
)

ADMIN_TIER_CHANGED_USER_NOTIFY_TEMPLATE = (
    "Ваш тариф изменён на «{tier_ru}». Лимиты и интервалы опроса ваших товаров обновлены."
)

# --- Единый справочник команд и руководство /help, /guide (M5) ---
HELP_COMMANDS_SUMMARY = (
    "<b>Команды бота:</b>\n"
    "/my — мои товары, пауза, изменение цены\n"
    "/add — добавить новый товар по ссылке с Авито\n"
    "/found — последние найденные объявления\n"
    "/pause_all — поставить все товары на паузу\n"
    "/resume_all — возобновить поиск по всем товарам\n"
    "/quiet — настроить тихие часы (без ночных уведомлений)\n"
    "/support — задать вопрос владельцу бота\n"
    "/delete_me — удалить свои данные и товары\n"
    "/guide — подробное руководство по использованию\n"
    "/help — краткая справка"
)

GUIDE_TEXT = f"""📖 <b>Руководство пользователя:</b>

1. <b>О боте и правила</b>
{DISCLAIMER_NOT_AFFILIATED}
{DISCLAIMER_OWNER_SEES}
{DISCLAIMER_SEARCH_LINKS_ONLY}
{DISCLAIMER_DELAY_POSSIBLE}

2. <b>Как взять ссылку на Авито</b>
• Откройте Авито в браузере или приложении.
• Введите название нужного товара и примените фильтры (город, состояние, доставка).
• Скопируйте ссылку из адресной строки или через кнопку «Поделиться».
• Ссылка должна начинаться на avito.ru или m.avito.ru.

3. <b>Как добавить товар</b>
• Нажмите «➕ Добавить товар» или отправьте команду /add.
• Назовите товар понятным именем (например: Sony WH-1000XM6).
• Отправьте скопированную ссылку на поиск.
• Укажите вашу максимальную цену.

4. <b>Что значат сообщения и кнопки</b>
• Каждое сообщение содержит фото, название, цену и город найденного лота.
• Кнопка «Открыть на Авито» ведёт прямо к объявлению для связи с продавцом.

5. <b>Пауза и изменение цены</b>
• В разделе /my вы можете в любой момент изменить предельную цену или поставить товар на паузу.
• Команды /pause_all и /resume_all управляют сразу всеми товарами.

6. <b>Тихие часы</b>
• Команда /quiet позволяет задать интервал (например, 23:00–08:00), \
когда бот не будет беспокоить звуками, а соберёт утреннюю подборку.

7. <b>Удаление данных</b>
• Команда /delete_me полностью удаляет ваши товары, историю и настройки.

8. <b>Поддержка</b>
• Если у вас есть вопрос или пожелание, используйте команду /support."""
