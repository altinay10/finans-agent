"""Anahtar silme yetkisi ve JSON aynası.

NEDEN BÖYLE: panelde kimlik doğrulama yok. Bir dönem "yalnızca ekleyen
silebilir" kuralı denendi (silme kodu + özet) ve YETMEDİ: kural eklenmeden
ÖNCE kaydedilmiş satırlara muafiyet tanıyordu, çünkü aksi halde onları
kimse silemezdi. Canlıdaki tek anahtar tam olarak öyle bir satırdı ve
kodsuz silindi (2026-09-13).

Ders şu: panel açıkken İSTEMCİYE BAKAN her silme yolu benzer bir delik
taşıyor. Şimdiki kural deliksiz çünkü basit — PANELDEN HİÇ SİLİNEMEZ.
Silme yalnızca sunucudaki komut satırından yapılıyor, yani silebilmek için
makineye erişmek gerekiyor.
"""
from __future__ import annotations

import os
import stat

from llm import credentials, credentials_file


def _kaydet(anahtar="anahtar-1111", **kw):
    return credentials.save(
        anahtar,
        base_url=kw.get("base_url", "https://a.example/v1"),
        model=kw.get("model", "test-model"),
        extra_body=kw.get("extra_body"),
    )


# ------------------------------------------------- panelde silme YOK ----

def test_the_panel_has_no_delete_control_at_all():
    """Panelde silme düğmesi ÇİZİLMEMELİ — korumanın tamamı bu.

    Kaynağa bakılıyor çünkü kilitlenen şey bir davranış değil, bir şeyin
    YOKLUĞU: `agent.py` hiçbir koşulda `credentials.delete` çağırmamalı.
    Geri sızan tek bir düğme, paneli açan herkesin agent'ı durdurabilmesi
    demek.
    """
    from pathlib import Path

    kaynak = (
        Path(__file__).resolve().parent.parent / "app" / "panels" / "agent.py"
    ).read_text(encoding="utf-8")
    assert "credentials.delete" not in kaynak
    assert "_render_delete" not in kaynak


def test_deleting_from_the_server_works_and_reports_what_it_did(db):
    """Sunucu tarafı silme çalışmalı ve bulamadığını söylemeli."""
    cred = _kaydet()

    assert credentials.delete(cred.id) is True
    assert credentials.chain() == []
    # Olmayan bir id sessizce "başarılı" dememeli: yanlış id yazan kişi
    # sildiğini sanıp devam ederdi.
    assert credentials.delete(cred.id) is False


def test_the_cli_lists_and_deletes(db, capsys):
    """`worker.py keys` listeler, `keys rm <id>` siler."""
    import worker

    cred = _kaydet("gizli-anahtar-4242", model="gemini-3.5-flash-lite")

    assert worker._keys(["keys"]) == 0
    cikti = capsys.readouterr().out
    assert f"#{cred.id}" in cikti
    assert "gemini-3.5-flash-lite" in cikti
    # ANAHTAR TAM YAZDIRILMAZ: komutun çıktısı terminal kaydına düşebilir.
    assert "gizli-anahtar-4242" not in cikti
    assert "…4242" in cikti

    assert worker._keys(["keys", "rm", str(cred.id)]) == 0
    assert credentials.chain() == []


def test_the_cli_refuses_a_bad_id(db, capsys):
    """Hatalı kullanımda sessizce hiçbir şey yapmamalı, hata dönmeli."""
    import worker

    assert worker._keys(["keys", "rm", "abc"]) == 2      # sayı değil
    assert worker._keys(["keys", "rm", "999"]) == 1      # yok
    assert worker._keys(["keys", "sil", "1"]) == 2       # bilinmeyen alt komut


# --------------------------------------------------------- JSON aynası ----

def test_saved_keys_are_mirrored_to_json(db):
    _kaydet("gizli-anahtar-4242", model="gemini-3.5-flash-lite",
            extra_body={"enable_thinking": False})

    ayna = credentials_file.read()
    assert len(ayna) == 1
    satir = ayna[0]
    assert satir["api_key"] == "gizli-anahtar-4242"
    assert satir["base_url"] == "https://a.example/v1"
    assert satir["model"] == "gemini-3.5-flash-lite"
    assert satir["extra_body"] == {"enable_thinking": False}


def test_deleting_a_key_removes_it_from_the_mirror(db):
    cred = _kaydet()
    credentials.delete(cred.id)
    assert credentials_file.read() == []


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
    cred = _kaydet()
    assert credentials.chain()[0].id == cred.id
