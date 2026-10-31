"""
BON TOOLS — FastAPI + Telegram Bot + Key System
Deploy: Render (Python 3.11+)
"""
import os, json, time, secrets, asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes,
)

# ================== CONFIG ==================
BOT_TOKEN     = os.getenv("BOT_TOKEN", "8984428573:AAHquQC9pLKVd6y7lM8tXEBi25AmGnYLVB8")
ADMIN_IDS     = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
PUBLIC_URL    = os.getenv("PUBLIC_URL", "").rstrip("/")   # vd: https://bontools.onrender.com
PORT          = int(os.getenv("PORT", "8000"))
BACCARAT_API  = "https://apibcr-t1gk.onrender.com/api/baccarat"
DATA_FILE     = Path(os.getenv("DATA_FILE", "keys.json"))

# (tuỳ chọn) Đồng bộ keys.json lên GitHub để không mất khi Render restart
GITHUB_TOKEN  = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO   = os.getenv("GITHUB_REPO", "")   # dạng "user/repo-data"
GITHUB_PATH   = "keys.json"

PLANS = {
    "1day":  {"name": "1 Ngày",  "price": 10000, "days": 1},
    "3day":  {"name": "3 Ngày",  "price": 20000, "days": 3},
    "30day": {"name": "1 Tháng", "price": 70000, "days": 30},
}

# ================== KEY STORAGE ==================
def _local_load() -> dict:
    if DATA_FILE.exists():
        try: return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception: pass
    return {}

def _local_save(keys: dict):
    DATA_FILE.write_text(json.dumps(keys, ensure_ascii=False, indent=2), encoding="utf-8")

async def gh_load() -> dict:
    if not (GITHUB_TOKEN and GITHUB_REPO): return _local_load()
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_PATH}"
    h = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(url, headers=h)
        if r.status_code == 404: return {}
        if r.status_code >= 300: return _local_load()
        import base64
        return json.loads(base64.b64decode(r.json()["content"]).decode())

async def gh_save(keys: dict):
    _local_save(keys)
    if not (GITHUB_TOKEN and GITHUB_REPO): return
    import base64
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_PATH}"
    h = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(url, headers=h)
        sha = r.json().get("sha") if r.status_code == 200 else None
        body = {
            "message": f"keys update {datetime.now().isoformat(timespec='seconds')}",
            "content": base64.b64encode(json.dumps(keys, indent=2).encode()).decode(),
        }
        if sha: body["sha"] = sha
        await c.put(url, headers=h, json=body)

async def get_keys() -> dict:  return await gh_load()
async def set_keys(k: dict):   await gh_save(k)

# ================== KEY LOGIC ==================
def gen_key() -> str:
    return "BON-" + "-".join(secrets.token_hex(2).upper() for _ in range(3))

async def create_key(user_id: int, plan_id: str) -> dict:
    keys = await get_keys()
    plan = PLANS[plan_id]
    k = gen_key()
    while k in keys: k = gen_key()
    now = datetime.now(timezone.utc)
    keys[k] = {
        "user_id": user_id, "plan": plan_id,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(days=plan["days"])).isoformat(),
        "revoked": False,
    }
    await set_keys(keys)
    return {"key": k, **keys[k]}

async def validate_key(key: str) -> dict:
    if not key: return {"valid": False, "reason": "Thiếu key"}
    keys = await get_keys()
    k = keys.get(key.strip().upper())
    if not k: return {"valid": False, "reason": "Key không tồn tại"}
    if k.get("revoked"): return {"valid": False, "reason": "Key đã bị khoá"}
    exp = datetime.fromisoformat(k["expires_at"])
    if datetime.now(timezone.utc) > exp: return {"valid": False, "reason": "Key đã hết hạn"}
    return {"valid": True, "plan": k["plan"], "expires_at": k["expires_at"]}

# ================== FASTAPI ==================
app = FastAPI(title="BON TOOLS")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/")
async def index(): return FileResponse("index.html")

@app.get("/health")
async def health(): return {"ok": True, "time": datetime.now(timezone.utc).isoformat()}

@app.post("/api/check-key")
async def api_check_key(body: dict):
    key = (body or {}).get("key", "").strip().upper()
    return await validate_key(key)

@app.get("/api/baccarat")
async def api_baccarat(x_key: Optional[str] = Header(None, alias="X-Key")):
    v = await validate_key(x_key or "")
    if not v["valid"]: raise HTTPException(401, v["reason"])
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(BACCARAT_API, params={"_t": int(time.time())})
            return JSONResponse(r.json())
    except Exception as e:
        raise HTTPException(502, f"Upstream lỗi: {e}")

# ================== TELEGRAM BOT ==================
tg_app: Optional[Application] = None

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔑 Tạo Key", callback_data="menu_create")],
        [InlineKeyboardButton("📋 Key của tôi", callback_data="menu_mykeys")],
        [InlineKeyboardButton("💰 Bảng giá", callback_data="menu_price")],
        ([InlineKeyboardButton("🌐 Mở BON TOOLS", url=PUBLIC_URL)] if PUBLIC_URL else []),
    ])
    await update.message.reply_text(
        "👋 *BON TOOLS*\n\n"
        "Bot dự đoán Baccarat AI với 1000+ thuật toán.\n"
        "Chọn chức năng:",
        reply_markup=kb, parse_mode="Markdown"
    )

async def cmd_myid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🆔 User ID của bạn: `{update.effective_user.id}`", parse_mode="Markdown")

async def cmd_mykeys(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    keys = await get_keys()
    mine = [(k, v) for k, v in keys.items() if v["user_id"] == uid and not v.get("revoked")]
    if not mine:
        await update.message.reply_text("Bạn chưa có key nào. Nhấn /start để tạo.")
        return
    txt = "🔑 *Key của bạn:*\n\n"
    for k, v in mine:
        exp = datetime.fromisoformat(v["expires_at"])
        live = datetime.now(timezone.utc) < exp
        txt += f"`{k}`\n{'✅' if live else '❌'} {PLANS[v['plan']]['name']} · HH: {exp.strftime('%d/%m/%Y %H:%M')}\n\n"
    await update.message.reply_text(txt, parse_mode="Markdown")

async def cb_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    txt = "💰 *Bảng giá BON TOOLS*\n\n"
    for p in PLANS.values(): txt += f"• {p['name']}: *{p['price']:,}đ*\n"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔑 Tạo Key ngay", callback_data="menu_create")]])
    await q.edit_message_text(txt, reply_markup=kb, parse_mode="Markdown")

async def cb_mykeys(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id
    keys = await get_keys()
    mine = [(k, v) for k, v in keys.items() if v["user_id"] == uid and not v.get("revoked")]
    if not mine:
        await q.edit_message_text("Bạn chưa có key. Nhấn /start → Tạo Key.")
        return
    txt = "🔑 *Key của bạn:*\n\n"
    for k, v in mine:
        exp = datetime.fromisoformat(v["expires_at"])
        live = datetime.now(timezone.utc) < exp
        txt += f"`{k}`\n{'✅' if live else '❌'} {PLANS[v['plan']]['name']} · HH: {exp.strftime('%d/%m/%Y %H:%M')}\n\n"
    await q.edit_message_text(txt, parse_mode="Markdown")

async def cb_create(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("1 Ngày · 10.000đ", callback_data="buy_1day")],
        [InlineKeyboardButton("3 Ngày · 20.000đ", callback_data="buy_3day")],
        [InlineKeyboardButton("1 Tháng · 70.000đ", callback_data="buy_30day")],
    ])
    await q.edit_message_text("Chọn gói bạn muốn mua:", reply_markup=kb)

async def cb_buy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    plan_id = q.data.split("_", 1)[1]
    plan = PLANS[plan_id]
    ctx.user_data["pending_plan"] = plan_id
    await q.edit_message_text(
        f"💳 Gói *{plan['name']}* — *{plan['price']:,}đ*\n\n"
        f"🏦 *MB Bank*\nSTK: `0000000000`\nChủ TK: BON TOOLS\n"
        f"Số tiền: `{plan['price']}`\nNội dung: `{q.from_user.id}`\n\n"
        f"Chuyển xong, gửi *ảnh bill* vào đây. Admin sẽ duyệt và trả key.",
        parse_mode="Markdown"
    )

async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    plan_id = ctx.user_data.get("pending_plan")
    if not plan_id:
        await update.message.reply_text("Vui lòng chọn gói trước bằng /start → Tạo Key.")
        return
    plan = PLANS[plan_id]
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Duyệt", callback_data=f"approve_{uid}_{plan_id}"),
        InlineKeyboardButton("❌ Từ chối", callback_data=f"reject_{uid}_{plan_id}"),
    ]])
    caption = (f"🧾 *Bill chờ duyệt*\nUser: `{uid}`\n"
               f"Gói: *{plan['name']}* — {plan['price']:,}đ")
    sent = 0
    for aid in ADMIN_IDS:
        try:
            await ctx.bot.send_photo(chat_id=aid, photo=update.message.photo[-1].file_id,
                                     caption=caption, reply_markup=kb, parse_mode="Markdown")
            sent += 1
        except Exception: pass
    if sent:
        await update.message.reply_text("✅ Đã gửi bill tới admin, vui lòng chờ duyệt.")
    else:
        await update.message.reply_text("⚠️ Admin chưa cấu hình. Vui lòng liên hệ admin.")

async def cb_approve_reject(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id not in ADMIN_IDS:
        await q.answer("Bạn không phải admin.", show_alert=True); return
    parts = q.data.split("_")
    action, target_uid, plan_id = parts[0], int(parts[1]), parts[2]
    if action == "approve":
        k = await create_key(target_uid, plan_id)
        exp = datetime.fromisoformat(k["expires_at"])
        try:
            await ctx.bot.send_message(target_uid,
                f"🎉 *Key BON TOOLS của bạn:*\n`{k['key']}`\n\n"
                f"Gói: *{PLANS[plan_id]['name']}*\n"
                f"Hết hạn: {exp.strftime('%d/%m/%Y %H:%M')}\n\n"
                f"Nhập key vào: {PUBLIC_URL or 'https://bontools.onrender.com'}",
                parse_mode="Markdown")
        except Exception: pass
        await q.edit_message_caption(caption=(q.message.caption or "") + "\n\n✅ ĐÃ DUYỆT")
    else:
        try:
            await ctx.bot.send_message(target_uid, "❌ Bill của bạn bị từ chối. Vui lòng liên hệ admin.")
        except Exception: pass
        await q.edit_message_caption(caption=(q.message.caption or "") + "\n\n❌ ĐÃ TỪ CHỐI")

async def cmd_genkey(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Không có quyền."); return
    if len(ctx.args) != 2:
        await update.message.reply_text("Dùng: /genkey <user_id> <1day|3day|30day>"); return
    try: target = int(ctx.args[0])
    except ValueError: await update.message.reply_text("user_id sai."); return
    plan = ctx.args[1]
    if plan not in PLANS:
        await update.message.reply_text("Plan: 1day | 3day | 30day"); return
    k = await create_key(target, plan)
    await update.message.reply_text(f"✅ Đã tạo key cho `{target}`:\n`{k['key']}`", parse_mode="Markdown")
    try:
        await ctx.bot.send_message(target,
            f"🎉 Key BON TOOLS:\n`{k['key']}`\nGói: {PLANS[plan]['name']}",
            parse_mode="Markdown")
    except Exception: pass

async def cmd_revoke(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    if not ctx.args:
        await update.message.reply_text("Dùng: /revoke <key>"); return
    key = ctx.args[0].upper()
    keys = await get_keys()
    if key not in keys:
        await update.message.reply_text("Không tìm thấy key."); return
    keys[key]["revoked"] = True
    await set_keys(keys)
    await update.message.reply_text(f"✅ Đã khoá `{key}`", parse_mode="Markdown")

# ================== BOT LIFECYCLE ==================
@app.on_event("startup")
async def on_startup():
    global tg_app
    tg_app = Application.builder().token(BOT_TOKEN).build()
    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CommandHandler("myid", cmd_myid))
    tg_app.add_handler(CommandHandler("mykeys", cmd_mykeys))
    tg_app.add_handler(CommandHandler("genkey", cmd_genkey))
    tg_app.add_handler(CommandHandler("revoke", cmd_revoke))
    tg_app.add_handler(CallbackQueryHandler(cb_price,    pattern="^menu_price$"))
    tg_app.add_handler(CallbackQueryHandler(cb_create,   pattern="^menu_create$"))
    tg_app.add_handler(CallbackQueryHandler(cb_mykeys,   pattern="^menu_mykeys$"))
    tg_app.add_handler(CallbackQueryHandler(cb_buy,      pattern="^buy_"))
    tg_app.add_handler(CallbackQueryHandler(cb_approve_reject, pattern="^(approve|reject)_"))
    tg_app.add_handler(MessageHandler(filters.PHOTO, on_photo))

    await tg_app.initialize()
    await tg_app.start()
    if PUBLIC_URL:
        try:
            await tg_app.bot.set_webhook(f"{PUBLIC_URL}/telegram/webhook", drop_pending_updates=True)
            print(f"[bot] webhook → {PUBLIC_URL}/telegram/webhook")
        except Exception as e:
            print("[bot] set_webhook fail:", e)
    else:
        await tg_app.updater.start_polling()
        print("[bot] polling mode (chưa có PUBLIC_URL)")

@app.on_event("shutdown")
async def on_shutdown():
    if tg_app:
        try:
            if not PUBLIC_URL and tg_app.updater.running:
                await tg_app.updater.stop()
        except Exception: pass
        try: await tg_app.stop()
        except Exception: pass
        try: await tg_app.shutdown()
        except Exception: pass

@app.post("/telegram/webhook")
async def tg_webhook(request: Request):
    data = await request.json()
    upd = Update.de_json(data, tg_app.bot)
    await tg_app.process_update(upd)
    return {"ok": True}

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=PORT)
