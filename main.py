import discord
from discord.ext import commands

import asyncio
import os
import logging
import traceback
import sys
import json
import aiohttp
import time
import urllib.parse

intents = discord.Intents.none()
intents.guilds = True
logger = logging.getLogger('discord.bot')
base_url = os.getenv('BASE_URL', 'https://vps.suchmeme.nl/print')
bot_token = os.getenv('BOT_TOKEN')
if bot_token is None:
    raise Exception('BOT_TOKEN env var not set')

class MyClient(discord.Client):
    def __init__(self, *, intents: discord.Intents):
        super().__init__(intents=intents)
        self.tree = discord.app_commands.CommandTree(self)
        self.print_group = discord.app_commands.Group(name='print', description='Manage the queue for 3d printer files')
        self.tree.add_command(self.print_group)

    async def setup_hook(self):
        await self.tree.sync()

bot = MyClient(intents=intents)

CHANNEL_MAPPINGS = {}
CACHE = {}

def add_channel_mapping(channel : str, mapping : str):
    global CHANNEL_MAPPINGS
    CHANNEL_MAPPINGS[channel] = mapping

    if not os.path.exists('data'):
        os.mkdir('data')

    with open('data/mappings.json', 'w') as fp:
        json.dump(CHANNEL_MAPPINGS, fp)

def load_channel_mappings():
    global CHANNEL_MAPPINGS

    if not os.path.exists('data/mappings.json'):
        return

    with open('data/mappings.json', 'r') as fp:
        CHANNEL_MAPPINGS = json.load(fp)

load_channel_mappings()

async def get_channel_mapping(channel : str, channel_name : str | None = None) -> str:
    global CHANNEL_MAPPINGS

    if channel not in CHANNEL_MAPPINGS:
        async with aiohttp.ClientSession() as session:
            async with session.post(base_url + '/Saved', json={
                'CollectionName': channel_name if channel_name is not None else channel
            }) as response:
                if response.status != 200:
                    raise Exception('Got non-200 status code in get_channel_mapping()')

                token = await response.text()
                add_channel_mapping(channel, token)

    return CHANNEL_MAPPINGS[channel]

async def uid_embed(uid : str, color : int = 0x0000FF) -> discord.Embed:
    async with aiohttp.ClientSession() as session:
        async with session.get(base_url + f'/Posts/universal/{uid}') as response: 
            if response.status != 200:
                raise Exception(f'Request failed! {response.status}')

            data = await response.json()

    embed = discord.Embed(colour=color, title=data['name'][:60], url=data['website'])
    if data['thumbnail'] is not None or data['thumbnail']['url'] is not None:
        embed.set_image(url=data['thumbnail']['url'])

    embed.set_author(name=data['author']['name'], url=data['author']['website'], icon_url=data['author']['thumbnail']['url'])
    return embed

async def uid_download_embed(uid : str, color : int = 0x00FF00) -> discord.Embed:
    async with aiohttp.ClientSession() as session:
        async with session.get(base_url + f'/Posts/universal/{uid}') as response: 
            if response.status != 200:
                raise Exception(f'Request failed! {response.status}')

            data = await response.json()

    def generate_addons(x) -> str:
        addons = []
        if (uid.startswith('prusa-printables:')):
            addons.append(f"([PrusaSlicer]({base_url + '/Hacks/prusa?url=' + urllib.parse.quote(x['url'])}))")

        return ' '.join(addons)

    embed = discord.Embed(colour=color, title=data['name'][:60], url=data['website'])

    embed.add_field(name='Downloads', value='\n'.join(
        [f"- [{x['name']}]({x['url']}) {generate_addons(x)}" for x in data['downloads']]))

    embed.set_author(name=data['author']['name'], url=data['author']['website'], icon_url=data['author']['thumbnail']['url'])
    return embed

async def get_prints_from_token(token : str, invalidate_cache : bool = False) -> dict:
    global CACHE

    if token in CACHE and not invalidate_cache and (time.time() < (CACHE[token]['time'] + 3600)):
        print('CACHE HIT!')
        return CACHE[token]['data']

    async with aiohttp.ClientSession() as session:
        async with session.get(base_url + f'/Saved/{token}') as response: 
            if response.status != 200:
                raise Exception(f'Request failed! {response.status}')

            data = await response.json()

    CACHE[token] = {
        'time': time.time(),
        'data': [{
            'uid': x['universalId'],
            'name': x['name'],
            'url': x['website'],
            'image': x['thumbnail']['url'],
            'author': x['author']['name']
        } for x in data['posts'][::-1]]
    }

    return CACHE[token]['data']

async def posts_autocomplete(interaction: discord.Interaction, current: str) -> list[discord.app_commands.Choice[str]]:
    token = await get_channel_mapping(str(interaction.user.id), interaction.user.name)
    items = await get_prints_from_token(token)
    try:
        result = [
            discord.app_commands.Choice(name=x['name'], value=x['uid'])
            for x in items if current.lower() in x['name'].lower()
        ]
        print(result)
    except Exception as e:
        result = []
        print(str(e))

    return result

@bot.print_group.command(name='complete', description='Mark a print as completed')
@discord.app_commands.autocomplete(uid=posts_autocomplete)
async def print_complete_command(interaction: discord.Interaction, uid : str, show_in_channel : bool = False):
    await print_complete(interaction, uid, show_in_channel)

async def print_complete(interaction: discord.Interaction, uid : str, show_in_channel : bool = False):
    await interaction.response.defer(ephemeral=not show_in_channel)
    token = await get_channel_mapping(str(interaction.user.id), interaction.user.name)

    async with aiohttp.ClientSession() as session:
        async with session.delete(base_url + f'/Saved/{token}/remove', json={
            'UID': uid
        }) as response: 
            if response.status != 200:
                raise Exception('Failed to complete post')

    asyncio.create_task(get_prints_from_token(token, True))

    embed = await uid_embed(uid, 0xFF0000)
    await interaction.followup.send('Print completed, removed from queue', embed=embed, ephemeral=not show_in_channel)

class InteractButton(discord.ui.View):
    def __init__(self, uid : str, user_id: int):
        super().__init__(timeout=86400)
        self.uid = uid
        self.user_id = user_id
        self.completed = False

    @discord.ui.button(label='Complete', style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.Button):
        if (interaction.user.id != self.user_id):
            await interaction.response.send_message('This button belongs to someone else.', ephemeral=True)
            return

        if (self.completed):
            await interaction.response.send_message('This print has already been completed.', ephemeral=True)
            return

        await print_complete(interaction, self.uid)
        self.completed = True

    @discord.ui.button(label='Download', style=discord.ButtonStyle.primary)
    async def list_downloads(self, interaction : discord.Interaction, button : discord.Button):
        await interaction.response.defer(ephemeral=True)
        embed = await uid_download_embed(self.uid)
        await interaction.followup.send(embed=embed, ephemeral=True)

@bot.print_group.command(name='add', description='Add a 3d print URL to the queue')
async def print_add(interaction: discord.Interaction, url : str, show_in_channel : bool = False):
    url = url.strip()
    await interaction.response.defer(ephemeral=not show_in_channel)
    token = await get_channel_mapping(str(interaction.user.id), interaction.user.name)

    uid = None
    if (url.startswith('https://www.thingiverse.com/thing:')):
        uid = f"thingiverse:{url.split(':')[-1]}"
    elif (url.startswith("https://www.myminifactory.com/object/")):
        uid = f"myminifactory:{url.split('-')[-1]}"
    elif (url.startswith("https://www.printables.com/model")):
        uid = f"prusa-printables:{url.split('/')[-1].split('-')[0]}"

    if uid == None:
        await interaction.followup.send(f'URL was not recognised', ephemeral=True)
        return
    
    success = True

    async with aiohttp.ClientSession() as session:
        async with session.post(base_url + f'/Saved/{token}/add', json={
            'UID': uid
        }) as response: 
            success = response.status == 200
            fail = await response.text()
            print(fail, response.status)

    asyncio.create_task(get_prints_from_token(token, True))
    view = InteractButton(uid, interaction.user.id)

    if success:
        embed = await uid_embed(uid)
        if not show_in_channel:
            embed.set_footer(text=f'API Code: {token}')

        await interaction.followup.send('Done!', embed=embed, view=view, ephemeral=not show_in_channel)
    else:
        await interaction.followup.send(f'Failed! {fail}', ephemeral=True)

@bot.print_group.command(name='list', description='List current 3d print files in queue')
@discord.app_commands.autocomplete(print_name=posts_autocomplete)
async def print_list(interaction: discord.Interaction, print_name : str = None, show_in_channel : bool = False):
    await interaction.response.defer(ephemeral=not show_in_channel)
    
    token = await get_channel_mapping(str(interaction.user.id), interaction.user.name)
    items = await get_prints_from_token(token)

    if (print_name != None):
        embed = await uid_embed(print_name)

        if not show_in_channel:
            embed.set_footer(text=f'API Code: {token}')

        view = InteractButton(print_name, interaction.user.id)
        await interaction.followup.send(embed=embed, view=view, ephemeral=not show_in_channel)
        return

    embed = discord.Embed(title='Queued Items', color=0xFFFF00, description='\n'.join(
        f"- [{x['name']}]({x['url']})" for x in items
    ))

    if not show_in_channel:
        embed.set_footer(text=f'API Code: {token}')

    await interaction.followup.send(embed=embed, ephemeral=not show_in_channel)

@bot.print_group.command(name='help', description='Shows the help message of this bot')
async def print_help(interaction : discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    token = await get_channel_mapping(str(interaction.user.id), interaction.user.name)
    embed = discord.Embed(title='3D Print Queue Bot', color=0x00FF00, description='This bot allows you to manage a queue of 3d prints')
    embed.add_field(name='Commands', value='\n'.join(
        f'`/print {x.name}` - {x.description}' for x in bot.print_group.commands
    ), inline=False)

    embed.add_field(name='API Code', value=f'You can programatically interact with your stored prints via [your share code]({base_url + "/Saved/" + token}).\nNote that anyone can edit your stored prints using the share code.\nDo not share this with others.', inline=False)
    embed.add_field(name='Supported sites', value="Thingiverse, MyMiniFactory and Printables are supported by this bot. If you want more sites to be added, please see the contact info below.", inline=False)
    embed.add_field(name='Contact', value='If you have any questions, please contact [suchmememanyskill on Discord](https://discord.com/users/249186838592487425)\nThe source code of the bot can be found [on Github](https://github.com/suchmememanyskill/3d-print-queue-discord-bot).', inline=False)

    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.event
async def on_ready():
    logger.info(f'Logged in as {bot.user} (ID: {bot.user.id})')
    logger.info('------')

bot.run(bot_token)
