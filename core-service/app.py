# bot.py

import os
from dotenv import load_dotenv

import discord
from discord import app_commands
from discord.ui import View, Select, Modal, TextInput
from typing import List

# .env 로부터 환경변수 로드
load_dotenv()  # 기본적으로 프로젝트 루트의 .env 파일을 읽는다

# 환경변수 읽기
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")

if BOT_TOKEN is None:
    raise RuntimeError("DISCORD_BOT_TOKEN 환경변수가 설정되지 않았습니다.")

class PlayerSelectView(View):
    def __init__(self, selectable_members: List[discord.Member]):
        super().__init__(timeout=60)
        options = [
            discord.SelectOption(label=member.display_name, value=str(member.id))
            for member in selectable_members
        ]
        self.select = Select(
            placeholder="4명을 선택하세요",
            min_values=1,
            max_values=4,
            options=options,
            custom_id="player_select"
        )
        self.add_item(self.select)

    @discord.ui.select(custom_id="player_select")
    async def select_callback(self, select: Select, interaction: discord.Interaction):
        selected_ids = [int(v) for v in select.values]
        selected_members = [interaction.guild.get_member(uid) for uid in selected_ids]

        modal = ScoreModal(selected_members)
        await interaction.response.send_modal(modal)


class ScoreModal(Modal):
    def __init__(self, members: List[discord.Member]):
        super().__init__(title="점수 입력")
        self.members = members
        for m in members:
            txt = TextInput(
                label=f"{m.display_name} 점수",
                style=discord.TextStyle.short,
                placeholder="정수 입력",
                required=True
            )
            self.add_item(txt)

    async def on_submit(self, interaction: discord.Interaction):
        scores = {}
        for idx, m in enumerate(self.members):
            txt_input = self.children[idx]
            try:
                val = int(txt_input.value)
            except ValueError:
                await interaction.response.send_message(
                    f"{m.display_name}의 입력이 정수가 아닙니다.", ephemeral=True
                )
                return
            scores[m.id] = val

        await interaction.response.send_message(
            f"점수 저장됨: {scores}", ephemeral=True
        )


class MyBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()


bot = MyBot()

@bot.tree.command(name="점수입력", description="게임 역할을 가진 사용자 선택 후 점수 입력")
async def 점수입력(interaction: discord.Interaction):
    guild = interaction.guild
    game_role = discord.utils.get(guild.roles, name="게임")
    if not game_role:
        await interaction.response.send_message("‘게임’ 역할이 존재하지 않습니다.", ephemeral=True)
        return

    members = [m for m in guild.members if (game_role in m.roles and not m.bot)]
    if not members:
        await interaction.response.send_message("선택 가능한 사용자가 없습니다.", ephemeral=True)
        return

    view = PlayerSelectView(members)
    await interaction.response.send_message(
        "점수를 입력할 플레이어를 선택하세요.", view=view, ephemeral=True
    )

bot.run(BOT_TOKEN)
