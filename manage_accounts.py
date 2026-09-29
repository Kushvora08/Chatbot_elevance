"""Account recovery for an operator with trusted access to the hosting shell."""

import argparse
from getpass import getpass
import os
from pathlib import Path

from .hosting_auth import AccountStore


def main():
    parser = argparse.ArgumentParser(description="Reset a hosted account password without exposing it in command history.")
    parser.add_argument("username")
    args = parser.parse_args()
    root = os.environ.get("APP_DATA_DIR")
    if not root or not (Path(root) / "accounts.sqlite3").is_file():
        parser.error("APP_DATA_DIR must point to the existing hosted data directory.")
    accounts = AccountStore(Path(root) / "accounts.sqlite3")
    user = next((user for user in accounts.users() if user["username"] == args.username.strip().casefold()), None)
    if not user:
        parser.error("Account not found.")
    password = getpass("New password (14-128 characters): ")
    if password != getpass("Confirm password: "):
        parser.error("Passwords do not match.")
    try:
        accounts.reset_password(user["id"], password)
    except ValueError as exc:
        parser.error(str(exc))
    print("Password changed. All existing sign-ins for this account have been revoked.")


if __name__ == "__main__":
    main()
