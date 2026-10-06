import asyncio
import os
from threading import Thread
import discord
from discord.ext import commands
from discord.ui import Button, Select, View
from flask import Flask

# ==========================================
# 1. FLASK WEB SERVER (Για Render / Uptime)
# ==========================================
app = Flask("")


@app.route("/")
def home():
    return "Matchmaking Bot is Online and Healthy!"


def run_flask():
    app.run(host="0.0.0.0", port=8080)


def keep_alive():
    t = Thread(target=run_flask)
    t.daemon = True
    t.start()


# ==========================================
# 2. DISCORD BOT SETUP
# ==========================================
DISPUTE_CHANNEL_NAME = "match-logs"

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

active_match_players = set()
admin_channels_set = set()


# ==========================================
# 3. HELPER VIEWS & UTILITIES
# ==========================================
class SubstituteSelectView(View):
    def __init__(self, match_view, leader_team, is_ready_phase=False):
        super().__init__(timeout=60)
        self.match_view = match_view
        self.leader_team = leader_team
        self.is_ready_phase = is_ready_phase

        options = [
            discord.SelectOption(
                label=f"Player ID: {pid}", value=str(pid)
            )
            for pid in leader_team
        ]

        select = Select(
            placeholder="Επίλεξε παίκτη για αντικατάσταση...",
            options=options,
        )
        select.callback = self.select_callback
        self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        selected_pid = int(interaction.data["values"][0])
        await interaction.response.send_message(
            f"🔄 Ο παίκτης <@{selected_pid}> επιλέχθηκε για αντικατάσταση. "
            "Στείλε το mention του νέου παίκτη στο κανάλι!",
            ephemeral=True,
        )


class ResultConfirmationView(View):
    def __init__(
        self, match_view, claimer_id, opponent_leader_id, claimed_winner, is_claim_win
    ):
        super().__init__(timeout=180)
        self.match_view = match_view
        self.claimer_id = claimer_id
        self.opponent_leader_id = opponent_leader_id
        self.claimed_winner = claimed_winner
        self.is_claim_win = is_claim_win

    @discord.ui.button(label="✅ Agree", style=discord.ButtonStyle.success)
    async def btn_agree(
        self, interaction: discord.Interaction, button: Button
    ):
        if interaction.user.id != self.opponent_leader_id:
            await interaction.response.send_message(
                "⚠️ Μόνο ο αντίπαλος Leader μπορεί να επιβεβαιώσει το αποτέλεσμα!",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        # Καθαρισμός παικτών από το ενεργό σετ
        for pid in self.match_view.match_players.union(
            self.match_view.initial_players
        ):
            active_match_players.discard(pid)

        for child in self.children:
            child.disabled = True

        await interaction.message.edit(view=self)

        embed = discord.Embed(
            title="🏆 MATCH FINISHED & CONFIRMED",
            description=f"🎉 **Winner:** `{self.claimed_winner}`!\n\nΤα κανάλια θα διαγραφούν σε 10 δευτερόλεπτα.",
            color=discord.Color.green(),
        )
        await self.match_view.text_channel.send(embed=embed)

        await asyncio.sleep(10)
        if self.match_view.category_id:
            bot.loop.create_task(
                self.match_view.delete_category_delayed(
                    interaction.guild, self.match_view.category_id
                )
            )

    @discord.ui.button(label="❌ Dispute / Reject", style=discord.ButtonStyle.danger)
    async def btn_dispute(
        self, interaction: discord.Interaction, button: Button
    ):
        if interaction.user.id != self.opponent_leader_id:
            await interaction.response.send_message(
                "⚠️ Μόνο ο αντίπαλος Leader μπορεί να αμφισβητήσει το αποτέλεσμα!",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "🚨 Το αποτέλεσμα αμφισβητήθηκε! Πατήστε το κουμπί **🚨 Call Admin** για να ειδοποιηθεί moderator.",
            ephemeral=False,
        )


# ==========================================
# 4. MAIN MATCH CONTROL PANEL VIEW
# ==========================================
class ActiveMatchView(View):
    def __init__(
        self,
        mode,
        match_players,
        category,
        team_a,
        team_b,
        t1_leader,
        t2_leader,
        text_channel,
        initial_players,
    ):
        super().__init__(timeout=300)
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

        self.room_votes = {}
        self.total_rooms_agreed = None

        self.t1_screenshot_count = 0
        self.t2_screenshot_count = 0

        self.btn_claim_win.disabled = True
        self.btn_confirm_loss.disabled = True

    async def on_timeout(self):
        for pid in self.match_players.union(self.initial_players):
            active_match_players.discard(pid)

        for child in self.children:
            child.disabled = True

        try:
            await self.text_channel.send(
                "⏰ **MATCH TIMEOUT:** No action was taken for 5 minutes!\n"
                "🚨 **Match cancelled due to inactivity.** Channels will self-destruct in 10 seconds."
            )
        except Exception as e:
            print(f"Timeout message error: {e}")

        guild = self.text_channel.guild
        logs_ch = discord.utils.get(
            guild.text_channels, name=DISPUTE_CHANNEL_NAME
        )
        if logs_ch:
            log_embed = discord.Embed(
                title="⏰ MATCH EXPIRED & DELETED (INACTIVITY)",
                description=(
                    f"**Mode:** `{self.mode}`\n"
                    f"**Leader 1:** <@{self.t1_leader}>\n"
                    f"**Leader 2:** <@{self.t2_leader}>\n"
                    f"**Reason:** Match cancelled due to **5 minutes of inactivity**."
                ),
                color=discord.Color.orange(),
            )
            try:
                await logs_ch.send(embed=log_embed)
            except Exception as e:
                print(f"Failed to send timeout log: {e}")

        await asyncio.sleep(10)
        if self.category_id:
            bot.loop.create_task(
                self.delete_category_delayed(guild, self.category_id)
            )

    def get_status_embed(self):
        v1 = (
            f"✅ `{self.room_votes[self.t1_leader]} Rooms`"
            if self.t1_leader in self.room_votes
            else "⏳ *Pending*"
        )
        v2 = (
            f"✅ `{self.room_votes[self.t2_leader]} Rooms`"
            if self.t2_leader in self.room_votes
            else "⏳ *Pending*"
        )

        if self.total_rooms_agreed is None:
            rooms_status = (
                f"**Select Match Format:** Both Leaders must select if you play 2 or 3 Rooms.\n"
                f"• Leader 1 (<@{self.t1_leader}>): {v1}\n"
                f"• Leader 2 (<@{self.t2_leader}>): {v2}"
            )
        else:
            rooms_status = f"✅ **Format Confirmed:** Best of `{self.total_rooms_agreed}` Rooms."

        shots_t1 = f"🔵 **Team 1 (<@{self.t1_leader}>):** `{self.t1_screenshot_count}/{self.total_rooms_agreed if self.total_rooms_agreed else '?'}` Uploaded"
        shots_t2 = f"🔴 **Team 2 (<@{self.t2_leader}>):** `{self.t2_screenshot_count}/{self.total_rooms_agreed if self.total_rooms_agreed else '?'}` Uploaded"

        embed = discord.Embed(
            title=f"⚔ MATCH CONTROL PANEL ({self.mode})",
            description=(
                f"Welcome Leaders <@{self.t1_leader}> & <@{self.t2_leader}>!\n\n"
                f"{rooms_status}\n\n"
                f"📸 **Screenshots Status:**\n{shots_t1}\n{shots_t2}\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            ),
            color=discord.Color.gold(),
        )
        return embed

    async def delete_category_delayed(self, guild, category_id):
        cat = guild.get_channel(category_id)
        if cat:
            for channel in cat.channels:
                try:
                    await channel.delete()
                except Exception as e:
                    print(f"Error deleting channel: {e}")
            try:
                await cat.delete()
            except Exception as e:
                print(f"Error deleting category: {e}")

    async def check_rooms_agreement(self, interaction: discord.Interaction):
        if len(self.room_votes) == 2:
            v1 = self.room_votes[self.t1_leader]
            v2 = self.room_votes[self.t2_leader]

            if v1 == v2:
                self.total_rooms_agreed = v1
                self.btn_rooms_2.disabled = True
                self.btn_rooms_3.disabled = True
                await interaction.message.edit(
                    embed=self.get_status_embed(), view=self
                )
                await self.text_channel.send(
                    f"🎉 **Both Leaders agreed to play {v1} Rooms!** Both Leaders please click **📸 Upload Screenshots** to upload your result screenshots."
                )
            else:
                self.room_votes.clear()
                await interaction.message.edit(
                    embed=self.get_status_embed(), view=self
                )
                await self.text_channel.send(
                    "⚠ **Conflict:** Team Leaders selected different number of rooms! Please discuss in chat and vote again."
                )
        else:
            await interaction.message.edit(
                embed=self.get_status_embed(), view=self
            )

    def check_and_enable_win_loss_buttons(self):
        if (
            self.total_rooms_agreed
            and self.t1_screenshot_count >= self.total_rooms_agreed
            and self.t2_screenshot_count >= self.total_rooms_agreed
        ):
            self.btn_claim_win.disabled = False
            self.btn_confirm_loss.disabled = False
            return True
        return False

    @discord.ui.button(
        label="2️⃣ Played 2 Rooms", style=discord.ButtonStyle.primary
    )
    async def btn_rooms_2(
        self, interaction: discord.Interaction, button: Button
    ):
        if interaction.user.id not in [self.t1_leader, self.t2_leader]:
            await interaction.response.send_message(
                "⚠️ Only **Team Leaders** can use this panel!", ephemeral=True
            )
            return
        await interaction.response.defer()
        self.room_votes[interaction.user.id] = 2
        await self.check_rooms_agreement(interaction)

    @discord.ui.button(
        label="3️⃣ Played 3 Rooms", style=discord.ButtonStyle.primary
    )
    async def btn_rooms_3(
        self, interaction: discord.Interaction, button: Button
    ):
        if interaction.user.id not in [self.t1_leader, self.t2_leader]:
            await interaction.response.send_message(
                "⚠ Only **Team Leaders** can use this panel!", ephemeral=True
            )
            return
        await interaction.response.defer()
        self.room_votes[interaction.user.id] = 3
        await self.check_rooms_agreement(interaction)

    @discord.ui.button(
        label="📸 Upload Screenshots", style=discord.ButtonStyle.secondary
    )
    async def btn_upload_screenshot(
        self, interaction: discord.Interaction, button: Button
    ):
        if interaction.user.id not in [self.t1_leader, self.t2_leader]:
            await interaction.response.send_message(
                "⚠️ Only **Team Leaders** can upload match screenshots!",
                ephemeral=True,
            )
            return

        if not self.total_rooms_agreed:
            await interaction.response.send_message(
                "⚠️ Please select match format (2 or 3 Rooms) first!",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        is_t1 = interaction.user.id == self.t1_leader
        current_count = (
            self.t1_screenshot_count if is_t1 else self.t2_screenshot_count
        )

        needed = self.total_rooms_agreed - current_count
        if needed <= 0:
            await interaction.followup.send(
                "✅ You have already uploaded all required screenshots!",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="📸 UPLOAD MATCH SCREENSHOTS",
            description=(
                f"{interaction.user.mention}, please upload **{needed}** screenshot(s) now in this chat!\n\n"
                f"📊 **Your Progress:** `{current_count}/{self.total_rooms_agreed}` Uploaded."
            ),
            color=discord.Color.blue(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

        def check(m):
            return (
                m.author.id == interaction.user.id
                and m.channel.id == self.text_channel.id
                and len(m.attachments) > 0
            )

        try:
            logs_ch = discord.utils.get(
                interaction.guild.text_channels, name=DISPUTE_CHANNEL_NAME
            )

            while (
                self.t1_screenshot_count if is_t1 else self.t2_screenshot_count
            ) < self.total_rooms_agreed:
                msg = await bot.wait_for("message", timeout=120.0, check=check)

                for attachment in msg.attachments:
                    if is_t1:
                        self.t1_screenshot_count += 1
                    else:
                        self.t2_screenshot_count += 1

                    if logs_ch:
                        log_embed = discord.Embed(
                            title="📸 MATCH SCREENSHOT LOG",
                            description=f"**User:** {interaction.user.mention}\n**Mode:** `{self.mode}`",
                            color=discord.Color.blue(),
                        )
                        log_embed.set_image(url=attachment.url)
                        try:
                            await logs_ch.send(embed=log_embed)
                        except Exception as e:
                            print(f"Failed to log screenshot: {e}")

                count_now = (
                    self.t1_screenshot_count
                    if is_t1
                    else self.t2_screenshot_count
                )
                if count_now < self.total_rooms_agreed:
                    await self.text_channel.send(
                        f"📥 **Screenshot Received!** ({count_now}/{self.total_rooms_agreed}). "
                        f"Upload **{self.total_rooms_agreed - count_now}** more!"
                    )
                else:
                    ready = self.check_and_enable_win_loss_buttons()
                    try:
                        await interaction.message.edit(
                            embed=self.get_status_embed(), view=self
                        )
                    except:
                        pass

                    if ready:
                        await self.text_channel.send(
                            f"✅ **{interaction.user.mention} successfully uploaded all screenshots!**\n"
                            "🔓 **Both Teams uploaded screenshots! WIN/LOSS buttons are now UNLOCKED!**"
                        )
                    else:
                        await self.text_channel.send(
                            f"✅ **{interaction.user.mention} successfully uploaded all screenshots!**\n"
                            "⏳ Waiting for opponent Team Leader to upload screenshots..."
                        )
                    break
        except asyncio.TimeoutError:
            await self.text_channel.send(
                "⚠️️ **Timeout:** Upload timed out. Click **📸 Upload Screenshots** to try again."
            )

    @discord.ui.button(
        label="🏆 Claim Win", style=discord.ButtonStyle.success
    )
    async def btn_claim_win(
        self, interaction: discord.Interaction, button: Button
    ):
        if interaction.user.id not in [self.t1_leader, self.t2_leader]:
            await interaction.response.send_message(
                "⚠️ Only **Team Leaders** can submit match results!",
                ephemeral=True,
            )
            return

        is_team_a = interaction.user.id in self.team_a
        claimed_winner = "Team 1" if is_team_a else "Team 2"
        opponent_leader = self.t2_leader if is_team_a else self.t1_leader

        await interaction.response.defer()

        confirm_view = ResultConfirmationView(
            self,
            interaction.user.id,
            opponent_leader,
            claimed_winner,
            is_claim_win=True,
        )
        embed = discord.Embed(
            title="⚠️ CONFIRM MATCH RESULT",
            description=(
                f"Leader {interaction.user.mention} claimed **WIN** for **{claimed_winner}**!\n\n"
                f"<@{opponent_leader}>, please verify if this result is correct and click **Agree** below."
            ),
            color=discord.Color.gold(),
        )
        await self.text_channel.send(
            content=f"<@{opponent_leader}>", embed=embed, view=confirm_view
        )

    @discord.ui.button(
        label="💀 Confirm Loss", style=discord.ButtonStyle.danger
    )
    async def btn_confirm_loss(
        self, interaction: discord.Interaction, button: Button
    ):
        if interaction.user.id not in [self.t1_leader, self.t2_leader]:
            await interaction.response.send_message(
                "⚠️ Only **Team Leaders** can submit match results!",
                ephemeral=True,
            )
            return

        is_team_a = interaction.user.id in self.team_a
        claimed_winner = "Team 2" if is_team_a else "Team 1"
        opponent_leader = self.t2_leader if is_team_a else self.t1_leader

        await interaction.response.defer()

        confirm_view = ResultConfirmationView(
            self,
            interaction.user.id,
            opponent_leader,
            claimed_winner,
            is_claim_win=False,
        )
        embed = discord.Embed(
            title="⚠️ CONFIRM MATCH RESULT",
            description=(
                f"Leader {interaction.user.mention} submitted **LOSS** (Winner: **{claimed_winner}**)!\n\n"
                f"<@{opponent_leader}>, please verify if this result is correct and click **Agree** below."
            ),
            color=discord.Color.gold(),
        )
        await self.text_channel.send(
            content=f"<@{opponent_leader}>", embed=embed, view=confirm_view
        )

    @discord.ui.button(
        label="🔄 Substitute Player", style=discord.ButtonStyle.primary
    )
    async def btn_substitute(
        self, interaction: discord.Interaction, button: Button
    ):
        if interaction.user.id not in [self.t1_leader, self.t2_leader]:
            await interaction.response.send_message(
                "⚠️ Only **Team Leaders** can manage substitutions!",
                ephemeral=True,
            )
            return

        leader_team = (
            self.team_a
            if interaction.user.id in self.team_a
            else self.team_b
        )
        select_view = SubstituteSelectView(
            self, leader_team, is_ready_phase=False
        )
        await interaction.response.send_message(
            "Select player to substitute:", view=select_view, ephemeral=True
        )

    @discord.ui.button(label="🚨 Call Admin", style=discord.ButtonStyle.danger)
    async def btn_dispute(
        self, interaction: discord.Interaction, button: Button
    ):
        if interaction.user.id not in [self.t1_leader, self.t2_leader]:
            await interaction.response.send_message(
                "⚠️ Only **Team Leaders** can Call Admin!", ephemeral=True
            )
            return

        await interaction.response.defer()
        guild = interaction.guild

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(
                read_messages=False, connect=False
            ),
            guild.me: discord.PermissionOverwrite(
                read_messages=True,
                send_messages=True,
                connect=True,
                manage_channels=True,
            ),
        }
        for pid in self.match_players:
            m = guild.get_member(pid)
            if m:
                overwrites[m] = discord.PermissionOverwrite(
                    read_messages=True, send_messages=True, connect=True
                )

        admin_voice = await guild.create_voice_channel(
            "🚨 Call Admin", category=self.category, overwrites=overwrites
        )
        admin_channels_set.add(admin_voice.id)

        for pid in self.match_players:
            m = guild.get_member(pid)
            if m and m.voice and m.voice.channel:
                try:
                    await m.move_to(admin_voice)
                except Exception as e:
                    print(f"Error moving user {pid}: {e}")

        dispute_ch = discord.utils.get(
            guild.text_channels, name=DISPUTE_CHANNEL_NAME
        )
        if dispute_ch:
            embed = discord.Embed(
                title="🚨 CALL ADMIN DISPUTE 🚨",
                description=(
                    f"**Match Mode:** `{self.mode}`\n"
                    f"**Triggered by Leader:** {interaction.user.mention}\n"
                    f"**Voice Channel:** {admin_voice.mention}"
                ),
                color=discord.Color.red(),
            )
            await dispute_ch.send(
                content="@everyone 🚨 **NEW DISPUTE!**", embed=embed
            )

        category = interaction.guild.get_channel(self.category_id)
        if category:
            for channel in category.channels:
                if channel.id != admin_voice.id:
                    try:
                        await channel.delete()
                    except:
                        pass

        for pid in self.match_players.union(self.initial_players):
            active_match_players.discard(pid)


# ==========================================
# 5. BOT EVENTS & COMMANDS
# ==========================================
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user.name} ({bot.user.id})")
    print(" Matchmaking & Leaderboard Bot is Online!")


@bot.command()
async def startmatch(ctx, user2: discord.Member):
    """
    Δοκιμαστική εντολή: Ξεκινάει ένα match 1v1 ανάμεσα σε εσένα και ένα μέλος.
    Παράδειγμα: !startmatch @User
    """
    team_a = [ctx.author.id]
    team_b = [user2.id]
    all_players = set(team_a + team_b)

    view = ActiveMatchView(
        mode="1v1 Test",
        match_players=all_players,
        category=ctx.channel.category,
        team_a=team_a,
        team_b=team_b,
        t1_leader=ctx.author.id,
        t2_leader=user2.id,
        text_channel=ctx.channel,
        initial_players=all_players,
    )

    await ctx.send(embed=view.get_status_embed(), view=view)


@bot.command()
async def setup(ctx):
    """Εντολή επιβεβαίωσης λειτουργίας."""
    await ctx.send("🤖 **Matchmaking Bot status:** 100% Operational!")


# ==========================================
# 6. RUNNER
# ==========================================
if __name__ == "__main__":
    keep_alive()
    TOKEN = os.getenv("DISCORD_TOKEN")
    if TOKEN:
        bot.run(TOKEN)
    else:
        print(
            "❌ ERROR: DISCORD_TOKEN is not set in Environment Variables!"
        )
