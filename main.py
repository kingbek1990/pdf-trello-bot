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

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TRELLO_KEY = os.environ["TRELLO_KEY"]
TRELLO_TOKEN = os.environ["TRELLO_TOKEN"]
TRELLO_LIST_ID = os.environ["TRELLO_LIST_ID"]

WAITING_FOR_TYPE = 1

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

def extract_pdf_text(pdf_bytes: bytes) -> str:
    reader = PyPDF2.PdfReader(BytesIO(pdf_bytes))
    text = ""
    for page in reader.pages:
        text += page.extract_text() or ""
    return text

def parse_zayavka(text: str) -> dict:
    result = {}

    # Zayavka raqami
    raqam_match = re.search(r'(\d+/\d+-\d+)', text)
    if raqam_match:
        result['raqam'] = raqam_match.group(1)

    # Sana
    sana_match = re.search(r'(\d{4})\s*yil\s*[«"»]?(\d{1,2})[«"»]?\s*(\w+)', text)
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
    bolim_match = re.search(r"Ariza Beruvchi Bo'lim[i]?\s+([^\n]+)", text)
    if bolim_match:
        bolim_full = bolim_match.group(1).strip()
        if bolim_full:
            bolim_short = re.split(r'[,.(]', bolim_full)[0].strip()
            result['bolim'] = bolim_short if bolim_short else "Noma'lum"
        else:
            # Bo'lim bo'sh — "X uchun" formatini qidirish
            uchun_match = re.search(r"([^\n]+)\s+uchun\b", text)
            if uchun_match:
                result['bolim'] = uchun_match.group(1).strip()
            else:
                result['bolim'] = "Noma'lum"
    else:
        zayav_match = re.search(r'Заявитель[:\s]+([^\n]+)', text)
        if zayav_match:
            bolim_full = zayav_match.group(1).strip()
            result['bolim'] = re.split(r'[,.(]', bolim_full)[0].strip()
        else:
            result['bolim'] = "Noma'lum"

    # O'lchov birliklari — barcha uchragan variantlar
    o_lchovlar = (
        r'(?:'
        r'шт|ШТ|'
        r'[Dd][Oo][Nn][Aa]|дона|DONA|'
        r'компл|комплект|КОМПЛ|'
        r'комп|КОМП|'
        r'komplekt|'
        r'nafar|нафар|'
        r'м\b|кг\b|л\b|'
        r'kg\b|m2\b|м2\b|'
        r'pcs\b|pc\b'
        r')'
    )

    # Matnni bir qatorga keltirish
    text_oneline = re.sub(r'\n(?!\d)', ' ', text)

    mahsulotlar = []

    # Format 1: Raqamli detal raqami (6+ raqam, tire bilan ham)
    detal_matches = re.findall(
        rf'(?<!\d)(\d{{1,2}})\s+(\d[\d\-]+\d)\s+(.+?)\s+{o_lchovlar}[\s\n]',
        text_oneline
    )
    if detal_matches:
        filtered = [(q, d, n) for q, d, n in detal_matches
                    if len(d.replace('-', '')) >= 6]
        if filtered:
            for _, _, nom in sorted(filtered, key=lambda x: int(x[0])):
                nom = nom.strip()
                if len(nom) > 2:
                    mahsulotlar.append(nom)

    # Format 2: Harfli detal raqami + nom + o'lchov
    if not mahsulotlar:
        harfli_matches = re.findall(
            rf'(?<!\d)(\d{{1,2}})\s+[A-ZА-Я][A-ZА-Яa-zа-я0-9\s\-\.\/]+?\s+([А-ЯA-Za-zа-яёЁ][а-яА-Яa-zA-Z\s\-\/]+?)\s+{o_lchovlar}[\s\n]',
            text_oneline
        )
        if harfli_matches:
            skip = ['ariza', 'byurtma', 'tasdiq', 'nomi', 'birl', 'narx']
            for _, nom in sorted(harfli_matches, key=lambda x: int(x[0])):
                nom = nom.strip()
                if len(nom) > 2 and not any(w in nom.lower() for w in skip):
                    mahsulotlar.append(nom)

    # Format 3: Damas\Labo tipidagi matnli detal raqami
    if not mahsulotlar:
        damas_matches = re.findall(
            r'(?<!\d)(\d{1,2})\s+[A-Za-z]+[\\\/][A-Za-z]+\s+([A-Z][A-Z\s\-\/]+?)\s+\d',
            text_oneline
        )
        if damas_matches:
            for _, nom in sorted(damas_matches, key=lambda x: int(x[0])):
                nom = nom.strip()
                if len(nom) > 2:
                    mahsulotlar.append(nom)

    # Format 4: Detal raqamisiz, o'lchov bilan
    if not mahsulotlar:
        simple_matches = re.findall(
            rf'(?<!\d)(\d{{1,2}})\s+([А-ЯA-Za-zа-яёЁ\-].+?)\s+{o_lchovlar}[\s\n]',
            text_oneline
        )
        skip = ['ariza', 'zayavka', 'byurtma', 'muddati', "bo'limi", 'xajmi',
                'total', 'jami', 'tasdiq', 'nomi', 'birl', 'narx', 'detal',
                'bir', 'oy', 'davomida', 'sarflanadigan', 'materiallar']
        for _, nom in sorted(simple_matches, key=lambda x: int(x[0])):
            nom = nom.strip()
            if len(nom) > 2 and not any(w in nom.lower() for w in skip):
                mahsulotlar.append(nom)

    result['mahsulotlar'] = mahsulotlar
    logger.info(f"Bo'lim: {result.get('bolim')}")
    logger.info(f"Mahsulotlar ({len(mahsulotlar)} ta): {mahsulotlar[:5]}")
    return result

def format_card_name(raqam, sana, bolim, tur, mahsulotlar):
    if len(mahsulotlar) >= 2:
        nom_str = ", ".join(mahsulotlar[:2])
    elif mahsulotlar:
        nom_str = mahsulotlar[0]
    else:
        nom_str = "Mahsulot"
    return f"{raqam} | {sana} | {bolim} | {tur} ({nom_str})"

def format_description(mahsulotlar):
    if not mahsulotlar:
        return ""
    lines = [f"{i+1}. {nom}" for i, nom in enumerate(mahsulotlar)]
    return "Mahsulotlar ro'yxati:\n" + "\n".join(lines)

def create_trello_card(card_name: str, description: str) -> bool:
    url = "https://api.trello.com/1/cards"
    params = {
        "key": TRELLO_KEY,
        "token": TRELLO_TOKEN,
        "idList": TRELLO_LIST_ID,
        "name": card_name,
        "desc": description,
    }
    response = requests.post(url, params=params)
    return response.status_code == 200

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
        logger.info(f"PDF matni:\n{text[:1000]}")

        data = parse_zayavka(text)

        raqam = data.get('raqam', "Noma'lum")
        sana = data.get('sana', "Noma'lum")
        bolim = data.get('bolim', "Noma'lum")
        mahsulotlar = data.get('mahsulotlar', [])

        card_name = format_card_name(raqam, sana, bolim, tur, mahsulotlar)
        description = format_description(mahsulotlar)

        await update.message.reply_text(
            f"📋 Trello kartasi yaratilmoqda:\n\n`{card_name}`",
            parse_mode="Markdown"
        )

        success = create_trello_card(card_name, description)

        if success:
            await update.message.reply_text(
                f"✅ Trello kartasi muvaffaqiyatli yaratildi!\n\n"
                f"📌 *{card_name}*\n\n"
                f"Endi Trello'da srok va mas'ulni qo'yishingiz mumkin.",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                "❌ Trello'ga karta qo'shishda xatolik yuz berdi."
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
