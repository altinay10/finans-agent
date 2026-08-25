#!/usr/bin/env python3
"""cron entry noktası — tasarım dokümanı §09.

Kullanım: python worker.py <collector_adı|all>
Örn:      python worker.py fx_tcmb
          python worker.py all      # hepsini sırayla tazele
"""
from __future__ import annotations

import sys

from store.db import init_db
from store.logging_setup import setup_logging

setup_logging("worker")

COLLECTORS = {
    "fx_tcmb": lambda: __import__("collectors.fx_tcmb", fromlist=["TcmbCollector"]).TcmbCollector(),
    "fx_banks": lambda: __import__("collectors.fx_banks", fromlist=["BankFxCollector"]).BankFxCollector(),
    # Fon fiyatları: TEFAS EMEKLİYE AYRILDI (uç nokta 404 + robots.txt '/api/'
    # yasağı + WAF "Request Rejected" — üç bağımsız engel). Yerine Ak Portföy.
    # collectors/tefas.py referans olarak duruyor ama kayıtlı DEĞİL: çalıştırılamaz
    # bir toplayıcının tazelik şeridinde kalıcı kırmızı göstermesi yanıltıcı olur.
    # Çok sağlayıcılı: config/funds.yaml'daki her fon kendi portföy
    # şirketinin adaptöründen çekilir (collectors/fund_providers.py).
    "funds": lambda: __import__(
        "collectors.fund_prices", fromlist=["FundPriceCollector"]
    ).FundPriceCollector(),
    "deposits": lambda: __import__(
        "collectors.deposit_rates", fromlist=["DepositRateCollector"]
    ).DepositRateCollector(),
    "loan_rates": lambda: __import__(
        "collectors.loan_rates", fromlist=["LoanRateCollector"]
    ).LoanRateCollector(),
    # Katılım bankası kâr PAYLAŞIM oranları — faiz değil, ayrı tabloda
    # tutulur ve hesaplamaya girmez (bkz. collectors/profit_shares.py).
    "profit_shares": lambda: __import__(
        "collectors.profit_shares", fromlist=["EmlakKatilimProfitShareCollector"]
    ).EmlakKatilimProfitShareCollector(),
    "profit_shares_kt": lambda: __import__(
        "collectors.profit_shares", fromlist=["KuveytTurkProfitShareCollector"]
    ).KuveytTurkProfitShareCollector(),
    # Katılım bankalarının YILLIK kâr payı oranı — yukarıdakilerle
    # karıştırma: onlar paylaşım ORANI (%93), bunlar yıllık GETİRİ oranı
    # (%33,97) ve mevduatla aynı tabloda, aynı hesapla kıyaslanıyor.
    "participation_rates": lambda: __import__(
        "collectors.participation_rates", fromlist=["EmlakKatilimAnnualRateCollector"]
    ).EmlakKatilimAnnualRateCollector(),
    "participation_rates_kt": lambda: __import__(
        "collectors.participation_rates", fromlist=["KuveytTurkAnnualRateCollector"]
    ).KuveytTurkAnnualRateCollector(),
}


def _run_all() -> int:
    """Tüm toplayıcıları sırayla çalıştırır — günlük tazeleme için tek komut.

    Bir toplayıcının düşmesi diğerlerini durdurmaz; çıkış kodu, en az bir
    toplayıcı başarısız olduysa 1'dir. Cron'da her toplayıcıyı kendi saatinde
    çalıştırmak daha iyidir (bkz. deploy/crontab.example); bu komut elle
    tazeleme içindir.
    """
    failures = []
    for name in COLLECTORS:
        result = COLLECTORS[name]().run(trigger="manual")
        print(f"{name}: {result}")
        if not result.ok:
            failures.append(name)
    if failures:
        print(f"BAŞARISIZ: {', '.join(failures)}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    if len(sys.argv) != 2 or (sys.argv[1] != "all" and sys.argv[1] not in COLLECTORS):
        names = ", ".join(sorted(COLLECTORS))
        print(f"Kullanım: python worker.py <all|{names}>", file=sys.stderr)
        return 2

    init_db()

    # LLM çağrı bütçesi koşu başına sıfırlanır (bkz. llm/extract.py).
    from llm import extract as llm_extract

    llm_extract.reset_budget()

    if sys.argv[1] == "all":
        return _run_all()

    collector = COLLECTORS[sys.argv[1]]()
    result = collector.run(trigger="manual")
    print(result)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
