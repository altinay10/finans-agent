"""`.env` dosyasına ayar yazma — panelden anahtar kaydı için.

NEDEN DOSYAYI BAŞTAN YAZMIYORUZ. `.env` yalnızca LLM anahtarını değil
DB_URL'i, saklama sürelerini ve log ayarlarını da taşıyor. Paneldeki bir
kutuya anahtar girip dosyayı komple yeniden üretmek, kullanıcının elle
yazdığı her şeyi sessizce silerdi. Bu yüzden satır satır düzenleniyor:
ilgili anahtarın satırı değişiyor, gerisi (yorumlar dahil) olduğu gibi
kalıyor.

DOSYA İZNİ. Yeni dosya 0600 ile açılıyor. Sunucuda `.env` gerçek bir sır
taşıyor; varsayılan umask ile 0644 kalması, aynı makinedeki her kullanıcıya
okutur.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = REPO_ROOT / ".env"


def read_values(path: Path | str | None = None) -> dict[str, str]:
    """Dosyadaki ham `AD=değer` çiftleri (yorumlar ve boş satırlar atılır)."""
    p = Path(path or DEFAULT_PATH)
    if not p.exists():
        return {}
    values: dict[str, str] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        values[name.strip()] = value.strip()
    return values


def set_values(values: dict[str, str], path: Path | str | None = None) -> Path:
    """Verilen anahtarları yazar/günceller, dosyanın gerisine dokunmaz."""
    for name, value in values.items():
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
            raise ValueError(f"geçersiz ayar adı: {name!r}")
        # Değere kaçan bir satır sonu, dosyayı ikiye bölüp sonraki satırı
        # yeni bir ayar gibi gösterirdi.
        if "\n" in value or "\r" in value:
            raise ValueError(f"{name}: değer satır sonu içeremez")

    p = Path(path or DEFAULT_PATH)
    lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    kalan = dict(values)

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name = stripped.partition("=")[0].strip()
        if name in kalan:
            lines[i] = f"{name}={kalan.pop(name)}"

    if kalan:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("# panelden eklendi")
        lines.extend(f"{name}={value}" for name, value in kalan.items())

    yeni = not p.exists()
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if yeni:
        os.chmod(p, 0o600)
    return p
