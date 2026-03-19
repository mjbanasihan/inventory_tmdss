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

# Re-detect after migrations have run
def refresh_flags():
    global HAS_DATE_GIVEN, HAS_CB_GIVEN, HAS_TXN_DATE, HAS_CB_TXN, HAS_CB_INV
    HAS_DATE_GIVEN  = col_exists("given_out_items", "date_given")
    HAS_CB_GIVEN    = col_exists("given_out_items", "changed_by")
    HAS_TXN_DATE    = col_exists("transaction_log",  "date_given")
    HAS_CB_TXN      = col_exists("transaction_log",  "changed_by")
    HAS_CB_INV      = col_exists("inventory_items",  "changed_by")
    print(f"Columns — given_out.date_given:{HAS_DATE_GIVEN} given_out.changed_by:{HAS_CB_GIVEN} txn.date_given:{HAS_TXN_DATE} txn.changed_by:{HAS_CB_TXN} inv.changed_by:{HAS_CB_INV}", flush=True)

HAS_DATE_GIVEN = HAS_CB_GIVEN = HAS_TXN_DATE = HAS_CB_TXN = HAS_CB_INV = False
refresh_flags()

app = FastAPI(title="TSD-TMDSS Inventory API", version="1.0.0")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
def on_startup():
    refresh_flags()

def write_log(db, txn_type, supply_name, quantity, detail=None, date_given=None, changed_by=None):
    """Write to transaction_log using only columns that exist, never crashes."""
    ca = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    # Always try full insert first, fall back to minimal
    for sql, params in [
        (
            "INSERT INTO transaction_log (txn_type,supply_name,quantity,detail,date_given,changed_by,created_at) VALUES (:t,:sn,:qty,:det,:dg,:cb,:ca)",
            {"t":txn_type,"sn":supply_name,"qty":quantity,"det":detail,"dg":date_given,"cb":changed_by,"ca":ca}
        ),
        (
            "INSERT INTO transaction_log (txn_type,supply_name,quantity,detail,created_at) VALUES (:t,:sn,:qty,:det,:ca)",
            {"t":txn_type,"sn":supply_name,"qty":quantity,"det":detail,"ca":ca}
        ),
    ]:
        try:
            db.execute(text(sql), params)
            db.commit()
            return
        except Exception:
            try: db.rollback()
            except: pass
    # If all fail, silently ignore — log failure is non-fatal

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

        # Log transaction
        write_log(db, "inventory", item.supply_name, item.quantity,
                  detail=item.date_received, changed_by=item.changed_by)
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

    # Log the edit
    write_log(db, "inventory", item.supply_name, item.quantity,
              detail=item.date_received, changed_by=item.changed_by)
    return db_item


@app.delete("/api/inventory/{item_id}", status_code=204)
def delete_inventory_item(item_id: int, db: Session = Depends(get_db)):
    db_item = db.query(models.InventoryItem).filter(models.InventoryItem.id == item_id).first()
    if not db_item:
        raise HTTPException(status_code=404, detail="Item not found")
    supply_name  = db_item.supply_name
    quantity     = db_item.quantity
    date_received = db_item.date_received
    db.delete(db_item)
    db.commit()
    write_log(db, "inventory_deleted", supply_name, quantity, detail=date_received)
        pass


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
        if HAS_DATE_GIVEN and HAS_CB_GIVEN:
            db.execute(text(
                "INSERT INTO given_out_items (supply_name, quantity, who_received, date_given, changed_by) VALUES (:sn, :qty, :who, :dg, :cb)"
            ), {"sn": item.supply_name, "qty": item.quantity, "who": item.who_received, "dg": item.date_given, "cb": item.changed_by})
        elif HAS_DATE_GIVEN:
            db.execute(text(
                "INSERT INTO given_out_items (supply_name, quantity, who_received, date_given) VALUES (:sn, :qty, :who, :dg)"
            ), {"sn": item.supply_name, "qty": item.quantity, "who": item.who_received, "dg": item.date_given})
        elif HAS_CB_GIVEN:
            db.execute(text(
                "INSERT INTO given_out_items (supply_name, quantity, who_received, changed_by) VALUES (:sn, :qty, :who, :cb)"
            ), {"sn": item.supply_name, "qty": item.quantity, "who": item.who_received, "cb": item.changed_by})
        else:
            db.execute(text(
                "INSERT INTO given_out_items (supply_name, quantity, who_received) VALUES (:sn, :qty, :who)"
            ), {"sn": item.supply_name, "qty": item.quantity, "who": item.who_received})

        # Log transaction
        try:
            if HAS_TXN_DATE and HAS_CB_TXN:
                db.execute(text(
                    "INSERT INTO transaction_log (txn_type, supply_name, quantity, detail, date_given, changed_by, created_at) VALUES (:t, :sn, :qty, :det, :dg, :cb, :ca)"
                ), {"t": "given_out", "sn": item.supply_name, "qty": item.quantity,
                    "det": item.who_received, "dg": item.date_given, "cb": item.changed_by,
                    "ca": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")})
            elif HAS_TXN_DATE:
                db.execute(text(
                    "INSERT INTO transaction_log (txn_type, supply_name, quantity, detail, date_given, created_at) VALUES (:t, :sn, :qty, :det, :dg, :ca)"
                ), {"t": "given_out", "sn": item.supply_name, "qty": item.quantity,
                    "det": item.who_received, "dg": item.date_given,
                    "ca": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")})
            elif HAS_CB_TXN:
                db.execute(text(
                    "INSERT INTO transaction_log (txn_type, supply_name, quantity, detail, changed_by, created_at) VALUES (:t, :sn, :qty, :det, :cb, :ca)"
                ), {"t": "given_out", "sn": item.supply_name, "qty": item.quantity,
                    "det": item.who_received, "cb": item.changed_by,
                    "ca": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")})
            else:
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

        # Update — use flags to decide columns
        if HAS_DATE_GIVEN and HAS_CB_GIVEN:
            db.execute(text(
                "UPDATE given_out_items SET supply_name=:sn, quantity=:qty, who_received=:who, date_given=:dg, changed_by=:cb WHERE id=:id"
            ), {"sn": item.supply_name, "qty": item.quantity, "who": item.who_received, "dg": item.date_given, "cb": item.changed_by, "id": item_id})
        elif HAS_DATE_GIVEN:
            db.execute(text(
                "UPDATE given_out_items SET supply_name=:sn, quantity=:qty, who_received=:who, date_given=:dg WHERE id=:id"
            ), {"sn": item.supply_name, "qty": item.quantity, "who": item.who_received, "dg": item.date_given, "id": item_id})
        elif HAS_CB_GIVEN:
            db.execute(text(
                "UPDATE given_out_items SET supply_name=:sn, quantity=:qty, who_received=:who, changed_by=:cb WHERE id=:id"
            ), {"sn": item.supply_name, "qty": item.quantity, "who": item.who_received, "cb": item.changed_by, "id": item_id})
        else:
            db.execute(text(
                "UPDATE given_out_items SET supply_name=:sn, quantity=:qty, who_received=:who WHERE id=:id"
            ), {"sn": item.supply_name, "qty": item.quantity, "who": item.who_received, "id": item_id})

        db.commit()

        # Log the edit
        try:
            if HAS_TXN_DATE and HAS_CB_TXN:
                db.execute(text(
                    "INSERT INTO transaction_log (txn_type, supply_name, quantity, detail, date_given, changed_by, created_at) VALUES (:t, :sn, :qty, :det, :dg, :cb, :ca)"
                ), {"t": "given_out", "sn": item.supply_name, "qty": item.quantity,
                    "det": item.who_received, "dg": item.date_given, "cb": item.changed_by,
                    "ca": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")})
            elif HAS_TXN_DATE:
                db.execute(text(
                    "INSERT INTO transaction_log (txn_type, supply_name, quantity, detail, date_given, created_at) VALUES (:t, :sn, :qty, :det, :dg, :ca)"
                ), {"t": "given_out", "sn": item.supply_name, "qty": item.quantity,
                    "det": item.who_received, "dg": item.date_given,
                    "ca": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")})
            else:
                db.execute(text(
                    "INSERT INTO transaction_log (txn_type, supply_name, quantity, detail, created_at) VALUES (:t, :sn, :qty, :det, :ca)"
                ), {"t": "given_out", "sn": item.supply_name, "qty": item.quantity,
                    "det": item.who_received, "ca": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")})
            db.commit()
        except Exception:
            pass  # log failure is non-fatal

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
    # Log the deletion
    try:
        if HAS_TXN_DATE and HAS_CB_TXN:
            db.execute(text(
                "INSERT INTO transaction_log (txn_type, supply_name, quantity, detail, date_given, changed_by, created_at) VALUES (:t, :sn, :qty, :det, :dg, :cb, :ca)"
            ), {"t": "given_out_deleted", "sn": row["supply_name"], "qty": row["quantity"],
                "det": row["who_received"], "dg": row.get("date_given"), "cb": None,
                "ca": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")})
        else:
            db.execute(text(
                "INSERT INTO transaction_log (txn_type, supply_name, quantity, detail, created_at) VALUES (:t, :sn, :qty, :det, :ca)"
            ), {"t": "given_out_deleted", "sn": row["supply_name"], "qty": row["quantity"],
                "det": row["who_received"], "ca": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")})
        db.commit()
    except Exception:
        pass


# ─── DELETE LOG ENTRY ────────────────────────────────────────────────────────

@app.delete("/api/log/{log_id}", status_code=204)
def delete_log_entry(log_id: int, db: Session = Depends(get_db)):
    row = db.execute(text("SELECT id FROM transaction_log WHERE id=:id"), {"id": log_id}).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Log entry not found")
    db.execute(text("DELETE FROM transaction_log WHERE id=:id"), {"id": log_id})
    db.commit()


# ─── SUMMARY — reads from transaction log ────────────────────────────────────

@app.get("/api/summary")
def get_summary(db: Session = Depends(get_db)):
    # Join transaction_log with given_out_items to backfill missing date_given
    try:
        result = db.execute(text("""
            SELECT tl.*,
                   COALESCE(tl.date_given, gi.date_given) AS date_given_resolved
            FROM transaction_log tl
            LEFT JOIN given_out_items gi
              ON LOWER(tl.supply_name) = LOWER(gi.supply_name)
              AND tl.txn_type IN ('given_out', 'given_out_deleted')
            ORDER BY tl.id
        """)).mappings().all()
    except Exception:
        # Fallback if join fails
        result = db.execute(text("SELECT * FROM transaction_log ORDER BY id")).mappings().all()

    logs = []
    for r in result:
        d = dict(r)
        # Use resolved date_given if available
        if d.get("date_given_resolved"):
            d["date_given"] = d["date_given_resolved"]
        logs.append(d)

    inv_logs  = [l for l in logs if l.get("txn_type") in ("inventory", "inventory_deleted")]
    giv_logs  = [l for l in logs if l.get("txn_type") in ("given_out", "given_out_deleted")]

    # Deduplicate given_out logs by id (join may produce duplicates)
    seen = set()
    deduped_giv = []
    for l in giv_logs:
        if l["id"] not in seen:
            seen.add(l["id"])
            deduped_giv.append(l)
    giv_logs = deduped_giv

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
