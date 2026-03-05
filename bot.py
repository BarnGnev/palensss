import asyncio
import logging
import json
import os
import time
import hashlib
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup,
    KeyboardButton, ReplyKeyboardRemove
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramAPIError
import warnings
warnings.filterwarnings("ignore")

logging.basicConfig(level=logging.CRITICAL)
for lib in ("aiogram", "aiogram.dispatcher", "asyncio"):
    logging.getLogger(lib).setLevel(logging.CRITICAL)

BOT_TOKEN = os.getenv("BOT_TOKEN", "TOKEN")

if not BOT_TOKEN:
    print("❌ ОШИБКА: BOT_TOKEN не установлен!")
    exit(1)

ADMIN_IDS: list[int] = [ # ТУТ ТОЛЬКО ВЛАДЕЛЬЦЫ
    5023066540,  # BarnGnev
    7816073349,  # Bludu455
    # 111222333, # Хз
]

ADMIN_PASSWORD = "PASSWORD"   # ← ЗАМЕНИ на свой пароль

# ==============================================================

DATA_FILE = "data.json"
ADMINS_FILE = "admins.json"

_logs: list = []


def add_log(level: str, message: str, user_id=None):
    try:
        _logs.append({
            "time": datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
            "level": level,
            "message": message,
            "user_id": user_id,
        })
        if len(_logs) > 500:
            _logs.pop(0)
        print(f"[{level}] {message}")
    except Exception as e:
        print(f"Ошибка логирования: {e}")


def hash_pw(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def is_owner(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def load_sessions() -> list:
    if not os.path.exists(ADMINS_FILE):
        return []
    try:
        with open(ADMINS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        add_log("ERROR", f"Ошибка загрузки admins.json: {e}")
        return []


def save_sessions(sessions: list):
    try:
        with open(ADMINS_FILE, "w", encoding="utf-8") as f:
            json.dump(sessions, f, ensure_ascii=False, indent=2)
    except Exception as e:
        add_log("ERROR", f"Ошибка сохранения admins.json: {e}")


def is_admin(user_id: int) -> bool:
    sessions = load_sessions()
    return any(s["user_id"] == user_id for s in sessions)


def check_password(password: str) -> bool:
    return hash_pw(password) == hash_pw(ADMIN_PASSWORD)


def open_session(user_id: int):
    sessions = load_sessions()
    if not any(s["user_id"] == user_id for s in sessions):
        sessions.append({
            "user_id": user_id,
            "created": datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
        })
        save_sessions(sessions)


def close_session(user_id: int):
    sessions = load_sessions()
    sessions = [s for s in sessions if s["user_id"] != user_id]
    save_sessions(sessions)


def load_data() -> dict:
    if not os.path.exists(DATA_FILE):
        return {
            "channels": [],
            "file_url": "",
            "start_text": "👋 Привет! Подпишись на каналы ниже, чтобы получить файл.",
            "wait_minutes": 0,
            "wait_enabled": False,
            "link_delete_seconds": 0,
            "users": {},
            "banned": [],
        }
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        add_log("ERROR", f"Ошибка загрузки data.json: {e}")
        return {
            "channels": [],
            "file_url": "",
            "start_text": "👋 Привет! Подпишись на каналы ниже, чтобы получить файл.",
            "wait_minutes": 0,
            "wait_enabled": False,
            "link_delete_seconds": 0,
            "users": {},
            "banned": [],
        }


def save_data(data: dict):
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        add_log("ERROR", f"Ошибка сохранения data.json: {e}")

class AdminStates(StatesGroup):
    entering_password = State()
    adding_channel_id = State()
    adding_channel_name = State()
    adding_channel_url = State()
    setting_file_url = State()
    setting_start_text = State()
    broadcast_text = State()
    ban_user_id = State()


# ===== ИНИЦИАЛИЗАЦИЯ БОТА =====

bot = None
dp = None


async def check_subscriptions(user_id: int, channels: list) -> list:
    if not bot:
        return channels
    not_subbed = []
    for ch in channels:
        try:
            m = await bot.get_chat_member(ch["id"], user_id)
            if m.status in ("left", "kicked", "banned"):
                not_subbed.append(ch)
        except TelegramAPIError as e:
            add_log("WARN", f"Ошибка проверки канала {ch['id']}: {e}")
            not_subbed.append(ch)
        except Exception as e:
            add_log("ERROR", f"Ошибка проверки {ch['id']}: {e}")
            not_subbed.append(ch)
    return not_subbed


def build_sub_keyboard(channels: list) -> InlineKeyboardMarkup:
    rows = []
    for ch in channels:
        rows.append([InlineKeyboardButton(
            text=f"📢 {ch.get('name', ch['id'])}",
            url=ch.get("url", f"https://t.me/{ch['id'].lstrip('@')}")
        )])
    rows.append([InlineKeyboardButton(text="✅ Проверить подписку", callback_data="check_sub")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="⚙️ Настройки")],
            [KeyboardButton(text="👥 Пользователи"), KeyboardButton(text="📨 Рассылка")],
            [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="📋 Логи")],
            [KeyboardButton(text="🚪 Выйти из админки")],
        ],
        resize_keyboard=True
    )


async def send_file_async(user, data: dict):
    if not bot:
        return
    try:
        uid = user.id if isinstance(user, types.User) else user
        file_url = data.get("file_url", "")
        wait_enabled = data.get("wait_enabled", False)
        wait_min = data.get("wait_minutes", 0)
        del_secs = data.get("link_delete_seconds", 0)
        udata = data["users"].get(str(uid), {})

        if wait_enabled and wait_min > 0:
            ws = udata.get("wait_start")
            if not ws:
                data["users"][str(uid)]["wait_start"] = time.time()
                save_data(data)
                await bot.send_message(
                    uid,
                    f"⏳ <b>Почти готово!</b>\n\nФайл будет доступен через <b>{wait_min} мин.</b>\n"
                    f"Нажми /start снова, когда время истечёт.",
                    parse_mode="HTML"
                )
                add_log("INFO", f"Таймер ожидания запущен для ID: {uid} на {wait_min} мин.", uid)
                return
            elapsed = (time.time() - ws) / 60
            if elapsed < wait_min:
                rem = wait_min - elapsed
                await bot.send_message(uid, f"⏳ Подождите ещё <b>{rem:.1f} мин.</b>", parse_mode="HTML")
                return
            data["users"][str(uid)]["wait_start"] = None

        data["users"][str(uid)]["subscribed"] = True
        data["users"][str(uid)]["in_channel"] = True
        save_data(data)

        if file_url:
            sent = await bot.send_message(
                uid,
                f"✅ <b>Спасибо за подписку!</b>\n\n🔗 Вот ваша ссылка:\n{file_url}",
                parse_mode="HTML"
            )
            add_log("INFO", f"Ссылка отправлена пользователю ID: {uid}", uid)
            if del_secs and del_secs > 0:
                asyncio.create_task(_delete_later(uid, sent.message_id, del_secs))
        else:
            await bot.send_message(uid, "✅ Вы подписаны! Ссылка пока не задана администратором.")
    except TelegramAPIError as e:
        add_log("ERROR", f"Ошибка отправки файла: {e}")
    except Exception as e:
        add_log("ERROR", f"Ошибка send_file_async: {e}")


async def _delete_later(uid: int, msg_id: int, delay: int):
    if not bot:
        return
    try:
        await asyncio.sleep(delay)
        await bot.delete_message(uid, msg_id)
        await bot.send_message(uid, "⏰ Ссылка была удалена по истечении срока действия.")
        add_log("INFO", f"Ссылка удалена у пользователя ID: {uid}", uid)
    except Exception as e:
        add_log("WARN", f"Не удалось удалить сообщение у {uid}: {e}")


async def setup_handlers(dp: Dispatcher):

    # ===== /start =====

    @dp.message(Command("start"))
    async def cmd_start(message: types.Message, state: FSMContext):
        try:
            uid = message.from_user.id
            name = message.from_user.username or message.from_user.first_name
            data = load_data()

            # Регистрируем нового пользователя
            if str(uid) not in data["users"]:
                data["users"][str(uid)] = {
                    "id": uid,
                    "username": name,
                    "first_name": message.from_user.first_name,
                    "joined": datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
                    "subscribed": False,
                    "role": "member",
                    "wait_start": None,
                    "in_channel": False,
                }
                save_data(data)
                add_log("INFO", f"Новый пользователь: {name} (ID: {uid})", uid)

            # Бан
            if uid in data.get("banned", []):
                await message.answer("🚫 Вы заблокированы и не можете использовать этого бота.")
                add_log("WARN", f"Заблокированный пользователь (ID: {uid})", uid)
                return

            if is_admin(uid):
                await state.clear()
                await message.answer("🔑 Добро пожаловать, админ!", reply_markup=admin_keyboard())
                return

            channels = data.get("channels", [])
            if not channels:
                await message.answer("⚙️ Бот ещё не настроен администратором.")
                return

            not_subbed = await check_subscriptions(uid, channels)
            start_text = data.get("start_text", "👋 Подпишись на каналы, чтобы получить файл.")

            if not_subbed:
                kb = build_sub_keyboard(not_subbed)
                await message.answer(start_text, reply_markup=kb, parse_mode="HTML")
                add_log("INFO", f"Пользователь {name} не подписан на {len(not_subbed)} канал(а)", uid)
            else:
                await send_file_async(message.from_user, data)
        except Exception as e:
            add_log("ERROR", f"Ошибка в cmd_start: {e}")

    @dp.callback_query(F.data == "check_sub")
    async def on_check_sub(cb: types.CallbackQuery):
        try:
            uid = cb.from_user.id
            data = load_data()
            if uid in data.get("banned", []):
                await cb.answer("🚫 Вы заблокированы.", show_alert=True)
                return
            not_subbed = await check_subscriptions(uid, data.get("channels", []))
            if not_subbed:
                names = ", ".join(ch.get("name", ch["id"]) for ch in not_subbed)
                await cb.answer(f"❌ Ещё не подписаны на: {names}", show_alert=True)
                return
            await cb.message.delete()
            await send_file_async(cb.from_user, data)
            await cb.answer("✅ Спасибо за подписку!")
        except Exception as e:
            add_log("ERROR", f"Ошибка в on_check_sub: {e}")

    @dp.message(Command("admin_panel"))
    async def cmd_admin_panel(message: types.Message, state: FSMContext):
        try:
            uid = message.from_user.id

            if is_admin(uid):
                await state.clear()
                await message.answer("🔑 Вы уже в админ-панели.", reply_markup=admin_keyboard())
                return

            if is_owner(uid):
                open_session(uid)
                await state.clear()
                await message.answer(
                    "🔑 <b>Добро пожаловать, владелец!</b>",
                    parse_mode="HTML",
                    reply_markup=admin_keyboard()
                )
                add_log("INFO", f"Владелец ID {uid} вошёл в панель")
                return

            await message.answer(
                "🔐 <b>Введите пароль администратора:</b>\n\n"
                "<i>Если не знаете пароль — обратитесь к владельцу бота.</i>",
                parse_mode="HTML",
                reply_markup=ReplyKeyboardRemove()
            )
            await state.set_state(AdminStates.entering_password)
            add_log("INFO", f"Попытка входа в админку: ID {uid}")
        except Exception as e:
            add_log("ERROR", f"Ошибка в cmd_admin_panel: {e}")

    @dp.message(AdminStates.entering_password)
    async def process_admin_password(message: types.Message, state: FSMContext):
        try:
            uid = message.from_user.id
            entered = message.text.strip()

            try:
                await message.delete()
            except Exception:
                pass

            if check_password(entered):
                open_session(uid)
                await state.clear()
                await message.answer(
                    "✅ <b>Вход выполнен!</b>\nДобро пожаловать в админ-панель.",
                    parse_mode="HTML",
                    reply_markup=admin_keyboard()
                )
                add_log("INFO", f"Успешный вход в админку: ID {uid}")
            else:
                await state.clear()
                await message.answer("❌ <b>Неверный пароль.</b>\nДоступ запрещён.", parse_mode="HTML")
                add_log("WARN", f"Неверный пароль от ID {uid}")
        except Exception as e:
            add_log("ERROR", f"Ошибка в process_admin_password: {e}")

    @dp.message(F.text == "🚪 Выйти из админки")
    async def logout_admin(message: types.Message, state: FSMContext):
        try:
            uid = message.from_user.id
            if not is_admin(uid):
                await message.answer("❌ Вы не в админ-панели.", reply_markup=ReplyKeyboardRemove())
                return

            close_session(uid)
            await state.clear()
            await message.answer(
                "👋 Вы вышли из админ-панели.\n"
                "Чтобы войти снова — напишите /admin_panel",
                reply_markup=ReplyKeyboardRemove()
            )
            add_log("INFO", f"Админ ID {uid} вышел из панели")
        except Exception as e:
            add_log("ERROR", f"Ошибка в logout_admin: {e}")

    @dp.message(F.text == "⚙️ Настройки")
    async def show_settings(message: types.Message):
        if not is_admin(message.from_user.id):
            await message.answer("❌ Доступ запрещён")
            return

        data = load_data()
        text = (
            f"⚙️ <b>НАСТРОЙКИ</b>\n\n"
            f"📢 <b>Каналы:</b> {len(data['channels'])} шт.\n"
            f"🔗 <b>Ссылка на файл:</b> {data['file_url'] if data['file_url'] else 'Не установлена'}\n"
            f"⏳ <b>Таймер:</b> {data['wait_minutes']} мин "
            f"({'включен' if data['wait_enabled'] else 'отключен'})\n"
            f"⏰ <b>Автоудаление:</b> {data['link_delete_seconds']} сек"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить канал", callback_data="add_ch")],
            [InlineKeyboardButton(text="🔗 Установить файл", callback_data="set_file")],
            [InlineKeyboardButton(text="✏️ Изменить текст", callback_data="set_text")],
        ])
        await message.answer(text, reply_markup=kb, parse_mode="HTML")

    @dp.callback_query(F.data == "add_ch")
    async def add_channel_handler(cb: types.CallbackQuery, state: FSMContext):
        if not is_admin(cb.from_user.id):
            await cb.answer("❌ Доступ запрещён", show_alert=True)
            return
        await cb.message.answer("📢 Введи ID или username канала (@channel):")
        await state.set_state(AdminStates.adding_channel_id)
        await cb.answer()

    @dp.message(AdminStates.adding_channel_id)
    async def process_channel_id(message: types.Message, state: FSMContext):
        await state.update_data(channel_id=message.text)
        await message.answer("📝 Введи название канала:")
        await state.set_state(AdminStates.adding_channel_name)

    @dp.message(AdminStates.adding_channel_name)
    async def process_channel_name(message: types.Message, state: FSMContext):
        await state.update_data(channel_name=message.text)
        await message.answer("🔗 Введи ссылку на канал (или . для автогенерации):")
        await state.set_state(AdminStates.adding_channel_url)

    @dp.message(AdminStates.adding_channel_url)
    async def process_channel_url(message: types.Message, state: FSMContext):
        data_ctx = await state.get_data()
        data = load_data()
        ch_id = data_ctx["channel_id"]
        ch_url = message.text if message.text != "." else f"https://t.me/{ch_id.lstrip('@')}"
        data["channels"].append({"id": ch_id, "name": data_ctx["channel_name"], "url": ch_url})
        save_data(data)
        await state.clear()
        await message.answer("✅ Канал добавлен!")
        add_log("INFO", f"Канал добавлен: {data_ctx['channel_name']}")

    @dp.callback_query(F.data == "set_file")
    async def set_file_handler(cb: types.CallbackQuery, state: FSMContext):
        if not is_admin(cb.from_user.id):
            await cb.answer("❌ Доступ запрещён", show_alert=True)
            return
        await cb.message.answer("🔗 Введи ссылку на файл:")
        await state.set_state(AdminStates.setting_file_url)
        await cb.answer()

    @dp.message(AdminStates.setting_file_url)
    async def process_file_url(message: types.Message, state: FSMContext):
        data = load_data()
        data["file_url"] = message.text
        save_data(data)
        await state.clear()
        await message.answer("✅ Ссылка установлена!")
        add_log("INFO", f"Ссылка: {message.text}")

    @dp.callback_query(F.data == "set_text")
    async def set_text_handler(cb: types.CallbackQuery, state: FSMContext):
        if not is_admin(cb.from_user.id):
            await cb.answer("❌ Доступ запрещён", show_alert=True)
            return
        await cb.message.answer("✏️ Введи новый текст для /start:")
        await state.set_state(AdminStates.setting_start_text)
        await cb.answer()

    @dp.message(AdminStates.setting_start_text)
    async def process_start_text(message: types.Message, state: FSMContext):
        data = load_data()
        data["start_text"] = message.text
        save_data(data)
        await state.clear()
        await message.answer("✅ Текст обновлён!")

    # ===== ПОЛЬЗОВАТЕЛИ + БАН ПО ID =====

    @dp.message(F.text == "👥 Пользователи")
    async def show_users(message: types.Message):
        if not is_admin(message.from_user.id):
            await message.answer("❌ Доступ запрещён")
            return
        data = load_data()
        total = len(data["users"])
        subscribed = sum(1 for u in data["users"].values() if u.get("subscribed"))
        banned = len(data.get("banned", []))
        text = (
            f"👥 <b>ПОЛЬЗОВАТЕЛИ</b>\n\n"
            f"📊 Всего: <b>{total}</b>\n"
            f"✅ Подписаны: <b>{subscribed}</b>\n"
            f"🚫 Заблокированы: <b>{banned}</b>"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚫 Заблокировать по ID", callback_data="ban_by_id")],
            [InlineKeyboardButton(text="✅ Разблокировать по ID", callback_data="unban_by_id")],
        ])
        await message.answer(text, reply_markup=kb, parse_mode="HTML")

    @dp.callback_query(F.data == "ban_by_id")
    async def ban_by_id_handler(cb: types.CallbackQuery, state: FSMContext):
        if not is_admin(cb.from_user.id):
            await cb.answer("❌ Доступ запрещён", show_alert=True)
            return
        await cb.message.answer(
            "🚫 <b>Бан пользователя</b>\n\n"
            "Введи <b>Telegram ID</b> пользователя:\n"
            "<i>(ID можно узнать у бота @userinfobot)</i>",
            parse_mode="HTML"
        )
        await state.set_state(AdminStates.ban_user_id)
        await state.update_data(ban_action="ban")
        await cb.answer()

    @dp.callback_query(F.data == "unban_by_id")
    async def unban_by_id_handler(cb: types.CallbackQuery, state: FSMContext):
        if not is_admin(cb.from_user.id):
            await cb.answer("❌ Доступ запрещён", show_alert=True)
            return
        await cb.message.answer(
            "✅ <b>Разбан пользователя</b>\n\nВведи <b>Telegram ID</b>:",
            parse_mode="HTML"
        )
        await state.set_state(AdminStates.ban_user_id)
        await state.update_data(ban_action="unban")
        await cb.answer()

    @dp.message(AdminStates.ban_user_id)
    async def process_ban_user_id(message: types.Message, state: FSMContext):
        try:
            target_id = int(message.text.strip())
        except ValueError:
            await message.answer(
                "❌ Некорректный ID. Введи только цифры, например: <code>123456789</code>",
                parse_mode="HTML"
            )
            await state.clear()
            return

        data_ctx = await state.get_data()
        action = data_ctx.get("ban_action", "ban")
        data = load_data()

        if action == "ban":
            if target_id in data.get("banned", []):
                await message.answer(f"ℹ️ Пользователь <code>{target_id}</code> уже заблокирован.", parse_mode="HTML")
            else:
                data.setdefault("banned", []).append(target_id)
                save_data(data)
                await message.answer(f"🚫 Пользователь <code>{target_id}</code> заблокирован.", parse_mode="HTML")
                add_log("INFO", f"Забанен ID: {target_id}", message.from_user.id)
        else:
            if target_id not in data.get("banned", []):
                await message.answer(f"ℹ️ Пользователь <code>{target_id}</code> не в бане.", parse_mode="HTML")
            else:
                data["banned"].remove(target_id)
                save_data(data)
                await message.answer(f"✅ Пользователь <code>{target_id}</code> разблокирован.", parse_mode="HTML")
                add_log("INFO", f"Разбанен ID: {target_id}", message.from_user.id)

        await state.clear()

    # ===== РАССЫЛКА =====

    @dp.message(F.text == "📨 Рассылка")
    async def show_broadcast(message: types.Message, state: FSMContext):
        if not is_admin(message.from_user.id):
            await message.answer("❌ Доступ запрещён")
            return
        await message.answer("📝 Введи текст для рассылки всем пользователям:")
        await state.set_state(AdminStates.broadcast_text)

    @dp.message(AdminStates.broadcast_text)
    async def process_broadcast(message: types.Message, state: FSMContext):
        if not bot:
            return
        data = load_data()
        users = [int(u) for u in data["users"] if int(u) not in data.get("banned", [])]
        sent = 0
        errors = 0
        for uid in users:
            try:
                await bot.send_message(uid, message.text, parse_mode="HTML")
                sent += 1
                await asyncio.sleep(0.05)
            except Exception as e:
                errors += 1
                add_log("ERROR", f"Ошибка рассылки → ID {uid}: {e}")
        await state.clear()
        await message.answer(f"✅ Рассылка завершена!\n📤 Отправлено: {sent}\n❌ Ошибок: {errors}")
        add_log("INFO", f"Рассылка: {sent} отправлено, {errors} ошибок")

    # ===== СТАТИСТИКА =====

    @dp.message(F.text == "📊 Статистика")
    async def show_stats(message: types.Message):
        if not is_admin(message.from_user.id):
            await message.answer("❌ Доступ запрещён")
            return
        data = load_data()
        total = len(data["users"])
        subscribed = sum(1 for u in data["users"].values() if u.get("subscribed"))
        in_channel = sum(1 for u in data["users"].values() if u.get("in_channel"))
        banned = len(data.get("banned", []))
        text = (
            f"📊 <b>СТАТИСТИКА</b>\n\n"
            f"👥 <b>Всего пользователей:</b> {total}\n"
            f"✅ <b>Подписаны:</b> {subscribed}\n"
            f"📍 <b>В канале:</b> {in_channel}\n"
            f"🚫 <b>Заблокированы:</b> {banned}"
        )
        await message.answer(text, parse_mode="HTML")

    @dp.message(F.text == "📋 Логи")
    async def show_logs(message: types.Message):
        if not is_admin(message.from_user.id):
            await message.answer("❌ Доступ запрещён")
            return
        logs = _logs[-20:]
        text = "📋 <b>ПОСЛЕДНИЕ ЛОГИ:</b>\n\n"
        for log in logs:
            text += f"[{log['level']}] {log['time']} - {log['message']}\n"
        await message.answer(text, parse_mode="HTML")


async def start_bot_polling():
    global bot, dp

    retry_count = 0
    max_retries = 10

    while True:
        try:
            if bot is None:
                bot = Bot(token=BOT_TOKEN)
                dp = Dispatcher(storage=MemoryStorage())
                await setup_handlers(dp)
                add_log("INFO", "🌸 Бот работает!")

            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

        except asyncio.CancelledError:
            break

        except TelegramAPIError as e:
            retry_count += 1
            wait_time = min(2 ** retry_count, 60)
            add_log("WARN", f"Ошибка API (попытка {retry_count}): {e}")
            await asyncio.sleep(wait_time)

        except Exception as e:
            retry_count += 1
            wait_time = min(2 ** retry_count, 60)
            add_log("ERROR", f"Ошибка polling (попытка {retry_count}): {e}")
            await asyncio.sleep(wait_time)

            if retry_count >= max_retries:
                add_log("CRITICAL", "Максимум попыток исчерпано")
                break


if __name__ == "__main__":
    try:
        asyncio.run(start_bot_polling())
    except KeyboardInterrupt:
        add_log("INFO", "Бот остановлен")
    except Exception as e:
        add_log("CRITICAL", f"Ошибка: {e}")