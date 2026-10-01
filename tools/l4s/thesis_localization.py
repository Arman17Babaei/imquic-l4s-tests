#!/usr/bin/env python3
"""Shared Persian/English text handling for thesis figures."""

from __future__ import annotations

import re

PERSIAN_RE = re.compile(r"[\u0600-\u06ff]")

ENGLISH_TRANSLATIONS = {
    "تاخیر صف (میلی‌ثانیه)": "Queue delay (ms)",
    "تأخیر صف (میلی‌ثانیه)": "Queue delay (ms)",
    "تابع توزیع تجمعی تجربی": "Empirical cumulative distribution function",
    "حالت جریان MoQ در اندازه‌گیری هم‌زیستی": "MoQ flow mode in the coexistence measurement",
    "گذردهی (مگابیت‌برثانیه)": "Throughput (Mbit/s)",
    "بهره‌برداری": "Utilization",
    "عدالت جین": "Jain fairness",
    "درصد": "Percent",
    "زمان از آغاز پنجرهٔ اندازه‌گیری (ثانیه؛ پنجرهٔ ۸ ثانیه‌ای)":
        "Time since measurement-window start (s; 8 s window)",
    "میانه تأخیر صف در بازه ۲۵۰ میلی‌ثانیه": "Median queue delay per 250 ms bin (ms)",
    "تعداد جریان‌های Cubic": "Number of Cubic flows",
    "گذردهی نرمال‌شده (مگابیت‌برثانیه)": "Normalized throughput (Mbit/s)",
    "تاخیر آمادگی تا تکمیل دریافت (ثانیه)": "Ready-to-receive-completion latency (s)",
    "سهم تجمعی تکمیل‌شده‌ها؛ نرمال‌شده با بیشترین تحویل":
        "Cumulative completion fraction; normalized by maximum delivery",
    "مهلت 45 ثانیه": "45 s deadline",
    "پایان دوره گذار پراگ": "End of Prague transition",
    "زمان از لحظهٔ تغییر ناگهانی دیدگاه (ثانیه)": "Time since abrupt viewpoint change (s)",
    "کلاسیک": "Classic",
    "فقط کلاسیک": "Classic only",
    "دو جریان، ۲۵٪ L4S": "Two flows, 25% L4S",
    "دو جریان، ۵۰٪ L4S": "Two flows, 50% L4S",
    "دو جریان، ۷۵٪ L4S": "Two flows, 75% L4S",
    "تک‌جریان L4S (مبنا)": "Single L4S flow (baseline)",
    "زمان از آماده‌شدن شیء (ثانیه)": "Time since object became ready (s)",
    "زمان از شروع بارکاری (ثانیه)": "Time since workload start (s)",
    "درصد وزنی بایت‌های مهم تکمیل‌شده (۲۵٪)":
        "Weighted percentage of important bytes completed (25%)",
    "باقی‌مانده داده کم‌اولویت، B / BDP": "Low-priority residual data, B / BDP",
    "بیشینه صرفه‌جویی ممکن (میلی‌ثانیه)": "Maximum possible saving (ms)",
    "B / پهنای‌باند (میلی‌ثانیه)": "B / Bandwidth (ms)",
    "B0 / پهنای‌باند (میلی‌ثانیه)": "B0 / Bandwidth (ms)",
    "بیشینه سود ممکن (میلی‌ثانیه)": "Maximum possible gain (ms)",
    "حد تحلیلی؛ نه اندازه‌گیری شبکه": "Analytical bound; not a network measurement",
    "مرز تخلیه کامل": "Complete-drain boundary",
    "داده کم‌اولویت هنگام ارسال": "Low-priority data at release",
    "فاصله تا تغییر اولویت": "Lead time until priority change",
    "فاصله تا تغییر اولویت (میلی‌ثانیه)": "Lead time until priority change (ms)",
    "حد بالای صرفه‌جویی (میلی‌ثانیه)": "Upper-bound saving (ms)",
    "مدل تخلیه با نرخ گلوگاه": "Bottleneck-rate drain model",
    "رخدادهای واقعی داخل شبکه": "Real in-network events",
    "باقی‌مانده واقعی در لحظه تغییر، B / BDP": "Actual residual at change, B / BDP",
    "CDF رخدادهای دارای باقی‌مانده": "CDF of events with residual backlog",
    "مرز شبکه کنترل‌شده": "Controlled-network boundary",
    "کنترل ازدحام TCP پس‌زمینه": "Background TCP congestion control",
    "سهم جریان MoQ از ترافیک گلوگاه": "MoQ share of bottleneck traffic",
    "کنترل ازدحام IMQUIC": "IMQUIC congestion control",
    "فرستنده": "Sender",
    "گیرنده": "Receiver",
    "سوییچ s1": "Switch s1",
    "سوییچ s2": "Switch s2",
    "گلوگاه مشترک": "Shared bottleneck",
    "روی هر دو خروجی": "On both egresses",
    "روی خروجی": "On egress",
    "خروجی رو به گیرنده": "Receiver-facing egress",
    "جریان رو به جلو": "Forward flow",
    "پس‌زمینه": "Background",
    "حالت کلاسیک": "Classic mode",
    "یک جریان 3DGS": "One 3DGS flow",
    "جریان پس‌زمینه": "Background flow",
    "دو جریان TCP هم‌زمان از گلوگاه میان‌سوییچی مشترک عبور می‌کنند":
        "Two concurrent TCP flows traverse the shared inter-switch bottleneck",
    "گلوگاه دوسویهٔ مشترک میان s1 و s2 با ظرفیت 10 Mbit/s در هر جهت":
        "Shared bidirectional bottleneck between s1 and s2: 10 Mbit/s each way",
    "در Linux، شکل‌دهی و AQM با qdisc اجرا می‌شود؛ در P4، برنامهٔ P4 روی هر دو سوییچ اجرا می‌شود":
        "Linux uses qdisc for shaping and AQM; P4 runs the P4 program on both switches",
    "؛": ";",
}


class FigureLocalizer:
    """Translate visible figure text while keeping technical labels intact."""

    def __init__(self, language: str) -> None:
        if language not in {"fa", "en"}:
            raise ValueError(f"unsupported figure language: {language}")
        self.language = language

    def __call__(self, text: str) -> str:
        if self.language == "fa":
            import arabic_reshaper
            from bidi.algorithm import get_display

            return get_display(arabic_reshaper.reshape(text))
        if not PERSIAN_RE.search(text):
            return text
        try:
            return ENGLISH_TRANSLATIONS[text]
        except KeyError as error:
            raise KeyError(f"missing English translation for: {text}") from error

    def stem(self, stem: str) -> str:
        if self.language == "fa":
            return stem
        if stem.endswith("-fa"):
            return stem[:-3] + "-en"
        return stem.replace("-fa.", "-en.", 1)
