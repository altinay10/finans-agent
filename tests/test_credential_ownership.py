"""Anahtar sahipliği, silme koruması ve JSON aynası.

NEDEN VAR: panelde kimlik doğrulama yok ve "Sil" düğmesi herkese açıktı;
paneli açabilen biri bütün anahtarları silip agent'ı tamamen durdurabiliyordu
(kullanıcı bildirimi, 2026-09-12). Buradaki testler korumanın gerçekten
koruduğunu ve kurtarma yolunun açık kaldığını kilitliyor.
"""
from __future__ import annotations

import os
import stat

import pytest

from llm import credentials, credentials_file


def _kaydet(anahtar="anahtar-1111", **kw):
    return credentials.save(
        anahtar,
        base_url=kw.get("base_url", "https://a.example/v1"),
        model=kw.get("model", "test-model"),
        extra_body=kw.get("extra_body"),
    )


# ------------------------------------------------------------ sahiplik ----

def test_a_stranger_cannot_delete_someone_elses_key(db):
    """KORUMANIN ASIL NOKTASI: kodu olmayan silemez.

    Kötü niyetli biri paneli açtığında bütün anahtarları silip sistemi
    durdurabiliyordu. Artık "Sil"e basmak yetmiyor.
    """
    cred, _kod = _kaydet()

    with pytest.raises(credentials.NotAuthorized):
        credentials.delete(cred.id)              # kodsuz
    with pytest.raises(credentials.NotAuthorized):
        credentials.delete(cred.id, "")          # boş kod
    with pytest.raises(credentials.NotAuthorized):
        credentials.delete(cred.id, "yanlis-kod")

    assert len(credentials.chain()) == 1, "anahtar silinmemeliydi"


def test_the_owner_can_delete_with_the_code(db):
    cred, kod = _kaydet()
    credentials.delete(cred.id, kod)
    assert credentials.chain() == []


def test_the_code_itself_is_never_stored_or_returned(db):
    """Sunucuda yalnızca ÖZET var; kod hiçbir okuma yolundan dönmüyor.

    Aksi halde veritabanını (ya da bir yedeği) okuyabilen biri bütün
    silme kodlarını ele geçirirdi ve koruma anlamsız olurdu.
    """
    cred, kod = _kaydet()

    kayit = credentials.chain()[0]
    assert kayit.owner_hash and kayit.owner_hash != kod
    assert kayit.owner_hash == credentials.hash_code(kod)
    # Dataclass'ın hiçbir alanında kodun kendisi geçmiyor.
    assert kod not in repr(kayit)


def test_each_key_gets_its_own_code(db):
    """Bir anahtarın kodu BAŞKA bir anahtarı silmeye yaramamalı."""
    birinci, kod1 = _kaydet("bir-1111")
    ikinci, _kod2 = _kaydet("iki-2222")

    with pytest.raises(credentials.NotAuthorized):
        credentials.delete(ikinci.id, kod1)
    assert len(credentials.chain()) == 2

    credentials.delete(birinci.id, kod1)
    assert [c.id for c in credentials.chain()] == [ikinci.id]


def test_keys_saved_before_this_protection_stay_deletable(db):
    """Sahibi bilinmeyen ESKİ satır kodsuz silinebilmeli.

    Aksi halde koruma eklendiği anda o satırlar panelde kalıcı olarak
    takılı kalırdı — kimse silemezdi.
    """
    from store.db import SessionLocal
    from store.models import LlmCredential
    from store.clock import utc_now

    with SessionLocal() as s:
        s.add(LlmCredential(
            api_key="eski-9999", base_url="https://a.example/v1", model="m",
            status="ok", created_at=utc_now(), owner_hash=None,
        ))
        s.commit()

    eski = credentials.chain()[0]
    assert eski.korumali is False
    credentials.delete(eski.id)
    assert credentials.chain() == []


# --------------------------------------------------------- JSON aynası ----

def test_saved_keys_are_mirrored_to_json(db):
    """Ayna anahtarı, sağlayıcısını ve silme kodunu taşımalı."""
    cred, kod = _kaydet("gizli-anahtar-4242", model="gemini-3.5-flash-lite",
                        extra_body={"enable_thinking": False})

    ayna = credentials_file.read()
    assert len(ayna) == 1
    satir = ayna[0]
    assert satir["api_key"] == "gizli-anahtar-4242"
    assert satir["base_url"] == "https://a.example/v1"
    assert satir["model"] == "gemini-3.5-flash-lite"
    # Ayna sözlük olarak saklıyor: dosyayı açan insan okuyabilsin diye,
    # JSON içine gömülü ikinci bir JSON metni olarak değil.
    assert satir["extra_body"] == {"enable_thinking": False}
    # KURTARMA YOLU: kodu kaybeden kullanıcı buradan okuyabilmeli.
    assert satir["delete_code"] == kod


def test_deleting_a_key_removes_it_from_the_mirror(db):
    cred, kod = _kaydet()
    credentials.delete(cred.id, kod)
    assert credentials_file.read() == []


def test_a_new_key_does_not_erase_earlier_delete_codes(db):
    """İkinci kaydın, birincinin kurtarma kodunu silmemesi gerekiyor.

    Ayna her yazışta baştan kuruluyor; önceki kodlar dosyadan geri
    okunmasaydı ilk anahtarın kurtarma yolu ikinci kayıtta yok olurdu.
    """
    birinci, kod1 = _kaydet("bir-1111")
    ikinci, kod2 = _kaydet("iki-2222")

    kodlar = {s["id"]: s["delete_code"] for s in credentials_file.read()}
    assert kodlar[birinci.id] == kod1
    assert kodlar[ikinci.id] == kod2


def test_the_mirror_is_not_world_readable(db):
    """Dosya düz metin API anahtarı taşıyor; izni 0600 olmalı."""
    _kaydet()
    mod = stat.S_IMODE(os.stat(credentials_file.PATH).st_mode)
    assert mod == 0o600, oct(mod)


def test_a_broken_mirror_does_not_block_saving(db, monkeypatch):
    """Ayna yazılamazsa anahtar YİNE DE kaydedilmeli.

    Kaynak doğru veritabanı; aynanın başarısızlığı kullanıcıyı anahtarsız
    bırakmamalı.
    """
    def _patla(*_a, **_k):
        raise OSError("disk dolu")

    monkeypatch.setattr(credentials_file, "write", _patla)
    cred, kod = _kaydet()
    assert credentials.chain()[0].id == cred.id
    assert kod
