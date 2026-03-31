from sqlalchemy import Column, Integer, String
from database import Base


class InventoryItem(Base):
    __tablename__ = "inventory_items"

    id            = Column(Integer, primary_key=True, index=True)
    supply_name   = Column(String, nullable=False)
    variety       = Column(String, nullable=True)
    quantity      = Column(Integer, nullable=False, default=0)
    date_received = Column(String, nullable=True)
    changed_by    = Column(String, nullable=True)


class GivenOutItem(Base):
    __tablename__ = "given_out_items"

    id           = Column(Integer, primary_key=True, index=True)
    supply_name  = Column(String, nullable=False)
    variety      = Column(String, nullable=True)
    quantity     = Column(Integer, nullable=False, default=0)
    who_received = Column(String, nullable=True)
    date_given   = Column(String, nullable=True)
    changed_by   = Column(String, nullable=True)


class TransactionLog(Base):
    """Append-only log — every add/edit is recorded here for the Summary page."""
    __tablename__ = "transaction_log"

    id           = Column(Integer, primary_key=True, index=True)
    txn_type     = Column(String, nullable=False)   # 'inventory' or 'given_out'
    supply_name  = Column(String, nullable=False)
    quantity     = Column(Integer, nullable=False)
    detail       = Column(String, nullable=True)    # date_received or who_received
    date_given   = Column(String, nullable=True)    # date_given for given_out rows
    changed_by   = Column(String, nullable=True)
    created_at   = Column(String, nullable=True)    # ISO datetime string
