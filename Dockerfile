# Tek imaj iki rolü de koşar: panel (varsayılan) ve worker (entrypoint ile).
FROM python:3.12-slim

# TZ önemli: valid_date Türkiye takvim gününe göre yazılır. store/clock.py
# bunu saat diliminden bağımsız olarak zaten garantiliyor, ama log'lar ve
# cron da yerel saatte okunabilsin diye konteyner de İstanbul'a ayarlanıyor.
ENV TZ=Europe/Istanbul \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata curl \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Bağımlılıklar önce: kod değiştiğinde pip katmanı yeniden kurulmaz.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Root olarak koşma. data/ dizini volume olarak bağlanacağı için sahipliği
# imaj içinde ayarlanır.
RUN useradd --create-home --uid 10001 finans \
    && mkdir -p /app/data/snapshots /app/data/logs \
    && chown -R finans:finans /app
USER finans

EXPOSE 8501

# Streamlit'in kendi sağlık uç noktası; compose bunu bekler.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8501/_stcore/health || exit 1

# ROLE varsayılanı "all": tek konteyner çalıştırıldığında ZAMANLAYICI DA
# kalkar. Eskiden CMD doğrudan streamlit'ti ve `docker run imaj` diyen
# kullanıcı yalnızca paneli ayağa kaldırıp hiç veri toplamıyordu — sunucuda
# en sık yaşanan arıza buydu. Bkz. run.py.
ENV ROLE=all
CMD ["python", "run.py"]
