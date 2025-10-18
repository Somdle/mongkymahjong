# bot.py

import os
from typing import List

import aiomysql
import discord
from discord import app_commands
from discord.ui import View, Select, Modal, TextInput, Button
from dotenv import load_dotenv

# ── ENV ────────────────────────────────────────────────────────────────────────
load_dotenv()
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
ROLE_NAME = os.getenv("DISCORD_ROLE_NAME", "게임")
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))           # 이 채널에서만 작동
PAGE_SIZE = int(os.getenv("DISCORD_SELECT_PAGE_SIZE", "25"))     # Select 옵션 최대 25개/페이지
DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_NAME = os.getenv("DB_NAME")

if not BOT_TOKEN or CHANNEL_ID == 0:
    raise RuntimeError("DISCORD_BOT_TOKEN / DISCORD_CHANNEL_ID 환경변수 필요")

DIRECTIONS = ["동", "서", "남", "북"]  # 선택 순서 → 위치 0~3

# ── UI ─────────────────────────────────────────────────────────────────────────
class ScoreModal(Modal):
    """선택된 4명에 대해 '동/서/남/북 <닉네임> 점수' 입력 모달 + DB 저장."""
    def __init__(self, ordered_members: List[discord.Member], pool: aiomysql.Pool):
        super().__init__(title="점수 입력")
        if len(ordered_members) != 4:
            raise ValueError("ScoreModal requires exactly 4 members")
        self.members = ordered_members
        self.pool = pool
        for dir_name, m in zip(DIRECTIONS, self.members):
            self.add_item(TextInput(
                label=f"{dir_name} {m.display_name} 점수",
                style=discord.TextStyle.short,
                placeholder="정수 입력",
                required=True,
                max_length=10
            ))

    async def on_submit(self, interaction: discord.Interaction):
        # 1) 정수 변환 + 합계 검증
        scores: dict[int, int] = {}
        total = 0
        for i, m in enumerate(self.members):
            raw = self.children[i].value
            try:
                val = int(raw)
            except ValueError:
                await interaction.response.send_message(
                    f"{DIRECTIONS[i]} {m.display_name}: 정수만 입력하세요.", ephemeral=True
                )
                return
            scores[m.id] = val
            total += val

        if total != 10000:
            await interaction.response.send_message(
                f"총합이 {total}점입니다. 10000점이 되어야 합니다. 다시 입력하세요.",
                view=ReenterView(self.members, self.pool),
                ephemeral=True
            )
            return

        # 2) DB 트랜잭션 저장: game 1건 + game_detail 4건
        try:
            async with self.pool.acquire() as conn:
                await conn.begin()
                async with conn.cursor() as cur:
                    # game: 기본값 삽입
                    await cur.execute("INSERT INTO game () VALUES ()")
                    game_id = cur.lastrowid

                    # position: 0(east),1(west),2(south),3(north)
                    inserts = []
                    for pos, member in enumerate(self.members):
                        uid = int(member.id)  # Discord snowflake
                        sc = scores[uid]
                        inserts.append((game_id, uid, sc, pos))

                    await cur.executemany(
                        "INSERT INTO game_detail (game_id, user_id, score, position) VALUES (%s, %s, %s, %s)",
                        inserts
                    )
                await conn.commit()
        except Exception as e:
            # 롤백 및 에러 보고
            try:
                await conn.rollback()
            except Exception:
                pass
            await interaction.response.send_message(f"DB 오류: {e}", ephemeral=True)
            return

        await interaction.response.send_message(
            f"저장 완료. game_id={game_id}, 합계 {total}", ephemeral=True
        )


class ReenterView(View):
    """합계 불일치 시 재입력 버튼 제공."""
    def __init__(self, members: List[discord.Member], pool: aiomysql.Pool):
        super().__init__(timeout=120)
        self.members = members
        self.pool = pool
        re_btn = Button(label="다시 입력", style=discord.ButtonStyle.primary)

        async def on_reenter(interaction: discord.Interaction):
            await interaction.response.send_modal(ScoreModal(self.members, self.pool))

        re_btn.callback = on_reenter
        self.add_item(re_btn)


class PagedPlayerSelectView(View):
    """역할 보유 사용자 목록을 페이지로 나눠 Select 제공. 현재 페이지에서 정확히 4명 선택."""
    def __init__(self, members: List[discord.Member], pool: aiomysql.Pool, per_page: int = PAGE_SIZE):
        super().__init__(timeout=120)
        self.members = members
        self.pool = pool
        self.per_page = max(4, min(25, per_page))  # Select 옵션 최대 25개/페이지
        self.page = 0
        self._rebuild()

    def _slice(self) -> List[discord.Member]:
        s, e = self.page * self.per_page, (self.page + 1) * self.per_page
        return self.members[s:e]

    @property
    def total_pages(self) -> int:
        n = len(self.members)
        return (n + self.per_page - 1) // self.per_page

    def _rebuild(self):
        self.clear_items()
        page_members = self._slice()

        if len(page_members) < 4:
            self.add_item(Button(label="이 페이지 인원이 4명 미만입니다.", disabled=True))
            self._add_pager()
            return

        options = [
            discord.SelectOption(label=m.display_name[:100], value=str(m.id))
            for m in page_members
        ]

        select = Select(
            placeholder=f"이 페이지에서 정확히 4명 선택 ({self.page+1}/{self.total_pages})",
            min_values=4,
            max_values=4,
            options=options,
            custom_id=f"player_select_p{self.page}"
        )

        async def on_select(interaction: discord.Interaction):
            selected_ids = [int(v) for v in select.values]
            ordered = [interaction.guild.get_member(uid) for uid in selected_ids]
            ordered = [m for m in ordered if m is not None]
            if len(ordered) != 4:
                await interaction.response.send_message("정확히 4명을 선택해야 합니다.", ephemeral=True)
                return
            await interaction.response.send_modal(ScoreModal(ordered, self.pool))

        select.callback = on_select
        self.add_item(select)
        self._add_pager()

    def _add_pager(self):
        if self.total_pages <= 1:
            return
        prev_btn = Button(emoji="◀", style=discord.ButtonStyle.secondary, disabled=self.page == 0)
        next_btn = Button(emoji="▶", style=discord.ButtonStyle.secondary, disabled=self.page >= self.total_pages - 1)

        async def on_prev(interaction: discord.Interaction):
            self.page = max(0, self.page - 1)
            self._rebuild()
            await interaction.response.edit_message(view=self)

        async def on_next(interaction: discord.Interaction):
            self.page = min(self.total_pages - 1, self.page + 1)
            self._rebuild()
            await interaction.response.edit_message(view=self)

        prev_btn.callback = on_prev
        next_btn.callback = on_next
        self.add_item(prev_btn)
        self.add_item(next_btn)

# ── BOT ────────────────────────────────────────────────────────────────────────
class MyBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.db_pool: aiomysql.Pool | None = None

    async def setup_hook(self):
        # aiomysql 풀 생성
        self.db_pool = await aiomysql.create_pool(
            host=DB_HOST, port=DB_PORT,
            user=DB_USER, password=DB_PASSWORD, db=DB_NAME,
            autocommit=False, minsize=1, maxsize=5,
        )  # 기본 예제 패턴 참조. :contentReference[oaicite:1]{index=1}
        await self.tree.sync()

    async def close(self):
        # 풀 정리
        if self.db_pool is not None:
            self.db_pool.close()
            await self.db_pool.wait_closed()
        await super().close()

bot = MyBot()

@bot.tree.command(name="점수입력", description=f"{ROLE_NAME} 역할 대상 4명 선택 후 점수 입력")
async def 점수입력(interaction: discord.Interaction):
    # 채널 제한
    if interaction.channel_id != CHANNEL_ID:
        await interaction.response.send_message("이 명령은 지정된 채널에서만 사용할 수 있습니다.", ephemeral=True)
        return

    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("길드에서만 사용할 수 있습니다.", ephemeral=True)
        return

    # DB 풀 준비 확인
    if bot.db_pool is None:
        await interaction.response.send_message("DB 연결을 초기화하지 못했습니다.", ephemeral=True)
        return

    role = discord.utils.get(guild.roles, name=ROLE_NAME)
    if role is None:
        await interaction.response.send_message(f"역할 '{ROLE_NAME}' 이(가) 없습니다.", ephemeral=True)
        return

    members = [m for m in guild.members if (role in m.roles and not m.bot)]
    if len(members) < 4:
        await interaction.response.send_message("인원 부족: 최소 4명이 필요합니다.", ephemeral=True)
        return

    view = PagedPlayerSelectView(members, pool=bot.db_pool, per_page=PAGE_SIZE)
    if not view.children:
        await interaction.response.send_message("옵션이 부족하여 선택 UI를 만들 수 없습니다.", ephemeral=True)
        return

    await interaction.response.send_message("현재 페이지에서 정확히 4명을 선택하세요.", view=view, ephemeral=True)

if __name__ == "__main__":
    bot.run(BOT_TOKEN)
