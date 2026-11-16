# WooCommerce Image Replacer

**Replace one shared image across many WooCommerce product galleries — safely, through the REST API, with no plugin and no hosting access.**

A Windows desktop tool (Python + pywebview) with a Persian interface. You pick the old shared image and its replacement; the tool finds every product that uses the old image, shows a before/after preview, and only after your explicit confirmation swaps that one image reference in the products you selected.

## Download / دریافت

**[⬇ WooCommerceImageReplacer.exe — latest release](https://github.com/mahdiPourkazemi/woocommerce-image-replacer/releases/latest)** (Windows 10/11, 64-bit)

<div dir="rtl">

فایل اجرایی ویندوز آماده است و به نصب پایتون نیاز ندارد:

1. از [صفحهٔ آخرین نسخه](https://github.com/mahdiPourkazemi/woocommerce-image-replacer/releases/latest) فایل `WooCommerceImageReplacer.exe` و `default.env.example` را بگیرید و در یک پوشه بگذارید.
2. نام `default.env.example` را به `.env` تغییر دهید و آدرس فروشگاه و کلید ووکامرس را در آن بنویسید.
3. برنامه را اجرا کنید. برای تمرین بدون کلید و بدون اینترنت، دکمهٔ «حالت نمایشی» را بزنید.

فایل اجرایی امضای دیجیتال ندارد؛ ممکن است ویندوز در اولین اجرا هشدار SmartScreen نشان دهد («More info» و سپس «Run anyway»). اگر ترجیح می‌دهید، می‌توانید آن را با دستور بخش «ساخت فایل اجرایی» خودتان از روی کد بسازید.

## این ابزار چه کاری می‌کند؟

فرض کنید یک عکس (مثلاً «راهنمای اندازه») در گالری ده‌ها یا صدها محصول ووکامرس تکرار شده و حالا باید با عکس تازه‌ای عوض شود. این برنامه:

1. محصولاتی را که آن عکس را دارند پیدا می‌کند؛
2. قبل و بعدِ گالری هر محصول را نشان می‌دهد؛
3. پس از تأیید شما، فقط همان عکس را در محصولات انتخاب‌شده عوض می‌کند.

فقط فیلد تصاویر محصول ویرایش می‌شود. قیمت، نام، موجودی، توضیحات و دسته‌بندی دست نمی‌خورند و هیچ عکسی از کتابخانهٔ سایت حذف نمی‌شود.

## برای چه کسی مناسب است؟

- به API ووکامرس دسترسی دارید، اما اجازهٔ نصب افزونه یا دسترسی به فایل‌های هاست ندارید.
- سایت کند است و نمی‌خواهید با درخواست‌های هم‌زمان به آن فشار بیاید.
- می‌خواهید پیش از هر تغییر، دقیقاً ببینید چه چیزی عوض می‌شود.

## ویژگی‌ها

- **تشخیص دقیق با شناسهٔ رسانه**، نه نام فایل یا شباهت تصویر؛ ترتیب و تعداد بقیهٔ عکس‌ها ثابت می‌ماند.
- **پیش‌نمایش قبل و بعد** با امکان کنارگذاشتن هر محصول.
- **ملایم با سایت کند:** یک درخواست در هر لحظه، صفحه‌های ۲۰ تایی، فاصله بین محصولات.
- **مقاوم در برابر قطعی:** اگر پاسخ نوشتن نرسد، برنامه اول محصول را دوباره می‌خواند و بعد تصمیم می‌گیرد؛ نوشتن کور تکرار نمی‌شود.
- **صف پایدار:** وضعیت در SQLite ذخیره می‌شود؛ با بسته‌شدن ناگهانی برنامه، کار از همان‌جا قابل ادامه است.
- **احترام به ویرایش دیگران:** اگر گالری محصولی پس از پیش‌نمایش عوض شده باشد، بازنویسی نمی‌شود و به «نیازمند بررسی» می‌رود.
- **برگشت مشروط:** فقط محصولات موفقی که هنوز دست‌نخورده‌اند به حالت قبل برمی‌گردند.
- **بارگذاری یک‌باره:** عکس جدید حتی پس از قطعی، دوباره و دوباره بارگذاری نمی‌شود.
- **حالت نمایشی آفلاین** با ۴۵ محصول ساختگی، برای تمرین بدون کلید و بدون خطر.
- **بدون راز در کد:** کلیدها فقط از فایل `.env` بیرونی خوانده می‌شوند و در پنجره، لاگ، گزارش یا فایل اجرایی نیستند.

## شروع سریع

پایتون ۳.۱۱ یا جدیدتر و ویندوز با Microsoft Edge WebView2 Runtime لازم است (روی ویندوز ۱۱ از قبل نصب است).

</div>

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe woocommerce_image_replacer.py --demo
```

<div dir="rtl">

دستور بالا حالت نمایشی را باز می‌کند. برای اتصال به فروشگاه خودتان، `.env.example` را با نام `.env` کپی و تکمیل کنید و برنامه را بدون `--demo` اجرا کنید.

| متغیر | توضیح |
|---|---|
| `WC_BASE_URL` | آدرس فروشگاه، فقط با `https://` |
| `WC_CONSUMER_KEY` / `WC_CONSUMER_SECRET` | کلید REST ووکامرس با دسترسی خواندن و نوشتن |
| `WP_USERNAME` / `WP_APPLICATION_PASSWORD` | اختیاری؛ فقط برای بارگذاری عکس جدید از داخل برنامه |

**پیشنهاد برای اولین اجرای واقعی:** اول در حالت نمایشی تمرین کنید، سپس روی سایت فقط یک محصول را انتخاب و نتیجه را بررسی کنید، و بعد بقیه را اجرا کنید.

راهنمای کامل فارسی: [docs/GUIDE_FA.md](docs/GUIDE_FA.md)

## ساخت فایل اجرایی ویندوز

</div>

```powershell
.\.venv\Scripts\python.exe -m PyInstaller WooCommerceImageReplacer.spec --noconfirm
.\dist\WooCommerceImageReplacer.exe --self-check check.json
```

<div dir="rtl">

فایل `.env` در exe بسته‌بندی نمی‌شود و باید کنار آن قرار بگیرد.

## تست‌ها

</div>

```powershell
.\.venv\Scripts\python.exe -m pytest app/test -q
```

<div dir="rtl">

۱۱۵ تست، همه با پاسخ شبیه‌سازی‌شده و پایگاه دادهٔ موقت؛ هیچ تستی به سایت واقعی وصل نمی‌شود.

## محدودیت‌ها

- تصاویر مستقل تنوع‌ها (variation)، عکس‌های داخل متن توضیحات، بنرها و تصاویر دسته‌بندی تغییر نمی‌کنند.
- اگر عکس قدیمی در جایگاه اول گالری باشد، تصویر اصلی محصول هم عوض می‌شود (در پیش‌نمایش علامت دارد).
- API ووکامرس نوشتن شرطی ندارد؛ اگر شخص دیگری دقیقاً بین خواندن و نوشتن همان محصول را ویرایش کند، احتمال تداخل صفر نیست. زمان خلوت را انتخاب کنید.
- مجوز نوشتنِ کلید هنگام اتصال آزمایش نمی‌شود، چون آزمودنش یعنی ویرایش سایت.
- رابط کاربری فقط فارسی است و نسخهٔ دسکتاپ فقط روی ویندوز آزمایش شده است.

## ساختار کد

| فایل | مسئولیت |
|---|---|
| `woocommerce_image_replacer.py` | ورودی برنامه و پنجره |
| `app/image_replacer/core.py` | منطق خالص: خواندن گالری، پیش‌نمایش، اسکن |
| `app/image_replacer/engine.py` | صف نوشتن، تلاش مجدد، تعیین تکلیف قطعی |
| `app/image_replacer/journal.py` | ذخیرهٔ پایدار کارها در SQLite |
| `app/image_replacer/transport.py` | تماس با API ووکامرس و وردپرس |
| `app/image_replacer/media.py` | بارگذاری یک‌بارهٔ عکس |
| `app/image_replacer/bridge.py` | پل بین صفحه و موتور؛ نگهبان تأیید صریح |
| `app/image_replacer/demo.py` | فروشگاه نمایشی آفلاین |
| `app/image_replacer/ui/index.html` | رابط کاربری |

</div>

## How it stays safe (English summary)

- **Read before write:** each product is re-read immediately before its `PUT`; if its gallery differs from the approved preview it is set aside for review instead of being overwritten.
- **Images field only:** the request body is `{"images": [{"id": …}, …]}` with the complete ordered gallery; nothing else is sent.
- **No blind retries:** after a timeout the product is read back to learn whether the write landed; the write is repeated only if the gallery still equals the "before" snapshot.
- **Durable queue:** before/after snapshots are committed to SQLite before the HTTP call, so a crash can be reconciled on restart.
- **Explicit confirmation:** connecting, searching, scanning and previewing are read-only; a queue starts only from a confirmed action.
- **No deletes:** the tool has no code path that deletes media or creates products.

This is an independent tool and is not affiliated with WooCommerce or Automattic.
