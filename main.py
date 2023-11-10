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

intents = discord.Intents.none()
intents.guilds = True
logger = logging.getLogger('discord.bot')
base_url = os.getenv('BASE_URL', 'https://vps.suchmeme.nl/print')
token = os.getenv('BOT_TOKEN')
if token is None:
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
    channel = str(interaction.channel.id)
    token = await get_channel_mapping(channel, interaction.channel.name)
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
async def print_complete_command(interaction: discord.Interaction, uid : str):
    await print_complete(interaction, uid)

async def print_complete(interaction: discord.Interaction, uid : str):
    await interaction.response.defer()

    channel = str(interaction.channel.id)
    token = await get_channel_mapping(channel, interaction.channel.name)
    items = await get_prints_from_token(token)

    async with aiohttp.ClientSession() as session:
        async with session.delete(base_url + f'/Saved/{token}/remove', json={
            'UID': uid
        }) as response: 
            if response.status != 200:
                raise Exception('Failed to complete post')

    asyncio.create_task(get_prints_from_token(token, True))

    embed = await uid_embed(uid, 0xFF0000)
    await interaction.followup.send('Print completed, removed from queue', embed=embed)

class CompleteButton(discord.ui.View):
    def __init__(self, uid : str):
        super().__init__(timeout=None)
        self.uid = uid

    @discord.ui.button(label='Complete', style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.Button):
        await print_complete(interaction, self.uid)
        self.stop()

@bot.print_group.command(name='add', description='Add a 3d print URL to the queue')
async def print_add(interaction: discord.Interaction, url : str):
    url = url.strip()
    channel = str(interaction.channel.id)
    await interaction.response.defer()
    token = await get_channel_mapping(channel, interaction.channel.name)

    uid = None
    if (url.startswith('https://www.thingiverse.com/thing:')):
        uid = f"thingiverse:{url.split(':')[-1]}"
    elif (url.startswith("https://www.myminifactory.com/object/")):
        uid = f"myminifactory:{url.split('-')[-1]}"
    elif (url.startswith("https://www.printables.com/model")):
        uid = f"prusa-printables:{url.split('/')[-1].split('-')[0]}"

    if uid == None:
        await interaction.followup.send(f'URL was not recognised')
        return
    
    success = True

    print(token)
    print(uid)

    async with aiohttp.ClientSession() as session:
        async with session.post(base_url + f'/Saved/{token}/add', json={
            'UID': uid
        }) as response: 
            success = response.status == 200
            fail = await response.text()
            print(fail, response.status)

    asyncio.create_task(get_prints_from_token(token, True))
    view = CompleteButton(uid)

    if success:
        embed = await uid_embed(uid)
        embed.set_footer(text=f'StlSpy Share Code: {token}')
        await interaction.followup.send('Done!', embed=embed, view=view)
    else:
        await interaction.followup.send(f'Failed! {fail}')

@bot.print_group.command(name='list', description='List current 3d print files in queue')
@discord.app_commands.autocomplete(uid=posts_autocomplete)
async def print_list(interaction: discord.Interaction, uid : str = None):
    await interaction.response.defer()

    channel = str(interaction.channel.id)
    token = await get_channel_mapping(channel, interaction.channel.name)
    items = await get_prints_from_token(token)

    if (uid != None):
        embed = await uid_embed(uid)
        embed.set_footer(text=f'StlSpy Share Code: {token}')
        view = CompleteButton(uid)
        await interaction.followup.send(embed=embed, view=view)
        return

    embed = discord.Embed(title='Queued Items', color=0xFFFF00, description='\n'.join(
        f"- [{x['name']}]({x['url']})" for x in items
    ))
    embed.set_footer(text=f'StlSpy Share Code: {token}')
    await interaction.followup.send(embed=embed)

@bot.event
async def on_ready():
    logger.info(f'Logged in as {bot.user} (ID: {bot.user.id})')
    logger.info('------')

bot.run(token)
