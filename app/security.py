import os
from datetime import datetime, timedelta, timezone
from typing import Any

from jose import jwt
from passlib.context import CryptContext

PWD_CONTEXT = CryptContext(
    schemes=["pbkdf2_sha256"],
    deprecated="auto",
)
JWT_SECRET = os.getenv("JWT_SECRET", "dev-only-change-me")
JWT_ALG = os.getenv("JWT_ALG", "HS256")
JWT_EXPIRE_MINUTES = int(os.getenv("JWT_EXPIRE_MINUTES", "120"))

def hash_password(password: str) -> str:
    return PWD_CONTEXT.hash(password)

def verify_password(plain: str, hashed: str) -> bool:
    return PWD_CONTEXT.verify(plain, hashed)

def create_access_token(payload: dict[str, Any], expires_minutes: int | None = None) -> str:
    minutes = expires_minutes or JWT_EXPIRE_MINUTES
    exp = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    to_encode = {**payload, "exp": exp}
    return jwt.encode(to_encode, JWT_SECRET, algorithm=JWT_ALG)

def decode_token(token: str) -> dict[str, Any]:
    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])

if __name__=='__main__':
    print(hash_password('123456')) # $bcrypt-sha256$v=2,t=2b,r=12$k1IuD2oo9bigKqhlqhxRZe$YZV3h9Zym1RApvjKejp.bB9892BuJ8G
    print(hash_password('123456'))
    print(verify_password("123456", "$bcrypt-sha256$v=2,t=2b,r=12$k1IuD2oo9bigKqhlqhxRZe$YZV3h9Zym1RApvjKejp.bB9892BuJ8G"))

    d={"username":"tom", "role":"admin"}
    print(create_access_token(d))  # eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VybmFtZSI6InRvbSIsInJvbGUiOiJhZG1pbiIsImV4cCI6MTc2NTg2MDU1NX0.2qrVQktSgTMhEF37FWW2Ic-Gl1CU8ieMXzNcqPobuig
    print(decode_token("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VybmFtZSI6InRvbSIsInJvbGUiOiJhZG1pbiIsImV4cCI6MTc2NTg2MDU1NX0.2qrVQktSgTMhEF37FWW2Ic-Gl1CU8ieMXzNcqPobuig"))