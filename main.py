from fastapi import FastAPI, HTTPException, Depends, BackgroundTasks
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
        # Verify variety column is readable — force a read to confirm
        try:
            conn.execute(text("SELECT variety FROM inventory_items LIMIT 1"))
            conn.commit()
            print("variety column confirmed on inventory_items", flush=True)
        except Exception as e:
            print(f"variety column MISSING on inventory_items: {e}", flush=True)

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
    global HAS_DATE_GIVEN, HAS_CB_GIVEN, HAS_TXN_DATE, HAS_CB_TXN, HAS_CB_INV, HAS_VARIETY, HAS_VARIETY_GIVEN, HAS_VARIETY_TXN
    HAS_DATE_GIVEN    = col_exists("given_out_items", "date_given")
    HAS_CB_GIVEN      = col_exists("given_out_items", "changed_by")
    HAS_TXN_DATE      = col_exists("transaction_log",  "date_given")
    HAS_CB_TXN        = col_exists("transaction_log",  "changed_by")
    HAS_CB_INV        = col_exists("inventory_items",  "changed_by")
    HAS_VARIETY       = col_exists("inventory_items",  "variety")
    HAS_VARIETY_GIVEN = col_exists("given_out_items",  "variety")
    HAS_VARIETY_TXN   = col_exists("transaction_log",  "variety")
    print(f"Columns — inv.variety:{HAS_VARIETY} giv.variety:{HAS_VARIETY_GIVEN} txn.variety:{HAS_VARIETY_TXN}", flush=True)

HAS_DATE_GIVEN = HAS_CB_GIVEN = HAS_TXN_DATE = HAS_CB_TXN = HAS_CB_INV = HAS_VARIETY = HAS_VARIETY_GIVEN = HAS_VARIETY_TXN = False
refresh_flags()

app = FastAPI(title="TSD-TMDSS Inventory API", version="1.0.0")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
def on_startup():
    refresh_flags()

def write_log(db, txn_type, supply_name, quantity, detail=None, date_given=None, changed_by=None, variety=None):
    """Append to transaction_log. Uses savepoints so failures never roll back the caller's data."""
    ca = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    for sql, params in [
        (
            "INSERT INTO transaction_log (txn_type,supply_name,variety,quantity,detail,date_given,changed_by,created_at) VALUES (:t,:sn,:v,:qty,:det,:dg,:cb,:ca)",
            {"t":txn_type,"sn":supply_name,"v":variety,"qty":quantity,"det":detail,"dg":date_given,"cb":changed_by,"ca":ca}
        ),
        (
            "INSERT INTO transaction_log (txn_type,supply_name,quantity,detail,date_given,changed_by,created_at) VALUES (:t,:sn,:qty,:det,:dg,:cb,:ca)",
            {"t":txn_type,"sn":supply_name,"qty":quantity,"det":detail,"dg":date_given,"cb":changed_by,"ca":ca}
        ),
        (
            "INSERT INTO transaction_log (txn_type,supply_name,quantity,detail,changed_by,created_at) VALUES (:t,:sn,:qty,:det,:cb,:ca)",
            {"t":txn_type,"sn":supply_name,"qty":quantity,"det":detail,"cb":changed_by,"ca":ca}
        ),
        (
            "INSERT INTO transaction_log (txn_type,supply_name,quantity,detail,created_at) VALUES (:t,:sn,:qty,:det,:ca)",
            {"t":txn_type,"sn":supply_name,"qty":quantity,"det":detail,"ca":ca}
        ),
    ]:
        try:
            db.execute(text("SAVEPOINT log_sp"))
            db.execute(text(sql), params)
            db.execute(text("RELEASE SAVEPOINT log_sp"))
            return  # success
        except Exception:
            try: db.execute(text("ROLLBACK TO SAVEPOINT log_sp"))
            except: pass

# ─── INVENTORY ────────────────────────────────────────────────────────────────

@app.get("/api/inventory")
def get_inventory(search: str = "", db: Session = Depends(get_db)):
    try:
        if search:
            rows = db.execute(text(
                "SELECT * FROM inventory_items WHERE LOWER(supply_name) LIKE LOWER(:s) ORDER BY id"
            ), {"s": f"%{search}%"}).mappings().all()
        else:
            rows = db.execute(text(
                "SELECT * FROM inventory_items ORDER BY id"
            )).mappings().all()
        return [dict(r) for r in rows]
    except Exception as e:
        import traceback
        print("GET /api/inventory ERROR:", traceback.format_exc(), flush=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/inventory", status_code=201)
def create_inventory_item(item: schemas.InventoryItemCreate, db: Session = Depends(get_db)):
    import traceback
    try:
        existing = db.execute(text(
            "SELECT * FROM inventory_items WHERE LOWER(supply_name)=LOWER(:sn) LIMIT 1"
        ), {"sn": item.supply_name}).mappings().first()

        if existing:
            new_qty = existing["quantity"] + item.quantity
            dr = item.date_received or existing.get("date_received")
            if HAS_VARIETY:
                db.execute(text("UPDATE inventory_items SET quantity=:qty,date_received=:dr,variety=:v,changed_by=:cb WHERE id=:id"),
                    {"qty": new_qty, "dr": dr, "v": item.variety, "cb": item.changed_by, "id": existing["id"]})
            else:
                db.execute(text("UPDATE inventory_items SET quantity=:qty,date_received=:dr WHERE id=:id"),
                    {"qty": new_qty, "dr": dr, "id": existing["id"]})
            db.commit()
            result = db.execute(text("SELECT * FROM inventory_items WHERE id=:id"), {"id": existing["id"]}).mappings().first()
        else:
            if HAS_VARIETY:
                db.execute(text("INSERT INTO inventory_items (supply_name,variety,quantity,date_received,changed_by) VALUES (:sn,:v,:qty,:dr,:cb)"),
                    {"sn": item.supply_name, "v": item.variety, "qty": item.quantity, "dr": item.date_received, "cb": item.changed_by})
            else:
                db.execute(text("INSERT INTO inventory_items (supply_name,quantity,date_received) VALUES (:sn,:qty,:dr)"),
                    {"sn": item.supply_name, "qty": item.quantity, "dr": item.date_received})
            db.commit()
            result = db.execute(text("SELECT * FROM inventory_items WHERE LOWER(supply_name)=LOWER(:sn) ORDER BY id DESC LIMIT 1"),
                {"sn": item.supply_name}).mappings().first()

        write_log(db, "inventory", item.supply_name, item.quantity,
                  detail=item.date_received, changed_by=item.changed_by, variety=item.variety)
        db.commit()
        return dict(result) if result else {"id": 0, "supply_name": item.supply_name, "quantity": item.quantity}

    except HTTPException:
        raise
    except Exception as e:
        print("POST /api/inventory ERROR:", traceback.format_exc(), flush=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/api/inventory/{item_id}")
def update_inventory_item(item_id: int, item: schemas.InventoryItemCreate, db: Session = Depends(get_db)):
    import traceback
    try:
        print(f"PUT /api/inventory/{item_id} — variety={item.variety!r}", flush=True)
        row = db.execute(text("SELECT * FROM inventory_items WHERE id=:id"), {"id": item_id}).mappings().first()
        if not row:
            raise HTTPException(status_code=404, detail="Item not found")
        if HAS_VARIETY:
            db.execute(text("UPDATE inventory_items SET supply_name=:sn,variety=:v,quantity=:qty,date_received=:dr,changed_by=:cb WHERE id=:id"),
                {"sn": item.supply_name, "v": item.variety, "qty": item.quantity, "dr": item.date_received, "cb": item.changed_by, "id": item_id})
            print(f"PUT inventory — variety={item.variety!r} saved", flush=True)
        else:
            db.execute(text("UPDATE inventory_items SET supply_name=:sn,quantity=:qty,date_received=:dr WHERE id=:id"),
                {"sn": item.supply_name, "qty": item.quantity, "dr": item.date_received, "id": item_id})
        db.commit()
        write_log(db, "inventory", item.supply_name, item.quantity,
                  detail=item.date_received, changed_by=item.changed_by, variety=item.variety)
        db.commit()
        result = db.execute(text("SELECT * FROM inventory_items WHERE id=:id"), {"id": item_id}).mappings().first()
        print(f"PUT inventory — result variety={dict(result).get('variety')!r}", flush=True)
        return dict(result)
    except HTTPException:
        raise
    except Exception as e:
        print("PUT /api/inventory ERROR:", traceback.format_exc(), flush=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/inventory/{item_id}", status_code=204)
def delete_inventory_item(item_id: int, db: Session = Depends(get_db)):
    row = db.execute(text("SELECT * FROM inventory_items WHERE id=:id"), {"id": item_id}).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Item not found")
    db.execute(text("DELETE FROM inventory_items WHERE id=:id"), {"id": item_id})
    db.commit()
    write_log(db, "inventory_deleted", row["supply_name"], row["quantity"], detail=row.get("date_received"))
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
        # Check available stock — fetch variety too
        inv = db.execute(text(
            "SELECT * FROM inventory_items WHERE LOWER(supply_name)=LOWER(:sn) LIMIT 1"
        ), {"sn": item.supply_name}).mappings().first()
        if not inv:
            raise HTTPException(status_code=400, detail=f"'{item.supply_name}' not found in inventory")
        if inv["quantity"] < item.quantity:
            raise HTTPException(status_code=400, detail=f"Only {inv['quantity']} unit(s) available")

        # Carry variety from inventory item
        variety = inv.get("variety") or item.variety or None

        # Deduct from inventory
        new_qty = inv["quantity"] - item.quantity
        if new_qty == 0:
            db.execute(text("DELETE FROM inventory_items WHERE id=:id"), {"id": inv["id"]})
        else:
            db.execute(text("UPDATE inventory_items SET quantity=:q WHERE id=:id"), {"q": new_qty, "id": inv["id"]})

        # Insert given-out row — include variety if column exists
        # Try full insert with all optional columns, fallback gracefully
        for giv_sql, giv_p in [
            ("INSERT INTO given_out_items (supply_name,variety,quantity,who_received,date_given,changed_by) VALUES (:sn,:v,:qty,:who,:dg,:cb)",
             {"sn":item.supply_name,"v":variety,"qty":item.quantity,"who":item.who_received,"dg":item.date_given,"cb":item.changed_by}),
            ("INSERT INTO given_out_items (supply_name,quantity,who_received,date_given,changed_by) VALUES (:sn,:qty,:who,:dg,:cb)",
             {"sn":item.supply_name,"qty":item.quantity,"who":item.who_received,"dg":item.date_given,"cb":item.changed_by}),
            ("INSERT INTO given_out_items (supply_name,quantity,who_received) VALUES (:sn,:qty,:who)",
             {"sn":item.supply_name,"qty":item.quantity,"who":item.who_received}),
        ]:
            try:
                db.execute(text("SAVEPOINT giv_ins"))
                db.execute(text(giv_sql), giv_p)
                db.execute(text("RELEASE SAVEPOINT giv_ins"))
                break
            except Exception:
                try: db.execute(text("ROLLBACK TO SAVEPOINT giv_ins"))
                except: pass

        # Log transaction — include variety if column exists
        write_log(db, "given_out", item.supply_name, item.quantity,
                  detail=item.who_received, date_given=item.date_given, changed_by=item.changed_by, variety=variety)

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
        write_log(db, "given_out", item.supply_name, item.quantity,
                  detail=item.who_received, date_given=item.date_given, changed_by=item.changed_by)
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
    write_log(db, "given_out_deleted", row["supply_name"], row["quantity"],
              detail=row["who_received"], date_given=row.get("date_given"))
    db.commit()


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
    import traceback
    try:
        # Fetch all log entries
        result = db.execute(text("SELECT * FROM transaction_log ORDER BY id")).mappings().all()
        logs = [dict(r) for r in result]

        # Fetch given_out_items to backfill missing date_given in log
        try:
            gi_rows = db.execute(text("SELECT supply_name, who_received, date_given FROM given_out_items")).mappings().all()
            # Build two maps for matching:
            # 1. (supply_name, who_received) -> date_given  (precise match)
            # 2. supply_name -> date_given                  (fallback)
            gi_map_precise = {}
            gi_map_supply  = {}
            for gi in gi_rows:
                sn  = (gi["supply_name"] or "").lower()
                who = (gi.get("who_received") or "").lower()
                dg  = gi.get("date_given")
                if dg:
                    key_precise = (sn, who)
                    if key_precise not in gi_map_precise:
                        gi_map_precise[key_precise] = dg
                    if sn not in gi_map_supply:
                        gi_map_supply[sn] = dg
        except Exception:
            gi_map_precise = {}
            gi_map_supply  = {}

        # Backfill date_given from given_out_items where missing in log
        for l in logs:
            if l.get("txn_type") in ("given_out", "given_out_deleted") and not l.get("date_given"):
                sn  = (l.get("supply_name") or "").lower()
                who = (l.get("detail") or "").lower()  # detail = who_received in log
                dg  = gi_map_precise.get((sn, who)) or gi_map_supply.get(sn)
                if dg:
                    l["date_given"] = dg

        inv_logs = [l for l in logs if l.get("txn_type") in ("inventory", "inventory_deleted")]
        giv_logs = [l for l in logs if l.get("txn_type") in ("given_out", "given_out_deleted")]

        def safe_log(l):
            return {
                "id":          l.get("id"),
                "txn_type":    l.get("txn_type"),
                "supply_name": l.get("supply_name"),
                "variety":     l.get("variety"),
                "quantity":    l.get("quantity", 0),
                "detail":      l.get("detail"),
                "date_given":  l.get("date_given"),
                "changed_by":  l.get("changed_by"),
                "created_at":  l.get("created_at"),
            }

        # Get actual current stock from live tables (not log totals)
        try:
            inv_stock = db.execute(text("SELECT COALESCE(SUM(quantity),0) AS total FROM inventory_items")).mappings().first()
            current_inv_units = int(inv_stock["total"] or 0)
        except Exception:
            current_inv_units = sum(l.get("quantity", 0) for l in inv_logs)

        try:
            giv_stock = db.execute(text("SELECT COALESCE(SUM(quantity),0) AS total FROM given_out_items")).mappings().first()
            current_giv_units = int(giv_stock["total"] or 0)
        except Exception:
            current_giv_units = sum(l.get("quantity", 0) for l in giv_logs)

        return {
            "inventory": {
                "total_lines": len(inv_logs),
                "total_units": current_inv_units,
                "items": [safe_log(l) for l in inv_logs]
            },
            "given_out": {
                "total_lines": len(giv_logs),
                "total_units": current_giv_units,
                "items": [safe_log(l) for l in giv_logs]
            }
        }
    except Exception as e:
        print("GET /api/summary ERROR:", traceback.format_exc(), flush=True)
        raise HTTPException(status_code=500, detail=str(e))


# ─── DEBUG — remove after confirming fix ─────────────────────────────────────

@app.get("/api/debug")
def debug_data(db: Session = Depends(get_db)):
    result = {}
    try:
        inv = db.execute(text("SELECT * FROM inventory_items ORDER BY id DESC LIMIT 5")).mappings().all()
        result["inventory_items"] = [dict(r) for r in inv]
    except Exception as e:
        result["inventory_items_error"] = str(e)
    try:
        gi = db.execute(text("SELECT * FROM given_out_items ORDER BY id DESC LIMIT 5")).mappings().all()
        result["given_out_items"] = [dict(r) for r in gi]
    except Exception as e:
        result["given_out_items_error"] = str(e)
    try:
        logs = db.execute(text("SELECT * FROM transaction_log ORDER BY id DESC LIMIT 5")).mappings().all()
        result["transaction_log"] = [dict(r) for r in logs]
    except Exception as e:
        result["transaction_log_error"] = str(e)
    result["flags"] = {
        "HAS_VARIETY":       HAS_VARIETY,
        "HAS_VARIETY_GIVEN": HAS_VARIETY_GIVEN,
        "HAS_VARIETY_TXN":   HAS_VARIETY_TXN,
        "HAS_DATE_GIVEN":    HAS_DATE_GIVEN,
        "HAS_CB_GIVEN":      HAS_CB_GIVEN,
        "HAS_TXN_DATE":      HAS_TXN_DATE,
        "HAS_CB_TXN":        HAS_CB_TXN,
    }
    return result


# ─── SERVE FRONTEND ──────────────────────────────────────────────────────────

frontend_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend")

@app.get("/", include_in_schema=False)
@app.get("/{full_path:path}", include_in_schema=False)
def serve_frontend(full_path: str = ""):
    if full_path.startswith("api/") or full_path in ("docs", "openapi.json"):
        raise HTTPException(status_code=404)
    return FileResponse(os.path.join(frontend_path, "index.html"))
