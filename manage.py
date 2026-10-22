"""Служебные команды.

Создать менеджера:
    python manage.py create-admin admin@example.com secret123 --name "Иван" --access full
"""

import argparse
import sys

from sqlalchemy import select

from app.database import AppSession
from app.models import User
from app.security import hash_password


def create_admin(email: str, password: str, access: str, name: str = "") -> int:
    email = email.strip().lower()
    name = name.strip()
    db = AppSession()
    try:
        existing = db.execute(
            select(User).where(User.email == email)
        ).scalar_one_or_none()
        if existing is not None:
            print(f"Пользователь {email} уже существует (id={existing.id}).")
            return 1
        user = User(
            name=name,
            email=email,
            password_hash=hash_password(password),
            role="manager",
            manager_access=access,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        print(f"Менеджер создан: {name or email} (id={user.id}, доступ={access}).")
        return 0
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Служебные команды Passengers Flow")
    sub = parser.add_subparsers(dest="command", required=True)

    p_admin = sub.add_parser("create-admin", help="Создать менеджера")
    p_admin.add_argument("email")
    p_admin.add_argument("password")
    p_admin.add_argument(
        "--name", default="", help="Имя менеджера",
    )
    p_admin.add_argument(
        "--access", choices=["view", "full"], default="full",
        help="Уровень доступа менеджера (по умолчанию full)",
    )

    args = parser.parse_args()
    if args.command == "create-admin":
        return create_admin(args.email, args.password, args.access, args.name)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
