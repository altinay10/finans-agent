"""Var olan tablolara yeni sütun ekleme — küçük ve KASITLI olarak sınırlı.

Neden gerekiyor: `Base.metadata.create_all()` yalnızca OLMAYAN tabloyu
yaratır. Var olan bir tabloya sütun eklemez. Bu projede veritabanı dosyası
sunucuda kalıcıdır (docker named volume); şemaya yeni bir sütun eklendiğinde
uygulama açılışta `no such column` ile patlar ve kullanıcının elinde ne
migration aracı ne de veriyi kurtaracak bir yol olur.

Neden Alembic değil: Alembic'in tuttuğu revizyon zinciri, tek dosyalık bir
SQLite'ı olan tek kullanıcılık bir panelde taşıma maliyeti getiriyor. Buradaki
ihtiyaç tek bir işlem: "modelde olup tabloda olmayan sütunu ekle". Bu işlem
SQLite'ta `ALTER TABLE ... ADD COLUMN` ile atomiktir ve veriyi taşımaz.

SINIR — bilerek YAPMADIKLARI:
  * sütun silmez, yeniden adlandırmaz, tipini değiştirmez
  * NOT NULL sütun ekleyemez (SQLite varsayılansız NOT NULL eklemeye izin
    vermez); bu yüzden yeni sütunlar nullable ya da varsayılanlı olmalı
  * veri dönüştürmez
Bu üçünden biri gerekiyorsa elle bir migration yazılmalıdır. O gün geldiğinde
bu dosya sessizce yanlış bir şey yapmasın diye kapsamı burada yazılı.
"""
from __future__ import annotations

import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from store.models import Base

logger = logging.getLogger(__name__)


def _sqlite_type(column) -> str:
    """SQLAlchemy tipini SQLite DDL karşılığına çevirir."""
    try:
        return column.type.compile(dialect=Base.metadata.bind.dialect)  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 - bind yoksa jenerik derleme yeterli
        from sqlalchemy.dialects import sqlite

        return column.type.compile(dialect=sqlite.dialect())


def add_missing_columns(engine: Engine) -> list[str]:
    """Modelde tanımlı olup tabloda bulunmayan sütunları ekler.

    Dönen liste 'tablo.sütun' biçimindedir; boşsa şema zaten günceldi.
    """
    if engine.url.get_backend_name() != "sqlite":
        # Postgres'e geçildiğinde gerçek bir migration aracı kullanılmalı;
        # sessizce yanlış bir şey yapmaktansa hiçbir şey yapma.
        return []

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    added: list[str] = []

    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue  # create_all yaratacak
            have = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in have:
                    continue
                if not column.nullable and column.default is None and column.server_default is None:
                    logger.error(
                        "%s.%s eklenemedi: NOT NULL sütun varsayılansız eklenemez; "
                        "elle migration gerekiyor",
                        table.name,
                        column.name,
                    )
                    continue
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {_sqlite_type(column)}'
                default = column.default
                if default is not None and getattr(default, "is_scalar", False):
                    literal = default.arg
                    if isinstance(literal, bool):
                        literal = int(literal)
                    if isinstance(literal, str):
                        literal = f"'{literal}'"
                    ddl += f" DEFAULT {literal}"
                conn.execute(text(ddl))
                added.append(f"{table.name}.{column.name}")
                logger.info("şema güncellendi: %s", added[-1])

    return added
