# bot.py

import os
from typing import List, Tuple, Dict, Any
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import aiomysql  # pip install aiomysql python-dotenv discord.py
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

# 좌석 및 타이브레이크
POS_LABEL = {0: "동", 1: "서", 2: "남", 3: "북"}
LABEL_ORDER_FOR_INPUT = [0, 1, 2, 3]
TIEBREAK_ESWN = {0: 0, 2: 1, 1: 2, 3: 3}

def rank_key(score: int, pos: int) -> Tuple[int, int]:
    return (-score, TIEBREAK_ESWN[pos])

def mention(uid: int) -> str:
    return f"<@{uid}>"

# ── DB 유틸 ───────────────────────────────────────────────────────────────────
async def fetch_game(pool: aiomysql.Pool, game_id: int) -> List[Dict[str, Any]]:
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT user_id, score, position FROM game_detail WHERE game_id=%s",
            (game_id,),
        )
        rows = await cur.fetchall()
    return [{"user_id": int(r[0]), "score": int(r[1]), "position": int(r[2])} for r in rows]

async def delete_game(pool: aiomysql.Pool, game_id: int) -> int:
    async with pool.acquire() as conn:
        await conn.begin()
        try:
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM game_detail WHERE game_id=%s", (game_id,))
                await cur.execute("DELETE FROM game WHERE id=%s", (game_id,))
                affected = cur.rowcount
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
    return affected

async def fetch_games_between(pool: aiomysql.Pool, start_dt: datetime, end_dt: datetime, limit: int = 5) -> Dict[int, List[Dict[str, Any]]]:
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT g.id, g.date, gd.user_id, gd.score, gd.position
            FROM game g
            JOIN game_detail gd ON gd.game_id = g.id
            WHERE g.date >= %s AND g.date < %s
            ORDER BY g.id ASC, gd.position ASC
            """,
            (start_dt, end_dt)
        )
        rows = await cur.fetchall()
    grouped: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    order: List[int] = []
    for gid, gdate, uid, score, pos in rows:
        gid = int(gid)
        if gid not in grouped:
            order.append(gid)
        grouped[gid].append({
            "user_id": int(uid),
            "score": int(score),
            "position": int(pos),
            "date": gdate,
        })
    limited: Dict[int, List[Dict[str, Any]]] = {}
    for gid in order[:limit]:
        limited[gid] = grouped[gid]
    return limited

# ── 임베드 ────────────────────────────────────────────────────────────────────
def build_game_embed(game_id: int, rows: List[Dict[str, Any]], *, title_prefix: str = "게임 결과") -> discord.Embed:
    embed = discord.Embed(
        title=f"{title_prefix} #{game_id}",
        description="동점 시 ESWN(동→남→서→북) 순",
        colour=discord.Colour.blue(),
        timestamp=datetime.now(timezone.utc),
    )
    total = 0
    by_pos = {r["position"]: r for r in rows}
    for p in [0, 1, 2, 3]:
        r = by_pos.get(p)
        if not r:
            continue
        total += r["score"]
        embed.add_field(
            name=f"{POS_LABEL[p]} {mention(r['user_id'])}",
            value=f"**{r['score']}**",
            inline=True
        )
    ranked = sorted(rows, key=lambda r: rank_key(r["score"], r["position"]))
    ranks = "\n".join(
        f"{i}. {POS_LABEL[r['position']]} {mention(r['user_id'])} **{r['score']}**"
        for i, r in enumerate(ranked, 1)
    )
    embed.add_field(name="순위", value=ranks or "-", inline=False)
    embed.set_footer(text=f"합계 {total} • /점수조회_게임 {game_id}")
    return embed

# ── UI ─────────────────────────────────────────────────────────────────────────
class ScoreModal(Modal):
    """새 게임 입력 + 저장 + 공개 임베드."""
    def __init__(self, ordered_members: List[discord.Member], pool: aiomysql.Pool):
        super().__init__(title="점수 입력")
        if len(ordered_members) != 4:
            raise ValueError("ScoreModal requires exactly 4 members")
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

        # DB 저장
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

        # 개인 확인 후 공개 임베드 + 관리 버튼
        await interaction.response.send_message(f"저장 완료. game_id={game_id}", ephemeral=True)
        rows = [{"user_id": int(self.members_by_pos[p].id), "score": scores[p], "position": p} for p in [0,1,2,3]]
        embed = build_game_embed(game_id, rows, title_prefix="게임 결과")
        await interaction.followup.send(embed=embed, view=ManageGameView(game_id, self.pool))

class ReenterView(View):
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
                default=str(r["score"]),
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

        rows = [{"user_id": self.members_by_pos[p][0], "score": new_scores[p], "position": p} for p in [0,1,2,3]]
        embed = build_game_embed(self.game_id, rows, title_prefix="게임 수정 결과")
        await interaction.response.send_message(f"게임 #{self.game_id} 수정 완료", ephemeral=True)
        await interaction.followup.send(embed=embed)

class ReenterEditView(View):
    def __init__(self, game_id: int, rows: List[Dict[str, Any]], guild: discord.Guild, pool: aiomysql.Pool):
        super().__init__(timeout=120)
        self.game_id, self.rows, self.guild, self.pool = game_id, rows, guild, pool
        btn = Button(label="다시 입력", style=discord.ButtonStyle.primary)
        async def on_click(itx: discord.Interaction):
            await itx.response.send_modal(EditScoreModal(self.game_id, self.rows, self.guild, self.pool))
        btn.callback = on_click
        self.add_item(btn)

class ConfirmDeleteView(View):
    def __init__(self, game_id: int, pool: aiomysql.Pool):
        super().__init__(timeout=60)
        self.game_id = game_id
        self.pool = pool
        ok_btn = Button(label="삭제 확인", style=discord.ButtonStyle.danger)
        cancel_btn = Button(label="취소", style=discord.ButtonStyle.secondary)

        async def on_ok(itx: discord.Interaction):
            if itx.channel_id != CHANNEL_ID:
                await itx.response.send_message("지정 채널에서만 가능", ephemeral=True)
                return
            try:
                await delete_game(self.pool, self.game_id)
            except Exception as e:
                await itx.response.send_message(f"삭제 실패: {e}", ephemeral=True)
                return
            await itx.response.send_message(f"게임 #{self.game_id} 삭제됨", ephemeral=True)
            await itx.followup.send(f"🗑️ 게임 #{self.game_id} 기록이 삭제되었습니다.")

        async def on_cancel(itx: discord.Interaction):
            await itx.response.send_message("삭제 취소", ephemeral=True)

        ok_btn.callback = on_ok
        cancel_btn.callback = on_cancel
        self.add_item(ok_btn); self.add_item(cancel_btn)

class ManageGameView(View):
    """결과 메시지 아래 관리 버튼. 콜백으로 직접 처리."""
    def __init__(self, game_id: int, pool: aiomysql.Pool):
        super().__init__(timeout=300)
        self.game_id = game_id
        self.pool = pool

        btn_view = Button(label="게임 조회", style=discord.ButtonStyle.secondary)
        btn_edit = Button(label="게임 수정", style=discord.ButtonStyle.primary)
        btn_del  = Button(label="게임 삭제", style=discord.ButtonStyle.danger)

        async def on_view(itx: discord.Interaction):
            if itx.channel_id != CHANNEL_ID:
                await itx.response.send_message("지정 채널에서만 가능", ephemeral=True)
                return
            rows = await fetch_game(self.pool, self.game_id)
            if len(rows) != 4:
                await itx.response.send_message("게임 데이터를 찾을 수 없습니다.", ephemeral=True)
                return
            await itx.response.send_message(embed=build_game_embed(self.game_id, rows, title_prefix="게임 조회"), ephemeral=True)

        async def on_edit(itx: discord.Interaction):
            if itx.channel_id != CHANNEL_ID:
                await itx.response.send_message("지정 채널에서만 가능", ephemeral=True)
                return
            rows = await fetch_game(self.pool, self.game_id)
            if len(rows) != 4:
                await itx.response.send_message("게임 데이터를 찾을 수 없습니다.", ephemeral=True)
                return
            await itx.response.send_modal(EditScoreModal(self.game_id, rows, itx.guild, self.pool))

        async def on_del(itx: discord.Interaction):
            if itx.channel_id != CHANNEL_ID:
                await itx.response.send_message("지정 채널에서만 가능", ephemeral=True)
                return
            await itx.response.send_message(
                f"게임 #{self.game_id} 삭제 확인이 필요합니다.",
                view=ConfirmDeleteView(self.game_id, self.pool),
                ephemeral=True
            )

        btn_view.callback = on_view
        btn_edit.callback = on_edit
        btn_del.callback  = on_del

        self.add_item(btn_view); self.add_item(btn_edit); self.add_item(btn_del)

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

# ── 명령들 ─────────────────────────────────────────────────────────────────────
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

class PagedPlayerSelectView(View):
    """역할 보유 사용자 목록을 페이지로 나눠 Select 제공. 정확히 4명 선택."""
    def __init__(self, members: List[discord.Member], pool: aiomysql.Pool, per_page: int = PAGE_SIZE):
        super().__init__(timeout=120)
        self.members = members
        self.pool = pool
        self.per_page = max(4, min(25, per_page))  # Select 옵션은 최대 25개/페이지. :contentReference[oaicite:1]{index=1}
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
    await interaction.response.send_message(embed=build_game_embed(game_id, rows, title_prefix="게임 조회"))

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
    embed = discord.Embed(
        title="누적 랭킹",
        colour=discord.Colour.gold(),
        timestamp=datetime.now(timezone.utc),
    )
    for i, (uid, total, games) in enumerate(rows, 1):
        embed.add_field(name=f"{i}. {mention(int(uid))}", value=f"총 {int(total)}점 • {int(games)}판", inline=False)
    embed.set_footer(text=f"상위 {len(rows)}명")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="점수조회_기간", description="기간 내 게임 목록 조회")
@app_commands.describe(start="YYYY-MM-DD", end="YYYY-MM-DD", limit="최대 몇 건(기본 5)")
async def 점수조회_기간(interaction: discord.Interaction, start: str, end: str, limit: int = 5):
    if interaction.channel_id != CHANNEL_ID:
        await interaction.response.send_message("지정 채널에서만 사용 가능합니다.", ephemeral=True)
        return
    if bot.db_pool is None:
        await interaction.response.send_message("DB 연결 초기화 실패", ephemeral=True)
        return
    try:
        start_dt = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end_dt = datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
    except ValueError:
        await interaction.response.send_message("날짜 형식 오류. 예: 2025-10-01", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    grouped = await fetch_games_between(bot.db_pool, start_dt, end_dt, limit=limit)
    if not grouped:
        await interaction.followup.send("해당 기간 기록이 없습니다.", ephemeral=True)
        return
    for gid, rows in grouped.items():
        await interaction.followup.send(embed=build_game_embed(gid, rows, title_prefix="기간 조회"), ephemeral=False)
    await interaction.followup.send(f"{len(grouped)}건 표시 완료.", ephemeral=True)

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

@bot.tree.command(name="게임삭제", description="game_id로 게임 기록 삭제")
@app_commands.describe(game_id="삭제할 게임 ID")
async def 게임삭제(interaction: discord.Interaction, game_id: int):
    if interaction.channel_id != CHANNEL_ID:
        await interaction.response.send_message("지정 채널에서만 사용 가능합니다.", ephemeral=True)
        return
    if bot.db_pool is None:
        await interaction.response.send_message("DB 연결 초기화 실패", ephemeral=True)
        return
    rows = await fetch_game(bot.db_pool, game_id)
    if not rows:
        await interaction.response.send_message("게임을 찾을 수 없습니다.", ephemeral=True)
        return
    await interaction.response.send_message(
        f"게임 #{game_id} 삭제 확인이 필요합니다.",
        view=ConfirmDeleteView(game_id, bot.db_pool),
        ephemeral=True
    )

# ── ENTRY ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    bot.run(BOT_TOKEN)
