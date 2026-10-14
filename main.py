import asyncio
import io
import json
import os
import re
import sqlite3
import threading
from collections import Counter, OrderedDict
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp
import discord
import uvicorn
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

from api import app, discard_code, get_pending  # noqa: E402  (load_dotenv 이후에 불러온다)

# =========================
# 설정
# =========================
TOKEN = os.getenv("TOKEN")
PORT = int(os.getenv("PORT", "8000"))
JSON_DB = "database.json"
KST = ZoneInfo("Asia/Seoul")

GUILD_ID = 848855044655808542
ROLE_ID = 1534137711798124614
LOG_CHANNEL_ID = 1534147708124925962
STATUS_CHANNEL_ID = 1534151355558793216

# 티켓 시스템 (.env에서 선택적으로 변경 가능)
TICKET_LOG_CHANNEL_ID = int(os.getenv("TICKET_LOG_CHANNEL_ID", str(LOG_CHANNEL_ID)))
TICKET_CATEGORY_ID = int(os.getenv("TICKET_CATEGORY_ID", "0"))
TICKET_STAFF_ROLE_ID = int(os.getenv("TICKET_STAFF_ROLE_ID", "0"))

GUILD = discord.Object(id=GUILD_ID)

if not TOKEN:
    raise RuntimeError(".env에 TOKEN이 없습니다.")


# =========================
# DB
# =========================
db = sqlite3.connect("users.db")
db.execute(
    """
    CREATE TABLE IF NOT EXISTS users (
        discord_id TEXT PRIMARY KEY,
        number TEXT UNIQUE,
        roblox TEXT,
        created TEXT
    )
    """
)
db.commit()


def now():
    return datetime.now(KST)


def db_get(discord_id):
    return db.execute(
        "SELECT * FROM users WHERE discord_id=?", (str(discord_id),)
    ).fetchone()


def db_get_number(number):
    return db.execute("SELECT * FROM users WHERE number=?", (number,)).fetchone()


def db_get_roblox(roblox):
    return db.execute(
        "SELECT * FROM users WHERE lower(roblox)=lower(?)", (roblox,)
    ).fetchone()


def next_serial():
    """고유번호를 1부터 순서대로 배정한다. (현재 가장 큰 번호 + 1)"""
    row = db.execute("SELECT MAX(CAST(number AS INTEGER)) FROM users").fetchone()
    return str((row[0] or 0) + 1)


def _read_json():
    try:
        with open(JSON_DB, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _write_json(data):
    # 임시 파일에 쓴 뒤 교체해서, API가 쓰다 만 파일을 읽는 일을 막는다.
    tmp = f"{JSON_DB}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
    os.replace(tmp, JSON_DB)


def save_json(discord_id, number, roblox):
    data = _read_json()
    data[str(discord_id)] = {"serial": number, "roblox": roblox}
    _write_json(data)


def delete_json(discord_id):
    data = _read_json()
    data.pop(str(discord_id), None)
    _write_json(data)


def delete_user(discord_id):
    db.execute("DELETE FROM users WHERE discord_id=?", (str(discord_id),))
    db.commit()
    delete_json(discord_id)


# =========================
# 공용 헬퍼
# =========================
def log_time():
    return (
        f"🖥️ Server : {datetime.now():%Y-%m-%d %H:%M:%S}\n"
        f"🇰🇷 KST : {now():%Y-%m-%d %H:%M:%S}"
    )


async def deny_non_admin(interaction):
    if not interaction.user.guild_permissions.administrator:
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
    try:
        await channel.send(embed=embed)
    except discord.HTTPException as e:
        print(f"로그 전송 실패: {e}")


def auth_info_embed(member, data):
    embed = discord.Embed(title="🔎 인증 정보", color=0x3498DB)
    embed.add_field(name="유저", value=member.mention, inline=False)
    embed.add_field(name="고유번호", value=data[1])
    embed.add_field(name="로블록스", value=data[2])
    embed.add_field(name="인증일", value=data[3], inline=False)
    return embed


async def apply_auth(member, number, roblox):
    """닉네임/역할 적용.

    닉네임은 바꿀 수 있는 유저만 바꾸고, 못 바꾸는 유저(서버장, 봇보다 높은 역할)는
    그대로 둔다. 역할 지급에 실패하면 (False, 메시지)를 반환하고,
    성공하면 (True, 바뀐 닉네임 또는 None)을 반환한다.
    """
    nickname = f"{number} · {roblox} · 주민"
    try:
        await member.edit(nick=nickname)
    except discord.Forbidden:
        nickname = None  # 서버장 등은 봇이 닉네임을 바꿀 수 없으니 건너뛴다.

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


async def register_member(member, number, roblox):
    """중복 확인 → 닉네임/역할 적용 → DB 저장. (성공 여부, 메시지)를 반환."""
    if db_get(member.id):
        return False, "❌ 이미 인증된 계정입니다."
    if db_get_number(number):
        return False, "❌ 이미 사용 중인 고유번호입니다."

    ok, result = await apply_auth(member, number, roblox)
    if not ok:
        return False, result

    db.execute(
        "INSERT INTO users(discord_id, number, roblox, created) VALUES (?, ?, ?, ?)",
        (str(member.id), number, roblox, now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    db.commit()
    save_json(member.id, number, roblox)
    return True, result


# =========================
# 삭제된 메시지 로그
# =========================
MESSAGE_CACHE = OrderedDict()
MESSAGE_CACHE_LIMIT = 2000


def cache_message(message):
    if not message.guild or not message.content:
        return
    MESSAGE_CACHE[message.id] = message.content
    MESSAGE_CACHE.move_to_end(message.id)
    while len(MESSAGE_CACHE) > MESSAGE_CACHE_LIMIT:
        MESSAGE_CACHE.popitem(last=False)


async def find_delete_actor(message):
    if not message.guild.me.guild_permissions.view_audit_log:
        return None
    try:
        async for entry in message.guild.audit_logs(
            limit=5, action=discord.AuditLogAction.message_delete
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


async def send_message_delete_log(message, deleter, content):
    channel = bot.get_channel(LOG_CHANNEL_ID)
    if not channel:
        return

    content = content or "(내용 없음)"
    if len(content) > 1000:
        content = content[:997] + "..."

    embed = discord.Embed(title="🗑️ 메시지 삭제", color=0xFF4444)
    embed.add_field(name="작성자", value=message.author.mention, inline=True)
    embed.add_field(name="채널", value=message.channel.mention, inline=True)
    embed.add_field(name="삭제자", value=deleter.mention if deleter else "알 수 없음", inline=True)
    embed.add_field(name="내용", value=f"```{content}```", inline=False)
    embed.add_field(name="시간", value=log_time(), inline=False)

    try:
        await channel.send(embed=embed)
    except discord.HTTPException as e:
        print(f"메시지 삭제 로그 실패: {e}")


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
REGISTER_LOCK = asyncio.Lock()  # 동시에 인증해도 고유번호가 겹치지 않게 한다.


class VerifyModal(discord.ui.Modal, title="서버 인증"):
    code = discord.ui.TextInput(
        label="인증 코드",
        placeholder="인증 맵에 표시된 코드를 입력하세요",
        min_length=4,
        max_length=12,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        code = self.code.value.strip().upper()

        pending = get_pending(code)
        if pending is None:
            await interaction.followup.send(
                "❌ 코드가 올바르지 않거나 만료되었습니다.\n"
                "인증 맵에 다시 접속해서 새 코드를 확인해주세요.",
                ephemeral=True,
            )
            return

        roblox = pending["roblox"]
        if db_get_roblox(roblox):
            await interaction.followup.send(
                "❌ 이미 다른 디스코드 계정에 연결된 로블록스 계정입니다.", ephemeral=True
            )
            return

        async with REGISTER_LOCK:
            number = next_serial()
            ok, result = await register_member(interaction.user, number, roblox)

        if not ok:
            await interaction.followup.send(result, ephemeral=True)
            return

        discard_code(code)

        nick_text = (
            f"닉네임: `{result}`" if result else "닉네임은 변경할 수 없어 그대로 두었습니다."
        )
        await interaction.followup.send(
            f"✅ 인증 완료!\n고유번호: `{number}`\n로블록스: `{roblox}`\n{nick_text}",
            ephemeral=True,
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

        if not data:
            await interaction.response.send_message(
                "❌ 해당 유저의 인증 정보가 없습니다.", ephemeral=True
            )
            return

        if self.mode == "search":
            await interaction.response.send_message(
                embed=auth_info_embed(member, data), ephemeral=True
            )
            return

        delete_user(member.id)
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
# 티켓 시스템
# =========================
def is_ticket_staff(member: discord.Member) -> bool:
    perms = member.guild_permissions
    if perms.administrator or perms.manage_channels:
        return True
    return bool(
        TICKET_STAFF_ROLE_ID
        and any(role.id == TICKET_STAFF_ROLE_ID for role in member.roles)
    )


def get_ticket_owner_id(channel: discord.TextChannel):
    match = re.search(r"(?:^|\|)ticket-owner:(\d+)(?:\||$)", channel.topic or "")
    return int(match.group(1)) if match else None


async def make_ticket_transcript(channel: discord.TextChannel) -> bytes:
    lines = [
        f"티켓 채널: #{channel.name}",
        f"채널 ID: {channel.id}",
        f"생성 시각(UTC): {channel.created_at.isoformat()}",
        "=" * 70,
    ]
    async for message in channel.history(limit=None, oldest_first=True):
        timestamp = message.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
        content = message.content or ""
        if message.attachments:
            attachments = " | ".join(a.url for a in message.attachments)
            content = f"{content}\n[첨부파일] {attachments}".strip()
        if message.embeds:
            embed_text = " | ".join(
                f"Embed: {e.title or ''} {e.description or ''}".strip()
                for e in message.embeds
            )
            content = f"{content}\n[{embed_text}]".strip()
        lines.append(f"[{timestamp}] {message.author} ({message.author.id}): {content}")
    return "\n".join(lines).encode("utf-8", errors="replace")


class TicketCreateModal(discord.ui.Modal, title="문의 티켓 생성"):
    subject = discord.ui.TextInput(
        label="문의 제목",
        placeholder="예: 인증이 되지 않아요",
        min_length=2,
        max_length=100,
    )
    details = discord.ui.TextInput(
        label="문의 내용",
        placeholder="문제 상황을 자세히 적어주세요.",
        style=discord.TextStyle.paragraph,
        min_length=5,
        max_length=1500,
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "❌ 서버 안에서만 티켓을 만들 수 있습니다.", ephemeral=True
            )
            return

        guild = interaction.guild
        member = interaction.user

        for channel in guild.text_channels:
            if get_ticket_owner_id(channel) == member.id:
                await interaction.response.send_message(
                    f"이미 열린 티켓이 있습니다: {channel.mention}", ephemeral=True
                )
                return

        await interaction.response.defer(ephemeral=True, thinking=True)

        member_perms = dict(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
            embed_links=True,
        )
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(**member_perms),
        }
        if guild.me:
            overwrites[guild.me] = discord.PermissionOverwrite(
                **member_perms, manage_channels=True, manage_messages=True
            )

        staff_role = guild.get_role(TICKET_STAFF_ROLE_ID) if TICKET_STAFF_ROLE_ID else None
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(
                **member_perms, manage_messages=True
            )

        category = guild.get_channel(TICKET_CATEGORY_ID) if TICKET_CATEGORY_ID else None
        if not isinstance(category, discord.CategoryChannel):
            category = None

        safe_name = re.sub(r"[^a-z0-9가-힣-]", "-", member.name.lower())
        safe_name = re.sub(r"-+", "-", safe_name).strip("-")[:35] or "user"

        try:
            channel = await guild.create_text_channel(
                name=f"ticket-{safe_name}",
                category=category,
                overwrites=overwrites,
                topic=f"ticket-owner:{member.id}|subject:{self.subject.value[:100]}",
                reason=f"티켓 생성: {member} ({member.id})",
            )
            embed = discord.Embed(
                title="🎫 문의 티켓",
                description=(
                    f"**문의자:** {member.mention}\n"
                    f"**제목:** {self.subject.value}\n\n"
                    f"**문의 내용**\n{self.details.value}"
                ),
                color=0x5865F2,
                timestamp=now(),
            )
            embed.set_footer(text=f"문의자 ID: {member.id}")
            await channel.send(
                content=member.mention + (f" {staff_role.mention}" if staff_role else ""),
                embed=embed,
                view=TicketCloseView(),
                allowed_mentions=discord.AllowedMentions(
                    users=True, roles=bool(staff_role), everyone=False
                ),
            )
            await interaction.followup.send(
                f"✅ 티켓이 생성되었습니다: {channel.mention}", ephemeral=True
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ 티켓 채널을 만들 권한이 없습니다. 봇에 채널 관리 권한을 부여해주세요.",
                ephemeral=True,
            )
        except discord.HTTPException as e:
            print(f"❌ 티켓 생성 실패: {e}")
            await interaction.followup.send(
                "❌ 티켓 생성 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.",
                ephemeral=True,
            )


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="🎫 문의하기",
        style=discord.ButtonStyle.primary,
        custom_id="ticket_open_button",
    )
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            await interaction.response.send_message(
                "서버 안에서만 사용할 수 있습니다.", ephemeral=True
            )
            return
        await interaction.response.send_modal(TicketCreateModal())


class TicketCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="🔒 티켓 닫기",
        style=discord.ButtonStyle.danger,
        custom_id="ticket_close_button",
    )
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel
        guild = interaction.guild
        if not isinstance(channel, discord.TextChannel) or not guild:
            await interaction.response.send_message(
                "❌ 티켓 채널에서만 사용할 수 있습니다.", ephemeral=True
            )
            return

        owner_id = get_ticket_owner_id(channel)
        if owner_id is None:
            await interaction.response.send_message(
                "❌ 이 채널은 티켓 채널로 확인되지 않습니다.", ephemeral=True
            )
            return

        is_owner = interaction.user.id == owner_id
        is_staff = isinstance(interaction.user, discord.Member) and is_ticket_staff(interaction.user)
        if not is_owner and not is_staff:
            await interaction.response.send_message(
                "❌ 티켓 작성자 또는 운영진만 티켓을 닫을 수 있습니다.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        log_channel = guild.get_channel(TICKET_LOG_CHANNEL_ID)
        try:
            transcript = await make_ticket_transcript(channel)
            if isinstance(log_channel, discord.TextChannel):
                embed = discord.Embed(
                    title="🔒 티켓 종료",
                    description=(
                        f"**채널:** #{channel.name}\n"
                        f"**종료자:** {interaction.user.mention}\n"
                        f"**문의자:** <@{owner_id}>"
                    ),
                    color=0xE74C3C,
                    timestamp=now(),
                )
                await log_channel.send(
                    embed=embed,
                    file=discord.File(
                        io.BytesIO(transcript),
                        filename=f"ticket-{channel.id}-transcript.txt",
                    ),
                )
            else:
                print(
                    f"⚠️ 티켓 로그 채널({TICKET_LOG_CHANNEL_ID})을 찾을 수 없어 "
                    "대화 기록을 저장하지 못했습니다."
                )

            await interaction.followup.send(
                "🔒 티켓을 닫았습니다. 대화 기록을 저장한 뒤 채널을 삭제합니다.",
                ephemeral=True,
            )
            await asyncio.sleep(3)
            await channel.delete(reason=f"티켓 종료: {interaction.user} ({interaction.user.id})")
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ 기록 저장 또는 채널 삭제 권한이 없습니다. 봇 권한을 확인해주세요.",
                ephemeral=True,
            )
        except discord.HTTPException as e:
            print(f"❌ 티켓 종료 처리 실패: {e}")
            await interaction.followup.send(
                "❌ 티켓 종료 처리 중 오류가 발생했습니다. 콘솔 로그를 확인해주세요.",
                ephemeral=True,
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
                counts = Counter(m.status for m in guild.members)
                name = (
                    f"🟢 {counts[discord.Status.online]}  "
                    f"🌙 {counts[discord.Status.idle]}  "
                    f"⛔ {counts[discord.Status.dnd]}"
                )
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
    await bot.process_commands(message)


@bot.event
async def on_message_delete(message):
    content = MESSAGE_CACHE.pop(message.id, None)
    if message.guild and not message.author.bot:
        deleter = await find_delete_actor(message)
        await send_message_delete_log(message, deleter, content or message.content)


@bot.event
async def on_ready():
    global status_task, views_registered, commands_synced
    print(f"{bot.user} 로그인 완료!")

    if not views_registered:
        for view in (VerifyButton(), AdminMenu(), TicketPanelView(), TicketCloseView()):
            bot.add_view(view)
        views_registered = True

    if not commands_synced:
        await bot.tree.sync(guild=GUILD)
        commands_synced = True
        print("슬래시 명령어 동기화 완료!")

    if status_task is None or status_task.done():
        status_task = asyncio.create_task(update_status())


# =========================
# 슬래시 명령어: 인증 / 티켓
# =========================
@bot.tree.command(name="티켓설정", description="문의 티켓 패널을 생성합니다.", guild=GUILD)
async def ticket_setup(interaction: discord.Interaction):
    if await deny_non_admin(interaction):
        return
    embed = discord.Embed(
        title="🎫 고객센터",
        description=(
            "문의가 필요하면 아래 **문의하기** 버튼을 눌러주세요.\n"
            "문의 제목과 내용을 작성하면 운영진만 볼 수 있는 비공개 티켓이 생성됩니다.\n\n"
            "• 한 번에 열린 티켓은 1개만 만들 수 있습니다.\n"
            "• 문의가 끝나면 티켓 닫기를 눌러주세요.\n"
            "• 종료된 티켓 대화 기록은 로그 채널에 저장됩니다."
        ),
        color=0x5865F2,
    )
    await interaction.response.send_message(embed=embed, view=TicketPanelView())


@bot.tree.command(name="인증설정", description="인증 버튼 생성", guild=GUILD)
async def verify_setup(interaction: discord.Interaction):
    if await deny_non_admin(interaction):
        return
    embed = discord.Embed(
        title="🔐 서버 인증",
        description=(
            "**인증 방법**\n"
            "1. 인증 맵에 접속해서 화면에 표시되는 **인증 코드**를 확인해요.\n"
            "2. 아래 버튼을 누르고 코드를 입력해요.\n"
            "3. 닉네임 변경과 역할 지급이 자동으로 진행돼요.\n\n"
            "고유번호는 자동으로 배정돼요."
        ),
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
    ok, result = await register_member(member, number, roblox)
    if not ok:
        await interaction.response.send_message(result, ephemeral=True)
        return

    nick_text = "" if result else "\n※ 닉네임은 변경할 수 없어 그대로 두었습니다."
    await interaction.response.send_message(
        f"✅ 수동 인증 완료\n대상: {member.mention}\n고유번호: `{number}`\n로블록스: `{roblox}`{nick_text}",
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
    await interaction.response.send_message(embed=auth_info_embed(member, data), ephemeral=True)


@bot.tree.command(name="인증삭제", description="인증 정보 삭제", guild=GUILD)
async def verify_delete(interaction: discord.Interaction, member: discord.Member):
    if await deny_non_admin(interaction):
        return
    data = db_get(member.id)
    if not data:
        await interaction.response.send_message("❌ 인증 정보가 없습니다.", ephemeral=True)
        return

    delete_user(member.id)
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


async def fetch_json(session, url, params):
    async with session.get(url, params=params) as response:
        response.raise_for_status()
        return await response.json()


@bot.tree.command(name="날씨", description="원하는 지역의 현재 날씨를 확인합니다.", guild=GUILD)
async def weather(interaction: discord.Interaction, 지역: str):
    region = 지역.strip()
    if not region:
        await interaction.response.send_message(
            "❌ 지역을 입력해주세요. 예: `/날씨 전주`", ephemeral=True
        )
        return

    await interaction.response.defer()

    try:
        timeout = aiohttp.ClientTimeout(total=10)
        headers = {"User-Agent": "DiscordWeatherBot/1.0"}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            geo = await fetch_json(
                session,
                "https://geocoding-api.open-meteo.com/v1/search",
                {
                    "name": REGION_ALIASES.get(region, region),
                    "count": 5,
                    "language": "en",
                    "format": "json",
                    "countryCode": "KR",
                },
            )
            results = geo.get("results", [])
            if not results:
                await interaction.followup.send(
                    f"❌ **{region}** 지역을 찾지 못했어.\n예: `/날씨 전주` 또는 `/날씨 서울`"
                )
                return

            place = next((x for x in results if x.get("country_code") == "KR"), results[0])
            data = await fetch_json(
                session,
                "https://api.open-meteo.com/v1/forecast",
                {
                    "latitude": place["latitude"],
                    "longitude": place["longitude"],
                    "current": (
                        "temperature_2m,relative_humidity_2m,apparent_temperature,"
                        "weather_code,wind_speed_10m,precipitation"
                    ),
                    "temperature_unit": "celsius",
                    "wind_speed_unit": "kmh",
                    "timezone": "auto",
                },
            )

        current = data.get("current", {})
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

    except aiohttp.ClientResponseError as e:
        await interaction.followup.send(f"❌ 날씨 서버 요청에 실패했어. (`HTTP {e.status}`)")
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        await interaction.followup.send(f"❌ 날씨 서버에 연결하지 못했어.\n```{type(e).__name__}: {e}```")
    except Exception as e:
        await interaction.followup.send(f"❌ 날씨 조회에 실패했어.\n```{type(e).__name__}: {e}```")



# =========================
# FastAPI + 봇 실행
# =========================
def start_api():
    try:
        print(f"[API] 서버 시작 시도: 포트 {PORT}", flush=True)
        uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
    except Exception as e:
        print(f"[API] 서버 시작 실패: {type(e).__name__}: {e}", flush=True)


if __name__ == "__main__":
    threading.Thread(target=start_api, daemon=True).start()
    bot.run(TOKEN)


