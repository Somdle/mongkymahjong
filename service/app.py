# app.py  (전체 코드)

import os
from typing import List, Tuple, Dict, Any, Iterable
from collections import defaultdict
from datetime import datetime, timezone

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

# ── 마작 점수 상수 ────────────────────────────────────────────────────────────
START_POINTS = 25_000                   # 시작 점수
TARGET_TOTAL = 100_000                  # 4인 합계 검증
UMA_BY_RANK = {1: 15, 2: 5, 3: -5, 4: -15}  # 1위~4위 우마 15/5/-5/-15
POS_LABEL = {0: "동", 1: "서", 2: "남", 3: "북"}
# 동점 타이브레이크: ESWN(동(0)→남(2)→서(1)→북(3)) 우선
TIEBREAK_ESWN = {0: 0, 2: 1, 1: 2, 3: 3}

def rank_sort_key(score: int, pos: int) -> Tuple[int, int]:
    """점수 내림차순, 동점 시 ESWN 우선순위."""
    return (-score, TIEBREAK_ESWN[pos])

def calc_hanchan_points(end_points: int, rank: int) -> float:
    """
    리치 마작 대회 점수(반장 포인트):
    ((종료점수 - 25,000) / 1,000) + 우마(rank). 1위 +15, 2위 +5, 3위 -5, 4위 -15.
    """
    base = (end_points - START_POINTS) / 1000.0
    return base + UMA_BY_RANK[rank]

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

async def delete_game(pool: aiomysql.Pool, game_id: int) -> None:
    async with pool.acquire() as conn:
        await conn.begin()
        try:
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM game_detail WHERE game_id=%s", (game_id,))
                await cur.execute("DELETE FROM game WHERE id=%s", (game_id,))
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise

async def fetch_all_details(pool: aiomysql.Pool) -> List[Tuple[int, int, int, int]]:
    """모든 game_detail: (game_id, user_id, score, position)"""
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute("SELECT game_id, user_id, score, position FROM game_detail ORDER BY game_id ASC")
        rows = await cur.fetchall()
    return [(int(g), int(u), int(s), int(p)) for (g, u, s, p) in rows]

# ── 임베드 ────────────────────────────────────────────────────────────────────
def build_game_embed(game_id: int, rows: List[Dict[str, Any]], *, title_prefix: str = "게임 결과") -> discord.Embed:
    """
    공개용 임베드:
      - 좌석별: 멘션, 원점수, 계산점(+/-) 표시
      - 하단: 원점수 기준 순위(동점 ESWN)
    """
    embed = discord.Embed(
        title=f"{title_prefix} #{game_id}",
        description="계산식: ((종료점수-25,000)/1,000)+우마 [1위+15, 2위+5, 3위-5, 4위-15] • 동점 ESWN(동→남→서→북)",
        colour=discord.Colour.blue(),
        timestamp=datetime.now(timezone.utc),
    )

    total_raw = 0
    # 순위 산출을 위해 점수 정렬
    sorted_rows = sorted(rows, key=lambda r: rank_sort_key(r["score"], r["position"]))
    # user_id -> rank
    rank_by_uid: Dict[int, int] = {r["user_id"]: i+1 for i, r in enumerate(sorted_rows)}

    # 좌석표시는 좌석 순서대로
    by_pos = {r["position"]: r for r in rows}
    for p in [0, 1, 2, 3]:
        r = by_pos.get(p)
        if not r:
            continue
        uid = int(r["user_id"]); raw = int(r["score"])
        rk = rank_by_uid[uid]
        hp = calc_hanchan_points(raw, rk)
        total_raw += raw
        embed.add_field(
            name=f"{POS_LABEL[p]}",
            value=f"{mention(uid)}\n원점수 **{raw}**\n계산점 **{hp:+.1f}**",
            inline=True
        )

    # 원점수 기준 순위표
    rank_lines = [
        f"{i}. {POS_LABEL[r['position']]} {mention(int(r['user_id']))} **{int(r['score'])}**"
        for i, r in enumerate(sorted_rows, 1)
    ]
    embed.add_field(name="원점수 순위(동점 ESWN)", value="\n".join(rank_lines) or "-", inline=False)
    embed.set_footer(text=f"합계 {total_raw} • game_id {game_id}")
    return embed

# ── View ──────────────────────────────────────────────────────────────────────
class ManageGameView(View):
    """
    재시작 생존: 버튼 로직은 on_interaction에서 custom_id로 처리.
    custom_id: mm_edit:<game_id>:<message_id>:<channel_id> / mm_del:...
    """
    def __init__(self, game_id: int, message_id: int, channel_id: int):
        super().__init__(timeout=None)
        self.add_item(Button(
            label="게임 수정",
            style=discord.ButtonStyle.primary,
            custom_id=f"mm_edit:{game_id}:{message_id}:{channel_id}",
        ))
        self.add_item(Button(
            label="게임 삭제",
            style=discord.ButtonStyle.danger,
            custom_id=f"mm_del:{game_id}:{message_id}:{channel_id}",
        ))

class ConfirmDeleteView(View):
    def __init__(self, game_id: int, message_id: int, channel_id: int):
        super().__init__(timeout=60)
        self.add_item(Button(
            label="삭제 확인",
            style=discord.ButtonStyle.danger,
            custom_id=f"mm_del_ok:{game_id}:{message_id}:{channel_id}",
        ))
        self.add_item(Button(
            label="취소",
            style=discord.ButtonStyle.secondary,
            custom_id=f"mm_del_cancel:{game_id}:{message_id}:{channel_id}",
        ))

# ── 모달 ──────────────────────────────────────────────────────────────────────
class ScoreModal(Modal):
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
        for p in [0, 1, 2, 3]:
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
        scores_by_pos: Dict[int, int] = {}
        total = 0
        for p in [0, 1, 2, 3]:
            raw = self.children[p].value
            try:
                v = int(raw)
            except ValueError:
                await interaction.response.send_message(
                    f"{POS_LABEL[p]} {self.members_by_pos[p].display_name}: 정수만 입력", ephemeral=True
                )
                return
            scores_by_pos[p] = v
            total += v

        if total != TARGET_TOTAL:
            await interaction.response.send_message(
                f"총합 {total}. {TARGET_TOTAL}이어야 합니다.", ephemeral=True
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
                        inserts.append((game_id, uid, scores_by_pos[p], p))
                    await cur.executemany(
                        "INSERT INTO game_detail (game_id, user_id, score, position) VALUES (%s,%s,%s,%s)",
                        inserts
                    )
                await conn.commit()
        except Exception as e:
            try: await conn.rollback()
            except Exception: pass
            await interaction.response.send_message(f"DB 오류: {e}", ephemeral=True)
            return

        await interaction.response.send_message(f"저장 완료. game_id={game_id}", ephemeral=True)

        # 공개 메시지 + 관리 버튼
        rows = [{"user_id": int(self.members_by_pos[p].id), "score": scores_by_pos[p], "position": p} for p in [0,1,2,3]]
        embed = build_game_embed(game_id, rows, title_prefix="게임 결과")
        msg = await interaction.followup.send(embed=embed, wait=True)  # 공개
        await msg.edit(view=ManageGameView(game_id, msg.id, msg.channel.id))

class EditScoreModal(Modal):
    def __init__(self, game_id: int, rows: List[Dict[str, Any]], guild: discord.Guild,
                 pool: aiomysql.Pool, message_id: int, channel_id: int):
        super().__init__(title=f"게임 #{game_id} 점수 수정")
        self.game_id = game_id
        self.pool = pool
        self.message_id = message_id
        self.channel_id = channel_id
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
                await interaction.response.send_message(f"{POS_LABEL[p]}: 정수만 입력", ephemeral=True)
                return
            new_scores[p] = v
            total += v
        if total != TARGET_TOTAL:
            await interaction.response.send_message(f"총합 {total}. {TARGET_TOTAL}이어야 합니다.", ephemeral=True)
            return

        # DB 업데이트
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
            try: await conn.rollback()
            except Exception: pass
            await interaction.response.send_message(f"DB 오류: {e}", ephemeral=True)
            return

        # 공개 메시지 편집
        try:
            channel = interaction.client.get_channel(self.channel_id) or await interaction.client.fetch_channel(self.channel_id)  # type: ignore
            msg = await channel.fetch_message(self.message_id)  # type: ignore
            rows = await fetch_game(self.pool, self.game_id)
            new_embed = build_game_embed(self.game_id, rows, title_prefix="게임 수정 결과")
            await msg.edit(embed=new_embed, view=ManageGameView(self.game_id, self.message_id, self.channel_id))
            await interaction.response.send_message("수정 완료", ephemeral=True)
            await interaction.followup.send(f"🛠️ 게임 #{self.game_id} 점수 수정됨.", ephemeral=False)
        except Exception as e:
            await interaction.response.send_message(f"메시지 편집 실패: {e}", ephemeral=True)

# ── 선택 뷰 ────────────────────────────────────────────────────────────────────
class PagedPlayerSelectView(View):
    """역할 보유 사용자 목록을 페이지로 나눠 Select 제공. 정확히 4명 선택."""
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

# ── 랭킹 계산 ──────────────────────────────────────────────────────────────────
def iter_groupby_game(rows: Iterable[Tuple[int, int, int, int]]) -> Iterable[Tuple[int, List[Tuple[int,int,int,int]]]]:
    """game_id 기준으로 묶기."""
    cur_gid = None
    bucket: List[Tuple[int,int,int,int]] = []
    for gid, uid, sc, pos in rows:
        if cur_gid is None:
            cur_gid = gid
        if gid != cur_gid:
            yield cur_gid, bucket
            bucket = []
            cur_gid = gid
        bucket.append((gid, uid, sc, pos))
    if cur_gid is not None and bucket:
        yield cur_gid, bucket

def assign_ranks_for_game(game_rows: List[Tuple[int,int,int,int]]) -> Dict[int, int]:
    """해당 게임의 user_id -> rank(1~4). 원점수 내림차순, 동점 ESWN."""
    # 튜플: (uid, score, pos)
    triples = [(uid, sc, pos) for (_, uid, sc, pos) in game_rows]
    triples.sort(key=lambda t: rank_sort_key(t[1], t[2]))
    return {uid: i+1 for i, (uid, _, _) in enumerate(triples)}

async def compute_aggregate_points(pool: aiomysql.Pool) -> List[Tuple[int, float, int]]:
    """
    사용자별 총 계산점 합계와 판수.
    return: [(user_id, total_points, games), ...]
    """
    rows = await fetch_all_details(pool)
    if not rows:
        return []
    totals: Dict[int, float] = defaultdict(float)
    counts: Dict[int, int] = defaultdict(int)

    for gid, bucket in iter_groupby_game(rows):
        if len(bucket) != 4:
            # 불완전 게임은 스킵
            continue
        ranks = assign_ranks_for_game(bucket)
        for _, uid, sc, _ in bucket:
            rk = ranks[uid]
            hp = calc_hanchan_points(sc, rk)
            totals[uid] += hp
            counts[uid] += 1

    result = [(uid, totals[uid], counts[uid]) for uid in totals.keys()]
    # 정렬: 평균 내림차순, 총점 내림차순, user_id 오름차순
    result.sort(key=lambda t: (-(t[1] / t[2] if t[2] else -1e9), -t[1], t[0]))
    return result

# ── BOT ────────────────────────────────────────────────────────────────────────
mahjong_group = app_commands.Group(name="마장", description="마장 명령 모음")

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
        self.tree.add_command(mahjong_group)
        await self.tree.sync()

    async def close(self):
        if self.db_pool is not None:
            self.db_pool.close()
            await self.db_pool.wait_closed()
        await super().close()

bot = MyBot()

# ── /마장 ─────────────────────────────────────────────────────────────────────
@mahjong_group.command(name="점수입력", description=f"{ROLE_NAME} 역할 4명 선택 후 점수 입력")
async def cmd_score_input(interaction: discord.Interaction):
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

@mahjong_group.command(name="순위조회", description="계산점 기준 상위 사용자(평균=총점/판수)")
@app_commands.describe(limit="상위 N명 (기본 10)")
async def cmd_rank(interaction: discord.Interaction, limit: int = 10):
    if interaction.channel_id != CHANNEL_ID:
        await interaction.response.send_message("지정 채널에서만 사용 가능합니다.", ephemeral=True)
        return
    pool = bot.db_pool
    if pool is None:
        await interaction.response.send_message("DB 연결 초기화 실패", ephemeral=True)
        return

    agg = await compute_aggregate_points(pool)
    if not agg:
        await interaction.response.send_message("데이터가 없습니다.", ephemeral=True)
        return

    rows = agg[:max(1, int(limit))]
    embed = discord.Embed(
        title="마장 순위조회 — 계산점 평균(총점/판수)",
        colour=discord.Colour.gold(),
        timestamp=datetime.now(timezone.utc),
    )
    for i, (uid, total, games) in enumerate(rows, 1):
        avg_val = total / games if games else 0.0
        embed.add_field(
            name=f"{i}.",
            value=f"{mention(uid)}\n총점 **{total:+.1f}** / 판수 {games} = 평균 **{avg_val:+.2f}**",
            inline=False
        )
    embed.set_footer(text="계산식: ((종료-25,000)/1,000)+우마 • 우마 15/5/-5/-15")
    # 호출자에게만 표시
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ── 버튼 처리: 재시작 후에도 동작 ──────────────────────────────────────────────
@bot.event
async def on_interaction(interaction: discord.Interaction):
    if interaction.type != discord.InteractionType.component:
        return
    data = interaction.data or {}
    cid = str(data.get("custom_id", ""))
    if not cid:
        return

    try:
        prefix, gid, mid, ch = cid.split(":", 3)
    except ValueError:
        return

    if interaction.channel_id != CHANNEL_ID:
        await interaction.response.send_message("지정 채널에서만 가능", ephemeral=True)
        return

    pool = bot.db_pool
    if pool is None:
        await interaction.response.send_message("DB 연결 초기화 실패", ephemeral=True)
        return

    if prefix == "mm_edit":
        rows = await fetch_game(pool, int(gid))
        if len(rows) != 4:
            await interaction.response.send_message("게임 데이터를 찾을 수 없습니다.", ephemeral=True)
            return
        await interaction.response.send_modal(
            EditScoreModal(int(gid), rows, interaction.guild, pool, int(mid), int(ch))
        )
        return

    if prefix == "mm_del":
        await interaction.response.send_message(
            f"게임 #{gid} 삭제 확인이 필요합니다.",
            view=ConfirmDeleteView(int(gid), int(mid), int(ch)),
            ephemeral=True
        )
        return

    if prefix == "mm_del_ok":
        try:
            await delete_game(pool, int(gid))
        except Exception as e:
            await interaction.response.send_message(f"삭제 실패: {e}", ephemeral=True)
            return
        # 공개 알림
        try:
            channel = interaction.client.get_channel(int(ch)) or await interaction.client.fetch_channel(int(ch))  # type: ignore
            msg = await channel.fetch_message(int(mid))  # type: ignore
            await msg.delete()
        except Exception:
            pass
        await interaction.response.send_message("삭제 완료", ephemeral=True)
        await interaction.followup.send(f"🗑️ 게임 #{gid} 기록이 삭제되었습니다.", ephemeral=False)
        return

    if prefix == "mm_del_cancel":
        await interaction.response.send_message("삭제 취소", ephemeral=True)
        return

# ── ENTRY ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    bot.run(BOT_TOKEN)
