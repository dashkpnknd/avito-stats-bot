from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, KeyboardButtonRequestChat, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


class StoreCallback(CallbackData, prefix="store"):
    action: str
    store_id: int
    report_type: str = ""


def _enabled(value: int | bool) -> str:
    return "✅" if value else "⛔"


def get_admin_panel_keyboard(stores: list) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for store in stores:
        statuses = " ".join(
            (_enabled(store["daily_enabled"]), _enabled(store["weekly_enabled"]), _enabled(store["monthly_enabled"]))
        )
        builder.button(
            text=f"{store['store_name']} — {statuses}",
            callback_data=StoreCallback(action="manage", store_id=store["id"]),
        )
    builder.row(InlineKeyboardButton(text="➕ Добавить проект", callback_data="add_store"))
    builder.row(InlineKeyboardButton(text="🔄 Обновить список", callback_data="refresh_panel"))
    builder.adjust(1)
    return builder.as_markup()


def get_store_management_keyboard(store) -> InlineKeyboardMarkup:
    store_id = store["id"]
    builder = InlineKeyboardBuilder()
    for report_type, label, field in (
        ("daily", "Ежедневный", "daily_enabled"),
        ("weekly", "Еженедельный", "weekly_enabled"),
        ("monthly", "Ежемесячный", "monthly_enabled"),
    ):
        builder.button(
            text=f"{_enabled(store[field])} {label}",
            callback_data=StoreCallback(action="toggle", store_id=store_id, report_type=report_type),
        )
    builder.button(
        text=f"{_enabled(store['low_balance_enabled'])} Контроль баланса",
        callback_data=StoreCallback(action="toggle_balance", store_id=store_id),
    )
    mention = store["client_mention"] or "не задан"
    builder.button(
        text=f"✏️ Тег клиента: {mention[:24]}",
        callback_data=StoreCallback(action="set_mention", store_id=store_id),
    )
    builder.button(
        text="🧪 Тест: ежедневный отчёт",
        callback_data=StoreCallback(action="test", store_id=store_id, report_type="daily"),
    )
    builder.button(
        text="🗑 Удалить проект",
        callback_data=StoreCallback(action="confirm_delete", store_id=store_id),
    )
    builder.button(text="⬅️ К проектам", callback_data="back_to_panel")
    builder.adjust(1)
    return builder.as_markup()


def get_delete_confirmation_keyboard(store_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, удалить", callback_data=StoreCallback(action="delete", store_id=store_id).pack()
                ),
                InlineKeyboardButton(
                    text="Отмена", callback_data=StoreCallback(action="manage", store_id=store_id).pack()
                ),
            ]
        ]
    )


def get_chat_selection_keyboard() -> ReplyKeyboardMarkup:
    request_button = KeyboardButton(
        text="📍 Выбрать чат из списка",
        request_chat=KeyboardButtonRequestChat(request_id=1, chat_is_channel=False, bot_is_member=True),
    )
    return ReplyKeyboardMarkup(
        keyboard=[[request_button], [KeyboardButton(text="❌ Отмена")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
