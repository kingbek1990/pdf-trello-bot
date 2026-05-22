import logging
import os
import re
import requests
import PyPDF2
from io import BytesIO
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)

# === SOZLAMALAR — Render'dagi Environment Variables dan o'qiladi ===
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TRELLO_KEY = os.environ["TRELLO_KEY"]
TRELLO_TOKEN = os.environ["TRELLO_TOKEN"]
TRELLO_LIST_ID = os.environ["TRELLO_LIST_ID"]

# === CONVERSATION STATES ===
WAITING_FOR_TYPE = 1

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# === PDF'dan ma'lumot olish ===
def extract_pdf_text(pdf_bytes: bytes) -> str:
    reader = PyPDF2.PdfReader(BytesIO(pdf_bytes))
    text = ""
    for page in reader.pages:
        text += page.extract_text() or ""
    return text

def parse_zayavka(text: str) -> dict:
    result = {}

    # Zayavka raqami: masalan 22/01-0187
    raqam_match = re.search(r'(\d{2}/\d{2}-\d{4})', text)
    if raqam_match:
        result['raqam'] = raqam_match.group(1)

    # Sana
    sana_match = re.search(r'(\d{4})\s*yil\s*[«"]?(\d{1,2})[»"]?\s*(\w+)', text)
    if sana_match:
        yil = sana_match.group(1)
        kun = sana_match.group(2).zfill(2)
        oy_nomi = sana_match.group(3).lower()
        oylar = {
            'yanvar': '01', 'fevral': '02', 'mart': '03', 'aprel': '04',
            'may': '05', 'iyun': '06', 'iyul': '07', 'avgust': '08',
            'sentabr': '09', 'oktyabr': '10', 'noyabr': '11', 'dekabr': '12'
        }
        oy = oylar.get(oy_nomi, '00')
        result['sana'] = f"{kun}.{oy}.{yil}"
    else:
        sana2 = re.search(r'(\d{2})\.(\d{2})\.(\d{4})', text)
        if sana2:
            result['sana'] = sana2.group(0)

    # Bo'lim
    bolim_match = re.search(r"Ariza Beruvchi Bo'limi\s+([^\n]+)", text)
    if bolim_match:
        result['bolim'] = bolim_match.group(1).strip()
    else:
        zayav_match = re.search(r'Заявитель[:\s]+([^\n]+)', text)
        if zayav_match:
            result['bolim'] = zayav_match.group(1).strip()

    # Mahsulotlar
    mahsulotlar = []
    lines = text.split('\n')
    for line in lines:
        line = line.strip()
        if re.match(r'^\d+\s+[А-ЯA-Za-zа-я]', line):
            if re.search(r'\bdonа?\b|\bшт\b|\bдона\b', line, re.IGNORECASE):
                parts = re.split(r'\s{2,}', line)
                if len(parts) >= 2:
                    nom = parts[1].strip()
                    nom = re.sub(r'\d[\d\s]*$', '', nom).strip()
                    if nom:
                        mahsulotlar.append(nom)

    if not mahsulotlar:
        for line in lines:
            line = line.strip()
            if re.search(r'[А-ЯA-Za-z]{3,}', line) and re.search(r'\d{3,}', line):
                if 'ariza' not in line.lower() and 'zayavka' not in line.lower():
                    nom = re.sub(r'\s+\d[\d\s]*$', '', line).strip()
                    if len(nom) > 5:
                        mahsulotlar.append(nom)

    result['mahsulotlar'] = mahsulotlar
    return result

def format_card_name(raqam, sana, bolim, tur, mahsulotlar):
    mahsulot_str = ", ".join(mahsulotlar) if mahsulotlar else "Noma'lum mahsulot"
    return f"{raqam} | {sana} | {bolim} | {tur} ({mahsulot_str})"

def create_trello_card(card_name: str) -> bool:
    url = "https://api.trello.com/1/cards"
    params = {
        "key": TRELLO_KEY,
        "token": TRELLO_TOKEN,
        "idList": TRELLO_LIST_ID,
        "name": card_name,
    }
    response = requests.post(url, params=params)
    return response.status_code == 200

# === BOT HANDLERLARI ===

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Salom! Men PDF zayavkalarni Trello'ga kirituvchi botman.\n\n"
        "Ishlatish tartibi:\n"
        "1️⃣ Menga PDF fayl yuboring\n"
        "2️⃣ Keyin zayavka turini yozing: Mahalliy, Import yoki Aralash\n\n"
        "Boshlash uchun PDF faylni yuboring!"
    )

async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc or doc.mime_type != "application/pdf":
        await update.message.reply_text("Iltimos, faqat PDF fayl yuboring.")
        return ConversationHandler.END

    await update.message.reply_text(
        "📄 PDF qabul qilindi. Endi zayavka turini yozing:\n\n"
        "*Mahalliy*, *Import* yoki *Aralash*",
        parse_mode="Markdown"
    )

    file = await doc.get_file()
    pdf_bytes = await file.download_as_bytearray()
    context.user_data['pdf_bytes'] = bytes(pdf_bytes)

    return WAITING_FOR_TYPE

async def handle_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tur_text = update.message.text.strip()
    tur_lower = tur_text.lower()

    if 'mahalliy' in tur_lower:
        tur = 'Mahalliy'
    elif 'import' in tur_lower:
        tur = 'Import'
    elif 'aralash' in tur_lower:
        tur = 'Aralash'
    else:
        await update.message.reply_text(
            "❌ Noto'g'ri tur. Iltimos, faqat quyidagilardan birini yozing:\n"
            "*Mahalliy*, *Import* yoki *Aralash*",
            parse_mode="Markdown"
        )
        return WAITING_FOR_TYPE

    await update.message.reply_text("⏳ PDF o'qilmoqda...")

    pdf_bytes = context.user_data.get('pdf_bytes')
    if not pdf_bytes:
        await update.message.reply_text("❌ Xatolik: PDF topilmadi. Qaytadan yuboring.")
        return ConversationHandler.END

    try:
        text = extract_pdf_text(pdf_bytes)
        data = parse_zayavka(text)

        raqam = data.get('raqam', "Noma'lum")
        sana = data.get('sana', "Noma'lum")
        bolim = data.get('bolim', "Noma'lum")
        mahsulotlar = data.get('mahsulotlar', [])

        card_name = format_card_name(raqam, sana, bolim, tur, mahsulotlar)

        await update.message.reply_text(
            f"📋 Trello kartasi yaratilmoqda:\n\n`{card_name}`",
            parse_mode="Markdown"
        )

        success = create_trello_card(card_name)

        if success:
            await update.message.reply_text(
                f"✅ Trello kartasi muvaffaqiyatli yaratildi!\n\n"
                f"📌 *{card_name}*\n\n"
                f"Endi Trello'da srok va mas'ulni qo'yishingiz mumkin.",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                "❌ Trello'ga karta qo'shishda xatolik yuz berdi. Qayta urinib ko'ring."
            )

    except Exception as e:
        logger.error(f"Xatolik: {e}")
        await update.message.reply_text(f"❌ PDF o'qishda xatolik: {str(e)}")

    context.user_data.clear()
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Bekor qilindi. Yangi PDF yuboring.")
    return ConversationHandler.END

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Document.PDF, handle_pdf)],
        states={
            WAITING_FOR_TYPE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_type)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv_handler)

    logger.info("Bot ishga tushdi...")
    app.run_polling()

if __name__ == "__main__":
    main()
