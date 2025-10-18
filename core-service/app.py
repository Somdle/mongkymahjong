# bot.py

import os
from typing import List

import discord
from discord import app_commands
from discord.ui import View, Select, Modal, TextInput, Button
from dotenv import load_dotenv

# ── ENV ────────────────────────────────────────────────────────────────────────
load_dotenv()
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
ROLE_NAME = os.getenv("DISCORD_ROLE_NAME", "게임")
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))  # 이 채널에서만 작동
PAGE_SIZE = int(os.getenv("DISCORD_SELECT_PAGE_SIZE", "25"))  # Select 옵션 최대 25개

if not BOT_TOKEN or CHANNEL_ID == 0:
    raise RuntimeError("DISCORD_BOT_TOKEN / DISCORD_CHANNEL_ID 환경변수 필요")

# ── UI ─────────────────────────────────────────────────────────────────────────
class ScoreModal(Modal):
    """선택된 멤버별 정수 점수 입력 모달. 정확히 4명 전제."""
    def __init__(self, members: List[discord.Member]):
        super().__init__(title="점수 입력")
        # 정확히 4명만 허용
        self.members = members[:4]
        for m in self.members:
            self.add_item(TextInput(
                label=f"{m.display_name} 점수",
                style=discord.TextStyle.short,
                placeholder="정수 입력",
                required=True,
                max_length=10
            ))

    async def on_submit(self, interaction: discord.Interaction):
        scores: dict[int, int] = {}
        for i, m in enumerate(self.members):
            raw = self.children[i].value
            try:
                scores[m.id] = int(raw)
            except ValueError:
                await interaction.response.send_message(
                    f"{m.display_name} 점수는 정수여야 합니다: '{raw}'",
                    ephemeral=True
                )
                return
        # TODO: 저장 로직 삽입
        await interaction.response.send_message(f"점수 저장됨: {scores}", ephemeral=True)


class PagedPlayerSelectView(View):
    """
    역할 보유 사용자 목록을 페이지로 나눠 Select 제공.
    - Discord 제한: Select options 최대 25개. (페이지당)  :contentReference[oaicite:3]{index=3}
    - 정확히 4명 선택: min_values=4, max_values=4.         :contentReference[oaicite:4]{index=4}
    - Prev/Next 버튼으로 페이지 전환.
    - 선택은 "현재 페이지"에서만 4명.
    """
    def __init__(self, members: List[discord.Member], per_page: int = PAGE_SIZE):
        super().__init__(timeout=120)
        # 안전 가드
        self.members = members
        self.per_page = max(4, min(25, per_page))  # 최소 4, 최대 25
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

        # 현재 페이지 옵션이 4명 미만이면 선택 UI 제공 불가
        if len(page_members) < 4:
            btn = Button(label="이 페이지에 선택 가능한 인원이 4명 미만입니다.", disabled=True)
            self.add_item(btn)
            # 페이지 네비게이션은 제공
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
            # 선택된 4명으로 모달 표시
            selected_ids = [int(v) for v in select.values]
            selected = [interaction.guild.get_member(uid) for uid in selected_ids]
            selected = [m for m in selected if m is not None]

            # 방어: 정확히 4명인지 확인
            if len(selected) != 4:
                await interaction.response.send_message(
                    "정확히 4명을 선택해야 합니다.", ephemeral=True
                )
                return

            await interaction.response.send_modal(ScoreModal(selected))

        select.callback = on_select
        self.add_item(select)

        # 페이지 네비게이션
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
        intents.members = True  # 멤버 목록 접근. 포털에서 Privileged Intents 설정 필요할 수 있음. :contentReference[oaicite:5]{index=5}
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()

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

    role = discord.utils.get(guild.roles, name=ROLE_NAME)
    if role is None:
        await interaction.response.send_message(f"역할 '{ROLE_NAME}' 이(가) 없습니다.", ephemeral=True)
        return

    # 역할 보유자 필터 (봇 제외)
    members = [m for m in guild.members if (role in m.roles and not m.bot)]

    # 전체 인원 자체가 4명 미만이면 즉시 인원 부족 알림
    if len(members) < 4:
        await interaction.response.send_message("인원 부족: 최소 4명이 필요합니다.", ephemeral=True)
        return

    view = PagedPlayerSelectView(members, per_page=PAGE_SIZE)
    if not view.children:
        await interaction.response.send_message("옵션이 부족하여 선택 UI를 만들 수 없습니다.", ephemeral=True)
        return

    await interaction.response.send_message("현재 페이지에서 정확히 4명을 선택하세요.", view=view, ephemeral=True)

if __name__ == "__main__":
    bot.run(BOT_TOKEN)
