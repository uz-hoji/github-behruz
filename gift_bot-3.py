"""
Telegram Gift Bot (aiogram 3.x)
O'rnatish:  pip install aiogram
Ishga tushirish:  python gift_bot.py
"""
import logging
import os
import re
import sqlite3

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from aiohttp import web

# ===================== SOZLAMALAR =====================
# Render'da bu qiymatlarni Environment Variables orqali berish tavsiya etiladi
# (Dashboard → Environment). Agar shu yerda berilsa ham ishlayveradi.
BOT_API = os.environ.get("BOT_API", "8917898454:AAGq0iXi3z3XM6Z0thymQLa1clc7WS4xlMI")
ADMIN_IDS = [int(x) for x in os.environ.get("ADMIN_IDS", "8742802365").split(",")]
CARD_NUMBER = "0000 0000 0000 0000"       # Karta raqami
CARD_OWNER = "ISM FAMILIYA"               # Karta egasi
MIN_TOPUP = 1000                          # Minimal to'ldirish summasi (UZS)
DB_FILE = "gifts.db"
# Majburiy obuna kanallari endi kod ichida emas, /admin paneli orqali
# bazaga qo'shiladi/o'chiriladi (pastdagi "MAJBURIY OBUNA" bo'limiga qarang).

# ---- Webhook sozlamalari (Render uchun) ----
WEBHOOK_PATH = "/webhook"
BASE_WEBHOOK_URL = os.environ.get("RENDER_EXTERNAL_URL", "")  # Render avtomatik beradi
PORT = int(os.environ.get("PORT", 8080))
# ======================================================

# Bo'limlar: stars -> [(emoji, nomi), ...]  (narxlar endi bazada, admin panel orqali o'zgaradi)
CATEGORIES = {
    15: [("🧸", "Ayiq"), ("❤️", "Yurak")],
    25: [("🎁", "Sovg'a quti"), ("🌹", "Atirgul")],
    50: [("🎂", "Tort"), ("🚀", "Raketa"), ("💐", "Gul")],
    100: [("💍", "Uzuk"), ("🏆", "Kubok")],
}
DEFAULT_PRICES = {15: 2500, 25: 5000, 50: 10000, 100: 20000}  # boshlang'ich narxlar

router = Router()


# ===================== BAZA =====================
def db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT, balance INTEGER DEFAULT 0)")
        c.execute(
            "CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, "
            "gift TEXT, stars INTEGER, price INTEGER, target TEXT, status TEXT DEFAULT 'new')"
        )
        c.execute(
            "CREATE TABLE IF NOT EXISTS topups(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, "
            "amount INTEGER, status TEXT DEFAULT 'pending')"
        )
        c.execute(
            "CREATE TABLE IF NOT EXISTS sub_channels(id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "chat_id TEXT UNIQUE, url TEXT, title TEXT)"
        )
        c.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS category_prices(stars INTEGER PRIMARY KEY, price INTEGER)")
        c.execute("CREATE TABLE IF NOT EXISTS admins(id INTEGER PRIMARY KEY)")
        for admin_id in ADMIN_IDS:
            c.execute("INSERT OR IGNORE INTO admins(id) VALUES(?)", (admin_id,))


def ensure_user(user_id: int, name: str):
    with db() as c:
        c.execute("INSERT OR IGNORE INTO users(id, name) VALUES(?, ?)", (user_id, name))


def get_balance(user_id: int) -> int:
    with db() as c:
        row = c.execute("SELECT balance FROM users WHERE id=?", (user_id,)).fetchone()
    return row["balance"] if row else 0


def add_balance(user_id: int, amount: int):
    with db() as c:
        c.execute("UPDATE users SET balance = balance + ? WHERE id=?", (amount, user_id))


def fmt(n: int) -> str:
    return f"{n:,}".replace(",", " ") + " UZS"


# ===================== SOZLAMALAR (BAZADA) =====================
def get_setting(key: str, default: str = "") -> str:
    with db() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?,?)", (key, value))


def get_card_number() -> str:
    return get_setting("card_number", CARD_NUMBER)


def get_card_owner() -> str:
    return get_setting("card_owner", CARD_OWNER)


def get_price(stars: int) -> int:
    with db() as c:
        row = c.execute("SELECT price FROM category_prices WHERE stars=?", (stars,)).fetchone()
    return row["price"] if row else DEFAULT_PRICES[stars]


def set_price(stars: int, price: int):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO category_prices(stars, price) VALUES(?,?)", (stars, price))


# ===================== ADMINLAR (BAZADA) =====================
def is_admin(user_id: int) -> bool:
    with db() as c:
        row = c.execute("SELECT 1 FROM admins WHERE id=?", (user_id,)).fetchone()
    return row is not None


def list_admins() -> list:
    with db() as c:
        rows = c.execute("SELECT id FROM admins ORDER BY id").fetchall()
    return [r["id"] for r in rows]


def add_admin(user_id: int):
    with db() as c:
        c.execute("INSERT OR IGNORE INTO admins(id) VALUES(?)", (user_id,))


def remove_admin(user_id: int):
    with db() as c:
        c.execute("DELETE FROM admins WHERE id=?", (user_id,))


# ===================== MAJBURIY OBUNA =====================
def get_sub_channels() -> list:
    with db() as c:
        rows = c.execute("SELECT * FROM sub_channels ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def add_sub_channel(chat_id: str, url: str, title: str):
    with db() as c:
        c.execute(
            "INSERT OR REPLACE INTO sub_channels(chat_id, url, title) VALUES(?,?,?)",
            (chat_id, url, title),
        )


def remove_sub_channel(channel_db_id: int):
    with db() as c:
        c.execute("DELETE FROM sub_channels WHERE id=?", (channel_db_id,))


async def get_not_subscribed(bot: Bot, user_id: int) -> list:
    """Foydalanuvchi obuna bo'lmagan kanallar ro'yxatini qaytaradi."""
    channels = get_sub_channels()
    not_subbed = []
    for ch in channels:
        try:
            member = await bot.get_chat_member(ch["chat_id"], user_id)
            if member.status in ("left", "kicked"):
                not_subbed.append(ch)
        except Exception as e:
            # Bot kanalda admin bo'lmasa yoki chat_id noto'g'ri bo'lsa shu yerga tushadi
            logging.warning("Obuna tekshirilmadi (%s): %s", ch["chat_id"], e)
            not_subbed.append(ch)
    return not_subbed


def sub_kb(not_subbed: list) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"➕ {ch['title']}", url=ch["url"])] for ch in not_subbed]
    rows.append([InlineKeyboardButton(text="✅ Men obuna bo'ldim", callback_data="check_sub")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def require_subscription(bot: Bot, user_id: int, message: Message) -> bool:
    """True qaytarsa — hammasiga obuna. False bo'lsa — obuna so'rovini yuboradi."""
    not_subbed = await get_not_subscribed(bot, user_id)
    if not_subbed:
        await message.answer(
            "⚠️ Botdan foydalanish uchun quyidagi kanal(lar)ga obuna bo'ling, "
            "so'ng <b>✅ Men obuna bo'ldim</b> tugmasini bosing:",
            parse_mode="HTML",
            reply_markup=sub_kb(not_subbed),
        )
        return False
    return True


# ===================== ADMIN PANEL: MAJBURIY OBUNA =====================
class AddChannel(StatesGroup):
    chat_id = State()
    url = State()
    title = State()


def admin_sub_kb() -> InlineKeyboardMarkup:
    channels = get_sub_channels()
    rows = [
        [InlineKeyboardButton(text=f"❌ {ch['title']} ({ch['chat_id']})", callback_data=f"subdel:{ch['id']}")]
        for ch in channels
    ]
    rows.append([InlineKeyboardButton(text="➕ Kanal qo'shish", callback_data="subadd")])
    rows.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="adm:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ===================== ADMIN PANEL: BOSH MENYU =====================
def admin_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📢 Majburiy obuna", callback_data="adm:sub")],
            [InlineKeyboardButton(text="💳 Karta ma'lumotlari", callback_data="adm:card")],
            [InlineKeyboardButton(text="💰 Gift narxlari", callback_data="adm:price")],
            [InlineKeyboardButton(text="👤 Adminlar", callback_data="adm:admins")],
        ]
    )


@router.message(Command("admin"))
async def admin_panel(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    await state.clear()
    await m.answer("🛠 <b>Admin panel</b>\n\nBo'limni tanlang:", parse_mode="HTML", reply_markup=admin_main_kb())


@router.callback_query(F.data == "adm:home")
async def adm_home(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        await c.answer("Ruxsat yo'q", show_alert=True)
        return
    await state.clear()
    await c.message.edit_text("🛠 <b>Admin panel</b>\n\nBo'limni tanlang:", parse_mode="HTML", reply_markup=admin_main_kb())
    await c.answer()


@router.callback_query(F.data == "adm:sub")
async def adm_sub(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Ruxsat yo'q", show_alert=True)
        return
    await c.message.edit_text(
        "📢 <b>Majburiy obuna kanallari</b>\n\n"
        "➕ tugmasi orqali kanal qo'shing yoki ❌ bosib o'chiring.\n"
        "Bot qo'shilayotgan kanalda <b>admin</b> bo'lishi shart!",
        parse_mode="HTML",
        reply_markup=admin_sub_kb(),
    )
    await c.answer()


@router.callback_query(F.data == "subadd")
async def sub_add_start(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        await c.answer("Ruxsat yo'q", show_alert=True)
        return
    await state.set_state(AddChannel.chat_id)
    await c.message.answer(
        "Kanal <b>chat_id</b> yoki <b>@username</b>ini yuboring.\n"
        "Masalan: <code>@mychannel</code> yoki <code>-1001234567890</code>\n\n"
        "❗️Eslatma: bot shu kanalda admin bo'lishi shart.",
        parse_mode="HTML",
        reply_markup=cancel_kb,
    )
    await c.answer()


@router.message(AddChannel.chat_id)
async def sub_add_chatid(m: Message, state: FSMContext):
    await state.update_data(chat_id=m.text.strip())
    await state.set_state(AddChannel.url)
    await m.answer("Kanalga qo'shilish uchun havola (URL) yuboring:\nMasalan: <code>https://t.me/mychannel</code>", parse_mode="HTML")


@router.message(AddChannel.url)
async def sub_add_url(m: Message, state: FSMContext):
    await state.update_data(url=m.text.strip())
    await state.set_state(AddChannel.title)
    await m.answer("Kanal nomini yuboring (foydalanuvchiga ko'rinadigan nom):")


@router.message(AddChannel.title)
async def sub_add_title(m: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await state.clear()
    chat_id, url, title = data["chat_id"], data["url"], m.text.strip()

    # Bot shu kanalda ishlashini tekshirib ko'ramiz
    try:
        await bot.get_chat(chat_id)
    except Exception as e:
        await m.answer(
            f"⚠️ Ogohlantirish: bot bu kanalga ({chat_id}) murojaat qila olmadi.\n"
            f"Sabab: {e}\n\n"
            f"Bot kanalga <b>admin</b> qilib qo'shilganini tekshiring. "
            f"Baribir bazaga saqlandi.",
            parse_mode="HTML",
        )

    add_sub_channel(chat_id, url, title)
    await m.answer(f"✅ Kanal qo'shildi: <b>{title}</b> ({chat_id})", parse_mode="HTML", reply_markup=admin_sub_kb())


@router.callback_query(F.data.startswith("subdel:"))
async def sub_delete(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Ruxsat yo'q", show_alert=True)
        return
    channel_id = int(c.data.split(":")[1])
    remove_sub_channel(channel_id)
    await c.message.edit_reply_markup(reply_markup=admin_sub_kb())
    await c.answer("O'chirildi ✅")


# ===================== ADMIN PANEL: KARTA MA'LUMOTLARI =====================
class EditCard(StatesGroup):
    number = State()
    owner = State()


def card_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Karta raqamini o'zgartirish", callback_data="card:number")],
            [InlineKeyboardButton(text="✏️ Karta egasini o'zgartirish", callback_data="card:owner")],
            [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="adm:home")],
        ]
    )


@router.callback_query(F.data == "adm:card")
async def adm_card(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Ruxsat yo'q", show_alert=True)
        return
    await c.message.edit_text(
        f"💳 <b>Joriy karta ma'lumotlari</b>\n\n"
        f"Raqam: <code>{get_card_number()}</code>\n"
        f"Egasi: <b>{get_card_owner()}</b>",
        parse_mode="HTML",
        reply_markup=card_kb(),
    )
    await c.answer()


@router.callback_query(F.data == "card:number")
async def card_number_start(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        await c.answer("Ruxsat yo'q", show_alert=True)
        return
    await state.set_state(EditCard.number)
    await c.message.answer("Yangi karta raqamini yuboring:\nMasalan: <code>8600 1234 5678 9012</code>", parse_mode="HTML", reply_markup=cancel_kb)
    await c.answer()


@router.message(EditCard.number)
async def card_number_save(m: Message, state: FSMContext):
    await state.clear()
    set_setting("card_number", m.text.strip())
    await m.answer("✅ Karta raqami yangilandi.", reply_markup=card_kb())


@router.callback_query(F.data == "card:owner")
async def card_owner_start(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        await c.answer("Ruxsat yo'q", show_alert=True)
        return
    await state.set_state(EditCard.owner)
    await c.message.answer("Karta egasining yangi ism-familiyasini yuboring:", reply_markup=cancel_kb)
    await c.answer()


@router.message(EditCard.owner)
async def card_owner_save(m: Message, state: FSMContext):
    await state.clear()
    set_setting("card_owner", m.text.strip())
    await m.answer("✅ Karta egasi yangilandi.", reply_markup=card_kb())


# ===================== ADMIN PANEL: GIFT NARXLARI =====================
class EditPrice(StatesGroup):
    value = State()


def price_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"⭐ {stars} starslik — {fmt(get_price(stars))}", callback_data=f"price:{stars}")]
        for stars in CATEGORIES
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="adm:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "adm:price")
async def adm_price(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Ruxsat yo'q", show_alert=True)
        return
    await c.message.edit_text(
        "💰 <b>Gift narxlari</b>\n\nNarxini o'zgartirmoqchi bo'lgan bo'limni tanlang:",
        parse_mode="HTML",
        reply_markup=price_kb(),
    )
    await c.answer()


@router.callback_query(F.data.startswith("price:"))
async def price_edit_start(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        await c.answer("Ruxsat yo'q", show_alert=True)
        return
    stars = int(c.data.split(":")[1])
    await state.set_state(EditPrice.value)
    await state.update_data(stars=stars)
    await c.message.answer(
        f"⭐ <b>{stars} starslik</b> bo'lim uchun yangi narxni kiriting (so'mda, faqat raqam):\n"
        f"Joriy narx: <b>{fmt(get_price(stars))}</b>",
        parse_mode="HTML",
        reply_markup=cancel_kb,
    )
    await c.answer()


@router.message(EditPrice.value)
async def price_edit_save(m: Message, state: FSMContext):
    data = await state.get_data()
    digits = re.sub(r"\D", "", m.text or "")
    if not digits:
        await m.answer("⚠️ Iltimos, faqat raqam kiriting. Masalan: <code>15000</code>", parse_mode="HTML")
        return
    stars = data["stars"]
    set_price(stars, int(digits))
    await state.clear()
    await m.answer(
        f"✅ ⭐ {stars} starslik bo'lim narxi <b>{fmt(int(digits))}</b> ga o'zgartirildi.",
        parse_mode="HTML",
        reply_markup=price_kb(),
    )


# ===================== ADMIN PANEL: ADMINLAR =====================
class AddAdmin(StatesGroup):
    user_id = State()


def admins_kb() -> InlineKeyboardMarkup:
    admins = list_admins()
    rows = []
    for aid in admins:
        # oxirgi bitta adminni o'chirib qo'yib qolmaslik uchun himoya
        if len(admins) > 1:
            rows.append([InlineKeyboardButton(text=f"❌ {aid}", callback_data=f"admdel:{aid}")])
        else:
            rows.append([InlineKeyboardButton(text=f"🔒 {aid} (yagona admin)", callback_data="noop")])
    rows.append([InlineKeyboardButton(text="➕ Admin qo'shish", callback_data="admadd")])
    rows.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="adm:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "adm:admins")
async def adm_admins(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Ruxsat yo'q", show_alert=True)
        return
    await c.message.edit_text(
        "👤 <b>Adminlar ro'yxati</b>\n\n"
        "➕ tugmasi orqali yangi admin qo'shing (uning Telegram ID'sini kiritasiz) "
        "yoki ❌ bosib olib tashlang.",
        parse_mode="HTML",
        reply_markup=admins_kb(),
    )
    await c.answer()


@router.callback_query(F.data == "noop")
async def noop(c: CallbackQuery):
    await c.answer()


@router.callback_query(F.data == "admadd")
async def admin_add_start(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        await c.answer("Ruxsat yo'q", show_alert=True)
        return
    await state.set_state(AddAdmin.user_id)
    await c.message.answer(
        "Yangi adminning Telegram ID'sini yuboring (faqat raqam).\n\n"
        "ID'ni bilish uchun o'sha kishi botga /start yozsin, keyin @userinfobot "
        "kabi botdan o'z ID'sini olsin.",
        reply_markup=cancel_kb,
    )
    await c.answer()


@router.message(AddAdmin.user_id)
async def admin_add_save(m: Message, state: FSMContext, bot: Bot):
    digits = re.sub(r"\D", "", m.text or "")
    if not digits:
        await m.answer("⚠️ Iltimos, faqat raqamli ID yuboring.")
        return
    new_id = int(digits)
    await state.clear()
    add_admin(new_id)
    await m.answer(f"✅ <code>{new_id}</code> admin sifatida qo'shildi.", parse_mode="HTML", reply_markup=admins_kb())
    try:
        await bot.send_message(new_id, "🎉 Siz botga admin qilib tayinlandingiz! /admin buyrug'i orqali panelni oching.")
    except Exception:
        pass  # foydalanuvchi botga hali /start bosmagan bo'lishi mumkin


@router.callback_query(F.data.startswith("admdel:"))
async def admin_delete(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Ruxsat yo'q", show_alert=True)
        return
    target_id = int(c.data.split(":")[1])
    if len(list_admins()) <= 1:
        await c.answer("❌ Yagona adminni o'chirib bo'lmaydi!", show_alert=True)
        return
    remove_admin(target_id)
    await c.message.edit_reply_markup(reply_markup=admins_kb())
    await c.answer("O'chirildi ✅")


# ===================== HOLATLAR =====================
class BuyGift(StatesGroup):
    username = State()
    confirm = State()


class TopUp(StatesGroup):
    amount = State()
    receipt = State()


# ===================== TUGMALAR =====================
BTN_GIFT = "🎁 Gift olish"
BTN_TOPUP = "💰 Hisob to'ldirish"
BTN_BALANCE = "👤 Hisobim"
BTN_ADMIN = "🛠 Admin panel"


def get_main_kb(user_id: int) -> ReplyKeyboardMarkup:
    rows = []
    if is_admin(user_id):
        rows.append([KeyboardButton(text=BTN_ADMIN)])  # admin panel eng tepada, Gift/Hisobdan oldin
    rows.append([KeyboardButton(text=BTN_GIFT)])
    rows.append([KeyboardButton(text=BTN_TOPUP), KeyboardButton(text=BTN_BALANCE)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def categories_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"⭐ {stars} starslik — {fmt(get_price(stars))}", callback_data=f"cat:{stars}")]
        for stars in CATEGORIES
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def gifts_kb(stars: int) -> InlineKeyboardMarkup:
    price = get_price(stars)
    gifts = CATEGORIES[stars]
    rows = [
        [InlineKeyboardButton(text=f"{emoji} {name} — {fmt(price)}", callback_data=f"gift:{stars}:{i}")]
        for i, (emoji, name) in enumerate(gifts)
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="back:cats")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


topup_kb = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="💰 Hisob to'ldirish", callback_data="topup")]]
)
cancel_kb = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel")]]
)


# ===================== START / ASOSIY MENYU =====================
@router.message(CommandStart())
async def start(m: Message, state: FSMContext, bot: Bot):
    await state.clear()
    ensure_user(m.from_user.id, m.from_user.full_name)
    if not await require_subscription(bot, m.from_user.id, m):
        return
    await m.answer(
        f"Salom, {m.from_user.full_name}! 👋\nGift botiga xush kelibsiz.",
        reply_markup=get_main_kb(m.from_user.id),
    )


@router.callback_query(F.data == "check_sub")
async def check_sub(c: CallbackQuery, bot: Bot):
    not_subbed = await get_not_subscribed(bot, c.from_user.id)
    if not_subbed:
        await c.answer("❌ Siz hali barcha kanallarga obuna bo'lmagansiz!", show_alert=True)
        await c.message.edit_reply_markup(reply_markup=sub_kb(not_subbed))
        return
    ensure_user(c.from_user.id, c.from_user.full_name)
    await c.message.edit_text("✅ Obuna tasdiqlandi! Endi botdan foydalanishingiz mumkin.")
    await c.message.answer(
        f"Salom, {c.from_user.full_name}! 👋\nGift botiga xush kelibsiz.",
        reply_markup=get_main_kb(c.from_user.id),
    )
    await c.answer()


@router.message(F.text == BTN_ADMIN)
async def admin_btn(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    await state.clear()
    await m.answer("🛠 <b>Admin panel</b>\n\nBo'limni tanlang:", parse_mode="HTML", reply_markup=admin_main_kb())


@router.message(F.text == BTN_BALANCE)
async def my_balance(m: Message, state: FSMContext, bot: Bot):
    await state.clear()
    ensure_user(m.from_user.id, m.from_user.full_name)
    if not await require_subscription(bot, m.from_user.id, m):
        return
    await m.answer(
        f"👤 ID: <code>{m.from_user.id}</code>\n💳 Balans: <b>{fmt(get_balance(m.from_user.id))}</b>",
        parse_mode="HTML",
        reply_markup=topup_kb,
    )


@router.message(F.text == BTN_GIFT)
async def gift_menu(m: Message, state: FSMContext, bot: Bot):
    await state.clear()
    ensure_user(m.from_user.id, m.from_user.full_name)
    if not await require_subscription(bot, m.from_user.id, m):
        return
    await m.answer("Bo'limni tanlang:", reply_markup=categories_kb())


@router.callback_query(F.data == "back:cats")
async def back_cats(c: CallbackQuery):
    await c.message.edit_text("Bo'limni tanlang:", reply_markup=categories_kb())
    await c.answer()


@router.callback_query(F.data == "cancel")
async def cancel(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text("❌ Bekor qilindi.")
    await c.answer()


# ===================== GIFT OLISH =====================
@router.callback_query(F.data.startswith("cat:"))
async def open_category(c: CallbackQuery):
    stars = int(c.data.split(":")[1])
    price = get_price(stars)
    await c.message.edit_text(
        f"⭐ <b>{stars} starslik</b> bo'lim\nNarxi: <b>{fmt(price)}</b>\n\nGiftni tanlang:",
        parse_mode="HTML",
        reply_markup=gifts_kb(stars),
    )
    await c.answer()


@router.callback_query(F.data.startswith("gift:"))
async def choose_gift(c: CallbackQuery, state: FSMContext):
    _, stars, idx = c.data.split(":")
    stars, idx = int(stars), int(idx)
    price = get_price(stars)
    gifts = CATEGORIES[stars]
    emoji, name = gifts[idx]
    balance = get_balance(c.from_user.id)

    # Mablag' yetarli emas
    if balance < price:
        await c.message.answer(
            f"❌ Hisobingizda mablag' yetarli emas.\n\n"
            f"Gift narxi: <b>{fmt(price)}</b>\n"
            f"Sizning balansingiz: <b>{fmt(balance)}</b>\n\n"
            f"Iltimos, <b>hisob to'ldiring</b> 👇",
            parse_mode="HTML",
            reply_markup=topup_kb,
        )
        await c.answer()
        return

    await state.set_state(BuyGift.username)
    await state.update_data(stars=stars, gift=f"{emoji} {name}", price=price)
    await c.message.answer(
        f"{emoji} <b>{name}</b> ({stars} ⭐) tanlandi.\n\n"
        f"Gift yuboriladigan odamning <b>@username</b>ini yuboring:",
        parse_mode="HTML",
        reply_markup=cancel_kb,
    )
    await c.answer()


@router.message(BuyGift.username)
async def got_username(m: Message, state: FSMContext):
    text = (m.text or "").strip()
    if not re.fullmatch(r"@?[A-Za-z0-9_]{5,32}", text):
        await m.answer("⚠️ Noto'g'ri username. Masalan: <code>@username</code>", parse_mode="HTML")
        return
    username = text if text.startswith("@") else "@" + text
    await state.update_data(target=username)
    data = await state.get_data()
    await state.set_state(BuyGift.confirm)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Tasdiqlash", callback_data="buy:yes"),
                InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel"),
            ]
        ]
    )
    await m.answer(
        f"🎁 Gift: <b>{data['gift']}</b> ({data['stars']} ⭐)\n"
        f"👤 Kimga: <b>{username}</b>\n"
        f"💵 Narxi: <b>{fmt(data['price'])}</b>\n\nTasdiqlaysizmi?",
        parse_mode="HTML",
        reply_markup=kb,
    )


@router.callback_query(BuyGift.confirm, F.data == "buy:yes")
async def confirm_buy(c: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await state.clear()
    price = data["price"]

    with db() as conn:
        row = conn.execute("SELECT balance FROM users WHERE id=?", (c.from_user.id,)).fetchone()
        if not row or row["balance"] < price:
            await c.message.edit_text("❌ Hisobingizda mablag' yetarli emas. Hisobni to'ldiring.", reply_markup=topup_kb)
            await c.answer()
            return
        conn.execute("UPDATE users SET balance = balance - ? WHERE id=?", (price, c.from_user.id))
        cur = conn.execute(
            "INSERT INTO orders(user_id, gift, stars, price, target) VALUES(?,?,?,?,?)",
            (c.from_user.id, data["gift"], data["stars"], price, data["target"]),
        )
        order_id = cur.lastrowid

    await c.message.edit_text(
        f"✅ Buyurtma #{order_id} qabul qilindi!\n"
        f"🎁 {data['gift']} → {data['target']}\n"
        f"💳 Qolgan balans: {fmt(get_balance(c.from_user.id))}\n\n"
        f"Admin tez orada giftni yuboradi."
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Yuborildi", callback_data=f"ord_ok:{order_id}"),
                InlineKeyboardButton(text="↩️ Bekor + qaytarish", callback_data=f"ord_no:{order_id}"),
            ]
        ]
    )
    text = (
        f"🛒 <b>Yangi buyurtma #{order_id}</b>\n"
        f"👤 Xaridor: {c.from_user.full_name} (<code>{c.from_user.id}</code>)\n"
        f"🎁 Gift: {data['gift']} ({data['stars']} ⭐)\n"
        f"📨 Kimga: {data['target']}\n"
        f"💵 Narxi: {fmt(price)}"
    )
    for admin in list_admins():
        try:
            await bot.send_message(admin, text, parse_mode="HTML", reply_markup=kb)
        except Exception as e:
            logging.warning("Adminga yuborilmadi %s: %s", admin, e)
    await c.answer()


@router.callback_query(F.data.startswith("ord_"))
async def order_action(c: CallbackQuery, bot: Bot):
    if not is_admin(c.from_user.id):
        await c.answer("Ruxsat yo'q", show_alert=True)
        return
    action, oid = c.data.split(":")
    oid = int(oid)
    with db() as conn:
        o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if not o or o["status"] != "new":
            await c.answer("Bu buyurtma allaqachon ko'rib chiqilgan", show_alert=True)
            return
        if action == "ord_ok":
            conn.execute("UPDATE orders SET status='done' WHERE id=?", (oid,))
        else:
            conn.execute("UPDATE orders SET status='canceled' WHERE id=?", (oid,))
            conn.execute("UPDATE users SET balance = balance + ? WHERE id=?", (o["price"], o["user_id"]))

    if action == "ord_ok":
        await c.message.edit_text(c.message.html_text + "\n\n✅ <b>Yuborildi</b>", parse_mode="HTML")
        await bot.send_message(o["user_id"], f"🎉 Buyurtma #{oid} bajarildi! {o['gift']} → {o['target']} yuborildi.")
    else:
        await c.message.edit_text(c.message.html_text + "\n\n↩️ <b>Bekor qilindi, pul qaytarildi</b>", parse_mode="HTML")
        await bot.send_message(
            o["user_id"], f"❌ Buyurtma #{oid} bekor qilindi. {fmt(o['price'])} balansingizga qaytarildi."
        )
    await c.answer()


# ===================== HISOB TO'LDIRISH =====================
@router.message(F.text == BTN_TOPUP)
async def topup_msg(m: Message, state: FSMContext, bot: Bot):
    if not await require_subscription(bot, m.from_user.id, m):
        return
    await start_topup(m, state)


@router.callback_query(F.data == "topup")
async def topup_cb(c: CallbackQuery, state: FSMContext, bot: Bot):
    if not await require_subscription(bot, c.from_user.id, c.message):
        await c.answer()
        return
    await start_topup(c.message, state)
    await c.answer()


async def start_topup(m: Message, state: FSMContext):
    await state.set_state(TopUp.amount)
    await m.answer(
        f"💰 Necha so'm to'ldirmoqchisiz?\nMinimal: {fmt(MIN_TOPUP)}\n\nSummani raqamda yuboring (masalan: <code>10000</code>):",
        parse_mode="HTML",
        reply_markup=cancel_kb,
    )


@router.message(TopUp.amount)
async def topup_amount(m: Message, state: FSMContext):
    digits = re.sub(r"\D", "", m.text or "")
    if not digits or int(digits) < MIN_TOPUP:
        await m.answer(f"⚠️ Iltimos, {fmt(MIN_TOPUP)} dan kam bo'lmagan summa kiriting.")
        return
    amount = int(digits)
    await state.update_data(amount=amount)
    await state.set_state(TopUp.receipt)
    await m.answer(
        f"💳 Quyidagi kartaga <b>{fmt(amount)}</b> o'tkazing:\n\n"
        f"Karta: <code>{get_card_number()}</code>\n"
        f"Egasi: <b>{get_card_owner()}</b>\n\n"
        f"To'lovdan so'ng <b>chek (skrinshot)</b>ni rasm qilib yuboring 📸",
        parse_mode="HTML",
        reply_markup=cancel_kb,
    )


@router.message(TopUp.receipt, F.photo)
async def topup_receipt(m: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await state.clear()
    with db() as conn:
        cur = conn.execute("INSERT INTO topups(user_id, amount) VALUES(?,?)", (m.from_user.id, data["amount"]))
        tid = cur.lastrowid

    await m.answer("⏳ Chek adminga yuborildi. Tasdiqlangach balansingiz to'ldiriladi.")
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"top_ok:{tid}"),
                InlineKeyboardButton(text="❌ Rad etish", callback_data=f"top_no:{tid}"),
            ]
        ]
    )
    caption = (
        f"💰 <b>To'ldirish #{tid}</b>\n"
        f"👤 {m.from_user.full_name} (<code>{m.from_user.id}</code>)\n"
        f"💵 Summa: <b>{fmt(data['amount'])}</b>"
    )
    for admin in list_admins():
        try:
            await bot.send_photo(admin, m.photo[-1].file_id, caption=caption, parse_mode="HTML", reply_markup=kb)
        except Exception as e:
            logging.warning("Adminga yuborilmadi %s: %s", admin, e)


@router.message(TopUp.receipt)
async def topup_need_photo(m: Message):
    await m.answer("📸 Iltimos, chekni rasm (skrinshot) ko'rinishida yuboring.")


@router.callback_query(F.data.startswith("top_"))
async def topup_action(c: CallbackQuery, bot: Bot):
    if not is_admin(c.from_user.id):
        await c.answer("Ruxsat yo'q", show_alert=True)
        return
    action, tid = c.data.split(":")
    tid = int(tid)
    with db() as conn:
        t = conn.execute("SELECT * FROM topups WHERE id=?", (tid,)).fetchone()
        if not t or t["status"] != "pending":
            await c.answer("Bu so'rov allaqachon ko'rib chiqilgan", show_alert=True)
            return
        if action == "top_ok":
            conn.execute("UPDATE topups SET status='approved' WHERE id=?", (tid,))
            conn.execute("UPDATE users SET balance = balance + ? WHERE id=?", (t["amount"], t["user_id"]))
        else:
            conn.execute("UPDATE topups SET status='rejected' WHERE id=?", (tid,))

    if action == "top_ok":
        await c.message.edit_caption(caption=c.message.html_text + "\n\n✅ <b>Tasdiqlandi</b>", parse_mode="HTML")
        await bot.send_message(
            t["user_id"],
            f"✅ Hisobingiz {fmt(t['amount'])} ga to'ldirildi!\n💳 Balans: {fmt(get_balance(t['user_id']))}",
        )
    else:
        await c.message.edit_caption(caption=c.message.html_text + "\n\n❌ <b>Rad etildi</b>", parse_mode="HTML")
        await bot.send_message(t["user_id"], f"❌ To'ldirish #{tid} rad etildi. Muammo bo'lsa admin bilan bog'laning.")
    await c.answer()


# ===================== ADMIN =====================
@router.message(Command("add"))
async def admin_add(m: Message, bot: Bot):
    """/add <user_id> <summa>  — qo'lda balans qo'shish"""
    if not is_admin(m.from_user.id):
        return
    parts = (m.text or "").split()
    if len(parts) != 3 or not parts[1].lstrip("-").isdigit() or not parts[2].lstrip("-").isdigit():
        await m.answer("Format: /add <user_id> <summa>")
        return
    uid, amount = int(parts[1]), int(parts[2])
    ensure_user(uid, "")
    add_balance(uid, amount)
    await m.answer(f"✅ {uid} balansi {fmt(amount)} ga o'zgardi. Yangi balans: {fmt(get_balance(uid))}")
    try:
        await bot.send_message(uid, f"💰 Balansingiz {fmt(amount)} ga o'zgartirildi.")
    except Exception:
        pass


@router.message(Command("stats"))
async def admin_stats(m: Message):
    if not is_admin(m.from_user.id):
        return
    with db() as c:
        users = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        done = c.execute("SELECT COUNT(*) n, COALESCE(SUM(price),0) s FROM orders WHERE status='done'").fetchone()
        new = c.execute("SELECT COUNT(*) n FROM orders WHERE status='new'").fetchone()["n"]
    await m.answer(
        f"📊 Foydalanuvchilar: {users}\n"
        f"🕐 Kutilayotgan buyurtmalar: {new}\n"
        f"✅ Bajarilgan: {done['n']} ta ({fmt(done['s'])})"
    )


# ===================== ISHGA TUSHIRISH (WEBHOOK / RENDER) =====================
async def on_startup(bot: Bot):
    if BASE_WEBHOOK_URL:
        webhook_url = BASE_WEBHOOK_URL.rstrip("/") + WEBHOOK_PATH
        await bot.set_webhook(webhook_url)
        logging.info("Webhook o'rnatildi: %s", webhook_url)
    else:
        logging.warning("RENDER_EXTERNAL_URL topilmadi — webhook o'rnatilmadi!")


def main():
    logging.basicConfig(level=logging.INFO)
    init_db()

    bot = Bot(BOT_API)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    dp.startup.register(on_startup)

    app = web.Application()
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    # Render "/" manzilga oddiy health-check so'rovi yuborishi mumkin
    async def health(request):
        return web.Response(text="Bot ishlayapti ✅")

    app.router.add_get("/", health)

    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
