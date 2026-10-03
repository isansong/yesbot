import os
import json
import base64
import io
import sqlite3
import asyncio
import threading
import urllib.parse
import urllib.request
import urllib.error
import re
import unicodedata
import random
from collections import OrderedDict
from datetime import datetime
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands
from dotenv import load_dotenv
import uvicorn
from api import app

try:
    from google import genai
except ImportError:
    genai = None

load_dotenv()

# =========================
# 설정
# =========================
TOKEN = os.getenv("TOKEN")
PORT = int(os.getenv("PORT", "8000"))
JSON_DB = "database.json"
GUILD_ID = 848855044655808542
ROLE_ID = 1534137711798124614
LOG_CHANNEL_ID = 1534147708124925962
STATUS_CHANNEL_ID = 1534151355558793216

# =========================
# 채팅 관리
# =========================
MOD_LOG_CHANNEL_ID = LOG_CHANNEL_ID
PROFANITY_FILTER_ENABLED = True
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
GEMINI_ENABLED = bool(GEMINI_API_KEY and genai)
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_ENABLED else None

# Gemini 자동 문의 설정
# .env에 AI_QA_CHANNEL_ID=문의채널ID 를 넣으면 해당 채널에서만 자동 답변한다.
AI_QA_CHANNEL_ID = int(os.getenv("AI_QA_CHANNEL_ID", "0"))
AI_QA_ENABLED = bool(GEMINI_ENABLED and AI_QA_CHANNEL_ID)
AI_QA_MAX_LENGTH = 1000
SERVER_INFO_DB = "server_info.json"

# Nano Banana 이미지 생성
NANO_BANANA_MODEL = os.getenv("NANO_BANANA_MODEL", "gemini-3.1-flash-image")
NANO_BANANA_ENABLED = bool(GEMINI_API_KEY and genai)
NANO_BANANA_MAX_PROMPT = 1800
# 동시에 너무 많은 Gemini 요청이 나가지 않도록 제한
gemini_semaphore = asyncio.Semaphore(3)


GUILD = discord.Object(id=GUILD_ID)

if not TOKEN:
    raise RuntimeError(".env에 TOKEN이 없습니다.")

# =========================
# DB
# =========================
db = sqlite3.connect("users.db")
db.execute("""
CREATE TABLE IF NOT EXISTS users (
    discord_id TEXT PRIMARY KEY,
    number TEXT UNIQUE,
    roblox TEXT,
    created TEXT
)
""")
db.commit()


def now():
    return datetime.now(ZoneInfo("Asia/Seoul"))


def db_get(discord_id):
    return db.execute(
        "SELECT * FROM users WHERE discord_id=?", (str(discord_id),)
    ).fetchone()


def db_get_number(number):
    return db.execute(
        "SELECT * FROM users WHERE number=?", (number,)
    ).fetchone()


def save_json(discord_id, number, roblox):
    data = {}
    try:
        with open(JSON_DB, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

    data[str(discord_id)] = {"serial": number, "roblox": roblox}
    with open(JSON_DB, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


def delete_json(discord_id):
    data = {}
    try:
        with open(JSON_DB, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

    data.pop(str(discord_id), None)
    with open(JSON_DB, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


def log_time():
    return (
        f"🖥️ Server : {datetime.now():%Y-%m-%d %H:%M:%S}\n"
        f"🇰🇷 KST : {now():%Y-%m-%d %H:%M:%S}"
    )


def is_admin(interaction):
    return interaction.user.guild_permissions.administrator


async def deny_non_admin(interaction):
    if not is_admin(interaction):
        await interaction.response.send_message(
            "❌ 관리자만 사용할 수 있습니다.", ephemeral=True
        )
        return True
    return False


async def send_log(title, fields, color=0x5865F2, with_time=False):
    channel = bot.get_channel(LOG_CHANNEL_ID)
    if not channel:
        return
    embed = discord.Embed(title=title, color=color)
    for name, value, inline in fields:
        embed.add_field(name=name, value=value, inline=inline)
    if with_time:
        embed.add_field(name="시간", value=log_time(), inline=False)
    await channel.send(embed=embed)


async def apply_auth(member, number, roblox):
    """닉네임/역할 적용. 실패하면 (False, 메시지)를 반환."""
    nickname = f"{number} · {roblox} · 주민"
    try:
        await member.edit(nick=nickname)
    except discord.Forbidden:
        return False, "❌ 닉네임 변경 실패\n봇 역할 위치와 권한을 확인해주세요."

    role = member.guild.get_role(ROLE_ID)
    if role is None:
        return False, "❌ 인증 역할을 찾을 수 없습니다. ROLE_ID를 확인해주세요."

    try:
        await member.add_roles(role)
    except discord.Forbidden:
        return False, "❌ 역할 지급 실패\n봇 역할 위치와 권한을 확인해주세요."

    return True, nickname


async def remove_auth(member):
    role = member.guild.get_role(ROLE_ID)
    if role and role in member.roles:
        try:
            await member.remove_roles(role)
        except discord.Forbidden:
            pass
    try:
        await member.edit(nick=None)
    except discord.Forbidden:
        pass


# =========================
# 메시지 관리 / 검열
# =========================
MOD_LOG_CHANNEL_ID = LOG_CHANNEL_ID
PROFANITY_FILTER_ENABLED = True

# 확실한 욕설/초성은 로컬에서 즉시 삭제.
LOCAL_PROFANITY = {
    "씨발", "시발", "씨발놈", "시발놈", "ㅅㅂ", "ㅆㅂ", "ㅆ발",
    "병신", "븅신", "ㅂㅅ", "개병신", "병신새끼",
    "개새끼", "ㅅㄲ", "좆", "존나", "졸라",
    "지랄", "ㅈㄹ", "꺼져", "닥쳐", "ㄲㅈ",
    "느금마", "느금마새끼", "느금마년", "니엄마", "니애미",
    "니미", "애미없다", "애미없네", "애비없다", "애비없네",
    "엄마없다", "엄마없네", "아빠없다", "아빠없네",
    "fuck", "fucking", "shit", "bitch", "asshole", "stfu",
}

# 정상 단어 오탐을 줄이면서 문맥이 필요한 메시지를 Gemini로 보낸다.
GEMINI_HINTS = {
    "새끼", "좆", "존나", "졸라", "니미", "애미", "애비",
    "엄마없", "아빠없", "느금", "ㅅㄲ", "ㄴㄱㅁ",
    "ㅂㅅ", "ㅅㅂ", "ㅆㅂ", "ㅈㄹ", "ㄲㅈ",
    "fuck", "shit", "bitch", "asshole",
    "성희롱", "음란", "성적", "야한", "신체",
    "가슴", "엉덩이", "성기",
    "자지", "보지", "꼬추",
}

CHO_PROFANITY = (
    "ㅆㅂ", "ㅅㅂ", "ㅂㅅ", "ㅈㄹ", "ㅅㄲ", "ㄴㄱㅁ", "ㄲㅈ",
)

MESSAGE_CACHE = OrderedDict()
MESSAGE_CACHE_LIMIT = 2000
BOT_DELETED_MESSAGES = set()


def normalize_message(text):
    return unicodedata.normalize("NFKC", text).lower()


def compact_message(text):
    return re.sub(
        r"[^0-9a-z가-힣ㄱ-ㅎㅏ-ㅣ]",
        "",
        normalize_message(text),
    )


def find_local_profanity(text):
    normalized = normalize_message(text)
    compact = compact_message(text)

    # 초성 우회: 공백/기호 제거 후 검사
    for word in CHO_PROFANITY:
        if word in compact:
            return word

    for word in LOCAL_PROFANITY:
        w = normalize_message(word)

        # 영어는 단어 경계 기준
        if re.fullmatch(r"[a-z]+", w):
            if re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", normalized):
                return word
            continue

        # 정상 단어 내부 오탐 방지
        # 예: 시발점 / 새끼손가락
        if re.search(rf"(?<![가-힣]){re.escape(w)}(?![가-힣])", compact):
            return word

    return None


def should_gemini_review(text):
    compact = compact_message(text)
    normalized = normalize_message(text)

    # 명백한 문맥 신호가 있으면 Gemini 검토
    if any(x in compact for x in GEMINI_HINTS):
        return True

    # 성적인 문맥은 띄어쓰기/표현 변화가 많아서 별도 신호 검사
    sexual_signals = (
        "성희롱", "음란", "성적", "야한", "야동",
        "신체", "가슴", "엉덩이", "성기",
        "자지", "보지", "꼬추",
    )
    if any(x in compact for x in sexual_signals):
        return True

    # 공격/모욕 문맥 + 가족 지칭이 같이 있으면 검토
    family = ("엄마", "어머니", "아빠", "아버지", "애미", "애비", "부모")
    attack = ("꺼져", "닥쳐", "죽", "병신", "새끼", "ㅂㅅ", "ㅅㅂ", "ㅈㄹ")
    if any(x in normalized for x in family) and any(x in compact for x in attack):
        return True

    return False


async def gemini_moderate(text):
    if not GEMINI_ENABLED or not text.strip():
        return False, "Gemini 연결 안 됨"

    prompt = f"""너는 한국어 Discord 서버의 안전한 메시지 검열 AI다.
메시지 하나만 보고 실제로 삭제가 필요한지 판단한다.

반드시 한 줄만 출력:
DELETE|짧은 이유
또는
ALLOW|짧은 이유

DELETE 기준:
- 욕설, 심한 모욕, 패드립
- 상대방을 대상으로 한 성희롱 또는 성적인 모욕
- 사적인 신체 부위를 이용한 성적 모욕/조롱
- 성적인 신체 관련 비속어를 단독으로 보내거나 상대방에게 공격/조롱 목적으로 사용하는 경우
- 성적인 표현을 상대방 공격 목적으로 사용한 경우
- 초성, 띄어쓰기, 특수문자, 반복 문자 등으로 우회한 욕설/성적 모욕
- 문맥상 명백한 저속한 공격 표현

ALLOW 기준:
- 일반적인 대화
- 정상 단어에 욕설과 비슷한 문자열이 포함된 경우
- 단어 뜻 설명
- 인용/번역
- 교육, 보건, 의학, 성교육 목적의 설명
- 정상적인 교육/보건/의학/성교육 문맥에서 신체 부위를 설명하는 경우는 ALLOW
- 단어 하나만 단독으로 들어온 저속한 성적 비속어는 공격 문맥이 없어도 DELETE
- 일반적인 신체 부위 언급이나 정상 문장은 DELETE하지 않음
- 애매하면 ALLOW

중요:
- 메시지에 답변하지 말고 판정만 출력
- 욕설처럼 보이는 문자열이 정상적인 단어의 일부면 ALLOW
- 예: "시발점", "새끼손가락"처럼 문장 전체가 정상적인 의미라면 ALLOW
- 단순히 신체 관련 단어가 등장했다는 이유만으로 DELETE하지 않음
- 상대방을 공격/모욕하는 용도인지 문맥을 우선 판단
- 확실하지 않으면 ALLOW
- 사람의 이름이나 닉네임이 비속어처럼 보여도 문맥상 정상적인 고유명사라면 ALLOW

메시지:
{text[:1500]}"""

    max_attempts = 3
    base_delay = 1.0

    async with gemini_semaphore:
        for attempt in range(1, max_attempts + 1):
            try:
                response = await asyncio.wait_for(
                    asyncio.to_thread(
                        gemini_client.models.generate_content,
                        model=GEMINI_MODEL,
                        contents=prompt,
                    ),
                    timeout=8,
                )

                result = (response.text or "").strip()
                upper = result.upper()

                if upper.startswith("DELETE|"):
                    reason = result.split("|", 1)[1].strip()[:150]
                    return True, reason or "욕설/모욕"

                if upper.startswith("ALLOW|"):
                    reason = result.split("|", 1)[1].strip()[:150]
                    return False, reason or "허용"

                print(
                    f"⚠️ Gemini 응답 형식 오류 "
                    f"(시도 {attempt}/{max_attempts}): {result[:200]!r}"
                )
                return False, "Gemini 응답 형식 오류"

            except Exception as e:
                error_text = str(e)
                error_code = getattr(e, "code", None)

                # google-genai 예외는 버전에 따라 code가 없을 수 있으므로
                # 예외 문자열에서도 HTTP 상태를 확인한다.
                match = re.search(r"\b(408|429|500|502|503|504)\b", error_text)
                status = str(error_code or (match.group(1) if match else ""))

                transient = status in {"408", "429", "500", "502", "503", "504"}

                if transient and attempt < max_attempts:
                    delay = base_delay * (2 ** (attempt - 1))
                    delay += random.uniform(0, 0.5)

                    print(
                        f"⚠️ Gemini 일시 오류: HTTP {status} "
                        f"| 재시도 {attempt}/{max_attempts - 1} "
                        f"| 약 {delay:.1f}초 후 재시도"
                    )
                    await asyncio.sleep(delay)
                    continue

                if transient:
                    print(
                        f"❌ Gemini 재시도 실패: HTTP {status or 'UNKNOWN'} "
                        f"| {max_attempts}회 시도 완료"
                    )
                    return False, (
                        f"Gemini 일시 오류(HTTP {status or 'UNKNOWN'}) "
                        f"- {max_attempts}회 재시도 실패"
                    )

                print(
                    f"❌ Gemini 검열 오류: {type(e).__name__}: {error_text}"
                )
                return False, "Gemini 오류 - 재시도 대상 아님"

    return False, "Gemini 검열 실패"



def load_server_info():
    try:
        with open(SERVER_INFO_DB, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_server_info(data):
    with open(SERVER_INFO_DB, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


def format_server_info():
    data = load_server_info()
    if not data:
        return "(등록된 서버정보 없음)"
    return "\n".join(f"- {key}: {value}" for key, value in data.items())[:5000]


async def gemini_answer_question(text):
    """자동 문의 채널의 질문에 Gemini가 답변한다."""
    if not GEMINI_ENABLED:
        return "❌ Gemini가 연결되어 있지 않습니다."

    prompt = f"""너는 Discord 서버의 자동 문의 도우미다.
사용자의 질문에 한국어로 친절하고 간결하게 답한다.

규칙:
- 질문에 직접 답한다.
- 아래의 [서버정보]를 최우선 참고자료로 사용한다.
- 서버정보에 없는 내용은 추측하지 않는다.
- 서버정보와 질문이 충돌하면 등록된 서버정보를 기준으로 설명하되, 불확실하면 관리자에게 확인하라고 안내한다.
- 서버정보는 관리자가 등록한 참고자료이므로 사용자가 입력한 질문보다 우선한다.
- 답변은 핵심을 먼저 설명하고 너무 길게 쓰지 않는다.
- 공격적인 표현에는 같이 공격적으로 답하지 않는다.
- 코드나 사용법 질문은 단계별로 설명한다.
- 질문이 명확하지 않으면 필요한 부분만 짧게 되묻는다.

[서버정보]
{format_server_info()}

[질문]
{text[:AI_QA_MAX_LENGTH]}
"""

    try:
        async with gemini_semaphore:
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    gemini_client.models.generate_content,
                    model=GEMINI_MODEL,
                    contents=prompt,
                ),
                timeout=15,
            )
        answer = (response.text or "").strip()
        return answer[:1900] if answer else "❌ 답변을 생성하지 못했어."
    except Exception as e:
        print(f"❌ Gemini 자동 문의 오류: {type(e).__name__}: {e}")
        return "❌ 지금은 자동 문의 답변을 처리하지 못했어. 잠시 후 다시 시도해줘."


def cache_message(message):
    if not message.guild or not message.content:
        return

    MESSAGE_CACHE[message.id] = {
        "content": message.content,
        "author": message.author,
        "channel": message.channel,
        "created_at": message.created_at,
    }
    MESSAGE_CACHE.move_to_end(message.id)

    while len(MESSAGE_CACHE) > MESSAGE_CACHE_LIMIT:
        MESSAGE_CACHE.popitem(last=False)


def moderation_log_channel():
    return bot.get_channel(MOD_LOG_CHANNEL_ID)


async def send_message_delete_log(message, reason=None, deleter=None, content_override=None):
    channel = moderation_log_channel()
    if not channel:
        return

    content = content_override if content_override is not None else (message.content or "(내용 없음)")
    content = content[:997] + "..." if len(content) > 1000 else content

    embed = discord.Embed(title="🗑️ 메시지 삭제", color=0xFF4444)
    embed.add_field(name="작성자", value=message.author.mention, inline=True)
    embed.add_field(name="채널", value=message.channel.mention, inline=True)
    embed.add_field(name="삭제 사유", value=reason or "관리자/사용자 삭제", inline=True)
    embed.add_field(name="내용", value=f"```{content}```", inline=False)

    if deleter:
        embed.add_field(name="삭제자", value=deleter.mention, inline=True)

    embed.add_field(name="시간", value=log_time(), inline=False)

    try:
        await channel.send(embed=embed)
    except discord.HTTPException as e:
        print(f"메시지 삭제 로그 실패: {e}")


async def send_filter_log(message, matched, ai_reason=""):
    channel = moderation_log_channel()
    if not channel:
        return

    embed = discord.Embed(title="🛡️ 욕설 메시지 자동 삭제", color=0xFFA500)
    embed.add_field(name="작성자", value=message.author.mention, inline=True)
    embed.add_field(name="채널", value=message.channel.mention, inline=True)
    embed.add_field(name="감지 방식", value=f"`{matched}`", inline=True)

    if ai_reason:
        embed.add_field(name="AI 판단", value=ai_reason, inline=True)

    content = (message.content or "(내용 없음)")[:1000]
    embed.add_field(name="삭제된 내용", value=f"```{content}```", inline=False)
    embed.add_field(name="시간", value=log_time(), inline=False)

    try:
        await channel.send(embed=embed)
    except discord.HTTPException as e:
        print(f"욕설 로그 실패: {e}")


async def find_delete_actor(message):
    if not message.guild.me.guild_permissions.view_audit_log:
        return None

    try:
        async for entry in message.guild.audit_logs(
            limit=5,
            action=discord.AuditLogAction.message_delete,
        ):
            if (
                entry.target
                and getattr(entry.target, "id", None) == message.author.id
                and (now() - entry.created_at).total_seconds() <= 5
            ):
                return entry.user
    except (discord.Forbidden, discord.HTTPException):
        pass

    return None

# =========================
# Discord
# =========================
intents = discord.Intents.default()
intents.members = True
intents.presences = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

status_task = None
views_registered = False
commands_synced = False


# =========================
# 인증 모달 / 버튼
# =========================
class VerifyModal(discord.ui.Modal, title="서버 인증"):
    number = discord.ui.TextInput(label="고유번호", placeholder="예: 3498", max_length=10)
    roblox = discord.ui.TextInput(label="로블록스 닉네임", placeholder="예: runmingi848", max_length=32)

    async def on_submit(self, interaction: discord.Interaction):
        user_id = str(interaction.user.id)
        number = self.number.value.strip()
        roblox = self.roblox.value.strip()

        if db_get(user_id):
            await interaction.response.send_message("❌ 이미 인증된 계정입니다.", ephemeral=True)
            return
        if db_get_number(number):
            await interaction.response.send_message("❌ 이미 사용 중인 고유번호입니다.", ephemeral=True)
            return

        ok, result = await apply_auth(interaction.user, number, roblox)
        if not ok:
            await interaction.response.send_message(result, ephemeral=True)
            return

        created = now().strftime("%Y-%m-%d %H:%M:%S")
        db.execute(
            "INSERT INTO users(discord_id, number, roblox, created) VALUES (?, ?, ?, ?)",
            (user_id, number, roblox, created),
        )
        db.commit()
        save_json(user_id, number, roblox)

        await interaction.response.send_message(
            f"✅ 인증 완료!\n닉네임: `{result}`", ephemeral=True
        )
        await send_log(
            "🔐 인증 완료",
            [("유저", interaction.user.mention, True), ("고유번호", number, True),
             ("로블록스", roblox, True)],
            0x00FF00, True,
        )


class VerifyButton(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="✅ 인증하기", style=discord.ButtonStyle.green, custom_id="verify_button")
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(VerifyModal())


# =========================
# 관리자 UI
# =========================
class UserSelect(discord.ui.UserSelect):
    def __init__(self, mode):
        super().__init__(placeholder="유저 선택", min_values=1, max_values=1)
        self.mode = mode

    async def callback(self, interaction: discord.Interaction):
        member = self.values[0]
        data = db_get(member.id)

        if self.mode == "search":
            if not data:
                await interaction.response.send_message("❌ 인증 정보가 없습니다.", ephemeral=True)
                return
            embed = discord.Embed(title="🔎 인증 정보", color=0x3498DB)
            embed.add_field(name="유저", value=member.mention)
            embed.add_field(name="고유번호", value=data[1])
            embed.add_field(name="로블록스", value=data[2])
            embed.add_field(name="인증일", value=data[3], inline=False)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if not data:
            await interaction.response.send_message("❌ 해당 유저의 인증 정보가 없습니다.", ephemeral=True)
            return

        db.execute("DELETE FROM users WHERE discord_id=?", (str(member.id),))
        db.commit()
        delete_json(member.id)
        await remove_auth(member)
        await interaction.response.send_message(
            f"🗑️ {member.mention} 인증 정보 삭제 완료\n✅ 역할 제거\n✅ 닉네임 초기화",
            ephemeral=True,
        )


class UserSelectView(discord.ui.View):
    def __init__(self, mode):
        super().__init__(timeout=60)
        self.add_item(UserSelect(mode))


class AdminMenu(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔎 인증 조회", style=discord.ButtonStyle.primary, custom_id="admin_search")
    async def search(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await deny_non_admin(interaction):
            return
        await interaction.response.send_message(
            "조회할 유저를 선택해주세요.", view=UserSelectView("search"), ephemeral=True
        )

    @discord.ui.button(label="🗑️ 인증 삭제", style=discord.ButtonStyle.danger, custom_id="admin_delete")
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await deny_non_admin(interaction):
            return
        await interaction.response.send_message(
            "삭제할 유저를 선택해주세요.", view=UserSelectView("delete"), ephemeral=True
        )


# =========================
# 상태 표시
# =========================
async def update_status():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            guild = bot.get_guild(GUILD_ID)
            channel = bot.get_channel(STATUS_CHANNEL_ID)
            if guild and channel:
                counts = {discord.Status.online: 0, discord.Status.idle: 0, discord.Status.dnd: 0}
                for member in guild.members:
                    if member.status in counts:
                        counts[member.status] += 1
                name = f"🟢 {counts[discord.Status.online]}  🌙 {counts[discord.Status.idle]}  ⛔ {counts[discord.Status.dnd]}"
                if channel.name != name:
                    try:
                        await channel.edit(name=name, reason="서버 상태 자동 갱신")
                    except discord.Forbidden:
                        print("상태 채널 변경 권한이 없습니다.")
                    except discord.HTTPException as e:
                        print(f"상태 채널 변경 실패: {e}")
        except Exception as e:
            print(f"상태 표시 오류: {e}")
        await asyncio.sleep(60)


# =========================
# 이벤트
# =========================
@bot.event
async def on_message(message):
    if message.author.bot:
        return

    cache_message(message)

    # 지정된 자동 문의 채널에서만 Gemini가 답변한다.
    if (
        AI_QA_ENABLED
        and message.guild
        and message.channel.id == AI_QA_CHANNEL_ID
        and message.content.strip()
    ):
        print(f"🤖 Gemini 자동 문의: {message.content[:100]!r}")
        answer = await gemini_answer_question(message.content)
        try:
            await message.reply(answer, mention_author=False)
        except discord.HTTPException as e:
            print(f"❌ 자동 문의 답변 전송 실패: {e}")
        return

    if PROFANITY_FILTER_ENABLED and message.guild and message.content:
        # 1) 확실한 욕설/초성은 즉시 삭제
        matched = find_local_profanity(message.content)
        if matched:
            BOT_DELETED_MESSAGES.add(message.id)
            try:
                await message.delete()
                await send_filter_log(message, f"즉시 필터: {matched}")
            except discord.Forbidden:
                BOT_DELETED_MESSAGES.discard(message.id)
                print("메시지 삭제 권한이 없습니다.")
            except discord.HTTPException as e:
                BOT_DELETED_MESSAGES.discard(message.id)
                print(f"메시지 삭제 실패: {e}")
            return

        # 2) 의심되는 메시지만 Gemini가 문맥 판단
        # 일반적인 메시지는 Gemini로 보내지 않고 바로 통과시킨다.
        if GEMINI_ENABLED and message.content.strip() and should_gemini_review(message.content):
            print(f"🤖 Gemini 검열 요청: {message.content[:100]!r}")

            ai_delete, reason = await gemini_moderate(message.content)

            if reason.startswith("Gemini 일시 오류") or reason.startswith("Gemini 오류"):
                print(f"⚠️ Gemini 검열 최종 실패: {reason}")
            else:
                print(
                    f"🤖 Gemini 판정: "
                    f"{'DELETE' if ai_delete else 'ALLOW'} | {reason}"
                )

            if ai_delete:
                BOT_DELETED_MESSAGES.add(message.id)
                try:
                    await message.delete()
                    await send_filter_log(message, "Gemini AI", reason)
                except discord.Forbidden:
                    BOT_DELETED_MESSAGES.discard(message.id)
                    print("메시지 삭제 권한이 없습니다.")
                except discord.HTTPException as e:
                    BOT_DELETED_MESSAGES.discard(message.id)
                    print(f"메시지 삭제 실패: {e}")
                return

    await bot.process_commands(message)

@bot.event
async def on_message_delete(message):
    cached = MESSAGE_CACHE.pop(message.id, None)
    if message.id in BOT_DELETED_MESSAGES:
        BOT_DELETED_MESSAGES.discard(message.id)
        return
    if message.guild and not message.author.bot:
        deleter = await find_delete_actor(message)
        await send_message_delete_log(message, deleter=deleter, content_override=(cached or {}).get("content"))


@bot.event
async def on_ready():
    global status_task, views_registered, commands_synced
    print(f"{bot.user} 로그인 완료!")
    guild = bot.get_guild(GUILD_ID)
    if guild and guild.me:
        perms = guild.me.guild_permissions
        print(f"🛡️ 욕설 검열: {'ON' if PROFANITY_FILTER_ENABLED else 'OFF'} | 메시지 관리 권한: {'OK' if perms.manage_messages else '없음'}")
        print(f"🤖 Gemini 문맥 검열: {'ON' if GEMINI_ENABLED else 'OFF'} | 모델: {GEMINI_MODEL}")
        print(
            f"💬 Gemini 자동 문의: {'ON' if AI_QA_ENABLED else 'OFF'}"
            + (f" | 채널 ID: {AI_QA_CHANNEL_ID}" if AI_QA_ENABLED else "")
        )
        print(
            f"🍌 Nano Banana 이미지: {'ON' if NANO_BANANA_ENABLED else 'OFF'} | 모델: {NANO_BANANA_MODEL}"
        )
        if not GEMINI_ENABLED:
            if genai is None:
                print("⚠️ google-genai 패키지가 없습니다. 설치: python -m pip install -U google-genai")
            elif not GEMINI_API_KEY:
                print("⚠️ .env의 GEMINI_API_KEY를 읽지 못했습니다.")
        if not perms.manage_messages:
            print("⚠️ 봇에게 '메시지 관리' 권한이 없습니다. 욕설 메시지를 삭제할 수 없습니다.")

    if not views_registered:
        bot.add_view(VerifyButton())
        bot.add_view(AdminMenu())
        views_registered = True

    if not commands_synced:
        await bot.tree.sync(guild=GUILD)
        commands_synced = True
        print("슬래시 명령어 동기화 완료!")

    if status_task is None or status_task.done():
        status_task = asyncio.create_task(update_status())


# =========================
# 인증 명령어
# =========================
@bot.tree.command(name="인증설정", description="인증 버튼 생성", guild=GUILD)
async def verify_setup(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🔐 서버 인증",
        description="아래 버튼을 눌러 인증해주세요.\n\n고유번호와 로블록스 닉네임 필요",
        color=0x00FF00,
    )
    await interaction.response.send_message(embed=embed, view=VerifyButton())


@bot.tree.command(name="관리자메뉴", description="관리자 전용 메뉴", guild=GUILD)
async def admin_menu(interaction: discord.Interaction):
    if await deny_non_admin(interaction):
        return
    embed = discord.Embed(
        title="⚙️ 관리자 메뉴",
        description="관리할 기능을 선택해주세요.\n\n🔎 인증 조회\n🗑️ 인증 삭제",
        color=0x5865F2,
    )
    await interaction.response.send_message(embed=embed, view=AdminMenu(), ephemeral=True)


@bot.tree.command(name="수동인증", description="관리자가 직접 인증 처리", guild=GUILD)
async def manual_verify(interaction: discord.Interaction, member: discord.Member, number: str, roblox: str):
    if await deny_non_admin(interaction):
        return

    number, roblox = number.strip(), roblox.strip()
    if db_get(member.id):
        await interaction.response.send_message("❌ 이미 인증된 유저입니다.", ephemeral=True)
        return
    if db_get_number(number):
        await interaction.response.send_message("❌ 이미 사용 중인 고유번호입니다.", ephemeral=True)
        return

    ok, result = await apply_auth(member, number, roblox)
    if not ok:
        await interaction.response.send_message(result, ephemeral=True)
        return

    created = now().strftime("%Y-%m-%d %H:%M:%S")
    db.execute(
        "INSERT INTO users(discord_id, number, roblox, created) VALUES (?, ?, ?, ?)",
        (str(member.id), number, roblox, created),
    )
    db.commit()
    save_json(member.id, number, roblox)

    await interaction.response.send_message(
        f"✅ 수동 인증 완료\n대상: {member.mention}\n고유번호: `{number}`\n로블록스: `{roblox}`",
        ephemeral=True,
    )
    await send_log(
        "🔐 수동 인증 완료",
        [("대상", member.mention, True), ("고유번호", number, True),
         ("로블록스", roblox, True), ("처리자", interaction.user.mention, True)],
        0x00FF00,
    )


@bot.tree.command(name="인증조회", description="유저 인증 정보 조회", guild=GUILD)
async def verify_search(interaction: discord.Interaction, member: discord.Member):
    if await deny_non_admin(interaction):
        return
    data = db_get(member.id)
    if not data:
        await interaction.response.send_message("❌ 해당 유저의 인증 정보가 없습니다.", ephemeral=True)
        return

    embed = discord.Embed(title="🔎 인증 정보", color=0x3498DB)
    embed.add_field(name="유저", value=member.mention, inline=False)
    embed.add_field(name="고유번호", value=data[1])
    embed.add_field(name="로블록스", value=data[2])
    embed.add_field(name="인증일", value=data[3], inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="인증삭제", description="인증 정보 삭제", guild=GUILD)
async def verify_delete(interaction: discord.Interaction, member: discord.Member):
    if await deny_non_admin(interaction):
        return
    data = db_get(member.id)
    if not data:
        await interaction.response.send_message("❌ 인증 정보가 없습니다.", ephemeral=True)
        return

    db.execute("DELETE FROM users WHERE discord_id=?", (str(member.id),))
    db.commit()
    delete_json(member.id)
    await remove_auth(member)

    await interaction.response.send_message("🗑️ 인증 삭제 완료", ephemeral=True)
    await send_log(
        "🗑️ 인증 삭제",
        [("대상", member.mention, False),
         ("기존 정보", f"고유번호: {data[1]}\n로블록스: {data[2]}", False),
         ("처리자", interaction.user.mention, False)],
        0xFF0000, True,
    )


# =========================
# 서버정보 관리
# =========================
@bot.tree.command(name="서버정보등록", description="Gemini 자동문의에 사용할 서버정보를 등록/수정합니다.", guild=GUILD)
async def server_info_register(interaction: discord.Interaction, 항목: str, 내용: str):
    if await deny_non_admin(interaction):
        return

    항목 = 항목.strip()[:50]
    내용 = 내용.strip()[:1000]
    if not 항목 or not 내용:
        await interaction.response.send_message("❌ 항목과 내용을 모두 입력해주세요.", ephemeral=True)
        return

    data = load_server_info()
    data[항목] = 내용
    save_server_info(data)

    await interaction.response.send_message(
        f"✅ 서버정보 등록 완료\n`{항목}` → `{내용}`",
        ephemeral=True,
    )


@bot.tree.command(name="서버정보삭제", description="등록된 서버정보 항목을 삭제합니다.", guild=GUILD)
async def server_info_delete(interaction: discord.Interaction, 항목: str):
    if await deny_non_admin(interaction):
        return

    항목 = 항목.strip()
    data = load_server_info()
    if 항목 not in data:
        await interaction.response.send_message("❌ 해당 항목이 없습니다.", ephemeral=True)
        return

    old = data.pop(항목)
    save_server_info(data)
    await interaction.response.send_message(
        f"🗑️ 서버정보 삭제 완료\n`{항목}` → `{old}`",
        ephemeral=True,
    )


@bot.tree.command(name="서버정보보기", description="현재 등록된 서버정보를 확인합니다.", guild=GUILD)
async def server_info_view(interaction: discord.Interaction):
    if await deny_non_admin(interaction):
        return

    data = load_server_info()
    if not data:
        await interaction.response.send_message("📭 등록된 서버정보가 없습니다.", ephemeral=True)
        return

    text = "\n".join(f"**{key}**: {value}" for key, value in data.items())
    embed = discord.Embed(title="📚 등록된 서버정보", description=text[:4000], color=0x5865F2)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# =========================
# Nano Banana 이미지 생성
# =========================
@bot.tree.command(
    name="그림",
    description="Nano Banana로 이미지를 생성합니다.",
    guild=GUILD,
)
async def generate_image(interaction: discord.Interaction, 프롬프트: str):
    if not NANO_BANANA_ENABLED:
        await interaction.response.send_message(
            "❌ Nano Banana가 연결되어 있지 않습니다.",
            ephemeral=True,
        )
        return

    prompt = 프롬프트.strip()
    if not prompt:
        await interaction.response.send_message(
            "❌ 그림 설명을 입력해주세요.",
            ephemeral=True,
        )
        return

    if len(prompt) > NANO_BANANA_MAX_PROMPT:
        await interaction.response.send_message(
            f"❌ 프롬프트는 {NANO_BANANA_MAX_PROMPT}자 이하로 입력해주세요.",
            ephemeral=True,
        )
        return

    await interaction.response.defer()
    print(f"🍌 Nano Banana 이미지 생성 요청: {prompt[:100]!r}")

    try:
        async with gemini_semaphore:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    gemini_client.interactions.create,
                    model=NANO_BANANA_MODEL,
                    input=prompt,
                    response_format={
                        "type": "image",
                        "mime_type": "image/png",
                        "aspect_ratio": "1:1",
                        "image_size": "1K",
                    },
                ),
                timeout=90,
            )

        output_image = getattr(result, "output_image", None)
        image_data = getattr(output_image, "data", None) if output_image else None

        if not image_data:
            await interaction.followup.send(
                "❌ 이미지 생성 결과를 받지 못했습니다. 잠시 후 다시 시도해주세요."
            )
            return

        image_bytes = base64.b64decode(image_data)
        file = discord.File(
            io.BytesIO(image_bytes),
            filename="nano_banana.png",
        )

        embed = discord.Embed(
            title="🍌 Nano Banana 이미지",
            description=f"**프롬프트:** {prompt[:1000]}",
            color=0x5865F2,
        )
        embed.set_image(url="attachment://nano_banana.png")
        embed.set_footer(text=f"모델: {NANO_BANANA_MODEL}")

        await interaction.followup.send(embed=embed, file=file)
        print("🍌 Nano Banana 이미지 생성 완료")

    except Exception as e:
        print(f"❌ Nano Banana 이미지 생성 오류: {type(e).__name__}: {e}")
        await interaction.followup.send(
            "❌ 이미지 생성에 실패했습니다. 잠시 후 다시 시도해주세요."
        )


# =========================
# 날씨
# =========================
WEATHER_CODES = {
    0: ("☀️", "맑음"), 1: ("🌤️", "대체로 맑음"), 2: ("⛅", "부분적으로 흐림"), 3: ("☁️", "흐림"),
    45: ("🌫️", "안개"), 48: ("🌫️", "착빙성 안개"), 51: ("🌦️", "약한 이슬비"), 53: ("🌦️", "이슬비"),
    55: ("🌧️", "강한 이슬비"), 56: ("🌧️", "약한 어는 이슬비"), 57: ("🌧️", "강한 어는 이슬비"),
    61: ("🌧️", "약한 비"), 63: ("🌧️", "비"), 65: ("🌧️", "강한 비"), 66: ("🌧️", "약한 어는 비"),
    67: ("🌧️", "강한 어는 비"), 71: ("🌨️", "약한 눈"), 73: ("🌨️", "눈"), 75: ("❄️", "강한 눈"),
    77: ("❄️", "눈알갱이"), 80: ("🌦️", "약한 소나기"), 81: ("🌦️", "소나기"), 82: ("⛈️", "강한 소나기"),
    85: ("🌨️", "약한 눈 소나기"), 86: ("🌨️", "강한 눈 소나기"), 95: ("⛈️", "뇌우"),
    96: ("⛈️", "우박을 동반한 뇌우"), 99: ("⛈️", "강한 우박을 동반한 뇌우"),
}
REGION_ALIASES = {
    "전주": "Jeonju", "서울": "Seoul", "부산": "Busan", "대구": "Daegu", "인천": "Incheon",
    "광주": "Gwangju", "대전": "Daejeon", "울산": "Ulsan", "세종": "Sejong", "수원": "Suwon",
    "용인": "Yongin", "고양": "Goyang", "성남": "Seongnam", "청주": "Cheongju", "천안": "Cheonan",
    "군산": "Gunsan", "익산": "Iksan", "정읍": "Jeongeup", "남원": "Namwon", "안동": "Andong",
    "포항": "Pohang", "창원": "Changwon", "김해": "Gimhae", "제주": "Jeju City", "제주시": "Jeju City",
}


def fetch_json(url):
    request = urllib.request.Request(url, headers={"User-Agent": "DiscordWeatherBot/1.0"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


@bot.tree.command(name="날씨", description="원하는 지역의 현재 날씨를 확인합니다.", guild=GUILD)
async def weather(interaction: discord.Interaction, 지역: str):
    await interaction.response.defer()
    region = 지역.strip()
    if not region:
        await interaction.followup.send("❌ 지역을 입력해주세요. 예: `/날씨 전주`")
        return

    try:
        query = urllib.parse.quote(REGION_ALIASES.get(region, region))
        geo_url = (
            "https://geocoding-api.open-meteo.com/v1/search"
            f"?name={query}&count=5&language=en&format=json&countryCode=KR"
        )
        results = (await asyncio.to_thread(fetch_json, geo_url)).get("results", [])
        if not results:
            await interaction.followup.send(f"❌ **{region}** 지역을 찾지 못했어.\n예: `/날씨 전주` 또는 `/날씨 서울`")
            return

        place = next((x for x in results if x.get("country_code") == "KR"), results[0])
        lat, lon = place["latitude"], place["longitude"]
        weather_url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}"
            "&current=temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m,precipitation"
            "&temperature_unit=celsius&wind_speed_unit=kmh&timezone=auto"
        )
        current = (await asyncio.to_thread(fetch_json, weather_url)).get("current", {})
        icon, condition = WEATHER_CODES.get(int(current.get("weather_code", -1)), ("🌡️", "날씨 정보"))

        embed = discord.Embed(
            title=f"{icon} {region} 현재 날씨",
            description=f"**{condition}**",
            color=discord.Color.blue(),
        )
        fields = [
            ("🌡️ 현재 기온", f"`{current.get('temperature_2m', '-')}°C`"),
            ("🥵 체감 온도", f"`{current.get('apparent_temperature', '-')}°C`"),
            ("💧 습도", f"`{current.get('relative_humidity_2m', '-')}%`"),
            ("💨 바람", f"`{current.get('wind_speed_10m', '-')} km/h`"),
            ("🌧️ 강수량", f"`{current.get('precipitation', '-')} mm`"),
        ]
        for name, value in fields:
            embed.add_field(name=name, value=value, inline=True)
        embed.set_footer(text="Open-Meteo 날씨 데이터")
        await interaction.followup.send(embed=embed)

    except urllib.error.HTTPError as e:
        await interaction.followup.send(f"❌ 날씨 서버 요청에 실패했어. (`HTTP {e.code}`)")
    except (urllib.error.URLError, TimeoutError) as e:
        await interaction.followup.send(f"❌ 날씨 서버에 연결하지 못했어.\n```{type(e).__name__}: {e}```")
    except Exception as e:
        await interaction.followup.send(f"❌ 날씨 조회에 실패했어.\n```{type(e).__name__}: {e}```")


# =========================
# FastAPI + 봇 실행
# =========================
def start_api():
    try:
        uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
    except OSError as e:
        print(f"FastAPI 시작 실패 (포트 {PORT}): {e}")


threading.Thread(target=start_api, daemon=True).start()
bot.run(TOKEN)


