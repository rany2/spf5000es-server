#!/usr/bin/env python3

"""Generate a random salt and hash a password for use in the config.ini file."""

import argparse
import hmac
from getpass import getpass
from hashlib import sha1
from secrets import token_hex
from typing import Optional


def hash_password(password: str, salt: str) -> str:
    """Hash a password using a salt."""
    return hmac.new(bytes(salt, "utf-8"), bytes(password, "utf-8"), sha1).hexdigest()


class HashPassArgs(argparse.Namespace):  # pylint: disable=too-few-public-methods
    """Class to hold the command line arguments."""

    password: Optional[str] = None
    password_file: Optional[str] = None


def main():
    """Generate a random salt and hash a password for use in the config.ini file."""
    parser = argparse.ArgumentParser(
        description="Hash a password for use in the config.ini file.",
        epilog="If no password is provided, you will be prompted for it.",
    )
    password_group = parser.add_mutually_exclusive_group()
    password_group.add_argument("password", nargs="?", help="The password to hash.")
    password_group.add_argument(
        "-f",
        "--password-file",
        help="The file containing the password to hash (UTF-8 encoded).",
    )
    args = parser.parse_args(namespace=HashPassArgs())

    password = None
    if args.password_file:
        with open(args.password_file, "r", encoding="utf-8") as file:
            password = file.read().strip("\n")
    elif args.password:
        password = args.password
    else:
        password = getpass("Enter your password: ")

    salt = token_hex(20)
    hashed_password = hash_password(password, salt)
    print("#" * 52)
    print(f"# {'Copy these values to your config.ini file.':^48} #")
    print(f"PASS_SALT = {salt}")
    print(f"PASS_HASH = {hashed_password}")
    print("#" * 52)


if __name__ == "__main__":
    main()
