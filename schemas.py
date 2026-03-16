from pydantic import BaseModel
from typing import Optional


class InventoryItemCreate(BaseModel):
    supply_name:   str
    quantity:      int
    date_received: Optional[str] = None
    changed_by:    Optional[str] = None


class InventoryItem(InventoryItemCreate):
    id: int
    model_config = {"from_attributes": True}


class GivenOutItemCreate(BaseModel):
    supply_name:  str
    quantity:     int
    who_received: Optional[str] = None
    date_given:   Optional[str] = None
    changed_by:   Optional[str] = None


class GivenOutItem(GivenOutItemCreate):
    id: int
    model_config = {"from_attributes": True}
