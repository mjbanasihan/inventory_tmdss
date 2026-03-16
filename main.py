from fastapi import FastAPI, HTTPException, Depends
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import List
import os

from database import engine, get_db, Base
import models
import schemas

# Verify we're using PostgreSQL on Render (not SQLite)
import sys
db_url = str(engine.url)
if "sqlite" in db_url:
    print("WARNING: Running on SQLite — data will not persist across deploys!", file=sys.stderr)

# Create tables on startup (safe — never drops existing data)
Base.metadata.create_all(bind=engine)

# ── Migrations ───────────────────────────────────────────────────────────────
def run_migrations():
    migrations = [
        "ALTER TABLE given_out_items ADD COLUMN date_given VARCHAR",
        "ALTER TABLE given_out_items ADD COLUMN changed_by VARCHAR",
        "ALTER TABLE inventory_items ADD COLUMN changed_by VARCHAR",
    ]
    with engine.connect() as conn:
        for sql in migrations:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception:
                pass  # Column already exists

run_migrations()

app = FastAPI(
    title="TSD-TMDSS Inventory API",
    description="Inventory management system for TSD-TMDSS",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── CURRENT INVENTORY ROUTES ────────────────────────────────────────────────

@app.get("/api/inventory", response_model=List[schemas.InventoryItem])
def get_inventory(search: str = "", db: Session = Depends(get_db)):
    query = db.query(models.InventoryItem)
    if search:
        query = query.filter(models.InventoryItem.supply_name.ilike(f"%{search}%"))
    return query.order_by(models.InventoryItem.id).all()


@app.post("/api/inventory", response_model=schemas.InventoryItem, status_code=201)
def create_inventory_item(item: schemas.InventoryItemCreate, db: Session = Depends(get_db)):
    # If supply already exists (case-insensitive), increment its quantity instead
    existing = db.query(models.InventoryItem).filter(
        models.InventoryItem.supply_name.ilike(item.supply_name)
    ).first()
    if existing:
        existing.quantity += item.quantity
        if item.date_received:
            existing.date_received = item.date_received
        db.commit()
        db.refresh(existing)
        return existing
    db_item = models.InventoryItem(**item.model_dump())
    db.add(db_item)
    db.commit()
    db.refresh(db_item)
    return db_item


@app.put("/api/inventory/{item_id}", response_model=schemas.InventoryItem)
def update_inventory_item(item_id: int, item: schemas.InventoryItemCreate, db: Session = Depends(get_db)):
    db_item = db.query(models.InventoryItem).filter(models.InventoryItem.id == item_id).first()
    if not db_item:
        raise HTTPException(status_code=404, detail="Item not found")
    for key, value in item.model_dump().items():
        setattr(db_item, key, value)
    db.commit()
    db.refresh(db_item)
    return db_item


@app.delete("/api/inventory/{item_id}", status_code=204)
def delete_inventory_item(item_id: int, db: Session = Depends(get_db)):
    db_item = db.query(models.InventoryItem).filter(models.InventoryItem.id == item_id).first()
    if not db_item:
        raise HTTPException(status_code=404, detail="Item not found")
    db.delete(db_item)
    db.commit()


# ─── SUPPLY GIVEN OUT ROUTES ──────────────────────────────────────────────────

@app.get("/api/given-out", response_model=List[schemas.GivenOutItem])
def get_given_out(search: str = "", db: Session = Depends(get_db)):
    query = db.query(models.GivenOutItem)
    if search:
        query = query.filter(models.GivenOutItem.supply_name.ilike(f"%{search}%"))
    return query.order_by(models.GivenOutItem.id).all()


@app.post("/api/given-out", response_model=schemas.GivenOutItem, status_code=201)
def create_given_out_item(item: schemas.GivenOutItemCreate, db: Session = Depends(get_db)):
    # Check inventory has enough stock
    inv_item = db.query(models.InventoryItem).filter(
        models.InventoryItem.supply_name.ilike(item.supply_name)
    ).first()
    if not inv_item:
        raise HTTPException(status_code=400, detail=f"'{item.supply_name}' not found in inventory")
    if inv_item.quantity < item.quantity:
        raise HTTPException(
            status_code=400,
            detail=f"Only {inv_item.quantity} unit(s) available for '{item.supply_name}'"
        )

    # Deduct from inventory
    inv_item.quantity -= item.quantity
    if inv_item.quantity == 0:
        db.delete(inv_item)   # remove from inventory when fully depleted

    # Upsert given-out: combine if same supply + same recipient
    existing = db.query(models.GivenOutItem).filter(
        models.GivenOutItem.supply_name.ilike(item.supply_name),
        models.GivenOutItem.who_received == item.who_received
    ).first()
    if existing:
        existing.quantity += item.quantity
        if item.date_given:
            existing.date_given = item.date_given
        db.commit()
        db.refresh(existing)
        return existing

    db_item = models.GivenOutItem(**item.model_dump())
    db.add(db_item)
    db.commit()
    db.refresh(db_item)
    return db_item


@app.put("/api/given-out/{item_id}", response_model=schemas.GivenOutItem)
def update_given_out_item(item_id: int, item: schemas.GivenOutItemCreate, db: Session = Depends(get_db)):
    db_item = db.query(models.GivenOutItem).filter(models.GivenOutItem.id == item_id).first()
    if not db_item:
        raise HTTPException(status_code=404, detail="Item not found")

    qty_diff = item.quantity - db_item.quantity  # positive = more given out, negative = returning

    if qty_diff != 0:
        inv_item = db.query(models.InventoryItem).filter(
            models.InventoryItem.supply_name.ilike(item.supply_name)
        ).first()
        if qty_diff > 0:
            # Giving out more — check stock
            if not inv_item or inv_item.quantity < qty_diff:
                available = inv_item.quantity if inv_item else 0
                raise HTTPException(status_code=400, detail=f"Only {available} unit(s) available")
            inv_item.quantity -= qty_diff
            if inv_item.quantity == 0:
                db.delete(inv_item)
        else:
            # Returning units back to inventory
            if inv_item:
                inv_item.quantity += abs(qty_diff)
            else:
                # Re-create inventory row if it was fully depleted
                restored = models.InventoryItem(
                    supply_name=item.supply_name,
                    quantity=abs(qty_diff)
                )
                db.add(restored)

    for key, value in item.model_dump().items():
        setattr(db_item, key, value)
    db.commit()
    db.refresh(db_item)
    return db_item


@app.delete("/api/given-out/{item_id}", status_code=204)
def delete_given_out_item(item_id: int, db: Session = Depends(get_db)):
    db_item = db.query(models.GivenOutItem).filter(models.GivenOutItem.id == item_id).first()
    if not db_item:
        raise HTTPException(status_code=404, detail="Item not found")

    # Restore quantity back to inventory on delete
    inv_item = db.query(models.InventoryItem).filter(
        models.InventoryItem.supply_name.ilike(db_item.supply_name)
    ).first()
    if inv_item:
        inv_item.quantity += db_item.quantity
    else:
        restored = models.InventoryItem(
            supply_name=db_item.supply_name,
            quantity=db_item.quantity
        )
        db.add(restored)

    db.delete(db_item)
    db.commit()


# ─── SUMMARY ROUTE ───────────────────────────────────────────────────────────

@app.get("/api/summary")
def get_summary(db: Session = Depends(get_db)):
    inventory_items = db.query(models.InventoryItem).all()
    given_out_items = db.query(models.GivenOutItem).all()

    return {
        "inventory": {
            "total_lines": len(inventory_items),
            "total_units": sum(i.quantity for i in inventory_items),
            "items": [schemas.InventoryItem.model_validate(i) for i in inventory_items]
        },
        "given_out": {
            "total_lines": len(given_out_items),
            "total_units": sum(i.quantity for i in given_out_items),
            "items": [schemas.GivenOutItem.model_validate(i) for i in given_out_items]
        }
    }


# ─── SERVE FRONTEND ──────────────────────────────────────────────────────────

frontend_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend")

@app.get("/", include_in_schema=False)
@app.get("/{full_path:path}", include_in_schema=False)
def serve_frontend(full_path: str = ""):
    if full_path.startswith("api/") or full_path == "docs" or full_path == "openapi.json":
        from fastapi import HTTPException
        raise HTTPException(status_code=404)
    return FileResponse(os.path.join(frontend_path, "index.html"))
