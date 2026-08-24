# universalbot

@BotFather orqali yaratilgan Telegram Bot API bot. aiogram, yt-dlp va ffmpeg (imageio-ffmpeg
orqali) asosida ishlaydi.

## Imkoniyatlari

- Shaxsiy chatda musiqa nomini yozsangiz - SoundCloud'dan qidirib, MP3 qilib yuboradi.
- Instagram / TikTok / Pinterest havolasini tashlasangiz - "Video / Rasm / Musiqa" tugmalari
  chiqadi, tanlaganingizni yuklab beradi.
- Har qanday video yuborsangiz - aylana video (video note, maksimal 60 soniya) qilib qaytaradi.
- Musiqa fayl (MP3) yuborsangiz - yangi nom va ijrochini so'rab, ID3 teglarini o'zgartirib beradi.
- Guruhda `.id` - reply qilingan foydalanuvchining ID'sini chiqaradi.

Erkin matn orqali musiqa qidirish faqat shaxsiy chatda ishlaydi (guruhda har bir xabarni qidiruvga
aylantirmaslik uchun). Havola-tugmalar, aylana video va teg o'zgartirish guruhda ham ishlaydi.

## O'rnatish

```bash
pip install -r requirements.txt
BOT_TOKEN=... python bot.py
```

Muhit o'zgaruvchilari:

- `BOT_TOKEN` - @BotFather'dan olingan bot tokeni (majburiy).
- `PORT` - Render kabi platformalarda health-check uchun ochiladigan port (ixtiyoriy, standart 8080).
- `COOKIES_FILE` - yt-dlp uchun cookies.txt fayl yo'li (ixtiyoriy, masalan Render'ning "Secret
  File" xususiyati orqali qo'shilgan bo'lsa). Instagram ko'pincha login talab qiladi - bunday
  hollarda brauzerdan eksport qilingan cookies.txt yordam beradi.
- `COOKIES_CONTENT` - yuqoridagi bilan bir xil, lekin fayl yo'li o'rniga cookies.txt'ning butun
  matnini to'g'ridan-to'g'ri muhit o'zgaruvchisiga qo'yish uchun (Secret File kerak bo'lmaydi -
  bot ishga tushganda buni vaqtinchalik faylga yozib oladi). `COOKIES_FILE` berilgan bo'lsa, bu
  e'tiborga olinmaydi.

## Ma'lum cheklovlar

YouTube 2025-2026 yillarda kiritilgan bot-aniqlash cheklovlari (PO token talabi) tufayli
qo'llab-quvvatlanmaydi - shu sababli musiqa qidiruv SoundCloud orqali ishlaydi, YouTube havolalari
esa "qo'llab-quvvatlanmaydi" deb chiqadi. Instagram vaqti-vaqti bilan datacenter IP'lardan
(Render kabi) kelgan so'rovlarni bloklaydi yoki login talab qiladi - bunday hollarda `COOKIES_FILE`
zarur bo'ladi. TikTok va Pinterest odatda muammosiz ishlaydi.

## Guruhda ishlashi uchun

Bot guruhga qo'shilganda **admin** qilib qo'yiladi. Telegram bot API'da admin bo'lgan botlar
"privacy mode" cheklovidan qat'i nazar guruhdagi barcha xabarlarni ko'ra oladi - shu sababli
`.id`, havola aniqlash va video/musiqa funksiyalari guruhda ishlashi uchun bu shart.

## Deploy (Render)

Bu repo'ni Web Service sifatida deploy qiling (Start Command: `python bot.py`). `runtime.txt`
Python versiyasini belgilaydi, `requirements.txt` esa kerakli kutubxonalarni. ffmpeg tizim
paketi sifatida o'rnatilishi shart emas - `imageio-ffmpeg` orqali statik binary avtomatik
yuklab olinadi.
