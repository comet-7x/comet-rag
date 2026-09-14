"""SQLAlchemy 声明式基类与共用列类型。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, MetaData
from sqlalchemy.orm import DeclarativeBase, mapped_column

#: 显式命名约定。不定的话，Alembic 自动生成的迁移里约束名由数据库随机生成，
#: 日后想 drop 某个约束时你根本不知道它叫什么 —— 这是迁移最常见的死角。
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def timestamp_column(**kwargs):
    """带时区的时间列。

    一律用 `timezone=True`：naive datetime 落库后，跨时区部署或夏令时切换时
    会算错，而且错得毫无痕迹。
    """
    return mapped_column(DateTime(timezone=True), **kwargs)


__all__ = ["Base", "NAMING_CONVENTION", "datetime", "timestamp_column"]
