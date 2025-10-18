# bot.py

import os
from typing import List, Tuple, Dict, Any

import aiomysql  # pip install aiomysql
import discord
from discord import app_commands
from discord.ui import View, Select, Modal, TextInput, Button
from dotenv import load_dotenv

# ── ENV ────────────────────────────────────────────────────────────────────────
load_dotenv()
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
ROLE_NAME = os.getenv("DISCORD_ROLE_NAME", "게임")
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))
PAGE_SIZE = int(os.getenv("DISCORD_SELECT_PAGE_SIZE", "25"))

DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_USER = os.getenv("DB_USER", "monkeymahjong")
DB_PASSWORD = os.getenv("DB_PASSWORD", "monkeymahjong1324~")
DB_NAME = os.getenv("DB_NAME", "monkeymahjong")

if not BOT_TOKEN or CHANNEL_ID == 0:
    raise RuntimeError("DISCORD_BOT_TOKEN / DISCORD_CHANNEL_ID 필요")

# 좌석 라벨 및 포지션 인덱스
POS_LABEL = {0: "동", 1: "서", 2: "남", 3: "북"}
LABEL_ORDER_FOR_INPUT = [0, 1, 2, 3]  # 입력 라벨 순서: 동, 서, 남, 북
TIEBREAK_ESWN = {0: 0, 2: 1, 1: 2, 3: 3}  # 동(0)→남(2)→서(1)→북(3)

# ── 공용 유틸 ──────────────────────────────────────────────────────────────────
def mention(uid: int) -> str:
    return f"<@{uid}>"

def rank_key(score: int, pos: int) -> Tuple[int, int]:
    return (-score, TIEBREAK_ESWN[pos])

def fmt_game_result(game_id: int, rows: List[Dict[str, Any]], guild: discord.Guild) -> str:
    """
    rows: [{user_id:int, score:int, position:int}]
    """
    # 좌석순 표기
    seat_lines = []
    total = 0
    by_pos = {r["position"]: r for r in rows}
    for p in [0, 1, 2, 3]:
        r = by_pos.get(p)
        if not r:
            continue
        total += r["score"]
        seat_lines.append(f"{POS_LABEL[p]} {mention(r['user_id'])} {r['score']}")
    # 순위 계산(점수 desc, 타이브레이크 ESWN)
    ranked = sorted(rows, key=lambda r: rank_key(r["score"], r["position"]))
    rank_lines = []
    for i, r in enumerate(ranked, 1):
        rank_lines.append(f"{i}. {POS_LABEL[r['position']]} {mention(r['user_id'])} {r['score']}")
    return (
        f"[게임 #{game_id}] 결과\n"
        + "\n".join(seat_lines)
        + f"\n합계: {total}\n"
        + "순위(동점 ESWN):\n"
        + "\n".join(rank_lines)
    )

async def fetch_game(pool: aiomysql.Pool, game_id: int) -> List[Dict[str, Any]]:
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT user_id, score, position FROM game_detail WHERE game_id=%s",
            (game_id,),
        )
        rows = await cur.fetchall()
    return [{"user_id": int(r[0]), "score": int(r[1]), "position": int(r[2])} for r in rows]

# ── UI ─────────────────────────────────────────────────────────────────────────
class ScoreModal(Modal):
    """새 게임 입력. 선택된 4명에 대해 '동/서/남/북 <닉네임> 점수' 입력."""
    def __init__(self, ordered_members: List[discord.Member], pool: aiomysql.Pool):
        super().__init__(title="점수 입력")
        if len(ordered_members) != 4:
            raise ValueError("ScoreModal requires exactly 4 members")
        # 선택 순서 → position 매핑: 0:동,1:서,2:남,3:북
        self.members_by_pos: Dict[int, discord.Member] = {
            0: ordered_members[0],
            1: ordered_members[1],
            2: ordered_members[2],
            3: ordered_members[3],
        }
        self.pool = pool
        for p in LABEL_ORDER_FOR_INPUT:
            m = self.members_by_pos[p]
            self.add_item(TextInput(
                label=f"{POS_LABEL[p]} {m.display_name} 점수",
                style=discord.TextStyle.short,
                placeholder="정수 입력",
                required=True,
                max_length=10,
                custom_id=f"score_{p}",
            ))

    async def on_submit(self, interaction: discord.Interaction):
        # 정수 변환 + 합계 = 10000 확인
        scores: Dict[int, int] = {}
        total = 0
        for p in LABEL_ORDER_FOR_INPUT:
            raw = self.children[LABEL_ORDER_FOR_INPUT.index(p)].value
            try:
                v = int(raw)
            except ValueError:
                await interaction.response.send_message(
                    f"{POS_LABEL[p]} {self.members_by_pos[p].display_name}: 정수만 입력", ephemeral=True
                )
                return
            scores[p] = v
            total += v

        if total != 10000:
            await interaction.response.send_message(
                f"총합 {total}. 10000이어야 합니다. 다시 입력하세요.",
                view=ReenterView([self.members_by_pos[p] for p in LABEL_ORDER_FOR_INPUT], self.pool),
                ephemeral=True
            )
            return

        # DB 트랜잭션 저장
        try:
            async with self.pool.acquire() as conn:
                await conn.begin()
                async with conn.cursor() as cur:
                    await cur.execute("INSERT INTO game () VALUES ()")
                    game_id = cur.lastrowid
                    inserts = []
                    for p in [0, 1, 2, 3]:
                        uid = int(self.members_by_pos[p].id)
                        inserts.append((game_id, uid, scores[p], p))
                    await cur.executemany(
                        "INSERT INTO game_detail (game_id, user_id, score, position) VALUES (%s,%s,%s,%s)",
                        inserts
                    )
                await conn.commit()
        except Exception as e:
            try:
                await conn.rollback()
            except Exception:
                pass
            await interaction.response.send_message(f"DB 오류: {e}", ephemeral=True)
            return

        # 개인 응답 + 채널 공개 결과
        await interaction.response.send_message(f"저장 완료. game_id={game_id}", ephemeral=True)
        rows = [{"user_id": int(m.id), "score": scores[p], "position": p} for p, m in self.members_by_pos.items()]
        await interaction.followup.send(fmt_game_result(game_id, rows, interaction.guild))

class ReenterView(View):
    """합계 오류 시 재입력"""
    def __init__(self, members_in_order: List[discord.Member], pool: aiomysql.Pool):
        super().__init__(timeout=120)
        self.members = members_in_order
        self.pool = pool
        btn = Button(label="다시 입력", style=discord.ButtonStyle.primary)

        async def on_click(itx: discord.Interaction):
            await itx.response.send_modal(ScoreModal(self.members, self.pool))

        btn.callback = on_click
        self.add_item(btn)

class EditScoreModal(Modal):
    """기존 게임 점수 수정."""
    def __init__(self, game_id: int, rows: List[Dict[str, Any]], guild: discord.Guild, pool: aiomysql.Pool):
        super().__init__(title=f"게임 #{game_id} 점수 수정")
        self.game_id = game_id
        self.pool = pool
        # position 기준 정렬(동,서,남,북)
        self.rows = sorted(rows, key=lambda r: r["position"])
        self.members_by_pos: Dict[int, Tuple[int, str]] = {}
        for r in self.rows:
            uid = int(r["user_id"])
            m = guild.get_member(uid)
            name = m.display_name if m else str(uid)
            self.members_by_pos[r["position"]] = (uid, name)
        for r in self.rows:
            p = r["position"]
            uid, name = self.members_by_pos[p]
            self.add_item(TextInput(
                label=f"{POS_LABEL[p]} {name} 점수",
                style=discord.TextStyle.short,
                required=True,
                max_length=10,
                custom_id=f"edit_{p}",
                default=str(r["score"]),  # 프리필 :contentReference[oaicite:2]{index=2}
            ))

    async def on_submit(self, interaction: discord.Interaction):
        new_scores: Dict[int, int] = {}
        total = 0
        for p in [0, 1, 2, 3]:
            raw = self.children[p].value
            try:
                v = int(raw)
            except ValueError:
                await interaction.response.send_message(
                    f"{POS_LABEL[p]}: 정수만 입력", ephemeral=True
                )
                return
            new_scores[p] = v
            total += v
        if total != 10000:
            await interaction.response.send_message(
                f"총합 {total}. 10000이어야 합니다. 다시 입력하세요.",
                view=ReenterEditView(self.game_id, self.rows, interaction.guild, self.pool),
                ephemeral=True
            )
            return

        try:
            async with self.pool.acquire() as conn:
                await conn.begin()
                async with conn.cursor() as cur:
                    for p in [0, 1, 2, 3]:
                        await cur.execute(
                            "UPDATE game_detail SET score=%s WHERE game_id=%s AND position=%s",
                            (new_scores[p], self.game_id, p),
                        )
                await conn.commit()
        except Exception as e:
            try:
                await conn.rollback()
            except Exception:
                pass
            await interaction.response.send_message(f"DB 오류: {e}", ephemeral=True)
            return

        # 공개 결과 재게시
        rows = [{"user_id": self.members_by_pos[p][0], "score": new_scores[p], "position": p} for p in [0,1,2,3]]
        await interaction.response.send_message(f"게임 #{self.game_id} 수정 완료", ephemeral=True)
        await interaction.followup.send(fmt_game_result(self.game_id, rows, interaction.guild))

class ReenterEditView(View):
    """수정 합계 오류 시 재입력"""
    def __init__(self, game_id: int, rows: List[Dict[str, Any]], guild: discord.Guild, pool: aiomysql.Pool):
        super().__init__(timeout=120)
        self.game_id, self.rows, self.guild, self.pool = game_id, rows, guild, pool
        btn = Button(label="다시 입력", style=discord.ButtonStyle.primary)

        async def on_click(itx: discord.Interaction):
            await itx.response.send_modal(EditScoreModal(self.game_id, self.rows, self.guild, self.pool))

        btn.callback = on_click
        self.add_item(btn)

class PagedPlayerSelectView(View):
    """역할 보유 사용자 목록을 페이지로 나눠 Select 제공. 현재 페이지에서 정확히 4명."""
    def __init__(self, members: List[discord.Member], pool: aiomysql.Pool, per_page: int = PAGE_SIZE):
        super().__init__(timeout=120)
        self.members = members
        self.pool = pool
        self.per_page = max(4, min(25, per_page))
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

        options = [discord.SelectOption(label=m.display_name[:100], value=str(m.id)) for m in page_members]
        select = Select(
            placeholder=f"이 페이지에서 정확히 4명 선택 ({self.page+1}/{self.total_pages})",
            min_values=4, max_values=4, options=options, custom_id=f"player_select_p{self.page}"
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

        async def on_prev(itx: discord.Interaction):
            self.page = max(0, self.page - 1)
            self._rebuild()
            await itx.response.edit_message(view=self)

        async def on_next(itx: discord.Interaction):
            self.page = min(self.total_pages - 1, self.page + 1)
            self._rebuild()
            await itx.response.edit_message(view=self)

        prev_btn.callback = on_prev
        next_btn.callback = on_next
        self.add_item(prev_btn); self.add_item(next_btn)

# ── BOT ────────────────────────────────────────────────────────────────────────
class MyBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.db_pool: aiomysql.Pool | None = None

    async def setup_hook(self):
        self.db_pool = await aiomysql.create_pool(
            host=DB_HOST, port=DB_PORT,
            user=DB_USER, password=DB_PASSWORD, db=DB_NAME,
            autocommit=False, minsize=1, maxsize=5,
        )
        await self.tree.sync()

    async def close(self):
        if self.db_pool is not None:
            self.db_pool.close()
            await self.db_pool.wait_closed()
        await super().close()

bot = MyBot()

# ── 명령: 점수입력 ─────────────────────────────────────────────────────────────
@bot.tree.command(name="점수입력", description=f"{ROLE_NAME} 역할 대상 4명 선택 후 점수 입력")
async def 점수입력(interaction: discord.Interaction):
    if interaction.channel_id != CHANNEL_ID:
        await interaction.response.send_message("지정 채널에서만 사용 가능합니다.", ephemeral=True)
        return
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("길드에서만 사용 가능합니다.", ephemeral=True)
        return
    if bot.db_pool is None:
        await interaction.response.send_message("DB 연결 초기화 실패", ephemeral=True)
        return
    role = discord.utils.get(guild.roles, name=ROLE_NAME)
    if role is None:
        await interaction.response.send_message(f"역할 '{ROLE_NAME}' 없음", ephemeral=True)
        return
    members = [m for m in guild.members if (role in m.roles and not m.bot)]
    if len(members) < 4:
        await interaction.response.send_message("인원 부족: 최소 4명 필요", ephemeral=True)
        return
    view = PagedPlayerSelectView(members, pool=bot.db_pool, per_page=PAGE_SIZE)
    await interaction.response.send_message("현재 페이지에서 정확히 4명을 선택하세요.", view=view, ephemeral=True)

# ── 명령: 점수조회_게임 ─────────────────────────────────────────────────────────
@bot.tree.command(name="점수조회_게임", description="game_id로 해당 게임 점수 조회")
@app_commands.describe(game_id="조회할 게임 ID")
async def 점수조회_게임(interaction: discord.Interaction, game_id: int):
    if interaction.channel_id != CHANNEL_ID:
        await interaction.response.send_message("지정 채널에서만 사용 가능합니다.", ephemeral=True)
        return
    if bot.db_pool is None:
        await interaction.response.send_message("DB 연결 초기화 실패", ephemeral=True)
        return
    rows = await fetch_game(bot.db_pool, game_id)
    if len(rows) != 4:
        await interaction.response.send_message("게임을 찾을 수 없거나 데이터가 불완전합니다.", ephemeral=True)
        return
    await interaction.response.send_message(fmt_game_result(game_id, rows, interaction.guild))

# ── 명령: 점수조회_사용자 ───────────────────────────────────────────────────────
@bot.tree.command(name="점수조회_사용자", description="특정 사용자가 참여한 게임 목록")
@app_commands.describe(user="사용자", limit="최근 N건 (기본 10)")
async def 점수조회_사용자(interaction: discord.Interaction, user: discord.User, limit: int = 10):
    if interaction.channel_id != CHANNEL_ID:
        await interaction.response.send_message("지정 채널에서만 사용 가능합니다.", ephemeral=True)
        return
    if bot.db_pool is None:
        await interaction.response.send_message("DB 연결 초기화 실패", ephemeral=True)
        return
    async with bot.db_pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT g.id, g.date, gd.score, gd.position
            FROM game_detail gd
            JOIN game g ON g.id = gd.game_id
            WHERE gd.user_id=%s
            ORDER BY g.id DESC
            LIMIT %s
            """,
            (int(user.id), int(limit)),
        )
        rows = await cur.fetchall()
    if not rows:
        await interaction.response.send_message("해당 사용자의 기록이 없습니다.", ephemeral=True)
        return
    lines = []
    for gid, gdate, score, pos in rows:
        lines.append(f"#{gid} {gdate} — {POS_LABEL[int(pos)]} {score}")
    await interaction.response.send_message(
        f"{mention(int(user.id))} 최근 {len(rows)}건\n" + "\n".join(lines)
    )

# ── 명령: 점수조회_랭킹 ─────────────────────────────────────────────────────────
@bot.tree.command(name="점수조회_랭킹", description="누적 점수 상위 사용자")
@app_commands.describe(limit="상위 N명 (기본 10)")
async def 점수조회_랭킹(interaction: discord.Interaction, limit: int = 10):
    if interaction.channel_id != CHANNEL_ID:
        await interaction.response.send_message("지정 채널에서만 사용 가능합니다.", ephemeral=True)
        return
    if bot.db_pool is None:
        await interaction.response.send_message("DB 연결 초기화 실패", ephemeral=True)
        return
    async with bot.db_pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT user_id, SUM(score) AS total, COUNT(*) AS games
            FROM game_detail
            GROUP BY user_id
            ORDER BY total DESC, user_id ASC
            LIMIT %s
            """,
            (int(limit),),
        )
        rows = await cur.fetchall()
    if not rows:
        await interaction.response.send_message("데이터가 없습니다.", ephemeral=True)
        return
    lines = []
    for i, (uid, total, games) in enumerate(rows, 1):
        lines.append(f"{i}. {mention(int(uid))} 총 {int(total)} ({int(games)}판)")
    await interaction.response.send_message("누적 랭킹\n" + "\n".join(lines))

# ── 명령: 게임수정 ─────────────────────────────────────────────────────────────
@bot.tree.command(name="게임수정", description="game_id로 점수 수정")
@app_commands.describe(game_id="수정할 게임 ID")
async def 게임수정(interaction: discord.Interaction, game_id: int):
    if interaction.channel_id != CHANNEL_ID:
        await interaction.response.send_message("지정 채널에서만 사용 가능합니다.", ephemeral=True)
        return
    if bot.db_pool is None:
        await interaction.response.send_message("DB 연결 초기화 실패", ephemeral=True)
        return
    rows = await fetch_game(bot.db_pool, game_id)
    if len(rows) != 4:
        await interaction.response.send_message("게임을 찾을 수 없거나 데이터가 불완전합니다.", ephemeral=True)
        return
    await interaction.response.send_modal(EditScoreModal(game_id, rows, interaction.guild, bot.db_pool))

# ── ENTRY ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    bot.run(BOT_TOKEN)
