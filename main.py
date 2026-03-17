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

# ── Detect which optional columns actually exist in live DB ──────────────────
def col_exists(table, col):
    try:
        with engine.connect() as conn:
            conn.execute(text(f"SELECT {col} FROM {table} LIMIT 1"))
        return True
    except Exception:
        return False

HAS_DATE_GIVEN = col_exists("given_out_items", "date_given")
HAS_TXN_DATE   = col_exists("transaction_log",  "date_given")
print(f"Column detection — given_out_items.date_given: {HAS_DATE_GIVEN}, transaction_log.date_given: {HAS_TXN_DATE}", flush=True)

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
    import traceback
    try:
        # Upsert: merge into existing row
        existing = db.query(models.InventoryItem).filter(
            models.InventoryItem.supply_name.ilike(item.supply_name)
        ).first()
        if existing:
            existing.quantity += item.quantity
            if item.date_received:
                existing.date_received = item.date_received
            try:
                existing.changed_by = item.changed_by
            except Exception:
                pass
            db.commit()
            db.refresh(existing)
            result = existing
        else:
            db_item = models.InventoryItem(
                supply_name=item.supply_name,
                quantity=item.quantity,
                date_received=item.date_received
            )
            try:
                db_item.changed_by = item.changed_by
            except Exception:
                pass
            db.add(db_item)
            db.commit()
            db.refresh(db_item)
            result = db_item

        # Log transaction using raw SQL — safe against missing columns
        try:
            db.execute(text(
                "INSERT INTO transaction_log (txn_type, supply_name, quantity, detail, changed_by, created_at) VALUES (:t, :sn, :qty, :det, :cb, :ca)"
            ), {"t": "inventory", "sn": item.supply_name, "qty": item.quantity,
                "det": item.date_received, "cb": item.changed_by,
                "ca": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")})
            db.commit()
        except Exception:
            try:
                db.execute(text(
                    "INSERT INTO transaction_log (txn_type, supply_name, quantity, detail, created_at) VALUES (:t, :sn, :qty, :det, :ca)"
                ), {"t": "inventory", "sn": item.supply_name, "qty": item.quantity,
                    "det": item.date_received, "ca": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")})
                db.commit()
            except Exception:
                pass  # log failure is non-fatal

        return result

    except Exception as e:
        print("POST /api/inventory ERROR:", traceback.format_exc(), flush=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/api/inventory/{item_id}", response_model=schemas.InventoryItem)
def update_inventory_item(item_id: int, item: schemas.InventoryItemCreate, db: Session = Depends(get_db)):
    db_item = db.query(models.InventoryItem).filter(models.InventoryItem.id == item_id).first()
    if not db_item:
        raise HTTPException(status_code=404, detail="Item not found")
    db_item.supply_name   = item.supply_name
    db_item.quantity      = item.quantity
    db_item.date_received = item.date_received
    try:
        db_item.changed_by = item.changed_by
    except Exception:
        pass
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

@app.get("/api/given-out")
def get_given_out(search: str = "", db: Session = Depends(get_db)):
    if search:
        rows = db.execute(text(
            "SELECT * FROM given_out_items WHERE supply_name ILIKE :s ORDER BY id"
        ), {"s": f"%{search}%"}).mappings().all()
    else:
        rows = db.execute(text(
            "SELECT * FROM given_out_items ORDER BY id"
        )).mappings().all()
    return [dict(r) for r in rows]


@app.post("/api/given-out", status_code=201)
def create_given_out_item(item: schemas.GivenOutItemCreate, db: Session = Depends(get_db)):
    import traceback
    try:
        # Check available stock
        inv = db.execute(text(
            "SELECT id, quantity FROM inventory_items WHERE LOWER(supply_name)=LOWER(:sn) LIMIT 1"
        ), {"sn": item.supply_name}).mappings().first()
        if not inv:
            raise HTTPException(status_code=400, detail=f"'{item.supply_name}' not found in inventory")
        if inv["quantity"] < item.quantity:
            raise HTTPException(status_code=400, detail=f"Only {inv['quantity']} unit(s) available")

        # Deduct from inventory
        new_qty = inv["quantity"] - item.quantity
        if new_qty == 0:
            db.execute(text("DELETE FROM inventory_items WHERE id=:id"), {"id": inv["id"]})
        else:
            db.execute(text("UPDATE inventory_items SET quantity=:q WHERE id=:id"), {"q": new_qty, "id": inv["id"]})

        # Insert given-out row
        if HAS_DATE_GIVEN:
            db.execute(text(
                "INSERT INTO given_out_items (supply_name, quantity, who_received, date_given, changed_by) VALUES (:sn, :qty, :who, :dg, :cb)"
            ), {"sn": item.supply_name, "qty": item.quantity, "who": item.who_received, "dg": item.date_given, "cb": item.changed_by})
        else:
            db.execute(text(
                "INSERT INTO given_out_items (supply_name, quantity, who_received, changed_by) VALUES (:sn, :qty, :who, :cb)"
            ), {"sn": item.supply_name, "qty": item.quantity, "who": item.who_received, "cb": item.changed_by})

        # Log transaction
        if HAS_TXN_DATE:
            db.execute(text(
                "INSERT INTO transaction_log (txn_type, supply_name, quantity, detail, date_given, created_at) VALUES (:t, :sn, :qty, :det, :dg, :ca)"
            ), {"t": "given_out", "sn": item.supply_name, "qty": item.quantity,
                "det": item.who_received, "dg": item.date_given,
                "ca": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")})
        else:
            try:
                db.execute(text(
                    "INSERT INTO transaction_log (txn_type, supply_name, quantity, detail, created_at) VALUES (:t, :sn, :qty, :det, :ca)"
                ), {"t": "given_out", "sn": item.supply_name, "qty": item.quantity,
                    "det": item.who_received, "ca": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")})
            except Exception:
                pass  # log failure is non-fatal

        db.commit()

        row = db.execute(text("SELECT * FROM given_out_items ORDER BY id DESC LIMIT 1")).mappings().first()
        return dict(row) if row else {"id": 0, "supply_name": item.supply_name, "quantity": item.quantity, "who_received": item.who_received, "date_given": item.date_given}

    except HTTPException:
        raise
    except Exception as e:
        print("POST /api/given-out ERROR:", traceback.format_exc(), flush=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/api/given-out/{item_id}")
def update_given_out_item(item_id: int, item: schemas.GivenOutItemCreate, db: Session = Depends(get_db)):
    import traceback
    try:
        current = db.execute(text(
            "SELECT * FROM given_out_items WHERE id=:id"
        ), {"id": item_id}).mappings().first()
        if not current:
            raise HTTPException(status_code=404, detail="Item not found")

        qty_diff = item.quantity - current["quantity"]

        if qty_diff > 0:
            inv = db.execute(text(
                "SELECT id, quantity FROM inventory_items WHERE LOWER(supply_name)=LOWER(:sn) LIMIT 1"
            ), {"sn": item.supply_name}).mappings().first()
            avail = inv["quantity"] if inv else 0
            if avail < qty_diff:
                raise HTTPException(status_code=400, detail=f"Only {avail} unit(s) available in inventory")
            new_inv_qty = avail - qty_diff
            if new_inv_qty == 0:
                db.execute(text("DELETE FROM inventory_items WHERE id=:id"), {"id": inv["id"]})
            else:
                db.execute(text("UPDATE inventory_items SET quantity=:q WHERE id=:id"), {"q": new_inv_qty, "id": inv["id"]})

        elif qty_diff < 0:
            inv = db.execute(text(
                "SELECT id, quantity FROM inventory_items WHERE LOWER(supply_name)=LOWER(:sn) LIMIT 1"
            ), {"sn": item.supply_name}).mappings().first()
            if inv:
                db.execute(text("UPDATE inventory_items SET quantity=:q WHERE id=:id"),
                           {"q": inv["quantity"] + abs(qty_diff), "id": inv["id"]})
            else:
                db.execute(text(
                    "INSERT INTO inventory_items (supply_name, quantity) VALUES (:sn, :qty)"
                ), {"sn": item.supply_name, "qty": abs(qty_diff)})

        # Update — use flag to decide whether to include date_given
        if HAS_DATE_GIVEN:
            db.execute(text(
                "UPDATE given_out_items SET supply_name=:sn, quantity=:qty, who_received=:who, date_given=:dg, changed_by=:cb WHERE id=:id"
            ), {"sn": item.supply_name, "qty": item.quantity, "who": item.who_received, "dg": item.date_given, "cb": item.changed_by, "id": item_id})
        else:
            db.execute(text(
                "UPDATE given_out_items SET supply_name=:sn, quantity=:qty, who_received=:who, changed_by=:cb WHERE id=:id"
            ), {"sn": item.supply_name, "qty": item.quantity, "who": item.who_received, "cb": item.changed_by, "id": item_id})

        db.commit()
        updated = db.execute(text("SELECT * FROM given_out_items WHERE id=:id"), {"id": item_id}).mappings().first()
        return dict(updated)

    except HTTPException:
        raise
    except Exception as e:
        print("PUT /api/given-out ERROR:", traceback.format_exc(), flush=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/given-out/{item_id}", status_code=204)
def delete_given_out_item(item_id: int, db: Session = Depends(get_db)):
    row = db.execute(text(
        "SELECT * FROM given_out_items WHERE id=:id"
    ), {"id": item_id}).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Item not found")

    # Restore quantity to inventory
    inv = db.execute(text(
        "SELECT id, quantity FROM inventory_items WHERE supply_name ILIKE :sn LIMIT 1"
    ), {"sn": row["supply_name"]}).mappings().first()
    if inv:
        db.execute(text("UPDATE inventory_items SET quantity=:q WHERE id=:id"),
                   {"q": inv["quantity"] + row["quantity"], "id": inv["id"]})
    else:
        db.execute(text(
            "INSERT INTO inventory_items (supply_name, quantity) VALUES (:sn, :qty)"
        ), {"sn": row["supply_name"], "qty": row["quantity"]})

    db.execute(text("DELETE FROM given_out_items WHERE id=:id"), {"id": item_id})
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
