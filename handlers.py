from __future__ import annotations

from datetime import datetime
from html import escape

from aiogram import F, Router, types
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import ReplyKeyboardRemove

import avito_api
import database
from keyboards import (
    StoreCallback,
    get_admin_panel_keyboard,
    get_chat_selection_keyboard,
    get_delete_confirmation_keyboard,
    get_store_management_keyboard,
)
from scheduler import send_project_report
from states import AddStore, EditStore

router = Router()


async def _deny_message(message: types.Message) -> bool:
    return False


async def _deny_callback(callback: types.CallbackQuery) -> bool:
    return False


async def _show_panel(message: types.Message, *, edit: bool = False) -> None:
    stores = await database.get_all_stores()
    text = (
        "👋 <b>Панель отчётности Avito</b>\n\n"
        "Отметки: ежедневный / еженедельный / ежемесячный.\n"
        "Отдельно можно включить контроль низкого баланса и задать тег клиента.\n"
        f"Всего проектов: {len(stores)}"
    )
    markup = get_admin_panel_keyboard(stores)
    if edit:
        await message.edit_text(text, reply_markup=markup)
    else:
        await message.answer(text, reply_markup=markup)


@router.message(Command("get_id"))
async def cmd_get_id(message: types.Message):
    """Public utility: use it in the target group after adding the bot there."""
    if message.chat.type == "private":
        return await message.answer("Добавьте бота в нужную группу и выполните там /get_id.")
    await message.answer(f"ID этого чата: <code>{message.chat.id}</code>", parse_mode="HTML")


@router.message(Command("start", "admin"))
async def cmd_admin(message: types.Message, state: FSMContext):
    if message.chat.type != "private" or await _deny_message(message):
        return
    await state.clear()
    await _show_panel(message)


@router.message(Command("cancel"))
@router.message(F.text == "❌ Отмена")
async def cancel_add_store(message: types.Message, state: FSMContext):
    if await _deny_message(message):
        return
    if await state.get_state():
        await state.clear()
        await message.answer("Добавление проекта отменено.", reply_markup=ReplyKeyboardRemove())


@router.callback_query(F.data == "refresh_panel")
@router.callback_query(F.data == "back_to_panel")
async def refresh_panel(callback: types.CallbackQuery, state: FSMContext):
    if await _deny_callback(callback):
        return
    await state.clear()
    await _show_panel(callback.message, edit=True)
    await callback.answer()


@router.callback_query(F.data == "add_store")
async def start_add_store(callback: types.CallbackQuery, state: FSMContext):
    if await _deny_callback(callback):
        return
    await state.set_state(AddStore.waiting_for_name)
    await callback.message.answer("1️⃣ Введите название проекта:", reply_markup=get_chat_selection_keyboard())
    await callback.answer()


@router.message(AddStore.waiting_for_name)
async def process_name(message: types.Message, state: FSMContext):
    if await _deny_message(message):
        return
    name = (message.text or "").strip()
    if not name:
        return await message.answer("Название не должно быть пустым.")
    await state.update_data(name=name)
    await state.set_state(AddStore.waiting_for_chat_id)
    await message.answer("2️⃣ Выберите клиентский чат или отправьте его ID.")


@router.message(AddStore.waiting_for_chat_id)
async def process_chat_id(message: types.Message, state: FSMContext):
    if await _deny_message(message):
        return
    chat_id = message.chat_shared.chat_id if message.chat_shared else None
    if chat_id is None and message.forward_from_chat:
        chat_id = message.forward_from_chat.id
    if chat_id is None and message.text:
        try:
            chat_id = int(message.text.strip())
        except ValueError:
            pass
    if chat_id is None:
        return await message.answer("Не удалось определить чат. Выберите его, перешлите сообщение или введите числовой ID.")
    await state.update_data(chat_id=chat_id)
    await state.set_state(AddStore.waiting_for_client_id)
    await message.answer("3️⃣ Введите Avito Client ID:", reply_markup=ReplyKeyboardRemove())


@router.message(AddStore.waiting_for_client_id)
async def process_client_id(message: types.Message, state: FSMContext):
    if await _deny_message(message):
        return
    client_id = (message.text or "").strip()
    if not client_id:
        return await message.answer("Client ID не должен быть пустым.")
    await state.update_data(client_id=client_id)
    await state.set_state(AddStore.waiting_for_client_secret)
    await message.answer("4️⃣ Введите Avito Client Secret. После проверки бот попробует удалить это сообщение.")


@router.message(AddStore.waiting_for_client_secret)
async def process_client_secret(message: types.Message, state: FSMContext):
    if await _deny_message(message):
        return
    client_secret = (message.text or "").strip()
    if not client_secret:
        return await message.answer("Client Secret не должен быть пустым.")
    user_data = await state.get_data()
    try:
        token = await avito_api.get_avito_token(user_data["client_id"], client_secret)
        user_id = await avito_api.get_avito_user_id(token)
    except avito_api.AvitoAPIError as exc:
        return await message.answer(f"Не удалось проверить ключи Avito: {str(exc)[:500]}")
    finally:
        try:
            await message.delete()
        except TelegramAPIError:
            pass
    await state.update_data(client_secret=client_secret, user_id=user_id)
    await state.set_state(AddStore.waiting_for_client_mention)
    await message.answer(
        "5️⃣ Введите тег клиента для предупреждений, например @username. "
        "Если тег не нужен — отправьте «-»."
    )


@router.message(AddStore.waiting_for_client_mention)
async def process_client_mention(message: types.Message, state: FSMContext):
    if await _deny_message(message):
        return
    user_data = await state.get_data()
    mention = (message.text or "").strip()
    if mention == "-":
        mention = ""
    await database.add_store(
        user_data["chat_id"],
        user_data["name"],
        user_data["client_id"],
        user_data["client_secret"],
        user_data["user_id"],
        mention,
    )
    await state.clear()
    await message.answer("✅ Проект добавлен. Ежедневный и еженедельный отчёты включены, ежемесячный — выключен.")


@router.callback_query(StoreCallback.filter(F.action == "manage"))
async def manage_store(callback: types.CallbackQuery, callback_data: StoreCallback):
    if await _deny_callback(callback):
        return
    store = await database.get_store_by_id(callback_data.store_id)
    if not store:
        await callback.answer("Проект не найден", show_alert=True)
        return
    text = (
        f"🏪 <b>{escape(store['store_name'])}</b>\n"
        f"Клиентский чат: <code>{store['chat_id']}</code>\n\n"
        f"Тег клиента: {escape(store['client_mention']) if store['client_mention'] else 'не задан'}\n"
        f"Контроль баланса: {'включён' if store['low_balance_enabled'] else 'выключен'}\n\n"
        "Выберите, какие отчёты отправлять автоматически:"
    )
    await callback.message.edit_text(text, reply_markup=get_store_management_keyboard(store))
    await callback.answer()


@router.callback_query(StoreCallback.filter(F.action == "toggle"))
async def toggle_report(callback: types.CallbackQuery, callback_data: StoreCallback):
    if await _deny_callback(callback):
        return
    if callback_data.report_type not in {"daily", "weekly", "monthly"}:
        return await callback.answer("Неизвестный тип отчёта", show_alert=True)
    enabled = await database.toggle_report(callback_data.store_id, callback_data.report_type)
    store = await database.get_store_by_id(callback_data.store_id)
    if enabled is None or not store:
        return await callback.answer("Проект не найден", show_alert=True)
    await callback.message.edit_reply_markup(reply_markup=get_store_management_keyboard(store))
    await callback.answer("Включено" if enabled else "Выключено")


@router.callback_query(StoreCallback.filter(F.action == "toggle_balance"))
async def toggle_balance_monitoring(callback: types.CallbackQuery, callback_data: StoreCallback):
    if await _deny_callback(callback):
        return
    enabled = await database.toggle_low_balance_monitoring(callback_data.store_id)
    store = await database.get_store_by_id(callback_data.store_id)
    if enabled is None or not store:
        return await callback.answer("Проект не найден", show_alert=True)
    await callback.message.edit_reply_markup(reply_markup=get_store_management_keyboard(store))
    await callback.answer("Контроль баланса включён" if enabled else "Контроль баланса выключен")


@router.callback_query(StoreCallback.filter(F.action == "set_mention"))
async def start_set_client_mention(callback: types.CallbackQuery, callback_data: StoreCallback, state: FSMContext):
    if await _deny_callback(callback):
        return
    store = await database.get_store_by_id(callback_data.store_id)
    if not store:
        return await callback.answer("Проект не найден", show_alert=True)
    await state.set_state(EditStore.waiting_for_client_mention)
    await state.update_data(store_id=store["id"])
    await callback.message.answer(
        f"Введите новый тег клиента для «{escape(store['store_name'])}» (@username) или «-», чтобы убрать тег."
    )
    await callback.answer()


@router.message(EditStore.waiting_for_client_mention)
async def save_client_mention(message: types.Message, state: FSMContext):
    if await _deny_message(message):
        return
    data = await state.get_data()
    mention = (message.text or "").strip()
    if mention == "-":
        mention = ""
    if await database.update_client_mention(data["store_id"], mention):
        await message.answer("✅ Тег клиента обновлён.")
    else:
        await message.answer("❌ Проект не найден.")
    await state.clear()


@router.callback_query(StoreCallback.filter(F.action == "test"))
async def manual_send_report(callback: types.CallbackQuery, callback_data: StoreCallback):
    if await _deny_callback(callback):
        return
    store = await database.get_store_by_id(callback_data.store_id)
    if not store:
        return await callback.answer("Проект не найден", show_alert=True)
    await callback.answer("Собираю и отправляю тестовый отчёт…")
    sent = await send_project_report(callback.bot, store, callback_data.report_type, datetime.now().date(), test=True)
    if sent:
        await callback.message.answer("✅ Тестовый отчёт отправлен в клиентский чат.")
    else:
        await callback.message.answer("❌ Не удалось отправить тестовый отчёт. Администратор получил ошибку.")


@router.callback_query(StoreCallback.filter(F.action == "confirm_delete"))
async def confirm_delete(callback: types.CallbackQuery, callback_data: StoreCallback):
    if await _deny_callback(callback):
        return
    store = await database.get_store_by_id(callback_data.store_id)
    if not store:
        return await callback.answer("Проект не найден", show_alert=True)
    await callback.message.edit_text(
        f"Удалить проект «{store['store_name']}» и его журнал рассылок?",
        reply_markup=get_delete_confirmation_keyboard(store["id"]),
    )
    await callback.answer()


@router.callback_query(StoreCallback.filter(F.action == "delete"))
async def delete_store(callback: types.CallbackQuery, callback_data: StoreCallback):
    if await _deny_callback(callback):
        return
    if await database.delete_store(callback_data.store_id):
        await callback.answer("Проект удалён")
        await _show_panel(callback.message, edit=True)
    else:
        await callback.answer("Проект не найден", show_alert=True)
