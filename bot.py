#!/usr/bin/env python3
import logging
import subprocess
import json as json_lib
import re
import os
import shutil
from datetime import datetime, timedelta
from io import BytesIO
import qrcode
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

from config import (
    TELEGRAM_BOT_TOKEN, ALLOWED_USERS, SERVER_IP, PUBLIC_KEY, SHORT_ID, SNI,
    HYSTERIA_PASSWORD, HYSTERIA_PORT, HYSTERIA_OBFS,
    SPLIT_PORT, SPLIT_PATH
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

INBOUND_TAGS = {
    "vless": "proxy",
    "hysteria2": "hysteria",
    "split": "split"
}
CONFIG_PATH = "/usr/local/etc/xray/config.json"
CONFIG_BACKUP = "/usr/local/etc/xray/config.json.bak"
CLIENTS_DB = "/opt/xray-bot/clients.json"

DURATION_MAP = {
    "24 часа": 86400,
    "1 месяц": 2592000,
    "3 месяца": 7776000,
    "6 месяцев": 15552000,
    "12 месяцев": 31104000,
    "Постоянный": 0
}

# ---------------------- Вспомогательные функции ----------------------
def load_clients_db():
    if not os.path.exists(CLIENTS_DB):
        return {}
    with open(CLIENTS_DB, "r") as f:
        return json_lib.load(f)

def save_clients_db(db):
    with open(CLIENTS_DB, "w") as f:
        json_lib.dump(db, f, indent=2)

def is_allowed(update: Update) -> bool:
    user_id = update.effective_user.id
    return user_id in ALLOWED_USERS

def run_command(cmd_list):
    result = subprocess.run(cmd_list, capture_output=True, text=True)
    return result.stdout.strip(), result.stderr.strip(), result.returncode

def run_command_with_stdin(cmd_list, input_data=None, timeout=10):
    try:
        result = subprocess.run(
            cmd_list, input=input_data, capture_output=True, text=True, timeout=timeout
        )
        return result.stdout.strip(), result.stderr.strip(), result.returncode
    except subprocess.TimeoutExpired:
        return "", "Command timed out", -1
    except Exception as e:
        return "", str(e), -1

def load_xray_config():
    with open(CONFIG_PATH, 'r') as f:
        return json_lib.load(f)

def save_xray_config(config):
    shutil.copy(CONFIG_PATH, CONFIG_BACKUP)
    with open(CONFIG_PATH, 'w') as f:
        json_lib.dump(config, f, indent=2)

def reload_xray():
    subprocess.run(["/usr/local/bin/xray", "api", "restartlogger"], capture_output=True)
    rc = subprocess.run(["/usr/bin/systemctl", "reload", "xray"], capture_output=True).returncode
    if rc != 0:
        subprocess.run(["/usr/bin/systemctl", "restart", "xray"], capture_output=True)

def generate_uuid():
    out, err, rc = run_command(["/usr/local/bin/xray", "uuid"])
    if rc != 0:
        raise Exception(f"Ошибка генерации UUID: {err}")
    return out

def add_client_to_xray(email, uuid_or_password, protocol='vless'):
    config = load_xray_config()
    tag = INBOUND_TAGS[protocol]
    for inbound in config['inbounds']:
        if inbound.get('tag') == tag:
            clients = inbound['settings']['clients']
            for c in clients:
                if c.get('email') == email:
                    raise Exception(f"Клиент с email {email} уже существует")
            if protocol == 'vless' or protocol == 'split':
                clients.append({"email": email, "id": uuid_or_password, "level": 0})
                if protocol == 'vless':
                    clients[-1]["flow"] = "xtls-rprx-vision"
            else:  # hysteria2
                clients.append({"email": email, "password": uuid_or_password, "level": 0})
            save_xray_config(config)
            reload_xray()
            return True
    raise Exception(f"Inbound с тегом {tag} не найден")

def remove_client_from_xray(email, protocol=None):
    config = load_xray_config()
    tags = [INBOUND_TAGS[protocol]] if protocol else INBOUND_TAGS.values()
    deleted = False
    for tag in tags:
        for inbound in config['inbounds']:
            if inbound.get('tag') == tag:
                clients = inbound['settings']['clients']
                new_clients = [c for c in clients if c.get('email') != email]
                if len(new_clients) < len(clients):
                    inbound['settings']['clients'] = new_clients
                    deleted = True
    if not deleted:
        raise Exception(f"Клиент с email {email} не найден")
    save_xray_config(config)
    reload_xray()
    return True

# ---------------------- Генерация конфигов клиентов ----------------------
def generate_vless_link(uuid, name):
    base = f"vless://{uuid}@{SERVER_IP}:443"
    params = f"security=reality&encryption=none&pbk={PUBLIC_KEY}&sni={SNI}&fp=chrome&type=tcp&flow=xtls-rprx-vision&sid={SHORT_ID}"
    return f"{base}?{params}#{name}"

def generate_hysteria_link(name):
    return f"hysteria2://{HYSTERIA_PASSWORD}@{SERVER_IP}:{HYSTERIA_PORT}?sni={SNI}&insecure=1&obfs={HYSTERIA_OBFS}&alpn=h3#hy-{name}"

def generate_split_link(uuid, name):
    return f"vless://{uuid}@{SERVER_IP}:{SPLIT_PORT}?type=xhttp&path={SPLIT_PATH}&security=none#sp-{name}"

def generate_client_conf(uuid_or_password, name, protocol='vless'):
    if protocol == 'vless':
        conf = {
            "outbounds": [{
                "protocol": "vless",
                "settings": {
                    "vnext": [{
                        "address": SERVER_IP, "port": 443,
                        "users": [{"id": uuid_or_password, "flow": "xtls-rprx-vision", "encryption": "none"}]
                    }]
                },
                "streamSettings": {
                    "network": "tcp", "security": "reality",
                    "realitySettings": {
                        "serverName": SNI, "fingerprint": "chrome",
                        "publicKey": PUBLIC_KEY, "shortId": SHORT_ID
                    }
                },
                "tag": "proxy"
            }]
        }
    elif protocol == 'hysteria2':
        conf = {
            "outbounds": [{
                "protocol": "hysteria2",
                "settings": {
                    "server": f"{SERVER_IP}:{HYSTERIA_PORT}",
                    "password": HYSTERIA_PASSWORD,
                    "obfs": {"type": HYSTERIA_OBFS, "sni": SNI},
                    "tls": {"insecure": True, "alpn": ["h3"]}
                },
                "tag": "hysteria"
            }]
        }
    elif protocol == 'split':
        conf = {
            "outbounds": [{
                "protocol": "vless",
                "settings": {
                    "vnext": [{
                        "address": SERVER_IP, "port": SPLIT_PORT,
                        "users": [{"id": uuid_or_password, "encryption": "none"}]
                    }]
                },
                "streamSettings": {
                    "network": "xhttp", "xhttpSettings": {"mode": "auto", "path": SPLIT_PATH}
                },
                "tag": "split"
            }]
        }
    return json_lib.dumps(conf, indent=2)

def generate_qr_code(data):
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    bio = BytesIO()
    img.save(bio, "PNG")
    bio.seek(0)
    return bio

def get_main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить клиента", callback_data="add_client")],
        [InlineKeyboardButton("❌ Удалить клиента", callback_data="del_client")],
        [InlineKeyboardButton("📋 Список клиентов", callback_data="list_clients")],
        [InlineKeyboardButton("📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton("🆔 Мой ID", callback_data="my_id")]
    ])

# ---------------------- Обработчики команд ----------------------
async def set_commands(app):
    await app.bot.set_my_commands([
        BotCommand("start", "Главное меню"),
        BotCommand("menu", "Показать меню")
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("⛔ У вас нет доступа к этому боту.")
        return
    await update.message.reply_text(
        "Добро пожаловать! Выберите действие:",
        reply_markup=get_main_menu_keyboard()
    )

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("⛔ Нет доступа.")
        return
    await update.message.reply_text(
        "Главное меню:",
        reply_markup=get_main_menu_keyboard()
    )

async def show_my_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    await query.edit_message_text(f"Ваш Telegram ID: `{user_id}`", parse_mode="Markdown")
    await query.message.reply_text("Главное меню:", reply_markup=get_main_menu_keyboard())

# ---------------------- Callback обрабоотчик ----------------------
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.callback_query.answer("⛔ Нет доступа", show_alert=True)
        return
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "add_client":
        keyboard = [
            [InlineKeyboardButton("🕒 24 часа", callback_data="dur_86400")],
            [InlineKeyboardButton("📅 1 месяц", callback_data="dur_2592000")],
            [InlineKeyboardButton("📅 3 месяца", callback_data="dur_7776000")],
            [InlineKeyboardButton("📅 6 месяцев", callback_data="dur_15552000")],
            [InlineKeyboardButton("📅 12 месяцев", callback_data="dur_31104000")],
            [InlineKeyboardButton("♾️ Постоянный", callback_data="dur_0")],
            [InlineKeyboardButton("◀️ Назад", callback_data="back_to_menu")]
        ]
        await query.edit_message_text("Выберите срок действия:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("dur_"):
        seconds = int(data.split("_")[1])
        context.user_data['duration'] = seconds
        duration_text = next((k for k, v in DURATION_MAP.items() if v == seconds), f"{seconds} сек")
        keyboard = [
            [InlineKeyboardButton("🚀 Стандарт (VLESS)", callback_data="proto_vless")],
            [InlineKeyboardButton("🎥 Видеозвонок (Hysteria2)", callback_data="proto_hysteria")],
            [InlineKeyboardButton("🛡️ Максимальная защита (VLESS+Split)", callback_data="proto_both_split")],
            [InlineKeyboardButton("◀️ Назад", callback_data="add_client")]
        ]
        await query.edit_message_text(f"Вы выбрали: {duration_text}\nТеперь выберите тип подключения:",
                                      reply_markup=InlineKeyboardMarkup(keyboard))
        context.user_data['awaiting_protocol'] = True

    elif data.startswith("proto_"):
        proto = data.split("_", 1)[1]
        context.user_data['protocol'] = proto
        await query.edit_message_text("Введите имя клиента (латиница, 3-20 символов, можно - и _):")
        context.user_data['awaiting_name'] = True

    elif data == "del_client":
        await query.edit_message_text("Введите имя клиента (email) для удаления:")
        context.user_data['awaiting_delete'] = True

    elif data == "list_clients":
        db = load_clients_db()
        if not db:
            keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="back_to_menu")]]
            await query.edit_message_text("Нет клиентов.", reply_markup=InlineKeyboardMarkup(keyboard))
            return
        msg = "📋 *Список клиентов:*\n\n"
        for email, info in db.items():
            expires = info.get("expires", 0)
            name = info.get("name", email)
            proto = info.get("protocol", "vless")
            expire_str = "♾️ постоянный" if expires == 0 else f"⏰ до {datetime.fromtimestamp(expires).strftime('%Y-%m-%d %H:%M')}"
            msg += f"• *{name}* ({proto}) — {expire_str}\n"
        keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="back_to_menu")]]
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data == "stats":
        await query.edit_message_text("📊 Статистика временно недоступна")
        await query.message.reply_text("Главное меню:", reply_markup=get_main_menu_keyboard())

    elif data == "my_id":
        await show_my_id(update, context)

    elif data == "back_to_menu":
        await query.edit_message_text("Главное меню:", reply_markup=get_main_menu_keyboard())

# ---------------------- Обработчик текстовых сообщений ----------------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("⛔ Нет доступа.")
        return
    text = update.message.text.strip()

    if context.user_data.get('awaiting_name'):
        name = text
        if not re.match(r'^[a-zA-Z0-9_-]{3,20}$', name):
            await update.message.reply_text("Некорректное имя. Разрешены буквы, цифры, - и _. Длина 3-20. Попробуйте снова /menu")
            context.user_data['awaiting_name'] = False
            return
        duration = context.user_data.get('duration', 0)
        proto = context.user_data.get('protocol', 'vless')
        try:
            expire_ts = 0 if duration == 0 else int((datetime.now() + timedelta(seconds=duration)).timestamp())
            if proto == 'vless':
                uuid = generate_uuid()
                email = name
                add_client_to_xray(email, uuid, 'vless')
                db = load_clients_db()
                db[email] = {"name": name, "uuid": uuid, "expires": expire_ts, "protocol": "vless"}
                save_clients_db(db)
                link = generate_vless_link(uuid, name)
                qr = generate_qr_code(link)
                conf = generate_client_conf(uuid, name, 'vless')
                await update.message.reply_photo(photo=qr, caption=f"QR-код для {name}")
                await update.message.reply_document(document=BytesIO(conf.encode()), filename=f"{name}_vless.conf")
                await update.message.reply_text(f"✅ Клиент *{name}* (VLESS) добавлен.\n\nСсылка: `{link}`", parse_mode="Markdown")
            elif proto == 'hysteria':
                email = name
                add_client_to_xray(email, HYSTERIA_PASSWORD, 'hysteria2')
                db = load_clients_db()
                db[email] = {"name": name, "uuid": HYSTERIA_PASSWORD, "expires": expire_ts, "protocol": "hysteria2"}
                save_clients_db(db)
                link = generate_hysteria_link(name)
                qr = generate_qr_code(link)
                conf = generate_client_conf(HYSTERIA_PASSWORD, name, 'hysteria2')
                await update.message.reply_photo(photo=qr, caption=f"QR-код для {name} (Hysteria2)")
                await update.message.reply_document(document=BytesIO(conf.encode()), filename=f"{name}_hysteria.conf")
                await update.message.reply_text(f"✅ Клиент *{name}* (Hysteria2) добавлен.\n\nСсылка: `{link}`", parse_mode="Markdown")
            elif proto == 'both_split':
                uuid_v = generate_uuid()
                email_v = f"{name}_v"
                add_client_to_xray(email_v, uuid_v, 'vless')
                uuid_s = generate_uuid()
                email_s = f"{name}_s"
                add_client_to_xray(email_s, uuid_s, 'split')
                db = load_clients_db()
                db[email_v] = {"name": f"{name} (VLESS)", "uuid": uuid_v, "expires": expire_ts, "protocol": "vless"}
                db[email_s] = {"name": f"{name} (Split)", "uuid": uuid_s, "expires": expire_ts, "protocol": "split"}
                save_clients_db(db)
                # VLESS
                link_v = generate_vless_link(uuid_v, f"{name}_v")
                qr_v = generate_qr_code(link_v)
                conf_v = generate_client_conf(uuid_v, f"{name}_v", 'vless')
                await update.message.reply_photo(photo=qr_v, caption=f"QR VLESS {name}")
                await update.message.reply_document(document=BytesIO(conf_v.encode()), filename=f"{name}_vless.conf")
                # Split
                link_s = generate_split_link(uuid_s, f"{name}_s")
                qr_s = generate_qr_code(link_s)
                conf_s = generate_client_conf(uuid_s, f"{name}_s", 'split')
                await update.message.reply_photo(photo=qr_s, caption=f"QR SplitHTTP {name}")
                await update.message.reply_document(document=BytesIO(conf_s.encode()), filename=f"{name}_split.conf")
                await update.message.reply_text(f"✅ Клиент *{name}* (VLESS+SplitHTTP) добавлен.")
        except Exception as e:
            logger.error(f"Error adding client: {e}")
            await update.message.reply_text(f"❌ Ошибка: {e}")
        finally:
            context.user_data.clear()
            await update.message.reply_text("Главное меню:", reply_markup=get_main_menu_keyboard())

    elif context.user_data.get('awaiting_delete'):
        email = text
        try:
            remove_client_from_xray(email)
            db = load_clients_db()
            if email in db:
                del db[email]
                save_clients_db(db)
            await update.message.reply_text(f"🗑️ Клиент {email} удалён.")
        except Exception as e:
            logger.error(f"Error removing client: {e}")
            await update.message.reply_text(f"❌ Ошибка: {e}")
        finally:
            context.user_data['awaiting_delete'] = False
            await update.message.reply_text("Главное меню:", reply_markup=get_main_menu_keyboard())
    else:
        await update.message.reply_text("Используйте /menu для управления.")

def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.post_init = set_commands
    app.run_polling()

if __name__ == "__main__":
    main()