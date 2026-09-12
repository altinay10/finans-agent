"""Sağlayıcı hazır ayarları — taban URL, örnek model ve EK GÖVDE.

NEDEN VAR: anahtar giriş formu `extra_body` kutusunu o an geçerli olan
ayardan dolduruyordu. `.env`'de Qwen için `{"enable_thinking": false}`
yazdığı için, kullanıcı GEMINI anahtarı denediğinde o Qwen'e özel alan da
isteğe ekleniyor ve sağlayıcı isteği reddediyordu. Anahtar doğru olsa bile
"anahtar çalışmadı" hatası alınıyordu; kullanıcının gördüğü buydu
(kullanıcı bildirimi, 2026-09-12).

EK GÖVDE SAĞLAYICIYA ÖZELDİR VE ÇOĞUNDA BOŞTUR. OpenAI uyumlu uç
noktaların tamamı standart şemayı kabul ediyor; `extra_body` yalnızca o
şemada KARŞILIĞI OLMAYAN bir ayar gerektiğinde doldurulur. Bugün listede
böyle tek bir durum var (Qwen'in düşünme modu). Diğerlerine boş sözlük
göndermek hem doğru hem de güvenli; dolu göndermek değil.

MODEL ADLARI ÖNERİDİR, KURAL DEĞİL. Sağlayıcılar model adlarını sık
değiştiriyor ve bu dosya onları takip edemez. Kutular düzenlenebilir ve
kaydetmeden önce anahtar CANLI deneniyor (bkz. `credentials.test_credential`),
yani yanlış bir model adı sessizce kaydedilemiyor — hata anında görünür.
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: Hazır ayar seçilmediğinde kullanılan etiket.
CUSTOM = "Diğer / elle"


@dataclass(frozen=True)
class Provider:
    label: str
    base_url: str
    model: str
    extra_body: dict = field(default_factory=dict)
    note: str = ""


PROVIDERS: tuple[Provider, ...] = (
    Provider(
        label="Google Gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        model="gemini-3.5-flash-lite",
        extra_body={},
        note=(
            "Google'ın OpenAI uyumluluk katmanı. Ek gövde GEREKMİYOR — "
            "buraya Qwen'in `enable_thinking` alanını bırakırsan istek "
            "reddedilir."
        ),
    ),
    Provider(
        label="OpenAI (ChatGPT)",
        base_url="https://api.openai.com/v1",
        model="gpt-4o-mini",
        extra_body={},
        note="Yerel şema; ek gövde gerekmiyor.",
    ),
    Provider(
        label="Anthropic (Claude)",
        base_url="https://api.anthropic.com/v1/",
        model="claude-haiku-4-5-20251001",
        extra_body={},
        note=(
            "Anthropic'in OpenAI uyumluluk katmanı. Ek gövde gerekmiyor; "
            "uyumluluk katmanı OpenAI şemasının dışındaki alanları yok sayar."
        ),
    ),
    Provider(
        label="Qwen (Alibaba DashScope)",
        base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        model="qwen-flash",
        extra_body={"enable_thinking": False},
        note=(
            "LİSTEDE EK GÖVDE GEREKTİREN TEK SAĞLAYICI. `enable_thinking` "
            "varsayılan olarak AÇIK ve çıkarım işinde boşuna token yakıyor; "
            "ayrıca akışsız (non-streaming) çağrılarda bazı qwen3 modelleri "
            "bu alan olmadan hata veriyor."
        ),
    ),
    Provider(
        label="DeepSeek",
        base_url="https://api.deepseek.com/v1",
        model="deepseek-chat",
        extra_body={},
        note="OpenAI uyumlu; ek gövde gerekmiyor.",
    ),
    Provider(
        label=CUSTOM,
        base_url="",
        model="",
        extra_body={},
        note=(
            "Kutuları kendin doldur. Ek gövdeyi ancak sağlayıcının belgesi "
            "OpenAI şemasında olmayan bir alan istiyorsa doldur; şüphedeysen "
            "BOŞ bırak."
        ),
    ),
)

LABELS: tuple[str, ...] = tuple(p.label for p in PROVIDERS)

_BY_LABEL = {p.label: p for p in PROVIDERS}


def by_label(label: str) -> Provider:
    """Etikete göre hazır ayar; bilinmeyen etikette 'Diğer / elle' döner."""
    return _BY_LABEL.get(label, _BY_LABEL[CUSTOM])


def guess_label(base_url: str) -> str:
    """Taban URL'ye bakarak hangi sağlayıcı olduğunu tahmin eder.

    Form açılırken o an geçerli olan ayarın hangi sağlayıcıya ait olduğunu
    seçili göstermek için. Eşleşme bulunamazsa 'Diğer / elle' — yanlış bir
    sağlayıcıyı seçili göstermektense seçimsiz bırakmak doğru.
    """
    temiz = (base_url or "").strip().rstrip("/").lower()
    if not temiz:
        return CUSTOM
    for p in PROVIDERS:
        if p.label == CUSTOM:
            continue
        if temiz == p.base_url.rstrip("/").lower():
            return p.label
    # Uç nokta sürümü değişmiş olabilir (v1 -> v1beta); ana bilgisayar adı
    # yine de sağlayıcıyı belirler.
    for p in PROVIDERS:
        if p.label == CUSTOM:
            continue
        host = p.base_url.split("//", 1)[-1].split("/", 1)[0].lower()
        if host and host in temiz:
            return p.label
    return CUSTOM
