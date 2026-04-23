"""Emit a fresh Fernet key for SECRETS_ENCRYPTION_KEY. Run once, put into secret manager."""
from cryptography.fernet import Fernet

if __name__ == "__main__":
    print(Fernet.generate_key().decode("ascii"))
