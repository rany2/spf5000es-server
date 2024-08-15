#!/usr/bin/env python3

"""Generate a salt and hash a password."""
import hashlib
import hmac
import secrets
from getpass import getpass


def hash_password(password: str, salt: str) -> str:
    """Hash a password using a salt."""
    return hmac.new(
        bytes(salt, "utf-8"), bytes(password, "utf-8"), hashlib.sha1
    ).hexdigest()


def main():
    """Generate a salt and hash a password."""
    password = getpass("Enter your password: ")
    salt = secrets.token_hex(20)
    print(f"Salt: {salt}")
    print(f"Hashed password: {hash_password(password, salt)}")


if __name__ == "__main__":
    main()
