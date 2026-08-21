"""E2E step 1 - seed the SENDER's local dev DB with an E2E copier, accounts and group.

Run with the SENDER repo's venv, cwd = the sender repo (so its .env / dev DB apply):

    cd D:\\Codes\\prop-dashboard
    .venv\\Scripts\\python.exe D:\\Codes\\copier-service\\scripts\\e2e_seed_sender.py

Idempotent: re-running rotates the E2E copier's key and prints the fresh one.
Output lines (consumed by the E2E driver):  COPIER_KEY=...  GROUP_ID=...  MASTER_ID=...
"""

from decimal import Decimal

from sqlalchemy import select

from app.db import SessionLocal
from app.models import AccountGroup, CopierInstance, GroupMember, Symbol, TradingAccount, User
from app.services import trading as svc

db = SessionLocal()

master = db.scalar(select(User).where(User.is_master.is_(True), User.is_active.is_(True)))
if master is None:
    master = User(display_name="Master Desk", is_master=True, sort_order=0, is_active=True)
    db.add(master)
    db.flush()

for code, desc in [("MNQU6", "Micro Nasdaq 100"), ("MGCZ6", "Micro Gold")]:
    if db.scalar(select(Symbol).where(Symbol.code == code)) is None:
        db.add(Symbol(code=code, description=desc, sort_order=10, is_active=True))

key = svc.generate_api_key()
copier = db.scalar(select(CopierInstance).where(CopierInstance.name == "E2E-COPIER"))
if copier is None:
    copier = CopierInstance(name="E2E-COPIER", api_key_hash=svc.hash_api_key(key), status="offline")
    db.add(copier)
    db.flush()
else:
    copier.api_key_hash = svc.hash_api_key(key)


def upsert_account(alias: str, ref: str, ratio: str) -> TradingAccount:
    account = db.scalar(select(TradingAccount).where(TradingAccount.account_ref == ref))
    if account is None:
        account = TradingAccount(alias=alias, account_ref=ref, copy_ratio=Decimal(ratio),
                                 copier_id=copier.id, is_active=True)
        db.add(account)
        db.flush()
    else:
        account.alias, account.copy_ratio = alias, Decimal(ratio)
        account.copier_id, account.is_active = copier.id, True
    return account


a1 = upsert_account("E2E-Acc-1", "E2E-REF1", "1")
a2 = upsert_account("E2E-Acc-2", "E2E-REF2", "2")

group = db.scalar(select(AccountGroup).where(AccountGroup.name == "E2E Group"))
if group is None:
    group = AccountGroup(name="E2E Group", sort_order=999, is_active=True)
    db.add(group)
    db.flush()
for account in (a1, a2):
    member = db.scalar(select(GroupMember).where(
        GroupMember.group_id == group.id, GroupMember.trading_account_id == account.id))
    if member is None:
        db.add(GroupMember(group_id=group.id, trading_account_id=account.id))

db.commit()
print(f"COPIER_KEY={key}")
print(f"GROUP_ID={group.id}")
print(f"MASTER_ID={master.id}")
