"""
Core Cryptography module for Upganam.
Provides authenticated chunked AES-256-GCM encryption, decryption, and PBKDF2 key derivation.
"""
import os
import struct

try:
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.exceptions import InvalidTag
    HAS_CRYPTOGRAPHY = True
except ImportError:
    HAS_CRYPTOGRAPHY = False
    InvalidTag = Exception

MAGIC_HEADER = b"UPG_ENC1"
CHUNK_SIZE = 1024 * 1024  # 1 MB chunk streaming

def is_crypto_available():
    """Check if the cryptography dependency is installed."""
    return HAS_CRYPTOGRAPHY

def derive_key(password: str, salt: bytes, iterations: int = 100000) -> bytes:
    """Derive a 256-bit cryptographic key from a user password using PBKDF2-HMAC-SHA256."""
    if not HAS_CRYPTOGRAPHY:
        raise RuntimeError("The 'cryptography' library is required. Install via: pip install cryptography")
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=iterations,
    )
    return kdf.derive(password.encode("utf-8"))

def is_encrypted_container(file_path: str) -> bool:
    """Check whether a given file starts with the Upganam encrypted container magic header."""
    if not os.path.isfile(file_path):
        return False
    try:
        with open(file_path, "rb") as f:
            header = f.read(len(MAGIC_HEADER))
            return header == MAGIC_HEADER
    except Exception:
        return False

def encrypt_file(src_path: str, dst_path: str, password: str, progress_callback=None):
    """
    Encrypt a source file into an authenticated AES-256-GCM container file.
    Streams in 1MB chunks with unique nonces and authenticated chunk indices.
    """
    if not HAS_CRYPTOGRAPHY:
        raise RuntimeError("The 'cryptography' library is required for encrypted backups. Install via: pip install cryptography")
    
    salt = os.urandom(16)
    key = derive_key(password, salt)
    aesgcm = AESGCM(key)
    
    total_size = os.path.getsize(src_path)
    processed = 0

    with open(src_path, "rb") as in_f, open(dst_path, "wb") as out_f:
        out_f.write(MAGIC_HEADER + salt)
        chunk_idx = 0
        while True:
            data = in_f.read(CHUNK_SIZE)
            is_last = 1 if len(data) < CHUNK_SIZE else 0
            nonce = os.urandom(12)
            aad = struct.pack(">IB", chunk_idx, is_last)
            ct = aesgcm.encrypt(nonce, data, aad)
            out_f.write(aad + nonce + struct.pack(">I", len(ct)) + ct)
            
            processed += len(data)
            if progress_callback and total_size > 0:
                progress_callback(processed, total_size)

            chunk_idx += 1
            if is_last:
                break

def decrypt_file(src_path: str, dst_path: str, password: str, progress_callback=None):
    """
    Decrypt an authenticated Upganam container file back to its original plain archive.
    Raises InvalidTag or ValueError on incorrect password or file corruption.
    """
    if not HAS_CRYPTOGRAPHY:
        raise RuntimeError("The 'cryptography' library is required for encrypted backups. Install via: pip install cryptography")
    
    with open(src_path, "rb") as in_f:
        magic = in_f.read(len(MAGIC_HEADER))
        if magic != MAGIC_HEADER:
            raise ValueError("Invalid file format. Not a recognized Upganam encrypted backup.")
        salt = in_f.read(16)
        if len(salt) < 16:
            raise ValueError("Corrupted backup file: salt header truncated.")
        
        key = derive_key(password, salt)
        aesgcm = AESGCM(key)
        
        with open(dst_path, "wb") as out_f:
            chunk_idx = 0
            while True:
                hdr = in_f.read(5)
                if not hdr:
                    break
                idx, is_last = struct.unpack(">IB", hdr)
                if idx != chunk_idx:
                    raise ValueError("Corrupted chunk order or missing data in encrypted backup.")
                
                nonce = in_f.read(12)
                ct_len_b = in_f.read(4)
                if len(ct_len_b) < 4:
                    raise ValueError("Truncated encrypted chunk length header.")
                
                ct_len = struct.unpack(">I", ct_len_b)[0]
                ct = in_f.read(ct_len)
                if len(ct) < ct_len:
                    raise ValueError("Truncated ciphertext payload.")
                
                pt = aesgcm.decrypt(nonce, ct, hdr)
                out_f.write(pt)
                chunk_idx += 1
                if is_last:
                    break
