from sqlalchemy import Column, Integer, String
from database import Base


class InventoryItem(Base):
    __tablename__ = "inventory_items"

    id            = Column(Integer, primary_key=True, index=True)
    supply_name   = Column(String, nullable=False)
    quantity      = Column(Integer, nullable=False, default=0)
    date_received = Column(String, nullable=True)
    changed_by    = Column(String, nullable=True)  # last editor name


class GivenOutItem(Base):
    __tablename__ = "given_out_items"

    id           = Column(Integer, primary_key=True, index=True)
    supply_name  = Column(String, nullable=False)
    quantity     = Column(Integer, nullable=False, default=0)
    who_received = Column(String, nullable=True)
    date_given   = Column(String, nullable=True)
    changed_by   = Column(String, nullable=True)  # last editor name
