from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import sqlite3
import uuid

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

DB_PATH = "ledger.db"
app = FastAPI(
    title="Transaction Ledger API",
    description="A simple account and transaction ledger API.",
    version="1.0.0",
)

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def get_connection():
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    conn = get_connection()
    try:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS accounts (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL CHECK(length(trim(name)) > 0),
            balance_cents INTEGER NOT NULL DEFAULT 0 CHECK(balance_cents >= 0),
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS transactions (
            id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            type TEXT NOT NULL CHECK(type IN ('credit', 'debit')),
            amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
            description TEXT NOT NULL,
            reference_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(account_id) REFERENCES accounts(id),
            UNIQUE(account_id, reference_id)
        );
        CREATE INDEX IF NOT EXISTS idx_transactions_account_created
            ON transactions(account_id, created_at DESC);
        """)
    finally:
        conn.close()

@app.on_event("startup")
def startup():
    init_db()

def money_to_cents(value) -> int:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise HTTPException(status_code=422, detail="amount must be a valid number")
    if not amount.is_finite() or amount <= 0:
        raise HTTPException(status_code=422, detail="amount must be greater than 0")
    if amount.as_tuple().exponent < -2:
        raise HTTPException(status_code=422, detail="amount may have at most 2 decimal places")
    return int(amount * 100)

def cents_to_money(cents: int) -> float:
    return float(Decimal(cents) / Decimal(100))

class AccountCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, value):
        if not value.strip():
            raise ValueError("name must not be blank")
        return value.strip()

class TransactionCreate(BaseModel):
    type: str
    amount: Decimal
    description: str = Field(min_length=1, max_length=500)
    reference_id: str = Field(min_length=1, max_length=120)

    @field_validator("type")
    @classmethod
    def valid_type(cls, value):
        value = value.lower().strip()
        if value not in {"credit", "debit"}:
            raise ValueError("type must be 'credit' or 'debit'")
        return value

    @field_validator("description", "reference_id")
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError("value must not be blank")
        return value.strip()

class TransferCreate(BaseModel):
    source_account_id: str
    destination_account_id: str
    amount: Decimal
    description: str = Field(default="Money transfer", min_length=1, max_length=500)
    reference_id: str = Field(min_length=1, max_length=120)

@app.get("/")
def root():
    return {"message": "Transaction Ledger API is running", "docs": "/docs"}

@app.post("/accounts", status_code=201)
def create_account(payload: AccountCreate):
    account_id = str(uuid.uuid4())
    created_at = now_iso()
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO accounts(id, name, balance_cents, created_at) VALUES (?, ?, 0, ?)",
            (account_id, payload.name, created_at),
        )
        return {
            "id": account_id,
            "name": payload.name,
            "balance": 0.00,
            "created_at": created_at,
        }
    finally:
        conn.close()

@app.get("/accounts/{account_id}/balance")
def get_balance(account_id: str):
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, balance_cents FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Account not found")
        return {"account_id": row["id"], "balance": cents_to_money(row["balance_cents"])}
    finally:
        conn.close()

@app.post("/accounts/{account_id}/transactions", status_code=201)
def add_transaction(account_id: str, payload: TransactionCreate):
    amount_cents = money_to_cents(payload.amount)
    conn = get_connection()
    try:
        # BEGIN IMMEDIATE serializes writes, preventing concurrent balance races.
        conn.execute("BEGIN IMMEDIATE")
        account = conn.execute(
            "SELECT id, balance_cents FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
        if account is None:
            conn.rollback()
            raise HTTPException(status_code=404, detail="Account not found")

        duplicate = conn.execute(
            "SELECT id FROM transactions WHERE account_id = ? AND reference_id = ?",
            (account_id, payload.reference_id),
        ).fetchone()
        if duplicate:
            conn.rollback()
            raise HTTPException(status_code=409, detail="Transaction reference_id already exists for this account")

        current = account["balance_cents"]
        if payload.type == "debit" and current < amount_cents:
            conn.rollback()
            raise HTTPException(status_code=400, detail="Insufficient balance")

        new_balance = current + amount_cents if payload.type == "credit" else current - amount_cents
        transaction_id = str(uuid.uuid4())
        created_at = now_iso()
        conn.execute(
            """INSERT INTO transactions
            (id, account_id, type, amount_cents, description, reference_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (transaction_id, account_id, payload.type, amount_cents,
             payload.description, payload.reference_id, created_at),
        )
        conn.execute(
            "UPDATE accounts SET balance_cents = ? WHERE id = ?",
            (new_balance, account_id),
        )
        conn.commit()
        return {
            "id": transaction_id,
            "account_id": account_id,
            "type": payload.type,
            "amount": cents_to_money(amount_cents),
            "description": payload.description,
            "reference_id": payload.reference_id,
            "created_at": created_at,
            "balance": cents_to_money(new_balance),
        }
    except HTTPException:
        raise
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise HTTPException(status_code=409, detail="Transaction conflicts with an existing record") from exc
    finally:
        conn.close()

@app.get("/accounts/{account_id}/transactions")
def transaction_history(
    account_id: str,
    type: str | None = Query(default=None, pattern="^(credit|debit)$"),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=10, ge=1, le=100),
):
    conn = get_connection()
    try:
        account = conn.execute("SELECT id FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if account is None:
            raise HTTPException(status_code=404, detail="Account not found")

        where = "WHERE account_id = ?"
        params = [account_id]
        if type is not None:
            where += " AND type = ?"
            params.append(type)

        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM transactions {where}", params
        ).fetchone()["n"]
        offset = (page - 1) * limit
        rows = conn.execute(
            f"""SELECT id, account_id, type, amount_cents, description, reference_id, created_at
                FROM transactions {where}
                ORDER BY created_at DESC, rowid DESC
                LIMIT ? OFFSET ?""",
            params + [limit, offset],
        ).fetchall()
        return {
            "account_id": account_id,
            "filters": {"type": type},
            "pagination": {
                "current_page": page,
                "page_size": limit,
                "total_records": total,
                "total_pages": (total + limit - 1) // limit,
            },
            "transactions": [
                {
                    "id": row["id"],
                    "account_id": row["account_id"],
                    "type": row["type"],
                    "amount": cents_to_money(row["amount_cents"]),
                    "description": row["description"],
                    "reference_id": row["reference_id"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ],
        }
    finally:
        conn.close()

@app.post("/transfers", status_code=201)
def transfer_money(payload: TransferCreate):
    amount_cents = money_to_cents(payload.amount)
    if payload.source_account_id == payload.destination_account_id:
        raise HTTPException(status_code=422, detail="Source and destination accounts must be different")

    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        source = conn.execute(
            "SELECT id, balance_cents FROM accounts WHERE id = ?",
            (payload.source_account_id,),
        ).fetchone()
        destination = conn.execute(
            "SELECT id, balance_cents FROM accounts WHERE id = ?",
            (payload.destination_account_id,),
        ).fetchone()
        if source is None or destination is None:
            conn.rollback()
            raise HTTPException(status_code=404, detail="Source or destination account not found")
        if source["balance_cents"] < amount_cents:
            conn.rollback()
            raise HTTPException(status_code=400, detail="Insufficient balance in source account")

        debit_ref = f"{payload.reference_id}:debit"
        credit_ref = f"{payload.reference_id}:credit"
        duplicate = conn.execute(
            """SELECT 1 FROM transactions
               WHERE (account_id = ? AND reference_id = ?)
                  OR (account_id = ? AND reference_id = ?)""",
            (payload.source_account_id, debit_ref, payload.destination_account_id, credit_ref),
        ).fetchone()
        if duplicate:
            conn.rollback()
            raise HTTPException(status_code=409, detail="Transfer reference_id already exists")

        created_at = now_iso()
        conn.execute(
            """INSERT INTO transactions
            (id, account_id, type, amount_cents, description, reference_id, created_at)
            VALUES (?, ?, 'debit', ?, ?, ?, ?)""",
            (str(uuid.uuid4()), payload.source_account_id, amount_cents,
             payload.description, debit_ref, created_at),
        )
        conn.execute(
            """INSERT INTO transactions
            (id, account_id, type, amount_cents, description, reference_id, created_at)
            VALUES (?, ?, 'credit', ?, ?, ?, ?)""",
            (str(uuid.uuid4()), payload.destination_account_id, amount_cents,
             payload.description, credit_ref, created_at),
        )
        conn.execute(
            "UPDATE accounts SET balance_cents = balance_cents - ? WHERE id = ?",
            (amount_cents, payload.source_account_id),
        )
        conn.execute(
            "UPDATE accounts SET balance_cents = balance_cents + ? WHERE id = ?",
            (amount_cents, payload.destination_account_id),
        )
        conn.commit()
        return {
            "message": "Transfer completed",
            "reference_id": payload.reference_id,
            "source_account_id": payload.source_account_id,
            "destination_account_id": payload.destination_account_id,
            "amount": cents_to_money(amount_cents),
            "created_at": created_at,
        }
    except HTTPException:
        raise
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise HTTPException(status_code=409, detail="Transfer conflicts with an existing record") from exc
    finally:
        conn.close()
