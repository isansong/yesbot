import json
import os
import re
import secrets
import threading
import time

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

# main.py에서 `from api import ...`가 먼저 실행될 수 있으므로
# 여기서도 .env를 읽어야 API_KEY를 가져올 수 있다.
load_dotenv()

app = FastAPI()

JSON_DB = "database.json"
API_KEY = os.getenv("API_KEY")

# ==========================
# 인증 코드 저장소 (봇과 같은 프로세스에서 공유한다)
# ==========================
CODE_TTL = 600  # 코드 유효 시간(초): 10분
CODE_LENGTH = 6
# 헷갈리는 문자(0/O, 1/I)는 뺀다.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
ROBLOX_NAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")

_pending = {}  # code -> {"roblox": 닉네임, "key": 소문자 닉네임, "expires": 만료 시각}
_lock = threading.Lock()


def _purge_expired(now):
    for code in [c for c, v in _pending.items() if v["expires"] <= now]:
        del _pending[code]


def issue_code(roblox_name):
    """코드를 발급한다. 같은 닉네임에 유효한 코드가 있으면 그 코드를 다시 돌려준다."""
    name = roblox_name.strip()
    key = name.lower()
    now = time.time()
    with _lock:
        _purge_expired(now)
        for code, info in _pending.items():
            if info["key"] == key:
                return code, int(info["expires"] - now)

        while True:
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
            if code not in _pending:
                break
        _pending[code] = {"roblox": name, "key": key, "expires": now + CODE_TTL}
        return code, CODE_TTL


def get_pending(code):
    """유효한 코드의 정보를 돌려준다. 없거나 만료됐으면 None."""
    code = code.strip().upper()
    now = time.time()
    with _lock:
        _purge_expired(now)
        info = _pending.get(code)
        return dict(info) if info else None


def discard_code(code):
    """사용한 코드를 지운다."""
    with _lock:
        _pending.pop(code.strip().upper(), None)


# ==========================
# 공용
# ==========================
def require_key(x_api_key: str = Header(default="")):
    """로블록스 서버 스크립트만 호출할 수 있도록 API 키를 검사한다."""
    if not API_KEY or not secrets.compare_digest(
        x_api_key.encode(), API_KEY.encode()
    ):
        raise HTTPException(status_code=401, detail="unauthorized")


def find_verified(roblox_name):
    """database.json에서 해당 로블록스 닉네임의 인증 정보를 찾는다."""
    try:
        with open(JSON_DB, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None

    search_name = roblox_name.strip().lower()
    for user in data.values():
        if not isinstance(user, dict):
            continue
        saved_name = user.get("roblox")
        if saved_name and saved_name.strip().lower() == search_name:
            return user
    return None


# ==========================
# 엔드포인트
# ==========================
@app.get("/")
def root():
    return {"status": "online"}


@app.get("/roblox/{roblox_name}", dependencies=[Depends(require_key)])
def roblox_check(roblox_name: str):
    user = find_verified(roblox_name)
    if user:
        # discord_id 같은 불필요한 정보는 내보내지 않는다.
        return {
            "verified": True,
            "serial": user.get("serial"),
            "roblox": user.get("roblox"),
        }
    return {"verified": False}


class CodeRequest(BaseModel):
    roblox_name: str


@app.post("/verify/code", dependencies=[Depends(require_key)])
def verify_code(body: CodeRequest):
    """인증 맵에서 호출한다. 이미 인증됐으면 고유번호를, 아니면 인증 코드를 돌려준다."""
    name = body.roblox_name.strip()
    if not ROBLOX_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="invalid roblox name")

    user = find_verified(name)
    if user:
        return {
            "status": "already_verified",
            "serial": user.get("serial"),
            "roblox": user.get("roblox"),
        }

    code, expires_in = issue_code(name)
    return {"status": "ok", "code": code, "expires_in": expires_in}
