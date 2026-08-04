import os
import sqlite3
import asyncio
from datetime import datetime

import discord
from discord.ext import commands
from dotenv import load_dotenv


load_dotenv()

TOKEN = os.getenv("TOKEN")


# ==========================
# 서버 설정
# ==========================

GUILD_ID = 848855044655808542
ROLE_ID = 1534137711798124614
LOG_CHANNEL_ID = 1534147708124925962
STATUS_CHANNEL_ID = 1534151355558793216


# ==========================
# DB
# ==========================

db = sqlite3.connect("users.db")
cursor = db.cursor()


cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    discord_id TEXT PRIMARY KEY,
    number TEXT UNIQUE,
    roblox TEXT,
    created TEXT
)
""")


db.commit()



# ==========================
# 디스코드 설정
# ==========================

intents = discord.Intents.default()
intents.members = True
intents.presences = True


bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


status_task = None



# ==========================
# 인증 모달
# ==========================

class VerifyModal(discord.ui.Modal, title="서버 인증"):


    number = discord.ui.TextInput(
        label="고유번호",
        placeholder="예: 3498",
        max_length=10
    )


    roblox = discord.ui.TextInput(
        label="로블록스 닉네임",
        placeholder="예: runmingi848",
        max_length=32
    )



    async def on_submit(self, interaction):

        user_id = str(interaction.user.id)

        number = self.number.value
        roblox = self.roblox.value



        cursor.execute(
            "SELECT * FROM users WHERE discord_id=?",
            (user_id,)
        )

        if cursor.fetchone():

            await interaction.response.send_message(
                "❌ 이미 인증된 계정입니다.",
                ephemeral=True
            )
            return



        cursor.execute(
            "SELECT * FROM users WHERE number=?",
            (number,)
        )


        if cursor.fetchone():

            await interaction.response.send_message(
                "❌ 이미 사용 중인 고유번호입니다.",
                ephemeral=True
            )
            return



        nickname = f"{number} · {roblox} · 주민"



        try:

            await interaction.user.edit(
                nick=nickname
            )


        except discord.Forbidden:

            await interaction.response.send_message(
                "❌ 닉네임 변경 실패\n봇 역할 위치 확인 필요",
                ephemeral=True
            )

            return



        role = interaction.guild.get_role(
            ROLE_ID
        )


        await interaction.user.add_roles(role)



        cursor.execute(
            """
            INSERT INTO users
            VALUES (?, ?, ?, ?)
            """,
            (
                user_id,
                number,
                roblox,
                datetime.now().strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
            )
        )


        db.commit()



        await interaction.response.send_message(
            f"✅ 인증 완료!\n닉네임: `{nickname}`",
            ephemeral=True
        )



        log = bot.get_channel(
            LOG_CHANNEL_ID
        )


        if log:

            embed = discord.Embed(
                title="🔐 인증 완료",
                color=0x00ff00
            )


            embed.add_field(
                name="유저",
                value=interaction.user.mention
            )

            embed.add_field(
                name="고유번호",
                value=number
            )

            embed.add_field(
                name="로블록스",
                value=roblox
            )


            embed.add_field(
                name="시간",
                value=datetime.now().strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
            )


            await log.send(
                embed=embed
            )



# ==========================
# 버튼
# ==========================

class VerifyButton(discord.ui.View):

    def __init__(self):

        super().__init__(
            timeout=None
        )


    @discord.ui.button(
        label="✅ 인증하기",
        style=discord.ButtonStyle.green,
        custom_id="verify_button"
    )


    async def verify(
        self,
        interaction,
        button
    ):

        await interaction.response.send_modal(
            VerifyModal()
        )


# ==========================
# 관리자 메뉴 버튼
# ==========================

class AdminMenu(discord.ui.View):

    def __init__(self):
        super().__init__(
            timeout=None
        )


    @discord.ui.button(
        label="🔎 인증 조회",
        style=discord.ButtonStyle.primary,
        custom_id="admin_search"
    )
    async def search(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "❌ 관리자만 사용할 수 있습니다.",
                ephemeral=True
            )
            return


        await interaction.response.send_message(
            "조회할 유저를 선택해주세요.",
            view=UserSelectView("search"),
            ephemeral=True
        )



    @discord.ui.button(
        label="🗑️ 인증 삭제",
        style=discord.ButtonStyle.danger,
        custom_id="admin_delete"
    )
    async def delete(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "❌ 관리자만 사용할 수 있습니다.",
                ephemeral=True
            )
            return


        await interaction.response.send_message(
            "삭제할 유저를 선택해주세요.",
            view=UserSelectView("delete"),
            ephemeral=True
        )


# ==========================
# 유저 선택 메뉴
# ==========================

class UserSelectView(discord.ui.View):

    def __init__(self, mode):

        super().__init__(
            timeout=60
        )

        self.add_item(
            UserSelect(mode)
        )


class UserSelect(discord.ui.UserSelect):

    def __init__(self, mode):

        super().__init__(
            placeholder="유저 선택",
            min_values=1,
            max_values=1
        )

        self.mode = mode


    async def callback(
        self,
        interaction: discord.Interaction
    ):

        member = self.values[0]


        if self.mode == "search":

            user_id = str(member.id)


            cursor.execute(
                "SELECT * FROM users WHERE discord_id=?",
                (user_id,)
            )


            data = cursor.fetchone()


            if not data:

                await interaction.response.send_message(
                    "❌ 인증 정보가 없습니다.",
                    ephemeral=True
                )

                return


            embed = discord.Embed(
                title="🔎 인증 정보",
                color=0x3498db
            )


            embed.add_field(
                name="유저",
                value=member.mention
            )

            embed.add_field(
                name="고유번호",
                value=data[1]
            )

            embed.add_field(
                name="로블록스",
                value=data[2]
            )

            embed.add_field(
                name="인증일",
                value=data[3]
            )


            await interaction.response.send_message(
                embed=embed,
                ephemeral=True
            )


        elif self.mode == "delete":

            user_id = str(member.id)


            # 인증 정보 확인
            cursor.execute(
                "SELECT * FROM users WHERE discord_id=?",
                (user_id,)
            )

            data = cursor.fetchone()


            if not data:

                await interaction.response.send_message(
                    "❌ 해당 유저의 인증 정보가 없습니다.",
                    ephemeral=True
                )

                return


            # DB 삭제
            cursor.execute(
                "DELETE FROM users WHERE discord_id=?",
                (user_id,)
            )

            db.commit()


            # 역할 제거
            role = interaction.guild.get_role(
                ROLE_ID
            )

            if role and role in member.roles:

                try:
                    await member.remove_roles(role)

                except discord.Forbidden:
                    pass


            # 닉네임 초기화
            try:

                await member.edit(
                    nick=None
                )

            except discord.Forbidden:

                pass


            await interaction.response.send_message(
                f"🗑️ {member.mention} 인증 정보 삭제 완료\n"
                "✅ 역할 제거\n"
                "✅ 닉네임 초기화",
                ephemeral=True
            )

# ==========================
# 상태 표시
# ==========================


async def update_status():

    await bot.wait_until_ready()


    channel = bot.get_channel(
        STATUS_CHANNEL_ID
    )


    while not bot.is_closed():

        guild = bot.get_guild(
            GUILD_ID
        )


        if guild and channel:


            online = 0
            idle = 0
            dnd = 0



            for member in guild.members:


                if member.status == discord.Status.online:
                    online += 1


                elif member.status == discord.Status.idle:
                    idle += 1


                elif member.status == discord.Status.dnd:
                    dnd += 1




            new_name = (
                f"🟢 {online}  "
                f"🌙 {idle}  "
                f"⛔ {dnd}"
            )



            # 이름이 바뀔 때만 요청
            if channel.name != new_name:

                try:

                    await channel.edit(
                        name=new_name
                    )

                except Exception as e:

                    print(
                        "상태 채널 변경 오류:",
                        e
                    )



        # 1분마다 확인

        await asyncio.sleep(60)





# ==========================
# 봇 시작
# ==========================


@bot.event
async def on_ready():

    global status_task


    print(
        f"{bot.user} 로그인 완료!"
    )


    bot.add_view(
        VerifyButton()
    )
    bot.add_view(
        AdminMenu()
    )

    guild = discord.Object(
        id=GUILD_ID
    )


    await bot.tree.sync(
        guild=guild
    )


    if status_task is None:

        status_task = asyncio.create_task(
            update_status()
        )


    print(
        "슬래시 명령어 동기화 완료!"
    )





# ==========================
# 인증 설정
# ==========================


@bot.tree.command(
    name="인증설정",
    description="인증 버튼 생성",
    guild=discord.Object(id=GUILD_ID)
)

async def verify_setup(interaction):


    embed = discord.Embed(
        title="🔐 서버 인증",
        description=
        "아래 버튼을 눌러 인증해주세요.\n\n"
        "고유번호와 로블록스 닉네임 필요",
        color=0x00ff00
    )


    await interaction.response.send_message(
        embed=embed,
        view=VerifyButton()
    )

# ==========================
# 관리자 메뉴
# ==========================

@bot.tree.command(
    name="관리자메뉴",
    description="관리자 전용 메뉴",
    guild=discord.Object(id=GUILD_ID)
)
async def admin_menu(
    interaction: discord.Interaction
):

    if not interaction.user.guild_permissions.administrator:

        await interaction.response.send_message(
            "❌ 관리자만 사용할 수 있습니다.",
            ephemeral=True
        )

        return


    embed = discord.Embed(
        title="⚙️ 관리자 메뉴",
        description=
        "관리할 기능을 선택해주세요.\n\n"
        "🔎 인증 조회\n"
        "🗑️ 인증 삭제",
        color=0x5865F2
    )


    await interaction.response.send_message(
        embed=embed,
        view=AdminMenu(),
        ephemeral=True
    )


# ==========================
# 수동 인증
# ==========================


@bot.tree.command(
    name="수동인증",
    description="관리자가 직접 인증 처리",
    guild=discord.Object(id=GUILD_ID)
)
async def manual_verify(
    interaction: discord.Interaction,
    member: discord.Member,
    number: str,
    roblox: str
):

    if not interaction.user.guild_permissions.administrator:

        await interaction.response.send_message(
            "❌ 관리자만 사용할 수 있습니다.",
            ephemeral=True
        )

        return


    user_id = str(member.id)


    # 이미 인증 확인
    cursor.execute(
        "SELECT * FROM users WHERE discord_id=?",
        (user_id,)
    )


    if cursor.fetchone():

        await interaction.response.send_message(
            "❌ 이미 인증된 유저입니다.",
            ephemeral=True
        )

        return



    # 고유번호 중복 확인
    cursor.execute(
        "SELECT * FROM users WHERE number=?",
        (number,)
    )


    if cursor.fetchone():

        await interaction.response.send_message(
            "❌ 이미 사용 중인 고유번호입니다.",
            ephemeral=True
        )

        return



    nickname = f"{number} · {roblox} · 주민"


    try:

        await member.edit(
            nick=nickname
        )

    except:

        pass



    role = interaction.guild.get_role(
        ROLE_ID
    )


    if role:

        await member.add_roles(
            role
        )



    cursor.execute(
        """
        INSERT INTO users
        VALUES (?, ?, ?, ?)
        """,
        (
            user_id,
            number,
            roblox,
            datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        )
    )


    db.commit()



    await interaction.response.send_message(
        f"✅ 수동 인증 완료\n"
        f"대상: {member.mention}\n"
        f"고유번호: `{number}`\n"
        f"로블록스: `{roblox}`",
        ephemeral=True
    )



    log = bot.get_channel(
        LOG_CHANNEL_ID
    )


    if log:

        embed = discord.Embed(
            title="🔐 수동 인증 완료",
            color=0x00ff00
        )


        embed.add_field(
            name="대상",
            value=member.mention
        )

        embed.add_field(
            name="고유번호",
            value=number
        )

        embed.add_field(
            name="로블록스",
            value=roblox
        )

        embed.add_field(
            name="처리자",
            value=interaction.user.mention
        )


        await log.send(
            embed=embed
        )

        
# ==========================
# 인증 조회
# ==========================


@bot.tree.command(
    name="인증조회",
    description="유저 인증 정보 조회",
    guild=discord.Object(id=GUILD_ID)
)

async def verify_search(
    interaction,
    member: discord.Member
):

    if not interaction.user.guild_permissions.administrator:

        await interaction.response.send_message(
            "❌ 관리자만 사용할 수 있습니다.",
            ephemeral=True
        )

        return


    user_id = str(member.id)


    cursor.execute(
        "SELECT * FROM users WHERE discord_id=?",
        (user_id,)
    )


    data = cursor.fetchone()


    if not data:

        await interaction.response.send_message(
            "❌ 해당 유저의 인증 정보가 없습니다.",
            ephemeral=True
        )

        return


    number = data[1]
    roblox = data[2]
    created = data[3]


    embed = discord.Embed(
        title="🔎 인증 정보",
        color=0x3498db
    )


    embed.add_field(
        name="유저",
        value=member.mention,
        inline=False
    )


    embed.add_field(
        name="고유번호",
        value=number,
        inline=True
    )


    embed.add_field(
        name="로블록스",
        value=roblox,
        inline=True
    )


    embed.add_field(
        name="인증일",
        value=created,
        inline=False
    )


    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )

# ==========================
# 인증 삭제
# ==========================


@bot.tree.command(
    name="인증삭제",
    description="인증 정보 삭제",
    guild=discord.Object(id=GUILD_ID)
)

async def verify_delete(
    interaction,
    member: discord.Member
):


    if not interaction.user.guild_permissions.administrator:

        await interaction.response.send_message(
            "❌ 관리자만 사용 가능합니다.",
            ephemeral=True
        )

        return



    user_id = str(member.id)



    cursor.execute(
        "SELECT * FROM users WHERE discord_id=?",
        (user_id,)
    )


    data = cursor.fetchone()



    if not data:

        await interaction.response.send_message(
            "❌ 인증 정보가 없습니다.",
            ephemeral=True
        )

        return



    number = data[1]
    roblox = data[2]



    cursor.execute(
        "DELETE FROM users WHERE discord_id=?",
        (user_id,)
    )


    db.commit()



    role = interaction.guild.get_role(
        ROLE_ID
    )


    if role in member.roles:

        await member.remove_roles(
            role
        )



    try:

        await member.edit(
            nick=None
        )

    except:

        pass



    await interaction.response.send_message(
        "🗑️ 인증 삭제 완료",
        ephemeral=True
    )



    log = bot.get_channel(
        LOG_CHANNEL_ID
    )


    if log:


        embed = discord.Embed(
            title="🗑️ 인증 삭제",
            color=0xff0000
        )


        embed.add_field(
            name="대상",
            value=member.mention
        )


        embed.add_field(
            name="기존 정보",
            value=
            f"고유번호: {number}\n"
            f"로블록스: {roblox}"
        )


        embed.add_field(
            name="처리자",
            value=interaction.user.mention
        )


        embed.add_field(
            name="시간",
            value=datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        )


        await log.send(
            embed=embed
        )





# ==========================
# 실행
# ==========================


bot.run(TOKEN)