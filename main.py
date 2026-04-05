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
    """Add any columns that may be missing from older DB schemas."""
    migrations = [
        "ALTER TABLE given_out_items ADD COLUMN date_given VARCHAR",
        "ALTER TABLE given_out_items ADD COLUMN changed_by VARCHAR",
        "ALTER TABLE given_out_items ADD COLUMN variety VARCHAR",
        "ALTER TABLE inventory_items ADD COLUMN changed_by VARCHAR",
        "ALTER TABLE inventory_items ADD COLUMN variety VARCHAR",
        "ALTER TABLE transaction_log ADD COLUMN date_given VARCHAR",
        "ALTER TABLE transaction_log ADD COLUMN changed_by VARCHAR",
        "ALTER TABLE transaction_log ADD COLUMN created_at VARCHAR",
        "ALTER TABLE transaction_log ADD COLUMN variety VARCHAR",
    ]
    with engine.connect() as conn:
        for sql in migrations:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception:
                pass  # Column already exists — safe to ignore
    print("Migrations complete.", flush=True)

run_migrations()

app = FastAPI(title="TSD-TMDSS Inventory API", version="1.0.0")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ── Transaction log helper ─────────────────────────────────────────────────────
def write_log(db: Session, txn_type: str, supply_name: str, quantity: int,
              detail=None, date_given=None, changed_by=None, variety=None):
    entry = models.TransactionLog(
        txn_type=txn_type,
        supply_name=supply_name,
        variety=variety,
        quantity=quantity,
        detail=detail,
        date_given=date_given,
        changed_by=changed_by,
        created_at=datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"),
    )
    db.add(entry)


# ─── INVENTORY ────────────────────────────────────────────────────────────────

@app.get("/api/inventory")
def get_inventory(search: str = "", db: Session = Depends(get_db)):
    q = db.query(models.InventoryItem)
    if search:
        q = q.filter(models.InventoryItem.supply_name.ilike(f"%{search}%"))
    items = q.order_by(models.InventoryItem.id).all()
    return [
        {
            "id": it.id,
            "supply_name": it.supply_name,
            "variety": it.variety,
            "quantity": it.quantity,
            "date_received": it.date_received,
            "changed_by": it.changed_by,
        }
        for it in items
    ]


@app.post("/api/inventory", status_code=201)
def create_inventory_item(item: schemas.InventoryItemCreate, db: Session = Depends(get_db)):
    existing = (
        db.query(models.InventoryItem)
        .filter(models.InventoryItem.supply_name.ilike(item.supply_name))
        .first()
    )

    if existing:
        existing.quantity += item.quantity
        if item.date_received:
            existing.date_received = item.date_received
        if item.variety is not None:
            existing.variety = item.variety
        if item.changed_by:
            existing.changed_by = item.changed_by
        db.flush()
        write_log(db, "inventory", existing.supply_name, item.quantity,
                  detail=existing.date_received, changed_by=item.changed_by, variety=existing.variety)
        db.commit()
        db.refresh(existing)
        result = existing
    else:
        new_item = models.InventoryItem(
            supply_name=item.supply_name,
            variety=item.variety,
            quantity=item.quantity,
            date_received=item.date_received,
            changed_by=item.changed_by,
        )
        db.add(new_item)
        db.flush()
        write_log(db, "inventory", new_item.supply_name, new_item.quantity,
                  detail=new_item.date_received, changed_by=new_item.changed_by, variety=new_item.variety)
        db.commit()
        db.refresh(new_item)
        result = new_item

    return {
        "id": result.id,
        "supply_name": result.supply_name,
        "variety": result.variety,
        "quantity": result.quantity,
        "date_received": result.date_received,
        "changed_by": result.changed_by,
    }


@app.put("/api/inventory/{item_id}")
def update_inventory_item(item_id: int, item: schemas.InventoryItemCreate, db: Session = Depends(get_db)):
    row = db.query(models.InventoryItem).filter(models.InventoryItem.id == item_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Item not found")

    row.supply_name = item.supply_name
    row.variety = item.variety
    row.quantity = item.quantity
    row.date_received = item.date_received
    if item.changed_by:
        row.changed_by = item.changed_by

    db.flush()
    write_log(db, "inventory", row.supply_name, row.quantity,
              detail=row.date_received, changed_by=row.changed_by, variety=row.variety)
    db.commit()
    db.refresh(row)

    return {
        "id": row.id,
        "supply_name": row.supply_name,
        "variety": row.variety,
        "quantity": row.quantity,
        "date_received": row.date_received,
        "changed_by": row.changed_by,
    }


@app.delete("/api/inventory/{item_id}", status_code=204)
def delete_inventory_item(item_id: int, db: Session = Depends(get_db)):
    row = db.query(models.InventoryItem).filter(models.InventoryItem.id == item_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Item not found")
    write_log(db, "inventory_deleted", row.supply_name, row.quantity,
              detail=row.date_received, variety=row.variety)
    db.delete(row)
    db.commit()


# ─── GIVEN OUT ────────────────────────────────────────────────────────────────

@app.get("/api/given-out")
def get_given_out(search: str = "", db: Session = Depends(get_db)):
    q = db.query(models.GivenOutItem)
    if search:
        q = q.filter(models.GivenOutItem.supply_name.ilike(f"%{search}%"))
    items = q.order_by(models.GivenOutItem.id).all()
    return [
        {
            "id": it.id,
            "supply_name": it.supply_name,
            "variety": it.variety,
            "quantity": it.quantity,
            "who_received": it.who_received,
            "date_given": it.date_given,
            "changed_by": it.changed_by,
        }
        for it in items
    ]


@app.post("/api/given-out", status_code=201)
def create_given_out_item(item: schemas.GivenOutItemCreate, db: Session = Depends(get_db)):
    inv = (
        db.query(models.InventoryItem)
        .filter(models.InventoryItem.supply_name.ilike(item.supply_name))
        .first()
    )
    if not inv:
        raise HTTPException(status_code=400, detail=f"'{item.supply_name}' not found in inventory")
    if inv.quantity < item.quantity:
        raise HTTPException(status_code=400, detail=f"Only {inv.quantity} unit(s) available")

    # Carry variety from the inventory row
    variety = inv.variety or item.variety or None

    # Deduct from inventory
    inv.quantity -= item.quantity
    if inv.quantity == 0:
        db.delete(inv)
    db.flush()

    given = models.GivenOutItem(
        supply_name=item.supply_name,
        variety=variety,
        quantity=item.quantity,
        who_received=item.who_received,
        date_given=item.date_given,
        changed_by=item.changed_by,
    )
    db.add(given)
    db.flush()

    write_log(db, "given_out", given.supply_name, given.quantity,
              detail=given.who_received, date_given=given.date_given,
              changed_by=given.changed_by, variety=given.variety)
    db.commit()
    db.refresh(given)

    return {
        "id": given.id,
        "supply_name": given.supply_name,
        "variety": given.variety,
        "quantity": given.quantity,
        "who_received": given.who_received,
        "date_given": given.date_given,
        "changed_by": given.changed_by,
    }


@app.put("/api/given-out/{item_id}")
def update_given_out_item(item_id: int, item: schemas.GivenOutItemCreate, db: Session = Depends(get_db)):
    current = db.query(models.GivenOutItem).filter(models.GivenOutItem.id == item_id).first()
    if not current:
        raise HTTPException(status_code=404, detail="Item not found")

    qty_diff = item.quantity - current.quantity

    if qty_diff > 0:
        inv = (
            db.query(models.InventoryItem)
            .filter(models.InventoryItem.supply_name.ilike(item.supply_name))
            .first()
        )
        avail = inv.quantity if inv else 0
        if avail < qty_diff:
            raise HTTPException(status_code=400, detail=f"Only {avail} unit(s) available in inventory")
        inv.quantity -= qty_diff
        if inv.quantity == 0:
            db.delete(inv)
        db.flush()

    elif qty_diff < 0:
        inv = (
            db.query(models.InventoryItem)
            .filter(models.InventoryItem.supply_name.ilike(item.supply_name))
            .first()
        )
        if inv:
            inv.quantity += abs(qty_diff)
        else:
            db.add(models.InventoryItem(supply_name=item.supply_name, quantity=abs(qty_diff)))
        db.flush()

    current.quantity = item.quantity
    current.who_received = item.who_received
    current.date_given = item.date_given
    if item.changed_by:
        current.changed_by = item.changed_by

    db.flush()
    write_log(db, "given_out", current.supply_name, current.quantity,
              detail=current.who_received, date_given=current.date_given,
              changed_by=current.changed_by, variety=current.variety)
    db.commit()
    db.refresh(current)

    return {
        "id": current.id,
        "supply_name": current.supply_name,
        "variety": current.variety,
        "quantity": current.quantity,
        "who_received": current.who_received,
        "date_given": current.date_given,
        "changed_by": current.changed_by,
    }


@app.delete("/api/given-out/{item_id}", status_code=204)
def delete_given_out_item(item_id: int, db: Session = Depends(get_db)):
    row = db.query(models.GivenOutItem).filter(models.GivenOutItem.id == item_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Item not found")

    inv = (
        db.query(models.InventoryItem)
        .filter(models.InventoryItem.supply_name.ilike(row.supply_name))
        .first()
    )
    if inv:
        inv.quantity += row.quantity
    else:
        db.add(models.InventoryItem(supply_name=row.supply_name, quantity=row.quantity))
    db.flush()

    write_log(db, "given_out_deleted", row.supply_name, row.quantity,
              detail=row.who_received, date_given=row.date_given, variety=row.variety)
    db.delete(row)
    db.commit()


# ─── DELETE LOG ENTRY ─────────────────────────────────────────────────────────

@app.delete("/api/log/{log_id}", status_code=204)
def delete_log_entry(log_id: int, db: Session = Depends(get_db)):
    row = db.query(models.TransactionLog).filter(models.TransactionLog.id == log_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Log entry not found")
    db.delete(row)
    db.commit()


# ─── SUMMARY ──────────────────────────────────────────────────────────────────

@app.get("/api/summary")
def get_summary(db: Session = Depends(get_db)):
    logs = db.query(models.TransactionLog).order_by(models.TransactionLog.id).all()

    given_items = db.query(models.GivenOutItem).all()
    gi_map_precise = {}
    gi_map_supply = {}
    for gi in given_items:
        if gi.date_given:
            key = (gi.supply_name.lower(), (gi.who_received or "").lower())
            gi_map_precise.setdefault(key, gi.date_given)
            gi_map_supply.setdefault(gi.supply_name.lower(), gi.date_given)

    def to_dict(l):
        date_given = l.date_given
        if l.txn_type in ("given_out", "given_out_deleted") and not date_given:
            key = ((l.supply_name or "").lower(), (l.detail or "").lower())
            date_given = gi_map_precise.get(key) or gi_map_supply.get((l.supply_name or "").lower())
        return {
            "id": l.id,
            "txn_type": l.txn_type,
            "supply_name": l.supply_name,
            "variety": l.variety,
            "quantity": l.quantity,
            "detail": l.detail,
            "date_given": date_given,
            "changed_by": l.changed_by,
            "created_at": l.created_at,
        }

    inv_logs = [to_dict(l) for l in logs if l.txn_type in ("inventory", "inventory_deleted")]
    giv_logs = [to_dict(l) for l in logs if l.txn_type in ("given_out", "given_out_deleted")]

    current_inv = db.query(models.InventoryItem).all()
    current_giv = db.query(models.GivenOutItem).all()

    return {
        "inventory": {
            "total_lines": len(inv_logs),
            "total_units": sum(i.quantity for i in current_inv),
            "items": inv_logs,
        },
        "given_out": {
            "total_lines": len(giv_logs),
            "total_units": sum(g.quantity for g in current_giv),
            "items": giv_logs,
        },
    }


# ─── DEBUG ────────────────────────────────────────────────────────────────────

@app.get("/api/debug")
def debug_data(db: Session = Depends(get_db)):
    return {
        "inventory_items": [
            {"id": i.id, "supply_name": i.supply_name, "variety": i.variety, "quantity": i.quantity}
            for i in db.query(models.InventoryItem).order_by(models.InventoryItem.id.desc()).limit(5).all()
        ],
        "given_out_items": [
            {"id": g.id, "supply_name": g.supply_name, "variety": g.variety, "quantity": g.quantity}
            for g in db.query(models.GivenOutItem).order_by(models.GivenOutItem.id.desc()).limit(5).all()
        ],
        "transaction_log": [
            {"id": l.id, "txn_type": l.txn_type, "supply_name": l.supply_name, "variety": l.variety}
            for l in db.query(models.TransactionLog).order_by(models.TransactionLog.id.desc()).limit(5).all()
        ],
    }


# ─── SERVE FRONTEND ───────────────────────────────────────────────────────────

frontend_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend")

@app.get("/", include_in_schema=False)
@app.get("/{full_path:path}", include_in_schema=False)
def serve_frontend(full_path: str = ""):
    if full_path.startswith("api/") or full_path in ("docs", "openapi.json"):
        raise HTTPException(status_code=404)
    return FileResponse(os.path.join(frontend_path, "index.html"))
