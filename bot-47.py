import os
import json
import re
import requests
import firebase_admin
from firebase_admin import credentials, firestore
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

# ============================================================
# ENV VARIABLES (set di Railway)
# ============================================================
BOT_TOKEN          = os.environ["BOT_TOKEN"]
ALLOWED_USER_ID    = int(os.environ.get("ALLOWED_USER_ID", "0"))  # Telegram user ID lo
COLLECTION         = os.environ.get("COLLECTION", "products")
FIREBASE_CREDS_JSON = os.environ.get("FIREBASE_CREDS_JSON")

# ============================================================
# INIT FIRESTORE
# ============================================================
def init_firestore():
    if not firebase_admin._apps:
        info = json.loads(FIREBASE_CREDS_JSON)
        cred = credentials.Certificate(info)
        firebase_admin.initialize_app(cred)
    return firestore.client()

db = init_firestore()

# ============================================================
# SCRAPER SHOPEE
# ============================================================
def scrape_shopee(url: str) -> dict | None:
    # Extract shopid dan itemid dari URL
    # Format: shopee.co.id/nama-produk-i.SHOPID.ITEMID
    match = re.search(r"-i\.(\d+)\.(\d+)", url)
    if not match:
        # Coba format lain
        match = re.search(r"shopid=(\d+)&itemid=(\d+)", url)
        if not match:
            return None

    shop_id, item_id = match.group(1), match.group(2)

    api_url = f"https://shopee.co.id/api/v4/item/get?itemid={item_id}&shopid={shop_id}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36",
        "Referer": "https://shopee.co.id/",
    }

    try:
        resp = requests.get(api_url, headers=headers, timeout=10)
        data = resp.json()
        item = data.get("data", {})
        if not item:
            return None

        # Ambil gambar pertama
        images = item.get("images", [])
        gambar_url = ""
        if images:
            gambar_url = f"https://cf.shopee.co.id/file/{images[0]}"

        harga_raw = item.get("price", 0)
        harga = int(harga_raw / 100000)  # Shopee simpan harga * 100000

        return {
            "nama"      : item.get("name", ""),
            "brand"     : item.get("brand", ""),
            "kategori"  : "",
            "harga"     : harga,
            "deskripsi" : item.get("description", "")[:500],
            "gambar"    : gambar_url,
            "platform"  : "Shopee",
            "shop_id"   : shop_id,
            "item_id"   : item_id,
        }
    except Exception as e:
        print(f"[ERROR] Shopee scrape: {e}")
        return None

# ============================================================
# SCRAPER TIKTOK SHOP
# ============================================================
def scrape_tiktok(url: str) -> dict | None:
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36",
    }
    try:
        # Follow redirect buat dapetin URL asli
        resp = requests.get(url, headers=headers, timeout=10, allow_redirects=True)
        final_url = resp.url

        # Coba ambil meta tags dari halaman
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp.text, "html.parser")

        nama = ""
        gambar = ""
        deskripsi = ""

        # Meta tags
        og_title = soup.find("meta", property="og:title")
        og_image = soup.find("meta", property="og:image")
        og_desc  = soup.find("meta", property="og:description")

        if og_title:
            nama = og_title.get("content", "")
        if og_image:
            gambar = og_image.get("content", "")
        if og_desc:
            deskripsi = og_desc.get("content", "")[:500]

        if not nama:
            return None

        return {
            "nama"      : nama,
            "brand"     : "",
            "kategori"  : "",
            "harga"     : 0,  # TikTok harga harus diisi manual
            "deskripsi" : deskripsi,
            "gambar"    : gambar,
            "platform"  : "TikTok Shop",
        }
    except Exception as e:
        print(f"[ERROR] TikTok scrape: {e}")
        return None

# ============================================================
# DETEKSI PLATFORM & SCRAPE
# ============================================================
def detect_and_scrape(url: str) -> dict | None:
    if "shopee.co.id" in url or "shope.ee" in url:
        # Resolve short URL dulu kalau shope.ee
        if "shope.ee" in url:
            try:
                r = requests.head(url, allow_redirects=True, timeout=10)
                url = r.url
            except:
                pass
        return scrape_shopee(url)

    elif "tiktok.com" in url or "vt.tiktok.com" in url:
        return scrape_tiktok(url)

    return None

# ============================================================
# SIMPAN KE FIRESTORE
# ============================================================
def save_to_firestore(product: dict, affiliate_link: str) -> str:
    product["link"]       = affiliate_link
    product["updated_at"] = firestore.SERVER_TIMESTAMP

    doc_ref = db.collection(COLLECTION).document()
    doc_ref.set(product)
    return doc_ref.id

# ============================================================
# TELEGRAM HANDLER
# ============================================================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # Security: hanya lo yang bisa pakai bot ini
    if ALLOWED_USER_ID and user_id != ALLOWED_USER_ID:
        await update.message.reply_text("⛔ Akses ditolak.")
        return

    text = update.message.text or ""

    # Cari URL di pesan
    urls = re.findall(r"https?://\S+", text)
    if not urls:
        await update.message.reply_text(
            "📦 Kirim link produk Shopee atau TikTok Shop ke sini!\n\n"
            "Contoh:\n"
            "• https://shopee.co.id/...\n"
            "• https://shope.ee/...\n"
            "• https://vt.tiktok.com/..."
        )
        return

    url = urls[0]
    await update.message.reply_text(f"⏳ Lagi ambil data produk...")

    product = detect_and_scrape(url)

    if not product:
        await update.message.reply_text(
            "❌ Gagal ambil data produk.\n"
            "Pastikan link-nya dari Shopee atau TikTok Shop ya bro!"
        )
        return

    # Simpan ke Firestore dengan link affiliate = URL yang dikirim
    doc_id = save_to_firestore(product, affiliate_link=url)

    # Notif sukses
    harga_text = f"Rp {product['harga']:,}" if product['harga'] else "⚠️ Perlu diisi manual"
    msg = (
        f"✅ *Produk berhasil ditambah!*\n\n"
        f"📦 *{product['nama']}*\n"
        f"🏷 Brand: {product['brand'] or '-'}\n"
        f"💰 Harga: {harga_text}\n"
        f"🛒 Platform: {product['platform']}\n\n"
        f"🔗 Langsung tampil di website lo!"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    print("[START] Bot jalan...")
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT, handle_message))
    app.run_polling()
