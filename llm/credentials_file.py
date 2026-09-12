"""Çalışan anahtarların JSON aynası — insan okuyabilsin diye.

NE İŞE YARAR: veritabanı tek dosyalık bir SQLite ve içindeki anahtarı
görmek için sorgu yazmak gerekiyor. Bu ayna, kaydedilmiş ve ÇALIŞTIĞI
doğrulanmış anahtarları taban URL'si, modeli ve ek gövdesiyle birlikte düz
metin JSON olarak tutuyor; sunucu sahibi dosyayı açıp okuyabiliyor
(kullanıcı isteği, 2026-09-12).

KAYNAK DOĞRU DEĞİL, AYNA: zamanlayıcı ve panel anahtarları HÂLÂ
veritabanından okuyor. Bu dosya yalnızca yazılıyor, hiç okunmuyor. Böyle
kurgulandı çünkü iki ayrı kaynağın ikisinden de okunması, biri
güncellenip diğeri kalınca sessiz bir kayma üretirdi — hangi anahtarın
gerçekten kullanıldığı belirsizleşirdi.

SİLME KODU BURADA DÜZ METİN. Veritabanında yalnızca kodun özeti var, yani
kodunu kaybeden kullanıcı anahtarını panelden silemez. Kurtarma yolu bu
dosya: sunucuya erişebilen kişi kodu buradan okur. Dosya zaten API
anahtarlarının kendisini taşıdığı için bu ek bir sır açığa çıkarmıyor.

DOSYA İZNİ 0600 ve `data/` altında (yani `.gitignore` kapsamında, imaja da
girmiyor). Yine de düz metin sır taşıyor: bu dosyayı kopyalayan, bütün
anahtarları kopyalamış olur.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Aynanın yolu. `DB_URL` gibi ortamdan değiştirilebiliyor ki testler
#: gerçek dosyaya dokunmasın.
PATH = Path(
    os.environ.get("LLM_CREDENTIALS_FILE") or (REPO_ROOT / "data" / "llm_credentials.json")
)


def _satir(cred, silme_kodu: str | None) -> dict:
    return {
        "id": cred.id,
        "api_key": cred.api_key,
        "base_url": cred.base_url,
        "model": cred.model,
        "extra_body": cred.extra_body,
        "status": cred.status,
        "created_at": cred.created_at.isoformat() if cred.created_at else None,
        "delete_code": silme_kodu,
    }


def write(creds, kodlar: dict[int, str] | None = None) -> Path | None:
    """Aynayı baştan yazar. Dönen: dosya yolu ya da None (yazılamadıysa).

    HATA YUTULUR: ayna yazılamadı diye anahtarın KAYDEDİLMESİ düşmemeli —
    veritabanı kaynak doğru, bu yalnızca kolaylık. Başarısızlık log'a
    WARNING olarak düşer.

    ÖNCE GEÇİCİ DOSYAYA, SONRA TAŞI: yarım yazılmış bir JSON, bir sonraki
    okumada ayrıştırılamaz ve kurtarma yolu olması gereken dosya
    kullanılamaz hale gelirdi.

    `kodlar`: id -> silme kodu. Yalnızca BU çağrıda üretilen kod elde
    olduğu için, önceki satırların kodları dosyadan geri okunup korunuyor;
    aksi halde her yeni kayıt eskilerin kurtarma kodunu silerdi.
    """
    kodlar = dict(kodlar or {})
    kodlar = {**_mevcut_kodlar(), **kodlar}
    govde = [_satir(c, kodlar.get(c.id)) for c in creds]
    try:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=PATH.parent, delete=False, suffix=".tmp"
        ) as f:
            json.dump(govde, f, ensure_ascii=False, indent=2)
            gecici = Path(f.name)
        # İzni TAŞIMADAN ÖNCE ver: dosya bir an bile herkese okunur
        # durumda durmasın.
        gecici.chmod(0o600)
        gecici.replace(PATH)
    except OSError as exc:  # noqa: BLE001 - ayna, koşuyu düşürmemeli
        logger.warning("anahtar aynası yazılamadı (%s): %s", PATH, exc)
        return None
    return PATH


def _mevcut_kodlar() -> dict[int, str]:
    """Dosyada duran silme kodları — yeniden yazarken kaybolmasınlar."""
    try:
        govde = json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(govde, list):
        return {}
    return {
        satir["id"]: satir["delete_code"]
        for satir in govde
        if isinstance(satir, dict) and satir.get("id") is not None and satir.get("delete_code")
    }


def read() -> list[dict]:
    """Aynayı okur — yalnızca testler ve elle inceleme için."""
    try:
        govde = json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return govde if isinstance(govde, list) else []
