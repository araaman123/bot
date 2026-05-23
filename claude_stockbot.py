import os
import asyncio
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv
from anthropic import Anthropic
import aiohttp
from pathlib import Path
from dotenv import load_dotenv

env_path = Path(__file__).resolve().parent / '.env'
load_dotenv(dotenv_path=env_path)
GUILD = discord.Object(id=int(os.getenv("DISCORD_GUILD_ID")))

# Financial API Configuration
FINNHUB_KEY = os.getenv("FINNHUB_KEY")
FINNHUB_BASE_URL = "https://finnhub.io/api/v1"
HOUSE_WATCHER_URL = "https://house-stock-watcher-data.s3-us-west-2.amazonaws.com/data/all_transactions.json"
class StockBot(discord.Client):
    def __init__(self, *, intents: discord.Intents):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.claude = Anthropic()
        self.posted_trades_cache = set()
        self.session = None  # Persistent non-blocking session placeholder

    async def setup_hook(self):
        # Create a single global persistent HTTP session when the bot boots
        self.session = aiohttp.ClientSession()
        self.tree.copy_global_to(guild=GUILD)
        await self.tree.sync(guild=GUILD)

    async def close(self):
        # Gracefully shut down connection channels on stop
        if self.session:
            await self.session.close()
        await super().close()

    async def on_ready(self):
        print(f"Bot online as {self.user}")
        if not self.fetch_insider_trades.is_running():
            self.fetch_insider_trades.start()

    # --- PROCESS ENGINE: Core Scraper Logic ---
    async def process_and_post_trades(self, force_channel=None):
        """Shared tracking logic used by both the background loop and testing commands"""
        ALERT_CHANNEL_ID = int(os.getenv("ALERT_CHANNEL_ID", "0"))
        channel = force_channel or self.get_channel(ALERT_CHANNEL_ID)
        if not channel:
            print("Target alert channel could not be identified.")
            return

        async with self.session.get(HOUSE_WATCHER_URL, timeout=10) as response:
            if response.status != 200:
                print(f"Failed to pull disclosure logs. Status: {response.status}")
                return
            all_trades = await response.json()
        
        # Guard against malformed historical arrays
        if not all_trades or not isinstance(all_trades, list):
            return

        latest_trades = all_trades[:3]
        for trade in latest_trades:
            trade_id = f"{trade.get('representative')}_{trade.get('ticker')}_{trade.get('transaction_date')}"
            
            # If manually forced via command, we ignore the cache check to guarantee visual testing works
            if force_channel is None and trade_id in self.posted_trades_cache:
                continue

            prompt = (
                f"Analyze this congressional trade disclosure. "
                f"Representative: {trade.get('representative')}. "
                f"Ticker: {trade.get('ticker')} | Type: {trade.get('type')} | Amount: {trade.get('amount')}. "
                f"Identify what committees this politician sits on or what major legislation they influence. "
                f"Provide a 2-sentence 'Context Alert' explaining if this trade creates a conflict of interest or aligns with their legislative oversight."
            )

            # Creating the message via executor prevents blocking inside an async function
            loop = asyncio.get_event_loop()
            msg = await loop.run_in_executor(
                None, 
                lambda: self.claude.messages.create(
                    model="claude-haiku-4-5",
                    max_tokens=150,
                    messages=[{"role": "user", "content": prompt}]
                )
            )
            ai_analysis = msg.content[0].text

            embed = discord.Embed(title="🚨 Politician Insider Trade Flagged", color=discord.Color.dark_red())
            embed.add_field(name="Politician", value=trade.get('representative', 'Unknown'), inline=True)
            embed.add_field(name="Asset", value=trade.get('ticker', 'N/A'), inline=True)
            embed.add_field(name="Action", value=f"{trade.get('type', 'Unknown')} ({trade.get('amount', 'N/A')})", inline=True)
            embed.add_field(name="Claude Policy Analysis", value=ai_analysis, inline=False)
            
            await channel.send(embed=embed)
            self.posted_trades_cache.add(trade_id)

    # --- BACKGROUND TASK: Schedule execution ---
    @tasks.loop(minutes=15)
    async def fetch_insider_trades(self):
        try:
            await self.process_and_post_trades()
        except Exception as e:
            print(f"Background task execution failed: {e}")

# Initialize client container
intents = discord.Intents.default()
client = StockBot(intents=intents)

# --- INTERACTIVE COMMAND: Ask Claude ---
@client.tree.command(name="askclaude", description="Ask Claude a question")
async def askclaude(interaction: discord.Interaction, question: str):
    await interaction.response.defer()
    
    loop = asyncio.get_event_loop()
    msg = await loop.run_in_executor(
        None,
        lambda: client.claude.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1024,
            messages=[{"role": "user", "content": question}]
        )
    )
    await interaction.followup.send(msg.content[0].text)

# --- INTERACTIVE COMMAND: Forced Manual Smoke-Test Scraper ---
@client.tree.command(name="test_insider", description="Force run the politician insider trading tracker immediately for evaluation.")
async def test_insider(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        await client.process_and_post_trades(force_channel=interaction.channel)
        await interaction.followup.send("✅ Test executed successfully. Check this channel for output cards.")
    except Exception as e:
        await interaction.followup.send(f"❌ Test command execution failed: {str(e)}")

@client.tree.command(name="p", description="Get real-time market data and AI momentum overview.")
async def price_and_momentum(interaction: discord.Interaction, ticker: str):
    await interaction.response.defer()
    ticker = ticker.upper()

    try:
        # Construct the Finnhub core quote URL using your new key
        url = f"{FINNHUB_BASE_URL}/quote?symbol={ticker}&token={FINNHUB_KEY}"
        
        async with client.session.get(url) as response:
            if response.status != 200:
                await interaction.followup.send(f"❌ Market data provider returned HTTP status: {response.status}")
                return
            data = await response.json()

        # Finnhub Schema Mappings:
        # c = current price, d = net change, dp = percent change
        price = data.get("c", 0)
        change = data.get("d", 0)
        change_percent = data.get("dp", 0)

        # Finnhub returns 0 for all metrics if the symbol is completely invalid or restricted
        if price == 0:
            await interaction.followup.send(
                f"❌ Unable to process symbol lookup tracking for `{ticker}`. "
                f"Verify the symbol or check your bot's environment configurations."
            )
            return

        # Hand off the clean numerical metrics to Claude for situational analysis
        momentum_prompt = (
            f"Analyze the market momentum for asset ticker {ticker}. "
            f"Current Trading Price: ${price} (Moved {change:+} or {change_percent:.2f}% today). "
            f"Write a sharp 2-sentence investment health assessment explaining how this recent market velocity maps to macro trends."
        )

        loop = asyncio.get_event_loop()
        msg = await loop.run_in_executor(
            None,
            lambda: client.claude.messages.create(
                model="claude-haiku-4-5",
                max_tokens=150,
                messages=[{"role": "user", "content": momentum_prompt}]
            )
        )
        momentum_summary = msg.content[0].text

        # Build UI Dashboard Card
        embed = discord.Embed(
            title=f"📊 Market Dashboard: {ticker}",
            color=discord.Color.green() if change >= 0 else discord.Color.red()
        )
        embed.add_field(name="Last Price", value=f"${price:,.2f}", inline=True)
        embed.add_field(name="Daily Change", value=f"{change:+.2f} ({change_percent:+.2f}%)", inline=True)
        embed.add_field(name="Claude Momentum Summary", value=momentum_summary, inline=False)
        embed.set_footer(text="Live quote streaming provided by Finnhub Core Engine")

        await interaction.followup.send(embed=embed)

    except Exception as e:
        await interaction.followup.send(f"System encountered a processing error: {str(e)}")

if __name__ == "__main__":
    client.run(os.getenv("DISCORD_TOKEN"))