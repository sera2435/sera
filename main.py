import discord
from discord.ext import commands, tasks
from discord.ui import Button, View, Select, Modal, TextInput
import sqlite3
import os
import asyncio
import time
import re
import traceback
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

DB_PATH = "leaderboard.db"

RANK_ROLES = {
    "Bronze": (0, 50),
    "Silver": (51, 150),
    "Gold": (151, 300),
    "Diamond": (301, 500),
    "Grandmaster": (501, 999999)
}
MVP_ROLE_NAME = "👑 MVP of the Week"
DISPUTE_CHANNEL_NAME = "disputes"
MATCH_LOG_CHANNEL_NAME = "match-logs"
LEADERBOARD_CHANNEL_NAME = "leaderboard"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS stats (
            user_id INTEGER PRIMARY KEY,
            wins INTEGER DEFAULT 0,
            losses INTEGER DEFAULT 0,
            points INTEGER DEFAULT 0,
            not_ready_count INTEGER DEFAULT 0,
            cooldown_until TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS matches (
            match_id INTEGER PRIMARY KEY AUTOINCREMENT,
            mode TEXT,
            team1_leader INTEGER,
            team2_leader INTEGER,
            team1_players TEXT,
            team2_players TEXT,
            winner_team TEXT,
            screenshots TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    cursor.execute("PRAGMA table_info(stats)")
    columns = [column[1] for column in cursor.fetchall()]
    if "cooldown_until" not in columns:
        cursor.execute("ALTER TABLE stats ADD COLUMN cooldown_until TIMESTAMP")
    if "not_ready_count" not in columns:
        cursor.execute("ALTER TABLE stats ADD COLUMN not_ready_count INTEGER DEFAULT 0")

    conn.commit()
    conn.close()

init_db()

# --- DATABASE & LOGGING HELPERS ---

def save_match_history(mode, t1_leader, t2_leader, team_a, team_b, winner_team, screenshot_urls):
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO matches (mode, team1_leader, team2_leader, team1_players, team2_players, winner_team, screenshots)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            mode,
            t1_leader,
            t2_leader,
            ",".join(map(str, team_a)),
            ",".join(map(str, team_b)),
            winner_team,
            ",".join(screenshot_urls)
        ))
        match_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return match_id
    except Exception as e:
        print(f"Error saving match history: {e}")
        return None

async def log_match_to_admin_channel(guild, match_id, mode, t1_leader, t2_leader, team_a, team_b, winner_team, screenshots):
    log_channel = discord.utils.get(guild.text_channels, name=MATCH_LOG_CHANNEL_NAME)
    if not log_channel:
        print(f"⚠️ Warning: Channel '{MATCH_LOG_CHANNEL_NAME}' not found!")
        return

    t1_mentions = ", ".join([f"<@{p}>" for p in team_a])
    t2_mentions = ", ".join([f"<@{p}>" for p in team_b])
    shots_formatted = "\n".join([f"🖼️ [Screenshot {i+1}]({url})" for i, url in enumerate(screenshots)])

    embed = discord.Embed(
        title=f"📜 MATCH LOG #{match_id if match_id else 'N/A'} ({mode})",
        color=discord.Color.purple(),
        timestamp=datetime.now()
    )
    embed.add_field(name="👑 Team 1 Leader", value=f"<@{t1_leader}>", inline=True)
    embed.add_field(name="👑 Team 2 Leader", value=f"<@{t2_leader}>", inline=True)
    embed.add_field(name="🏆 Winner", value=f"**{winner_team}**", inline=False)
    embed.add_field(name="🔵 Team 1 Players", value=t1_mentions, inline=False)
    embed.add_field(name="🔴 Team 2 Players", value=t2_mentions, inline=False)
    embed.add_field(name="📸 Screenshots", value=shots_formatted if shots_formatted else "None Uploaded", inline=False)
    embed.set_footer(text="CHEMICAL B Match History")

    await log_channel.send(embed=embed)

def is_user_banned(user_id: int) -> tuple[bool, str]:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT cooldown_until FROM stats WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    
    if row and row[0]:
        cooldown_time = datetime.fromisoformat(row[0])
        if datetime.now() < cooldown_time:
            time_left = int((cooldown_time - datetime.now()).total_seconds() / 60)
            return True, f"🚫 **You are on Cooldown!** You are banned from matchmaking for another **{time_left} minutes**."
    return False, ""

def record_not_ready(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT not_ready_count FROM stats WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    
    if row:
        count = row[0] + 1
        if count >= 3:
            cooldown_until = (datetime.now() + timedelta(hours=2)).isoformat()
            cursor.execute("UPDATE stats SET not_ready_count = 0, cooldown_until = ? WHERE user_id = ?", (cooldown_until, user_id))
        else:
            cursor.execute("UPDATE stats SET not_ready_count = ? WHERE user_id = ?", (count, user_id))
    else:
        cursor.execute("INSERT INTO stats (user_id, not_ready_count) VALUES (?, 1)", (user_id,))
    
    conn.commit()
    conn.close()

async def update_user_rank_roles(guild: discord.Guild, user_id: int, points: int):
    member = guild.get_member(user_id)
    if not member:
        return

    target_rank = None
    for rank_name, (min_pts, max_pts) in RANK_ROLES.items():
        if min_pts <= points <= max_pts:
            target_rank = rank_name
            break

    try:
        for rank_name in RANK_ROLES.keys():
            role = discord.utils.get(guild.roles, name=rank_name)
            if role:
                if rank_name == target_rank and role not in member.roles:
                    await member.add_roles(role)
                elif rank_name != target_rank and role in member.roles:
                    await member.remove_roles(role)
    except discord.Forbidden:
        print(f"⚠️ Missing Permissions to update roles for user {user_id}")
    except Exception as e:
        print(f"Error updating roles: {e}")

def add_win_to_user(guild: discord.Guild, user_id: int, points_to_add: int = 10):
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT wins, points FROM stats WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        new_pts = points_to_add
        if row:
            new_pts = row[1] + points_to_add
            cursor.execute("UPDATE stats SET wins = wins + 1, points = points + ? WHERE user_id = ?", (points_to_add, user_id))
        else:
            cursor.execute("INSERT INTO stats (user_id, wins, losses, points) VALUES (?, 1, 0, ?)", (user_id, points_to_add))
        conn.commit()
        conn.close()
        
        asyncio.create_task(update_user_rank_roles(guild, user_id, new_pts))
    except Exception as e:
        print(f"Database error: {e}")

def add_loss_to_user(user_id: int):
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT losses FROM stats WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if row:
            cursor.execute("UPDATE stats SET losses = losses + 1 WHERE user_id = ?", (user_id,))
        else:
            cursor.execute("INSERT INTO stats (user_id, wins, losses, points) VALUES (?, 0, 1, 0)", (user_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Database error: {e}")

# --- MATCHMAKING GLOBALS ---

team_lobbies = {
    "1vs1": {"team1": {"leader": None, "members": []}, "team2": {"leader": None, "members": []}},
    "2vs2": {"team1": {"leader": None, "members": []}, "team2": {"leader": None, "members": []}},
    "3vs3": {"team1": {"leader": None, "members": []}, "team2": {"leader": None, "members": []}},
    "4vs4": {"team1": {"leader": None, "members": []}, "team2": {"leader": None, "members": []}}
}

TEAM_LIMITS = {"1vs1": 1, "2vs2": 2, "3vs3": 3, "4vs4": 4}
active_match_players = set()
admin_channels_set = set()
queue_tasks = {}
match_counter = 1

def create_matchmaking_embed():
    embed = discord.Embed(
        title="⚡ CHEMICAL B — OFFICIAL MATCHMAKING ⚡",
        description=(
            "Welcome to the **CHEMICAL B Competitive Free Fire Arena**.\n"
            "Select your mode and join Team 1 or Team 2 below.\n\n"
            "🔊 **Requirement:** You MUST be connected to a Voice Channel in this server to join!\n"
            "🎮 **Format:** Best of 2/3 Rooms\n"
            "⏱️ **Queue Timeout:** 10 Minutes\n"
            "👑 **Team Leader:** First player becomes leader.\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        ),
        color=discord.Color.from_rgb(46, 204, 113)
    )
    for mode, data in team_lobbies.items():
        limit = TEAM_LIMITS[mode]
        t1_count = (1 if data["team1"]["leader"] else 0) + len(data["team1"]["members"])
        t2_count = (1 if data["team2"]["leader"] else 0) + len(data["team2"]["members"])
        t1_leader_str = f"<@{data['team1']['leader']}> 👑" if data["team1"]["leader"] else "*Empty*"
        t2_leader_str = f"<@{data['team2']['leader']}> 👑" if data["team2"]["leader"] else "*Empty*"

        embed.add_field(
            name=f"⚔ **{mode} BET MODE**",
            value=(
                f"🔵 **Team 1 [{t1_count}/{limit}]:** Leader: {t1_leader_str}\n"
                f"🔴 **Team 2 [{t2_count}/{limit}]:** Leader: {t2_leader_str}\n"
            ),
            inline=False
        )
    embed.set_footer(text="CHEMICAL B Competitive System • Voice Required")
    return embed

def extract_user_id(input_str: str) -> int:
    clean_str = re.sub(r'[<@!>]', '', input_str.strip())
    if clean_str.isdigit():
        return int(clean_str)
    return None

# --- MODALS & SELECTS ---

class SubstituteModal(Modal, title="🔄 Substitute Player"):
    new_player_input = TextInput(label="Tag or ID of New Player", placeholder="e.g. @User or 123456789012345678")

    def __init__(self, target_player_id, parent_view, is_ready_phase=False):
        super().__init__()
        self.target_player_id = target_player_id
        self.parent_view = parent_view
        self.is_ready_phase = is_ready_phase

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        new_id = extract_user_id(self.new_player_input.value)
        if not new_id:
            await interaction.followup.send("❌ **Error:** Invalid user tag or ID!", ephemeral=True)
            return

        banned, msg = is_user_banned(new_id)
        if banned:
            await interaction.followup.send(msg, ephemeral=True)
            return

        if new_id in active_match_players:
            await interaction.followup.send("❌ The new player is already in an active match or queue!", ephemeral=True)
            return

        guild = interaction.guild
        old_member = guild.get_member(self.target_player_id)
        new_member = guild.get_member(new_id)

        if not new_member:
            await interaction.followup.send("❌ The new player was not found in this server!", ephemeral=True)
            return

        if not new_member.voice or not new_member.voice.channel:
            await interaction.followup.send(f"⚠️ {new_member.mention} **is not connected to a Voice Channel!** They must join voice first.", ephemeral=True)
            return

        category = self.parent_view.category
        if category:
            for ch in category.channels:
                if old_member and ch.type == discord.ChannelType.voice:
                    await ch.set_permissions(old_member, overwrite=None)
                    await ch.set_permissions(new_member, read_messages=True, send_messages=True, connect=True)

        if self.is_ready_phase:
            ready_view = self.parent_view
            ready_view.all_players.remove(self.target_player_id)
            ready_view.all_players.append(new_id)
            ready_view.ready_players.discard(self.target_player_id)

            if self.target_player_id == ready_view.t1_leader: ready_view.t1_leader = new_id
            elif self.target_player_id == ready_view.t2_leader: ready_view.t2_leader = new_id

            if self.target_player_id in ready_view.team_a:
                ready_view.team_a.remove(self.target_player_id)
                ready_view.team_a.append(new_id)
            elif self.target_player_id in ready_view.team_b:
                ready_view.team_b.remove(self.target_player_id)
                ready_view.team_b.append(new_id)

            active_match_players.discard(self.target_player_id)
            active_match_players.add(new_id)

            await interaction.followup.send(f"🔄 **Player Substituted!** {new_member.mention} replaced <@{self.target_player_id}>.", ephemeral=False)
            await ready_view.text_channel.send(embed=ready_view.update_embed(), view=ready_view)
        else:
            match_view = self.parent_view
            match_view.match_players.remove(self.target_player_id)
            match_view.match_players.append(new_id)
            match_view.subbed_in_players.add(new_id)

            if self.target_player_id == match_view.t1_leader: match_view.t1_leader = new_id
            elif self.target_player_id == match_view.t2_leader: match_view.t2_leader = new_id

            if self.target_player_id in match_view.team_a:
                match_view.team_a.remove(self.target_player_id)
                match_view.team_a.append(new_id)
            elif self.target_player_id in match_view.team_b:
                match_view.team_b.remove(self.target_player_id)
                match_view.team_b.append(new_id)

            active_match_players.discard(self.target_player_id)
            active_match_players.add(new_id)

            await interaction.followup.send(
                f"🔄 **Player Substituted!** {new_member.mention} replaced <@{self.target_player_id}>.\n"
                f"ℹ️ <@{self.target_player_id}> and {new_member.mention} will receive **5 PTS** (Half Points) if their team wins.",
                ephemeral=False
            )

class SubstituteSelectView(View):
    def __init__(self, parent_view, leader_team, is_ready_phase=False):
        super().__init__(timeout=60)
        self.parent_view = parent_view
        self.is_ready_phase = is_ready_phase

        options = []
        guild = parent_view.text_channel.guild
        for pid in leader_team:
            member = guild.get_member(pid)
            label_text = member.display_name if member else f"User {pid}"
            options.append(discord.SelectOption(label=label_text, value=str(pid), description=f"ID: {pid}"))

        self.select = Select(placeholder="Select player to remove...", options=options)
        self.select.callback = self.select_callback
        self.add_item(self.select)

    async def select_callback(self, interaction: discord.Interaction):
        target_id = int(self.select.values[0])
        await interaction.response.send_modal(SubstituteModal(target_id, self.parent_view, self.is_ready_phase))

# --- AGREEMENT CONFIRMATION VIEW ---

class ResultConfirmationView(View):
    def __init__(self, match_view, claiming_leader_id, opponent_leader_id, claimed_winner_label, is_claim_win):
        super().__init__(timeout=180)
        self.match_view = match_view
        self.claiming_leader_id = claiming_leader_id
        self.opponent_leader_id = opponent_leader_id
        self.claimed_winner_label = claimed_winner_label
        self.is_claim_win = is_claim_win

    @discord.ui.button(label="✅ Agree & Confirm Result", style=discord.ButtonStyle.success)
    async def btn_agree(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.opponent_leader_id:
            await interaction.response.send_message("⚠️ Only the **Opponent Leader** can confirm this result!", ephemeral=True)
            return

        await interaction.response.defer()
        for child in self.children:
            child.disabled = True

        is_team_a_winner = (self.claimed_winner_label == "Team 1")
        winning_team = self.match_view.team_a if is_team_a_winner else self.match_view.team_b
        losing_team = self.match_view.team_b if is_team_a_winner else self.match_view.team_a

        for pid in winning_team:
            if pid in self.match_view.subbed_in_players or pid not in self.match_view.initial_players:
                add_win_to_user(interaction.guild, pid, points_to_add=5)
            else:
                add_win_to_user(interaction.guild, pid, points_to_add=10)

        for pid in losing_team:
            add_loss_to_user(pid)

        all_screenshots = self.match_view.t1_screenshots + self.match_view.t2_screenshots
        match_id = save_match_history(self.match_view.mode, self.match_view.t1_leader, self.match_view.t2_leader, self.match_view.team_a, self.match_view.team_b, self.claimed_winner_label, all_screenshots)
        await log_match_to_admin_channel(interaction.guild, match_id, self.match_view.mode, self.match_view.t1_leader, self.match_view.t2_leader, self.match_view.team_a, self.match_view.team_b, self.claimed_winner_label, all_screenshots)

        msg_text = f"🎉 **RESULT CONFIRMED!** Both Leaders agreed. **{self.claimed_winner_label}** is official winner!"
        await interaction.message.edit(content=msg_text, view=self)
        await self.match_view.close_match_channels(interaction, msg_text)

    @discord.ui.button(label="❌ Disagree (Cancel Match)", style=discord.ButtonStyle.danger)
    async def btn_disagree(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.opponent_leader_id:
            await interaction.response.send_message("⚠️ Only the **Opponent Leader** can respond to this request!", ephemeral=True)
            return

        await interaction.response.defer()
        guild = interaction.guild
        category = self.match_view.category

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False, connect=False),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, connect=True, manage_channels=True)
        }
        for pid in self.match_view.match_players:
            m = guild.get_member(pid)
            if m:
                overwrites[m] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        cancel_channel = await guild.create_text_channel("cancel-match", category=category, overwrites=overwrites)

        embed = discord.Embed(
            title="⚠️ MATCH DISPUTED / CANCELLED",
            description=(
                f"🚨 <@{self.opponent_leader_id}> **disagreed** with the result submitted by <@{self.claiming_leader_id}>!\n\n"
                "Please present your proof/arguments here. An Admin will review the uploaded screenshots and decide.\n\n"
                "⏳ **This channel will auto-delete in 5 minutes.**"
            ),
            color=discord.Color.red()
        )
        await cancel_channel.send(content=" ".join([f"<@{p}>" for p in self.match_view.match_players]), embed=embed)

        dispute_ch = discord.utils.get(guild.text_channels, name=DISPUTE_CHANNEL_NAME)
        if dispute_ch:
            await dispute_ch.send(f"@everyone 🚨 **NEW DISPUTE IN {cancel_channel.mention}!** Result was rejected by {interaction.user.mention}.")

        for pid in self.match_view.match_players.union(self.match_view.initial_players):
            active_match_players.discard(pid)

        for child in self.children:
            child.disabled = True
        await interaction.message.edit(content="⚠️ **Result Rejected!** Match moved to cancel-match for Admin review. Other match channels auto-deleted.", view=self)

        async def cleanup_disagree_channels():
            await asyncio.sleep(5)
            if category:
                for ch in list(category.channels):
                    if ch.id != cancel_channel.id:
                        try:
                            await ch.delete()
                        except Exception as e:
                            print(f"Error deleting match channel: {e}")

            await asyncio.sleep(295)
            try:
                if cancel_channel:
                    await cancel_channel.delete()
                if category and len(category.channels) == 0:
                    await category.delete()
            except Exception as e:
                print(f"Error deleting cancel channel or category: {e}")

        bot.loop.create_task(cleanup_disagree_channels())

# --- ACTIVE MATCH VIEW ---

class ActiveMatchView(View):
    def __init__(self, mode, match_players, category, team_a, team_b, t1_leader, t2_leader, text_channel, initial_players):
        super().__init__(timeout=None)
        self.mode = mode
        self.match_players = set(match_players)
        self.category = category
        self.category_id = category.id if category else None
        self.team_a = list(team_a)
        self.team_b = list(team_b)
        self.t1_leader = t1_leader
        self.t2_leader = t2_leader
        self.text_channel = text_channel
        self.initial_players = set(initial_players)
        self.subbed_in_players = set()
        
        self.room_votes = {}
        self.total_rooms_agreed = None
        
        self.t1_screenshots = []
        self.t2_screenshots = []

        self.btn_claim_win.disabled = True
        self.btn_confirm_loss.disabled = True

    def get_status_embed(self):
        v1 = f"✅ `{self.room_votes[self.t1_leader]} Rooms`" if self.t1_leader in self.room_votes else "⏳ *Pending*"
        v2 = f"✅ `{self.room_votes[self.t2_leader]} Rooms`" if self.t2_leader in self.room_votes else "⏳ *Pending*"

        if self.total_rooms_agreed is None:
            rooms_status = f"**Select Match Format:** Both Leaders must select if you play 2 or 3 Rooms.\n• Leader 1 (<@{self.t1_leader}>): {v1}\n• Leader 2 (<@{self.t2_leader}>): {v2}"
        else:
            rooms_status = f"✅ **Format Confirmed:** Best of `{self.total_rooms_agreed}` Rooms."

        shots_t1 = f"🔵 **Team 1 (<@{self.t1_leader}>):** `{len(self.t1_screenshots)}/{self.total_rooms_agreed if self.total_rooms_agreed else '?'}` Uploaded"
        shots_t2 = f"🔴 **Team 2 (<@{self.t2_leader}>):** `{len(self.t2_screenshots)}/{self.total_rooms_agreed if self.total_rooms_agreed else '?'}` Uploaded"

        embed = discord.Embed(
            title=f"⚔ MATCH CONTROL PANEL ({self.mode})",
            description=(
                f"Welcome Leaders <@{self.t1_leader}> & <@{self.t2_leader}>!\n\n"
                f"{rooms_status}\n\n"
                f"📸 **Screenshots Status:**\n{shots_t1}\n{shots_t2}\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            ),
            color=discord.Color.gold()
        )
        return embed

    async def delete_category_delayed(self, guild, category_id):
        await asyncio.sleep(5)
        cat = guild.get_channel(category_id)
        if cat:
            for channel in cat.channels:
                try: await channel.delete()
                except Exception as e: print(f"Error deleting channel: {e}")
            try: await cat.delete()
            except Exception as e: print(f"Error deleting category: {e}")

    async def close_match_channels(self, interaction: discord.Interaction, result_text: str):
        for pid in self.match_players.union(self.initial_players):
            active_match_players.discard(pid)

        for child in self.children:
            child.disabled = True

        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(f"{result_text}\n\n⏳ **Channels self-destruct in 5 seconds!**")
            else:
                await interaction.followup.send(f"{result_text}\n\n⏳ **Channels self-destruct in 5 seconds!**")
        except Exception as e:
            print(f"Interaction response error: {e}")

        if self.category_id:
            bot.loop.create_task(self.delete_category_delayed(interaction.guild, self.category_id))

    async def check_rooms_agreement(self, interaction: discord.Interaction):
        if len(self.room_votes) == 2:
            v1 = self.room_votes[self.t1_leader]
            v2 = self.room_votes[self.t2_leader]

            if v1 == v2:
                self.total_rooms_agreed = v1
                self.btn_rooms_2.disabled = True
                self.btn_rooms_3.disabled = True
                await interaction.message.edit(embed=self.get_status_embed(), view=self)
                await self.text_channel.send(f"🎉 **Both Leaders agreed to play {v1} Rooms!** Both Leaders please click **📸 Upload Screenshots** to upload your result screenshots.")
            else:
                self.room_votes.clear()
                await interaction.message.edit(embed=self.get_status_embed(), view=self)
                await self.text_channel.send("⚠ **Conflict:** Team Leaders selected different number of rooms! Please discuss in chat and vote again.")
        else:
            await interaction.message.edit(embed=self.get_status_embed(), view=self)

    def check_and_enable_win_loss_buttons(self):
        if self.total_rooms_agreed and len(self.t1_screenshots) >= self.total_rooms_agreed and len(self.t2_screenshots) >= self.total_rooms_agreed:
            self.btn_claim_win.disabled = False
            self.btn_confirm_loss.disabled = False
            return True
        return False

    @discord.ui.button(label="2️⃣ Played 2 Rooms", style=discord.ButtonStyle.primary)
    async def btn_rooms_2(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id not in [self.t1_leader, self.t2_leader]:
            await interaction.response.send_message("⚠️ Only **Team Leaders** can use this panel!", ephemeral=True)
            return
        await interaction.response.defer()
        self.room_votes[interaction.user.id] = 2
        await self.check_rooms_agreement(interaction)

    @discord.ui.button(label="3️⃣ Played 3 Rooms", style=discord.ButtonStyle.primary)
    async def btn_rooms_3(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id not in [self.t1_leader, self.t2_leader]:
            await interaction.response.send_message("⚠️ Only **Team Leaders** can use this panel!", ephemeral=True)
            return
        await interaction.response.defer()
        self.room_votes[interaction.user.id] = 3
        await self.check_rooms_agreement(interaction)

    @discord.ui.button(label="📸 Upload Screenshots", style=discord.ButtonStyle.secondary)
    async def btn_upload_screenshot(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id not in [self.t1_leader, self.t2_leader]:
            await interaction.response.send_message("⚠️ Only **Team Leaders** can upload match screenshots!", ephemeral=True)
            return

        if not self.total_rooms_agreed:
            await interaction.response.send_message("⚠️ Please select match format (2 or 3 Rooms) first!", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        is_t1 = (interaction.user.id == self.t1_leader)
        target_list = self.t1_screenshots if is_t1 else self.t2_screenshots

        needed = self.total_rooms_agreed - len(target_list)
        if needed <= 0:
            await interaction.followup.send("✅ You have already uploaded all required screenshots!", ephemeral=True)
            return

        embed = discord.Embed(
            title="📸 UPLOAD MATCH SCREENSHOTS",
            description=(
                f"{interaction.user.mention}, please upload **{needed}** screenshot(s) now in this chat!\n\n"
                f"📊 **Your Progress:** `{len(target_list)}/{self.total_rooms_agreed}` Uploaded."
            ),
            color=discord.Color.blue()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

        def check(m):
            return m.author.id == interaction.user.id and m.channel.id == self.text_channel.id and len(m.attachments) > 0

        try:
            while len(target_list) < self.total_rooms_agreed:
                msg = await bot.wait_for('message', timeout=120.0, check=check)
                for attachment in msg.attachments:
                    target_list.append(attachment.url)
                
                if len(target_list) < self.total_rooms_agreed:
                    await self.text_channel.send(
                        f"📥 **Screenshot Received!** ({len(target_list)}/{self.total_rooms_agreed}). "
                        f"Upload **{self.total_rooms_agreed - len(target_list)}** more!"
                    )
                else:
                    ready = self.check_and_enable_win_loss_buttons()
                    try:
                        await interaction.message.edit(embed=self.get_status_embed(), view=self)
                    except:
                        pass

                    if ready:
                        await self.text_channel.send(
                            f"✅ **{interaction.user.mention} successfully uploaded all screenshots!**\n"
                            "🔓 ** Both Teams uploaded screenshots! WIN/LOSS buttons are now UNLOCKED!**"
                        )
                    else:
                        await self.text_channel.send(
                            f"✅ **{interaction.user.mention} successfully uploaded all screenshots!**\n"
                            "⏳ Waiting for opponent Team Leader to upload screenshots..."
                        )
                    break
        except asyncio.TimeoutError:
            await self.text_channel.send("⚠️ **Timeout:** Upload timed out. Click **📸 Upload Screenshots** to try again.")

    @discord.ui.button(label="🏆 Claim Win", style=discord.ButtonStyle.success)
    async def btn_claim_win(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id not in [self.t1_leader, self.t2_leader]:
            await interaction.response.send_message("⚠️ Only **Team Leaders** can submit match results!", ephemeral=True)
            return

        is_team_a = (interaction.user.id in self.team_a)
        claimed_winner = "Team 1" if is_team_a else "Team 2"
        opponent_leader = self.t2_leader if is_team_a else self.t1_leader

        await interaction.response.defer()
        
        confirm_view = ResultConfirmationView(self, interaction.user.id, opponent_leader, claimed_winner, is_claim_win=True)
        embed = discord.Embed(
            title="⚠️ CONFIRM MATCH RESULT",
            description=(
                f"Leader {interaction.user.mention} claimed **WIN** for **{claimed_winner}**!\n\n"
                f"<@{opponent_leader}>, please verify if this result is correct and click **Agree** below."
            ),
            color=discord.Color.gold()
        )
        await self.text_channel.send(content=f"<@{opponent_leader}>", embed=embed, view=confirm_view)

    @discord.ui.button(label="💀 Confirm Loss", style=discord.ButtonStyle.danger)
    async def btn_confirm_loss(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id not in [self.t1_leader, self.t2_leader]:
            await interaction.response.send_message("⚠️ Only **Team Leaders** can submit match results!", ephemeral=True)
            return

        is_team_a = (interaction.user.id in self.team_a)
        claimed_winner = "Team 2" if is_team_a else "Team 1"
        opponent_leader = self.t2_leader if is_team_a else self.t1_leader

        await interaction.response.defer()

        confirm_view = ResultConfirmationView(self, interaction.user.id, opponent_leader, claimed_winner, is_claim_win=False)
        embed = discord.Embed(
            title="⚠️ CONFIRM MATCH RESULT",
            description=(
                f"Leader {interaction.user.mention} submitted **LOSS** (Winner: **{claimed_winner}**)!\n\n"
                f"<@{opponent_leader}>, please verify if this result is correct and click **Agree** below."
            ),
            color=discord.Color.gold()
        )
        await self.text_channel.send(content=f"<@{opponent_leader}>", embed=embed, view=confirm_view)

    @discord.ui.button(label="🔄 Substitute Player", style=discord.ButtonStyle.primary)
    async def btn_substitute(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id not in [self.t1_leader, self.t2_leader]:
            await interaction.response.send_message("⚠️ Only **Team Leaders** can manage substitutions!", ephemeral=True)
            return

        leader_team = self.team_a if interaction.user.id in self.team_a else self.team_b
        select_view = SubstituteSelectView(self, leader_team, is_ready_phase=False)
        await interaction.response.send_message("Select player to substitute:", view=select_view, ephemeral=True)

    @discord.ui.button(label="🚨 Call Admin", style=discord.ButtonStyle.danger)
    async def btn_dispute(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id not in [self.t1_leader, self.t2_leader]:
            await interaction.response.send_message("⚠️ Only **Team Leaders** can Call Admin!", ephemeral=True)
            return

        await interaction.response.defer()
        guild = interaction.guild

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False, connect=False),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, connect=True, manage_channels=True)
        }
        for pid in self.match_players:
            m = guild.get_member(pid)
            if m:
                overwrites[m] = discord.PermissionOverwrite(read_messages=True, send_messages=True, connect=True)

        admin_voice = await guild.create_voice_channel("🚨 Call Admin", category=self.category, overwrites=overwrites)
        admin_channels_set.add(admin_voice.id)

        for pid in self.match_players:
            m = guild.get_member(pid)
            if m and m.voice and m.voice.channel:
                try:
                    await m.move_to(admin_voice)
                except Exception as e:
                    print(f"Error moving user {pid}: {e}")

        dispute_ch = discord.utils.get(guild.text_channels, name=DISPUTE_CHANNEL_NAME)
        if dispute_ch:
            embed = discord.Embed(
                title="🚨 CALL ADMIN DISPUTE 🚨",
                description=(
                    f"**Match Mode:** `{self.mode}`\n"
                    f"**Triggered by Leader:** {interaction.user.mention}\n"
                    f"**Voice Channel:** {admin_voice.mention}"
                ),
                color=discord.Color.red()
            )
            await dispute_ch.send(content="@everyone 🚨 **NEW DISPUTE!**", embed=embed)

        category = interaction.guild.get_channel(self.category_id)
        if category:
            for channel in category.channels:
                if channel.id != admin_voice.id:
                    try: await channel.delete()
                    except: pass

        for pid in self.match_players.union(self.initial_players):
            active_match_players.discard(pid)

# --- AUTO DELETE CALL ADMIN VOICE WHEN EMPTY ---

@bot.event
async def on_voice_state_update(member, before, after):
    if before.channel and before.channel.id in admin_channels_set:
        channel = before.channel
        if len(channel.members) == 0:
            admin_channels_set.discard(channel.id)
            try:
                category = channel.category
                await channel.delete()
                if category and len(category.channels) == 0:
                    await category.delete()
            except Exception as e:
                print(f"Error auto-deleting admin channel: {e}")

# --- READY CHECK VIEW ---

class ReadyCheckView(View):
    def __init__(self, mode, team_a, team_b, t1_leader, t2_leader, category, text_channel, voice_a, voice_b):
        super().__init__(timeout=300)
        self.mode = mode
        self.team_a = team_a
        self.team_b = team_b
        self.t1_leader = t1_leader
        self.t2_leader = t2_leader
        self.category = category
        self.text_channel = text_channel
        self.voice_a = voice_a
        self.voice_b = voice_b
        self.all_players = team_a + team_b
        self.ready_players = set()
        self.end_time = int(time.time()) + 300

    def update_embed(self):
        ready_list = "\n".join([f"✅ <@{p}>" if p in self.ready_players else f"⏳ <@{p}> (Waiting...)" for p in self.all_players])
        embed = discord.Embed(
            title="🎯 MATCH READY CHECK 🎯",
            description=(
                f"**Mode:** `{self.mode} Bet`\n"
                f"⏱ **Time Remaining:** <t:{self.end_time}:R>\n\n"
                f"All players click **Ready** before timer expires!\n\n"
                f"**Status:**\n{ready_list}"
            ),
            color=discord.Color.blue()
        )
        return embed

    async def on_timeout(self):
        for pid in self.all_players:
            active_match_players.discard(pid)
            if pid not in self.ready_players:
                record_not_ready(pid)

        for child in self.children: child.disabled = True

        try:
            await self.text_channel.send("⏰ **Match Cancelled:** Timer expired! Unready players received a penalty strike.")
        except:
            pass

        await asyncio.sleep(5)
        if self.category:
            for ch in self.category.channels:
                try: await ch.delete()
                except: pass
            try: await self.category.delete()
            except: pass

    @discord.ui.button(label="✅ Ready", style=discord.ButtonStyle.success)
    async def btn_ready(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer()
        if interaction.user.id not in self.all_players:
            await interaction.followup.send("⚠️ You are not part of this match!", ephemeral=True)
            return

        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.followup.send("⚠️ **You MUST be in a Voice Channel** in this server to click Ready!", ephemeral=True)
            return

        self.ready_players.add(interaction.user.id)

        if len(self.ready_players) == len(self.all_players):
            self.stop()
            for child in self.children: child.disabled = True
            await interaction.message.edit(embed=self.update_embed(), view=self)

            guild = interaction.guild
            for pid in self.team_a:
                m = guild.get_member(pid)
                if m and m.voice:
                    try: await m.move_to(self.voice_a)
                    except: pass

            for pid in self.team_b:
                m = guild.get_member(pid)
                if m and m.voice:
                    try: await m.move_to(self.voice_b)
                    except: pass

            initial_players = list(self.all_players)
            match_view = ActiveMatchView(self.mode, self.all_players, self.category, self.team_a, self.team_b, self.t1_leader, self.t2_leader, self.text_channel, initial_players)
            await self.text_channel.send(content=f"<@{self.t1_leader}> <@{self.t2_leader}>", embed=match_view.get_status_embed(), view=match_view)
        else:
            await interaction.message.edit(embed=self.update_embed(), view=self)

    @discord.ui.button(label="❌ Not Ready", style=discord.ButtonStyle.danger)
    async def btn_not_ready(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer()
        if interaction.user.id not in self.all_players:
            await interaction.followup.send("⚠️ You are not part of this match!", ephemeral=True)
            return

        record_not_ready(interaction.user.id)
        self.stop()
        for pid in self.all_players:
            active_match_players.discard(pid)

        for child in self.children: child.disabled = True
        await interaction.message.edit(content=f"❌ **Match Cancelled:** {interaction.user.mention} clicked Not Ready.", view=self)

        await asyncio.sleep(5)
        if self.category:
            for ch in self.category.channels:
                try: await ch.delete()
                except: pass
            try: await self.category.delete()
            except: pass

    @discord.ui.button(label="🔄 Substitute Player", style=discord.ButtonStyle.primary)
    async def btn_substitute_ready(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id not in [self.t1_leader, self.t2_leader]:
            await interaction.response.send_message("⚠ Only **Team Leaders** can substitute players!", ephemeral=True)
            return

        leader_team = self.team_a if interaction.user.id in self.team_a else self.team_b
        select_view = SubstituteSelectView(self, leader_team, is_ready_phase=True)
        await interaction.response.send_message("Choose which player from your team you want to substitute during Ready Check:", view=select_view, ephemeral=True)

# --- LEADER APPROVAL & MATCHSTART ---

class LeaderApprovalView(View):
    def __init__(self, applicant_id, mode, team_key, main_message):
        super().__init__(timeout=60)
        self.applicant_id = applicant_id
        self.mode = mode
        self.team_key = team_key
        self.main_message = main_message

    @discord.ui.button(label="✅ Accept", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer()
        data = team_lobbies[self.mode][self.team_key]
        limit = TEAM_LIMITS[self.mode]
        current_count = (1 if data["leader"] else 0) + len(data["members"])

        if current_count >= limit:
            await interaction.followup.send("❌ Team is already full!", ephemeral=True)
            return

        data["members"].append(self.applicant_id)
        await interaction.followup.send(f"✅ You accepted <@{self.applicant_id}> into your team!")
        for child in self.children: child.disabled = True
        await interaction.message.edit(view=self)
        if self.main_message:
            await check_and_start_match(self.main_message, self.mode)

    @discord.ui.button(label="❌ Reject", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer()
        await interaction.followup.send(f"🔴 You rejected <@{self.applicant_id}>'s request.")
        for child in self.children: child.disabled = True
        await interaction.message.edit(view=self)

async def check_and_start_match(message: discord.Message, mode: str):
    global match_counter
    try:
        data = team_lobbies[mode]
        limit = TEAM_LIMITS[mode]

        t1_count = (1 if data["team1"]["leader"] else 0) + len(data["team1"]["members"])
        t2_count = (1 if data["team2"]["leader"] else 0) + len(data["team2"]["members"])

        if t1_count == limit and t2_count == limit:
            team_a = [data["team1"]["leader"]] + data["team1"]["members"]
            team_b = [data["team2"]["leader"]] + data["team2"]["members"]
            all_players = team_a + team_b

            t1_leader = data["team1"]["leader"]
            t2_leader = data["team2"]["leader"]

            team_lobbies[mode] = {"team1": {"leader": None, "members": []}, "team2": {"leader": None, "members": []}}

            for pid in all_players:
                active_match_players.add(pid)
                if pid in queue_tasks:
                    queue_tasks[pid].cancel()
                    del queue_tasks[pid]

            guild = message.guild
            category_name = f"⚔ MATCH #{match_counter} ({mode})"
            match_counter += 1

            l1_member = guild.get_member(t1_leader)
            l2_member = guild.get_member(t2_leader)

            category_overwrites = {
                guild.default_role: discord.PermissionOverwrite(read_messages=False, connect=False),
                guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, connect=True, manage_channels=True)
            }
            for pid in all_players:
                m = guild.get_member(pid)
                if m: category_overwrites[m] = discord.PermissionOverwrite(read_messages=True, connect=True)

            category = await guild.create_category(category_name, overwrites=category_overwrites)

            text_overwrites = {
                guild.default_role: discord.PermissionOverwrite(read_messages=False, send_messages=False),
                guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True)
            }
            if l1_member: text_overwrites[l1_member] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
            if l2_member: text_overwrites[l2_member] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

            text_channel = await guild.create_text_channel("match-chat", category=category, overwrites=text_overwrites)

            voice_a = await guild.create_voice_channel("🔵 Team 1 Voice", category=category)
            voice_b = await guild.create_voice_channel("🔴 Team 2 Voice", category=category)

            ready_view = ReadyCheckView(mode, team_a, team_b, t1_leader, t2_leader, category, text_channel, voice_a, voice_b)
            
            for pid in all_players:
                m = guild.get_member(pid)
                if m: await text_channel.set_permissions(m, read_messages=True, send_messages=True)

            await text_channel.send(content=" ".join([f"<@{p}>" for p in all_players]), embed=ready_view.update_embed(), view=ready_view)

        embed = create_matchmaking_embed()
        if message:
            await message.edit(embed=embed)
    except Exception as e:
        print(f"Error in check_and_start_match: {e}")
        traceback.print_exc()

class MatchmakingView(View):
    def __init__(self):
        super().__init__(timeout=None)

    async def join_team(self, interaction: discord.Interaction, mode: str, team_key: str):
        try:
            message = interaction.message
            user_id = interaction.user.id

            if not interaction.user.voice or not interaction.user.voice.channel:
                await interaction.response.send_message("⚠️ **You MUST be connected to a Voice Channel** in this server to join matchmaking!", ephemeral=True)
                return

            banned, msg = is_user_banned(user_id)
            if banned:
                await interaction.response.send_message(msg, ephemeral=True)
                return

            if user_id in active_match_players:
                await interaction.response.send_message("⚠ You are currently in an active match or queue!", ephemeral=True)
                return

            data = team_lobbies[mode][team_key]

            if data["leader"] is None:
                data["leader"] = user_id
                await interaction.response.send_message(f"👑 You are now the **Team Leader** for **{team_key.upper()}** ({mode})!", ephemeral=True)
                embed = create_matchmaking_embed()
                if message:
                    await message.edit(embed=embed)
                await check_and_start_match(message, mode)
                return

            if user_id == data["leader"] or user_id in data["members"]:
                await interaction.response.send_message("⚠️ You are already in this team!", ephemeral=True)
                return

            leader_id = data["leader"]
            leader_user = interaction.guild.get_member(leader_id)

            if leader_user:
                approval_view = LeaderApprovalView(user_id, mode, team_key, message)
                await interaction.response.send_message("⏳ Request sent to the **Team Leader** for approval!", ephemeral=True)
                await leader_user.send(f"🔔 **Team Request ({mode}):** {interaction.user.mention} wants to join your team!", view=approval_view)
            else:
                await interaction.response.send_message("❌ Team Leader was not found in this server.", ephemeral=True)

        except Exception as e:
            print(f"❌ ERROR in join_team: {e}")
            traceback.print_exc()

    @discord.ui.button(label="🔵 1v1 (Team 1)", style=discord.ButtonStyle.primary, custom_id="mm_1v1_t1")
    async def btn_1v1_t1(self, interaction: discord.Interaction, button: Button):
        await self.join_team(interaction, "1vs1", "team1")

    @discord.ui.button(label="🔴 1v1 (Team 2)", style=discord.ButtonStyle.danger, custom_id="mm_1v1_t2")
    async def btn_1v1_t2(self, interaction: discord.Interaction, button: Button):
        await self.join_team(interaction, "1vs1", "team2")

    @discord.ui.button(label="🔵 2v2 (Team 1)", style=discord.ButtonStyle.primary, custom_id="mm_2v2_t1")
    async def btn_2v2_t1(self, interaction: discord.Interaction, button: Button):
        await self.join_team(interaction, "2vs2", "team1")

    @discord.ui.button(label="🔴 2v2 (Team 2)", style=discord.ButtonStyle.danger, custom_id="mm_2v2_t2")
    async def btn_2v2_t2(self, interaction: discord.Interaction, button: Button):
        await self.join_team(interaction, "2vs2", "team2")

    @discord.ui.button(label="🔵 3v3 (Team 1)", style=discord.ButtonStyle.primary, custom_id="mm_3v3_t1")
    async def btn_3v3_t1(self, interaction: discord.Interaction, button: Button):
        await self.join_team(interaction, "3vs3", "team1")

    @discord.ui.button(label="🔴 3v3 (Team 2)", style=discord.ButtonStyle.danger, custom_id="mm_3v3_t2")
    async def btn_3v3_t2(self, interaction: discord.Interaction, button: Button):
        await self.join_team(interaction, "3vs3", "team2")

    @discord.ui.button(label="🔵 4v4 (Team 1)", style=discord.ButtonStyle.primary, custom_id="mm_4v4_t1")
    async def btn_4v4_t1(self, interaction: discord.Interaction, button: Button):
        await self.join_team(interaction, "4vs4", "team1")

    @discord.ui.button(label="🔴 4v4 (Team 2)", style=discord.ButtonStyle.danger, custom_id="mm_4v4_t2")
    async def btn_4v4_t2(self, interaction: discord.Interaction, button: Button):
        await self.join_team(interaction, "4vs4", "team2")

    @discord.ui.button(label="❌ Cancel / Leave", style=discord.ButtonStyle.secondary, custom_id="mm_cancel")
    async def btn_cancel(self, interaction: discord.Interaction, button: Button):
        try:
            message = interaction.message
            user_id = interaction.user.id
            removed = False

            for mode, data in team_lobbies.items():
                for t in ["team1", "team2"]:
                    if data[t]["leader"] == user_id:
                        data[t]["leader"] = None
                        if data[t]["members"]:
                            data[t]["leader"] = data[t]["members"].pop(0)
                        removed = True
                    elif user_id in data[t]["members"]:
                        data[t]["members"].remove(user_id)
                        removed = True

            if removed:
                await interaction.response.send_message("🔴 You left the queue.", ephemeral=True)
                embed = create_matchmaking_embed()
                if message:
                    await message.edit(embed=embed)
            else:
                await interaction.response.send_message("⚠️ You are not in any queue.", ephemeral=True)
        except Exception as e:
            print(f"Error in cancel: {e}")

# --- 🔄 AUTOMATIC LEADERBOARD TASK (EVERY 10 MINUTES) ---

@tasks.loop(minutes=10)
async def auto_post_leaderboard():
    for guild in bot.guilds:
        lb_channel = discord.utils.get(guild.text_channels, name=LEADERBOARD_CHANNEL_NAME)
        if not lb_channel:
            continue

        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("""
                SELECT user_id, wins, losses, points,
                       CASE 
                           WHEN (wins + losses) = 0 THEN 0.0
                           ELSE ROUND((wins * 100.0) / (wins + losses), 1)
                       END as win_rate
                FROM stats
                WHERE (wins + losses) >= 1
                ORDER BY wins DESC, points DESC, win_rate DESC
                LIMIT 10
            """)
            top_players = cursor.fetchall()
            conn.close()

            if not top_players:
                continue

            embed = discord.Embed(
                title="🏆 CHEMICAL B — AUTOMATIC LEADERBOARD UPDATE",
                description="Updates automatically every 10 minutes!",
                color=discord.Color.gold(),
                timestamp=datetime.now()
            )
            lb_text = ""
            for idx, (uid, wins, losses, pts, wr) in enumerate(top_players, 1):
                medal = "🥇" if idx == 1 else "🥈" if idx == 2 else "🥉" if idx == 3 else f"**#{idx}**"
                lb_text += f"{medal} <@{uid}> — **{wins}** Wins | **{pts}** PTS | **{wr}%** WR ({losses} L)\n"

            embed.add_field(name="📊 Top Players", value=lb_text, inline=False)
            embed.set_footer(text="CHEMICAL B Ranking System")

            await lb_channel.send(embed=embed)
        except Exception as e:
            print(f"Error sending auto leaderboard: {e}")

# --- BOT EVENTS & COMMANDS ---

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name}")
    bot.add_view(MatchmakingView())  # Εξασφαλίζει ότι τα κουμπιά δουλεύουν μετά από restart
    if not auto_post_leaderboard.is_running():
        auto_post_leaderboard.start()

@bot.command()
@commands.has_permissions(administrator=True)
async def setup(ctx):
    embed = create_matchmaking_embed()
    view = MatchmakingView()
    await ctx.send(embed=embed, view=view)

@bot.command()
@commands.has_permissions(manage_messages=True)
async def matchinfo(ctx, match_id: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT mode, team1_leader, team2_leader, team1_players, team2_players, winner_team, screenshots, timestamp FROM matches WHERE match_id = ?", (match_id,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        await ctx.send(f"❌ Match ID `#{match_id}` not found in database.")
        return

    mode, t1_leader, t2_leader, t1_p, t2_p, winner, screenshots, ts = row
    t1_list = ", ".join([f"<@{p}>" for p in t1_p.split(",") if p])
    t2_list = ", ".join([f"<@{p}>" for p in t2_p.split(",") if p])
    shot_links = "\n".join([f"🖼️ [Screenshot {i+1}]({url})" for i, url in enumerate(screenshots.split(",")) if url])

    embed = discord.Embed(
        title=f"🔎 MATCH DETAILS #{match_id}",
        description=f"**Mode:** {mode}\n**Date/Time:** {ts}\n**Winner:** {winner}",
        color=discord.Color.blue()
    )
    embed.add_field(name="🔵 Team 1", value=f"Leader: <@{t1_leader}>\nPlayers: {t1_list}", inline=False)
    embed.add_field(name="🔴 Team 2", value=f"Leader: <@{t2_leader}>\nPlayers: {t2_list}", inline=False)
    embed.add_field(name="📸 Uploaded Screenshots", value=shot_links if shot_links else "No Screenshots Saved", inline=False)

    await ctx.send(embed=embed)

@bot.command()
@commands.has_permissions(administrator=True)
async def setwin(ctx, leader: discord.Member):
    add_win_to_user(ctx.guild, leader.id, 10)
    await ctx.send(f"✅ Admin manually set WIN for {leader.mention}.")

@bot.command()
@commands.has_permissions(administrator=True)
async def setloss(ctx, leader: discord.Member):
    add_loss_to_user(leader.id)
    await ctx.send(f"💀 Admin manually set LOSS for {leader.mention}.")

@bot.command()
async def leaderboard(ctx):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT user_id, wins, losses, points,
               CASE 
                   WHEN (wins + losses) = 0 THEN 0.0
                   ELSE ROUND((wins * 100.0) / (wins + losses), 1)
               END as win_rate
        FROM stats
        WHERE (wins + losses) >= 1
        ORDER BY wins DESC, points DESC, win_rate DESC
        LIMIT 10
    """)
    top_players = cursor.fetchall()
    conn.close()

    if not top_players:
        await ctx.send("🏆 **Leaderboard:** No stats recorded yet!")
        return

    embed = discord.Embed(title="🏆 CHEMICAL B — TOP 10 LEADERBOARD", color=discord.Color.gold())
    lb_text = ""
    for idx, (uid, wins, losses, pts, wr) in enumerate(top_players, 1):
        medal = "🥇" if idx == 1 else "🥈" if idx == 2 else "🥉" if idx == 3 else f"**#{idx}**"
        lb_text += f"{medal} <@{uid}> — **{wins}** Wins | **{pts}** PTS | **{wr}%** WR | **{losses}** Losses\n"

    embed.description = lb_text
    await ctx.send(embed=embed)

if TOKEN:
    bot.run(TOKEN)
else:
    print("❌ ERROR: DISCORD_TOKEN is missing in .env file!")
