import asyncio
from datetime import datetime
from threading import Thread
import discord
from discord.ext import commands
from flask import Flask

# ==========================================
# 1. FLASK KEEP ALIVE WORKAROUND (Για Render)
# ==========================================
app = Flask('')


@app.route('/')
def home():
  return "Bot is active and running on Render!"


def run_flask():
  app.run(host='0.0.0.0', port=8080)


def keep_alive():
  t = Thread(target=run_flask)
  t.start()


# ==========================================
# 2. DISCORD BOT CONFIGURATION & SETUP
# ==========================================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


# ==========================================
# 3. MATCH VIEW ME 5-MINUTE TIMEOUT & LOGS
# ==========================================
class ActiveMatchView(discord.ui.View):

  def __init__(self, match_id: str, timeout: float = 300.0):
    super().__init__(timeout=timeout)
    self.match_id = match_id
    self.screenshot_count = 0  # Βελτιστοποίηση μνήμης: Counters αντί για λίστες URLs

  async def on_timeout(self):
    # Αυτόματη καταγραφή logs και καθαρισμός μετά από 5 λεπτά αδράνειας
    print(f"Match {self.match_id} timed out after 5 minutes of inactivity.")

    # Εντοπισμός του καναλιού match-logs
    log_channel = discord.utils.get(self.message.guild.text_channels,
                                    name="match-logs")
    if log_channel:
      embed = discord.Embed(
          title=f" Match Inactivity Log - ID: {self.match_id}",
          description="Το αγώνας έκλεισε αυτόματα λόγω αδράνειας 5 λεπτών.",
          color=discord.Color.red(),
          timestamp=datetime.utcnow(),
      )
      embed.add_field(
          name="Screenshots Processed",
          value=str(self.screenshot_count),
          inline=True,
      )
      embed.set_footer(text="Automated Match System • Render Hosted")
      await log_channel.send(embed=embed)

    # Απενεργοποίηση των κουμπιών στη θέαση
    for child in self.children:
      child.disabled = True

    try:
      await self.message.edit(view=self)
    except Exception as e:
      print(f"Error updating timeout message: {e}")


# ==========================================
# 4. BOT EVENTS & COMMANDS
# ==========================================
@bot.event
async def on_ready():
  print(f"Logged in as {bot.user.name} ({bot.user.id})")
  print("Matchmaking & Leaderboard Bot is Online!")


@bot.command()
async def startmatch(ctx, match_id: str):
  view = ActiveMatchView(match_id=match_id)
  msg = await ctx.send(
      f"Match **#{match_id}** ξεκίνησε! Πατήστε τα κουμπιά παρακάτω.",
      view=view,
  )
  view.message = msg


# ==========================================
# 5. START SERVER AND BOT
# ==========================================
import os

if __name__ == "__main__":
    keep_alive()
    TOKEN = os.getenv('DISCORD_TOKEN')
    bot.run(TOKEN)
