# Click_Earn_tbot – Full Production Code (Aiogram 3 + FastAPI webhook)
# ---------------------------------------------------------------
# Features:
# • 4‑channel mandatory join verification
# • Referral system (bonus credited)
# • USDT balance, task marketplace (join‑channel)
# • Xrocket RocketPay API – automatic Deposit & Withdraw
# • Admin panel (inline): broadcast, set fees/bonus/min, set XR token, users count, withdraw queue
# • SQLite database (users, tasks, withdraws, settings)
# • FastAPI /webhook endpoint for RocketPay callbacks
#
# How to run (Render VPS):
# 1. requirements.txt → aiogram==3.5.1 aiosqlite aiohttp fastapi uvicorn
# 2. python bot.py  (worker)  |  uvicorn webhook:app  (web service)
# ---------------------------------------------------------------

import asyncio, logging, uuid, hmac, hashlib, aiohttp, aiosqlite, json
from fastapi import FastAPI, Request, HTTPException
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup,
                           InlineKeyboardButton)
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

# === Basic Config ============================================================
BOT_TOKEN   = "7876058142:AAG_F90xpBhsjR9ODa_DZE73TvbF1uFJZas"
ADMIN_ID    = 7390932497
CHANNELS    = ["@AnasEarnHunter","@ExpossDark","@Anas_Promotion","@givwas"]

REF_BONUS      = 0.02     # USDT
PLATFORM_FEE   = 0.20     # 20 %
MIN_CPC        = 0.005
MAX_CPC        = 0.100
MIN_DEPOSIT    = 5.0
MIN_WITHDRAW   = 5.0

XR_TOKEN       = ""        # set in admin panel
XR_SECRET      = ""        # webhook secret (optional signature)
WEBHOOK_URL    = "https://your-domain.onrender.com/xr_webhook"

DB             = "clickearn.db"
# ============================================================================

bot = Bot(BOT_TOKEN, parse_mode="HTML")
dp  = Dispatcher(storage=MemoryStorage())
app = FastAPI()
logging.basicConfig(level=logging.INFO)

# === DB Init =================================================================
async def db_init():
    async with aiosqlite.connect(DB) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,balance REAL DEFAULT 0,ref_by INTEGER);
        CREATE TABLE IF NOT EXISTS tasks(id INTEGER PRIMARY KEY AUTOINCREMENT,owner INTEGER,target TEXT,cpc REAL,reward REAL,budget INTEGER,done INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS withdraws(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,amount REAL,invoice TEXT,status TEXT);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,val TEXT);
        """)
        await db.commit()

# === Helper ==================================================================
async def joined_all(uid:int)->bool:
    for ch in CHANNELS:
        try:
            mem=await bot.get_chat_member(ch,uid)
            if mem.status in ("left","kicked"):
                return False
        except:
            return False
    return True

async def bal(uid:int)->float:
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT OR IGNORE INTO users(id) VALUES(?)", (uid,)); await db.commit()
        cur=await db.execute("SELECT balance FROM users WHERE id=?", (uid,)); row=await cur.fetchone()
        return row[0] if row else 0.0

async def set_bal(uid:int,amount:float):
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE users SET balance=? WHERE id=?", (amount,uid)); await db.commit()

# === Keyboards ===============================================================

def menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("📢 Tasks",callback_data="tasks"),
         InlineKeyboardButton("💰 Balance",callback_data="bal")],
        [InlineKeyboardButton("🙌 Referral",callback_data="ref"),
         InlineKeyboardButton("🛠 Create Task",callback_data="crt")],
        [InlineKeyboardButton("💳 Deposit",callback_data="dep"),
         InlineKeyboardButton("🏧 Withdraw",callback_data="wd")]
    ])

admin_kb = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton("Users",callback_data="a_users"),
     InlineKeyboardButton("Broadcast",callback_data="a_bc")],
    [InlineKeyboardButton("Set XR",callback_data="a_xr"),
     InlineKeyboardButton("Withdraw Queue",callback_data="a_wdq")]
])

# === /start ==================================================================
@dp.message(CommandStart())
async def start(m:Message):
    ref = int(m.text.split()[1]) if len(m.text.split())>1 and m.text.split()[1].isdigit() else None
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT OR IGNORE INTO users(id,ref_by) VALUES(?,?)", (m.from_user.id,ref)); await db.commit()
    if m.from_user.id!=ADMIN_ID:
        await bot.send_message(ADMIN_ID,f"🆕 New user {m.from_user.id} Ref={ref}")
    if ref and ref!=m.from_user.id:
        async with aiosqlite.connect(DB) as db:
            await db.execute("UPDATE users SET balance=balance+? WHERE id=?", (REF_BONUS,ref)); await db.commit()
    if not await joined_all(m.from_user.id):
        join_txt="\n".join(f"👉 {c}" for c in CHANNELS)
        return await m.answer(f"🔐 Join channels first:\n{join_txt}")
    await m.answer("✅ Verified! /menu",reply_markup=menu_kb())

@dp.message(Command("menu"))
async def menu(m:Message): await m.answer("Menu",reply_markup=menu_kb())

# === Balance / Referral ======================================================
@dp.callback_query(F.data=="bal")
async def cb_bal(c:CallbackQuery):
    await c.answer(); await c.message.edit_text(f"Balance: {await bal(c.from_user.id):.3f} USDT",reply_markup=menu_kb())

@dp.callback_query(F.data=="ref")
async def cb_ref(c:CallbackQuery):
    link=f"https://t.me/{(await bot.me()).username}?start={c.from_user.id}"
    await c.answer(); await c.message.edit_text(f"Referral:\n<code>{link}</code>",reply_markup=menu_kb())

# === Create Task FSM =========================================================
class NewTask(StatesGroup): target=State(); cpc=State(); bud=State(); conf=State()

@dp.callback_query(F.data=="crt")
async def crt1(c:CallbackQuery,state:FSMContext):
    await state.set_state(NewTask.target); await c.message.edit_text("Send @channel"); await c.answer()

@dp.message(NewTask.target)
async def crt2(m:Message,state:FSMContext):
    if not m.text.startswith("@"): return await m.answer("Start with @")
    await state.update_data(target=m.text.strip()); await state.set_state(NewTask.cpc); await m.answer(f"CPC {MIN_CPC}-{MAX_CPC}:")

@dp.message(NewTask.cpc)
async def crt3(m:Message,state:FSMContext):
    try: cpc=float(m.text); assert MIN_CPC<=cpc<=MAX_CPC
    except: return await m.answer("Invalid CPC")
    reward=round(cpc*(1-PLATFORM_FEE),6)
    await state.update_data(cpc=cpc,reward=reward); await state.set_state(NewTask.bud); await m.answer("Budget (joins):")

@dp.message(NewTask.bud)
async def crt4(m:Message,state:FSMContext):
    if not m.text.isdigit(): return await m.answer("Integer")
    bud=int(m.text); d=await state.get_data(); cost=bud*d["cpc"]
    if await bal(m.from_user.id)<cost:
        await state.clear(); return await m.answer("Insufficient balance")
    await state.update_data(budget=bud,cost=cost); await state.set_state(NewTask.conf)
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton("✅",callback_data="t_ok"),InlineKeyboardButton("❌",callback_data="t_no")]])
    await m.answer(f
