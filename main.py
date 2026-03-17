from fastapi import FastAPI, HTTPException, Depends
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import List
from datetime import datetime
import os, sys

from database import engine, get_db, Base
import models
import schemas

# Warn if falling back to SQLite
if "sqlite" in str(engine.url):
    print("WARNING: Using SQLite — data will not persist on Render!", file=sys.stderr)

# Create tables (safe — never drops existing data)
Base.metadata.create_all(bind=engine)

# ── Migrations ────────────────────────────────────────────────────────────────
def run_migrations():
    migrations = [
        "ALTER TABLE given_out_items ADD COLUMN date_given VARCHAR",
        "ALTER TABLE given_out_items ADD COLUMN changed_by VARCHAR",
        "ALTER TABLE inventory_items ADD COLUMN changed_by VARCHAR",
        "ALTER TABLE transaction_log ADD COLUMN date_given VARCHAR",
        "ALTER TABLE transaction_log ADD COLUMN changed_by VARCHAR",
        "ALTER TABLE transaction_log ADD COLUMN created_at VARCHAR",
    ]
    with engine.connect() as conn:
        for sql in migrations:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception:
                pass  # Column already exists — safe to ignore

run_migrations()

app = FastAPI(title="TSD-TMDSS Inventory API", version="1.0.0")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

def log_txn(db, txn_type, supply_name, quantity, detail=None, date_given=None, changed_by=None):
    """Append a record to the transaction log."""
    entry = models.TransactionLog(
        txn_type=txn_type,
        supply_name=supply_name,
        quantity=quantity,
        detail=detail,
        date_given=date_given,
        changed_by=changed_by,
        created_at=datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    )
    db.add(entry)

# ─── INVENTORY ────────────────────────────────────────────────────────────────

@app.get("/api/inventory", response_model=List[schemas.InventoryItem])
def get_inventory(search: str = "", db: Session = Depends(get_db)):
    q = db.query(models.InventoryItem)
    if search:
        q = q.filter(models.InventoryItem.supply_name.ilike(f"%{search}%"))
    return q.order_by(models.InventoryItem.id).all()


@app.post("/api/inventory", response_model=schemas.InventoryItem, status_code=201)
def create_inventory_item(item: schemas.InventoryItemCreate, db: Session = Depends(get_db)):
    # Upsert: merge into existing row so current inventory stays consolidated
    existing = db.query(models.InventoryItem).filter(
        models.InventoryItem.supply_name.ilike(item.supply_name)
    ).first()
    if existing:
        existing.quantity += item.quantity
        if item.date_received:
            existing.date_received = item.date_received
        if item.changed_by:
            existing.changed_by = item.changed_by
        db.commit()
        db.refresh(existing)
        result = existing
    else:
        db_item = models.InventoryItem(**item.model_dump())
        db.add(db_item)
        db.commit()
        db.refresh(db_item)
        result = db_item

    # Always log every add as a separate transaction
    log_txn(db, "inventory", item.supply_name, item.quantity,
            detail=item.date_received, changed_by=item.changed_by)
    db.commit()
    return result


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


# ─── GIVEN OUT ────────────────────────────────────────────────────────────────

@app.get("/api/given-out", response_model=List[schemas.GivenOutItem])
def get_given_out(search: str = "", db: Session = Depends(get_db)):
    q = db.query(models.GivenOutItem)
    if search:
        q = q.filter(models.GivenOutItem.supply_name.ilike(f"%{search}%"))
    return q.order_by(models.GivenOutItem.id).all()


@app.post("/api/given-out", response_model=schemas.GivenOutItem, status_code=201)
def create_given_out_item(item: schemas.GivenOutItemCreate, db: Session = Depends(get_db)):
    # Check total available stock
    inv_items = db.query(models.InventoryItem).filter(
        models.InventoryItem.supply_name.ilike(item.supply_name)
    ).all()
    if not inv_items:
        raise HTTPException(status_code=400, detail=f"'{item.supply_name}' not found in inventory")
    total_avail = sum(i.quantity for i in inv_items)
    if total_avail < item.quantity:
        raise HTTPException(status_code=400, detail=f"Only {total_avail} unit(s) available for '{item.supply_name}'")

    # Deduct from inventory rows oldest first
    remaining = item.quantity
    for inv in inv_items:
        if remaining <= 0:
            break
        if inv.quantity <= remaining:
            remaining -= inv.quantity
            db.delete(inv)
        else:
            inv.quantity -= remaining
            remaining = 0

    # Always create a new given-out row (full history)
    db_item = models.GivenOutItem(**item.model_dump())
    db.add(db_item)

    # Log the transaction
    log_txn(db, "given_out", item.supply_name, item.quantity,
            detail=item.who_received, date_given=item.date_given, changed_by=item.changed_by)
    db.commit()
    db.refresh(db_item)
    return db_item


@app.put("/api/given-out/{item_id}", response_model=schemas.GivenOutItem)
def update_given_out_item(item_id: int, item: schemas.GivenOutItemCreate, db: Session = Depends(get_db)):
    db_item = db.query(models.GivenOutItem).filter(models.GivenOutItem.id == item_id).first()
    if not db_item:
        raise HTTPException(status_code=404, detail="Item not found")

    qty_diff = item.quantity - db_item.quantity

    if qty_diff != 0:
        inv_items = db.query(models.InventoryItem).filter(
            models.InventoryItem.supply_name.ilike(item.supply_name)
        ).all()
        if qty_diff > 0:
            total_avail = sum(i.quantity for i in inv_items)
            if total_avail < qty_diff:
                raise HTTPException(status_code=400, detail=f"Only {total_avail} unit(s) available")
            remaining = qty_diff
            for inv in inv_items:
                if remaining <= 0: break
                if inv.quantity <= remaining:
                    remaining -= inv.quantity
                    db.delete(inv)
                else:
                    inv.quantity -= remaining
                    remaining = 0
        else:
            # Return units to inventory
            if inv_items:
                inv_items[0].quantity += abs(qty_diff)
            else:
                db.add(models.InventoryItem(supply_name=item.supply_name, quantity=abs(qty_diff)))

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

    # Restore to inventory
    inv_item = db.query(models.InventoryItem).filter(
        models.InventoryItem.supply_name.ilike(db_item.supply_name)
    ).first()
    if inv_item:
        inv_item.quantity += db_item.quantity
    else:
        db.add(models.InventoryItem(supply_name=db_item.supply_name, quantity=db_item.quantity))

    db.delete(db_item)
    db.commit()


# ─── SUMMARY — reads from transaction log ────────────────────────────────────

@app.get("/api/summary")
def get_summary(db: Session = Depends(get_db)):
    # Use raw SQL so we never crash if a column is missing in the live DB
    result = db.execute(text("SELECT * FROM transaction_log ORDER BY id")).mappings().all()
    logs = [dict(r) for r in result]

    inv_logs  = [l for l in logs if l.get("txn_type") == "inventory"]
    giv_logs  = [l for l in logs if l.get("txn_type") == "given_out"]

    def safe_log(l):
        return {
            "id":          l.get("id"),
            "txn_type":    l.get("txn_type"),
            "supply_name": l.get("supply_name"),
            "quantity":    l.get("quantity", 0),
            "detail":      l.get("detail"),
            "date_given":  l.get("date_given"),
            "changed_by":  l.get("changed_by"),
            "created_at":  l.get("created_at"),
        }

    return {
        "inventory": {
            "total_lines": len(inv_logs),
            "total_units": sum(l.get("quantity", 0) for l in inv_logs),
            "items": [safe_log(l) for l in inv_logs]
        },
        "given_out": {
            "total_lines": len(giv_logs),
            "total_units": sum(l.get("quantity", 0) for l in giv_logs),
            "items": [safe_log(l) for l in giv_logs]
        }
    }


# ─── SERVE FRONTEND ──────────────────────────────────────────────────────────

frontend_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend")

@app.get("/", include_in_schema=False)
@app.get("/{full_path:path}", include_in_schema=False)
def serve_frontend(full_path: str = ""):
    if full_path.startswith("api/") or full_path in ("docs", "openapi.json"):
        raise HTTPException(status_code=404)
    return FileResponse(os.path.join(frontend_path, "index.html"))
